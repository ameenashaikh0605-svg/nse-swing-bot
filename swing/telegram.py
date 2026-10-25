"""Send messages to a Telegram chat through the Bot API."""

import requests

from . import config

MAX_LEN = 4000  # Telegram's limit is 4096 characters per message


def send(text: str) -> bool:
    """Send text (HTML formatting) to the configured chat. Prints instead if no token is set."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[telegram disabled, would send]\n" + text)
        return False
    ok = True
    for chunk in _chunks(text):
        resp = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"Telegram error {resp.status_code}: {resp.text[:300]}")
            ok = False
    return ok


def _chunks(text: str):
    while len(text) > MAX_LEN:
        cut = text.rfind("\n\n", 0, MAX_LEN)
        if cut <= 0:
            cut = MAX_LEN
        yield text[:cut]
        text = text[cut:].lstrip()
    if text:
        yield text
