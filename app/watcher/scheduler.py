"""Zoeklogica per zoekwoord en de achtergrondworker."""
import os
import threading
import time as time_module
from datetime import datetime, time, timedelta
from typing import Optional

from . import db
from . import marketplace as mp
from . import notify
from .config import (
    BACKOFF_MAX_HOURS,
    EXPIRY_CHECK_BATCH,
    EXPIRY_CHECK_INTERVAL_HOURS,
    SCHEDULER_STALE_SECONDS,
    SEARCH_SPACING_SECONDS,
    MarketplaceError,
    logger,
)
from .utils import (
    ad_matches_term,
    format_cents,
    int_or_none,
    normalize_title,
    title_passes_filters,
)

# ------------------------------------------------------------------------------
# Status van de worker
# ------------------------------------------------------------------------------

_worker_thread_started = False
_worker_lock = threading.Lock()
_last_heartbeat: Optional[float] = None


def get_scheduler_status() -> dict:
    """Echte status van de achtergrondworker voor de statuskaart in de UI."""
    if not _worker_thread_started:
        return {"state": "off", "label": "niet gestart", "age_seconds": None}
    if _last_heartbeat is None:
        return {"state": "starting", "label": "start op…", "age_seconds": None}
    age = int(time_module.monotonic() - _last_heartbeat)
    if age > SCHEDULER_STALE_SECONDS:
        return {"state": "stale", "label": f"geen teken van leven sinds {age // 60} min", "age_seconds": age}
    return {"state": "ok", "label": f"actief (laatste hartslag {age} s geleden)", "age_seconds": age}


# ------------------------------------------------------------------------------
# Timing
# ------------------------------------------------------------------------------

def _parse_time_str(value: str) -> time:
    try:
        hh, mm = value.strip().split(":")
        return time(int(hh), int(mm))
    except Exception:
        return time(23, 0)


def is_in_sleep_window(now_t: time, start: time, end: time) -> bool:
    if start < end:
        return start <= now_t < end
    return now_t >= start or now_t < end


def in_sleep_now(settings: dict, now: datetime) -> bool:
    return bool(settings.get("sleep_mode")) and is_in_sleep_window(
        now.time(),
        _parse_time_str(settings.get("sleep_start", "23:00")),
        _parse_time_str(settings.get("sleep_end", "07:00")),
    )


def backoff_factor(error_count: int) -> int:
    """1, 2, 4, 8, … per opeenvolgende mislukking (begrensd via BACKOFF_MAX_HOURS)."""
    return 2 ** max(0, min(int(error_count or 0), 8))


def effective_interval(kw: dict, settings: dict, now: datetime) -> timedelta:
    """Interval van een zoekwoord, rekening houdend met slaapstand en backoff
    na mislukte zoekopdrachten."""
    interval_minutes = int(kw.get("interval_minutes") or settings["default_interval_minutes"])
    interval = timedelta(minutes=interval_minutes)
    if in_sleep_now(settings, now) and interval < timedelta(hours=1):
        interval = timedelta(hours=1)

    errors = int(kw.get("error_count") or 0)
    if errors:
        interval = min(interval * backoff_factor(errors), timedelta(hours=BACKOFF_MAX_HOURS))
    return interval


def _parse_iso(value) -> Optional[datetime]:
    if not value or value == "Nooit":
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def compute_next_run(kw: dict, settings: dict, now: datetime) -> Optional[datetime]:
    """Verwachte volgende zoekactie; None als het zoekwoord nog nooit (met
    succes of mislukking) liep. Na een fout telt de fouttijd als startpunt,
    zodat de backoff werkt zonder de 'eerste run'-status te verliezen."""
    candidates = [dt for dt in (_parse_iso(kw.get("last_run_at")),) if dt]
    if int(kw.get("error_count") or 0):
        err_dt = _parse_iso(kw.get("last_error_at"))
        if err_dt:
            candidates.append(err_dt)
    if not candidates:
        return None
    return max(candidates) + effective_interval(kw, settings, now)


def format_next_run(next_dt: Optional[datetime], now: datetime) -> str:
    if next_dt is None:
        return "wacht op eerste run"
    secs = int((next_dt - now).total_seconds())
    if secs <= 0:
        return "nu"
    mins = secs // 60
    if mins < 1:
        return "< 1 min"
    if mins < 60:
        return f"over {mins} min"
    return f"over {mins // 60} uur {mins % 60} min"


# ------------------------------------------------------------------------------
# Eén zoekactie
# ------------------------------------------------------------------------------

def keyword_settings(keyword: dict, settings: dict) -> dict:
    """Globale instellingen met de override van één zoekwoord (marketplace)."""
    merged = dict(settings)
    choice = (keyword.get("marketplace") or "").strip().lower()
    if choice in ("marktplaats", "2dehands"):
        merged["marketplace"] = choice
    return merged


def run_search_for_keyword(keyword: dict, settings: dict, manual: bool = False) -> tuple[int, int]:
    """Eén zoekactie. Geeft (aantal resultaten na filters, aantal nieuw) terug.
    Gooit MarketplaceError als de zoekopdracht zelf mislukt.

    De allereerste run van een zoekwoord (last_run_at leeg, ook na Reset) is
    'stil': resultaten worden opgeslagen maar niet via Telegram gemeld."""
    term = keyword["term"]
    kid = keyword["id"]
    limit_per_run = int(keyword.get("limit_per_run") or settings["default_limit_per_run"])
    limit_per_run = max(1, min(20, limit_per_run))
    min_price = int_or_none(keyword.get("min_price"))
    max_price = int_or_none(keyword.get("max_price"))
    first_run = keyword.get("last_run_at") in (None, "", "Nooit")
    settings = keyword_settings(keyword, settings)

    raw_ads = mp.fetch_market_results(
        term, settings, limit_per_run,
        category_id=int_or_none(keyword.get("category_id")),
        category_parent_id=int_or_none(keyword.get("category_parent_id")),
    )

    # Filters: prijs, titelwoorden, titel-blocklist, gereserveerd
    blocked_titles = {normalize_title(t) for t in (settings.get("blocked_titles") or [])}
    skip_reserved = bool(settings.get("skip_reserved"))
    ads: list[dict] = []
    for ad in raw_ads:
        cents = ad.get("price_cents")
        if min_price is not None and (cents is None or cents < min_price * 100):
            continue
        if max_price is not None and (cents is None or cents > max_price * 100):
            continue
        if not title_passes_filters(ad.get("title", ""), keyword.get("include_terms"), keyword.get("exclude_terms")):
            continue
        if not ad_matches_term(term, ad.get("title", ""), ad.get("description", ""), settings.get("match_mode", "text")):
            logger.debug("Niet relevant voor '%s': %s", term, ad.get("title"))
            continue
        if blocked_titles and normalize_title(ad.get("title")) in blocked_titles:
            continue
        if skip_reserved and ad.get("reserved"):
            continue
        ads.append(ad)

    # Alleen écht nieuwe advertenties verdienen de (dure) seller-fallback;
    # bekende ads krijgen hooguit een veld-update met verse API-data.
    known_ids = db.get_known_ad_ids(kid)
    new_candidates = [ad for ad in ads if ad["ad_id"] not in known_ids]
    known_ads = [ad for ad in ads if ad["ad_id"] in known_ids]

    mp.enrich_ads_with_seller(new_candidates)

    # Blocklist ná de seller-verrijking, anders glippen verkopers door
    # waarvan de API geen naam meegeeft. (Lege lijst = blocklist uit.)
    blocked_sellers = {s.strip().lower() for s in (settings.get("blocked_sellers") or []) if s.strip()}
    if blocked_sellers:
        new_candidates = [
            ad for ad in new_candidates
            if (ad.get("seller") or "").strip().lower() not in blocked_sellers
        ]

    # Herplaatsingen (zelfde titel/verkoper/prijs, nieuw id) markeren vóór het
    # opslaan, anders vindt de check de advertentie zelf.
    repost_days = int(settings.get("repost_dedup_days") or 0)
    if repost_days:
        for ad in new_candidates:
            if db.find_recent_repost(ad.get("title", ""), ad.get("seller", ""), ad.get("price_cents"), repost_days):
                ad["_repost"] = True

    # Prijsverlagingen detecteren vóórdat update_known_ads de oude prijs overschrijft
    price_drops = db.find_price_drops(kid, known_ads) if settings.get("price_drop_alerts") else []

    db.update_known_ads(kid, known_ads)
    new_ads = db.insert_new_ads(kid, new_candidates)
    db.prune_results_for_keyword(kid)

    if first_run:
        if new_ads:
            logger.info("Eerste run van '%s': %s advertenties stil opgeslagen (geen Telegram)", term, len(new_ads))
            db.log_notification("silent", kid, term, detail=f"eerste run: {len(new_ads)} advertenties stil opgeslagen")
        return len(ads), len(new_ads)

    notify_on = (not manual) or settings.get("manual_telegram")
    if notify_on:
        for ad, old_c, new_c in price_drops:
            logger.info("Prijsverlaging voor '%s': %s -> %s", ad.get("title"), format_cents(old_c), format_cents(new_c))
            sent = notify.send_telegram_price_drop(ad, old_c, new_c, settings, term=term)
            db.log_notification("price_drop", kid, term, ad, detail=f"{format_cents(old_c)} → {format_cents(new_c)}", sent=sent)

        to_notify: list[dict] = []
        for ad in new_ads:
            if ad.get("_seen_elsewhere"):
                logger.info("Telegram overgeslagen voor '%s' (al gemeld via ander zoekwoord)", ad.get("title"))
                db.log_notification("suppressed", kid, term, ad, detail="al gemeld via ander zoekwoord")
                continue
            if ad.get("_repost"):
                logger.info("Telegram overgeslagen voor '%s' (herplaatsing van bekende advertentie)", ad.get("title"))
                db.log_notification("suppressed", kid, term, ad, detail="herplaatsing van bekende advertentie")
                continue
            to_notify.append(ad)

        digest_threshold = int(settings.get("telegram_digest_threshold") or 0)
        if digest_threshold and len(to_notify) >= digest_threshold:
            logger.info("%s nieuwe advertenties voor '%s' als samenvatting gemeld", len(to_notify), term)
            sent = notify.send_telegram_digest(term, to_notify, settings)
            db.log_notification("digest", kid, term, detail=f"{len(to_notify)} advertenties in één samenvatting", sent=sent)
            for ad in to_notify:
                db.log_notification("new", kid, term, ad, detail="in samenvatting", sent=sent)
        else:
            for ad in to_notify:
                sent = notify.send_telegram_ad(ad, settings, term=term)
                db.log_notification("new", kid, term, ad, sent=sent)
    elif new_ads:
        for ad in new_ads:
            db.log_notification("suppressed", kid, term, ad, detail="handmatige zoekactie (Telegram uit)")

    db.prune_notifications()
    return len(ads), len(new_ads)


# ------------------------------------------------------------------------------
# Achtergrondworker
# ------------------------------------------------------------------------------

def record_search_failure(kw: dict, message: str) -> None:
    """Fout vastleggen en de backoff-teller ophogen."""
    db.update_keyword_fields(
        kw["id"],
        last_error=message[:200],
        last_error_at=datetime.now().isoformat(timespec="seconds"),
        error_count=int(kw.get("error_count") or 0) + 1,
    )


def record_search_success(kw_id: int) -> None:
    db.update_keyword_fields(
        kw_id,
        last_run_at=datetime.now().isoformat(timespec="seconds"),
        last_error=None,
        last_error_at=None,
        error_count=0,
    )


_last_expiry_check: Optional[float] = None


def run_expiry_check(settings: dict, batch: int = EXPIRY_CHECK_BATCH) -> tuple[int, int]:
    """Controleer recente advertenties op verwijderd/verlopen (HTTP 404/410).
    Geeft (gecontroleerd, verlopen) terug. Twijfelgevallen (netwerkfout)
    worden niet gemarkeerd."""
    days = int(settings.get("expiry_check_days") or 0)
    if not days:
        return 0, 0
    todo = db.get_results_to_check(days, batch)
    if not todo:
        return 0, 0

    checked: list[int] = []
    expired: list[int] = []
    for row in todo:
        alive = mp.check_ad_alive(row["url"])
        if alive is None:
            continue
        checked.append(row["id"])
        if alive is False:
            expired.append(row["id"])
            logger.info("Advertentie verlopen: %s", row.get("title"))
        time_module.sleep(1)
    db.mark_results_checked(checked, expired)
    if checked:
        logger.info("Controle op verlopen advertenties: %s gecontroleerd, %s verlopen", len(checked), len(expired))
    return len(checked), len(expired)


def run_scheduler_iteration(now: Optional[datetime] = None) -> int:
    """Eén ronde langs alle zoekwoorden. Geeft terug hoeveel seconden de lus
    daarna moet wachten. Los getrokken uit de lus zodat het testbaar is."""
    global _last_heartbeat, _last_expiry_check
    _last_heartbeat = time_module.monotonic()
    now = now or datetime.now()

    settings = db.load_settings()
    keywords = db.load_keywords()
    if not keywords:
        return 30

    # Eén keer per dag: recente advertenties controleren op verlopen.
    mono = time_module.monotonic()
    if _last_expiry_check is None or mono - _last_expiry_check >= EXPIRY_CHECK_INTERVAL_HOURS * 3600:
        _last_expiry_check = mono
        try:
            run_expiry_check(settings)
        except Exception:
            logger.exception("Controle op verlopen advertenties mislukt")

    for kw in keywords:
        _last_heartbeat = time_module.monotonic()
        if not kw.get("term"):
            continue

        next_run = compute_next_run(kw, settings, now)
        if next_run is not None and next_run > now:
            continue

        try:
            total, new_count = run_search_for_keyword(kw, settings, manual=False)
            # Alleen op INFO loggen als er iets gebeurd is; anders DEBUG.
            log = logger.info if new_count else logger.debug
            log("Zoekactie '%s': %s resultaten, %s nieuw", kw["term"], total, new_count)
            record_search_success(kw["id"])
        except MarketplaceError as exc:
            errors = int(kw.get("error_count") or 0) + 1
            logger.warning(
                "Zoekactie '%s' mislukt (%s op rij, volgende poging %s): %s",
                kw["term"], errors,
                format_next_run(now + effective_interval({**kw, "error_count": errors}, settings, now), now),
                exc,
            )
            record_search_failure(kw, str(exc))
            continue
        except Exception as exc:
            logger.exception("Zoekactie voor '%s' mislukt", kw.get("term"))
            record_search_failure(kw, f"{type(exc).__name__}: {exc}")
            continue
        time_module.sleep(SEARCH_SPACING_SECONDS)

    return 60


def scheduler_loop() -> None:
    while True:
        try:
            pause = run_scheduler_iteration()
        except Exception:
            logger.exception("Onverwachte fout in scheduler")
            pause = 60
        time_module.sleep(pause)


def start_background_worker() -> None:
    global _worker_thread_started
    if os.environ.get("MPWATCHER_DISABLE_WORKER") == "1":
        return
    with _worker_lock:
        if not _worker_thread_started:
            t = threading.Thread(target=scheduler_loop, daemon=True, name="watcher-scheduler")
            t.start()
            _worker_thread_started = True
            logger.debug("Planner voor zoekopdrachten gestart")
