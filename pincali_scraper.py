"""Pincali (EasyBroker marketplace) México — Terrenos y Locales Comerciales,
renta + venta, nacional.

Single phase, no detail crawl. Every SERP card carries **its own coordinates**
(`data-lat` / `data-long` / `data-exact-location`) next to a `ld+json`
`RealEstateListing` with price, floor size, type, city, region and `datePosted`.
42 cards per page at ~69 KB gzipped = **1.64 KB/listing**, the cheapest of the
five corpora in this repo — and the only one where the SERP gives coordinates.

Getting in — the part that is not curl_cffi:

    AWS WAF challenges every path except `/`. Real headful Chrome solves it in
    ~5 s and leaves an `aws-waf-token` cookie; curl_cffi then sweeps behind that
    token for **exactly 300 s** (measured: 178 requests over 299.3 s, then 202).
    Headless Chrome — including `--headless=new` — never gets a token, so the
    mint runs headful under Xvfb (this module re-execs itself under `xvfb-run`
    when `DISPLAY` is unset). The token is bound to the minting IP, so the sweep
    runs direct: proxying it would need the browser to mint through the same
    exit IP, which is not wired up.

    Refreshing that token needs `_install`, not `cookies.set`: the server sets
    an `aws-waf-token` of its own, and with two in the jar neither one can be
    replaced, so every token after the first was minted and never sent. Two
    nationwide attempts died at the 300 s boundary looking precisely like
    rate-limiting. It was the cookie jar.

    robots.txt (itself behind the WAF) has **no Disallow at all** — one
    `Crawl-delay: 1` and a sitemap index. `--min-gap` defaults to 1.5 s.

How the crawl shards:

    `?page=101` is a hard 404 (page 100 is 200), so a query tops out at 4,200
    listings. `search_criteria[min_price]` + `sort_by=price-asc` walk past that
    by keyset: take the last price on page 100, make it the next floor. Measured
    on Terreno/venta — page 100 ends at $2,750, floor 2750 re-serves from there,
    71,188 → 67,005 remaining, 17 rows of overlap that dedupe absorbs.

    The trap: any `search_criteria[...]` param on a pretty `/inmuebles/<slug>`
    URL **discards the slug's own filters** and serves all 315,589 sale
    listings with HTTP 200. So every request passes the property type and the
    operation explicitly, and `_verify` reads both back off the rows.

Two fields the shared `Listing` has no room for, both unrecoverable later:
`priceIsPerM2` (27 of 42 cards on Terreno's cheapest page are priced *per m²* —
storing 2,750 as the price of a 5,000 m² lot would poison a search index) and
`coordsExact` (a `false` pin is a colonia centroid, not the property).

Known gap: `plotAreaM2` is exact only for the land types. On a Local the card's
one m² figure is construction, and lot size lives on the 278 KB detail page —
18× the SERP cost per row, so it is not fetched. `--audit` reports the split.

    .venv/bin/python pincali_scraper.py --survey     # size the job + verify types
    .venv/bin/python pincali_scraper.py --out data/pincali.jsonl
    .venv/bin/python pincali_scraper.py --status     # health of a run in flight
    .venv/bin/python pincali_scraper.py --since 2026-07-01   # delta, newest-first
    .venv/bin/python pincali_scraper.py --audit      # offline coverage check
    .venv/bin/python pincali_scraper.py --selfcheck  # offline fixture parse
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from selectolax.parser import HTMLParser
from tqdm import tqdm

from stealth_scraper import Scraper
from scrape_utils import graceful, setup_logging
# Row shape and wire meter are shared with the other four corpora.
from navent_serp import WIRE, Listing, serialize

BASE = "https://www.pincali.com"
PER_PAGE = 42
PAGE_CAP = 100            # ?page=101 is a hard 404 → 4,200 listings per query
TOKEN_TTL = 280.0         # the WAF token dies at 300 s; refresh with margin
# 0 = re-mint and retry at once (the ordinary expired token). A challenge that
# survives a *fresh* token means something we cannot fix by minting harder, so
# wait it out in minutes rather than burn the query in fifteen seconds.
_COOLDOWN = (0, 90, 300, 420)
# robots.txt asks for 1 s; 1.5 s buys margin without inventing a limit nobody
# published. Two nationwide attempts died at the 300 s token boundary and looked
# exactly like rate-limiting — it was `_install`, not the pace. Raise this only
# on evidence, and log what the evidence was.
MIN_GAP = 1.5

# (slug, property_type_id, the site's own type label, operation)
# The slug only makes the URL readable and the Referer honest — the query
# params are what actually filter, because they overrule the slug entirely.
SEARCHES = [
    ("terrenos-en-venta", 29059, "Terreno", "sale"),
    ("terrenos-en-renta", 29059, "Terreno", "rental"),
    ("terrenos-comerciales-en-venta", 28661, "Terreno comercial", "sale"),
    ("terrenos-comerciales-en-renta", 28661, "Terreno comercial", "rental"),
    ("terrenos-industriales-en-venta", 29257, "Terreno industrial", "sale"),
    ("terrenos-industriales-en-renta", 29257, "Terreno industrial", "rental"),
    ("locales-comerciales-en-venta", 29061, "Local comercial", "sale"),
    ("locales-comerciales-en-renta", 29061, "Local comercial", "rental"),
    ("locales-en-centro-comercial-en-venta", 28701, "Local en centro comercial", "sale"),
    ("locales-en-centro-comercial-en-renta", 28701, "Local en centro comercial", "rental"),
]

# Types whose single m² figure is the LOT, not the construction. Everything else
# reports built area and leaves plotAreaM2 empty rather than guessing.
LAND_TYPES = {"Terreno", "Terreno comercial", "Terreno industrial", "Huerta"}

OPERATION = {"sale": "sale", "rental": "rent"}      # site's word -> corpus word
OP_LABEL = {"sale": "En Venta", "rental": "En Renta"}

# The 32 states as `addressRegion` spells them, for the coverage audit.
PROVINCES = [
    "Aguascalientes", "Baja California", "Baja California Sur", "Campeche",
    "Chiapas", "Chihuahua", "Ciudad de México", "Coahuila", "Colima", "Durango",
    "Estado de México", "Guanajuato", "Guerrero", "Hidalgo", "Jalisco",
    "Michoacán", "Morelos", "Nayarit", "Nuevo León", "Oaxaca", "Puebla",
    "Querétaro", "Quintana Roo", "San Luis Potosí", "Sinaloa", "Sonora",
    "Tabasco", "Tamaulipas", "Tlaxcala", "Veracruz", "Yucatán", "Zacatecas",
]

_COMPLETE = "done"
_CHALLENGE = re.compile(r"awsWafCookieDomainList|challenge\.js|gokuProps")


# --------------------------------------------------------------------------- #
# AWS WAF token
# --------------------------------------------------------------------------- #
class WafToken:
    """One `aws-waf-token`, re-minted by a real browser when it goes stale.

    Headless Chrome fails the challenge (measured: 0 tokens in 30 s, both
    `--headless=old` and `--headless=new`); headful under Xvfb succeeds in
    ~5.4 s. That is the whole reason this scraper needs a browser at all — the
    42 listings it then reads come over plain curl_cffi.
    """

    def __init__(self, logger=None) -> None:
        self._value = ""
        self._minted = 0.0
        self._log = logger
        self.mints = 0

    @property
    def value(self) -> str:
        if not self._value or time.monotonic() - self._minted > TOKEN_TTL:
            self.refresh()
        return self._value

    def refresh(self) -> None:
        from patchright.sync_api import sync_playwright

        started = time.monotonic()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=False,
                                         args=["--no-sandbox"])
            try:
                ctx = browser.new_context(locale="es-MX",
                                          timezone_id="America/Mexico_City")
                page = ctx.new_page()
                page.goto(f"{BASE}/inmuebles/terrenos-en-venta",
                          wait_until="domcontentloaded", timeout=60_000)
                for _ in range(30):     # the challenge solves itself, then reloads
                    token = {c["name"]: c["value"] for c in ctx.cookies()}
                    if token.get("aws-waf-token"):
                        self._value = token["aws-waf-token"]
                        break
                    page.wait_for_timeout(1000)
                else:
                    raise RuntimeError("browser never received an aws-waf-token")
            finally:
                browser.close()
        self._minted = time.monotonic()
        self.mints += 1
        if self._log:
            self._log.info("WAF token minted in %.1fs (#%d)",
                           self._minted - started, self.mints)


def ensure_display() -> None:
    """Re-exec under Xvfb when there is no display: the mint needs headful
    Chrome, and forgetting the wrapper otherwise fails 20 minutes in."""
    if os.environ.get("DISPLAY"):
        return
    xvfb = shutil.which("xvfb-run")
    if not xvfb:
        sys.exit("no DISPLAY and no xvfb-run: the WAF token needs headful Chrome "
                 "(apt install xvfb, or run under a desktop session)")
    os.execv(xvfb, [xvfb, "-a", sys.executable, *sys.argv])


def _install(scraper: Scraper, value: str) -> None:
    """Put `value` in the jar as *the* WAF token, by emptying the jar first.

    `cookies.set()` alone cannot do this. The server sends its own
    `Set-Cookie: aws-waf-token` for `www.pincali.com`, and once that sits beside
    the one we set for domain `""`, further `set()` calls silently change
    neither — so every token after the first was minted, installed, and never
    actually sent. The crawl kept presenting an expired token and re-minting
    against a challenge that could not clear. Nothing else in the jar matters:
    the very first probe of this site got a 200 with the token cookie alone."""
    # Keyed on the session too: Scraper.get rotates to a brand-new session on a
    # transport error, which empties the jar without changing the token.
    if getattr(scraper, "_waf_installed", None) == (id(scraper.sess), value):
        return
    scraper.sess.cookies.clear()
    scraper.sess.cookies.set("aws-waf-token", value)
    scraper._waf_installed = (id(scraper.sess), value)


def fetch(scraper: Scraper, token: WafToken, url: str, referer: str = "") -> str:
    """GET behind the WAF token, metering compressed bytes.

    A stale token comes back as HTTP **202** carrying the challenge page — not a
    4xx, so `Scraper.get` sees a perfectly good response. Re-mint and retry;
    rotating the proxy/TLS identity would not help and would invalidate the
    token's IP binding.

    A challenge that survives a *fresh* token is a different animal: the IP is in
    a rate-based penalty box, where minting faster only adds load. Measured on
    the first nationwide run — ~230 requests inside one 5-minute window, then
    every new token rejected for several minutes. So back off in minutes, not
    seconds, and keep the query alive across it; aborting instead cost 9 of 10
    queries in under two minutes."""
    for attempt in range(len(_COOLDOWN) + 1):
        _install(scraper, token.value)
        resp = scraper.get(url, headers={"Referer": referer} if referer else None)
        WIRE["requests"] += 1
        WIRE["bytes"] += len(gzip.compress(resp.content, 6))
        html = resp.text
        if resp.status_code != 202 and not _CHALLENGE.search(html[:3000]):
            return html
        if attempt < len(_COOLDOWN):
            nap = _COOLDOWN[attempt] + random.uniform(0, 15)
            if nap:
                print(f"    · WAF penalty box — cooling down {nap / 60:.1f} min",
                      file=sys.stderr)
                time.sleep(nap)
            token.refresh()
    raise RuntimeError(f"WAF challenge could not be cleared: {url}")


# --------------------------------------------------------------------------- #
# URL grammar
# --------------------------------------------------------------------------- #
def search_url(slug: str, type_id: int, operation: str, page: int = 1,
               floor: int = 0, sort: str = "price-asc") -> str:
    """Type and operation always ride in the query string, never in the slug:
    a `search_criteria[...]` param resets the criteria the slug encoded, so a
    URL that mixes the two silently searches everything."""
    params = [
        ("search_criteria[property_type_ids][]", str(type_id)),
        ("search_criteria[operation_type]", operation),
        ("search_criteria[sort_by]", sort),
    ]
    if floor:
        params.append(("search_criteria[min_price]", str(floor)))
    url = f"{BASE}/inmuebles/{slug}?{urlencode(params)}"
    return f"{url}&page={page}" if page > 1 else url


# --------------------------------------------------------------------------- #
# SERP parsing
# --------------------------------------------------------------------------- #
@dataclass
class PincaliListing(Listing):
    """The shared row plus what only Pincali can tell us.

    `priceIsPerM2`: the card says "$8,800 MXN por m²" while the JSON-LD offer
    says 8800 — indistinguishable downstream, and wrong by orders of magnitude.
    `coordsExact`: `data-exact-location="false"` means the pin is the colonia,
    which a map search must not treat as an address."""
    priceIsPerM2: bool = False
    coordsExact: bool | None = None


def total_results(html: str) -> int | None:
    """The site's own count for this query, off `[data-total]`. It is also the
    only signal that a filter applied — a dropped filter reports the corpus."""
    node = HTMLParser(html).css_first("[data-total]")
    if not node:
        return None
    try:
        return int(node.attributes.get("data-total") or "")
    except ValueError:
        return None


def _jsonld(html: str) -> dict[str, dict]:
    """listing path -> RealEstateListing node. Keyed on the URL path because
    that is the only id the JSON-LD and the card DOM share."""
    out: dict[str, dict] = {}
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            doc = json.loads(node.text())
        except json.JSONDecodeError:
            continue
        for obj in (doc if isinstance(doc, list) else [doc]):
            if not isinstance(obj, dict):
                continue
            types = obj.get("@type") or []
            if "RealEstateListing" not in (types if isinstance(types, list) else [types]):
                continue
            url = obj.get("url") or obj.get("@id") or ""
            if url:
                out[url.replace(BASE, "")] = obj
    return out


def _num(value) -> float | None:
    if value is None:
        return None
    m = re.search(r"[\d.]+", str(value).replace(",", ""))
    try:
        return float(m.group(0)) if m else None
    except ValueError:
        return None


def _price(value) -> int | float | None:
    """Sub-peso prices are real here — a per-m² lot can ask $0.50 — so keep the
    float rather than truncate it to a listing that looks free."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return int(value) if float(value).is_integer() else value


def _coord(value) -> float | None:
    """Signed float or nothing — never a partial coordinate, and never 0.0 from
    a failed parse, which would land the property in the Gulf of Guinea."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _listed_at(raw: str) -> str:
    try:
        return datetime.fromisoformat(raw).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def parse_serp(html: str, operation: str):
    """Yield one PincaliListing per card, in the page's own (sorted) order.

    The card owns the coordinates, the price semantics and the location chain;
    the JSON-LD owns the numbers. Cards without a JSON-LD twin are still
    yielded — losing a row to a missing script tag would be silent."""
    ld = _jsonld(html)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for card in HTMLParser(html).css("div.property__component"):
        link = card.css_first("a.property__content")
        href = (link.attributes.get("href") or "") if link else ""
        lid = card.attributes.get("data-id") or ""
        if not lid or not href:
            continue
        obj = ld.get(href, {})
        offers = obj.get("offers") or []
        # `offers` is a list, a bare object, or an empty list ("Consulte precio").
        offer = (offers[0] if isinstance(offers, list) and offers
                 else offers if isinstance(offers, dict) else {})
        addr = obj.get("address") or {}

        price_node = card.css_first(".price")
        price_text = price_node.text(separator=" ", strip=True) if price_node else ""
        feats = [f.text(strip=True) for f in card.css(".features div")]
        prop_type = obj.get("category") or (feats[0] if feats else "")
        area = _num((obj.get("floorSize") or {}).get("value"))
        if area is None:                          # JSON-LD gone: read the chip
            area = next((_num(f) for f in feats if "m²" in f), None)
        is_land = prop_type in LAND_TYPES

        lat = _coord(card.attributes.get("data-lat"))
        lng = _coord(card.attributes.get("data-long"))   # negative across Mexico
        exact = card.attributes.get("data-exact-location")

        yield PincaliListing(
            listingId=lid,
            url=BASE + href,
            title=obj.get("name") or (link.text(strip=True) if link else ""),
            imageUrl=obj.get("image") or "",
            operation=OPERATION.get(operation, operation),
            # int where it is one, float where it isn't: a "$0.50 MXN por m²"
            # lot truncates to a price of 0, which reads as free.
            price=_price(offer.get("price")),
            currency=offer.get("priceCurrency") or "",
            propertyType=prop_type,
            areaM2=area,
            plotAreaM2=area if is_land else None,
            builtAreaM2=None if is_land else area,
            location=_text(card, ".location"),
            city=addr.get("addressLocality") or "",
            province=addr.get("addressRegion") or "",
            agencyName="",           # not on the SERP; detail page only
            agentPhone="",           # not on the SERP; detail page only
            postingCode=lid,         # EasyBroker public id — stable across portals
            description=obj.get("description") or "",
            coordinates=(lat, lng) if lat is not None and lng is not None else None,
            listedAt=_listed_at(obj.get("datePosted") or ""),
            observedAt=now,
            priceIsPerM2="por m" in price_text,
            coordsExact=(exact == "true") if exact else None,
        )


def _text(node, sel: str) -> str:
    found = node.css_first(sel)
    return found.text(separator=" ", strip=True) if found else ""


# --------------------------------------------------------------------------- #
# keyset on the price axis
# --------------------------------------------------------------------------- #
def next_floor(cards, floor: int) -> int:
    """The next `min_price`, read off the page we just walked.

    **In pesos, from a peso row.** `min_price` filters on the MXN-converted
    amount and the grid sorts by it, but a USD card reports its own 193 — take
    that as the floor and the sweep walks *backwards* into the rows it already
    served. So use the last MXN card: it is at most the true sort key of the
    page's last row, which costs a little overlap and can never open a gap.

    Falls back to `floor + 1` when nothing can advance it (>4,200 listings at
    one price, or a page of pure USD). That skips the overflow of that one price
    point instead of looping forever; the caller counts it."""
    tail = next((c.price for c in reversed(cards)
                 if c.price and c.currency == "MXN"), None)
    return max(int(tail), floor + 1) if tail else floor + 1


def _verify(cards, type_label: str, operation: str, floor: int) -> str:
    """Read the filters back off the rows, because every failure here is a 200.
    Returns the reason it looks wrong, or "" when the page is trustworthy.

    A `search_criteria` param that lands on a slug URL drops the slug's filters
    and serves the whole 315k sale corpus; a dropped `min_price` restarts the
    keyset from the bottom and the sweep never terminates. Both show up in the
    rows we already parsed."""
    types = [c.propertyType for c in cards if c.propertyType]
    if types and sum(t == type_label for t in types) < len(types) / 2:
        return (f"type filter not applied: rows say "
                f"{Counter(types).most_common(2)}, wanted {type_label!r}")
    ops = [c.operation for c in cards if c.operation]
    if ops and sum(o == OPERATION[operation] for o in ops) < len(ops) / 2:
        return f"operation filter not applied: rows are not {OPERATION[operation]}"
    # MXN only: a USD row priced 193 is legitimately above a 2,750-peso floor.
    priced = [c.price for c in cards if c.price and c.currency == "MXN"]
    below = sum(1 for p in priced if p < floor)
    # One straggler is a listing repriced between the query and the render.
    if floor and below > max(2, len(priced) // 4):
        return f"min_price not applied: {below}/{len(priced)} peso rows below {floor}"
    return ""


# --------------------------------------------------------------------------- #
# crawl
# --------------------------------------------------------------------------- #
def _load_seen(out: Path) -> set[tuple[str, str]]:
    """Keyed on (listing, operation), not the listing alone.

    ~1.4% of this corpus is offered for sale *and* for rent, at two different
    prices. Deduping on the id alone banks whichever query ran first — always
    the venta one — and the rental side of the property vanishes from the index
    while every count still looks healthy: the renta query simply reports 406 of
    501 and nothing errors. Measured on terrenos-industriales-en-renta: 501
    advertised, 501 served, 0 missing from the file, 95 of them filed as `sale`."""
    if not out.exists():
        return set()
    seen = set()
    with out.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
                seen.add((row["listingId"], row.get("operation", "")))
            except (json.JSONDecodeError, KeyError):
                continue
    return seen


def _load_done(path: Path) -> dict[str, str | int]:
    """query -> "done", or the price floor the sweep reached, so a killed run
    resumes there instead of re-walking shards it already paid for."""
    done: dict[str, str | int] = {}
    if path.exists():
        for tok in path.read_text(encoding="utf-8").split():
            query, _, floor = tok.partition("@")
            if done.get(query) == _COMPLETE:
                continue
            done[query] = int(floor) if floor else _COMPLETE
    return done


def crawl(out_path, min_gap=MIN_GAP, only=None, since="") -> None:
    """Sweep every (type, operation) by price keyset until the site runs out.

    `since` switches to delta mode: newest-first, stopping at the watermark.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ckpt = out.with_name(out.name + ".done")
    seen = _load_seen(out)
    done = _load_done(ckpt)
    logger = setup_logging(out)
    token = WafToken(logger)
    # Direct connection on purpose: the WAF token is bound to the IP that minted
    # it, and the browser mints locally.
    scraper = Scraper(min_gap=min_gap, _pool=[None])
    targets = [s for s in SEARCHES if not only or s[0] in only]
    stats = {"added": 0, "queries": 0, "errors": 0, "short": 0,
             "capped": 0, "advertised": 0}

    def summary() -> str:
        mb, adv = WIRE["bytes"] / 1e6, stats["advertised"]
        pct = f"{stats['added'] / adv * 100:.0f}%" if adv else "n/a"
        return (f"{stats['added']} new listings → {out}\n"
                f"queries {stats['queries']}/{len(targets)} complete, coverage "
                f"{stats['added']:,}/{adv:,} advertised ({pct}), "
                f"{stats['short']} short of their advertised total, "
                f"{stats['capped']} price-degenerate windows, "
                f"{stats['errors']} errors\n"
                f"wire: {WIRE['requests']} requests, {mb:.1f} MB compressed "
                f"(what a proxy would bill), {token.mints} WAF token mints "
                f"— log: {out}.log")

    with graceful(logger, summary), \
            out.open("a", encoding="utf-8") as sink, \
            ckpt.open("a", encoding="utf-8") as log:
        for slug, type_id, type_label, operation in targets:
            query = slug
            if not since and done.get(query) == _COMPLETE:
                stats["queries"] += 1
                logger.info("%s: skip (checkpointed)", query)
                continue
            floor = 0 if since else int(done.get(query) or 0)
            logger.info("%s: start at price floor %d", query, floor)

            advertised = got = 0
            bar = None
            aborted = exhausted = False
            for window in range(500):        # bounded: each window advances the floor
                page = 1
                cards: list[PincaliListing] = []
                while page <= PAGE_CAP:
                    url = search_url(slug, type_id, operation, page, floor,
                                     "date_activated-desc" if since else "price-asc")
                    try:
                        html = fetch(scraper, url=url, token=token,
                                     referer=f"{BASE}/inmuebles/{slug}")
                    except RuntimeError as exc:
                        stats["errors"] += 1
                        logger.error("%s [floor %d page %d] -> %s", query, floor, page, exc)
                        aborted = True
                        break
                    cards = list(parse_serp(html, operation))
                    total = total_results(html)
                    if total is None:
                        total = len(cards)
                    bad = _verify(cards, type_label, operation, floor)
                    if bad:
                        stats["errors"] += 1
                        logger.error("%s [floor %d page %d]: %s — %s",
                                     query, floor, page, bad, url)
                        print(f"    ✖ {query}: {bad}", file=sys.stderr)
                        aborted = True
                        break
                    if bar is None:
                        advertised = total
                        stats["advertised"] += advertised
                        bar = tqdm(desc=query, unit=" listing", total=advertised)
                        logger.info("%s: %d advertised", query, advertised)

                    new = [c for c in cards
                           if (c.listingId, c.operation) not in seen]
                    for listing in new:
                        seen.add((listing.listingId, listing.operation))
                        sink.write(json.dumps(serialize(listing), ensure_ascii=False) + "\n")
                    sink.flush()
                    stats["added"] += len(new)
                    got += len(new)
                    bar.update(len(new))
                    logger.info("%s [floor %d page %d] total=%d cards=%d new=%d",
                                query, floor, page, total, len(cards), len(new))

                    if since and cards and all(c.listedAt and c.listedAt < since
                                               for c in cards):
                        exhausted = True     # newest-first: past the watermark
                        break
                    if not cards or page * PER_PAGE >= total:
                        exhausted = True
                        break
                    page += 1
                if aborted or exhausted or since:
                    break        # delta mode is one newest-first window, no keyset
                nxt = next_floor(cards, floor)
                if nxt == floor + 1 and cards and cards[-1].price == floor:
                    stats["capped"] += 1
                    logger.warning("%s: >%d listings at price %d, skipping the "
                                   "overflow", query, PER_PAGE * PAGE_CAP, floor)
                floor = nxt
                log.write(f"{query}@{floor}\n")
                log.flush()
            else:
                logger.warning("%s: hit the 500-window guard at floor %d", query, floor)

            if bar:
                bar.close()
            if aborted:
                logger.warning("%s: incomplete (aborted mid-sweep)", query)
                continue
            stats["queries"] += 1
            if not since:
                log.write(query + "\n")
                log.flush()
            if not floor and advertised and got < advertised * 0.9 and not since:
                stats["short"] += 1
                logger.warning("%s: yielded %d of %d advertised (%.0f%%)",
                               query, got, advertised, got / advertised * 100)
            logger.info("%s: complete (%d rows)", query, got)


# --------------------------------------------------------------------------- #
def survey(min_gap=MIN_GAP) -> None:
    """Size the job and prove each type+operation pair resolves, before paying
    for the crawl. A wrong filter does not 404 here — it serves the corpus — so
    the check is that the rows come back labelled with the type we asked for."""
    ensure_display()
    token = WafToken()
    scraper = Scraper(min_gap=min_gap, _pool=[None])
    grand = bad = 0
    print(f"{'query':<38} {'listings':>9}  {'site label':<26} pages")
    for slug, type_id, type_label, operation in SEARCHES:
        url = search_url(slug, type_id, operation)
        try:
            html = fetch(scraper, token=token, url=url)
        except RuntimeError as exc:
            print(f"  ✖ {slug:<36} {exc}")
            bad += 1
            continue
        total = total_results(html) or 0
        cards = list(parse_serp(html, operation))
        label = Counter(c.propertyType for c in cards if c.propertyType).most_common(1)
        label = label[0][0] if label else "(none)"
        flag = ""
        if label != type_label:
            flag = "  ✖ filter dropped — rows are another type"
            bad += 1
        grand += total
        windows = -(-total // (PER_PAGE * PAGE_CAP))
        print(f"{slug:<38} {total:>9,}  {label:<26} "
              f"{-(-total // PER_PAGE) + max(windows - 1, 0):>5}{flag}")

    pages = -(-grand // PER_PAGE) + len(SEARCHES)
    kb = WIRE["bytes"] / max(WIRE["requests"], 1) / 1024
    print(f"\n  TOTAL {grand:,} listings ≈ {pages:,} requests at {PER_PAGE}/page "
          f"≈ {pages * kb / 1024:.0f} MB ({kb:.0f} KB/page gzipped) "
          f"≈ {pages * min_gap / 3600:.1f} h at {min_gap:g}s/request "
          f"+ {pages * min_gap / TOKEN_TTL * 5.4 / 60:.0f} min of token mints")
    print(f"  survey cost: {WIRE['requests']} requests, {WIRE['bytes'] / 1e6:.1f} MB")
    if bad:
        print(f"  ✖ {bad} query(s) failed to resolve — fix before crawling")


# --------------------------------------------------------------------------- #
# live monitor
# --------------------------------------------------------------------------- #
# What the 2026-07-30 survey measured per query, as the denominator for progress.
# Drifts by a few rows a day; it is a yardstick, not a checksum.
SURVEYED = {
    "terrenos-en-venta": 71190, "terrenos-en-renta": 2849,
    "terrenos-comerciales-en-venta": 4609, "terrenos-comerciales-en-renta": 1889,
    "terrenos-industriales-en-venta": 3501, "terrenos-industriales-en-renta": 501,
    "locales-comerciales-en-venta": 4285, "locales-comerciales-en-renta": 12532,
    "locales-en-centro-comercial-en-venta": 542,
    "locales-en-centro-comercial-en-renta": 1680,
}

_LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\w+) +(.*)$")


def _crawl_pids() -> list[int]:
    """The running crawl, read straight off /proc — no `pgrep -f`, whose pattern
    matches the watcher's own command line and waits on itself forever."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode().split("\0")
        except OSError:
            continue                       # process exited between listing and read
        # argv[0] must be the interpreter, or the `xvfb-run` wrapper shell — whose
        # command line also names the script — doubles every hit.
        if (any("pincali_scraper.py" in a for a in argv[1:])
                and "python" in argv[0] and "--status" not in argv):
            pids.append(int(entry.name))
    return pids


def status(out_path, stall_after: float = 600.0, _pids=None) -> int:
    """One-shot health read of a run in flight: no network, no browser, no loop.

    Exit code is the contract, so a watcher can poll this instead of sleeping
    blind — **0** healthy (running, or finished with every query done), **1**
    something needs a look, **2** not running and not finished.

        watch -n 60 '.venv/bin/python pincali_scraper.py --status'
        until .venv/bin/python pincali_scraper.py --status; do sleep 300; done

    The three failure shapes this run actually produced, each with its own tell:
    a stalled log (nothing since `stall_after`), a **mint storm** (>3 WAF tokens
    in 10 minutes means the token being installed is not the one being sent),
    and queries that aborted mid-sweep and were skipped."""
    out, now = Path(out_path), datetime.now()
    log_path = out.with_name(out.name + ".log")
    if not log_path.exists():
        print(f"✖ no run found at {out_path}")
        return 2

    rows = sum(1 for _ in out.open(encoding="utf-8")) if out.exists() else 0
    pids = _crawl_pids() if _pids is None else _pids   # _pids: seam for --selfcheck
    lines = [m.groups() for m in
             (_LOG_LINE.match(l) for l in log_path.read_text(encoding="utf-8").splitlines())
             if m]
    # Health is about *this* run: <out>.log is appended across restarts, so the
    # errors of a run you already fixed would otherwise be reported forever.
    starts = [i for i, (_, _, msg) in enumerate(lines) if msg.startswith("run start")]
    lines = lines[starts[-1]:] if starts else lines
    stamps = [datetime.strptime(t, "%Y-%m-%d %H:%M:%S") for t, _, _ in lines]
    idle = (now - stamps[-1]).total_seconds() if stamps else 0.0
    mints_10m = sum(1 for ts, (_, _, msg) in zip(stamps, lines)
                    if "token minted" in msg and (now - ts).total_seconds() < 600)
    errors = [msg for _, lvl, msg in lines if lvl == "ERROR"]
    aborted = [msg for _, lvl, msg in lines if lvl == "WARNING" and "aborted" in msg]

    done = _load_done(out.with_name(out.name + ".done"))
    complete = [q for q, v in done.items() if v == _COMPLETE]
    pending = [s[0] for s in SEARCHES if done.get(s[0]) != _COMPLETE]

    # Rate over the last 200 progress lines: recent enough to reflect a slowdown.
    prog = [(ts, msg) for ts, (_, _, msg) in zip(stamps, lines) if " page " in msg][-200:]
    rate = 0.0
    if len(prog) > 1:
        span = (prog[-1][0] - prog[0][0]).total_seconds()
        rate = (len(prog) - 1) / span if span > 0 else 0.0
    # Rough on purpose: the survey total minus what is banked, at the current
    # page rate. Overlap between price windows makes it slightly pessimistic.
    left = max(sum(SURVEYED.values()) - rows, 0)
    eta = f"{left / PER_PAGE / rate / 3600:.1f} h" if rate else "n/a"

    state = "RUNNING" if pids else ("FINISHED" if not pending else "NOT RUNNING")
    print(f"{state:<12} pid={pids or '-'}  {rows:,} rows  "
          f"{len(complete)}/{len(SEARCHES)} queries complete")
    print(f"  last log entry {idle:.0f}s ago  ·  {rate * 60:.0f} pages/min  ·  "
          f"ETA {eta}  ·  {mints_10m} token mints in 10 min")
    print(f"  errors {len(errors)}  ·  aborted queries {len(aborted)}")
    if lines:
        print(f"  → {lines[-1][2][:110]}")
    for slug in [s[0] for s in SEARCHES]:
        mark = "✔" if done.get(slug) == _COMPLETE else (
            f"@{done[slug]}" if slug in done else "·")
        print(f"    {mark:>12}  {slug:<38} {SURVEYED.get(slug, 0):>7,} surveyed")

    bad = []
    if pids and idle > stall_after:
        bad.append(f"stalled: nothing logged in {idle / 60:.0f} min")
    if mints_10m > 3:
        bad.append(f"mint storm: {mints_10m} tokens in 10 min — the token being "
                   f"installed is probably not the one being sent (see _install)")
    if aborted:
        bad.append(f"{len(aborted)} query(s) aborted mid-sweep: {aborted[-1][:70]}")
    if not pids and pending:
        bad.append(f"not running, {len(pending)} query(s) unfinished — resume with "
                   f"the same --out, the checkpoint picks up")
    for line in bad:
        print(f"  ✖ {line}")
    if bad:
        return 1
    print("  ✔ healthy" if pids else "  ✔ complete")
    return 0


def audit(path) -> None:
    """Offline coverage audit: bucket rows by the province the *site* reported,
    then report what share of each field actually arrived."""
    rows = [json.loads(l) for l in Path(path).open(encoding="utf-8") if l.strip()]
    if not rows:
        sys.exit(f"{path} is empty")
    ids = {r["listingId"] for r in rows}
    keys = {(r["listingId"], r.get("operation")) for r in rows}
    by_prov = Counter(r.get("province") or "(missing)" for r in rows)
    # A property on sale AND for rent is two rows by design — one per operation,
    # each with that operation's price. Only a repeated (id, operation) is a dup.
    print(f"\n{len(rows):,} rows, {len(ids):,} properties, {len(keys):,} "
          f"property×operation ({len(rows) - len(keys):,} duplicate lines, "
          f"{len(rows) - len(ids):,} listed under both operations)\n")
    for prov, n in sorted(by_prov.items(), key=lambda kv: -kv[1]):
        mark = " " if prov in PROVINCES else "?"
        print(f" {mark}{prov:<34} {n:>7,}")
    missing = [p for p in PROVINCES if p not in by_prov]
    print(f"\n{sum(p in by_prov for p in PROVINCES)}/32 states present"
          + (f" — missing: {', '.join(missing)}" if missing else ""))

    land = [r for r in rows if r.get("propertyType") in LAND_TYPES]
    filled = {k: sum(1 for r in rows if r.get(k) not in (None, "", False)) for k in
              ("price", "coordinates", "areaM2", "listedAt", "description", "city")}
    print(f"\noperations: {dict(Counter(r.get('operation') for r in rows))}")
    print(f"types:      {dict(Counter(r.get('propertyType') for r in rows).most_common())}")
    print("field fill: " + ", ".join(f"{k}={v / len(rows) * 100:.0f}%"
                                     for k, v in filled.items()))
    exact = sum(1 for r in rows if r.get("coordsExact"))
    perm2 = sum(1 for r in rows if r.get("priceIsPerM2"))
    plot = sum(1 for r in land if r.get("plotAreaM2"))
    print(f"coordinates exact: {exact:,}/{len(rows):,} "
          f"({exact / len(rows) * 100:.0f}%) — the rest are colonia centroids")
    print(f"priced per m²:     {perm2:,} rows (price is $/m², not the total)")
    print(f"lot size known:    {plot:,}/{len(land):,} land-type rows "
          f"({plot / max(len(land), 1) * 100:.0f}%); "
          f"{len(rows) - len(land):,} built rows report construction m² only")


def _selfcheck_status() -> None:
    """The monitor has to detect the three ways this crawl has actually broken,
    so drive it over synthetic logs rather than waiting for the next outage."""
    import io
    import tempfile
    from contextlib import redirect_stdout

    def run(entries, done_lines, pids, stall_after=600.0):
        """`stall_after=0` is how the stall branch is provoked: the log is fresh,
        so shrink the patience instead of forging old timestamps."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "x.jsonl"
            out.write_text('{"listingId": "EB-1"}\n', encoding="utf-8")
            stamp = datetime.now().replace(microsecond=0)
            body = [f"{stamp:%Y-%m-%d %H:%M:%S} INFO    run start → {out}"]
            for lvl, msg in entries:
                body.append(f"{stamp:%Y-%m-%d %H:%M:%S} {lvl:<7} {msg}")
            out.with_name(out.name + ".log").write_text("\n".join(body), encoding="utf-8")
            out.with_name(out.name + ".done").write_text("\n".join(done_lines),
                                                         encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = status(out, stall_after=stall_after, _pids=pids)
            return code, buf.getvalue()

    every = [s[0] for s in SEARCHES]
    page = [("INFO", "terrenos-en-venta [floor 0 page 1] total=9 cards=9 new=9")]

    code, text = run(page, every, pids=[])
    assert code == 0 and "complete" in text, text          # every query checkpointed
    code, text = run(page, ["terrenos-en-renta"], pids=[])
    assert code == 1 and "not running" in text, text       # died with work left
    code, text = run(page, ["terrenos-en-renta"], pids=[42])
    assert code == 0 and "healthy" in text, text           # mid-run, progressing

    storm = page + [("INFO", f"WAF token minted in 5.0s (#{i})") for i in range(1, 6)]
    code, text = run(storm, every, pids=[42])
    assert code == 1 and "mint storm" in text, text        # the bug that cost 2 runs
    dead = page + [("WARNING", "terrenos-en-venta: incomplete (aborted mid-sweep)")]
    code, text = run(dead, every, pids=[42])
    assert code == 1 and "aborted mid-sweep" in text, text
    code, text = run(page, every, pids=[42], stall_after=0)
    assert code == 1 and "stalled" in text, text

    # A log from an earlier, broken run must not condemn the current one.
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "x.jsonl"
        out.write_text("{}\n", encoding="utf-8")
        now = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        out.with_name(out.name + ".log").write_text(
            f"2020-01-01 00:00:00 INFO    run start → old\n"
            f"2020-01-01 00:00:01 ERROR   everything is on fire\n"
            f"{now} INFO    run start → {out}\n"
            f"{now} INFO    terrenos-en-venta [floor 0 page 1] total=9 cards=9 new=9\n",
            encoding="utf-8")
        out.with_name(out.name + ".done").write_text("\n".join(every), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = status(out, _pids=[42])
        assert code == 0, f"stale errors leaked into this run's health:\n{buf.getvalue()}"


def _selfcheck() -> None:
    """Offline: URL grammar, the keyset step, then a saved SERP fixture."""
    u = search_url("terrenos-en-venta", 29059, "sale")
    assert u.startswith(BASE + "/inmuebles/terrenos-en-venta?"), u
    assert "search_criteria%5Bproperty_type_ids%5D%5B%5D=29059" in u, u
    assert "search_criteria%5Boperation_type%5D=sale" in u, u
    assert "min_price" not in u, "floor 0 must not emit a filter"
    assert "page=" not in u, "page 1 must not emit a page param"
    u2 = search_url("terrenos-en-venta", 29059, "sale", page=100, floor=2750)
    assert u2.endswith("&page=100"), u2
    assert "search_criteria%5Bmin_price%5D=2750" in u2, u2

    # Keyset: the floor is the LAST row's price (DOM order == price-asc order),
    # not the max, and it always advances even on a degenerate page.
    def _row(price, currency="MXN", **kw):
        return PincaliListing(listingId="x", url="", price=price,
                              currency=currency, **kw)

    assert next_floor([_row(p) for p in (100, 200, 300)], 0) == 300, \
        "floor must be the last row served"
    assert next_floor([_row(500)] * 3, 500) == 501, \
        "a single-price window must still advance"
    assert next_floor([], 7) == 8, "an empty page must still advance"
    # A USD tail reports 193 against a 2,750-peso floor: taking it would walk the
    # sweep backwards. This is the bug the first nationwide run hit at page 12.
    assert next_floor([_row(3000), _row(193, "USD")], 2750) == 3000, \
        "the floor must come from a peso row"
    assert next_floor([_row(193, "USD")], 2750) == 2751, "USD-only page advances"

    # Token replacement. `cookies.set` alone leaves the old value in place once
    # the server has set one of its own, which cost two nationwide runs.
    class _FakeSession:
        def __init__(self):
            self.cookies = __import__("curl_cffi").requests.Session().cookies

    sc = type("S", (), {})()
    sc.sess = _FakeSession()
    _install(sc, "first")
    sc.sess.cookies.set("aws-waf-token", "server-copy", domain="www.pincali.com")
    _install(sc, "second")
    jarred = {c.value for c in sc.sess.cookies.jar if c.name == "aws-waf-token"}
    assert jarred == {"second"}, f"stale WAF token survived the refresh: {jarred}"

    # Dedupe key. On the id alone, a property offered both ways loses its rental
    # side silently — the renta query just yields 81% and nothing errors.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        f = Path(tmp) / "seen.jsonl"
        f.write_text('{"listingId": "EB-1", "operation": "sale"}\n', encoding="utf-8")
        seen = _load_seen(f)
        assert ("EB-1", "sale") in seen, seen
        assert ("EB-1", "rent") not in seen, "the rental side must still be crawlable"

    # The guard that keeps a dropped filter from being scraped as if it were real.
    assert not _verify([_row(100, propertyType="Terreno", operation="sale")],
                       "Terreno", "sale", 50)
    assert _verify([_row(1, propertyType="Casa", operation="sale")] * 3,
                   "Terreno", "sale", 0), "wrong type must fail"
    # ...and does not fire on USD rows that are only nominally below the floor.
    assert not _verify([_row(193, "USD", propertyType="Terreno", operation="sale")] * 42,
                       "Terreno", "sale", 2750), "USD rows are not a filter failure"

    _selfcheck_status()

    fx = Path(__file__).parent / ".fixtures" / "pincali_serp.html"
    if not fx.exists():
        print("URL/keyset/verify checks OK; save a SERP to "
              ".fixtures/pincali_serp.html for the parse check")
        return
    html = fx.read_text(encoding="utf-8")
    cards = list(parse_serp(html, "sale"))
    assert len(cards) == PER_PAGE, f"expected {PER_PAGE} cards, got {len(cards)}"
    assert total_results(html), "[data-total] gone — the keyset walks blind"
    c = cards[0]
    assert c.listingId.startswith("EB-"), c.listingId
    assert c.url.startswith(BASE + "/inmueble/"), c.url
    # The three fields this scraper exists for.
    assert sum(bool(x.coordinates) for x in cards) > len(cards) * 0.9, \
        "coordinates gone from the SERP — that was the whole point"
    assert all(-118 < x.coordinates[1] < -86 and 14 < x.coordinates[0] < 33
               for x in cards if x.coordinates), "coordinates outside Mexico"
    assert sum(bool(x.areaM2) for x in cards) > len(cards) // 2, "areas gone"
    assert sum(bool(x.price) for x in cards) > len(cards) // 2, \
        "prices gone — the keyset needs them"
    assert sum(bool(x.province) for x in cards) > len(cards) // 2, \
        "addressRegion gone — the audit breaks"
    assert sum(bool(x.listedAt) for x in cards) > len(cards) // 2, "datePosted gone"
    land = [x for x in cards if x.propertyType in LAND_TYPES]
    assert all(x.plotAreaM2 and not x.builtAreaM2 for x in land if x.areaM2), \
        "a land type reported construction area"
    print(f"OK selfcheck: {len(cards)} cards, total={total_results(html):,}, "
          f"sample={c.propertyType!r} {c.price} {c.currency} "
          f"per_m2={c.priceIsPerM2} {c.coordinates} exact={c.coordsExact} "
          f"area={c.areaM2} city={c.city!r} province={c.province!r}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/pincali.jsonl")
    ap.add_argument("--only", nargs="*", default=None,
                    help=f"limit to these query slugs (default: all "
                         f"{len(SEARCHES)})")
    ap.add_argument("--min-gap", type=float, default=MIN_GAP,
                    help="seconds between requests (robots.txt asks for 1; the "
                         "WAF's rate rule wants more)")
    ap.add_argument("--since", default="",
                    help="delta run: walk newest-first, stop at this ISO date")
    ap.add_argument("--status", action="store_true",
                    help="one-shot health read of a run in flight; exit 0 healthy, "
                         "1 needs a look, 2 not running and unfinished")
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--audit", metavar="JSONL", nargs="?", const="data/pincali.jsonl")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.status:
        sys.exit(status(args.out))
    elif args.selfcheck:
        _selfcheck()
    elif args.audit:
        audit(args.audit)
    elif args.survey:
        survey(args.min_gap)
    else:
        ensure_display()
        crawl(args.out, args.min_gap, args.only, args.since)


if __name__ == "__main__":
    main()
