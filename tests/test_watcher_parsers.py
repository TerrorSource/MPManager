from datetime import datetime, time, timedelta

from watcher import marketplace as mp
from watcher import scheduler, utils
from watcher.config import DEFAULT_SETTINGS


# --- prijs -------------------------------------------------------------------

def test_price_from_api_cents():
    assert mp.parse_price_to_cents({"priceCents": 125000}, "") == 125000


def test_price_display_variants():
    assert mp.parse_price_to_cents({}, "€ 12,50") == 1250
    assert mp.parse_price_to_cents({}, "€ 1.250,00") == 125000
    assert mp.parse_price_to_cents({}, "€ 1.250") == 125000  # 1250 euro, niet 1250 cent
    assert mp.parse_price_to_cents({}, "€ 9,5") == 950


def test_price_without_amount_is_none():
    assert mp.parse_price_to_cents({}, "Bieden") is None
    assert mp.parse_price_to_cents({"priceCents": 0, "priceType": "SEE_DESCRIPTION"}, "") is None


def test_format_price_by_type():
    assert mp.format_price({"priceCents": 15000, "priceType": "FIXED"}) == "€ 150,00"
    assert mp.format_price({"priceCents": 15000, "priceType": "MIN_BID"}) == "€ 150,00 (bieden)"
    assert mp.format_price({"priceCents": 0, "priceType": "SEE_DESCRIPTION"}) == "Zie omschrijving"
    assert mp.format_price({"priceCents": 0, "priceType": "FAST_BID"}) == "Bieden"
    assert mp.format_price({"priceCents": 0, "priceType": "FREE"}) == "Gratis"
    assert mp.format_price({"priceDisplay": "€ 5,00"}) == "€ 5,00"
    assert mp.format_price(None) == ""


def test_format_cents():
    assert utils.format_cents(19500) == "€ 195,00"
    assert utils.format_cents(950) == "€ 9,50"


# --- datum -------------------------------------------------------------------

def test_posted_at_iso_and_dutch():
    assert mp.parse_posted_at_to_dt("2026-01-05T12:30:00", None) == datetime(2026, 1, 5, 12, 30)
    assert mp.parse_posted_at_to_dt("5 mei 25", None) == datetime(2025, 5, 5)


def test_posted_at_relative_words():
    seen = "2026-09-13T10:00:00"
    assert mp.parse_posted_at_to_dt("Vandaag", seen) == datetime(2026, 9, 13)
    assert mp.parse_posted_at_to_dt("Gisteren", seen) == datetime(2026, 9, 12)
    assert mp.parse_posted_at_to_dt("Eergisteren", seen) == datetime(2026, 9, 11)


def test_posted_at_fallbacks():
    assert mp.parse_posted_at_to_dt("onzin", "2026-02-01T08:00:00") == datetime(2026, 2, 1, 8, 0)
    assert mp.parse_posted_at_to_dt(None, None) == datetime.min


def test_extract_posted_at():
    assert mp._extract_posted_at({"date": 1767225600000}).startswith("2026-01-01")
    assert mp._extract_posted_at({"date": "Vandaag"}) == "Vandaag"


# --- locatie / verkoper ------------------------------------------------------

def test_extract_location():
    assert mp._extract_location({"location": {"cityName": "Utrecht"}}) == "Utrecht"
    assert mp._extract_location({"location": "Zwolle"}) == "Zwolle"
    assert mp._extract_location({"locationName": "Amsterdam"}) == "Amsterdam"
    assert mp._extract_location({}) == ""


def test_extract_seller_from_api_item():
    assert mp._extract_seller_from_api_item({"sellerInformation": {"sellerName": "Max"}}) == "Max"
    assert mp._extract_seller_from_api_item({"seller": {"name": "Piet"}}) == "Piet"
    assert mp._extract_seller_from_api_item({}) == ""


def test_item_to_ad_with_real_api_shape():
    item = {
        "itemId": "m123", "title": "Startbewijs marathon", "vipUrl": "/v/tickets/m123",
        "priceInfo": {"priceCents": 0, "priceType": "SEE_DESCRIPTION"},
        "date": "Vandaag", "categoryId": 2002,
        "location": {"cityName": "Utrecht"},
        "sellerInformation": {"sellerName": "Max"},
        "pictures": [{"largeUrl": "//img.example.com/1.jpg"}],
    }
    ad = mp._item_to_ad(item, "www.marktplaats.nl")
    assert ad["url"] == "https://www.marktplaats.nl/v/tickets/m123"
    assert ad["price"] == "Zie omschrijving" and ad["price_cents"] is None
    assert ad["location"] == "Utrecht" and ad["seller"] == "Max"
    assert ad["image_url"] == "https://img.example.com/1.jpg"
    assert ad["category_id"] == 2002


# --- url ---------------------------------------------------------------------

def test_build_search_url_encodes_term():
    url = mp.build_search_url("lego & duplo #5", dict(DEFAULT_SETTINGS))
    assert "/q/lego+%26+duplo+%235/" in url


def test_build_search_url_radius_postcode():
    url = mp.build_search_url("fiets", dict(DEFAULT_SETTINGS, postcode="1234AB", radius_km="10"))
    assert "distanceMeters:10000" in url and "postcode:1234AB" in url


# --- slaapstand / timing -----------------------------------------------------

def test_sleep_window():
    assert scheduler.is_in_sleep_window(time(23, 30), time(23, 0), time(7, 0))
    assert scheduler.is_in_sleep_window(time(3, 0), time(23, 0), time(7, 0))
    assert not scheduler.is_in_sleep_window(time(12, 0), time(23, 0), time(7, 0))
    assert scheduler.is_in_sleep_window(time(14, 0), time(13, 0), time(15, 0))


def test_compute_next_run_and_format():
    now = datetime(2026, 9, 13, 12, 0, 0)
    settings = dict(DEFAULT_SETTINGS, default_interval_minutes=15)
    kw = {"interval_minutes": 15, "last_run_at": (now - timedelta(minutes=5)).isoformat()}
    assert scheduler.compute_next_run(kw, settings, now) == now + timedelta(minutes=10)
    assert scheduler.format_next_run(now + timedelta(minutes=10), now) == "over 10 min"
    assert scheduler.format_next_run(now - timedelta(minutes=1), now) == "nu"
    assert scheduler.format_next_run(None, now) == "wacht op eerste run"
    night = datetime(2026, 9, 13, 2, 0, 0)
    sleepy = dict(settings, sleep_mode=True, sleep_start="23:00", sleep_end="07:00")
    assert scheduler.compute_next_run({"interval_minutes": 15, "last_run_at": night.isoformat()}, sleepy, night) == night + timedelta(hours=1)


# --- utils -------------------------------------------------------------------

def test_form_bool_and_int_or_none():
    assert utils.form_bool(" JA ") is True and utils.form_bool("nee") is False and utils.form_bool(None) is False
    assert utils.int_or_none("5") == 5 and utils.int_or_none("") is None and utils.int_or_none("abc") is None


def test_title_filters():
    assert utils.title_passes_filters("Gezocht: startbewijs marathon", None, "gezocht, gevraagd") is False
    assert utils.title_passes_filters("Startbewijs marathon Berlijn", None, "gezocht") is True
    assert utils.title_passes_filters("Marathon Berlijn", "startbewijs, ticket", None) is False
    assert utils.title_passes_filters("Ticket marathon Berlijn", "startbewijs, ticket", None) is True
    assert utils.parse_terms(" Gezocht ,, gevraagd, gezocht ") == ["gezocht", "gevraagd"]
    assert utils.normalize_terms("A, b ,A") == "a, b"


def test_normalize_title_and_clean_lines():
    assert utils.normalize_title("  Viking  Klapschaatsen\tMaat 41 ") == "viking klapschaatsen maat 41"
    assert utils.clean_lines("Gert\n\n gert \nArnaud\n") == ["Gert", "Arnaud"]


def test_format_relative_time():
    now = datetime.now()
    assert utils.format_relative_time("Nooit") == "Nooit"
    assert utils.format_relative_time(now.isoformat(timespec="seconds")) == "zojuist"
    assert utils.format_relative_time((now - timedelta(minutes=5)).isoformat(timespec="seconds")) == "5 min geleden"
    assert utils.format_relative_time((now - timedelta(hours=3)).isoformat(timespec="seconds")) == "3 uur geleden"
    assert utils.format_relative_time((now - timedelta(days=1, hours=1)).isoformat(timespec="seconds")) == "gisteren"
    assert utils.format_relative_time("onzin") == "onzin"


# --- v22 ---------------------------------------------------------------------

def test_extract_distance_and_reserved():
    item = {"location": {"cityName": "Utrecht", "distanceMeters": 12345}, "reserved": True,
            "itemId": "d1", "title": "x", "vipUrl": "/d1", "priceInfo": {"priceCents": 100, "priceType": "FIXED"}}
    ad = mp._item_to_ad(item, "www.marktplaats.nl")
    assert ad["distance_km"] == 12.3 and ad["reserved"] is True
    assert mp._extract_distance_km({"location": {"distanceMeters": -1000}}) is None
    assert mp._extract_distance_km({}) is None


def test_fetch_categories_parses_facets(monkeypatch):
    monkeypatch.setattr(mp, "_api_get", lambda domain, params: {"facets": [
        {"key": "PriceCents", "type": "AttributeRangeFacet"},
        {"key": "RelevantCategories", "type": "CategoryTreeFacet", "categories": [
            {"id": 1984, "label": "Tickets en Kaartjes", "parentId": None},
            {"id": 2002, "label": "Sport | Overige", "parentId": 1984, "histogramCount": 339},
            {"id": 1448, "label": "Evenementen en Festivals", "parentId": 1984, "histogramCount": 289},
            {"id": 784, "label": "Sport en Fitness", "parentId": None},
            {"id": 806, "label": "Schaatsen", "parentId": 784, "histogramCount": 12},
        ]},
    ]})
    cats = mp.fetch_categories("marathon", dict(DEFAULT_SETTINGS))
    assert [c["id"] for c in cats] == [1984, 2002, 1448, 784, 806]  # hoofdcategorie, dan subs op aantal
    assert cats[1]["parent_label"] == "Tickets en Kaartjes" and cats[1]["count"] == 339
    assert cats[0]["parent_id"] is None


def test_backoff_factor():
    assert [scheduler.backoff_factor(n) for n in (0, 1, 2, 3)] == [1, 2, 4, 8]
    assert scheduler.backoff_factor(20) == 256  # begrensd; het plafond in uren zit in effective_interval


# --- v26: relevantiefilter ---------------------------------------------------

def test_clean_search_term_and_words():
    assert utils.clean_search_term('  "feyenoord   marathon" ') == "feyenoord marathon"
    assert utils.term_words("Cardiff-Marathon 2027!") == ["cardiff", "marathon", "2027"]
    assert utils.term_words("a b") == []  # woorden korter dan 2 tekens tellen niet


def test_ad_matches_term_modes():
    m = utils.ad_matches_term
    assert m("cardiff marathon", "Finish as one - Amsterdam Marathon", "boek", "text") is False
    assert m("cardiff marathon", "Startbewijs Cardiff Half Marathon", "", "text") is True
    assert m("cardiff marathon", "Startbewijs marathon", "loopt in Cardiff op 5 oktober", "text") is True
    assert m("cardiff marathon", "Startbewijs marathon", "loopt in Cardiff", "title") is False
    assert m('"feyenoord marathon"', "Feyenoord short 1999 XXL", "voetbalshort", "text") is False
    assert m("feyenoord marathon", "FEYENOORD short", "loop mee met de Marathon", "off") is True
    assert m("", "iets", "", "text") is True
