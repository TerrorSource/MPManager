"""Herplaatsen: wat er gebeurt als de verbinding halverwege wegvalt (met een nagebootst Marktplaats)."""
import time

import pytest
import requests

import mp_api
import notify
import store
import worker
from mp_api import MarktplaatsError


class FakeMarktplaats:
    """Doet wat mp_api.Marktplaats doet, zonder netwerk; per stap kan een fout worden ingesteld."""

    site_name = "Marktplaats"

    def __init__(self):
        self.placed, self.deleted, self.fail, self.downloaded, self.prices = [], [], {}, [], []

    def read_ad(self, item_id):
        error = self.fail.get("read")
        if error and error[1] == item_id:
            raise error[0]
        return {"item_id": item_id, "form": {"category": {}},
                "listing": {"title": f"Advertentie {item_id}", "startDate": int(time.time() * 1000),
                            "price": {"priceType": "FIXED", "priceCent": 1000},
                            "gallery": {"imageUrls": [f"//img/{item_id}.jpg"]}}}

    def download_photo(self, url):
        error = self.fail.get("photo")
        if error and (error[1] is None or error[1] in url):
            raise error[0]
        self.downloaded.append(url)
        return f"gedownload {url}".encode()

    def update_price(self, item_id, price, min_bid=None, dry_run=False):
        if not dry_run:
            self.prices.append((item_id, price, min_bid))
        return {"dry_run": True, "fields": []} if dry_run else {"item_id": item_id}

    def place(self, form, photos, price=None, min_bid=None, dry_run=False):
        if "place" in self.fail:
            raise self.fail["place"]
        new_id = f"m9{len(self.placed)}"
        self.placed.append(new_id)
        return {"item_id": new_id, "url": ""}

    def delete(self, item_id):
        if "delete" in self.fail:
            raise self.fail["delete"]
        self.deleted.append(item_id)


@pytest.fixture()
def mp(monkeypatch, tmp_path):
    fake = FakeMarktplaats()
    monkeypatch.setattr(store, "BACKUP_DIR", str(tmp_path / "backups"))     # elke test zijn eigen backups
    monkeypatch.setattr(worker, "client", fake)
    monkeypatch.setattr(notify, "send", lambda text: None)
    store.save_settings({"dry_run": False})
    return fake


def test_connection_lost_while_backing_up_the_original_changes_nothing(mp):
    mp.fail["photo"] = (requests.ConnectionError("Connection aborted."), "m100")
    with pytest.raises(requests.ConnectionError):
        worker.do_repost("m100")
    assert mp.placed == [] and mp.deleted == []


def test_connection_lost_while_backing_up_the_new_ad_is_still_a_repost(mp):
    mp.fail["read"] = (requests.ConnectionError("Connection aborted."), "m90")
    message = worker.do_repost("m101")
    assert mp.placed == ["m90"] and mp.deleted == ["m101"]
    assert message.startswith("herplaatst als m90") and "backup volgt later" in message


def test_unexpected_error_while_deleting_keeps_the_new_ad_and_warns(mp):
    mp.fail["delete"] = requests.ConnectionError("Connection aborted.")
    message = worker.do_repost("m102")
    assert mp.placed == ["m90"] and mp.deleted == []
    assert "niet verwijderd" in message


def test_failed_placement_leaves_the_original(mp):
    mp.fail["place"] = MarktplaatsError("Niet geplaatst: test")
    with pytest.raises(MarktplaatsError):
        worker.do_repost("m103")
    assert mp.deleted == []


def test_photo_download_retries_and_reports_cleanly(monkeypatch):
    client = mp_api.Marktplaats(store.CONFIG_DIR + "/state-test.json")
    monkeypatch.setattr(mp_api.time, "sleep", lambda s: None)
    calls = []

    class Photo:
        status_code, content, headers = 200, b"x" * 600, {"Content-Type": "image/jpeg"}

    def flaky(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise requests.ConnectionError("Connection aborted.")
        return Photo()

    monkeypatch.setattr(client.http, "get", flaky)
    assert client.download_photo("//images.example/a.jpg?rule=x") == Photo.content
    assert len(calls) == 3

    monkeypatch.setattr(client.http, "get", lambda url, timeout: (_ for _ in ()).throw(requests.ConnectionError("weg")))
    with pytest.raises(MarktplaatsError, match="Foto niet te downloaden"):
        client.download_photo("//images.example/b.jpg")


def test_reads_are_retried_but_writes_are_not(monkeypatch):
    client = mp_api.Marktplaats(store.CONFIG_DIR + "/state-test2.json")
    client._set_cookies({"MpSecurity": "a", "MpSslSecurity": "b"})
    client._authenticated = True
    monkeypatch.setattr(mp_api.time, "sleep", lambda s: None)
    calls = []

    class Answer:
        status_code, url = 200, client.base + "/x"

    def flaky(method, url, timeout, **kw):
        calls.append(method)
        if len(calls) < 3:
            raise requests.ConnectionError("Connection aborted.")
        return Answer()

    monkeypatch.setattr(client.http, "request", flaky)
    monkeypatch.setattr(client, "_persist", lambda: None)
    assert client._request("GET", "/x").status_code == 200 and calls == ["GET"] * 3

    calls.clear()
    with pytest.raises(MarktplaatsError):
        client._request("POST", "/plaats/ads", data={})
    assert calls == ["POST"]


def test_auto_backup_redoes_a_backup_without_photos(mp, monkeypatch):
    mp.listings = lambda: [{"itemId": "m200", "title": "Zonder foto's"}, {"itemId": "m201", "title": "Met foto's"}]
    mp.fail["photo"] = (MarktplaatsError("nog niet verwerkt"), "m200")
    with pytest.raises(MarktplaatsError):
        worker.do_backup("m200")
    mp.fail.clear()
    empty = mp.read_ad("m200")
    empty["listing"]["gallery"] = {"imageUrls": []}
    store.save_backup(mp, empty)                     # zoals vlak na het plaatsen: nog geen foto's
    worker.do_backup("m201")
    queued = []
    monkeypatch.setattr(worker, "enqueue", lambda kind, item_id, title=None, **kw: queued.append((kind, item_id)))
    store.save_settings({"auto_backup": True, "auto_repost": False, "backup_retention_days": 0})
    worker._scan()
    assert queued == [("backup", "m200")]


# --- originele foto's ---------------------------------------------------------

def test_reposted_ad_keeps_the_original_photo_files(mp):
    worker.do_repost("m300")
    assert mp.placed == ["m90"]
    original = open(store.backup_photos("m300")[0], "rb").read()
    assert open(store.backup_photos("m90")[0], "rb").read() == original       # overgenomen, niet opnieuw gedownload
    assert [u for u in mp.downloaded if "m90" in u] == []
    assert store.read_meta("m90")["photos_from"] == "m300"

    # een latere backup laat ze staan; pas als de foto's op de site veranderen wordt er gedownload
    worker.do_backup("m90")
    assert [u for u in mp.downloaded if "m90" in u] == []
    changed = mp.read_ad("m90")
    changed["listing"]["gallery"] = {"imageUrls": ["//img/m90-nieuw.jpg"]}
    store.save_backup(mp, changed)
    assert open(store.backup_photos("m90")[0], "rb").read() == b"gedownload //img/m90-nieuw.jpg"
    assert store.read_meta("m90")["photos_from"] is None


# --- prijs aanpassen zonder herplaatsen ---------------------------------------

def test_price_change_goes_to_the_ad_itself(mp):
    message = worker.do_price("m400", 7.5)
    assert mp.prices == [("m400", 7.5, None)] and mp.placed == [] and mp.deleted == []
    assert "€ 10,00 → € 7,50" in message
    assert store.history()[0]["action"] == "prijs aangepast"

    store.save_settings({"dry_run": True})
    assert worker.do_price("m400", 5).startswith("proef")
    assert len(mp.prices) == 1


def _edit_page(price_type="MIN_BID"):
    return {"reactData": {
        "form": {"isEditMode": True, "xsrfToken": "x", "placeAdURL": "/plaats/m1/edit", "tmsid": "t", "timestamp": 5, "signature": "s"},
        "category": {"l1Id": "1", "l2Id": "2", "categoryFullName": "Cat"},
        "adDetails": {"locale": "nl-NL", "title": {"nl-NL": "Titel"}, "description": {"nl-NL": "Tekst"}},
        "attributeDetails": {"prefillValues": {"singleSelectAttribute[condition]": "Nieuw"}},
        "price": {"typeValue": price_type, "priceValue": 70, "biddingEnabled": True, "minimumBidPrice": 60,
                  "previousPriceType": price_type, "priceTypes": [{"value": "FIXED"}]},
        "shipping": {"deliveryMethods": [{"type": "Ophalen", "selected": True}]},
        "contacts": {"contactInfo": {"sellerName": "Naam", "postCode": "1234AB", "addressIndex": "syi-address-nl"}},
        "features": {"bundles": {"isEnabled": False}},
        "media": {"imageUpload": {"initialImageIds": ["id1"], "initialImageUrls": ["//img/a?rule=x$_#.jpg"]}},
    }}


def test_update_price_sends_the_edit_form_back_with_the_new_price(monkeypatch):
    client = mp_api.Marktplaats(store.CONFIG_DIR + "/state-test3.json")
    client._authenticated = True
    sent = {}

    class Page:
        url = client.base + "/plaats/m1/edit"

    def page(path, params=None):
        if path.startswith("/seller/view/"):
            return {"listing": {"price": {"priceCent": sent.get("cents", 7000)}}}, Page()
        return _edit_page(), Page()

    def request(method, path, **kw):
        sent.update(method=method, path=path, fields=kw["data"], cents=5000)

        class Answer:
            url = client.base + "/seller/view/m1"
        return Answer()

    monkeypatch.setattr(client, "_page", page)
    monkeypatch.setattr(client, "_request", request)

    dry = client.update_price("m1", 50, dry_run=True)
    assert sent == {} and ("price.value", "50,00") in dry["fields"]
    assert not any(k in ("xsrf.token", "signature") for k, _ in dry["fields"])

    assert client.update_price("m1", 50) == {"item_id": "m1"}
    fields = sent["fields"]
    assert (sent["method"], sent["path"]) == ("POST", "/plaats/m1/edit")
    assert ("price.value", "50,00") in fields and ("price.minimumBidPrice", "50,00") in fields    # bod nooit boven de prijs
    assert ("previousPriceType", "MIN_BID") in fields and ("price.typeValue", "MIN_BID") in fields
    assert ("images.ids", "id1") in fields and ("images.urls", "//img/a?rule=x$_86.jpg") in fields
    assert ("title_nl-NL", "Titel") in fields and ("timestamp", "6") in fields and ("bucket", "") in fields

    # de site toont daarna een andere prijs: niet als gelukt melden
    monkeypatch.setattr(client, "_request", lambda method, path, **kw: (_ for _ in ()).throw(MarktplaatsError("weg")))
    sent["cents"] = 7000
    with pytest.raises(MarktplaatsError, match="Prijs niet aangepast"):
        client.update_price("m1", 50)


# --- geen antwoord op het plaatsen ---------------------------------------------

def test_lost_answer_on_placing_is_settled_by_the_ad_list(monkeypatch, tmp_path):
    client = mp_api.Marktplaats(store.CONFIG_DIR + "/state-test4.json")
    client._authenticated = True
    monkeypatch.setattr(mp_api.time, "sleep", lambda s: None)
    photo = tmp_path / "001.jpg"
    photo.write_bytes(b"x")
    form = _edit_page("FIXED")["reactData"]
    new = dict(form, form={"xsrfToken": "x", "tmsid": "t", "timestamp": 1, "signature": "s"})
    lists = [[{"itemId": "m1", "title": "Titel"}]]

    class Page:
        url = client.base + "/plaats/1/2"

    monkeypatch.setattr(client, "_bucket", lambda l1, l2: "9")
    monkeypatch.setattr(client, "_page", lambda path, params=None: ({"reactData": new}, Page()))
    monkeypatch.setattr(client, "_upload", lambda path, xsrf, referer: ("id", "url"))
    monkeypatch.setattr(client, "listings", lambda: lists[0])
    monkeypatch.setattr(client, "_request", lambda method, path, **kw: (_ for _ in ()).throw(MarktplaatsError("geen antwoord")))

    with pytest.raises(MarktplaatsError, match="waarschijnlijk niet geplaatst"):
        client.place(form, [str(photo)])

    # dezelfde titel onder een nieuw nummer in de lijst: dat is hem
    calls = []

    def listings():
        calls.append(1)
        return lists[0] if len(calls) == 1 else lists[0] + [{"itemId": "m2", "title": "Titel"}]
    monkeypatch.setattr(client, "listings", listings)
    assert client.place(form, [str(photo)]) == {"item_id": "m2", "url": client.base + "/seller/view/m2", "confirmed_by": "list"}


# --- wachtrij en log overleven een herstart ------------------------------------

def test_queue_survives_a_restart(mp, monkeypatch):
    monkeypatch.setattr(worker, "_jobs", {})
    waiting = worker.enqueue("repost", "m500", "Wacht", delay=300)
    running = worker.enqueue("repost", "m501", "Bezig", delay=0)
    backing = worker.enqueue("backup", "m502", "Backup", delay=0)
    worker._set(running, status="bezig")
    worker._set(backing, status="bezig")

    monkeypatch.setattr(worker, "_jobs", {})          # het proces is weg; alleen het bestand is er nog
    worker.restore()
    jobs = {j["item_id"]: j for j in worker.jobs()}
    assert jobs["m500"]["status"] == "wacht" and jobs["m500"]["run_at"] >= waiting["run_at"]
    assert jobs["m501"]["status"] == "mislukt" and "herstart" in jobs["m501"]["message"]    # niet nog eens uitvoeren
    assert jobs["m502"]["status"] == "wacht"                                                # een backup mag opnieuw
    assert worker.enqueue("backup", "m503")["id"] > max(j["id"] for j in jobs.values())
    assert mp.placed == []


def test_several_ads_at_once_are_spaced_out(mp, monkeypatch):
    monkeypatch.setattr(worker, "_jobs", {})
    store.save_settings({"manual_delay_min": 30, "manual_delay_max": 30})
    now = time.time()
    queued = worker.enqueue_many("repost", [("m600", "a"), ("m601", "b"), ("m602", "c")])
    assert [round(j["run_at"] - now, -1) for j in queued] == [30, 60, 90]
    store.save_settings({"dry_run": True})
    monkeypatch.setattr(worker, "_jobs", {})
    assert all(j["run_at"] - time.time() < 1 for j in worker.enqueue_many("repost", [("m603", "d"), ("m604", "e")]))


def test_log_is_kept_on_disk():
    store.log("regel voor de herstart")
    store.LOG.clear()
    store._load_log()
    assert store.LOG[-1].endswith("regel voor de herstart")


# --- instellingen: één bestand, één schema -------------------------------------

def test_all_settings_live_in_one_file():
    from watcher import db
    s, refused = store.save_settings({"max_per_day": 5, "default_limit_per_run": 50, "postcode": "1234 ab",
                                      "sleep_start": "7:5", "match_mode": "x", "blocked_sellers": "Piet\npiet\n\nJan",
                                      "telegram_bot_id": "hoort hier niet"})
    assert sorted(refused) == ["default_limit_per_run", "match_mode"]
    assert (s["max_per_day"], s["postcode"], s["sleep_start"], s["blocked_sellers"]) == (5, "1234AB", "07:05", ["Piet", "Jan"])
    assert "telegram_bot_id" not in s
    assert db.load_settings()["postcode"] == "1234AB" and "max_per_day" not in db.load_settings()
    db.save_settings({"postcode": "", "blocked_sellers": []})
    assert store.settings()["postcode"] == ""


# --- 2dehands -------------------------------------------------------------------

def test_site_follows_the_login(monkeypatch):
    client = mp_api.Marktplaats(store.CONFIG_DIR + "/state-test5.json")
    assert client.base == "https://www.marktplaats.nl" and not client.has_login()
    monkeypatch.setattr(client, "_accepted", lambda: False)
    with pytest.raises(mp_api.AuthError, match="2dehands accepteert"):
        client.set_cookies({"MpSecurity": "a"}, "2dehands")
    assert client.site == "marktplaats" and not client.has_login()            # een mislukte poging verandert niets

    monkeypatch.setattr(client, "_accepted", lambda: True)
    client.set_cookies({"MpSecurity": "a"}, "2dehands")
    assert client.base == "https://www.2dehands.be" and client.site_name == "2dehands" and client.has_login()
    assert {c.domain for c in client.http.cookies} == {".2dehands.be"}
    again = mp_api.Marktplaats(store.CONFIG_DIR + "/state-test5.json")       # na een herstart
    assert again.site == "2dehands" and again.has_login()
