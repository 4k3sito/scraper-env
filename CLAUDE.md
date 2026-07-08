# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project structure

```
scrape-env/
├── src/                     # all Python source
│   ├── db.py                # Supabase upsert/sync — called by every scraper
│   ├── parser.py            # regex-based description parser (size, rooms, amenities, price)
│   ├── utils.py              # retry, checkpoint, atomic write, graceful shutdown, dual logging
│   ├── geocode.py            # backfills missing lat/lon in the DB via Mapbox Geocoding v6
│   ├── lamudi.py             # Lamudi scraper
│   ├── inmuebles24.py        # Inmuebles24 scraper
│   ├── vivaanuncios.py       # Vivanuncios scraper
│   ├── mercadolibre.py       # MercadoLibre scraper (needs login session)
│   ├── propiedadesmx.py      # PropiedadesMexico scraper
│   ├── century21.py          # Century21 scraper
│   └── pincali.py            # Pincali (EasyBroker) scraper
├── output/                  # all generated JSON/CSV output + per-scraper checkpoint/log files
├── profiles/ml_session/     # persistent MercadoLibre browser session
├── scrape_links.json        # config: site keys → search URLs
├── .env                     # DATABASE_URL, MAPBOX_API_KEY
└── .mcp.json                # Supabase MCP server config
```

## Running scrapers

All scrapers run from the project root using `python -m`:

```bash
python -m src.lamudi              # headless Botasaurus browser
python -m src.inmuebles24         # headless Botasaurus browser
python -m src.mercadolibre        # requires prior login session (see below)
python -m src.vivaanuncios        # headful browser (headless=False for anti-bot)
python -m src.propiedadesmx       # headful browser + Next.js SSR extraction
python -m src.century21           # headless Botasaurus browser, Vue-rendered site
python -m src.pincali              # headless Botasaurus browser
```

**MercadoLibre first-time setup** — run `python -m src.mercadolibre login` once to open a visible browser, log in manually, and save the session to `profiles/ml_session/`. Subsequent runs reuse it automatically.

**Self-tests** (validate coordinate extraction on a single URL):
```bash
python -m src.inmuebles24 --selftest <URL>
python -m src.vivaanuncios --selftest <URL>
python -m src.pincali --selftest <URL>
```

**Sample runs** (1 search page, N listings — for quick testing):
```bash
python -m src.inmuebles24 --sample 5
python -m src.vivaanuncios --sample 5
```

**Geocoding backfill** (fills missing lat/lon for rows already in the DB):
```bash
python -m src.geocode                   # geocode all missing records
python -m src.geocode --dry-run         # count + preview only, no writes
python -m src.geocode --source lamudi   # geocode a specific source only
python -m src.geocode --limit 10        # cap the number of records
```

## Configuration

`scrape_links.json` is the sole config file. It maps site keys to search-result page URLs (MercadoLibre is the exception — its URL lives in the `SEARCH_URL` constant in `mercadolibre.py`). Add or change search URLs here; each scraper reads its own key on startup.

## Architecture

Every scraper follows the same two-phase pattern:

- **Phase 1** — paginate search results, collect unique listing URLs (deduped by ID)
- **Phase 2** — fetch each detail page and extract structured fields, then upsert to the DB

Output is written to `output/` as JSON (plus CSV for Lamudi and Inmuebles24). Every scraper uses **Botasaurus** (browser automation) for transport and **Scrapling** (adaptive HTML parser) for extraction.

| Site | Browser mode | Notes |
|------|-----------|-------|
| Lamudi | `headless=True` | Coords from JSON-LD + inline JS |
| Inmuebles24 | `headless=True` | Needs a wait for JS hydration before reading coords — they live in an inline `postingGeolocation`/`geolocation` object, not JSON-LD |
| Vivanuncios | `headless=True` | Text extraction from `<article>` element |
| MercadoLibre | `headless=True`, `profile="ml_session"` | Persistent login session required — see setup above |
| PropiedadesMX | `headless=False` | Primary data from `<script id="__NEXT_DATA__">`; gallery images taken from the rendered DOM because the SSR `images` array is sometimes incomplete |
| Century21 | `headless=True` | Vue-rendered; SSR HTML is unreliable, so extraction combines JSON-LD with DOM fallback |
| Pincali | `headless=True` | Multiple coordinate fallbacks |

### Shared modules

- **`src/utils.py`** — reusable, stdlib-only helpers every scraper imports: `retry_extract`/`retry_fetch` (exponential backoff), `Checkpoint` (resume Phase 2 after a crash via `output/<site>_checkpoint.json`), `atomic_write_json` (write-tmp-then-rename), `setup_graceful_shutdown`/`should_stop` (SIGINT/SIGTERM-safe partial saves), `setup_log` (dual file+stdout logger).
- **`src/parser.py`** — `parse_description()` runs regex extraction over Spanish real-estate description text (size, bedrooms, bathrooms, parking, floor, amenities, condition). `normalize()` maps each site's raw record shape into the canonical DB column set; `merge_parsed()` combines a scraper's structured fields with parser output, preferring the more reliable source per field. No LLM backend is currently wired in despite the module docstring — extraction is regex-only.
- **`src/db.py`** — `sync(source, phase1_urls)` reconciles a fresh Phase-1 URL list against existing DB rows (drives incremental re-scraping). `upsert(source, records)` maps records through the per-site `MAPPERS` dict into `COLS` and bulk-upserts into `public.listings`, keyed on `UNIQUE (source, external_id)`. Requires `DATABASE_URL` in `.env`; if unset, `upsert` is a no-op. JSON output in `output/` is always written first and is the source of truth — DB errors never lose data (callers wrap `db` calls in try/except).

### Environment variables

- `DATABASE_URL` — Supabase Postgres connection string (must be the IPv4 pooler URI, `aws-*-*.pooler.supabase.com:6543`, not the direct connection — WSL2 has no IPv6 route to the direct host).
- `MAPBOX_API_KEY` — used only by `src/geocode.py` for forward geocoding.
