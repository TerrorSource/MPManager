"""SQLite-laag: migraties, instellingen, zoekwoorden en resultaten."""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from .config import (
    CONFIG_DIR,
    DB_FILE,
    DEFAULT_SETTINGS,
    MAX_RESULTS_PER_KEYWORD,
    MPWATCHER_DB_FILE,
    NOTIFICATIONS_KEEP,
    logger,
)
from .marketplace import parse_posted_at_to_dt, parse_price_to_cents
from .utils import int_or_none, normalize_terms, normalize_title


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------------------
# Schema & migraties
# ------------------------------------------------------------------------------

def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword_id INTEGER NOT NULL,
                    ad_id TEXT NOT NULL,
                    title TEXT,
                    price TEXT,
                    price_cents INTEGER,
                    url TEXT,
                    image_url TEXT,
                    seller TEXT,
                    location TEXT,
                    distance_km REAL,
                    reserved INTEGER,
                    seller_url TEXT,
                    attributes TEXT,
                    description TEXT,
                    expired INTEGER DEFAULT 0,
                    expired_at TEXT,
                    checked_at TEXT,
                    first_seen_at TEXT,
                    posted_at TEXT,
                    posted_ts TEXT,
                    UNIQUE(keyword_id, ad_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    term TEXT NOT NULL,
                    interval_minutes INTEGER,
                    min_price INTEGER,
                    max_price INTEGER,
                    limit_per_run INTEGER,
                    last_run_at TEXT,
                    last_error TEXT,
                    last_error_at TEXT,
                    error_count INTEGER DEFAULT 0,
                    exclude_terms TEXT,
                    include_terms TEXT,
                    category_id INTEGER,
                    category_parent_id INTEGER,
                    category_label TEXT,
                    attr_terms TEXT,
                    telegram_chat_id TEXT,
                    marketplace TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    keyword_id INTEGER,
                    term TEXT,
                    kind TEXT NOT NULL,
                    title TEXT,
                    url TEXT,
                    price TEXT,
                    detail TEXT,
                    sent INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            # Kolommen aanvullen voor databases uit oudere versies
            cols = [row[1] for row in conn.execute("PRAGMA table_info(results)")]
            for col, ctype in (
                ("posted_at", "TEXT"), ("seller", "TEXT"), ("posted_ts", "TEXT"),
                ("price_cents", "INTEGER"), ("location", "TEXT"),
                ("distance_km", "REAL"), ("reserved", "INTEGER"),
                ("seller_url", "TEXT"), ("attributes", "TEXT"), ("description", "TEXT"),
                ("expired", "INTEGER DEFAULT 0"), ("expired_at", "TEXT"), ("checked_at", "TEXT"),
            ):
                if col not in cols:
                    conn.execute(f"ALTER TABLE results ADD COLUMN {col} {ctype}")

            kw_cols = [row[1] for row in conn.execute("PRAGMA table_info(keywords)")]
            for col, ctype in (
                ("last_error", "TEXT"), ("exclude_terms", "TEXT"), ("include_terms", "TEXT"),
                ("category_id", "INTEGER"), ("category_parent_id", "INTEGER"), ("category_label", "TEXT"),
                ("last_error_at", "TEXT"), ("error_count", "INTEGER DEFAULT 0"),
                ("attr_terms", "TEXT"), ("telegram_chat_id", "TEXT"), ("marketplace", "TEXT"),
            ):
                if col not in kw_cols:
                    conn.execute(f"ALTER TABLE keywords ADD COLUMN {col} {ctype}")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_results_ad_id ON results(ad_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_results_first_seen ON results(first_seen_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at)")

        _backfill_posted_ts(conn)
        _backfill_price_cents(conn)
    finally:
        conn.close()


def import_mpwatcher() -> bool:
    """
    Neem de database van een losse MPWatcher-installatie over: zoekwoorden,
    resultaten, meldingenlog en instellingen. Alleen als er nog geen eigen
    database is; het origineel blijft onaangeroerd (de backup-API van SQLite
    leest ook wat nog in een -wal-bestand staat).
    """
    if DB_FILE.exists() or not MPWATCHER_DB_FILE.exists():
        return False
    src = sqlite3.connect(f"file:{MPWATCHER_DB_FILE}?mode=ro", uri=True)
    dst = sqlite3.connect(DB_FILE)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    logger.info("Zoekopdrachten van MPWatcher overgenomen uit %s", MPWATCHER_DB_FILE.name)
    return True


def _backfill_posted_ts(conn: sqlite3.Connection) -> None:
    """Vul posted_ts (genormaliseerde sorteertijd) voor rijen uit oudere versies."""
    rows = conn.execute(
        "SELECT id, posted_at, first_seen_at FROM results WHERE posted_ts IS NULL"
    ).fetchall()
    if not rows:
        return
    updates = [
        (parse_posted_at_to_dt(r["posted_at"], r["first_seen_at"]).isoformat(timespec="seconds"), r["id"])
        for r in rows
    ]
    with conn:
        conn.executemany("UPDATE results SET posted_ts = ? WHERE id = ?", updates)
    logger.info("posted_ts ingevuld voor %s bestaande resultaten", len(updates))


def _backfill_price_cents(conn: sqlite3.Connection) -> None:
    """Vul price_cents (voor prijsverlaging-detectie) voor rijen uit oudere versies."""
    rows = conn.execute(
        "SELECT id, price FROM results WHERE price_cents IS NULL AND price != ''"
    ).fetchall()
    updates = []
    for row in rows:
        cents = parse_price_to_cents({}, row["price"])
        if cents is not None:
            updates.append((cents, row["id"]))
    if updates:
        with conn:
            conn.executemany("UPDATE results SET price_cents = ? WHERE id = ?", updates)
        logger.info("price_cents ingevuld voor %s bestaande resultaten", len(updates))


def config_writable() -> bool:
    """Controleer of de config-map (en dus de database) beschrijfbaar is.
    Vangt bijv. NAS-permissieproblemen die pas na de start ontstaan."""
    try:
        probe = CONFIG_DIR / ".health-write-test"
        probe.write_text("ok")
        probe.unlink()
        return os.access(DB_FILE, os.W_OK)
    except Exception:
        return False


# ------------------------------------------------------------------------------
# Instellingen
# ------------------------------------------------------------------------------

def load_settings() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()

    merged = {
        k: (v.copy() if isinstance(v, (list, dict)) else v)
        for k, v in DEFAULT_SETTINGS.items()
    }
    for row in rows:
        if row["key"] not in DEFAULT_SETTINGS:
            continue
        try:
            merged[row["key"]] = json.loads(row["value"])
        except Exception:
            logger.warning("Instelling '%s' kon niet gelezen worden, default gebruikt", row["key"])

    merged["default_interval_minutes"] = int_or_none(merged.get("default_interval_minutes")) or 15
    merged["default_limit_per_run"] = int_or_none(merged.get("default_limit_per_run")) or 5

    mp = str(merged.get("marketplace") or "marktplaats").strip().lower()
    merged["marketplace"] = "2dehands" if mp in ("2dehands", "2dehands.be", "2dehandsbe") else "marktplaats"

    for key in ("sleep_mode", "manual_telegram", "price_drop_alerts", "skip_reserved"):
        merged[key] = bool(merged.get(key))

    digest = int_or_none(merged.get("telegram_digest_threshold"))
    merged["telegram_digest_threshold"] = max(0, digest) if digest is not None else 5

    repost = int_or_none(merged.get("repost_dedup_days"))
    merged["repost_dedup_days"] = max(0, repost) if repost is not None else 30

    expiry = int_or_none(merged.get("expiry_check_days"))
    merged["expiry_check_days"] = max(0, min(90, expiry)) if expiry is not None else 7

    mode = str(merged.get("match_mode") or "text").strip().lower()
    merged["match_mode"] = mode if mode in ("text", "title", "off") else "text"

    for key in ("blocked_sellers", "blocked_titles"):
        if not isinstance(merged.get(key), list):
            merged[key] = []

    return merged


def save_settings(values: dict) -> None:
    """Sla (alleen) de meegegeven instellingen op; per key een upsert,
    zodat gelijktijdige schrijvers elkaars keys niet overschrijven."""
    if not values:
        return
    conn = connect()
    try:
        with conn:
            conn.executemany(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                [(k, json.dumps(v)) for k, v in values.items()],
            )
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Zoekwoorden
# ------------------------------------------------------------------------------

def _row_to_keyword(row: sqlite3.Row) -> dict:
    keys = row.keys()

    def col(name, default=None):
        return row[name] if name in keys else default

    return {
        "id": row["id"],
        "term": row["term"],
        "interval_minutes": row["interval_minutes"],
        "min_price": row["min_price"],
        "max_price": row["max_price"],
        "limit_per_run": row["limit_per_run"],
        "last_run_at": row["last_run_at"] or "Nooit",
        "last_error": col("last_error") or "",
        "last_error_at": col("last_error_at"),
        "error_count": col("error_count") or 0,
        "exclude_terms": col("exclude_terms") or "",
        "include_terms": col("include_terms") or "",
        "category_id": col("category_id"),
        "category_parent_id": col("category_parent_id"),
        "category_label": col("category_label") or "",
        "attr_terms": col("attr_terms") or "",
        "telegram_chat_id": col("telegram_chat_id") or "",
        "marketplace": col("marketplace") or "",
    }


def load_keywords() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM keywords ORDER BY id").fetchall()
    finally:
        conn.close()
    return [_row_to_keyword(r) for r in rows]


def get_keyword(keyword_id: int) -> Optional[dict]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_keyword(row) if row else None


def keyword_exists(term: str) -> bool:
    """Bestaat er al een zoekwoord met deze term (hoofdletter-ongevoelig)?"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM keywords WHERE LOWER(TRIM(term)) = ? LIMIT 1",
            ((term or "").strip().lower(),),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def add_keyword_row(
    term: str, interval_minutes, min_price, max_price, limit_per_run,
    exclude_terms: str = "", include_terms: str = "", attr_terms: str = "",
) -> int:
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO keywords
                    (term, interval_minutes, min_price, max_price, limit_per_run,
                     last_run_at, exclude_terms, include_terms, attr_terms)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    term, interval_minutes, min_price, max_price, limit_per_run,
                    normalize_terms(exclude_terms), normalize_terms(include_terms), normalize_terms(attr_terms),
                ),
            )
            return cur.lastrowid
    finally:
        conn.close()


def update_keyword_fields(keyword_id: int, **fields) -> bool:
    """Update alleen de meegegeven kolommen; scheduler (last_run_at) en UI-edits
    kunnen elkaar zo niet overschrijven."""
    if not fields:
        return get_keyword(keyword_id) is not None
    assignments = ", ".join(f"{col} = ?" for col in fields)
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                f"UPDATE keywords SET {assignments} WHERE id = ?",
                (*fields.values(), keyword_id),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def delete_keyword_row(keyword_id: int) -> bool:
    conn = connect()
    try:
        with conn:
            cur = conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Resultaten
# ------------------------------------------------------------------------------

def get_known_ad_ids(keyword_id: int) -> set[str]:
    conn = connect()
    try:
        rows = conn.execute("SELECT ad_id FROM results WHERE keyword_id = ?", (keyword_id,)).fetchall()
    finally:
        conn.close()
    return {row["ad_id"] for row in rows}


def get_result_counts() -> dict[int, int]:
    """Aantal opgeslagen resultaten per zoekwoord (voor het overzicht)."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT keyword_id, COUNT(*) AS n FROM results GROUP BY keyword_id"
        ).fetchall()
    finally:
        conn.close()
    return {row["keyword_id"]: row["n"] for row in rows}


def find_price_drops(keyword_id: int, ads: list[dict]) -> list[tuple[dict, int, int]]:
    """Vergelijk verse prijzen van bekende advertenties met de opgeslagen prijs.
    Geeft (ad, oude_centen, nieuwe_centen) terug voor elke prijsverlaging.
    Aanroepen vóór update_known_ads, anders is de oude prijs al overschreven."""
    if not ads:
        return []

    ad_ids = [ad["ad_id"] for ad in ads]
    placeholders = ",".join("?" for _ in ad_ids)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT ad_id, price_cents FROM results WHERE keyword_id = ? AND ad_id IN ({placeholders})",
            (keyword_id, *ad_ids),
        ).fetchall()
        stored: dict[str, Optional[int]] = {row["ad_id"]: row["price_cents"] for row in rows}
    finally:
        conn.close()

    drops = []
    for ad in ads:
        new_c = ad.get("price_cents")
        old_c = stored.get(ad["ad_id"])
        if isinstance(new_c, int) and isinstance(old_c, int) and new_c < old_c:
            drops.append((ad, old_c, new_c))
    return drops


def find_recent_repost(title: str, seller: str, price_cents: Optional[int], days: int) -> bool:
    """Is dezelfde advertentie (titel + verkoper + prijs) recent al gezien onder
    een ander id? Marktplaats-verkopers plaatsen advertenties vaak opnieuw."""
    want = normalize_title(title)
    if not days or not want:
        return False
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    seller_l = (seller or "").strip().lower()
    conn = connect()
    try:
        # Grove voorselectie in SQL (periode + eerste woord van de titel), de
        # precieze titelvergelijking (spaties, hoofdletters) in Python.
        first_word = want.split(" ", 1)[0]
        rows = conn.execute(
            """
            SELECT title, seller, price_cents FROM results
            WHERE first_seen_at >= ? AND LOWER(title) LIKE ?
            """,
            (cutoff, f"%{first_word}%"),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        if normalize_title(row["title"]) != want:
            continue
        stored_seller = (row["seller"] or "").strip().lower()
        if seller_l and stored_seller and stored_seller != seller_l:
            continue
        if price_cents is not None and row["price_cents"] is not None and row["price_cents"] != price_cents:
            continue
        return True
    return False


def insert_new_ads(keyword_id: int, ads: list[dict]) -> list[dict]:
    """Voeg nieuwe advertenties toe. Markeert per ad of dezelfde advertentie al
    via een ander zoekwoord bekend was (dan geen dubbele Telegram-melding)."""
    if not ads:
        return []

    inserted: list[dict] = []
    conn = connect()
    try:
        with conn:
            cur = conn.cursor()
            for ad in ads:
                now_iso = datetime.now().isoformat(timespec="seconds")
                posted_ts = parse_posted_at_to_dt(ad.get("posted_at"), now_iso).isoformat(timespec="seconds")

                seen_elsewhere = cur.execute(
                    "SELECT 1 FROM results WHERE ad_id = ? AND keyword_id != ? LIMIT 1",
                    (ad["ad_id"], keyword_id),
                ).fetchone() is not None

                try:
                    cur.execute(
                        """
                        INSERT INTO results
                            (keyword_id, ad_id, title, price, price_cents, url, image_url, seller,
                             location, distance_km, reserved, seller_url, attributes, description,
                             first_seen_at, posted_at, posted_ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            keyword_id, ad["ad_id"], ad.get("title", ""), ad.get("price", ""),
                            ad.get("price_cents"),
                            ad.get("url", ""), ad.get("image_url", ""), ad.get("seller", ""),
                            ad.get("location", ""), ad.get("distance_km"), 1 if ad.get("reserved") else 0,
                            ad.get("seller_url", ""), ad.get("attributes", ""), ad.get("description", ""),
                            now_iso, ad.get("posted_at", ""), posted_ts,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue

                ad["_seen_elsewhere"] = seen_elsewhere
                inserted.append(ad)
    finally:
        conn.close()

    return inserted


def update_known_ads(keyword_id: int, ads: list[dict]) -> None:
    """Ververs velden van reeds bekende advertenties met verse API-data."""
    if not ads:
        return

    conn = connect()
    try:
        with conn:
            for ad in ads:
                posted_at = ad.get("posted_at", "")
                posted_ts = ""
                if posted_at:
                    dt = parse_posted_at_to_dt(posted_at, None)
                    if dt != datetime.min:
                        posted_ts = dt.isoformat(timespec="seconds")
                conn.execute(
                    """
                    UPDATE results
                    SET
                        title = COALESCE(NULLIF(?, ''), title),
                        price = COALESCE(NULLIF(?, ''), price),
                        price_cents = COALESCE(?, price_cents),
                        url = COALESCE(NULLIF(?, ''), url),
                        image_url = CASE
                            WHEN (image_url IS NULL OR image_url = '') AND ? != '' THEN ?
                            ELSE image_url
                        END,
                        posted_at = CASE
                            WHEN (posted_at IS NULL OR posted_at = '') AND ? != '' THEN ?
                            ELSE posted_at
                        END,
                        posted_ts = CASE
                            WHEN (posted_at IS NULL OR posted_at = '') AND ? != '' THEN ?
                            ELSE posted_ts
                        END,
                        seller = CASE
                            WHEN (seller IS NULL OR seller = '') AND ? != '' THEN ?
                            ELSE seller
                        END,
                        location = CASE
                            WHEN (location IS NULL OR location = '') AND ? != '' THEN ?
                            ELSE location
                        END,
                        distance_km = COALESCE(?, distance_km),
                        reserved = ?,
                        seller_url = COALESCE(NULLIF(?, ''), seller_url),
                        attributes = COALESCE(NULLIF(?, ''), attributes),
                        description = COALESCE(NULLIF(?, ''), description),
                        expired = 0, expired_at = NULL
                    WHERE keyword_id = ? AND ad_id = ?
                    """,
                    (
                        ad.get("title", ""), ad.get("price", ""), ad.get("price_cents"),
                        ad.get("url", ""),
                        ad.get("image_url", ""), ad.get("image_url", ""),
                        posted_at, posted_at,
                        posted_ts, posted_ts,
                        ad.get("seller", ""), ad.get("seller", ""),
                        ad.get("location", ""), ad.get("location", ""),
                        ad.get("distance_km"), 1 if ad.get("reserved") else 0,
                        ad.get("seller_url", ""), ad.get("attributes", ""), ad.get("description", ""),
                        keyword_id, ad["ad_id"],
                    ),
                )
    finally:
        conn.close()


def _results_where(keyword_id: int, query: str) -> tuple[str, list]:
    where = "keyword_id = ?"
    params: list = [keyword_id]
    q = (query or "").strip()
    if q:
        where += " AND (title LIKE ? OR seller LIKE ? OR location LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    return where, params


def get_results_for_keyword(keyword_id: int, limit: int = 100, offset: int = 0, query: str = "") -> list[dict]:
    where, params = _results_where(keyword_id, query)
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT title, price, url, image_url, seller, seller_url, location, distance_km, reserved,
                   attributes, description, expired, expired_at, first_seen_at, posted_at
            FROM results
            WHERE {where}
            ORDER BY COALESCE(posted_ts, first_seen_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, max(0, offset)),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count_results_for_keyword(keyword_id: int, query: str = "") -> int:
    where, params = _results_where(keyword_id, query)
    conn = connect()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM results WHERE {where}", params).fetchone()[0]
    finally:
        conn.close()


def get_recent_results(limit: int = 10) -> list[dict]:
    """De laatst gevonden advertenties over alle zoekwoorden heen (voor het overzicht)."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT r.title, r.price, r.url, r.image_url, r.seller, r.location,
                   r.distance_km, r.reserved, r.expired, r.first_seen_at, r.keyword_id, k.term
            FROM results r
            LEFT JOIN keywords k ON k.id = r.keyword_id
            ORDER BY r.first_seen_at DESC, r.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def reset_results_for_keyword(keyword_id: int) -> None:
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM results WHERE keyword_id = ?", (keyword_id,))
    finally:
        conn.close()


def delete_results_with_titles(titles: list[str]) -> int:
    """Verwijder bestaande resultaten waarvan de titel op de titel-blocklist staat."""
    blocked = {normalize_title(t) for t in titles if (t or "").strip()}
    if not blocked:
        return 0
    conn = connect()
    try:
        rows = conn.execute("SELECT id, title FROM results").fetchall()
        doomed = [row["id"] for row in rows if normalize_title(row["title"]) in blocked]
        if doomed:
            with conn:
                conn.executemany("DELETE FROM results WHERE id = ?", [(i,) for i in doomed])
        return len(doomed)
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Verlopen advertenties
# ------------------------------------------------------------------------------

def get_results_to_check(days: int, limit: int, recheck_after_hours: int = 24) -> list[dict]:
    """Recente, nog niet als verlopen gemarkeerde advertenties die (opnieuw)
    gecontroleerd mogen worden; oudste controle eerst."""
    now = datetime.now()
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    recheck = (now - timedelta(hours=recheck_after_hours)).isoformat(timespec="seconds")
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, url, title FROM results
            WHERE (expired IS NULL OR expired = 0)
              AND first_seen_at >= ?
              AND (checked_at IS NULL OR checked_at < ?)
            ORDER BY checked_at IS NOT NULL, checked_at, id
            LIMIT ?
            """,
            (cutoff, recheck, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def mark_results_checked(ids: list[int], expired_ids: list[int]) -> None:
    if not ids:
        return
    now_iso = datetime.now().isoformat(timespec="seconds")
    expired = set(expired_ids)
    conn = connect()
    try:
        with conn:
            conn.executemany(
                "UPDATE results SET checked_at = ? WHERE id = ?",
                [(now_iso, i) for i in ids],
            )
            if expired:
                conn.executemany(
                    "UPDATE results SET expired = 1, expired_at = ? WHERE id = ?",
                    [(now_iso, i) for i in expired],
                )
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Meldingenlogboek
# ------------------------------------------------------------------------------

def log_notification(
    kind: str, keyword_id: Optional[int], term: str, ad: Optional[dict] = None,
    detail: str = "", sent: bool = False,
) -> None:
    """Registreer een (al dan niet verstuurde) melding, zodat de UI kan tonen
    wat er gemeld of juist onderdrukt is en waarom."""
    ad = ad or {}
    conn = connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO notifications (created_at, keyword_id, term, kind, title, url, price, detail, sent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"), keyword_id, term, kind,
                    (ad.get("title") or "")[:200], ad.get("url") or "", ad.get("price") or "",
                    detail[:200], 1 if sent else 0,
                ),
            )
    finally:
        conn.close()


def get_notifications(
    limit: int = 200, kind: str = "", keyword_id: Optional[int] = None, query: str = "",
) -> list[dict]:
    where = ["1=1"]
    params: list = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if keyword_id is not None:
        where.append("keyword_id = ?")
        params.append(keyword_id)
    q = (query or "").strip()
    if q:
        where.append("(title LIKE ? OR detail LIKE ? OR term LIKE ?)")
        params += [f"%{q}%"] * 3
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM notifications WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def prune_notifications() -> None:
    conn = connect()
    try:
        with conn:
            conn.execute(
                """
                DELETE FROM notifications
                WHERE id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT ?)
                """,
                (NOTIFICATIONS_KEEP,),
            )
    finally:
        conn.close()


def prune_results_for_keyword(keyword_id: int) -> None:
    """Houd maximaal MAX_RESULTS_PER_KEYWORD resultaten per zoekwoord."""
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                """
                DELETE FROM results
                WHERE keyword_id = ?
                  AND id NOT IN (
                      SELECT id FROM results
                      WHERE keyword_id = ?
                      ORDER BY first_seen_at DESC, id DESC
                      LIMIT ?
                  )
                """,
                (keyword_id, keyword_id, MAX_RESULTS_PER_KEYWORD),
            )
            if cur.rowcount:
                logger.info("%s oude resultaten opgeruimd voor zoekwoord %s", cur.rowcount, keyword_id)
    finally:
        conn.close()
