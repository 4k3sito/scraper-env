# inmuebles24_scraper.py
# Botasaurus (browser + anti-detection) + Scrapling 0.4.9 (adaptive parsing)
#
# CAMBIO CLAVE — COORDENADAS:
#   En Inmuebles24 lat/lon NO estan en el JSON-LD (que suele ser solo
#   WebSite/Organization) ni en una URL de Google static map. Estan en un
#   objeto JSON inline bajo las claves postingGeolocation / geolocation con
#   el formato:  "latitude":17.9672...  "longitude":-92.9375...
#   (numeros SIN comillas, latitude antes que longitude). El HTML debe leerse
#   despues de la hidratacion (tras wait_for_element + un pequeño delay).

import os, sys
import time, json, csv, random, re, html as ihtml
from urllib.parse import urljoin

from tqdm import tqdm
from scrapling.parser import Selector
from botasaurus import bt
from botasaurus.browser import browser, Driver
from src.utils import atomic_write_json, setup_graceful_shutdown, Checkpoint, setup_log
from src.parser import parse_description, merge_parsed

ORIGIN = "https://www.inmuebles24.com"
MAX_PAGES = 57   # 1,689 results / ~30 per page ≈ 57 pages

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


@browser(headless=False, block_images=True, reuse_driver=True,
         max_retry=3, close_on_crash=True, output=None)
def collect_urls(driver: Driver, search_urls):
    seen = {}
    for search_url in search_urls:
        print(f"\n[PHASE 1] Processing: {search_url}")
        base_path = search_url.split(".html")[0]

        page_iter = tqdm(range(1, MAX_PAGES + 1), desc=f"  Collecting pages", unit="pg", leave=False)
        for p in page_iter:
            url = f"{base_path}.html" if p == 1 else f"{base_path}-pagina-{p}.html"
            driver.get(url)

            # Cloudflare challenge: esperar hasta 12s a que resuelva (headful)
            for _ in range(12):
                if "Just a moment" not in driver.title:
                    break
                time.sleep(1)
            else:
                print(f"page {p}: Cloudflare no resuelve -> stopping early")
                break

            # Wait for listing cards to load
            try:
                driver.wait_for_element(
                    '[data-qa="posting PROPERTY"], [data-posting-id], article[class*="card"], '
                    '.posting-card, [class*="Posting"]',
                    wait=5,
                )
                # Scroll to trigger lazy content
                driver.scroll_to_bottom(smooth_scroll=True)
                time.sleep(random.uniform(0.5, 1.2))
                driver.run_js("window.scrollTo(0, 0);")
                time.sleep(random.uniform(0.3, 0.8))
            except Exception:
                # Fallback: wait for body and try regex extraction
                try:
                    driver.wait_for_element("body", wait=2)
                except Exception:
                    print(f"page {p}: pagina no cargo -> stop")
                    break

            html = driver.page_html

            # Primary: Scrapling CSS extraction
            cards = parse_listing_cards(html)
            # Fallback: regex extraction if CSS found nothing
            if not cards:
                cards = _fallback_cards(html, ORIGIN)

            added = 0
            for c in cards:
                if c["id"] not in seen:
                    seen[c["id"]] = c
                    added += 1
            page_iter.set_description(f"  Page {p}: {len(cards)} cards, +{added} new, total {len(seen)}")
            page_iter.set_postfix(unique=len(seen))

            if not cards or added == 0:
                print(f"page {p}: 0 cards found -> stopping early")
                break
            time.sleep(random.uniform(1.5, 3.0))

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


@browser(headless=True, block_images=True, reuse_driver=True,
         max_retry=3, close_on_crash=True, output=None)
def extract_details(driver: Driver, listing):
    """Botasaurus itera la lista: recibe UN listing, devuelve UN dict."""
    global pbar
    url = listing["url"]
    for attempt in range(1, 4):
        try:
            driver.get(url)
            driver.wait_for_element("h1", wait=15)
            d = parse_detail(driver.page_html)
            d.update(id=listing["id"], url=url)
            _cp_checkpoint.done(url)
            break
        except Exception as e:
            d = {"id": listing["id"], "url": url, "error": str(e)}
            if attempt < 3:
                delay = 3 * attempt
                print(f"   [retry] {url} — attempt {attempt}/3: {e}, waiting {delay}s")
                time.sleep(delay)
    if pbar:
        pbar.update(1)
    return d


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


def selftest(url=None):
    """Verifica coordenadas en una URL de detalle.
    Uso:  python -m src.inmuebles24 --selftest <URL>
    """
    if url is None:
        raise SystemExit("Pasa una URL de detalle:  python -m src.inmuebles24 --selftest <URL>")

    @browser(headless=True, block_images=True, output=None)
    def _one(driver: Driver, u):
        driver.get(u)
        driver.wait_for_element("h1", wait=15)
        return parse_detail(driver.page_html)

    result = _one(url)
    result.update(id="test", url=url)
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


if __name__ == "__main__":
    os.makedirs("output", exist_ok=True)
    log = setup_log("inmuebles24")

    if "--selftest" in sys.argv:
        idx = sys.argv.index("--selftest")
        _url = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        selftest(_url)
        raise SystemExit(0)

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
    urls = (collect_urls([search_urls_list]) or [[]])[0] or []
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
    print(f"PHASE 2: extracting details...")
    pbar = tqdm(total=len(urls), desc="Fase 2 (Inmuebles24)")
    listings = extract_details(urls) or []
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
