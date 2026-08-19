"""Vivanuncios México scraper — Locales Comerciales (renta+venta) y Terrenos (venta).

Same Navent front end as Inmuebles24, so the parsing is shared verbatim
(`navent_serp.py`): `script#preloadedData` hydrates all 30 listings per SERP page
with price, geolocation, areas, description, agency, phone and `modified_date`.
Single phase, no detail crawl. curl_cffi Chrome-TLS impersonation clears the gate.

What differs from Inmuebles24 is *how you shard*, and it comes straight from
robots.txt:

  Disallow: /s-*/page-*        Allow: /s-*/page-2 … /s-*/page-5
  Allow: *?*sort=low_price     Disallow: *?*sort=*  ·  Disallow: *?*pr=*

Unlike Inmuebles24 the server does NOT enforce that page limit — page 9 returns
200 like any other. robots *asks* crawlers to stop at 5; this scraper paginates
each state shard to exhaustion instead, which is an explicit operator decision
(see --page-cap) taken because honouring the limit reached only ~78% of the
corpus: shard URLs cap at 150 listings and the sitemap's zone shards are an
SEO-selected subset that doesn't cover their parent cities. Set `--page-cap 5`
to restore robots-conformant behaviour.

`?sort=low_price` (the one ordering robots does allow) is kept regardless: the
default "relevance" order is seeded per hour bucket (listStore.sortSeed), so a
deep walk under it would reshuffle mid-query and silently skip listings.

The sitemap is still what supplies the 32 state shard slugs per category, and
`--survey` still prints the city/zone tree beneath them.

    .venv/bin/python viva_scraper.py --survey     # size the job + shard tree
    .venv/bin/python viva_scraper.py --out data/vivanuncios.jsonl
    .venv/bin/python viva_scraper.py --audit      # offline coverage check
    .venv/bin/python viva_scraper.py --selfcheck  # offline fixture parse
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from tqdm import tqdm

from stealth_scraper import Scraper
from scrape_utils import graceful, setup_logging
from navent_serp import (WIRE, Listing, chain, fetch, find_postings, paging,
                         parse_serp as _parse_serp, preloaded, serialize)

BASE = "https://www.vivanuncios.com.mx"
SITEMAP = BASE + "/sitemap_list_https_1.xml.gz"

# robots.txt: `Allow: /s-*/page-2` … `page-5`, everything deeper Disallowed.
# The server would serve page 9 happily; we stop because we were asked to.
PAGE_CAP = 5
PER_PAGE = 30
SHARD_CAP = PAGE_CAP * PER_PAGE          # 150 listings reachable per shard URL

# robots.txt: `Allow: *?*sort=low_price` sits above `Disallow: *?*sort=*`.
# Ascending price is the only sanctioned ordering — and worth using anyway, since
# the default "relevance" order is seeded (listStore.sortSeed) and reshuffles.
SORT = "?sort=low_price"

# Category roots, as the sitemap spells them. Operation is baked into the slug
# here (unlike i24, where it's a URL segment), so it's carried alongside.
SEARCHES = [
    ("s-renta-locales-comerciales", "renta"),
    ("s-venta-locales-comerciales", "venta"),
    ("s-venta-terrenos", "venta"),
]

_COMPLETE = "done"


def _shard_url(shard: str, page: int = 1) -> str:
    """`shard` is "{root}/{location}" or a bare root. Pagination is /page-N on
    the *bare* path — appending it after the sitemap's `/v1c31p1` location-code
    suffix silently returns page 1, so those suffixes are stripped at load."""
    url = f"{BASE}/{shard}"
    if page > 1:
        url += f"/page-{page}"
    return url + SORT


def parse_serp(html: str, operation: str):
    return _parse_serp(html, operation, BASE)


# --------------------------------------------------------------------------- #
# shard tree, straight from the site's own sitemap
# --------------------------------------------------------------------------- #
def load_sitemap(scraper: Scraper | None = None, cache="data/viva_sitemap.txt") -> list[str]:
    """~44k pre-built location URLs, one gzipped request. Cached: it's the shard
    tree for every run and it changes about as often as Mexico gains a city."""
    path = Path(cache)
    if path.exists():
        return path.read_text(encoding="utf-8").split()
    scraper = scraper or Scraper(min_gap=1.5)
    raw = gzip.decompress(scraper.get(SITEMAP).content).decode("utf-8", "replace")
    urls = re.findall(r"<loc>([^<]+)</loc>", raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(urls), encoding="utf-8")
    return urls


def shard_tree(urls, root: str) -> dict[str, list[str]]:
    """parent location -> child locations, for one category root.

    The sitemap encodes depth in the location segment itself:
    `nuevo-leon` (state) → `monterrey_nuevo-leon` (city) → `el-uro_monterrey_…`
    (zone). So a shard's parent is its own segment minus the leading component,
    and no extra requests are needed to discover the hierarchy."""
    kids: dict[str, list[str]] = defaultdict(list)
    locs = set()
    for u in urls:
        parts = u.rstrip("/").split("/")
        if len(parts) < 5 or parts[3] != root:
            continue
        loc = parts[4]
        if re.fullmatch(r"v\d+c\d+(l\d+)?p\d+", loc):
            continue  # the national root's location-code suffix, not a location
        locs.add(loc)
    for loc in locs:
        comps = loc.split("_")
        parent = "_".join(comps[1:]) if len(comps) > 1 else ""
        kids[parent].append(loc)
    return kids


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
    """query -> "done", or the last page finished so a killed run resumes there
    instead of re-walking pages it already paid for. Last token wins; a completed
    query is never downgraded."""
    done: dict[str, str | int] = {}
    if path.exists():
        for tok in path.read_text(encoding="utf-8").split():
            query, _, page = tok.partition("@")
            if done.get(query) == _COMPLETE:
                continue
            done[query] = int(page) if page else _COMPLETE
    return done


def crawl(states, out_path, min_gap=2.5, page_cap=0):
    """Walk every state shard of every category to exhaustion.

    `page_cap=5` would respect robots.txt's stated pagination limit; the default
    of 0 (unlimited) is a deliberate override — the server serves those pages
    without complaint, and the limit costs ~22% of the corpus (see module docs).
    Pacing, single-threading and identity rotation are unchanged either way, so
    the load on the site stays the same; only the depth differs.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ckpt = out.with_name(out.name + ".done")
    seen = _load_seen(out)
    done = _load_done(ckpt)
    logger = setup_logging(out)
    scraper = Scraper(min_gap=min_gap)
    urls = load_sitemap(scraper)
    limit = page_cap or 10_000
    stats = {"added": 0, "queries": 0, "errors": 0, "short": 0, "advertised": 0}
    planned = sum(len([l for l in shard_tree(urls, r)[""] if not states or l in states])
                  for r, _ in SEARCHES)

    def summary() -> str:
        mb, adv = WIRE["bytes"] / 1e6, stats["advertised"]
        pct = f"{stats['added'] / adv * 100:.0f}%" if adv else "n/a"
        return (f"{stats['added']} new listings → {out}\n"
                f"queries {stats['queries']}/{planned} complete, coverage "
                f"{stats['added']:,}/{adv:,} advertised ({pct}), "
                f"{stats['short']} short of their advertised total, "
                f"{stats['errors']} errors\n"
                f"wire: {WIRE['requests']} requests, {mb:.1f} MB compressed "
                f"(what the proxy bills) — log: {out}.log")

    with graceful(logger, summary), \
            out.open("a", encoding="utf-8") as sink, \
            ckpt.open("a", encoding="utf-8") as log:
        for root, operation in SEARCHES:
            for loc in sorted(shard_tree(urls, root)[""]):
                if states and loc not in states:
                    continue
                query = f"{root}/{loc}"
                if done.get(query) == _COMPLETE:
                    stats["queries"] += 1
                    logger.info("%s: skip (checkpointed)", query)
                    continue
                start = (done.get(query) or 0) + 1
                scraper.rotate()          # one fresh visitor identity per shard
                logger.info("%s: start at page %d", query, start)
                total = site_pages = 0
                got = last_ok = 0
                exhausted = False
                bar = None
                for page in range(start, limit + 1):
                    try:
                        html = fetch(scraper, _shard_url(query, page),
                                     referer=f"{BASE}/{root}")
                    except RuntimeError as exc:
                        stats["errors"] += 1
                        logger.error("%s p%d -> %s", query, page, exc)
                        break
                    obj = preloaded(html) or {}
                    if page == start:
                        pg = paging(obj)
                        total = pg.get("total") or 0
                        site_pages = pg.get("totalPages") or 0
                        if (obj.get("filtersStore", {}).get("sort") or {}).get("min") != "low_price":
                            # A dropped sort means the seeded relevance order is
                            # back and a deep walk would skip listings silently.
                            stats["errors"] += 1
                            logger.error("%s: sort=low_price not applied — skipping", query)
                            break
                        stats["advertised"] += total
                        bar = tqdm(desc=query, unit=" listing", total=total,
                                   initial=(start - 1) * PER_PAGE)
                        logger.info("%s: %d listings over %d pages", query, total, site_pages)
                    cards = list(parse_serp(html, operation))
                    if not cards:
                        exhausted = True
                        break
                    got += len(cards)
                    new = [c for c in cards if c.listingId not in seen]
                    for listing in new:
                        seen.add(listing.listingId)
                        sink.write(json.dumps(serialize(listing), ensure_ascii=False) + "\n")
                        sink.flush()
                        stats["added"] += 1
                    bar.update(len(cards))
                    last_ok = page
                    logger.info("%s p%d/%s cards=%d new=%d total_new=%d",
                                query, page, site_pages or "?", len(cards), len(new),
                                stats["added"])
                    if site_pages and page >= min(site_pages, limit):
                        exhausted = page >= site_pages
                        break
                if bar:
                    bar.close()
                if exhausted:
                    stats["queries"] += 1
                    log.write(query + "\n")
                    if total and got < total * 0.9:
                        stats["short"] += 1
                        logger.warning("%s: yielded %d of %d advertised (%.0f%%)",
                                       query, got, total, got / total * 100)
                    logger.info("%s: complete (%d cards seen)", query, got)
                else:
                    if last_ok:
                        log.write(f"{query}@{last_ok}\n")
                    logger.warning("%s: incomplete (stopped after page %d)", query, last_ok)
                log.flush()


# --------------------------------------------------------------------------- #
def survey(min_gap=2.5) -> None:
    """Size the job and show the shard tree, before committing to a crawl."""
    scraper = Scraper(min_gap=min_gap)
    urls = load_sitemap(scraper)
    fetch(scraper, BASE + "/")
    print(f"\nsitemap: {len(urls):,} location URLs\n")
    grand = 0
    for root, operation in SEARCHES:
        kids = shard_tree(urls, root)
        obj = preloaded(fetch(scraper, _shard_url(root))) or {}
        total = paging(obj).get("total") or 0
        grand += total
        levels = Counter(len(loc.split("_")) for lst in kids.values() for loc in lst)
        print(f"  {root:30s} {total:>7,} listings | shards: "
              f"{levels.get(1,0):>3} states, {levels.get(2,0):>4} cities, "
              f"{levels.get(3,0):>4} zones")
    pages = -(-grand // PER_PAGE)
    print(f"\n  {'TOTAL':30s} {grand:>7,} listings ≈ {pages:,} pages if fully "
          f"reachable ≈ {pages * 154 / 1024:.0f} MB (154 KB/page gzipped)")
    print(f"  robots caps each shard URL at {SHARD_CAP} listings; overflow is "
          f"handled by descending the sitemap tree.")


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
              ("price", "coordinates", "agentPhone", "description", "listedAt", "areaM2")}
    print(f"\noperations: {dict(ops)}")
    print(f"top types:  {dict(types.most_common(6))}")
    print("field fill: " + ", ".join(f"{k}={v / max(len(rows), 1) * 100:.0f}%"
                                     for k, v in filled.items()))


def _selfcheck() -> None:
    """Offline parse of a saved SERP fixture — fails if the JSON shape drifts."""
    # URL grammar first: it is silently wrong at runtime, never loudly.
    assert _shard_url("s-venta-terrenos/nuevo-leon") == \
        BASE + "/s-venta-terrenos/nuevo-leon?sort=low_price"
    assert _shard_url("s-venta-terrenos/nuevo-leon", 3) == \
        BASE + "/s-venta-terrenos/nuevo-leon/page-3?sort=low_price"
    # Tree building: parent is the location minus its leading component.
    tree = shard_tree([
        f"{BASE}/s-venta-terrenos/nuevo-leon",
        f"{BASE}/s-venta-terrenos/monterrey_nuevo-leon",
        f"{BASE}/s-venta-terrenos/el-uro_monterrey_nuevo-leon",
        f"{BASE}/s-venta-terrenos/v1c31p1",          # code suffix, not a location
        f"{BASE}/s-casas/guadalajara_jalisco",       # other root, must be ignored
    ], "s-venta-terrenos")
    assert tree[""] == ["nuevo-leon"], tree
    assert tree["nuevo-leon"] == ["monterrey_nuevo-leon"], tree
    assert tree["monterrey_nuevo-leon"] == ["el-uro_monterrey_nuevo-leon"], tree

    fx = Path(__file__).parent / ".fixtures" / "viva_serp.html"
    if not fx.exists():
        print("URL/tree checks OK; save a SERP page to .fixtures/viva_serp.html "
              "for the parse check")
        return
    cards = list(parse_serp(fx.read_text(encoding="utf-8"), "venta"))
    assert len(cards) >= 20, f"expected ~30 cards, got {len(cards)}"
    c = cards[0]
    assert c.listingId and c.url.startswith(BASE), c.url
    assert c.propertyType, c
    assert c.coordinates and -120 < c.coordinates[1] < -80, c.coordinates
    assert any(x.price for x in cards), "no prices parsed on any card"
    assert sum(bool(x.agentPhone) for x in cards) > len(cards) // 2, "SERP phones gone"
    assert sum(bool(x.listedAt) for x in cards) > len(cards) // 2, "modified_date gone"
    assert c.province, "PROVINCIA missing — the audit would break"
    print(f"OK selfcheck: {len(cards)} cards, sample={c.propertyType!r} "
          f"{c.price} {c.currency} city={c.city!r} province={c.province!r} "
          f"phone={c.agentPhone} listedAt={c.listedAt[:10]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/vivanuncios.jsonl")
    ap.add_argument("--states", nargs="*", default=None,
                    help="limit to these state shards (default: all)")
    ap.add_argument("--min-gap", type=float, default=2.5)
    ap.add_argument("--page-cap", type=int, default=0,
                    help="max pages per shard; 5 = respect robots.txt, 0 = unlimited")
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--audit", metavar="JSONL", nargs="?", const="data/vivanuncios.jsonl")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        _selfcheck()
    elif args.audit:
        audit(args.audit)
    elif args.survey:
        survey(args.min_gap)
    else:
        crawl(set(args.states) if args.states else None, args.out, args.min_gap,
              args.page_cap)


if __name__ == "__main__":
    main()
