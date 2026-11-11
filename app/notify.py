"""
Telegram: the one place that sends, for everything in the tool.

Messages about your own ads (failures, reposts, login trouble) and the search
alerts (new ad, price drop, summary) all go through post(), to every chat id
that is set, with a retry when Telegram rate-limits.

Nothing here may break what it reports on: a message that cannot be sent is
logged and forgotten, never raised into the job that triggered it.
"""
import html
import time

import requests

import store
from store import log

TIMEOUT = 15
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER = 30          # seconds; waiting longer would hold up the schedulers
DIGEST_MAX_ITEMS = 20         # lines in one summary of search results

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


def configured():
    return bool(get_token() and chat_ids())


def _post_one(url, payload):
    """One message to one chat. Returns None, or why it did not arrive."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = requests.post(url, json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            return f"Telegram niet bereikbaar: {type(e).__name__}"      # the URL holds the token: keep it out of the log
        if r.status_code == 200:
            return None
        try:
            answer = r.json()
        except ValueError:
            answer = {}
        if r.status_code == 429 and attempt < MAX_ATTEMPTS:
            wait = (answer.get("parameters") or {}).get("retry_after") or 3
            time.sleep(max(1, min(MAX_RETRY_AFTER, int(wait))))
            continue
        return answer.get("description") or f"HTTP {r.status_code}"
    return "te veel berichten achter elkaar"


def deliver(method, payload):
    """To every chat. Returns (number that got it, list of problems)."""
    token, chats = get_token(), chat_ids()
    if not token or not chats:
        return 0, ["geen bot-token of chat-id ingesteld"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    problems = []
    for chat in chats:
        problem = _post_one(url, {"chat_id": chat, **payload})
        if problem:
            problems.append(f"chat {chat}: {problem}" if len(chats) > 1 else problem)
    return len(chats) - len(problems), problems


def post(method, payload, label=""):
    """Send and log what went wrong. True if at least one chat got it."""
    if not configured():
        return False
    delivered, problems = deliver(method, payload)
    if problems:
        log(f"Telegram-melding{' over ' + repr(label) if label else ''} niet verstuurd: {'; '.join(problems)}")
    return delivered > 0


def send(text):
    """An HTML message. Returns None if every chat got it, or the reason it did not go out."""
    _, problems = deliver("sendMessage", {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
    return "; ".join(problems) or None


def _emit(text):
    if configured():
        post("sendMessage", {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True})


# ---- about your own ads ----------------------------------------------------

def failed(what, title, message):
    if store.settings()["notify_on"] != "off":
        _emit(f"⚠️ <b>MPManager</b>\n{html.escape(what)} van <b>{html.escape(title or '?')}</b> mislukt:\n"
              f"<code>{html.escape(message[:400])}</code>")


def reposted(title, old_id, new_id, note=None):
    if store.settings()["notify_on"] == "all":
        extra = f" ({html.escape(note)})" if note else ""
        _emit(f"✅ <b>MPManager</b>\n<b>{html.escape(title or '?')}</b> herplaatst{extra}\n{old_id} → {new_id}")


def price_changed(title, note):
    if store.settings()["notify_on"] == "all":
        _emit(f"✏️ <b>MPManager</b>\nPrijs van <b>{html.escape(title or '?')}</b> aangepast\n{html.escape(note or '')}")


def login_problem(message):
    if store.settings()["notify_on"] != "off":
        _emit(f"🔑 <b>MPManager</b>\nDe login heeft aandacht nodig:\n<code>{html.escape(message[:400])}</code>")


# ---- search alerts ---------------------------------------------------------
#
# Plain text, no parse_mode: ad titles with _ * [ ] broke Markdown parsing and
# Telegram then refused the message without saying so.

def _cents(cents):
    return f"€ {cents / 100:.2f}".replace(".", ",")


def _place(ad):
    """'Utrecht (12 km)' / 'Utrecht' / '': the place, with the distance if known."""
    location = (ad.get("location") or "").strip()
    distance = ad.get("distance_km")
    if location and isinstance(distance, (int, float)):
        return f"{location} ({distance:g} km)"
    return location


def _ad_caption(ad, term=""):
    lines = []
    if term:
        lines.append(f"Zoekwoord = {term}")
    if ad.get("reserved"):
        lines.append("⚠ Gereserveerd")
    lines.append(f"Titel = {(ad.get('title') or '').strip()}")
    lines.append(f"Prijs = {(ad.get('price') or '').strip()}")
    if _place(ad):
        lines.append(f"Plaats = {_place(ad)}")
    if (ad.get("attributes") or "").strip():
        lines.append(f"Kenmerken = {ad['attributes'].strip()}")
    if (ad.get("posted_at") or "").strip():
        lines.append(f"Datum = {ad['posted_at'].strip()}")
    description = (ad.get("description") or "").strip()
    if description:
        lines.append("\n" + (description if len(description) <= 160 else description[:159].rstrip() + "…"))
    return "\n".join(lines)


def _send_ad(caption, ad):
    title, url = (ad.get("title") or "").strip(), (ad.get("url") or "").strip()
    image = (ad.get("image_url") or "").strip()
    button = {"inline_keyboard": [[{"text": "Bekijk advertentie", "url": url}]]}
    if image:
        return post("sendPhoto", {"photo": image, "caption": caption, "reply_markup": button}, label=title)
    return post("sendMessage", {"text": caption, "reply_markup": button}, label=title)


def search_ad(ad, term=""):
    return _send_ad(_ad_caption(ad, term), ad)


def search_price_drop(ad, old_cents, new_cents, term=""):
    caption = "📉 Prijsverlaging!\n"
    if term:
        caption += f"Zoekwoord = {term}\n"
    caption += f"Titel = {(ad.get('title') or '').strip()}\nPrijs = {_cents(old_cents)} → {_cents(new_cents)}"
    if _place(ad):
        caption += f"\nPlaats = {_place(ad)}"
    return _send_ad(caption, ad)


def search_digest(term, ads):
    """One message for many new ads at once."""
    lines = [f"🔎 {term}: {len(ads)} nieuwe advertenties"]
    for i, ad in enumerate(ads[:DIGEST_MAX_ITEMS], start=1):
        meta = ((ad.get("price") or "").strip() or "—") + (f" · {_place(ad)}" if _place(ad) else "") \
            + (" · gereserveerd" if ad.get("reserved") else "")
        lines.append(f"\n{i}. {(ad.get('title') or '').strip()}\n   {meta}\n   {ad.get('url', '')}")
    if len(ads) > DIGEST_MAX_ITEMS:
        lines.append(f"\n… en nog {len(ads) - DIGEST_MAX_ITEMS} meer")
    return post("sendMessage", {"text": "\n".join(lines), "disable_web_page_preview": True}, label=f"samenvatting {term}")
