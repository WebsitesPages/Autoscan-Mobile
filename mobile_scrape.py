"""
mobile.de Scraper via ScrapingBee
- SRP scrapen: extrahiert Listings (ID, Preis, KM, EZ, Leistung, Standort, Preisbewertung, Unfallfrei-Badge)
- Detail scrapen: extrahiert Ausstattung, Beschreibung, alle technischen Daten
- Gesponserte Inserate werden ignoriert
"""
import requests
import re
import json
import sys
import sqlite3
import os
from urllib.parse import urlencode, quote
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv('/opt/mobile-test/.env')

API_KEY = os.environ.get("SCRAPINGBEE_API_KEY")
if not API_KEY:
    raise RuntimeError("SCRAPINGBEE_API_KEY not set in environment. Copy .env.example to .env and fill in your key.")
DB_PATH = "/opt/mobile-test/mobile.db"


# ============================================================
# DB INIT
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS mobile_listings(
        id TEXT PRIMARY KEY,
        url TEXT,
        title TEXT,
        brand TEXT,
        model_text TEXT,
        price_eur INTEGER,
        price_rating TEXT,
        price_negotiable INTEGER DEFAULT 0,
        unfallfrei INTEGER DEFAULT 0,
        km INTEGER,
        first_reg TEXT,
        power_kw INTEGER,
        power_ps INTEGER,
        fuel TEXT,
        gearbox TEXT,
        previous_owners INTEGER,
        emission_class TEXT,
        category TEXT,
        hu_until TEXT,
        color TEXT,
        seller_type TEXT,
        location TEXT,
        postal_code TEXT,
        city TEXT,
        online_since TEXT,
        online_since_dt TEXT,
        image_url TEXT,
        image_urls_json TEXT,
        features_json TEXT,
        description TEXT,
        first_seen TEXT DEFAULT (datetime('now')),
        last_seen TEXT DEFAULT (datetime('now')),
        detail_scraped INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mobile_scrape_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scraped_at TEXT DEFAULT (datetime('now')),
        action TEXT,
        url TEXT,
        cost INTEGER,
        result TEXT
    )""")
    conn.commit()
    conn.close()


# ============================================================
# ScrapingBee Helper
# ============================================================
def fetch(url, render_js=True, premium=True, wait_ms=3000):
    params = {
        'api_key': API_KEY,
        'url': url,
        'country_code': 'de',
        'wait': str(wait_ms),
    }
    if render_js: params['render_js'] = 'true'
    if premium: params['premium_proxy'] = 'true'
    r = requests.get('https://app.scrapingbee.com/api/v1/', params=params, timeout=120)
    cost = int(r.headers.get('Spb-cost', 0) or 0)
    # UTF-8 Encoding erzwingen
    r.encoding = 'utf-8'
    return r.text, r.status_code, cost


def log_scrape(action, url, cost, result):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO mobile_scrape_log(action, url, cost, result) VALUES(?,?,?,?)",
                 (action, url[:200], cost, result[:500]))
    conn.commit()
    conn.close()


# ============================================================
# SRP Parser
# ============================================================
def parse_srp(html):
    """Parst SRP HTML, gibt Liste von Listings zurück (ohne Detailseite)."""
    soup = BeautifulSoup(html, 'lxml')
    listings = []

    # Alle article tags durchsuchen
    for art in soup.select('article'):
        # Sponsored skip
        if art.select_one('[data-testid="sponsored-badge"]'):
            continue

        link = art.select_one('a[href*="/fahrzeuge/details.html"], a[href*="/auto-inserat/"]')
        if not link:
            continue
        href = link.get('href', '')

        # ID extrahieren
        m = re.search(r'[?&]id=(\d+)', href) or re.search(r'/auto-inserat/[^/]+/(\d+)', href)
        if not m:
            continue
        lid = m.group(1)

        # URL absolut machen
        url = href if href.startswith('http') else 'https://suchen.mobile.de' + href

        # Preis
        price_el = art.select_one('[data-testid="price-label"]')
        price_text = price_el.get_text(strip=True) if price_el else ''
        price_num = re.sub(r'[^\d]', '', price_text)
        price_eur = int(price_num) if price_num and 100 <= int(price_num) <= 999999 else None

        # Preisbewertung (Sehr guter Preis, Ohne Bewertung, etc.)
        rating_el = art.select_one('[data-testid="main-price-label"] ._u77E')
        if not rating_el:
            rating_el = art.select_one('._u77E')
        price_rating = rating_el.get_text(strip=True) if rating_el else None

        # Verhandlungsbasis
        price_neg = 1 if art.select_one('[data-testid="price-negotiable"]') else 0

        # Marke/Titel
        brand_el = art.select_one('span.eO87w[title]')
        brand = brand_el.get('title', '').strip() if brand_el else ''
        title_el = art.select_one('span.dc_Br[title]')
        model_text = title_el.get('title', '').strip() if title_el else ''
        title = (brand + ' ' + model_text).strip()

        # Attribute Block: "Unfallfrei • EZ 04/2015 • 80.000 km • 103 kW (140 PS) • Benzin"
        attr_el = art.select_one('[data-testid="listing-details-attributes"]')
        attr_text = attr_el.get_text(' ', strip=True) if attr_el else ''
        unfallfrei = 1 if 'Unfallfrei' in attr_text else 0

        first_reg = None
        m_ez = re.search(r'EZ\s+(\d{2}/\d{4})', attr_text)
        if m_ez: first_reg = m_ez.group(1)

        km = None
        m_km = re.search(r'([\d.]+)\s*km', attr_text)
        if m_km:
            km = int(m_km.group(1).replace('.', '').replace(' ', ''))

        power_kw, power_ps = None, None
        m_pw = re.search(r'(\d+)\s*kW\s*\(?\s*(\d+)\s*PS', attr_text)
        if m_pw:
            power_kw, power_ps = int(m_pw.group(1)), int(m_pw.group(2))

        fuel = None
        for f in ['Benzin', 'Diesel', 'Hybrid', 'Elektro', 'Erdgas', 'Autogas']:
            if f in attr_text:
                fuel = f
                break

        # Standort
        loc_el = art.select_one('[data-testid="seller-info"] .Kh0Rn, [data-testid="seller-info"] div')
        loc_text = loc_el.get_text(' ', strip=True) if loc_el else ''
        seller_type = None
        if 'Privatanbieter' in loc_text: seller_type = 'private'
        elif 'Händler' in loc_text or 'gewerblich' in loc_text.lower(): seller_type = 'dealer'

        plz, city = None, None
        m_plz = re.match(r'(\d{5})\s+([^,]+)', loc_text)
        if m_plz:
            plz = m_plz.group(1)
            city = m_plz.group(2).strip()

        # Online seit
        online_el = art.select_one('[data-testid="online-since"]')
        online = online_el.get_text(' ', strip=True).replace('Inserat online seit', '').strip() if online_el else None
        online_dt = None
        if online:
            m = re.match(r'(\d{1,2})\.(\d{1,2})\.(\d{4}),?\s*(\d{1,2}):(\d{2})', online)
            if m:
                online_dt = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}T{int(m.group(4)):02d}:{m.group(5)}"

        # Bild
        img_el = art.select_one('img[data-testid^="result-listing-image-"]')
        image_url = img_el.get('src') if img_el else None

        listings.append({
            'id': lid,
            'url': url,
            'title': title,
            'brand': brand,
            'model_text': model_text,
            'price_eur': price_eur,
            'price_rating': price_rating,
            'price_negotiable': price_neg,
            'unfallfrei': unfallfrei,
            'km': km,
            'first_reg': first_reg,
            'power_kw': power_kw,
            'power_ps': power_ps,
            'fuel': fuel,
            'seller_type': seller_type,
            'location': loc_text,
            'postal_code': plz,
            'city': city,
            'online_since': online,
            'online_since_dt': online_dt,
            'image_url': image_url,
        })

    return listings


# ============================================================
# Detail Parser
# ============================================================
def parse_detail(html):
    """Parst Detailseite, gibt erweiterte Daten zurück."""
    soup = BeautifulSoup(html, 'lxml')
    data = {}

    # Quick features
    for testid in ['mileage', 'power', 'fuel', 'transmission', 'firstRegistration', 'numberOfPreviousOwners']:
        el = soup.select_one(f'[data-testid="vip-key-features-list-item-{testid}"] .geJSa')
        if not el: continue
        val = el.get_text(' ', strip=True).replace('\xa0', ' ')
        if testid == 'mileage':
            n = re.sub(r'[^\d]', '', val)
            data['km'] = int(n) if n else None
        elif testid == 'power':
            m = re.search(r'(\d+)\s*kW.*?\(?(\d+)\s*PS', val)
            if m:
                data['power_kw'], data['power_ps'] = int(m.group(1)), int(m.group(2))
        elif testid == 'fuel':
            data['fuel'] = val
        elif testid == 'transmission':
            data['gearbox'] = val
        elif testid == 'firstRegistration':
            data['first_reg'] = val
        elif testid == 'numberOfPreviousOwners':
            n = re.sub(r'[^\d]', '', val)
            data['previous_owners'] = int(n) if n else None

    # Technische Daten (alle dt[data-testid$="-item"] dd)
    for dt in soup.select('dt[data-testid$="-item"]'):
        testid = dt.get('data-testid', '').replace('-item', '')
        dd = dt.find_next_sibling('dd')
        if not dd: continue
        val = dd.get_text(' ', strip=True).replace('\xa0', ' ')
        if testid == 'category':
            data['category'] = val
        elif testid == 'emissionsSticker':
            data['emission_class'] = val
        elif testid == 'hu':
            data['hu_until'] = val
        elif testid == 'manufacturerColorName':
            data['color'] = val

    # Ausstattung
    features = [li.get_text(' ', strip=True) for li in soup.select('[data-testid="vip-features-list"] li')]
    features = [f for f in features if f]
    if features:
        data['features_json'] = json.dumps(features, ensure_ascii=False)

    # Beschreibung
    desc_el = soup.select_one('[data-testid="vip-vehicle-description-text"]')
    if desc_el:
        # <br> als newline
        for br in desc_el.find_all('br'):
            br.replace_with('\n')
        data['description'] = desc_el.get_text('\n', strip=True)

    # Bilder
    img_urls = []
    for img in soup.select('img[src*="img.classistatic.de"], img[srcset*="img.classistatic.de"]'):
        src = img.get('src', '')
        if 'img.classistatic.de' in src and src not in img_urls:
            # Größtes Format
            src_big = re.sub(r'rule=mo-\d+w', 'rule=mo-1024w', src)
            img_urls.append(src_big)
    if img_urls:
        data['image_urls_json'] = json.dumps(img_urls[:20], ensure_ascii=False)

    return data


# ============================================================
# Save / Sync
# ============================================================
def upsert_listing(data):
    """Insert oder update mit dynamischer Spaltenliste."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cols = list(data.keys())
    values = [data[c] for c in cols]
    placeholders = ','.join(['?'] * len(cols))
    set_clause = ', '.join([f"{c}=excluded.{c}" for c in cols if c != 'id'])

    sql = f"""INSERT INTO mobile_listings ({','.join(cols)}) VALUES ({placeholders})
              ON CONFLICT(id) DO UPDATE SET {set_clause}, last_seen=datetime('now')"""
    cur.execute(sql, values)
    conn.commit()
    conn.close()


def get_existing_ids():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id FROM mobile_listings").fetchall()
    conn.close()
    return set(r[0] for r in rows)


def sync_srp(srp_url, fetch_details=True, max_details=10):
    """Hauptfunktion: SRP scrapen, neue Listings finden, Details holen."""
    init_db()
    print(f"[i] Sync SRP: {srp_url[:80]}...", file=sys.stderr)

    html, status, cost = fetch(srp_url)
    log_scrape('srp', srp_url, cost, f'status={status}')
    print(f"[i] SRP status={status} cost={cost}", file=sys.stderr)

    if status != 200:
        return {"ok": False, "error": f"status_{status}"}

    listings = parse_srp(html)
    print(f"[i] {len(listings)} Listings auf SRP", file=sys.stderr)

    existing = get_existing_ids()
    new_listings = [l for l in listings if l['id'] not in existing]
    print(f"[i] {len(new_listings)} davon NEU", file=sys.stderr)

    # Save SRP-Daten aller neuen Listings
    for l in new_listings:
        upsert_listing(l)

    # Aktualisiere last_seen für alte
    for l in listings:
        if l['id'] in existing:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE mobile_listings SET last_seen=datetime('now') WHERE id=?", (l['id'],))
            conn.commit()
            conn.close()

    # Detailseiten für neue Listings (limitiert)
    detail_count = 0
    if fetch_details:
        for l in new_listings[:max_details]:
            try:
                dhtml, dstatus, dcost = fetch(l['url'])
                log_scrape('detail', l['url'], dcost, f'status={dstatus}')
                if dstatus == 200:
                    det = parse_detail(dhtml)
                    det['id'] = l['id']
                    det['detail_scraped'] = 1
                    upsert_listing(det)
                    detail_count += 1
                    print(f"[i] Detail OK: {l['title']}", file=sys.stderr)
            except Exception as e:
                print(f"[!] Detail-Fehler {l['id']}: {e}", file=sys.stderr)

    return {
        "ok": True,
        "total_on_srp": len(listings),
        "new_count": len(new_listings),
        "details_fetched": detail_count,
        "cost_estimate": cost + (detail_count * 25),
    }


if __name__ == "__main__":
    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://suchen.mobile.de/fahrzeuge/search.html?cn=DE&dam=false&fr=2012%3A&gn=Bayern&isSearchRequest=true&ll=48.7904472%2C11.4978895&ml=%3A100000&od=down&p=%3A9000&rd=100&ref=srpHead&s=Car&sb=doc&st=FSBO&vc=Car"
    max_details = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    result = sync_srp(test_url, fetch_details=True, max_details=max_details)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
