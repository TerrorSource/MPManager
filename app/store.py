"""
Persistence: the activity log, the queue, the settings, the repost history and
the backups on disk.

One folder per ad:

  backups/<itemId>_<title>/
      ad.json      the ad, readable on top, the site's own data below
      meta.json    what the overview needs (title, price, photo list, dates)
      photos/      001.jpg, 002.jpg, ... the original uploads, in order
"""
import json
import os
import re
import shutil
import threading
import time
from collections import deque

BACKUP_DIR = os.environ.get("BACKUP_DIR", "/data/backups")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
HISTORY_PATH = os.path.join(CONFIG_DIR, "history.json")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "mpmanager.json")
QUEUE_PATH = os.path.join(CONFIG_DIR, "queue.json")
LOG_PATH = os.path.join(CONFIG_DIR, "mpmanager.log")

# ---- log -----------------------------------------------------------------
#
# Shown on the Logs tab, printed for `docker logs`, and kept in a file so it
# is still there after a restart: that is when you want to read it.

LOG_KEEP = 500
LOG_MAX_BYTES = 1_000_000
LOG = deque(maxlen=LOG_KEEP)
_log_lock = threading.Lock()


def _load_log():
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            LOG.extend(line.rstrip("\n") for line in deque(f, maxlen=LOG_KEEP))
    except OSError:
        pass


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _log_lock:
        LOG.append(line)
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass                          # a full or read-only disk must not stop the work


_load_log()


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---- queue ---------------------------------------------------------------

def load_queue():
    jobs = _read(QUEUE_PATH, []) or []
    return [j for j in jobs if isinstance(j, dict) and "id" in j and "kind" in j]


def save_queue(jobs):
    try:
        _atomic_write(QUEUE_PATH, jobs)
    except OSError as e:
        log(f"Wachtrij niet op schijf bewaard: {e}")


# ---- settings ------------------------------------------------------------
#
# Every setting of the whole tool, in one file. Per key: the default, how a
# value is read (see _parse) and, for some, the docker-compose variable that
# gives a starting value until Settings is saved on the page.

_HOURS = r"([01]\d|2[0-3]):[0-5]\d"

SCHEMA = {
    # your own ads
    "dry_run": (True, "bool", "DRY_RUN"),
    "auto_backup": (True, "bool", "AUTO_BACKUP"),
    "auto_repost": (False, "bool", "AUTO_REPOST"),
    "min_age_days": (14, "int:0:3650", "MIN_ITEM_AGE_DAYS"),       # only repost ads that have been up this long
    "max_per_day": (3, "int:0:1000", "MAX_REPOSTS_PER_DAY"),
    "active_hours": ("09:00-22:00", "hours", "ACTIVE_HOURS"),
    "auto_delay_min": (120, "int:0:86400", "MIN_DELAY_SECONDS"),   # between automatic reposts
    "auto_delay_max": (600, "int:0:86400", "MAX_DELAY_SECONDS"),
    "manual_delay_min": (30, "int:0:86400", "MANUAL_DELAY_MIN_SECONDS"),   # a clicked job waits this long
    "manual_delay_max": (90, "int:0:86400", "MANUAL_DELAY_MAX_SECONDS"),
    "exclude": ([], "ids", None),                                  # ads automatic reposting leaves alone
    "backup_retention_days": (0, "int:0:36500", "BACKUP_RETENTION_DAYS"),  # 0 = keep forever
    # Telegram (the bot token itself is kept encrypted, with the login)
    "telegram_chat_id": ("", "text", "TELEGRAM_CHAT_ID"),
    "notify_on": ("off", "choice:off,failures,all", "NOTIFY_ON"),
    # searches
    "marketplace": ("marktplaats", "choice:marktplaats,2dehands", None),
    "default_interval_minutes": (15, "int:1:1440", None),
    "default_limit_per_run": (5, "int:1:20", None),
    "match_mode": ("text", "choice:text,title,off", None),         # all words of the term must be in text / title
    "repost_dedup_days": (30, "int:0:365", None),                  # a seller's own repost is not reported again
    "expiry_check_days": (7, "int:0:90", None),
    "skip_reserved": (False, "bool", None),
    "postcode": ("", "postcode", None),
    "radius_km": ("alle", "choice:alle,3,5,10,15,25,50,75", None),
    "sleep_mode": (False, "bool", None),
    "sleep_start": ("23:00", "time", None),
    "sleep_end": ("07:00", "time", None),
    "price_drop_alerts": (True, "bool", None),
    "telegram_digest_threshold": (5, "int:0:100", None),           # one summary from this many new ads
    "manual_telegram": (False, "bool", None),
    "blocked_sellers": ([], "lines", None),
    "blocked_titles": ([], "lines", None),
}
DEFAULTS = {key: spec[0] for key, spec in SCHEMA.items()}


def _parse(kind, value):
    """One value as its kind wants it; ValueError if it is not acceptable."""
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "nee", "uit")
        return bool(value)
    if kind.startswith("int:"):
        _, lo, hi = kind.split(":")
        number = int(str(value).strip())
        if not int(lo) <= number <= int(hi):
            raise ValueError
        return number
    if kind.startswith("choice:"):
        value = str(value).strip().lower()
        if value not in kind[7:].split(","):
            raise ValueError
        return value
    if kind == "time":
        hh, mm = str(value).strip().split(":")
        value = f"{int(hh):02d}:{int(mm):02d}"
        if not re.fullmatch(_HOURS, value):
            raise ValueError
        return value
    if kind == "hours":
        value = str(value).strip()
        if not re.fullmatch(f"{_HOURS}-{_HOURS}", value):
            raise ValueError
        return value
    if kind == "postcode":
        value = str(value or "").replace(" ", "").upper()
        if value and not re.fullmatch(r"\d{4}([A-Z]{2})?", value):
            raise ValueError
        return value
    if kind == "ids":
        return sorted({str(v) for v in value if re.fullmatch(r"m\d+", str(v))})
    if kind == "lines":                   # a list, or a text box with one item per line; no doubles
        items = value.splitlines() if isinstance(value, str) else list(value or [])
        out, seen = [], set()
        for item in items:
            item = str(item).strip()
            key = re.sub(r"\s+", " ", item).casefold()
            if item and key not in seen:
                seen.add(key)
                out.append(item)
        return out
    return str(value).strip()


def _from_env():
    out = {}
    for key, (_, kind, name) in SCHEMA.items():
        raw = os.environ.get(name, "").strip() if name else ""
        if raw:
            try:
                out[key] = _parse(kind, raw)
            except (TypeError, ValueError):
                pass
    return out


def settings():
    s = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    s.update(_from_env())
    s.update({k: v for k, v in (_read(SETTINGS_PATH, {}) or {}).items() if k in SCHEMA})
    return s


def save_settings(changes):
    """Take over what is valid, ignore the rest; returns the settings and what was refused."""
    s, refused = settings(), []
    for key, value in changes.items():
        if key not in SCHEMA:
            continue
        try:
            s[key] = _parse(SCHEMA[key][1], value)
        except (TypeError, ValueError, AttributeError):
            refused.append(key)
    for lo, hi in (("auto_delay_min", "auto_delay_max"), ("manual_delay_min", "manual_delay_max")):
        s[hi] = max(s[lo], s[hi])
    _atomic_write(SETTINGS_PATH, s)
    return s, refused


def has_saved(key):
    """Is this setting in the file already (as opposed to still on its default)?"""
    return key in (_read(SETTINGS_PATH, {}) or {})


def set_excluded(item_id, excluded):
    current = set(settings()["exclude"])
    (current.add if excluded else current.discard)(item_id)
    return save_settings({"exclude": list(current)})[0]


# ---- history -------------------------------------------------------------

def history():
    return _read(HISTORY_PATH, []) or []


def record(action, title, old_id=None, new_id=None, note=None):
    h = history()
    h.insert(0, {"ts": int(time.time()), "action": action, "title": title,
                 "old_id": old_id, "new_id": new_id, "note": note})
    _atomic_write(HISTORY_PATH, h[:500])


def reposts_today():
    today = time.strftime("%Y-%m-%d")
    return sum(1 for h in history() if h.get("action") == "herplaatst"
               and time.strftime("%Y-%m-%d", time.localtime(h.get("ts", 0))) == today)


# ---- backups -------------------------------------------------------------

def _slug(title):
    return re.sub(r"[^A-Za-z0-9]+", "-", title or "").strip("-")[:50]


def _dir(item_id, title=None):
    """The folder of this ad; the title in its name is only for people."""
    if os.path.isdir(BACKUP_DIR):
        for name in os.listdir(BACKUP_DIR):
            if name == item_id or name.startswith(item_id + "_"):
                return os.path.join(BACKUP_DIR, name)
    slug = _slug(title)
    return os.path.join(BACKUP_DIR, f"{item_id}_{slug}" if slug else item_id)


def read_meta(item_id):
    return _read(os.path.join(_dir(item_id), "meta.json"))


def load_backup(item_id):
    return _read(os.path.join(_dir(item_id), "ad.json"))


def backup_photos(item_id):
    """Absolute paths of the backed-up photos, in order."""
    photo_dir = os.path.join(_dir(item_id), "photos")
    if not os.path.isdir(photo_dir):
        return []
    return [os.path.join(photo_dir, n) for n in sorted(os.listdir(photo_dir))
            if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]


def _photo_urls(ad):
    form, listing = ad.get("form") or {}, ad.get("listing") or {}
    return (((form.get("media") or {}).get("imageUpload") or {}).get("initialImageUrls")
            or (listing.get("gallery") or {}).get("imageUrls") or [])


def _price_text(listing):
    p = listing.get("price") or {}
    ptype, cents = p.get("priceType"), p.get("priceCent")
    if ptype == "FREE":
        return "Gratis"
    if ptype == "SEE_DESCRIPTION":
        return "Zie omschrijving"
    if cents is None:
        return "Bieden" if ptype == "FAST_BID" else ""
    amount = "€ " + f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"Bieden vanaf {amount}" if ptype == "MIN_BID" else amount


def _readable(ad):
    """The ad as a person would describe it, on top of the raw data."""
    listing, form = ad["listing"], ad.get("form") or {}
    cat = listing.get("categoryInformation") or {}
    price = listing.get("price") or {}
    return {
        "item_id": ad["item_id"],
        "title": listing.get("title"),
        "description": listing.get("description"),
        "price": _price_text(listing),
        "price_type": price.get("priceType"),
        "price_cents": price.get("priceCent"),
        "minimum_bid_cents": price.get("minimumBidPriceCent"),
        "bidding_enabled": price.get("biddingEnabled"),
        "category": " > ".join(x for x in (cat.get("parentCategoryName"), cat.get("categoryName")) if x),
        "category_id": cat.get("categoryId"),
        "attributes": {a.get("label"): a.get("value") for a in listing.get("attributes") or []},
        "delivery": listing.get("deliveryMethod"),
        "shipping": [s.get("code") for s in listing.get("shippingServices") or []],
        "package_size": next((p.get("title") for p in (form.get("shipping") or {}).get("shippingPackages") or []
                              if p.get("isSelected")), None),
        "site": ad.get("site") or "marktplaats",
        "url": ad.get("url") or f"https://www.marktplaats.nl/seller/view/{ad['item_id']}",
        "placed_at": (listing.get("startDate") or 0) // 1000 or None,
        "expires_at": (listing.get("closeDate") or 0) // 1000 or None,
    }


def _origin(item_id, count, old):
    """
    The photo files of the ad this one was reposted from, if they can stand in
    for this ad's own: the same number of them, and this ad has not had a
    complete set yet. Those files are what was uploaded; downloading them back
    gives a copy the site compressed once more, and that adds up over reposts.
    Returns (settled, origin): settled says this ad now has its first full set.
    """
    if old.get("photos_settled"):
        return True, None
    previous = next((m for m in list_backups() if m.get("reposted_to") == item_id), None)
    if not previous:
        return bool(count), None
    expected = len(previous.get("photos") or [])
    if count < expected:                  # the site is still processing the uploads
        return False, None
    paths = backup_photos(previous["item_id"])
    return True, ((previous["item_id"], paths) if count == expected == len(paths) else None)


def save_backup(client, ad):
    """
    Write the ad and its photos. Photos are only fetched again when the set on
    the site changed, checked on their URLs: file names stay 001, 002, ...
    whatever is in them. A reposted ad takes over the photo files of the ad it
    came from instead of downloading them.
    """
    item_id = ad["item_id"]
    summary = _readable(ad)
    d = _dir(item_id, summary["title"])
    photo_dir = os.path.join(d, "photos")
    os.makedirs(photo_dir, exist_ok=True)

    urls = _photo_urls(ad)
    old = read_meta(item_id) or {}
    settled, origin = _origin(item_id, len(urls), old)
    photos_from = old.get("photos_from") if old.get("photo_urls") == urls else None
    if old.get("photo_urls") != urls:
        for name in os.listdir(photo_dir):
            os.remove(os.path.join(photo_dir, name))

    saved = []
    for i, url in enumerate(urls, 1):
        name = f"{i:03d}.jpg"
        path = os.path.join(photo_dir, name)
        if not os.path.exists(path):
            if origin:
                shutil.copyfile(origin[1][i - 1], path + ".tmp")
                photos_from = origin[0]
            else:
                with open(path + ".tmp", "wb") as f:
                    f.write(client.download_photo(url))
            os.replace(path + ".tmp", path)
        saved.append(name)

    _atomic_write(os.path.join(d, "ad.json"), {**summary, "listing": ad["listing"], "form": ad.get("form")})
    meta = {
        "item_id": item_id,
        "title": summary["title"],
        "price": summary["price"],
        "category": summary["category"],
        "photos": saved,
        "photo_urls": urls,
        "backed_up_at": int(time.time()),
        "placed_at": summary["placed_at"],
        "partial": not ad.get("form"),
        "photos_from": photos_from,       # the ad whose original photo files these are, after a repost
        "photos_settled": settled,
    }
    for k in ("reposted_to", "reposted_at"):
        if k in old:
            meta[k] = old[k]
    _atomic_write(os.path.join(d, "meta.json"), meta)
    return meta


def mark_reposted(item_id, new_id):
    meta = read_meta(item_id)
    if meta:
        meta["reposted_to"], meta["reposted_at"] = new_id, int(time.time())
        _atomic_write(os.path.join(_dir(item_id), "meta.json"), meta)


def delete_backup(item_id):
    """Remove one backup folder. Touches nothing on the site."""
    target = os.path.realpath(_dir(item_id))
    if os.path.dirname(target) != os.path.realpath(BACKUP_DIR) or not os.path.isfile(os.path.join(target, "meta.json")):
        return False
    shutil.rmtree(target)
    log(f"Backup van {item_id} verwijderd.")
    return True


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for name in os.listdir(BACKUP_DIR):
        meta = _read(os.path.join(BACKUP_DIR, name, "meta.json"))
        if meta:
            meta["folder"] = name
            out.append(meta)
    out.sort(key=lambda m: m.get("backed_up_at", 0), reverse=True)
    return out


def prune_backups(max_age_days, live_ids):
    """
    Drop backups older than the retention window, but never of an ad that is
    still online: those are the ones a repost is made from.
    """
    if not max_age_days:
        return []
    cutoff = time.time() - max_age_days * 86400
    removed = [m["item_id"] for m in list_backups()
               if m["item_id"] not in live_ids and (m.get("backed_up_at") or 0) < cutoff
               and delete_backup(m["item_id"])]
    if removed:
        log(f"{len(removed)} backup(s) ouder dan {max_age_days} dagen opgeruimd.")
    return removed
