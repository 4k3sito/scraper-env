"""
Scraper robusto para inmuebles.mercadolibre.com.mx
Motor principal: Botasaurus (con PERFIL PERSISTENTE para mantener la sesion)
Parseo: Scrapling (Selector) sobre el HTML que entrega Botasaurus.

>>> SESION / LOGIN <<<
Este script NO automatiza el login (no maneja tus credenciales). En su lugar
usa un PERFIL persistente de Botasaurus: la primera vez se abre el navegador,
TU inicias sesion manualmente UNA vez, y la sesion queda guardada en disco
para reutilizarse en las siguientes corridas. Asi el sitio no te pide
re-loguear ni te bloquea por parecer bot.

FASE 0: setup_login()  -> abre el navegador con el perfil; inicias sesion tu.
FASE 1: recolecta URLs de anuncios paginando con _Desde_{offset}_NoIndex_True
FASE 2: entra a cada anuncio y extrae titulo, precio, operacion, codigo,
        direccion, caracteristicas, descripcion e imagenes.

Requisitos:
    pip install botasaurus
    pip install scrapling          # parser (no requiere sus fetchers)

Uso:
    # 1) primera vez, para iniciar sesion manualmente:
    python -m src.mercadolibre login
    # 2) despues, para scrapear:
    python -m src.mercadolibre
"""

import os, sys
import json
import math
import re
import time
import random
from urllib.parse import urljoin

from botasaurus.browser import browser, Driver
from scrapling.parser import Selector
from tqdm import tqdm
from src.utils import atomic_write_json, setup_graceful_shutdown, should_stop, Checkpoint, setup_log
from src.parser import parse_description, merge_parsed


# ==========================================================================
# CONSTANTES
# ==========================================================================
BASE = "https://inmuebles.mercadolibre.com.mx"
PER_PAGE = 48
OUTPUT_FILE = "output/mercadolibre.json"

# Nombre del perfil persistente (carpeta donde se guarda la sesion/cookies)
PROFILE = "ml_session"

AD_URL_RE = re.compile(r"https?://(?:inmueble|articulo)\.mercadolibre\.com\.mx/MLM-?\d+[^\s\"'<>]*-_JM", re.I)
MLM_ID_RE = re.compile(r"MLM-?(\d+)")


# ==========================================================================
# UTILIDADES
# ==========================================================================
def human_delay(a=1.5, b=3.5):
    time.sleep(random.uniform(a, b))


def build_page_url(base_search, page_num):
    """
    Paginacion de ML: _Desde_{offset}_NoIndex_True
    offset = (page-1)*48 + 1.  El sufijo _NoIndex_True es OBLIGATORIO:
    sin el, el sitio redirige a la pagina 1.
    base_search debe terminar en '/' (ej: .../locales-comerciales/)
    """
    base = base_search.split("#")[0].rstrip("/")
    if page_num <= 1:
        return base + "/"
    offset = (page_num - 1) * PER_PAGE + 1
    return f"{base}/_Desde_{offset}_NoIndex_True"


# ==========================================================================
# PARSEO DE LA PAGINA DE RESULTADOS  (Scrapling)
# ==========================================================================
def ads_from_results(html):
    """Extrae las URLs de anuncios (unicas por MLM-id)."""
    page = Selector(html)
    urls = []

    # Cada tarjeta es li.ui-search-layout__item con un <a> al detalle
    for a in page.css('li.ui-search-layout__item a::attr(href)').getall():
        if a and "MLM" in a:
            urls.append(a.split("#")[0].split("?")[0])

    # Respaldo: regex sobre el HTML crudo
    if not urls:
        urls = AD_URL_RE.findall(html)

    # Deduplicar por id de publicacion
    seen, out = set(), []
    for u in urls:
        m = MLM_ID_RE.search(u)
        if not m:
            continue
        mid = m.group(1)
        if mid not in seen:
            seen.add(mid)
            out.append(u.split("#")[0].split("?")[0])
    return out


def total_results(html):
    """Lee el total de resultados ('13,938 resultados')."""
    page = Selector(html)
    el = page.css(
        '.ui-search-search-result__quantity-results::text, '
        '.ui-search-results__quantity-results::text'
    ).get()
    txt = el or html
    m = re.search(r"([\d,\.]{3,})\s*resultados", txt)
    return int(re.sub(r"[,\.]", "", m.group(1))) if m else None


# ==========================================================================
# PARSEO DEL DETALLE  (Scrapling + JSON-LD)
# ==========================================================================
def jsonld_product(html):
    """Devuelve el JSON-LD de tipo Product (precio, productID, imagen)."""
    for block in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    ):
        try:
            d = json.loads(block)
        except json.JSONDecodeError:
            continue
        for o in (d if isinstance(d, list) else [d]):
            if isinstance(o, dict) and o.get("@type") == "Product":
                return o
    return None


def extract_coordinates(html):
    """
    Extrae las coordenadas de la propiedad del HTML de Mercado Libre.
    Metodo 1: JSON en script inline bajo 'map_info.location.latitude/longitude'
    Metodo 2: URL de Google Maps estatico (center=lat%2Clon)
    Metodo 3: JSON-LD GeoCoordinates
    Devuelve (lat, lon) como floats, o (None, None).
    """
    # Metodo 1: map_info.location en JSON inline (confirmado por probe)
    m = re.search(
        r'"map_info"\s*:\s*\{[^}]*?"location"\s*:\s*\{[^}]*?'
        r'"latitude"\s*:\s*"?(-?\d+\.\d+)"?[^}]*?'
        r'"longitude"\s*:\s*"?(-?\d+\.\d+)"?',
        html, re.S
    )
    if m:
        return float(m.group(1)), float(m.group(2))

    # Metodo 2: Google Maps static URL center=lat%2Clon
    m = re.search(
        r'maps\.googleapis\.com[^"]*center=(-?\d+\.\d+)(?:%2C|,)(-?\d+\.\d+)',
        html, re.I
    )
    if m:
        return float(m.group(1)), float(m.group(2))

    # Metodo 3: JSON-LD GeoCoordinates
    m = re.search(
        r'"GeoCoordinates"[^}]*?"latitude"\s*:\s*"?(-?\d+\.\d+)"?'
        r'[^}]*?"longitude"\s*:\s*"?(-?\d+\.\d+)"?',
        html, re.S
    )
    if m:
        return float(m.group(1)), float(m.group(2))

    return None, None


def parse_detail(url, html):
    page = Selector(html)
    ld = jsonld_product(html) or {}
    offers = ld.get("offers") or {}

    rec = {
        "url": url,
        "codigo_publicacion": None,
        "titulo": None,
        "precio": None,
        "precio_numerico": None,
        "moneda": offers.get("priceCurrency") or "MXN",
        "tipo_transaccion": None,
        "tipo_propiedad": None,
        "direccion": None,
        "ubicacion_breadcrumb": [],
        "lat": None,
        "lon": None,
        "map_link": None,
        "caracteristicas": {},
        "descripcion": None,
        "imagenes": [],
        "num_imagenes": 0,
    }

    # --- Codigo de publicacion (MLM id) ---
    pid = ld.get("productID") or ld.get("sku")
    if pid:
        rec["codigo_publicacion"] = re.sub(r"[^0-9]", "", pid)
    else:
        m = MLM_ID_RE.search(url)
        rec["codigo_publicacion"] = m.group(1) if m else None

    # --- Titulo ---
    rec["titulo"] = (
        page.css('h1.ui-pdp-title::text').get()
        or ld.get("name")
    )
    if rec["titulo"]:
        rec["titulo"] = rec["titulo"].strip()

    # --- Precio ---
    if offers.get("price") is not None:
        rec["precio_numerico"] = offers["price"]
        rec["precio"] = f'{offers["price"]:,}'
    else:
        frac = page.css('.andes-money-amount__fraction::text').get()
        if frac:
            rec["precio"] = frac.strip()
            try:
                rec["precio_numerico"] = int(frac.replace(",", ""))
            except ValueError:
                pass

    # --- Tipo de operacion y de propiedad (subtitulo: "Local comercial en Renta") ---
    subtitle = page.css('.ui-pdp-subtitle::text, .ui-pdp-header__subtitle::text').get()
    if subtitle:
        sub = subtitle.strip()
        if re.search(r"\brenta\b", sub, re.I):
            rec["tipo_transaccion"] = "Renta"
        elif re.search(r"\bventa\b", sub, re.I):
            rec["tipo_transaccion"] = "Venta"
        rec["tipo_propiedad"] = re.split(r"\s+en\s+", sub, flags=re.I)[0].strip()

    # --- Breadcrumb (ubicacion + operacion de respaldo) ---
    crumbs = [c.strip() for c in page.css('.andes-breadcrumb__item a::text, .andes-breadcrumb__item::text').getall() if c.strip()]
    rec["ubicacion_breadcrumb"] = crumbs
    if not rec["tipo_transaccion"]:
        for c in crumbs:
            if c.lower() in ("renta", "venta"):
                rec["tipo_transaccion"] = c.capitalize()

    # --- Direccion (seccion de ubicacion/mapa) ---
    loc = page.css(
        '.ui-vip-location__subtitle::text, '
        '.ui-pdp-media__title::text'
    ).getall()
    addr = next((t.strip() for t in loc if "," in (t or "") and "m²" not in t), None)
    rec["direccion"] = addr or (", ".join(crumbs[-3:]) if len(crumbs) >= 3 else None)

    # --- Caracteristicas (tabla de especificaciones key/value) ---
    specs = {}
    for row in page.css('.andes-table__row, tr.ui-pdp-specs__table__row, tr'):
        k = row.css('th::text, .andes-table__header::text').get()
        v = row.css('td::text, .andes-table__column--value::text').get()
        if k and v:
            specs[k.strip()] = v.strip()
    # Specs destacados (m2, banos arriba)
    for hl in page.css('.ui-pdp-highlighted-specs-res__attribute::text, .ui-vpp-highlighted-specs__key-value::text').getall():
        if hl and hl.strip():
            specs.setdefault(hl.strip(), True)
    rec["caracteristicas"] = specs

    # --- Descripcion completa ---
    desc = page.css('.ui-pdp-description__content::text').get()
    if not desc:
        desc = "\n".join(page.css('.ui-pdp-description__content *::text').getall())
    rec["descripcion"] = desc.strip() if desc else None

    # --- Imagenes (todas las de la propiedad) ---
    rec["imagenes"] = extract_photos(url, html)
    rec["num_imagenes"] = len(rec["imagenes"])

    # --- Coordenadas (3 metodos de fallback) ---
    lat, lon = extract_coordinates(html)
    rec["lat"] = lat
    rec["lon"] = lon
    if lat is not None and lon is not None:
        rec["map_link"] = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    # Backfill missing fields from description via parser
    if rec.get("descripcion"):
        merge_parsed(rec, parse_description(rec["descripcion"]))
    return rec


def extract_photos(url, html):
    """
    Extrae TODAS las fotos de la propiedad. Las fotos de la galeria llevan
    en su nombre de archivo el slug del titulo de la publicacion, lo que
    permite descartar fotos de anuncios relacionados. Se deduplica por
    foto-id (MLM<digitos>) y se normaliza a la version grande (-O-, sin _2X).
    """
    # slug del titulo desde la URL: .../MLM-<id>-<slug>-_JM
    m = re.search(r"/MLM-?\d+-(.+?)-_JM", url)
    slug = m.group(1) if m else None
    slug_key = slug[:20] if slug else None

    all_urls = re.findall(
        r"https?://http2\.mlstatic\.com/D_[NQ_]*NP_(?:2X_)?[^\"'\s\\)]+\.(?:webp|jpg)",
        html, re.I,
    )

    def norm(u):
        # normaliza a version grande "original"
        u = u.replace("D_NQ_NP_2X_", "D_NQ_NP_").replace("D_Q_NP_2X_", "D_NQ_NP_")
        u = u.replace("D_Q_NP_", "D_NQ_NP_")
        u = re.sub(r"-[A-Z]-", "-O-", u, count=1)
        return u

    def pid(u):
        mm = re.search(r"-(MLM\d+)_", u)
        return mm.group(1) if mm else u

    seen, out = set(), []
    for u in all_urls:
        # Si tenemos slug, filtra solo fotos de esta publicacion
        if slug_key and slug_key not in u:
            continue
        i = pid(u)
        if i not in seen:
            seen.add(i)
            out.append(norm(u))
    return out


# ==========================================================================
# FASE 0 — LOGIN MANUAL (perfil persistente)
# ==========================================================================
@browser(
    headless=False,        # visible: TU inicias sesion aqui
    profile=PROFILE,       # <<< guarda cookies/sesion en disco y las reutiliza
    reuse_driver=True,
    close_on_crash=True,
    output=None,
)
def setup_login(driver: Driver, data=None):
    """Abre MercadoLibre para que inicies sesion manualmente UNA vez."""
    driver.google_get("https://www.mercadolibre.com.mx", bypass_cloudflare=True)
    print("\n" + "=" * 60)
    print(" Inicia sesion MANUALMENTE en la ventana del navegador.")
    print(" Cuando termines y veas tu cuenta logueada, vuelve aqui")
    print(" y presiona ENTER para guardar la sesion.")
    print("=" * 60)
    try:
        input(" >> ENTER cuando hayas iniciado sesion... ")
    except EOFError:
        driver.sleep(60)  # si no hay stdin, espera 60s
    print("Sesion guardada en el perfil:", PROFILE)
    return True


# ==========================================================================
# FASE 1 — RECOLECCION DE URLs  (usa el perfil con sesion)
# ==========================================================================
@browser(
    headless=True,
    profile=PROFILE,       # <<< reutiliza la sesion guardada
    block_images=True,
    reuse_driver=True,
    max_retry=3,
    close_on_crash=True,
    output=None,
)
def do_scrape_all(driver: Driver, data):
    search_url = data["search_url"]
    max_pages = data.get("max_pages")
    max_ads = data.get("max_ads")

    collected, seen = [], set()
    total_pages = None
    page_num = 1
    empty_streak = 0
    page_iter = tqdm(desc="  Collecting pages", unit="pg")

    driver.get(build_page_url(search_url, 1))

    while True:
        if max_pages and page_num > max_pages:
            break
        if total_pages and page_num > total_pages:
            break

        url = build_page_url(search_url, page_num)
        if page_num > 1:
            driver.get(url)

        try:
            driver.wait_for_element('li.ui-search-layout__item', wait=15)
        except Exception:
            page_iter.close()
            print(f"[Fase 1] page {page_num}: sin resultados -> stop")
            break

        cur = driver.current_url
        if page_num > 1 and "_Desde_" not in cur:
            page_iter.close()
            print(f"[Fase 1] page {page_num}: redirigido a {cur} -> stop")
            break

        driver.scroll_to_bottom(smooth_scroll=True)
        human_delay(1.0, 2.0)
        html = driver.page_html

        if total_pages is None:
            total = total_results(html)
            if total:
                total_pages = max(1, math.ceil(total / PER_PAGE))
                print(f"[Fase 1] Total: {total} anuncios -> {total_pages} paginas")
                page_iter = tqdm(total=total_pages, desc=f"  Collecting {total} results", unit="pg")

        new = 0
        for u in ads_from_results(html):
            mid = MLM_ID_RE.search(u).group(1)
            if mid not in seen:
                seen.add(mid)
                collected.append(u)
                new += 1
        page_iter.set_description(f"  Page {page_num}: +{new} (acum {len(collected)})")
        page_iter.update(1)

        if new == 0:
            empty_streak += 1
            if empty_streak >= 10:
                print(f"[Fase 1] page {page_num}: 10 consecutive empty pages -> stopping early")
                break
        else:
            empty_streak = 0

        page_num += 1
        human_delay()

    print(f"\n[Fase 1] {len(collected)} anuncios totales\n")
    if max_ads:
        collected = collected[:max_ads]

    # Reconcile with DB: skip URLs already stored, delete vanished ones.
    try:
        from src import db
        collected = db.sync("mercadolibre", collected)
    except Exception as e:
        print(f"[db] sync skipped: {e}")

    results = []
    cp = Checkpoint("output/ml_checkpoint.json")
    collected = cp.resume(collected)
    setup_graceful_shutdown()
    pbar_ml = tqdm(total=len(collected), desc="Fase 2 (ML)", unit="anuncio")
    for url in collected:
        if should_stop():
            print("\n[shutdown] Stopping Phase 2 after current item...")
            break
        for attempt in range(1, 4):
            try:
                driver.get(url)
                driver.wait_for_element("h1.ui-pdp-title", wait=15)

                if "login" in driver.current_url or "registration" in driver.current_url:
                    print(f"   [!] Muro de login en {url} -> revisa la sesion (corre 'login')")
                    results.append({"url": url, "error": "login_required"})
                    cp.done(url)
                    break

                try:
                    driver.run_js(
                        "var b=[].slice.call(document.querySelectorAll('button,a,span'))"
                        ".find(e=>/Ver descripción completa|Mostrar más/i.test(e.textContent)); "
                        "if(b) b.click();"
                    )
                    human_delay(0.4, 0.9)
                except Exception:
                    pass

                html = driver.page_html
                rec = parse_detail(url, html)
                print(f"   OK {rec.get('codigo_publicacion')} "
                      f"({rec['num_imagenes']} fotos) {rec.get('tipo_transaccion')}")
                results.append(rec)
                cp.done(url)
                break  # success — exit retry loop
            except Exception as e:
                if attempt < 3:
                    delay = 3 * attempt
                    print(f"   [retry] {url} — attempt {attempt}/3: {e}, waiting {delay}s")
                    human_delay(delay, delay + 0.5)
                else:
                    print(f"   error en {url}: {e} [after 3 retries]")
                    results.append({"url": url, "error": f"{e} [after 3 retries]"})
        pbar_ml.update(1)
    pbar_ml.close()
    return results

def scrape(search_url, max_pages=None, max_ads=None):
    results = do_scrape_all({"search_url": search_url, "max_pages": max_pages, "max_ads": max_ads})

    os.makedirs("output", exist_ok=True)
    atomic_write_json(results, OUTPUT_FILE, indent=2)
    # Clear checkpoint on successful completion
    cp = __import__("src.utils", fromlist=["Checkpoint"]).Checkpoint("output/ml_checkpoint.json")
    cp.clear()
    print(f"\nGuardado {OUTPUT_FILE} ({len(results)} registros)")
    try:
        from src import db
        db.upsert("mercadolibre", results)
    except Exception as e:
        print(f"[db] upsert failed: {e}")
    return results


if __name__ == "__main__":
    SEARCH_URL = "https://inmuebles.mercadolibre.com.mx/locales-comerciales/renta/nuevo-leon/#applied_filter_id%3Dstate%26applied_filter_name%3DUbicaci%C3%B3n%26applied_filter_order%3D1%26applied_value_id%3DTUxNUE5VRTEzNTU%26applied_value_name%3DNuevo+Le%C3%B3n%26applied_value_order%3D18%26applied_value_results%3D817%26is_custom%3Dfalse%26view_more_flag%3Dtrue"

    # Paso 1 (una sola vez): iniciar sesion manualmente y guardar el perfil
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        setup_login()
    else:
        # Paso 2: scrapear reutilizando la sesion guardada (todas las paginas y anuncios)
        scrape(SEARCH_URL)
