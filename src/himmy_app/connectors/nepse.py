"""NEPSE (Nepal Stock Exchange) prices — live OHLCV via Merolagani, keyless.

Merolagani's TradingView UDF chart handler serves a stock's daily candles directly as JSON
(no auth, no key) at ``merolagani.com/handlers/TechnicalChartHandler.ashx``. It quirkily labels
the body ``text/plain`` even though it IS JSON, so we read it with
:func:`himmy_app.connectors._net.safe_get_text` (host-pinned to ``merolagani.com``) using a
``content_ok`` that accepts ``text/plain``/``application/json`` and then ``json.loads`` the text.

Design mirrors the other connectors (Buddha Air / weather):

* **Host-pinned, guarded fetch.** Every request goes through ``_net.safe_get_text`` with
  ``allow_hosts=("merolagani.com",)`` so it inherits the SSRF / redirect / size / retry defences.
  The symbol is sanitised to ``[A-Z0-9]`` and never interpolated raw into the URL.
* **Already corp-action adjusted.** We pass ``isAdjust=1`` so the close series is bonus/rights
  adjusted at source — we do NOT re-adjust (that would double-adjust).
* **Rate-limited.** Merolagani is a small public endpoint; a process-wide async limiter spaces
  requests to ~0.5 req/s so a refresh loop can't hammer it.
* **Persisted, validated daily bars.** Good bars are written to a small SQLite store
  (``nepse_prices.db`` in the app data dir) via an atomic upsert; junk rows (high<low, non-positive
  prices, zero-volume non-trading rows) are rejected before they ever touch the DB.
* **Graceful by design.** A bad symbol or a dead upstream returns ``{"ok": False, "message", ...}``
  rather than raising, so the tool / refresh loop degrades cleanly.

Public surface: :func:`nepse_price` (the tool handler), :func:`fetch_ohlcv`,
:func:`store_bars`, :func:`load_recent`, and :class:`NepseConnector`.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._net import NetError, safe_get_text
from himmy_app.connectors._register import safe_register_local_tool

__all__ = [
    "NepseConnector",
    "nepse_price",
    "fetch_ohlcv",
    "store_bars",
    "load_recent",
    "sanitise_symbol",
]

# --- endpoint --------------------------------------------------------------------------------
_HOST = "merolagani.com"
_URL = "https://merolagani.com/handlers/TechnicalChartHandler.ashx"
#: A browser-shaped header set; the handler is picky about XHR + Referer (returns empty otherwise).
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://merolagani.com/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",
}
#: Only ``[A-Z0-9]`` survives sanitation — never interpolate raw user text into the URL.
_SYMBOL_RE = re.compile(r"[^A-Z0-9]")

# --- rate limiting ---------------------------------------------------------------------------
#: ~0.5 req/s — Merolagani is a small public endpoint; a refresh loop must not hammer it.
_MIN_INTERVAL_S = 2.0
_rate_lock = asyncio.Lock()
_last_request_at = 0.0


async def _rate_limit() -> None:
    """Block until at least ``_MIN_INTERVAL_S`` has elapsed since the previous request.

    Process-wide and serialised by a lock so concurrent callers still respect ~0.5 req/s.
    """
    global _last_request_at
    async with _rate_lock:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


# --- data dir / DB ---------------------------------------------------------------------------
def _data_dir() -> Path:
    """The app data dir (``.scholar-desk``), mirroring ``config.load_config`` without importing it.

    Kept dependency-light so this connector module imports cleanly on its own.
    """
    env = os.environ.get("HIMMY_APP_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[3] / ".scholar-desk"


def _db_path() -> Path:
    return _data_dir() / "nepse_prices.db"


def _connect() -> sqlite3.Connection:
    """Open the prices DB (creating dir + schema), with WAL + a busy timeout for the refresh loop."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_prices (
            symbol TEXT NOT NULL,
            date   TEXT NOT NULL,
            open   REAL,
            high   REAL,
            low    REAL,
            close  REAL,
            volume REAL,
            PRIMARY KEY (symbol, date)
        )
        """
    )
    return conn


# --- symbol sanitation -----------------------------------------------------------------------
def sanitise_symbol(symbol: Any) -> str:
    """Uppercase ``symbol`` and strip it to ``[A-Z0-9]`` only (empty if nothing survives).

    This is the *only* thing that ever reaches the URL, so raw user text can never be injected.
    """
    return _SYMBOL_RE.sub("", str(symbol or "").strip().upper())


# --- bar validation --------------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return f


def _valid_bar(o: Any, h: Any, l: Any, c: Any, v: Any) -> dict[str, float] | None:
    """Coerce + sanity-check one OHLCV bar; return a clean dict or None to reject it.

    Rejects: any non-numeric/NaN field, non-positive prices, ``high < low``, and zero-volume rows
    (Merolagani pads non-trading days with a zero-volume flat candle — not a real session).
    """
    of, hf, lf, cf, vf = (_to_float(x) for x in (o, h, l, c, v))
    if None in (of, hf, lf, cf, vf):
        return None
    if of <= 0 or hf <= 0 or lf <= 0 or cf <= 0:
        return None
    if hf < lf:
        return None
    if vf <= 0:  # zero-volume non-trading row — not a real session
        return None
    return {"open": of, "high": hf, "low": lf, "close": cf, "volume": vf}


def _bs_iso(d: datetime.date) -> str | None:
    """``YYYY-MM-DD`` Bikram-Sambat string for an AD date, or None if conversion is unavailable."""
    try:
        from himmy.nepal.calendar import ad_to_bs

        bs = ad_to_bs(d)
        return f"{bs.year:04d}-{bs.month:02d}-{bs.day:02d}"
    except Exception:  # noqa: BLE001 - BS date is a nicety, never fatal
        return None


# --- fetch -----------------------------------------------------------------------------------
def _chart_ct_ok(content_type: str | None) -> bool:
    """Accept the handler's ``text/plain`` body (and a real ``application/json`` if it ever sends one)."""
    if not content_type:
        return False
    main = content_type.split(";", 1)[0].strip().lower()
    return main in {"text/plain", "application/json", "text/json"} or main.endswith("+json")


async def fetch_ohlcv(symbol: str, *, days: int = 400) -> list[dict[str, Any]]:
    """Fetch ``symbol``'s adjusted daily OHLCV from Merolagani as validated, date-stamped bars.

    Args:
        symbol: A NEPSE ticker (already sanitised by the caller, or sanitised here defensively).
        days: How far back to request (the handler returns whatever sessions exist in the range).

    Returns:
        A chronologically-sorted list of ``{date, open, high, low, close, volume}`` dicts (``date``
        is the AD ``YYYY-MM-DD``). Empty list if the symbol is unknown or the upstream gave nothing
        usable. ``date`` keys are de-duplicated (last bar for a date wins).

    Raises:
        NetError: only on a true network/guard failure (so the caller can distinguish "down" from
            "no data"); an ``"s" != "ok"`` payload returns an empty list, not an error.
    """
    sym = sanitise_symbol(symbol)
    if not sym:
        return []
    end = int(time.time())
    start = end - max(1, int(days)) * 24 * 60 * 60
    params = {
        "type": "get_advanced_chart",
        "symbol": sym,
        "resolution": "1D",
        "rangeStartDate": str(start),
        "rangeEndDate": str(end),
        "isAdjust": "1",  # bonus/rights adjusted AT SOURCE — do NOT re-adjust downstream
        "currencyCode": "NPR",
    }
    await _rate_limit()
    text = await safe_get_text(
        _URL,
        params=params,
        headers=_HEADERS,
        allow_hosts=(_HOST,),
        timeout=20.0,
        max_bytes=5_000_000,
        retries=1,
        content_ok=_chart_ct_ok,
    )
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise NetError(f"invalid chart JSON ({exc.__class__.__name__})") from None
    if not isinstance(data, dict) or data.get("s") != "ok":
        # "no_data" / error status: a real but empty result, not a transport failure.
        return []

    t = data.get("t") or []
    o, h, l, c, v = (data.get(k) or [] for k in ("o", "h", "l", "c", "v"))
    n = min(len(t), len(o), len(h), len(l), len(c), len(v))
    by_date: dict[str, dict[str, Any]] = {}
    for i in range(n):
        try:
            d = datetime.datetime.utcfromtimestamp(int(t[i])).date()
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        bar = _valid_bar(o[i], h[i], l[i], c[i], v[i])
        if bar is None:
            continue
        by_date[d.isoformat()] = {"date": d.isoformat(), **bar}
    return [by_date[k] for k in sorted(by_date)]


# --- store -----------------------------------------------------------------------------------
def store_bars(symbol: str, bars: list[dict[str, Any]]) -> int:
    """Upsert ``bars`` for ``symbol`` into the prices DB; return the number of rows written.

    An EMPTY ``bars`` list is a no-op (returns 0) — we NEVER overwrite good stored data with an
    empty fetch. The write is a single transaction so a crash leaves the table consistent.
    """
    sym = sanitise_symbol(symbol)
    if not sym or not bars:
        return 0
    rows = [
        (sym, b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"])
        for b in bars
        if b.get("date")
    ]
    if not rows:
        return 0
    conn = _connect()
    try:
        with conn:  # atomic: commit on success, rollback on error
            conn.executemany(
                """
                INSERT INTO stock_prices (symbol, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume
                """,
                rows,
            )
        return len(rows)
    finally:
        conn.close()


def load_recent(symbol: str, n: int = 7) -> list[dict[str, Any]]:
    """Read the last ``n`` stored daily bars for ``symbol`` (chronological order; [] if none)."""
    sym = sanitise_symbol(symbol)
    if not sym:
        return []
    n = max(1, int(n))
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM stock_prices WHERE symbol = ?
            ORDER BY date DESC LIMIT ?
            """,
            (sym, n),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    out = [
        {"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
        for r in rows
    ]
    out.reverse()  # DESC fetch -> chronological
    return out


# --- tool handler ----------------------------------------------------------------------------
async def nepse_price(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler: latest NEPSE price + recent OHLCV for a symbol (Merolagani, NPR, adjusted).

    Args (in ``args``):
        symbol: The NEPSE ticker (e.g. ``NABIL``). Required; sanitised to ``[A-Z0-9]``.
        days: Optional lookback for the fetch (default 400); the response ``ohlcv`` is the last ~7.

    Returns the shared contract on success::

        {ok: True, symbol, price, prev_close, change, change_pct, currency: "NPR",
         ohlcv: [{date,o,h,l,c,v} ...last 7], as_of, date_bs, source: "Merolagani"}

    A bad symbol or a down upstream returns ``{ok: False, message, symbol}`` — never raises.
    """
    sym = sanitise_symbol(args.get("symbol"))
    if not sym:
        return {"ok": False, "message": "Need a stock symbol, e.g. NABIL.", "symbol": ""}
    try:
        days = int(args.get("days") or 400)
    except (TypeError, ValueError):
        days = 400

    bars: list[dict[str, Any]] = []
    try:
        bars = await fetch_ohlcv(sym, days=days)
    except NetError:
        bars = []  # upstream down: fall back to whatever we have stored
    except Exception:  # noqa: BLE001 - never let an unexpected upstream shape break the tool
        bars = []

    if bars:
        # EMPTY-READ = NO-WRITE is enforced inside store_bars (empty -> 0 rows), but only call on data.
        try:
            store_bars(sym, bars)
        except sqlite3.Error:
            pass  # persistence is best-effort; the live read is still returned

    if not bars:
        # No live data — serve the last stored bars if we have them, else a clean failure.
        bars = load_recent(sym, max(7, 2))
        if not bars:
            return {
                "ok": False,
                "symbol": sym,
                "message": (
                    f"Couldn't get a price for '{sym}' (unknown symbol or NEPSE data is "
                    "unavailable right now)."
                ),
            }

    last = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else None
    price = round(float(last["close"]), 2)
    prev_close = round(float(prev["close"]), 2) if prev else None
    change = round(price - prev_close, 2) if prev_close is not None else None
    change_pct = (
        round((change / prev_close) * 100, 2)
        if (change is not None and prev_close not in (None, 0))
        else None
    )

    as_of = str(last["date"])
    try:
        as_of_d = datetime.date.fromisoformat(as_of)
    except ValueError:
        as_of_d = datetime.date.today()
    date_bs = _bs_iso(as_of_d)

    ohlcv = [
        {
            "date": b["date"],
            "o": round(float(b["open"]), 2),
            "h": round(float(b["high"]), 2),
            "l": round(float(b["low"]), 2),
            "c": round(float(b["close"]), 2),
            "v": float(b["volume"]),
        }
        for b in bars[-7:]
    ]

    return {
        "ok": True,
        "symbol": sym,
        "price": price,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "currency": "NPR",
        "ohlcv": ohlcv,
        "as_of": as_of,
        "date_bs": date_bs,
        "source": "Merolagani",
    }


# --- connector -------------------------------------------------------------------------------
class NepseConnector:
    """Registers ``nepse_price`` — latest NEPSE stock price + recent OHLCV (Merolagani, keyless)."""

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        safe_register_local_tool(
            registry,
            name="nepse_price",
            read_only=True,
            handler=nepse_price,
            description=(
                "Get the latest NEPSE (Nepal Stock Exchange) price for a listed company by its "
                "trading SYMBOL (e.g. NABIL, NICA, HDL, NTC, UPPER). Returns the last traded price "
                "in NPR, the day's change and change %, the previous close, the Bikram-Sambat date, "
                "and the last ~7 days of OHLCV. Prices are corporate-action adjusted (bonus/rights). "
                "Pass the symbol uppercase. For an unknown symbol or when NEPSE data is unavailable "
                "it returns a clear, graceful message."
            ),
            args_json_schema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "NEPSE trading symbol, e.g. NABIL"},
                    "days": {"type": "integer", "description": "Lookback window for history (default 400)"},
                },
                "required": ["symbol"],
            },
        )
        return ["nepse_price"]
