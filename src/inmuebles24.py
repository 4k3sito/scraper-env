# inmuebles24_scraper.py
# Crawlee (PlaywrightCrawler — driver, proxy, sesiones, reintentos y
# deteccion de bloqueo nativa) + Scrapling 0.4.9 (adaptive parsing)
#
# CAMBIO CLAVE — COORDENADAS:
#   En Inmuebles24 lat/lon NO estan en el JSON-LD (que suele ser solo
#   WebSite/Organization) ni en una URL de Google static map. Estan en un
#   objeto JSON inline bajo las claves postingGeolocation / geolocation con
#   el formato:  "latitude":17.9672...  "longitude":-92.9375...
#   (numeros SIN comillas, latitude antes que longitude). El HTML debe leerse
#   despues de la hidratacion (tras esperar el selector + un pequeño delay).

import asyncio
import os, sys
import json, csv, re, html as ihtml
from datetime import timedelta
from urllib.parse import urljoin

from tqdm import tqdm
from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.proxy_configuration import ProxyConfiguration
from crawlee.storage_clients import MemoryStorageClient
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapling.parser import Selector
from src.utils import atomic_write_json, setup_graceful_shutdown, Checkpoint, setup_log
from src.parser import parse_description, merge_parsed
from src.proxy import res_tier


ORIGIN = "https://www.inmuebles24.com"
MAX_PAGES = 57   # 1,689 results / ~30 per page ≈ 57 pages
LISTING_WAIT_SELECTOR = (
    '[data-qa="posting PROPERTY"], [data-posting-id], article[class*="card"], '
    '.posting-card, [class*="Posting"]'
)

# Module-level checkpoint ref (set in __main__, used by extract_details)
_cp_checkpoint = None
pbar = None


def _page(html: str) -> Selector:
    return Selector(html, auto_match=True, keep_comments=False)


def _first(node, sel):
    found = node.css(sel)
    return found[0] if found else None


def _clean(s):
    if not s:
        return None
    return re.sub(r"\s+", " ", ihtml.unescape(s)).strip()


def _text(node):
    return _clean(node.text) if node is not None else None


def _photoid(url: str) -> str:
    m = re.search(r"/(\d+)\.(?:jpg|jpeg|webp|png)", url, re.I)
    return m.group(1) if m else url


def valid_mx(lat, lon):
    """Filtro de cordura: Mexico aprox lat 14..33, lon -86..-118."""
    return (
        lat is not None and lon is not None
        and 14 < lat < 33 and -118 < lon < -86
    )


def extractcoords(html: str):
    """
    Inmuebles24: lat/lon viven en un JSON inline (postingGeolocation /
    geolocation). latitude antes que longitude, numeros sin comillas.
    Se prueban variantes y se valida que caigan dentro de Mexico.
    Devuelve (lat, lon) como floats, o (None, None).
    """
    # Metodo 1 (principal): latitude -> longitude, numeros con o sin comillas.
    m = re.search(
        r'"latitude"\s*:\s*"?(-?\d{1,3}\.\d+)"?'
        r'[^{}]{0,300}?"longitude"\s*:\s*"?(-?\d{1,3}\.\d+)"?',
        html, re.S
    )
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if valid_mx(lat, lon):
            return lat, lon

    # Metodo 2: orden invertido
    m = re.search(
        r'"longitude"\s*:\s*"?(-?\d{1,3}\.\d+)"?'
        r'[^{}]{0,300}?"latitude"\s*:\s*"?(-?\d{1,3}\.\d+)"?',
        html, re.S
    )
    if m:
        lat, lon = float(m.group(2)), float(m.group(1))
        if valid_mx(lat, lon):
            return lat, lon

    # Metodo 3: JSON-LD GeoCoordinates
    m = re.search(
        r'"GeoCoordinates"[^}]*?"latitude"\s*:\s*"?(-?\d+\.\d+)"?'
        r'[^}]*?"longitude"\s*:\s*"?(-?\d+\.\d+)"?',
        html, re.S
    )
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if valid_mx(lat, lon):
            return lat, lon

    # Metodo 4: claves cortas "lat"/"lng"
    m = re.search(
        r'"lat"\s*:\s*"?(-?\d{1,3}\.\d+)"?'
        r'[^{}]{0,200}?"(?:lng|lon)"\s*:\s*"?(-?\d{1,3}\.\d+)"?',
        html, re.S
    )
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if valid_mx(lat, lon):
            return lat, lon

    return None, None


# ----------------------- PHASE 1: collect listing URLs -----------------------
def _fallback_cards(html, origin):
    """Regex-based card extraction fallback when CSS selectors fail."""
    out = []
    for m in re.finditer(r'href=["\'](/propiedades/[^"\']+?)["\']', html):
        path = m.group(1).split("?")[0]
        pid = re.search(r"/propiedades/([^/]+)", path)
        out.append({
            "id": pid.group(1) if pid else path,
            "url": urljoin(origin, path),
        })
    return out


def parse_listing_cards(html: str):
    page = _page(html)
    out = []
    for card in page.css('[data-qa="posting PROPERTY"]'):
        cid = card.attrib.get("data-id")
        path = card.attrib.get("data-to-posting")
        if not path:
            a = _first(card, "a[href]")
            path = a.attrib.get("href") if a is not None else None
        if not path:
            continue
        path = path.split("?")[0]
        out.append({
            "id": cid or path,
            "url": path if path.startswith("http") else urljoin(ORIGIN, path),
        })
    return out


async def collect_urls_for(search_url: str) -> list[dict]:
    """Pagina un search_url y devuelve listings unicos {id, url}.

    Encadena la pagina N+1 solo si la pagina N aporto cards nuevas (para
    exactamente en "hasta agotar", igual que Fase 1 de Pincali).
    """
    base_path = search_url.split(".html")[0]
    seen = {}
    page_iter = tqdm(total=MAX_PAGES, desc="  Collecting pages", unit="pg", leave=False)

    # Residencial en las dos fases: verificado que este sitio devuelve 403
    # (bloqueo) con el proxy datacenter incluso para la busqueda (Fase 1),
    # a diferencia de Pincali/PropiedadesMX donde datacenter si funciona.
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

        title = await context.page.title()
        if "Just a moment" in title:
            raise RuntimeError("Cloudflare challenge not resolved")

        try:
            await context.page.wait_for_selector(LISTING_WAIT_SELECTOR, timeout=5000)
            await context.page.mouse.wheel(0, 20000)  # trigger lazy content
            await context.page.wait_for_timeout(400)
        except PlaywrightTimeoutError:
            pass  # fallback: intentar de todos modos con regex

        html = await context.page.content()
        cards = parse_listing_cards(html) or _fallback_cards(html, ORIGIN)

        added = 0
        for c in cards:
            if c["id"] not in seen:
                seen[c["id"]] = c
                added += 1
        page_iter.update(1)
        page_iter.set_description(f"  Page {page_num}: {len(cards)} cards, +{added} new")
        page_iter.set_postfix(unique=len(seen))
        context.log.info("page %d: %d cards, +%d new (total %d)", page_num, len(cards), added, len(seen))

        if not cards or added == 0:
            context.log.info("page %d: 0 cards nuevas -> stop", page_num)
            return

        if page_num < MAX_PAGES:
            next_num = page_num + 1
            next_url = f"{base_path}.html" if next_num == 1 else f"{base_path}-pagina-{next_num}.html"
            await context.add_requests([
                Request.from_url(next_url, user_data={"page_num": next_num}, unique_key=f"p{next_num}")
            ])

    first_url = f"{base_path}.html"
    await crawler.run([Request.from_url(first_url, user_data={"page_num": 1}, unique_key="p1")])
    page_iter.close()
    return list(seen.values())


async def collect_urls(search_urls: list[str]) -> list[dict]:
    """Pagina TODOS los search_urls y devuelve listings unicos {id, url}."""
    seen = {}
    for search_url in search_urls:
        print(f"\n[PHASE 1] Processing: {search_url}")
        for c in await collect_urls_for(search_url):
            if c["id"] not in seen:
                seen[c["id"]] = c
    return list(seen.values())


# ----------------------- PHASE 2: parse a rendered detail page -----------------------
def parse_detail(html: str) -> dict:
    page = _page(html)

    name = _text(_first(page, "h1"))

    # Price: regex over the main-features section text
    main = _first(page, ".section-main-features")
    price = None
    if main is not None:
        m = re.search(r"(?:Venta|Renta)?\s*MN\s*[\d,]+", _text(main) or "", re.I)
        price = m.group(0).strip() if m else None

    location = _text(_first(page, ".section-location-property h4")) \
        or _text(_first(page, ".section-location-property"))

    description = _text(_first(page, ".article-section-description"))

    icon_features = [_text(li) for li in page.css("#section-icon-features-property li")]
    icon_features = [f for f in icon_features if f]
    size = next((f for f in icon_features if "m²" in f), None)

    # Characteristics + specifications from general-features block
    specifications, characteristics = {}, []
    seen_items = set()
    for el in page.css('[class*="generalFeaturesProperty"] *'):
        if el.children:
            continue
        txt = _text(el)
        if not txt or len(txt) >= 60:
            continue
        if re.search(r"Conoce más|Características generales|Servicios", txt):
            continue
        if txt in seen_items:
            continue
        seen_items.add(txt)
        if ":" in txt:
            k, _, v = txt.partition(":")
            specifications[_clean(k)] = _clean(v)
        else:
            characteristics.append(txt)

    # Photos: naventcdn /avisos/
    photos, seen_p = [], set()
    for img in page.css("img"):
        src = img.attrib.get("src") or img.attrib.get("data-src") or ""
        if "naventcdn.com/avisos/" not in src or "empresas" in src:
            continue
        pid = _photoid(src)
        if pid in seen_p:
            continue
        seen_p.add(pid)
        photos.append(src)

    lat, lon = extractcoords(html)
    map_link = (
        f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
        if lat is not None else None
    )
    record = {
        "name": name, "price": price, "location": location, "size": size,
        "icon_features": icon_features, "specifications": specifications,
        "description": description, "characteristics": characteristics,
        "photos": photos, "photo_count": len(photos),
        "lat": lat, "lon": lon, "map_link": map_link,
    }
    if description:
        merge_parsed(record, parse_description(description))
    return record


async def extract_details(listings: list[dict]) -> list[dict]:
    """Entra a cada listing {id, url} y devuelve la lista de records extraidos."""
    results = []

    proxy_config = ProxyConfiguration(tiered_proxy_urls=[res_tier()])
    crawler = PlaywrightCrawler(
        proxy_configuration=proxy_config,
        storage_client=MemoryStorageClient(),
        headless=True,
        max_request_retries=3,
        request_handler_timeout=timedelta(seconds=30),
    )

    @crawler.router.default_handler
    async def handler(context: PlaywrightCrawlingContext):
        await context.block_requests()
        await context.page.wait_for_selector("h1", timeout=15000)
        html = await context.page.content()
        d = parse_detail(html)
        d.update(id=context.request.user_data["id"], url=context.request.url)
        if _cp_checkpoint:
            _cp_checkpoint.done(context.request.url)
        results.append(d)
        if pbar:
            pbar.update(1)

    async def failed_handler(context: PlaywrightCrawlingContext, error: Exception):
        results.append({"id": context.request.user_data["id"], "url": context.request.url, "error": str(error)})
        if pbar:
            pbar.update(1)

    crawler.failed_request_handler(failed_handler)

    await crawler.run([
        Request.from_url(l["url"], user_data={"id": l["id"]}) for l in listings
    ])
    return results


# ----------------------- output -----------------------
def write_csv(rows, path):
    cols = ["id", "url", "name", "price", "location", "size",
            "description", "specifications", "characteristics",
            "photo_count", "lat", "lon", "map_link"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            row = []
            for c in cols:
                v = r.get(c)
                if isinstance(v, list):
                    v = " | ".join(map(str, v))
                elif isinstance(v, dict):
                    v = json.dumps(v, ensure_ascii=False)
                row.append("" if v is None else v)
            w.writerow(row)


async def selftest(url=None):
    """Verifica coordenadas en una URL de detalle.
    Uso:  python -m src.inmuebles24 --selftest <URL>
    """
    if url is None:
        raise SystemExit("Pasa una URL de detalle:  python -m src.inmuebles24 --selftest <URL>")

    results = await extract_details([{"id": "test", "url": url}])
    result = results[0] if results else {}
    lat, lon = result.get("lat"), result.get("lon")
    ok = valid_mx(lat, lon)
    print("\n[selftest inmuebles24]")
    print(f"  url      : {url}")
    print(f"  lat      : {lat}")
    print(f"  lon      : {lon}")
    print(f"  map_link : {result.get('map_link')}")
    print(f"  valid_mx : {ok}")
    print(f"  {'PASS' if ok else 'FAIL — coordenadas no encontradas o fuera de Mexico'}")
    return ok


async def main():
    global pbar, _cp_checkpoint

    os.makedirs("output", exist_ok=True)
    log = setup_log("inmuebles24")

    if "--selftest" in sys.argv:
        idx = sys.argv.index("--selftest")
        _url = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        await selftest(_url)
        return

    try:
        with open("scrape_links.json", "r", encoding="utf-8") as f:
            links_data = json.load(f)
            search_urls = links_data.get("inmuebles24", [])
    except Exception as e:
        print(f"Error cargando scrape_links.json: {e}")
        search_urls = []

    if not search_urls:
        raise SystemExit("No URLs to process. Add links to 'inmuebles24' in scrape_links.json")

    sample_n = None
    if "--sample" in sys.argv:
        i = sys.argv.index("--sample")
        sample_n = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit() else 5

    print("PHASE 1: collecting URLs...")
    search_urls_list = search_urls[:1] if sample_n else search_urls
    urls = await collect_urls(search_urls_list)
    if sample_n:
        urls = urls[:sample_n]
    atomic_write_json(urls, "output/inm24_urls.json", indent=2)
    log.info("PHASE 1 complete: %d unique URLs", len(urls))

    if not urls:
        raise SystemExit("No URLs collected — aborting Phase 2.")

    # Reconcile with DB
    try:
        from src import db
        urls = db.sync("inmuebles24", urls)
    except Exception as e:
        print(f"[db] sync skipped: {e}")
    if not urls:
        raise SystemExit("Nothing new to scrape — DB already up to date.")

    # Checkpoint
    _cp_checkpoint = Checkpoint("output/inm24_checkpoint.json")
    urls = _cp_checkpoint.resume(urls, key=lambda x: x["url"])
    if not urls:
        raise SystemExit("All URLs already extracted — checkpoint shows nothing to do.")

    setup_graceful_shutdown()
    print("PHASE 2: extracting details...")
    pbar = tqdm(total=len(urls), desc="Fase 2 (Inmuebles24)")
    listings = await extract_details(urls)
    if pbar:
        pbar.close()
    listings = [r for r in listings if isinstance(r, dict)]
    atomic_write_json(listings, "output/inm24_listings.json", indent=2)
    write_csv(listings, "output/inm24_listings.csv")
    _cp_checkpoint.clear()
    try:
        from src import db
        db.upsert("inmuebles24", listings)
    except Exception as e:
        print(f"[db] upsert failed: {e}")
    ok = sum(1 for r in listings if "error" not in r)
    log.info("PHASE 2 complete: %d/%d OK", ok, len(listings))


if __name__ == "__main__":
    asyncio.run(main())
