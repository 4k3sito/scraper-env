"""Mercado Libre Inmuebles México — Locales Comerciales (renta+venta) y
Terrenos (venta), nacional.

Single phase, no detail crawl: every SERP page ships a
`<script type="application/ld+json">` `@graph` with one `RealEstateListing` per
card, already carrying price, **full description**, address, floor size, seller
and `datePosted`. curl_cffi Chrome-TLS impersonation clears the gate — no
browser, and no login (unlike the older `mercadolibre.py`, which drove
Botasaurus through a persistent profile just to reach the same fields).

How you shard is dictated by robots.txt, which here is unusually decisive:

    Disallow: /*_Desde_          ← pagination itself
    Disallow: /*_NoIndex_True
    Disallow: /*_OrderId_        ← every sort order
    Disallow: /*_PublishedToday_ ← the only publication-date filter
    Allow:    /*_PriceRange_     ← the one filter that *is* sanctioned
    Crawl-delay: 5

So: page 1 only, no sort, and the price axis is the sanctioned way to reach past
listing 48. That is not merely a courtesy — the server also stops serving results
past offset ~2000 (`_Desde_2017` returns zero cards), so pagination could not
exhaust a 4,571-result state anyway. Sharding was mandatory; robots just says
which axis to use.

The partition is a price sweep, left to right. Each request returns 48 listings
*and* the range's total, so a range that overflows is split using the quantiles
of the 48 prices it just returned — internal nodes of the tree are never wasted
requests, they are the pages that told us where to cut. `--paginate` drains a
range that cannot be split any further (>48 listings at a single price) with
`_Desde_`; it is off by default because robots asks us not to.

No delta mode: `_PublishedToday_` and every `_OrderId_` sort are Disallowed, so
there is no conformant way to ask for "only what changed". `datePosted` is
captured per row, so deltas are computable offline between two runs.

    .venv/bin/python mercadolibre_scraper.py --survey     # size the job + verify slugs
    .venv/bin/python mercadolibre_scraper.py --out data/mercadolibre.jsonl
    .venv/bin/python mercadolibre_scraper.py --audit      # offline coverage check
    .venv/bin/python mercadolibre_scraper.py --selfcheck  # offline fixture parse
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from selectolax.parser import HTMLParser
from tqdm import tqdm

from stealth_scraper import Scraper
from scrape_utils import graceful, setup_logging
# fetch/WIRE/Listing are transport+schema, not Navent-specific: reused as-is so
# all four corpora share one row shape and one wire meter.
from navent_serp import WIRE, Listing, fetch, serialize

BASE = "https://inmuebles.mercadolibre.com.mx"
PER_PAGE = 48
DESDE_CAP = 2000          # results past this offset are not served, at any depth

# Category root -> operation. Mercado Libre bakes the operation into the path.
SEARCHES = [
    ("locales-comerciales/renta", "rent"),
    ("locales-comerciales/venta", "sale"),
    ("terrenos/venta", "sale"),
]

# Split a range into ~total/TARGET children. Below PER_PAGE so a slightly wrong
# quantile estimate still lands most children on a single page.
TARGET = 40
# Rows actually banked per request, measured over the full nationwide run
# (57,921 rows / 2,963 requests). Lower than TARGET because ranges that overshoot
# get re-split, and the re-splits return mostly rows the parent already handed
# over. Used for --survey estimates: quoting TARGET understates the job by ~2×.
YIELD = 19.5

_COMPLETE = "done"

STATES = [
    "aguascalientes", "baja-california", "baja-california-sur", "campeche",
    "chiapas", "chihuahua", "coahuila", "colima", "distrito-federal", "durango",
    "estado-de-mexico", "guanajuato", "guerrero", "hidalgo", "jalisco",
    "michoacan", "morelos", "nayarit", "nuevo-leon", "oaxaca", "puebla",
    "queretaro", "quintana-roo", "san-luis-potosi", "sinaloa", "sonora",
    "tabasco", "tamaulipas", "tlaxcala", "veracruz", "yucatan", "zacatecas",
]

# The site's own spelling of each state, as it appears in JSON-LD addressRegion.
# Used to prove the location filter actually applied — a dropped path segment
# returns the national corpus with HTTP 200 and no other signal. Filled from
# `--survey`; states absent here simply skip the check.
PROVINCE_LABEL = {
    "aguascalientes": "Aguascalientes",
    "baja-california": "Baja California",
    "baja-california-sur": "Baja California Sur",
    "campeche": "Campeche",
    "chiapas": "Chiapas",
    "chihuahua": "Chihuahua",
    "coahuila": "Coahuila",
    "colima": "Colima",
    "distrito-federal": "Distrito Federal",
    "durango": "Durango",
    "estado-de-mexico": "Estado De México",
    "guanajuato": "Guanajuato",
    "guerrero": "Guerrero",
    "hidalgo": "Hidalgo",
    "jalisco": "Jalisco",
    "michoacan": "Michoacán",
    "morelos": "Morelos",
    "nayarit": "Nayarit",
    "nuevo-leon": "Nuevo León",
    "oaxaca": "Oaxaca",
    "puebla": "Puebla",
    "queretaro": "Querétaro",
    "quintana-roo": "Quintana Roo",
    "san-luis-potosi": "San Luis Potosí",
    "sinaloa": "Sinaloa",
    "sonora": "Sonora",
    "tabasco": "Tabasco",
    "tamaulipas": "Tamaulipas",
    "tlaxcala": "Tlaxcala",
    "veracruz": "Veracruz",
    "yucatan": "Yucatán",
    "zacatecas": "Zacatecas",
}


# --------------------------------------------------------------------------- #
# URL grammar
# --------------------------------------------------------------------------- #
def _search_url(category: str, state: str, lo: int = 0, hi: int = 0,
                offset: int = 0) -> str:
    """`hi=0` means "no upper bound" — `_PriceRange_100000-0` is how the site
    spells an open-ended floor. Both ends are inclusive, so adjacent shards are
    built to overlap by one peso rather than risk a gap (dedupe absorbs it)."""
    url = f"{BASE}/{category}/{state}/"
    if lo or hi:
        url += f"_PriceRange_{lo}-{hi}"
    if offset:
        # robots Disallows this; only reachable behind --paginate. ML's grammar
        # chains `_Key_Value` segments, so no extra separator goes here.
        url += f"_Desde_{offset}_NoIndex_True"
    return url


# --------------------------------------------------------------------------- #
# SERP parsing: JSON-LD for the data, the card DOM for what JSON-LD omits
# --------------------------------------------------------------------------- #
_MLM = re.compile(r"MLM-?(\d+)")
_TOTAL = re.compile(r"([\d,\.]{1,12})\s*resultados")
_AREA = re.compile(r"([\d.,]+)\s*m²\s*(construidos|de terreno|totales)", re.I)


def total_results(html: str) -> int | None:
    """The "N resultados" headline. Absent on very small result sets, in which
    case the caller falls back to the card count — which is exact there, because
    everything fits on the one page we already have."""
    m = _TOTAL.search(html)
    return int(re.sub(r"[,.]", "", m.group(1))) if m else None


def _jsonld_listings(html: str) -> dict[str, dict]:
    """MLM id -> RealEstateListing node from the SERP's ld+json @graph."""
    out: dict[str, dict] = {}
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            doc = json.loads(node.text())
        except json.JSONDecodeError:
            continue
        graph = doc.get("@graph") if isinstance(doc, dict) else doc
        for obj in (graph if isinstance(graph, list) else [graph]):
            if not isinstance(obj, dict) or obj.get("@type") != "RealEstateListing":
                continue
            url = (obj.get("offers") or {}).get("url") or obj.get("mainEntityOfPage") or ""
            m = _MLM.search(url)
            if m:
                out[m.group(1)] = obj
    return out


def _num(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", text.replace(",", "")))
    except ValueError:
        return None


def parse_serp(html: str, operation: str):
    """Yield one Listing per result card. The @graph carries price, description,
    address, floorSize, seller and datePosted; the card adds the property type
    and the full location chain (colonia → municipio → estado), which the
    PostalAddress flattens away."""
    ld = _jsonld_listings(html)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for card in HTMLParser(html).css("li.ui-search-layout__item"):
        link = card.css_first("a.poly-component__title")
        href = link.attributes.get("href", "") if link else ""
        m = _MLM.search(href)
        if not m:
            continue
        lid = m.group(1)
        obj = ld.get(lid, {})
        offers = obj.get("offers") or {}
        addr = obj.get("address") or {}

        headline = _text(card, "span.poly-component__headline")   # "Terreno en venta"
        prop_type = re.split(r"\s+en\s+", headline, maxsplit=1)[0].strip() if headline else ""
        chain = _text(card, "span.poly-component__location")

        built = plot = None
        for item in card.css("li.poly-attributes_list__item"):
            am = _AREA.search(item.text())
            if not am:
                continue
            value = _num(am.group(1))
            if am.group(2).lower().startswith("construid"):
                built = value
            else:
                plot = value

        posted = obj.get("datePosted") or ""
        yield Listing(
            listingId=lid,
            url=(offers.get("url") or href).split("#")[0],
            title=obj.get("name") or (link.text().strip() if link else ""),
            imageUrl=obj.get("image") or "",
            operation=operation,
            price=int(offers["price"]) if isinstance(offers.get("price"), (int, float)) else None,
            currency=offers.get("priceCurrency") or "MXN",
            propertyType=prop_type,
            areaM2=_num(str((obj.get("floorSize") or {}).get("value") or "")) or built or plot,
            plotAreaM2=plot,
            builtAreaM2=built,
            location=chain,
            city=addr.get("addressLocality") or "",
            province=addr.get("addressRegion") or (chain.split(", ")[-1] if chain else ""),
            agencyName=(obj.get("seller") or {}).get("name") or "",
            agentPhone="",          # not exposed on the SERP; detail page only
            postingCode=f"MLM{lid}",
            description=obj.get("description") or "",
            coordinates=None,       # not exposed on the SERP; detail page only
            listedAt=f"{posted}T00:00:00+00:00" if posted else "",
            observedAt=now,
        )


def _text(node, sel: str) -> str:
    found = node.css_first(sel)
    return found.text().strip() if found else ""


# --------------------------------------------------------------------------- #
# price partition
# --------------------------------------------------------------------------- #
def needs_split(total: int, cards: int) -> bool:
    """Whether a range holds more than the one page we're allowed to ask for.

    Decided on the site's count alone. `total > cards` looks like the obvious
    test and is wrong: Mercado Libre's "N resultados" runs exactly one ahead of
    what it renders on every partial page (15 advertised → 14 `li` → 14 @graph
    entries), so that test fires on *every* leaf and splits the corpus to dust."""
    return total > PER_PAGE


def split_range(lo: int, hi: int, prices, total: int) -> list[tuple[int, int]]:
    """Cut [lo, hi] into ~total/TARGET sub-ranges at the quantiles of the prices
    this range just returned. The 48 cards are a sample of the range, so their
    quantiles are a far better guess than a uniform or geometric cut — prices are
    lognormal and a midpoint split puts ~everything in the lower half.

    Correctness never depends on the estimate: a child that still overflows is
    split again. A bad estimate costs requests, not rows. Returns [] when no
    progress is possible (a single price point holding more than one page)."""
    pool = sorted(p for p in prices if p and p >= lo and (not hi or p <= hi))
    k = max(2, min(60, -(-total // TARGET)))
    if len(pool) >= 2:
        cuts = [pool[round(i * (len(pool) - 1) / k)] for i in range(1, k)]
    elif hi:
        cuts = [int((lo * hi) ** 0.5) or (lo + hi) // 2]   # geometric: prices are lognormal
    else:
        cuts = [max(lo * 4, 1_000)]                        # open top: probe upward

    bounds, prev = [], lo
    for cut in sorted(set(cuts)):
        if cut <= prev or (hi and cut >= hi):
            continue
        bounds.append((prev, cut))
        prev = cut                       # inclusive both ends: overlap, never gap
    bounds.append((prev, hi))
    return bounds if len(bounds) > 1 else []


# --------------------------------------------------------------------------- #
# crawl
# --------------------------------------------------------------------------- #
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


def _load_done(path: Path) -> dict[str, str | int]:
    """query -> "done", or the price the sweep had completed through, so a killed
    run resumes at that floor instead of re-walking shards it already paid for.
    A completed query is never downgraded by a later partial token."""
    done: dict[str, str | int] = {}
    if path.exists():
        for tok in path.read_text(encoding="utf-8").split():
            query, _, floor = tok.partition("@")
            if done.get(query) == _COMPLETE:
                continue
            done[query] = int(floor) if floor else _COMPLETE
    return done


def _verify(cards, state: str, lo: int, hi: int, url: str) -> bool:
    """Read the filters back off the returned rows, not off a chip.

    Both failure modes here are silent 200s: an unrecognised path segment serves
    the *national* corpus, and a dropped `_PriceRange_` serves the *unfiltered*
    one. Prices and provinces are in the rows we just parsed, so check those."""
    want = PROVINCE_LABEL.get(state)
    if want:
        provs = [c.province for c in cards if c.province]
        if provs and sum(p == want for p in provs) < len(provs) / 2:
            print(f"    ✖ state filter not applied on {url}: rows say "
                  f"{Counter(provs).most_common(2)}, wanted {want!r}", file=sys.stderr)
            return False
    # `_PriceRange_` filters in pesos, so USD-denominated rows are legitimately
    # outside it and must not count against the guard.
    priced = [c.price for c in cards if c.price and c.currency == "MXN"]
    # One straggler is a listing edited between the filter and the render; a page
    # that is mostly out of range means the filter was dropped entirely.
    if priced and (lo or hi):
        out_of_range = sum(1 for p in priced if p < lo or (hi and p > hi))
        if out_of_range > max(2, len(priced) // 4):
            print(f"    ✖ price filter not applied on {url}: {out_of_range}/"
                  f"{len(priced)} rows outside [{lo}, {hi or '∞'}]", file=sys.stderr)
            return False
    return True


def crawl(states, out_path, min_gap=5.0, paginate=False, categories=None) -> None:
    """Sweep every (category, state) by price until each shard fits on one page.

    `min_gap` defaults to the 5 s robots.txt asks for. `paginate` enables the
    Disallowed `_Desde_` walk, and only for shards the price axis cannot split —
    a single price point with more than 48 listings. Everything else is
    robots-conformant by construction.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ckpt = out.with_name(out.name + ".done")
    seen = _load_seen(out)
    done = _load_done(ckpt)
    logger = setup_logging(out)
    scraper = Scraper(min_gap=min_gap)
    searches = [(c, o) for c, o in SEARCHES if not categories or c in categories]
    targets = [(c, o, s) for c, o in searches for s in (states or STATES)]
    stats = {"added": 0, "queries": 0, "errors": 0, "short": 0,
             "capped": 0, "advertised": 0}

    def summary() -> str:
        mb, adv = WIRE["bytes"] / 1e6, stats["advertised"]
        pct = f"{stats['added'] / adv * 100:.0f}%" if adv else "n/a"
        return (f"{stats['added']} new listings → {out}\n"
                f"queries {stats['queries']}/{len(targets)} complete, coverage "
                f"{stats['added']:,}/{adv:,} advertised ({pct}), "
                f"{stats['short']} short of their advertised total, "
                f"{stats['capped']} price-degenerate shards, "
                f"{stats['errors']} errors\n"
                f"wire: {WIRE['requests']} requests, {mb:.1f} MB compressed "
                f"(what the proxy bills) — log: {out}.log")

    with graceful(logger, summary), \
            out.open("a", encoding="utf-8") as sink, \
            ckpt.open("a", encoding="utf-8") as log:
        for category, operation, state in targets:
            query = f"{category}/{state}"
            if done.get(query) == _COMPLETE:
                stats["queries"] += 1
                logger.info("%s: skip (checkpointed)", query)
                continue
            start = int(done.get(query) or 0)
            scraper.rotate()          # one fresh visitor identity per shard
            logger.info("%s: start at price floor %d", query, start)

            stack: list[tuple[int, int]] = [(start, 0)]
            advertised = got = 0
            bar = None
            aborted = False
            while stack:
                lo, hi = stack.pop()
                url = _search_url(category, state, lo, hi)
                try:
                    html = fetch(scraper, url, referer=f"{BASE}/{category}/")
                except RuntimeError as exc:
                    stats["errors"] += 1
                    logger.error("%s [%d-%s] -> %s", query, lo, hi or "∞", exc)
                    aborted = True
                    break
                cards = list(parse_serp(html, operation))
                total = total_results(html)
                if total is None:
                    total = len(cards)    # no headline => it all fit on this page

                # Checked on every shard, not just the first: a dropped
                # `_PriceRange_` re-serves the unfiltered corpus, and the sweep
                # would happily split that forever.
                if not _verify(cards, state, lo, hi, url):
                    stats["errors"] += 1
                    aborted = True
                    break
                if bar is None:           # first page of this query
                    advertised = total
                    stats["advertised"] += advertised
                    bar = tqdm(desc=query, unit=" listing", total=advertised)
                    logger.info("%s: %d advertised", query, advertised)

                new = [c for c in cards if c.listingId not in seen]
                for listing in new:
                    seen.add(listing.listingId)
                    sink.write(json.dumps(serialize(listing), ensure_ascii=False) + "\n")
                stats["added"] += len(new)
                got += len(new)
                sink.flush()
                bar.update(len(new))
                logger.info("%s [%d-%s] total=%d cards=%d new=%d",
                            query, lo, hi or "∞", total, len(cards), len(new))

                if needs_split(total, len(cards)):
                    children = split_range(
                        lo, hi, [c.price for c in cards if c.currency == "MXN"], total)
                    if children:
                        stack.extend(reversed(children))   # leftmost first
                        continue
                    stats["capped"] += 1
                    logger.warning("%s [%d-%s]: %d listings at one price point, "
                                   "unsplittable", query, lo, hi or "∞", total)
                    if paginate:
                        got += _drain(scraper, sink, seen, stats, logger, bar,
                                      category, state, operation, lo, hi, total)
                # Leaf done: the sweep is complete through `hi`, so that is the
                # only number a resume needs.
                if hi:
                    log.write(f"{query}@{hi}\n")
                    log.flush()

            if bar:
                bar.close()
            if aborted:
                logger.warning("%s: incomplete (aborted mid-sweep)", query)
                continue
            stats["queries"] += 1
            log.write(query + "\n")
            log.flush()
            # `got` counts rows new to this process, so a resumed query looks
            # short by exactly what the previous run already banked.
            if not start and advertised and got < advertised * 0.9:
                stats["short"] += 1
                logger.warning("%s: yielded %d of %d advertised (%.0f%%)",
                               query, got, advertised, got / advertised * 100)
            logger.info("%s: complete (%d rows)", query, got)


def _drain(scraper, sink, seen, stats, logger, bar, category, state, operation,
           lo, hi, total) -> int:
    """`_Desde_` walk of one unsplittable range. robots Disallows this, which is
    why it sits behind --paginate; the server stops serving past offset ~2000
    regardless, so it is a partial rescue, not an escape hatch."""
    added = 0
    for offset in range(PER_PAGE + 1, min(total, DESDE_CAP) + 1, PER_PAGE):
        url = _search_url(category, state, lo, hi, offset)
        try:
            html = fetch(scraper, url, referer=f"{BASE}/{category}/")
        except RuntimeError as exc:
            stats["errors"] += 1
            logger.error("%s _Desde_%d -> %s", url, offset, exc)
            break
        cards = [c for c in parse_serp(html, operation) if c.listingId not in seen]
        if not cards:
            break
        for listing in cards:
            seen.add(listing.listingId)
            sink.write(json.dumps(serialize(listing), ensure_ascii=False) + "\n")
        sink.flush()
        stats["added"] += len(cards)
        added += len(cards)
        bar.update(len(cards))
    return added


# --------------------------------------------------------------------------- #
def survey(states, min_gap=5.0) -> None:
    """Size the job and prove every state slug resolves, before paying for a
    crawl. A wrong slug does not 404 here — it serves the national corpus — so
    the check is that the rows come back labelled with the state we asked for."""
    scraper = Scraper(min_gap=min_gap)
    grand = requests = bad = 0
    for category, operation in SEARCHES:
        national = total_results(fetch(scraper, f"{BASE}/{category}/")) or 0
        requests += 1
        print(f"\n{category}  ({national:,} nationwide)")
        subtotal = 0
        for state in (states or STATES):
            url = _search_url(category, state)
            try:
                html = fetch(scraper, url)
            except RuntimeError as exc:
                print(f"  ✖ {state:22s} {exc}")
                bad += 1
                continue
            requests += 1
            total = total_results(html) or 0
            cards = list(parse_serp(html, operation))
            label = Counter(c.province for c in cards if c.province).most_common(1)
            label = label[0][0] if label else "(none)"
            flag = ""
            if total == national:
                flag = "  ✖ national corpus — slug dropped"
                bad += 1
            elif cards and PROVINCE_LABEL.get(state) and label != PROVINCE_LABEL[state]:
                flag = f"  ✖ rows say {label!r}"
                bad += 1
            subtotal += total
            print(f"  {state:22s} {total:>7,}  {label:22s}{flag}")
        pct = f"({subtotal / national * 100:.0f}% of nationwide)" if national else ""
        print(f"  {'— sum of states':22s} {subtotal:>7,}  {pct}")
        grand += subtotal

    pages = int(grand / YIELD) + 1
    kb = WIRE["bytes"] / max(WIRE["requests"], 1) / 1024
    print(f"\n  TOTAL {grand:,} listings ≈ {pages:,} requests at ~{YIELD} rows/request "
          f"(measured, incl. re-splits) ≈ {pages * kb / 1024:.0f} MB "
          f"({kb:.0f} KB/page gzipped) ≈ {pages * min_gap / 3600:.1f} h "
          f"at {min_gap:g}s/request")
    print(f"  survey cost: {requests} requests, {WIRE['bytes'] / 1e6:.1f} MB")
    if bad:
        print(f"  ✖ {bad} shard(s) failed to resolve — fix before crawling")


def audit(path, expected=32) -> None:
    """Coverage audit, offline. Buckets rows by the province the site itself
    reported, so a shard that silently resolved elsewhere shows up."""
    rows = [json.loads(l) for l in Path(path).open(encoding="utf-8") if l.strip()]
    ids = {r["listingId"] for r in rows}
    by_prov = Counter(r.get("province") or "(missing)" for r in rows)
    print(f"\n{len(rows):,} rows, {len(ids):,} unique ids "
          f"({len(rows) - len(ids):,} duplicate lines)\n")
    for prov, n in sorted(by_prov.items(), key=lambda kv: -kv[1]):
        print(f"  {prov:34s} {n:>7,}")
    print(f"\n{len(by_prov)}/{expected} provinces present")
    ops = Counter(r.get("operation") for r in rows)
    types = Counter(r.get("propertyType") for r in rows)
    filled = {k: sum(1 for r in rows if r.get(k)) for k in
              ("price", "coordinates", "agentPhone", "description", "listedAt",
               "areaM2", "agencyName")}
    print(f"\noperations: {dict(ops)}")
    print(f"top types:  {dict(types.most_common(6))}")
    print("field fill: " + ", ".join(f"{k}={v / max(len(rows), 1) * 100:.0f}%"
                                     for k, v in filled.items()))


def _selfcheck() -> None:
    """Offline: URL grammar, the partition, then a saved SERP fixture."""
    assert _search_url("terrenos/venta", "jalisco") == \
        BASE + "/terrenos/venta/jalisco/"
    assert _search_url("terrenos/venta", "jalisco", 0, 20000) == \
        BASE + "/terrenos/venta/jalisco/_PriceRange_0-20000"
    assert _search_url("terrenos/venta", "jalisco", 100000, 0) == \
        BASE + "/terrenos/venta/jalisco/_PriceRange_100000-0"
    assert _search_url("terrenos/venta", "jalisco", 0, 0, 49) == \
        BASE + "/terrenos/venta/jalisco/_Desde_49_NoIndex_True"
    assert _search_url("terrenos/venta", "jalisco", 0, 20000, 49) == \
        BASE + "/terrenos/venta/jalisco/_PriceRange_0-20000_Desde_49_NoIndex_True"

    # A partial page is exhausted even though the site claims one more result
    # than it rendered — the whole efficiency of the sweep rests on this.
    assert not needs_split(15, 14), "ML's +1 phantom result must not force a split"
    assert not needs_split(PER_PAGE, PER_PAGE), "a full page that is the whole range"
    assert needs_split(PER_PAGE + 1, PER_PAGE), "one more than a page must split"

    # Partition: contiguous (no gap), strictly advancing, open-ended on the right.
    parts = split_range(0, 0, [10, 20, 30, 40, 50, 60, 70, 80], 400)
    assert parts[0][0] == 0 and parts[-1][1] == 0, parts
    for (a, b), (c, _) in zip(parts, parts[1:]):
        assert b == c and b > a, parts          # inclusive ends → overlap, no gap
    assert split_range(5000, 5000, [5000] * 48, 300) == [], "no-progress must stop"
    assert len(split_range(0, 1000, [1] * 48, 96)) >= 2, "must split a 2-page range"

    fx = Path(__file__).parent / ".fixtures" / "ml_serp.html"
    if not fx.exists():
        print("URL/partition checks OK; save a SERP page to .fixtures/ml_serp.html "
              "for the parse check")
        return
    html = fx.read_text(encoding="utf-8")
    cards = list(parse_serp(html, "rent"))
    assert len(cards) == PER_PAGE, f"expected {PER_PAGE} cards, got {len(cards)}"
    assert total_results(html), "'N resultados' headline gone — the partition is blind"
    c = cards[0]
    assert c.listingId and c.url.startswith("https://"), c.url
    assert c.propertyType and " en " not in c.propertyType, c.propertyType
    assert all(x.price for x in cards), "prices missing — the price sweep needs them"
    assert sum(bool(x.description) for x in cards) > len(cards) // 2, "descriptions gone"
    assert sum(bool(x.listedAt) for x in cards) > len(cards) // 2, "datePosted gone"
    assert sum(bool(x.areaM2) for x in cards) > len(cards) // 2, "areas gone"
    assert c.province, "addressRegion missing — the audit and the guard both break"
    print(f"OK selfcheck: {len(cards)} cards, total={total_results(html):,}, "
          f"sample={c.propertyType!r} {c.price} {c.currency} city={c.city!r} "
          f"province={c.province!r} listedAt={c.listedAt[:10]} area={c.areaM2}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/mercadolibre.jsonl")
    ap.add_argument("--states", nargs="*", default=None,
                    help="limit to these state slugs (default: all 32)")
    ap.add_argument("--categories", nargs="*", default=None,
                    help=f"limit to these roots (default: {[c for c, _ in SEARCHES]})")
    ap.add_argument("--min-gap", type=float, default=5.0,
                    help="seconds between requests (robots.txt asks for 5)")
    ap.add_argument("--paginate", action="store_true",
                    help="drain unsplittable price points with _Desde_ "
                         "(robots Disallows it; off by default)")
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--audit", metavar="JSONL", nargs="?", const="data/mercadolibre.jsonl")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        _selfcheck()
    elif args.audit:
        audit(args.audit)
    elif args.survey:
        survey(args.states, args.min_gap)
    else:
        crawl(args.states, args.out, args.min_gap, args.paginate, args.categories)


if __name__ == "__main__":
    main()
