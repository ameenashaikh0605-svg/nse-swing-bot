"""End-of-day screener: finds swing setups across the Nifty 500 and posts the best ones to Telegram.

Run after the close (around 16:00 IST). Each pick is a buy-stop: it becomes a position only if
price trades above the entry trigger in the next sessions.
"""

import datetime as dt
import html

import pandas as pd

from . import catalysts, config, data, indicators, state, telegram, tracker


def evaluate(symbol: str, df: pd.DataFrame) -> dict | None:
    """Return a candidate setup for one stock's daily bars, or None if nothing qualifies."""
    if len(df) < 210:
        return None
    df = indicators.add_all(df)
    t, y = df.iloc[-1], df.iloc[-2]
    if t["Close"] < config.MIN_PRICE or t["traded_value20"] < config.MIN_AVG_TRADED_VALUE:
        return None
    uptrend = t["Close"] > t["ema50"] > t["ema200"] and t["ema50"] > df["ema50"].iloc[-10]
    if not uptrend:
        return None

    vol_ratio = t["Volume"] / t["vol20"] if t["vol20"] else 0
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
        "score": round(float(score), 2),
        "reasons": reasons,
        "date": pd.Timestamp(df.index[-1]).date().isoformat(),
    }


def enrich(cand: dict, company: str) -> dict | None:
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
    return cand


def _growth_text(res: dict) -> str:
    parts = []
    if res["rev_growth"] is not None:
        parts.append(f"revenue {res['rev_growth']:+.0%}")
    if res["profit_growth"] is not None:
        parts.append(f"profit {res['profit_growth']:+.0%}")
    return f"{res['basis']} " + ", ".join(parts) if parts else "n/a"


def format_picks(picks: list[dict], universe: dict[str, str]) -> str:
    today = dt.date.today().strftime("%d %b %Y")
    if not picks:
        return f"<b>Swing screener, {today}</b>\nNo setups passed the filters today.\n\n<i>{config.DISCLAIMER}</i>"
    lines = [f"<b>Swing picks for next session ({today})</b>", ""]
    for i, p in enumerate(picks, 1):
        name = html.escape(universe.get(p["symbol"], p["symbol"]))
        lines += [
            f"<b>{i}. {p['symbol']}</b> · {name} · {p['setup']}",
            f"Buy above ₹{p['entry']:,.2f} (close ₹{p['close']:,.2f})",
            f"Stop loss ₹{p['stop']:,.2f} (-{p['risk_pct']}%) · Target ₹{p['target']:,.2f} (+{p['risk_pct'] * config.TARGET_R_MULT:.1f}%)",
            "Why: " + html.escape("; ".join(p["reasons"])),
        ]
        if p.get("headlines"):
            lines.append("News: " + html.escape(p["headlines"][0][:140]))
        lines.append("")
    lines += [
        f"Entry valid for {config.ENTRY_VALID_DAYS} sessions. Stop moves to cost at +{config.BREAKEVEN_AT_R:g}R. "
        f"Time exit after {config.MAX_HOLD_DAYS} sessions. You will get an alert when a stop or target is hit.",
        "",
        f"<i>{config.DISCLAIMER}</i>",
    ]
    return "\n".join(lines)


def run(force: bool = False) -> None:
    last = data.last_trading_date()
    today = pd.Timestamp(dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date())
    if not force and (last is None or last.date() != today.date()):
        print(f"No NSE session today (last bar {last}); skipping")
        return

    universe = data.load_universe()
    print(f"Universe: {len(universe)} stocks")
    daily = data.download_daily(list(universe))
    print(f"Downloaded {len(daily)} stocks")

    positions = state.load()
    tracker.end_of_day(positions, daily)

    held = {p["symbol"] for p in state.active(positions)}
    slots = max(0, config.MAX_OPEN_POSITIONS - len(state.active(positions)))
    cands = [c for s, df in daily.items() if s not in held and (c := evaluate(s, df))]
    cands.sort(key=lambda c: c["score"], reverse=True)
    print(f"{len(cands)} technical candidates")

    picks = []
    for c in cands[: config.MAX_PICKS_PER_DAY * 3]:
        if len(picks) >= min(config.MAX_PICKS_PER_DAY, slots):
            break
        if (e := enrich(c, universe.get(c["symbol"], c["symbol"]))) is not None and e["score"] > 1.5:
            picks.append(e)
    picks.sort(key=lambda c: c["score"], reverse=True)

    telegram.send(format_picks(picks, universe))
    for p in picks:
        positions.append(
            {
                "id": f"{p['symbol']}-{p['date']}",
                "symbol": p["symbol"],
                "setup": p["setup"],
                "created": p["date"],
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
    telegram.send(tracker.summary(positions, daily))


if __name__ == "__main__":
    import sys

    run(force="--force" in sys.argv)
