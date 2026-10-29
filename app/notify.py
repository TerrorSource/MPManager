"""
Telegram notifications.

Nothing here may break what it reports on: a message that cannot be sent is
logged and forgotten, never raised into the job that triggered it.
"""
import html

import requests

import store
from store import log

TIMEOUT = 15

# set by app.py: where the bot token is kept (encrypted, with the login)
get_token = lambda: ""       # noqa: E731


def chat_ids(raw=None):
    """'123, -456 789' -> ['123', '-456', '789']: one or more recipients."""
    raw = store.settings()["telegram_chat_id"] if raw is None else raw
    ids = []
    for part in str(raw or "").replace(";", ",").replace(" ", ",").split(","):
        if part.strip() and part.strip() not in ids:
            ids.append(part.strip())
    return ids


def target():
    """(token, chat ids) for everything that sends: also the search alerts."""
    return get_token(), ",".join(chat_ids())


def send(text):
    """Returns None if every recipient got it, or the reason it did not go out."""
    token, chats = get_token(), chat_ids()
    if not token or not chats:
        return "geen bot-token of chat-id ingesteld"
    problems = []
    for chat in chats:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=TIMEOUT, json={
                "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        except requests.RequestException as e:
            problems.append(f"Telegram niet bereikbaar: {type(e).__name__}")    # the URL holds the token: keep it out of the log
            continue
        if r.status_code != 200:
            try:
                problems.append(f"chat {chat}: " + (r.json().get("description") or f"HTTP {r.status_code}"))
            except ValueError:
                problems.append(f"chat {chat}: HTTP {r.status_code}")
    return "; ".join(problems) or None


def _emit(text):
    problem = send(text)
    if problem:
        log(f"Telegram-melding niet verstuurd: {problem}")


def failed(what, title, message):
    if store.settings()["notify_on"] != "off":
        _emit(f"⚠️ <b>MPManager</b>\n{html.escape(what)} van <b>{html.escape(title or '?')}</b> mislukt:\n"
              f"<code>{html.escape(message[:400])}</code>")


def reposted(title, old_id, new_id, note=None):
    if store.settings()["notify_on"] == "all":
        extra = f" ({html.escape(note)})" if note else ""
        _emit(f"✅ <b>MPManager</b>\n<b>{html.escape(title or '?')}</b> herplaatst{extra}\n{old_id} → {new_id}")


def login_problem(message):
    if store.settings()["notify_on"] != "off":
        _emit(f"🔑 <b>MPManager</b>\nDe Marktplaats-login heeft aandacht nodig:\n<code>{html.escape(message[:400])}</code>")
