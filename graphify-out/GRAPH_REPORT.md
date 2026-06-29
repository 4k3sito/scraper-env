# Graph Report - .  (2026-06-22)

## Corpus Check
- 80 files · ~477,840 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 371 nodes · 477 edges · 43 communities (36 shown, 7 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 43 edges (avg confidence: 0.85)
- Token cost: 18,500 input · 2,800 output

## Community Hubs (Navigation)
- [[_COMMUNITY_ScraperAPI Product Concepts|ScraperAPI Product Concepts]]
- [[_COMMUNITY_PropiedadesMX Scraper|PropiedadesMX Scraper]]
- [[_COMMUNITY_Vivaanuncios Scraper|Vivaanuncios Scraper]]
- [[_COMMUNITY_Scraper-Builder Reference Guides|Scraper-Builder Reference Guides]]
- [[_COMMUNITY_Lamudi Scraper|Lamudi Scraper]]
- [[_COMMUNITY_MercadoLibre Scraper|MercadoLibre Scraper]]
- [[_COMMUNITY_Inmuebles24 Scraper|Inmuebles24 Scraper]]
- [[_COMMUNITY_Research Agent Script|Research Agent Script]]
- [[_COMMUNITY_Anti-Bot & SEO Concepts|Anti-Bot & SEO Concepts]]
- [[_COMMUNITY_Scraper Architecture Concepts|Scraper Architecture Concepts]]
- [[_COMMUNITY_Error Screenshots|Error Screenshots]]
- [[_COMMUNITY_OpenClaw Agent Identity|OpenClaw Agent Identity]]
- [[_COMMUNITY_Real Skill Config|Real Skill Config]]
- [[_COMMUNITY_Scraper-Builder Skill Config|Scraper-Builder Skill Config]]
- [[_COMMUNITY_Error Log HTML Pages|Error Log HTML Pages]]
- [[_COMMUNITY_Price Report Template|Price Report Template]]
- [[_COMMUNITY_scrape_links.json Config|scrape_links.json Config]]
- [[_COMMUNITY_Skill Lock onboarding|Skill Lock: onboarding]]
- [[_COMMUNITY_Skill Lock async|Skill Lock: async]]
- [[_COMMUNITY_Skill Lock CLI|Skill Lock: CLI]]
- [[_COMMUNITY_Skill Lock crawler|Skill Lock: crawler]]
- [[_COMMUNITY_Skill Lock datapipeline|Skill Lock: datapipeline]]
- [[_COMMUNITY_Skill Lock market-research|Skill Lock: market-research]]
- [[_COMMUNITY_Skill Lock MCP|Skill Lock: MCP]]
- [[_COMMUNITY_Skill Lock price-monitoring|Skill Lock: price-monitoring]]
- [[_COMMUNITY_Skill Lock python-sdk|Skill Lock: python-sdk]]
- [[_COMMUNITY_Skill Lock research-agent|Skill Lock: research-agent]]
- [[_COMMUNITY_Skill Lock scraper-builder|Skill Lock: scraper-builder]]
- [[_COMMUNITY_Skill Lock seo-audit|Skill Lock: seo-audit]]
- [[_COMMUNITY_Skill Lock serp-intelligence|Skill Lock: serp-intelligence]]
- [[_COMMUNITY_Misc Data 30|Misc Data 30]]
- [[_COMMUNITY_Misc Data 31|Misc Data 31]]
- [[_COMMUNITY_Misc Data 32|Misc Data 32]]
- [[_COMMUNITY_Misc Data 33|Misc Data 33]]
- [[_COMMUNITY_Misc Data 34|Misc Data 34]]
- [[_COMMUNITY_Misc Data 35|Misc Data 35]]
- [[_COMMUNITY_Misc Data 40|Misc Data 40]]
- [[_COMMUNITY_Misc Data 41|Misc Data 41]]

## God Nodes (most connected - your core abstractions)
1. `skills` - 14 edges
2. `ScraperAPI MCP Skill` - 14 edges
3. `main()` - 9 edges
4. `parse_detail()` - 9 edges
5. `parse_detail()` - 8 edges
6. `do_scrape_all()` - 8 edges
7. `collect_listing_urls()` - 8 edges
8. `collect_ad_urls()` - 8 edges
9. `Coordinate Extraction Waterfall` - 8 edges
10. `ScraperAPI SEO Audit Skill` - 8 edges

## Surprising Connections (you probably didn't know these)
- `Anti-Bot Detection / Challenge Pages` --conceptually_related_to--> `Browser New Tab Page (Chrome NTP)`  [INFERRED]
  .agents/skills/scraperapi-scraper-builder/SKILL.md → error_logs/2026-06-18_15-22-48/page.html
- `ScraperAPI Webhook Callbacks` --semantically_similar_to--> `ScraperAPI Crawler (site-wide link-following)`  [INFERRED] [semantically similar]
  .agents/skills/scraperapi-async/SKILL.md → .agents/skills/scraperapi-crawler/SKILL.md
- `Vivanuncios Real Estate Listing Page` --conceptually_related_to--> `Anti-Bot Detection / Challenge Pages`  [INFERRED]
  error_logs/2026-06-20_17-40-41/page.html → .agents/skills/scraperapi-scraper-builder/SKILL.md
- `Bright Data Browser API (Playwright/CDP)` --semantically_similar_to--> `ScraperAPI MCP Server`  [INFERRED] [semantically similar]
  .agents/skills/scraper-builder/SKILL.md → .agents/skills/scraperapi-agent-onboarding/SKILL.md
- `Semaphore-Controlled Concurrency Pattern` --semantically_similar_to--> `ScraperAPI Batch Jobs (up to 50k URLs)`  [INFERRED] [semantically similar]
  .agents/skills/scraper-builder/references/concurrency-guide.md → .agents/skills/scraperapi-async/SKILL.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **All Scrapers Implement Two-Phase Pattern** — scrapeenv_lamudi_scraper, scrapeenv_inmuebles24_scraper, scrapeenv_mercadolibre_scraper, scrapeenv_vivaanuncios_scraper, scrapeenv_propiedadesmx_scraper, scrapeenv_two_phase_pattern [EXTRACTED 1.00]
- **All Scrapers Use Coordinate Extraction with Mexican Bbox Validation** — scrapeenv_lamudi_scraper, scrapeenv_inmuebles24_scraper, scrapeenv_mercadolibre_scraper, scrapeenv_vivaanuncios_scraper, scrapeenv_propiedadesmx_scraper, scrapeenv_coordinate_extraction, scrapeenv_mexican_bbox [EXTRACTED 1.00]
- **All Scrapers Read from scrape_links.json Config** — scrapeenv_lamudi_scraper, scrapeenv_inmuebles24_scraper, scrapeenv_vivaanuncios_scraper, scrapeenv_propiedadesmx_scraper, scrapeenv_scrape_links_config [EXTRACTED 1.00]
- **Bright Data API Selection Ecosystem (Web Unlocker + Browser API + Web Scraper API + SERP)** — scraper_builder_skill_web_unlocker_api, scraper_builder_skill_browser_api, scraper_builder_skill_web_scraper_api, scraper_builder_skill_serp_api [EXTRACTED 1.00]
- **OpenClaw Agent Context (SOUL + USER + IDENTITY)** — real_soul_agent_persona, real_user_user_profile, real_identity_org_identity [INFERRED 0.85]
- **ScraperAPI Product Suite (Async + Crawler + CLI)** — scraperapi_async_skill_scraperapi_async_jobs, scraperapi_crawler_skill_scraperapi_crawler, scraperapi_cli_skill_sapi_cli [INFERRED 0.85]
- **ScraperAPI Skills Suite** — scraperapi_datapipeline_skill, scraperapi_market_research_skill, scraperapi_mcp_skill, scraperapi_price_monitoring_skill, scraperapi_python_sdk_skill, scraperapi_research_agent_skill [INFERRED 0.90]
- **ScraperAPI MCP Reference Documents** — scraperapi_mcp_ref_amazon, scraperapi_mcp_ref_crawler, scraperapi_mcp_ref_ebay, scraperapi_mcp_ref_google, scraperapi_mcp_ref_redfin, scraperapi_mcp_ref_scraping, scraperapi_mcp_ref_setup, scraperapi_mcp_ref_walmart [EXTRACTED 1.00]
- **ScraperAPI Structured Data Extraction Tools** — scraperapi_mcp_ref_amazon, scraperapi_mcp_ref_ebay, scraperapi_mcp_ref_redfin, scraperapi_mcp_ref_walmart, scraperapi_price_monitoring_structured_endpoints [INFERRED 0.85]
- **ScraperAPI Agent Skills Suite** — scraperapi_scraper_builder_skill, scraperapi_seo_audit_skill, scraperapi_serp_intelligence_skill [INFERRED 0.95]
- **SEO Audit Reference Documents** — scraperapi_seo_audit_skill, scraperapi_seo_audit_page_analysis, scraperapi_seo_audit_report_template, scraperapi_seo_audit_serp_playbook [EXTRACTED 1.00]
- **Browser New Tab Error Log Cluster (2026-06-18)** — error_log_15_22_48, error_log_15_24_46, error_log_15_24_48, error_log_15_24_49, error_log_15_24_52 [EXTRACTED 1.00]

## Communities (43 total, 7 thin omitted)

### Community 0 - "ScraperAPI Product Concepts"
Cohesion: 0.08
Nodes (31): Anthropic Files API Integration, Crawler Depth vs Budget Control, ScraperAPI Credit Cost Model, DataPipeline Scheduled Scraping, Webhook Output Delivery, Market Research Analysis Modules, MCP Server Variants (Remote vs Local), MCP Tool Selection Decision Tree (+23 more)

### Community 1 - "PropiedadesMX Scraper"
Cohesion: 0.10
Nodes (29): clean_html_text(), collect_all_images(), collect_listing_urls(), extract_gallery_images(), extract_listings(), human_delay(), listing_hrefs_from_html(), maps_link() (+21 more)

### Community 2 - "Vivaanuncios Scraper"
Cohesion: 0.12
Nodes (27): ads_from_results(), build_page_url(), capture_map_tiles(), center_from_tiles(), collect_ad_urls(), extract_ads(), extract_photos(), geocode_address() (+19 more)

### Community 3 - "Scraper-Builder Reference Guides"
Cohesion: 0.10
Nodes (27): Concurrency Guide for Scrapers, Semaphore-Controlled Concurrency Pattern, Pagination Patterns Guide, Hidden API Discovery (Next.js __NEXT_DATA__, XHR endpoints), JSON-LD Structured Data Extraction, CSS Selector Reliability Ranking, Site Analysis Guide, SSR vs CSR Content Rendering Detection (+19 more)

### Community 4 - "Lamudi Scraper"
Cohesion: 0.15
Nodes (19): _clean(), collect_urls(), _extract_coords(), fetch(), _fetch_detail(), _first(), _maps_link(), _page() (+11 more)

### Community 5 - "MercadoLibre Scraper"
Cohesion: 0.14
Nodes (20): ads_from_results(), build_page_url(), do_scrape_all(), extract_coordinates(), extract_photos(), human_delay(), jsonld_product(), parse_detail() (+12 more)

### Community 6 - "Inmuebles24 Scraper"
Cohesion: 0.20
Nodes (17): _clean(), collect_urls(), extractcoords(), _first(), _page(), parse_detail(), parse_listing_cards(), _photoid() (+9 more)

### Community 7 - "Research Agent Script"
Cohesion: 0.20
Nodes (17): Anthropic, cleanup_artifacts(), deduplicate(), main(), plan_queries(), Fetch a page as markdown via ScraperAPI. Returns None on failure., Remove duplicate URLs and cap at max_sources., Upload scraped content as a text artifact. Returns file_id or None. (+9 more)

### Community 8 - "Anti-Bot & SEO Concepts"
Cohesion: 0.21
Nodes (17): Google AI Overview Detection, Anti-Bot Detection / Challenge Pages, Async Batch Scraping (ScraperAPI batchjobs), JavaScript Rendering (render=true), ScraperAPI Proxy Service, SEO Audit Methodology, SERP Analysis and Keyword Visibility, ScraperAPI Structured Data Endpoint (+9 more)

### Community 9 - "Scraper Architecture Concepts"
Cohesion: 0.29
Nodes (14): Botasaurus Browser Driver, Coordinate Extraction Waterfall, Google Maps Tile URL Interception, Inmuebles24 Scraper, Lamudi Scraper, MercadoLibre Scraper, Mexican Bounding Box Validation, MercadoLibre Browser Session Profile (+6 more)

### Community 10 - "Error Screenshots"
Cohesion: 0.31
Nodes (10): Inmuebles24 Search Results Screenshot (14:45:48), Inmuebles24 Search Results Screenshot (15:15:53), Inmuebles24 Search Results Screenshot (15:19:50), Google Homepage Screenshot - Browser Failed to Navigate (15:22:48), Google Homepage Screenshot - Browser Failed to Navigate (15:24:46), Google Homepage Screenshot - Browser Failed to Navigate (15:24:48), Google Homepage Screenshot - Browser Failed to Navigate (15:24:49), Google Homepage Screenshot - Browser Failed to Navigate (15:24:52) (+2 more)

### Community 11 - "OpenClaw Agent Identity"
Cohesion: 0.25
Nodes (9): Agent Boundaries, Agent Roles (planner/executor/reviewer), openclaw doctor (config validation command), Workspace Bootstrap Procedure, OpenClaw Platform, Agent Organization Identity, Project-Scoped Agent Memory, Agent Persona (SOUL) (+1 more)

### Community 12 - "Real Skill Config"
Cohesion: 0.22
Nodes (8): branch, name, owner, path, repo, sha, source, version

### Community 13 - "Scraper-Builder Skill Config"
Cohesion: 0.22
Nodes (8): branch, name, owner, path, repo, sha, source, version

### Community 14 - "Error Log HTML Pages"
Cohesion: 0.25
Nodes (8): Browser New Tab Page (Chrome NTP), Error Log 2026-06-18 14:45:48 (Browser New Tab Page), Error Log 2026-06-18 15:15:53 (Browser New Tab Page), Error Log 2026-06-18 15:22:48 (Browser New Tab Page), Error Log 2026-06-18 15:24:46 (Browser New Tab Page), Error Log 2026-06-18 15:24:48 (Browser New Tab Page), Error Log 2026-06-18 15:24:49 (Browser New Tab Page), Error Log 2026-06-18 15:24:52 (Browser New Tab Page)

### Community 15 - "Price Report Template"
Cohesion: 0.29
Nodes (6): _meta, default_country, description, last_updated, schema_version, products

### Community 16 - "scrape_links.json Config"
Cohesion: 0.40
Nodes (4): inmuebles24, lamudi, propiedadesmx, vivaanuncios

### Community 17 - "Skill Lock: onboarding"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-agent-onboarding

### Community 18 - "Skill Lock: async"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-async

### Community 19 - "Skill Lock: CLI"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-cli

### Community 20 - "Skill Lock: crawler"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-crawler

### Community 21 - "Skill Lock: datapipeline"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-datapipeline

### Community 22 - "Skill Lock: market-research"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-market-research

### Community 23 - "Skill Lock: MCP"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-mcp

### Community 24 - "Skill Lock: price-monitoring"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-price-monitoring

### Community 25 - "Skill Lock: python-sdk"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-python-sdk

### Community 26 - "Skill Lock: research-agent"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-research-agent

### Community 27 - "Skill Lock: scraper-builder"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-scraper-builder

### Community 28 - "Skill Lock: seo-audit"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-seo-audit

### Community 29 - "Skill Lock: serp-intelligence"
Cohesion: 0.40
Nodes (5): computedHash, skillPath, source, sourceType, scraperapi-serp-intelligence

### Community 32 - "Misc Data 32"
Cohesion: 1.00
Nodes (3): Agent Tools (gh/curl/rg), GitHub Skill (gh CLI wrapper), Agent Tool Guidance & Allow/Deny List

## Knowledge Gaps
- **118 isolated node(s):** `version`, `name`, `owner`, `repo`, `path` (+113 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `skills` connect `Misc Data 33` to `Skill Lock: onboarding`, `Skill Lock: async`, `Skill Lock: CLI`, `Skill Lock: crawler`, `Skill Lock: datapipeline`, `Skill Lock: market-research`, `Skill Lock: MCP`, `Skill Lock: price-monitoring`, `Skill Lock: python-sdk`, `Skill Lock: research-agent`, `Skill Lock: scraper-builder`, `Skill Lock: seo-audit`, `Skill Lock: serp-intelligence`?**
  _High betweenness centrality (0.030) - this node is a cross-community bridge._
- **Why does `scraperapi-agent-onboarding` connect `Skill Lock: onboarding` to `Misc Data 33`?**
  _High betweenness centrality (0.004) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `ScraperAPI MCP Skill` (e.g. with `ScraperAPI Market Research Skill` and `ScraperAPI Python SDK Skill`) actually correct?**
  _`ScraperAPI MCP Skill` has 3 INFERRED edges - model-reasoned connections that need verification._
- **What connects `version`, `name`, `owner` to the rest of the system?**
  _173 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `ScraperAPI Product Concepts` be split into smaller, more focused modules?**
  _Cohesion score 0.08387096774193549 - nodes in this community are weakly interconnected._
- **Should `PropiedadesMX Scraper` be split into smaller, more focused modules?**
  _Cohesion score 0.10114942528735632 - nodes in this community are weakly interconnected._
- **Should `Vivaanuncios Scraper` be split into smaller, more focused modules?**
  _Cohesion score 0.11904761904761904 - nodes in this community are weakly interconnected._