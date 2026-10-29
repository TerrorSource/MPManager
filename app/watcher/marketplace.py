"""API-client voor Marktplaats/2dehands en het parsen van advertenties."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote_plus

import requests

from .config import (
    ACCEPT_LANGUAGE,
    CATEGORY_MAX_FETCH,
    CATEGORY_OVERSAMPLE,
    DESCRIPTION_MAX_CHARS,
    USER_AGENT,
    MarketplaceError,
    logger,
)

# Eén sessie voor alle uitgaande HTTP-verkeer: hergebruikt TCP/TLS-verbindingen.
HTTP = requests.Session()
HTTP.headers["User-Agent"] = USER_AGENT
HTTP.headers["Accept-Language"] = ACCEPT_LANGUAGE


# ------------------------------------------------------------------------------
# URL's
# ------------------------------------------------------------------------------

def get_domain(settings: dict) -> str:
    return "www.2dehands.be" if (settings.get("marketplace") == "2dehands") else "www.marktplaats.nl"


def build_search_url(term: str, settings: dict) -> str:
    domain = get_domain(settings)
    postcode = (settings.get("postcode") or "").strip()
    radius_km = (settings.get("radius_km") or "alle").strip()

    query = quote_plus(term.strip())
    base = f"https://{domain}/q/{query}/#offeredSince:Altijd|sortBy:SORT_INDEX|sortOrder:DECREASING"

    if radius_km and radius_km != "alle":
        try:
            meters = int(radius_km) * 1000
            base += f"|distanceMeters:{meters}"
        except ValueError:
            pass

    if postcode:
        base += f"|postcode:{postcode}"

    return base


# ------------------------------------------------------------------------------
# Datum
# ------------------------------------------------------------------------------

def _format_epoch_to_str(v) -> str:
    try:
        ts = float(v)
    except Exception:
        return ""
    if ts > 10**12:
        ts = ts / 1000.0
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _extract_posted_at(item: dict) -> str:
    for key in ("date", "dateTime", "startTime", "startDateTime", "startDate", "postedAt"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            s = _format_epoch_to_str(v)
            if s:
                return s
        if isinstance(v, dict):
            for sub in ("value", "date", "iso", "iso8601"):
                sv = v.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return sv.strip()
                if isinstance(sv, (int, float)):
                    s = _format_epoch_to_str(sv)
                    if s:
                        return s

    for nk in ("dateInfo", "metadata", "timing"):
        nv = item.get(nk)
        if isinstance(nv, dict):
            for sub in ("date", "dateTime", "start", "value"):
                sv = nv.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return sv.strip()
                if isinstance(sv, (int, float)):
                    s = _format_epoch_to_str(sv)
                    if s:
                        return s
    return ""


_RELATIVE_DAYS = {"vandaag": 0, "gisteren": 1, "eergisteren": 2}

_MONTHS = {
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}


def parse_posted_at_to_dt(posted_at: str | None, fallback_first_seen: str | None) -> datetime:
    """Marktplaats geeft 'Vandaag', 'Gisteren', '5 mei 25' of een ISO-datum."""
    if posted_at:
        s = posted_at.strip()
        if s:
            for sep in [",", "|"]:
                if sep in s:
                    s = s.split(sep, 1)[0].strip()

            rel = _RELATIVE_DAYS.get(s.lower())
            if rel is not None:
                base = datetime.now()
                if fallback_first_seen:
                    try:
                        base = datetime.fromisoformat(fallback_first_seen)
                    except Exception:
                        pass
                day = (base - timedelta(days=rel)).date()
                return datetime(day.year, day.month, day.day)

            try:
                return datetime.fromisoformat(s)
            except Exception:
                pass

            try:
                parts = s.replace(".", "").split()
                if len(parts) >= 3:
                    day = int(parts[0])
                    month = _MONTHS.get(parts[1].lower())
                    year = int(parts[2])
                    if year < 100:
                        year += 2000
                    if month:
                        return datetime(year, month, day)
            except Exception:
                pass

    if fallback_first_seen:
        try:
            return datetime.fromisoformat(fallback_first_seen)
        except Exception:
            pass

    return datetime.min


# ------------------------------------------------------------------------------
# Prijs
# ------------------------------------------------------------------------------

_PRICE_TYPE_LABELS = {
    "FAST_BID": "Bieden",
    "SEE_DESCRIPTION": "Zie omschrijving",
    "FREE": "Gratis",
    "RESERVED": "Gereserveerd",
    "ON_REQUEST": "Op aanvraag",
    "NOTK": "N.o.t.k.",
    "EXCHANGE": "Ruilen",
}


def parse_price_to_cents(price_info, price_display: str) -> Optional[int]:
    """Prijs in centen, bij voorkeur direct uit de API (priceCents).
    Fallback: weergavestring zoals '€ 1.250' of '€ 12,50' correct parsen.
    'Bieden'/'Gratis' e.d. leveren None op."""
    if isinstance(price_info, dict):
        cents = price_info.get("priceCents")
        if isinstance(cents, (int, float)) and cents > 0:
            return int(cents)

    if price_display:
        m = re.search(r"(\d[\d\.]*)(?:,(\d{1,2}))?", price_display)
        if m:
            try:
                euros = int(m.group(1).replace(".", ""))
                cents = int((m.group(2) or "0").ljust(2, "0"))
                return euros * 100 + cents
            except ValueError:
                pass

    return None


def format_price(price_info) -> str:
    """Weergaveprijs uit priceInfo: 'FIXED' -> '€ 150,00', 'MIN_BID' ->
    '€ 150,00 (bieden)', en zonder bedrag het type-label ('Bieden', 'Gratis'...)."""
    if not isinstance(price_info, dict):
        return ""
    display = price_info.get("priceDisplay")
    if isinstance(display, str) and display.strip():
        return display.strip()

    cents = price_info.get("priceCents")
    ptype = str(price_info.get("priceType") or "").upper()
    if isinstance(cents, (int, float)) and cents > 0:
        text = f"€ {cents/100:.2f}".replace(".", ",")
        return f"{text} (bieden)" if ptype == "MIN_BID" else text
    return _PRICE_TYPE_LABELS.get(ptype, "")


# ------------------------------------------------------------------------------
# Locatie & verkoper
# ------------------------------------------------------------------------------

def _extract_location(item: dict) -> str:
    """Woonplaats van de verkoper uit het API-item (best effort)."""
    loc = item.get("location")
    if isinstance(loc, str) and loc.strip():
        return loc.strip()
    if isinstance(loc, dict):
        for key in ("cityName", "city", "name", "locationName", "abbreviatedCity"):
            v = loc.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    for key in ("locationName", "cityName", "city"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _extract_distance_km(item: dict) -> Optional[float]:
    """Afstand tot de ingestelde postcode (alleen aanwezig als die is ingesteld;
    de API geeft -1000 als onbekend)."""
    loc = item.get("location")
    if not isinstance(loc, dict):
        return None
    meters = loc.get("distanceMeters")
    if isinstance(meters, (int, float)) and meters >= 0:
        return round(meters / 1000, 1)
    return None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "verkoper"


def _extract_seller_url(item: dict, seller_name: str, domain: str) -> str:
    """Profielpagina van de verkoper: /u/<naam>/<id>/ (de naam corrigeert de
    marketplace zelf; het id is bepalend)."""
    info = item.get("sellerInformation")
    if not isinstance(info, dict):
        return ""
    sid = info.get("sellerId")
    if not isinstance(sid, int) or sid <= 0:
        return ""
    return f"https://{domain}/u/{_slugify(seller_name)}/{sid}/"


def _extract_attributes(item: dict, max_items: int = 4) -> str:
    """Kenmerken zoals conditie en maat: 'Zo goed als nieuw · Maat 58' (best effort)."""
    attrs = item.get("attributes")
    if not isinstance(attrs, list):
        return ""
    values: list[str] = []
    for a in attrs:
        if not isinstance(a, dict):
            continue
        v = a.get("value")
        if isinstance(v, str) and v.strip() and v.strip() not in values:
            values.append(v.strip())
        if len(values) >= max_items:
            break
    return " · ".join(values)


def _extract_description(item: dict) -> str:
    text = item.get("description")
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > DESCRIPTION_MAX_CHARS:
        text = text[: DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"
    return text


def _extract_seller_from_api_item(item: dict) -> str:
    direct_keys = (
        "sellerName", "seller", "vendor", "userName", "username",
        "advertiserName", "advertiser"
    )
    for k in direct_keys:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for sub in ("name", "displayName", "username", "userName", "sellerName"):
                sv = v.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return sv.strip()

    nested_candidates = (
        "sellerInformation", "contactInformation", "user", "account",
        "sellerInfo", "contact", "advertiser", "profile"
    )
    for nk in nested_candidates:
        nv = item.get(nk)
        if isinstance(nv, dict):
            for sub in ("name", "displayName", "username", "userName", "sellerName"):
                sv = nv.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return sv.strip()
    return ""


def _extract_seller_from_html(html: str) -> str:
    # 1) JSON-LD blocks
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE):
        blob = m.group(1).strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            for key in ("seller", "author"):
                who = obj.get(key)
                if isinstance(who, dict):
                    nm = who.get("name")
                    if isinstance(nm, str) and nm.strip():
                        return nm.strip()

    # 2) Common preloaded state / inline JSON patterns
    patterns = [
        r'"sellerName"\s*:\s*"([^"]+)"',
        r'"displayName"\s*:\s*"([^"]+)"',
        r'"userName"\s*:\s*"([^"]+)"',
        r'"username"\s*:\s*"([^"]+)"',
        r'"advertiserName"\s*:\s*"([^"]+)"',
        r'"seller"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"',
        r'"account"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"',
    ]
    for pat in patterns:
        mm = re.search(pat, html, re.IGNORECASE)
        if mm and mm.group(1).strip():
            return mm.group(1).strip()

    return ""


def fetch_seller_from_ad_page(url: str) -> str:
    if not url:
        return ""
    try:
        resp = HTTP.get(url, timeout=8)
        resp.raise_for_status()
        return _extract_seller_from_html(resp.text)
    except Exception as exc:
        logger.warning("Seller ophalen mislukt voor %s: %s", url, exc)
        return ""


def enrich_ads_with_seller(ads: list[dict]) -> list[dict]:
    """Vul seller aan voor ads waar die leeg is (HTML fallback).
    Detailpagina's worden parallel opgehaald zodat een run niet
    minutenlang blokkeert bij veel advertenties."""
    todo = [
        ad for ad in ads
        if not (ad.get("seller") or "").strip() and (ad.get("url") or "").strip()
    ]
    if not todo:
        return ads

    with ThreadPoolExecutor(max_workers=4) as pool:
        sellers = pool.map(lambda ad: fetch_seller_from_ad_page(ad["url"].strip()), todo)
        for ad, seller in zip(todo, sellers, strict=True):
            if seller:
                ad["seller"] = seller
    return ads


# ------------------------------------------------------------------------------
# Zoeken
# ------------------------------------------------------------------------------

def _search_params(term: str, settings: dict, limit: int) -> dict:
    postcode = (settings.get("postcode") or "").strip() or None
    radius_km = (settings.get("radius_km") or "alle").strip()

    params = {
        "query": term,
        "sortBy": "SORT_INDEX",
        "sortOrder": "DECREASING",
        "viewOptions": "list-view",
        "limit": limit,
        "offset": 0,
    }
    if postcode:
        params["postcode"] = postcode
    if radius_km and radius_km != "alle":
        try:
            params["distanceMeters"] = int(radius_km) * 1000
        except ValueError:
            pass
    return params


def _api_get(domain: str, params: dict) -> dict:
    api_url = f"https://{domain}/lrp/api/search"
    try:
        resp = HTTP.get(api_url, params=params, timeout=15)
    except Exception as exc:
        raise MarketplaceError(f"zoekopdracht bij {domain} mislukt: {exc}") from exc
    if resp.status_code in (403, 429):
        raise MarketplaceError(
            f"{domain} weigert de zoekopdracht (HTTP {resp.status_code}); mogelijk geblokkeerd"
        )
    try:
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise MarketplaceError(f"zoekopdracht bij {domain} mislukt: {exc}") from exc


def _find_listings(obj):
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and ("itemId" in obj[0] or "id" in obj[0]) and "title" in obj[0]:
            return obj
        for item in obj:
            res = _find_listings(item)
            if res is not None:
                return res
    elif isinstance(obj, dict):
        listings = obj.get("listings")
        if isinstance(listings, list):
            return listings
        for v in obj.values():
            res = _find_listings(v)
            if res is not None:
                return res
    return None


def _item_to_ad(item: dict, domain: str) -> Optional[dict]:
    ad_id = str(item.get("itemId") or item.get("id") or "")
    url_path = item.get("url") or item.get("vipUrl") or item.get("relativeUrl")
    url = ""
    if url_path:
        url = str(url_path) if str(url_path).startswith("http") else f"https://{domain}{url_path}"
    if not ad_id or not url:
        return None

    price_info = item.get("priceInfo") or {}
    price = format_price(price_info)

    image_url = ""
    media = item.get("media") or {}
    if isinstance(media, dict):
        imgs = media.get("images")
        if isinstance(imgs, list) and imgs:
            image_url = imgs[0].get("url") or ""
    elif isinstance(media, list) and media:
        image_url = media[0].get("url") or ""
    if not image_url:
        for key in ("imageUrls", "pictures"):
            imgs = item.get(key)
            if isinstance(imgs, list) and imgs:
                first = imgs[0]
                if isinstance(first, str):
                    image_url = first
                elif isinstance(first, dict):
                    image_url = first.get("extraExtraLargeUrl") or first.get("largeUrl") or first.get("url") or ""
                break
    if image_url.startswith("//"):
        image_url = "https:" + image_url

    seller = _extract_seller_from_api_item(item)
    return {
        "ad_id": ad_id,
        "title": item.get("title") or "",
        "price": price,
        "price_cents": parse_price_to_cents(price_info, price),
        "url": url,
        "image_url": image_url,
        "posted_at": _extract_posted_at(item),
        "seller": seller,
        "seller_url": _extract_seller_url(item, seller, domain),
        "location": _extract_location(item),
        "distance_km": _extract_distance_km(item),
        "reserved": bool(item.get("reserved")),
        "attributes": _extract_attributes(item),
        "description": _extract_description(item),
        "category_id": item.get("categoryId"),
    }


def check_ad_alive(url: str) -> Optional[bool]:
    """Bestaat de advertentie nog? True = ja, False = verwijderd (HTTP 404/410),
    None = onbekend (netwerkfout, blokkade) — dan niets concluderen."""
    if not url:
        return None
    try:
        resp = HTTP.get(url, timeout=8, allow_redirects=True)
    except Exception as exc:
        logger.debug("Controle op verlopen mislukt voor %s: %s", url, exc)
        return None
    if resp.status_code in (404, 410):
        return False
    if resp.status_code == 200:
        return True
    return None


def fetch_market_results(
    term: str, settings: dict, limit: int,
    category_id: Optional[int] = None, category_parent_id: Optional[int] = None,
) -> list[dict]:
    """De nieuwste `limit` advertenties voor `term`. Gooit MarketplaceError bij fouten.

    Categorie: de API filtert alleen op hoofdcategorie (l1CategoryId). Een
    subcategorie passen we zelf toe op `categoryId` van de advertenties; daarvoor
    halen we wat meer op zodat er na filteren genoeg overblijft."""
    domain = get_domain(settings)
    l1 = category_parent_id or category_id
    l2 = category_id if category_parent_id else None

    fetch_limit = limit
    if l2:
        fetch_limit = min(limit * CATEGORY_OVERSAMPLE, CATEGORY_MAX_FETCH)

    params = _search_params(term, settings, fetch_limit)
    if l1:
        params["l1CategoryId"] = l1
    if l2:
        params["l2CategoryId"] = l2

    data = _api_get(domain, params)
    listings = _find_listings(data) or []

    results: list[dict] = []
    for item in listings:
        if not isinstance(item, dict):
            continue
        if l2 and item.get("categoryId") not in (None, l2):
            continue
        ad = _item_to_ad(item, domain)
        if ad:
            results.append(ad)
        if len(results) >= limit:
            break
    return results


def fetch_categories(term: str, settings: dict) -> list[dict]:
    """Relevante categorieën voor een zoekterm (uit de facetten van de API):
    [{id, label, parent_id, parent_label, count}], hoofdcategorieën eerst."""
    domain = get_domain(settings)
    data = _api_get(domain, _search_params(term, settings, 1))

    raw: list[dict] = []
    for facet in data.get("facets") or []:
        if isinstance(facet, dict) and facet.get("key") == "RelevantCategories":
            raw = [c for c in (facet.get("categories") or []) if isinstance(c, dict)]
            break

    by_id = {c.get("id"): c for c in raw}
    cats: list[dict] = []
    for c in raw:
        cid = c.get("id")
        if not isinstance(cid, int):
            continue
        parent_id = c.get("parentId")
        parent = by_id.get(parent_id) if parent_id else None
        cats.append({
            "id": cid,
            "label": c.get("label") or str(cid),
            "parent_id": parent_id if isinstance(parent_id, int) else None,
            "parent_label": (parent or {}).get("label") or "",
            "count": c.get("histogramCount") or 0,
        })

    # Sorteer: hoofdcategorie, daarna de subcategorieën ervan op aantal
    order: list[dict] = []
    for top in [c for c in cats if not c["parent_id"]]:
        order.append(top)
        subs = [c for c in cats if c["parent_id"] == top["id"]]
        order.extend(sorted(subs, key=lambda c: -c["count"]))
    orphans = [c for c in cats if c["parent_id"] and c["parent_id"] not in by_id]
    order.extend(orphans)
    return order
