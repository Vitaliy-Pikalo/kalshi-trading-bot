"""kalshi api client.

uses RSA signing per kalshi docs: https://trading-api.readme.io/reference/api-keys

every request signs `timestamp + method + path` with the private key.
the kalshi-python SDK handles this for us — this module wraps it with
our settings + sane defaults.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization

from src.config import settings


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

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Authenticated GET. `endpoint` is relative, e.g. '/markets'."""
        if self._client is None:
            raise RuntimeError("use as context manager: `with KalshiClient() as k:`")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        path_to_sign = self._path_for_signing(endpoint)
        headers = self._signed_headers("GET", path_to_sign)
        url = self.base_url + endpoint
        r = self._client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

    def list_markets(
        self,
        status: str = "open",
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List markets. status one of: open, closed, settled."""
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self.get("/markets", params=params)
