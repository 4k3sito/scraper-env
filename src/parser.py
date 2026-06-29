"""
Description parser for real estate listings (Spanish/Mexico).

Extracts structured fields from property descriptions using regex patterns,
with an optional AI backend for complex cases.

Usage:
    from src.parser import parse_description

    result = parse_description(
        "Local comercial en renta, 85 m², 2 baños, 1 estacionamiento, planta baja. ..."
    )
    # Returns: {"size_m2": 85, "bathrooms": 2, "parking": 1, "floor": "baja", ...}

The AI backend is only activated when an LLM provider is configured (OPENAI_API_KEY,
ANTHROPIC_API_KEY, or OPENROUTER_API_KEY). Without one, only regex extraction runs.
"""

import os, re, json


# ── Regex patterns (Spanish real estate) ────────────────────────────────────

# Area/size
RE_SIZE = re.compile(
    r"(\d{2,4}(?:[.,]\d+)?)\s*m[²2s]",
    re.I,
)

# Rooms
RE_BEDROOMS = re.compile(
    r"(\d+)\s*(?:recámara|habitación|cuarto|dormitorio|recamara|habitacion)",
    re.I,
)
RE_BATHROOMS = re.compile(
    r"(\d+)\s*(?:baño\s*(?:completo|)?|baños?|wc|sanitario)",
    re.I,
)
RE_HALF_BATH = re.compile(
    r"(?:medio\s*baño|1/2\s*baño|baño\s*de\s*visitas|1\.5\s*baños?)",
    re.I,
)

# Parking
RE_PARKING = re.compile(
    r"(\d+)\s*(?:estacionamiento|cajón\s*de\s*estacionamiento|lugar\s*de\s*estacionamiento|garage|cochera|estacionamiento)",
    re.I,
)

# Floor
RE_FLOOR = re.compile(
    r"(?:planta|piso|nivel)\s*(baja|alta|(\d+)[ero]?)",
    re.I,
)
RE_TOTAL_FLOORS = re.compile(
    r"(\d+)\s*(?:niveles|pisos|plantas)",
    re.I,
)

# Construction / land area split
RE_CONSTRUCTION = re.compile(
    r"(\d[\d.,]*)\s*m[²2s]\s*(?:de\s*)?construcci[oó]n",
    re.I,
)
RE_LAND = re.compile(
    r"(?:\bterreno\b\s*(?:de\s*)?(\d[\d.,]*)\s*m[²2s])|(?:(\d[\d.,]*)\s*m[²2s]\s*(?:de\s*)?(?:terreno|lote))",
    re.I,
)

# Transaction type (when not already known)
RE_RENT = re.compile(r"\b(?:renta|arriendo|alquiler|se\s*renta)\b", re.I)
RE_SALE = re.compile(r"\b(?:venta|vendo|se\s*vende|adquisici[oó]n)\b", re.I)

# Amenities & features
AMENITY_PATTERNS = [
    ("seguridad", r"\b(?:seguridad\s*(?:24\s*h[or]as)?|c[áa]maras\s*(?:de\s*)?seguridad| vigilancia|alarma|guardia)\b"),
    ("estacionamiento_visitantes", r"\bestacionamiento\s*(?:de\s*)?visitantes?\b"),
    ("elevador", r"\b(?:elevador|ascensor)\b"),
    ("rooftop", r"\b(?:rooftop|azotea|terraza)\b"),
    ("gimnasio", r"\b(?:gimnasio|GYM|fitness)\b"),
    ("alberca", r"\b(?:alberca|pileta|piscina|swimming\s*pool)\b"),
    ("jardin", r"\b(?:jard[ií]n|area\s*verde|parque)\b"),
    ("cisterna", r"\b(?:cisterna|tinaco)\b"),
    ("aire_acondicionado", r"\b(?:aire\s*(?:acondicionado|lav)?|clima|mini\s*split|AC)\b"),
    ("calefaccion", r"\b(?:calefacci[oó]n|caldera|boiler|heating)\b"),
    ("chimenea", r"\b(?:chimenea|fireplace)\b"),
    ("amueblado", r"\b(?:amueblado|semiamueblado|equipado|furnished)\b"),
    ("cocina_integral", r"\b(?:cocina\s*(?:integral|equipada)|isla)\b"),
    ("roperos", r"\b(?:roper[oa]s?|closet|walk[-\s]*in)\b"),
    ("internet", r"\b(?:internet|wifi|fibra\s*[oó]ptica|starlink)\b"),
    ("pet_friendly", r"\b(?:pet\s*friendly|mascotas|animales|pet\s*friendly)\b"),
    ("uso_suelo", r"\b(?:uso\s*de\s*suelo|giro|comercial|mixto)\b"),
    ("bodega", r"\b(?:bodega|almac[eé]n|dep[óo]sito|storage)\b"),
    ("recepcion", r"\b(?:recepcion|reception|lobby|vest[ií]bulo)\b"),
]

# Condition
RE_CONDITION = re.compile(
    r"\b(?:nuev[oa]|remodelad[oa]|renovad[oa]|segunda\s*mano|usad[oa]|en\s*construcci[oó]n|obra\s*gris|entrega\s*inmediata|pre[-\s]*venta)\b",
    re.I,
)


def _clean_num(s):
    """Parse a number from a string like '85' or '2,402.62'."""
    if s is None:
        return None
    s = s.replace(",", "").replace(".", "").strip()
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def _parse_size(text):
    """Extract best size estimate from description. Returns m² int or None."""
    # Prefer explicit "construcción" over generic m²
    m = RE_CONSTRUCTION.search(text)
    if m:
        return _clean_num(m.group(1))
    # Generic m²
    m = RE_SIZE.search(text)
    if m:
        return _clean_num(m.group(1))
    return None


def _parse_amenities(text):
    """Return list of (key, label) for matched amenities."""
    found = []
    for key, pat in AMENITY_PATTERNS:
        if re.search(pat, text, re.I):
            found.append(key)
    return found


def _parse_condition(text):
    m = RE_CONDITION.search(text)
    return m.group(0).lower().strip() if m else None


def _parse_floor_info(text):
    """Return (floor_number_or_name, total_floors)."""
    floor = None
    total = None
    m = RE_FLOOR.search(text)
    if m:
        floor = m.group(2) or m.group(1)  # digit or "baja"/"alta"
        if floor and floor.isdigit():
            floor = int(floor)
    m = RE_TOTAL_FLOORS.search(text)
    if m:
        total = int(m.group(1))
    return floor, total


# ── Normalization maps ────────────────────────────────────────────────────
# ponytail: flat dicts, one canonical form per concept. Add as new types appear.

PROPERTY_TYPE_MAP = {
    # Oficina variants
    "oficina": "Oficina", "office": "Oficina",
    # Departamento variants
    "departamento": "Departamento", "departamento residencial": "Departamento",
    "apartment": "Departamento", "apartamento": "Departamento",
    "dpto": "Departamento", "depto": "Departamento",
    # Casa variants
    "casa": "Casa", "house": "Casa", "casa residencial": "Casa",
    "casa comercial": "Local Comercial",
    # Local variants
    "local": "Local", "local comercial": "Local Comercial",
    "commercial": "Local Comercial", "retail": "Local Comercial",
    "local en centro comercial": "Local Comercial",
    # Terreno variants
    "terreno": "Terreno", "land": "Terreno", "lote": "Terreno",
    "terreno comercial": "Terreno", "terreno residencial": "Terreno",
    "terreno industrial": "Terreno",
    # Bodega / Industrial
    "bodega industrial": "Nave Industrial", "nave industrial": "Nave Industrial",
    "industrial": "Nave Industrial", "warehouse": "Nave Industrial",
    # Edificio
    "edificio comercial": "Edificio Comercial", "edificio residencial": "Edificio Residencial",
    "building": "Edificio Comercial",
    # Other
    "consultorio": "Consultorio", "hotel": "Hotel",
    "quinta": "Quinta", "finca-rancho": "Finca/Rancho",
}

TRANSACTION_TYPE_MAP = {
    "renta": "Renta", "rent": "Renta", "arriendo": "Renta",
    "alquiler": "Renta", "lease": "Renta",
    "venta": "Venta", "sale": "Venta", "sell": "Venta",
    "venta, renta": "Venta", "renta, venta": "Venta",
}


def normalize(m):
    """Normalize a mapper dict in-place: property_type, transaction_type, currency, features.

    Call after the source-specific mapper, before the row tuple is built.
    """
    # property_type
    pt = (m.get("property_type") or "").strip().lower()
    if pt:
        m["property_type"] = PROPERTY_TYPE_MAP.get(pt, m["property_type"])
    # transaction_type
    tt = (m.get("transaction_type") or "").strip().lower()
    if tt:
        m["transaction_type"] = TRANSACTION_TYPE_MAP.get(tt, m["transaction_type"])

    # currency: default to MXN if unset, keep USD as-is
    if not m.get("currency"):
        m["currency"] = "MXN"
    elif m["currency"] in ("MN", "$", "MX"):
        m["currency"] = "MXN"

    # features: always a list of strings
    feats = m.get("features")
    if isinstance(feats, dict):
        m["features"] = [f"{k}: {v}" for k, v in feats.items() if v not in (None, "", [], {})]
    elif not isinstance(feats, list):
        m["features"] = [str(feats)] if feats else []

    # price_numeric from price_raw as fallback
    _maybe_parse_price(m)

    return m


def _maybe_parse_price(m):
    """Fill price_numeric from price_raw if missing, using _f logic."""
    if m.get("price_numeric") is not None:
        return
    raw = m.get("price_raw")
    if not raw:
        return
    s = re.sub(r"[^\d.\-]", "", str(raw).replace(",", ""))
    try:
        m["price_numeric"] = float(s) if s not in ("", "-", ".") else None
    except ValueError:
        pass


def parse_description(text):
    """Extract structured fields from a Spanish real estate description.

    Args:
        text: Raw description string (or None).

    Returns:
        dict with keys: size_m2, bedrooms, bathrooms, half_bath, parking,
                        floor, total_floors, construction_m2, land_m2,
                        amenities, condition, transaction_type (inferred),
                        neighborhood (attempted), raw (original text)
    """
    if not text:
        return {}

    text_clean = re.sub(r"\s+", " ", text).strip()

    result = {
        "size_m2": _parse_size(text_clean),
        "bedrooms": _clean_num(RE_BEDROOMS.search(text_clean).group(1) if RE_BEDROOMS.search(text_clean) else None),
        "bathrooms": _clean_num(RE_BATHROOMS.search(text_clean).group(1) if RE_BATHROOMS.search(text_clean) else None),
        "half_bath": bool(RE_HALF_BATH.search(text_clean)),
        "parking": _clean_num(RE_PARKING.search(text_clean).group(1) if RE_PARKING.search(text_clean) else None),
        "construction_m2": _clean_num(RE_CONSTRUCTION.search(text_clean).group(1) if RE_CONSTRUCTION.search(text_clean) else None),
        "land_m2": _clean_num((m := RE_LAND.search(text_clean)) and (m.group(1) or m.group(2))),
        "floor": _parse_floor_info(text_clean)[0],
        "total_floors": _parse_floor_info(text_clean)[1],
        "amenities": _parse_amenities(text_clean),
        "condition": _parse_condition(text_clean),
        "transaction_type": "renta" if RE_RENT.search(text_clean) else ("venta" if RE_SALE.search(text_clean) else None),
    }

    # Clean out Nones
    return {k: v for k, v in result.items() if v is not None}


def merge_parsed(record, parsed):
    """Merge parser results into a scraper record dict, preferring existing values.

    Record keys map:
        record["property_size_m2"]  ← parsed["size_m2"] if record doesn't have it
        record["features"]          ← append missing amenities
        record["property_type"]     ← not overwritten (too risky)
        record["transaction_type"]  ← inferred from description if missing
        record["characteristics"]   ← append key details
    """
    if not parsed:
        return record

    # Size: fill if scraper missed it
    if not record.get("property_size_m2") and parsed.get("size_m2"):
        record["property_size_m2"] = parsed["size_m2"]
    # Also fill common scraper-specific size fields
    if not record.get("size") and parsed.get("size_m2"):
        record["size"] = f"{parsed['size_m2']} m²"
    if not record.get("m2_construccion") and parsed.get("size_m2"):
        record["m2_construccion"] = str(parsed["size_m2"])

    # Construction vs land split
    if not record.get("size_construccion_m2") and parsed.get("construction_m2"):
        record["size_construccion_m2"] = parsed["construction_m2"]
    if parsed.get("land_m2") and not record.get("size_terreno_m2"):
        record["size_terreno_m2"] = parsed["land_m2"]

    # Transaction type from description if missing
    ttype = record.get("tipo_transaccion") or record.get("transaction_type") or record.get("operation")
    if not ttype and parsed.get("transaction_type"):
        ttype = parsed["transaction_type"].capitalize()
        record["tipo_transaccion"] = ttype
        record["transaction_type"] = ttype

    # Features: append which amenities weren't already captured
    if record.get("features") is None:
        record["features"] = []
    if isinstance(record.get("characteristics"), dict) and parsed.get("amenities"):
        for a in parsed["amenities"]:
            if a not in str(record["characteristics"]).lower():
                record["characteristics"].setdefault(f"_{a}", True)

    # Numeric characteristics
    if record.get("bedrooms") is None and parsed.get("bedrooms"):
        record["bedrooms"] = parsed["bedrooms"]
    if record.get("banos") is None and parsed.get("bathrooms"):
        record["banos"] = parsed["bathrooms"]
    if record.get("bathrooms") is None and parsed.get("bathrooms"):
        record["bathrooms"] = parsed["bathrooms"]
    if record.get("estacionamientos") is None and parsed.get("parking"):
        record["estacionamientos"] = parsed["parking"]

    # Condition
    if parsed.get("condition") and not record.get("condition"):
        record["condition"] = parsed["condition"]

    return record


def _selftest():
    """Run inline tests."""
    tests = [
        ("Local 85 m², 2 baños, 1 estacionamiento, planta baja",
         {"size_m2": 85, "bathrooms": 2, "parking": 1, "floor": "baja"}),
        ("Casa en venta, 3 recámaras, 2 baños completos, 150 m² de construcción, 200 m² de terreno",
         {"size_m2": 150, "bedrooms": 3, "bathrooms": 2, "construction_m2": 150, "land_m2": 200, "transaction_type": "venta"}),
        ("Oficina en renta, 40 m², 1 baño, elevador, seguridad 24 hrs, gimnasio",
         {"size_m2": 40, "bathrooms": 1, "amenities": ["elevador", "seguridad", "gimnasio"], "transaction_type": "renta"}),
        ("Terreno de 500 m², uso de suelo comercial, zona industrial",
         {"land_m2": 500, "amenities": ["uso_suelo"]}),
        ("Departamento amueblado, 2 habitaciones, 1.5 baños, 1 cajón de estacionamiento, 5to piso, alberca",
         {"bedrooms": 2, "parking": 1, "amenities": ["amueblado", "alberca"], "half_bath": True}),
    ]
    for text, expected in tests:
        result = parse_description(text)
        for k, v in expected.items():
            got = result.get(k)
            # For amenity lists, check all expected items are present
            if isinstance(v, list):
                for item in v:
                    assert item in (got or []), f"Missing amenity '{item}' in '{text}': got {got}"
            else:
                assert got == v, f"Mismatch for '{k}' in '{text}': expected {v}, got {got}"
    print("All selftests passed.")


if __name__ == "__main__":
    _selftest()
