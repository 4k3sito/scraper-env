# Real-estate scrapers — México

Five nationwide scrapers at the repo root: `lamudi_scraper.py`,
`inmuebles24_scraper.py`, `viva_scraper.py`, `mercadolibre_scraper.py` and
`pincali_scraper.py`. They share `stealth_scraper.py` (curl_cffi/camoufox
transport), `scrape_utils.py` (logging + Ctrl-C-safe run guard) and — for the two
Navent-built portals, Inmuebles24 and Vivanuncios — `navent_serp.py`, which holds
the entire SERP data layer because those two sites ship the identical
`preloadedData` blob (Mercado Libre and Pincali reuse its `Listing`/wire meter).

All five share the same flows: `--survey` (size the job + verify shard slugs),
`--selfcheck` (offline fixture parse), `--audit` (offline coverage check), resume
via `<out>.done`, and wire-byte metering. Read
**[SCRAPING_PLAYBOOK.md](SCRAPING_PLAYBOOK.md) §11** before writing a sixth one;
it is the distilled result of all five runs.

```bash
.venv/bin/python inmuebles24_scraper.py --survey        # before the crawl
.venv/bin/python inmuebles24_scraper.py --out data/inmuebles24.jsonl
.venv/bin/python inmuebles24_scraper.py --audit         # after the crawl

.venv/bin/python viva_scraper.py --survey
.venv/bin/python viva_scraper.py --out data/vivanuncios.jsonl

.venv/bin/python mercadolibre_scraper.py --survey
.venv/bin/python mercadolibre_scraper.py --out data/mercadolibre.jsonl

.venv/bin/python pincali_scraper.py --survey
.venv/bin/python pincali_scraper.py --out data/pincali.jsonl
.venv/bin/python pincali_scraper.py --status      # health of a run in flight
```

Pincali adds a sixth flow the others lack: `--status`, a one-shot health read (no
network, no browser) whose **exit code** is the contract — 0 healthy, 1 needs a
look, 2 dead with work left — so `until … ; do sleep 300; done` waits on a
condition instead of spinning. It flags a stalled log, a WAF **mint storm**, and
queries that aborted mid-sweep.

What differs per site is the *sharding strategy*, never the parser: i24 caps at 4
pages (hard 403) so it shards by price keyset; Vivanuncios shards from its own
sitemap tree and takes `--page-cap` for how deep to paginate; Mercado Libre
Disallows pagination outright and Allows `_PriceRange_`, so it partitions the
price axis and never asks for page 2 (`--paginate` overrides, off by default);
Pincali caps at page 100 and walks past it by price keyset.

**Pincali is the only one that needs a browser** — AWS WAF challenges every path
but `/`, and only *headful* Chrome (patchright under Xvfb, ~5 s) gets an
`aws-waf-token`. That token then feeds curl_cffi for exactly 300 s per mint, so
the browser costs ~2% of the run and every listing still arrives over plain HTTP.
The script re-execs itself under `xvfb-run` when `DISPLAY` is unset. It is also
the only SERP in the repo that carries **coordinates per card**.

Use `.venv/`, not `env/` (stale, missing deps).

Make sure your use of each site and any proxy service is authorized and complies
with applicable terms, robots directives, and local law.
