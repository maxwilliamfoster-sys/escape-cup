"""Telegram alerts (shared bot with the other automations). Never raises."""
import os

import requests

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


def send(text):
    if not _TOKEN or not _CHAT:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
                          json={"chat_id": _CHAT, "text": text[:4000], "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=20)
        return r.ok
    except requests.RequestException as e:
        print(f"[telegram] {e}")
        return False


def send_video(path, caption=""):
    if not _TOKEN or not _CHAT or not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as fh:
            r = requests.post(f"https://api.telegram.org/bot{_TOKEN}/sendVideo",
                              data={"chat_id": _CHAT, "caption": caption[:1024], "parse_mode": "HTML",
                                    "supports_streaming": "true", "width": 1080, "height": 1920},
                              files={"video": (os.path.basename(path), fh, "video/mp4")}, timeout=300)
        if not r.ok:
            print(f"[telegram] sendVideo {r.status_code} {r.text[:200]}")
        return r.ok
    except requests.RequestException as e:
        print(f"[telegram] {e}")
        return False
