# NSE swing trade bot

A daily screener for Nifty 500 stocks that posts short-term swing trade ideas to Telegram, then tracks each one and alerts you when the entry triggers, the stop loss or target is hit, or the stop moves to cost.

**Not financial advice.** This is an educational tool built on free, delayed data. Check every idea yourself before trading, and size positions so a stop loss hit is a loss you can accept.

## How it works

**Screener** (`swing/screener.py`, scans at 11:00 and 13:00 IST on trading days, using the day's prices so far)

1. Loads the Nifty 500 list from NSE and a year of daily prices from Yahoo Finance.
2. Keeps liquid stocks (20-day average traded value over ₹10 crore) in an uptrend (close > 50 EMA > 200 EMA, 50 EMA rising).
3. Looks for two setups:
   - **Breakout**: close above the prior 20-day high on at least 1.5x average volume, RSI 55 to 75.
   - **Pullback**: a bounce off the rising 20 EMA, RSI 40 to 60, MACD histogram turning up.
4. Sets entry just above the day's high so far, stop at 1.5x ATR below entry (or below the 10-day swing low for pullbacks), and target at 2R. Setups with a stop more than 8% away are dropped.
5. For the best candidates, checks quarterly results (revenue and profit growth) and recent Google News headlines, and skips any stock with results due in the next 5 days.
6. Posts the best picks to Telegram (at most 5 a day across both scans) and records them in `state/positions.json`.

A third run at 16:00 IST posts a summary of open positions and closed-trade results.

**Tracker** (`swing/tracker.py`, every 15 minutes from 09:15 to 15:45 IST)

- A pick opens when price trades above its entry within 2 sessions. If it gaps more than 2% above the entry, it is cancelled.
- Alerts are sent when the entry triggers, at +1R (move stop to cost), on the stop loss, and on the target.
- After the close, picks that never triggered expire, and positions older than 10 sessions get a time exit.

All thresholds are in `swing/config.py`.

## Setup

1. **Create a Telegram bot.** In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token it gives you.
2. **Get your chat ID.** Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number under `"chat":{"id": ...}`.
3. **Add repo secrets.** In this repo on GitHub, open Settings, then Secrets and variables, then Actions, and add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. **Test.** Open the Actions tab, pick **Telegram test**, and click **Run workflow**. You should get a message within a minute.
5. Optionally run **Screener** by hand with *force* ticked to see a first set of picks.

## Limits to know

- Yahoo Finance data for NSE can lag by a few minutes, and GitHub may start scheduled runs 5 to 15 minutes late, so alerts are not tick-perfect. Place a real stop loss order with your broker.
- News scoring is keyword based and only reads headlines.
- In a public repo, GitHub pauses scheduled workflows after 60 days with no activity. The bot commits its state whenever picks change, which normally keeps it active. A private repo avoids this and keeps your picks private.

## Tests

```
pip install -r requirements.txt pytest
python -m pytest tests
```
