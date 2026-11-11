"""Constanten, paden, logger en default-instellingen van de zoekopdrachten."""
import logging
import os
from pathlib import Path

import store

# Meldingen gaan naar het log van MPManager (app.py hangt daar een handler aan).
logger = logging.getLogger("mpmanager.watcher")
logger.setLevel(logging.INFO)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = CONFIG_DIR / "watcher.db"
# De database van een losse MPWatcher-installatie: staat die in de config-map,
# dan wordt hij bij de eerste start overgenomen (zie db.import_mpwatcher).
MPWATCHER_DB_FILE = CONFIG_DIR / "results.db"

# Maximaal bewaarde resultaten per zoekwoord; oudere rijen worden opgeruimd.
MAX_RESULTS_PER_KEYWORD = 500

# Aantal resultaten per pagina op de resultatenpagina.
RESULTS_PAGE_SIZE = 100

# /health meldt unhealthy als de scheduler zo lang geen teken van leven gaf.
SCHEDULER_STALE_SECONDS = 300

# Pauze tussen twee zoekacties binnen één schedulerronde, zodat de requests
# naar Marktplaats gespreid worden in plaats van in één burst.
SEARCH_SPACING_SECONDS = 5

# De schrijftest in /health wordt zo lang gecachet (scheelt schrijfacties op de NAS).
CONFIG_CHECK_TTL_SECONDS = 300

# Bij een subcategorie-filter (dat de API niet zelf toepast) halen we meer
# advertenties op en filteren we lokaal; dit is de factor en het plafond.
CATEGORY_OVERSAMPLE = 3
CATEGORY_MAX_FETCH = 60

# De instellingen van de zoekopdrachten staan, met alle andere, in het ene
# schema van MPManager (store.SCHEMA). Dit zijn de namen die hier gebruikt worden.
SETTING_KEYS = (
    "marketplace", "default_interval_minutes", "default_limit_per_run", "match_mode",
    "repost_dedup_days", "expiry_check_days", "skip_reserved", "postcode", "radius_km",
    "sleep_mode", "sleep_start", "sleep_end", "price_drop_alerts", "telegram_digest_threshold",
    "manual_telegram", "blocked_sellers", "blocked_titles",
)
DEFAULT_SETTINGS = {key: store.DEFAULTS[key] for key in SETTING_KEYS}

# Controle op verlopen advertenties: hoe vaak, en hoeveel per ronde.
EXPIRY_CHECK_INTERVAL_HOURS = 24
EXPIRY_CHECK_BATCH = 60

# Maximale lengte van de opgeslagen omschrijving.
DESCRIPTION_MAX_CHARS = 300

# Na een mislukte zoekopdracht (blokkade, storing) wordt het interval van dat
# zoekwoord per mislukking verdubbeld tot dit maximum, om een tijdelijke
# blokkade niet te verlengen door te blijven hameren.
BACKOFF_MAX_HOURS = 6

# Aantal bewaarde regels in het meldingenlogboek.
NOTIFICATIONS_KEEP = 1000

# Browser-identificatie richting Marktplaats; instelbaar omdat een verouderde
# User-Agent op termijn geweigerd kan worden.
USER_AGENT = os.environ.get("USER_AGENT") or os.environ.get("MPWATCHER_USER_AGENT") or (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "nl-NL,nl;q=0.9,en;q=0.5"


class MarketplaceError(Exception):
    """De zoekopdracht bij Marktplaats/2dehands is mislukt (netwerk, blokkade, API-wijziging)."""
