"""Constanten, paden, logger en default-instellingen van de zoekopdrachten."""
import logging
import os
from pathlib import Path

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

# Maximaal aantal regels in een Telegram-samenvatting (digest).
DIGEST_MAX_ITEMS = 20

# Bij een subcategorie-filter (dat de API niet zelf toepast) halen we meer
# advertenties op en filteren we lokaal; dit is de factor en het plafond.
CATEGORY_OVERSAMPLE = 3
CATEGORY_MAX_FETCH = 60

DEFAULT_SETTINGS = {
    "marketplace": "marktplaats",  # marktplaats | 2dehands
    "default_interval_minutes": 15,
    "default_limit_per_run": 5,

    "sleep_mode": False,
    "sleep_start": "23:00",
    "sleep_end": "07:00",

    "postcode": "",
    "radius_km": "alle",

    "telegram_bot_id": "",
    "telegram_chat_id": "",
    "manual_telegram": False,
    "price_drop_alerts": True,
    # Vanaf dit aantal nieuwe advertenties in één run één samenvattend
    # bericht i.p.v. losse meldingen (0 = altijd losse meldingen).
    "telegram_digest_threshold": 5,

    # Herplaatsingen (zelfde titel/verkoper/prijs met nieuw id) binnen dit
    # aantal dagen niet opnieuw melden (0 = uit).
    "repost_dedup_days": 30,

    # Gereserveerde advertenties overslaan (meestal al verkocht)
    "skip_reserved": False,

    # Relevantiefilter: de marketplace zoekt 'fuzzy' ("cardiff marathon" geeft
    # alles met "marathon"). Modes:
    #   text  = alle woorden van de zoekterm moeten in titel + omschrijving staan
    #   title = alle woorden moeten in de titel staan (strenger)
    #   off   = alles wat de marketplace teruggeeft (oude gedrag)
    "match_mode": "text",

    # Advertenties van de afgelopen N dagen dagelijks controleren op
    # verwijderd/verlopen (0 = uit).
    "expiry_check_days": 7,

    # Blocklists (een lege lijst = uit)
    "blocked_sellers": [],
    "blocked_titles": [],
}

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
