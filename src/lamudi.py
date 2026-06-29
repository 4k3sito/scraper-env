# lamudi.py
# Botasaurus (browser + anti-detection) + Scrapling 0.4.9 (adaptive parsing)

import os, sys, time, json, csv, random, re, base64
from urllib.parse import urljoin

from tqdm import tqdm
from src.utils import atomic_write_json, setup_graceful_shutdown, should_stop, setup_log
from botasaurus import bt
from botasaurus.browser import browser, Driver
from scrapling.parser import Selector
from src.parser import parse_description, merge_parsed

BASE = "https://www.lamudi.com.mx/nuevo-leon/monterrey/comercial/venta-al-por-menor/for-sale/"
BOUNDS = "-100.47016411545054,25.419606909860605,-100.09276415132233,25.838957457589444"
ORIGIN = "https://www.lamudi.com.mx"
MAX_PAGES = 30  # real set ends at page 16 (~454 cards / 446 unique)

SIZE_SPEC_KEYS = ("Superficie total", "Superficie de terreno",
                  "Superficie construida", "Superficie útil", "Superficie")


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


@browser(headless=True, block_images=True, reuse_driver=True,
         max_retry=3, close_on_crash=True, output=None)
def collect_urls(driver: Driver, search_urls):
    seen = {}
    for base_url in search_urls:
        print(f"\n[PHASE 1] Processing: {base_url}")

        if "?" in base_url:
            base, qs = base_url.split("?", 1)
            query_prefix = f"&{qs}"
        else:
            base = base_url
            query_prefix = ""

        empty_streak = 0
        page_iter = tqdm(range(1, MAX_PAGES + 1), desc=f"  Collecting pages", unit="pg", leave=False)
        for p in page_iter:
            url = f"{base}?page={p}{query_prefix}"
            try:
                driver.google_get(url, bypass_cloudflare=True)
                driver.wait_for_element(".js-snippet", wait=15)
                driver.scroll_to_bottom(smooth_scroll=True)
                time.sleep(random.uniform(0.5, 1.0))
                driver.run_js("window.scrollTo(0, 0);")
                time.sleep(random.uniform(0.3, 0.7))
            except Exception:
                print(f"page {p}: sin resultados -> stop")
                break
            html = driver.page_html
            cards = parse_listing_cards(html)

            added = 0
            for c in cards:
                if c["id"] not in seen:
                    seen[c["id"]] = c
                    added += 1
            page_iter.set_description(f"  Page {p}: {len(cards)} cards, +{added} new, total {len(seen)}")
            page_iter.set_postfix(unique=len(seen))

            has_next = f'page={p + 1}"' in html or f"page={p + 1}'" in html
            if not cards or added == 0:
                empty_streak += 1
                if empty_streak >= 10:
                    print(f"page {p}: 10 consecutive empty pages -> stopping early")
                    break
            else:
                empty_streak = 0
            if not has_next:
                break
            time.sleep(random.uniform(2.0, 4.0))
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


pbar = None


@browser(headless=True, block_images=True, reuse_driver=True,
         max_retry=3, close_on_crash=True, output=None)
def extract_details(driver: Driver, listing):
    """Botasaurus itera la lista de cards: recibe UNO, devuelve UN dict."""
    global pbar
    url = listing["url"]
    for attempt in range(1, 4):
        try:
            driver.get(url)
            driver.wait_for_element("h1", wait=15)
            d = parse_detail(driver.page_html)
            d.update(url=url, lat=listing.get("lat"), lng=listing.get("lng"))
            # backfill listing-card coords from detail page if the card lacked them
            if d.get("lat") is None:
                d["lat"] = d.get("map_lat")
            if d.get("lng") is None:
                d["lng"] = d.get("map_lng")
            # success — mark checkpoint
            _cp_checkpoint.done(url)
            break
        except Exception as e:
            d = {"url": url, "error": str(e)}
            if attempt < 3:
                delay = 3 * attempt
                print(f"   [retry] {url} — attempt {attempt}/3: {e}, waiting {delay}s")
                time.sleep(delay)
    if pbar:
        pbar.update(1)
    return d


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


if __name__ == "__main__":
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
    # Wrap in a list: botasaurus iterates list data per-item, so [search_urls]
    # passes the whole list to one call; result comes back wrapped too.
    urls = (collect_urls([search_urls]) or [[]])[0] or []
    bt.write_json(urls, "output/lamudi_urls.json")
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
    _cp_checkpoint = __import__("src.utils", fromlist=["Checkpoint"]).Checkpoint("output/lamudi_checkpoint.json")
    urls = _cp_checkpoint.resume(urls, key=lambda x: x["url"])
    if not urls:
        raise SystemExit("All URLs already extracted — checkpoint shows nothing to do.")

    setup_graceful_shutdown()
    print("PHASE 2: extracting details...")
    pbar = tqdm(total=len(urls), desc="Fase 2 (Lamudi)")
    listings = extract_details(urls) or []
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
