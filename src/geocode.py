#!/usr/bin/env python3
"""
Geocode pipeline: backfill missing lat/lon for listings in the DB using Mapbox Geocoding API v6.

Queries public.listings for records WHERE lat IS NULL (or lon is NULL) and location is not empty,
then forward-geocodes each address via Mapbox and updates the row.

Usage:
    python -m src.geocode                   # geocode all missing records
    python -m src.geocode --dry-run          # count + preview only, no writes
    python -m src.geocode --source lamudi    # geocode a specific source only
    python -m src.geocode --limit 10         # geocode at most N records
"""

import os, sys, time, json
from urllib.request import Request, urlopen
from urllib.parse import quote

from dotenv import load_dotenv
import psycopg2

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────

MAPBOX_TOKEN = os.environ.get("MAPBOX_API_KEY", "").strip("\"'")
if not MAPBOX_TOKEN:
    raise SystemExit("MAPBOX_API_KEY not found in .env")

GEOCODE_URL = "https://api.mapbox.com/search/geocode/v6/forward"
RATE_LIMIT_DELAY = 0.15   # ~400 req/min (Mapbox limit is 1000/min)
BATCH_SIZE = 50            # rows to fetch per DB query
CHECKPOINT_FILE = "output/geocode_checkpoint.json"

# Mexico bounding box (same as scrapers)
def _in_mx(lat, lon):
    return lat is not None and lon is not None and 14 < lat < 33 and -118 < lon < -86


# ── Mapbox API ──────────────────────────────────────────────────────────────

def geocode_address(address):
    """Forward-geocode an address string via Mapbox v6. Returns (lat, lon) or (None, None)."""
    q = f"{address}, México"
    url = f"{GEOCODE_URL}?q={quote(q)}&country=MX&language=es&limit=1&access_token={MAPBOX_TOKEN}"
    req = Request(url, headers={"User-Agent": "scrape-env-geocoder/1.0"})
    try:
        with urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
    except Exception as e:
        print(f"   [geocode] HTTP error: {e}")
        return None, None

    features = body.get("features", [])
    if not features:
        return None, None

    geom = features[0].get("geometry", {})
    if geom.get("type") != "Point":
        return None, None

    coords = geom.get("coordinates")  # [lon, lat]
    if not coords or len(coords) < 2:
        return None, None

    lon, lat = float(coords[0]), float(coords[1])
    if _in_mx(lat, lon):
        return round(lat, 6), round(lon, 6)
    return None, None


# ── DB helpers ──────────────────────────────────────────────────────────────

def get_conn():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set in .env")
    return psycopg2.connect(url)


def prepare_address(location_raw):
    """Extract a clean geocoding string from a DB location field.

    Some records store location as a JSON blob with a 'name' key and sometimes
    embedded lat/lon. Returns (address_string, (lat_or_None, lon_or_None)).
    If embedded coords are found and valid, lat/lon are returned so the caller
    can skip the API call.
    """
    if not location_raw:
        return None, (None, None)

    # Try JSON — easybroker stores location as {"name": "...", "latitude": ..., "longitude": ...}
    if isinstance(location_raw, str) and location_raw.strip().startswith("{"):
        try:
            obj = json.loads(location_raw)
            name = obj.get("name") or obj.get("nombre") or ""
            lat = obj.get("latitude") or obj.get("lat")
            lon = obj.get("longitude") or obj.get("lon") or obj.get("lng")
            # Return embedded coords if valid Mexico coords
            if lat and lon:
                try:
                    flat, flon = float(lat), float(lon)
                    if _in_mx(flat, flon):
                        return name or None, (round(flat, 6), round(flon, 6))
                except (TypeError, ValueError):
                    pass
            return name or None, (None, None)
        except (json.JSONDecodeError, TypeError):
            pass

    return str(location_raw).strip(), (None, None)


def fetch_missing(conn, source=None, limit=None):
    """Return list of dicts: {id, external_id, source, location, url}."""
    clauses = ["lat IS NULL", "is_active = true"]
    params = []
    if source:
        clauses.append("source = %s")
        params.append(source)
    where = " AND ".join(clauses)
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    sql = f"""
        SELECT id, external_id, source, location, url
        FROM public.listings
        WHERE {where} AND location IS NOT NULL AND location != ''
        ORDER BY id
        {limit_clause}
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def update_coords(conn, row_id, lat, lon):
    """Set lat, lon, maps_url for a single row."""
    maps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.listings SET lat=%s, lon=%s, maps_url=%s WHERE id=%s",
            (lat, lon, maps_url, row_id),
        )
    conn.commit()


def count_missing(conn, source=None):
    clauses = ["lat IS NULL", "is_active = true"]
    params = []
    if source:
        clauses.append("source = %s")
        params.append(source)
    where = " AND ".join(clauses)
    sql = f"SELECT count(*) FROM public.listings WHERE {where} AND location IS NOT NULL AND location != ''"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


# ── Checkpoint ──────────────────────────────────────────────────────────────

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        try:
            data = json.load(open(CHECKPOINT_FILE))
            return set(data.get("done", []))
        except Exception:
            return set()
    return set()


def save_checkpoint(done_set):
    os.makedirs("output", exist_ok=True)
    tmp = CHECKPOINT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"done": list(done_set)}, f)
    os.replace(tmp, CHECKPOINT_FILE)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    source = None
    limit_flag = None

    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        source = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        limit_flag = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else None

    conn = get_conn()
    total_missing = count_missing(conn, source)
    print(f"Records missing coords (with location): {total_missing}")
    if source:
        print(f"  Filtered by source: {source}")

    if total_missing == 0:
        print("Nothing to geocode.")
        conn.close()
        return

    records = fetch_missing(conn, source, limit_flag or None)
    if not records:
        print("No records to process.")
        conn.close()
        return

    done = load_checkpoint()
    records = [r for r in records if str(r["id"]) not in done]
    print(f"To process (after checkpoint): {len(records)}\n")

    if dry_run:
        print("DRY RUN — no updates will be made.\n")
        for r in records[:min(20, len(records))]:
            print(f"  [{r['source']:15s}] {r['location'][:80]}")
        if len(records) > 20:
            print(f"  ... and {len(records) - 20} more")
        print(f"\nTotal to geocode: {len(records)}")
        conn.close()
        return

    from tqdm import tqdm
    ok = 0
    fail = 0
    skipped = 0
    embedded = 0

    try:
        for r in tqdm(records, desc="Geocoding", unit="addr"):
            if str(r["id"]) in done:
                skipped += 1
                continue

            address, coords = prepare_address(r["location"])

            # Embedded coords from JSON location field — no API call needed
            if coords[0] is not None:
                update_coords(conn, r["id"], coords[0], coords[1])
                embedded += 1
                done.add(str(r["id"]))
                save_checkpoint(done)
                continue

            if not address:
                fail += 1
                done.add(str(r["id"]))
                save_checkpoint(done)
                continue

            lat, lon = geocode_address(address)

            if lat is not None:
                update_coords(conn, r["id"], lat, lon)
                ok += 1
            else:
                fail += 1

            done.add(str(r["id"]))
            save_checkpoint(done)
            time.sleep(RATE_LIMIT_DELAY)

    except KeyboardInterrupt:
        print("\nInterrupted — progress saved to checkpoint.")

    processed = ok + embedded + fail + skipped
    print(f"\nDone. {ok} geocoded from Mapbox, {embedded} via embedded JSON coords, {fail} failed, {skipped} checkpoint-skipped.")
    if fail:
        print(f"Failed records left in checkpoint — re-run to retry them.")
    if total_missing - processed > 0:
        print(f"{total_missing - processed} remaining (not in this batch due to --limit). Re-run without --limit.")

    # Clear checkpoint on full success
    if ok > 0 and fail == 0 and os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("Checkpoint cleared (all done).")

    conn.close()


if __name__ == "__main__":
    main()
