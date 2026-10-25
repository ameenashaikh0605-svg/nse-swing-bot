"""Intraday tracker: watches pending and open picks and alerts on entry, stop loss, target and breakeven.

Run every 15 to 30 minutes during market hours (09:15 to 15:30 IST).
"""

import datetime as dt

import pandas as pd

from . import config, data, state, telegram

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def process_bars(p: dict, bars: pd.DataFrame) -> list[str]:
    """Walk bars in time order and update position p in place. Returns alert messages.

    When a single bar touches both the stop and the target, the stop is assumed to come first.
    """
    alerts = []
    last = pd.Timestamp(p["last_checked"]) if p.get("last_checked") else None
    created = dt.date.fromisoformat(p["created"])
    for ts, bar in bars.iterrows():
        ts = pd.Timestamp(ts)
        if ts.date() <= created or (last is not None and ts < last):
            continue
        if p["status"] == "pending" and bar["High"] >= p["entry"]:
            fill = max(p["entry"], float(bar["Open"]))
            if fill > p["entry"] * (1 + config.MAX_CHASE_PCT):
                p.update(status="cancelled", closed=ts.isoformat(), exit_reason="gapped above entry")
                alerts.append(f"⏭ <b>{p['symbol']}</b> gapped to ₹{fill:,.2f}, more than {config.MAX_CHASE_PCT:.0%} above the ₹{p['entry']:,.2f} trigger. Pick cancelled, don't chase.")
                break
            p.update(status="open", fill=round(fill, 2), opened=ts.isoformat())
            alerts.append(f"🟢 <b>{p['symbol']}</b> entry triggered at ₹{fill:,.2f}. Stop ₹{p['stop']:,.2f}, target ₹{p['target']:,.2f}.")
        if p["status"] != "open":
            continue
        risk = p["fill"] - p["initial_stop"]
        if bar["Low"] <= p["stop"]:
            price = min(p["stop"], float(bar["Open"]))
            reason = "breakeven stop" if p["breakeven"] else "stop loss"
            _close(p, ts, price, reason)
            icon = "⚪" if p["breakeven"] else "🔴"
            alerts.append(f"{icon} <b>{p['symbol']} {reason.upper()} HIT</b> at ₹{price:,.2f}. Exit. Result {p['pnl_pct']:+.2f}% ({p['r_multiple']:+.2f}R).")
            break
        if bar["High"] >= p["target"]:
            price = max(p["target"], float(bar["Open"]))
            _close(p, ts, price, "target")
            alerts.append(f"🎯 <b>{p['symbol']} TARGET HIT</b> at ₹{price:,.2f}. Book profit. Result {p['pnl_pct']:+.2f}% ({p['r_multiple']:+.2f}R).")
            break
        if not p["breakeven"] and bar["High"] >= p["fill"] + config.BREAKEVEN_AT_R * risk:
            p.update(breakeven=True, stop=p["fill"])
            alerts.append(f"🛡 <b>{p['symbol']}</b> is up {config.BREAKEVEN_AT_R:g}R. Move stop loss to cost, ₹{p['fill']:,.2f}.")
    if len(bars):
        p["last_checked"] = max(pd.Timestamp(bars.index[-1]), last or pd.Timestamp(bars.index[-1])).isoformat()
    return alerts


def _close(p: dict, ts, price: float, reason: str) -> None:
    risk = p["fill"] - p["initial_stop"]
    p.update(
        status="closed",
        closed=pd.Timestamp(ts).isoformat(),
        exit=round(price, 2),
        exit_reason=reason,
        pnl_pct=round((price / p["fill"] - 1) * 100, 2),
        r_multiple=round((price - p["fill"]) / risk, 2) if risk > 0 else 0.0,
    )


def _sessions_since(df: pd.DataFrame, iso: str) -> int:
    d = pd.Timestamp(iso).date()
    return int(sum(pd.Timestamp(x).date() > d for x in df.index))


def end_of_day(positions: list[dict], daily: dict[str, pd.DataFrame]) -> None:
    """After the close: catch anything the intraday runs missed, expire stale picks and apply time exits."""
    alerts = []
    for p in state.active(positions):
        df = daily.get(p["symbol"])
        if df is None or df.empty:
            continue
        # Daily bars as a fallback for sessions the intraday runs never saw.
        if p.get("last_checked"):
            seen = pd.Timestamp(p["last_checked"]).date()
            df_new = df[[pd.Timestamp(x).date() > seen for x in df.index]]
        else:
            df_new = df
        p["last_checked"] = None
        alerts += process_bars(p, df_new)
        # Everything up to today's close is now accounted for.
        close_ts = pd.Timestamp(pd.Timestamp(df.index[-1]).date()) + pd.Timedelta(hours=15, minutes=31)
        p["last_checked"] = close_ts.tz_localize("Asia/Kolkata").isoformat()
        if p["status"] == "pending" and _sessions_since(df, p["created"]) >= config.ENTRY_VALID_DAYS:
            p.update(status="cancelled", closed=dt.date.today().isoformat(), exit_reason="entry not triggered")
            alerts.append(f"⌛ <b>{p['symbol']}</b> never crossed ₹{p['entry']:,.2f}. Pick expired.")
        elif p["status"] == "open" and _sessions_since(df, p["opened"]) >= config.MAX_HOLD_DAYS:
            close = float(df["Close"].iloc[-1])
            _close(p, df.index[-1], close, "time exit")
            alerts.append(f"⏱ <b>{p['symbol']}</b> time exit after {config.MAX_HOLD_DAYS} sessions at ₹{close:,.2f}. Result {p['pnl_pct']:+.2f}%.")
    if alerts:
        telegram.send("\n\n".join(alerts))


def summary(positions: list[dict], daily: dict[str, pd.DataFrame]) -> str:
    """Daily status of open positions and closed-trade stats."""
    lines = ["<b>Open positions</b>"]
    opened = [p for p in positions if p["status"] == "open"]
    for p in opened:
        df = daily.get(p["symbol"])
        last = float(df["Close"].iloc[-1]) if df is not None and len(df) else p["fill"]
        lines.append(f"{p['symbol']}: in at ₹{p['fill']:,.2f}, now ₹{last:,.2f} ({(last / p['fill'] - 1) * 100:+.2f}%), stop ₹{p['stop']:,.2f}, target ₹{p['target']:,.2f}")
    if not opened:
        lines.append("None")
    pending = [p["symbol"] for p in positions if p["status"] == "pending"]
    if pending:
        lines.append("Waiting for entry: " + ", ".join(pending))
    closed = [p for p in positions if p["status"] == "closed"]
    if closed:
        wins = sum(p["pnl_pct"] > 0 for p in closed)
        avg_r = sum(p["r_multiple"] for p in closed) / len(closed)
        lines.append(f"\nClosed trades: {len(closed)}, win rate {wins / len(closed):.0%}, average {avg_r:+.2f}R")
    return "\n".join(lines)


def run() -> None:
    now = dt.datetime.now(IST)
    if now.weekday() >= 5 or not (dt.time(9, 15) <= now.time() <= dt.time(15, 40)):
        print(f"Market closed at {now:%H:%M} IST; skipping")
        return
    positions = state.load()
    act = state.active(positions)
    if not act:
        print("No active picks")
        return
    bars = data.download_intraday(sorted({p["symbol"] for p in act}))
    alerts = []
    for p in act:
        if p["symbol"] in bars:
            alerts += process_bars(p, bars[p["symbol"]])
    state.save(positions)
    if alerts:
        telegram.send("\n\n".join(alerts) + f"\n\n<i>{config.DISCLAIMER}</i>")
    print(f"Checked {len(act)} picks, {len(alerts)} alerts")


if __name__ == "__main__":
    run()
