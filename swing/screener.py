"""Screener: finds swing setups across the Nifty 500 and posts the best ones to Telegram.

Scans run during the session (11:00 and 13:00 IST) on the day's bars so far. A run after the
close (16:00 IST) only expires stale picks, applies time exits and posts a summary.
Each pick is a buy-stop: it becomes a position only if price trades above the entry trigger.
"""

import datetime as dt
import html

import pandas as pd

from . import catalysts, config, data, indicators, state, telegram, tracker

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SESSION_OPEN, SESSION_CLOSE = dt.time(9, 15), dt.time(15, 30)


def evaluate(symbol: str, df: pd.DataFrame, session_fraction: float = 1.0) -> dict | None:
    """Return a candidate setup for one stock's daily bars, or None if nothing qualifies.

    During the session the last bar is partial, so its volume is compared with the share of a
    normal day's volume expected by now (session_fraction of the day elapsed).
    """
    if len(df) < 210:
        return None
    df = indicators.add_all(df)
    t, y = df.iloc[-1], df.iloc[-2]
    if t["Close"] < config.MIN_PRICE or t["traded_value20"] < config.MIN_AVG_TRADED_VALUE:
        return None
    uptrend = t["Close"] > t["ema50"] > t["ema200"] and t["ema50"] > df["ema50"].iloc[-10]
    if not uptrend:
        return None

    vol_ratio = t["Volume"] / (t["vol20"] * session_fraction) if t["vol20"] else 0
    setup = None
    reasons = []
    if t["Close"] > t["high20"] and vol_ratio >= 1.5 and 55 <= t["rsi"] <= 75:
        setup = "Breakout"
        reasons.append(f"Closed above 20-day high on {vol_ratio:.1f}x volume")
    else:
        touched = min(df["Low"].iloc[-3:]) <= t["ema20"] * 1.01
        if touched and t["Close"] > t["ema20"] and 40 <= t["rsi"] <= 60 and t["macd_hist"] > y["macd_hist"] and t["Close"] > y["High"] * 0.995:
            setup = "Pullback"
            reasons.append("Bounced off rising 20 EMA in an uptrend")
    if not setup:
        return None

    entry = round(t["High"] * 1.001, 2)
    risk = config.ATR_STOP_MULT * t["atr"]
    stop = round(max(entry - risk, t["low10"] * 0.995) if setup == "Pullback" else entry - risk, 2)
    risk = entry - stop
    if risk <= 0 or risk / entry > config.MAX_STOP_PCT:
        return None
    target = round(entry + config.TARGET_R_MULT * risk, 2)

    if t["macd"] > t["macd_sig"]:
        reasons.append("MACD above signal")
    reasons.append(f"RSI {t['rsi']:.0f}")

    score = (2 if setup == "Breakout" else 1.5) + min(vol_ratio, 3) / 2 + max(min(t["ret_3m"], 0.5), -0.2) * 4
    return {
        "symbol": symbol,
        "setup": setup,
        "close": round(float(t["Close"]), 2),
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
        "risk_pct": round(float(risk / entry * 100), 2),
        "vol_ratio": round(float(vol_ratio), 2),
        "score": round(float(score), 2),
        "reasons": reasons,
        "date": pd.Timestamp(df.index[-1]).date().isoformat(),
    }


def enrich(cand: dict, company: str, nifty_change: float | None = None) -> dict | None:
    """Add news and results. Returns None if results are due inside the blackout window."""
    res = catalysts.results_check(cand["symbol"])
    if res["next_earnings"]:
        days = (dt.date.fromisoformat(res["next_earnings"]) - dt.date.today()).days
        if 0 <= days <= config.EARNINGS_BLACKOUT_DAYS:
            print(f"Skip {cand['symbol']}: results on {res['next_earnings']}")
            return None
    rscore = catalysts.results_score(res)
    nscore, headlines = catalysts.news_sentiment(company)
    cand["score"] = round(cand["score"] + rscore * 0.75 + nscore * 0.5, 2)
    cand["results"] = res
    cand["news_score"] = nscore
    cand["headlines"] = headlines[:2]
    if rscore > 0:
        cand["reasons"].append(f"Results improving ({_growth_text(res)})")
    elif rscore < 0:
        cand["reasons"].append(f"Weak results ({_growth_text(res)})")
    if nscore > 0:
        cand["reasons"].append("Positive news flow")
    elif nscore < 0:
        cand["reasons"].append("Negative headlines, be careful")
    if cand["vol_ratio"] >= 1.3:
        vr = catalysts.volume_reason(cand["symbol"], headlines, res, nifty_change)
        cand["volume"] = vr
        if vr["one_off"]:
            cand["score"] = round(cand["score"] - 1, 2)
        elif vr["unexplained"]:
            cand["score"] = round(cand["score"] - 0.5, 2)
    return cand


def _volume_line(p: dict) -> str | None:
    vr = p.get("volume")
    if not vr:
        return None
    head = f"Why volume is {p['vol_ratio']:.1f}x normal: "
    if vr["unexplained"]:
        return head + "no news or event found. It could be speculative or operator buying, so be extra careful."
    text = head + ", ".join(vr["causes"])
    if vr["evidence"]:
        text += f' ("{vr["evidence"][:120]}")'
    if vr["one_off"]:
        text += ". ⚠️ This kind of volume is often a one-off and may not mean fresh buying."
    return text


def _growth_text(res: dict) -> str:
    parts = []
    if res["rev_growth"] is not None:
        parts.append(f"revenue {res['rev_growth']:+.0%}")
    if res["profit_growth"] is not None:
        parts.append(f"profit {res['profit_growth']:+.0%}")
    return f"{res['basis']} " + ", ".join(parts) if parts else "n/a"


def format_picks(picks: list[dict], universe: dict[str, str]) -> str:
    now = dt.datetime.now(IST).strftime("%d %b %Y, %H:%M IST")
    if not picks:
        return f"<b>Swing scan, {now}</b>\nNo new setups passed the filters.\n\n<i>{config.DISCLAIMER}</i>"
    lines = [f"<b>Swing picks, {now}</b>", ""]
    for i, p in enumerate(picks, 1):
        name = html.escape(universe.get(p["symbol"], p["symbol"]))
        lines += [
            f"<b>{i}. {p['symbol']}</b> · {name} · {p['setup']}",
            f"Buy above ₹{p['entry']:,.2f} (now ₹{p['close']:,.2f})",
            f"Stop loss ₹{p['stop']:,.2f} (-{p['risk_pct']}%) · Target ₹{p['target']:,.2f} (+{p['risk_pct'] * config.TARGET_R_MULT:.1f}%)",
            "Why: " + html.escape("; ".join(p["reasons"])),
        ]
        if (vl := _volume_line(p)):
            lines.append(html.escape(vl))
        if p.get("headlines") and not (p.get("volume") or {}).get("evidence"):
            lines.append("News: " + html.escape(p["headlines"][0][:140]))
        lines.append("")
    lines += [
        f"Entry valid for {config.ENTRY_VALID_DAYS} sessions. Stop moves to cost at +{config.BREAKEVEN_AT_R:g}R. "
        f"Time exit after {config.MAX_HOLD_DAYS} sessions. You will get an alert when a stop or target is hit.",
        "",
        f"<i>{config.DISCLAIMER}</i>",
    ]
    return "\n".join(lines)


def _session_fraction(now: dt.datetime) -> float:
    start = dt.datetime.combine(now.date(), SESSION_OPEN, IST)
    minutes = (now - start).total_seconds() / 60
    return min(1.0, max(0.1, minutes / 375))


def run(force: bool = False) -> None:
    now = dt.datetime.now(IST)
    last = data.last_trading_date()
    if not force and (last is None or last.date() != now.date()):
        print(f"No NSE session today (last bar {last}); skipping")
        return
    in_session = SESSION_OPEN <= now.time() < SESSION_CLOSE and last is not None and last.date() == now.date()
    eod = not force and not in_session

    universe = data.load_universe()
    print(f"Universe: {len(universe)} stocks")
    positions = state.load()

    if eod:
        daily = data.download_daily(sorted({p["symbol"] for p in state.active(positions)}))
        tracker.end_of_day(positions, daily)
        state.save(positions)
        telegram.send(tracker.summary(positions, daily))
        return

    daily = data.download_daily(list(universe))
    print(f"Downloaded {len(daily)} stocks")

    today = now.date().isoformat()
    picked_today = [p for p in positions if p.get("created_at", "").startswith(today)]
    skip = {p["symbol"] for p in state.active(positions)} | {p["symbol"] for p in picked_today}
    slots = min(
        config.MAX_OPEN_POSITIONS - len(state.active(positions)),
        config.MAX_PICKS_PER_DAY - len(picked_today),
    )
    if slots <= 0:
        print("No free slots; skipping scan")
        return
    frac = _session_fraction(now) if in_session else 1.0
    nifty_change = data.index_change()
    cands = [c for s, df in daily.items() if s not in skip and (c := evaluate(s, df, frac))]
    cands.sort(key=lambda c: c["score"], reverse=True)
    print(f"{len(cands)} technical candidates")

    picks = []
    for c in cands[: config.MAX_PICKS_PER_DAY * 3]:
        if len(picks) >= slots:
            break
        if (e := enrich(c, universe.get(c["symbol"], c["symbol"]), nifty_change)) is not None and e["score"] > 1.5:
            picks.append(e)
    picks.sort(key=lambda c: c["score"], reverse=True)

    telegram.send(format_picks(picks, universe))
    for p in picks:
        positions.append(
            {
                "id": f"{p['symbol']}-{now:%Y-%m-%dT%H%M}",
                "symbol": p["symbol"],
                "setup": p["setup"],
                "created": p["date"],
                "created_at": now.isoformat(timespec="seconds"),
                "entry": p["entry"],
                "stop": p["stop"],
                "initial_stop": p["stop"],
                "target": p["target"],
                "status": "pending",
                "breakeven": False,
                "last_checked": None,
            }
        )
    state.save(positions)


if __name__ == "__main__":
    import sys

    run(force="--force" in sys.argv)
