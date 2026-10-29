"""Telegram-meldingen (met retry bij rate-limiting)."""
import time as time_module

from .config import DIGEST_MAX_ITEMS, logger
from .marketplace import HTTP
from .utils import format_cents

def telegram_target(settings: dict) -> tuple[str, str]:
    """(bot-token, chat-id's). In MPManager vervangt app.py dit door de
    Telegram-instellingen van MPManager zelf; zo is er één bot voor alles."""
    return settings.get("telegram_bot_id") or "", settings.get("telegram_chat_id") or ""

TELEGRAM_MAX_ATTEMPTS = 3
TELEGRAM_MAX_RETRY_AFTER = 30  # seconden; langer wachten blokkeert de scheduler te veel


def parse_chat_ids(raw: str | None) -> list[str]:
    """'123, -456 789' -> ['123', '-456', '789'] — één of meer ontvangers."""
    if not raw:
        return []
    ids: list[str] = []
    for part in str(raw).replace(";", ",").replace(" ", ",").split(","):
        p = part.strip()
        if p and p not in ids:
            ids.append(p)
    return ids


def _telegram_post(method: str, payload: dict, settings: dict, label: str = "") -> bool:
    """POST naar de Bot API, naar elke geconfigureerde chat (komma-gescheiden),
    met retry bij rate-limiting. True als minstens één chat het bericht kreeg."""
    bot_id, chats = telegram_target(settings)
    bot_id = (bot_id or "").strip()
    chat_ids = parse_chat_ids(chats)
    if not bot_id or not chat_ids:
        return False

    api_url = f"https://api.telegram.org/bot{bot_id}/{method}"
    ok_any = False
    for chat_id in chat_ids:
        if _telegram_post_one(api_url, {"chat_id": chat_id, **payload}, method, label):
            ok_any = True
    return ok_any


def _telegram_post_one(api_url: str, payload: dict, method: str, label: str) -> bool:
    for attempt in range(1, TELEGRAM_MAX_ATTEMPTS + 1):
        try:
            resp = HTTP.post(api_url, json=payload, timeout=10)
        except Exception as exc:
            logger.warning("Telegram %s voor '%s' mislukt: %s", method, label, type(exc).__name__)  # de URL bevat het token
            return False

        if resp.ok:
            return True

        if resp.status_code == 429 and attempt < TELEGRAM_MAX_ATTEMPTS:
            try:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 3))
            except Exception:
                retry_after = 3
            retry_after = max(1, min(TELEGRAM_MAX_RETRY_AFTER, retry_after))
            logger.warning(
                "Telegram rate-limit voor '%s'; opnieuw over %s s (poging %s/%s)",
                label, retry_after, attempt, TELEGRAM_MAX_ATTEMPTS,
            )
            time_module.sleep(retry_after)
            continue

        logger.warning(
            "Telegram %s voor '%s' mislukt (%s): %s",
            method, label, resp.status_code, resp.text,
        )
        return False
    return False


def send_telegram_message(text: str, settings: dict) -> None:
    # Geen parse_mode: advertentietitels met _ * [ ] braken Markdown-parsing
    # waardoor Telegram het bericht stilletjes weigerde.
    _telegram_post(
        "sendMessage",
        {"text": text, "disable_web_page_preview": False},
        settings,
        label=text[:40],
    )


def _send_ad_via_telegram(caption: str, ad: dict, settings: dict) -> bool:
    title = (ad.get("title") or "").strip()
    url = (ad.get("url") or "").strip()
    image_url = (ad.get("image_url") or "").strip()
    reply_markup = {"inline_keyboard": [[{"text": "Bekijk advertentie", "url": url}]]}

    if image_url:
        return _telegram_post(
            "sendPhoto",
            {"photo": image_url, "caption": caption, "reply_markup": reply_markup},
            settings, label=title,
        )
    return _telegram_post(
        "sendMessage",
        {"text": caption, "reply_markup": reply_markup},
        settings, label=title,
    )


def _place(ad: dict) -> str:
    """'Utrecht (12 km)' / 'Utrecht' / '' — plaats met afstand als die bekend is."""
    location = (ad.get("location") or "").strip()
    distance = ad.get("distance_km")
    if location and isinstance(distance, (int, float)):
        return f"{location} ({distance:g} km)"
    return location


def _ad_caption(ad: dict, term: str = "") -> str:
    title = (ad.get("title") or "").strip()
    price = (ad.get("price") or "").strip()
    posted_at = (ad.get("posted_at") or "").strip()
    lines = []
    if term:
        lines.append(f"Zoekwoord = {term}")
    if ad.get("reserved"):
        lines.append("⚠ Gereserveerd")
    lines.append(f"Titel = {title}")
    lines.append(f"Prijs = {price}")
    place = _place(ad)
    if place:
        lines.append(f"Plaats = {place}")
    attributes = (ad.get("attributes") or "").strip()
    if attributes:
        lines.append(f"Kenmerken = {attributes}")
    if posted_at:
        lines.append(f"Datum = {posted_at}")
    description = (ad.get("description") or "").strip()
    if description:
        snippet = description if len(description) <= 160 else description[:159].rstrip() + "…"
        lines.append(f"\n{snippet}")
    return "\n".join(lines)


def send_telegram_ad(ad: dict, settings: dict, term: str = "") -> bool:
    return _send_ad_via_telegram(_ad_caption(ad, term), ad, settings)


def send_telegram_price_drop(ad: dict, old_cents: int, new_cents: int, settings: dict, term: str = "") -> bool:
    title = (ad.get("title") or "").strip()
    caption = "📉 Prijsverlaging!\n"
    if term:
        caption += f"Zoekwoord = {term}\n"
    caption += f"Titel = {title}\nPrijs = {format_cents(old_cents)} → {format_cents(new_cents)}"
    place = _place(ad)
    if place:
        caption += f"\nPlaats = {place}"
    return _send_ad_via_telegram(caption, ad, settings)


def send_telegram_digest(term: str, ads: list[dict], settings: dict) -> bool:
    """Eén samenvattend bericht voor veel nieuwe advertenties tegelijk."""
    lines = [f"🔎 {term}: {len(ads)} nieuwe advertenties"]
    for i, ad in enumerate(ads[:DIGEST_MAX_ITEMS], start=1):
        title = (ad.get("title") or "").strip()
        price = (ad.get("price") or "").strip() or "—"
        place = _place(ad)
        meta = f"{price}" + (f" · {place}" if place else "") + (" · gereserveerd" if ad.get("reserved") else "")
        lines.append(f"\n{i}. {title}\n   {meta}\n   {ad.get('url', '')}")
    if len(ads) > DIGEST_MAX_ITEMS:
        lines.append(f"\n… en nog {len(ads) - DIGEST_MAX_ITEMS} meer")
    return _telegram_post(
        "sendMessage",
        {"text": "\n".join(lines), "disable_web_page_preview": True},
        settings, label=f"digest {term}",
    )
