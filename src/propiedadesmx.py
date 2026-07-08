"""
Scraper robusto para propiedadesmexico.com
Motor principal: Crawlee (PlaywrightCrawler — driver, proxy, sesiones,
reintentos y deteccion de bloqueo nativa) + Scrapling (parser).

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

Uso:
    python -m src.propiedadesmx
    (edita SEARCH al final con la URL de busqueda que quieras)
"""

import asyncio
import os, json
import math
import re
from datetime import timedelta
from urllib.parse import (
    urljoin, urlparse, parse_qs, urlencode, urlunparse, unquote,
)
from tqdm import tqdm

from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.proxy_configuration import ProxyConfiguration
from crawlee.storage_clients import MemoryStorageClient
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapling.parser import Selector
from src.utils import atomic_write_json, setup_graceful_shutdown, Checkpoint
from src.parser import parse_description, merge_parsed
from src.proxy import dc_tier, res_tier


pbar = None

# Module-level checkpoint reference (set by scrape(), used by extract_listings)
_cp_checkpoint = None


# ==========================================================================
# CONSTANTES
# ==========================================================================
BASE = "https://www.propiedadesmexico.com"
PER_PAGE = 12
MAX_PAGES = 200  # tope de seguridad; la parada real es por total_pages/empty_streak

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
() => {
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
}
"""


async def extract_gallery_images(page):
    """Extrae las URLs del carrusel del DOM. Devuelve (urls, total_esperado)."""
    try:
        res = await page.evaluate(GALLERY_JS)
        if isinstance(res, dict):
            return res.get("urls", []) or [], res.get("total")
    except Exception as e:
        print(f"   [img] error JS: {e}")
    return [], None


async def collect_all_images(page, ssr_images):
    """
    Combina las imagenes del DOM (carrusel) y del SSR (__NEXT_DATA__),
    priorizando el DOM cuando tenga imagenes, y usando SSR como respaldo.
    El contador del carrusel DOM a veces muestra menos del total real
    (por agrupacion de slides), por lo que no se usa como validacion
    cuando SSR trae mas fotos.
    """
    dom_imgs, expected = await extract_gallery_images(page)

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
async def collect_listing_urls(search_url: str, max_pages: int | None = None) -> list[str]:
    """
    Parada robusta (doble criterio):
      1. Total de paginas calculado desde el <h1> ("Se han encontrado N").
      2. Se detiene si una pagina no aporta listings nuevos.
    """
    collected, seen = [], set()
    state = {"total_pages": None, "empty_streak": 0}
    cap = min(max_pages or MAX_PAGES, MAX_PAGES)
    page_iter = tqdm(total=cap, desc="  Collecting pages", unit="pg", leave=False)

    # Tiered: intenta datacenter primero (barato), escala a residencial solo
    # si Crawlee detecta bloqueo — asi no hace falta apagar dc a mano si se
    # quema a mitad de corrida (le paso a Pincali con el grupo datacenter).
    proxy_config = ProxyConfiguration(tiered_proxy_urls=[dc_tier(), res_tier()])
    crawler = PlaywrightCrawler(
        proxy_configuration=proxy_config,
        storage_client=MemoryStorageClient(),
        headless=True,
        max_request_retries=3,
        request_handler_timeout=timedelta(seconds=45),
    )

    @crawler.router.default_handler
    async def handler(context: PlaywrightCrawlingContext):
        page_num = context.request.user_data["page_num"]
        await context.block_requests()
        # CLAVE: esperar la HIDRATACION de Next.js antes de leer el HTML.
        try:
            await context.page.wait_for_selector(LISTING_SELECTOR, timeout=15000)
        except PlaywrightTimeoutError:
            context.log.info("page %d: sin listings tras espera -> stop", page_num)
            return
        await context.page.mouse.wheel(0, 20000)
        await context.page.wait_for_timeout(1200)
        html = await context.page.content()

        if state["total_pages"] is None:
            total_pages, total = total_pages_from_html(html)
            if total_pages:
                state["total_pages"] = total_pages
                context.log.info("Total: %d resultados -> %d paginas", total, total_pages)

        new = 0
        for link in listing_hrefs_from_html(html):
            if link not in seen:
                seen.add(link)
                collected.append(link)
                new += 1
        page_iter.update(1)
        page_iter.set_description(f"  Page {page_num}: +{new} (acum {len(collected)})")
        context.log.info("page %d: +%d (acum %d)", page_num, new, len(collected))

        if new == 0:
            state["empty_streak"] += 1
            if state["empty_streak"] >= 10:
                context.log.info("page %d: 10 paginas vacias seguidas -> stop", page_num)
                return
        else:
            state["empty_streak"] = 0

        next_num = page_num + 1
        if next_num <= cap and (state["total_pages"] is None or next_num <= state["total_pages"]):
            next_url = set_page_param(search_url, next_num)
            await context.add_requests([
                Request.from_url(next_url, user_data={"page_num": next_num}, unique_key=f"p{next_num}")
            ])

    first_url = set_page_param(search_url, 1)
    await crawler.run([Request.from_url(first_url, user_data={"page_num": 1}, unique_key="p1")])
    page_iter.close()
    return collected


# ==========================================================================
# FASE 2 — EXTRACCION POR LISTING
# ==========================================================================
async def extract_listings(urls: list[str]) -> list[dict]:
    """Entra a cada URL y devuelve la lista de records extraidos."""
    results = []

    proxy_config = ProxyConfiguration(tiered_proxy_urls=[res_tier()])
    crawler = PlaywrightCrawler(
        proxy_configuration=proxy_config,
        storage_client=MemoryStorageClient(),
        headless=True,
        max_request_retries=3,
        request_handler_timeout=timedelta(seconds=45),
    )

    @crawler.router.default_handler
    async def handler(context: PlaywrightCrawlingContext):
        # block_images=False en el original: las imagenes deben cargar para
        # el carrusel, asi que solo bloqueamos CSS/fuentes, no imagenes.
        # state="attached": <script> nunca es "visible" (sin caja visual),
        # el default de Playwright esperaria eso para siempre y haria timeout.
        await context.page.wait_for_selector("script#__NEXT_DATA__", timeout=15000, state="attached")

        # Esperar a que el carrusel renderice para capturar TODAS las fotos
        try:
            await context.page.wait_for_selector(".swiper-pagination-total", timeout=10000)
        except PlaywrightTimeoutError:
            pass  # algunas publicaciones podrian tener 1 sola foto sin contador

        await context.page.wait_for_timeout(1000)
        html = await context.page.content()
        record = parse_one(context.request.url, html)

        # Imagenes: requieren el DOM renderizado
        imgs, expected, status = await collect_all_images(context.page, record.get("_ssr_images"))
        record["imagenes"] = imgs
        record["num_imagenes"] = len(imgs)
        record["imagenes_status"] = status
        record.pop("_ssr_images", None)

        if _cp_checkpoint:
            _cp_checkpoint.done(context.request.url)
        results.append(record)
        if pbar:
            pbar.update(1)

    async def failed_handler(context: PlaywrightCrawlingContext, error: Exception):
        results.append({"url": context.request.url, "error": str(error)})
        if pbar:
            pbar.update(1)

    crawler.failed_request_handler(failed_handler)

    await crawler.run([Request.from_url(u) for u in urls])
    return results


# ==========================================================================
# ORQUESTADOR
# ==========================================================================
async def scrape(search_urls, max_pages=None, max_listings=None):
    all_urls = []

    # ----- FASE 1 -----
    for search_url in search_urls:
        print(f"\n[Fase 1] Procesando URL: {search_url}")
        urls = await collect_listing_urls(search_url, max_pages=max_pages)
        all_urls.extend(urls)

    unique_urls = list(dict.fromkeys(all_urls))
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

    _cp_checkpoint = Checkpoint("output/propiedades_checkpoint.json")
    unique_urls = _cp_checkpoint.resume(unique_urls)
    if not unique_urls:
        raise SystemExit("All URLs already extracted — checkpoint shows nothing to do.")

    setup_graceful_shutdown()
    pbar = tqdm(total=len(unique_urls), desc="Fase 2 (Propiedades)")
    results = await extract_listings(unique_urls)
    if pbar:
        pbar.close()

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


async def main():
    try:
        with open("scrape_links.json", "r", encoding="utf-8") as f:
            links_data = json.load(f)
            search_urls = links_data.get("propiedadesmx", [])
    except Exception as e:
        print(f"Error cargando scrape_links.json: {e}")
        search_urls = []

    if search_urls:
        # max_pages / max_listings son opcionales (utiles para pruebas)
        await scrape(search_urls, max_pages=None, max_listings=None)
    else:
        print("No hay URLs para procesar. Agrega enlaces a la clave 'propiedadesmx' en scrape_links.json.")


if __name__ == "__main__":
    asyncio.run(main())
