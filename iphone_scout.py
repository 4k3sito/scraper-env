"""iPhone 17 scout — Mercado Libre México.

Busca anuncios de la familia iPhone 17 y marca los que están por debajo del
precio de reventa típico de *su mismo* modelo y capacidad.

Cómo decide qué es "barato":

1. Clasifica cada anuncio por título: modelo (17, 17e, Air, 17 Pro, 17 Pro Max),
   capacidad (128 GB … 2 TB) y condición (nuevo / usado / reacondicionado).
2. Tira la basura que contamina el promedio: fundas, micas, cargadores, piezas,
   réplicas, equipos bloqueados por iCloud, "solo caja", etc.
3. Referencia de reventa = **mediana** de los anuncios usados + reacondicionados
   de ese (modelo, capacidad). Mediana y no media: un solo anuncio a $1 o a
   $99,999 mueve la media y no la mediana. Si un grupo tiene menos de
   `--min-sample` anuncios, no se puntúa (se reporta como "sin referencia").
4. Oferta = precio <= referencia × (1 − `--below`). Lo que cae por debajo de
   `--suspicious` × referencia se separa como **sospechoso**: casi siempre es
   fraude, equipo bloqueado o un accesorio mal clasificado.

Fuente: el listado público de Mercado Libre (página 1 por rango de precio, el
mismo esquema `_PriceRange_` que usa `mercadolibre_scraper.py`; sin `_Desde_` ni
`_OrderId_`, que robots.txt prohíbe). Si Mercado Libre responde con su muro de
"ingresa a tu cuenta", el script se detiene en vez de insistir: en ese caso
guarda las páginas del listado desde tu navegador (Ctrl+S) y analízalas con
`--from-html`.

    .venv/bin/python iphone_scout.py                          # buscar y reportar
    .venv/bin/python iphone_scout.py --model "17 pro" --below 0.20
    .venv/bin/python iphone_scout.py --from-html ~/Descargas/ml_*.html
    .venv/bin/python iphone_scout.py --watch 1800             # repetir cada 30 min, solo ofertas nuevas
    .venv/bin/python iphone_scout.py --selfcheck              # prueba offline del parser y la lógica
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

try:  # selectolax >= 1.0 retiró el backend Modest
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:
    from selectolax.parser import HTMLParser

BASE = "https://listado.mercadolibre.com.mx/celulares-telefonos/celulares-smartphones"
PER_PAGE = 48            # el listado sirve 48 tarjetas por página
PRICE_FLOOR = 3_000      # MXN; debajo de esto no hay iPhone 17 real
PRICE_CEIL = 90_000      # MXN; arriba de un 17 Pro Max 2 TB nuevo
MIN_SPAN = 200           # no partir rangos más finos que esto

# ---------------------------------------------------------------------------
# Clasificación por título
# ---------------------------------------------------------------------------
_JUNK = re.compile(
    r"\b(funda|case|carcasa|mica|protector|cristal templado|vidrio templado|"
    r"cargador|cable|adaptador|lente|camara para|soporte|correa|skin|sticker|"
    r"refacci[oó]n|repuesto|piezas?|pantalla|display|flex|bater[ií]a|tapa|"
    r"r[eé]plica|clon|1:1|tipo iphone|estilo iphone|similar|"
    r"icloud|bloquead[oa]|reportad[oa]|imei|para partes|no funciona|"
    r"solo caja|caja vac[ií]a|dummy|maqueta|"
    r"magsafe|airpods|apple watch|ipad|macbook|"
    r"renta|apartado|intercambio|cambio por)\b",
    re.I,
)
# "para iPhone 17" casi siempre es accesorio ("Funda para iPhone 17 Pro").
_FOR_IPHONE = re.compile(r"\b(para|compatible con|p/)\s+(el\s+)?iphone", re.I)
_IPHONE17 = re.compile(r"\biphone\s*17(?:e\b|\b)|\biphone\s*air\b", re.I)
_OTHER_GEN = re.compile(r"\biphone\s*(1[0-6]|[4-9]|x[rs]?|se)\b", re.I)

_STORAGE = re.compile(r"\b(64|128|256|512)\s*gb\b|\b([12])\s*tb\b", re.I)
_USED = re.compile(r"\b(usad[oa]|seminuev[oa]|semi\s*nuev[oa]|de uso|open\s*box|exhibici[oó]n)\b", re.I)
_REFURB = re.compile(r"\b(reacondicionad[oa]|refurbished|renewed|grado [abc]|grade [abc])\b", re.I)
_NEW = re.compile(r"\b(nuev[oa]|sellad[oa])\b", re.I)


def classify_model(title: str) -> str | None:
    t = title.lower()
    if not _IPHONE17.search(t) or _OTHER_GEN.search(t.replace("iphone 17", "")):
        return None
    if re.search(r"\bair\b", t):
        return "iPhone Air"
    if re.search(r"\b17\s*e\b|\b17e\b", t):
        return "iPhone 17e"
    if re.search(r"\bpro\s*max\b|\bpromax\b", t):
        return "iPhone 17 Pro Max"
    if re.search(r"\bpro\b", t):
        return "iPhone 17 Pro"
    return "iPhone 17"


def classify_storage(title: str) -> str | None:
    m = _STORAGE.search(title)
    if not m:
        return None
    return f"{m.group(1)} GB" if m.group(1) else f"{m.group(2)} TB"


def classify_condition(title: str, label: str = "") -> str:
    text = f"{label} {title}"
    if _REFURB.search(text):
        return "reacondicionado"
    if _USED.search(text):
        return "usado"
    if _NEW.search(label) or (not label and _NEW.search(title)):
        return "nuevo"
    # El listado de ML etiqueta usados/reacondicionados; sin etiqueta = nuevo.
    return "nuevo"


def is_junk(title: str) -> bool:
    return bool(_JUNK.search(title) or _FOR_IPHONE.search(title))


# ---------------------------------------------------------------------------
# Parseo del listado
# ---------------------------------------------------------------------------
_MLM = re.compile(r"MLM-?U?(\d+)", re.I)
_TOTAL = re.compile(r"([\d,\.]{1,9})\s*resultados")
_WALL = re.compile(r"account-verification|suspicious[_-]traffic|ingresa a<br>tu cuenta", re.I)


@dataclass
class Item:
    id: str
    title: str
    price: int
    url: str
    condition: str
    model: str | None
    storage: str | None
    seller: str = ""
    location: str = ""
    observed_at: str = ""


def _txt(node, *sels: str) -> str:
    for sel in sels:
        found = node.css_first(sel)
        if found and found.text(strip=True):
            return found.text(separator=" ", strip=True)
    return ""


def _price(card) -> int | None:
    """Precio actual (no el tachado). ML pinta el anterior en <s>/previous."""
    for sel in ("div.poly-price__current span.andes-money-amount__fraction",
                "span.andes-money-amount--cents-superscript span.andes-money-amount__fraction",
                "span.andes-money-amount__fraction"):
        for node in card.css(sel):
            parent = node.parent
            skip = False
            while parent is not None and parent.tag not in ("li", "body"):
                cls = parent.attributes.get("class") or ""
                if parent.tag == "s" or "previous" in cls or "original" in cls:
                    skip = True
                    break
                parent = parent.parent
            if skip:
                continue
            digits = re.sub(r"\D", "", node.text())
            if digits:
                return int(digits)
    return None


def parse_listing(html: str) -> list[Item]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out: list[Item] = []
    tree = HTMLParser(html)
    cards = tree.css("li.ui-search-layout__item") or tree.css("div.poly-card")
    for card in cards:
        link = (card.css_first("a.poly-component__title")
                or card.css_first("h3 a") or card.css_first("a.ui-search-link"))
        if not link:
            continue
        href = (link.attributes.get("href") or "").split("#")[0]
        m = _MLM.search(href)
        title = link.text(separator=" ", strip=True)
        price = _price(card)
        if not m or not title or not price:
            continue
        label = _txt(card, "span.poly-component__item-condition",
                     "span.poly-component__condition", "span.ui-search-item__group__element--condition")
        if not label:
            lm = re.search(r"\b(Usado|Reacondicionado)\b", card.text(separator=" "))
            label = lm.group(1) if lm else ""
        out.append(Item(
            id=f"MLM{m.group(1)}",
            title=title,
            price=price,
            url=href,
            condition=classify_condition(title, label),
            model=classify_model(title),
            storage=classify_storage(title),
            seller=_txt(card, "span.poly-component__seller"),
            location=_txt(card, "span.poly-component__location"),
            observed_at=now,
        ))
    return out


def total_results(html: str) -> int | None:
    m = _TOTAL.search(html)
    return int(re.sub(r"\D", "", m.group(1))) if m else None


# ---------------------------------------------------------------------------
# Descarga: barrido de precio, página 1 por rango
# ---------------------------------------------------------------------------
class LoginWall(RuntimeError):
    pass


def search_url(query: str, lo: int, hi: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
    return f"{BASE}/{slug}_PriceRange_{lo}-{hi}_NoIndex_True"


def crawl(query: str, lo: int, hi: int, min_gap: float) -> list[Item]:
    from stealth_scraper import Scraper  # import tardío: --from-html/--selfcheck no lo necesitan

    scraper = Scraper(min_gap=min_gap)
    seen: dict[str, Item] = {}
    stack = [(lo, hi)]
    requests = 0
    while stack:
        a, b = stack.pop()
        url = search_url(query, a, b)
        resp = scraper.get(url)
        requests += 1
        if _WALL.search(resp.url) or _WALL.search(resp.text[:20000]):
            raise LoginWall(url)
        items = parse_listing(resp.text)
        for it in items:
            seen.setdefault(it.id, it)
        total = total_results(resp.text) or len(items)
        print(f"  ${a:>6,}–${b:>6,}  {total:>5} resultados  {len(items):>2} tarjetas  "
              f"(acumulado {len(seen)})", file=sys.stderr)
        if total > PER_PAGE and b - a > MIN_SPAN:
            mid = (a + b) // 2
            stack += [(mid, b), (a, mid)]   # ambos extremos inclusivos: traslape, nunca hueco
    print(f"  {requests} peticiones, {len(seen)} anuncios únicos", file=sys.stderr)
    return list(seen.values())


# ---------------------------------------------------------------------------
# Análisis
# ---------------------------------------------------------------------------
def reference_prices(items: list[Item], min_sample: int) -> dict[tuple[str, str], dict]:
    groups: dict[tuple[str, str], list[int]] = {}
    for it in items:
        if it.condition != "nuevo" and it.model and it.storage and not is_junk(it.title):
            groups.setdefault((it.model, it.storage), []).append(it.price)
    ref = {}
    for key, prices in groups.items():
        if len(prices) < min_sample:
            continue
        prices.sort()
        cut = len(prices) // 10            # media recortada 10%: informativa
        trimmed = prices[cut:len(prices) - cut] or prices
        ref[key] = {
            "n": len(prices),
            "median": int(statistics.median(prices)),
            "mean_trim": int(statistics.fmean(trimmed)),
            "p25": int(prices[len(prices) // 4]),
        }
    return ref


def score(items, ref, below: float, suspicious: float, model_filter: str | None):
    deals, sus = [], []
    for it in items:
        if not it.model or not it.storage or is_junk(it.title):
            continue
        if model_filter and it.model.lower() != model_filter:
            continue
        r = ref.get((it.model, it.storage))
        if not r:
            continue
        ratio = it.price / r["median"]
        row = {**asdict(it), "ref_median": r["median"], "ref_n": r["n"],
               "discount_pct": round((1 - ratio) * 100, 1)}
        if ratio < suspicious:
            sus.append(row)
        elif ratio <= 1 - below:
            deals.append(row)
    key = lambda d: -d["discount_pct"]
    return sorted(deals, key=key), sorted(sus, key=key)


def report(items, ref, deals, sus, *, show_all_ref=True) -> None:
    clean = [i for i in items if i.model and i.storage and not is_junk(i.title)]
    print(f"\n{len(items)} anuncios leídos · {len(clean)} iPhone 17 válidos "
          f"(sin accesorios/piezas/bloqueados)\n")
    if show_all_ref:
        print("Precio de reventa de referencia (usado + reacondicionado):")
        print(f"  {'modelo':<20}{'capacidad':<10}{'n':>4}{'mediana':>11}{'media rec.':>12}{'p25':>10}")
        order = ["iPhone 17e", "iPhone 17", "iPhone Air", "iPhone 17 Pro", "iPhone 17 Pro Max"]
        cap = lambda s: int(s.split()[0]) * (1024 if "TB" in s else 1)
        for (m, s), r in sorted(ref.items(), key=lambda kv: (order.index(kv[0][0]), cap(kv[0][1]))):
            print(f"  {m:<20}{s:<10}{r['n']:>4}{r['median']:>11,}{r['mean_trim']:>12,}{r['p25']:>10,}")
        if not ref:
            print("  (ningún grupo alcanzó la muestra mínima)")
    print(f"\n🔥 {len(deals)} ofertas por debajo de la referencia:")
    for d in deals:
        print(f"  -{d['discount_pct']:>4.1f}%  ${d['price']:>7,} vs ${d['ref_median']:>7,}  "
              f"{d['model']} {d['storage']} [{d['condition']}]\n           {d['title'][:80]}\n           {d['url']}")
    if sus:
        print(f"\n⚠️  {len(sus)} demasiado baratos (revisa con cuidado: posible fraude/bloqueo):")
        for d in sus:
            print(f"  -{d['discount_pct']:>4.1f}%  ${d['price']:>7,}  {d['model']} {d['storage']}  {d['url']}")


def save(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Selfcheck offline
# ---------------------------------------------------------------------------
def _card(i: int, title: str, price: int, cond: str = "") -> str:
    label = f'<span class="poly-component__item-condition">{cond}</span>' if cond else ""
    return (f'<li class="ui-search-layout__item"><div class="poly-card">{label}'
            f'<h3><a class="poly-component__title" href="https://articulo.mercadolibre.com.mx/MLM-{1000 + i}-x-_JM">{title}</a></h3>'
            f'<div class="poly-price__current"><span class="andes-money-amount__fraction">{price:,}</span></div>'
            f'<s><span class="andes-money-amount__fraction">99,999</span></s></div></li>')


def _selfcheck() -> None:
    assert classify_model("Apple iPhone 17 Pro Max 256 GB Azul") == "iPhone 17 Pro Max"
    assert classify_model("iPhone 17 Pro 512gb") == "iPhone 17 Pro"
    assert classify_model("Apple iPhone Air 256 GB") == "iPhone Air"
    assert classify_model("iPhone 17e 128 GB") == "iPhone 17e"
    assert classify_model("iPhone 17 256GB Lavanda") == "iPhone 17"
    assert classify_model("iPhone 16 Pro 256GB") is None
    assert classify_storage("iPhone 17 Pro Max 1TB") == "1 TB"
    assert classify_storage("iPhone 17 256 gb") == "256 GB"
    assert is_junk("Funda Para iPhone 17 Pro Max Silicón")
    assert is_junk("iPhone 17 Pro 256GB bloqueado por iCloud")
    assert not is_junk("Apple iPhone 17 Pro 256 GB Plata Usado")
    assert classify_condition("iPhone 17 Pro", "Reacondicionado") == "reacondicionado"
    assert classify_condition("iPhone 17 Pro seminuevo 256gb") == "usado"
    assert classify_condition("Apple iPhone 17 Pro 256 GB") == "nuevo"

    used = [21500, 22000, 22500, 23000, 23000, 23500, 24000, 25000]
    cards = [_card(i, f"Apple iPhone 17 Pro 256 GB Plata", p, "Usado") for i, p in enumerate(used)]
    cards += [_card(50, "Apple iPhone 17 Pro 256 GB Azul", 18900, "Usado"),     # oferta (-17%)
              _card(51, "iPhone 17 Pro 256gb sellado nuevo", 19000),            # nuevo bajo la ref
              _card(52, "Apple iPhone 17 Pro 256 GB", 9000, "Usado"),           # sospechoso
              _card(53, "Funda Para iPhone 17 Pro 256 GB", 300),                 # basura
              _card(54, "Apple iPhone 16 Pro 256 GB", 15000, "Usado")]           # otra generación
    html = f"<html><body>1,234 resultados<ol>{''.join(cards)}</ol></body></html>"
    items = parse_listing(html)
    assert len(items) == 13, len(items)
    assert total_results(html) == 1234
    assert all(i.price != 99999 for i in items), "tomó el precio tachado"
    ref = reference_prices(items, min_sample=5)
    r = ref[("iPhone 17 Pro", "256 GB")]
    assert r["median"] == 22750 and r["n"] == 10, r    # la funda y el 16 Pro no entran; el outlier de $9k no la mueve
    deals, sus = score(items, ref, below=0.15, suspicious=0.5, model_filter=None)
    assert [d["id"] for d in deals] == ["MLM1050", "MLM1051"], deals
    assert [d["id"] for d in sus] == ["MLM1052"], sus
    assert search_url("iPhone 17", 10000, 20000).endswith("/iphone-17_PriceRange_10000-20000_NoIndex_True")
    assert _WALL.search("https://www.mercadolibre.com.mx/gz/account-verification?go=x")
    print("OK iphone_scout selfcheck")


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", default="iphone 17")
    ap.add_argument("--model", help='filtra ofertas: "17", "17e", "air", "17 pro", "17 pro max"')
    ap.add_argument("--below", type=float, default=0.15, help="descuento mínimo vs mediana (0.15 = 15%%)")
    ap.add_argument("--suspicious", type=float, default=0.55,
                    help="debajo de esta fracción de la mediana = sospechoso")
    ap.add_argument("--min-sample", type=int, default=5, help="anuncios usados mínimos por grupo")
    ap.add_argument("--min-price", type=int, default=PRICE_FLOOR)
    ap.add_argument("--max-price", type=int, default=PRICE_CEIL)
    ap.add_argument("--min-gap", type=float, default=5.0, help="segundos entre peticiones (Crawl-delay)")
    ap.add_argument("--from-html", nargs="+", type=Path, help="analiza páginas guardadas en vez de descargar")
    ap.add_argument("--out", type=Path, default=Path("data/iphone17"), help="prefijo de salida")
    ap.add_argument("--watch", type=int, metavar="SEG", help="repite cada SEG segundos y avisa solo de lo nuevo")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        _selfcheck()
        return

    model_filter = None
    if args.model:
        m = args.model.lower().replace("iphone", "").strip()
        model_filter = "iphone air" if m == "air" else f"iphone {m}"

    alerted: set[str] = set()
    seen_path = args.out.with_name(args.out.name + ".alerted")
    if args.watch and seen_path.exists():
        alerted = set(seen_path.read_text().split())

    while True:
        if args.from_html:
            items = {}
            for p in args.from_html:
                for it in parse_listing(p.read_text(encoding="utf-8", errors="replace")):
                    items.setdefault(it.id, it)
            items = list(items.values())
        else:
            print(f"Buscando '{args.query}' en Mercado Libre (${args.min_price:,}–${args.max_price:,})…",
                  file=sys.stderr)
            try:
                items = crawl(args.query, args.min_price, args.max_price, args.min_gap)
            except LoginWall as wall:
                sys.exit(f"\nMercado Libre pidió iniciar sesión ({wall}).\n"
                         "No se insiste: abre el listado en tu navegador, guarda cada página "
                         "(Ctrl+S) y corre:\n  .venv/bin/python iphone_scout.py --from-html página1.html página2.html …")

        ref = reference_prices(items, args.min_sample)
        deals, sus = score(items, ref, args.below, args.suspicious, model_filter)

        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        save([asdict(i) for i in items], args.out.with_name(f"{args.out.name}_{stamp}_all.csv"))
        save(deals, args.out.with_name(f"{args.out.name}_{stamp}_deals.csv"))
        (args.out.with_name(f"{args.out.name}_{stamp}_ref.json")
         .write_text(json.dumps({f"{m} {s}": r for (m, s), r in ref.items()}, ensure_ascii=False, indent=1)))

        if not args.watch:
            report(items, ref, deals, sus)
            print(f"\nCSV en {args.out.parent}/ ({args.out.name}_{stamp}_*.csv)")
            return

        fresh = [d for d in deals if d["id"] not in alerted]
        if fresh:
            report(items, ref, fresh, [], show_all_ref=False)
            alerted |= {d["id"] for d in fresh}
            seen_path.parent.mkdir(parents=True, exist_ok=True)
            seen_path.write_text("\n".join(sorted(alerted)))
        else:
            print(f"[{stamp}] sin ofertas nuevas ({len(deals)} ya avisadas)", file=sys.stderr)
        if args.from_html:
            return
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
