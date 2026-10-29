"""MPManager: web page + JSON API. The work is in worker.py, Marktplaats in mp_api.py, the disk in store.py."""
import datetime
import hmac
import io
import json
import os
import secrets
import zipfile

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, session

import mp_api
import notify
import store
import watch_api
import worker
from mp_api import AuthError, MarktplaatsError
from store import log
from watcher import scheduler as watch_scheduler

app = Flask(__name__)
app.register_blueprint(watch_api.bp)
client = mp_api.Marktplaats(os.path.join(store.CONFIG_DIR, "state.json"))
worker.client = client
notify.get_token = lambda: client.secret("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

WEB_USER = os.environ.get("WEB_USER", "").strip()
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "").strip()
# The example values from the README: anyone who read it knows them.
PLACEHOLDER_PASSWORDS = {"password", "wachtwoord", "changeme", "change_me", "change-me"}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


# ---- the page's own login --------------------------------------------------
#
# A page of our own with a session cookie instead of the browser's basic-auth
# popup: a password manager can fill and save a form. Basic auth still works
# on every request, for curl and scripts.

def _session_key():
    """Signs the session cookie; kept with the encryption key so a restart logs nobody out."""
    path = os.path.join(os.path.dirname(mp_api.KEY_PATH) or ".", "web_secret")
    try:
        with open(path) as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(key)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


app.secret_key = _session_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_NAME="mpmanager", PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30))


def placeholder_password():
    return WEB_PASSWORD.lower() in PLACEHOLDER_PASSWORDS


def credentials_ok(username, password):
    if placeholder_password():
        return False
    user_ok = not WEB_USER or hmac.compare_digest((username or "").encode(), WEB_USER.encode())
    return user_ok and hmac.compare_digest((password or "").encode(), WEB_PASSWORD.encode())


def web_logged_in():
    if not WEB_PASSWORD:
        return True
    if session.get("user") is not None and not placeholder_password():
        return True
    basic = request.authorization
    return bool(basic and credentials_ok(basic.username, basic.password))


@app.before_request
def guard():
    api = request.path.startswith("/api/")
    # Other sites open in the same browser cannot set this header on a request to us.
    if api and request.method in MUTATING and request.headers.get("X-Requested-With") != "mpmanager":
        return jsonify({"error": "Verzoek geweigerd."}), 403
    if request.path in ("/login", "/logout", "/healthz") or web_logged_in():
        return None
    if api:
        return jsonify({"error": "Log eerst in op deze pagina.", "web_login": True}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def web_login():
    if not WEB_PASSWORD:
        return redirect("/")
    error = None
    if placeholder_password():
        error = ("WEB_PASSWORD is nog de voorbeeldwaarde. Deze pagina kan je advertenties verwijderen, dus dat "
                 "wachtwoord wordt niet geaccepteerd. Zet een eigen wachtwoord in docker-compose.yml en herstart.")
    elif request.method == "POST":
        if credentials_ok(request.form.get("username"), request.form.get("password")):
            session.permanent = True
            session["user"] = request.form.get("username") or ""
            return redirect("/")
        error = "Gebruikersnaam of wachtwoord klopt niet."
        log("Mislukte aanmelding op de pagina.")
    return render_template("login.html", error=error, ask_user=bool(WEB_USER))


@app.route("/logout", methods=["GET", "POST"])
def web_logout():
    session.clear()
    return redirect("/login")


# ---- errors ----------------------------------------------------------------

@app.errorhandler(AuthError)
def on_auth_error(e):
    if len(e.args) > 1:      # where a login attempt stopped; never holds the password or the code
        log(f"LOGIN: {e.args[1]}")
    return jsonify({"error": e.args[0], "auth": True}), 401


@app.errorhandler(MarktplaatsError)
def on_mp_error(e):
    log(f"FOUT: {e.args[0]}")
    return jsonify({"error": e.args[0]}), 502


def item(item_id):
    if not mp_api.valid_id(item_id):
        raise MarktplaatsError(f"Ongeldig advertentienummer: {item_id}")
    return item_id


def body():
    return request.get_json(silent=True) or {}


# ---- startup migrations ------------------------------------------------------

def import_old_session():
    """Take over the login of the previous (Playwright) version, once."""
    old = os.path.join(store.CONFIG_DIR, "cookies.json")
    if client.has_cookies() or client.saved_email() or not os.path.exists(old):
        return
    try:
        with open(old) as f:
            client.set_cookies(mp_api.parse_cookies(f.read()))
        log("Login overgenomen uit cookies.json van de vorige versie.")
    except (AuthError, OSError, ValueError) as e:
        log(f"cookies.json van de vorige versie is niet meer bruikbaar: {e}")


def encrypt_old_password():
    """The previous version kept the password readable in settings.json; move it into the encrypted state."""
    path = os.path.join(store.CONFIG_DIR, "settings.json")
    try:
        with open(path) as f:
            old = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(old, dict) or not old.get("password"):
        return
    if old.get("email") and not client.saved_email():
        client.remember(old["email"], old["password"])
    old.pop("password")
    with open(path, "w") as f:
        json.dump(old, f, indent=2)
    log("Leesbaar wachtwoord uit settings.json van de vorige versie verwijderd; het staat nu versleuteld in state.json.")


# ---- page and status ---------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", web_login=bool(WEB_PASSWORD))


@app.get("/api/status")
def api_status():
    s = store.settings()
    return jsonify({"logged_in": client.logged_in(), "dry_run": s["dry_run"], "auto_repost": s["auto_repost"],
                    "reposts_today": store.reposts_today(), "max_per_day": s["max_per_day"],
                    "backups": len(store.list_backups()), "saved_email": client.saved_email(),
                    "state_error": client.state_error})


@app.get("/api/settings")
def api_get_settings():
    return jsonify({**store.settings(), "telegram_token_set": bool(notify.get_token())})


@app.post("/api/settings")
def api_settings():
    data = body()
    before = store.settings()
    s, refused = store.save_settings(data)
    if data.get("telegram_bot_token"):
        client.set_secret("telegram_bot_token", str(data["telegram_bot_token"]).strip())
    if data.get("telegram_clear"):
        client.set_secret("telegram_bot_token", "")
    changed = [k for k in s if s[k] != before[k] and k != "exclude"]
    if changed:
        log("Instellingen gewijzigd: " + ", ".join(f"{k}={s[k]}" for k in changed))
    return jsonify({**s, "refused": refused, "telegram_token_set": bool(notify.get_token())})


@app.post("/api/notify/test")
def api_notify_test():
    problem = notify.send("👋 <b>MPManager</b>\nTestbericht: Telegram-meldingen werken.")
    if problem:
        return jsonify({"error": f"Niet verstuurd: {problem}"}), 502
    return jsonify({"ok": True})


# ---- Marktplaats login -------------------------------------------------------

@app.post("/api/login")
def api_login():
    data = body()
    email, password = (data.get("email") or "").strip(), data.get("password") or ""
    if not email or not password:
        raise AuthError("Vul e-mailadres en wachtwoord in.")
    result = client.login(email, password, remember=bool(data.get("remember")))
    log("Ingelogd op Marktplaats." if result.get("logged_in") else "Wachtwoord geaccepteerd; wacht op de sms-code.")
    return jsonify(result)


@app.post("/api/login/code")
def api_login_code():
    result = client.login_code(body().get("code") or "")
    log("Ingelogd op Marktplaats met sms-code.")
    return jsonify(result)


@app.post("/api/login/resend")
def api_login_resend():
    return jsonify(client.login_resend())


@app.delete("/api/login/password")
def api_forget_password():
    client.forget_password()
    log("Bewaard wachtwoord gewist.")
    return jsonify({"ok": True})


@app.post("/api/session")
def api_session():
    client.set_cookies(mp_api.parse_cookies(body().get("cookies", "")))
    log("Nieuwe Marktplaats-cookies opgeslagen.")
    return jsonify({"ok": True})


@app.delete("/api/session")
def api_logout():
    client.logout()
    log("Marktplaats-login gewist.")
    return jsonify({"ok": True})


# ---- ads -----------------------------------------------------------------------

@app.get("/api/ads")
def api_ads():
    s = store.settings()
    backups = {m["item_id"]: m for m in store.list_backups()}
    ads = []
    for raw in client.listings():
        image = raw.get("primaryImageUrl") or ""
        item_id = raw.get("itemId")
        meta = backups.get(item_id)
        age = worker.age_days(item_id)
        ads.append({
            "item_id": item_id,
            "title": raw.get("title"),
            "category": raw.get("categoryName"),
            "price_cents": raw.get("priceCents"),
            "price_type": raw.get("priceType"),
            "bidding": bool(raw.get("biddingEnabled")),
            "min_bid_cents": (store.load_backup(item_id) or {}).get("minimum_bid_cents") if raw.get("biddingEnabled") else None,
            "views": raw.get("viewCount"),
            "expires": (raw.get("closeDate") or "")[:10],
            "image": ("https:" + image if image.startswith("//") else image).replace("$_#", "$_82"),
            "url": mp_api.BASE + (raw.get("sVipUrl") or f"/seller/view/{item_id}"),
            "backed_up_at": meta and meta["backed_up_at"],
            "age_days": None if age is None else int(age),
            "excluded": item_id in s["exclude"],
            "auto_skip": worker.skip_reason(raw, s) if s["auto_repost"] else None,
        })
    return jsonify({"ads": ads})


def queued(kind, item_id, title=None):
    return jsonify(worker.enqueue(kind, item(item_id), title))


@app.post("/api/ads/<item_id>/backup")
def api_backup(item_id):
    return queued("backup", item_id, body().get("title"))


@app.post("/api/backup-all")
def api_backup_all():
    listings = client.listings()
    for raw in listings:
        worker.enqueue("backup", raw["itemId"], raw.get("title"))
    return jsonify({"queued": len(listings)})


@app.post("/api/ads/<item_id>/repost")
def api_repost(item_id):
    return queued("repost", item_id, body().get("title"))


def amount(value, what):
    """Euros as typed ('65', '65,50', '1.250,00') -> 65.5, or None when left empty."""
    if value in (None, ""):
        return None
    text = str(value).strip().replace("€", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        number = round(float(text), 2)
    except ValueError:
        raise MarktplaatsError(f"Ongeldige {what}: {value}")
    if not 0 < number < 1_000_000:
        raise MarktplaatsError(f"Ongeldige {what}: {value}")
    return number


@app.post("/api/ads/<item_id>/repost-adjust")
def api_repost_adjust(item_id):
    data = body()
    price = amount(data.get("price"), "prijs")
    if price is None:
        raise MarktplaatsError("Vul een nieuwe prijs in.")
    min_bid = amount(data.get("min_bid"), "minimumbod")
    if min_bid is not None and min_bid > price:
        raise MarktplaatsError("Het minimumbod mag niet hoger zijn dan de prijs.")
    return jsonify(worker.enqueue("repost_adjust", item(item_id), data.get("title"), price=price, min_bid=min_bid))


@app.post("/api/ads/<item_id>/delete")
def api_delete(item_id):
    return queued("delete", item_id, body().get("title"))


@app.post("/api/ads/<item_id>/exclude")
def api_exclude(item_id):
    excluded = bool(body().get("excluded"))
    store.set_excluded(item(item_id), excluded)
    return jsonify({"excluded": excluded})


@app.get("/api/jobs")
def api_jobs():
    return jsonify({"jobs": worker.jobs()[:30]})


@app.post("/api/jobs/<int:job_id>/cancel")
def api_job_cancel(job_id):
    return jsonify({"ok": worker.cancel(job_id)})


# ---- backups -------------------------------------------------------------------

@app.get("/api/backups")
def api_backups():
    return jsonify({"backups": store.list_backups()})


@app.get("/api/backups/<item_id>/photo/<int:n>")
def api_backup_photo(item_id, n):
    paths = store.backup_photos(item(item_id))
    if not 1 <= n <= len(paths):
        return jsonify({"error": "geen foto"}), 404
    return send_file(paths[n - 1], mimetype="image/jpeg", max_age=3600)


@app.get("/api/backups/<item_id>/download")
def api_backup_download(item_id):
    ad = store.load_backup(item(item_id))
    if not ad:
        return jsonify({"error": "geen backup"}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{item_id}/ad.json", json.dumps(ad, indent=2, ensure_ascii=False))
        for p in store.backup_photos(item_id):
            z.write(p, f"{item_id}/photos/{os.path.basename(p)}")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"{item_id}.zip")


@app.post("/api/backups/<item_id>/place")
def api_backup_place(item_id):
    return queued("place", item_id)


@app.delete("/api/backups/<item_id>")
def api_backup_delete(item_id):
    return jsonify({"ok": store.delete_backup(item(item_id))})


@app.get("/api/history")
def api_history():
    return jsonify({"history": store.history()})


@app.get("/api/log")
def api_log():
    return jsonify({"lines": list(store.LOG)[-200:]})


@app.get("/healthz")
def healthz():
    # Unhealthy when the search scheduler hangs, so Docker can restart the container.
    if watch_scheduler.get_scheduler_status()["state"] == "stale":
        return Response("zoekplanner reageert niet", status=503, mimetype="text/plain")
    return Response("ok", mimetype="text/plain")


def adopt_mpwatcher_telegram():
    """A taken-over MPWatcher database brings its Telegram bot; use it if MPManager has none."""
    token, chats = watch_api.mpwatcher_telegram()
    if not token and not chats:
        return
    if token and not client.secret("telegram_bot_token"):
        client.set_secret("telegram_bot_token", token)
    if chats and not store.settings()["telegram_chat_id"]:
        store.save_settings({"telegram_chat_id": chats})
    watch_api.forget_mpwatcher_telegram()
    log("Telegram-bot van MPWatcher overgenomen (token versleuteld bewaard).")


def main():
    import_old_session()
    encrypt_old_password()
    if client.state_error:
        log(client.state_error)
    s = store.settings()
    log(f"MPManager gestart. Proefmodus {'aan' if s['dry_run'] else 'uit'}, "
        f"{'ingelogd' if client.has_cookies() else 'nog niet ingelogd op Marktplaats'}.")
    if WEB_PASSWORD and placeholder_password():
        log("WEB_PASSWORD is de voorbeeldwaarde: niemand kan inloggen tot je een eigen wachtwoord zet.")
    worker.start()
    if watch_api.setup(notify.target):
        log("Zoekwoorden, resultaten en instellingen van MPWatcher overgenomen.")
    adopt_mpwatcher_telegram()
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threads=6)
