# inmuebles24_scraper.py
# Botasaurus (browser + anti-detection) + Scrapling 0.4.9 (adaptive parsing)
import time, json, csv, random, re, html as ihtml
from urllib.parse import urljoin
from botasaurus.browser import browser, Driver
from botasaurus import bt
from scrapling.parser import Selector

ORIGIN = "https://www.inmuebles24.com"
LISTING_BASE = "terrenos-en-venta-en-monterrey"   # change for other searches
MAX_PAGES = 70   # ~1,763 results / ~28 per page ≈ 63 pages; cap for safety
CONCURRENCY = 4  # Inmuebles24 is stricter on anti-bot; keep modest


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


def _photo_id(url: str) -> str:
    m = re.search(r"/(\d+)\.(?:jpg|jpeg|webp|png)", url, re.I)
    return m.group(1) if m else url


# ----------------------- PHASE 1: collect listing URLs -----------------------
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


@browser(headless=True, block_images=True, reuse_driver=True, output=None)
def collect_urls(driver: Driver, data):
    seen = {}
    for p in range(1, MAX_PAGES + 1):
        url = (f"{ORIGIN}/{LISTING_BASE}.html" if p == 1
               else f"{ORIGIN}/{LISTING_BASE}-pagina-{p}.html")
        driver.get(url)
        driver.wait_for_element('[data-qa="posting PROPERTY"]', wait=20)
        cards = parse_listing_cards(driver.page_html)

        added = 0
        for c in cards:
            if c["id"] not in seen:
                seen[c["id"]] = c
                added += 1
        print(f"page {p}: {len(cards)} cards, +{added} new, total {len(seen)}")

        if not cards or added == 0:
            break
        time.sleep(random.uniform(2.0, 4.0))   # gentler pacing for Inmuebles24
    return list(seen.values())


# ----------------------- PHASE 2: parse a rendered detail page -----------------------
def parse_detail(html: str) -> dict:
    page = _page(html)

    name = _text(_first(page, "h1"))

    # Price: regex over the main-features section text (price spans are class-less)
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

    # Characteristics + specifications from the (JS-rendered) general-features block
    specifications, characteristics = {}, []
    seen_items = set()
    for el in page.css('[class*="generalFeaturesProperty"] *'):
        if el.children:                 # leaf nodes only
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

    # Photos: naventcdn /avisos/ images, deduped by photo id (full set after gallery expand)
    photos, seen_p = [], set()
    for img in page.css("img"):
        src = img.attrib.get("src") or img.attrib.get("data-src") or ""
        if "naventcdn.com/avisos/" not in src or "empresas" in src:
            continue
        pid = _photo_id(src)
        if pid in seen_p:
            continue
        seen_p.add(pid)
        photos.append(src)

    return {
        "name": name, "price": price, "location": location, "size": size,
        "icon_features": icon_features, "specifications": specifications,
        "description": description, "characteristics": characteristics,
        "photos": photos, "photo_count": len(photos),
    }


@browser(headless=True, reuse_driver=True, parallel=CONCURRENCY,
         block_images=False, output=None, close_on_crash=True)
def scrape_one(driver: Driver, listing):
    try:
        driver.get(listing["url"])
        driver.wait_for_element("h1", wait=20)
        # Read fields BEFORE expanding the gallery (modal mutates the DOM)
        base_html = driver.page_html

        # Expand the gallery to load ALL photos, then merge photo set
        try:
            driver.click('button:contains("Ver todas las fotos")')
            driver.wait_for_element('img[src*="naventcdn.com/avisos"]', wait=8)
            time.sleep(random.uniform(0.6, 1.2))
        except Exception:
            pass  # listings with ≤5 photos have no "ver todas" button
        full_html = driver.page_html

        d = parse_detail(base_html)
        photos_full = parse_detail(full_html)["photos"]   # richer photo set
        if len(photos_full) > len(d["photos"]):
            d["photos"] = photos_full
            d["photo_count"] = len(photos_full)

        d.update(id=listing["id"], url=listing["url"])
        print(f"OK  {d['name'] or '(no name)'} — {d['price'] or ''} — {d['photo_count']} fotos")
        return d
    except Exception as e:
        print(f"FAIL {listing.get('url')}: {e}")
        return {"id": listing.get("id"), "url": listing.get("url"), "error": str(e)}


# ----------------------- output -----------------------
def write_csv(rows, path):
    cols = ["id", "url", "name", "price", "location", "size",
            "description", "specifications", "characteristics", "photo_count"]
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
    print("PHASE 1: collecting URLs...")
    urls = collect_urls() or []
    bt.write_json(urls, "inm24_urls.json")
    print(f"Collected {len(urls)} unique URLs")

    if not urls:
        raise SystemExit("No URLs collected — aborting Phase 2.")

    print(f"PHASE 2: extracting details with {CONCURRENCY} parallel workers...")
    listings = scrape_one(urls) or []
    listings = [r for r in listings if isinstance(r, dict)]
    bt.write_json(listings, "inm24_listings.json")
    write_csv(listings, "inm24_listings.csv")
    ok = sum(1 for r in listings if "error" not in r)
    print(f"Done. {ok}/{len(listings)} OK -> inm24_listings.json / .csv")