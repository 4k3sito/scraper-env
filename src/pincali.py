#!/usr/bin/env python3
"""
Pincali (EasyBroker) — locales/terrenos comerciales en venta en Nuevo León.
Motor: Crawlee (PlaywrightCrawler — driver, proxy, sesiones, reintentos,
deteccion de bloqueo nativa) + Scrapling (parser robusto).

FASE 1: pagina ?page=N y recolecta URLs /inmueble/ unicas (para hasta agotar).
FASE 2: entra a cada detalle y extrae los campos estructurados.

Salida: output/pincali.json (+ upsert a Supabase via src.db).
Config: clave "pincali" en scrape_links.json (cae al SEARCH_URL por defecto).

Uso:
    python -m src.pincali
    python -m src.pincali --selftest <URL>     # valida extraccion de coords en 1 URL
"""

import asyncio
import os, sys
import json
import re
from datetime import timedelta
from urllib.parse import urljoin

from tqdm import tqdm
from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.proxy_configuration import ProxyConfiguration
from crawlee.storage_clients import MemoryStorageClient
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapling.parser import Selector
from src.utils import atomic_write_json, setup_graceful_shutdown, Checkpoint
from src.parser import parse_description, merge_parsed
from src.proxy import res_tier


# ── Phase 1 checkpoint ──────────────────────────────────────────────────────
# Un archivo por search_url (no uno compartido): las 5 URLs de scrape_links.json
# corren en paralelo (ver scrape()), y un checkpoint compartido se pisaria
# entre corrutinas concurrentes.
import hashlib

def _phase1_cp_path(search_url):
    slug = hashlib.sha1(search_url.encode()).hexdigest()[:10]
    return f"output/pincali_phase1_{slug}.json"

def _load_phase1(search_url):
    """Retorna (completed_pages, collected) para este search_url."""
    try:
        with open(_phase1_cp_path(search_url)) as f:
            d = json.load(f)
        return set(d.get("completed_pages", [])), d.get("collected", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), []

def _save_phase1(search_url, completed_pages, collected):
    os.makedirs("output", exist_ok=True)
    atomic_write_json({
        "search_url": search_url,
        "completed_pages": sorted(completed_pages),
        "collected": collected,
    }, _phase1_cp_path(search_url))

def _clear_phase1(search_url):
    path = _phase1_cp_path(search_url)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    try:
        os.remove(path.replace(".json", ".tmp"))
    except FileNotFoundError:
        pass


BASE_URL = "https://www.pincali.com"
SEARCH_URL = "https://www.pincali.com/inmuebles/propiedades-en-venta-en-nuevo-leon"
# Local comercial + Local en CC + Terreno comercial
QUERY = "property_type_ids=29061-28701-28661"
OUTPUT_FILE = "output/pincali.json"
LISTING_SELECTOR = 'a[href^="/inmueble/"]'
MAX_PAGES = 60  # tope de seguridad; la parada real es por paginas vacias
MIN_COLLECTED = 100  # si se agota antes de esto, algo salio mal -> stop

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


async def collect_listing_urls(search_url: str) -> list[str]:
    """Recolecta URLs de listings paginando hasta agotar.

    Robusta: Crawlee reintenta paginas bloqueadas con sesion/proxy nuevos
    automaticamente (retry_on_blocked). Checkpoint de Fase 1 para reanudar
    si el scraper se interrumpe. Encadena pagina N+1 solo si la pagina N
    aporto resultados nuevos (para exactamente en "hasta agotar").
    """
    sep = "&" if "?" in search_url else "?"

    completed_pages, collected = _load_phase1(search_url)
    if collected:
        print(f"   [cp] Fase 1 checkpoint encontrado ({search_url}): {len(collected)} URLs, {len(completed_pages)} paginas completadas")
        seen = set(collected)
    else:
        seen = set()

    page_iter = tqdm(total=MAX_PAGES, desc="  Collecting pages", unit="pg", leave=False)
    page_iter.update(len(completed_pages))

    # Residencial tambien en Fase 1: el datacenter (BUYPROXIES94952) empezo a
    # devolver 405 de forma consistente hoy tras uso intensivo — probable IP
    # quemada, no flakiness transitoria. Mismo patron que Century21/Vivanuncios/
    # Inmuebles24.
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
        page_num = context.request.user_data["page_num"]
        await context.block_requests()
        try:
            await context.page.wait_for_selector(LISTING_SELECTOR, timeout=12000)
        except PlaywrightTimeoutError:
            pass  # puede ser una pagina legitimamente vacia; se decide abajo
        html = await context.page.content()

        new = 0
        for link in listing_urls_from_html(html):
            if link not in seen:
                seen.add(link)
                collected.append(link)
                new += 1
        completed_pages.add(page_num)
        _save_phase1(search_url, completed_pages, collected)
        page_iter.update(1)
        page_iter.set_postfix(unique=len(collected))
        context.log.info("page %d: +%d nuevos (acum %d)", page_num, new, len(collected))

        if new == 0:
            context.log.info("page %d: 0 nuevos -> stop", page_num)
            if len(collected) < MIN_COLLECTED:
                context.log.warning("solo %d URLs (< %d) -> posible bloqueo", len(collected), MIN_COLLECTED)
            return

        if page_num < MAX_PAGES:
            next_url = search_url if page_num + 1 == 1 else f"{search_url}{sep}page={page_num + 1}"
            await context.add_requests([
                Request.from_url(next_url, user_data={"page_num": page_num + 1}, unique_key=f"p{page_num + 1}")
            ])

    start_page = max(completed_pages) + 1 if completed_pages else 1
    if start_page <= MAX_PAGES:
        start_url = search_url if start_page == 1 else f"{search_url}{sep}page={start_page}"
        await crawler.run([
            Request.from_url(start_url, user_data={"page_num": start_page}, unique_key=f"p{start_page}")
        ])
    page_iter.close()
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
        "descripcion": None, "imagenes": [], "map_link": None,
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
    rec["map_link"] = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}" if lat is not None else None

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


async def extract_listing(urls: list[str]) -> list[dict]:
    """Entra a cada URL y devuelve la lista de records extraidos.

    URLs ya conocidas de antemano (vienen de Fase 1) -> a diferencia de la
    paginacion, esto si es 100% paralelizable. ConcurrencySettings explicito
    en vez del ramp-up conservador por defecto de Crawlee, para bajar el
    tiempo total de la Fase 2 (la fase cara: un fetch residencial por URL).
    """
    results = []

    proxy_config = ProxyConfiguration(tiered_proxy_urls=[res_tier()])
    crawler = PlaywrightCrawler(
        proxy_configuration=proxy_config,
        storage_client=MemoryStorageClient(),
        headless=True,
        max_request_retries=3,
        request_handler_timeout=timedelta(seconds=30),
        concurrency_settings=ConcurrencySettings(min_concurrency=5, desired_concurrency=10, max_concurrency=20),
    )

    @crawler.router.default_handler
    async def handler(context: PlaywrightCrawlingContext):
        await context.block_requests()
        await context.page.wait_for_selector("h1", timeout=15000)
        html = await context.page.content()
        rec = parse_detail(context.request.url, html)
        if _cp_checkpoint:
            _cp_checkpoint.done(context.request.url)
        results.append(rec)
        if pbar:
            pbar.update(1)

    await crawler.run([Request.from_url(u) for u in urls])
    return results


pbar = None

# Module-level checkpoint reference (set by scrape(), usado por extract_listing)
_cp_checkpoint = None


async def scrape(search_urls):
    global pbar, _cp_checkpoint
    # ponytail: secuencial, no asyncio.gather. Varias PlaywrightCrawler en el
    # MISMO proceso/event loop chocan internamente (verificado: 4/5 corridas
    # concurrentes se quedaban en 0 requests) — Crawlee no esta pensado para
    # multiples instancias concurrentes en un solo proceso. Fase 2 si soporta
    # concurrencia real (ConcurrencySettings) porque ahi es UNA sola instancia.
    all_urls, seen = [], set()
    for su in search_urls:
        print(f"\n[Fase 1] {su}")
        for u in await collect_listing_urls(su):
            if u not in seen:
                seen.add(u)
                all_urls.append(u)
    print(f"\n[Fase 1] {len(all_urls)} URLs unicas\n")
    for su in search_urls:
        _clear_phase1(su)  # Fase 1 completa, checkpoints ya no necesarios
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
    results = await extract_listing(all_urls)
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


async def _selftest(url):
    results = await extract_listing([url])
    rec = results[0] if results else {}
    print(json.dumps({k: rec.get(k) for k in ("titulo", "precio", "lat", "lng")},
                     ensure_ascii=False, indent=2))
    assert rec.get("lat") is None or _in_mx(rec["lat"], rec["lng"]), "coords fuera de MX"
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        asyncio.run(_selftest(sys.argv[2]))
        raise SystemExit

    try:
        with open("scrape_links.json", encoding="utf-8") as f:
            urls = json.load(f).get("pincali", [])
    except Exception:
        urls = []
    if not urls:
        urls = [f"{SEARCH_URL}?{QUERY}"]  # fallback al destino por defecto
    asyncio.run(scrape(urls))
