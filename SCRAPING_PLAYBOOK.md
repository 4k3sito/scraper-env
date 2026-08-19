# Scraping Playbook — anti-bot bypass, speed & proxy cost

Everything that actually worked on Lamudi MX (and generalizes). Written from a
real run: ~3.1k listings, zero paid CAPTCHA services, zero browsers, single IP.
Later extended with a byte-level cost audit of the nationwide run (§6), which
found ~95% of the proxy bill was buying data the SERP already shipped. Then
re-validated end to end on two more targets — Inmuebles24 MX, which is where the
"flows a nationwide run needs" checklist (§11) comes from, and Vivanuncios MX,
which proved the flows port to a new site in an afternoon when the platform is
shared (§4b).

Reference implementations in this repo: `stealth_scraper.py` (transport tier),
`scrape_utils.py` (logging + interrupt-safe run guard), `navent_serp.py` (the
SERP data layer shared by both Navent portals), `lamudi_scraper.py`
(target-specific parsing + challenge solver), `inmuebles24_scraper.py`
(price-keyset sharding under a hard page cap), `viva_scraper.py`
(sitemap-tree sharding), `mercadolibre_scraper.py` (recursive price-range
partitioning where robots forbids paging and sorting outright).

Four nationwide runs, all finished, all audited:

| target | listings | requests | wire | sharding strategy |
|---|---|---|---|---|
| Lamudi | 78k (est.) | — | 4.3 GB | state slugs + detail phase (94.6% of the bill) |
| Inmuebles24 | **69,536** | 2,508 | **439 MB** | price keyset under a hard 4-page 403 |
| Vivanuncios | **68,294** | 2,385 | **367 MB** | sitemap shard tree, depth per `--page-cap` |
| Mercado Libre | **57,921** | 2,963 | **688 MB** | recursive `_PriceRange_` partition, page 1 only |

Mercado Libre is the expensive one per row (11.9 KB vs ~6.3 KB) and the reason is
structural, not transport: with pagination Disallowed, every request is page 1 of
a narrower query, so the partition's internal nodes re-serve rows their children
will serve again. 19.5 rows/request against a 48-row page.

---

## The short version

If you only read one section, read **§11**: the six flows (`--survey`,
`--selfcheck`, resume, delta, byte metering, `--audit`) that turn "a scraper that
works on one page" into "a nationwide run that finishes and that you can trust."
Lamudi's first nationwide attempt reported 86/96 queries complete and was wrong
about two states; Inmuebles24's first attempt died at 33/96. Both failures were
missing flows, not missing evasion.

---

## 0. The one rule

**Never hand-roll fingerprint spoofing.** Canvas, WebGL, audio, fonts, WebRTC,
TLS/JA4 are hundreds of surfaces maintained full-time by library authors.
Your code adds *proxy rotation, pacing, retries, tier selection* — nothing else.

---

## 1. Pick the layer the target gates on, before picking a tool

| What the target checks | Tool | Cost |
|---|---|---|
| TLS/HTTP fingerprint (JA4), server-rendered HTML or JSON API | **`curl_cffi`** with `impersonate=` | ~0 |
| JS fingerprint / behavioral (Cloudflare, DataDome, Kasada) | **`camoufox`** (`humanize=True`, `geoip=True`) | high |
| Chromium-only stack, Node team | **Patchright** (drop-in Playwright) | high |
| Lightweight Chrome | **nodriver** | medium |

**Automation-protocol fingerprint is a cliff.** Vanilla Playwright/Selenium
(even with stealth plugins) fails regardless of patching — escalate the tool,
don't patch it.

**Always try tier 1 first.** Lamudi looked like it needed a browser (it serves a
CAPTCHA page). It didn't: `curl_cffi` + a math solver cleared it. A browser
would have been ~50× slower for the same data.

### How to know tier 1 works
Fetch with plain `requests` → blocked. Fetch with `curl_cffi(impersonate=...)`
→ 200. That delta *is* the proof the gate is TLS-level. Verify your JA4 is real:

```python
r = sess.get("https://tls.peet.ws/api/all", impersonate="chrome131")
assert r.json()["tls"]["ja4"].startswith("t13d")   # real Chrome TLS1.3 shape
```
Keep that as a self-check; it fails loudly when curl_cffi/target drifts.

---

## 2. Session hygiene (the part people get wrong)

```python
self._imp   = random.choice(["chrome131", "chrome124", "chrome120"])
self._proxy = next(self._rotator)
self.sess   = cffi.Session()      # persistent: cookies survive
```

- **One fixed TLS shape + one proxy per session.** Rotating impersonate profile
  or IP *mid-session* is itself a detection signal (same cookie, new fingerprint).
- **Persistent session is mandatory** when the gate issues a clearance cookie —
  a new session per request throws the clearance away and re-triggers the gate.
- **Rotate identity as a unit:** new cookies + next proxy + new TLS shape,
  together, only when an IP is hard-blocked.

```python
def rotate(self):
    self._imp = random.choice(_IMPERSONATE)
    self._proxy = next(self._rotator)
    self.sess = cffi.Session()
```

- Proxies from env, never in code: `PROXIES="http://u:p@ip:port,..."`.
  Residential/mobile > datacenter. `[None]` = direct, fine for polite crawls.

---

## 3. Pacing & backoff

```python
def _pace(self):                       # jittered floor, not a fixed sleep
    elapsed = time.monotonic() - self._last_request
    gap = self.min_gap + random.uniform(0, 0.6)
    if elapsed < gap:
        time.sleep(gap - elapsed)
    self._last_request = time.monotonic()
```

- Floor measured **since the last request**, so parsing time counts toward it —
  no wasted wall clock, still never bursty.
- `min_gap=2.5` was the value Lamudi tolerated indefinitely on one IP. Bursty
  traffic (`0.8`) triggered the CAPTCHA within a few dozen requests.
- **If robots.txt states a `Crawl-delay`, that is the default — use it.** Mercado
  Libre asks for 5 s and never blocked a single request at it. It is the one
  pacing number you don't have to discover empirically, and honouring it is most
  of what "rate-limit so the target never notices you" means in practice.
- Treat **403 / 429 / 503 / CAPTCHA-page as the same flag**: rotate identity +
  exponential backoff with jitter (`base * 2**attempt + random()`).
- Note the trap: a gate can return **HTTP 200** with a challenge body. Status
  codes alone are not enough — regex the body for the challenge marker too.
- **Sites stack vendors.** Inmuebles24 fronts with DataDome *and* Cloudflare, and
  they announce themselves differently — one body says `geo.captcha-delivery.com`
  / `Pardon Our Interruption`, the other says `Just a moment...`. A block regex
  written against the vendor you found first misses the other one entirely, and
  a missed interstitial parses as "0 cards" ⇒ your crawler thinks the query is
  exhausted and checkpoints it complete. Collect every marker you see:

  ```python
  _BLOCK = re.compile(r"geo\.captcha-delivery\.com|datadome|"
                      r"Pardon Our Interruption|Just a moment", re.I)
  ```
- **Sleep after rotating, before retrying.** A brand-new IP that fires a request
  the same millisecond the old one got blocked is its own signal. 5s, 10s, 15s
  between rotations costs nothing next to the request budget you just saved.

---

## 4. CAPTCHA: read it before you pay for it

Lamudi's "Security verification" was a **custom math CAPTCHA**, and the signed
token embedded the problem in **plaintext base64**. Cost to bypass: ~25 lines.

```python
token   = re.search(r'CAPTCHA_TOKEN\s*=\s*"([^"]+)"', html).group(1)
payload = base64.b64decode(token.split(".")[0] + "==").decode()   # "12 + 7"
answer  = _OPS[op](a, b)
time.sleep(random.uniform(4.5, 7.5))     # server checks elapsed time in the token
sess.get(f"{BASE}/verify-custom-captcha?url=…&token=…&answer={answer}&duration=5.3")
# clearance cookie now on the session — retry the original URL
```

Generalizable lessons:
1. **Decode the challenge payload first.** Home-grown CAPTCHAs (non-hCaptcha,
   non-Turnstile) frequently ship the answer client-side. Check before reaching
   for 2captcha/CapSolver.
2. **Timing is validated.** Sub-second solves get rejected — the token carries a
   timestamp, and the submitted `duration` must be human-plausible. Sleep 4–8s.
3. **Solve on the same session** so the clearance cookie sticks, then retry.
4. **Wrap it in the fetch**, transparently, with a solve cap:

```python
def _fetch(scraper, url, max_solves=3):
    for attempt in range(max_solves + 1):
        html = scraper.get(url).text
        if not CHALLENGE.search(html):
            return html
        if attempt == max_solves or not _solve_challenge(scraper, url, html):
            break
    raise RuntimeError("anti-bot challenge could not be solved")
```

Real hCaptcha/reCAPTCHA/Turnstile → escalate to camoufox or a solver service;
don't burn hours.

### A login wall is usually a fingerprint verdict, not an access rule

Same principle, one tier up. The Mercado Libre script we inherited drove
Botasaurus with a **persistent logged-in profile**, and its own docstring gives
the reason: *"así el sitio no te pide re-loguear ni te bloquea por parecer bot."*
The login was evasion, not authorization.

Measured, cold, zero cookies, `curl_cffi` with a Chrome JA4:

| what | logged out |
|---|---|
| search results page | 200, 48 cards |
| **detail page** | **200, no wall**, coordinates + 21 photos |
| phone / WhatsApp number | absent from the HTML either way — only a `whatsapp_available` flag; it needs a separate reveal call |

So the profile bought **nothing the script actually parsed** — its `parse_detail`
never extracted a phone at all. It was compensating at the wrong layer: a real
Chromium hands over an automation-protocol fingerprint (CDP), which is precisely
what gets you the interstitial, and logging in is the most expensive imaginable
way to look less like a bot. Fix the tier and the wall stops appearing.

Before you build a login flow — or worse, ask a human to sit in front of a
browser once per profile — do three cold requests without one and read the
response. **Ask which fields the wall is actually gating.** If the answer is
"none of the ones I parse", it was never a login problem. And when a wall *does*
gate a real field, that is a scope decision (auth-walled data, §12), not a
transport one.

---

## 4b. Identify the *platform* before you reverse-engineer the *site*

Before writing a parser, check whether the target is a white-labelled instance of
something you've already solved. Inmuebles24 and Vivanuncios are both Navent
builds: identical `script#preloadedData` → `window.__PRELOADED_STATE__` →
`listStore.listPostings` / `paging`, identical posting fields down to
`modified_date` and `whatsApp`. The i24 parser ran on Vivanuncios **unchanged**,
first try. A day of work became an afternoon.

Tells that you're on a known platform:

- the same hidden-blob key (`preloadedData`, `__NEXT_DATA__`, `__NUXT__`) with
  the same store names inside;
- the same robots.txt shape (both sites disallow `*?*sort=*` with a single
  `Allow` exception, both fence `/page-*`);
- the same URL-code grammar in pagination (`v1c31l1018p1`);
- the same anti-bot stack.

So keep the site-specific parts thin and the shared parts shared. Here that's
`navent_serp.py` (blob decoding, `Listing`, fetch+metering, block regex) with
each scraper owning only its base URL, URL grammar and sharding strategy. The
per-site files got *smaller* when the second target arrived, which is the sign
the split is in the right place.

**What differs between instances of the same platform is the URL grammar and the
crawl limits, not the data layer.** Budget your time accordingly: the parser is
free, the sharding strategy is where the work is.

---

## 4c. Tier 1.5: rent the browser for the token, not for the crawl

A JS challenge (AWS WAF, and Cloudflare's non-interactive one) does not mean
"tier 2 for the whole run". It means one cookie is missing. Mint that cookie in
a real browser, hand it to curl_cffi, and the browser goes back in its box:

```python
token = mint_with_browser()          # ~5 s
session.cookies.set("aws-waf-token", token)
# ...now every page is a plain HTTP GET again
```

**Measure the token's life before designing around it.** Pincali's AWS WAF token
died at *exactly* 300 s — 178 requests in 299.3 s, then a 202 — which is the AWS
default immunity window, not a request budget. Time-based, so the fix is a clock,
not a counter. At 5.4 s per mint that is ~2% overhead on a 45-minute run, versus
driving every one of 2,477 pages through a browser.

The cookie's `Expires` attribute lied about this: it said four days. Expiry is
when the *browser* forgets the cookie; validity is what the WAF checks. Time the
real thing — loop until it 202s and print the elapsed seconds.

Three details that cost an hour each if you assume them:

- **Headless fails the challenge, headful passes.** `--headless=new` got zero
  tokens in 30 s; the same patchright + real-Chrome under `xvfb-run` got one in
  5.4 s. If your token minter returns nothing, try a display before you try a
  different library.
- **The challenge response is `HTTP 202`, not 403.** It sails through
  `raise_for_status()` and parses as "0 listings" — which reads as *query
  exhausted*, the most expensive silent failure there is. Detect it by status
  *and* by body (`gokuProps`, `awsWafCookieDomainList`).
- **The token is bound to the IP that minted it.** So either run direct, or mint
  through the same sticky proxy exit you sweep with. Rotating identity on a
  challenge — the reflex from §3 — throws away the very thing that got you in.

**Then make sure the refresh actually replaces the cookie.** This one cost two
nationwide runs. The server sets an `aws-waf-token` of its own on every
response; once that sits in the jar beside the one you set for domain `""`,
`cookies.set()` silently changes *neither*:

```python
>>> [(c.domain, c.value[:8]) for c in s.cookies.jar if c.name == "aws-waf-token"]
[('', 'a7a7241d'), ('www.pincali.com', 'a7a7241d')]
>>> s.cookies.set("aws-waf-token", brand_new_token)   # no-op, both stay old
```

So every token after the first was minted, installed, and never sent. The
symptom is a perfect impostor of rate-limiting: a clean opening stretch, then a
hard wall, then *fresh* tokens bouncing too — which reads as "the IP is in a
penalty box", and sends you off slowing the crawl down. It died at exactly 300 s
both times, and that is the tell: a rate rule does not fire on a stopwatch. **If
a block lands on the same second as your token TTL, suspect your own refresh
before you suspect the target.** `clear()` the jar, then set; key the "already
installed" check on the *session object* too, since a transport-error rotation
hands you a new session with an empty jar and an unchanged token.

Assert it offline — the whole failure is two cookies with one name:

```python
_install(sc, "first")
sc.sess.cookies.set("aws-waf-token", "server-copy", domain="www.pincali.com")
_install(sc, "second")
assert {c.value for c in jar if c.name == "aws-waf-token"} == {"second"}
```

Also: WAF rules are scoped per path. Pincali's homepage answered 200 to a bare
curl_cffi while `/robots.txt` and every listing path returned the challenge. A
single unchallenged URL is not evidence the site is open.

---

## 5. Get the data the cheap way

Ranked by cost, take the first that works:

1. **`data-*` attributes on the SERP card.** Lamudi ships `data-idanuncio`
   (stable ID) and `data-serp-map-hover-listing` (JSON with lat/lng) right in
   the list HTML — no detail fetch needed for the core fields.
   **Read the whole card before you write a detail fetch.** On Lamudi the card
   also carries the *full* description (`[data-itemdescription]`, byte-identical
   to the detail page's JSON-LD description — not a truncated teaser), the
   agent's phone (inside the WhatsApp button's `value=`, i.e.
   `api.whatsapp.com/send?phone=+52…`), amenity chips, `data-year`, and
   `exactLocation: true` coordinates. Everything the detail page contributed
   except the extra gallery photos was already in hand. Dump one card's full
   HTML and diff it against a parsed detail page **before** deciding the detail
   phase exists (§6).
2. **JSON-LD** (`application/ld+json`, `@type: RealEstateListing`): clean numeric
   price, currency, address, floorSize, description — already parsed, no selector
   drift. Walk `@graph` and lists, not just dicts.
   **Check the SERP for it, not just the detail page.** Mercado Libre has no
   hydration blob at all, but every search page carries one `ld+json` `@graph`
   holding a `RealEstateListing` per card — with the **full description** (not a
   teaser), `address`, `floorSize`, `seller` and `datePosted`. That is the entire
   detail phase, shipped with the SERP, for a site that looks like it needs a
   browser. It cost one `grep 'ld+json'` to find.
   Pair it with the card DOM rather than choosing between them: schema.org
   flattens `PostalAddress` to one locality, while the card's
   `poly-component__location` keeps the whole colonia → municipio → estado chain,
   and the card headline (`"Terreno en venta"`) is the only place the property
   type appears. Key both by the listing id and merge.
3. **Hidden JSON blobs**: `__NEXT_DATA__`, `window.__INITIAL_STATE__`,
   `script#preloadedData`, or the XHR the page itself calls. Often the *entire*
   result set is hydrated here — Inmuebles24's `preloadedData` carries all 30
   listings **fully populated** (price per operation, exact geolocation, area,
   description, agency, photos) **plus `paging.total`/`paging.totalPages`**. That
   killed the detail phase entirely: ~30× fewer requests than Lamudi's enrich
   crawl. Always dump the blob and check what's already in it *before* writing a
   detail fetch. (Parse gotcha: it's a JS assignment — `raw_decode` from the
   first `{`, not `json.loads`, which dies on "Extra data" after the object.)
4. **CSS selectors** — last resort, most brittle.

**The blob almost always ships the two fields you were going to build a phase
for: the publication date and the phone.** On Inmuebles24 every posting carries
`modified_date` (`"2026-07-23T14:29:13-0400"`) and `whatsApp` (`"52 8180294264"`)
right in `listStore.listPostings`. That is a free `listedAt` *and* a free
`agentPhone` — no detail page, and no call to the site's phone-reveal endpoint,
which is the request most likely to be rate-limited or logged against you.
Before writing any second phase, dump one blob and grep it for `date`, `phone`,
`whats`, `mail`, `publi`. Same for the **location chain**: i24's
`postingLocation.location.parent…` walks ZONA → CIUDAD → PROVINCIA → PAIS, so the
province *the site itself* assigns each listing is free — store it as a column,
it's what makes the coverage audit (§8) exact instead of a string-matching guess.

A hidden blob is also where the site describes *itself*, not just its listings:
`paging.total`/`totalPages` (job size), `filtersStore` (which filters the server
thinks are applied — §6), `sortFilter.options` (what orderings exist),
`breadCrumb` / `seoAttributes.canonical` (where your slug actually resolved).
Each is a check you'd otherwise pay requests to run.

Two-phase design: SERP pass (cheap, 30 listings/request) → optional detail pass
(`--enrich`) only for the subset you actually need. Detail is ~30× the requests
and ~18× the bytes per listing (§6); make it opt-in, default it off, and keep it
re-runnable over the SERP file already on disk (`reenrich`).

Parser: **`selectolax`** (Lexbor), several times faster than BeautifulSoup and
with the same ergonomics. `lxml` also fine. Never regex whole HTML documents —
regex only tiny, well-anchored things (a coordinate, a phone).

---

## 6. Cost: proxies bill **bytes**, not requests

Residential proxies charge per GB. So the crawl's price is `Σ compressed
response size`, and the only lever that matters is *fetching fewer, smaller
pages*. Optimize this before you optimize anything else — it dwarfs every other
knob.

### Measure the wire, not the parse

`len(response.content)` in curl_cffi is **decompressed** and will overstate your
bill by 6–9× (measured on Lamudi's own pages). What the proxy meters is the
compressed body. Estimate it with
`len(gzip.compress(r.content, 6))` (close enough to server-side gzip), and check
`r.headers["content-encoding"]` to see what you're actually getting.

**Put the meter in the fetch wrapper, not in a one-off audit script.** Four lines
buy every future run a truthful bill in its own summary, instead of an estimate
you reconstruct afterwards from page counts:

```python
WIRE = {"bytes": 0, "requests": 0}          # module-level

resp = scraper.get(url, **kw)
WIRE["requests"] += 1
WIRE["bytes"]    += len(gzip.compress(resp.content, 6))
```

Lamudi MX, measured (July 2026):

| request | decompressed | **on the wire** | per listing |
|---|---|---|---|
| home `/` | 246 KB | 27 KB | — |
| state landing `/{state}/` | 641 KB | 80 KB | — |
| SERP page (30 cards) | 678 KB | **88 KB** | **2.9 KB** |
| detail `/detalle/{id}` | 309 KB | **52 KB** | **52 KB** |

40% of that SERP page and **65%** of the detail page is inline `<style>`. You
pay for the site's CSS on every single request and there is nothing you can do
about it — which is exactly why the fix is *not making the request*.

### The detail phase is the whole bill

78,137 listings nationwide (3 categories) → SERP 229 MB, entry 3.4 MB, detail
**4.06 GB**. The detail phase is **94.6%** of the run, an 18× multiplier, and on
Lamudi it bought only extra gallery photos. General rule:

> Detail is ~18× the bytes per listing. Price it that way, make it opt-in, and
> justify it field by field against what the card already gives you.

### A fat SERP page is cheap if it kills the detail phase

Inmuebles24's SERP is **twice** the size of Lamudi's — 1,250 KB decompressed,
**188 KB on the wire** for 30 cards — because the `preloadedData` blob hydrates
every listing completely. Judged per page that looks like a worse target. Judged
per *listing delivered*, which is the only unit that matters:

| | per SERP page | per listing, all-in | nationwide (~75k listings) |
|---|---|---|---|
| Lamudi (SERP + detail) | 88 KB | **~55 KB** | 4.3 GB |
| Inmuebles24 (SERP only) | 188 KB | **~6 KB** | **~440 MB** |

Nine times cheaper, from a page that's twice as heavy. Never compare page sizes
across targets; compare bytes per row you actually keep.

The i24 numbers above are the completed run, not a projection: **69,536 unique
listings, 2,508 requests, 438.7 MB** on the wire, 32/32 states, 96/96 queries,
~3h wall clock, single residential proxy pool, no browser. The `--survey` run
predicted 72,236 listings / ~440 MB from 35 requests beforehand — sizing a job up
front is accurate enough to budget from.

### Find the pagination ceiling before you plan the crawl

**Most sites cap how deep a single query can be paginated, and the cap is far
lower than the result count.** Inmuebles24 advertises 13,763 results for one
query and serves exactly **four pages of it**: `pagina-5` is a hard 403 — on a
cold IP, on a fresh identity, behind any filter, every time. In the first
nationwide attempt, **63 of 63 failures were exactly `pagina-5`**, which read
like flaky rate limiting for a whole run and was actually a fixed wall.

Diagnose it in three requests, and do this *before* writing the crawl loop:

```python
s.rotate(); print(try_(url + "-pagina-4.html"))   # OK
s.rotate(); print(try_(url + "-pagina-5.html"))   # 403
s.rotate(); print(try_(url + "-pagina-6.html"))   # 403
```

A fresh identity per probe is the whole point — it separates "we got flagged"
(the block moves as you keep crawling) from "this URL is fenced" (the block is
always at the same N). If the boundary is stable across cold IPs, **stop trying
to evade it**: rotation, pacing, and browsers will not move a page cap.

### Enforced cap vs requested cap — same robots line, different problem

Run the probe even when robots.txt already states a limit, because the two cases
need opposite responses and robots does not distinguish them:

| | Inmuebles24 | Vivanuncios | Mercado Libre |
|---|---|---|---|
| robots.txt | `Allow: pagina-2..5`, rest Disallowed | `Allow: /s-*/page-2..5`, rest Disallowed | `Disallow: /*_Desde_` — **all** pagination |
| server, past the line | **403, always, cold IP** | **200, full results** | 200 to offset ~2000, then **0 cards** |
| what it is | a hard engineering constraint | a stated site preference | a stated preference *and*, deeper, a wall |
| your options | shard, or get nothing | shard *or* paginate — a judgment call | shard; pagination can't finish the job anyway |

On i24 the cap dictated the architecture and there was nothing to decide. On
Vivanuncios the identical robots line is only a request, so what to do is a
policy question with a measurable price tag — which makes it the *operator's*
call, not the crawler author's. Measure the cost of compliance, put the number
in front of whoever owns the decision, and make it a flag (`--page-cap 5`)
rather than a hardcoded assumption in either direction. Note that depth is
orthogonal to load: same pacing, same concurrency, same identity rotation — only
the number of pages differs.

Mercado Libre is the case where the question dissolves. robots Disallows
pagination outright, and the server independently stops returning results past
offset ~2000 (`_Desde_1969` → 48 cards, `_Desde_2017` → zero) — so a
4,571-result state could not be exhausted by paging even if you ignored robots.
**When the two agree, stop treating it as a policy question and go find the
partition axis**; the only thing left to decide is whether to burn a flag on the
residue (`--paginate`, for the rare shard the axis cannot split).

### Sitemaps are a pre-built shard tree — but they are a subset, not a partition

Vivanuncios publishes ~44k location URLs in `sitemap_list_https_1.xml.gz`, one
gzipped request, with the hierarchy encoded *in the path segment itself*:

```
/s-venta-terrenos/nuevo-leon                    ← state
/s-venta-terrenos/monterrey_nuevo-leon          ← city
/s-venta-terrenos/el-uro_monterrey_nuevo-leon   ← zone
```

A shard's parent is its own segment minus the leading component, so the whole
tree is `"_".join(loc.split("_")[1:])` — no discovery requests at all. That is
the cheapest sharding dimension in this playbook when it works.

**Verify that it works before designing around it.** SEO sitemaps list the pages
the site wants *indexed*, which is not the same as a complete partition of its
inventory. Measured on Vivanuncios: Monterrey advertises 1,641 terrenos and
publishes 54 zone shards whose totals sum to well under that. Walking the tree
under a 150-listing cap reached **6,547 of 8,431** listings in Nuevo León — 78%,
and the missing 22% is invisible unless you check.

The check is two requests per sample: fetch a parent's total, fetch its
children's totals, compare the sum. Do it on your biggest shard before you
commit — if `Σ children << parent`, the tree is a hint, not a partition, and you
need a different dimension (or the operator's blessing to paginate deeper).

The consequence is a design change, not a transport change: **every query URL
must be sharded until it holds fewer than `pages × per_page` listings.** Which
means you need a partition dimension.

### Sharding below the cap: keyset pagination on a filter

Location is the obvious shard and it runs out fast (a state is one slug and can
hold 13k listings). What actually works is a **sortable field plus a one-sided
filter on that same field** — the site's own indexes, used as a cursor:

1. Sort ascending by price.
2. Walk the 4 pages you're allowed (120 listings).
3. Take the **highest price seen**, make it the next shard's floor
   (`-mas-de-{price}-pesos`), reset to page 1.
4. Repeat until a shard's advertised total fits inside the cap.

```python
floor = 0
while True:
    ...walk pages 1..PAGE_CAP of url(floor)...
    if pages_walked * per_page >= shard_total:
        break                                    # this shard was the last
    floor = max(prices_seen) if max(prices_seen) > floor else floor + 1
```

Notes that cost real time to learn:

- **You do not need a two-sided range.** i24 has `-hasta-N-pesos` and
  `-mas-de-N-pesos` but no working `-desde-A-hasta-B` form. A floor alone is
  enough — that's the point of keyset pagination, and it's why this generalizes
  to any site with one sortable filter.
- **The floor being inclusive is a feature.** `-mas-de-N` returns the boundary
  listing again; dedupe eats the repeat, and the overlap is what guarantees you
  never skip a listing straddling a shard edge. An *exclusive* filter silently
  drops every listing at exactly N.
- **Guard the degenerate case.** More than `cap` listings sharing one identical
  price never advances the floor. `floor + 1` when the max didn't move — you lose
  the ties, you don't lose the run to an infinite loop.
- **Arbitrary thresholds work**, even when the UI only offers round ones: i24
  accepts `-hasta-333333-pesos` and labels it `MN333K`. Don't assume the filter
  vocabulary is limited to the values the dropdown shows.
- **Know what the sort silently excludes.** Sorting by price drops listings with
  no price ("consultar precio"): the same query reports 246 unsorted and 240
  sorted. So the *reachable* total is the sorted one — measure progress against
  that, not against the advertised total, or every query looks like it failed.
  Log the gap (~2-3% here); it's the honest cost of this technique.

### When you can't paginate at all: partition, don't walk

Keyset walking needs a sort. Mercado Libre Disallows every `_OrderId_`, so there
is no cursor to advance — but it Allows `_PriceRange_`, and a *range* filter
gives you something better than a cursor: a **recursive partition**. Fetch a
range, and the same response tells you both the 48 rows and the range's total.
If the total overflows one page, cut the range up and push the pieces.

The trick that makes it cheap is **where you cut**: use the quantiles of the 48
prices that request just returned.

```python
pool = sorted(p for p in prices_on_this_page if lo <= p <= (hi or inf))
k    = ceil(total / TARGET)                      # TARGET ≈ 40, just under a page
cuts = [pool[round(i * (len(pool)-1) / k)] for i in range(1, k)]
```

Prices are lognormal, so a midpoint split puts ~everything in the lower half and
a uniform grid is worse; a sample of the range's *own* prices is a far better
estimator than either, and it arrived for free with the rows. **Correctness never
depends on the estimate** — a child that still overflows is simply split again,
so a bad guess costs requests, not rows. Keep the ranges inclusive on both ends
and overlap them by a peso rather than stepping `+1`: dedupe eats the repeat, and
overlap is what guarantees no listing falls between two shards.

Order the traversal leftmost-first (a stack, children pushed reversed) and the
resume cursor is a single number again — everything below the last finished
range's ceiling is done, so the checkpoint format from keyset walking
(`query@floor`) works unchanged.

What it costs, measured over the full nationwide run: **2,963 requests, 688 MB,
57,921 rows of 59,067 advertised (98%) — 19.5 rows/request against a 48-row
page.** The tax is re-splitting: ranges that overflow their target cost ~2 extra
requests to deliver a handful of new rows. Tuning `TARGET` moves this very little
(the yield plateaus around 18-23/request either way, because leaves shrink as
fast as overflows disappear) — measure once, pick a number, stop tuning. Quote
the *measured* yield in `--survey`, not `TARGET`: the difference is 2× on the
estimate a person uses to approve the job.

Two things this technique cannot do, and you should say so out loud rather than
let them hide in a coverage number:

- **Priceless listings are unreachable** on the price axis, the same blind spot a
  price *sort* has (§ above).
- **A single price point holding more than one page is unsplittable.** 128 of the
  run's shards were degenerate — round numbers ($15,000 rent, $500,000 land) are
  where prices cluster — and that is exactly where the missing 2% went.
  `--paginate` drains them with the Disallowed `_Desde_`; leaving it off and
  reporting 98% is the honest default.

Note what the coverage number is *not* hiding: 0 errors, 0 queries short of their
advertised total, 32/32 provinces, and 57,921 unique ids across 57,921 rows. A
sharded sweep with overlapping ranges is the case where "unique rows" and "rows
written" diverging would be invisible — check it, don't assume it.

### Read robots.txt as a specification, not just a permission slip

The single most useful request of this job was `GET /robots.txt`. It disclosed:

```
Allow: /*-ordenado-por-precio-ascendente*
Disallow: /*-ordenado-por-*
Allow: /*pagina-2.html$ … /*pagina-5.html$
Disallow: /*pagina-*.html
```

Two things fell out of four lines. First, **the sort syntax we had failed to
guess** — we had probed `?orden=`, `?sort=`, `-orden-publicado-descendente`,
`-mas-recientes`, all silently ignored; the real form is `-ordenado-por-…`, and
robots names it. Second, **the page cap is stated site policy**, not a bug and
not something to route around: robots allows pages 2-5 and disallows the rest,
which is exactly where the 403 lives.

That combination is also why the sharded design is the *right* answer rather
than a clever bypass: it stays inside the pages robots allows, uses the one
ordering robots explicitly permits, and reaches the whole corpus through URLs the
site publishes for indexing. When robots and the server agree on a limit, treat
it as the API contract — then design within it.

Grep every robots.txt for query-ish path fragments before guessing at filter
syntax. Sitemaps listed there are the other freebie: they enumerate the site's
own shard URLs, which is a second partition dimension for nothing.

**The `Allow:` lines are the shortest list of what the site *wants* crawled, and
that list is usually your architecture.** Mercado Libre's `User-agent: *` block
is 370 lines, 358 of them `Disallow:` — pagination, every sort, every area /
bathroom / parking facet, the publication-date filter. Exactly one search filter
survives:

```
Disallow: /*_Desde_        Disallow: /*_OrderId_       Disallow: /*_PublishedToday_
Allow:    /*_PriceRange_   Crawl-delay: 5
```

One `Allow:` line among hundreds of `Disallow:` is not a footnote, it is the spec
for the only crawl the site sanctions: **page 1 only, unsorted, partitioned by
price, one request every five seconds.** Reading that block took one request and
decided the entire design — including `Crawl-delay`, which is the polite floor
they *asked* for and therefore the default your `--min-gap` should carry (§3).

Also parse robots per user-agent block, not with a flat grep: `Bingbot` had its
own 367-line block here, and the rules that bind you are the ones under
`User-agent: *`. Equal-length `Allow` beats `Disallow` (RFC 9309), which is
precisely how `/*_PriceRange_` survives its own `Disallow`.

### Ask the site how big the job is, in 3 requests

Drop the location shard and hit the bare category URL: the SERP's result counter
(`[data-test*=result]`) reports the national total. Three requests told us the
job was 78k listings before writing a line of crawl code. Then use the counter
per query to cap pagination at `ceil(count/30)` and to **skip 0-result queries
entirely** instead of paying a page to discover they're empty.

### Delta runs: only fetch what changed

A "full run" rebuilds the whole corpus. A **delta run** fetches only listings
that appeared since the last run. Dedupe-by-ID doesn't get you this — it stops
duplicate *writes*, not duplicate *fetches*; you still walk all 2,605 pages to
discover that 98% of them are old. A delta run needs two things:

1. **A newest-first ordering**, so new listings cluster on page 1 instead of
   being scattered through the corpus. On Lamudi that's an *undocumented*
   `?sorting=newest` — every other spelling (`sort=`, `sortBy=`, `orderBy=`,
   `sorting=recent|mostRecent|date`) is silently ignored and returns default
   order. **Silently.** Always verify a sort param actually sorted, don't
   assume a 200 means it took.
2. **A watermark** — the newest listing timestamp from the previous run,
   persisted. Walk pages newest-first, stop when a page's newest item predates
   the watermark (plus a page or two of margin, since ordering can wobble
   *within* a page), then advance the watermark.

```python
# stop condition, newest-first
if max(c.listed_at for c in cards) < watermark:
    stale_pages += 1
    if stale_pages > 2:
        break
```

The floor is one page per query no matter what — 96 queries × 88 KB ≈ 8 MB — so
shard count, not corpus size, sets a delta run's minimum. Measured full rebuild
4.3 GB; a weekly delta lands around **30 MB** (estimate: ~2%/week new listings,
plus 2 margin pages per query — not yet observed across two real runs). Run the
full crawl once, deltas forever.

**Now observed, on Inmuebles24.** Full run: 2,508 requests / **438.7 MB**.
Immediately after, a 7-day delta over the same 96 queries: 229 requests /
**35.2 MB** and 41 genuinely new listings — a **12.5× cost reduction**, and the
delta doubles as proof the full run wasn't missing anything (it found almost
nothing new). The shard count really is the floor: 229 requests for 96 queries
is ~2.4 pages each, and no amount of freshness would push it much below that.

### Better than a watermark: look for a publication-date *filter* first

The sort+watermark dance above is the fallback. **Check whether the site will
just filter the result set server-side** — you get the same delta for fewer
requests, no margin pages, and no risk from ordering wobble. Inmuebles24 has no
URL-addressable sort at all (every spelling of `orden=`/`sort=`, and every
`-orden-publicado-descendente` style path, is silently ignored) but it *does*
honour publication-window path suffixes:

| suffix | `filtersStore.publicationdate.min` | one query's total |
|---|---|---|
| *(none)* | `None` | 13,763 |
| `-publicado-hace-menos-de-1-semana` | `"7"` | 96 |
| `-publicado-hace-menos-de-15-dias` | `"15"` | 200 |
| `-publicado-hace-menos-de-1-mes` | `"30"` | 289 |
| `-publicado-hace-menos-de-45-dias` | `"45"` | 372 |
| `-publicado-hoy`, `-publicado-hace-menos-de-2-meses` | `None` | **13,763** |

Look at the last row. Those are plausible-looking suffixes that don't exist, and
the site does not 404 them — it **drops the filter and serves the entire
corpus**. Run a "weekly delta" on a typo'd suffix and you pay for a full rebuild
while believing you paid for 2%. Which is the general rule:

> **Never trust that a filter or sort applied. Read it back.** The state blob
> tells you what the *server* thinks is applied (`filtersStore.publicationdate.min`,
> `filtersStore.sort.min`). Compare it to what you asked for on the first page of
> every query and raise if they differ. Silent-ignore is the default failure mode
> of every URL-encoded filter on every site in this playbook.

```python
applied = obj["filtersStore"]["publicationdate"]["min"]
if days and applied != str(days):
    raise RuntimeError(f"filter not applied (server says {applied!r})")
```

**And check the result count, not just the filter flag** — a dropped suffix can
take part of your URL with it. The same publication-window suffix that works on
Inmuebles24, pasted onto a Vivanuncios location path, returned
`total=52,506`: not the state's 6,486 unfiltered, but the **national** total. The
site discarded the suffix *and* the location and answered a question nobody
asked, with a 200. A run built on that is quietly scraping the wrong scope at
40× the volume. Assert on both the echoed filter and a plausible total.

Find the available windows the same way you find anything else: they're listed in
the blob, at `listStore.moreFilters.publicationDate.options`. The site tells you
its own vocabulary — you just have to map option → URL suffix once, empirically,
and hardcode it.

### The default ordering may be randomly seeded

Inmuebles24's blob carries `listStore.sortSeed = "RandomName_23-07-2026-02-3"` —
its "Relevantes" ranking is seeded per date+hour bucket. A query deep enough to
straddle a seed change gets **reshuffled under the crawler**: some listings shift
onto pages you already walked (harmless, dedupe eats them) and some onto pages
you already passed (**silently lost**).

You usually can't pin the seed — it isn't URL-addressable. So measure the damage
instead: compare unique cards collected per query against the site's own
`paging.total`, and warn below ~90%. It costs nothing, and it's the difference
between "the run finished" and "the run finished correctly".

```python
if site_total and got < site_total * 0.9:
    logger.warning("%s: yielded %d of %d advertised — likely a seed reshuffle",
                   query, got, site_total)
```

### Free timestamps: check if the ID is a UUIDv7

Lamudi's `data-idanuncio` is a UUIDv7 — the creation time is the first 48 bits:

```python
listed_at = datetime.fromtimestamp(int(uuid.replace("-", "")[:12], 16) / 1000, timezone.utc)
```

A publication date for zero requests, and the watermark the delta run needs.

**Check the version nibble, not just the date.** `plain[12] != "7"` ⇒ reject.
Lamudi still serves legacy v3/v4 ids whose *random* bits decode to a
perfectly plausible epoch — we shipped 78 rows dated up to 2099 before adding
that one check, because a range gate of 2010..2100 happily passes garbage.
Sequential integer IDs give you the same lever (monotonic ⇒ comparable), just
without the date.

### Everything else is rounding error

Checked on Lamudi, all dead ends worth ruling out fast:

- **No JSON search API.** Pagination is full page loads; the only `/api/`
  routes are tracking, leads, phone-reveal and alerts.
- **No page-size param.** `limit`/`size`/`perPage`/`pageSize` are all ignored —
  always 30. (When one *does* work it's a real win: it amortizes the page
  chrome you're paying for either way.)
- **No brotli/zstd.** CloudFront in front of Jetty serves gzip only, despite
  Chrome advertising `br, zstd`. Worth one check — br is ~20% under gzip free.
- **No `ETag`/`Last-Modified`** ⇒ no conditional `If-None-Match` GETs, which
  would otherwise make unchanged pages cost ~0 bytes.
- **Mobile UA is ~6% smaller** (86 KB vs 91 KB, same 30 cards). Free, but noise
  next to killing the detail phase.
- **Blocks are cheap.** A 401/403 from the gate is ~13 bytes. Getting blocked
  costs you time and IPs, not bandwidth — don't optimize retries for cost.

Order of attack: kill the detail phase → delta runs → cap pagination from the
counter → compression/UA scraps.

---

## 7. Speed, without more concurrency

Most of the wins here were structural, not parallel:

- **Resume via a `seen` set** loaded from the output JSONL. Re-runs only fetch
  new IDs; a crash costs nothing. `_load_seen()` + append-mode + `flush()` per
  row means every row survives a kill -9.
- **JSONL, appended and flushed**, not a giant list held in RAM until the end.
- **Skip the detail phase** for fields the SERP already has.
- **Shard the query space** instead of paginating deep: 32 state slugs × 3
  categories keeps every query under the site's page cap and lets you stop early.
- **Cache aggressively.** Every re-hit is another chance to get flagged; saved
  HTML fixtures cost nothing.
- **Checkpoint the query, not just the row.** Dedupe by ID stops duplicate
  *writes*; it does not stop re-*fetching* pages you already walked. A sidecar
  `<out>.done` with one `state/category/operation` per exhausted query turns a
  killed nationwide run into a resume instead of a restart.
- **Then checkpoint the position *inside* the query too.** Query-level alone is
  half a resume: a query that dies 40 shards deep restarts those 40 shards. One
  append-only line per shard boundary fixes it, and the same file still holds the
  completions — encode the difference in the token, not in a second file:

  ```
  colima/terrenos/venta@2004860     ← resume the sweep from this price floor
  colima/terrenos/venta             ← exhausted, skip entirely
  ```

  Parse last-token-wins, never downgrade a completed query, and write the
  progress line only at boundaries (not every page) so the file stays small.
- **Entering through the homepage costs 2 requests per state, not per page.**
  Home → `/{state}/` → SERP p1 → p2 (Referer = previous page). The realism is in
  the header chain and the cookies, so paying for it once per session is enough.
  Clicking your way to every SERP would triple traffic — more exposure, not less.
- **One identity per shard.** `rotate()` at the state boundary = a new visitor
  arriving from the homepage. Rotating *within* a state is the suspicious move.
- Concurrency is the *last* lever, and it is capped per-domain. Sequential +
  2.5s gaps ran for hours unblocked; 8 workers would have gotten the IP banned
  in minutes. Parallelize across *proxies*, not within one IP.

---

## 8. Correctness traps (cost the most time on this job)

- **The site's advertised total ≠ unique listings.** Lamudi's "1178 resultados"
  counted the raw cards its pagination serves; ~12% are **duplicates repeated
  across pages**. Deduping by ID gave 1032. Always report both numbers before
  concluding the scraper is losing data.
- **Look harder for the result counter before concluding there isn't one.** We
  paginated Lamudi until empty for weeks on the belief that its SERP exposed no
  total. It does — `[data-test*=result]` (`"3,377"`), rendered on every page.
  Cap the loop with `ceil(count/30)` and keep `break`-on-empty as the backstop.
  Beyond the last real page the site keeps returning 200 with zero cards (and
  the counter eventually reads `0`), so a bare "paginate until empty" is correct
  but pays for the probe.
- **The counter can also be *off by a constant*, on some queries and not others.**
  On Mercado Libre's Aguascalientes pages the count runs exactly one ahead of what
  renders — "15 resultados" → 14 `<li>` → 14 `@graph` entries, on every partial
  page. On Nuevo León, zero pages disagree. (Likely cause: the count includes the
  fuzzy out-of-state matches ML mixes in, which the grid then drops — an
  Aguascalientes page came back holding a Playa del Carmen listing.)
  Harmless as data, lethal as a *loop condition* — the natural "is this range
  exhausted?" test, `total > len(cards)`, is then true for **every leaf**, so a
  recursive partition never terminates on a leaf and keeps subdividing until each
  range is a single price point. Nothing fails; the rows are right and the crawl
  just pays. Measured on the affected query: **25 requests to bank 103 rows (4.1
  rows/request)** against ~18 elsewhere — and *because* it was query-dependent,
  the dense state we happened to benchmark on looked perfectly healthy.
  Two rules fall out. **Never compare the site's count against your own card
  count** to decide "there is more" — compare it against the page *size* you were
  going to fetch (`total > PER_PAGE`), which is independent of both rendering and
  the site's arithmetic. And **put the loop condition in a named function with an
  assertion**, because it is the one line where an off-by-one is invisible:
  ```python
  assert not needs_split(15, 14)      # the phantom result must not force a split
  assert needs_split(PER_PAGE + 1, PER_PAGE)
  ```
- **A pretty URL and a query parameter may not compose.** On Pincali,
  `/inmuebles/terrenos-en-venta` is 71,186 lots — add `?search_criteria[min_price]`
  to it and you get 291,916 rows, because the first `search_criteria` param
  *replaces* the criteria the slug encoded instead of narrowing them. HTTP 200,
  a plausible-looking grid, and the shard you thought was Terrenos/Guanajuato is
  the national all-types corpus. The fix is to stop mixing the two languages:
  send **every** criterion as a parameter (type, operation, price) and treat the
  slug as decoration. Then read the type back off the rows on every page, not
  just the first — this is the same lesson as i24's `filtersStore` read-back and
  Viva's national-total, arriving through a third door.
- **A dedupe `seen` set shared across queries** hides overlaps between categories
  (a listing in both "venta" and "renta"). Intended, but it makes per-query
  counts lower than per-query totals — know which you're reporting.
- **Order matters in prefix classifiers**: check `"Lote Comercial"` before
  `"Lote"`, `"Local Comercial"` before `"Local"`.
- **Mexican number format**: `,` = thousands. Strip before `float()`. Currency
  from the text (`US$`/`USD` → USD, else MXN), never assumed.
- **Fuzzed coordinates — but check, don't assume.** SERP map-hover coords *may*
  be jittered for privacy. Lamudi's aren't: the blob self-declares
  `{"latitude":…,"longitude":…,"exactLocation":true}`. Believing the folklore
  cost us a 52 KB detail fetch per listing for coordinates we already had. If
  the payload carries a precision flag, trust it; otherwise verify by diffing a
  handful of cards against their detail pages, once.
- **Non-breaking spaces** (` `) inside prices/areas break naive regex.
- **A location slug can silently resolve to the wrong place.** On Inmuebles24 a
  bare state slug resolves to a same-named *city* (`puebla` → Puebla city, not
  the state) or an unrelated municipality (`guerrero` → a town in Chihuahua),
  returning a page that looks fine but has a fraction of the listings. Rule of
  thumb: when a state shares its capital's name, the province page needs a
  disambiguating suffix (`-provincia`, or a site-specific form like
  `edo.-de-mexico`, `baja-california-norte`). **Resolve every slug once against
  the site's own reported total + resolved-location label, then hardcode** — a
  wrong slug is invisible (no error, just a low count). Don't trust a slug you
  haven't seen return the expected order of magnitude.
- **The same trap on Lamudi, worse: states and *neighbourhoods* share one slug
  namespace.** Four of 32 needed the formal state name, and three of those
  resolved to a same-named colonia in Baja California — HTTP 200, real listings,
  checkpointed `complete`, just the wrong place:

  | intuitive slug | actually resolves to | correct slug |
  |---|---|---|
  | `coahuila` | "Coahuila, Mexicali" (4 results) | `coahuila-de-zaragoza` |
  | `estado-de-mexico` | "Estado de México, Ensenada" (0) | `mexico` |
  | `michoacan` | 301 → `/baja-california/tijuana/michoacan/` | `michoacan-de-ocampo` |
  | `ciudad-de-mexico` | 404 | `distrito-federal` |

  Only the 404 announced itself. Note the 301: a redirect into a *deeper* path
  is the tell that your slug matched a child location, and following it through
  a proxy tunnel is what surfaced as `CONNECT tunnel failed` — a transport error
  that was really a slug bug. Read redirect targets before blaming the proxy.
- **Audit coverage from the data you already have, for free.** After the run,
  bucket rows by the trailing component of their location string and compare
  against your expected shard list. A shard with *zero* rows carrying its own
  name did not fail — it silently scraped somewhere else. This caught Coahuila
  and Estado de México after the crawl reported 86/96 queries "complete". Do it
  every run: it costs no requests and it is the only check that catches a slug
  that resolved to a real, wrong place.
- **Prefer the site's own total over guessing the last page.** When the blob
  ships `paging.totalPages`, cap the loop with it (still `break` on empty as a
  backstop). Cleaner than Lamudi's "paginate until empty," and it sidesteps the
  unique-vs-advertised-count confusion — though the *advertised* total still
  counts cross-page duplicates, so dedupe by ID regardless.

---

## 9. Keep a self-check, offline

Save one SERP page and one detail page to `.fixtures/`, then assert on them:

```python
cards = list(parse_serp(fixture_html, "terreno", "for-sale"))
assert len(cards) >= 20
assert cards[0].listingId and cards[0].url.startswith(BASE + "/detalle/")
assert -120 < cards[0].coordinates[1] < -80          # plausible MX longitude
assert parse_detail(detail_html, cards[0]).photos
```

Runs in milliseconds, no network, and fails the moment the site's markup drifts.
Pair it with the JA4 self-check from §1 — one guards parsing, one guards evasion.

---

## 10. Ops notes

- `pip install curl_cffi selectolax camoufox[geoip]`; then `camoufox fetch` once.
- A venv breaks if you move its directory (`env/bin/pip` shebang is absolute).
  Use `env/bin/python -m pip …`, or recreate the venv.
- Import camoufox **lazily**, inside `render()` — don't pay the browser cost on
  runs that never leave tier 1.

## 11. The flows a nationwide run needs

Both of the first targets in this repo failed their *first* nationwide attempt,
and neither failure was evasion. Lamudi reported "86/96 queries complete" while
two states had silently scraped the wrong place. Inmuebles24 died at 33/96 on a
page cap it mistook for rate limiting. Both now finish 96/96. What fixed both was
structure, so build these six flows into every scraper of this style — they're
each 20-60 lines and they are the difference between a script that works on one
page and a run you can trust. Vivanuncios and Mercado Libre were both written
flows-first and neither had a failed attempt.

| flow | when | costs | catches |
|---|---|---|---|
| `--selfcheck` | every edit | 0 requests | markup/JSON drift, silently-empty fields |
| `--survey` | before the crawl | ~35 requests | wrong slugs, job size, page cap |
| resume (`<out>.done`) | always on | 0 | a killed run restarting from zero |
| `--days N` (delta) | after run #1 | ~1 page/query | paying for a full rebuild weekly |
| wire metering | always on | 0 | a proxy bill you can't explain |
| `--audit` | after every crawl | 0 requests | a shard that scraped the wrong place |

**A delta run right after a full run is also the cheapest completeness proof you
have.** i24's 7-day delta cost 35 MB and returned 41 new listings against a
69,536-row corpus. If it had returned thousands, the "complete" full run wasn't.

**1. `--selfcheck` — offline, on a saved fixture.** Assert the count, then assert
the *specific fields that let you skip a phase*. A parser that returns 30 cards
with an empty `agentPhone` is worse than one that crashes, because the run
succeeds and the data is quietly thin:

```python
assert sum(bool(x.agentPhone) for x in cards) > len(cards) // 2, "SERP phones gone"
assert c.province, "PROVINCIA missing — the audit would break"
assert _search_url("colima-provincia", "terrenos", "venta", 2, 30) == (...)
```

Assert your **URL builder** too. Suffix order is not free-form (i24 wants
`{window}{price floor}{sort}{page}` and drops what it reads out of order), and a
one-character URL bug is invisible at runtime — the site 200s and ignores you.

**2. `--survey` — size the job and prove every shard slug resolves.** One
command, run before the crawl, that answers three questions the crawl otherwise
answers too late:

- *How big is this?* Bare category URLs, one request each → the national total.
  72,236 listings ≈ 2,400 pages ≈ 440 MB, known before writing a crawl loop.
- *Do my slugs resolve where I think?* One request per shard, comparing the
  site's own resolved label against an expected table. Print ✔/✖ per row.
- *What does a page cost?* Meter it (below) and you get bytes/listing for free.

Compare against the label **the site itself uses**, and copy its exact spelling —
i24 says `"Baja California Norte"` and `"San luis Potosí"` (lowercase L). Two of
our 32 rows failed the first survey purely because the *expectation table* was
prettier than reality. Normalize to the site's strings; you're checking identity,
not orthography.

**3. Resume, at both levels.** `<out>.done` for finished queries, plus a
position token for the query in flight (§7). Non-negotiable on a multi-hour run.

**4. Delta mode.** A publication-window filter if the site has one, else
newest-first + a watermark (§6). Verify it applied; a silently-dropped filter
means you pay full price believing you paid 2%.

This is the one flow that isn't always available — two of four targets here don't
have it. Vivanuncios has no publication-date filter and robots disallows
`sort=more_recent`; Mercado Libre's only date filter is `_PublishedToday_` and
every `_OrderId_` sort are both Disallowed. Say so in the module docstring rather
than shipping a `--days` flag that quietly re-crawls everything — a delta that
isn't one is worse than no delta, because it gets trusted and scheduled. Capture
the per-row publication date anyway (`datePosted`, `modified_date`) so at least
the diff is computable offline between two runs.

**5. Wire metering in the fetch wrapper.** Four lines, and every run reports its
own proxy bill instead of an estimate reconstructed afterwards (§6).

**6. `--audit` — the free one everybody skips.** After the crawl, bucket rows by
the location the *site* reported and compare against the expected shard list. A
shard with zero rows carrying its own name did not fail; it silently scraped
somewhere else. Costs no requests, and it is the only check that catches that.
Report field-fill percentages in the same pass — `price=100%` on a price-sorted
sweep, for instance, is not good news, it's the priceless listings missing.

```
✔ colima                 Colima                    297
✖ coahuila               Coahuila                    0     ← slug went elsewhere
unexpected provinces in the data (slug resolved elsewhere?):
  Baja California                                   412
```

**Report both numbers, always.** Unique rows *and* the site's advertised total,
per query and for the run. On Lamudi ~12% of served cards were cross-page
duplicates; on a price-sharded sweep the boundary listing repeats by design.
"3,377 advertised → 243 cards seen → 240 unique" is a healthy query; a single
number is an unfalsifiable claim.

---

## 12. Legal / ethical floor

Respect robots.txt intent and ToS. Never scrape auth-walled or PII data without
authorization. Rate-limit so the target never notices you in its metrics — which
is also, conveniently, how you avoid getting blocked.
