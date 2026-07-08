# lamudi.py
# Crawlee (PlaywrightCrawler — driver, proxy, sesiones, reintentos y
# deteccion de bloqueo nativa) + Scrapling 0.4.9 (adaptive parsing)

import asyncio
import os, sys, json, csv, re, base64
from datetime import timedelta
from functools import lru_cache
from urllib.parse import urljoin

from tqdm import tqdm
from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.proxy_configuration import ProxyConfiguration
from crawlee.storage_clients import MemoryStorageClient
from scrapling.parser import Selector
from src.utils import atomic_write_json, setup_graceful_shutdown, setup_log, Checkpoint
from src.parser import parse_description, merge_parsed
from src.proxy import ApifyProxyConfig

# ponytail: datacenter para paginar (barato), residencial para el detalle
# (donde el sitio bloquea mas fuerte). Lazy: solo exige APIFY_PROXY_PASSWORD
# al correr de verdad, no al importar el modulo.
@lru_cache(maxsize=None)
def _proxy(groups):
    # "auto" -> grupo datacenter dedicado, con password propia (grupo comprado
    # aparte, no cubierto por APIFY_PROXY_PASSWORD general).
    if groups == "auto":
        return ApifyProxyConfig(groups="BUYPROXIES94952", password_env="APIFY_PROXY_PASSWORD_DATACENTER", country=None)
    return ApifyProxyConfig(groups=groups)


async def _dc_proxy_url(session_id=None, request=None, proxy_tier=None):
    return _proxy("auto").url(session=session_id or ApifyProxyConfig.new_session_id())


async def _res_proxy_url(session_id=None, request=None, proxy_tier=None):
    return _proxy("RESIDENTIAL").url(session=session_id or ApifyProxyConfig.new_session_id())


BASE = "https://www.lamudi.com.mx/nuevo-leon/monterrey/comercial/venta-al-por-menor/for-sale/"
BOUNDS = "-100.47016411545054,25.419606909860605,-100.09276415132233,25.838957457589444"
ORIGIN = "https://www.lamudi.com.mx"
MAX_PAGES = 30  # real set ends at page 16 (~454 cards / 446 unique)
EMPTY_STREAK_LIMIT = 10

SIZE_SPEC_KEYS = ("Superficie total", "Superficie de terreno",
                  "Superficie construida", "Superficie útil", "Superficie")

pbar = None
_cp_checkpoint = None


def _page(html: str) -> Selector:
    return Selector(html, auto_match=True, keep_comments=False)


def _first(node, sel):
    """css_first replacement: first matching element or None."""
    found = node.css(sel)
    return found[0] if found else None


def phototrue_key(url: str) -> str:
    """Decode lamudi's base64 image-transform URL to the underlying S3 key,
    so the same photo at different sizes dedups to one entry."""
    try:
        token = url.split("/")[-1].split("?")[0]
        token += "=" * (-len(token) % 4)  # pad base64 if needed
        meta = json.loads(base64.b64decode(token))
        return meta.get("key") or token
    except Exception:
        return url


def _text(node):
    if node is None:
        return None
    t = node.text
    return re.sub(r"\s+", " ", t).strip() if t else None


def _clean(s):
    return re.sub(r"\s+", " ", s).strip() if s else None


def _extract_coords(html: str):
    """Return (lat, lng) as floats for the property map location.

    Lamudi embeds coordinates in the initial HTML (no JS render needed) in two
    independent places. We try the structured JSON-LD GeoCoordinates first, then
    fall back to the inline `mapData.adLocationData.coordinates` JS config block.
    Returns (None, None) if neither is present.
    """
    # Primary: JSON-LD GeoCoordinates
    m = re.search(
        r'"GeoCoordinates"[^}]*?"latitude"\s*:\s*"?(-?\d+\.\d+)"?'
        r'[^}]*?"longitude"\s*:\s*"?(-?\d+\.\d+)"?',
        html,
    )
    # Fallback: inline JS mapData coordinates
    if not m:
        m = re.search(
            r'coordinates\s*:\s*\{\s*latitude\s*:\s*"(-?\d+\.\d+)"\s*,'
            r'\s*longitude\s*:\s*"(-?\d+\.\d+)"',
            html,
        )
    if not m:
        return None, None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None, None


def _maps_link(lat, lng):
    """Build a shareable Google Maps link from coordinates (Lamudi exposes no
    direct map link in the markup, so we construct one)."""
    if lat is None or lng is None:
        return None
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"


# ----------------------- PHASE 1: collect listing URLs -----------------------
def parse_listing_cards(html: str):
    page = _page(html)
    out = []
    for snip in page.css(".js-snippet"):
        a = _first(snip, 'a[href*="/detalle/"]')
        if a is None:
            continue
        href = (a.attrib.get("href") or "").split("?")[0]
        geo = {}
        try:
            geo = json.loads(snip.attrib.get("data-serp-map-hover-listing", "{}"))
        except Exception:
            pass
        out.append({
            "id": snip.attrib.get("data-idanuncio") or href,
            "url": urljoin(ORIGIN, href),
            "lat": geo.get("latitude"),
            "lng": geo.get("longitude"),
        })
    return out


async def collect_urls_for(base_url: str) -> list[dict]:
    """Pagina un search_url y devuelve listings unicos {id, url, lat, lng}.

    Encadena la pagina N+1 solo si la anterior no rompio la racha de vacias
    y el HTML anuncia una pagina siguiente (mismo criterio que la version
    Botasaurus original: has_next via 'page={p+1}' en el HTML + empty_streak).
    """
    if "?" in base_url:
        base, qs = base_url.split("?", 1)
        query_prefix = f"&{qs}"
    else:
        base, query_prefix = base_url, ""

    seen = {}
    state = {"empty_streak": 0}
    page_iter = tqdm(total=MAX_PAGES, desc="  Collecting pages", unit="pg", leave=False)

    proxy_config = ProxyConfiguration(new_url_function=_dc_proxy_url)
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
        await context.page.wait_for_selector(".js-snippet", timeout=15000)
        html = await context.page.content()
        cards = parse_listing_cards(html)

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
            state["empty_streak"] += 1
            if state["empty_streak"] >= EMPTY_STREAK_LIMIT:
                context.log.info("page %d: %d paginas vacias seguidas -> stop", page_num, EMPTY_STREAK_LIMIT)
                return
        else:
            state["empty_streak"] = 0

        has_next = f'page={page_num + 1}"' in html or f"page={page_num + 1}'" in html
        if has_next and page_num < MAX_PAGES:
            next_url = f"{base}?page={page_num + 1}{query_prefix}"
            await context.add_requests([
                Request.from_url(next_url, user_data={"page_num": page_num + 1}, unique_key=f"p{page_num + 1}")
            ])

    first_url = f"{base}?page=1{query_prefix}"
    await crawler.run([Request.from_url(first_url, user_data={"page_num": 1}, unique_key="p1")])
    page_iter.close()
    return list(seen.values())


async def collect_urls(search_urls: list[str]) -> list[dict]:
    seen = {}
    for base_url in search_urls:
        print(f"\n[PHASE 1] Processing: {base_url}")
        for c in await collect_urls_for(base_url):
            if c["id"] not in seen:
                seen[c["id"]] = c
    return list(seen.values())


# ----------------------- PHASE 2: extract one detail page -----------------------
def parse_detail(html: str) -> dict:
    page = _page(html)

    name = _text(_first(page, "h1"))
    price = _text(_first(page, ".prices-and-fees__price"))

    date_raw = _text(_first(page, ".date"))
    date_posted = agency = None
    if date_raw:
        parts = date_raw.split(" - ")
        date_posted = parts[0].strip()
        if len(parts) > 1:
            agency = re.sub(r"^Publicado por\s*", "", parts[1]).strip()

    location = _text(_first(page, ".location-map__location-address")) \
        or _text(_first(page, ".view-map__text"))

    # Specifications: label/value pairs across feature rows
    specifications = {}
    for row in page.css(".features-component__row"):
        cells = [_clean(c.text) for c in row.css("*") if not c.children and _clean(c.text)]
        for i in range(0, len(cells) - 1, 2):
            specifications[cells[i]] = cells[i + 1]

    # Size: m² icon item, else fall back to spec table
    detail_items = [_text(e) for e in page.css(".place-details .details-item")]
    detail_items = [d for d in detail_items if d]
    size = next((d for d in detail_items if "m²" in d), None)
    if not size:
        for k in SIZE_SPEC_KEYS:
            if specifications.get(k):
                size = specifications[k]
                break

    # Description: .content block after the "Descripción" .title heading
    description = None
    for h in page.css(".title"):
        if re.match(r"(?i)^descripci", _clean(h.text) or ""):
            sib = h.next
            if sib is not None and "content" in (sib.attrib.get("class") or ""):
                description = _text(sib)
            break

    # Characteristics: de-duplicated items in .facilities__options
    facil = _first(page, ".facilities__options")
    characteristics = []
    if facil is not None:
        seen_c = set()
        for child in facil.children:
            t = _text(child)
            if t and t not in seen_c:
                seen_c.add(t)
                characteristics.append(t)

    # Photos: full carousel from the gallery slides (NOT generic <img>, NOT
    # similar-snippets). Dedup by the decoded underlying image, not the URL,
    # since the same photo recurs with different resize params.
    photos, seen_keys = [], set()
    for img in page.css(".gallery__slide img"):
        src = img.attrib.get("src") or img.attrib.get("data-src") or ""
        if "img.lamudi.com.mx" not in src:
            continue
        key = phototrue_key(src)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        photos.append(src)

    photo_count = len(photos)

    # Map / location coordinates (pulled from raw HTML: JSON-LD or inline JS)
    map_lat, map_lng = _extract_coords(html)
    map_link = _maps_link(map_lat, map_lng)

    record = {
        "name": name, "price": price, "date_posted": date_posted, "agency": agency,
        "location": location, "size": size, "detail_items": detail_items,
        "specifications": specifications, "description": description,
        "characteristics": characteristics, "photos": photos, "photo_count": photo_count,
        "map_lat": map_lat, "map_lng": map_lng, "map_link": map_link,
    }
    # Backfill missing fields from description via parser
    if description:
        merge_parsed(record, parse_description(description))
    return record


async def extract_details(listings: list[dict]) -> list[dict]:
    """Entra a cada listing {id, url, lat, lng} y devuelve records extraidos."""
    results = []

    proxy_config = ProxyConfiguration(new_url_function=_res_proxy_url)
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
        ud = context.request.user_data
        d.update(url=context.request.url, lat=ud.get("lat"), lng=ud.get("lng"))
        # backfill listing-card coords from detail page if the card lacked them
        if d.get("lat") is None:
            d["lat"] = d.get("map_lat")
        if d.get("lng") is None:
            d["lng"] = d.get("map_lng")
        if _cp_checkpoint:
            _cp_checkpoint.done(context.request.url)
        results.append(d)
        if pbar:
            pbar.update(1)

    async def failed_handler(context: PlaywrightCrawlingContext, error: Exception):
        results.append({"url": context.request.url, "error": str(error)})
        if pbar:
            pbar.update(1)

    crawler.failed_request_handler(failed_handler)

    await crawler.run([
        Request.from_url(l["url"], user_data={"lat": l.get("lat"), "lng": l.get("lng")})
        for l in listings
    ])
    return results


def write_csv(rows, path):
    cols = ["url", "name", "price", "date_posted", "agency", "location", "size",
            "description", "characteristics", "photo_count", "photos",
            "lat", "lng", "map_lat", "map_lng", "map_link"]
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


async def main():
    global pbar, _cp_checkpoint

    os.makedirs("output", exist_ok=True)
    log = setup_log("lamudi")

    try:
        with open("scrape_links.json", "r", encoding="utf-8") as f:
            links_data = json.load(f)
            search_urls = links_data.get("lamudi", [])
    except Exception as e:
        print(f"Error cargando scrape_links.json: {e}")
        search_urls = []

    if not search_urls:
        raise SystemExit("No URLs to process. Add links to 'lamudi' in scrape_links.json")

    print("PHASE 1: collecting URLs...")
    urls = await collect_urls(search_urls)
    atomic_write_json(urls, "output/lamudi_urls.json", indent=2)
    log.info("PHASE 1 complete: %d unique URLs", len(urls))

    if not urls:
        raise SystemExit("No URLs collected — aborting Phase 2.")

    # Reconcile with DB: skip URLs already stored, delete vanished ones.
    try:
        from src import db
        urls = db.sync("lamudi", urls)
    except Exception as e:
        print(f"[db] sync skipped: {e}")
    if not urls:
        raise SystemExit("Nothing new to scrape — DB already up to date.")

    # Checkpoint for resumable Phase 2
    _cp_checkpoint = Checkpoint("output/lamudi_checkpoint.json")
    urls = _cp_checkpoint.resume(urls, key=lambda x: x["url"])
    if not urls:
        raise SystemExit("All URLs already extracted — checkpoint shows nothing to do.")

    setup_graceful_shutdown()
    print("PHASE 2: extracting details...")
    pbar = tqdm(total=len(urls), desc="Fase 2 (Lamudi)")
    listings = await extract_details(urls)
    pbar.close()

    # Write output atomically
    atomic_write_json(listings, "output/lamudi_listings.json", indent=2)
    write_csv(listings, "output/lamudi_listings.csv")
    _cp_checkpoint.clear()
    log.info("PHASE 2 complete: %d listings saved", len(listings))

    # Push to Supabase (JSON above is the backup; never let a DB error lose the scrape)
    try:
        from src import db
        db.upsert("lamudi", listings)
    except Exception as e:
        print(f"[db] upsert skipped: {e}")


if __name__ == "__main__":
    asyncio.run(main())
