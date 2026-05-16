"""kalshi api client.

uses RSA signing per kalshi docs: https://trading-api.readme.io/reference/api-keys

every request signs `timestamp + method + path` with the private key.
the kalshi-python SDK handles this for us — this module wraps it with
our settings + sane defaults.

rate limiting:
    kalshi caps at ~10 req/sec for the read endpoints. we self-throttle
    to ~8 req/sec and retry once on 429 with exponential backoff.
"""
from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization

from src.config import settings

# self-throttle: minimum seconds between requests
_MIN_REQUEST_INTERVAL_S = 0.12  # ~8 req/sec
_last_request_ts = 0.0


def _throttle() -> None:
    """Sleep if needed to keep us under the rate limit."""
    global _last_request_ts
    now = time.monotonic()
    delta = now - _last_request_ts
    if delta < _MIN_REQUEST_INTERVAL_S:
        time.sleep(_MIN_REQUEST_INTERVAL_S - delta)
    _last_request_ts = time.monotonic()


@lru_cache(maxsize=1)
def _load_private_key():
    """Load RSA private key from disk, cached."""
    path = Path(settings.kalshi_private_key_path)
    if not path.exists():
        raise FileNotFoundError(
            f"kalshi private key not found at {path}. "
            "set KALSHI_PRIVATE_KEY_PATH in .env"
        )
    with path.open("rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


class KalshiClient:
    """Thin wrapper around kalshi REST API.

    For phase 0 we only need: list_markets(). More methods added as
    we move into phase 1 (snapshots) and phase 4 (orders).
    """

    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or settings.api_base_url).rstrip("/")
        self.timeout = timeout
        self._client: httpx.Client | None = None

    def __enter__(self) -> "KalshiClient":
        self._client = httpx.Client(timeout=self.timeout)
        return self

    def __exit__(self, *exc) -> None:
        if self._client:
            self._client.close()

    def _signed_headers(self, method: str, path_to_sign: str) -> dict[str, str]:
        """Build kalshi auth headers (timestamp + RSA-PSS signature).

        `path_to_sign` must be the FULL URL path including /trade-api/v2/...
        e.g. `/trade-api/v2/markets`, NOT just `/markets`.
        """
        import base64
        import time

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp_ms = str(int(time.time() * 1000))
        msg = f"{timestamp_ms}{method.upper()}{path_to_sign}".encode("utf-8")
        key = _load_private_key()
        sig = key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": settings.kalshi_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("ascii"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _path_for_signing(self, endpoint: str) -> str:
        """Extract URL path component including API version prefix."""
        from urllib.parse import urlparse

        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        # base_url is like https://demo-api.kalshi.co/trade-api/v2
        # we want /trade-api/v2 + /markets = /trade-api/v2/markets
        base_path = urlparse(self.base_url).path  # /trade-api/v2
        return base_path + endpoint

    def get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Authenticated GET. `endpoint` is relative, e.g. '/markets'.

        Self-throttles + retries on 429 with exponential backoff.
        """
        if self._client is None:
            raise RuntimeError("use as context manager: `with KalshiClient() as k:`")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        url = self.base_url + endpoint
        path_to_sign = self._path_for_signing(endpoint)

        backoff = 1.0
        for attempt in range(max_retries):
            _throttle()
            # signature is per-request (includes timestamp), so re-sign each retry
            headers = self._signed_headers("GET", path_to_sign)
            r = self._client.get(url, headers=headers, params=params)
            if r.status_code == 429 and attempt < max_retries - 1:
                # respect Retry-After if present, else exponential backoff
                wait = float(r.headers.get("Retry-After", backoff))
                time.sleep(wait)
                backoff *= 2
                continue
            r.raise_for_status()
            return r.json()
        # shouldn't reach here, but for type safety
        r.raise_for_status()
        return r.json()

    def list_markets(
        self,
        status: str | None = "open",
        limit: int = 100,
        cursor: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        tickers: str | None = None,
    ) -> dict[str, Any]:
        """List markets.

        status: open | closed | settled | None (all)
        limit: 1-1000 (kalshi caps at 1000)
        cursor: pagination token from previous response
        event_ticker / series_ticker: scope to a parent
        tickers: comma-separated ticker list to fetch specific markets
        """
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if tickers:
            params["tickers"] = tickers
        return self.get("/markets", params=params)

    def iter_markets(
        self,
        status: str | None = "open",
        page_size: int = 1000,
        max_pages: int = 50,
        **filters: Any,
    ):
        """Generator that auto-paginates through all matching markets.

        Yields one market dict at a time. Stops at max_pages safety cap.
        """
        cursor = None
        for _ in range(max_pages):
            resp = self.list_markets(
                status=status, limit=page_size, cursor=cursor, **filters
            )
            for m in resp.get("markets", []):
                yield m
            cursor = resp.get("cursor")
            if not cursor:
                return

    def get_market(self, ticker: str) -> dict[str, Any]:
        """Full detail of a single market."""
        return self.get(f"/markets/{ticker}")

    def list_events(
        self,
        status: str | None = "open",
        limit: int = 100,
        cursor: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
    ) -> dict[str, Any]:
        """List events. Events group related markets (e.g. one french open
        match has yes/no for each player as separate markets but same event).
        """
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        return self.get("/events", params=params)

    def get_event(
        self, event_ticker: str, with_nested_markets: bool = True
    ) -> dict[str, Any]:
        """Full event detail; includes child markets if with_nested_markets."""
        params: dict[str, Any] = {}
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        return self.get(f"/events/{event_ticker}", params=params)

    def list_series(
        self,
        category: str | None = None,
        include_product_metadata: bool = False,
    ) -> dict[str, Any]:
        """List series (top-level market groupings).

        category: e.g. 'Sports', 'Crypto', 'Climate', 'Politics'.
        """
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        if include_product_metadata:
            params["include_product_metadata"] = "true"
        return self.get("/series", params=params)

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        """Top-of-book + depth for a market."""
        return self.get(f"/markets/{ticker}/orderbook", params={"depth": depth})
