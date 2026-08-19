"""Lamudi México scraper — Locales Comerciales (venta+renta) y Terrenos (venta).

Two phases:
  1. SERP  → each /{state}/{category}/{operation}/?page=N page ships 30 listing
     cards as server-rendered HTML with rich data-* attrs. No browser needed;
     curl_cffi's Chrome-TLS impersonation clears Lamudi's gate (verified).
  2. detail → /detalle/{id} carries a JSON-LD RealEstateListing (clean price,
     address, floorSize, description) plus phone, coordinates, plot/built area,
     condition and the full photo gallery. Only fetched when enrich=True.

Scale: iterate the 32 state slugs so no single query hits Lamudi's page cap.
Each state is crawled as a fresh visitor — new session/proxy, entering through
the homepage and the state landing page, with a Referer chain down the pages.
Resumable twice over: dedupes on listingId against the existing JSONL, and
checkpoints exhausted queries to <out>.done so a killed nationwide run doesn't
re-walk pages. Sequential + jittered pacing on purpose (polite, low ban risk).

    .venv/bin/python lamudi_scraper.py --out data/lamudi.jsonl   # all 32 states
    .venv/bin/python lamudi_scraper.py --states nuevo-leon jalisco --no-enrich
    .venv/bin/python lamudi_scraper.py --selfcheck        # offline fixture parse
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import re
import sys
import time
from urllib.parse import quote, urlparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from selectolax.parser import HTMLParser

from tqdm import tqdm

from stealth_scraper import Scraper
from scrape_utils import graceful, setup_logging

BASE = "https://www.lamudi.com.mx"

# 32 federal entities. Slugs are stable; hardcoding them is the lazy correct move.
STATES = [
    "aguascalientes", "baja-california", "baja-california-sur", "campeche",
    # Four slugs must use the *formal* state name. The intuitive short form is a
    # same-named colonia in Baja California and resolves 200 with a handful of
    # unrelated listings — silent, no error, verified empirically:
    #   coahuila         -> "Coahuila, Mexicali"        (4 results)
    #   estado-de-mexico -> "Estado de México, Ensenada" (0 results)
    #   michoacan        -> 301 to /baja-california/tijuana/michoacan/
    #   ciudad-de-mexico -> 404 outright
    "chiapas", "chihuahua", "distrito-federal", "coahuila-de-zaragoza", "colima", "durango",
    "guanajuato", "guerrero", "hidalgo", "jalisco", "mexico",
    "michoacan-de-ocampo", "morelos", "nayarit", "nuevo-leon", "oaxaca", "puebla",
    "queretaro", "quintana-roo", "san-luis-potosi", "sinaloa", "sonora",
    "tabasco", "tamaulipas", "tlaxcala", "veracruz", "yucatan", "zacatecas",
]

# (category slug, operation slug). We keep the whole category — Lamudi's
# "Comerciales" count (what the user compares against) spans every commercial
# subtype (Tienda/Local Comercial, Local, Nave/Bodega, Lote Comercial). The
# real subtype is preserved per-listing in propertyType so you can subset later.
SEARCHES = [
    ("comercial", "for-sale"),
    ("comercial", "for-rent"),
    ("terreno", "for-sale"),
]
OPERATION = {"for-sale": "sale", "for-rent": "rent"}

# When it rate-limits an IP, Lamudi serves a 200 page with a *custom math
# CAPTCHA* ("Security verification"). It's weak: the signed token embeds the
# problem in plaintext (base64), so we read it, solve it, and POST the answer
# back — no third-party CAPTCHA-solving service needed. The one catch is timing:
# the server checks real elapsed time via the token's timestamp, so we must wait
# a human-like few seconds before submitting.
_CHALLENGE = re.compile(r"Security verification|custom-captcha", re.I)
_TOKEN = re.compile(r'CAPTCHA_TOKEN\s*=\s*"([^"]+)"')
# Added by Lamudi mid-2026: verify also needs a nonce where
# sha256(f"{payload}:{nonce}") starts with N hex zeros. Read N off the page —
# they can raise it, and each extra zero is 16x the work.
_POW_ZEROS = re.compile(r"POW_DIFFICULTY_ZEROS\s*=\s*(\d+)")
_MATH = re.compile(r"(\d+)\s*([-+*/x×÷])\s*(\d+)")
_OPS = {
    "+": lambda a, b: a + b, "-": lambda a, b: a - b,
    "*": lambda a, b: a * b, "×": lambda a, b: a * b, "x": lambda a, b: a * b,
    "/": lambda a, b: a // b if b else 0, "÷": lambda a, b: a // b if b else 0,
}


def _solve_pow(payload: str, zeros: int) -> str:
    """Brute-force nonce with sha256(payload:nonce) starting in `zeros` hex 0s."""
    prefix = "0" * zeros
    prefix_bytes = payload.encode() + b":"
    for nonce in range(1 << 30):
        if hashlib.sha256(prefix_bytes + str(nonce).encode()).hexdigest().startswith(prefix):
            return str(nonce)
    raise RuntimeError(f"no PoW nonce for {zeros} zeros")  # unreachable in practice


def _solve_challenge(scraper: Scraper, url: str, html: str) -> bool:
    """Solve Lamudi's math CAPTCHA on the scraper's session. Returns True if an
    answer was submitted (clearance cookie now set on the session)."""
    tok_m = _TOKEN.search(html)
    if not tok_m:
        return False
    token = tok_m.group(1)
    try:
        payload = base64.b64decode(token.split(".")[0] + "==").decode("utf-8", "replace")
    except ValueError:
        return False
    math_m = _MATH.search(payload.strip().splitlines()[-1])
    if not math_m:
        return False
    answer = _OPS[math_m.group(2)](int(math_m.group(1)), int(math_m.group(3)))

    zeros_m = _POW_ZEROS.search(html)
    started = time.time()
    nonce = _solve_pow(token.split(".")[0], int(zeros_m.group(1)) if zeros_m else 5)
    # Server checks real elapsed time; the PoW usually covers it, top up if not.
    if (elapsed := time.time() - started) < 4.5:
        time.sleep(random.uniform(4.5, 7.5) - elapsed)
    verify = (
        f"{BASE}/verify-custom-captcha?url={quote(urlparse(url).path or '/')}"
        f"&token={quote(token)}&answer={answer}&powNonce={nonce}"
        f"&duration={round(time.time() - started, 1)}"
    )
    scraper.sess.get(verify, impersonate=scraper._imp, referer=url, timeout=40,
                     proxies={"http": scraper._proxy, "https": scraper._proxy} if scraper._proxy else None)
    return True


def _fetch(scraper: Scraper, url: str, max_solves: int = 3, **kw) -> str:
    """GET, transparently solving the anti-bot challenge on the same session.
    Extra kwargs (e.g. referer=) go straight to curl_cffi."""
    for attempt in range(max_solves + 1):
        html = scraper.get(url, **kw).text
        if not _CHALLENGE.search(html):
            return html
        if attempt == max_solves or not _solve_challenge(scraper, url, html):
            break
        print(f"    · solved challenge on {url}", file=sys.stderr)
    raise RuntimeError("anti-bot challenge could not be solved")


@dataclass
class Listing:
    listingId: str
    url: str
    title: str = ""
    imageUrl: str = ""
    operation: str = ""
    price: int | None = None
    currency: str = ""
    propertyType: str = ""
    bedrooms: int | None = None
    bathrooms: int | None = None
    areaM2: float | None = None
    location: str = ""
    city: str = ""
    agencyName: str = ""
    agentPhone: str = ""
    description: str = ""
    coordinates: tuple[float, float] | None = None
    condition: str = ""
    plotAreaM2: float | None = None
    builtAreaM2: float | None = None
    photos: list[str] = field(default_factory=list)
    listedAt: str = ""
    observedAt: str = ""


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #
def _num(text: str | None) -> float | None:
    """First number in a string like '160 m²' or '$ 44,000,000 MXN'."""
    if not text:
        return None
    m = re.search(r"[\d.,]+", text.replace(" ", " "))
    if not m:
        return None
    raw = m.group(0)
    # Mexican format uses ',' as thousands sep; drop them, keep a single dot.
    raw = raw.replace(",", "")
    try:
        v = float(raw)
        return v
    except ValueError:
        return None


def _price(text: str) -> tuple[int | None, str]:
    currency = "USD" if re.search(r"US\$|\bUSD\b", text) else ("MXN" if text.strip() else "")
    n = _num(text)
    return (int(n) if n is not None else None), currency


def _listed_at(listing_id: str) -> str:
    """Lamudi's ids are UUIDv7 — the publication time is the leading 48 bits.
    A free `listedAt` (and the watermark a delta run needs) with zero requests."""
    plain = listing_id.replace("-", "")
    # Version nibble first: Lamudi still serves legacy v3/v4 ids, whose random
    # bits decode to a plausible-looking epoch (we shipped dates up to 2099 for
    # 78 rows before this check). The range gate alone does not catch them.
    if len(plain) < 32 or plain[12] != "7":
        return ""
    try:
        ms = int(plain[:12], 16)
    except ValueError:
        return ""
    if not 1_262_304_000_000 < ms < 4_102_444_800_000:  # 2010..2100, belt and braces
        return ""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _proptype(title: str, category: str) -> str:
    """Normalize the title prefix to a stable propertyType. Order matters:
    'Lote Comercial' before 'Lote', 'Local Comercial' before 'Local'."""
    prefix = title.split(" en ")[0].strip()
    checks = [
        ("Lote Comercial", "Lote Comercial"),
        ("Local Comercial", "Local Comercial"),
        ("Nave Industrial", "Bodega / Nave Industrial"),
        ("Bodega", "Bodega / Nave Industrial"),
        ("Oficina", "Oficina"),
        ("Terreno", "Terreno"),
        ("Lote", "Terreno"),
        ("Local", "Local"),
    ]
    for needle, label in checks:
        if needle in prefix:
            return label
    return prefix or category.title()


def parse_serp(html: str, category: str, operation: str, keep: str | None = None) -> Iterator[Listing]:
    tree = HTMLParser(html)
    now = datetime.now(timezone.utc).isoformat()
    for card in tree.css("div.snippet.js-snippet"):
        lid = card.attributes.get("data-idanuncio")
        href_node = card.css_first('a[href^="/detalle/"]')
        if not lid or not href_node:
            continue
        title_node = card.css_first(".snippet__content__title")
        title = title_node.text(strip=True) if title_node else ""
        if keep and keep not in title:
            continue  # filter out Naves/Bodegas/Hoteles from the broad comercial category

        loc_node = card.css_first('[data-test="snippet-content-location"]')
        location = loc_node.text(strip=True) if loc_node else ""
        price_node = card.css_first(".snippet__content__price")
        price, currency = _price(price_node.text(strip=True)) if price_node else (None, "")
        img = card.css_first(".snippet__image img")
        area_node = card.css_first('[data-test="area-value"]')
        agency_node = card.css_first('[data-test="agency-name"]')

        coords = None
        raw_coords = card.attributes.get("data-serp-map-hover-listing")
        if raw_coords:
            try:
                c = json.loads(raw_coords)
                coords = (c["latitude"], c["longitude"])
            except (json.JSONDecodeError, KeyError):
                pass

        # The card already carries what the detail page would sell us at ~18x the
        # bytes: the full description (verified byte-identical to the detail
        # JSON-LD, not a teaser) and the agent's phone, parked in the WhatsApp
        # deep link. Reading them here is what makes --no-enrich lossless.
        desc_node = card.css_first("[data-itemdescription]")
        wa = card.css_first("#button-chat-whatsapp-desktop, .js-whatsapp")
        phone = re.search(r"phone=(\+?\d{10,15})", wa.attributes.get("value", "")) if wa else None

        parts = [p.strip() for p in location.split(",") if p.strip()]
        yield Listing(
            listingId=lid,
            url=BASE + href_node.attributes.get("href", ""),
            title=title,
            imageUrl=(img.attributes.get("src", "") if img else ""),
            operation=OPERATION.get(operation, operation),
            price=price,
            currency=currency,
            propertyType=_proptype(title, category),
            areaM2=_num(area_node.text()) if area_node else None,
            location=location,
            city=(parts[-2] if len(parts) >= 2 else (parts[-1] if parts else "")),
            agencyName=(agency_node.text(strip=True) if agency_node else ""),
            agentPhone=(phone.group(1) if phone else ""),
            description=(desc_node.text(strip=True) if desc_node else ""),
            coordinates=coords,
            listedAt=_listed_at(lid),
            observedAt=now,
        )


def parse_detail(html: str, base: Listing) -> Listing:
    """Enrich a SERP listing with detail-only fields. Mutates and returns base."""
    tree = HTMLParser(html)

    ld = _jsonld_listing(html)
    if ld:
        base.description = ld.get("description", base.description)
        offer = ld.get("offers") or {}
        if offer.get("price"):
            base.price = int(float(offer["price"]))
            base.currency = offer.get("priceCurrency", base.currency)
        addr = ld.get("address") or {}
        base.city = addr.get("addressLocality", base.city) or base.city
        floor = ld.get("floorSize") or {}
        base.areaM2 = _num(str(floor.get("value"))) or base.areaM2

    # coordinates (detail is exact; SERP hover may be fuzzed)
    lat = re.search(r'latitude["\s:=]+(-?\d{1,2}\.\d+)', html)
    lng = re.search(r'l(?:o?ng|ongitude)["\s:=]+(-?\d{2,3}\.\d+)', html)
    if lat and lng:
        base.coordinates = (float(lat.group(1)), float(lng.group(1)))

    phone = re.search(r"(\+?52\d{10})", re.sub(r"[\s\-()]", "", html))
    if phone:
        base.agentPhone = phone.group(1)

    feats = _features(tree)
    base.builtAreaM2 = _num(feats.get("Área construida")) or base.builtAreaM2
    base.plotAreaM2 = _num(feats.get("Superficie de terreno")) or base.plotAreaM2
    base.bedrooms = _int(feats.get("Recámaras") or feats.get("Habitaciones")) or base.bedrooms
    base.bathrooms = _int(feats.get("Baños")) or base.bathrooms
    for label in ("Condición", "Estado", "Antigüedad", "Estatus"):
        if feats.get(label):
            base.condition = feats[label]
            break

    base.photos = sorted(
        {n.attributes.get("src") for n in tree.css("img")
         if n.attributes.get("src", "").startswith("https://img.lamudi.com.mx/")}
    )
    return base


def _jsonld_listing(html: str) -> dict | None:
    for m in re.finditer(r'application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        graph = data.get("@graph", data if isinstance(data, list) else [data])
        for item in graph:
            if isinstance(item, dict) and item.get("@type") == "RealEstateListing":
                return item
    return None


def _features(tree: HTMLParser) -> dict[str, str]:
    """label -> value map from the detail 'features-component' rows."""
    feats: dict[str, str] = {}
    for row in tree.css(".features-component__row"):
        label = row.css_first(".features-component__label")
        value = row.css_first(".features-component__value")
        if label and value:
            feats[label.text(strip=True)] = value.text(strip=True)
    return feats


def _int(text: str | None) -> int | None:
    n = _num(text)
    return int(n) if n is not None else None


# --------------------------------------------------------------------------- #
# crawl
# --------------------------------------------------------------------------- #
def _enter(scraper: Scraper, state: str) -> str:
    """Arrive like a visitor: home → state landing page, once per state (two
    requests, not two per page). Seeds the session cookies the SERP expects and
    gives the first SERP hit a plausible Referer. Returns that referer."""
    _fetch(scraper, BASE + "/")
    landing = f"{BASE}/{state}/"
    _fetch(scraper, landing, referer=BASE + "/")
    return landing


def _load_done(path: Path) -> set[str]:
    """Queries already paginated to exhaustion, so a 32-state run resumes where
    it died instead of re-walking pages whose listings are all in `seen`."""
    return set(path.read_text(encoding="utf-8").split()) if path.exists() else set()


def crawl(states, searches, enrich, out_path, max_pages):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ckpt = out.with_name(out.name + ".done")
    seen = _load_seen(out)
    done = _load_done(ckpt)
    logger = setup_logging(out)
    scraper = Scraper(min_gap=2.5)  # slower floor: Lamudi challenges bursty single-IP traffic
    planned = len(states) * len(searches)
    stats = {"added": 0, "queries": 0, "errors": 0}

    def summary() -> str:
        return (f"{stats['added']} new listings → {out}\n"
                f"queries {stats['queries']}/{planned} complete "
                f"({stats['queries'] / max(planned, 1) * 100:.0f}%), "
                f"{stats['errors']} errors — log: {out}.log")

    # graceful outermost: the file handles close (flushing) before the summary prints.
    with graceful(logger, summary), \
            out.open("a", encoding="utf-8") as sink, \
            ckpt.open("a", encoding="utf-8") as log:
        for state in states:
            todo = [s for s in searches if f"{state}/{s[0]}/{s[1]}" not in done]
            stats["queries"] += len(searches) - len(todo)  # checkpointed = already complete
            if not todo:
                logger.info("%s: skip (checkpointed)", state)
                continue
            # One identity per state: a fresh visitor arriving from the homepage.
            # Rotating mid-state would be the suspicious move, not this.
            scraper.rotate()
            try:
                referer = _enter(scraper, state)
            except RuntimeError as exc:
                stats["errors"] += 1
                logger.error("entry %s -> %s", state, exc)
                continue

            for category, operation in todo:
                query = f"{state}/{category}/{operation}"
                logger.info("%s: start", query)
                exhausted = False
                ref = referer
                # ponytail: indeterminate bar (no total=) — Lamudi's SERP exposes
                # no result count, so completeness is "reached an empty page".
                bar = tqdm(desc=query, unit=" listing")
                for page in range(1, max_pages + 1):
                    url = f"{BASE}/{state}/{category}/{operation}/"
                    if page > 1:
                        url += f"?page={page}"
                    try:
                        html = _fetch(scraper, url, referer=ref)
                    except RuntimeError as exc:
                        stats["errors"] += 1
                        logger.error("%s -> %s", url, exc)
                        break
                    ref = url  # next page is a click from this one
                    cards = list(parse_serp(html, category, operation))
                    if not cards:
                        exhausted = True
                        break  # past the last page for this query
                    new = [c for c in cards if c.listingId not in seen]
                    for listing in new:
                        seen.add(listing.listingId)
                        if enrich:
                            try:
                                parse_detail(_fetch(scraper, listing.url, referer=url), listing)
                            except RuntimeError as exc:
                                stats["errors"] += 1
                                logger.error("detail %s: %s", listing.url, exc)
                        sink.write(json.dumps(_serialize(listing), ensure_ascii=False) + "\n")
                        sink.flush()
                        stats["added"] += 1
                    bar.update(len(cards))
                    logger.info("%s p%d cards=%d new=%d total_new=%d",
                                query, page, len(cards), len(new), stats["added"])
                bar.close()
                if exhausted:
                    stats["queries"] += 1
                    log.write(query + "\n")
                    log.flush()
                    logger.info("%s: complete", query)
                else:
                    logger.warning("%s: incomplete (stopped early)", query)


def _load_seen(out: Path) -> set[str]:
    if not out.exists():
        return set()
    seen = set()
    with out.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                seen.add(json.loads(line)["listingId"])
            except (json.JSONDecodeError, KeyError):
                continue
    return seen


def _serialize(listing: Listing) -> dict:
    d = asdict(listing)
    if d["coordinates"]:
        d["coordinates"] = {"lat": d["coordinates"][0], "lng": d["coordinates"][1]}
    return d


def _row_to_listing(row: dict) -> Listing:
    d = dict(row)
    c = d.get("coordinates")
    d["coordinates"] = (c["lat"], c["lng"]) if isinstance(c, dict) else c
    d["photos"] = d.get("photos") or []
    return Listing(**d)


def reenrich(in_path, out_path, keep_types):
    """Enrich an existing SERP-only JSONL in place: fetch each listing's detail
    page. Reuses SERP work already on disk (no re-pagination) and only touches
    the requested propertyTypes. Resumable via the output file."""
    rows = [json.loads(l) for l in Path(in_path).open(encoding="utf-8")]
    targets = [r for r in rows if not keep_types or r["propertyType"] in keep_types]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = _load_seen(out)
    logger = setup_logging(out)
    scraper = Scraper(min_gap=2.5)
    todo = [r for r in targets if r["listingId"] not in seen]
    logger.info("reenrich: %d rows, %d match %s, %d already done, %d to fetch",
                len(rows), len(targets), keep_types or "ALL", len(seen), len(todo))
    stats = {"done": 0, "errors": 0}

    def summary() -> str:
        return (f"enriched {stats['done']}/{len(todo)} listings → {out}\n"
                f"{stats['errors']} detail errors — log: {out}.log")

    bar = tqdm(desc="reenrich", total=len(todo), unit=" listing")
    with graceful(logger, summary), out.open("a", encoding="utf-8") as sink:
        for r in todo:
            listing = _row_to_listing(r)
            try:
                parse_detail(_fetch(scraper, listing.url), listing)
            except RuntimeError as exc:
                stats["errors"] += 1
                logger.error("detail %s: %s", listing.url, exc)
            sink.write(json.dumps(_serialize(listing), ensure_ascii=False) + "\n")
            sink.flush()
            stats["done"] += 1
            bar.update()
            logger.info("enriched %d/%d %s", stats["done"], len(todo), listing.url)
        bar.close()


# --------------------------------------------------------------------------- #
def _selfcheck() -> None:
    """Offline parse of saved fixtures — fails if selectors/JSON-LD drift."""
    n = _solve_pow("MTc4NTE3MDIzNjAzMgoxODkuMTUzLjE2MC4xMDcKMTYgLSAxMg", 5)
    assert hashlib.sha256(
        f"MTc4NTE3MDIzNjAzMgoxODkuMTUzLjE2MC4xMDcKMTYgLSAxMg:{n}".encode()
    ).hexdigest().startswith("00000"), n

    sc = Path(__file__).parent / ".fixtures"
    terreno = (sc / "terreno.html")
    detail = (sc / "detail.html")
    if not terreno.exists():
        print("fixtures missing; run once online then save terreno.html/detail.html "
              "into .fixtures/ to enable offline selfcheck")
        return
    cards = list(parse_serp(terreno.read_text(), "terreno", "for-sale", None))
    assert len(cards) >= 20, f"expected ~30 terreno cards, got {len(cards)}"
    c = cards[0]
    assert c.listingId and c.url.startswith(BASE + "/detalle/"), c
    assert c.propertyType == "Terreno", c.propertyType
    assert c.coordinates and -120 < c.coordinates[1] < -80, c.coordinates
    assert c.price and c.currency == "MXN", (c.price, c.currency)
    # These three are what let --no-enrich skip the detail page. If a markup
    # change silently empties them, the cheap run starts losing fields quietly.
    assert sum(bool(x.agentPhone) for x in cards) > len(cards) // 2, "SERP phones gone"
    assert sum(bool(x.description) for x in cards) > len(cards) // 2, "SERP descriptions gone"
    assert c.listedAt.startswith("20"), f"UUIDv7 listedAt broke: {c.listedAt!r}"

    enriched = parse_detail(detail.read_text(), cards[0])
    assert enriched.photos, "no gallery photos parsed"
    assert enriched.agentPhone.startswith("+52"), enriched.agentPhone
    assert enriched.coordinates, enriched.coordinates
    print(f"OK selfcheck: {len(cards)} cards, sample price={c.price} {c.currency}, "
          f"photos={len(enriched.photos)}, phone={enriched.agentPhone}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/lamudi.jsonl")
    ap.add_argument("--states", nargs="*", default=STATES)
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--no-enrich", dest="enrich", action="store_false",
                    help="SERP only; skips detail-page fields (phone, photos, areas, condition)")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--reenrich", metavar="SERP_JSONL",
                    help="enrich an existing SERP-only JSONL instead of crawling")
    ap.add_argument("--types", nargs="*", default=None,
                    help="only keep these propertyType values (e.g. 'Local Comercial' Terreno)")
    args = ap.parse_args()

    if args.selfcheck:
        _selfcheck()
        return
    if args.reenrich:
        reenrich(args.reenrich, args.out, set(args.types) if args.types else None)
        return
    crawl(args.states, SEARCHES, args.enrich, args.out, args.max_pages)


if __name__ == "__main__":
    main()
