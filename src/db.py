"""sqlite schema + session.

tables:
  markets      — one row per kalshi market we've seen
  snapshots    — time-series of bid/ask/volume per market (every 60s)
  predictions  — model output: predicted prob + edge per snapshot
  fills        — actual orders placed (paper or live)
  bankroll     — daily snapshot of total $ for drawdown tracking

run `python -m src.db init` to create the db.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import settings


class Base(DeclarativeBase):
    pass


class Market(Base):
    __tablename__ = "markets"
    ticker = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    series_ticker = Column(String, index=True)
    event_ticker = Column(String, index=True)
    open_ts = Column(DateTime)
    close_ts = Column(DateTime)
    settled_outcome = Column(String)  # 'yes' / 'no' / null if open
    first_seen = Column(DateTime, default=datetime.utcnow)
    # strike semantics — populated from kalshi response, NOT ticker parsing
    strike_type = Column(String)        # 'greater' | 'less' | 'between' | 'structured'
    floor_strike = Column(Float)        # used by greater + between
    cap_strike = Column(Float)          # used by less + between


class Snapshot(Base):
    __tablename__ = "snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, ForeignKey("markets.ticker"), index=True, nullable=False)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    yes_bid = Column(Integer)  # cents 0-100
    yes_ask = Column(Integer)
    no_bid = Column(Integer)
    no_ask = Column(Integer)
    last_price = Column(Integer)
    volume = Column(Integer)
    open_interest = Column(Integer)


class Prediction(Base):
    __tablename__ = "predictions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, ForeignKey("markets.ticker"), index=True, nullable=False)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    model_version = Column(String, nullable=False)
    predicted_prob = Column(Float, nullable=False)  # 0.0 - 1.0
    market_implied_prob = Column(Float, nullable=False)
    edge = Column(Float, nullable=False)  # predicted - implied


class Fill(Base):
    __tablename__ = "fills"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, ForeignKey("markets.ticker"), index=True, nullable=False)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    side = Column(String, nullable=False)  # 'yes' or 'no'
    price_cents = Column(Integer, nullable=False)
    contracts = Column(Integer, nullable=False)
    cost_usd = Column(Float, nullable=False)
    is_paper = Column(Integer, default=1)  # 1=paper, 0=live
    realized_pnl = Column(Float)  # filled in when market settles


class BankrollSnapshot(Base):
    __tablename__ = "bankroll"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    total_usd = Column(Float, nullable=False)
    open_position_value = Column(Float, default=0.0)
    realized_pnl_today = Column(Float, default=0.0)
    note = Column(String)


# --- engine + session ---

def _ensure_data_dir() -> None:
    """Create data/ folder if needed (sqlite needs the parent dir)."""
    if settings.database_url.startswith("sqlite:///"):
        path = Path(settings.database_url.replace("sqlite:///", ""))
        path.parent.mkdir(parents=True, exist_ok=True)


_ensure_data_dir()
engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Session:
    """Use as: `with session_scope() as s: ...`"""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    """Create all tables (idempotent)."""
    _ensure_data_dir()
    Base.metadata.create_all(engine)
    print(f"db initialized: {settings.database_url}")
    print("tables:")
    for t in Base.metadata.sorted_tables:
        print(f"  {t.name}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        init_db()
    else:
        print("usage: python -m src.db init")
