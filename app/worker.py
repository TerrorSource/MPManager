"""
The work itself (backup, place, repost, delete), a queue that does it one at a
time, and a scheduler that fills the queue on its own.

Everything towards Marktplaats that changes something goes through the queue.
A clicked repost first waits a random while, so clicks never arrive as a burst;
automatic reposts are spaced further apart.
"""
import itertools
import random
import threading
import time
from datetime import datetime

import notify
import store
from mp_api import AuthError, MarktplaatsError
from store import log

client = None                # set by app.py

KINDS = {"backup": "Backup", "repost": "Herplaatsen", "auto_repost": "Automatisch herplaatsen",
         "place": "Plaatsen uit backup", "delete": "Verwijderen"}
HOLD = {"repost", "place", "delete"}          # kinds that wait before they run
CHECK_INTERVAL = 3600                         # how often the scheduler looks at the ads
TICK = 20

_jobs = {}
_ids = itertools.count(1)
_lock = threading.Lock()
_wake = threading.Event()


# ---- the work ------------------------------------------------------------

def do_backup(item_id):
    meta = store.save_backup(client, client.read_ad(item_id))
    note = " (onvolledig: niet meer te wijzigen op Marktplaats)" if meta["partial"] else ""
    log(f"Backup van {item_id} '{meta['title']}': {len(meta['photos'])} foto's{note}.")
    return meta


def do_place(item_id):
    """New ad from the backup on disk. Honors dry run."""
    ad = store.load_backup(item_id)
    if not ad:
        raise MarktplaatsError(f"Geen backup van {item_id}.")
    dry = store.settings()["dry_run"]
    result = client.place(ad.get("form"), store.backup_photos(item_id), dry_run=dry)
    if dry:
        log(f"PROEF: '{ad['title']}' zou geplaatst worden met {len(result['fields'])} velden; niets verstuurd.")
    else:
        log(f"Geplaatst: '{ad['title']}' als {result['item_id']}.")
    return ad, result


def _backup_new(new_id):
    try:
        do_backup(new_id)             # so the live ad has a backup under its own number
    except (MarktplaatsError, AuthError) as e:
        log(f"Backup van de nieuwe advertentie {new_id} lukte nog niet: {e.args[0]}")


def do_repost(item_id):
    """
    Backup, place the copy, and only then delete the original. Whatever fails
    before the last step leaves the original untouched.
    """
    do_backup(item_id)
    ad, result = do_place(item_id)
    if result.get("dry_run"):
        return "proef: voorbereid, niets verstuurd"
    new_id = result["item_id"]
    store.mark_reposted(item_id, new_id)
    store.record("herplaatst", ad["title"], item_id, new_id)
    warning = None
    try:
        client.delete(item_id)
        log(f"Oude advertentie {item_id} verwijderd.")
    except (MarktplaatsError, AuthError) as e:
        warning = f"nieuwe advertentie {new_id} staat online, maar de oude {item_id} is niet verwijderd: {e.args[0]}"
        log("LET OP: " + warning)
        notify.failed("Verwijderen van de oude advertentie", ad["title"], warning)
    _backup_new(new_id)
    notify.reposted(ad["title"], item_id, new_id)
    return warning or f"herplaatst als {new_id}"


def do_restore(item_id):
    ad, result = do_place(item_id)
    if result.get("dry_run"):
        return "proef: voorbereid, niets verstuurd"
    store.mark_reposted(item_id, result["item_id"])
    store.record("geplaatst uit backup", ad["title"], item_id, result["item_id"])
    _backup_new(result["item_id"])
    return f"geplaatst als {result['item_id']}"


def do_delete(item_id):
    if store.settings()["dry_run"]:
        log(f"PROEF: {item_id} zou verwijderd worden; niets verstuurd.")
        return "proef: niets verwijderd"
    client.delete(item_id)
    store.record("verwijderd", (store.read_meta(item_id) or {}).get("title") or item_id, item_id)
    log(f"Advertentie {item_id} verwijderd van Marktplaats.")
    return "verwijderd"


def _run(job):
    kind, item_id = job["kind"], job["item_id"]
    if kind == "backup":
        return f"{len(do_backup(item_id)['photos'])} foto's"
    if kind in ("repost", "auto_repost"):
        return do_repost(item_id)
    if kind == "place":
        return do_restore(item_id)
    return do_delete(item_id)


# ---- the queue -----------------------------------------------------------

def enqueue(kind, item_id, title=None, delay=None):
    """Add a job, unless this ad already has one waiting or running."""
    with _lock:
        for job in _jobs.values():
            if job["item_id"] == item_id and job["status"] in ("wacht", "bezig"):
                return job
        if delay is None:
            s = store.settings()
            hold = kind in HOLD and not s["dry_run"]
            delay = random.uniform(s["manual_delay_min"], s["manual_delay_max"]) if hold else 0
        job = {"id": next(_ids), "kind": kind, "label": KINDS[kind], "item_id": item_id,
               "title": title or (store.read_meta(item_id) or {}).get("title") or item_id,
               "status": "wacht", "message": "", "created": time.time(), "run_at": time.time() + delay}
        _jobs[job["id"]] = job
        for old in sorted(_jobs)[:-100]:
            if _jobs[old]["status"] not in ("wacht", "bezig"):
                del _jobs[old]
    _wake.set()
    return job


def cancel(job_id):
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] != "wacht":
            return False
        job.update(status="geannuleerd", message="")
    log(f"Geannuleerd: {job['label'].lower()} van '{job['title']}'.")
    return True


def jobs():
    with _lock:
        return sorted((dict(j) for j in _jobs.values()), key=lambda j: j["id"], reverse=True)


def busy_ids():
    with _lock:
        return {j["item_id"] for j in _jobs.values() if j["status"] in ("wacht", "bezig")}


def _next_job():
    with _lock:
        waiting = [j for j in _jobs.values() if j["status"] == "wacht"]
        due = [j for j in waiting if j["run_at"] <= time.time()]
        if due:
            job = min(due, key=lambda j: (j["run_at"], j["id"]))
            job["status"] = "bezig"
            return job, 0
        return None, min((j["run_at"] - time.time() for j in waiting), default=TICK)


def _worker():
    while True:
        job, wait = _next_job()
        if not job:
            _wake.clear()
            _wake.wait(min(max(wait, 0.5), TICK))
            continue
        try:
            job["message"] = _run(job)
            job["status"] = "klaar"
        except (MarktplaatsError, AuthError) as e:
            _fail(job, e.args[0], login=isinstance(e, AuthError))
        except Exception as e:                    # a bug must not kill the queue
            _fail(job, f"onverwachte fout: {type(e).__name__}: {e}")
        job["finished"] = time.time()


def _fail(job, message, login=False):
    job.update(status="mislukt", message=message)
    log(f"FOUT: {job['label'].lower()} van '{job['title']}': {message}")
    if login:
        notify.login_problem(message)
    else:
        notify.failed(job["label"], job["title"], message)


# ---- the scheduler -------------------------------------------------------

def within_active_hours():
    lo, hi = (int(t[:2]) * 60 + int(t[3:]) for t in store.settings()["active_hours"].split("-"))
    now = datetime.now()
    mins = now.hour * 60 + now.minute
    return lo <= mins < hi if lo <= hi else (mins >= lo or mins < hi)


def age_days(item_id):
    placed = (store.read_meta(item_id) or {}).get("placed_at")
    if placed is None:                    # a backup from before meta.json kept the date
        placed = (store.load_backup(item_id) or {}).get("placed_at")
    return (time.time() - placed) / 86400 if placed else None


def skip_reason(raw, settings=None):
    """Why automatic reposting leaves this ad alone, or None if it is a candidate."""
    s = settings or store.settings()
    item_id = raw.get("itemId")
    if item_id in s["exclude"]:
        return "uitgesloten"
    if raw.get("status") != "ACTIVE":
        return "niet actief"
    if raw.get("reserved"):
        return "gereserveerd"
    if raw.get("highestBid"):
        return "heeft een bod"            # a repost would throw the bids away
    age = age_days(item_id)
    if age is None:
        return "nog geen backup"
    if age < s["min_age_days"]:
        return f"{age:.0f} dagen oud, grens is {s['min_age_days']}"
    return None


_proposed = set()            # (day, item) already shown as a dry-run candidate


def _auto_repost(listings):
    s = store.settings()
    budget = s["max_per_day"] - store.reposts_today()
    budget -= sum(1 for j in jobs() if j["kind"] == "auto_repost" and j["status"] in ("wacht", "bezig"))
    if budget <= 0:
        return
    picked = [r for r in listings if not skip_reason(r, s) and r["itemId"] not in busy_ids()]
    picked.sort(key=lambda r: age_days(r["itemId"]), reverse=True)
    delay = 0
    for raw in picked[:budget]:
        if s["dry_run"]:
            key = (time.strftime("%Y-%m-%d"), raw["itemId"])
            if key in _proposed:
                continue
            _proposed.add(key)
        enqueue("auto_repost", raw["itemId"], raw.get("title"), delay=delay)
        log(f"Automatisch herplaatsen ingepland: '{raw.get('title')}' ({age_days(raw['itemId']):.0f} dagen oud).")
        delay += random.uniform(s["auto_delay_min"], s["auto_delay_max"])


def _scan():
    s = store.settings()
    listings = client.listings()
    if s["auto_backup"]:
        backed = {m["item_id"] for m in store.list_backups()}
        for raw in listings:
            if raw.get("itemId") not in backed:
                enqueue("backup", raw["itemId"], raw.get("title"))
    store.prune_backups(s["backup_retention_days"], {r.get("itemId") for r in listings})
    if s["auto_repost"] and within_active_hours():
        _auto_repost(listings)


def _scheduler():
    """
    Wakes often, so a change in Settings takes effect soon, but only looks at
    Marktplaats once per CHECK_INTERVAL (or right after a relevant change).
    """
    last_scan, last_state, login_reported = 0.0, None, False
    time.sleep(10)
    while True:
        try:
            s = store.settings()
            state = (s["auto_backup"], s["auto_repost"], s["auto_repost"] and within_active_hours())
            if state != last_state:
                if s["auto_repost"]:
                    log(f"Automatisch herplaatsen aan: max {s['max_per_day']} per dag, vanaf {s['min_age_days']} dagen oud, "
                        f"tussen {s['active_hours']}" + ("" if state[2] else " (nu buiten die uren)") + ".")
                elif last_state and last_state[1]:
                    log("Automatisch herplaatsen uit.")
                last_state, last_scan = state, 0.0
            if (s["auto_backup"] or s["auto_repost"] or s["backup_retention_days"]) and \
                    time.time() - last_scan >= CHECK_INTERVAL and client.has_cookies():
                last_scan = time.time()
                _scan()
                login_reported = False
        except AuthError as e:
            if not login_reported:
                log(f"Planner: {e.args[0]}")
                notify.login_problem(e.args[0])
                login_reported = True
        except Exception as e:
            log(f"Planner: {type(e).__name__}: {e}")
        time.sleep(TICK)


def start():
    threading.Thread(target=_worker, daemon=True, name="worker").start()
    threading.Thread(target=_scheduler, daemon=True, name="scheduler").start()
