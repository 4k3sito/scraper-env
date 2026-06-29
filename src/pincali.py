#!/usr/bin/env python3
"""
Pincali (EasyBroker) — locales/terrenos comerciales en venta en Nuevo León.
Motor: Botasaurus (navegador + anti-deteccion) + Scrapling (parser robusto).

FASE 1: pagina ?page=N y recolecta URLs /inmueble/ unicas (para hasta agotar).
FASE 2: entra a cada detalle y extrae los campos estructurados.

Salida: output/pincali.json (+ upsert a Supabase via src.db).
Config: clave "pincali" en scrape_links.json (cae al SEARCH_URL por defecto).

Uso:
    python -m src.pincali
    python -m src.pincali --selftest <URL>     # valida extraccion de coords en 1 URL
"""

import os, sys
import json
import re
import time
import random
from urllib.parse import urljoin

from tqdm import tqdm
from botasaurus.browser import browser, Driver
from scrapling.parser import Selector
from src.utils import atomic_write_json, setup_graceful_shutdown, Checkpoint
from src.parser import parse_description, merge_parsed


# ── User Agent rotation ─────────────────────────────────────────────────────
# ponytail: 8 modern UAs covering Chrome, Firefox, Edge — enough to escape
# Cloudflare blocks without needing a proxy farm.
USER_AGENTS = [
    # Chrome 120+ Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Firefox Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    # Firefox macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    # Edge Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    # Chrome Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

_ua_index = 0

def _rotate_ua(driver):
    """Cambia el User-Agent via Chrome DevTools Protocol. Cicla la lista."""
    global _ua_index
    ua = USER_AGENTS[_ua_index % len(USER_AGENTS)]
    _ua_index += 1
    try:
        driver.driver.execute_cdp_cmd('Network.setUserAgentOverride', {'userAgent': ua})
        print(f"   [ua] rotado a: {ua.split('Chrome/')[1].split()[0] if 'Chrome/' in ua else ua.split('Firefox/')[1].split()[0]}")
    except Exception:
        # Fallback: JS override
        try:
            driver.run_js(f"Object.defineProperty(navigator, 'userAgent', {{get: () => '{ua}'}});")
        except Exception:
            pass
    time.sleep(1)


# ── Phase 1 checkpoint ──────────────────────────────────────────────────────
# Guarda progreso por (search_url, pagina) para reanudar si el scraper muere.

PHASE1_CP = "output/pincali_phase1.json"

def _load_phase1():
    """Retorna (search_url, completed_pages, collected) o None."""
    try:
        with open(PHASE1_CP) as f:
            d = json.load(f)
        return d.get("search_url"), set(d.get("completed_pages", [])), d.get("collected", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return None, set(), []

def _save_phase1(search_url, completed_pages, collected):
    os.makedirs("output", exist_ok=True)
    atomic_write_json({
        "search_url": search_url,
        "completed_pages": sorted(completed_pages),
        "collected": collected,
    }, PHASE1_CP)

def _clear_phase1():
    try:
        os.remove(PHASE1_CP)
    except FileNotFoundError:
        pass
    try:
        os.remove(PHASE1_CP.replace(".json", ".tmp"))
    except FileNotFoundError:
        pass


BASE_URL = "https://www.pincali.com"
SEARCH_URL = "https://www.pincali.com/inmuebles/propiedades-en-venta-en-nuevo-leon"
# Local comercial + Local en CC + Terreno comercial
QUERY = "property_type_ids=29061-28701-28661"
OUTPUT_FILE = "output/pincali.json"
LISTING_SELECTOR = 'a[href^="/inmueble/"]'
MAX_PAGES = 60  # tope de seguridad; la parada real es por paginas vacias

# Caja delimitadora de Mexico para validar coordenadas (igual que el resto del repo)
def _in_mx(lat, lon):
    return lat is not None and lon is not None and 14 < lat < 33 and -118 < lon < -86


def _page(html):
    return Selector(html)


def _txt(node_text):
    return re.sub(r"\s+", " ", node_text).strip() if node_text else None


# ──────────────────────────────────────────────────────────────────────────
# FASE 1 — recolectar URLs
# ──────────────────────────────────────────────────────────────────────────
def listing_urls_from_html(html):
    page = _page(html)
    out = []
    for href in page.css(f'{LISTING_SELECTOR}::attr(href)').getall():
        href = (href or "").split("?")[0].strip()
        if href.startswith("/inmueble/"):
            out.append(urljoin(BASE_URL, href))
    return list(dict.fromkeys(out))  # unicas, en orden


@browser(headless=True, block_images=True, reuse_driver=True,
         max_retry=3, close_on_crash=True, output=None)
def collect_listing_urls(driver: Driver, data):
    """Recolecta URLs de listings paginando hasta agotar.

    Robusta: reintenta paginas vacias con rotacion de UA, detecta
    "sin resultados" explicito, checkpoint de Fase 1 para reanudar
    si el scraper se interrumpe.
    """
    search_url = data["search_url"]
    sep = "&" if "?" in search_url else "?"

    NO_RESULTS_PATTERNS = [
        "sin resultados", "no hay propiedades", "no encontramos",
        "no listings", "no properties", "no results",
        "no se encontraron", "0 resultados",
    ]

    # ── Checkpoint: reanudar desde donde nos quedamos ──
    cp_search, completed_pages, collected = _load_phase1()
    if cp_search == search_url and collected:
        print(f"   [cp] Fase 1 checkpoint encontrado: {len(collected)} URLs, {len(completed_pages)} paginas completadas")
        seen = set(collected)
    else:
        completed_pages, collected, seen = set(), [], set()
    # ─────────────────────────────────────────────────────

    min_collected = 100

    driver.google_get(search_url, bypass_cloudflare=True)
    page_iter = tqdm(range(1, MAX_PAGES + 1), desc=f"  Collecting pages", unit="pg", leave=False)
    for page_num in page_iter:
        # Saltar paginas ya completadas del checkpoint
        if page_num in completed_pages:
            page_iter.set_description(f"  Page {page_num}: (checkpoint, skipping)")
            continue

        url = f"{search_url}{sep}page={page_num}"
        if page_num > 1 or (page_num == 1 and collected):
            driver.google_get(url, bypass_cloudflare=True)

        retries = 3 if len(collected) < min_collected else 2
        page_html = None

        for attempt in range(1, retries + 2):
            try:
                driver.wait_for_element(LISTING_SELECTOR, wait=12)
                driver.scroll_to_bottom(smooth_scroll=True)
                time.sleep(random.uniform(0.6, 1.2))
                driver.run_js("window.scrollTo(0,0);")
                page_html = driver.page_html
                break
            except Exception:
                html_check = driver.page_html or ""
                no_results = any(p in html_check.lower() for p in NO_RESULTS_PATTERNS)
                if no_results:
                    print(f"[Fase 1] page {page_num}: sin resultados (detectado) -> stop")
                    _save_phase1(search_url, completed_pages, collected)
                    page_iter.close()
                    return collected
                if attempt <= retries:
                    delay = 4 * attempt
                    print(f"   [retry] page {page_num} attempt {attempt}/{retries}, waiting {delay}s")
                    _rotate_ua(driver)
                    time.sleep(delay)
                    try:
                        driver.google_get(url, bypass_cloudflare=True)
                    except Exception:
                        pass
                else:
                    print(f"[Fase 1] page {page_num}: sin listings tras {retries} intentos -> stop")
                    _save_phase1(search_url, completed_pages, collected)
                    page_iter.close()
                    return collected

        if not page_html:
            print(f"[Fase 1] page {page_num}: no se obtuvo HTML -> stop")
            _save_phase1(search_url, completed_pages, collected)
            page_iter.close()
            return collected

        new = 0
        for link in listing_urls_from_html(page_html):
            if link not in seen:
                seen.add(link)
                collected.append(link)
                new += 1
        completed_pages.add(page_num)
        page_iter.set_description(f"  Page {page_num}: +{new} (acum {len(collected)})")
        page_iter.set_postfix(unique=len(collected))

        # Checkpoint despues de cada pagina exitosa
        _save_phase1(search_url, completed_pages, collected)

        if new == 0:
            print(f"[Fase 1] page {page_num}: 0 nuevos. Probando pagina siguiente...")
            if len(collected) < min_collected:
                page_iter.close()
                print(f"[Fase 1] solo {len(collected)} URLs (< {min_collected}) y se agotaron -> stop")
                _save_phase1(search_url, completed_pages, collected)
                return collected
        time.sleep(random.uniform(1.5, 3.0))

    _save_phase1(search_url, completed_pages, collected)
    return collected


# ──────────────────────────────────────────────────────────────────────────
# FASE 2 — extraer detalle
# ──────────────────────────────────────────────────────────────────────────
def _coords(html):
    """(lat, lon) validados contra Mexico. JSON-LD geo -> script inline."""
    page = _page(html)
    # Metodo 1: Schema.org geo en JSON-LD
    for block in page.css('script[type="application/ld+json"]::text').getall():
        try:
            obj = json.loads(block)
        except Exception:
            continue
        for item in (obj if isinstance(obj, list) else [obj]):
            geo = isinstance(item, dict) and (item.get("geo") or {})
            if geo and geo.get("latitude"):
                try:
                    lat, lon = float(geo["latitude"]), float(geo["longitude"])
                    if _in_mx(lat, lon):
                        return lat, lon
                except (TypeError, ValueError):
                    pass
    # Metodo 2: atributos data-lat/data-long del marcador del mapa (EasyBroker)
    m = re.search(r'data-lat="(-?\d{1,2}\.\d{3,})"\s+data-long="(-?\d{1,3}\.\d{3,})"', html)
    if m:
        try:
            lat, lon = float(m.group(1)), float(m.group(2))
            if _in_mx(lat, lon):
                return lat, lon
        except ValueError:
            pass
    # Metodo 3: patron lat/lng en cualquier script inline
    m_lat = re.search(r'"lat(?:itude)?"\s*:\s*"?(-?\d{1,3}\.\d{4,})"?', html, re.I)
    m_lon = re.search(r'"l(?:ng|on|ongitude)"\s*:\s*"?(-?\d{1,3}\.\d{4,})"?', html, re.I)
    if m_lat and m_lon:
        try:
            lat, lon = float(m_lat.group(1)), float(m_lon.group(1))
            if _in_mx(lat, lon):
                return lat, lon
        except ValueError:
            pass
    return None, None


def parse_detail(url, html):
    page = _page(html)
    full = _txt(" ".join(page.css("body ::text").getall())) or ""

    rec = {
        "url": url, "id_anuncio": None, "titulo": None, "precio": None,
        "tipo_operacion": None, "tipo_inmueble": None,
        "m2_construccion": None, "m2_terreno": None,
        "banos": None, "estacionamientos": None,
        "direccion": None, "colonia": None, "codigo_postal": None,
        "lat": None, "lng": None, "agente": None,
        "descripcion": None, "imagenes": [],
    }

    rec["titulo"] = _txt(page.css("h1::text").get())
    # El precio vive en .price > .digits ("$5,990,000 MXN"); el wrapper .price
    # solo tiene whitespace como texto directo, por eso ::text del wrapper falla.
    rec["precio"] = _txt(page.css('.price .digits::text').get()) or _txt(page.css(
        '[class*="price"] ::text, [class*="Price"] ::text, '
        '[class*="precio"] ::text, [class*="Precio"] ::text').get())
    rec["direccion"] = _txt(page.css(
        '[class*="address"]::text, [class*="Address"]::text, '
        '[class*="location"]::text, [class*="Location"]::text, '
        '[class*="ubicacion"]::text').get())
    rec["descripcion"] = _txt(" ".join(page.css(
        '[class*="description"] ::text, [class*="Description"] ::text, '
        '[class*="descripcion"] ::text').getall()))[:1000] or None
    rec["agente"] = _txt(" ".join(page.css(
        '[class*="agent"] ::text, [class*="Agent"] ::text, '
        '[class*="broker"] ::text, [class*="contact"] ::text').getall()))[:200] or None

    # "Datos del anuncio": Pincali los lista como <li class="listing__data-row">
    # <span>label</span><span>value</span></li> (no usa <table>/<dl>).
    for row in page.css("li.listing__data-row, table tr, dl > div"):
        cells = [c for c in (_txt(t) for t in row.css("::text").getall()) if c]
        if len(cells) < 2:
            continue
        label, value = cells[0].lower(), cells[1]
        if "operaci" in label:
            rec["tipo_operacion"] = value
        elif "tipo de inmueble" in label or "tipo de propiedad" in label:
            rec["tipo_inmueble"] = value
        elif "colonia" in label:
            rec["colonia"] = value
        elif "postal" in label:
            rec["codigo_postal"] = value

    # ID del anuncio: <div class="listing-id"><span>ID: EB-WI1817</span></div>
    m_id = re.search(r'ID:\s*(EB-\w+)', full)
    if m_id:
        rec["id_anuncio"] = m_id.group(1)

    # Caracteristicas numericas via regex sobre el texto (robusto a cambios de DOM)
    def _num(pat):
        m = re.search(pat, full, re.I)
        return m.group(1).replace(",", "") if m else None
    rec["m2_construccion"] = _num(r"([\d,\.]+)\s*m[²2]\s*de\s*construcci")
    rec["m2_terreno"] = _num(r"([\d,\.]+)\s*m[²2]\s*de\s*terreno")
    rec["banos"] = _num(r"(\d+)\s*ba[ñn]os?")
    rec["estacionamientos"] = _num(r"(\d+)\s*estacionamiento")

    lat, lon = _coords(html)
    rec["lat"], rec["lng"] = lat, lon

    # Imagenes de la galeria: solo fotos reales de la propiedad
    # (assets.easybroker.com/property_images/...), NO los logos/avatares de
    # cdn.easybroker.com/assets/{marketplace,agent,account}/.
    seen, imgs = set(), []
    for src in page.css("img::attr(src)").getall():
        src = (src or "").split("?")[0]
        if "easybroker.com/property_images/" in src and src not in seen:
            seen.add(src)
            imgs.append(src)
    rec["imagenes"] = imgs
    if rec.get("descripcion"):
        merge_parsed(rec, parse_description(rec["descripcion"]))
    return rec


@browser(headless=True, block_images=True, reuse_driver=True,
         max_retry=3, close_on_crash=True, output=None)
def extract_listing(driver: Driver, url: str):
    """Botasaurus itera la lista: recibe UNA url, devuelve UN dict."""
    global pbar
    for attempt in range(1, 4):
        try:
            driver.get(url)
            driver.wait_for_element("h1", wait=15)
            time.sleep(random.uniform(0.6, 1.4))
            rec = parse_detail(url, driver.page_html)
            _cp_checkpoint.done(url)
            if pbar:
                pbar.update(1)
            return rec
        except Exception as e:
            if attempt < 3:
                delay = 3 * attempt
                print(f"   [retry] {url} — attempt {attempt}/3: {e}, waiting {delay}s")
                time.sleep(delay)
            else:
                if pbar:
                    pbar.update(1)
                return {"url": url, "error": f"{e} [after 3 retries]"}


pbar = None

# Module-level checkpoint reference (set by scrape(), used by extract_listing)
_cp_checkpoint = None


def scrape(search_urls):
    global pbar, _cp_checkpoint
    all_urls, seen = [], set()
    for su in search_urls:
        print(f"\n[Fase 1] {su}")
        for u in collect_listing_urls({"search_url": su}):
            if u not in seen:
                seen.add(u)
                all_urls.append(u)
    print(f"\n[Fase 1] {len(all_urls)} URLs unicas\n")
    _clear_phase1()  # Fase 1 completa, checkpoint ya no necesario
    if not all_urls:
        raise SystemExit("No se recolectaron URLs.")

    # Reconcile with DB: skip URLs already stored, delete vanished ones.
    try:
        from src import db
        all_urls = db.sync("pincali", all_urls)
    except Exception as e:
        print(f"[db] sync skipped: {e}")
    if not all_urls:
        raise SystemExit("Nothing new to scrape — DB already up to date.")

    # Checkpoint for resumable Phase 2
    _cp_checkpoint = Checkpoint("output/pincali_checkpoint.json")
    all_urls = _cp_checkpoint.resume(all_urls)
    if not all_urls:
        raise SystemExit("All URLs already extracted — checkpoint shows nothing to do.")

    setup_graceful_shutdown()
    pbar = tqdm(total=len(all_urls), desc="Fase 2 (Pincali)")
    results = extract_listing(all_urls)
    pbar.close()

    os.makedirs("output", exist_ok=True)
    atomic_write_json(results, OUTPUT_FILE, indent=2)
    _cp_checkpoint.clear()
    print(f"\nGuardado {OUTPUT_FILE} ({len(results)} registros)")

    try:
        from src import db
        db.upsert("pincali", results)
    except Exception as e:
        print(f"[db] upsert skipped: {e}")
    return results


def _selftest(url):
    @browser(headless=True, block_images=True, close_on_crash=True, output=None)
    def _one(driver: Driver, u):
        driver.get(u)
        driver.wait_for_element("h1", wait=15)
        return parse_detail(u, driver.page_html)
    rec = _one(url)
    print(json.dumps({k: rec[k] for k in ("titulo", "precio", "lat", "lng")},
                     ensure_ascii=False, indent=2))
    assert rec.get("lat") is None or _in_mx(rec["lat"], rec["lng"]), "coords fuera de MX"
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        _selftest(sys.argv[2])
        raise SystemExit

    try:
        with open("scrape_links.json", encoding="utf-8") as f:
            urls = json.load(f).get("pincali", [])
    except Exception:
        urls = []
    if not urls:
        urls = [f"{SEARCH_URL}?{QUERY}"]  # fallback al destino por defecto
    scrape(urls)
