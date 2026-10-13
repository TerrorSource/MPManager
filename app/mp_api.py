"""
Marktplaats over plain HTTP: no browser involved.

Everything the site's own pages need is embedded in them as JSON
(window.__CONFIG__), so reading an ad is one GET and placing one is the same
form POST the browser sends:

  /my-account/sell/api/listings   the list of your ads
  /seller/view/<id>               one ad as shown to its owner
  /plaats/<id>/edit               the same ad as form values (what gets re-posted)
  /plaats/<l1>/<l2>?bucketId=..   an empty form with fresh tokens
  /plaats/api/image/upload        photo upload
  /plaats/ads                     the form POST that creates the ad
  /seller/api/delete-ad           delete

The login is the pair of cookies MpSecurity + MpSslSecurity. They come from
logging in here with e-mail, password and (if set) the SMS code, the same JSON
calls the login page makes; or pasted from a browser as a fallback. They stay
valid for months. The SMS code is passed on and never stored; the password
only when asked to, so an expired session can renew itself.

Everything kept on disk (cookies, password) is in one file, encrypted with a key
that lives outside the config folder: SECRET_KEY, or a generated key file.

  /identity/v2/login                              tokens for the login call
  /identity/v2/api/login                          e-mail + password
  /identity/v2/two-factor-auth-setup/challenge    tokens for the code call
  /identity/v2/api/two-factor-auth/submit-verify-code
"""
import json
import os
import re
import base64
import hashlib
import threading
import time

import requests
from cryptography.fernet import Fernet, InvalidToken

BASE = "https://www.marktplaats.nl"
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
)
LOGIN_COOKIES = ("MpSecurity", "MpSslSecurity")
TIMEOUT = 30

# Parts of the edit form that are tokens or bulk, not ad content.
FORM_DROP = ("auth", "experiments", "tenantContext", "genAiSyi")


class AuthError(RuntimeError):
    """The cookies are missing or no longer accepted."""


class MarktplaatsError(RuntimeError):
    pass


def valid_id(item_id):
    return bool(re.fullmatch(r"m\d+", str(item_id)))


def parse_cookies(text):
    """
    Accept whatever is easiest to copy from a browser: a "Cookie:" request
    header, name=value lines, a JSON object, or a JSON cookie export.
    """
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("cookies", data)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
        if isinstance(data, list):
            return {c["name"]: c["value"] for c in data
                    if isinstance(c, dict) and "name" in c and "value" in c}
    except ValueError:
        pass
    text = re.sub(r"^cookie:\s*", "", text, flags=re.I)
    out = {}
    for part in re.split(r"[;\n]+", text):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            out[name.strip()] = value.strip()
    return out


LOGIN_ERRORS = {
    "WRONG_USERNAME_PASSWORD": "E-mailadres of wachtwoord klopt niet.",
    "AUTHENTICATION_FAILED": "E-mailadres of wachtwoord klopt niet.",
    "VERIFICATION_FAILED": "De code klopt niet.",
    "VERIFICATION_CODE_BLOCKED": "Marktplaats heeft verificatiecodes tijdelijk geblokkeerd na te veel pogingen. Probeer het later opnieuw.",
    "VERIFICATION_REQUESTS_BLOCKED": "Marktplaats heeft verificatiecodes tijdelijk geblokkeerd na te veel pogingen. Probeer het later opnieuw.",
    "TWO_FACTOR_AUTH_SETUP_REQUIRED": "Marktplaats wil dat je eerst een telefoonnummer instelt. Doe dat eenmalig op marktplaats.nl.",
    "CONFIRMATION_REQUIRED": "Marktplaats wil eerst een bevestiging van je account. Log eenmalig in op marktplaats.nl.",
    "FORCE_PASSWORD_RESET": "Marktplaats eist een nieuw wachtwoord. Stel dat eerst in op marktplaats.nl.",
    "CREDENTIALS_MUST_BE_UPDATED": "Marktplaats eist een nieuw wachtwoord. Stel dat eerst in op marktplaats.nl.",
}
PENDING_TTL = 600        # how long a login waits for its SMS code
RELOGIN_PAUSE = 900      # never hammer the login with a saved password
KEY_PATH = os.environ.get("KEY_PATH", "/keys/secret.key")


def _cipher():
    """
    The key for the state file. SECRET_KEY from the environment if given, else a
    key generated once into KEY_PATH, which is deliberately not the config
    folder: a copy of that folder alone must not be enough to read the login.
    """
    secret = os.environ.get("SECRET_KEY", "").strip()
    if secret:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
    if not os.path.exists(KEY_PATH):
        os.makedirs(os.path.dirname(KEY_PATH) or ".", exist_ok=True)
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    with open(KEY_PATH, "rb") as f:
        return Fernet(f.read().strip())


def page_config(html, name="__CONFIG__"):
    """The JSON a Marktplaats page ships in window.__CONFIG__ (or a sibling)."""
    m = re.search(r"window\.%s\s*=\s*" % name, html)
    if not m:
        return None
    try:
        return json.JSONDecoder().raw_decode(html[m.end():])[0]
    except ValueError:
        return None


def money(value):
    """12 -> '12,00', the way the form sends amounts."""
    return f"{float(value):.2f}".replace(".", ",")


class Marktplaats:
    def __init__(self, state_path):
        self.state_path = state_path
        self._lock = threading.Lock()
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        })
        self._saved = None            # what the state file holds right now
        self._credentials = None      # {'email', 'password'}, only if the user asked to keep them
        self._secrets = {}            # other things worth encrypting, e.g. the Telegram bot token
        self._pending = None          # a login that is waiting for its SMS code
        self._last_relogin = 0
        self.state_error = None
        self._cipher = _cipher()
        self._load()

    # ---- session ---------------------------------------------------------

    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            legacy = "enc" not in data          # written before encryption existed
            if not legacy:
                data = json.loads(self._cipher.decrypt(data["enc"].encode()))
        except InvalidToken:
            self.state_error = "De opgeslagen login is met een andere sleutel versleuteld en niet te lezen. Log opnieuw in."
            return
        except (ValueError, OSError, KeyError):
            return
        self._set_cookies(data.get("cookies") or {})
        self._credentials = data.get("credentials") or None
        self._secrets = data.get("secrets") or {}
        if not legacy:
            self._saved = self._state()
        self._persist()

    def _set_cookies(self, cookies):
        self.http.cookies.clear()
        for name, value in cookies.items():
            self.http.cookies.set(name, value, domain=".marktplaats.nl", path="/")

    def _state(self):
        return {"cookies": {c.name: c.value for c in self.http.cookies},
                "credentials": self._credentials, "secrets": dict(self._secrets)}

    def _persist(self):
        """Marktplaats may hand out renewed cookies; keep whatever is current."""
        state = self._state()
        if state == self._saved:
            return
        with self._lock:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"enc": self._cipher.encrypt(json.dumps(state).encode()).decode()}, f)
            os.replace(tmp, self.state_path)
            os.chmod(self.state_path, 0o600)
            self._saved = state
            self.state_error = None

    def secret(self, name):
        return self._secrets.get(name, "")

    def set_secret(self, name, value):
        if value:
            self._secrets[name] = value
        else:
            self._secrets.pop(name, None)
        self._persist()

    def remember(self, email, password):
        """Keep the password (encrypted) so an expired session can renew itself."""
        self._credentials = {"email": email, "password": password}
        self._persist()

    def forget_password(self):
        self._credentials = None
        self._persist()

    def saved_email(self):
        return (self._credentials or {}).get("email")

    def has_cookies(self):
        return all(self.http.cookies.get(n) for n in LOGIN_COOKIES)

    def set_cookies(self, cookies):
        missing = [n for n in LOGIN_COOKIES if not cookies.get(n)]
        if missing:
            raise AuthError(f"Deze cookies ontbreken: {', '.join(missing)}")
        previous = {c.name: c.value for c in self.http.cookies}
        self._set_cookies(cookies)
        if not self.logged_in():
            self._set_cookies(previous)       # a failed attempt must not cost a working login
            raise AuthError("Marktplaats accepteert deze login niet (verlopen of onvolledig).")
        self._persist()

    # ---- logging in ------------------------------------------------------

    def _new_session(self):
        s = requests.Session()
        s.headers.update(dict(self.http.headers))
        return s

    def _identity_post(self, s, path, xsrf, payload, referer):
        """One call of the login pages' own API. Returns (ok, error code, body)."""
        try:
            r = s.post(BASE + path, json=payload, timeout=TIMEOUT,
                       headers={"x-mp-xsrf": xsrf, "Origin": BASE, "Referer": referer,
                                "Accept": "application/json"})
        except requests.RequestException as e:
            raise MarktplaatsError(f"Marktplaats is niet bereikbaar: {e}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        code = body.get("code") if isinstance(body.get("code"), str) else None
        return 200 <= r.status_code < 300, code or f"HTTP_{r.status_code}", body

    def _adopt(self, s, keep=None):
        """Take over a session that just logged in."""
        cookies = {c.name: c.value for c in s.cookies}
        self._pending = None
        if keep:
            self._credentials = keep
        try:
            self.set_cookies(cookies)
        except AuthError as e:
            raise AuthError(e.args[0], f"na inloggen: cookies {sorted(cookies)}") from e

    def _login_error(self, step, code, body):
        # What came back, without values: enough to see where Marktplaats stopped us.
        detail = f"{step}: {code}, velden {sorted(body.keys())}"
        return AuthError(LOGIN_ERRORS.get(code) or f"Inloggen mislukt ({code}).", detail)

    def login(self, email, password, remember=False):
        """
        Returns {'logged_in': True}, or {'need_code': True, 'phone': '***123'}
        when Marktplaats has sent an SMS code; finish that with login_code().
        """
        s = self._new_session()
        url = f"{BASE}/identity/v2/login"
        try:
            page = s.get(url, headers={"Accept": "text/html"}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise MarktplaatsError(f"Marktplaats is niet bereikbaar: {e}") from e
        env = page_config(page.text, "__ENV_CONFIG__") or {}
        xsrf = env.get("xsrfToken")
        if page.status_code != 200 or not xsrf:
            raise MarktplaatsError(f"Loginpagina van Marktplaats niet leesbaar (status {page.status_code}).")

        ok, code, body = self._identity_post(
            s, "/identity/v2/api/login", xsrf, referer=url,
            payload={"email": email, "password": password, "rememberMe": True,
                     "threatMetrix": env.get("threatMetrix") or {}})
        keep = {"email": email, "password": password} if remember else None
        if ok:
            self._adopt(s, keep)
            return {"logged_in": True}
        if code != "TWO_FACTOR_AUTH_NEEDED":
            raise self._login_error("wachtwoordstap", code, body)

        key = (body.get("verification") or {}).get("id")
        if not key:
            raise self._login_error("wachtwoordstap zonder verificatie-id", code, body)
        challenge = f"{BASE}/identity/v2/two-factor-auth-setup/challenge?key={key}&success_url=/&flow=login"
        try:
            cpage = s.get(challenge, headers={"Accept": "text/html", "Referer": url}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise MarktplaatsError(f"Marktplaats is niet bereikbaar: {e}") from e
        cenv = page_config(cpage.text, "__ENV_CONFIG__") or {}
        phone = (cenv.get("userDetails") or {}).get("twoAuthPhoneNumber") or cenv.get("tamperedRecoveryPhoneNumber") or ""
        self._pending = {
            "session": s, "referer": challenge, "ts": time.time(), "keep": keep,
            "xsrf": cenv.get("xsrfToken") or (page_config(cpage.text, "__HEADER_CONFIG__") or {}).get("xsrfToken") or xsrf,
            "request_id": cenv.get("twoFactorAuthRequestId") or key,
            # an account that already has a phone number verifies; a new one activates
            "verify": cenv.get("twoFactorAuthSetupChallenge", True) is not False,
        }
        return {"need_code": True, "phone": "*" * 6 + str(phone)[-3:] if phone else ""}

    def _pending_login(self):
        p = self._pending
        if not p or time.time() - p["ts"] > PENDING_TTL:
            self._pending = None
            raise AuthError("Deze inlogpoging is verlopen. Begin opnieuw met e-mail en wachtwoord.")
        return p

    def login_code(self, code):
        p = self._pending_login()
        path = "submit-verify-code" if p["verify"] else "submit-code"
        ok, err, body = self._identity_post(
            p["session"], f"/identity/v2/api/two-factor-auth/{path}", p["xsrf"], referer=p["referer"],
            payload={"code": re.sub(r"\D", "", code), "requestId": p["request_id"]})
        if not ok:
            raise self._login_error("codestap", err, body)
        self._adopt(p["session"], p["keep"])
        return {"logged_in": True}

    def login_resend(self):
        p = self._pending_login()
        ok, err, body = self._identity_post(
            p["session"], "/identity/v2/api/two-factor-auth/resend-code", p["xsrf"], referer=p["referer"],
            payload={"requestId": p["request_id"]})
        if not ok:
            raise self._login_error("code opnieuw sturen", err, body)
        return {"sent": True}

    def _relogin(self):
        """
        The session died and a password is kept: try once, quietly. An SMS code
        cannot be answered from here, and a refused password is thrown away
        rather than tried again.
        """
        if not self._credentials or time.time() - self._last_relogin < RELOGIN_PAUSE:
            return False
        self._last_relogin = time.time()
        creds = self._credentials
        try:
            result = self.login(creds["email"], creds["password"], remember=True)
        except AuthError as e:
            if len(e.args) > 1 and "WRONG_USERNAME_PASSWORD" in e.args[1]:
                self.forget_password()
            return False
        except MarktplaatsError:
            return False
        if result.get("need_code"):
            self._pending = None          # nobody is there to type the code
            return False
        return True

    def logout(self):
        self.http.cookies.clear()
        self._saved = None
        self._credentials = None
        self._pending = None
        self._persist()

    def logged_in(self):
        if not self.has_cookies():
            return False
        try:
            r = self.http.get(f"{BASE}/my-account/sell/api/listings",
                              params={"batchNumber": 1, "batchSize": 1},
                              headers={"Accept": "application/json"}, timeout=TIMEOUT)
        except requests.RequestException:
            return False
        return r.status_code == 200

    def _request(self, method, path, **kw):
        if not self.has_cookies():
            if not self._relogin():
                raise AuthError("Niet ingelogd: log eerst in bij Instellingen.")
        url = path if path.startswith("http") else BASE + path
        retried = kw.pop("_retried", False)
        timeout = kw.pop("timeout", TIMEOUT)
        try:
            r = self.http.request(method, url, timeout=timeout, **kw)
        except requests.RequestException as e:
            raise MarktplaatsError(f"Marktplaats is niet bereikbaar: {e}") from e
        if r.status_code in (401, 403) or "/identity/" in r.url:
            if not retried and "files" not in kw and self._relogin():
                return self._request(method, path, _retried=True, timeout=timeout, **kw)
            raise AuthError("Marktplaats-sessie verlopen: log opnieuw in bij Instellingen.")
        self._persist()
        return r

    def _page(self, path, params=None):
        r = self._request("GET", path, params=params, headers={"Accept": "text/html"})
        if r.status_code != 200:
            raise MarktplaatsError(f"{path} gaf status {r.status_code}")
        cfg = page_config(r.text)
        if cfg is None:
            raise MarktplaatsError(f"{path}: geen paginagegevens gevonden (Marktplaats heeft de pagina veranderd?)")
        return cfg, r

    # ---- reading ---------------------------------------------------------

    def listings(self):
        """Every ad of the account, as the seller overview lists them."""
        ads, batch = [], 1
        while batch <= 40:
            r = self._request("GET", "/my-account/sell/api/listings",
                              params={"batchNumber": batch, "batchSize": 50, "query": "",
                                      "categoryId": "", "inExpirationWindow": ""},
                              headers={"Accept": "application/json"})
            if r.status_code != 200:
                raise MarktplaatsError(f"Advertentielijst gaf status {r.status_code}")
            data = r.json()
            page = data.get("ads") or []
            ads.extend(page)
            if len(page) < 50 or len(ads) >= (data.get("totalNumberOfResults") or 0):
                break
            batch += 1
        return ads

    def read_ad(self, item_id):
        """
        The full ad: how it is shown (listing) and how the form holds it (form).
        The form half is what a re-post is built from; it is only there while
        the ad can still be edited.
        """
        cfg, _ = self._page(f"/seller/view/{item_id}")
        listing = cfg.get("listing")
        if not listing:
            raise MarktplaatsError(f"{item_id}: advertentie niet gevonden")
        form = None
        try:
            edit, _ = self._page(f"/plaats/{item_id}/edit")
            form = {k: v for k, v in (edit.get("reactData") or {}).items() if k not in FORM_DROP}
            for key in ("xsrfToken", "tmsid", "timestamp", "signature"):
                form.get("form", {}).pop(key, None)
            form.get("contacts", {}).get("contactInfo", {}).pop("countries", None)
        except MarktplaatsError:
            pass
        return {"item_id": item_id, "listing": listing, "form": form}

    def download_photo(self, url):
        """The original upload: the CDN serves it when the size rule is left off."""
        if url.startswith("//"):
            url = "https:" + url
        for candidate in (url.split("?")[0], re.sub(r"\$_(#|\d+)", "$_86", url)):
            r = self.http.get(candidate, timeout=TIMEOUT)
            if r.status_code == 200 and len(r.content) > 500 and \
                    r.headers.get("Content-Type", "").startswith("image/"):
                return r.content
        raise MarktplaatsError(f"Foto niet te downloaden: {url}")

    # ---- placing ---------------------------------------------------------

    def _bucket(self, l1, l2):
        """The middle category level; the form wants it next to l1 and l2."""
        r = self._request("GET", f"/plaats/api/v1/category-aggregate/{l1}/buckets",
                          headers={"Accept": "application/json"})
        if r.status_code != 200:
            return ""
        for bucket in r.json():
            c = self._request("GET", f"/plaats/api/v1/category-aggregate/{l1}/buckets/{bucket['id']}/categories",
                              headers={"Accept": "application/json"})
            if c.status_code == 200 and any(str(cat.get("id")) == str(l2) for cat in c.json()):
                return str(bucket["id"])
        return ""

    def _upload(self, path, xsrf, referer):
        name = os.path.basename(path)
        with open(path, "rb") as f:
            r = self._request("POST", "/plaats/api/image/upload",
                              headers={"x-mp-xsrf": xsrf, "Origin": BASE, "Referer": referer,
                                       "Accept": "application/json"},
                              data={"name": name},
                              files={"imageData": (name, f, "image/jpeg")}, timeout=90)
        if r.status_code not in (200, 201):
            raise MarktplaatsError(f"Foto {name} uploaden mislukt: {r.status_code} {r.text[:200]}")
        data = r.json()
        if not data.get("id") or data.get("valid") is False:
            raise MarktplaatsError(f"Foto {name} geweigerd door Marktplaats: {r.text[:200]}")
        return data["id"], data.get("largeImageUrl") or data.get("imageUrl") or ""

    def build_fields(self, form, new, bucket, images, price=None):
        """
        The form POST, field for field as the browser sends it. `form` is the
        backed-up ad, `new` the empty form Marktplaats just gave us (tokens,
        current contact details, current shipping prices).
        """
        locale = (new.get("adDetails") or {}).get("locale") or "nl-NL"
        details = form.get("adDetails") or {}
        fields = []

        for image_id, url in images:
            fields += [("images.ids", image_id), ("images.urls", url)]
        fields.append(("images.ids", ""))

        def localized(value):
            return (value.get(locale) or next(iter(value.values()), "")) if isinstance(value, dict) else (value or "")

        fields += [(f"title_{locale}", localized(details.get("title"))),
                   (f"description_{locale}", localized(details.get("description"))),
                   ("url", "")]

        for name, value in ((form.get("attributeDetails") or {}).get("prefillValues") or {}).items():
            for v in (value if isinstance(value, list) else [value]):
                if v not in ("", None):
                    fields.append((name, str(v)))

        p = form.get("price") or {}
        ptype = p.get("typeValue") or "FIXED"
        amount = price if price is not None else p.get("priceValue")
        offered = {t.get("value") for t in (new.get("price") or {}).get("priceTypes") or []}
        fields.append(("Dropdown-prijstype", ptype if ptype in offered else "FIXED"))
        bidding = bool(p.get("biddingEnabled"))
        if ptype in ("FIXED", "MIN_BID") and amount not in (None, ""):
            fields.append(("price.value", money(amount)))
            if bidding and p.get("minimumBidPrice"):
                min_bid = min(float(p["minimumBidPrice"]), float(amount))
                fields.append(("price.minimumBidPrice", money(min_bid)))
        fields += [("price.typeValue", ptype), ("previousPriceType", ""),
                   ("price.biddingEnabled", "on" if bidding else "off")]

        s, ns = form.get("shipping") or {}, new.get("shipping") or {}
        method = next((d["type"] for d in s.get("deliveryMethods") or [] if d.get("selected")), None)
        if method:
            fields.append(("deliveryMethod", method))
        if method and method != "Ophalen" and ns.get("isShippingAvailable"):
            if (s.get("buyerProtectionConfig") or {}).get("value"):
                fields.append(("buyerProtectionAllowed", "on"))
            for carrier in s.get("shippingMethods") or []:
                if carrier.get("isSelected"):
                    fields.append(("shippingMethod", carrier["id"]))
                    locker = carrier.get("lockerAvailability") or {}
                    if carrier["id"] == "budbee" and locker.get("postcode"):
                        fields.append(("dropOffLocationCode", locker["postcode"]))
            size = next((pk["id"] for pk in s.get("shippingPackages") or [] if pk.get("isSelected")), None)
            if size:
                current = next((pk for pk in ns.get("shippingPackages") or [] if pk.get("id") == size), {})
                cost = ((current.get("options") or [{}])[0]).get("cost", "")
                fields += [("packageSize", size), ("shippingDetails.price", str(cost)),
                           ("shippingDetails.provider", "other")]

        contact = (new.get("contacts") or {}).get("contactInfo") or {}
        old_contact = (form.get("contacts") or {}).get("contactInfo") or {}
        phone = old_contact.get("phoneNumber") or ""
        address = contact.get("addressIndex") or "syi-address-nl"
        fields.append(("contactInformation.sellerName", contact.get("sellerName") or old_contact.get("sellerName", "")))
        if phone:
            fields.append(("contactInformation.phoneNumber", phone))
        fields += [("contactInformation.phoneNumberSwitch", "true" if phone else "false"),
                   ("syi-address-radio-home", address), ("address-index-filter", address),
                   ("contactInformation.postCode", contact.get("postCode") or old_contact.get("postCode", ""))]

        if ((new.get("features") or {}).get("bundles") or {}).get("isEnabled"):
            fields += [("bundle-selection", "FREE"), ("selectedBundle", "FREE")]

        f, cat = new.get("form") or {}, new.get("category") or {}
        fields += [("xsrf.token", f.get("xsrfToken", "")), ("l1", cat.get("l1Id", "")),
                   ("bucket", bucket), ("l2", cat.get("l2Id", "")),
                   ("l2CategoryFullName", cat.get("categoryFullName", "")),
                   ("tmsid", f.get("tmsid", "")), ("timestamp", str(int(f.get("timestamp") or 0) + 1)),
                   ("signature", f.get("signature", ""))]
        return fields

    def place(self, form, photo_paths, price=None, dry_run=False):
        """
        Create a new ad from a backed-up form. Returns {'item_id', 'url'}, or
        with dry_run the fields that would have been sent (nothing is uploaded
        or posted, the tokens are left out).
        """
        if not form:
            raise MarktplaatsError("Deze backup is onvolledig (geen formuliergegevens) en kan niet geplaatst worden.")
        if not photo_paths:
            raise MarktplaatsError("Deze backup heeft geen foto's.")
        cat = form.get("category") or {}
        l1, l2 = cat.get("l1Id"), cat.get("l2Id")
        if not l1 or not l2:
            raise MarktplaatsError("Categorie ontbreekt in de backup.")

        bucket = self._bucket(l1, l2)
        cfg, page = self._page(f"/plaats/{l1}/{l2}", params={"bucketId": bucket} if bucket else None)
        new = cfg.get("reactData") or {}
        ncat = new.get("category") or {}
        if ncat.get("errors"):
            raise MarktplaatsError(f"Marktplaats weigert deze categorie: {ncat['errors']}")
        # Never spend money: only place where the free ad type is on offer.
        if ncat.get("isPaidAd") or ncat.get("isNewPaidAd"):
            raise MarktplaatsError("In deze categorie kost plaatsen geld; dat doet deze tool niet.")
        bundles = (new.get("features") or {}).get("bundles") or {}
        if bundles.get("isEnabled") and not any(
                b.get("bundleType") == "FREE" and not b.get("priceCents") for b in bundles.get("bundles") or []):
            raise MarktplaatsError("Er is geen gratis advertentievorm beschikbaar; niet geplaatst.")
        xsrf = (new.get("form") or {}).get("xsrfToken")
        if not xsrf:
            raise MarktplaatsError("Geen formuliertoken gekregen van Marktplaats.")

        if dry_run:
            images = [(f"<foto {os.path.basename(p)}>", "") for p in photo_paths]
            hidden = {"xsrf.token", "signature", "tmsid", "timestamp"}
            return {"dry_run": True,
                    "fields": [(k, v) for k, v in self.build_fields(form, new, bucket, images, price)
                               if k not in hidden and k != "images.urls"]}

        images = [self._upload(p, xsrf, page.url) for p in photo_paths]
        fields = self.build_fields(form, new, bucket, images, price)
        r = self._request("POST", "/plaats/ads", data=fields, timeout=90,
                          headers={"Origin": BASE, "Referer": page.url,
                                   "Accept": "text/html,application/xhtml+xml"})
        m = re.search(r"/seller/view/(m\d+)", r.url)
        if m:
            return {"item_id": m.group(1), "url": f"{BASE}/seller/view/{m.group(1)}"}
        raise MarktplaatsError("Niet geplaatst: " + self._form_errors(r), r.text)

    @staticmethod
    def _form_errors(r):
        """What the re-rendered form complains about, as readable text."""
        found = []

        def walk(node, path=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ("errors", "error", "errorMessage", "errorText") and v:
                        found.append(f"{path}: {json.dumps(v, ensure_ascii=False)[:200]}")
                    else:
                        walk(v, f"{path}.{k}" if path else k)
            elif isinstance(node, list):
                for item in node[:50]:
                    walk(item, path)

        walk((page_config(r.text) or {}).get("reactData") or {})
        if found:
            return "; ".join(found[:6])
        return f"onverwacht antwoord van Marktplaats (status {r.status_code}, {r.url})"

    # ---- deleting --------------------------------------------------------

    def delete(self, item_id):
        _, page = self._page(f"/seller/view/{item_id}")
        m = re.search(r'"xsrfToken":"([^"]+)"', page.text)
        if not m:
            raise MarktplaatsError("Geen token gevonden om te verwijderen.")
        r = self._request("DELETE", "/seller/api/delete-ad",
                          json={"itemId": item_id, "reason": "NO_DEAL_VIA_MP"},
                          headers={"x-mp-xsrf": m.group(1), "Origin": BASE,
                                   "Referer": f"{BASE}/seller/view/{item_id}", "Accept": "*/*"})
        if r.status_code not in (200, 204):
            raise MarktplaatsError(f"Verwijderen mislukt: {r.status_code} {r.text[:200]}")
        return True
