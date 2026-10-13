"""
Persistence: the activity log, the repost history and the backups on disk.

One folder per ad:

  backups/<itemId>_<title>/
      ad.json      the ad, readable on top, Marktplaats' own data below
      meta.json    what the overview needs (title, price, photo list, dates)
      photos/      001.jpg, 002.jpg, ... the original uploads, in order
"""
import json
import os
import re
import shutil
import time
from collections import deque

BACKUP_DIR = os.environ.get("BACKUP_DIR", "/data/backups")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
HISTORY_PATH = os.path.join(CONFIG_DIR, "history.json")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "mpmanager.json")

LOG = deque(maxlen=500)


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


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


# ---- settings ------------------------------------------------------------

DEFAULTS = {
    "dry_run": True,
    "auto_backup": True,
    "auto_repost": False,
    "min_age_days": 14,           # only repost ads that have been up at least this long
    "max_per_day": 3,
    "active_hours": "09:00-22:00",
    "auto_delay_min": 120,        # seconds between automatic reposts
    "auto_delay_max": 600,
    "manual_delay_min": 30,       # seconds a clicked repost waits before it runs
    "manual_delay_max": 90,
    "exclude": [],                # item ids automatic reposting leaves alone
    "backup_retention_days": 0,   # 0 = keep forever
    "telegram_chat_id": "",
    "notify_on": "off",           # off | failures | all
}


# docker-compose may give starting values; once Settings is saved, the file owns them.
ENV_NAMES = {
    "dry_run": "DRY_RUN", "auto_backup": "AUTO_BACKUP", "auto_repost": "AUTO_REPOST",
    "min_age_days": "MIN_ITEM_AGE_DAYS", "max_per_day": "MAX_REPOSTS_PER_DAY", "active_hours": "ACTIVE_HOURS",
    "auto_delay_min": "MIN_DELAY_SECONDS", "auto_delay_max": "MAX_DELAY_SECONDS",
    "manual_delay_min": "MANUAL_DELAY_MIN_SECONDS", "manual_delay_max": "MANUAL_DELAY_MAX_SECONDS",
    "backup_retention_days": "BACKUP_RETENTION_DAYS", "telegram_chat_id": "TELEGRAM_CHAT_ID", "notify_on": "NOTIFY_ON",
}


def _from_env():
    out = {}
    for key, name in ENV_NAMES.items():
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        kind = type(DEFAULTS[key])
        try:
            if kind is bool:
                out[key] = raw.lower() not in ("0", "false", "no", "uit", "nee")
            elif kind is int:
                out[key] = max(0, int(raw))
            elif key == "active_hours" and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d", raw):
                continue
            elif key == "notify_on" and raw not in ("off", "failures", "all"):
                continue
            else:
                out[key] = raw
        except ValueError:
            pass
    return out


def settings():
    s = dict(DEFAULTS)
    s.update(_from_env())
    s.update({k: v for k, v in (_read(SETTINGS_PATH, {}) or {}).items() if k in DEFAULTS})
    return s


def save_settings(changes):
    """Take over what is valid, ignore the rest; returns the settings and what was refused."""
    s, refused = settings(), []
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        kind = type(DEFAULTS[key])
        try:
            if kind is bool:
                value = bool(value)
            elif kind is int:
                value = int(value)
                if value < 0:
                    raise ValueError
            elif key == "active_hours":
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d", str(value).strip()):
                    raise ValueError
                value = str(value).strip()
            elif key == "notify_on":
                if value not in ("off", "failures", "all"):
                    raise ValueError
            elif key == "exclude":
                value = sorted({str(v) for v in value if re.fullmatch(r"m\d+", str(v))})
            else:
                value = str(value).strip()
        except (TypeError, ValueError):
            refused.append(key)
            continue
        s[key] = value
    for lo, hi in (("auto_delay_min", "auto_delay_max"), ("manual_delay_min", "manual_delay_max")):
        s[hi] = max(s[lo], s[hi])
    _atomic_write(SETTINGS_PATH, s)
    return s, refused


def set_excluded(item_id, excluded):
    current = set(settings()["exclude"])
    (current.add if excluded else current.discard)(item_id)
    return save_settings({"exclude": list(current)})[0]


# ---- history -------------------------------------------------------------

def history():
    return _read(HISTORY_PATH, []) or []


def record(action, title, old_id=None, new_id=None):
    h = history()
    h.insert(0, {"ts": int(time.time()), "action": action, "title": title,
                 "old_id": old_id, "new_id": new_id})
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
        "url": f"https://www.marktplaats.nl/seller/view/{ad['item_id']}",
        "placed_at": (listing.get("startDate") or 0) // 1000 or None,
        "expires_at": (listing.get("closeDate") or 0) // 1000 or None,
    }


def save_backup(client, ad):
    """
    Write the ad and its photos. Photos are only fetched again when the set on
    Marktplaats changed, checked on their URLs: file names stay 001, 002, ...
    whatever is in them.
    """
    item_id = ad["item_id"]
    summary = _readable(ad)
    d = _dir(item_id, summary["title"])
    photo_dir = os.path.join(d, "photos")
    os.makedirs(photo_dir, exist_ok=True)

    urls = _photo_urls(ad)
    old = read_meta(item_id) or {}
    if old.get("photo_urls") != urls:
        for name in os.listdir(photo_dir):
            os.remove(os.path.join(photo_dir, name))

    saved = []
    for i, url in enumerate(urls, 1):
        name = f"{i:03d}.jpg"
        path = os.path.join(photo_dir, name)
        if not os.path.exists(path):
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
    """Remove one backup folder. Touches nothing on Marktplaats."""
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
