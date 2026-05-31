"""
mobile_fetcher.py - SBSD Challenge Solver für mobile.de
Via Hyper Solutions SDK + DataImpulse Residential Proxy + curl_cffi
"""
import os
import re
import time
import uuid
import json as _json
import logging
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from curl_cffi import requests as cffi_requests
from hyper_sdk import Session as HyperSession, SbsdInput, SensorInput

load_dotenv('/opt/mobile-test/.env')

HYPER_API_KEY  = os.environ.get("HYPER_API_KEY", "").strip()
PROXY_HOST     = os.environ.get("PROXY_HOST", "gw.dataimpulse.com").strip()
PROXY_PORT     = os.environ.get("PROXY_PORT", "823").strip()
PROXY_USER     = os.environ.get("PROXY_USER", "").strip()
PROXY_PASS     = os.environ.get("PROXY_PASS", "").strip()
PROXY_COUNTRY  = os.environ.get("PROXY_COUNTRY", "de").strip().lower()
USER_AGENT     = os.environ.get(
    "MOBILE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
).strip()
ACCEPT_LANGUAGE = "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"
AKAMAI_VERSION = int(os.environ.get("AKAMAI_VERSION", "3"))
MAX_SENSOR_POSTS = 3
COOKIE_TTL_SECONDS = int(os.environ.get("MOBILE_COOKIE_TTL", "600"))

logger = logging.getLogger("mobile_fetcher")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] [%(name)s/%(levelname)s] %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

if not HYPER_API_KEY:
    raise RuntimeError("HYPER_API_KEY nicht gesetzt in /opt/mobile-test/.env")
if not PROXY_USER or not PROXY_PASS:
    raise RuntimeError("PROXY_USER/PROXY_PASS nicht gesetzt in /opt/mobile-test/.env")


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]


def _build_proxy_url(session_id: str) -> str:
    """DataImpulse sticky session: user__cr.de__sessid.<id>:pass"""
    user = f"{PROXY_USER}__cr.{PROXY_COUNTRY}__sessid.{session_id}"
    return f"http://{user}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"


# ============================================================
# Challenge Detection
# ============================================================
def _parse_sbsd_challenge(html: str) -> Optional[Dict[str, str]]:
    """
    Erkennt SBSD Challenge im HTML.
    Pattern aus Hyper Docs: <script src="/path?v=UUID&t=TOKEN">
    
    Returns dict mit path, v, t -- oder None wenn keine SBSD Challenge
    """
    # Erst: Script mit BEIDEN v und t Parametern (= Challenge)
    m = re.search(
        r'<script[^>]+src="(/[A-Za-z0-9_/\-\.]+)\?v=([a-f0-9\-]+)&(?:amp;)?t=(\d+)"',
        html
    )
    if m:
        return {"path": m.group(1), "v": m.group(2), "t": m.group(3)}
    return None


def _parse_sbsd_basic(html: str) -> Optional[Dict[str, str]]:
    """
    Erkennt Basic SBSD (nur ?v=, kein t).
    Returns dict mit path, v -- oder None
    """
    # Script mit v aber OHNE t
    for m in re.finditer(
        r'<script[^>]+src="(/[A-Za-z0-9_/\-\.]+)\?v=([a-f0-9\-]+)"',
        html
    ):
        # Sicherstellen dass kein "&t=" dahinter kommt
        full = m.group(0)
        if '&t=' not in full and '&amp;t=' not in full:
            return {"path": m.group(1), "v": m.group(2)}
    return None


def _parse_classic_script_path(html: str) -> Optional[str]:
    """Klassischer Akamai Script-Path (defer attribute, kein ?v=)."""
    matches = re.findall(r'<script[^>]+src="(/[A-Za-z0-9_/-]{20,})"[^>]*defer', html)
    if matches:
        return matches[0]
    return None


# ============================================================
# Headers
# ============================================================
def _build_browser_headers(host: str, referer: Optional[str] = None,
                           is_xhr: bool = False) -> Dict[str, str]:
    headers = {
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": USER_AGENT,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
        "Sec-Fetch-Site": "none" if not referer else "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": ACCEPT_LANGUAGE,
        "Priority": "u=0, i",
    }
    if referer:
        headers["Referer"] = referer
    if is_xhr:
        headers["Accept"] = "*/*"
        headers["Sec-Fetch-Site"] = "same-origin"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Content-Type"] = "application/json"
        headers.pop("Upgrade-Insecure-Requests", None)
        headers.pop("Sec-Fetch-User", None)
        headers["Priority"] = "u=1, i"
    return headers


# ============================================================
# Akamai Session mit SBSD Support
# ============================================================
class AkamaiSession:
    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id or new_session_id()
        self.proxy_url = _build_proxy_url(self.session_id)
        self.hyper = HyperSession(HYPER_API_KEY)
        self.client = cffi_requests.Session(
            impersonate="chrome131",
            proxies={"http": self.proxy_url, "https": self.proxy_url},
            timeout=60,
            verify=True,
        )
        self.warmed_up: bool = False
        self.warmed_at: float = 0.0
        self.public_ip: Optional[str] = None
        self.sbsd_solved: bool = False

    def _get_public_ip(self) -> str:
        if self.public_ip:
            return self.public_ip
        try:
            r = self.client.get("https://api.ipify.org?format=text", timeout=20)
            ip = (r.text or "").strip()
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                self.public_ip = ip
                logger.info(f"[{self.session_id}] Proxy IP: {ip}")
                return ip
        except Exception as e:
            logger.warning(f"ipify failed: {e}")
        return "0.0.0.0"

    def _get_cookie(self, name: str) -> Optional[str]:
        try:
            for c in self.client.cookies.jar:
                if c.name == name:
                    return c.value
        except Exception:
            pass
        try:
            return self.client.cookies.get(name)
        except Exception:
            return None

    # --------------------------------------------------------
    # SBSD CHALLENGE SOLVER
    # --------------------------------------------------------
    def _solve_sbsd_challenge(self, target_url: str, html: str,
                              challenge: Dict[str, str]) -> bool:
        """
        Komplette SBSD Challenge laut Hyper Docs:
          1. GET Script: /<path>?v=<v>&t=<t>
          2. Hyper API: generate_sbsd_data(uuid=v, ...)
          3. POST {"body": payload} an /<path>?t=<t>
          4. Caller macht dann GET der Original-URL → echte Seite
        """
        host = urlparse(target_url).netloc
        path = challenge["path"]
        v = challenge["v"]
        t = challenge["t"]
        logger.info(f"[{self.session_id}] SBSD CHALLENGE: path={path[:40]}... v={v[:8]}... t={t}")

        # --- Step 1: Script holen ---
        script_url = f"https://{host}{path}?v={v}&t={t}"
        try:
            r_script = self.client.get(
                script_url,
                headers=_build_browser_headers(host, referer=target_url),
            )
        except Exception as e:
            logger.error(f"SBSD script GET failed: {e}")
            return False
        if r_script.status_code != 200:
            logger.error(f"[{self.session_id}] SBSD script GET {r_script.status_code}")
            return False
        script_body = r_script.text
        logger.info(f"[{self.session_id}] SBSD script ok ({len(script_body)}b)")

        # --- Step 2: Hyper Payload generieren ---
        ip = self._get_public_ip()
        # o cookie aus bm_so (oder sbsd_o falls vorhanden)
        o_cookie = self._get_cookie("sbsd_o") or self._get_cookie("bm_so") or ""

        try:
            sbsd_payload = self.hyper.generate_sbsd_data(SbsdInput(
                index=0,
                user_agent=USER_AGENT,
                uuid=v,
                page_url=target_url,
                o_cookie=o_cookie,
                script=script_body,
                accept_language=ACCEPT_LANGUAGE,
                ip=ip,
            ))
        except Exception as e:
            logger.error(f"hyper generate_sbsd_data failed: {e}")
            return False
        logger.info(f"[{self.session_id}] SBSD payload generiert ({len(str(sbsd_payload))}b)")

        # --- Step 3: Payload an /<path>?t=<t> POSTen ---
        submit_url = f"https://{host}{path}?t={t}"
        body_dict = {"body": sbsd_payload if isinstance(sbsd_payload, str) else str(sbsd_payload)}
        try:
            r_submit = self.client.post(
                submit_url,
                headers=_build_browser_headers(host, referer=target_url, is_xhr=True),
                data=_json.dumps(body_dict),
            )
        except Exception as e:
            logger.error(f"SBSD submit POST failed: {e}")
            return False
        logger.info(f"[{self.session_id}] SBSD submit POST → {r_submit.status_code}")

        if r_submit.status_code in (200, 201, 204):
            self.sbsd_solved = True
            return True
        else:
            logger.warning(f"[{self.session_id}] SBSD submit unerwarteter Status {r_submit.status_code}")
            return False

    # --------------------------------------------------------
    # CLASSIC AKAMAI SENSOR FLOW (Fallback)
    # --------------------------------------------------------
    def _solve_classic_sensors(self, target_url: str, script_path: str,
                               script_body: str) -> bool:
        host = urlparse(target_url).netloc
        script_url = f"https://{host}{script_path}"
        ip = self._get_public_ip()
        sensor_context = ""

        for attempt in range(1, MAX_SENSOR_POSTS + 1):
            abck_cookie = self._get_cookie("_abck") or ""
            bm_sz = self._get_cookie("bm_sz") or ""

            if not sensor_context:
                sensor_input = SensorInput(
                    abck=abck_cookie, bmsz=bm_sz,
                    version=str(AKAMAI_VERSION), page_url=target_url,
                    user_agent=USER_AGENT, ip=ip, accept_language=ACCEPT_LANGUAGE,
                    context="", script=script_body, script_url=script_url,
                )
            else:
                sensor_input = SensorInput(
                    abck=abck_cookie, bmsz=bm_sz,
                    version=str(AKAMAI_VERSION), page_url=target_url,
                    user_agent=USER_AGENT, ip=ip, accept_language=ACCEPT_LANGUAGE,
                    context=sensor_context, script="", script_url=script_url,
                )

            try:
                sensor_data, new_context = self.hyper.generate_sensor_data(sensor_input)
            except Exception as e:
                logger.error(f"hyper generate_sensor_data failed: {e}")
                return False
            sensor_context = new_context or sensor_context

            try:
                rp = self.client.post(
                    script_url,
                    headers=_build_browser_headers(host, referer=target_url, is_xhr=True),
                    data='{"sensor_data":' + _json.dumps(sensor_data if isinstance(sensor_data, str) else _json.dumps(sensor_data)) + '}',
                )
            except Exception as e:
                logger.error(f"sensor POST failed: {e}")
                return False
            logger.info(f"[{self.session_id}] sensor POST #{attempt} → {rp.status_code}")

            new_abck = self._get_cookie("_abck") or ""
            if "~0~" in new_abck:
                logger.info(f"[{self.session_id}] _abck VALID (~0~) nach #{attempt}")
                return True
        return False

    # --------------------------------------------------------
    # WARMUP — automatisch SBSD oder Classic
    # --------------------------------------------------------
    def warmup(self, target_url: str = "https://www.mobile.de/") -> bool:
        host = urlparse(target_url).netloc
        logger.info(f"[{self.session_id}] WARMUP start → {target_url}")

        # Step 1: Initial GET
        try:
            r1 = self.client.get(target_url,
                                 headers=_build_browser_headers(host),
                                 allow_redirects=True)
        except Exception as e:
            logger.error(f"warmup step 1 failed: {e}")
            return False
        logger.info(f"[{self.session_id}] step1 GET {r1.status_code} body={len(r1.text)}b")

        # 403 = harter Block (IP geflaggt)
        if r1.status_code == 403:
            logger.error(f"[{self.session_id}] HARD BLOCK 403 — IP {self.public_ip or '?'} verbrannt")
            return False

        # Detect challenge type
        sbsd_chal = _parse_sbsd_challenge(r1.text)
        if sbsd_chal:
            logger.info(f"[{self.session_id}] → SBSD CHALLENGE detected")
            ok = self._solve_sbsd_challenge(target_url, r1.text, sbsd_chal)
            if ok:
                self.warmed_up = True
                self.warmed_at = time.time()
                return True
            return False

        classic = _parse_classic_script_path(r1.text)
        if classic:
            logger.info(f"[{self.session_id}] → CLASSIC AKAMAI sensor flow")
            try:
                r2 = self.client.get(f"https://{host}{classic}",
                                     headers=_build_browser_headers(host, referer=target_url))
                if r2.status_code == 200:
                    ok = self._solve_classic_sensors(target_url, classic, r2.text)
                    self.warmed_up = ok
                    self.warmed_at = time.time()
                    return ok
            except Exception as e:
                logger.error(f"classic flow failed: {e}")
                return False

        # Keine Challenge → Seite ist schon clean
        logger.info(f"[{self.session_id}] → keine Challenge erkannt, Seite scheint clean")
        self.warmed_up = True
        self.warmed_at = time.time()
        return True

    def is_expired(self) -> bool:
        if not self.warmed_up:
            return True
        return (time.time() - self.warmed_at) > COOKIE_TTL_SECONDS

    def fetch(self, url: str, referer: Optional[str] = None) -> Tuple[str, int]:
        host = urlparse(url).netloc
        if self.is_expired():
            logger.info(f"[{self.session_id}] cookies expired, re-warmup...")
            self.warmup(f"https://{host}/")
        try:
            r = self.client.get(
                url,
                headers=_build_browser_headers(host, referer=referer or f"https://{host}/"),
                allow_redirects=True,
            )
        except Exception as e:
            logger.error(f"fetch failed: {e}")
            return "", 0

        # Falls wir trotzdem auf einer SBSD Challenge landen → nochmal lösen
        if r.status_code == 200 and len(r.text) < 5000:
            sbsd_chal = _parse_sbsd_challenge(r.text)
            if sbsd_chal:
                logger.info(f"[{self.session_id}] fetch hit SBSD challenge → solve & retry")
                self._solve_sbsd_challenge(url, r.text, sbsd_chal)
                try:
                    r = self.client.get(
                        url,
                        headers=_build_browser_headers(host, referer=referer or f"https://{host}/"),
                        allow_redirects=True,
                    )
                except Exception as e:
                    logger.error(f"fetch retry failed: {e}")
        return r.text, r.status_code

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


_global_session: Optional[AkamaiSession] = None


def get_session(force_new: bool = False) -> AkamaiSession:
    global _global_session
    if force_new or _global_session is None or _global_session.is_expired():
        if _global_session is not None:
            _global_session.close()
        # Bis zu 3 Versuche mit unterschiedlichen IPs
        for attempt in range(3):
            _global_session = AkamaiSession()
            ok = _global_session.warmup("https://www.mobile.de/")
            if ok:
                break
            logger.warning(f"warmup attempt {attempt+1}/3 failed, neue Session...")
            _global_session.close()
            _global_session = None
            time.sleep(2)
        if _global_session is None:
            # Fallback: leere Session zurückgeben damit nichts crasht
            _global_session = AkamaiSession()
    return _global_session


def fetch_page(url: str, session_id: Optional[str] = None,
               referer: Optional[str] = None) -> Tuple[str, int, Dict[str, Any]]:
    if session_id:
        sess = AkamaiSession(session_id=session_id)
        sess.warmup("https://www.mobile.de/")
    else:
        sess = get_session()
    html, status = sess.fetch(url, referer=referer)
    debug = {
        "session_id": sess.session_id,
        "warmed_up": sess.warmed_up,
        "ip": sess.public_ip,
        "sbsd_solved": sess.sbsd_solved,
        "abck": (sess._get_cookie("_abck") or "")[:60],
    }
    return html, status, debug


if __name__ == "__main__":
    import sys
    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.mobile.de/"
    print(f"--- TEST: {test_url} ---")
    html, status, debug = fetch_page(test_url)
    print(f"status={status} bytes={len(html)} debug={debug}")
    print("--- HTML head 1500b ---")
    print(html[:1500])
