# century21.py
# Century21 Mexico scraper. Botasaurus (browser) + Scrapling (parser).
#
# The site is Vue-rendered: listing data loads via XHR, but the detail page
# does contain structured data in JSON-LD and inline HTML after rendering.
# We use Botasaurus to load pages and Scrapling to parse the DOM.
#
# PHASE 1: paginate search result pages via Botasaurus, extract detail URLs
# PHASE 2: load each detail page, extract fields from HTML + JSON-LD

import os, time, json, re, random
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from scrapling.parser import Selector
from botasaurus.browser import browser, Driver
from src.utils import atomic_write_json, setup_graceful_shutdown, Checkpoint
from src.parser import parse_description, merge_parsed

ORIGIN = "https://century21mexico.com"
MAX_PAGES = 30
FAIL_LIMIT = 10
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


# ----------------------- PHASE 1: collect listing URLs -----------------------
@browser(headless=True, block_images=True, reuse_driver=True,
         max_retry=3, close_on_crash=True, output=None)
def collect_urls(driver: Driver, search_urls):
    """Collect unique listing URLs by paginating search result pages."""
    seen = {}
    for search_url in search_urls:
        print(f"\n[PHASE 1] {search_url}")
        page_iter = tqdm(range(1, MAX_PAGES + 1), desc=f"  Collecting pages", unit="pg", leave=False)
        for p in page_iter:
            url = _page_url(search_url, p)
            try:
                driver.google_get(url, bypass_cloudflare=True)
                driver.wait_for_element("body", wait=15)
                time.sleep(1.5)
                driver.scroll_to_bottom(smooth_scroll=True)
                time.sleep(random.uniform(0.5, 1.0))
                driver.run_js("window.scrollTo(0, 0);")
                time.sleep(random.uniform(0.3, 0.7))
            except Exception as e:
                print(f"  page {p}: {e} -> stop")
                break
            html = driver.page_html
            page = _page(html)

            added = 0
            for a in page.css('a[href*="propiedad/"], a[href*="/detalle/"]'):
                href = a.attrib.get("href", "")
                if not href or "javascript" in href:
                    continue
                full = href if href.startswith("http") else ORIGIN + href
                rid = re.search(r"/(\d+)(?:/|\?|$)", full)
                rid = rid.group(1) if rid else full
                if rid not in seen:
                    seen[rid] = {"id": rid, "url": full.split("?")[0]}
                    added += 1

            for block in page.css('script[type="application/ld+json"]::text').getall():
                try:
                    data = json.loads(block)
                except json.JSONDecodeError:
                    continue
                for item in (data if isinstance(data, list) else [data]):
                    if isinstance(item, dict) and "url" in item.get("mainEntityOfPage", {}):
                        u = item["mainEntityOfPage"]["url"]
                        rid = re.search(r"/(\d+)", u)
                        if rid and rid.group(1) not in seen:
                            seen[rid.group(1)] = {"id": rid.group(1), "url": u}
                            added += 1

            page_iter.set_description(f"  Page {p}: +{added} new, {len(seen)} total")
            page_iter.set_postfix(unique=len(seen))
            if added == 0:
                break
            time.sleep(1.5)
    return list(seen.values())


def _page_url(url, page=None):
    """Build the URL for a given page number."""
    base = url.split("?")[0].rstrip("/")
    if page and page > 1:
        return f"{base}/pagina_{page}"
    return base


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


@browser(headless=True, block_images=True, reuse_driver=True,
         max_retry=3, close_on_crash=True, output=None)
def extract_detail(driver: Driver, listing):
    """Botasaurus itera: recibe UN listing, devuelve UN dict."""
    global pbar
    for attempt in range(1, 4):
        try:
            driver.get(listing["url"])
            driver.wait_for_element("body", wait=15)
            time.sleep(2)
            html = driver.page_html
            rec = parse_detail(html, listing)
            if rec.get("description"):
                merge_parsed(rec, parse_description(rec["description"]))
            _cp_checkpoint.done(listing["url"])
            if pbar:
                pbar.update(1)
            return rec
        except Exception as e:
            if attempt < 3:
                delay = 3 * attempt
                print(f"   [retry] {listing['url']} — attempt {attempt}/3: {e}, waiting {delay}s")
                time.sleep(delay)
    if pbar:
        pbar.update(1)
    return {"id": listing.get("id"), "url": listing.get("url"), "error": "failed after 3 retries"}


if __name__ == "__main__":
    try:
        with open("scrape_links.json", encoding="utf-8") as f:
            search_urls = json.load(f).get("century21", [])
    except Exception as e:
        raise SystemExit(f"Error loading scrape_links.json: {e}")
    if not search_urls:
        raise SystemExit("No URLs. Add a 'century21' list to scrape_links.json")

    print("PHASE 1: collecting URLs...")
    listings = collect_urls(search_urls) or []
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
    results = extract_detail(listings) or []
    if pbar:
        pbar.close()
    if not isinstance(results, list):
        results = [results]
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
