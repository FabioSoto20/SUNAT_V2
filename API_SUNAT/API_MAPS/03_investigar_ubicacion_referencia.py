import csv
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher

import pandas as pd
import requests

import config


OUTPUT_COLUMNS = [
    "NumeroRuc", "UBICACION_ORIGINAL", "ESTADO_REFERENCIA",
    "TIPO_UBICACION", "NIVEL_UBICACION", "CONFIANZA",
    "LATITUD_REFERENCIA", "LONGITUD_REFERENCIA",
    "FUENTE_REFERENCIA", "REFERENCIA_ENCONTRADA",
    "QUERY_INVESTIGACION", "SCORE_REFERENCIA",
    "EVIDENCIA_REFERENCIA", "URL_FUENTE", "ERROR",
]


def norm(text):
    text = "" if text is None else str(text)
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    ).upper()
    text = re.sub(r"[^A-Z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


STOP = {
    "DE", "DEL", "LA", "LAS", "LOS", "EL", "Y", "EN",
    "AV", "AVENIDA", "CALLE", "JR", "JIRON", "PERU",
    "MZ", "MZA", "LT", "LOTE", "URB", "URBANIZACION",
}


def tokens(text):
    return [
        t for t in norm(text).split()
        if len(t) >= 2 and t not in STOP
    ]


def coverage(target, result):
    a = tokens(target)
    b = set(tokens(result))
    if not a:
        return 0.0
    return sum(1 for x in a if x in b) / len(a)


def similarity(a, b):
    na = norm(a)
    nb = norm(b)
    if not na or not nb:
        return 0.0

    return max(
        coverage(a, b),
        coverage(b, a),
        SequenceMatcher(None, na, nb).ratio(),
    )


def value(row, col):
    return str(row.get(col, "") or "").strip()


def admin_text(row):
    parts, seen = [], set()

    for col in [
        "DISTRITO_BUSQUEDA",
        "PROVINCIA_BUSQUEDA",
        "DEPARTAMENTO_BUSQUEDA",
    ]:
        v = value(row, col)
        k = norm(v)
        if v and k not in seen:
            seen.add(k)
            parts.append(v)

    parts.append("PERU")
    return ", ".join(parts)


def build_reference_queries(row):
    loc = admin_text(row)

    road = value(row, "VIA_CORREGIDA")
    zone = value(row, "ZONA_CORREGIDA")
    mz = value(row, "MANZANA")
    poi = value(row, "POI_DIRECCION")
    reference = value(row, "REFERENCIA_ORIGINAL")
    business = value(row, "Nombre ó RazonSocial")

    queries = []
    seen = set()

    def add(level, q):
        q = re.sub(r"\s+", " ", str(q or "")).strip(" ,")
        k = norm(q)
        if not q or not k or k in seen:
            return
        seen.add(k)
        queries.append((level, q))

    if road and mz and zone:
        add("MANZANA", f"{road}, MZ {mz}, {zone}, {loc}")

    if road and zone:
        add("VIA_ZONA", f"{road}, {zone}, {loc}")

    if mz and zone:
        add("MANZANA", f"MZ {mz}, {zone}, {loc}")

    if poi:
        add("POI_ZONA", f"{poi}, {loc}")

    if reference:
        add("REFERENCIA_TEXTO", f"{reference}, {loc}")

    if road:
        add("VIA", f"{road}, {loc}")

    if zone:
        add("ZONA", f"{zone}, {loc}")

    if business:
        add("NEGOCIO", f"{business}, {loc}")

    district = value(row, "DISTRITO_BUSQUEDA")
    province = value(row, "PROVINCIA_BUSQUEDA")
    department = value(row, "DEPARTAMENTO_BUSQUEDA")

    add(
        "DISTRITO",
        ", ".join(
            x for x in [district, province, department, "PERU"]
            if x
        ),
    )

    return queries


HTTP = requests.Session()
HTTP.headers.update(
    {
        "User-Agent": config.NOMINATIM_USER_AGENT,
        "Accept-Language": "es",
    }
)


def nominatim_base():
    if config.NOMINATIM_LOCAL_URL.strip():
        return config.NOMINATIM_LOCAL_URL.rstrip("/")
    return config.NOMINATIM_PUBLIC_URL.rstrip("/")


def nominatim_search(query, limit=5):
    url = f"{nominatim_base()}/search"
    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
        "countrycodes": "pe",
    }

    r = HTTP.get(
        url,
        params=params,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    r.raise_for_status()

    if not config.NOMINATIM_LOCAL_URL.strip():
        time.sleep(config.NOMINATIM_DELAY_SECONDS)

    out = []

    for item in r.json():
        out.append(
            {
                "source": "NOMINATIM",
                "name": item.get("display_name", ""),
                "address": item.get("display_name", ""),
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
                "raw": item,
                "url": (
                    "https://www.openstreetmap.org/"
                    f"{item.get('osm_type', '')}/"
                    f"{item.get('osm_id', '')}"
                ),
            }
        )

    return out


def arcgis_search(query, limit=5):
    params = {
        "f": "json",
        "SingleLine": query,
        "maxLocations": limit,
        "outFields": "Match_addr,Addr_type,City,District,Region,Country",
        "countryCode": "PER",
    }

    r = HTTP.get(
        config.ARCGIS_URL,
        params=params,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    r.raise_for_status()

    time.sleep(config.ARCGIS_DELAY_SECONDS)

    out = []

    for item in r.json().get("candidates", []):
        loc = item.get("location") or {}

        if "y" not in loc or "x" not in loc:
            continue

        out.append(
            {
                "source": "ARCGIS",
                "name": item.get("address", ""),
                "address": item.get("address", ""),
                "lat": float(loc["y"]),
                "lon": float(loc["x"]),
                "raw": item,
                "url": config.ARCGIS_URL,
            }
        )

    return out


def candidate_score(row, level, query, candidate):
    text = f"{candidate['name']} {candidate['address']}"

    district = value(row, "DISTRITO_BUSQUEDA")
    province = value(row, "PROVINCIA_BUSQUEDA")
    department = value(row, "DEPARTAMENTO_BUSQUEDA")
    road = value(row, "VIA_CORREGIDA")
    zone = value(row, "ZONA_CORREGIDA")
    mz = value(row, "MANZANA")
    poi = value(row, "POI_DIRECCION")
    business = value(row, "Nombre ó RazonSocial")

    score = 0.0

    district_cov = coverage(district, text) if district else 0
    province_cov = coverage(province, text) if province else 0
    department_cov = coverage(department, text) if department else 0

    score += district_cov * 25
    score += province_cov * 8
    score += department_cov * 5

    if road:
        score += coverage(road, text) * 30

    if zone:
        score += coverage(zone, text) * 25

    if mz and re.search(
        rf"\b(?:MZ|MZA|MANZANA)\s*{re.escape(norm(mz))}\b",
        norm(text),
    ):
        score += 30

    if poi:
        score += similarity(poi, text) * 25

    if level == "NEGOCIO" and business:
        score += similarity(business, text) * 25

    if (
        department
        and department_cov == 0
        and district_cov == 0
        and level != "DISTRITO"
    ):
        score -= 20

    return round(score, 2)


def choose_best(row, level, query, candidates):
    if not candidates:
        return None

    scored = []

    for c in candidates:
        c = dict(c)
        c["score"] = candidate_score(row, level, query, c)
        scored.append(c)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[0]


def min_score_for_level(level):
    return {
        "MANZANA": 50,
        "VIA_ZONA": 45,
        "POI_ZONA": 42,
        "REFERENCIA_TEXTO": 40,
        "VIA": 38,
        "ZONA": 35,
        "NEGOCIO": 42,
        "DISTRITO": 0,
    }.get(level, 40)


def district_centroid(row):
    q = ", ".join(
        x
        for x in [
            value(row, "DISTRITO_BUSQUEDA"),
            value(row, "PROVINCIA_BUSQUEDA"),
            value(row, "DEPARTAMENTO_BUSQUEDA"),
            "PERU",
        ]
        if x
    )

    for searcher in [nominatim_search, arcgis_search]:
        try:
            results = searcher(q, limit=3)
            if results:
                best = choose_best(row, "DISTRITO", q, results)
                if best:
                    return best, q
        except Exception:
            continue

    return None, q


def overpass_candidates(row, centroid):
    terms = []

    for col in ["VIA_CORREGIDA", "ZONA_CORREGIDA", "POI_DIRECCION"]:
        v = value(row, col)
        if v:
            terms.append(v)

    if not terms:
        return []

    lat = centroid["lat"]
    lon = centroid["lon"]
    radius = config.OVERPASS_RADIUS_METERS

    out = []

    for term in terms[:3]:
        toks = [t for t in tokens(term) if len(t) >= 4][:3]
        if not toks:
            continue

        regex = ".*".join(re.escape(t.title()) for t in toks)

        query = f"""
        [out:json][timeout:25];
        nwr(around:{radius},{lat},{lon})["name"~"{regex}",i];
        out center tags;
        """

        try:
            r = HTTP.post(
                config.OVERPASS_URL,
                data={"data": query},
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            r.raise_for_status()
            time.sleep(config.OVERPASS_DELAY_SECONDS)

            for element in r.json().get("elements", []):
                tags = element.get("tags", {})
                name = tags.get("name", "")

                if not name:
                    continue

                if element.get("type") == "node":
                    e_lat = element.get("lat")
                    e_lon = element.get("lon")
                else:
                    center = element.get("center") or {}
                    e_lat = center.get("lat")
                    e_lon = center.get("lon")

                if e_lat is None or e_lon is None:
                    continue

                out.append(
                    {
                        "source": "OVERPASS_OSM",
                        "name": name,
                        "address": " ".join(
                            x
                            for x in [
                                tags.get("addr:street", ""),
                                tags.get("addr:housenumber", ""),
                                tags.get("addr:suburb", ""),
                                tags.get("addr:city", ""),
                            ]
                            if x
                        ),
                        "lat": float(e_lat),
                        "lon": float(e_lon),
                        "raw": element,
                        "url": (
                            "https://www.openstreetmap.org/"
                            f"{element.get('type')}/"
                            f"{element.get('id')}"
                        ),
                    }
                )
        except Exception:
            continue

    return out


def investigate(row):
    queries = build_reference_queries(row)

    centroid = None
    centroid_query = ""

    try:
        centroid, centroid_query = district_centroid(row)
    except Exception:
        centroid = None

    # 1) Nominatim + ArcGIS
    for level, query in queries:
        if level == "DISTRITO":
            continue

        all_candidates = []

        for searcher in [nominatim_search, arcgis_search]:
            try:
                all_candidates.extend(searcher(query, limit=5))
            except Exception as e:
                print(f"      [WARN] {searcher.__name__}: {e}")

        best = choose_best(row, level, query, all_candidates)

        if best and best["score"] >= min_score_for_level(level):
            return {
                "level": level,
                "query": query,
                "candidate": best,
                "evidence": (
                    f"Coincidencia multifuente. "
                    f"Score={best['score']}. Fuente={best['source']}."
                ),
            }

    # 2) Overpass cerca del distrito
    if centroid:
        try:
            osm = overpass_candidates(row, centroid)

            if osm:
                best = choose_best(
                    row,
                    "VIA_ZONA",
                    "OVERPASS_AROUND_DISTRICT",
                    osm,
                )

                if best and best["score"] >= 35:
                    return {
                        "level": "POI_VIA_OSM",
                        "query": "OVERPASS_AROUND_DISTRICT",
                        "candidate": best,
                        "evidence": (
                            "Elemento OSM localizado cerca del distrito "
                            "y relacionado con vía/zona/POI."
                        ),
                    }
        except Exception as e:
            print(f"      [WARN] Overpass: {e}")

    # 3) Centro del distrito
    if config.ALLOW_DISTRICT_CENTROID_FALLBACK and centroid:
        return {
            "level": "DISTRITO",
            "query": centroid_query,
            "candidate": centroid,
            "evidence": (
                "No se encontró una referencia más específica. "
                "Se utiliza el centro del distrito como último fallback."
            ),
        }

    return None


def append_csv(path, row):
    exists = path.exists()

    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=OUTPUT_COLUMNS,
            extrasaction="ignore",
        )

        if not exists:
            writer.writeheader()

        writer.writerow({c: row.get(c, "") for c in OUTPUT_COLUMNS})
        f.flush()
        os.fsync(f.fileno())


def load_done():
    if not config.REFERENCE_CSV.exists():
        return set()

    df = pd.read_csv(
        config.REFERENCE_CSV,
        dtype=str,
        low_memory=False,
    ).fillna("")

    return set(df["NumeroRuc"].astype(str))


def main():
    if not config.PREPARED_CSV.exists():
        raise FileNotFoundError(f"Falta Paso 01: {config.PREPARED_CSV}")

    if not config.EXACT_CSV.exists():
        raise FileNotFoundError(f"Falta Paso 02: {config.EXACT_CSV}")

    prep = pd.read_csv(
        config.PREPARED_CSV,
        dtype=str,
        low_memory=False,
    ).fillna("")

    exact = pd.read_csv(
        config.EXACT_CSV,
        dtype=str,
        low_memory=False,
    ).fillna("")

    exact = exact.drop_duplicates(subset=["NumeroRuc"], keep="last")

    pending = exact[
        exact["ESTADO_EXACTO"].eq("PENDIENTE_REFERENCIA")
    ][["NumeroRuc"]]

    df = pending.merge(prep, on="NumeroRuc", how="left")

    done = load_done()
    df = df[
        ~df["NumeroRuc"].astype(str).isin(done)
    ].copy()

    print("=" * 100)
    print("03 - INVESTIGAR UBICACION DE REFERENCIA")
    print("=" * 100)
    print("Pendientes:", len(df))
    print("Nominatim :", nominatim_base())
    print("ArcGIS    :", config.ARCGIS_URL)
    print("Overpass  :", config.OVERPASS_URL)

    for pos, (_, row) in enumerate(df.iterrows(), start=1):
        print(
            f"\n{pos}/{len(df)} | RUC {row['NumeroRuc']} "
            f"| {row['DIRECCION_ORIGINAL']}"
        )

        try:
            result = investigate(row)

            if result:
                c = result["candidate"]

                confidence = {
                    "MANZANA": "MEDIA_ALTA",
                    "VIA_ZONA": "MEDIA",
                    "POI_ZONA": "MEDIA",
                    "REFERENCIA_TEXTO": "MEDIA",
                    "VIA": "MEDIA_BAJA",
                    "ZONA": "MEDIA_BAJA",
                    "NEGOCIO": "MEDIA",
                    "POI_VIA_OSM": "MEDIA",
                    "DISTRITO": "BAJA",
                }.get(result["level"], "MEDIA_BAJA")

                out = {
                    "NumeroRuc": row["NumeroRuc"],
                    "UBICACION_ORIGINAL": row["UBICACION_ORIGINAL"],
                    "ESTADO_REFERENCIA": "ENCONTRADA",
                    "TIPO_UBICACION": "UBICACION_REFERENCIA",
                    "NIVEL_UBICACION": result["level"],
                    "CONFIANZA": confidence,
                    "LATITUD_REFERENCIA": f"{c['lat']:.6f}",
                    "LONGITUD_REFERENCIA": f"{c['lon']:.6f}",
                    "FUENTE_REFERENCIA": c["source"],
                    "REFERENCIA_ENCONTRADA": c["name"],
                    "QUERY_INVESTIGACION": result["query"],
                    "SCORE_REFERENCIA": c.get("score", ""),
                    "EVIDENCIA_REFERENCIA": result["evidence"],
                    "URL_FUENTE": c.get("url", ""),
                    "ERROR": "",
                }

            else:
                out = {
                    "NumeroRuc": row["NumeroRuc"],
                    "UBICACION_ORIGINAL": row["UBICACION_ORIGINAL"],
                    "ESTADO_REFERENCIA": "SIN_REFERENCIA",
                    "TIPO_UBICACION": "",
                    "NIVEL_UBICACION": "",
                    "CONFIANZA": "",
                    "LATITUD_REFERENCIA": "",
                    "LONGITUD_REFERENCIA": "",
                    "FUENTE_REFERENCIA": "",
                    "REFERENCIA_ENCONTRADA": "",
                    "QUERY_INVESTIGACION": "",
                    "SCORE_REFERENCIA": "",
                    "EVIDENCIA_REFERENCIA": "",
                    "URL_FUENTE": "",
                    "ERROR": "",
                }

        except Exception as e:
            out = {
                "NumeroRuc": row["NumeroRuc"],
                "UBICACION_ORIGINAL": row["UBICACION_ORIGINAL"],
                "ESTADO_REFERENCIA": "ERROR_TECNICO",
                "TIPO_UBICACION": "",
                "NIVEL_UBICACION": "",
                "CONFIANZA": "",
                "LATITUD_REFERENCIA": "",
                "LONGITUD_REFERENCIA": "",
                "FUENTE_REFERENCIA": "",
                "REFERENCIA_ENCONTRADA": "",
                "QUERY_INVESTIGACION": "",
                "SCORE_REFERENCIA": "",
                "EVIDENCIA_REFERENCIA": "",
                "URL_FUENTE": "",
                "ERROR": str(e),
            }

        append_csv(config.REFERENCE_CSV, out)

        print(
            f"  FINAL -> {out['ESTADO_REFERENCIA']} "
            f"| {out['NIVEL_UBICACION']} "
            f"| {out['LATITUD_REFERENCIA']}, "
            f"{out['LONGITUD_REFERENCIA']} "
            f"| {out['FUENTE_REFERENCIA']}"
        )

    print("\nOK:", config.REFERENCE_CSV)

if __name__ == "__main__":
    main()
