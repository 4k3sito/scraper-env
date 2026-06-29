"""
Scraper robusto para vivanuncios.com.mx (inmuebles)
Motor: Botasaurus (navegador) + Scrapling (parser)

FASE 1: recolecta URLs paginando con /page-N/.
FASE 2: extrae titulo, precio, codigo, direccion, caracteristicas,
        descripcion, fotos y coordenadas del HTML renderizado.
"""
import os, json, math, re, sys, time, random, urllib.parse, urllib.request
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from scrapling.parser import Selector
from botasaurus.browser import browser, Driver
from dotenv import load_dotenv
from src.utils import atomic_write_json, setup_graceful_shutdown, Checkpoint, setup_log
from src.parser import parse_description, merge_parsed

load_dotenv()

BASE = "https://www.vivanuncios.com.mx"
PER_PAGE = 30
OUTPUT_FILE = "output/vivanuncios.json"
MAX_PAGES = 50

_cp_checkpoint = None
pbar = None

AD_ID_RE = re.compile(r"/(\d{6,})(?:[/?#]|$)")
PHOTO_RE = re.compile(
    r"https?://img\d+\.naventcdn\.com/avisos/(?:resize/)?"
    r"((?:\d+/){6})\d+x\d+/(\d+)\.jpg", re.I
)


def _page(html):
    return Selector(html)


def _text(node):
    if node is None:
        return None
    t = node.text
    return re.sub(r"\s+", " ", t).strip() if t else None


def _first(node, sel):
    found = node.css(sel)
    return found[0] if found else None


def valid_mx(lat, lon):
    return lat is not None and lon is not None and 14 < lat < 33 and -118 < lon < -86


def extract_coords_inline(html):
    """Lat/lon desde JSON inline en el HTML."""
    for pat in (
        r'"latitude"\s*:\s*"?(-?\d{1,3}\.\d+)"?[^{}]{0,300}?"longitude"\s*:\s*"?(-?\d{1,3}\.\d+)"?',
        r'"lat"\s*:\s*"?(-?\d{1,3}\.\d+)"?[^{}]{0,200}?"(?:lng|lon|long)"\s*:\s*"?(-?\d{1,3}\.\d+)"?',
    ):
        m = re.search(pat, html, re.S)
        if m:
            lat, lon = round(float(m.group(1)), 6), round(float(m.group(2)), 6)
            if valid_mx(lat, lon):
                return lat, lon
    return None, None


# --------------------------------------------------------------------------
# GEOCODING de respaldo (Nominatim)
# --------------------------------------------------------------------------
ENABLE_GEOCODE_FALLBACK = True
GEOCODE_USER_AGENT = "vivanuncios-scraper/1.0 (villegoniko@gmail.com)"
_geocode_cache = {}

def geocode_address(address):
    if not address:
        return None, None
    key = address.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]
    q = urllib.parse.urlencode({"q": address + ", Mexico", "format": "json", "limit": "1", "countrycodes": "mx"})
    req = urllib.request.Request(f"https://nominatim.openstreetmap.org/search?{q}", headers={"User-Agent": GEOCODE_USER_AGENT})
    try:
        time.sleep(1.1)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data:
            lat = round(float(data[0]["lat"]), 6)
            lon = round(float(data[0]["lon"]), 6)
            if valid_mx(lat, lon):
                _geocode_cache[key] = (lat, lon)
                return lat, lon
    except Exception:
        pass
    _geocode_cache[key] = (None, None)
    return None, None


def build_page_url(search_url, page_num):
    if page_num == 1:
        return search_url
    parts = search_url.split("/v1c")
    if len(parts) == 2:
        prefix = parts[0]
        suffix = "v1c" + parts[1]
        suffix = re.sub(r"p\d+($|\?)", f"p{page_num}\\1", suffix)
        return f"{prefix}/page-{page_num}/{suffix}"
    return re.sub(r"p\d+($|\?)", f"p{page_num}\\1", search_url)


# ==========================================================================
# PARSEO DE PAGINA DE RESULTADOS (Scrapling)
# ==========================================================================
def ads_from_results(html):
    """Extrae URLs de anuncios del HTML."""
    page = _page(html)
    urls = []

    # Fuente 1: JSON-LD
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for o in (data if isinstance(data, list) else [data]):
            if isinstance(o, dict) and o.get("@type") in ("RentAction", "SellAction", "BuyAction"):
                u = (o.get("object") or {}).get("url")
                if u:
                    urls.append(u.split("?")[0])

    # Fuente 2: <a href> que termine en /<id>
    for a in page.css('a[href]'):
        href = a.attrib.get("href", "")
        if href and AD_ID_RE.search(href) and "/avisos/" not in href:
            urls.append(urljoin(BASE, href.split("?")[0]))

    seen, out = set(), []
    for u in urls:
        m = AD_ID_RE.search(u)
        if not m:
            continue
        aid = m.group(1)
        if aid not in seen:
            seen.add(aid)
            out.append(u)
    return out


def total_results(html):
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for o in (data if isinstance(data, list) else [data]):
            if isinstance(o, dict) and o.get("@type") == "Product":
                cnt = (o.get("offers") or {}).get("offerCount")
                if cnt:
                    try:
                        return int(str(cnt).replace(",", ""))
                    except ValueError:
                        pass
    m = re.search(r"([\d,]{3,})\s+\w", html)
    return int(m.group(1).replace(",", "")) if m else None


# ==========================================================================
# PARSEO DEL DETALLE (Scrapling)
# ==========================================================================

def extract_article_text(html):
    """Obtiene el texto del <article> usando Scrapling."""
    page = _page(html)
    art = _first(page, "article")
    if art is not None:
        return _text(art) or ""
    # fallback: body text
    body = _first(page, "body")
    return _text(body) or ""


STOP_LINES = re.compile(
    r"^(Leer descripción completa|Preguntas para|Selecciona una|¿Sigue|"
    r"¿Cuáles|¿Tiene|¿Acepta|Enviar|Conoce más|Características generales|"
    r"Servicios|Información del anunciante|Ver teléfono|¿Tienes algún|"
    r"¿Cómo evitar|Publicado hace|Avisarme si baja|CONTÁCTANOS)",
    re.I,
)


def parse_detail(url, article_text, html):
    lines = [l.strip() for l in article_text.split("\n") if l.strip()]

    rec = {
        "url": url, "codigo_publicacion": None, "codigo_anunciante": None,
        "titulo": None, "resumen": None, "precio": None, "precio_numerico": None,
        "moneda": "MXN", "tipo_transaccion": None, "tipo_propiedad": None,
        "direccion": None, "lat": None, "lon": None,
        "coord_source": None, "coord_precision_m": None,
        "map_image": None, "map_link": None,
        "caracteristicas": {}, "descripcion": None, "anunciante": None,
        "imagenes": [], "num_imagenes": 0,
    }

    m = re.search(r"Cód\.?\s*Vivanuncios:?\s*(\d+)", article_text, re.I)
    rec["codigo_publicacion"] = m.group(1) if m else (
        AD_ID_RE.search(url).group(1) if AD_ID_RE.search(url) else None
    )
    m = re.search(r"Cód\.?\s*del\s*anunciante:?\s*([A-Za-z0-9._-]+)", article_text, re.I)
    if m:
        rec["codigo_anunciante"] = m.group(1).rstrip("Cód").strip(".")

    if lines and "·" in lines[0]:
        rec["resumen"] = lines[0]
        rec["tipo_propiedad"] = lines[0].split("·")[0].strip()

    for l in lines[:6]:
        pm = re.search(r"\b(Renta|Venta)?\s*(?:MN|MXN|\$)\s*([\d,]+)", l, re.I)
        if pm and re.search(r"\d", l):
            if pm.group(1):
                rec["tipo_transaccion"] = pm.group(1).capitalize()
            rec["precio"] = l
            try:
                rec["precio_numerico"] = int(pm.group(2).replace(",", ""))
            except ValueError:
                pass
            break
    if not rec["tipo_transaccion"]:
        rec["tipo_transaccion"] = "Renta" if "renta" in url.lower() else ("Venta" if "venta" in url.lower() else None)

    for l in lines[:6]:
        if "," in l and not re.search(r"MN|MXN|\$|·", l) and len(l) < 90:
            rec["direccion"] = l
            break

    feats = {}
    for l in lines[:12]:
        if re.match(r"^\d+\s*m²\s*lote", l, re.I):
            feats["terreno"] = l
        elif re.match(r"^\d+\s*m²\s*constr", l, re.I):
            feats["construccion"] = l
        elif re.match(r"^\d+\s*estac", l, re.I):
            feats["estacionamientos"] = l
        elif re.search(r"medio baño|baño", l, re.I) and re.match(r"^\d", l):
            feats["banos"] = l
        elif re.match(r"^\d+\s*años?$", l, re.I):
            feats["antiguedad"] = l
        elif re.match(r"^\d+\s*recámara", l, re.I):
            feats["recamaras"] = l
    rec["caracteristicas"] = feats

    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if m:
        rec["titulo"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    desc, started = [], False
    for l in lines:
        if rec["titulo"] and l == rec["titulo"]:
            started = True
            continue
        if started:
            if STOP_LINES.search(l):
                break
            desc.append(l)
    rec["descripcion"] = "\n".join(desc).strip() or None

    m = re.search(r"Información del anunciante\s*\n?\s*([^\n]+)", article_text)
    if m:
        rec["anunciante"] = m.group(1).strip()

    m = re.search(r"(https?://img\d+\.naventcdn\.com/ficha/map/Vivanuncios/\d+E\.png)", html, re.I)
    if m:
        rec["map_image"] = m.group(1)
    elif rec["codigo_publicacion"]:
        rec["map_image"] = f"https://img10.naventcdn.com/ficha/map/Vivanuncios/{rec['codigo_publicacion']}E.png"

    lat, lon = extract_coords_inline(html)
    if valid_mx(lat, lon):
        rec["lat"], rec["lon"] = lat, lon
        rec["coord_source"] = "inline_html"
        rec["coord_precision_m"] = 10
        rec["map_link"] = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"

    rec["imagenes"] = extract_photos(html)
    rec["num_imagenes"] = len(rec["imagenes"])
    if rec.get("descripcion"):
        merge_parsed(rec, parse_description(rec["descripcion"]))
    return rec


def extract_photos(html):
    found = PHOTO_RE.findall(html)
    if not found:
        return []
    by_dir = {}
    for d, pid in found:
        by_dir.setdefault(d, set()).add(pid)
    prop_dir = max(by_dir, key=lambda d: len(by_dir[d]))
    return [f"https://img10.naventcdn.com/avisos/{prop_dir}1200x1200/{pid}.jpg" for pid in sorted(by_dir[prop_dir])]


# ==========================================================================
# FASE 1 — Botasaurus
# ==========================================================================
@browser(headless=True, block_images=True, reuse_driver=True,
         max_retry=3, close_on_crash=True, output=None)
def collect_ad_urls(driver: Driver, data):
    """data = {"search_url": str, "max_pages": int|None}"""
    search_url = data["search_url"]
    max_pages = data.get("max_pages")
    collected, seen = [], set()
    total_pages = None
    empty_streak = 0
    cap = min(max_pages or MAX_PAGES, MAX_PAGES)

    page_iter = tqdm(range(1, cap + 1), desc=f"  Collecting pages", unit="pg", leave=False)
    for page_num in page_iter:
        if total_pages and page_num > total_pages:
            break
        url = build_page_url(search_url, page_num)
        try:
            driver.google_get(url, bypass_cloudflare=True)
            time.sleep(random.uniform(2, 4))
            driver.scroll_to_bottom(smooth_scroll=True)
            time.sleep(random.uniform(0.5, 1.0))
            driver.run_js("window.scrollTo(0, 0);")
            time.sleep(random.uniform(0.3, 0.7))
        except Exception as e:
            print(f"[Fase 1] page {page_num}: {e} -> stop")
            break

        html = driver.page_html

        if total_pages is None:
            total = total_results(html)
            if total:
                total_pages = max(1, math.ceil(total / PER_PAGE))
                page_iter.set_description(f"  {total} anuncios, {total_pages} pgs")

        new = 0
        for u in ads_from_results(html):
            aid = AD_ID_RE.search(u)
            key = aid.group(1) if aid else u
            if key not in seen:
                seen.add(key)
                collected.append(u)
                new += 1
        page_iter.set_description(f"  Page {page_num}: +{new} (acum {len(collected)})")
        page_iter.set_postfix(total=len(collected))

        if new == 0:
            empty_streak += 1
            if empty_streak >= 10:
                print(f"[Fase 1] page {page_num}: 10 consecutive empty pages -> stopping early")
                break
        else:
            empty_streak = 0
        time.sleep(random.uniform(1.0, 2.0))

    return collected


# ==========================================================================
# FASE 2 — Botasaurus (itera la lista)
# ==========================================================================
@browser(headless=True, block_images=True, reuse_driver=True,
         max_retry=3, close_on_crash=True, output=None)
def extract_one(driver: Driver, url: str):
    global pbar
    for attempt in range(1, 4):
        try:
            driver.get(url)
            time.sleep(random.uniform(2, 4))
            html = driver.page_html
            article_text = extract_article_text(html)
            rec = parse_detail(url, article_text, html)
            _cp_checkpoint.done(url)
            if pbar:
                pbar.update(1)
            return rec
        except Exception as e:
            if attempt < 3:
                delay = 3 * attempt
                print(f"   [retry] {url} — attempt {attempt}/3: {e}, waiting {delay}s")
                time.sleep(delay)
    if pbar:
        pbar.update(1)
    return {"url": url, "error": "failed after 3 retries"}


def geocode_missing(records):
    if not ENABLE_GEOCODE_FALLBACK:
        return records
    todo = [r for r in records if r.get("lat") is None and r.get("direccion") and "error" not in r]
    for r in tqdm(todo, desc="Geocoding", disable=not todo):
        glat, glon = geocode_address(r["direccion"])
        if valid_mx(glat, glon):
            r["lat"], r["lon"] = glat, glon
            r["coord_source"] = "geocode"
            r["coord_precision_m"] = 250
            r["map_link"] = f"https://www.google.com/maps/search/?api=1&query={glat},{glon}"
    return records


# ==========================================================================
# ORQUESTADOR
# ==========================================================================
def scrape(search_urls, max_pages=None, max_ads=None):
    global _cp_checkpoint
    all_urls = []
    for url in search_urls:
        print(f"\n[Fase 1] Procesando URL: {url}")
        all_urls.extend(collect_ad_urls({"search_url": url, "max_pages": max_pages}))

    unique_urls = list(dict.fromkeys(all_urls))
    print(f"\n[Fase 1] {len(all_urls)} anuncios, {len(unique_urls)} únicos.\n")

    if max_ads:
        unique_urls = unique_urls[:max_ads]

    try:
        from src import db
        unique_urls = db.sync("vivaanuncios", unique_urls)
    except Exception as e:
        print(f"[db] sync skipped: {e}")
    if not unique_urls:
        raise SystemExit("Nothing new to scrape — DB already up to date.")

    _cp_checkpoint = Checkpoint("output/vivanuncios_checkpoint.json")
    pending = _cp_checkpoint.resume(unique_urls)
    if not pending:
        raise SystemExit("All URLs already extracted — checkpoint shows nothing to do.")

    setup_graceful_shutdown()
    global pbar
    pbar = tqdm(total=len(pending), desc="Fase 2 (Vivanuncios)", unit="anuncio")
    results = extract_one(pending) or []
    if pbar:
        pbar.close()
    if not isinstance(results, list):
        results = [results]
    results = [r for r in results if isinstance(r, dict)]

    geocode_missing(results)

    os.makedirs("output", exist_ok=True)
    atomic_write_json(results, OUTPUT_FILE, indent=2)
    _cp_checkpoint.clear()
    print(f"\nGuardado {OUTPUT_FILE} ({len(results)} registros)")
    try:
        from src import db
        db.upsert("vivaanuncios", results)
    except Exception as e:
        print(f"[db] upsert failed: {e}")
    return results


def selftest(url=None):
    if url is None:
        raise SystemExit("Pasa una URL:  python -m src.vivaanuncios --selftest <URL>")

    @browser(headless=True, block_images=True, output=None)
    def _one(driver: Driver, u):
        driver.get(u)
        time.sleep(3)
        html = driver.page_html
        return parse_detail(u, extract_article_text(html), html)

    result = _one(url)
    geocode_missing([result])
    lat, lon = result.get("lat"), result.get("lon")
    ok = valid_mx(lat, lon)
    print("\n[selftest vivanuncios]")
    print(f"  url              : {url}")
    print(f"  lat              : {lat}")
    print(f"  lon              : {lon}")
    print(f"  coord_source     : {result.get('coord_source')}")
    print(f"  coord_precision  : {result.get('coord_precision_m')} m")
    print(f"  map_link         : {result.get('map_link')}")
    print(f"  valid_mx         : {ok}")
    print(f"  {'PASS' if ok else 'WARN — sin coords'}")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        idx = sys.argv.index("--selftest")
        _url = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        selftest(_url)
        raise SystemExit(0)

    try:
        with open("scrape_links.json", encoding="utf-8") as f:
            links_data = json.load(f)
            search_urls = links_data.get("vivaanuncios", [])
    except Exception as e:
        print(f"Error cargando scrape_links.json: {e}")
        search_urls = []

    if not search_urls:
        print("No hay URLs. Agrega enlaces a 'vivaanuncios' en scrape_links.json.")
        raise SystemExit(0)

    if "--sample" in sys.argv:
        i = sys.argv.index("--sample")
        n = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit() else 5
        scrape(search_urls, max_pages=1, max_ads=n)
    else:
        scrape(search_urls, max_pages=None, max_ads=None)
