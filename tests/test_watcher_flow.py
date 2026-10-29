"""Integratietests voor de zoek-flow en de schedulerlus (met gemockte netwerkcalls)."""
from datetime import datetime, timedelta

import pytest

from watcher import db, marketplace, notify, scheduler
from watcher.config import MarketplaceError


def _ad(ad_id, title, cents, seller="", location=""):
    return {
        "ad_id": ad_id, "title": title,
        "price": f"€ {cents/100:.2f}".replace(".", ",") if cents is not None else "Bieden",
        "price_cents": cents, "url": f"https://example.com/{ad_id}", "image_url": "",
        "posted_at": "", "seller": seller, "location": location,
    }


def _fetch(ads):
    """Mock voor marketplace.fetch_market_results die ook de categorie-argumenten accepteert."""
    return lambda term, settings, limit, category_id=None, category_parent_id=None: [dict(a) for a in ads]


@pytest.fixture()
def telegram_log(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(notify, "_telegram_post", lambda method, payload, settings, label="": sent.append((method, payload)) or True)
    return sent


@pytest.fixture()
def keyword():
    db.init_db()
    kid = db.add_keyword_row("flow-test", 15, None, None, 5)
    db.update_keyword_fields(kid, last_run_at=datetime.now().isoformat(timespec="seconds"))
    yield db.get_keyword(kid)
    db.delete_keyword_row(kid)
    db.reset_results_for_keyword(kid)


def _settings(**overrides):
    s = db.load_settings()
    # match_mode uit: deze tests gebruiken fictieve titels die niet op "flow-test" lijken
    s.update({"telegram_bot_id": "bot", "telegram_chat_id": "chat", "repost_dedup_days": 0, "match_mode": "off"})
    s.update(overrides)
    return s


# --- eerste run stil ---------------------------------------------------------

def test_first_run_is_silent_then_notifies_with_term(monkeypatch, telegram_log):
    db.init_db()
    kid = db.add_keyword_row("stil-test", 15, None, None, 5)
    kw = db.get_keyword(kid)
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("a1", "Fiets", 10000)]))
    assert scheduler.run_search_for_keyword(kw, _settings()) == (1, 1)
    assert telegram_log == []

    db.update_keyword_fields(kid, last_run_at=datetime.now().isoformat(timespec="seconds"))
    kw = db.get_keyword(kid)
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("a1", "Fiets", 10000), _ad("a2", "Nieuwe fiets", 12000, location="Utrecht")]))
    assert scheduler.run_search_for_keyword(kw, _settings()) == (2, 1)
    assert len(telegram_log) == 1
    text = telegram_log[0][1]["text"]
    assert "Zoekwoord = stil-test" in text and "Nieuwe fiets" in text and "Plaats = Utrecht" in text
    db.delete_keyword_row(kid)


# --- filters -----------------------------------------------------------------

def test_exclude_terms_and_title_blocklist(monkeypatch, telegram_log, keyword):
    db.update_keyword_fields(keyword["id"], exclude_terms="gezocht")
    kw = db.get_keyword(keyword["id"])
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([
        _ad("g1", "Gezocht: ticket marathon", 5000),
        _ad("g2", "Ticket marathon te koop", 5000),
        _ad("g3", "Viking klapschaatsen", 5000),
    ]))
    settings = _settings(blocked_titles=["  viking  KLAPSCHAATSEN "])
    assert scheduler.run_search_for_keyword(kw, settings) == (1, 1)
    assert [r["title"] for r in db.get_results_for_keyword(kw["id"])] == ["Ticket marathon te koop"]


def test_category_is_passed_to_fetch(monkeypatch, keyword):
    db.update_keyword_fields(keyword["id"], category_id=2002, category_parent_id=1984, category_label="Sport")
    kw = db.get_keyword(keyword["id"])
    seen = {}

    def fake_fetch(term, settings, limit, category_id=None, category_parent_id=None):
        seen.update(category_id=category_id, category_parent_id=category_parent_id)
        return []
    monkeypatch.setattr(marketplace, "fetch_market_results", fake_fetch)
    scheduler.run_search_for_keyword(kw, _settings())
    assert seen == {"category_id": 2002, "category_parent_id": 1984}


def test_fetch_market_results_filters_subcategory_locally(monkeypatch, real_fetch_market_results):
    calls = {}

    def fake_api(domain, params):
        calls.update(params)
        return {"listings": [
            {"itemId": "x1", "title": "in cat", "vipUrl": "/x1", "categoryId": 2002, "priceInfo": {"priceCents": 100, "priceType": "FIXED"}},
            {"itemId": "x2", "title": "andere cat", "vipUrl": "/x2", "categoryId": 1448, "priceInfo": {"priceCents": 100, "priceType": "FIXED"}},
            {"itemId": "x3", "title": "ook in cat", "vipUrl": "/x3", "categoryId": 2002, "priceInfo": {"priceCents": 100, "priceType": "FIXED"}},
        ]}
    monkeypatch.setattr(marketplace, "_api_get", fake_api)
    ads = real_fetch_market_results("q", db.load_settings(), 5, category_id=2002, category_parent_id=1984)
    assert [a["ad_id"] for a in ads] == ["x1", "x3"]
    assert calls["l1CategoryId"] == 1984 and calls["limit"] == 15  # oversampling bij subcategorie


# --- blocklist verkoper + prijsverlaging ------------------------------------

def test_blocklist_after_enrichment_and_price_drop(monkeypatch, telegram_log, keyword):
    settings = _settings(blocklist_enabled=True, blocked_sellers=["Gert"], price_drop_alerts=True)
    monkeypatch.setattr(marketplace, "fetch_seller_from_ad_page", lambda url: "Gert" if url.endswith("/b1") else "Piet")
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("b1", "Van Gert", 5000), _ad("b2", "Van Piet", 8000)]))
    assert scheduler.run_search_for_keyword(keyword, settings) == (2, 1)
    assert len(telegram_log) == 1

    telegram_log.clear()
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("b2", "Van Piet", 6000)]))
    assert scheduler.run_search_for_keyword(keyword, settings)[1] == 0
    assert len(telegram_log) == 1 and "€ 80,00 → € 60,00" in telegram_log[0][1]["text"]
    assert "Zoekwoord = flow-test" in telegram_log[0][1]["text"]


# --- herplaatsing ------------------------------------------------------------

def test_repost_dedup(monkeypatch, telegram_log, keyword):
    settings = _settings(repost_dedup_days=30)
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("r1", "Startbewijs marathon", 5000, seller="Max")]))
    scheduler.run_search_for_keyword(keyword, settings)
    assert len(telegram_log) == 1

    # Zelfde advertentie opnieuw geplaatst met nieuw id -> wel opgeslagen, niet gemeld
    telegram_log.clear()
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("r2", "Startbewijs  marathon ", 5000, seller="max")]))
    assert scheduler.run_search_for_keyword(keyword, settings) == (1, 1)
    assert telegram_log == []

    # Andere prijs -> geen herplaatsing, gewoon melden
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("r3", "Startbewijs marathon", 4000, seller="Max")]))
    scheduler.run_search_for_keyword(keyword, settings)
    assert len(telegram_log) == 1

    # Dedup uit -> wél melden
    telegram_log.clear()
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("r4", "Startbewijs marathon", 5000, seller="Max")]))
    scheduler.run_search_for_keyword(keyword, _settings(repost_dedup_days=0))
    assert len(telegram_log) == 1


# --- digest ------------------------------------------------------------------

def test_digest_when_many_new_ads(monkeypatch, telegram_log, keyword):
    ads = [_ad(f"d{i}", f"Advertentie {i}", 1000 * i, location="Zwolle") for i in range(1, 7)]
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch(ads))
    assert scheduler.run_search_for_keyword(keyword, _settings(telegram_digest_threshold=5))[1] == 6
    assert len(telegram_log) == 1
    text = telegram_log[0][1]["text"]
    assert "6 nieuwe advertenties" in text and "Advertentie 6" in text and "Zwolle" in text

    db.reset_results_for_keyword(keyword["id"])
    telegram_log.clear()
    scheduler.run_search_for_keyword(keyword, _settings(telegram_digest_threshold=0))
    assert len(telegram_log) == 6


# --- schedulerlus ------------------------------------------------------------

def test_scheduler_iteration_runs_due_keywords(monkeypatch, telegram_log):
    db.init_db()
    monkeypatch.setattr(scheduler.time_module, "sleep", lambda s: None)
    db.save_settings({"telegram_bot_id": "bot", "telegram_chat_id": "chat", "repost_dedup_days": 0, "match_mode": "off"})

    now = datetime.now()
    due = db.add_keyword_row("due", 15, None, None, 5)
    db.update_keyword_fields(due, last_run_at=(now - timedelta(minutes=20)).isoformat(timespec="seconds"))
    fresh = db.add_keyword_row("fresh", 15, None, None, 5)
    db.update_keyword_fields(fresh, last_run_at=(now - timedelta(minutes=2)).isoformat(timespec="seconds"))
    broken = db.add_keyword_row("broken", 15, None, None, 5)
    db.update_keyword_fields(broken, last_run_at=(now - timedelta(minutes=20)).isoformat(timespec="seconds"))

    def fake_fetch(term, settings, limit, category_id=None, category_parent_id=None):
        if term == "broken":
            raise MarketplaceError("HTTP 403")
        return [_ad(f"{term}-1", f"Nieuw voor {term}", 1000)]
    monkeypatch.setattr(marketplace, "fetch_market_results", fake_fetch)

    assert scheduler.run_scheduler_iteration(now) == 60
    assert scheduler.get_scheduler_status()["state"] in ("off", "ok")  # heartbeat gezet, worker niet gestart

    due_kw, fresh_kw, broken_kw = db.get_keyword(due), db.get_keyword(fresh), db.get_keyword(broken)
    assert due_kw["last_run_at"] >= now.isoformat(timespec="seconds") and due_kw["last_error"] == ""
    assert fresh_kw["last_run_at"] < now.isoformat(timespec="seconds")  # niet aan de beurt
    assert "HTTP 403" in broken_kw["last_error"]
    assert [p["text"] for _, p in telegram_log] and "Nieuw voor due" in telegram_log[0][1]["text"]

    for kid in (due, fresh, broken):
        db.delete_keyword_row(kid)
        db.reset_results_for_keyword(kid)


def test_scheduler_iteration_without_keywords_sleeps_30(monkeypatch):
    db.init_db()
    for kw in db.load_keywords():
        db.delete_keyword_row(kw["id"])
    assert scheduler.run_scheduler_iteration() == 30


# --- v22: backoff, meldingenlogboek, gereserveerd, afstand -------------------

def test_backoff_doubles_interval_after_failures(monkeypatch):
    db.init_db()
    monkeypatch.setattr(scheduler.time_module, "sleep", lambda s: None)
    now = datetime.now()
    kid = db.add_keyword_row("backoff", 15, None, None, 5)
    # laatste geslaagde run ligt ver terug; de fouten die volgen zijn recenter
    db.update_keyword_fields(kid, last_run_at=(now - timedelta(hours=2)).isoformat(timespec="seconds"))

    def boom(term, settings, limit, category_id=None, category_parent_id=None):
        raise MarketplaceError("HTTP 403")
    monkeypatch.setattr(marketplace, "fetch_market_results", boom)

    settings = db.load_settings()
    scheduler.run_scheduler_iteration(now)
    kw = db.get_keyword(kid)
    assert kw["error_count"] == 1 and kw["last_error_at"]
    assert scheduler.effective_interval(kw, settings, now) == timedelta(minutes=30)
    # Direct daarna is het zoekwoord niet aan de beurt (backoff vanaf de fouttijd)
    assert scheduler.compute_next_run(kw, settings, now) > now

    # Tweede mislukking (geforceerd, alsof de tijd verstreken is)
    db.update_keyword_fields(kid, last_error_at=(now - timedelta(hours=1)).isoformat(timespec="seconds"))
    scheduler.run_scheduler_iteration(now)
    kw = db.get_keyword(kid)
    assert kw["error_count"] == 2
    assert scheduler.effective_interval(kw, settings, now) == timedelta(minutes=60)
    assert scheduler.effective_interval({**kw, "error_count": 9}, settings, now) == timedelta(hours=6)  # plafond

    # Geslaagde run herstelt alles
    db.update_keyword_fields(kid, last_error_at=(now - timedelta(hours=5)).isoformat(timespec="seconds"))
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([]))
    scheduler.run_scheduler_iteration(now)
    kw = db.get_keyword(kid)
    assert kw["error_count"] == 0 and kw["last_error"] == "" and kw["last_error_at"] is None
    db.delete_keyword_row(kid)


def test_first_run_stays_silent_after_failed_attempt(monkeypatch, telegram_log):
    """Een mislukte eerste run mag de 'eerste run'-status niet wegnemen."""
    db.init_db()
    kid = db.add_keyword_row("stil-na-fout", 15, None, None, 5)
    kw = db.get_keyword(kid)
    scheduler.record_search_failure(kw, "HTTP 403")
    kw = db.get_keyword(kid)
    assert kw["last_run_at"] == "Nooit"
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("f1", "Oud", 1000)]))
    scheduler.run_search_for_keyword(kw, _settings())
    assert telegram_log == []
    db.delete_keyword_row(kid)


def test_notification_log_records_sent_and_suppressed(monkeypatch, telegram_log, keyword):
    settings = _settings(repost_dedup_days=30)
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("n1", "Ticket", 5000, seller="Max")]))
    scheduler.run_search_for_keyword(keyword, settings)
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch([_ad("n2", "Ticket", 5000, seller="Max")]))
    scheduler.run_search_for_keyword(keyword, settings)

    items = [n for n in db.get_notifications(50) if n["keyword_id"] == keyword["id"]]
    kinds = [(n["kind"], n["sent"], n["detail"]) for n in items]
    assert ("new", 1, "") in kinds
    assert any(k == "suppressed" and "herplaatsing" in d for k, _, d in kinds)


def test_skip_reserved_and_distance_in_caption(monkeypatch, telegram_log, keyword):
    ads = [dict(_ad("r1", "Vrij", 5000, location="Utrecht"), distance_km=12.3),
           dict(_ad("r2", "Weg", 5000), reserved=True)]
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch(ads))
    assert scheduler.run_search_for_keyword(keyword, _settings(skip_reserved=True)) == (1, 1)
    assert "Plaats = Utrecht (12.3 km)" in telegram_log[0][1]["text"]

    db.reset_results_for_keyword(keyword["id"])
    telegram_log.clear()
    assert scheduler.run_search_for_keyword(keyword, _settings(skip_reserved=False)) == (2, 2)
    assert any("Gereserveerd" in p["text"] for _, p in telegram_log)


# --- v23: verlopen advertenties, Telegram-retry, parsing ---------------------

def test_expiry_check_marks_removed_ads(monkeypatch, keyword):
    conn = db.connect()
    with conn:
        conn.execute("DELETE FROM results")  # gedeelde test-DB: alleen onze eigen rijen controleren
    conn.close()
    db.insert_new_ads(keyword["id"], [_ad("e1", "Nog te koop", 1000), _ad("e2", "Verwijderd", 1000), _ad("e3", "Onbekend", 1000)])
    monkeypatch.setattr(scheduler.time_module, "sleep", lambda s: None)
    verdicts = {"https://example.com/e1": True, "https://example.com/e2": False, "https://example.com/e3": None}
    monkeypatch.setattr(marketplace, "check_ad_alive", lambda url: verdicts.get(url))

    checked, expired = scheduler.run_expiry_check(_settings(expiry_check_days=7))
    assert (checked, expired) == (2, 1)
    rows = {r["title"]: r for r in db.get_results_for_keyword(keyword["id"])}
    assert rows["Verwijderd"]["expired"] == 1 and rows["Verwijderd"]["expired_at"]
    assert rows["Nog te koop"]["expired"] in (0, None)
    assert rows["Onbekend"]["expired"] in (0, None)

    # Gecontroleerde advertenties komen binnen 24 uur niet opnieuw aan de beurt; de onbekende wel
    assert [r["title"] for r in db.get_results_to_check(7, 10)] == ["Onbekend"]
    # Uitgeschakeld -> niets
    assert scheduler.run_expiry_check(_settings(expiry_check_days=0)) == (0, 0)


def test_check_ad_alive_status_codes(monkeypatch, real_fetch_market_results):
    class Resp:
        def __init__(self, code):
            self.status_code = code

    class FakeHTTP:
        def __init__(self, code):
            self.code = code

        def get(self, url, timeout=8, allow_redirects=True):
            if isinstance(self.code, Exception):
                raise self.code
            return Resp(self.code)

    for code, expected in ((200, True), (410, False), (404, False), (500, None), (ConnectionError("x"), None)):
        monkeypatch.setattr(marketplace, "HTTP", FakeHTTP(code))
        assert marketplace.check_ad_alive("https://example.com/ad") is expected


def test_telegram_retries_on_rate_limit(monkeypatch, real_telegram_post):
    class Resp:
        def __init__(self, code, retry_after=None):
            self.status_code, self.ok, self.text = code, code == 200, f"HTTP {code}"
            self._retry = retry_after

        def json(self):
            return {"parameters": {"retry_after": self._retry}} if self._retry else {}

    calls, sleeps = [], []

    class FakeHTTP:
        def __init__(self, responses):
            self.responses = list(responses)

        def post(self, url, json=None, timeout=10):
            calls.append(url)
            return self.responses.pop(0)

    monkeypatch.setattr(notify.time_module, "sleep", lambda s: sleeps.append(s))

    # 429 met retry_after 2 -> wachten en opnieuw -> 200
    monkeypatch.setattr(notify, "HTTP", FakeHTTP([Resp(429, retry_after=2), Resp(200)]))
    assert real_telegram_post("sendMessage", {"text": "hi"}, {"telegram_bot_id": "b", "telegram_chat_id": "c"}) is True
    assert len(calls) == 2 and sleeps == [2]

    # Blijvend 429 -> na 3 pogingen opgeven
    calls.clear()
    sleeps.clear()
    monkeypatch.setattr(notify, "HTTP", FakeHTTP([Resp(429, retry_after=1)] * 3))
    assert real_telegram_post("sendMessage", {"text": "hi"}, {"telegram_bot_id": "b", "telegram_chat_id": "c"}) is False
    assert len(calls) == 3 and sleeps == [1, 1]

    # Andere fout -> direct opgeven, geen retry
    calls.clear()
    monkeypatch.setattr(notify, "HTTP", FakeHTTP([Resp(400)]))
    assert real_telegram_post("sendMessage", {"text": "hi"}, {"telegram_bot_id": "b", "telegram_chat_id": "c"}) is False
    assert len(calls) == 1


def test_item_to_ad_extracts_seller_url_attributes_description():
    item = {
        "itemId": "m9", "title": "Racefiets", "vipUrl": "/v/fietsen/m9",
        "priceInfo": {"priceCents": 100000, "priceType": "FIXED"},
        "sellerInformation": {"sellerName": "Thomas van Dijk", "sellerId": 123456},
        "attributes": [{"key": "condition", "value": "Zo goed als nieuw"}, {"key": "frameSize", "value": "58 cm"}, {"key": "x", "value": ""}],
        "description": "  Weinig   gereden,\n altijd binnen gestald. " + "x" * 400,
    }
    ad = marketplace._item_to_ad(item, "www.marktplaats.nl")
    assert ad["seller_url"] == "https://www.marktplaats.nl/u/thomas-van-dijk/123456/"
    assert ad["attributes"] == "Zo goed als nieuw · 58 cm"
    assert ad["description"].startswith("Weinig gereden, altijd binnen gestald.") and len(ad["description"]) <= 300
    caption = notify._ad_caption(ad, term="racefiets")
    assert "Kenmerken = Zo goed als nieuw · 58 cm" in caption and "Weinig gereden" in caption


# --- v25 ----------------------------------------------------------------------


def test_marketplace_override_per_keyword(monkeypatch, telegram_log, keyword):
    db.update_keyword_fields(keyword["id"], marketplace="2dehands")
    kw = db.get_keyword(keyword["id"])
    seen = {}

    def fake_fetch(term, settings, limit, category_id=None, category_parent_id=None):
        seen["marketplace"] = settings["marketplace"]
        return [_ad("o1", "Koersfiets", 1000)]
    monkeypatch.setattr(marketplace, "fetch_market_results", fake_fetch)

    sent_to = []
    monkeypatch.setattr(notify, "_telegram_post", lambda method, payload, settings, label="": sent_to.append(settings["telegram_chat_id"]) or True)
    scheduler.run_search_for_keyword(kw, _settings(marketplace="marktplaats", telegram_chat_id="123"))
    assert seen["marketplace"] == "2dehands"
    assert sent_to == ["123"]  # chat komt uit de globale configuratie

    merged = scheduler.keyword_settings({"marketplace": ""}, {"marketplace": "marktplaats", "telegram_chat_id": "123"})
    assert merged["marketplace"] == "marktplaats" and merged["telegram_chat_id"] == "123"


def test_telegram_sends_to_all_chat_ids(monkeypatch, real_telegram_post):
    class Resp:
        status_code, ok, text = 200, True, "ok"

    posted = []

    class FakeHTTP:
        def post(self, url, json=None, timeout=10):
            posted.append(json["chat_id"])
            return Resp()
    monkeypatch.setattr(notify, "HTTP", FakeHTTP())
    assert notify.parse_chat_ids(" 12, -34;56  78 ,12") == ["12", "-34", "56", "78"]
    assert real_telegram_post("sendMessage", {"text": "hi"}, {"telegram_bot_id": "b", "telegram_chat_id": "12, -34"}) is True
    assert posted == ["12", "-34"]
    assert real_telegram_post("sendMessage", {"text": "hi"}, {"telegram_bot_id": "b", "telegram_chat_id": ""}) is False


# --- v26: relevantiefilter in de zoekactie -----------------------------------

def test_irrelevant_fuzzy_results_are_dropped(monkeypatch, keyword):
    db.update_keyword_fields(keyword["id"], term="cardiff marathon")
    kw = db.get_keyword(keyword["id"])
    ads = [
        dict(_ad("f1", "Finish as one - Amsterdam Marathon - 50 years", 0), description="boek"),
        dict(_ad("f2", "Startbewijs halve marathon eindhoven", 5000), description="1 startnummer"),
        dict(_ad("f3", "Startbewijs Cardiff Half Marathon 2027", 8000), description=""),
        dict(_ad("f4", "Startbewijs marathon", 8000), description="Cardiff, 5 oktober, overdraagbaar"),
    ]
    monkeypatch.setattr(marketplace, "fetch_market_results", _fetch(ads))
    assert scheduler.run_search_for_keyword(kw, _settings(match_mode="text")) == (2, 2)
    assert sorted(r["title"] for r in db.get_results_for_keyword(kw["id"])) == ["Startbewijs Cardiff Half Marathon 2027", "Startbewijs marathon"]

    db.reset_results_for_keyword(kw["id"])
    assert scheduler.run_search_for_keyword(kw, _settings(match_mode="title"))[0] == 1
    db.reset_results_for_keyword(kw["id"])
    assert scheduler.run_search_for_keyword(kw, _settings(match_mode="off"))[0] == 4
