"""Two-tier stealth scraper.

Tier 1 (cheap): curl_cffi — impersonates Chrome's TLS/JA4 handshake. Use for
    JSON APIs and server-rendered HTML behind TLS-fingerprint gates.
Tier 2 (expensive): camoufox — engine-patched Firefox for JS-fingerprint /
    behavioral targets (Cloudflare, DataDome, Kasada).

The hard part of evasion (canvas/WebGL/audio/font/WebRTC/TLS spoofing) lives
inside those libraries, not here. This file only adds proxy rotation, retries
with jitter, and tier selection.

    pip install curl_cffi camoufox[geoip]
    camoufox fetch          # one-time: download the patched Firefox build
    PROXIES="http://user:pass@ip1:port,http://user:pass@ip2:port" python stealth_scraper.py
"""

from __future__ import annotations

import itertools
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from curl_cffi import requests as cffi

# Real Chrome builds curl_cffi can mimic. Rotate so you don't pin one JA4.
_IMPERSONATE = ["chrome131", "chrome124", "chrome120"]


def _apify() -> str | None:
    """Apify residential proxy from .env, as a template. `{session}` makes the
    exit IP sticky for a whole session — Apify rotates per request without it,
    which would hand the target a new IP mid-cookie-jar. Datacenter groups are
    not offered here on purpose: Lamudi 403s them outright, only residential
    clears the gate."""
    env = {}
    try:
        for line in Path(".env").read_text().splitlines():
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    except OSError:
        return None
    if not env.get("PASSWORD"):
        return None
    country = os.environ.get("PROXY_COUNTRY", "MX")
    return (f"http://groups-RESIDENTIAL,country-{country},session-{{session}}"
            f":{env['PASSWORD']}@proxy.apify.com:8000")


def _proxies() -> list[str | None]:
    raw = os.environ.get("PROXIES", "").strip()
    proxies: list[str | None] = [p.strip() for p in raw.split(",") if p.strip()]
    return proxies or [_apify()]  # [None] => direct connection


@dataclass
class Scraper:
    max_retries: int = 4
    base_delay: float = 1.5           # backoff base, seconds
    min_gap: float = 0.8              # polite floor between requests
    _pool: list[str | None] = field(default_factory=_proxies)
    _last_request: float = 0.0

    def __post_init__(self) -> None:
        random.shuffle(self._pool)
        self._rotator = itertools.cycle(self._pool)
        # Persistent session: cookies survive across requests (needed so an
        # anti-bot clearance cookie sticks). One fixed TLS shape + proxy per
        # session — rotating either mid-session is itself a detection signal.
        self.rotate()

    def rotate(self) -> None:
        """New identity: fresh cookies, next proxy, new TLS shape. Call when an
        IP gets hard-blocked and clearance can't be recovered."""
        self._imp = random.choice(_IMPERSONATE)
        proxy = next(self._rotator)
        # A `{session}` template (Apify) means "give me a sticky exit IP" — a new
        # id here is what actually changes the IP, so it belongs in rotate().
        self._proxy = (proxy.format(session=random.randbytes(6).hex())
                       if proxy and "{session}" in proxy else proxy)
        self.sess = cffi.Session()

    def _pace(self) -> None:
        """Explicit wait, never a bare sleep-per-request: jittered floor."""
        elapsed = time.monotonic() - self._last_request
        gap = self.min_gap + random.uniform(0, 0.6)
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_request = time.monotonic()

    # ---- Tier 1: HTTP + TLS impersonation --------------------------------
    def get(self, url: str, **kw) -> cffi.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._pace()
            try:
                resp = self.sess.get(
                    url,
                    impersonate=self._imp,
                    proxies={"http": self._proxy, "https": self._proxy} if self._proxy else None,
                    timeout=30,
                    **kw,
                )
                # 403/429/503 => the gate flagged us; rotate and back off.
                if resp.status_code in (403, 429, 503):
                    raise cffi.RequestsError(f"blocked: {resp.status_code}")
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001 - retry on any transport/gate error
                last_exc = exc
                # A dead sticky exit IP ("CONNECT tunnel failed") can't be waited
                # out — every retry rides the same dead tunnel and the whole query
                # dies. Spend the first half of the budget on the current identity
                # (backoff fixes rate-limits), then rotate for the rest.
                if attempt == self.max_retries // 2 - 1:
                    self.rotate()
                time.sleep(self.base_delay * 2**attempt + random.uniform(0, 1))
        raise RuntimeError(f"tier-1 failed after {self.max_retries} tries: {last_exc}")

    # ---- Tier 2: full browser for JS / behavioral gates ------------------
    def render(self, url: str, wait_selector: str | None = None) -> str:
        """Lazy import: don't pay the camoufox cost unless a target needs it."""
        from camoufox.sync_api import Camoufox

        proxy = next(self._rotator)
        with Camoufox(
            headless=True,
            humanize=True,                       # human-shaped cursor motion
            geoip=bool(proxy),                   # align locale/timezone to proxy exit
            proxy={"server": proxy} if proxy else None,
        ) as browser:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=30_000)
            return page.content()


def _selfcheck() -> None:
    """Fails loudly if tier-1 TLS impersonation regresses."""
    s = Scraper()
    r = s.get("https://tls.peet.ws/api/all")  # echoes the JA4 it sees
    data = r.json()
    ja4 = data.get("tls", {}).get("ja4", "")
    assert ja4, f"no JA4 in response: {data}"
    # A real Chrome JA4 starts with 't13d' (TLS1.3). Python's stock stack would not.
    assert ja4.startswith("t13d"), f"impersonation looks off, JA4={ja4}"
    print(f"OK  tier-1 JA4={ja4}  proxies={len(s._pool)}")


if __name__ == "__main__":
    _selfcheck()
