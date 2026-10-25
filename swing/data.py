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
                df = raw[yf_symbol(s)] if len(chunk) > 1 else raw
                if isinstance(df.columns, pd.MultiIndex):
                    df = df.droplevel(1, axis=1) if df.columns.nlevels > 1 else df
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
            df = raw[yf_symbol(s)] if len(symbols) > 1 else raw
            if isinstance(df.columns, pd.MultiIndex):
                df = df.droplevel(1, axis=1)
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
