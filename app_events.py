"""Send an alert to Bot HQ (the phone app). MASTER copy lives in growth-lab;
the bots carry byte-identical copies (check with md5).

    import app_events
    app_events.emit("chess", "posted", "info", "Chess: new video posted", body,
                    data={"video_url": url}, fallback=notifier.send_telegram)

Needs APP_URL and APP_EVENTS_TOKEN in the environment. If either is missing,
or Bot HQ can't be reached, `fallback(text)` is called instead (e.g. the old
Telegram sender) so an alert is never lost. Never raises; never prints the token.
"""
import json
import os
import urllib.error
import urllib.request

TIMEOUT_S = 10


def _fallback(fallback, title, body):
    if fallback is None:
        return
    try:
        fallback(f"{title}\n\n{body}" if body else title)
    except Exception as e:  # the fallback failing must not break the bot
        print(f"[app_events] fallback failed: {type(e).__name__}")


def emit(bot, kind, severity, title, body="", data=None, dedupe_key=None, fallback=None):
    url = (os.environ.get("APP_URL") or "").rstrip("/")
    token = os.environ.get("APP_EVENTS_TOKEN") or ""
    try:
        payload = {"bot": bot, "kind": kind, "severity": severity, "title": str(title)[:120],
                   "body": str(body or "")[:4000], "data": data or {}}
        if dedupe_key:
            payload["dedupe_key"] = str(dedupe_key)[:200]
        raw = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError) as e:
        print(f"[app_events] could not encode event {kind}: {e}")
        _fallback(fallback, title, body)
        return False
    if not url or not token:
        _fallback(fallback, title, body)
        return False
    req = urllib.request.Request(
        f"{url}/api/event", data=raw, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                 "User-Agent": "bot-hq-events/1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:
            if 200 <= res.status < 300:
                return True
            print(f"[app_events] Bot HQ answered HTTP {res.status} for {kind}")
    except urllib.error.HTTPError as e:
        print(f"[app_events] Bot HQ answered HTTP {e.code} for {kind}")
    except Exception as e:
        print(f"[app_events] Bot HQ unreachable for {kind}: {type(e).__name__}")
    _fallback(fallback, title, body)
    return False
