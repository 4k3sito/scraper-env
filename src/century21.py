# century21.py
# Century21 Mexico scraper. Crawlee (PlaywrightCrawler — driver, proxy,
# sesiones, reintentos y deteccion de bloqueo nativa) + Scrapling (parser).
#
# The site is Vue-rendered: listing data loads via XHR, but the detail page
# does contain structured data in JSON-LD and inline HTML after rendering.
#
# PHASE 1: paginate search result pages, extract detail URLs
# PHASE 2: load each detail page, extract fields from HTML + JSON-LD

import asyncio
import os, json, re
from datetime import timedelta
from functools import lru_cache

from tqdm import tqdm
from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.proxy_configuration import ProxyConfiguration
from crawlee.storage_clients import MemoryStorageClient
from scrapling.parser import Selector
from src.utils import atomic_write_json, setup_graceful_shutdown, Checkpoint
from src.parser import parse_description, merge_parsed
from src.proxy import ApifyProxyConfig

# ponytail: datacenter para paginar (barato), residencial para el detalle
# (donde el sitio bloquea mas fuerte). Lazy: solo exige APIFY_PROXY_PASSWORD
# al correr de verdad, no al importar el modulo.
@lru_cache(maxsize=None)
def _proxy(groups):
    # "auto" -> grupo datacenter dedicado, con password propia (grupo comprado
    # aparte, no cubierto por APIFY_PROXY_PASSWORD general). No usado
    # actualmente (ambas fases corren en RESIDENTIAL, ver comentario abajo),
    # pero se deja consistente con el resto de scrapers.
    if groups == "auto":
        return ApifyProxyConfig(groups="BUYPROXIES94952", password_env="APIFY_PROXY_PASSWORD_DATACENTER", country=None)
    return ApifyProxyConfig(groups=groups)


async def _res_proxy_url(session_id=None, request=None, proxy_tier=None):
    return _proxy("RESIDENTIAL").url(session=session_id or ApifyProxyConfig.new_session_id())


ORIGIN = "https://century21mexico.com"
MAX_PAGES = 30
OUTPUT_FILE = "output/century21.json"

_cp_checkpoint = None
pbar = None


def _page(html):
    return Selector(html)


def _first(node, sel):
    found = node.css(sel)
    return found[0] if found else None


def _text(node):
    if node is None:
        return None
    t = node.text
    return re.sub(r"\s+", " ", t).strip() if t else None


def _maps_link(lat, lon):
    if lat is None or lon is None:
        return None
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"


def _page_url(url, page=None):
    """Build the URL for a given page number."""
    base = url.split("?")[0].rstrip("/")
    if page and page > 1:
        return f"{base}/pagina_{page}"
    return base


# ----------------------- PHASE 1: collect listing URLs -----------------------
def _cards_from_html(html):
    """Devuelve dict {id: {id, url}} de una pagina de resultados."""
    page = _page(html)
    found = {}
    for a in page.css('a[href*="propiedad/"], a[href*="/detalle/"]'):
        href = a.attrib.get("href", "")
        if not href or "javascript" in href:
            continue
        full = href if href.startswith("http") else ORIGIN + href
        rid = re.search(r"/(\d+)(?:/|\?|$)", full)
        rid = rid.group(1) if rid else full
        found[rid] = {"id": rid, "url": full.split("?")[0]}

    for block in page.css('script[type="application/ld+json"]::text').getall():
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and "url" in item.get("mainEntityOfPage", {}):
                u = item["mainEntityOfPage"]["url"]
                rid = re.search(r"/(\d+)", u)
                if rid:
                    found[rid.group(1)] = {"id": rid.group(1), "url": u}
    return found


async def collect_urls_for(search_url: str) -> list[dict]:
    seen = {}
    page_iter = tqdm(total=MAX_PAGES, desc="  Collecting pages", unit="pg", leave=False)

    # RESIDENTIAL en ambas fases: verificado que el Cloudflare de este sitio
    # corta el tunel con IPs datacenter (ERR_TUNNEL_CONNECTION_FAILED), pero
    # pasa limpio con residencial.
    proxy_config = ProxyConfiguration(new_url_function=_res_proxy_url)
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
        await context.page.wait_for_load_state("networkidle", timeout=15000)
        html = await context.page.content()

        cards = _cards_from_html(html)
        added = 0
        for rid, c in cards.items():
            if rid not in seen:
                seen[rid] = c
                added += 1
        page_iter.update(1)
        page_iter.set_description(f"  Page {page_num}: +{added} new, {len(seen)} total")
        page_iter.set_postfix(unique=len(seen))
        context.log.info("page %d: +%d new (total %d)", page_num, added, len(seen))

        if added == 0:
            context.log.info("page %d: 0 nuevos -> stop", page_num)
            return

        if page_num < MAX_PAGES:
            next_num = page_num + 1
            next_url = _page_url(search_url, next_num)
            await context.add_requests([
                Request.from_url(next_url, user_data={"page_num": next_num}, unique_key=f"p{next_num}")
            ])

    first_url = _page_url(search_url, 1)
    await crawler.run([Request.from_url(first_url, user_data={"page_num": 1}, unique_key="p1")])
    page_iter.close()
    return list(seen.values())


async def collect_urls(search_urls: list[str]) -> list[dict]:
    seen = {}
    for search_url in search_urls:
        print(f"\n[PHASE 1] {search_url}")
        for c in await collect_urls_for(search_url):
            if c["id"] not in seen:
                seen[c["id"]] = c
    return list(seen.values())


# ----------------------- PHASE 2: extract one detail page -----------------------
def parse_detail(html, listing):
    """Extract fields from the rendered detail HTML using Scrapling + JSON-LD."""
    page = _page(html)
    ld_data = _extract_ld(html)

    # Prefer JSON-LD data when available
    name = None
    price = None
    price_value = None
    currency = None
    description = None
    lat = lon = None
    photos = []
    features = {}
    operation = None
    prop_type = None
    location = None
    address = None

    if ld_data:
        name = ld_data.get("name") or ld_data.get("headline")
        desc_text = ld_data.get("description")
        if desc_text:
            description = desc_text.strip()
        offers = ld_data.get("offers", {})
        if offers:
            price = offers.get("price")
            price_value = price
            currency = offers.get("priceCurrency")
        geo = ld_data.get("geo", {})
        lat = geo.get("latitude") if isinstance(geo, dict) else None
        lon = geo.get("longitude") if isinstance(geo, dict) else None
        image_obj = ld_data.get("image", {})
        if isinstance(image_obj, dict):
            main_img = image_obj.get("url") or image_obj.get("contentUrl")
            if main_img:
                photos = [main_img]

    # DOM fallback for fields JSON-LD might not have
    if not name:
        name = _text(_first(page, "h1"))
    if not price:
        price_el = _first(page, '[class*="price"], [class*="precio"], [class*="Price"]')
        if price_el is not None:
            price = _text(price_el)
    if not location:
        loc_el = _first(page, '[class*="location"], [class*="ubicacion"], [class*="address"]')
        if loc_el is not None:
            location = _text(loc_el)
    if not photos:
        for img in page.css('img[src*="propiedad"], img[class*="gallery"], .gallery img, [class*="foto"] img'):
            src = img.attrib.get("src") or ""
            if src and "http" in src:
                photos.append(src)

    if lat is None:
        m = re.search(r'"lat"\s*:\s*"?(-?\d+\.\d+)"?', html)
        if m:
            lat = float(m.group(1))
        m = re.search(r'"lon"\s*:\s*"?(-?\d+\.\d+)"?', html)
        if m:
            lon = float(m.group(1))

    return {
        "id": listing["id"],
        "url": listing["url"],
        "name": name,
        "price": f"${price:,.2f}" if isinstance(price, (int, float)) else str(price) if price else None,
        "price_value": price_value,
        "currency": currency or "MXN",
        "property_type": prop_type,
        "operation": operation,
        "location": location,
        "address": address,
        "lat": lat,
        "lon": lon,
        "map_link": _maps_link(lat, lon),
        "description": description,
        "characteristics": features,
        "photos": list(dict.fromkeys(photos)),
        "photo_count": len(photos),
    }


def _extract_ld(html):
    """Extract first JSON-LD with RealEstateListing or Product schema."""
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict):
                atype = item.get("@type", "")
                if any(t in atype for t in ("RealEstateListing", "Product", "Place", "LocalBusiness")):
                    return item
    return None


async def extract_details(listings: list[dict]) -> list[dict]:
    """Entra a cada listing {id, url} y devuelve records extraidos."""
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
        await context.page.wait_for_load_state("networkidle", timeout=15000)
        html = await context.page.content()
        listing = context.request.user_data
        rec = parse_detail(html, listing)
        if rec.get("description"):
            merge_parsed(rec, parse_description(rec["description"]))
        if _cp_checkpoint:
            _cp_checkpoint.done(listing["url"])
        results.append(rec)
        if pbar:
            pbar.update(1)

    async def failed_handler(context: PlaywrightCrawlingContext, error: Exception):
        listing = context.request.user_data
        results.append({"id": listing.get("id"), "url": context.request.url, "error": str(error)})
        if pbar:
            pbar.update(1)

    crawler.failed_request_handler(failed_handler)

    await crawler.run([
        Request.from_url(l["url"], user_data={"id": l["id"], "url": l["url"]}) for l in listings
    ])
    return results


async def main():
    global pbar, _cp_checkpoint

    try:
        with open("scrape_links.json", encoding="utf-8") as f:
            search_urls = json.load(f).get("century21", [])
    except Exception as e:
        raise SystemExit(f"Error loading scrape_links.json: {e}")
    if not search_urls:
        raise SystemExit("No URLs. Add a 'century21' list to scrape_links.json")

    print("PHASE 1: collecting URLs...")
    listings = await collect_urls(search_urls)
    print(f"Collected {len(listings)} unique listings")
    if not listings:
        raise SystemExit("No URLs collected — aborting Phase 2.")

    try:
        from src import db
        listings = db.sync("century21", listings)
    except Exception as e:
        print(f"[db] sync skipped: {e}")
    if not listings:
        raise SystemExit("Nothing new to scrape — DB already up to date.")

    _cp_checkpoint = Checkpoint("output/century21_checkpoint.json")
    listings = _cp_checkpoint.resume(listings, key=lambda x: x["url"])
    if not listings:
        raise SystemExit("All URLs already extracted — checkpoint shows nothing to do.")

    setup_graceful_shutdown()
    pbar = tqdm(total=len(listings), desc="Fase 2 (C21)", unit="anuncio")
    results = await extract_details(listings)
    if pbar:
        pbar.close()
    results = [r for r in results if isinstance(r, dict)]

    os.makedirs("output", exist_ok=True)
    atomic_write_json(results, OUTPUT_FILE, indent=2)
    _cp_checkpoint.clear()
    print(f"Done. {len(results)} listings -> {OUTPUT_FILE}")
    try:
        from src import db
        db.upsert("century21", results)
    except Exception as e:
        print(f"[db] upsert failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
