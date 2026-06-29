# CLAUDE.md

This file provides guidance when working with code in this repository.

## Project structure

```
scrape-env/
├── src/                     # all Python source
│   ├── db.py                # Supabase DB upsert (called by all scrapers)
│   ├── lamudi.py            # Lamudi scraper
│   ├── inmuebles24.py       # Inmuebles24 scraper
│   ├── vivaanuncios.py      # Vivanuncios scraper
│   ├── mercadolibre.py      # MercadoLibre scraper (needs login session)
│   ├── propiedadesmx.py     # PropiedadesMexico scraper
│   ├── century21.py         # Century21 scraper
│   └── pincali.py           # Pincali (EasyBroker) scraper
├── output/                  # all generated JSON/CSV output
├── profiles/ml_session/     # persistent MercadoLibre browser session
├── scrape_links.json        # config: site keys → search URLs
├── .env                     # SCRAPERAPI_KEY, DATABASE_URL, etc.
└── .mcp.json                # Supabase MCP server config
```

## Running scrapers

All scrapers run from the project root directory using `python -m`:

```bash
python -m src.lamudi              # headless Botasaurus browser
python -m src.inmuebles24          # headless Botasaurus browser
python -m src.mercadolibre         # requires prior login session (see below)
python -m src.vivaanuncios         # headful browser (headless=False for anti-bot)
python -m src.propiedadesmx        # headful browser + Next.js SSR extraction
python -m src.century21            # ScraperAPI proxy to JSON endpoint
python -m src.pincali              # headless Botasaurus browser
```

**MercadoLibre first-time setup** — run `python -m src.mercadolibre login` once to open a visible browser, log in manually, and save the session to the `profiles/ml_session/` directory. Subsequent runs reuse it automatically.

**Self-tests** (validate coordinate extraction on a single URL):
```bash
python -m src.inmuebles24 --selftest <URL>
python -m src.vivaanuncios --selftest <URL>
python -m src.pincali --selftest <URL>
```

**Sample runs** (1 page, N listings — for quick testing):
```bash
python -m src.inmuebles24 --sample 5
python -m src.vivaanuncios --sample 5
```

## Configuration

`scrape_links.json` is the sole config file. It maps site keys to search-result page URLs:

```json
{
  "lamudi": [...],
  "inmuebles24": [...],
  "vivaanuncios": [...],
  "propiedadesmx": [...],
  "mercadolibre": [...]   // added directly in mercadolibre.py SEARCH_URL constant
}
```

Add or change search URLs here — each scraper reads its own key on startup.

## Architecture

Every scraper follows the same two-phase pattern:

- **Phase 1** — paginate search results, collect unique listing URLs (deduped by ID)
- **Phase 2** — fetch each detail page and extract structured fields

Output is written to `output/` directory as JSON (and CSV for Lamudi and Inmuebles24).

### Frameworks

Every scraper uses **Botasaurus** (browser automation) for transport and **Scrapling** (adaptive HTML parser) for data extraction.

| Site | Botasaurus | Scrapling | Notes |
|------|-----------|-----------|-------|
| Lamudi | `headless=True` | ✓ | Coords from JSON-LD + inline JS |
| Inmuebles24 | `headless=True` | ✓ | Needs wait for JS hydration before reading coords |
| Vivanuncios | `headless=True` | ✓ | Text extraction from `<article>` element |
| MercadoLibre | `headless=True`, `profile="ml_session"` | ✓ | Persistent login session required |
| PropiedadesMX | `headless=False` | ✓ | Primary data from `<script id="__NEXT_DATA__">` |
| Century21 | `headless=True` | ✓ | JSON-LD + DOM fallback for Vue-rendered pages |
| Pincali | `headless=True` | ✓ | Multiple coordinate fallbacks |

### Dependencies

`botasaurus`, `scrapling`, `tqdm`, `psycopg2-binary`, `python-dotenv` — all installed in `.venv/`.

## DB integration

`src/db.py` provides `sync()` and `upsert()` to reconcile and push scraped data to Supabase Postgres (`public.listings`). Requires `DATABASE_URL` in `.env`. JSON output in `output/` is always the primary backup — DB errors never lose data.
