"""
JSON-API voor de zoekopdrachten (de MPWatcher-functies), voor het tabblad
Zoekopdrachten. De logica zelf staat in watcher/; dit is alleen de vertaling
naar wat de pagina nodig heeft.
"""
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request

import store
import notify
from watcher import db, scheduler
from watcher import marketplace as mp
from watcher.config import RESULTS_PAGE_SIZE, MarketplaceError, logger
from watcher.utils import (
    clean_search_term,
    format_relative_time,
    int_or_none,
    normalize_terms,
    normalize_title,
)

bp = Blueprint("watch", __name__, url_prefix="/api/watch")

NOTIFICATION_KINDS = {
    "new": "Nieuw",
    "price_drop": "Prijsverlaging",
    "digest": "Samenvatting",
    "suppressed": "Onderdrukt",
    "silent": "Stille run",
}

class _ToMPManagerLog(logging.Handler):
    """Wat de zoekopdrachten loggen komt in het log van MPManager."""

    def emit(self, record):
        try:
            store.log("Zoeken: " + record.getMessage())
        except Exception:
            pass


def setup():
    """At the start: database ready, a stand-alone MPWatcher taken over, scheduler running."""
    if not any(isinstance(h, _ToMPManagerLog) for h in logger.handlers):
        logger.addHandler(_ToMPManagerLog(level=logging.INFO))
        logger.propagate = False
    imported = db.import_mpwatcher()
    db.init_db()
    scheduler.start_background_worker()
    return imported


def legacy_settings():
    return db.take_legacy_settings()


def drop_blocked_titles(titles):
    return db.delete_results_with_titles(titles)


@bp.errorhandler(MarketplaceError)
def _marketplace_error(e):
    return jsonify({"error": str(e)}), 502


def _body():
    return request.get_json(silent=True) or {}


def _keyword_or_404(keyword_id):
    kw = db.get_keyword(keyword_id)
    if not kw:
        return None, (jsonify({"error": "Zoekwoord niet gevonden."}), 404)
    return kw, None


def _keyword_view(kw, settings, counts, now):
    return {
        **kw,
        "mp_url": mp.build_search_url(kw["term"], scheduler.keyword_settings(kw, settings)),
        "result_count": counts.get(kw["id"], 0),
        "last_run_display": format_relative_time(kw["last_run_at"]),
        "next_run_display": scheduler.format_next_run(scheduler.compute_next_run(kw, settings, now), now),
    }


def _ad_view(ad):
    return {**ad, "first_seen_display": format_relative_time(ad.get("first_seen_at"))}


# ---- overview ------------------------------------------------------------

@bp.get("")
def overview():
    settings = db.load_settings()
    keywords = db.load_keywords()
    counts = db.get_result_counts()
    now = datetime.now()
    return jsonify({
        "keywords": [_keyword_view(kw, settings, counts, now) for kw in keywords],
        "recent": [_ad_view(a) for a in db.get_recent_results(limit=10)],
        "scheduler": scheduler.get_scheduler_status(),
        "in_sleep": scheduler.in_sleep_now(settings, now),
        "error_count": sum(1 for kw in keywords if kw.get("last_error")),
        "telegram_configured": notify.configured(),
        "defaults": {"interval": settings["default_interval_minutes"], "limit": settings["default_limit_per_run"],
                     "marketplace": settings["marketplace"]},
    })


# ---- keywords ------------------------------------------------------------

@bp.post("/keywords")
def add_keyword():
    data = _body()
    settings = db.load_settings()
    term = clean_search_term(data.get("term"))
    if not term:
        return jsonify({"error": "Zoekwoord mag niet leeg zijn."}), 400
    if db.keyword_exists(term):
        return jsonify({"error": f"Zoekwoord '{term}' bestaat al."}), 400
    kid = db.add_keyword_row(
        term=term,
        interval_minutes=settings["default_interval_minutes"],
        min_price=int_or_none(data.get("min_price")),
        max_price=int_or_none(data.get("max_price")),
        limit_per_run=settings["default_limit_per_run"],
        exclude_terms=data.get("exclude_terms", ""),
        include_terms=data.get("include_terms", ""),
    )
    store.log(f"Zoekwoord '{term}' toegevoegd.")
    # First search right away, silently: the results page is filled without a Telegram burst of old ads.
    kw = db.get_keyword(kid)
    try:
        total, _ = scheduler.run_search_for_keyword(kw, settings, manual=True)
        scheduler.record_search_success(kid)
        return jsonify({"id": kid, "message": f"'{term}' toegevoegd; {total} bestaande advertenties stil opgeslagen."})
    except MarketplaceError as exc:
        scheduler.record_search_failure(kw, str(exc))
        return jsonify({"id": kid, "message": f"'{term}' toegevoegd, maar de eerste zoekactie mislukte: {exc}", "warning": True})


@bp.patch("/keywords/<int:keyword_id>")
def edit_keyword(keyword_id):
    data = _body()
    fields = {}
    if "term" in data:
        term = clean_search_term(data.get("term"))
        if not term:
            return jsonify({"error": "Zoekwoord mag niet leeg zijn."}), 400
        fields["term"] = term
    if "interval_minutes" in data:
        value = int_or_none(data.get("interval_minutes"))
        if value:
            fields["interval_minutes"] = max(1, value)
    for key in ("min_price", "max_price"):
        if key in data:
            fields[key] = int_or_none(data.get(key))
    if "limit_per_run" in data:
        value = int_or_none(data.get("limit_per_run"))
        if value:
            fields["limit_per_run"] = max(1, min(20, value))
    for key in ("exclude_terms", "include_terms"):
        if key in data:
            fields[key] = normalize_terms(data.get(key))
    if "marketplace" in data:
        choice = (data.get("marketplace") or "").strip().lower()
        fields["marketplace"] = choice if choice in ("marktplaats", "2dehands") else ""
    if not db.update_keyword_fields(keyword_id, **fields):
        return jsonify({"error": "Zoekwoord niet gevonden."}), 404
    return jsonify(db.get_keyword(keyword_id))


@bp.post("/keywords/<int:keyword_id>/search")
def manual_search(keyword_id):
    kw, missing = _keyword_or_404(keyword_id)
    if missing:
        return missing
    try:
        total, new_count = scheduler.run_search_for_keyword(kw, db.load_settings(), manual=True)
    except MarketplaceError as exc:
        scheduler.record_search_failure(kw, str(exc))
        raise
    scheduler.record_search_success(keyword_id)
    return jsonify({"message": f"Zoekactie voor '{kw['term']}': {total} resultaten, {new_count} nieuw."})


@bp.post("/keywords/<int:keyword_id>/reset")
def reset_keyword(keyword_id):
    kw, missing = _keyword_or_404(keyword_id)
    if missing:
        return missing
    db.reset_results_for_keyword(keyword_id)
    db.update_keyword_fields(keyword_id, last_run_at=None)
    store.log(f"Resultaten van zoekwoord '{kw['term']}' gereset; de volgende zoekactie is weer stil.")
    return jsonify({"ok": True})


@bp.delete("/keywords/<int:keyword_id>")
def delete_keyword(keyword_id):
    kw, missing = _keyword_or_404(keyword_id)
    if missing:
        return missing
    db.delete_keyword_row(keyword_id)
    db.reset_results_for_keyword(keyword_id)
    store.log(f"Zoekwoord '{kw['term']}' verwijderd.")
    return jsonify({"ok": True})


@bp.get("/keywords/<int:keyword_id>/results")
def results(keyword_id):
    kw, missing = _keyword_or_404(keyword_id)
    if missing:
        return missing
    query = (request.args.get("q") or "").strip()
    offset = max(0, int_or_none(request.args.get("offset")) or 0)
    return jsonify({
        "keyword": kw,
        "total": db.count_results_for_keyword(keyword_id, query),
        "ads": [_ad_view(a) for a in db.get_results_for_keyword(keyword_id, limit=RESULTS_PAGE_SIZE,
                                                                 offset=offset, query=query)],
        "offset": offset,
        "page_size": RESULTS_PAGE_SIZE,
    })


@bp.get("/keywords/<int:keyword_id>/categories")
def categories(keyword_id):
    kw, missing = _keyword_or_404(keyword_id)
    if missing:
        return missing
    return jsonify({"categories": mp.fetch_categories(kw["term"], scheduler.keyword_settings(kw, db.load_settings()))})


@bp.post("/keywords/<int:keyword_id>/category")
def set_category(keyword_id):
    kw, missing = _keyword_or_404(keyword_id)
    if missing:
        return missing
    data = _body()
    cid = int_or_none(data.get("category_id"))
    if cid is None:
        db.update_keyword_fields(keyword_id, category_id=None, category_parent_id=None, category_label="")
        return jsonify({"message": f"Categoriefilter voor '{kw['term']}' verwijderd."})
    label = (data.get("category_label") or str(cid)).strip()[:120]
    db.update_keyword_fields(keyword_id, category_id=cid, category_parent_id=int_or_none(data.get("category_parent_id")),
                             category_label=label)
    return jsonify({"message": f"'{kw['term']}' zoekt voortaan alleen in '{label}'."})


# ---- blocklists ----------------------------------------------------------

@bp.post("/block/seller")
def block_seller():
    seller = (_body().get("seller") or "").strip()
    if not seller:
        return jsonify({"error": "Geen verkoper."}), 400
    current = db.load_settings().get("blocked_sellers") or []
    if seller.lower() not in {str(x).strip().lower() for x in current}:
        current.append(seller)
    db.save_settings({"blocked_sellers": current})
    store.log(f"Zoeken: verkoper '{seller}' geblokkeerd.")
    return jsonify({"message": f"Verkoper '{seller}' wordt voortaan genegeerd."})


@bp.post("/block/title")
def block_title():
    title = (_body().get("title") or "").strip()
    if not title:
        return jsonify({"error": "Geen titel."}), 400
    current = db.load_settings().get("blocked_titles") or []
    if normalize_title(title) not in {normalize_title(x) for x in current}:
        current.append(title)
    db.save_settings({"blocked_titles": current})
    removed = db.delete_results_with_titles([title])
    return jsonify({"message": f"'{title[:60]}' wordt voortaan genegeerd" + (f" ({removed} resultaten verwijderd)." if removed else ".")})


# ---- notifications log ---------------------------------------------------

@bp.get("/notifications")
def notifications():
    kind = (request.args.get("kind") or "").strip()
    items = db.get_notifications(limit=200, kind=kind if kind in NOTIFICATION_KINDS else "",
                                 keyword_id=int_or_none(request.args.get("keyword_id")),
                                 query=(request.args.get("q") or "").strip())
    for it in items:
        it["created_display"] = format_relative_time(it.get("created_at"))
        it["kind_label"] = NOTIFICATION_KINDS.get(it.get("kind"), it.get("kind"))
    return jsonify({"items": items, "kinds": NOTIFICATION_KINDS})
