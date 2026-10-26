"""News and quarterly results checks for a shortlist of candidates."""

import datetime as dt
import re
import urllib.parse

import feedparser
import pandas as pd
import yfinance as yf

from .data import yf_symbol

POSITIVE = [
    "order win", "bags order", "wins order", "secures", "record", "beats", "profit jumps", "profit rises",
    "profit surges", "upgrade", "target raised", "buy rating", "outperform", "expansion", "acquires",
    "approval", "approved", "launch", "strong demand", "margin expands", "dividend", "bonus", "buyback",
    "rallies", "surges", "hits 52-week high", "all-time high", "partnership", "contract",
]
NEGATIVE = [
    "downgrade", "target cut", "sell rating", "underperform", "misses", "profit falls", "profit drops",
    "loss widens", "net loss", "probe", "raid", "sebi notice", "penalty", "fraud", "resigns", "default",
    "pledge", "plunges", "slumps", "crashes", "52-week low", "block deal", "stake sale", "ofs", "warning",
    "recall", "usfda observation", "import alert", "strike", "fire at",
]


def news_sentiment(company: str, days: int = 4, limit: int = 8) -> tuple[int, list[str]]:
    """Score recent Google News headlines for a company with keyword matching.

    Returns (score, headlines). Score is positive matches minus negative matches, capped to [-3, 3].
    """
    name = re.sub(r"\b(Ltd|Limited|Ltd\.)\b", "", company).strip()
    query = urllib.parse.quote(f'"{name}" stock when:{days}d')
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"News fetch failed for {company}: {e}")
        return 0, []
    score = 0
    headlines = []
    for entry in feed.entries[:limit]:
        title = entry.get("title", "")
        low = title.lower()
        pos = sum(k in low for k in POSITIVE)
        neg = sum(k in low for k in NEGATIVE)
        score += (pos > 0) - (neg > 0)
        headlines.append(title)
    return max(-3, min(3, score)), headlines


def results_check(symbol: str) -> dict:
    """Latest quarterly revenue and profit growth, plus the next earnings date if known.

    Growth is year on year when five quarters are available, otherwise quarter on quarter.
    Any field can be None if Yahoo has no data.
    """
    info = {"rev_growth": None, "profit_growth": None, "basis": None, "next_earnings": None}
    t = yf.Ticker(yf_symbol(symbol))
    try:
        q = t.quarterly_income_stmt
        if q is not None and not q.empty:
            q = q.sort_index(axis=1, ascending=False)
            prev_col = 4 if q.shape[1] >= 5 else 1 if q.shape[1] >= 2 else None
            if prev_col is not None:
                info["basis"] = "YoY" if prev_col == 4 else "QoQ"
                for row, key in (("Total Revenue", "rev_growth"), ("Net Income", "profit_growth")):
                    if row in q.index:
                        cur, prev = q.loc[row].iloc[0], q.loc[row].iloc[prev_col]
                        if pd.notna(cur) and pd.notna(prev) and prev != 0:
                            info[key] = float((cur - prev) / abs(prev))
    except Exception as e:
        print(f"Results fetch failed for {symbol}: {e}")
    try:
        cal = t.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            upcoming = [d for d in dates if isinstance(d, dt.date) and d >= dt.date.today()]
            if upcoming:
                info["next_earnings"] = min(upcoming).isoformat()
    except Exception as e:
        print(f"Calendar fetch failed for {symbol}: {e}")
    return info


def results_score(info: dict) -> int:
    """+2 for strong growth in both lines, down to -2 for falling profit and revenue."""
    rev, prof = info.get("rev_growth"), info.get("profit_growth")
    score = 0
    if rev is not None:
        score += 1 if rev > 0.10 else -1 if rev < -0.05 else 0
    if prof is not None:
        score += 1 if prof > 0.15 else -1 if prof < -0.10 else 0
    return score


# Common causes of a volume spike, matched against recent headlines. "one_off" causes are trades or
# events that don't signal new buying interest, so the pick is marked down.
VOLUME_CAUSES = [
    ("Block or bulk deal / stake sale", True, ["block deal", "bulk deal", "stake sale", "offer for sale", "ofs", "promoter sells", "sells stake", "offloads", "large trade"]),
    ("Quarterly results", False, ["results", "q1", "q2", "q3", "q4", "quarterly", "net profit", "earnings", "revenue"]),
    ("Order win or contract", False, ["order", "contract", "bags", "secures", "wins deal", "tender"]),
    ("Broker rating or target change", False, ["upgrade", "downgrade", "target price", "buy rating", "sell rating", "initiates coverage", "brokerage"]),
    ("Dividend, bonus, split or buyback", True, ["dividend", "bonus", "split", "buyback", "record date", "ex-date", "rights issue"]),
    ("Merger, acquisition or demerger", False, ["acquire", "acquisition", "merger", "demerger", "takeover", "amalgamation"]),
    ("Index inclusion or rebalancing", True, ["index inclusion", "msci", "ftse", "sensex inclusion", "nifty inclusion", "rejig", "rebalancing", "rebalance"]),
    ("Regulatory or legal news", False, ["sebi", "usfda", "probe", "raid", "penalty", "approval", "licence", "license", "ban"]),
    ("Fundraise or QIP", True, ["qip", "fundraise", "raises", "preferential issue", "ncd"]),
]


def volume_reason(symbol: str, headlines: list[str], results: dict, nifty_change: float | None) -> dict:
    """Best guess at why a stock's volume jumped, from headlines, corporate actions and the market.

    Returns {"causes": [str], "one_off": bool, "unexplained": bool, "evidence": str | None}.
    """
    causes, evidence, one_off = [], None, False
    for title in headlines:
        low = title.lower()
        for label, is_one_off, words in VOLUME_CAUSES:
            if label not in causes and any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
                causes.append(label)
                one_off |= is_one_off
                evidence = evidence or title
    t = yf.Ticker(yf_symbol(symbol))
    try:
        actions = t.actions
        if actions is not None and len(actions):
            recent = actions[actions.index >= pd.Timestamp.now(tz=actions.index.tz) - pd.Timedelta(days=3)]
            if len(recent) and "Dividend, bonus, split or buyback" not in causes:
                causes.append("Dividend, bonus, split or buyback")
                one_off = True
    except Exception as e:
        print(f"Actions fetch failed for {symbol}: {e}")
    try:
        ed = t.get_earnings_dates(limit=4)
        if ed is not None and len(ed):
            now = pd.Timestamp.now(tz=ed.index.tz)
            if any(now - pd.Timedelta(days=3) <= d <= now for d in ed.index) and "Quarterly results" not in causes:
                causes.append("Quarterly results")
    except Exception as e:
        print(f"Earnings dates fetch failed for {symbol}: {e}")
    if nifty_change is not None and abs(nifty_change) >= 0.015:
        causes.append(f"Whole market moving (Nifty {nifty_change:+.1%})")
    return {"causes": causes, "one_off": one_off, "unexplained": not causes, "evidence": evidence}
