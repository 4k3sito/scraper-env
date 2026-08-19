"""Mercado Libre — coordenadas desde el detail page, con sesión autenticada.

El SERP de `mercadolibre_scraper.py` no trae lat/lng; el detail page sí
(`"latitude"`/`"longitude"`, 7 decimales, en el HTML SSR). Llegar ahí es el
problema: ML sirve un proof-of-work a IPs limpias, aguanta ~3 páginas y después
exige cuenta. Este script usa esa cuenta.

Credenciales: **nunca pasan por el código**. El primer run abre Chrome headful,
tú te logueas a mano, y la sesión queda en un perfil persistente fuera del repo
(`~/.cache/ml-scraper-profile`). Los runs siguientes lo reutilizan.

El barrido va por `fetch()` DENTRO de la página, no navegando. Sale por la pila
de red de Chrome —mismo TLS, mismas cookies, mismo todo— pero sin renderizar ni
bajar subrecursos: 0.6 s por anuncio contra los 7 s de una navegación completa.
Handoff a curl_cffi ya no existe: el 2026-08-08 medí que ML lo topa con PoW y
muro aunque le repliques VERBATIM los 16 headers de Chrome y su cookie de 3.6 KB.
El discriminador está debajo de HTTP y no se pelea; se usa el navegador.

`fetch` es same-origin, y el corpus vive en dos hosts (terreno.* e inmueble.*),
así que el barrido va por host, anclado en un anuncio de ese host. Y hace falta
`bypass_csp`: el `connect-src` de ML bloquea el fetch entre documentos.

    .venv/bin/python ml_geo.py --login          # una vez, a mano
    .venv/bin/python ml_geo.py --limit 30       # piloto
    .venv/bin/python ml_geo.py --limit 0        # todo lo pendiente
    .venv/bin/python ml_geo.py --validate       # marcar coordenadas de relleno

`--min-gap` es piso de cortesía nuestro, no directiva: el `Crawl-delay: 5` de
robots.txt vive en el bloque de Bingbot y el de `*` no tiene ninguno.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import random
import re
import statistics
import sys
import time
import unicodedata

PROFILE = pathlib.Path.home() / ".cache" / "ml-scraper-profile"
SRC = pathlib.Path("data/mercadolibre.jsonl")
LAT = re.compile(r'"latitude"\s*:\s*"?(-?\d+\.\d+)')
LNG = re.compile(r'"longitude"\s*:\s*"?(-?\d+\.\d+)')
LOGIN_WALL = "ingresa a tu cuenta"
# ponytail: paro el run al 3er gate seguido en vez de reintentar — si la sesión
# cayó, insistir sólo acelera el baneo. Subir si el piloto muestra gates aislados.
MAX_CONSECUTIVE_GATES = 3
MAX_REMINTS = 5          # tope de re-acuñadas por run; más que eso no es la cookie
# Un detail page real pesa 400-640 KB; los muros pesan 11-25 KB. Cualquier cosa
# por debajo de esto no es un anuncio, diga lo que diga su cuerpo.
REAL_PAGE_BYTES = 100_000
# Cortacircuitos: si en la ventana reciente no hubo un solo `ok`, para — sin
# depender de que yo haya enumerado bien las páginas de bloqueo de ML. Esa
# suposición es justo la que falló a las 5,000.
WINDOW = 40


def rows_needing_coords(limit: int | None, done: set[str]) -> list[tuple[str, str]]:
    """(listingId, url) de mercadolibre.jsonl que aún no tienen coordenada."""
    out = []
    for line in SRC.open():
        d = json.loads(line)
        if d.get("coordinates") or d["listingId"] in done:
            continue
        out.append((d["listingId"], d["url"]))
    random.shuffle(out)          # no barrer en orden de corpus: es un patrón obvio
    return out[:limit] if limit else out


def load_done(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(l)["listingId"] for l in path.open()}


def verdict(n_bytes: int, coords: tuple[float, float] | None,
            wall: bool, pow_: bool) -> tuple[str, tuple[float, float] | None]:
    """Cuatro veredictos. El default es `desconocido`, NO `sin-coords`: en la
    corrida de 5,000 el muro de login traía `ingresa a<br/>tu cuenta`, la
    etiqueta partía la frase, la busqué sobre el HTML crudo y no empató. Cayó al
    default, que entonces era benigno y reseteaba el contador de gates — el run
    siguió 3,824 requests contra un cliente ya bloqueado. Lo desconocido se trata
    como gate; sólo una página de anuncio real y completa cuenta como benigna.

    Se llama desde Python (`classify`) y desde el JS del barrido, que manda estas
    cuatro señales en vez del HTML entero — 485 KB por anuncio no cruzan el puente.
    """
    if coords:
        return "ok", coords
    if wall:
        return "login", None
    if pow_:
        return "pow", None
    if n_bytes >= REAL_PAGE_BYTES:
        return "sin-coords", None      # anuncio real que no publica ubicación
    return "desconocido", None         # página chica no reconocida: sospechosa


def classify(html: str) -> tuple[str, tuple[float, float] | None]:
    la, ln = LAT.search(html), LNG.search(html)
    # sin etiquetas: los muros parten frases con <br/>, y el crudo no empata
    text = " ".join(re.sub(r"<[^>]+>", " ", html).lower().split())
    return verdict(len(html),
                   (float(la.group(1)), float(ln.group(1))) if la and ln else None,
                   LOGIN_WALL in text,
                   "bot_challenge" in html or "_bmc" in html)


def open_context(pw, headless: bool, bypass_csp: bool = False):
    # `bypass_csp` sólo lo necesita el barrido (el connect-src de ML mata el
    # fetch entre documentos). El login corre con el CSP intacto: ahí ML monta
    # su flujo de verificación y no hay por qué alterarle la página.
    PROFILE.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=headless, bypass_csp=bypass_csp,
        args=["--no-sandbox"], locale="es-MX", timezone_id="America/Mexico_City")


SESSION_COOKIES = ("orguseridp", "ssid", "orgnickp")


def logged_in(ctx) -> str | None:
    """El nickname si la sesión está viva. Se pregunta al HEADER, no a las
    cookies: cuando ML te desloguea a la fuerza deja las cookies puestas
    (vencen en 2027) y sólo el header dice la verdad."""
    page = ctx.new_page()
    try:
        page.goto("https://www.mercadolibre.com.mx/", wait_until="domcontentloaded",
                  timeout=60_000)
        page.wait_for_timeout(3000)
        el = page.query_selector(".nav-header-user, .nav-header-username")
        return el.inner_text().strip() if el else None
    finally:
        page.close()


def do_login() -> None:
    """Abre ML y espera a que TÚ te loguees. El script no ve las credenciales."""
    from patchright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = open_context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.mercadolibre.com.mx/", timeout=60_000)
        # La presencia de las cookies NO sirve como señal: siguen ahí después del
        # deslogueo forzado, y esa comprobación daba "sesión detectada" al
        # instante sin que nadie se hubiera logueado. Espero a que ROTEN.
        before = {c["name"]: c["value"] for c in ctx.cookies()
                  if c["name"] in SESSION_COOKIES}
        print("Chrome abierto. Inicia sesión a mano; detecto la cookie solo.")
        print("(Ctrl-C para abortar)")
        for _ in range(120):                      # 10 min de margen
            now = {c["name"]: c["value"] for c in ctx.cookies()
                   if c["name"] in SESSION_COOKIES}
            if now and now != before:
                who = logged_in(ctx)
                if who:
                    print(f"\nSesión viva como «{who}». Perfil guardado en {PROFILE}")
                    break
                before = now                      # rotó pero aún no entra; sigo esperando
            time.sleep(5)
        else:
            print("\nNo detecté sesión. Corre --login otra vez.", file=sys.stderr)
        ctx.close()


# El barrido entero vive aquí. Devuelve las cuatro señales de `verdict()`, nunca
# el HTML: son 485 KB por anuncio y cruzar eso por el puente CDP cuesta más que
# la request. El gap va adentro para no pagar un round-trip por anuncio.
FETCH_JS = """async ([urls, gap]) => {
  const out = [];
  for (const u of urls) {
    try {
      // por path, no por URL absoluta: ML redirige a un host canónico, y contra
      // ese origen la URL del corpus sale cross-origin y CORS la mata.
      const p = new URL(u);
      const r = await fetch(p.pathname + p.search, {credentials: 'include'});
      const html = await r.text();
      const la = html.match(/"latitude"\\s*:\\s*"?(-?\\d+\\.\\d+)/);
      const ln = html.match(/"longitude"\\s*:\\s*"?(-?\\d+\\.\\d+)/);
      const text = html.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').toLowerCase();
      out.push({n: html.length,
                lat: la ? parseFloat(la[1]) : null, lng: ln ? parseFloat(ln[1]) : null,
                wall: text.includes('__WALL__'),
                pow: html.includes('bot_challenge') || html.includes('_bmc')});
    } catch (e) { out.push({n: 0, err: String(e).slice(0, 80)}); }
    await new Promise(k => setTimeout(k, gap + Math.random() * gap * 0.4));
  }
  return out;
}""".replace("__WALL__", LOGIN_WALL)

BATCH = 25          # anuncios por viaje al browser; ~1 min de trabajo por llamada
ANCHOR_TRIES = 5    # anclas candidatas antes de declarar muerta la sesión


def _host(url: str) -> str:
    return url.split("/")[2]


def crawl(limit: int | None, out_path: pathlib.Path, min_gap: float) -> None:
    if not PROFILE.exists():
        sys.exit("no hay perfil: corre primero  .venv/bin/python ml_geo.py --login")

    done = load_done(out_path)
    targets = rows_needing_coords(limit, done)
    if not targets:
        sys.exit("nada pendiente")

    # `fetch` es same-origin: hay que anclarse en un anuncio del mismo host.
    by_host: dict[str, list[tuple[str, str]]] = {}
    for lid, url in targets:
        by_host.setdefault(_host(url), []).append((lid, url))
    print(f"{len(targets)} pendientes · gap {min_gap}s · "
          + " ".join(f"{h}:{len(v)}" for h, v in by_host.items()))

    from patchright.sync_api import sync_playwright

    stats: collections.Counter[str] = collections.Counter()
    recent: collections.deque[str] = collections.deque(maxlen=WINDOW)
    consecutive_gates = 0
    remints = 0
    t0 = time.monotonic()
    wire = 0
    i = 0
    stop = False

    with sync_playwright() as pw, out_path.open("a") as sink:
        ctx = open_context(pw, headless=False, bypass_csp=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(300_000)

        def sweep(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
            """Ancla en el primer anuncio del lote y baja el resto por fetch.
            Devuelve los que salieron cross-origin, para reintentarlos anclados
            en uno de ELLOS."""
            nonlocal i, wire, consecutive_gates, remints, stop
            # Varias candidatas: un anuncio dado de baja redirige a www con muro,
            # y con una sola ancla eso mataba el run con la sesión intacta.
            for cand in rows[:ANCHOR_TRIES]:
                page.goto(cand[1], wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(3000)
                anchor, _ = classify(page.content())
                print(f"\n-- ancla {anchor} en {_host(page.url)} · {len(rows)} anuncios",
                      flush=True)
                if anchor == "ok":
                    break
            else:
                print(f"   {ANCHOR_TRIES} anclas seguidas sin abrir: la sesión no pasa "
                      f"ni navegando")
                stop = True
                return []

            crossed: list[tuple[str, str]] = []
            for b in range(0, len(rows), BATCH):
                chunk = rows[b:b + BATCH]
                res = page.evaluate(FETCH_JS, [[u for _, u in chunk], min_gap * 1000])
                for (lid, url), d in zip(chunk, res):
                    i += 1
                    wire += d["n"]
                    if d.get("err"):
                        # CORS, no bloqueo: el anuncio vive en el otro host. No
                        # toca el contador de gates — confundirlos paró el run
                        # de las 100 en la #37 con la sesión perfectamente sana.
                        stats["otro-host"] += 1
                        crossed.append((lid, url))
                        recent.append("otro-host")
                        continue
                    co = (d["lat"], d["lng"]) if d["lat"] and d["lng"] else None
                    v, coords = verdict(d["n"], co, d["wall"], d["pow"])
                    stats[v] += 1

                    if v == "ok":
                        sink.write(json.dumps({"listingId": lid,
                                               "coordinates": {"lat": coords[0], "lng": coords[1]},
                                               "source": "detail-auth"}) + "\n")
                        consecutive_gates = 0
                    elif v in ("login", "pow", "desconocido"):
                        consecutive_gates += 1
                        print(f"  [{i}] {lid} GATE={v} ({consecutive_gates} seguidos)")
                        # Un gate aislado suele ser la página, no un bloqueo: vale
                        # re-anclar. Varios seguidos ya es la sesión.
                        if consecutive_gates == 1 and remints < MAX_REMINTS:
                            remints += 1
                            print(f"      re-anclando (#{remints})")
                            page.goto(chunk[0][1], wait_until="domcontentloaded", timeout=60_000)
                            page.wait_for_timeout(3000)
                        elif consecutive_gates >= MAX_CONSECUTIVE_GATES:
                            print(f"\nParando: {consecutive_gates} gates seguidos en la #{i}.")
                            stop = True
                    else:
                        consecutive_gates = 0

                    recent.append(v)
                    if len(recent) == WINDOW and "ok" not in recent:
                        print(f"\nParando: {WINDOW} anuncios seguidos sin un solo dato "
                              f"(#{i}). Últimos veredictos: {collections.Counter(recent)}")
                        stop = True
                    if stop:
                        return crossed

                sink.flush()
                if (b // BATCH) % 4 == 0:
                    print(f"  [{i}] {dict(stats)} · {(time.monotonic()-t0)/max(i,1):.1f}s/anuncio",
                          flush=True)
            return crossed

        work = list(by_host.values())
        while work and not stop:
            rows = work.pop(0)
            crossed = sweep(rows)
            # Sólo se reencola si hubo avance; si el lote entero salió cross-origin
            # es que el ancla estaba del lado equivocado y reintentar es un bucle.
            if crossed and len(crossed) < len(rows):
                work.append(crossed)
        ctx.close()

    el = time.monotonic() - t0
    n = sum(stats.values())
    # `wire` es HTML ya descomprimido. ML sirve brotli (~23% con gzip como cota
    # superior), así que el dato de red es ~1/4.
    net = wire * 0.23
    print(f"\n{dict(stats)} · {n} anuncios · {el:.0f}s · {el/max(n,1):.1f}s/anuncio · "
          f"{wire/max(n,1)/1024:.0f}KB HTML (~{net/max(n,1)/1024:.0f}KB de red) · "
          f"{remints} re-anclajes")
    if stats["ok"]:
        pend = len(rows_needing_coords(None, load_done(out_path)))
        print(f"éxito {100*stats['ok']/n:.0f}% · faltan {pend} · "
              f"proyección {pend*el/max(n,1)/3600:.1f} h · ~{pend*net/max(n,1)/1e9:.1f} GB de red")
        print("corre  --validate  antes de usar: ~18% son coordenadas de relleno")


# ML nombra a CDMX "Distrito Federal"; los otros cuatro portales "Ciudad de
# México". Sin esto la llave (ciudad, provincia) falla en las 1,185 filas de CDMX
# y salen todas como no verificables.
_PROV_ALIAS = {"distrito federal": "ciudad de mexico", "cdmx": "ciudad de mexico",
               "estado de mexico": "mexico", "edomex": "mexico",
               "edo. de mexico": "mexico", "edo de mexico": "mexico"}


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    out = " ".join("".join(c for c in s if not unicodedata.combining(c))
                   .replace(",", " ").split())
    return _PROV_ALIAS.get(out, out)


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((a[0] - b[0]) * 111,
                      (a[1] - b[1]) * 100 * math.cos(math.radians(a[0])))


def validate(out_path: pathlib.Path, max_km: float = 40.0, min_states: int = 2) -> None:
    """Marca coordenadas de relleno. ML sirve un punto por defecto cuando el
    anuncio no publica ubicación — en el piloto, 19.39068,-99.283699 salió en 4
    anuncios de estados distintos. Dos señales, porque ninguna sola basta:

      lejos  → a más de `max_km` del centroide de su ciudad (caza las absurdas)
      repe   → la misma coordenada declarada en >=`min_states` estados distintos

    `repe` cuenta estados, no repeticiones. Con 1,260 filas medidas la separación
    es tajante: el relleno salió 107 veces en 19 estados, y TODA otra coordenada
    repetida (2-5 veces) cayó dentro de un solo estado — locales de una misma
    plaza, lotes de un mismo fraccionamiento. Un umbral por conteo marcaba esas
    35 filas buenas; contar estados no necesita calibrarse cuando crezca el corpus.

    Marca, no borra: escribe `<out>.validated.jsonl` con un campo `suspect`.
    """
    rows = [json.loads(l) for l in out_path.open()]
    src = {}
    for line in SRC.open():
        d = json.loads(line)
        src[d["listingId"]] = d

    # centroides de ciudad donados por los corpus que sí traen coordenadas
    cent: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for f in ("inmuebles24", "lamudi", "pincali", "vivanuncios"):
        p = SRC.parent / f"{f}.jsonl"
        if not p.exists():
            continue
        for line in p.open():
            d = json.loads(line)
            c = d.get("coordinates")
            if c and d.get("city"):
                cent.setdefault((_norm(d["city"]), _norm(d.get("province"))), []).append(
                    (c["lat"], c["lng"]))
    med = {k: (statistics.median(p[0] for p in v), statistics.median(p[1] for p in v))
           for k, v in cent.items() if len(v) >= 5}
    # Respaldo por ciudad sola, sólo cuando ese nombre existe en un único estado
    # ("Cancún/Benito Juárez" no empata con "Cancún", pero hay un solo Cancún).
    # Contar llaves no sirve para decidir si un nombre es ambiguo: lamudi no trae
    # provincia, así que "Naucalpan de Juárez" existe bajo ('...','mexico') y
    # ('...',''), y parecerían dos municipios homónimos. Lo que decide es si los
    # centroides caen en el mismo lugar.
    by_city: dict[str, list[tuple[str, str]]] = {}
    for k in med:
        by_city.setdefault(k[0], []).append(k)
    unique_city = {}
    for c, ks in by_city.items():
        pts = [med[k] for k in ks]
        if all(_km(pts[0], p) <= max_km for p in pts):
            unique_city[c] = (statistics.median(p[0] for p in pts),
                              statistics.median(p[1] for p in pts))

    def centroid_for(city: str, prov: str) -> tuple[float, float] | None:
        """Exacta, luego ciudad sola, luego prefijo. ML abrevia lo que los otros
        portales escriben completo ("Naucalpan" vs "Naucalpan de Juárez"); sin el
        prefijo eso sale como no verificable, ~3% de falsas alarmas. Exijo que el
        prefijo empate con un solo nombre, para no confundir dos municipios."""
        for key in ((city, prov),):
            if key in med:
                return med[key]
        for c in (city, city.split("/")[0]):
            if c in unique_city:
                return unique_city[c]
        pref = [c for c in unique_city if c.startswith(city + " ")]
        return unique_city[pref[0]] if len(pref) == 1 else None

    spread: dict[tuple[float, float], set[str]] = {}
    for r in rows:
        c = (round(r["coordinates"]["lat"], 6), round(r["coordinates"]["lng"], 6))
        spread.setdefault(c, set()).add(
            _norm(src.get(r["listingId"], {}).get("province")))
    tally: collections.Counter[str] = collections.Counter()
    dest = out_path.with_suffix(".validated.jsonl")
    with dest.open("w") as sink:
        for r in rows:
            c = (r["coordinates"]["lat"], r["coordinates"]["lng"])
            s = src.get(r["listingId"], {})
            city = _norm(s.get("city"))
            key = (city, _norm(s.get("province")))
            ref = centroid_for(city, key[1])
            flags = []
            if len(spread[(round(c[0], 6), round(c[1], 6))]) >= min_states:
                flags.append("repe")
            if ref is None:
                flags.append("sin-centroide")
            elif _km(c, ref) > max_km:
                flags.append("lejos")
            r["suspect"] = flags
            tally["limpia" if not flags else "+".join(flags)] += 1
            sink.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{dest}: {dict(tally)}")
    print(f"usables (sin marca): {tally['limpia']}/{len(rows)}")


def _selfcheck() -> None:
    """Las dos marcas se cazan cosas distintas; una sola deja pasar basura."""
    assert classify('{"latitude":21.84,"longitude":-102.32}') == ("ok", (21.84, -102.32))
    assert classify('{"latitude":"21.8","longitude":"-102.3"}')[0] == "ok"   # con comillas
    # el markup exacto que se comió la corrida de 5,000: el <br/> parte la frase
    wall = ('<p class="message-card">¡Hola! Para continuar, ingresa a<br/>'
            'tu cuenta</p>')
    assert classify(wall)[0] == "login", "el <br/> no debe esconder el muro"
    assert classify('window._bmc="abc"')[0] == "pow"
    # una página chica que no reconozco es sospechosa, nunca benigna
    assert classify("<html>algo raro</html>")[0] == "desconocido"
    # sólo un anuncio real y completo puede declararse sin ubicación
    assert classify("<html>" + "x" * REAL_PAGE_BYTES + "</html>")[0] == "sin-coords"

    # el camino del JS no manda HTML, manda las cuatro señales: mismo veredicto
    assert verdict(500_000, (19.4, -99.1), False, False)[0] == "ok"
    assert verdict(21_000, None, True, False)[0] == "login"
    assert verdict(9_000, None, False, False)[0] == "desconocido"
    assert LOGIN_WALL in FETCH_JS, "el JS tiene que buscar la misma frase que Python"

    # el alias de provincia: sin él, CDMX entera sale como no verificable
    assert _norm("Distrito Federal") == _norm("Ciudad de México") == "ciudad de mexico"

    # el punto de relleno de ML cae en CDMX: cerca de Tlalpan, lejos de Cadereyta.
    # `lejos` sola perdona la primera, `repe` sola no distingue — hacen falta las dos.
    fill = (19.39068, -99.2836995)
    assert _km(fill, (19.29, -99.16)) < 40, "Tlalpan queda cerca del relleno"
    assert _km(fill, (25.59, -99.98)) > 40, "Cadereyta queda lejos del relleno"
    print("OK selfcheck: classify, alias de provincia y ambas marcas")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--login", action="store_true", help="abrir Chrome para loguearte a mano")
    ap.add_argument("--limit", type=int, default=30, help="piloto: cuántos listings (0 = todos)")
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("data/ml_coords.jsonl"))
    ap.add_argument("--min-gap", type=float, default=5.0, help="Crawl-delay de robots.txt")
    ap.add_argument("--validate", action="store_true",
                    help="marcar coordenadas de relleno en <out> (no toca la red)")
    ap.add_argument("--selfcheck", action="store_true", help="pruebas offline")
    a = ap.parse_args()

    if a.selfcheck:
        _selfcheck()
    elif a.login:
        do_login()
    elif a.validate:
        validate(a.out)
    else:
        crawl(a.limit or None, a.out, a.min_gap)


if __name__ == "__main__":
    main()
