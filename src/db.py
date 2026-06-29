"""Upsert scraped listings into Supabase Postgres (public.listings).

Each scraper calls db.upsert("<source>", records) right after it writes its JSON.
Dedup key is the table's UNIQUE (source, external_id). JSON stays as the backup;
a DB/connection error here never loses the scrape (callers wrap in try/except).

Needs DATABASE_URL in .env (Supabase -> Connect). If unset, upsert is a no-op.
"""
import os
import re
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import execute_values

# ponytail: try both import strategies — relative (package import) and
# absolute (direct run), avoids conflict with stdlib `parser` module.
try:
    from .parser import normalize
except ImportError:
    from parser import normalize

load_dotenv()

# Canonical column order for the bulk insert.
COLS = [
    "external_id", "source", "url", "title", "broker_name", "description",
    "price_raw", "price_numeric", "currency", "property_type", "features",
    "property_size_m2", "transaction_type", "location", "neighborhood",
    "country", "image", "images", "maps_url", "lat", "lon",
]


def _f(x):
    """Parse a float out of messy strings: '$70,000 MXN', '2,402.62 m²', '-100.35'."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = re.sub(r"[^\d.\-]", "", str(x).replace(",", ""))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def _feats(c):
    if isinstance(c, dict):
        return [f"{k}: {v}" for k, v in c.items() if v not in (None, "", [], {})]
    if isinstance(c, list):
        return [str(x) for x in c if x not in (None, "")]
    return []


def _first(lst):
    return lst[0] if isinstance(lst, list) and lst else None


def _century21(r):
    return dict(
        external_id=r.get("id"), url=r.get("url"), title=r.get("name"),
        description=r.get("description"), price_raw=r.get("price"),
        price_numeric=_f(r.get("price_value")), currency=r.get("currency"),
        property_type=r.get("property_type"), transaction_type=r.get("operation"),
        location=r.get("location"), property_size_m2=_f(r.get("size_construccion_m2")),
        features=_feats(r.get("characteristics")), image=_first(r.get("photos")),
        images=r.get("photos"), maps_url=r.get("map_link"),
        lat=_f(r.get("lat")), lon=_f(r.get("lon")),
    )


def _lamudi(r):
    # external_id: the slug after /detalle/ (detail JSON drops the card id)
    ext = (r.get("url") or "").rstrip("/").rsplit("/detalle/", 1)[-1] or None
    return dict(
        external_id=ext, url=r.get("url"), title=r.get("name"),
        description=r.get("description"), price_raw=r.get("price"),
        broker_name=r.get("agency"), location=r.get("location"),
        property_size_m2=_f(r.get("size")),
        features=_feats(r.get("characteristics") or r.get("specifications")),
        image=_first(r.get("photos")), images=r.get("photos"),
        maps_url=r.get("map_link"),
        lat=_f(r.get("lat")), lon=_f(r.get("lng")),  # lamudi uses "lng"
    )


def _inmuebles24(r):
    return dict(
        external_id=r.get("id"), url=r.get("url"), title=r.get("name"),
        description=r.get("description"), price_raw=r.get("price"),
        location=r.get("location"), property_size_m2=_f(r.get("size")),
        features=_feats(r.get("icon_features") or r.get("characteristics")),
        image=_first(r.get("photos")), images=r.get("photos"),
        maps_url=r.get("map_link"), lat=_f(r.get("lat")), lon=_f(r.get("lon")),
    )


def _mercadolibre(r):
    return dict(
        external_id=r.get("codigo_publicacion"), url=r.get("url"), title=r.get("titulo"),
        description=r.get("descripcion"), price_raw=r.get("precio"),
        price_numeric=_f(r.get("precio_numerico")), currency=r.get("moneda"),
        property_type=r.get("tipo_propiedad"), transaction_type=r.get("tipo_transaccion"),
        location=r.get("direccion"), features=_feats(r.get("caracteristicas")),
        image=_first(r.get("imagenes")), images=r.get("imagenes"),
        maps_url=r.get("map_link"), lat=_f(r.get("lat")), lon=_f(r.get("lon")),
    )


def _vivaanuncios(r):
    return dict(
        external_id=r.get("codigo_publicacion"), url=r.get("url"), title=r.get("titulo"),
        description=r.get("descripcion"), price_raw=r.get("precio"),
        price_numeric=_f(r.get("precio_numerico")), currency=r.get("moneda"),
        property_type=r.get("tipo_propiedad"), transaction_type=r.get("tipo_transaccion"),
        location=r.get("direccion"), broker_name=r.get("anunciante"),
        features=_feats(r.get("caracteristicas")), image=_first(r.get("imagenes")),
        images=r.get("imagenes"), maps_url=r.get("map_link"),
        lat=_f(r.get("lat")), lon=_f(r.get("lon")),
    )


def _propiedadesmx(r):
    d = r.get("direccion")
    d = d if isinstance(d, dict) else {}
    loc = ", ".join(str(d[k]) for k in ("colonia", "ciudad", "estado") if d.get(k))
    if not loc and isinstance(r.get("direccion"), str):
        loc = r["direccion"]
    c = r.get("caracteristicas") if isinstance(r.get("caracteristicas"), dict) else {}
    return dict(
        external_id=r.get("codigo_publicacion"), url=r.get("url"), title=r.get("titulo"),
        description=r.get("descripcion"), price_raw=r.get("precio"),
        price_numeric=_f(r.get("precio_numerico")), currency=r.get("moneda"),
        property_type=r.get("tipo_propiedad"), transaction_type=r.get("tipo_transaccion"),
        location=loc or None, neighborhood=d.get("colonia"), broker_name=r.get("inmobiliaria"),
        features=_feats(c), property_size_m2=_f(c.get("construccion")),
        image=r.get("imagen_principal"), images=r.get("imagenes"),
        maps_url=d.get("map_link"), lat=_f(d.get("lat")), lon=_f(d.get("lng")),
    )


def _pincali(r):
    ext = r.get("id_anuncio") or (r.get("url") or "").rstrip("/").rsplit("/", 1)[-1] or None
    return dict(
        external_id=ext, url=r.get("url"), title=r.get("titulo"),
        description=r.get("descripcion"), price_raw=r.get("precio"),
        property_type=r.get("tipo_inmueble"), transaction_type=r.get("tipo_operacion"),
        location=r.get("direccion"), neighborhood=r.get("colonia"),
        property_size_m2=_f(r.get("m2_construccion")), broker_name=r.get("agente"),
        features=_feats({k: r.get(k) for k in
                         ("m2_terreno", "banos", "estacionamientos", "codigo_postal")}),
        image=_first(r.get("imagenes")), images=r.get("imagenes"),
        lat=_f(r.get("lat")), lon=_f(r.get("lng")),
    )


MAPPERS = {
    "lamudi": _lamudi,
    "pincali": _pincali,
    "century21": _century21,
    "inmuebles24": _inmuebles24,
    "mercadolibre": _mercadolibre,
    "vivaanuncios": _vivaanuncios,
    "propiedadesmx": _propiedadesmx,
}


def _reconcile(fresh_ids, existing_ids):
    """Pure set logic: (ids_to_process, ids_to_delete)."""
    fresh, existing = set(fresh_ids), set(existing_ids)
    return fresh - existing, existing - fresh


def _url_of(r):
    """Phase-1 records come as bare URL strings or dicts with a 'url' key."""
    if isinstance(r, str):
        return r
    if isinstance(r, dict):
        return r.get("url")
    return None


def sync(source, phase1_records):
    """Reconcile the DB against a fresh Phase-1 collection, keyed on URL.

    - url already in DB  -> dropped from the returned list (skip Phase 2)
    - url in DB but not in Phase 1 -> deleted from DB (listing vanished)
    Returns the subset of phase1_records whose listings are NEW (go to Phase 2).
    Records may be URL strings or dicts with a "url" key.
    No DATABASE_URL -> returns all records unchanged (DB is optional).

    ponytail: keys on exact URL match, and a truncated/blocked Phase 1 deletes
    live rows. If Phase 2 redirects to a canonical URL, or Phase 1 gets cut
    short, those rows churn — add a min-count guard before deleting if it bites.
    """
    fresh = {}  # url -> original record
    for r in phase1_records:
        u = _url_of(r)
        if u:
            fresh[u] = r

    url = os.getenv("DATABASE_URL")
    if not url:
        print("[db] DATABASE_URL not set — sync skipped (all URLs go to Phase 2)")
        return list(phase1_records)

    conn = psycopg2.connect(url)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "select url from public.listings where source=%s and url is not null",
                (source,),
            )
            existing = {row[0] for row in cur.fetchall()}
            new_urls, stale = _reconcile(fresh, existing)
            if stale:
                cur.execute(
                    "delete from public.listings where source=%s and url = any(%s)",
                    (source, list(stale)),
                )
        print(f"[db] {source}: {len(fresh) - len(new_urls)} already in DB (skip), "
              f"{len(new_urls)} new -> Phase 2, {len(stale)} stale deleted")
        return [fresh[u] for u in new_urls]
    finally:
        conn.close()


def upsert(source, records):
    """Map records via the source's mapper and upsert into public.listings."""
    mapper = MAPPERS[source]
    rows = []
    for r in records:
        if not isinstance(r, dict) or r.get("error"):
            continue
        m = mapper(r)
        if not m.get("external_id"):
            continue
        m["source"] = source
        m.setdefault("country", "México")
        normalize(m)
        rows.append(tuple(m.get(c) for c in COLS))

    if not rows:
        print(f"[db] {source}: nothing to upsert")
        return 0

    url = os.getenv("DATABASE_URL")
    if not url:
        print("[db] DATABASE_URL not set — skipping DB upsert (JSON still written)")
        return 0

    set_clause = ", ".join(f"{c}=excluded.{c}" for c in COLS if c not in ("external_id", "source"))
    sql = (
        f"insert into public.listings ({', '.join(COLS)}) values %s "
        f"on conflict (source, external_id) do update set {set_clause}, scraped_at=now()"
    )
    conn = psycopg2.connect(url)
    try:
        with conn, conn.cursor() as cur:
            execute_values(cur, sql, rows)
        print(f"[db] {source}: upserted {len(rows)} rows")
    finally:
        conn.close()
    return len(rows)


def _selftest():
    assert _f("$70,000 MXN") == 70000
    assert _f("2,402.62 m²") == 2402.62
    assert _f("-100.349978") == -100.349978
    assert _f(None) is None and _f("") is None
    assert _feats({"a": 1, "b": None}) == ["a: 1"]
    assert _feats(["x", "y"]) == ["x", "y"]
    assert _reconcile({"a", "b", "c"}, {"b", "c", "d"}) == ({"a"}, {"d"})
    import json
    for src, f in [("lamudi", "output/lamudi_listings.json"), ("pincali", "output/pincali.json"),
                   ("century21", "output/century21.json"), ("mercadolibre", "output/mercadolibre.json"),
                   ("vivaanuncios", "output/vivanuncios.json"), ("propiedadesmx", "output/propiedades.json"),
                   ("inmuebles24", "output/inm24_listings.json")]:
        try:
            d = json.load(open(f))
        except FileNotFoundError:
            continue
        m = MAPPERS[src](d[0])
        assert "external_id" in m and len(tuple(m.get(c) for c in COLS)) == len(COLS)
    print("ok")


if __name__ == "__main__":
    _selftest()
