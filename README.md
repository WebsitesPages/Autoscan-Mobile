# Autoscan Mobile

mobile.de Scanner — Akamai Bot Manager Bypass via Hyper Solutions API.

## Stack

- **Backend:** Python 3.12, Flask, SQLite
- **Scraping:** curl_cffi (TLS fingerprinting), DataImpulse Residential Proxies
- **Akamai Bypass:** Hyper Solutions SDK (`hyper-sdk`)
- **Hosting:** Hetzner CX22, Nginx, autoscanner.space/mobile/

## Status

- ✅ UI Live auf https://autoscanner.space/mobile/
- ✅ ScrapingBee Integration (Test-Phase, 660/1000 Credits used)
- ⚙️ Hyper Solutions Integration (in progress)
- ⚠️ Proxy IP-Quality Issues (DataImpulse Range partially flagged on mobile.de)

## Setup

```bash
git clone <repo>
cd Autoscan-Mobile
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env mit echten Credentials
python3 mobile_app.py

EOR
