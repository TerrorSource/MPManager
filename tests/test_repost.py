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

    def __init__(self):
        self.placed, self.deleted, self.fail = [], [], {}

    def read_ad(self, item_id):
        return {"item_id": item_id, "form": {"category": {}},
                "listing": {"title": f"Advertentie {item_id}", "startDate": int(time.time() * 1000),
                            "price": {"priceType": "FIXED", "priceCent": 1000},
                            "gallery": {"imageUrls": [f"//img/{item_id}.jpg"]}}}

    def download_photo(self, url):
        error = self.fail.get("photo")
        if error and (error[1] is None or error[1] in url):
            raise error[0]
        return b"jpeg"

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
def mp(monkeypatch):
    fake = FakeMarktplaats()
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
    mp.fail["photo"] = (requests.ConnectionError("Connection aborted."), "m90")
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
    monkeypatch.setattr(mp_api.time, "sleep", lambda s: None)
    calls = []

    class Answer:
        status_code, url = 200, mp_api.BASE + "/x"

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
