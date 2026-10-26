"""Market data from free public sources: NSE's index list and Yahoo Finance via yfinance."""

import io
import time

import pandas as pd
import requests
import yfinance as yf

from . import config

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def load_universe() -> dict[str, str]:
    """Return {NSE symbol: company name} for the Nifty 500, or the fallback list."""
    try:
        resp = requests.get(config.UNIVERSE_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        universe = dict(zip(df["Symbol"].str.strip(), df["Company Name"].str.strip()))
        if len(universe) > 100:
            return universe
    except Exception as e:  # NSE often rejects cloud IPs
        print(f"Universe download failed ({e}); using fallback list")
    return {s: s for s in config.FALLBACK_UNIVERSE}


def yf_symbol(nse_symbol: str) -> str:
    return f"{nse_symbol}.NS"


def _extract(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """One ticker's OHLCV columns from a yf.download result, whichever column level holds the ticker.

    Raises KeyError if the ticker is missing.
    """
    if not isinstance(raw.columns, pd.MultiIndex):
        return raw
    for level in range(raw.columns.nlevels):
        if ticker in raw.columns.get_level_values(level):
            return raw.xs(ticker, level=level, axis=1)
    raise KeyError(ticker)


def download_daily(symbols: list[str], period: str = "1y", batch: int = 100) -> dict[str, pd.DataFrame]:
    """Daily OHLCV per NSE symbol. Symbols with no data are left out."""
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i : i + batch]
        raw = yf.download(
            [yf_symbol(s) for s in chunk],
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        for s in chunk:
            try:
                df = _extract(raw, yf_symbol(s))
                df = df.dropna(subset=["Close"])
                if len(df) > 0:
                    out[s] = df
            except KeyError:
                continue
        time.sleep(1)
    return out


def download_intraday(symbols: list[str], period: str = "5d", interval: str = "5m") -> dict[str, pd.DataFrame]:
    """Intraday bars per NSE symbol, indexed in IST."""
    out: dict[str, pd.DataFrame] = {}
    if not symbols:
        return out
    raw = yf.download(
        [yf_symbol(s) for s in symbols],
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    for s in symbols:
        try:
            df = _extract(raw, yf_symbol(s))
            df = df.dropna(subset=["Close"])
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df.index = df.index.tz_convert("Asia/Kolkata")
            if len(df) > 0:
                out[s] = df
        except KeyError:
            continue
    return out


def last_trading_date() -> pd.Timestamp | None:
    """Date of the latest Nifty 50 daily bar, used to detect holidays."""
    df = yf.download("^NSEI", period="5d", interval="1d", progress=False, auto_adjust=True)
    if df.empty:
        return None
    return pd.Timestamp(df.index[-1]).normalize()


def index_change() -> float | None:
    """Nifty 50's change today versus the previous close, as a fraction."""
    try:
        df = yf.download("^NSEI", period="5d", interval="1d", progress=False, auto_adjust=True)
        close = df["Close"].squeeze().dropna()
        return float(close.iloc[-1] / close.iloc[-2] - 1) if len(close) >= 2 else None
    except Exception as e:
        print(f"Nifty fetch failed: {e}")
        return None
