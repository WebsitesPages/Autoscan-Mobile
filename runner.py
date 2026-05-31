"""runner.py - 10-Min-Loop für Mobile-Scraper"""
import os
import sys
import time
import json
import signal
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv('/opt/mobile-test/.env')

from mobile_scrape import sync_srp, init_db
from mobile_fetcher import get_session

# Push-Notify Funktion aus mobile_app importieren (wir laden lazy)
def _notify(ids):
    if not ids: return 0
    try:
        import sys
        if '/opt/mobile-test' not in sys.path:
            sys.path.insert(0, '/opt/mobile-test')
        from mobile_app import notify_new_listings
        return notify_new_listings(list(ids))
    except Exception as e:
        log.warning(f"Push-Notify fehlgeschlagen: {e}")
        return 0

INTERVAL = int(os.environ.get("MOBILE_INTERVAL", "600"))

# Pause-Zeit in Berlin-Lokalzeit (24h-Format)
PAUSE_FROM_HOUR = int(os.environ.get("MOBILE_PAUSE_FROM", "2"))
PAUSE_TO_HOUR   = int(os.environ.get("MOBILE_PAUSE_TO",   "9"))

def is_pause_time():
    try:
        from zoneinfo import ZoneInfo
        now_berlin = datetime.now(ZoneInfo("Europe/Berlin"))
    except Exception:
        now_berlin = datetime.utcnow() + timedelta(hours=2)
    h = now_berlin.hour
    if PAUSE_FROM_HOUR < PAUSE_TO_HOUR:
        return PAUSE_FROM_HOUR <= h < PAUSE_TO_HOUR
    else:
        return h >= PAUSE_FROM_HOUR or h < PAUSE_TO_HOUR

def seconds_until_resume():
    try:
        from zoneinfo import ZoneInfo
        now_berlin = datetime.now(ZoneInfo("Europe/Berlin"))
    except Exception:
        now_berlin = datetime.utcnow() + timedelta(hours=2)
    target = now_berlin.replace(hour=PAUSE_TO_HOUR, minute=0, second=0, microsecond=0)
    if target <= now_berlin:
        target = target + timedelta(days=1)
    delta = (target - now_berlin).total_seconds()
    return max(60, int(delta))
MAX_DETAILS_PER_RUN = int(os.environ.get("MOBILE_MAX_DETAILS", "20"))
CONFIG_PATH = os.environ.get("MOBILE_FILTER_CONFIG", "/opt/mobile-test/filter_config.json")

logging.basicConfig(level=logging.INFO,
    format="[%(asctime)s] [runner/%(levelname)s] %(message)s")
log = logging.getLogger("runner")

_shutdown = False
def _sig(_n, _f):
    global _shutdown
    log.info("SIGTERM/SIGINT empfangen → shutdown")
    _shutdown = True
signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)

def load_searches():
    if not os.path.exists(CONFIG_PATH):
        log.warning(f"keine {CONFIG_PATH}, nutze Default")
        return [{"name": "default",
                 "url": ("https://suchen.mobile.de/fahrzeuge/search.html?"
                         "cn=DE&dam=false&fr=2012%3A&gn=Bayern&isSearchRequest=true"
                         "&ml=%3A100000&od=down&p=%3A9000&rd=100&ref=srpHead"
                         "&s=Car&sb=doc&st=FSBO&vc=Car")}]
    with open(CONFIG_PATH) as f:
        data = json.load(f)
    searches = data.get("searches") or []
    if not searches:
        raise ValueError(f"{CONFIG_PATH}: 'searches' leer")
    return searches

def run_once():
    searches = load_searches()
    log.info(f"Run start: {len(searches)} Suchen")
    total_new, total_details = 0, 0
    for s in searches:
        if _shutdown: break
        name = s.get("name", "unnamed")
        log.info(f"  → {name}")
        try:
            # NEW IDs vor dem sync_srp ermitteln
            import sqlite3
            conn = sqlite3.connect("/opt/mobile-test/mobile.db")
            existing_before = set(r[0] for r in conn.execute("SELECT id FROM mobile_listings"))
            conn.close()

            result = sync_srp(s["url"], fetch_details=True,
                              max_details=MAX_DETAILS_PER_RUN, reuse_session=True)
            log.info(f"    {result}")
            total_new += result.get("new_count", 0)
            total_details += result.get("details_fetched", 0)

            # NEUE IDs ermitteln und Push-Notifications schicken
            if result.get("new_count", 0) > 0:
                conn = sqlite3.connect("/opt/mobile-test/mobile.db")
                existing_after = set(r[0] for r in conn.execute("SELECT id FROM mobile_listings"))
                conn.close()
                new_ids = list(existing_after - existing_before)
                if new_ids:
                    sent = _notify(new_ids)
                    log.info(f"    📲 Push: {sent} Notifications verschickt für {len(new_ids)} neue Listings")
        except Exception as e:
            log.error(f"    Fehler bei '{name}': {e}", exc_info=True)
    log.info(f"Run done: {total_new} neue, {total_details} Details")

def main():
    init_db()
    log.info(f"Runner gestartet, intervall={INTERVAL}s, Pause {PAUSE_FROM_HOUR}-{PAUSE_TO_HOUR} Uhr Berlin")
    while not _shutdown:
        # Nacht-Pause: spart Tokens & Proxy-Traffic
        if is_pause_time():
            wait = seconds_until_resume()
            wait_h = wait // 3600
            wait_m = (wait % 3600) // 60
            log.info(f"💤 Nacht-Pause aktiv ({PAUSE_FROM_HOUR}-{PAUSE_TO_HOUR} Uhr Berlin) — Schlaf bis {PAUSE_TO_HOUR}:00 Uhr (~{wait_h}h {wait_m}m)")
            slept = 0
            while slept < wait and not _shutdown:
                time.sleep(min(30, wait - slept))
                slept += 30
            if _shutdown: break
            log.info("☀️  Pause beendet, Runner aktiv")
            continue

        started = time.time()
        try: run_once()
        except Exception as e: log.exception(f"Run gescheitert: {e}")
        if _shutdown: break
        elapsed = time.time() - started
        sleep_for = max(5, INTERVAL - elapsed)
        log.info(f"Sleep {sleep_for:.0f}s...")
        slept = 0
        while slept < sleep_for and not _shutdown:
            time.sleep(min(2, sleep_for - slept))
            slept += 2
    log.info("Runner gestoppt")

if __name__ == "__main__":
    main()
