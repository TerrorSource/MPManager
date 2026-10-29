"""Kleine hulpfuncties zonder afhankelijkheden op de rest van de app."""
import re
from datetime import datetime
from typing import Optional


def form_bool(v) -> bool:
    """Formulier-selects sturen 'ja'/'nee'; intern werken we met booleans."""
    return str(v or "").strip().lower() == "ja"


def int_or_none(v) -> Optional[int]:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_terms(raw: str | None) -> list[str]:
    """'Gezocht, gevraagd ,,' -> ['gezocht', 'gevraagd'] (voor titelfilters)."""
    if not raw:
        return []
    seen: list[str] = []
    for part in str(raw).split(","):
        t = part.strip().lower()
        if t and t not in seen:
            seen.append(t)
    return seen


def normalize_terms(raw: str | None) -> str:
    return ", ".join(parse_terms(raw))


def title_passes_filters(title: str, include_terms: str | None, exclude_terms: str | None) -> bool:
    """Uitsluitwoorden: één match in de titel is genoeg om de advertentie te
    negeren. Verplichte woorden: minstens één ervan moet in de titel staan."""
    t = (title or "").lower()
    for word in parse_terms(exclude_terms):
        if word in t:
            return False
    required = parse_terms(include_terms)
    if required and not any(word in t for word in required):
        return False
    return True


_WS = re.compile(r"\s+")


def normalize_title(title: str | None) -> str:
    """Voor titel-blocklist en herplaatsings-dedup: kleine letters, enkele spaties."""
    return _WS.sub(" ", (title or "").strip()).casefold()


def clean_lines(raw: str | None) -> list[str]:
    """Tekstvak met één item per regel -> lijst zonder lege regels en dubbelen."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x:
            continue
        key = normalize_title(x)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(x)
    return cleaned


def format_cents(cents: int) -> str:
    return f"€ {cents/100:.2f}".replace(".", ",")


def format_relative_time(iso_str: str | None) -> str:
    """'2026-06-12T14:03:00' -> '5 min geleden' (voor het overzicht)."""
    if not iso_str or iso_str == "Nooit":
        return "Nooit"
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        return iso_str

    secs = int((datetime.now() - dt).total_seconds())
    if secs < 60:
        return "zojuist"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min geleden"
    hours = mins // 60
    if hours < 24:
        return f"{hours} uur geleden"
    days = hours // 24
    if days == 1:
        return "gisteren"
    if days < 7:
        return f"{days} dagen geleden"
    return dt.strftime("%d-%m-%Y")


_QUOTES = "\"'“”‘’«»"


def clean_search_term(term: str | None) -> str:
    """Aanhalingstekens weghalen (de marketplace doet er niets mee) en witruimte normaliseren."""
    t = (term or "").strip()
    t = t.translate({ord(c): " " for c in _QUOTES})
    return re.sub(r"\s+", " ", t).strip()


def term_words(term: str | None) -> list[str]:
    """Losse woorden van de zoekterm, zonder leestekens, in kleine letters."""
    words = re.findall(r"[0-9a-zà-ÿ]+", clean_search_term(term).casefold())
    return [w for w in words if len(w) >= 2]


def ad_matches_term(term: str | None, title: str | None, description: str | None, mode: str) -> bool:
    """Relevantiefilter tegen 'fuzzy' zoekresultaten: alle woorden van de
    zoekterm moeten voorkomen in de titel (mode 'title') of in titel +
    omschrijving (mode 'text'). Mode 'off' = alles accepteren."""
    if mode == "off":
        return True
    words = term_words(term)
    if not words:
        return True
    haystack = (title or "")
    if mode != "title":
        haystack += " " + (description or "")
    haystack = haystack.casefold()
    return all(w in haystack for w in words)


def attributes_pass_filter(attributes: str | None, attr_terms: str | None) -> bool:
    """Kenmerkfilter: minstens één van de opgegeven woorden moet in de
    kenmerken ('Zo goed als nieuw · 58 cm') voorkomen. Geen woorden = alles."""
    required = parse_terms(attr_terms)
    if not required:
        return True
    a = (attributes or "").lower()
    return any(word in a for word in required)
