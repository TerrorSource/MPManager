"""Tests draaien tegen een wegwerp-configmap, zonder netwerk en zonder achtergrondthreads."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="mpmanager-test-")
os.environ["BACKUP_DIR"] = tempfile.mkdtemp(prefix="mpmanager-backups-")
os.environ["KEY_PATH"] = os.path.join(tempfile.mkdtemp(prefix="mpmanager-keys-"), "secret.key")
os.environ["MPWATCHER_DISABLE_WORKER"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from watcher import db as _db  # noqa: E402
from watcher import marketplace as _marketplace  # noqa: E402
from watcher import notify as _notify  # noqa: E402

_db.init_db()

# De echte functies, voor tests die de client zelf willen testen (met gemockte HTTP).
_REAL_FETCH_MARKET_RESULTS = _marketplace.fetch_market_results
_REAL_TELEGRAM_POST = _notify._telegram_post


@pytest.fixture()
def real_fetch_market_results():
    return _REAL_FETCH_MARKET_RESULTS


@pytest.fixture()
def real_telegram_post():
    return _REAL_TELEGRAM_POST


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Geen echte calls naar Marktplaats/Telegram vanuit de tests."""
    from watcher import marketplace, notify

    monkeypatch.setattr(
        marketplace, "fetch_market_results",
        lambda term, settings, limit, category_id=None, category_parent_id=None: [],
    )
    monkeypatch.setattr(marketplace, "fetch_seller_from_ad_page", lambda url: "")
    monkeypatch.setattr(notify, "_telegram_post", lambda method, payload, settings, label="": True)
