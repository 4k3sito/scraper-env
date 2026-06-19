# lamudi.py
# Botasaurus (browser + anti-detection) + Scrapling 0.4.9 (adaptive parsing)
import os, time, json, csv, random, re, base64
from urllib.parse import quote, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, urllib3
from botasaurus import bt
from scrapling.parser import Selector

# ScraperAPI presents its own TLS cert in proxy mode, so requests must skip
# verification; silence the resulting InsecureRequestWarning.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "1cf28a1df4ffa6a63403dd2ba73b3384")
SCRAPERAPI_HOST = "proxy-server.scraperapi.com:8001"
MAX_WORKERS = int(os.environ.get("SCRAPERAPI_WORKERS", "5"))  # ScraperAPI plan concurrency limit

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


def _photo_true_key(url: str) -> str:
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


# ----------------------- fetch layer (ScraperAPI proxy) -----------------------
def _proxies(render=True, **opts):
    # ScraperAPI proxy mode: request options go in the username, dot-separated.
    params = {"render": str(render).lower(), "country_code": "mx", **opts}
    user = "scraperapi" + "".join(f".{k}={v}" for k, v in params.items())
    proxy = f"http://{user}:{SCRAPERAPI_KEY}@{SCRAPERAPI_HOST}"
    return {"http": proxy, "https": proxy}


def fetch(url, render=False, retries=3, timeout=90, **opts):
    """GET `url` through ScraperAPI and return HTML. Raises on failure.

    render defaults False: lamudi serves listing cards and detail fields in the
    initial HTML, so rendering only adds ~8x latency and ~10x credit cost with
    no extra data. Pass render=True per-call if a page proves JS-dependent.
    """
    proxies = _proxies(render=render, **opts)
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, proxies=proxies, verify=False, timeout=timeout)
            if r.status_code == 200 and r.text:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = type(e).__name__
        if attempt < retries:
            print(f"  retry {attempt}/{retries} for {url} ({last})")
            time.sleep(2 * attempt)
    raise RuntimeError(f"fetch failed ({last})")


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


def collect_urls():
    seen = {}
    for p in range(1, MAX_PAGES + 1):
        url = f"{BASE}?bounds={quote(BOUNDS)}&page={p}"
        try:
            html = fetch(url)
        except RuntimeError as e:
            print(f"page {p}: {e}")
            break
        cards = parse_listing_cards(html)

        added = 0
        for c in cards:
            if c["id"] not in seen:
                seen[c["id"]] = c
                added += 1
        print(f"page {p}: {len(cards)} cards, +{added} new, total {len(seen)}")

        has_next = f'page={p + 1}"' in html or f"page={p + 1}'" in html
        if not cards or added == 0 or not has_next:
            break
        time.sleep(random.uniform(1.5, 3.0))
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
        key = _photo_true_key(src)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        photos.append(src)

    photo_count = len(photos)

    return {
        "name": name, "price": price, "date_posted": date_posted, "agency": agency,
        "location": location, "size": size, "detail_items": detail_items,
        "specifications": specifications, "description": description,
        "characteristics": characteristics, "photos": photos, "photo_count": photo_count,
    }


def _fetch_detail(listing):
    d = parse_detail(fetch(listing["url"]))
    d.update(url=listing["url"], lat=listing.get("lat"), lng=listing.get("lng"))
    return d


def extract_details(urls, workers=MAX_WORKERS):
    # Fetch in parallel (ScraperAPI rotates IPs + throttles; concurrency is the
    # natural limiter). Results are slotted back by index to preserve input order.
    results = [None] * len(urls)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_detail, lst): i for i, lst in enumerate(urls)}
        for fut in as_completed(futures):
            i = futures[fut]
            done += 1
            try:
                d = fut.result()
                results[i] = d
                print(f"[{done}/{len(urls)}] {d['name'] or '(no name)'} — {d['price'] or ''}")
            except Exception as e:
                print(f"[{done}/{len(urls)}] FAILED {urls[i].get('url')}: {e}")
                results[i] = {"url": urls[i].get("url"), "error": str(e)}
    return results

def write_csv(rows, path):
    cols = ["url", "name", "price", "date_posted", "agency", "location", "size",
            "description", "characteristics", "photo_count", "lat", "lng"]
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
    bt.write_json(urls, "lamudi_urls.json")
    print(f"Collected {len(urls)} unique URLs")

    if not urls:
        raise SystemExit("No URLs collected — aborting Phase 2.")

    print("PHASE 2: extracting details...")
    listings = extract_details(urls) or []
    bt.write_json(listings, "lamudi_listings.json")
    write_csv(listings, "lamudi_listings.csv")
    print(f"Done. {len(listings)} listings -> lamudi_listings.json / .csv")