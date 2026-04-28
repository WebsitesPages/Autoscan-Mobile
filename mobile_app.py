"""
Standalone Test-App für mobile.de Scraper UI
Läuft auf Port 5001 (parallel zur Hauptapp auf 5000)
"""
import os
import sqlite3
import json
from dotenv import load_dotenv
load_dotenv('/opt/mobile-test/.env')
from flask import Flask, request, render_template_string, redirect, url_for
import sys
sys.path.insert(0, '/opt/mobile-test')
from mobile_scrape import sync_srp, init_db

app = Flask(__name__)

# Subpath /mobile via Nginx
from werkzeug.middleware.proxy_fix import ProxyFix
class PrefixMiddleware:
    def __init__(self, app, prefix=''):
        self.app = app
        self.prefix = prefix
    def __call__(self, environ, start_response):
        if environ.get('HTTP_X_SCRIPT_NAME'):
            environ['SCRIPT_NAME'] = environ['HTTP_X_SCRIPT_NAME']
            path_info = environ['PATH_INFO']
            if path_info.startswith(environ['SCRIPT_NAME']):
                environ['PATH_INFO'] = path_info[len(environ['SCRIPT_NAME']):]
        return self.app(environ, start_response)

app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.wsgi_app = PrefixMiddleware(app.wsgi_app)
DB_PATH = "/opt/mobile-test/mobile.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Default Suchfilter (werden in der UI änderbar)
DEFAULT_FILTERS = {
    "price_min": "",
    "price_max": "9000",
    "km_max": "100000",
    "ez_min": "2012",
    "ez_max": "",
    "region": "Bayern",
    "radius": "100",
    "lat": "48.7904472",
    "lng": "11.4978895",
    "seller": "FSBO",  # FSBO = privat, DEALER = Händler, "" = beide
}

CONFIG_PATH = "/opt/mobile-test/filter_config.json"

def load_filters():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                return {**DEFAULT_FILTERS, **json.load(f)}
        except: pass
    return dict(DEFAULT_FILTERS)

def save_filters(f):
    with open(CONFIG_PATH, "w") as ff:
        json.dump(f, ff)

def build_srp_url(f):
    """Baut mobile.de Such-URL aus Filter-Dict."""
    params = ["cn=DE", "dam=false", "isSearchRequest=true", "s=Car", "vc=Car",
              "od=down", "sb=doc", "ref=srpHead"]
    if f.get("region"): params.append(f"gn={f['region']}")
    if f.get("radius"): params.append(f"rd={f['radius']}")
    if f.get("lat") and f.get("lng"):
        params.append(f"ll={f['lat']}%2C{f['lng']}")
    if f.get("seller"):
        params.append(f"st={f['seller']}")

    pmin = f.get("price_min", "")
    pmax = f.get("price_max", "")
    if pmin or pmax:
        params.append(f"p={pmin}%3A{pmax}")

    kmax = f.get("km_max", "")
    if kmax:
        params.append(f"ml=%3A{kmax}")

    ezmin = f.get("ez_min", "")
    ezmax = f.get("ez_max", "")
    if ezmin or ezmax:
        params.append(f"fr={ezmin}%3A{ezmax}")

    return "https://suchen.mobile.de/fahrzeuge/search.html?" + "&".join(params)

@app.route("/")
def index():
    init_db()
    q = request.args.get("q", "")
    price_min = request.args.get("price_min", "")
    price_max = request.args.get("price_max", "")
    km_max = request.args.get("km_max", "")
    ez_min = request.args.get("ez_min", "")
    sort = request.args.get("sort", "first_seen DESC")
    only_unfallfrei = request.args.get("only_unfallfrei") == "1"

    where = []
    args = []
    if q:
        where.append("(title LIKE ? OR description LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if price_min:
        where.append("price_eur >= ?"); args.append(int(price_min))
    if price_max:
        where.append("price_eur <= ?"); args.append(int(price_max))
    if km_max:
        where.append("km <= ?"); args.append(int(km_max))
    if ez_min:
        where.append("substr(first_reg, -4) >= ?"); args.append(ez_min)
    if only_unfallfrei:
        where.append("unfallfrei = 1")

    # Scrape-Filter (immer angewendet)
    sf = load_filters()
    if sf.get("price_max"):
        where.append("(price_eur IS NULL OR price_eur <= ?)"); args.append(int(sf["price_max"]))
    if sf.get("price_min"):
        where.append("(price_eur IS NULL OR price_eur >= ?)"); args.append(int(sf["price_min"]))
    if sf.get("km_max"):
        where.append("(km IS NULL OR km <= ?)"); args.append(int(sf["km_max"]))
    if sf.get("ez_min"):
        where.append("(first_reg IS NULL OR substr(first_reg, -4) >= ?)"); args.append(sf["ez_min"])
    if sf.get("ez_max"):
        where.append("(first_reg IS NULL OR substr(first_reg, -4) <= ?)"); args.append(sf["ez_max"])
    if sf.get("seller") == "FSBO":
        where.append("(seller_type IS NULL OR seller_type = 'private')")
    elif sf.get("seller") == "DEALER":
        where.append("(seller_type IS NULL OR seller_type = 'dealer')")

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sort_sql = {
        "first_seen DESC": "online_since_dt DESC, first_seen DESC",
        "price ASC": "price_eur ASC",
        "price DESC": "price_eur DESC",
        "km ASC": "km ASC",
        "ez DESC": "first_reg DESC",
    }.get(sort, "first_seen DESC")

    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) FROM mobile_listings {where_sql}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM mobile_listings {where_sql} ORDER BY {sort_sql} LIMIT 50", args
    ).fetchall()

    # Stats
    last_log = conn.execute(
        "SELECT scraped_at, action, cost FROM mobile_scrape_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    total_cost = conn.execute("SELECT IFNULL(SUM(cost),0) FROM mobile_scrape_log").fetchone()[0]
    conn.close()

    scrape_filters = load_filters()
    scrape_url = build_srp_url(scrape_filters)

    return render_template_string(TPL,
        rows=rows, total=total,
        params={'q':q,'price_min':price_min,'price_max':price_max,'km_max':km_max,'ez_min':ez_min,'sort':sort,'only_unfallfrei':only_unfallfrei},
        last_log=last_log, total_cost=total_cost,
        scrape_filters=scrape_filters, scrape_url=scrape_url,
    )

@app.post("/sync")
def sync():
    max_details = int(request.form.get("max_details", "5"))
    f = load_filters()
    srp_url = build_srp_url(f)
    result = sync_srp(srp_url, fetch_details=True, max_details=max_details)
    return redirect(url_for('index'))

@app.post("/save_filters")
def save_filters_route():
    f = {
        "price_min": request.form.get("price_min", "").strip(),
        "price_max": request.form.get("price_max", "").strip(),
        "km_max": request.form.get("km_max", "").strip(),
        "ez_min": request.form.get("ez_min", "").strip(),
        "ez_max": request.form.get("ez_max", "").strip(),
        "region": request.form.get("region", "Bayern").strip(),
        "radius": request.form.get("radius", "100").strip(),
        "lat": request.form.get("lat", "48.7904472").strip(),
        "lng": request.form.get("lng", "11.4978895").strip(),
        "seller": request.form.get("seller", "FSBO").strip(),
    }
    save_filters(f)
    return redirect(url_for('index'))

@app.get("/listing/<lid>")
def listing(lid):
    conn = get_db()
    row = conn.execute("SELECT * FROM mobile_listings WHERE id=?", (lid,)).fetchone()
    conn.close()
    if not row: return "Not found", 404
    feats = json.loads(row['features_json'] or '[]')
    return render_template_string(DETAIL_TPL, r=row, features=feats)

# ============================================================
# Templates
# ============================================================
TPL = r"""
<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Autoscan Mobile</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root {
  --bg:#0f172a; --bg-card:#1e293b; --bg-input:#1e293b; --bg-sheet:#162032;
  --border:#334155; --text:#f1f5f9; --text-dim:#94a3b8; --text-muted:#64748b;
  --accent:#fb923c; --accent-h:#fdba74;
  --green:#34d399; --green-bg:rgba(52,211,153,0.12);
  --red:#fb7185; --amber:#fbbf24;
  --radius:16px; --radius-sm:10px;
  --font:-apple-system,BlinkMacSystemFont,'DM Sans',sans-serif;
  --mono:'JetBrains Mono',monospace;
  --safe-top:env(safe-area-inset-top,0px);
  --safe-bottom:env(safe-area-inset-bottom,0px);
}
html{font-family:var(--font);background:var(--bg);color:var(--text);-webkit-text-size-adjust:100%}
body{min-height:100dvh;padding-bottom:calc(20px + var(--safe-bottom))}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:10px}

.app-header{position:sticky;top:0;z-index:50;padding:calc(var(--safe-top) + 12px) 16px 12px;
  background:rgba(15,23,42,0.85);backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);border-bottom:1px solid var(--border)}
.header-row{display:flex;align-items:center;justify-content:space-between;gap:12px}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{width:36px;height:36px;border-radius:10px;
  background:linear-gradient(135deg,#fb923c,#f59e0b);display:grid;place-items:center;
  font-weight:700;font-size:14px;color:#fff}
.logo-text{font-size:20px;font-weight:700;letter-spacing:-0.5px}
.logo-sub{font-size:11px;color:var(--text-dim);font-weight:400}
.icon-btn{width:38px;height:38px;border-radius:10px;background:var(--bg-card);
  border:1px solid var(--border);color:var(--text-dim);display:grid;place-items:center;
  cursor:pointer;font-size:18px;text-decoration:none}
.icon-btn:active{transform:scale(0.93)}

.stats-bar{display:flex;align-items:center;gap:12px;padding:10px 16px;overflow-x:auto}
.stat-chip{flex-shrink:0;padding:6px 14px;border-radius:20px;background:var(--bg-card);
  border:1px solid var(--border);font-size:13px;font-weight:500;white-space:nowrap;
  display:flex;align-items:center;gap:6px}
.stat-chip .num{color:var(--accent);font-family:var(--mono);font-weight:600}

.filter-toggle{display:flex;align-items:center;gap:8px;margin:0 16px;
  padding:12px 16px;border-radius:var(--radius);background:var(--bg-card);
  border:1px solid var(--border);color:var(--text);font-size:14px;font-weight:500;
  cursor:pointer}
.filter-sheet{display:none;margin:12px 16px 0;padding:20px;border-radius:var(--radius);
  background:var(--bg-sheet);border:1px solid var(--border)}
.filter-sheet.open{display:block}
.filter-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.filter-grid .full{grid-column:1/-1}
.field-label{display:block;font-size:11px;font-weight:600;color:var(--text-muted);
  text-transform:uppercase;letter-spacing:0.5px;margin-bottom:5px}
.field-input{width:100%;padding:10px 14px;border-radius:var(--radius-sm);
  background:var(--bg-input);border:1px solid var(--border);color:var(--text);
  font-size:14px;font-family:var(--font);-webkit-appearance:none;appearance:none}
.field-input:focus{outline:none;border-color:var(--accent)}
.checkbox-row{display:flex;align-items:center;gap:8px;padding:10px 0}
.checkbox-row input{width:20px;height:20px;accent-color:var(--accent)}

.btn{flex:1;padding:12px;border-radius:var(--radius-sm);font-size:14px;font-weight:600;
  font-family:var(--font);cursor:pointer;border:none;text-align:center;text-decoration:none}
.btn:active{transform:scale(0.97)}
.btn-primary{background:var(--accent);color:#0f172a}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--text-dim)}
.btn-sm{padding:8px 14px;font-size:12px;flex:0}
.filter-actions{display:flex;gap:10px;margin-top:16px}

.toolbar{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 16px 0;padding:10px;
  border-radius:var(--radius);background:var(--bg-card);border:1px solid var(--border)}
.toolbar .full{grid-column:1/-1}
.toolbar input[type=text],.toolbar select{width:100%;padding:10px 12px;
  border-radius:var(--radius-sm);background:var(--bg-input);border:1px solid var(--border);
  color:var(--text);font-size:13px;font-family:var(--font);-webkit-appearance:none;appearance:none}
.toolbar input[type=text]:focus,.toolbar select:focus{outline:none;border-color:var(--accent)}
.toolbar .chip{display:flex;align-items:center;gap:6px;padding:8px 12px;border-radius:var(--radius-sm);
  background:var(--bg-input);border:1px solid var(--border);font-size:13px;cursor:pointer;user-select:none}
.toolbar .chip input{width:16px;height:16px;accent-color:var(--accent);margin:0}
.toolbar .chip.active{border-color:var(--accent);color:var(--accent)}
.toolbar .btn-apply{padding:10px 14px;border-radius:var(--radius-sm);background:var(--accent);
  color:#0f172a;border:none;font-size:13px;font-weight:600;cursor:pointer;font-family:var(--font);
  width:100%}
.toolbar .btn-reset{padding:10px 12px;border-radius:var(--radius-sm);background:transparent;
  color:var(--text-dim);border:1px solid var(--border);font-size:13px;cursor:pointer;
  text-decoration:none;font-family:var(--font);text-align:center;display:block}

.card-list{padding:12px 16px;display:flex;flex-direction:column;gap:10px}
.listing-card{background:var(--bg-card);border:1px solid var(--border);
  border-radius:var(--radius);padding:16px;position:relative;overflow:hidden}
.listing-card.unfallfrei{border-left:3px solid var(--green)}
.listing-card.has-warning{border-left:3px solid var(--red)}

.card-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.card-img{width:80px;height:60px;object-fit:cover;border-radius:8px;flex-shrink:0;background:#0a1426}
.card-content{flex:1;min-width:0}
.card-title{font-size:15px;font-weight:600;line-height:1.3;margin-bottom:4px}
.card-price{font-family:var(--mono);font-size:18px;font-weight:700;color:var(--accent);
  margin-top:4px}

.price-rating{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;
  border-radius:6px;margin-top:4px}
.pr-sehr-guter{background:rgba(52,211,153,0.18);color:var(--green)}
.pr-guter{background:rgba(56,189,248,0.18);color:#38bdf8}
.pr-fairer{background:rgba(251,191,36,0.18);color:var(--amber)}
.pr-erhoehter, .pr-hoher{background:rgba(251,113,133,0.18);color:var(--red)}
.pr-ohne{background:rgba(148,163,184,0.18);color:var(--text-dim)}

.card-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.tag{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:8px;
  background:rgba(255,255,255,0.05);border:1px solid var(--border);font-size:12px;
  color:var(--text-dim);white-space:nowrap}
.tag-green{color:var(--green);border-color:rgba(52,211,153,0.3)}
.tag-warn{color:#f59e0b;border-color:rgba(245,158,11,0.4);background:rgba(245,158,11,0.08);font-weight:600}

.card-actions{display:flex;gap:8px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.card-btn{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;
  padding:10px;border-radius:var(--radius-sm);background:rgba(255,255,255,0.04);
  border:1px solid var(--border);color:var(--text-dim);font-size:12px;font-weight:600;
  text-decoration:none;cursor:pointer;font-family:var(--font)}
.card-btn-accent{color:var(--accent);border-color:rgba(251,146,60,0.3)}

.empty-state{text-align:center;padding:48px 24px;color:var(--text-muted)}
.empty-state .icon{font-size:48px;margin-bottom:12px}
</style>
</head>
<body>

<header class="app-header">
  <div class="header-row">
    <div class="logo">
      <div class="logo-icon">M</div>
      <div>
        <div class="logo-text">Autoscan Mobile</div>
        <div class="logo-sub">mobile.de Scanner [TEST]</div>
      </div>
    </div>
    <form method="post" action="/sync" style="display:inline">
      <input type="hidden" name="max_details" value="5">
      <button class="icon-btn" type="submit" title="Sync">↻</button>
    </form>
  </div>
</header>

<div class="stats-bar">
  <div class="stat-chip"><span class="num">{{ total }}</span> Treffer</div>
  {% if last_log %}<div class="stat-chip">Letzter Sync: {{ last_log['scraped_at'][11:16] }}</div>{% endif %}
  <div class="stat-chip">💰 {{ total_cost }} Credits</div>
</div>

<div class="filter-toggle" onclick="document.getElementById('sf').classList.toggle('open')" style="background:rgba(251,146,60,0.1);border-color:rgba(251,146,60,0.3);color:var(--accent)">
  <span>🌐</span> Scrape-Filter (was wird von mobile.de geholt)
</div>

<form method="post" action="/save_filters">
<div class="filter-sheet" id="sf">
  <div style="font-size:11px;color:var(--text-muted);margin-bottom:12px;line-height:1.4">
    Diese Filter bestimmen welche Fahrzeuge von mobile.de gescraped werden. Nach Änderung "Speichern" + "Sync" klicken.
  </div>
  <div class="filter-grid">
    <div>
      <label class="field-label">Preis min €</label>
      <input class="field-input" type="number" name="price_min" value="{{ scrape_filters.price_min }}">
    </div>
    <div>
      <label class="field-label">Preis max €</label>
      <input class="field-input" type="number" name="price_max" value="{{ scrape_filters.price_max }}">
    </div>
    <div>
      <label class="field-label">EZ min (Jahr)</label>
      <input class="field-input" type="number" name="ez_min" value="{{ scrape_filters.ez_min }}" placeholder="2012">
    </div>
    <div>
      <label class="field-label">EZ max (Jahr)</label>
      <input class="field-input" type="number" name="ez_max" value="{{ scrape_filters.ez_max }}">
    </div>
    <div class="full">
      <label class="field-label">km max</label>
      <input class="field-input" type="number" name="km_max" value="{{ scrape_filters.km_max }}">
    </div>
    <div>
      <label class="field-label">Region</label>
      <input class="field-input" name="region" value="{{ scrape_filters.region }}" placeholder="Bayern">
    </div>
    <div>
      <label class="field-label">Radius km</label>
      <input class="field-input" type="number" name="radius" value="{{ scrape_filters.radius }}">
    </div>
    <div class="full">
      <label class="field-label">Anbieter</label>
      <select class="field-input" name="seller">
        <option value="FSBO" {% if scrape_filters.seller=='FSBO' %}selected{% endif %}>Privatanbieter</option>
        <option value="DEALER" {% if scrape_filters.seller=='DEALER' %}selected{% endif %}>Haendler</option>
        <option value="" {% if not scrape_filters.seller %}selected{% endif %}>Beide</option>
      </select>
    </div>
    <input type="hidden" name="lat" value="{{ scrape_filters.lat }}">
    <input type="hidden" name="lng" value="{{ scrape_filters.lng }}">
  </div>
  <div class="filter-actions">
    <button class="btn btn-primary" type="submit">Speichern</button>
  </div>
  <div style="margin-top:12px;padding:8px;background:#0a1426;border-radius:6px;font-size:10px;color:var(--text-muted);word-break:break-all;font-family:monospace">
    {{ scrape_url }}
  </div>
</div>
</form>

<form method="get">
<div class="toolbar">
  <input class="full" type="text" name="q" value="{{ params.q }}" placeholder="🔍 Suche (Marke, Modell, Beschreibung…)">
  <select name="sort">
    <option value="first_seen DESC" {% if params.sort=='first_seen DESC' %}selected{% endif %}>↓ Neueste zuerst</option>
    <option value="price ASC" {% if params.sort=='price ASC' %}selected{% endif %}>↑ Preis aufsteigend</option>
    <option value="price DESC" {% if params.sort=='price DESC' %}selected{% endif %}>↓ Preis absteigend</option>
    <option value="km ASC" {% if params.sort=='km ASC' %}selected{% endif %}>↑ KM aufsteigend</option>
    <option value="ez DESC" {% if params.sort=='ez DESC' %}selected{% endif %}>↓ EZ neuste zuerst</option>
  </select>
  <label class="chip {% if params.only_unfallfrei %}active{% endif %}">
    <input type="checkbox" name="only_unfallfrei" value="1" {% if params.only_unfallfrei %}checked{% endif %}>
    <span>Nur Unfallfrei</span>
  </label>
  <button class="full btn-apply" type="submit">Anwenden</button>
  {% if params.q or params.only_unfallfrei or (params.sort and params.sort != 'first_seen DESC') %}
  <a href="/" class="full btn-reset">Reset</a>
  {% endif %}
</div>
</form>

<div class="card-list">
{% for r in rows %}
  {% set rating_class = (r['price_rating'] or 'ohne')|lower %}
  {% set warn = ('unfall' in (r['title'] or '')|lower or 'motorschaden' in (r['description'] or '')|lower) and not r['unfallfrei'] %}
  <div class="listing-card {% if r['unfallfrei'] %}unfallfrei{% endif %} {% if warn %}has-warning{% endif %}">
    <div class="card-top">
      {% if r['image_url'] %}<img class="card-img" src="{{ r['image_url'] }}" alt="">{% endif %}
      <div class="card-content">
        <div class="card-title">{{ r['title'] or '—' }}</div>
        <div class="card-price">
          {% if r['price_eur'] %}{{ '{:,}'.format(r['price_eur']).replace(',','.') }} €{% else %}—{% endif %}
          {% if r['price_negotiable'] %}<span style="font-size:11px;color:var(--text-dim);font-weight:400">VB</span>{% endif %}
        </div>
        {% if r['price_rating'] %}
          {% if 'Sehr' in r['price_rating'] %}<span class="price-rating pr-sehr-guter">⭐ {{ r['price_rating'] }}</span>
          {% elif r['price_rating']|lower|trim == 'guter preis' %}<span class="price-rating pr-guter">{{ r['price_rating'] }}</span>
          {% elif 'Fairer' in r['price_rating'] %}<span class="price-rating pr-fairer">{{ r['price_rating'] }}</span>
          {% elif 'Erh' in r['price_rating'] or 'Hoher' in r['price_rating'] %}<span class="price-rating pr-erhoehter">⚠ {{ r['price_rating'] }}</span>
          {% else %}<span class="price-rating pr-ohne">{{ r['price_rating'] }}</span>{% endif %}
        {% endif %}
      </div>
    </div>
    <div class="card-meta">
      {% if r['unfallfrei'] %}<span class="tag tag-green">✓ Unfallfrei</span>{% endif %}
      {% if r['km'] %}<span class="tag">🛣 {{ '{:,}'.format(r['km']).replace(',','.') }} km</span>{% endif %}
      {% if r['first_reg'] %}<span class="tag">📅 EZ {{ r['first_reg'] }}</span>{% endif %}
      {% if r['power_ps'] %}<span class="tag">⚡ {{ r['power_ps'] }} PS</span>{% endif %}
      {% if r['fuel'] %}<span class="tag">⛽ {{ r['fuel'] }}</span>{% endif %}
      {% if r['previous_owners'] %}<span class="tag">👤 {{ r['previous_owners'] }} Halter</span>{% endif %}
      {% if r['city'] %}<span class="tag">📍 {{ r['city'] }}</span>{% endif %}
      {% if r['hu_until'] %}<span class="tag">🔧 HU {{ r['hu_until'] }}</span>{% endif %}
    </div>
    <div class="card-actions">
      <a class="card-btn card-btn-accent" href="{{ r['url'] }}" target="_blank">↗ Mobile.de</a>
      <a class="card-btn" href="/listing/{{ r['id'] }}">📋 Details</a>
    </div>
  </div>
{% endfor %}
{% if not rows %}
  <div class="empty-state"><div class="icon">🚗</div><div>Keine Treffer</div></div>
{% endif %}
</div>
</body>
</html>
"""

DETAIL_TPL = r"""
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ r['title'] }}</title>
<style>
body{font-family:-apple-system,sans-serif;background:#0f172a;color:#f1f5f9;padding:20px;max-width:600px;margin:0 auto}
h1{font-size:20px;margin-bottom:8px}
.back{color:#fb923c;text-decoration:none;display:inline-block;margin-bottom:16px}
.section{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px;margin-bottom:12px}
.section h3{font-size:14px;color:#94a3b8;text-transform:uppercase;margin-bottom:10px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:8px 16px;font-size:14px}
.kv dt{color:#94a3b8}
.feat-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.feat{padding:6px 10px;background:#0f172a;border:1px solid #334155;border-radius:8px;font-size:12px}
.desc{white-space:pre-wrap;font-size:13px;line-height:1.6}
</style></head><body>
<a class="back" href="/">← Zurück</a>
<h1>{{ r['title'] }}</h1>
<div style="font-size:24px;font-weight:700;color:#fb923c;margin-bottom:16px">{{ '{:,}'.format(r['price_eur']).replace(',','.') if r['price_eur'] else '—' }} €</div>

<div class="section">
  <h3>Daten</h3>
  <dl class="kv">
    <dt>Erstzulassung</dt><dd>{{ r['first_reg'] or '—' }}</dd>
    <dt>Kilometerstand</dt><dd>{{ '{:,}'.format(r['km']).replace(',','.') if r['km'] else '—' }} km</dd>
    <dt>Leistung</dt><dd>{{ r['power_kw'] }} kW ({{ r['power_ps'] }} PS)</dd>
    <dt>Kraftstoff</dt><dd>{{ r['fuel'] or '—' }}</dd>
    <dt>Getriebe</dt><dd>{{ r['gearbox'] or '—' }}</dd>
    <dt>Halter</dt><dd>{{ r['previous_owners'] or '—' }}</dd>
    <dt>HU bis</dt><dd>{{ r['hu_until'] or '—' }}</dd>
    <dt>Farbe</dt><dd>{{ r['color'] or '—' }}</dd>
    <dt>Standort</dt><dd>{{ r['location'] or '—' }}</dd>
    <dt>Online seit</dt><dd>{{ r['online_since'] or '—' }}</dd>
    <dt>Unfallfrei-Badge</dt><dd>{{ 'Ja' if r['unfallfrei'] else 'nicht angegeben' }}</dd>
    <dt>Preisbewertung</dt><dd>{{ r['price_rating'] or '—' }}</dd>
  </dl>
</div>

{% if features %}
<div class="section">
  <h3>Ausstattung ({{ features|length }})</h3>
  <div class="feat-grid">
    {% for f in features %}<div class="feat">✓ {{ f }}</div>{% endfor %}
  </div>
</div>
{% endif %}

{% if r['description'] %}
<div class="section">
  <h3>Beschreibung</h3>
  <div class="desc">{{ r['description'] }}</div>
</div>
{% endif %}

<a class="back" href="{{ r['url'] }}" target="_blank">↗ Auf mobile.de öffnen</a>
</body></html>
"""

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5001, debug=False)
