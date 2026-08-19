"""Inmuebles24 México scraper — Locales Comerciales (renta+venta) y Terrenos (venta).

Single phase. Every search-results page embeds a `script#preloadedData` JSON
blob (`window.__PRELOADED_STATE__`) carrying all 30 listings *fully hydrated* —
price per operation, exact geolocation, plot/built area, description, agency,
photos, the agent's WhatsApp number and a `modified_date` — plus
`paging.total`/`paging.totalPages`. So unlike the Lamudi crawl there is no detail
phase: one request per SERP page yields complete rows (~30× fewer requests).

Transport reuses stealth_scraper.Scraper: curl_cffi Chrome-TLS impersonation
clears inmuebles24's gate (verified — no browser needed). Inmuebles24 fronts with
DataDome *and* Cloudflare; when it flags an IP it 403s or serves an interstitial.
Neither CAPTCHA is the weak solvable kind (cf. Lamudi's plaintext math token), so
the response is to *rotate identity + back off*, never to solve in-process.

The shape of the crawl is set by one hard limit: **`pagina-5` is always a 403**,
so no query URL yields more than 120 listings (see PAGE_CAP). Queries are
therefore sharded by a price keyset — sort ascending, walk the 4 allowed pages,
use the highest price seen as the next shard's `-mas-de-N-pesos` floor — which
stays inside what robots.txt allows and reaches the whole corpus.

    .venv/bin/python inmuebles24_scraper.py --survey    # size the job + check slugs
    .venv/bin/python inmuebles24_scraper.py --out data/inmuebles24.jsonl   # all 32
    .venv/bin/python inmuebles24_scraper.py --days 7    # delta: only new listings
    .venv/bin/python inmuebles24_scraper.py --audit data/inmuebles24.jsonl # offline
    .venv/bin/python inmuebles24_scraper.py --selfcheck                    # offline

Design mirrors lamudi_scraper.py on purpose: same Listing schema, same entry
flow (home → state → Referer chain), same <out>.done query checkpoint, same
resume-by-id. If you know that file, you know this one.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from selectolax.parser import HTMLParser

from tqdm import tqdm

from stealth_scraper import Scraper
from scrape_utils import graceful, setup_logging
from navent_serp import (chain as _nav_chain, BLOCK as _BLOCK, WIRE, Listing, fetch as _navent_fetch,
                         find_postings as _find_postings, num as _num,
                         paging as _paging, preloaded as _preloaded,
                         parse_serp as _parse_serp, serialize as _serialize)

BASE = "https://www.inmuebles24.com"

# Inmuebles24's location taxonomy is ambiguous: a bare state slug often resolves
# to a same-named CITY (e.g. `puebla` → Puebla city) or, worse, to an unrelated
# municipality (`guerrero` → a town in Chihuahua). The province-level page needs
# a disambiguating suffix. These slugs were resolved empirically against the
# site's own `paging.total` + resolved-location label (`--survey` re-checks them),
# so hardcoding them is the lazy correct move. Key = canonical name, value = slug.
STATES = {
    "aguascalientes": "aguascalientes-provincia",
    "baja-california": "baja-california-norte",
    "baja-california-sur": "baja-california-sur",
    "campeche": "campeche-provincia",
    "chiapas": "chiapas",
    "chihuahua": "chihuahua-provincia",
    "ciudad-de-mexico": "ciudad-de-mexico",
    "coahuila": "coahuila",
    "colima": "colima-provincia",
    "durango": "durango-provincia",
    "guanajuato": "guanajuato-provincia",
    "guerrero": "guerrero-provincia",
    "hidalgo": "hidalgo-provincia",
    "jalisco": "jalisco",
    "estado-de-mexico": "edo.-de-mexico",
    "michoacan": "michoacan",
    "morelos": "morelos-provincia",
    "nayarit": "nayarit",
    "nuevo-leon": "nuevo-leon",
    "oaxaca": "oaxaca",
    "puebla": "puebla-provincia",
    "queretaro": "queretaro-provincia",
    "quintana-roo": "quintana-roo-provincia",
    "san-luis-potosi": "san-luis-potosi-provincia",
    "sinaloa": "sinaloa-provincia",
    "sonora": "sonora",
    "tabasco": "tabasco-provincia",
    "tamaulipas": "tamaulipas",
    "tlaxcala": "tlaxcala-provincia",
    "veracruz": "veracruz-provincia",
    "yucatan": "yucatan",
    "zacatecas": "zacatecas-provincia",
}

# What the site's own PROVINCIA label should read for each key above. `--audit`
# compares scraped rows against this, which is the only check that catches a slug
# that resolved to a real but *wrong* place (no error, just a low count).
PROVINCE_LABEL = {
    "aguascalientes": "Aguascalientes", "baja-california": "Baja California Norte",
    "baja-california-sur": "Baja California Sur", "campeche": "Campeche",
    "chiapas": "Chiapas", "chihuahua": "Chihuahua",
    "ciudad-de-mexico": "Ciudad de México", "coahuila": "Coahuila",
    "colima": "Colima", "durango": "Durango", "guanajuato": "Guanajuato",
    "guerrero": "Guerrero", "hidalgo": "Hidalgo", "jalisco": "Jalisco",
    "estado-de-mexico": "Edo. de México", "michoacan": "Michoacán",
    "morelos": "Morelos", "nayarit": "Nayarit", "nuevo-leon": "Nuevo León",
    "oaxaca": "Oaxaca", "puebla": "Puebla", "queretaro": "Querétaro",
    "quintana-roo": "Quintana Roo", "san-luis-potosi": "San luis Potosí",
    "sinaloa": "Sinaloa", "sonora": "Sonora", "tabasco": "Tabasco",
    "tamaulipas": "Tamaulipas", "tlaxcala": "Tlaxcala", "veracruz": "Veracruz",
    "yucatan": "Yucatán", "zacatecas": "Zacatecas",
}

# (realestate-type slug, operation slug). URL: {type}-en-{op}-en-{state}.html,
# paginated with a -pagina-N suffix. operationType names in the JSON are Spanish.
SEARCHES = [
    ("locales-comerciales", "renta"),
    ("locales-comerciales", "venta"),
    ("terrenos", "venta"),
]
OPERATION = {"venta": "sale", "renta": "rent"}
# Which JSON operationType.name each search's price should come from.
_OP_NAME = {"venta": "Venta", "renta": "Renta"}

# Publication-date filter, server-side: the delta lever, and better than the
# sort-newest-first-until-stale dance — the server returns only fresh listings,
# so a delta run walks a handful of pages per query instead of margin pages.
# Only these four windows exist (they're listed in the blob at
# listStore.moreFilters.publicationDate.options); anything
# else (`-publicado-hoy`, `-publicado-hace-menos-de-2-meses`) is silently dropped
# and you get the UNFILTERED corpus back, so the applied filter is verified below.
WINDOWS = {
    7: "-publicado-hace-menos-de-1-semana",
    15: "-publicado-hace-menos-de-15-dias",
    30: "-publicado-hace-menos-de-1-mes",
    45: "-publicado-hace-menos-de-45-dias",
}

# THE constraint on this target: page 5 of any query URL is a hard 403, on a cold
# IP, every time (63/63 failures in the first nationwide attempt were exactly
# `pagina-5`). It is not rate limiting and no amount of rotation clears it —
# robots.txt says the same thing out loud (`Allow: /*pagina-2..5$`,
# `Disallow: /*pagina-*.html`). So a query URL yields at most 4 × 30 = 120
# listings, and the answer is to shard queries below that ceiling, not to evade.
PAGE_CAP = 4
PER_PAGE = 30

# Also from robots.txt: `Allow: /*-ordenado-por-precio-ascendente*` next to
# `Disallow: /*-ordenado-por-*`. Ascending price is the one ordering the site
# invites crawlers to use — and it happens to be the one that makes sharding
# work, because it is *deterministic* (unlike the seeded default, §6) and
# composes with an inclusive `-mas-de-N-pesos` floor. That pair gives keyset
# pagination: walk 120, take the highest price seen, make it the next floor.
SORT_ASC = "-ordenado-por-precio-ascendente"

_COMPLETE = "done"  # sentinel checkpoint state meaning "query exhausted"


def _search_url(state_slug: str, category: str, operation: str, page: int = 1,
                days: int | None = None, min_price: int = 0,
                sort: bool = False) -> str:
    """Suffix order is not free-form — the site drops suffixes it reads out of
    order. Verified working: {window}{price floor}{sort}{page}."""
    path = f"{category}-en-{operation}-en-{state_slug}"
    if days:
        path += WINDOWS[days]
    if min_price:
        path += f"-mas-de-{min_price}-pesos"
    if sort:
        path += SORT_ASC
    if page > 1:
        path += f"-pagina-{page}"
    return f"{BASE}/{path}.html"


def _fetch(scraper: Scraper, url: str, **kw) -> str:
    return _navent_fetch(scraper, url, **kw)


def _verify_filters(obj: dict, url: str, days: int | None, floor: int) -> bool:
    """Read back what the *server* says it applied and compare with what we asked
    for. This is not paranoia: i24 silently drops URL suffixes it doesn't
    recognise and serves the UNFILTERED corpus with a 200. An unnoticed dropped
    `-mas-de-N-pesos` restarts the sweep at zero forever; an unnoticed dropped
    publication window turns a delta run into a full rebuild at delta prices."""
    fs = obj.get("filtersStore") or {}
    checks = [("publicationdate", (fs.get("publicationdate") or {}).get("min"),
               str(days) if days else None),
              ("sort", (fs.get("sort") or {}).get("min"), "low_price"),
              ("price", (fs.get("price") or {}).get("min"),
               str(floor) if floor else None)]
    for label, got, want in checks:
        if (got or None) != want:
            print(f"    ✖ {label} filter not applied on {url}: "
                  f"server says {got!r}, wanted {want!r}", file=sys.stderr)
            return False
    return True


# --------------------------------------------------------------------------- #
# crawl
# --------------------------------------------------------------------------- #
def parse_serp(html: str, operation: str):
    return _parse_serp(html, operation, BASE)


def _enter(scraper: Scraper, state_slug: str) -> str:
    """Arrive like a visitor: home → the state's terrenos landing, once per state.
    Seeds DataDome cookies and gives the first SERP a plausible Referer."""
    _fetch(scraper, BASE + "/")
    landing = _search_url(state_slug, "terrenos", "venta", 1)
    _fetch(scraper, landing, referer=BASE + "/")
    return landing


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
    """query -> "done", or the price floor to resume the sweep from.

    Dedupe-by-id stops duplicate *writes*, not duplicate *fetches*; this stops a
    query killed 40 shards deep from re-walking 40 shards it already paid for.
    Last token for a query wins (the file is append-only, so that's the newest)."""
    done: dict[str, str | int] = {}
    if path.exists():
        for tok in path.read_text(encoding="utf-8").split():
            query, _, floor = tok.partition("@")
            if done.get(query) == _COMPLETE:
                continue  # never downgrade a finished query
            done[query] = int(floor) if floor else _COMPLETE
    return done


def crawl(states, searches, out_path, max_shards, days=None, min_gap=2.5):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Delta runs get their own checkpoint namespace: "colima/terrenos/venta@7d"
    # exhausted means "all listings newer than 7 days", not "the whole query".
    suffix = f"@{days}d" if days else ""
    ckpt = out.with_name(out.name + ".done")
    seen = _load_seen(out)
    done = _load_done(ckpt)
    logger = setup_logging(out)
    scraper = Scraper(min_gap=min_gap)  # DataDome punishes bursts; slow floor on one IP
    planned = len(states) * len(searches)
    stats = {"added": 0, "queries": 0, "errors": 0, "short": 0}

    def summary() -> str:
        mb = WIRE["bytes"] / 1e6
        return (f"{stats['added']} new listings → {out}\n"
                f"queries {stats['queries']}/{planned} complete "
                f"({stats['queries'] / max(planned, 1) * 100:.0f}%), "
                f"{stats['errors']} errors, {stats['short']} short of site total\n"
                f"wire: {WIRE['requests']} requests, {mb:.1f} MB compressed "
                f"(what the proxy bills) — log: {out}.log")

    # graceful outermost: the file handles close (flushing) before the summary prints.
    with graceful(logger, summary), \
            out.open("a", encoding="utf-8") as sink, \
            ckpt.open("a", encoding="utf-8") as log:
        for name in states:
            slug = STATES.get(name, name)
            todo = [s for s in searches
                    if done.get(f"{name}/{s[0]}/{s[1]}{suffix}", 0) != _COMPLETE]
            stats["queries"] += len(searches) - len(todo)  # checkpointed = complete
            if not todo:
                logger.info("%s: skip (checkpointed)", name)
                continue
            scraper.rotate()  # one fresh visitor identity per state
            try:
                referer = _enter(scraper, slug)
            except RuntimeError as exc:
                stats["errors"] += 1
                logger.error("entry %s -> %s", name, exc)
                continue

            for category, operation in todo:
                query = f"{name}/{category}/{operation}{suffix}"
                floor = done.get(query, 0) or 0        # resume the sweep here
                logger.info("%s: start at price floor %s", query, floor)
                ref, bar = referer, None
                exhausted, got, reach = False, 0, None
                for shard in range(1, max_shards + 1):
                    shard_total, last_price, aborted = None, floor, False
                    for page in range(1, PAGE_CAP + 1):
                        url = _search_url(slug, category, operation, page, days,
                                          floor, sort=True)
                        try:
                            html = _fetch(scraper, url, referer=ref)
                        except RuntimeError as exc:
                            stats["errors"] += 1
                            logger.error("%s -> %s", url, exc)
                            break
                        ref = url
                        obj = _preloaded(html) or {}
                        if page == 1:
                            paging = _paging(obj)
                            shard_total = paging.get("total")
                            if not _verify_filters(obj, url, days, floor):
                                stats["errors"] += 1
                                aborted = True   # never keyset-step off a bad shard
                                break
                            if bar is None:
                                # `reach` is the *reachable* total: price-sorted
                                # results silently drop listings with no price,
                                # so the query's unsorted total is not the goal.
                                reach = shard_total
                                bar = tqdm(desc=query, unit=" listing", total=reach)
                                logger.info("%s: %s listings reachable", query, reach)
                        cards = list(parse_serp(html, operation))
                        if not cards:
                            exhausted = True
                            break
                        got += len(cards)
                        new = [c for c in cards if c.listingId not in seen]
                        for listing in new:
                            seen.add(listing.listingId)
                            sink.write(json.dumps(_serialize(listing), ensure_ascii=False) + "\n")
                            sink.flush()
                            stats["added"] += 1
                        last_price = max([c.price for c in cards if c.price] + [last_price])
                        bar.update(len(cards))
                        logger.info("%s shard%d(≥%s) p%d cards=%d new=%d total_new=%d",
                                    query, shard, floor, page, len(cards), len(new),
                                    stats["added"])
                        if page * PER_PAGE >= (shard_total or 0):
                            exhausted = True  # this shard was the last one
                            break
                    if exhausted or aborted or shard_total is None:
                        break
                    # Keyset step: the next shard starts at the highest price we
                    # just saw. `-mas-de-N-pesos` is inclusive, so the boundary
                    # listing repeats — dedupe eats it, and that overlap is what
                    # guarantees no gap. Equal floors would spin forever (>120
                    # listings at one identical price), so force progress by 1.
                    floor = last_price + 1 if last_price <= floor else last_price
                    log.write(f"{query}@{floor}\n")
                    log.flush()
                if bar:
                    bar.close()
                if exhausted:
                    stats["queries"] += 1
                    log.write(query + "\n")
                    if isinstance(reach, int) and reach and got < reach * 0.9:
                        stats["short"] += 1
                        logger.warning("%s: yielded %d of %d reachable (%.0f%%)",
                                       query, got, reach, got / reach * 100)
                    logger.info("%s: complete (%d cards seen)", query, got)
                else:
                    logger.warning("%s: incomplete (stopped at floor %s)", query, floor)
                log.flush()


# --------------------------------------------------------------------------- #
# survey: size the job and prove every slug resolves where you think it does
# --------------------------------------------------------------------------- #
def survey(states, searches, min_gap=2.5) -> None:
    """3 national requests answer 'how big is this job'; one request per state
    answers 'does my slug resolve to the right province'. ~35 requests total,
    run before the crawl. A wrong slug is otherwise invisible: HTTP 200, real
    listings, checkpointed complete, just the wrong place."""
    scraper = Scraper(min_gap=min_gap)
    _fetch(scraper, BASE + "/")

    def totals(url):
        obj = _preloaded(_fetch(scraper, url)) or {}
        posts = _find_postings(obj)
        return _paging(obj).get("total"), (
            _nav_chain((posts[0].get("postingLocation") or {}), "PROVINCIA") if posts else "")

    national = 0
    print("\nnational totals (3 requests):")
    for category, operation in searches:
        total, _ = totals(f"{BASE}/{category}-en-{operation}.html")
        national += total or 0
        print(f"  {category}/{operation:6s} {total:>8,}")
    pages = -(-national // 30)
    print(f"  {'TOTAL':22s} {national:>8,} listings ≈ {pages:,} SERP pages "
          f"≈ {pages * 188 / 1024:.0f} MB on the wire (188 KB/page gzipped, ~6 KB/listing)")

    print(f"\nslug resolution ({len(states)} requests, terrenos/venta):")
    bad = []
    for name in states:
        slug = STATES.get(name, name)
        try:
            total, province = totals(_search_url(slug, "terrenos", "venta", 1))
        except RuntimeError as exc:
            print(f"  ✖ {name:22s} {slug:26s} ERROR {exc}")
            bad.append(name)
            continue
        want = PROVINCE_LABEL.get(name, "")
        ok = province == want
        bad += [] if ok else [name]
        print(f"  {'✔' if ok else '✖'} {name:22s} {slug:26s} "
              f"{total or 0:>7,}  resolves to {province!r}"
              f"{'' if ok else f'  EXPECTED {want!r}'}")
    print(f"\n{len(states) - len(bad)}/{len(states)} slugs verified"
          + (f" — FIX: {', '.join(bad)}" if bad else ""))


# --------------------------------------------------------------------------- #
# audit: coverage check on data already on disk — costs zero requests
# --------------------------------------------------------------------------- #
def audit(path, states) -> None:
    """Bucket scraped rows by the province the site itself reported. A state with
    zero rows carrying its own name did not fail — it silently scraped somewhere
    else. Run after every crawl; it is the only check that catches that."""
    rows = [json.loads(l) for l in Path(path).open(encoding="utf-8") if l.strip()]
    by_province = Counter(r.get("province") or "(missing)" for r in rows)
    ids = {r["listingId"] for r in rows}
    print(f"\n{len(rows):,} rows, {len(ids):,} unique ids "
          f"({len(rows) - len(ids):,} duplicate lines)\n")
    missing = []
    for name in states:
        want = PROVINCE_LABEL.get(name, name)
        n = by_province.pop(want, 0)
        if not n:
            missing.append(name)
        print(f"  {'✔' if n else '✖'} {name:22s} {want:22s} {n:>7,}")
    if by_province:
        print("\n  unexpected provinces in the data (slug resolved elsewhere?):")
        for province, n in by_province.most_common():
            print(f"    {province:44s} {n:>7,}")
    print(f"\n{len(states) - len(missing)}/{len(states)} states covered"
          + (f" — EMPTY: {', '.join(missing)}" if missing else ""))

    ops = Counter(r.get("operation") for r in rows)
    types = Counter(r.get("propertyType") for r in rows)
    filled = {k: sum(1 for r in rows if r.get(k)) for k in
              ("price", "coordinates", "agentPhone", "description", "listedAt", "areaM2")}
    print(f"\noperations: {dict(ops)}")
    print(f"top types:  {dict(types.most_common(6))}")
    print("field fill: " + ", ".join(f"{k}={v / max(len(rows), 1) * 100:.0f}%"
                                     for k, v in filled.items()))


# --------------------------------------------------------------------------- #
def _selfcheck() -> None:
    """Offline parse of a saved SERP fixture — fails if the JSON shape drifts."""
    fx = Path(__file__).parent / ".fixtures" / "inmuebles24_serp.html"
    if not fx.exists():
        print("fixture missing; save one SERP page to .fixtures/inmuebles24_serp.html "
              "to enable offline selfcheck")
        return
    html = fx.read_text(encoding="utf-8")
    cards = list(parse_serp(html, "renta"))
    assert len(cards) >= 20, f"expected ~30 cards, got {len(cards)}"
    c = cards[0]
    assert c.listingId and c.url.startswith(BASE + "/propiedades/"), c.url
    assert c.propertyType, c
    assert c.coordinates and -120 < c.coordinates[1] < -80, c.coordinates
    assert any(x.price for x in cards), "no prices parsed on any card"
    # These four are what keeps this scraper SERP-only. If a JSON change empties
    # them the run silently starts shipping thinner rows at the same price.
    assert sum(bool(x.agentPhone) for x in cards) > len(cards) // 2, "SERP phones gone"
    assert sum(bool(x.description) for x in cards) > len(cards) // 2, "descriptions gone"
    assert sum(bool(x.listedAt) for x in cards) > len(cards) // 2, "modified_date gone"
    assert c.province, "PROVINCIA missing from the location chain — audit would break"
    assert c.listedAt.startswith("20") and c.listedAt.endswith("+00:00"), c.listedAt
    # URL builder: pagination and the delta window must compose, in that order.
    assert _search_url("colima-provincia", "terrenos", "venta", 2, 30) == (
        BASE + "/terrenos-en-venta-en-colima-provincia"
               "-publicado-hace-menos-de-1-mes-pagina-2.html")
    assert _load_done.__doc__  # keep the resume contract documented
    print(f"OK selfcheck: {len(cards)} cards, sample={c.propertyType!r} "
          f"{c.price} {c.currency} city={c.city!r} province={c.province!r} "
          f"phone={c.agentPhone} listedAt={c.listedAt[:10]} coords={c.coordinates}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/inmuebles24.jsonl")
    ap.add_argument("--states", nargs="*", default=list(STATES))
    ap.add_argument("--max-shards", type=int, default=400,
                    help="safety cap on price shards per query (120 listings each)")
    ap.add_argument("--min-gap", type=float, default=2.5,
                    help="polite floor between requests, seconds")
    ap.add_argument("--days", type=int, choices=sorted(WINDOWS),
                    help="delta run: only listings published within N days")
    ap.add_argument("--survey", action="store_true",
                    help="size the job and verify state slugs (~35 requests)")
    ap.add_argument("--audit", metavar="JSONL", nargs="?", const="data/inmuebles24.jsonl",
                    help="offline coverage audit of a scraped JSONL")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        _selfcheck()
    elif args.audit:
        audit(args.audit, args.states)
    elif args.survey:
        survey(args.states, SEARCHES, args.min_gap)
    else:
        crawl(args.states, SEARCHES, args.out, args.max_shards, args.days, args.min_gap)


if __name__ == "__main__":
    main()
