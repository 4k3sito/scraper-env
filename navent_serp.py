"""Shared SERP parsing for Navent-built real-estate portals (Inmuebles24,
Vivanuncios, and the rest of the family).

They run the same front end: every search-results page embeds
`script#preloadedData` holding `window.__PRELOADED_STATE__`, whose
`listStore.listPostings` is an array of *fully hydrated* listings — price per
operation, exact geolocation, plot/built area, description, agency, photos, the
agent's WhatsApp number and a `modified_date` — plus `paging.total` /
`paging.totalPages`. One request per SERP page yields complete rows; there is no
detail phase to write.

Only the base URL and the site's URL grammar differ between targets, so those
live in the per-site scrapers and everything here is shared. Verified identical
on inmuebles24.com and vivanuncios.com.mx (July 2026).
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterator

from selectolax.parser import HTMLParser

from stealth_scraper import Scraper

# Which JSON operationType.name a search's price should be read from.
OP_NAME = {"venta": "Venta", "renta": "Renta"}
OPERATION = {"venta": "sale", "renta": "rent"}

# DataDome/Cloudflare interstitials arrive as 200 HTML (not a 4xx) → rotate.
# Navent sites stack both vendors and they announce themselves differently; a
# missed interstitial parses as "0 cards", which reads as "query exhausted".
BLOCK = re.compile(
    r"geo\.captcha-delivery\.com|datadome|Pardon Our Interruption|Just a moment", re.I)

# What the proxy actually bills: compressed bytes on the wire, not len(.content)
# (which curl_cffi hands back decompressed, overstating the bill ~6-9×).
WIRE = {"bytes": 0, "requests": 0}


def fetch(scraper: Scraper, url: str, max_rotations: int = 3, **kw) -> str:
    """GET with DataDome/Cloudflare handling: on a 200-interstitial, rotate
    identity (fresh session/proxy/TLS), cool down, retry. Scraper.get already
    rotates+backs off on 4xx. Meters the compressed size of everything it pulls."""
    for attempt in range(max_rotations + 1):
        try:
            resp = scraper.get(url, **kw)
            WIRE["requests"] += 1
            WIRE["bytes"] += len(gzip.compress(resp.content, 6))
            html = resp.text
        except RuntimeError:
            if attempt == max_rotations:
                raise
            scraper.rotate()
            time.sleep(5 * (attempt + 1))
            continue
        if not BLOCK.search(html[:5000]):
            return html
        if attempt == max_rotations:
            break
        print(f"    · anti-bot flag on {url} — rotating identity", file=sys.stderr)
        scraper.rotate()
        time.sleep(5 * (attempt + 1))  # a fresh IP hitting instantly is the tell
    raise RuntimeError("anti-bot block could not be cleared")


# --------------------------------------------------------------------------- #
# schema (kept aligned with lamudi_scraper.Listing so tooling is shared)
# --------------------------------------------------------------------------- #
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
    areaM2: float | None = None
    plotAreaM2: float | None = None
    builtAreaM2: float | None = None
    location: str = ""
    city: str = ""
    province: str = ""
    agencyName: str = ""
    agentPhone: str = ""
    postingCode: str = ""
    description: str = ""
    coordinates: tuple[float, float] | None = None
    listedAt: str = ""
    observedAt: str = ""


def serialize(listing: Listing) -> dict:
    d = asdict(listing)
    if d["coordinates"]:
        d["coordinates"] = {"lat": d["coordinates"][0], "lng": d["coordinates"][1]}
    return d


# --------------------------------------------------------------------------- #
# preloadedData extraction
# --------------------------------------------------------------------------- #
def preloaded(html: str) -> dict | None:
    """Decode window.__PRELOADED_STATE__ from script#preloadedData. It's a JS
    assignment with trailing code, so raw_decode from the first brace, not
    json.loads (which chokes on the 'Extra data' after the object)."""
    node = HTMLParser(html).css_first("script#preloadedData")
    if not node:
        return None
    raw = node.text().strip()
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
        return obj
    except (ValueError, json.JSONDecodeError):
        return None


def find_postings(obj: dict) -> list[dict]:
    """Depth-first hunt for the listings array (list of dicts with postingId)."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for v in cur.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "postingId" in v[0]:
                    return v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return []


def paging(obj: dict) -> dict:
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if isinstance(cur.get("paging"), dict):
                return cur["paging"]
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(cur)
    return {}


def num(text) -> float | None:
    if text is None:
        return None
    m = re.search(r"[\d.,]+", str(text).replace(",", ""))
    try:
        return float(m.group(0)) if m else None
    except ValueError:
        return None


def chain(loc: dict, label: str) -> str:
    """Walk the location parent chain up to the node with this label
    (ZONA → CIUDAD → PROVINCIA → PAIS). The PROVINCIA node is what makes the
    coverage audit exact instead of a string-matching guess."""
    node = loc.get("location") or {}
    while node:
        if node.get("label") == label:
            return node.get("name", "")
        node = node.get("parent") or {}
    return ""


def listed_at(posting: dict) -> str:
    """Navent ships a real `modified_date` (ISO with offset) — no UUIDv7 decoding
    needed. Publication/refresh time for zero requests."""
    raw = posting.get("modified_date") or ""
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def parse_serp(html: str, operation: str, base: str) -> Iterator[Listing]:
    """Yield every listing hydrated in the page's preloadedData blob.

    `operation` selects which price to read (a posting can carry both a Venta and
    a Renta price); `base` is the site's origin, used to absolutise listing URLs.
    """
    obj = preloaded(html)
    if not obj:
        return
    now = datetime.now(timezone.utc).isoformat()
    op_name = OP_NAME[operation]
    for p in find_postings(obj):
        pid = p.get("postingId")
        rel = p.get("url")
        if not pid or not rel:
            continue

        # price for the operation this search is about
        price, currency = None, ""
        for pot in p.get("priceOperationTypes") or []:
            if (pot.get("operationType") or {}).get("name") == op_name:
                prices = pot.get("prices") or []
                if prices:
                    price = int(num(prices[0].get("amount")) or 0) or None
                    cur = prices[0].get("currency", "")
                    currency = "USD" if cur == "USD" else ("MXN" if cur else "")
                break

        loc = p.get("postingLocation") or {}
        addr = (loc.get("address") or {}).get("name", "")
        zone = (loc.get("location") or {}).get("name", "")
        geo = ((loc.get("postingGeolocation") or {}).get("geolocation")) or {}
        coords = None
        if geo.get("latitude") and geo.get("longitude"):
            coords = (float(geo["latitude"]), float(geo["longitude"]))

        # CFT100 = plot ("Terreno"), CFT101 = built ("Superficie construída").
        # areaM2 keeps the old meaning (plot first) so rows stay comparable.
        feats = p.get("mainFeatures") or {}
        plot = num((feats.get("CFT100") or {}).get("value"))
        built = num((feats.get("CFT101") or {}).get("value"))

        pics = ((p.get("visiblePictures") or {}).get("pictures")) or []
        img = pics[0].get("url730x532") or pics[0].get("url360x266") if pics else ""

        # The agent's phone is right here on the SERP — no detail fetch, no
        # phone-reveal API call. Digits only, normalised to +52.
        wa = re.sub(r"\D", "", str(p.get("whatsApp") or ""))
        phone = f"+{wa}" if len(wa) >= 10 else ""

        yield Listing(
            listingId=str(pid),
            url=rel if rel.startswith("http") else base + rel,
            title=p.get("title") or p.get("generatedTitle") or "",
            imageUrl=img or "",
            operation=OPERATION.get(operation, operation),
            price=price,
            currency=currency,
            propertyType=(p.get("realEstateType") or {}).get("name", ""),
            areaM2=plot or built,
            plotAreaM2=plot,
            builtAreaM2=built,
            location=", ".join(x for x in (addr, zone) if x),
            city=chain(loc, "CIUDAD") or (loc.get("location") or {}).get("name", ""),
            province=chain(loc, "PROVINCIA"),
            agencyName=(p.get("publisher") or {}).get("name", ""),
            agentPhone=phone,
            postingCode=p.get("postingCode") or "",
            description=(p.get("descriptionNormalized") or "")[:2000],
            coordinates=coords,
            listedAt=listed_at(p),
            observedAt=now,
        )
