"""
Scraper robusto para propiedadesmexico.com
Motor principal: Botasaurus

FASE 1: recolecta las URLs de todos los listings paginando por ?page=N
        (robusto para cualquier busqueda/filtro de la web).
FASE 2: entra a cada listing y extrae:
        titulo, precio, tipo de transaccion (venta/renta), codigo de
        publicacion (PM-XXXXXXXX), direccion, caracteristicas, descripcion
        completa y TODAS las imagenes de la galeria.

Fuente de datos primaria: el JSON embebido en <script id="__NEXT_DATA__">
(props.pageProps.propiedad[0]) -> robusto ante cambios de diseño.
Las imagenes se toman del carrusel renderizado (DOM) porque el array
'images' del SSR a veces viene incompleto; se combina con el SSR y se
valida contra el contador del carrusel para garantizar que esten TODAS.

Requisitos:
    pip install botasaurus

Uso:
    python -m src.propiedadesmx
    (edita SEARCH al final con la URL de busqueda que quieras)
"""

import os, json
import math
import re
import time
import random
from urllib.parse import (
    urljoin, urlparse, parse_qs, urlencode, urlunparse, unquote,
)
from tqdm import tqdm

from botasaurus.browser import browser, Driver
from scrapling.parser import Selector
from src.utils import atomic_write_json, setup_graceful_shutdown, should_stop, Checkpoint, setup_log
from src.parser import parse_description, merge_parsed

pbar = None

# Module-level checkpoint reference (set by scrape(), used by extract_listings)
_cp_checkpoint = None


# ==========================================================================
# CONSTANTES
# ==========================================================================
BASE = "https://www.propiedadesmexico.com"
PER_PAGE = 12

# Selector estable: el patron /<a>/<b>/<c>/PM-<digitos> identifica un listing
LISTING_HREF_RE = re.compile(r"/[^/\s]+/[^/\s]+/[^/\s]+/PM-\d+")
PM_CODE_RE = re.compile(r"PM-\d+")
LISTING_SELECTOR = 'a[href*="/PM-"]'

# Dominios donde viven las fotos reales de las propiedades
PROP_IMG_RE = re.compile(r"amazonaws|nocnok-img|ImagesLicAPI", re.I)

OUTPUT_FILE = "output/propiedades.json"


# ==========================================================================
# UTILIDADES
# ==========================================================================
def set_page_param(url, page_num):
    """Reemplaza/agrega ?page=N preservando TODOS los filtros de la busqueda."""
    parts = urlparse(url)
    q = parse_qs(parts.query)
    q["page"] = [str(page_num)]
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse(parts._replace(query=new_q))


def human_delay(a=1.2, b=3.0):
    """Pausa aleatoria para simular ritmo humano."""
    time.sleep(random.uniform(a, b))


def clean_html_text(text):
    """Convierte <br> en saltos de linea y elimina etiquetas HTML simples."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def maps_link(lat, lng):
    """Construye un enlace de Google Maps a partir de las coordenadas.
    propiedadesmexico.com no expone un enlace de mapa en el markup, asi que lo
    generamos nosotros. Devuelve None si falta alguna coordenada."""
    if lat in (None, "", 0) or lng in (None, "", 0):
        return None
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"


def _first(node, sel):
    found = node.css(sel)
    return found[0] if found else None


def _text(node):
    if node is None:
        return None
    t = node.text
    return re.sub(r"\s+", " ", t).strip() if t else None


# ==========================================================================
# PARSEO DE __NEXT_DATA__
# ==========================================================================
def parse_next_data(html):
    """Devuelve el objeto propiedad[0] (pagina de detalle) o None."""
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        prop = data["props"]["pageProps"].get("propiedad")
        if isinstance(prop, list) and prop:
            return prop[0]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def listing_hrefs_from_html(html):
    """Extrae las URLs de listings (/PM-\\d+) del HTML ya renderizado."""
    page = Selector(html)
    out = []
    for a in page.css(LISTING_SELECTOR):
        href = a.attrib.get("href", "")
        if href and LISTING_HREF_RE.search(href):
            out.append(urljoin(BASE, href.split("?")[0]))
    return list(dict.fromkeys(out))  # unicos, en orden


def total_pages_from_html(html):
    """Calcula el total de paginas desde el <h1> ('Se han encontrado N')."""
    m = re.search(r"encontrado\s+([\d,]+)", html, re.I)
    if m:
        total = int(m.group(1).replace(",", ""))
        return max(1, math.ceil(total / PER_PAGE)), total
    return None, None


# ==========================================================================
# EXTRACCION DE IMAGENES (todas las fotos de la galeria)
# ==========================================================================
# JS que aisla el primer swiper con paginacion (galeria principal),
# ignora los carruseles de 'sugerencias', decodifica /_next/image y
# descarta slides duplicados de Swiper (modo loop).
GALLERY_JS = r"""
return (function(){
  function decode(u){
    if(u && u.indexOf('/_next/image')>-1){
      var qs=(u.split('?')[1]||'');
      var p=new URLSearchParams(qs).get('url');
      if(p) return decodeURIComponent(p);
    }
    return u;
  }
  var swipers = Array.prototype.slice.call(document.querySelectorAll('.swiper'));
  var gallery=null;
  for(var i=0;i<swipers.length;i++){
    if(swipers[i].querySelector('.swiper-pagination-total')){ gallery=swipers[i]; break; }
  }
  if(!gallery) return {urls:[], total:null};
  var totalLabel = gallery.querySelector('.swiper-pagination-total');
  var total = totalLabel ? parseInt(totalLabel.textContent.trim(),10) : null;
  var slides = Array.prototype.slice.call(
    gallery.querySelectorAll('.swiper-slide:not(.swiper-slide-duplicate)'));
  var urls=[], seen={};
  for(var j=0;j<slides.length;j++){
    var img = slides[j].querySelector('img');
    if(!img) continue;
    var u = decode(img.getAttribute('src') || img.currentSrc || '');
    if(!u) continue;
    u = u.split('?')[0];
    if(!/amazonaws|nocnok-img|ImagesLicAPI/i.test(u)) continue;
    if(!seen[u]){ seen[u]=1; urls.push(u); }
  }
  return {urls:urls, total:total};
})();
"""


def run_js(driver, script):
    """Ejecuta JS en Botasaurus de forma tolerante al nombre del metodo."""
    for meth in ("run_js", "evaluate", "execute_script"):
        fn = getattr(driver, meth, None)
        if callable(fn):
            return fn(script)
    raise AttributeError("No se encontro metodo de ejecucion JS en el Driver")


def extract_gallery_images(driver):
    """Extrae las URLs del carrusel del DOM. Devuelve (urls, total_esperado)."""
    try:
        res = run_js(driver, GALLERY_JS)
        if isinstance(res, dict):
            return res.get("urls", []) or [], res.get("total")
    except Exception as e:
        print(f"   [img] error JS: {e}")
    return [], None


def collect_all_images(driver, ssr_images):
    """
    Combina las imagenes del DOM (carrusel) y del SSR (__NEXT_DATA__),
    priorizando el DOM cuando tenga imagenes, y usando SSR como respaldo.
    El contador del carrusel DOM a veces muestra menos del total real
    (por agrupacion de slides), por lo que no se usa como validacion
    cuando SSR trae mas fotos.
    """
    dom_imgs, expected = extract_gallery_images(driver)

    # Normaliza SSR: unescape de entidades URL (%281%29 -> (1))
    ssr = [unquote(u.split("?")[0]) for u in (ssr_images or [])]

    # Elegimos la fuente con mas fotos
    chosen = dom_imgs if len(dom_imgs) >= len(ssr) else ssr

    # Si el carrusel anuncia mas fotos de las que sacamos, intenta la union
    if expected and len(chosen) < expected:
        union = list(dict.fromkeys(dom_imgs + ssr))
        if len(union) >= len(chosen):
            chosen = union

    status = "ok"
    # Solo warning si DOM extrajo fotos Y el total es menor al esperado
    if expected and dom_imgs and len(chosen) < expected:
        status = f"parcial: {len(chosen)}/{expected}"
        print(f"   [img] solo {status} fotos (esperadas {expected})")
    elif expected and len(chosen) != expected and len(chosen) > expected:
        # DOM expected count is often lower than actual (grouped pagination) — normal
        pass

    return chosen, expected, status


# ==========================================================================
# PARSEO DE UN LISTING (detalle)
# ==========================================================================
def parse_one(url, html):
    """Construye el registro de un listing desde el HTML (SSR + DOM fallback)."""
    data = parse_next_data(html)

    if data:  # ---- Fuente primaria: __NEXT_DATA__ ----
        coords = data.get("Coordenadas") or {}
        lat = coords.get("Lat")
        lng = coords.get("Lng")
        try:
            amenidades = json.loads(data.get("Amenidades") or "[]")
        except json.JSONDecodeError:
            amenidades = []

        record = {
            "url": url,
            "codigo_publicacion": data.get("id"),               # PM-02136193
            "titulo": data.get("title"),
            "precio": data.get("price") or data.get("Precio"),
            "precio_numerico": data.get("PriceLink"),
            "moneda": data.get("Moneda"),
            "tipo_transaccion": data.get("Operacion"),          # Venta / Renta
            "tipo_propiedad": data.get("Tipo"),
            "direccion": {
                "colonia": data.get("neighborhood"),
                "ciudad": data.get("city"),
                "estado": data.get("state"),
                "lat": lat,
                "lng": lng,
                "map_link": maps_link(lat, lng),
            },
            "caracteristicas": {
                "construccion": data.get("construction"),
                "terreno": data.get("land"),
                "recamaras": data.get("bedrooms") or None,
                "banos": data.get("bathrooms") or None,
                "medios_banos": data.get("halfbathrooms") or None,
                "estacionamientos": data.get("cars") or None,
                "precio_m2_construccion": data.get("Price_Const"),
                "amenidades": amenidades,
            },
            "descripcion": clean_html_text(data.get("description")),
            "inmobiliaria": data.get("Inmobiliaria"),
            "asesor": data.get("Asesor"),
            "imagen_principal": data.get("imagen_principal"),
            "_ssr_images": data.get("images") or [],   # temporal (se usa y borra)
            "_source": "next_data",
        }
        if record.get("descripcion"):
            merge_parsed(record, parse_description(record["descripcion"]))
        return record

    # ---- Fallback: DOM visible (si __NEXT_DATA__ no esta disponible) ----
    page = Selector(html)
    body = " ".join(t for t in page.css("body ::text").getall() if t)

    def field(label):
        m = re.search(re.escape(label) + r"\s*([^\n]+?)(?:\s{2,}|$)", body)
        return m.group(1).strip() if m else None

    # Coordenadas de respaldo: buscar el bloque Coordenadas en el HTML crudo
    fb_lat = fb_lng = None
    mco = re.search(
        r'"Coordenadas"\s*:\s*\{\s*"Lat"\s*:\s*"?(-?\d+\.\d+)"?\s*,'
        r'\s*"Lng"\s*:\s*"?(-?\d+\.\d+)"?',
        html,
    )
    if mco:
        fb_lat, fb_lng = mco.group(1), mco.group(2)

    code = PM_CODE_RE.search(url)
    title_el = _first(page, "h1") or _first(page, "h2")
    return {
        "url": url,
        "codigo_publicacion": code.group(0) if code else None,
        "titulo": _text(title_el) if title_el is not None else None,
        "precio": field("Precio de operación"),
        "tipo_transaccion": field("Tipo de operación"),
        "tipo_propiedad": field("Tipo de propiedad"),
        "direccion": {
            "texto": field("Dirección"),
            "lat": fb_lat,
            "lng": fb_lng,
            "map_link": maps_link(fb_lat, fb_lng),
        },
        "caracteristicas": {
            "construccion": field("Superficie de construcción"),
            "banos": field("Baños y medios baños"),
            "estacionamientos": field("Estacionamientos"),
        },
        "descripcion": None,
        "_ssr_images": [],
        "_source": "dom_fallback",
    }


# ==========================================================================
# FASE 1 — RECOLECCION DE URLs
# ==========================================================================
@browser(
    headless=False,        # headful = menos detectable; pon True si lo prefieres
    block_images=True,     # acelera (no necesitamos fotos en esta fase)
    reuse_driver=True,     # UNA sola sesion para toda la paginacion
    max_retry=3,
    close_on_crash=True,
    output=None,
)
def collect_listing_urls(driver: Driver, data):
    """
    data = {"search_url": <url>, "max_pages": int|None}
    Devuelve la lista de URLs de listings (unicas).

    Parada robusta (doble criterio):
      1. Total de paginas calculado desde el <h1> ("Se han encontrado N").
      2. Se detiene si una pagina no aporta listings nuevos.
    """
    search_url = data["search_url"]
    max_pages = data.get("max_pages")

    collected, seen = [], set()
    total_pages = None
    page_num = 1
    empty_streak = 0

    # Primera visita con referer de Google (mas natural)
    driver.google_get(set_page_param(search_url, 1), bypass_cloudflare=True)

    page_iter = tqdm(desc=f"  Collecting pages", unit="pg")
    while True:
        if max_pages and page_num > max_pages:
            break
        if total_pages and page_num > total_pages:
            break

        url = set_page_param(search_url, page_num)
        if page_num > 1:                  # page 1 ya cargada arriba
            driver.get(url)

        # CLAVE: esperar la HIDRATACION de Next.js antes de leer el HTML.
        try:
            driver.wait_for_element(LISTING_SELECTOR, wait=15)
        except Exception:
            page_iter.close()
            print(f"[Fase 1] page {page_num}: sin listings tras espera -> stop")
            break

        driver.scroll_to_bottom(smooth_scroll=True)
        human_delay(0.8, 1.6)

        html = driver.page_html

        if total_pages is None:
            total_pages, total = total_pages_from_html(html)
            if total_pages:
                print(f"[Fase 1] Total: {total} resultados -> {total_pages} paginas")
                page_iter = tqdm(total=total_pages, desc=f"  Collecting {total} results", unit="pg")

        new = 0
        for link in listing_hrefs_from_html(html):
            if link not in seen:
                seen.add(link)
                collected.append(link)
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

    return collected


# ==========================================================================
# FASE 2 — EXTRACCION POR LISTING
# Botasaurus ITERA la lista automaticamente: la funcion recibe UNA url (str)
# y devuelve UN dict. NO hacer 'for url in urls' aqui dentro.
# ==========================================================================
@browser(
    headless=False,
    block_images=False,       # necesario: las imagenes deben cargar para el carrusel
    reuse_driver=True,
    max_retry=3,
    close_on_crash=True,
    output=None,
)
def extract_listings(driver: Driver, url: str):
    """Recibe UNA url, devuelve UN dict con todos los campos + imagenes."""
    global pbar
    for attempt in range(1, 4):
        try:
            driver.google_get(url, bypass_cloudflare=True)
            driver.wait_for_element("script#__NEXT_DATA__", wait=15)

            # Esperar a que el carrusel renderice para capturar TODAS las fotos
            try:
                driver.wait_for_element(".swiper-pagination-total", wait=10)
            except Exception:
                pass  # algunas publicaciones podrian tener 1 sola foto sin contador

            human_delay()
            html = driver.page_html
            record = parse_one(url, html)

            # Imagenes: requieren el DOM renderizado -> usamos el driver
            imgs, expected, status = collect_all_images(driver, record.get("_ssr_images"))
            record["imagenes"] = imgs
            record["num_imagenes"] = len(imgs)
            record["imagenes_status"] = status        # "ok" o "parcial: x/y"
            record.pop("_ssr_images", None)

            # Checkpoint
            _cp_checkpoint.done(url)

            if pbar: pbar.update(1)
            return record
        except Exception as e:
            if attempt < 3:
                delay = 3 * attempt
                print(f"   [retry] {url} — attempt {attempt}/3: {e}, waiting {delay}s")
                time.sleep(delay)
            else:
                if pbar: pbar.update(1)
                return {"url": url, "error": f"{e} [after 3 retries]"}


# ==========================================================================
# ORQUESTADOR
# ==========================================================================
def scrape(search_urls, max_pages=None, max_listings=None):
    all_urls = []

    # ----- FASE 1 -----
    for search_url in search_urls:
        print(f"\n[Fase 1] Procesando URL: {search_url}")
        urls = collect_listing_urls({"search_url": search_url, "max_pages": max_pages})
        all_urls.extend(urls)

    unique_urls = []
    seen = set()
    for u in all_urls:
        if u not in seen:
            unique_urls.append(u)
            seen.add(u)

    print(f"\n[Fase 1] {len(all_urls)} listings totales, {len(unique_urls)} únicos.\n")

    if max_listings:
        unique_urls = unique_urls[:max_listings]

    # Reconcile with DB: skip URLs already stored, delete vanished ones.
    try:
        from src import db
        unique_urls = db.sync("propiedadesmx", unique_urls)
    except Exception as e:
        print(f"[db] sync skipped: {e}")
    if not unique_urls:
        raise SystemExit("Nothing new to scrape — DB already up to date.")

    # ----- FASE 2 -----
    global pbar, _cp_checkpoint

    # Checkpoint for resumable Phase 2
    _cp_checkpoint = Checkpoint("output/propiedades_checkpoint.json")
    unique_urls = _cp_checkpoint.resume(unique_urls)
    if not unique_urls:
        raise SystemExit("All URLs already extracted — checkpoint shows nothing to do.")

    setup_graceful_shutdown()
    pbar = tqdm(total=len(unique_urls), desc="Fase 2 (Propiedades)")
    results = extract_listings(unique_urls)
    if pbar: pbar.close()

    os.makedirs("output", exist_ok=True)
    atomic_write_json(results, OUTPUT_FILE, indent=2)
    _cp_checkpoint.clear()
    print(f"\nGuardado {OUTPUT_FILE} ({len(results)} registros)")
    try:
        from src import db
        db.upsert("propiedadesmx", results)
    except Exception as e:
        print(f"[db] upsert failed: {e}")
    return results


if __name__ == "__main__":
    try:
        with open("scrape_links.json", "r", encoding="utf-8") as f:
            links_data = json.load(f)
            search_urls = links_data.get("propiedadesmx", [])
    except Exception as e:
        print(f"Error cargando scrape_links.json: {e}")
        search_urls = []

    if search_urls:
        # max_pages / max_listings son opcionales (utiles para pruebas)
        scrape(search_urls, max_pages=None, max_listings=None)
    else:
        print("No hay URLs para procesar. Agrega enlaces a la clave 'propiedadesmx' en scrape_links.json.")
