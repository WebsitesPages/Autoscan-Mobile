# mobile_app.py — Mobile.de Web-UI (1:1 zu Kleinanzeigen-Autoscan, Mobile.de-Variante)
import os
import sqlite3
import json
import threading
import logging
import sys
import re
from datetime import datetime, timedelta
from time import monotonic
from urllib.parse import parse_qs

from flask import Flask, request, redirect, url_for, render_template_string, send_from_directory, make_response
from dotenv import load_dotenv
from pywebpush import webpush, WebPushException
from py_vapid import Vapid

load_dotenv('/opt/mobile-test/.env')

# ============================================================
# Config
# ============================================================
APP_TITLE = "Autoscan Mobile"
DB_PATH = os.environ.get("MOBILE_DB", "/opt/mobile-test/mobile.db")
PER_PAGE_DEFAULT = 50
WEB_PORT = int(os.environ.get("WEB_PORT", "5001"))
SCRIPT_NAME = "/mobile"  # via Nginx

VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_PEM = ""
_pem_file = os.environ.get("VAPID_PRIVATE_KEY_PEM_FILE", "/opt/mobile-test/vapid_private.pem")
if _pem_file and os.path.exists(_pem_file):
    with open(_pem_file) as _f:
        VAPID_PRIVATE_PEM = _f.read().strip()
PUSH_SUBJECT = os.environ.get("PUSH_SUBJECT", "mailto:noreply@example.com")

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
    format="[%(asctime)s] [%(levelname)s] %(message)s")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config['APPLICATION_ROOT'] = SCRIPT_NAME

# Fix für Subpath /mobile/ (X-Script-Name vom Nginx)
class ScriptNameMiddleware:
    def __init__(self, app, script_name):
        self.app = app
        self.script_name = script_name
    def __call__(self, environ, start_response):
        sn = environ.get('HTTP_X_SCRIPT_NAME') or self.script_name
        if sn:
            environ['SCRIPT_NAME'] = sn
            path = environ['PATH_INFO']
            if path.startswith(sn):
                environ['PATH_INFO'] = path[len(sn):]
        return self.app(environ, start_response)
app.wsgi_app = ScriptNameMiddleware(app.wsgi_app, SCRIPT_NAME)

# ============================================================
# Helpers
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_push_tables():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions(
        id INTEGER PRIMARY KEY,
        endpoint TEXT UNIQUE,
        p256dh TEXT, auth TEXT,
        filters TEXT,
        max_price INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS push_sent(
        endpoint TEXT NOT NULL,
        listing_id TEXT NOT NULL,
        sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (endpoint, listing_id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS favorites(
        listing_id TEXT PRIMARY KEY,
        status TEXT DEFAULT 'interessant',
        note TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit(); conn.close()

init_push_tables()


def init_marketprice_cache():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS marketprice_cache(
        listing_id TEXT PRIMARY KEY,
        result_json TEXT,
        comparable_count INTEGER,
        cached_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit(); conn.close()
init_marketprice_cache()


# Mobile.de Codes laden
_mobile_codes = None
def get_mobile_codes():
    global _mobile_codes
    if _mobile_codes is None:
        try:
            with open('/opt/mobile-test/mobile_codes.json') as f:
                _mobile_codes = json.load(f)
        except Exception as e:
            logging.error(f"mobile_codes.json fehlt: {e}")
            _mobile_codes = {'brands': {}, 'models': {}, 'brand_models': {}}
    return _mobile_codes


def find_brand_model_codes(brand_text, model_text):
    """Findet (brand_code, model_code).
    DB-Format: brand_text = "Marke Modell" (z.B. "Opel Astra"), model_text = "Variante" (z.B. "ST 1.4")
    Strategie:
      1) Brand: längste passende Marke vom Anfang von brand_text matchen
      2) Modell: aus dem Rest von brand_text extrahieren
    """
    if not brand_text:
        return None, None
    codes = get_mobile_codes()
    brand_text_lower = brand_text.strip().lower()

    # 1) Brand finden: längste Marke die brand_text beginnt
    # (sortiere nach Länge absteigend, damit "Mercedes-Benz" vor "Mercedes" matcht)
    brand_candidates = sorted(codes['brands'].items(), key=lambda x: -len(x[0]))
    brand_code = None
    brand_name = None
    rest = ""
    for name, code in brand_candidates:
        nl = name.lower()
        if brand_text_lower == nl:
            brand_code = code; brand_name = name; rest = ""
            break
        if brand_text_lower.startswith(nl + " "):
            brand_code = code; brand_name = name
            rest = brand_text_lower[len(nl):].strip()
            break

    if not brand_code:
        return None, None

    # 2) Modell aus dem Rest finden
    if not rest:
        return brand_code, None

    # Versuche längsten Match aus brand_models[brand]
    candidates = codes['brand_models'].get(brand_name.lower(), [])
    candidates_sorted = sorted(candidates, key=lambda x: -len(x[0]))

    for model_name, model_code in candidates_sorted:
        ml = model_name.lower()
        # Match wenn Modell-Name irgendwo im "rest" vorkommt
        # ODER der "rest" mit Modell-Name beginnt
        if rest == ml or rest.startswith(ml + " ") or rest.startswith(ml):
            return brand_code, model_code
        # Auch: Modell-Name als Teil-Wort (z.B. "Astra" in "Astra ST")
        if (" " + ml + " ") in (" " + rest + " "):
            return brand_code, model_code

    # Falls model_text auch noch was sagt (Variante)
    if model_text:
        full_search = (rest + " " + model_text).lower()
        for model_name, model_code in candidates_sorted:
            ml = model_name.lower()
            if ml in full_search:
                return brand_code, model_code

    return brand_code, None


def parse_int(v, default=None):
    try:
        if v is None or v == "": return default
        return int(v)
    except: return default

# ============================================================
# Static / PWA
# ============================================================
@app.route("/manifest.webmanifest")
def manifest():
    resp = make_response(send_from_directory("static", "manifest.webmanifest"))
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp

@app.route("/sw.js")
def sw():
    resp = make_response(send_from_directory("static", "sw.js"))
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/mobile/"
    return resp

@app.after_request
def no_cache(resp):
    if request.path.startswith("/static"):
        return resp
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# ============================================================
# Query Builder für mobile_listings
# ============================================================
def build_query(params):
    where = []
    args = []

    if params.get("q"):
        where.append("title LIKE ?")
        args.append(f"%{params['q']}%")

    pmin = parse_int(params.get("price_min"))
    pmax = parse_int(params.get("price_max"))
    if pmin is not None: where.append("price_eur >= ?"); args.append(pmin)
    if pmax is not None: where.append("price_eur <= ?"); args.append(pmax)

    ez_min = parse_int(params.get("ez_min"))
    ez_max = parse_int(params.get("ez_max"))
    if ez_min is not None or ez_max is not None:
        # first_reg ist im Format "MM/YYYY" oder ähnlich
        ez_year_sql = """
        CASE
          WHEN first_reg IS NOT NULL AND length(first_reg) >= 4 THEN
            CAST(substr(first_reg, length(first_reg)-3, 4) AS INTEGER)
          ELSE NULL
        END
        """
        if ez_min is not None: where.append(f"({ez_year_sql}) >= ?"); args.append(ez_min)
        if ez_max is not None: where.append(f"({ez_year_sql}) <= ?"); args.append(ez_max)

    km_max = parse_int(params.get("km_max"))
    if km_max is not None: where.append("km <= ?"); args.append(km_max)

    plz = params.get("postal_prefix")
    if plz: where.append("postal_code LIKE ?"); args.append(plz.rstrip("%") + "%")

    city = params.get("city")
    if city: where.append("city LIKE ?"); args.append(f"%{city}%")

    seller = params.get("seller_type")
    if seller in ("private", "dealer"):
        where.append("seller_type = ?"); args.append(seller)

    fuel = params.get("fuel")
    if fuel: where.append("fuel = ?"); args.append(fuel)

    unfallfrei = params.get("unfallfrei")
    if unfallfrei == "1": where.append("unfallfrei = 1")

    posted_days = parse_int(params.get("posted_days"))
    if posted_days is not None and posted_days >= 0:
        where.append("online_since_dt IS NOT NULL AND online_since_dt >= datetime('now', ?)")
        args.append(f"-{posted_days} day")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sort = params.get("sort", "posted_desc")
    order_sql = {
        "price_asc":   "ORDER BY (price_eur IS NULL), price_eur ASC, last_seen DESC",
        "price_desc":  "ORDER BY (price_eur IS NULL), price_eur DESC, last_seen DESC",
        "km_asc":      "ORDER BY (km IS NULL), km ASC, last_seen DESC",
        "km_desc":     "ORDER BY (km IS NULL), km DESC, last_seen DESC",
        "posted_desc": "ORDER BY (online_since_dt IS NULL), online_since_dt DESC, last_seen DESC",
        "seen_desc":   "ORDER BY last_seen DESC",
        "title_asc":   "ORDER BY title COLLATE NOCASE ASC, last_seen DESC",
    }.get(sort, "ORDER BY (online_since_dt IS NULL), online_since_dt DESC, last_seen DESC")

    return where_sql, args, order_sql

# ============================================================
# Routes
# ============================================================
@app.route("/")
def index():
    try:
        q = request.args.get("q", "")
        params = {
            "q": q,
            "price_min": request.args.get("price_min", ""),
            "price_max": request.args.get("price_max", "9000"),
            "ez_min": request.args.get("ez_min", "2012"),
            "ez_max": request.args.get("ez_max", ""),
            "km_max": request.args.get("km_max", "150000"),
            "postal_prefix": request.args.get("postal_prefix", ""),
            "city": request.args.get("city", ""),
            "seller_type": request.args.get("seller_type", "private"),
            "fuel": request.args.get("fuel", ""),
            "unfallfrei": request.args.get("unfallfrei", ""),
            "posted_days": request.args.get("posted_days", ""),
            "sort": request.args.get("sort", "posted_desc"),
        }

        page = max(parse_int(request.args.get("page"), 1) or 1, 1)
        per_page = max(parse_int(request.args.get("per_page"), PER_PAGE_DEFAULT) or PER_PAGE_DEFAULT, 1)
        offset = (page - 1) * per_page

        where_sql, args, order_sql = build_query(params)
        conn = get_db(); cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) AS n FROM mobile_listings {where_sql}", args)
        total = cur.fetchone()[0]

        cur.execute(
            f"""SELECT id, url, title, brand, model_text, price_eur, price_rating,
                       price_negotiable, unfallfrei, km, first_reg, power_kw, power_ps,
                       fuel, gearbox, previous_owners, emission_class, category,
                       hu_until, color, seller_type, location, postal_code, city,
                       online_since, online_since_dt, image_url, image_urls_json,
                       features_json, description, first_seen, last_seen, detail_scraped,
                       price_rating_code, price_rating_label, price_thresholds_json, price_offset
                FROM mobile_listings {where_sql} {order_sql} LIMIT ? OFFSET ?""",
            args + [per_page, offset],
        )
        rows = cur.fetchall()
        conn.close()

        def page_url(p):
            if p < 1: p = 1
            qs = request.args.to_dict(flat=True)
            qs["page"] = str(p)
            return url_for("index", **qs)

        return render_template_string(TPL, **{
            "app_title": APP_TITLE,
            "rows": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": (offset + len(rows)) < total,
            "prev_url": page_url(page - 1),
            "next_url": page_url(page + 1),
            "params": params,
            "VAPID_PUBLIC": VAPID_PUBLIC,
        })
    except Exception as e:
        import traceback as tb
        return f"<h1>Fehler</h1><pre>{tb.format_exc()}</pre>", 500

@app.get("/api/table")
def api_table():
    params = {
        "q": request.args.get("q", ""),
        "price_min": request.args.get("price_min", ""),
        "price_max": request.args.get("price_max", "9000"),
        "ez_min": request.args.get("ez_min", "2012"),
        "ez_max": request.args.get("ez_max", ""),
        "km_max": request.args.get("km_max", "150000"),
        "postal_prefix": request.args.get("postal_prefix", ""),
        "city": request.args.get("city", ""),
        "seller_type": request.args.get("seller_type", "private"),
        "fuel": request.args.get("fuel", ""),
        "unfallfrei": request.args.get("unfallfrei", ""),
        "posted_days": request.args.get("posted_days", ""),
        "sort": request.args.get("sort", "posted_desc"),
    }
    where_sql, args, order_sql = build_query(params)
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        f"""SELECT id, url, title, brand, model_text, price_eur, price_rating,
                   price_negotiable, unfallfrei, km, first_reg, power_kw, power_ps,
                   fuel, gearbox, hu_until, seller_type, location, postal_code, city,
                   online_since, online_since_dt, image_url, image_urls_json,
                   price_rating_code, price_rating_label, price_thresholds_json, price_offset
            FROM mobile_listings {where_sql} {order_sql} LIMIT ? OFFSET ?""",
        args + [50, 0],
    )
    rows = cur.fetchall()
    conn.close()
    return render_template_string(CARDS_TPL, rows=rows)

# ============================================================
# Sync & Push trigger
# ============================================================
_sync_lock = threading.Lock()
_last_sync_ts = 0.0

@app.get("/api/marketprice/<listing_id>")
def api_marketprice(listing_id):
    """Berechnet Marktwert via live mobile.de-Suche mit ähnlichen Inseraten."""
    import sys as _sys, json as _json, re as _re, statistics as _stats
    if '/opt/mobile-test' not in _sys.path:
        _sys.path.insert(0, '/opt/mobile-test')

    force = request.args.get('force') == '1'
    conn = get_db(); cur = conn.cursor()

    # Cache-Check (1h gültig)
    if not force:
        cached = cur.execute("""SELECT result_json, comparable_count, cached_at
                                FROM marketprice_cache
                                WHERE listing_id=?
                                  AND cached_at > datetime('now', '-1 hour')""",
                             (listing_id,)).fetchone()
        if cached:
            conn.close()
            try:
                result = _json.loads(cached['result_json'])
                result['from_cache'] = True
                result['cached_at'] = cached['cached_at']
                return result
            except: pass

    row = cur.execute("""SELECT id, brand, model_text, price_eur, km, first_reg,
                                fuel, gearbox, power_ps, features_json
                         FROM mobile_listings WHERE id=?""", (listing_id,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "listing_not_found"}, 404

    brand_text = row['brand']
    model_text = row['model_text']
    price = row['price_eur']
    km = row['km']
    first_reg = row['first_reg']
    fuel = row['fuel']
    gearbox = row['gearbox']
    power_ps = row['power_ps']

    brand_code, model_code = find_brand_model_codes(brand_text, model_text)
    if not brand_code:
        conn.close()
        return {"ok": False, "error": "brand_unknown", "brand": brand_text}

    ez_year = None
    if first_reg:
        m = _re.search(r'(\d{4})', str(first_reg))
        if m: ez_year = int(m.group(1))

    km_min = km_max = None
    if km:
        km_min = int(km * 0.9)
        km_max = int(km * 1.1)

    features = []
    if row['features_json']:
        try:
            features = _json.loads(row['features_json'])
            if not isinstance(features, list): features = []
        except: features = []

    # Mobile.de Feature-Codes (String-IDs)
    feature_map_fe = {
        'sitzheizung': 'ELECTRIC_HEATED_SEATS',
        'navigationssystem': 'NAVIGATION_SYSTEM',
        'navi': 'NAVIGATION_SYSTEM',
        'bluetooth': 'BLUETOOTH',
        'klimaautomatik': 'AUTOMATIC_CLIMATISATION',
        'klimaanlage': 'CLIMATISATION',
        'einparkhilfe': 'PARKING_ASSISTANT_SENSORS_REAR',
        'rückfahrkamera': 'PARKING_ASSISTANT_CAMERA',
        'soundsystem': 'SOUND_SYSTEM',
        'leder': 'FULL_LEATHER',
        'panoramadach': 'PANORAMIC_GLASS_ROOF',
        'allradantrieb': 'FOUR_WHEEL_DRIVE',
        'xenon': 'XENON_HEADLIGHTS',
        'led': 'LED_HEADLIGHTS',
        'isofix': 'ISOFIX',
    }
    feature_map_spc = {
        'tempomat': 'CRUISE_CONTROL',
        'spurhalteassistent': 'LANE_DEPARTURE_WARNING',
        'totwinkelassistent': 'BLIND_SPOT_MONITOR',
        'notbremsassistent': 'EMERGENCY_BRAKE_ASSIST',
        'verkehrszeichenerkennung': 'TRAFFIC_SIGN_RECOGNITION',
        'head-up display': 'HEAD_UP_DISPLAY',
    }
    selected_fe = set()
    selected_spc = set()
    for feat in features:
        flow = feat.lower().strip() if isinstance(feat, str) else ''
        for keyword, code in feature_map_fe.items():
            if keyword in flow:
                selected_fe.add(code)
        for keyword, code in feature_map_spc.items():
            if keyword in flow:
                selected_spc.add(code)

    from urllib.parse import urlencode
    params = [
        ('isSearchRequest', 'true'),
        ('s', 'Car'), ('vc', 'Car'),
        ('cn', 'DE'),
        ('dam', 'false'),
        ('od', 'down'), ('sb', 'p'),
    ]
    if model_code:
        params.append(('ms', f'{brand_code};{model_code};;'))
    else:
        params.append(('ms', f'{brand_code};;;'))
    if ez_year:
        params.append(('fr', f'{ez_year}:{ez_year}'))
    if km_min and km_max:
        params.append(('ml', f'{km_min}:{km_max}'))
    elif km_max:
        params.append(('ml', f':{km_max}'))

    fuel_map = {'benzin': 'PETROL', 'diesel': 'DIESEL', 'hybrid': 'HYBRID',
                'elektro': 'ELECTRICITY', 'lpg': 'LPG', 'cng': 'CNG'}
    if fuel:
        fuel_code = fuel_map.get(fuel.lower())
        if fuel_code: params.append(('ft', fuel_code))

    if gearbox:
        gb = gearbox.lower()
        if 'automatik' in gb or 'automat' in gb:
            params.append(('tr', 'AUTOMATIC_GEAR'))
        elif 'schalt' in gb or 'manual' in gb:
            params.append(('tr', 'MANUAL_GEAR'))

    if power_ps:
        params.append(('powerf', str(int(power_ps * 0.85))))
        params.append(('powert', str(int(power_ps * 1.15))))

    for feat_code in selected_fe:
        params.append(('fe', feat_code))
    for feat_code in selected_spc:
        params.append(('spc', feat_code))

    search_url = 'https://suchen.mobile.de/fahrzeuge/search.html?' + urlencode(params)
    logging.info(f"[marketprice] {listing_id} -> {search_url}")

    try:
        from mobile_fetcher import get_session
        from mobile_scrape import parse_srp
        sess = get_session()
        html, status = sess.fetch(search_url)
        if status != 200 or len(html) < 5000:
            conn.close()
            return {"ok": False, "error": "scrape_failed", "status": status, "search_url": search_url}

        listings = parse_srp(html)
        comparables = [l for l in listings if str(l.get('id')) != str(listing_id)]

        if len(comparables) < 2:
            result = {
                "ok": True, "comparable_count": len(comparables),
                "search_url": search_url,
                "warning": "Zu wenige Vergleichsinserate",
                "your_price": price,
                "feature_filters_used_fe": list(selected_fe), "feature_filters_used_spc": list(selected_spc),
                "filters_applied": {
                    "brand": brand_text, "model": model_text,
                    "ez_year": ez_year,
                    "km_range": [km_min, km_max] if km_min else None,
                    "fuel": fuel, "gearbox": gearbox,
                }
            }
            cur.execute("""INSERT OR REPLACE INTO marketprice_cache(listing_id, result_json, comparable_count)
                            VALUES(?,?,?)""", (listing_id, _json.dumps(result), len(comparables)))
            conn.commit(); conn.close()
            return result

        prices = [int(l['price_eur']) for l in comparables if l.get('price_eur')]
        if not prices:
            conn.close()
            return {"ok": False, "error": "no_prices_in_comparables"}

        avg = round(sum(prices) / len(prices))
        median = round(_stats.median(prices))
        pmin, pmax = min(prices), max(prices)

        sellers = [l.get('seller_type') for l in comparables]
        private_count = sum(1 for s in sellers if s == 'private')
        dealer_count = sum(1 for s in sellers if s == 'dealer')

        top_comparables = sorted(comparables, key=lambda l: l.get('price_eur') or 99999)[:5]
        comp_preview = [{
            'id': l.get('id'),
            'title': (l.get('title', '') or '')[:60],
            'price_eur': l.get('price_eur'),
            'km': l.get('km'),
            'city': l.get('city'),
            'url': l.get('url'),
        } for l in top_comparables]

        assessment = None
        diff = price - avg if price else None
        diff_pct = round((diff / avg) * 100, 1) if (price and avg) else None
        if diff_pct is not None:
            if diff_pct < -15:
                assessment = "🔥 SCHNÄPPCHEN — deutlich unter Markt!"
            elif diff_pct < -5:
                assessment = "✅ UNTER Markt — gutes Angebot"
            elif diff_pct < 5:
                assessment = "≈ Marktpreis — fair"
            elif diff_pct < 15:
                assessment = "⚠️ Über Markt — verhandeln!"
            else:
                assessment = "❌ Deutlich überteuert"

        result = {
            "ok": True,
            "your_price": price,
            "avg_price": avg, "median_price": median,
            "min_price": pmin, "max_price": pmax,
            "diff_eur": diff, "diff_pct": diff_pct,
            "comparable_count": len(comparables),
            "private_count": private_count, "dealer_count": dealer_count,
            "assessment": assessment,
            "search_url": search_url,
            "comparables": comp_preview,
            "feature_filters_used_fe": list(selected_fe), "feature_filters_used_spc": list(selected_spc),
            "filters_applied": {
                "brand": brand_text, "model": model_text, "ez_year": ez_year,
                "km_range": [km_min, km_max] if km_min else None,
                "power_range_ps": [int(power_ps*0.85), int(power_ps*1.15)] if power_ps else None,
                "fuel": fuel, "gearbox": gearbox,
            }
        }

        cur.execute("""INSERT OR REPLACE INTO marketprice_cache(listing_id, result_json, comparable_count)
                       VALUES(?,?,?)""", (listing_id, _json.dumps(result), len(comparables)))
        conn.commit(); conn.close()
        return result

    except Exception as e:
        logging.exception(f"[marketprice] Fehler: {e}")
        conn.close()
        return {"ok": False, "error": "exception", "msg": str(e)}, 500


@app.post("/api/trigger_scrape")
def api_trigger_scrape():
    """Triggert sofort einen neuen Scrape (Cooldown 60s gegen Spam)."""
    import json as _json
    import threading as _th
    global _last_sync_ts
    now = monotonic()

    # Cooldown: max 1x pro 60 Sekunden
    if (now - _last_sync_ts) < 60.0:
        wait = int(60 - (now - _last_sync_ts))
        return {"ok": False, "error": f"cooldown", "wait_seconds": wait}, 429

    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "already_running"}, 409

    def _do_scrape():
        global _last_sync_ts
        try:
            # Filter-Config laden
            cfg_path = os.environ.get("MOBILE_FILTER_CONFIG", "/opt/mobile-test/filter_config.json")
            with open(cfg_path) as f:
                cfg = _json.load(f)
            searches = cfg.get("searches") or []

            # Import erst hier (lazy)
            import sys as _sys
            if '/opt/mobile-test' not in _sys.path:
                _sys.path.insert(0, '/opt/mobile-test')
            from mobile_scrape import sync_srp
            import sqlite3 as _sql

            new_ids_total = []
            for s in searches:
                # IDs vor Scrape
                conn = _sql.connect(DB_PATH)
                before = set(r[0] for r in conn.execute("SELECT id FROM mobile_listings"))
                conn.close()

                result = sync_srp(s["url"], fetch_details=True, max_details=10, reuse_session=True)
                logging.info(f"[trigger] {s.get('name','?')}: {result}")

                # Neue IDs ermitteln
                if result.get("new_count", 0) > 0:
                    conn = _sql.connect(DB_PATH)
                    after = set(r[0] for r in conn.execute("SELECT id FROM mobile_listings"))
                    conn.close()
                    new_ids_total.extend(list(after - before))

            # Push für neue Listings (falls welche dabei)
            if new_ids_total:
                try:
                    sent = notify_new_listings(new_ids_total)
                    logging.info(f"[trigger] {sent} Push-Notifications versendet")
                except Exception as e:
                    logging.warning(f"[trigger] Push-Fehler: {e}")

            _last_sync_ts = monotonic()
        except Exception as e:
            logging.exception(f"[trigger] Scrape-Fehler: {e}")
        finally:
            _sync_lock.release()

    # Async im Hintergrund starten — Frontend bekommt sofort Antwort
    _th.Thread(target=_do_scrape, daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/api/sync")
def api_sync():
    """Liefert echten letzten Server-Sync und Anzahl frischer Listings."""
    conn = get_db(); cur = conn.cursor()
    # Letzter erfolgreicher SRP-Scrape
    last_run = cur.execute(
        "SELECT MAX(scraped_at) FROM mobile_scrape_log WHERE action='srp' AND result LIKE 'status=200%'"
    ).fetchone()[0]
    # Listings seit letzten 15 Min
    fresh = cur.execute(
        "SELECT COUNT(*) FROM mobile_listings WHERE first_seen >= datetime('now', '-15 minutes')"
    ).fetchone()[0]
    # Max first_seen
    last_listing = cur.execute("SELECT MAX(first_seen) FROM mobile_listings").fetchone()[0]
    conn.close()
    return {
        "ok": True,
        "last_run_at": last_run,           # UTC-Zeitstempel des letzten Server-Scrapes
        "last_listing_at": last_listing,   # UTC-Zeitstempel des neuesten Listings
        "fresh_count_15min": fresh,
    }

# ============================================================
# Favorites API
# ============================================================
@app.get("/api/favs")
def api_favs():
    conn = get_db(); cur = conn.cursor()
    rows = cur.execute("SELECT listing_id, status, note FROM favorites ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"ok": True, "favs": [{"listing_id": r["listing_id"], "status": r["status"], "note": r["note"] or ""} for r in rows]}

@app.post("/api/fav")
def api_fav_set():
    data = request.get_json(force=True, silent=True) or {}
    lid = (data.get("id") or "").strip()
    status = (data.get("status") or "interessant").strip()
    note = (data.get("note") or "").strip()
    if not lid: return {"ok": False}, 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO favorites(listing_id, status, note) VALUES(?,?,?)
                   ON CONFLICT(listing_id) DO UPDATE SET status=excluded.status, note=excluded.note""",
                (lid, status, note))
    conn.commit(); conn.close()
    return {"ok": True}

@app.delete("/api/fav")
def api_fav_del():
    data = request.get_json(force=True, silent=True) or {}
    lid = (data.get("id") or "").strip()
    if not lid: return {"ok": False}, 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM favorites WHERE listing_id=?", (lid,))
    conn.commit(); conn.close()
    return {"ok": True}

# ============================================================
# Push API
# ============================================================
@app.post("/api/push/subscribe")
def api_push_sub():
    data = request.get_json(force=True, silent=True) or {}
    sub = data.get("subscription") or {}
    filt = data.get("filters")          # None = nicht mitgeschickt (blinder Re-Subscribe)
    maxp = data.get("max_price")
    old_ep = (data.get("old_endpoint") or "").strip()
    if not (sub.get("endpoint") and sub.get("keys",{}).get("p256dh") and sub["keys"].get("auth")):
        return {"ok": False, "error": "bad subscription"}, 400
    conn = get_db(); cur = conn.cursor()
    # Blinder Re-Subscribe (SW pushsubscriptionchange) schickt keine Filter mit →
    # vom rotierten alten bzw. zuletzt aktiven Abo übernehmen, damit die Filter nicht verloren gehen.
    if filt is None:
        src = None
        if old_ep:
            src = cur.execute("SELECT filters, max_price FROM push_subscriptions WHERE endpoint=?", (old_ep,)).fetchone()
        if src is None:
            src = cur.execute("SELECT filters, max_price FROM push_subscriptions ORDER BY created_at DESC LIMIT 1").fetchone()
        if src is not None:
            filt = src["filters"] or ""
            if maxp is None: maxp = src["max_price"]
        else:
            filt = ""
    cur.execute("""INSERT INTO push_subscriptions(endpoint,p256dh,auth,filters,max_price)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh,auth=excluded.auth,
                   filters=excluded.filters,max_price=excluded.max_price""",
                (sub["endpoint"], sub["keys"]["p256dh"], sub["keys"]["auth"], filt, maxp))
    # Rotiertes altes Endpoint aufräumen, damit keine Karteileiche zurückbleibt
    if old_ep and old_ep != sub["endpoint"]:
        cur.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (old_ep,))
        cur.execute("DELETE FROM push_sent WHERE endpoint=?", (old_ep,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/api/push/vapid_public")
def api_push_vapid():
    # Public Key (unkritisch) — der Service Worker holt ihn hier zum Re-Subscribe
    return {"ok": True, "key": VAPID_PUBLIC}

@app.post("/api/push/unsubscribe")
def api_push_unsub():
    data = request.get_json(force=True, silent=True) or {}
    ep = (data.get("endpoint") or "").strip()
    if not ep: return {"ok": False}, 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (ep,))
    cur.execute("DELETE FROM push_sent WHERE endpoint=?", (ep,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/api/push/list")
def api_push_list():
    conn = get_db(); cur = conn.cursor()
    rows = cur.execute("SELECT endpoint, filters, max_price, created_at FROM push_subscriptions ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"ok": True, "subs": [{"endpoint": r["endpoint"], "filters": r["filters"] or "", "max_price": r["max_price"], "created_at": r["created_at"]} for r in rows]}

# ============================================================
# Notify Function (called by runner.py)
# ============================================================
def notify_new_listings(listing_ids):
    """Wird vom runner.py aufgerufen wenn neue Listings da sind."""
    if not listing_ids or not VAPID_PRIVATE_PEM:
        return 0
    conn = get_db(); cur = conn.cursor()
    placeholders = ",".join(["?"] * len(listing_ids))
    cur.execute(f"""SELECT id, title, price_eur, km, city, url, online_since, postal_code,
                           first_reg, brand, seller_type, price_rating, price_thresholds_json
                    FROM mobile_listings WHERE id IN ({placeholders})""", listing_ids)
    new_rows = cur.fetchall()
    subs = list(cur.execute("SELECT endpoint,p256dh,auth,filters,max_price FROM push_subscriptions"))

    sent_count = 0
    for r in new_rows:
        rid = str(r["id"])
        for (endpoint, p256dh, auth, filters, max_price) in subs:
            if cur.execute("SELECT 1 FROM push_sent WHERE endpoint=? AND listing_id=?", (endpoint, rid)).fetchone():
                continue

            # Filter check
            params = {}
            if filters:
                for k, v in parse_qs(filters, keep_blank_values=True).items():
                    params[k] = v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else "")
            ok = True

            if max_price and r["price_eur"] is not None:
                try:
                    if int(r["price_eur"]) > int(max_price): ok = False
                except: pass

            if ok and params.get("price_max") and r["price_eur"] is not None:
                try:
                    if int(r["price_eur"]) > int(params["price_max"]): ok = False
                except: pass

            if ok and params.get("price_min") and r["price_eur"] is not None:
                try:
                    if int(r["price_eur"]) < int(params["price_min"]): ok = False
                except: pass

            if ok and params.get("km_max") and r["km"] is not None:
                try:
                    if int(r["km"]) > int(params["km_max"]): ok = False
                except: pass

            if ok and params.get("postal_prefix"):
                if not (r["postal_code"] or "").startswith(params["postal_prefix"]): ok = False

            if ok and params.get("city"):
                if params["city"].lower() not in (r["city"] or "").lower(): ok = False

            if ok and params.get("q"):
                if params["q"].lower() not in (r["title"] or "").lower(): ok = False

            if ok and params.get("seller_type") in ("private", "dealer"):
                if r["seller_type"] != params["seller_type"]: ok = False

            # HOT-DEAL-FILTER: Nur Push bei echten Schnäppchen
            # - "Ohne Bewertung" → immer (oft Goldgruben, seltene Modelle)
            # - "Sehr guter Preis" → NUR wenn Preis im unteren 30% der Sehr-Gut-Range liegt
            #   (verhindert Push bei "11.8k in 10-12k Range" — algorithmisch Sehr-Gut, aber kein echter Deal)
            if ok:
                rating = (r["price_rating"] or "").strip().lower()
                is_great = "sehr guter" in rating or "very_good" in rating
                is_unrated = (not rating) or ("ohne bewertung" in rating) or ("no_rating" in rating)
                
                if is_unrated:
                    pass  # immer OK
                elif is_great:
                    # Prüfe ob Preis im unteren 30% der Sehr-Gut-Range
                    thr_json = r["price_thresholds_json"] if "price_thresholds_json" in r.keys() else None
                    if thr_json and r["price_eur"]:
                        try:
                            import json as _j
                            thr = _j.loads(thr_json)
                            if len(thr) >= 2:
                                sg_min, sg_max = thr[0], thr[1]
                                # Zone der echten Schnäppchen = untere 30% des Sehr-Gut-Segments
                                cutoff = sg_min + (sg_max - sg_min) * 0.30
                                if r["price_eur"] > cutoff:
                                    ok = False  # zu nah an "Guter Preis"-Grenze
                        except: pass
                    # Falls keine Thresholds verfügbar → trotzdem durchlassen (besser als nichts)
                else:
                    ok = False  # Guter/Fairer/Erhöhter/Hoher Preis → kein Push

            if not ok: continue

            # Emoji je nach Rating für Push-Titel
            # Emoji: 🔥 nur für echte HOT DEALs (untere 30% bei Sehr-Gut), sonst 🆕
            rating_emoji = "🆕"
            try:
                rating_lower = (r["price_rating"] or "").lower()
                if "sehr guter" in rating_lower and r["price_thresholds_json"] and r["price_eur"]:
                    import json as _j_emo
                    thr_emo = _j_emo.loads(r["price_thresholds_json"])
                    if len(thr_emo) >= 2:
                        cutoff_emo = thr_emo[0] + (thr_emo[1] - thr_emo[0]) * 0.30
                        if r["price_eur"] <= cutoff_emo:
                            rating_emoji = "🔥"
            except: pass
            payload = {
                "title": f"{rating_emoji} {'HOT DEAL' if rating_emoji == '🔥' else 'Neues Inserat'} — Mobile.de",
                "body": f"{r['title']} — {r['price_eur'] or '—'} € • {r['city'] or ''}",
                "url": r["url"],
                "tag": rid,
            }
            try:
                webpush(
                    subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
                    data=json.dumps(payload),
                    vapid_private_key=Vapid.from_pem(VAPID_PRIVATE_PEM.encode() if isinstance(VAPID_PRIVATE_PEM, str) else VAPID_PRIVATE_PEM),
                    vapid_claims={"sub": PUSH_SUBJECT},
                )
                cur.execute("INSERT OR IGNORE INTO push_sent(endpoint, listing_id) VALUES(?,?)", (endpoint, rid))
                conn.commit()
                sent_count += 1
            except WebPushException as e:
                logging.warning(f"Push fail {endpoint[:40]}: {e}")
                # Bei 410 Gone: Sub löschen
                if "410" in str(e) or "404" in str(e):
                    cur.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
                    conn.commit()
    conn.close()
    return sent_count

# ============================================================
# TEMPLATE - 1:1 Style wie Kleinanzeigen, Mobile.de-orange als Akzent
# ============================================================
TPL = r"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <link rel="manifest" href="/mobile/manifest.webmanifest">
  <link rel="apple-touch-icon" href="/mobile/static/icons/icon-192x192.png">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="theme-color" content="#0f172a">
  <title>{{ app_title }}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300..700;1,9..40,300..700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0f172a; --bg-card: #1e293b; --bg-card-hover: #273548;
      --bg-input: #1e293b; --bg-sheet: #162032;
      --border: #334155; --border-light: #475569;
      --text: #f1f5f9; --text-dim: #94a3b8; --text-muted: #64748b;
      --accent: #fb923c; --accent-hover: #fdba74;
      --green: #34d399; --green-bg: rgba(52,211,153,0.12);
      --red: #fb7185; --red-bg: rgba(251,113,133,0.12);
      --amber: #fbbf24; --amber-bg: rgba(251,191,36,0.12);
      --fuchsia: #e879f9; --blue: #38bdf8;
      --radius: 16px; --radius-sm: 10px;
      --shadow: 0 4px 24px rgba(0,0,0,0.3);
      --font: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      --mono: 'JetBrains Mono', monospace;
      --safe-top: env(safe-area-inset-top, 0px);
      --safe-bottom: env(safe-area-inset-bottom, 0px);
    }
    html { font-family: var(--font); background: var(--bg); color: var(--text); -webkit-text-size-adjust: 100%; }
    body { min-height: 100dvh; padding-bottom: calc(80px + var(--safe-bottom)); }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }

    .app-header {
      position: sticky; top: 0; z-index: 50;
      padding: calc(var(--safe-top) + 12px) 16px 12px;
      background: rgba(15,23,42,0.85);
      backdrop-filter: blur(20px) saturate(1.4);
      -webkit-backdrop-filter: blur(20px) saturate(1.4);
      border-bottom: 1px solid var(--border);
    }
    .header-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .logo { display: flex; align-items: center; gap: 10px; }
    .logo-icon {
      width: 36px; height: 36px; border-radius: 10px;
      background: linear-gradient(135deg, #fb923c, #f97316);
      display: grid; place-items: center;
      font-weight: 700; font-size: 14px; color: #fff;
    }
    .logo-text { font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }
    .logo-sub { font-size: 11px; color: var(--text-dim); font-weight: 400; }
    .header-actions { display: flex; gap: 8px; }
    .icon-btn {
      width: 38px; height: 38px; border-radius: 10px;
      background: var(--bg-card); border: 1px solid var(--border);
      color: var(--text-dim); display: grid; place-items: center;
      cursor: pointer; transition: all 0.15s; font-size: 18px;
    }
    .icon-btn:active { transform: scale(0.93); }
    .icon-btn:hover { background: var(--bg-card-hover); color: var(--text); }
    .switch-link {
      padding: 8px 12px; border-radius: 10px;
      background: rgba(56,189,248,0.12); border: 1px solid rgba(56,189,248,0.3);
      color: var(--blue); font-size: 12px; font-weight: 600;
      text-decoration: none;
    }

    .stats-bar {
      display: flex; align-items: center; gap: 12px;
      padding: 10px 16px; overflow-x: auto; -webkit-overflow-scrolling: touch;
    }
    .stat-chip {
      flex-shrink: 0; padding: 6px 14px; border-radius: 20px;
      background: var(--bg-card); border: 1px solid var(--border);
      font-size: 13px; font-weight: 500; white-space: nowrap;
      display: flex; align-items: center; gap: 6px;
    }
    .stat-chip .num { color: var(--accent); font-family: var(--mono); font-weight: 600; }

    .filter-toggle {
      display: flex; align-items: center; gap: 8px;
      margin: 0 16px; padding: 12px 16px; border-radius: var(--radius);
      background: var(--bg-card); border: 1px solid var(--border);
      color: var(--text); font-size: 14px; font-weight: 500;
      cursor: pointer; transition: all 0.2s;
    }
    .filter-toggle:active { transform: scale(0.98); }
    .filter-toggle .count {
      margin-left: auto; padding: 2px 10px; border-radius: 12px;
      background: var(--accent); color: var(--bg); font-size: 12px; font-weight: 700;
    }

    .filter-sheet {
      display: none; margin: 12px 16px 0;
      padding: 20px; border-radius: var(--radius);
      background: var(--bg-sheet); border: 1px solid var(--border);
    }
    .filter-sheet.open { display: block; animation: slideDown 0.25s ease; }
    @keyframes slideDown { from { opacity:0; transform: translateY(-8px); } to { opacity:1; transform: translateY(0); } }

    .filter-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .filter-grid .full { grid-column: 1 / -1; }
    .field-label { display: block; font-size: 11px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 5px; }
    .field-input {
      width: 100%; padding: 10px 14px; border-radius: var(--radius-sm);
      background: var(--bg-input); border: 1px solid var(--border);
      color: var(--text); font-size: 14px; font-family: var(--font);
      transition: border-color 0.15s; -webkit-appearance: none; appearance: none;
    }
    .field-input:focus { outline: none; border-color: var(--accent); }
    .field-input::placeholder { color: var(--text-muted); }
    select.field-input { background-image: url("data:image/svg+xml,%3Csvg width='12' height='8' viewBox='0 0 12 8' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1.5L6 6.5L11 1.5' stroke='%2394a3b8' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 14px center; padding-right: 36px; }

    .filter-actions { display: flex; gap: 10px; margin-top: 16px; }
    .btn {
      flex: 1; padding: 12px; border-radius: var(--radius-sm);
      font-size: 14px; font-weight: 600; font-family: var(--font);
      cursor: pointer; transition: all 0.15s; border: none;
      text-align: center; text-decoration: none;
    }
    .btn:active { transform: scale(0.97); }
    .btn-primary { background: var(--accent); color: var(--bg); }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-ghost { background: transparent; border: 1px solid var(--border); color: var(--text-dim); }
    .btn-sm { padding: 8px 14px; font-size: 12px; flex: 0; }

    .card-list { padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }

    .listing-card {
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 16px;
      transition: all 0.2s; position: relative; overflow: hidden;
    }
    .listing-card.rating-VERY_GOOD { border-left: 3px solid var(--green); background: var(--green-bg); }
    .listing-card.rating-GOOD { border-left: 3px solid var(--blue); }
    .listing-card.rating-FAIR { border-left: 3px solid var(--amber); }
    .listing-card.rating-INCREASED, .listing-card.rating-HIGH { border-left: 3px solid var(--red); background: var(--red-bg); }

    .card-image {
      width: 100%; height: 180px; border-radius: var(--radius-sm);
      background: var(--bg-input); margin-bottom: 12px;
      object-fit: cover; display: block;
    }
    .card-image-placeholder {
      width: 100%; height: 180px; border-radius: var(--radius-sm);
      background: var(--bg-input); margin-bottom: 12px;
      display: grid; place-items: center; color: var(--text-muted); font-size: 32px;
    }

    .card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
    .card-title { font-size: 15px; font-weight: 600; line-height: 1.3; flex: 1; }
    .card-price {
      font-family: var(--mono); font-size: 17px; font-weight: 700;
      color: var(--accent); white-space: nowrap;
    }
    .card-price-vb {
      font-size: 10px; color: var(--text-muted); display: block; text-align: right;
    }

    .card-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
    .tag {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 4px 10px; border-radius: 8px;
      background: rgba(255,255,255,0.05); border: 1px solid var(--border);
      font-size: 12px; color: var(--text-dim); white-space: nowrap;
    }
    .tag .icon { font-size: 13px; }
    .tag-green { color: var(--green); border-color: rgba(52,211,153,0.3); }
    .tag-amber { color: var(--amber); border-color: rgba(251,191,36,0.3); }
    .tag-blue { color: var(--blue); border-color: rgba(56,189,248,0.3); }
    .tag-red { color: var(--red); border-color: rgba(251,113,133,0.3); }
    .tag-warn { background: rgba(251,113,133,0.12); color: var(--red); border-color: rgba(251,113,133,0.3); font-weight: 600; }

    .fav-btn {
      background: none; border: none; cursor: pointer;
      font-size: 22px; padding: 4px; transition: transform 0.2s; line-height: 1;
    }
    .fav-btn:active { transform: scale(1.3); }
    .fav-btn.saved { animation: favPop 0.3s ease; }
    @keyframes favPop { 0% { transform: scale(1); } 50% { transform: scale(1.4); } 100% { transform: scale(1); } }

    .fav-status {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.3px;
    }
    .fav-status.s-interessant { background: rgba(56,189,248,0.15); color: var(--blue); }
    .fav-status.s-anrufen { background: rgba(251,191,36,0.15); color: var(--amber); }
    .fav-status.s-besichtigt { background: rgba(232,121,249,0.15); color: var(--fuchsia); }
    .fav-status.s-gekauft { background: rgba(52,211,153,0.2); color: var(--green); }
    .fav-status.s-abgelehnt { background: rgba(251,113,133,0.12); color: var(--red); }

    .fav-tabs {
      display: flex; gap: 6px; padding: 0 16px; margin: 20px 0 12px;
      overflow-x: auto; -webkit-overflow-scrolling: touch;
    }
    .fav-tab {
      flex-shrink: 0; padding: 6px 14px; border-radius: 20px;
      background: var(--bg-card); border: 1px solid var(--border);
      color: var(--text-muted); font-size: 12px; font-weight: 600;
      cursor: pointer; transition: all 0.15s; white-space: nowrap;
      font-family: var(--font);
    }
    .fav-tab:active { transform: scale(0.95); }
    .fav-tab.active { background: var(--accent); color: var(--bg); border-color: var(--accent); }

    .card-actions {
      display: flex; gap: 8px; margin-top: 12px; padding-top: 12px;
      border-top: 1px solid var(--border);
    }
    .card-btn {
      flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px;
      padding: 10px; border-radius: var(--radius-sm);
      background: rgba(255,255,255,0.04); border: 1px solid var(--border);
      color: var(--text-dim); font-size: 12px; font-weight: 600;
      text-decoration: none; cursor: pointer; transition: all 0.15s;
      font-family: var(--font);
    }
    .card-btn:active { transform: scale(0.96); }
    .card-btn:hover { background: rgba(255,255,255,0.08); color: var(--text); }
    .card-btn-accent { color: var(--accent); border-color: rgba(251,146,60,0.3); }

    .pagination {
      display: flex; align-items: center; justify-content: center; gap: 12px;
      padding: 16px; margin-bottom: 20px;
    }
    .page-btn {
      padding: 10px 20px; border-radius: var(--radius-sm);
      background: var(--bg-card); border: 1px solid var(--border);
      color: var(--text); font-size: 14px; font-weight: 500;
      text-decoration: none; transition: all 0.15s;
    }
    .page-btn.disabled { opacity: 0.3; pointer-events: none; }
    .page-info { font-size: 13px; color: var(--text-dim); }

    .bottom-nav {
      position: fixed; bottom: 0; left: 0; right: 0; z-index: 50;
      padding: 8px 16px calc(var(--safe-bottom) + 8px);
      background: rgba(15,23,42,0.92);
      backdrop-filter: blur(20px) saturate(1.4);
      -webkit-backdrop-filter: blur(20px) saturate(1.4);
      border-top: 1px solid var(--border);
      display: flex; justify-content: space-around; gap: 4px;
      /* iOS-Fix: fixed + backdrop-filter löst sich sonst beim Scrollen/Tastatur und
         bleibt mitten im Screen kleben. Eigene GPU-Ebene erzwingen stabilisiert es. */
      transform: translateZ(0);
      -webkit-transform: translateZ(0);
      will-change: transform;
      -webkit-backface-visibility: hidden;
      backface-visibility: hidden;
    }
    .nav-item {
      display: flex; flex-direction: column; align-items: center; gap: 2px;
      padding: 6px 12px; border-radius: 10px;
      color: var(--text-muted); font-size: 10px; font-weight: 600;
      cursor: pointer; transition: all 0.15s;
      background: none; border: none; font-family: var(--font); text-decoration: none;
    }
    .nav-item.active { color: var(--accent); }
    .nav-icon { font-size: 22px; }

    .modal-overlay {
      display: none; position: fixed; inset: 0; z-index: 100;
      background: rgba(0,0,0,0.6);
      backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
    }
    .modal-overlay.open { display: flex; align-items: flex-end; justify-content: center; animation: fadeIn 0.2s; }
    @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
    .modal-sheet {
      width: 100%; max-width: 560px; max-height: 85dvh;
      background: var(--bg-sheet); border-radius: var(--radius) var(--radius) 0 0;
      overflow: hidden; display: flex; flex-direction: column;
      animation: slideUp 0.3s cubic-bezier(0.32, 0.72, 0, 1);
      padding-bottom: var(--safe-bottom);
    }
    @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
    .modal-handle { width: 36px; height: 4px; border-radius: 2px; background: var(--border-light); margin: 10px auto; }
    .modal-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 0 20px 16px; border-bottom: 1px solid var(--border);
    }
    .modal-title { font-size: 17px; font-weight: 700; }
    .modal-close {
      width: 32px; height: 32px; border-radius: 50%;
      background: var(--bg-card); border: 1px solid var(--border);
      color: var(--text-dim); font-size: 18px;
      display: grid; place-items: center; cursor: pointer;
    }
    .modal-body { flex: 1; overflow-y: auto; padding: 20px; }
    .modal-footer { padding: 16px 20px; border-top: 1px solid var(--border); display: flex; gap: 10px; }

    .push-item { padding: 14px; border-radius: var(--radius-sm); background: var(--bg-card); border: 1px solid var(--border); margin-bottom: 10px; }
    .push-item-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
    .push-item-val { font-size: 13px; color: var(--text-dim); word-break: break-all; }

    .toast {
      position: fixed; top: calc(var(--safe-top) + 70px); left: 50%; transform: translateX(-50%);
      padding: 10px 20px; border-radius: 12px;
      font-size: 13px; font-weight: 600; z-index: 200;
      animation: toastIn 0.3s ease, toastOut 0.3s ease 2.5s forwards;
      pointer-events: none;
    }
    .toast-success { background: var(--green); color: var(--bg); }
    .toast-info { background: var(--accent); color: var(--bg); }
    .toast-error { background: var(--red); color: #fff; }
    @keyframes toastIn { from { opacity:0; transform: translateX(-50%) translateY(-12px); } to { opacity:1; transform: translateX(-50%) translateY(0); } }
    @keyframes toastOut { to { opacity: 0; transform: translateX(-50%) translateY(-12px); } }

    .empty-state { text-align: center; padding: 48px 24px; color: var(--text-muted); }
    .empty-state .icon { font-size: 48px; margin-bottom: 12px; }
    .empty-state .msg { font-size: 15px; font-weight: 500; }

    .ios-hint {
      margin: 12px 16px; padding: 12px 16px;
      border-radius: var(--radius-sm);
      background: rgba(251,146,60,0.1); border: 1px solid rgba(251,146,60,0.2);
      font-size: 12px; color: var(--accent);
    }


    /* Price Rating Info Icon */
    .price-info-btn {
      background: none; border: none; padding: 0 4px;
      color: var(--text-dim); cursor: pointer; font-size: 14px;
      vertical-align: middle; line-height: 1;
    }
    .price-info-btn:hover { color: var(--accent); }
    .price-info-btn:active { transform: scale(0.9); }

    /* Price Rating Modal */
    .price-modal-overlay {
      display: none; position: fixed; inset: 0; z-index: 110;
      background: rgba(0,0,0,0.6);
      backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
    }
    .price-modal-overlay.open { display: flex; align-items: flex-end; justify-content: center; animation: fadeIn 0.2s; }

    /* Marktwert-Modal */
    .mp-modal-sheet { max-height: 90dvh; display: flex; flex-direction: column; }
    .mp-loading { padding: 60px 20px; text-align: center; color: var(--text-dim); }
    .mp-loading .spin { font-size: 32px; animation: mpSpin 1s linear infinite; display: inline-block; }
    @keyframes mpSpin { to { transform: rotate(360deg); } }
    .mp-summary {
      padding: 14px; background: var(--bg-card); border: 1px solid var(--border);
      border-radius: var(--radius-sm); margin-bottom: 14px;
    }
    .mp-row { display: flex; justify-content: space-between; align-items: baseline; padding: 6px 0; }
    .mp-row.divider { border-top: 1px solid var(--border); margin-top: 4px; padding-top: 10px; }
    .mp-row .lbl { font-size: 13px; color: var(--text-dim); }
    .mp-row .val { font-family: var(--mono); font-weight: 600; font-size: 14px; }
    .mp-row .val.your { color: var(--accent); font-size: 18px; }
    .mp-row .val.good { color: var(--green); }
    .mp-row .val.bad { color: var(--red); }
    .mp-assessment {
      padding: 14px; border-radius: var(--radius-sm); text-align: center;
      font-weight: 700; font-size: 14px; margin-bottom: 14px;
    }
    .mp-assessment.deal { background: var(--green-bg); color: var(--green); border: 1px solid rgba(52,211,153,0.3); }
    .mp-assessment.under { background: rgba(56,189,248,0.1); color: var(--blue); border: 1px solid rgba(56,189,248,0.3); }
    .mp-assessment.fair { background: rgba(251,191,36,0.1); color: var(--amber); border: 1px solid rgba(251,191,36,0.3); }
    .mp-assessment.over { background: var(--red-bg); color: var(--red); border: 1px solid rgba(251,113,133,0.3); }
    .mp-stats {
      display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px;
      padding: 12px; background: rgba(255,255,255,0.03); border-radius: var(--radius-sm); margin-bottom: 14px;
    }
    .mp-stat { text-align: center; }
    .mp-stat .num { font-family: var(--mono); font-size: 16px; font-weight: 700; color: var(--text); }
    .mp-stat .lbl { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.4px; }
    .mp-comp-title { font-size: 12px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.4px; margin: 8px 0; }
    .mp-comp { padding: 10px 12px; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-sm); margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; gap: 10px; text-decoration: none; }
    .mp-comp .info { flex: 1; min-width: 0; }
    .mp-comp .title { font-size: 13px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .mp-comp .meta { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
    .mp-comp .price { font-family: var(--mono); font-weight: 700; font-size: 14px; color: var(--accent); white-space: nowrap; }
    .mp-filters { font-size: 11px; color: var(--text-muted); padding: 10px; background: rgba(255,255,255,0.03); border-radius: var(--radius-sm); margin-top: 12px; line-height: 1.6; }

      @media (min-width: 768px) { .price-modal-overlay.open { align-items: center; } }
    .price-modal-sheet {
      width: 100%; max-width: 520px;
      background: var(--bg-sheet); border-radius: var(--radius) var(--radius) 0 0;
      padding: 20px 20px calc(var(--safe-bottom) + 20px);
      animation: slideUp 0.3s cubic-bezier(0.32, 0.72, 0, 1);
    }
    @media (min-width: 768px) { .price-modal-sheet { border-radius: var(--radius); margin-bottom: 20px; } }
    .price-modal-handle { width: 36px; height: 4px; border-radius: 2px; background: var(--border-light); margin: 0 auto 14px; }
    .price-modal-title { font-size: 17px; font-weight: 700; margin-bottom: 6px; text-align: center; }
    .price-modal-sub { font-size: 12px; color: var(--text-muted); text-align: center; margin-bottom: 18px; }

    .price-modal-summary {
      display: flex; justify-content: space-between; align-items: baseline;
      padding: 12px 14px; background: var(--bg-card);
      border: 1px solid var(--border); border-radius: var(--radius-sm);
      margin-bottom: 18px;
    }
    .price-modal-summary .lbl { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.4px; }
    .price-modal-summary .val { font-family: var(--mono); font-size: 18px; font-weight: 700; color: var(--accent); }

    /* Skala */
    .price-scale-wrap { position: relative; margin: 36px 0 8px; padding: 0 4px; }
    .price-scale {
      position: relative; height: 44px;
      border-radius: 8px; overflow: hidden;
      background: linear-gradient(to right,
        #34d399 0%, #34d399 20%,
        #38bdf8 20%, #38bdf8 40%,
        #fbbf24 40%, #fbbf24 60%,
        #fb923c 60%, #fb923c 80%,
        #fb7185 80%, #fb7185 100%);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.08);
    }
    .price-scale-segments {
      position: absolute; inset: 0; display: flex;
    }
    .price-scale-seg {
      flex: 1; border-right: 1px solid rgba(0,0,0,0.18);
      display: flex; align-items: center; justify-content: center;
      font-size: 9px; font-weight: 700; color: rgba(255,255,255,0.95);
      text-transform: uppercase; letter-spacing: 0.4px;
      text-shadow: 0 1px 2px rgba(0,0,0,0.25);
    }
    .price-scale-seg:last-child { border-right: none; }

    /* Marker (Preis-Position auf Skala) */
    .price-scale-marker {
      position: absolute; top: -32px; transform: translateX(-50%);
      font-size: 12px; font-weight: 700; color: var(--text);
      white-space: nowrap;
      background: var(--bg); padding: 4px 10px; border-radius: 8px;
      border: 2px solid var(--accent); z-index: 2;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    }
    .price-scale-marker::after {
      content: ''; position: absolute; left: 50%; transform: translateX(-50%);
      bottom: -8px; width: 0; height: 0;
      border-left: 6px solid transparent;
      border-right: 6px solid transparent;
      border-top: 7px solid var(--accent);
    }

    /* Schwellen-Linien auf der Skala */
    .price-scale-tick {
      position: absolute; top: -4px; bottom: -4px;
      width: 1px; background: rgba(255,255,255,0.4);
      pointer-events: none;
    }

    /* Schwellenwerte unter der Skala */
    .price-scale-thresholds {
      position: relative; margin-top: 6px; height: 18px;
      font-family: var(--mono); font-size: 10px; color: var(--text-muted);
    }
    .price-scale-thresh-label {
      position: absolute; top: 0; transform: translateX(-50%);
      white-space: nowrap;
    }

    .price-modal-info {
      margin-top: 20px; padding: 12px 14px;
      background: rgba(56,189,248,0.06); border: 1px solid rgba(56,189,248,0.18);
      border-radius: var(--radius-sm); font-size: 12px; color: var(--text-dim); line-height: 1.5;
    }
    .price-modal-close {
      width: 100%; margin-top: 16px; padding: 12px;
      background: var(--accent); color: var(--bg);
      border: none; border-radius: var(--radius-sm);
      font-weight: 700; font-size: 14px; cursor: pointer;
      font-family: var(--font);
    }
    .price-modal-close:active { transform: scale(0.98); }

      @media (min-width: 768px) {
      .card-list { max-width: 680px; margin: 0 auto; }
      .stats-bar { justify-content: center; }
      body { padding-bottom: 0; }
      .bottom-nav { display: none; }
    }
  </style>
</head>
<body>

<header class="app-header">
  <div class="header-row">
    <div class="logo">
      <div class="logo-icon">M</div>
      <div>
        <div class="logo-text">{{ app_title }}</div>
        <div class="logo-sub">Mobile.de Scanner</div>
      </div>
    </div>
    <div class="header-actions">
      <button class="icon-btn" id="pushBtn" title="Push-Abo">🔔</button>
      <button class="icon-btn" id="pushManageBtn" title="Abos verwalten">⚙️</button>
    </div>
  </div>
</header>

<div class="stats-bar">
  <div class="stat-chip"><span class="num">{{ total }}</span> Treffer</div>
  <div class="stat-chip">Seite <span class="num">{{ page }}</span></div>
  <div class="stat-chip" id="dealCount">🔥 —</div>
  <div class="stat-chip" id="favCount">⭐ —</div>
  <div class="stat-chip" id="lastSyncChip">⏳ Sync…</div>
</div>

<div class="fav-tabs" id="favTabs">
  <button class="fav-tab active" data-fav-filter="deals">🔥 HOT DEALs</button>
  <button class="fav-tab" data-fav-filter="all">Alle</button>
  <button class="fav-tab" data-fav-filter="favs">⭐ Favoriten</button>
  <button class="fav-tab" data-fav-filter="interessant">Interessant</button>
  <button class="fav-tab" data-fav-filter="anrufen">Anrufen</button>
  <button class="fav-tab" data-fav-filter="besichtigt">Besichtigt</button>
  <button class="fav-tab" data-fav-filter="gekauft">Gekauft</button>
</div>

<div class="filter-toggle" onclick="toggleFilters()">
  <span>🔍</span>
  <span>Filter & Suche</span>
  {% set ac = 0 %}
  {% if params.q %}{% set ac = ac + 1 %}{% endif %}
  {% if params.price_max %}{% set ac = ac + 1 %}{% endif %}
  {% if params.km_max %}{% set ac = ac + 1 %}{% endif %}
  {% if params.ez_min %}{% set ac = ac + 1 %}{% endif %}
  {% if params.fuel %}{% set ac = ac + 1 %}{% endif %}
  <span class="count">{{ ac }}</span>
</div>

<form method="get" id="filterForm">
<div class="filter-sheet" id="filterSheet">
  <div class="filter-grid">
    <div class="full">
      <label class="field-label">Suche im Titel</label>
      <input class="field-input" name="q" value="{{ params.q }}" placeholder="z.B. Polo, Yaris…">
    </div>
    <div>
      <label class="field-label">Preis min €</label>
      <input class="field-input" type="number" name="price_min" value="{{ params.price_min }}" min="0">
    </div>
    <div>
      <label class="field-label">Preis max €</label>
      <input class="field-input" type="number" name="price_max" value="{{ params.price_max }}" min="0">
    </div>
    <div>
      <label class="field-label">EZ min</label>
      <input class="field-input" type="number" name="ez_min" value="{{ params.ez_min }}">
    </div>
    <div>
      <label class="field-label">EZ max</label>
      <input class="field-input" type="number" name="ez_max" value="{{ params.ez_max }}">
    </div>
    <div>
      <label class="field-label">km max</label>
      <input class="field-input" type="number" name="km_max" value="{{ params.km_max }}" min="0">
    </div>
    <div>
      <label class="field-label">PLZ Prefix</label>
      <input class="field-input" name="postal_prefix" value="{{ params.postal_prefix }}" placeholder="85">
    </div>
    <div>
      <label class="field-label">Stadt</label>
      <input class="field-input" name="city" value="{{ params.city }}" placeholder="München">
    </div>
    <div>
      <label class="field-label">Verkäufer</label>
      <select name="seller_type" class="field-input">
        <option value="" {% if not params.seller_type %}selected{% endif %}>Alle</option>
        <option value="private" {% if params.seller_type=='private' %}selected{% endif %}>Privat</option>
        <option value="dealer" {% if params.seller_type=='dealer' %}selected{% endif %}>Händler</option>
      </select>
    </div>
    <div>
      <label class="field-label">Kraftstoff</label>
      <select name="fuel" class="field-input">
        <option value="">Alle</option>
        <option value="Benzin" {% if params.fuel=='Benzin' %}selected{% endif %}>Benzin</option>
        <option value="Diesel" {% if params.fuel=='Diesel' %}selected{% endif %}>Diesel</option>
        <option value="Hybrid" {% if params.fuel=='Hybrid' %}selected{% endif %}>Hybrid</option>
        <option value="Elektro" {% if params.fuel=='Elektro' %}selected{% endif %}>Elektro</option>
      </select>
    </div>
    <div>
      <label class="field-label">Letzte Tage</label>
      <input class="field-input" type="number" name="posted_days" value="{{ params.posted_days }}" min="0">
    </div>
    <div>
      <label class="field-label">Nur Unfallfrei</label>
      <select name="unfallfrei" class="field-input">
        <option value="">Egal</option>
        <option value="1" {% if params.unfallfrei=='1' %}selected{% endif %}>Ja</option>
      </select>
    </div>
    <div class="full">
      <label class="field-label">Sortierung</label>
      <select name="sort" class="field-input">
        <option value="posted_desc" {% if params.sort=='posted_desc' %}selected{% endif %}>Neueste zuerst</option>
        <option value="price_asc"   {% if params.sort=='price_asc'   %}selected{% endif %}>Preis ↑</option>
        <option value="price_desc"  {% if params.sort=='price_desc'  %}selected{% endif %}>Preis ↓</option>
        <option value="km_asc"      {% if params.sort=='km_asc'      %}selected{% endif %}>Kilometer ↑</option>
        <option value="seen_desc"   {% if params.sort=='seen_desc'   %}selected{% endif %}>Zuletzt gesehen</option>
      </select>
    </div>
  </div>
  <div class="filter-actions">
    <a href="/mobile/" class="btn btn-ghost">Zurücksetzen</a>
    <button type="submit" class="btn btn-primary" id="applyFiltersBtn">🔄 Anwenden</button>
  </div>
</div>
</form>

<div id="iosHint" class="ios-hint" style="display:none">
  💡 Für Push-Benachrichtigungen: Seite zum Home-Bildschirm hinzufügen
</div>

<div class="card-list" id="cardList">
  {% for r in rows %}
  {% include '_card.html' ignore missing %}
  {% set _img = r['image_url'] %}
  <div class="listing-card {% if r['price_rating'] %}rating-{{ r['price_rating']|replace(' ','_')|upper }}{% endif %}"
       data-row-id="{{ r['id'] }}"
       data-price-eur="{{ r['price_eur'] or '' }}"
     data-price-rating-label="{{ (r['price_rating_label'] or r['price_rating'] or '')|e }}"
     data-price-thresholds="{{ r['price_thresholds_json'] or '' }}"
     data-price-offset="{{ r['price_offset'] if r['price_offset'] is not none else '' }}">

    {% if _img %}
    <img class="card-image" src="{{ _img }}" alt="" loading="lazy"
         onerror="this.outerHTML='<div class=card-image-placeholder>🚗</div>'">
    {% else %}
    <div class="card-image-placeholder">🚗</div>
    {% endif %}

    <div class="card-top">
      <button class="fav-btn" data-fav-id="{{ r['id'] }}" title="Favorit">🤍</button>
      <div class="card-title">{{ r['title'] or '—' }}</div>
      <div>
        <div class="card-price">
          {% if r['price_eur'] %}{{ '{:,}'.format(r['price_eur']).replace(',', '.') }} €{% else %}—{% endif %}
        </div>
        {% if r['price_negotiable'] %}<span class="card-price-vb">VB</span>{% endif %}
      </div>
    </div>

    <div class="fav-status-wrap" data-favstatus-id="{{ r['id'] }}" style="display:none; margin-top:6px;"></div>

    <div class="card-meta">
      {% if r['price_rating'] %}
        {% set pr = r['price_rating'] %}
        {% set has_detail = r['price_thresholds_json'] %}
        {% if 'Sehr guter' in pr or 'VERY_GOOD' in pr %}
          <span class="tag tag-green">💎 {{ pr }}{% if has_detail %}<button class="price-info-btn" data-price-info data-id="{{ r['id'] }}">ⓘ</button>{% endif %}</span>
        {% elif 'Guter' in pr or 'GOOD' in pr %}
          <span class="tag tag-blue">✓ {{ pr }}{% if has_detail %}<button class="price-info-btn" data-price-info data-id="{{ r['id'] }}">ⓘ</button>{% endif %}</span>
        {% elif 'Fair' in pr or 'FAIR' in pr %}
          <span class="tag tag-amber">{{ pr }}{% if has_detail %}<button class="price-info-btn" data-price-info data-id="{{ r['id'] }}">ⓘ</button>{% endif %}</span>
        {% else %}
          <span class="tag">{{ pr }}{% if has_detail %}<button class="price-info-btn" data-price-info data-id="{{ r['id'] }}">ⓘ</button>{% endif %}</span>
        {% endif %}
      {% endif %}
      {% if r['unfallfrei'] %}<span class="tag tag-green">✓ Unfallfrei</span>{% endif %}
      {% if r['km'] %}<span class="tag"><span class="icon">🛣</span> {{ '{:,}'.format(r['km']).replace(',', '.') }} km</span>{% endif %}
      {% if r['first_reg'] %}<span class="tag"><span class="icon">📅</span> EZ {{ r['first_reg'] }}</span>{% endif %}
      {% if r['power_ps'] %}<span class="tag">{{ r['power_ps'] }} PS</span>{% endif %}
      {% if r['fuel'] %}<span class="tag">{{ r['fuel'] }}</span>{% endif %}
      {% if r['gearbox'] %}<span class="tag">{{ r['gearbox'] }}</span>{% endif %}
      {% if r['hu_until'] %}<span class="tag"><span class="icon">🔧</span> HU {{ r['hu_until'] }}</span>{% endif %}
      {% if r['city'] %}<span class="tag"><span class="icon">📍</span> {{ r['city'] }}</span>{% endif %}
      {% if r['postal_code'] %}<span class="tag">{{ r['postal_code'] }}</span>{% endif %}
      {% if r['seller_type']=='private' %}<span class="tag tag-blue">👤 Privat</span>
      {% elif r['seller_type']=='dealer' %}<span class="tag">🏢 Händler</span>{% endif %}
      {% if r['online_since'] %}<span class="tag tag-green"><span class="icon">🕐</span> {{ r['online_since'] }}</span>{% endif %}
    </div>

    <div class="card-actions">
      <button class="card-btn" data-marketprice-id="{{ r['id'] }}" title="Marktwert berechnen">📊 Marktwert</button>
      <a class="card-btn card-btn-accent" href="{{ r['url'] }}" target="_blank" rel="noopener">↗ Mobile.de</a>
    </div>
  </div>
  {% endfor %}

  {% if not rows %}
  <div class="empty-state">
    <div class="icon">🚗</div>
    <div class="msg">Keine Treffer für diese Filter</div>
  </div>
  {% endif %}
</div>

<div class="pagination">
  <a class="page-btn {% if not has_prev %}disabled{% endif %}" href="{{ prev_url }}">← Zurück</a>
  <span class="page-info">Seite {{ page }}</span>
  <a class="page-btn {% if not has_next %}disabled{% endif %}" href="{{ next_url }}">Weiter →</a>
</div>

<nav class="bottom-nav">
  <button class="nav-item active" onclick="window.scrollTo({top:0,behavior:'smooth'})">
    <span class="nav-icon">🏠</span> Start
  </button>
  <button class="nav-item" onclick="toggleFilters()">
    <span class="nav-icon">🔍</span> Filter
  </button>
  <button class="nav-item" id="navPush">
    <span class="nav-icon">🔔</span> Push
  </button>
  <button class="nav-item" id="navManage">
    <span class="nav-icon">⚙️</span> Abos
  </button>
</nav>

<div class="modal-overlay" id="pushModal">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-header">
      <div>
        <div class="modal-title">Push-Abos</div>
        <div style="font-size:12px; color:var(--text-muted); margin-top:4px;">Mobile.de Benachrichtigungen</div>
      </div>
      <button class="modal-close" id="pushClose">✕</button>
    </div>
    <div class="modal-body" id="pushList"><div style="color:var(--text-muted)">Lade…</div></div>
    <div class="modal-footer">
      <span id="pushMsg" style="flex:1; font-size:12px; color:var(--text-dim);"></span>
      <button class="btn btn-ghost btn-sm" id="pushReloadBtn">Aktualisieren</button>
      <button class="btn btn-primary btn-sm" id="pushDoneBtn">Schließen</button>
    </div>
  </div>
</div>



<!-- Marktwert Modal -->
<div class="modal-overlay" id="mpModal">
  <div class="modal-sheet mp-modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-header">
      <div>
        <div class="modal-title">📊 Marktwert-Analyse</div>
        <div style="font-size:12px; color:var(--text-muted); margin-top:4px;" id="mpSubtitle">Lade Vergleichsdaten…</div>
      </div>
      <button class="modal-close" id="mpClose">✕</button>
    </div>
    <div class="modal-body" id="mpBody">
      <div class="mp-loading">
        <div class="spin">⏳</div>
        <div style="margin-top:14px">Suche ähnliche Inserate auf Mobile.de…</div>
        <div style="font-size:11px; margin-top:6px; color:var(--text-muted)">~5 Sekunden</div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost btn-sm" id="mpForceReload">🔄 Neu</button>
      <button class="btn btn-primary btn-sm" id="mpDone">Schließen</button>
    </div>
  </div>
</div>

<!-- Price Rating Modal -->
<div class="price-modal-overlay" id="priceModal">
  <div class="price-modal-sheet">
    <div class="price-modal-handle"></div>
    <div class="price-modal-title" id="priceModalTitle">mobile.de Preisbewertung</div>
    <div class="price-modal-sub">Im Vergleich zu ähnlichen Fahrzeugen<br>(bis zu 80 Fahrzeugmerkmale berücksichtigt)</div>

    <div class="price-modal-summary">
      <div><div class="lbl">Dieses Fahrzeug</div></div>
      <div class="val" id="priceModalVehicle">— €</div>
    </div>

    <div class="price-scale-wrap">
      <div class="price-scale-marker" id="priceModalMarker">— €</div>
      <div class="price-scale" id="priceModalScale">
        <div class="price-scale-segments">
          <div class="price-scale-seg">SEHR GUT</div>
          <div class="price-scale-seg">GUT</div>
          <div class="price-scale-seg">FAIR</div>
          <div class="price-scale-seg">ERHÖHT</div>
          <div class="price-scale-seg">HOCH</div>
        </div>
      </div>
      <div class="price-scale-thresholds" id="priceModalThresholds"></div>
    </div>

    <div class="price-modal-info">
      Berücksichtigt: Marke, Modell, EZ, KM, Leistung, Ausstattung.<br>
      Nicht berücksichtigt: Region, Reparaturen, Bilder, Beschreibung.
    </div>

    <button class="price-modal-close" id="priceModalClose">Schließen</button>
  </div>
</div>

<script>
document.addEventListener('DOMContentLoaded', () => {
  const REFRESH_MS = 60000;
  const VAPID_PUBLIC = "{{ VAPID_PUBLIC|default('')|safe }}".trim();

  function toast(msg, type='info') {
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  window.toggleFilters = function() {
    document.getElementById('filterSheet').classList.toggle('open');
  };

  // Filter "Anwenden" Button: triggert frischen Server-Scrape, dann Page-Reload
  const filterForm = document.getElementById('filterForm');
  const applyBtn = document.getElementById('applyFiltersBtn');
  if (filterForm && applyBtn) {
    filterForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      applyBtn.disabled = true;
      applyBtn.textContent = '🔍 Scrape läuft…';

      // 1) Versuch frischen Server-Scrape zu triggern
      try {
        const r = await fetch('/mobile/api/trigger_scrape', { method: 'POST', cache: 'no-store' });
        const j = await r.json().catch(() => ({}));
        if (r.status === 429) {
          toast(`⏳ Cooldown: noch ${j.wait_seconds || '?'}s warten`, 'info');
        } else if (j.ok) {
          toast('🔄 Scrape gestartet…', 'info');
          applyBtn.textContent = '⏳ Warte auf neue Daten…';
          // Kurz warten dass der Scrape Daten liefern kann
          await new Promise(r => setTimeout(r, 8000));
        }
      } catch (err) {
        console.warn('Trigger-Scrape fehlgeschlagen:', err);
      }

      // 2) Cache-Buster setzen + Form normal abschicken
      let buster = filterForm.querySelector('input[name="_t"]');
      if (!buster) {
        buster = document.createElement('input');
        buster.type = 'hidden';
        buster.name = '_t';
        filterForm.appendChild(buster);
      }
      buster.value = Date.now();
      applyBtn.textContent = '🔄 Lade Seite…';
      filterForm.submit();
    });
  }

  function fmtAgo(utcStr) {
    if (!utcStr) return '—';
    // Server liefert "YYYY-MM-DD HH:MM:SS" als UTC
    const t = new Date(utcStr.replace(' ', 'T') + 'Z').getTime();
    if (isNaN(t)) return '—';
    const diffSec = Math.round((Date.now() - t) / 1000);
    if (diffSec < 60) return 'gerade eben';
    const diffMin = Math.round(diffSec / 60);
    if (diffMin < 60) return 'vor ' + diffMin + ' Min';
    const diffH = Math.round(diffMin / 60);
    if (diffH < 24) return 'vor ' + diffH + ' h';
    return 'vor ' + Math.round(diffH / 24) + ' Tagen';
  }

  async function refreshSyncStatus() {
    try {
      const r = await fetch('/mobile/api/sync', { cache: 'no-store' });
      if (!r.ok) return;
      const j = await r.json();
      const chip = document.getElementById('lastSyncChip');
      if (chip && j.last_run_at) {
        chip.textContent = '🕐 ' + fmtAgo(j.last_run_at);
        chip.title = 'Letzter Server-Scrape: ' + j.last_run_at + ' UTC';
      } else if (chip) {
        chip.textContent = '⏳ noch kein Run';
      }
    } catch {}
  }

  async function reloadCards() {
    const params = new URLSearchParams(window.location.search);
    params.set('_', Date.now());
    // Anzahl Cards VOR dem Reload zählen
    const beforeIds = new Set(
      Array.from(document.querySelectorAll('.listing-card[data-row-id]'))
        .map(c => c.getAttribute('data-row-id'))
    );
    try {
      const r = await fetch('/mobile/api/table?' + params, { cache: 'no-store' });
      if (r.ok) {
        const html = await r.text();
        const list = document.getElementById('cardList');
        if (list) {
          list.innerHTML = html;
          bindFavButtons(list);
          bindPriceInfoButtons(list);
          bindMarketpriceButtons(list);
          applyFavUI();

          // Neue IDs erkennen + Toast anzeigen
          const afterIds = Array.from(list.querySelectorAll('.listing-card[data-row-id]'))
            .map(c => c.getAttribute('data-row-id'));
          const newOnes = afterIds.filter(id => !beforeIds.has(id));
          if (newOnes.length > 0 && beforeIds.size > 0) {
            toast('🆕 ' + newOnes.length + ' neue' + (newOnes.length === 1 ? 's Inserat' : ' Inserate'), 'success');
          }
        }
      }
    } catch {}
    refreshSyncStatus();
  }

  // --- Push ---
  function urlBase64ToUint8Array(b64) {
    const padding = '='.repeat((4 - b64.length % 4) % 4);
    const base64 = (b64 + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
    return outputArray;
  }

  function determineEnvironment() {
    const isIOS = (/iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream);
    const isStandalone = window.matchMedia('(display-mode: standalone)').matches || (navigator.standalone === true);
    return { isIOS, isStandalone };
  }

  async function ensureSW() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
    try {
      const reg = await navigator.serviceWorker.register('/mobile/sw.js', { scope: '/mobile/' });
      await navigator.serviceWorker.ready;
      return reg;
    } catch (err) { console.error('SW reg failed:', err); return null; }
  }

  async function subscribePush() {
    try {
      const reg = await ensureSW();
      if (!reg) { toast('SW/Push nicht verfügbar', 'error'); return; }
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') { toast('Benachrichtigungen nicht erlaubt', 'error'); return; }
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC)
      });
      const form = document.getElementById('filterForm');
      const fd = new FormData(form);
      const params = new URLSearchParams();
      for (const [k, v] of fd.entries()) {
        const vv = (v || '').toString().trim();
        if (vv !== '') params.set(k, vv);
      }
      params.delete('page'); params.delete('per_page'); params.delete('_');
      const filters = params.toString();
      const max_price = form.querySelector('input[name="price_max"]')?.value || null;
      const r = await fetch('/mobile/api/push/subscribe', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ subscription: sub.toJSON(), filters, max_price })
      });
      const j = await r.json();
      if (!j.ok) throw new Error('Server sagt nein');
      await reg.showNotification('Mobile.de Scanner aktiviert', { body: 'Push-Abo gespeichert.' });
      toast('Benachrichtigungen aktiviert ✓', 'success');
    } catch (e) { toast('Fehler: ' + e.message, 'error'); }
  }

  document.getElementById('pushBtn')?.addEventListener('click', subscribePush);
  document.getElementById('navPush')?.addEventListener('click', subscribePush);

  function currentFilterPayload() {
    const form = document.getElementById('filterForm');
    let filters = '', max_price = null;
    if (form) {
      const fd = new FormData(form);
      const params = new URLSearchParams();
      for (const [k, v] of fd.entries()) {
        const vv = (v || '').toString().trim();
        if (vv !== '') params.set(k, vv);
      }
      params.delete('page'); params.delete('per_page'); params.delete('_');
      filters = params.toString();
      max_price = form.querySelector('input[name="price_max"]')?.value || null;
    }
    return { filters, max_price };
  }

  // Heilt verlorene/rotierte Push-Abos: bei jedem App-Start (und Resume) das Abo
  // sicherstellen und idempotent am Server registrieren. iOS verwirft Push-Abos
  // alle paar Tage — ohne das hier blieb man bis zum manuellen Neu-Abo ohne Push.
  async function syncSubscription() {
    try {
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
      if (Notification.permission !== 'granted') return;  // nie ungefragt nachfragen
      const reg = await ensureSW();
      if (!reg) return;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        if (!VAPID_PUBLIC) return;
        // Abo von iOS verworfen → lautlos neu anlegen (kein Prompt, da bereits erlaubt)
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC)
        });
      }
      const { filters, max_price } = currentFilterPayload();
      await fetch('/mobile/api/push/subscribe', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ subscription: sub.toJSON(), filters, max_price })
      });
    } catch (e) { /* Auto-Sync bleibt geräuschlos */ }
  }
  syncSubscription();

  const env = determineEnvironment();
  if (env.isIOS && !env.isStandalone) {
    document.getElementById('iosHint').style.display = 'block';
  }

  // --- Push manage modal ---
  const pushModal = document.getElementById('pushModal');
  const pushList = document.getElementById('pushList');
  const pushMsg = document.getElementById('pushMsg');

  function openPushModal() { pushModal.classList.add('open'); loadPushSubs(); }
  function closePushModal() { pushModal.classList.remove('open'); }
  document.getElementById('pushManageBtn')?.addEventListener('click', openPushModal);
  document.getElementById('navManage')?.addEventListener('click', openPushModal);
  document.getElementById('pushClose')?.addEventListener('click', closePushModal);
  document.getElementById('pushDoneBtn')?.addEventListener('click', closePushModal);
  document.getElementById('pushReloadBtn')?.addEventListener('click', loadPushSubs);
  pushModal?.addEventListener('click', (e) => { if (e.target === pushModal) closePushModal(); });

  function esc(s) { return (s||'').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }

  function formatFilters(fs) {
    if (!fs) return 'Alle';
    let params;
    try { params = new URLSearchParams(fs); } catch { return fs; }
    const out = [];
    for (const [k,v] of params.entries()) { if((v||'').trim()) out.push(`${k}: ${v}`); }
    return out.length ? out.join(' · ') : 'Alle';
  }

  async function loadPushSubs() {
    pushList.innerHTML = '<div style="color:var(--text-muted)">Lade…</div>';
    try {
      const r = await fetch('/mobile/api/push/list', { cache: 'no-store' });
      const j = await r.json();
      const subs = j.subs || [];
      if (!subs.length) { pushList.innerHTML = '<div style="color:var(--text-muted)">Keine Abos vorhanden.</div>'; return; }
      pushList.innerHTML = subs.map(s => {
        const ep = (s.endpoint||'').slice(0,40)+'…';
        const f = formatFilters(s.filters);
        return `<div class="push-item">
          <div class="push-item-label">Endpoint</div>
          <div class="push-item-val">${esc(ep)}</div>
          <div style="margin-top:8px"><div class="push-item-label">Filter</div><div class="push-item-val">${esc(f)}</div></div>
          <div style="margin-top:8px;display:flex;justify-content:flex-end">
            <button class="btn btn-ghost btn-sm" style="color:var(--red);border-color:rgba(251,113,133,0.3)" data-push-del="${btoa(unescape(encodeURIComponent(s.endpoint||'')))}">Löschen</button>
          </div>
        </div>`;
      }).join('');
      pushList.querySelectorAll('[data-push-del]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const endpoint = decodeURIComponent(escape(atob(btn.getAttribute('data-push-del'))));
          btn.disabled = true; btn.textContent = '…';
          const rr = await fetch('/mobile/api/push/unsubscribe', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({endpoint}) });
          if (rr.ok) { pushMsg.textContent='Gelöscht.'; await loadPushSubs(); }
        });
      });
    } catch { pushList.innerHTML = '<div style="color:var(--red)">Netzwerkfehler.</div>'; }
  }

  // --- Favorites ---
  let _favCache = {};
  let _favFilter = 'deals';  // Default: nur Schnäppchen

  async function loadFavs() {
    try {
      const r = await fetch('/mobile/api/favs', { cache: 'no-store' });
      const j = await r.json();
      if (j.ok) { _favCache = {}; (j.favs||[]).forEach(f => _favCache[f.listing_id] = f); }
    } catch {}
  }

  function applyFavUI() {
    document.querySelectorAll('[data-fav-id]').forEach(btn => {
      const id = btn.getAttribute('data-fav-id');
      btn.textContent = _favCache[id] ? '⭐' : '🤍';
    });
    document.querySelectorAll('[data-favstatus-id]').forEach(el => {
      const id = el.getAttribute('data-favstatus-id');
      const fav = _favCache[id];
      if (fav && fav.status) {
        el.style.display = '';
        el.innerHTML = `<span class="fav-status s-${fav.status}">${fav.status}</span>`;
      } else {
        el.style.display = 'none'; el.innerHTML = '';
      }
    });
    document.getElementById('favCount').textContent = `⭐ ${Object.keys(_favCache).length}`;
    // HOT DEALs zählen (Ohne Bewertung ODER untere 30% bei Sehr-Gut)
    let dealCount = 0;
    document.querySelectorAll('.listing-card').forEach(card => {
      const rating = (card.dataset.priceRating || card.dataset.priceRatingLabel || '').toLowerCase();
      const isUnrated = !rating || rating.includes('ohne bewertung') || rating.includes('no_rating');
      const isGreat = rating.includes('sehr guter') || rating.includes('very_good');
      if (isUnrated) { dealCount++; return; }
      if (isGreat) {
        const price = parseFloat(card.dataset.priceEur || '0');
        let thr = [];
        try { thr = JSON.parse(card.dataset.priceThresholds || '[]'); } catch {}
        if (thr.length >= 2 && price > 0) {
          const cutoff = thr[0] + (thr[1] - thr[0]) * 0.30;
          if (price <= cutoff) dealCount++;
        } else {
          dealCount++;  // keine Thresholds → mitzählen
        }
      }
    });
    const dEl = document.getElementById('dealCount');
    if (dEl) dEl.textContent = `🔥 ${dealCount}`;
    applyFavFilter();
  }

  function applyFavFilter() {
    document.querySelectorAll('.listing-card').forEach(card => {
      const id = card.getAttribute('data-row-id');
      const fav = _favCache[id];
      if (_favFilter === 'all') {
        card.style.display = '';
      } else if (_favFilter === 'favs') {
        card.style.display = fav ? '' : 'none';
      } else if (_favFilter === 'deals') {
        // HOT DEALs: Ohne Bewertung ODER (Sehr guter Preis UND untere 30% der Range)
        const rating = (card.dataset.priceRating || card.dataset.priceRatingLabel || '').toLowerCase();
        const isGreat = rating.includes('sehr guter') || rating.includes('very_good');
        const isUnrated = !rating || rating.includes('ohne bewertung') || rating.includes('no_rating');
        
        let show = false;
        if (isUnrated) {
          show = true;
        } else if (isGreat) {
          // Prüfe ob Preis im unteren 30% der Sehr-Gut-Range liegt
          const price = parseFloat(card.dataset.priceEur || '0');
          let thresholds = [];
          try { thresholds = JSON.parse(card.dataset.priceThresholds || '[]'); } catch {}
          if (thresholds.length >= 2 && price > 0) {
            const cutoff = thresholds[0] + (thresholds[1] - thresholds[0]) * 0.30;
            show = price <= cutoff;
          } else {
            // Falls keine Thresholds → trotzdem durchlassen
            show = true;
          }
        }
        card.style.display = show ? '' : 'none';
      } else {
        card.style.display = (fav && fav.status === _favFilter) ? '' : 'none';
      }
    });
  }

  async function toggleFav(id) {
    const fav = _favCache[id];
    if (fav) {
      const cycle = ['interessant','anrufen','besichtigt','gekauft','abgelehnt'];
      const idx = cycle.indexOf(fav.status);
      if (idx >= 0 && idx < cycle.length - 1) {
        const next = cycle[idx + 1];
        await fetch('/mobile/api/fav', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id, status: next}) });
        _favCache[id] = {...fav, status: next};
      } else {
        await fetch('/mobile/api/fav', { method:'DELETE', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id}) });
        delete _favCache[id];
      }
    } else {
      await fetch('/mobile/api/fav', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id, status:'interessant'}) });
      _favCache[id] = {listing_id: id, status:'interessant', note:''};
    }
    applyFavUI();
  }

  function bindFavButtons(scope) {
    (scope || document).querySelectorAll('[data-fav-id]').forEach(btn => {
      if (btn.dataset.favBound === '1') return;
      btn.dataset.favBound = '1';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        btn.classList.add('saved');
        setTimeout(() => btn.classList.remove('saved'), 300);
        toggleFav(btn.getAttribute('data-fav-id'));
      });
    });
  }

  async function initFavorites() {
    await loadFavs();
    applyFavUI();
    bindFavButtons();
    document.querySelectorAll('[data-fav-filter]').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('[data-fav-filter]').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        _favFilter = tab.getAttribute('data-fav-filter');
        applyFavFilter();
      });
    });
  }


  // --- Price Rating Modal ---
  const priceModal = document.getElementById('priceModal');
  const priceModalMarker = document.getElementById('priceModalMarker');
  const priceModalVehicle = document.getElementById('priceModalVehicle');
  const priceModalThresholds = document.getElementById('priceModalThresholds');
  const priceModalTitle = document.getElementById('priceModalTitle');

  function closePriceModal() { priceModal.classList.remove('open'); }
  document.getElementById('priceModalClose')?.addEventListener('click', closePriceModal);
  priceModal?.addEventListener('click', (e) => { if (e.target === priceModal) closePriceModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePriceModal(); });

  function fmtEur(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return Number(n).toLocaleString('de-DE') + ' €';
  }

  function openPriceModal(card) {
    const price = parseInt(card.dataset.priceEur || '0');
    const ratingLabel = card.dataset.priceRatingLabel || 'Preisbewertung';
    let thresholds = [];
    try { thresholds = JSON.parse(card.dataset.priceThresholds || '[]'); } catch {}
    const title = card.querySelector('.card-title')?.textContent?.trim() || 'Fahrzeug';

    priceModalTitle.textContent = ratingLabel + (title ? ' — ' + title : '');
    priceModalVehicle.textContent = fmtEur(price);

    if (thresholds.length !== 6) {
      priceModalThresholds.innerHTML = '<div style="text-align:center;width:100%;color:var(--text-muted)">Keine Schwellenwerte verfügbar</div>';
      priceModalMarker.style.left = '50%';
      priceModalMarker.textContent = fmtEur(price);
      priceModal.classList.add('open');
      return;
    }

    // Mobile.de's Skala: 5 Segmente à 20% Breite, jedes Segment hat eigene €-Range.
    // Marker-Position muss INNERHALB seines Segments interpoliert werden.
    // thresholds: [t0, t1, t2, t3, t4, t5]
    //   SEHR GUT:  t0..t1   (Skala  0..20%)
    //   GUT:       t1..t2   (Skala 20..40%)
    //   FAIR:      t2..t3   (Skala 40..60%)
    //   ERHÖHT:    t3..t4   (Skala 60..80%)
    //   HOCH:      t4..t5   (Skala 80..100%)
    let markerPct;
    if (price <= thresholds[0]) {
      // Unterhalb der Skala: ganz links, am Anfang von SEHR GUT
      markerPct = 1;
    } else if (price >= thresholds[5]) {
      // Oberhalb der Skala: ganz rechts
      markerPct = 99;
    } else {
      // Finde das passende Segment und interpoliere INNERHALB des Segments
      let segIdx = 0;
      for (let i = 0; i < 5; i++) {
        if (price >= thresholds[i] && price <= thresholds[i+1]) {
          segIdx = i;
          break;
        }
      }
      const segStart = thresholds[segIdx];
      const segEnd = thresholds[segIdx + 1];
      const segPct = (price - segStart) / (segEnd - segStart);  // 0..1 innerhalb des Segments
      const segBaseLeft = segIdx * 20;        // 0, 20, 40, 60, 80
      markerPct = segBaseLeft + segPct * 20;  // exakte Position auf 0-100% Skala
    }
    markerPct = Math.max(1, Math.min(99, markerPct));
    priceModalMarker.style.left = markerPct + '%';
    priceModalMarker.textContent = fmtEur(price);

    // Threshold-Labels unter Skala an exakten Positionen
    // thresholds[0]=0%, thresholds[1]=20%, thresholds[2]=40%, thresholds[3]=60%, thresholds[4]=80%, thresholds[5]=100%
    const positions = [0, 20, 40, 60, 80, 100];
    priceModalThresholds.innerHTML = thresholds.map((t, i) => {
      const pos = positions[i];
      let style = `left: ${pos}%;`;
      if (i === 0) style = 'left: 0%; transform: translateX(0);';
      if (i === 5) style = 'right: 0%; left: auto; transform: translateX(0);';
      return `<span class="price-scale-thresh-label" style="${style}">${fmtEur(t)}</span>`;
    }).join('');

    // Vertikale Trennlinien auf der Skala
    const scale = document.getElementById('priceModalScale');
    // Alte Ticks entfernen
    scale.querySelectorAll('.price-scale-tick').forEach(t => t.remove());
    // Neue Ticks bei 20/40/60/80%
    [20, 40, 60, 80].forEach(p => {
      const tick = document.createElement('div');
      tick.className = 'price-scale-tick';
      tick.style.left = p + '%';
      scale.appendChild(tick);
    });

    priceModal.classList.add('open');
  }

  function bindPriceInfoButtons(scope) {
    (scope || document).querySelectorAll('[data-price-info]').forEach(btn => {
      if (btn.dataset.priceInfoBound === '1') return;
      btn.dataset.priceInfoBound = '1';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        const card = btn.closest('.listing-card');
        if (card) openPriceModal(card);
      });
    });
  }
  bindPriceInfoButtons();



  // --- Marktwert-Modal ---
  const mpModal = document.getElementById('mpModal');
  const mpBody = document.getElementById('mpBody');
  const mpSubtitle = document.getElementById('mpSubtitle');
  let _mpCurrentId = null;

  function closeMpModal() { mpModal.classList.remove('open'); _mpCurrentId = null; }
  document.getElementById('mpClose')?.addEventListener('click', closeMpModal);
  document.getElementById('mpDone')?.addEventListener('click', closeMpModal);
  mpModal?.addEventListener('click', (e) => { if (e.target === mpModal) closeMpModal(); });
  document.getElementById('mpForceReload')?.addEventListener('click', () => {
    if (_mpCurrentId) loadMarketprice(_mpCurrentId, true);
  });

  function fmtMpEur(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return Number(n).toLocaleString('de-DE') + ' €';
  }

  function renderMarketprice(data) {
    if (!data.ok) {
      mpBody.innerHTML = `<div class="mp-loading"><div style="font-size:32px">⚠️</div><div style="margin-top:14px">Fehler: ${data.error || 'unbekannt'}</div>${data.brand ? `<div style="font-size:12px;margin-top:6px;color:var(--text-muted)">Marke "${data.brand}" nicht in Codes-Liste gefunden</div>` : ''}</div>`;
      return;
    }

    const f = data.filters_applied || {};
    mpSubtitle.textContent = `${f.brand || ''} ${f.ez_year || ''}`.trim() + (data.from_cache ? ' (Cache)' : '');

    if (data.comparable_count < 2) {
      mpBody.innerHTML = `
        <div class="mp-summary">
          <div class="mp-row"><span class="lbl">Dein Preis</span><span class="val your">${fmtMpEur(data.your_price)}</span></div>
        </div>
        <div class="mp-loading">
          <div style="font-size:32px">🤷</div>
          <div style="margin-top:14px">Nur ${data.comparable_count} ähnliche Inserate gefunden</div>
          <div style="font-size:12px; color:var(--text-muted); margin-top:6px">Filter zu eng — kein Marktdurchschnitt möglich</div>
          <a href="${data.search_url}" target="_blank" style="color:var(--accent); font-size:12px; margin-top:14px; display:inline-block">↗ Auf Mobile.de öffnen</a>
        </div>`;
      return;
    }

    const assClass = data.diff_pct < -10 ? 'deal' : data.diff_pct < -3 ? 'under' : data.diff_pct < 5 ? 'fair' : 'over';
    const diffEur = data.diff_eur;
    const diffSign = diffEur > 0 ? '+' : '';
    const diffClass = diffEur < 0 ? 'good' : diffEur > 0 ? 'bad' : '';

    const compsHtml = (data.comparables || []).map(c => `
      <a class="mp-comp" href="${c.url}" target="_blank" rel="noopener">
        <div class="info">
          <div class="title">${(c.title || '—').replace(/</g,'&lt;')}</div>
          <div class="meta">${c.km ? Number(c.km).toLocaleString('de-DE') + ' km' : ''} ${c.city ? '• ' + c.city : ''}</div>
        </div>
        <div class="price">${fmtMpEur(c.price_eur)}</div>
      </a>
    `).join('');

    mpBody.innerHTML = `
      <div class="mp-summary">
        <div class="mp-row"><span class="lbl">Dein Preis</span><span class="val your">${fmtMpEur(data.your_price)}</span></div>
        <div class="mp-row divider"><span class="lbl">Marktdurchschnitt</span><span class="val">${fmtMpEur(data.avg_price)}</span></div>
        <div class="mp-row"><span class="lbl">Median</span><span class="val">${fmtMpEur(data.median_price)}</span></div>
        <div class="mp-row"><span class="lbl">Differenz</span><span class="val ${diffClass}">${diffSign}${fmtMpEur(diffEur)} (${data.diff_pct > 0 ? '+' : ''}${data.diff_pct}%)</span></div>
      </div>

      ${data.assessment ? `<div class="mp-assessment ${assClass}">${data.assessment}</div>` : ''}

      <div class="mp-stats">
        <div class="mp-stat"><div class="num">${fmtMpEur(data.min_price)}</div><div class="lbl">📉 Min</div></div>
        <div class="mp-stat"><div class="num">${fmtMpEur(data.max_price)}</div><div class="lbl">📈 Max</div></div>
        <div class="mp-stat"><div class="num">${data.private_count || 0}</div><div class="lbl">👤 Privat</div></div>
        <div class="mp-stat"><div class="num">${data.dealer_count || 0}</div><div class="lbl">🏢 Händler</div></div>
      </div>

      <div class="mp-comp-title">📋 Günstigste ${(data.comparables || []).length} von ${data.comparable_count}</div>
      ${compsHtml}

      <div class="mp-filters">
        <strong>Angewandte Filter:</strong><br>
        🚗 ${f.brand || ''} • EZ ${f.ez_year || '?'}<br>
        ${f.km_range ? `🛣️ ${f.km_range[0].toLocaleString('de-DE')} - ${f.km_range[1].toLocaleString('de-DE')} km` : ''}
        ${f.fuel ? '• ⛽ ' + f.fuel : ''}
        ${f.gearbox ? '• ⚙️ ' + f.gearbox : ''}
        ${f.power_range_ps ? `<br>💪 ${f.power_range_ps[0]}-${f.power_range_ps[1]} PS` : ''}
        ${((data.feature_filters_used_fe || []).length + (data.feature_filters_used_spc || []).length) > 0 ? `<br>✨ ${(data.feature_filters_used_fe || []).length + (data.feature_filters_used_spc || []).length} Ausstattungs-Filter: ${[...(data.feature_filters_used_fe || []), ...(data.feature_filters_used_spc || [])].slice(0, 4).join(", ")}` : ''}
        <br><a href="${data.search_url}" target="_blank" style="color:var(--accent)">↗ Suche auf Mobile.de öffnen</a>
      </div>
    `;
  }

  async function loadMarketprice(listingId, force) {
    _mpCurrentId = listingId;
    mpBody.innerHTML = `<div class="mp-loading"><div class="spin">⏳</div><div style="margin-top:14px">${force ? 'Berechne neu…' : 'Suche ähnliche Inserate auf Mobile.de…'}</div><div style="font-size:11px; margin-top:6px; color:var(--text-muted)">~5 Sekunden</div></div>`;
    mpSubtitle.textContent = 'Lade Vergleichsdaten…';
    try {
      const r = await fetch(`/mobile/api/marketprice/${listingId}${force ? '?force=1' : ''}`, { cache: 'no-store' });
      const data = await r.json();
      renderMarketprice(data);
    } catch (e) {
      mpBody.innerHTML = `<div class="mp-loading"><div style="font-size:32px">⚠️</div><div style="margin-top:14px">Netzwerkfehler: ${e.message}</div></div>`;
    }
  }

  function bindMarketpriceButtons(scope) {
    (scope || document).querySelectorAll('[data-marketprice-id]').forEach(btn => {
      if (btn.dataset.mpBound === '1') return;
      btn.dataset.mpBound = '1';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const id = btn.getAttribute('data-marketprice-id');
        mpModal.classList.add('open');
        loadMarketprice(id, false);
      });
    });
  }
  bindMarketpriceButtons();


  initFavorites();
  refreshSyncStatus();
  setInterval(refreshSyncStatus, 30000);     // Sync-Anzeige alle 30s aktualisieren
  setInterval(reloadCards, REFRESH_MS);

  // Auto-Refresh wenn App in den Vordergrund kommt (PWA, Tab-Switch, Lock-Screen)
  let _lastVisibleAt = Date.now();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      const wasAwaySec = Math.round((Date.now() - _lastVisibleAt) / 1000);
      // Erst nach mehr als 30 Sek Abwesenheit refreshen (nicht bei jedem Tab-Switch)
      if (wasAwaySec > 30) {
        console.log('[autoscan] App wieder sichtbar nach ' + wasAwaySec + 's → Refresh');
        reloadCards();
        refreshSyncStatus();
      }
      _lastVisibleAt = Date.now();
    } else {
      _lastVisibleAt = Date.now();
    }
  });

  // Auch bei Window-Focus refreshen (Desktop-Variante)
  window.addEventListener('focus', () => {
    refreshSyncStatus();
  });

  // Pull-to-Refresh feedback: bei manuellem Reload zeigen wir Toast
  window.addEventListener('pageshow', (e) => {
    if (e.persisted) {
      // Aus bfcache zurückgekommen → frische Daten holen + Push-Abo heilen
      reloadCards();
      refreshSyncStatus();
      syncSubscription();
    }
  });
});
</script>
</body>
</html>
"""

CARDS_TPL = r"""
{% for r in rows %}
<div class="listing-card {% if r['price_rating'] %}rating-{{ r['price_rating']|replace(' ','_')|upper }}{% endif %}"
     data-row-id="{{ r['id'] }}"
     data-price-eur="{{ r['price_eur'] or '' }}"
     data-price-rating-label="{{ (r['price_rating_label'] or r['price_rating'] or '')|e }}"
     data-price-thresholds="{{ r['price_thresholds_json'] or '' }}"
     data-price-offset="{{ r['price_offset'] if r['price_offset'] is not none else '' }}"
     data-price-rating="{{ (r['price_rating'] or '')|e }}">
  {% if r['image_url'] %}
  <img class="card-image" src="{{ r['image_url'] }}" alt="" loading="lazy"
       onerror="this.outerHTML='<div class=card-image-placeholder>🚗</div>'">
  {% else %}
  <div class="card-image-placeholder">🚗</div>
  {% endif %}
  <div class="card-top">
    <button class="fav-btn" data-fav-id="{{ r['id'] }}">🤍</button>
    <div class="card-title">{{ r['title'] or '—' }}</div>
    <div>
      <div class="card-price">
        {% if r['price_eur'] %}{{ '{:,}'.format(r['price_eur']).replace(',', '.') }} €{% else %}—{% endif %}
      </div>
      {% if r['price_negotiable'] %}<span class="card-price-vb">VB</span>{% endif %}
    </div>
  </div>
  <div class="fav-status-wrap" data-favstatus-id="{{ r['id'] }}" style="display:none; margin-top:6px;"></div>
  <div class="card-meta">
    {% if r['price_rating'] %}
      {% set pr = r['price_rating'] %}
      {% set has_detail = r['price_thresholds_json'] %}
      {% if 'Sehr guter' in pr or 'VERY_GOOD' in pr %}<span class="tag tag-green">💎 {{ pr }}{% if has_detail %}<button class="price-info-btn" data-price-info data-id="{{ r['id'] }}">ⓘ</button>{% endif %}</span>
      {% elif 'Guter' in pr or 'GOOD' in pr %}<span class="tag tag-blue">✓ {{ pr }}{% if has_detail %}<button class="price-info-btn" data-price-info data-id="{{ r['id'] }}">ⓘ</button>{% endif %}</span>
      {% elif 'Fair' in pr or 'FAIR' in pr %}<span class="tag tag-amber">{{ pr }}{% if has_detail %}<button class="price-info-btn" data-price-info data-id="{{ r['id'] }}">ⓘ</button>{% endif %}</span>
      {% else %}<span class="tag">{{ pr }}{% if has_detail %}<button class="price-info-btn" data-price-info data-id="{{ r['id'] }}">ⓘ</button>{% endif %}</span>
      {% endif %}
    {% endif %}
    {% if r['unfallfrei'] %}<span class="tag tag-green">✓ Unfallfrei</span>{% endif %}
    {% if r['km'] %}<span class="tag">🛣 {{ '{:,}'.format(r['km']).replace(',', '.') }} km</span>{% endif %}
    {% if r['first_reg'] %}<span class="tag">📅 EZ {{ r['first_reg'] }}</span>{% endif %}
    {% if r['power_ps'] %}<span class="tag">{{ r['power_ps'] }} PS</span>{% endif %}
    {% if r['fuel'] %}<span class="tag">{{ r['fuel'] }}</span>{% endif %}
    {% if r['gearbox'] %}<span class="tag">{{ r['gearbox'] }}</span>{% endif %}
    {% if r['hu_until'] %}<span class="tag">🔧 HU {{ r['hu_until'] }}</span>{% endif %}
    {% if r['city'] %}<span class="tag">📍 {{ r['city'] }}</span>{% endif %}
    {% if r['seller_type']=='private' %}<span class="tag tag-blue">👤 Privat</span>{% endif %}
    {% if r['online_since'] %}<span class="tag tag-green">🕐 {{ r['online_since'] }}</span>{% endif %}
  </div>
  <div class="card-actions">
    <button class="card-btn" data-marketprice-id="{{ r['id'] }}" title="Marktwert berechnen">📊 Marktwert</button>
    <a class="card-btn card-btn-accent" href="{{ r['url'] }}" target="_blank" rel="noopener">↗ Mobile.de</a>
  </div>
</div>
{% endfor %}
{% if not rows %}
<div class="empty-state"><div class="icon">🚗</div><div class="msg">Keine Treffer</div></div>
{% endif %}
"""

if __name__ == "__main__":
    print(f"[i] DB: {DB_PATH}")
    print(f"[i] Start auf 0.0.0.0:{WEB_PORT} (SCRIPT_NAME={SCRIPT_NAME})")
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
