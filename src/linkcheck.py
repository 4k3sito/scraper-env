"""Fase 3 (mantenimiento): revisa listings is_active=true no vistos hace mas
de N dias y los desactiva si el link ya no sirve.

Liviano a proposito: usa el Fetcher de Scrapling (peticion HTTP directa, sin
navegador) en vez de Crawlee/Playwright — no hay paginacion ni sesion que
mantener, solo N peticiones independientes en paralelo.

Uso:
    python -m src.linkcheck                       # todo, stale > 7 dias
    python -m src.linkcheck --stale-days 3
    python -m src.linkcheck --source pincali
"""
import argparse
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from scrapling.fetchers import Fetcher

from src import db
from src.proxy import ApifyProxyConfig

DEAD_TEXT_PATTERNS = re.compile(
    r"propiedad\s+no\s+disponible|anuncio\s+pausado|anuncio\s+no\s+encontrado|"
    r"p[aá]gina\s+no\s+encontrada|ya\s+no\s+est[aá]\s+disponible|"
    r"esta\s+propiedad\s+ya\s+no|contenido\s+no\s+disponible",
    re.I,
)


def _redirected_to_homepage(original: str, final: str) -> bool:
    from urllib.parse import urlparse
    o, f = urlparse(original), urlparse(final)
    return o.netloc == f.netloc and f.path in ("", "/") and o.path not in ("", "/")


def is_alive(url: str) -> bool:
    """True si el link sigue sirviendo la propiedad; False si esta muerto."""
    proxy = ApifyProxyConfig(groups="RESIDENTIAL")
    try:
        # http3 no funciona sobre un proxy HTTP (CONNECT no soporta QUIC) —
        # impersonate (TLS fingerprint) es lo que importa aqui, y el proxy
        # es innegociable dado todo lo que vimos este sesion sobre bloqueos.
        page = Fetcher.get(
            url,
            impersonate="chrome131",
            retries=2,
            retry_delay=2,
            timeout=15,
            follow_redirects=True,
            proxy=proxy.url(session=ApifyProxyConfig.new_session_id()),
        )
    except Exception:
        return False
    if page.status >= 400:
        return False
    if _redirected_to_homepage(url, page.url):
        return False
    if DEAD_TEXT_PATTERNS.search(page.get_all_text() or ""):
        return False
    return True


def run(stale_days: int, source: str | None = None, workers: int = 8) -> int:
    stale = db.get_stale_active_urls(stale_days, source)
    if not stale:
        print(f"[linkcheck] nada que revisar (0 activos sin ver hace {stale_days}+ dias)")
        return 0

    dead_by_source = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(is_alive, row["url"]): row for row in stale}
        for fut in as_completed(futures):
            row = futures[fut]
            if not fut.result():
                dead_by_source.setdefault(row["source"], []).append(row["external_id"])

    n_dead = 0
    for src, ext_ids in dead_by_source.items():
        n_dead += db.mark_inactive(src, ext_ids)
    print(f"[linkcheck] {n_dead}/{len(stale)} links muertos desactivados")
    return n_dead


def _selftest():
    assert _redirected_to_homepage("https://x.com/inmueble/123", "https://x.com/") is True
    assert _redirected_to_homepage("https://x.com/inmueble/123", "https://x.com/inmueble/123") is False
    assert DEAD_TEXT_PATTERNS.search("Esta Propiedad ya no esta disponible")
    assert not DEAD_TEXT_PATTERNS.search("Local comercial en renta, 85 m2")
    print("ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-days", type=int, default=7)
    ap.add_argument("--source", default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
    else:
        run(args.stale_days, args.source, args.workers)
