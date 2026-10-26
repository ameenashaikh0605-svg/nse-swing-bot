"""Offline tests with synthetic prices: python -m pytest tests"""

import numpy as np
import pandas as pd

from swing import screener, tracker


def make_daily(n=260, breakout=True, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=n)
    close = 100 * np.cumprod(1 + 0.002 + rng.normal(0, 0.01, n))
    if breakout:
        close[-1] = close[-21:-1].max() * 1.04
    df = pd.DataFrame({"Close": close}, index=idx)
    df["Open"] = df["Close"].shift().fillna(df["Close"])
    df["High"] = df[["Open", "Close"]].max(axis=1) * 1.005
    df["Low"] = df[["Open", "Close"]].min(axis=1) * 0.995
    df["Volume"] = 2_000_000.0
    if breakout:
        df.iloc[-1, df.columns.get_loc("Volume")] = 5_000_000.0
    return df


def test_breakout_detected():
    c = screener.evaluate("TEST", make_daily())
    assert c is not None and c["setup"] == "Breakout"
    assert c["stop"] < c["entry"] < c["target"]
    assert abs((c["target"] - c["entry"]) - 2 * (c["entry"] - c["stop"])) < 0.05


def test_no_setup_in_downtrend():
    df = make_daily(breakout=False)
    df["Close"] = df["Close"].iloc[::-1].values
    assert screener.evaluate("TEST", df) is None


def pos(**kw):
    p = {"symbol": "TEST", "created": "2026-01-05", "entry": 100.0, "stop": 95.0, "initial_stop": 95.0,
         "target": 110.0, "status": "pending", "breakeven": False, "last_checked": None}
    p.update(kw)
    return p


def bars(rows, start="2026-01-06 09:15"):
    idx = pd.date_range(start, periods=len(rows), freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)


def test_entry_then_stop():
    p = pos()
    alerts = tracker.process_bars(p, bars([(99, 99.5, 98.5, 99), (99.5, 100.5, 99, 100.2), (100, 100.1, 94.5, 95)]))
    assert p["status"] == "closed" and p["exit_reason"] == "stop loss" and p["exit"] == 95.0
    assert len(alerts) == 2 and "STOP LOSS HIT" in alerts[1]


def test_breakeven_then_target():
    p = pos()
    alerts = tracker.process_bars(p, bars([(100, 101, 99.8, 100.5), (104, 105.5, 103.8, 105), (108, 111, 107, 110.5)]))
    assert p["breakeven"] and p["status"] == "closed" and p["exit_reason"] == "target"
    assert p["r_multiple"] == 2.0 and len(alerts) == 3


def test_gap_cancels():
    p = pos()
    tracker.process_bars(p, bars([(103, 104, 102.5, 103.5)]))
    assert p["status"] == "cancelled"


def test_bars_not_reprocessed():
    p = pos()
    b = bars([(100, 101, 99.8, 100.5)])
    tracker.process_bars(p, b)
    assert p["status"] == "open"
    later = bars([(100.5, 100.8, 100.2, 100.6)], start="2026-01-06 09:25")
    tracker.process_bars(p, pd.concat([bars([(100, 100, 90, 92)], start="2026-01-06 09:10"), later]))
    assert p["status"] == "open"  # the old 09:10 bar is ignored


def test_end_of_day_expiry_and_time_exit(monkeypatch):
    monkeypatch.setattr("swing.telegram.send", lambda t: True)
    idx = pd.bdate_range("2026-01-05", periods=12)
    df = pd.DataFrame({"Open": 100.0, "High": 100.5, "Low": 99.5, "Close": 100.2, "Volume": 1e6}, index=idx)
    pend = pos(entry=120.0)
    opn = pos(status="open", fill=100.0, opened="2026-01-05T10:00:00+05:30")
    positions = [pend, opn]
    tracker.end_of_day(positions, {"TEST": df})
    assert pend["status"] == "cancelled" and opn["exit_reason"] == "time exit"


def test_format_picks():
    c = screener.evaluate("TEST", make_daily())
    msg = screener.format_picks([c], {"TEST": "Test & Co Ltd"})
    assert "Test &amp; Co" in msg and "Not financial advice" in msg


def test_intraday_volume_is_scaled():
    df = make_daily()
    df.iloc[-1, df.columns.get_loc("Volume")] = 1_600_000.0  # 0.8x a full day by 11:00
    assert screener.evaluate("TEST", df) is None
    assert screener.evaluate("TEST", df, session_fraction=0.3) is not None


def test_intraday_pick_ignores_bars_before_it():
    p = pos(created="2026-01-06", created_at="2026-01-06T11:00:00+05:30")
    b = bars([(100, 101, 99, 100.5), (99.5, 99.9, 99.2, 99.6)], start="2026-01-06 10:55")
    tracker.process_bars(p, b)
    assert p["status"] == "pending"  # the 10:55 bar crossed entry but came before the pick
    b2 = bars([(100.5, 101.2, 100.3, 101)], start="2026-01-06 11:10")
    tracker.process_bars(p, b2)
    assert p["status"] == "open"


class _NoTicker:
    actions = None

    def get_earnings_dates(self, limit=4):
        return None


def test_volume_reason(monkeypatch):
    from swing import catalysts
    monkeypatch.setattr(catalysts.yf, "Ticker", lambda s: _NoTicker())
    vr = catalysts.volume_reason("X", ["Promoter offloads 5% via block deal", "Bank of X sees ban lifted"], {}, 0.002)
    assert vr["causes"][0].startswith("Block") and vr["one_off"] and not vr["unexplained"]
    vr = catalysts.volume_reason("X", ["X Bank shares in focus"], {}, 0.02)
    assert vr["causes"] == ["Whole market moving (Nifty +2.0%)"]
    vr = catalysts.volume_reason("X", [], {}, None)
    assert vr["unexplained"]


def test_volume_line_in_message():
    c = screener.evaluate("TEST", make_daily())
    c["volume"] = {"causes": [], "one_off": False, "unexplained": True, "evidence": None}
    msg = screener.format_picks([c], {"TEST": "Test"})
    assert "Why volume is" in msg and "no news or event found" in msg
