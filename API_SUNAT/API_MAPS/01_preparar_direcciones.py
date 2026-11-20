import re
import unicodedata
import pandas as pd
import config

ROAD_TYPES = {
    "AV": "AVENIDA", "AVDA": "AVENIDA", "AVENIDA": "AVENIDA",
    "CAL": "CALLE", "CL": "CALLE", "CALLE": "CALLE",
    "JR": "JIRON", "JIRON": "JIRON",
    "PSJ": "PASAJE", "PJE": "PASAJE", "PJ": "PASAJE", "PASAJE": "PASAJE",
    "CAR": "CARRETERA", "CARRETERA": "CARRETERA",
    "MLC": "MALECON", "MALECON": "MALECON",
    "PQ": "PARQUE", "PARQUE": "PARQUE",
    "PZA": "PLAZA", "PLAZA": "PLAZA",
    "ALM": "ALAMEDA", "ALAMEDA": "ALAMEDA",
    "PRO": "PROLONGACION", "PROL": "PROLONGACION",
    "PROLONGACION": "PROLONGACION",
    "VIA": "VIA", "CAMINO": "CAMINO", "PASEO": "PASEO",
}

def sin_tildes(value):
    value = "" if value is None else str(value)
    return "".join(
        c for c in unicodedata.normalize("NFD", value)
        if unicodedata.category(c) != "Mn"
    )

def norm(value):
    return re.sub(r"\s+", " ", sin_tildes(value).upper().strip())

def norm_key(value):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", norm(value))).strip()

def clean_admin(value):
    t = norm(value)
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip()
    replacements = {
        "PROV. CONST. DEL CALLAO": "CALLAO",
        "PROV CONST DEL CALLAO": "CALLAO",
    }
    return replacements.get(t, t)

def build_original_location(row):
    parts = [
        str(row.get(config.COLUMN_ADDRESS, "") or "").strip(),
        str(row.get(config.COLUMN_DISTRICT, "") or "").strip(),
        str(row.get(config.COLUMN_PROVINCE, "") or "").strip(),
        str(row.get(config.COLUMN_DEPARTMENT, "") or "").strip(),
        "PERU",
    ]
    out, seen = [], set()
    for p in parts:
        if not p:
            continue
        k = norm_key(p)
        if k and k not in seen:
            seen.add(k)
            out.append(p)
    return ", ".join(out)

def remove_parentheses(text):
    return re.sub(r"\s*\([^)]*\)\s*", " ", norm(text)).strip()

def extract_reference(text):
    refs = re.findall(r"\(([^)]+)\)", str(text or ""))
    return " | ".join(x.strip() for x in refs if x.strip())

def clean_internal_noise(text):
    t = norm(text)
    patterns = [
        r"\bDPTO\.?\s*[A-Z0-9\-\/]+",
        r"\bDEPTO\.?\s*[A-Z0-9\-\/]+",
        r"\bINT\.?\s*[A-Z0-9\-\/]+",
        r"\bINTERIOR\.?\s*[A-Z0-9\-\/]+",
        r"\bPISO\s*[A-Z0-9\-\/]+",
        r"\bOFICINA\s*[A-Z0-9\-\/]+",
        r"\bOF\.?\s*[A-Z0-9\-\/]+",
        r"\bLOCAL\s*[A-Z0-9\-\/]+",
        r"\bTIENDA\s*[A-Z0-9\-\/]+",
    ]
    for pattern in patterns:
        t = re.sub(pattern, " ", t)
    return re.sub(r"\s+", " ", t).strip(" ,;-")

def extract_road(text):
    t = norm(text).replace(".", "")
    codes = "|".join(sorted((re.escape(k) for k in ROAD_TYPES), key=len, reverse=True))
    stop = (
        r"NRO|NUMERO|MZA|MANZANA|LOTE|LT|URB|ASOC|ASC|COO|APV|"
        r"AH|A H|RES|SEC|SECTOR|KM|DPTO|INT|PISO"
    )
    m = re.match(rf"^\s*({codes})\s+(.+?)(?=\s+(?:{stop})\b|$)", t)
    if m:
        return ROAD_TYPES.get(m.group(1), m.group(1)), m.group(2).strip(" ,;-")

    m = re.match(
        r"^\s*(.+?)\s+(?:NRO|NUMERO)\.?\s*"
        r"([A-Z]+-[0-9]+|[A-Z]?[0-9]+[A-Z]?(?:-[A-Z0-9]+)?)",
        norm(text),
    )
    if m:
        candidate = m.group(1).strip(" ,;-")
        forbidden = ("MZA ", "MANZANA ", "LOTE ", "OTR ", "BL ", "BLOCK ", "NRO ")
        if candidate and not candidate.startswith(forbidden) and len(candidate) <= 90:
            return "", candidate

    return "", ""

def extract_number(text):
    t = norm(text)
    patterns = [
        r"\b(?:NRO|NUMERO)\.?\s*([A-Z]+-[0-9]+[A-Z]?|[A-Z]?[0-9]+[A-Z]?(?:-[A-Z0-9]+)?)",
        r"\bN\s*[°º]\s*([A-Z]+-[0-9]+[A-Z]?|[A-Z]?[0-9]+[A-Z]?(?:-[A-Z0-9]+)?)",
    ]
    for pattern in patterns:
        m = re.search(pattern, t)
        if m:
            val = m.group(1).strip()
            if val not in {"0", "00"}:
                return val
    return ""

def extract_manzana(text):
    m = re.search(r"\b(?:MZA|MANZANA)\.?\s*([A-Z0-9'\-]+)", norm(text))
    if not m:
        return ""
    val = m.group(1).strip("'- ")
    return "" if val in {"0", "00"} else val

def extract_lote(text):
    m = re.search(r"\b(?:LOTE|LT)\.?\s*([A-Z0-9\-]+)", norm(text))
    if not m:
        return ""
    val = m.group(1).strip()
    return "" if val in {"0", "00"} else val

def extract_km(text):
    m = re.search(r"\bKM\.?\s*([0-9]+(?:\.[0-9]+)?)", norm(text))
    if not m:
        return ""
    val = m.group(1)
    return "" if val in {"0", "0.0", "00"} else val

ZONE_PREFIXES = (
    r"URB|URBANIZACION|ASOC|ASOCIACION|ASC|COO|COOPERATIVA|APV|"
    r"A\.?H\.?|ASENTAMIENTO HUMANO|RES|RESIDENCIAL|SEC|SECTOR|"
    r"C\.?P\.?|CENTRO POBLADO|FND|FUNDO|Z\.?I\.?|LOTIZACION|"
    r"BARRIO|CASERIO|COMUNIDAD"
)

def extract_zone(text):
    t = norm(text)
    m = re.search(
        rf"\b(?:{ZONE_PREFIXES})\.?\s+(.+?)"
        r"(?=\s+\b(?:NRO|MZA|MANZANA|LOTE|LT|DPTO|INT|PISO|KM)\b|\(|$)",
        t,
    )
    if m:
        z = re.sub(r"\s+", " ", m.group(1)).strip(" ,;-")
        if z:
            return z

    m = re.search(r"\b(?:LOTE|LT)\.?\s*[A-Z0-9\-]+\s+(.+?)(?:\(|$)", t)
    if m:
        z = re.split(
            r"\b(?:DPTO|INT|PISO|LOCAL|OFICINA)\b",
            m.group(1),
            maxsplit=1,
        )[0].strip(" ,;-.")
        if 3 <= len(z) <= 90:
            return z
    return ""

def extract_poi(text):
    t = norm(text)
    patterns = [
        (r"\bMCDO\.?\s+(.+?)(?:\(|$)", "MERCADO"),
        (r"\bMERCADO\s+(.+?)(?:\(|$)", "MERCADO"),
        (r"\bC\.?C\.?\s+(.+?)(?:\(|$)", "CENTRO COMERCIAL"),
        (r"\bGALERIA(?:S)?\s+(.+?)(?:\(|$)", "GALERIA"),
        (r"\bCONDOMINIO\s+(.+?)(?:\(|$)", "CONDOMINIO"),
        (r"\bCENTRO\s+SALUD\s+(.+?)(?:\(|$)", "CENTRO DE SALUD"),
    ]
    for pattern, prefix in patterns:
        m = re.search(pattern, t)
        if m:
            name = m.group(1).strip(" ,;-")
            if name:
                return f"{prefix} {name}"
    return ""

def classify(road, number, manzana, lote, km, original):
    t = norm(original)
    if road and number:
        return "VIA_NUMERO"
    if km:
        return "CARRETERA_KM"
    if road and (manzana or lote):
        return "VIA_MZA_LOTE"
    if manzana or lote:
        return "MZA_LOTE_SIN_VIA"
    if re.search(r"\b(?:S/N|SN|S\.N\.)\b", t):
        return "SIN_NUMERO"
    if road:
        return "SOLO_VIA"
    return "OTRO"

def admin_location(district, province, department):
    out, seen = [], set()
    for value in [district, province, department, "PERU"]:
        value = clean_admin(value)
        k = norm_key(value)
        if value and k not in seen:
            seen.add(k)
            out.append(value)
    return ", ".join(out)

def add_query(queries, seen, qtype, query):
    query = re.sub(r"\s+", " ", str(query or "")).strip(" ,")
    if not query:
        return
    key = norm_key(query)
    if key and key not in seen:
        seen.add(key)
        queries.append((qtype, query))

def build_queries(
    road_type, road, number, manzana, lote, km, zone, poi, reference,
    district, province, department, business_name
):
    loc = admin_location(district, province, department)
    road_google = " ".join(x for x in [road_type, road] if x).strip()
    queries, seen = [], set()

    if road_google and number:
        add_query(queries, seen, "VIA_NUMERO", f"{road_google} {number}, {loc}")
    if road_google and manzana and lote and zone:
        add_query(
            queries, seen, "VIA_MZA_LOTE_ZONA",
            f"{road_google}, MZ {manzana}, LT {lote}, {zone}, {loc}"
        )
    if manzana and lote and zone:
        add_query(queries, seen, "MZA_LOTE_ZONA", f"MZ {manzana}, LT {lote}, {zone}, {loc}")
    if road_google and zone:
        add_query(queries, seen, "VIA_ZONA", f"{road_google}, {zone}, {loc}")
    if km and road_google:
        add_query(queries, seen, "CARRETERA_KM", f"{road_google} KM {km}, {loc}")
    if poi:
        add_query(queries, seen, "POI", f"{poi}, {loc}")
    if business_name:
        add_query(queries, seen, "RAZON_SOCIAL", f"{business_name}, {loc}")
    if reference:
        add_query(queries, seen, "REFERENCIA", f"{reference}, {loc}")
    if road_google:
        add_query(queries, seen, "VIA", f"{road_google}, {loc}")
    if zone:
        add_query(queries, seen, "ZONA", f"{zone}, {loc}")

    return queries[:8]

def locate_input():
    for path in config.INPUT_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError(
        "No encontre el archivo fuente.\nBusque:\n"
        + "\n".join(str(x) for x in config.INPUT_CANDIDATES)
    )

def process_row(row):
    original_address = str(row.get(config.COLUMN_ADDRESS, "") or "").strip()
    original_department = str(row.get(config.COLUMN_DEPARTMENT, "") or "").strip()
    original_province = str(row.get(config.COLUMN_PROVINCE, "") or "").strip()
    original_district = str(row.get(config.COLUMN_DISTRICT, "") or "").strip()
    original_location = build_original_location(row)

    reference = extract_reference(original_address)
    cleaned = clean_internal_noise(remove_parentheses(original_address))

    road_type, road = extract_road(cleaned)
    number = extract_number(cleaned)
    manzana = extract_manzana(cleaned)
    lote = extract_lote(cleaned)
    km = extract_km(cleaned)
    zone = extract_zone(cleaned)
    poi = extract_poi(original_address)
    address_type = classify(road, number, manzana, lote, km, original_address)
    business_name = str(row.get(config.COLUMN_NAME, "") or "").strip()

    queries = build_queries(
        road_type, road, number, manzana, lote, km, zone, poi, reference,
        original_district, original_province, original_department, business_name
    )

    output = {
        "NumeroRuc": str(row.get(config.COLUMN_RUC, "") or "").strip(),
        "Nombre ó RazonSocial": business_name,
        "DEPARTAMENTO_ORIGINAL": original_department,
        "PROVINCIA_ORIGINAL": original_province,
        "DISTRITO_ORIGINAL": original_district,
        "DIRECCION_ORIGINAL": original_address,
        "UBICACION_ORIGINAL": original_location,
        "DIRECCION_LIMPIA": cleaned,
        "TIPO_DIRECCION": address_type,
        "TIPO_VIA": road_type,
        "VIA_ORIGINAL": road,
        "VIA_CORREGIDA": road,
        "NUMERO_BUSCADO": number,
        "MANZANA": manzana,
        "LOTE": lote,
        "KM": km,
        "ZONA_ORIGINAL": zone,
        "ZONA_CORREGIDA": zone,
        "POI_DIRECCION": poi,
        "REFERENCIA_ORIGINAL": reference,
        "DISTRITO_BUSQUEDA": clean_admin(original_district),
        "PROVINCIA_BUSQUEDA": clean_admin(original_province),
        "DEPARTAMENTO_BUSQUEDA": clean_admin(original_department),
        "DIRECCION_KEY": norm_key(original_location),
        "CONSULTAS_GENERADAS": len(queries),
    }

    for i in range(1, 9):
        if i <= len(queries):
            qtype, query = queries[i - 1]
        else:
            qtype, query = "", ""
        output[f"Q{i}_TIPO"] = qtype
        output[f"Q{i}"] = query

    return output

def main():
    source = locate_input()
    print("=" * 100)
    print("01 - PREPARAR DIRECCIONES - NUEVO FLUJO DESDE CERO")
    print("=" * 100)
    print("Entrada:", source)
    print("Salida :", config.PREPARED_CSV)

    chunksize = 20000
    first = True
    total = 0
    counts = {}

    for chunk in pd.read_csv(source, dtype=str, chunksize=chunksize, low_memory=False):
        chunk = chunk.fillna("")
        required = [
            config.COLUMN_RUC, config.COLUMN_NAME, config.COLUMN_DEPARTMENT,
            config.COLUMN_PROVINCE, config.COLUMN_DISTRICT, config.COLUMN_ADDRESS,
        ]
        missing = [c for c in required if c not in chunk.columns]
        if missing:
            raise KeyError("Faltan columnas obligatorias: " + ", ".join(missing))

        rows = []
        for idx_local, (_, row) in enumerate(chunk.iterrows(), start=1):
            try:
                out = process_row(row)
            except Exception as e:
                raise RuntimeError(
                    f"Error procesando fila aproximada {total + idx_local:,} "
                    f"| RUC={row.get(config.COLUMN_RUC, '')} "
                    f"| Direccion={row.get(config.COLUMN_ADDRESS, '')!r}"
                ) from e

            rows.append(out)
            counts[out["TIPO_DIRECCION"]] = counts.get(out["TIPO_DIRECCION"], 0) + 1

            if idx_local % 5000 == 0:
                print(
                    f"  Dentro del chunk: {idx_local:,}/{len(chunk):,} "
                    f"| total aproximado: {total + idx_local:,}"
                )

        df_out = pd.DataFrame(rows)
        df_out.to_csv(
            config.PREPARED_CSV,
            mode="w" if first else "a",
            header=first,
            index=False,
            encoding="utf-8-sig",
        )
        first = False
        total += len(df_out)
        print(f"Procesados: {total:,}")

    print("\nResumen:")
    for key, value in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        print(f"  {key:<20} {value:>10,}")

    print("\nOK:", config.PREPARED_CSV)

if __name__ == "__main__":
    main()
