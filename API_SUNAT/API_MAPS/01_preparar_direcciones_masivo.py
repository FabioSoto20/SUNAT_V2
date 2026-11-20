from pathlib import Path
import hashlib
import json
import re
import sqlite3
import unicodedata
import pandas as pd

# ============================================================
# 01_PREPARAR_DIRECCIONES_MASIVO.PY
# PREPARA + NUMERA + DIVIDE EL TRABAJO PARA 2 LAPTOPS
# ============================================================

BASE_DIR = Path(r"D:\API_SUNAT\API_SUNAT")

INPUT_CSV = (
    BASE_DIR
    / "archivos_ruc"
    / "final_results_completo_reducido.csv"
)

OUT_DIR = BASE_DIR / "API_MAPS"

OUT_PREPARADO = (
    OUT_DIR
    / "direcciones_preparadas_google_masivo.csv"
)

OUT_DIAGNOSTICO = (
    OUT_DIR
    / "diagnostico_tipos_direccion_google_masivo.csv"
)

CORRECCIONES_CSV = (
    OUT_DIR
    / "correcciones_aprendidas.csv"
)

DB_KEYS = (
    OUT_DIR
    / "_direcciones_keys_google_masivo_tmp.sqlite"
)

OUT_SQLITE = (
    OUT_DIR
    / "direcciones_preparadas_google_masivo.sqlite"
)

OUT_MANIFEST = (
    OUT_DIR
    / "manifest_google_masivo.json"
)

CHUNKSIZE = 20000
MAX_VARIANTES = 18

COLUMNAS_ENTRADA = [
    "NumeroRuc",
    "Nombre ó RazonSocial",
    "Departamento",
    "Provincia",
    "Distrito",
    "Direccion",
]


# ============================================================
# NORMALIZACION GENERAL
# ============================================================

def sin_tildes(texto):
    texto = "" if texto is None else str(texto)

    return "".join(
        c
        for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def norm(texto):
    texto = "" if texto is None else str(texto)
    texto = sin_tildes(texto).upper().strip()
    texto = re.sub(r"\s+", " ", texto)

    return texto


def norm_key(texto):
    texto = norm(texto)
    texto = re.sub(r"[^A-Z0-9]+", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto


def quitar_parentesis(texto):
    return re.sub(
        r"\([^)]*\)",
        " ",
        texto or "",
    )


def extraer_referencia(texto):
    partes = re.findall(
        r"\(([^)]*)\)",
        texto or "",
    )

    return " | ".join(
        p.strip()
        for p in partes
        if p.strip()
    )


# ============================================================
# LIMPIAR RUIDO DE INTERIOR / PISO / DPTO
# ============================================================

def limpiar_ruido_interno(texto):
    t = norm(texto)

    patrones = [
        r"\bDPTO\.?\s*[A-Z0-9\-]+",
        r"\bDEPTO\.?\s*[A-Z0-9\-]+",
        r"\bINT\.?\s*[A-Z0-9\-]+",
        r"\bINTERIOR\.?\s*[A-Z0-9\-]+",
        r"\b[0-9]+(?:RO|DO|TO|ER|TA)\s+PISO\b",
        r"\bPISO\s*(?!NRO\b|NUMERO\b)[A-Z0-9\-]+",
        r"\bPIS\.?\s*(?!NRO\b|NUMERO\b)[A-Z0-9\-]+",
        r"\bOFICINA\s*[A-Z0-9\-]+",
        r"\bOF\.?\s*[A-Z0-9\-]+",
        r"\bLOCAL\s*[A-Z0-9\-]+",
        r"\bTIENDA\s*[A-Z0-9\-]+",
        r"\bBLOCK\s*[A-Z0-9\-]+",
        r"\bBLOQUE\s*[A-Z0-9\-]+",
    ]

    for patron in patrones:
        t = re.sub(
            patron,
            " ",
            t,
        )

    t = re.sub(
        r"\s+",
        " ",
        t,
    ).strip(" ,;-")

    return t


# ============================================================
# EXTRAER VIA
# ============================================================

TIPOS_VIA = {
    "AV": "AVENIDA",
    "PQ": "PARQUE",
    "PQUE": "PARQUE",
    "PARQUE": "PARQUE",
    "AVENIDA": "AVENIDA",
    "AVDA": "AVENIDA",
    "CAL": "CALLE",
    "CALLE": "CALLE",
    "CL": "CALLE",
    "JR": "JIRON",
    "JIRON": "JIRON",
    "PJ": "PASAJE",
    "PSJ": "PASAJE",
    "PJE": "PASAJE",
    "PASAJE": "PASAJE",
    "CAR": "CARRETERA",
    "CARRETERA": "CARRETERA",
    "PRO": "PROLONGACION",
    "PROL": "PROLONGACION",
    "PROLONGACION": "PROLONGACION",
    "ALAMEDA": "ALAMEDA",
    "MALECON": "MALECON",
    "CAMINO": "CAMINO",
    "PASEO": "PASEO",
}

VIA_STOP = (
    r"NRO|NUMERO|MZA|MANZANA|LOTE|LT|DPTO|DEPTO|INT|INTERIOR|"
    r"URB|AH|COO|ASC|ASOC|APV|RES|CP|SEC|SECTOR|KM|"
    r"OFICINA|LOCAL|PISO|BLOCK|BLOQUE"
)


def extraer_via(direccion):
    raw = sin_tildes("" if direccion is None else str(direccion)).upper().strip()
    # P.J. al inicio suele significar PUEBLO JOVEN, no PASAJE.
    if re.match(r"^P\.\s*J\.\s+", raw):
        return "", ""

    t = norm(direccion)
    t = re.sub(r"\.", "", t)

    patron = (
        rf"^\s*"
        rf"(PQ|PQUE|PARQUE|AV|AVENIDA|AVDA|CAL|CALLE|CL|JR|JIRON|PJ|PSJ|PJE|PASAJE|"
        rf"CAR|CARRETERA|PRO|PROL|PROLONGACION|ALAMEDA|MALECON|CAMINO|PASEO)"
        rf"\s+(.+?)"
        rf"(?=\s+(?:{VIA_STOP})\b|$)"
    )

    match = re.match(
        patron,
        t,
    )

    if not match:
        return "", ""

    tipo = TIPOS_VIA.get(
        match.group(1),
        match.group(1),
    )

    via = match.group(2).strip(
        " ,;-"
    )

    return tipo, via


def expandir_via_interna(via):
    """
    Expansiones conservadoras.

    Ejemplo:
    AL DEL XXX -> ALAMEDA DEL XXX

    Importante:
    Las correcciones verificadas manualmente tienen prioridad
    sobre esta expansión genérica.
    """
    v = norm(via)

    if v.startswith("AL "):
        v = "ALAMEDA " + v[3:]

    return v


# ============================================================
# COMPONENTES NUMERICOS
# ============================================================

def extraer_numero(direccion):
    t = norm(direccion)

    match = re.search(
        r"\b(?:NRO|NUMERO)\.?\s*"
        r"(S/N|SN|S N|[0-9]+[A-Z]?(?:-[A-Z0-9]+)?)",
        t,
    )

    if not match:
        # Direcciones ya estandarizadas: "JR. JOSE SIBINA CAMPOS 598".
        # Solo inferir numero al final de una via y sin indicadores de MZ/LT/KM,
        # para evitar confundir lotes, kilometros o departamentos con puertas.
        if re.search(
            r"\b(?:MZA|MANZANA|LOTE|LT|KM|URB|DPTO|INT|PISO|OFICINA)\b",
            t,
        ):
            return ""
        directo = re.match(
            r"^(?:AV|AVENIDA|AVDA|CAL|CALLE|CL|JR|JIRON|PJ|PSJ|PJE|PASAJE|"
            r"PQ|PQUE|PARQUE|CAR|CARRETERA|PRO|PROL|PROLONGACION)\.?\s+"
            r".+?\s+(\d{1,5}[A-Z]?)$",
            t,
        )
        return directo.group(1) if directo else ""

    numero = (
        match.group(1)
        .replace(" ", "")
    )

    if numero in {
        "S/N",
        "SN",
    }:
        return ""

    return numero


def extraer_manzana(direccion):
    t = norm(direccion)

    match = re.search(
        r"\b(?:MZA|MANZANA)\.?\s*"
        r"(?:MZ\.?)?\s*([A-Z0-9\-]+)",
        t,
    )

    return (
        match.group(1)
        if match
        else ""
    )


def extraer_lote(direccion):
    t = norm(direccion)

    match = re.search(
        r"\b(?:LOTE|LT)\.?\s*"
        r"([A-Z0-9\-]+)",
        t,
    )

    return (
        match.group(1)
        if match
        else ""
    )


def extraer_km(direccion):
    t = norm(direccion)

    match = re.search(
        r"\bKM\.?\s*"
        r"([0-9]+(?:\.[0-9]+)?)",
        t,
    )

    return (
        match.group(1)
        if match
        else ""
    )


# ============================================================
# EXTRAER ZONA / URBANIZACION / ASENTAMIENTO
# ============================================================

TIPOS_ZONA = (
    r"URB|URBANIZACION|"
    r"A\.?H\.?|ASENTAMIENTO HUMANO|"
    r"COO|COOPERATIVA|"
    r"ASC|ASOC|ASOCIACION|"
    r"APV|"
    r"RES|RESIDENCIAL|"
    r"P\.?J\.?|"
    r"C\.?P\.?|"
    r"PROVIV|"
    r"FND|FUNDO|"
    r"U\.?V\.?|"
    r"Z\.?I\.?"
)

CORTES_ZONA = (
    r"NRO|NUMERO|MZA|MANZANA|LOTE|LT|DPTO|INT|INTERIOR|PISO|"
    r"OFICINA|LOCAL|BLOQUE|BLOCK|KM"
)


def extraer_zona(direccion):
    """Devuelve la referencia residencial más específica escrita al final.

    Ejemplo:
      SECTOR IMPERIAL ... P.J. PAMPLONA ALTA
      -> PAMPLONA ALTA
    """
    t = norm(direccion)

    marker = (
        r"URB|URBANIZACION|"
        r"A\.?H\.?|ASENTAMIENTO HUMANO|"
        r"COO|COOPERATIVA|"
        r"ASC|ASOC|ASOCIACION|"
        r"APV|ASOCIACION DE POBLADORES|"
        r"A\.?V\.?|ASOCIACION DE VIVIENDA|"
        r"RES|RESIDENCIAL|"
        r"P\.?J\.?|PUEBLO JOVEN|"
        r"C\.?P\.?|CENTRO POBLADO|"
        r"PROVIV|FND|FUNDO|U\.?V\.?|Z\.?I\.?"
    )

    encontrados = []

    for m in re.finditer(rf"(?:^|\s)(?:{marker})\.?\s+", t):
        rest = t[m.end():]

        stop = re.search(
            rf"\s+(?:(?:NRO|NUMERO|MZA|MANZANA|LOTE|LT|DPTO|DEPTO|INT|"
            rf"INTERIOR|PISO|OFICINA|LOCAL|BLOQUE|BLOCK|KM)\b|(?:{marker})\.?\s+)",
            rest,
        )

        value = rest[:stop.start()] if stop else rest
        value = re.sub(r"\s+", " ", value).strip(" ,;-()")

        if value:
            encontrados.append((m.start(), value))

    if not encontrados:
        return ""

    encontrados.sort(key=lambda x: x[0])
    return encontrados[-1][1]


# ============================================================
# CLASIFICACION
# ============================================================

def clasificar_direccion(
    direccion,
    via,
    numero,
    manzana,
    lote,
    km,
):
    t = norm(direccion)

    if km:
        return "CARRETERA_KM"

    if re.search(
        r"\b(?:S/N|SN)\b",
        t,
    ):
        return "SIN_NUMERO"

    if via and numero:
        return "VIA_NUMERO"

    if via and (
        manzana
        or lote
    ):
        return "VIA_MZA_LOTE"

    if (
        manzana
        or lote
    ):
        return "MZA_LOTE_SIN_VIA"

    if via:
        return "SOLO_VIA"

    return "OTRO"


def calidad_inicial(tipo_direccion, via, numero, manzana, lote, zona, referencia):
    """Calidad estructural, no significa que Google ya la haya encontrado."""
    if tipo_direccion in {"VIA_NUMERO", "VIA_MZA_LOTE"}:
        return "FUERTE"
    if (manzana and lote and zona) or (via and zona) or referencia:
        return "MEDIA"
    if via or zona or manzana or lote:
        return "MEDIA"
    return "DEBIL"


# ============================================================
# UBIGEO PARA CONSULTA
# ============================================================

def ubicacion_texto(
    distrito,
    provincia,
    departamento,
):
    partes = []

    for valor in [
        distrito,
        provincia,
        departamento,
    ]:
        valor = norm(valor)

        if (
            valor
            and valor not in partes
        ):
            partes.append(
                valor
            )

    partes.append(
        "PERU"
    )

    return ", ".join(
        partes
    )


# ============================================================
# VARIANTES
# ============================================================

def add_variant(
    lista,
    vistos,
    tipo,
    query,
):
    query = re.sub(
        r"\s+",
        " ",
        query or "",
    ).strip(" ,")

    if not query:
        return

    key = norm_key(
        query
    )

    if key in vistos:
        return

    vistos.add(
        key
    )

    lista.append(
        (
            tipo,
            query,
        )
    )


# ============================================================
# CORRECCIONES APRENDIDAS
# ============================================================

def crear_archivo_correcciones_si_no_existe():
    """
    No sobreescribe el archivo si ya existe.
    Puedes seguir agregando nuevas correcciones verificadas.
    """

    if CORRECCIONES_CSV.exists():
        return

    ejemplo = pd.DataFrame(
        [
            {
                "TIPO": "VIA",
                "ORIGINAL": "AL DEL ROMEDO",
                "CORREGIDO": "ALAMEDA DEL REMERO",
                "NOTA": (
                    "Correccion verificada manualmente "
                    "en Google Maps"
                ),
            },
            {
                "TIPO": "ZONA",
                "ORIGINAL": "HUERTOS DE VILLACRUC",
                "CORREGIDO": "LOS HUERTOS DE VILLA",
                "NOTA": (
                    "Correccion verificada manualmente "
                    "en Google Maps"
                ),
            },
        ]
    )

    ejemplo.to_csv(
        CORRECCIONES_CSV,
        index=False,
        encoding="utf-8-sig",
    )


def cargar_correcciones():
    crear_archivo_correcciones_si_no_existe()

    dfc = pd.read_csv(
        CORRECCIONES_CSV,
        dtype=str,
    ).fillna("")

    mapas = {
        "VIA": {},
        "ZONA": {},
    }

    for _, row in dfc.iterrows():
        tipo = norm(
            row.get(
                "TIPO",
                "",
            )
        )

        original = norm_key(
            row.get(
                "ORIGINAL",
                "",
            )
        )

        corregido = norm(
            row.get(
                "CORREGIDO",
                "",
            )
        )

        if (
            tipo in mapas
            and original
            and corregido
        ):
            mapas[tipo][
                original
            ] = corregido

    return mapas


def aplicar_correccion(
    valor,
    tipo,
    correcciones,
):
    original = norm(
        valor
    )

    if not original:
        return ""

    key = norm_key(
        original
    )

    return (
        correcciones
        .get(
            tipo,
            {},
        )
        .get(
            key,
            original,
        )
    )


# ============================================================
# CONSTRUIR VARIANTES DE UNA FILA
# ============================================================

def construir_variantes(
    row,
    correcciones,
):
    direccion = norm(row["Direccion"])
    nombre = norm(row["Nombre ó RazonSocial"])
    distrito = norm(row["Distrito"])
    provincia = norm(row["Provincia"])
    departamento = norm(row["Departamento"])

    sin_ref = quitar_parentesis(direccion)
    referencia = extraer_referencia(direccion)
    direccion_limpia = limpiar_ruido_interno(sin_ref)

    tipo_via, via_original = extraer_via(direccion_limpia)
    # Extraer el número ANTES de quitar piso/interior. Así no perdemos casos
    # como "495 4TO PISO NRO. 495".
    numero = extraer_numero(sin_ref)

    # Direcciones como "CAL. ERNESTO MORA 495 4TO PISO NRO. 495":
    # nunca dejar el número/piso dentro del nombre de la vía.
    if numero and via_original:
        # Si el número de puerta quedó incrustado dentro de VIA_ORIGINAL,
        # recortar desde esa aparición, pero solo cuando antes hay letras
        # (evita destruir nombres numéricos como "CALLE 5").
        m_num = re.search(rf"(?<![A-Z0-9]){re.escape(numero)}(?![A-Z0-9])", norm(via_original))
        if m_num:
            prefijo = norm(via_original)[:m_num.start()].strip()
            if re.search(r"[A-Z]", prefijo):
                via_original = prefijo

    manzana = extraer_manzana(direccion_limpia)
    lote = extraer_lote(direccion_limpia)
    km = extraer_km(direccion_limpia)
    zona_original = extraer_zona(direccion_limpia)

    via_diccionario = aplicar_correccion(
        via_original, "VIA", correcciones
    )

    if norm_key(via_diccionario) != norm_key(via_original):
        via_corregida = via_diccionario
    else:
        via_corregida = expandir_via_interna(via_original)

    zona_corregida = aplicar_correccion(
        zona_original, "ZONA", correcciones
    )

    hubo_correccion = (
        norm_key(via_corregida) != norm_key(via_original)
        or norm_key(zona_corregida) != norm_key(zona_original)
    )

    via_base = via_corregida or via_original

    tipos_via_completos = (
        "AVENIDA", "CALLE", "JIRON", "PASAJE", "CARRETERA",
        "PROLONGACION", "ALAMEDA", "MALECON", "CAMINO", "PASEO",
        "PARQUE", "PLAZA"
    )

    if via_base and any(norm(via_base).startswith(x + " ") or norm(via_base) == x for x in tipos_via_completos):
        via_busqueda = via_base
    elif via_base and tipo_via:
        via_busqueda = f"{tipo_via} {via_base}".strip()
    else:
        via_busqueda = via_base

    zona_busqueda = zona_corregida or zona_original
    loc = ubicacion_texto(distrito, provincia, departamento)

    variantes = []
    vistos = set()

    # --------------------------------------------------------
    # ORDEN DE BÚSQUEDA
    # Primero intentos con máxima precisión. Solo si fallan,
    # el robot llegará a vía / zona / referencia / distrito.
    # --------------------------------------------------------

    # 1) Original SUNAT. Google a veces interpreta mejor el texto original.
    add_variant(
        variantes, vistos, "ORIGINAL",
        f"{direccion}, {loc}"
    )

    # 2) Dirección limpia.
    add_variant(
        variantes, vistos, "DIRECCION_LIMPIA",
        f"{direccion_limpia}, {loc}"
    )

    # 3) Correcciones verificadas.
    if hubo_correccion and via_busqueda and numero and zona_busqueda:
        add_variant(
            variantes, vistos, "CORREGIDA_VIA_NUMERO_ZONA",
            f"{via_busqueda} {numero}, {zona_busqueda}, {loc}"
        )

    if hubo_correccion and via_busqueda and (manzana or lote) and zona_busqueda:
        partes = [via_busqueda]
        if manzana:
            partes.append(f"MZ {manzana}")
        if lote:
            partes.append(f"LT {lote}")
        partes.append(zona_busqueda)
        add_variant(
            variantes, vistos, "CORREGIDA_VIA_MZA_LOTE_ZONA",
            f"{', '.join(partes)}, {loc}"
        )

    # 4) Vía + número + zona.
    if via_busqueda and numero and zona_busqueda:
        add_variant(
            variantes, vistos, "VIA_NUMERO_ZONA",
            f"{via_busqueda} {numero}, {zona_busqueda}, {loc}"
        )

    # 5) Vía + número.
    if via_busqueda and numero:
        add_variant(
            variantes, vistos, "VIA_NUMERO",
            f"{via_busqueda} {numero}, {loc}"
        )

    # 6) Vía + MZ + lote + zona.
    if via_busqueda and (manzana or lote) and zona_busqueda:
        partes = [via_busqueda]
        if manzana:
            partes.append(f"MZ {manzana}")
        if lote:
            partes.append(f"LT {lote}")
        partes.append(zona_busqueda)
        add_variant(
            variantes, vistos, "VIA_MZA_LOTE_ZONA",
            f"{', '.join(partes)}, {loc}"
        )

    # 7) MZ + lote + zona (cuando no hay vía o Google ignora la vía).
    if (manzana or lote) and zona_busqueda:
        partes = []
        if manzana:
            partes.append(f"MZ {manzana}")
        if lote:
            partes.append(f"LT {lote}")
        partes.append(zona_busqueda)
        add_variant(
            variantes, vistos, "MZA_LOTE_ZONA",
            f"{', '.join(partes)}, {loc}"
        )

    # 8) Carretera + KM.
    if km:
        add_variant(
            variantes, vistos, "CARRETERA_KM",
            f"{direccion_limpia}, {loc}"
        )

    # 9) Cruce / referencia textual completa.
    if referencia:
        ref_norm = norm(referencia)
        if any(x in ref_norm for x in (" CRUCE ", " CON ", " ESQ", "ESQUINA")):
            add_variant(
                variantes, vistos, "INTERSECCION_REFERENCIA",
                f"{referencia}, {loc}"
            )

    # 10) Vía + zona.
    if via_busqueda and zona_busqueda:
        add_variant(
            variantes, vistos, "VIA_ZONA",
            f"{via_busqueda}, {zona_busqueda}, {loc}"
        )

    # 11) Vía + referencia.
    if via_busqueda and referencia:
        add_variant(
            variantes, vistos, "VIA_REFERENCIA",
            f"{via_busqueda}, {referencia}, {loc}"
        )

    # 12) Zona + referencia.
    if zona_busqueda and referencia:
        add_variant(
            variantes, vistos, "ZONA_REFERENCIA",
            f"{zona_busqueda}, {referencia}, {loc}"
        )

    # 13) Razón social: útil para negocios reales, pero el robot no la
    # considerará predial por sí sola si la dirección no coincide.
    if nombre:
        add_variant(
            variantes, vistos, "RAZON_SOCIAL",
            f"{nombre}, {distrito}, {provincia}, {departamento}, PERU"
        )

    # 14) Solo vía: fallback importante.
    if via_busqueda:
        add_variant(
            variantes, vistos, "SOLO_VIA",
            f"{via_busqueda}, {loc}"
        )

    # 15) Solo zona / AH / urbanización / asociación / cooperativa.
    if zona_busqueda:
        add_variant(
            variantes, vistos, "SOLO_ZONA",
            f"{zona_busqueda}, {loc}"
        )

    # 16) Referencia / POI.
    if referencia:
        add_variant(
            variantes, vistos, "SOLO_REFERENCIA",
            f"{referencia}, {loc}"
        )

    variantes = variantes[:MAX_VARIANTES]

    tipo_direccion = clasificar_direccion(
        direccion_limpia,
        via_busqueda,
        numero,
        manzana,
        lote,
        km,
    )

    calidad = calidad_inicial(
        tipo_direccion,
        via_busqueda,
        numero,
        manzana,
        lote,
        zona_busqueda,
        referencia,
    )

    return {
        "direccion_limpia": direccion_limpia,
        "referencia": referencia,
        "tipo_direccion": tipo_direccion,
        "calidad_inicial": calidad,
        "tipo_via": tipo_via,
        "via_original": via_original,
        "via_corregida": via_busqueda,
        "numero_buscado": numero,
        "manzana": manzana,
        "lote": lote,
        "km": km,
        "zona_original": zona_original,
        "zona_corregida": zona_busqueda,
        "hubo_correccion": int(hubo_correccion),
        "variantes": variantes,
    }


# ============================================================
# COLUMNAS DE SALIDA
# ============================================================

def columnas_salida():
    columnas = [
        "FILA_ID",
        "BLOQUE_LAPTOP",
        "ORDEN_LAPTOP",
        "NumeroRuc",
        "Nombre ó RazonSocial",
        "Departamento",
        "Provincia",
        "Distrito",
        "Direccion",
        "DIRECCION_KEY",
        "TIPO_DIRECCION",
        "CALIDAD_INICIAL",
        "DIRECCION_LIMPIA",
        "REFERENCIA",
        "TIPO_VIA",
        "VIA_ORIGINAL",
        "VIA_CORREGIDA",
        "NUMERO_BUSCADO",
        "MANZANA",
        "LOTE",
        "KM",
        "ZONA_ORIGINAL",
        "ZONA_CORREGIDA",
        "HUBO_CORRECCION",
    ]

    for i in range(1, MAX_VARIANTES + 1):
        columnas.extend([f"Q{i}_TIPO", f"Q{i}"])

    return columnas


# ============================================================
# MAIN - PROCESAMIENTO POR BLOQUES
# ============================================================

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not INPUT_CSV.exists():
        raise FileNotFoundError(f"No existe el archivo:\n{INPUT_CSV}")

    correcciones = cargar_correcciones()

    columnas_archivo = pd.read_csv(INPUT_CSV, nrows=0).columns.tolist()
    faltantes = [c for c in COLUMNAS_ENTRADA if c not in columnas_archivo]
    if faltantes:
        raise ValueError(f"Faltan columnas: {faltantes}")

    # --------------------------------------------------------
    # Primera pasada muy ligera: conocer N exacto para dividir 50/50.
    # --------------------------------------------------------
    total_filas = 0
    for chunk in pd.read_csv(
        INPUT_CSV,
        dtype=str,
        usecols=["NumeroRuc"],
        chunksize=CHUNKSIZE,
        keep_default_na=False,
    ):
        total_filas += len(chunk)

    corte_laptop1 = (total_filas + 1) // 2
    inicio_laptop2 = corte_laptop1 + 1

    print("=" * 100)
    print("PREPARADOR GOOGLE MASIVO - NUMERACION Y DIVISION EN 2 LAPTOPS")
    print("=" * 100)
    print(f"Entrada      : {INPUT_CSV}")
    print(f"Total filas  : {total_filas:,}")
    print(f"Laptop 1     : FILA_ID 1 -> {corte_laptop1:,} (ASC)")
    print(f"Laptop 2     : FILA_ID {total_filas:,} -> {inicio_laptop2:,} (DESC)")
    print(f"Salida CSV   : {OUT_PREPARADO}")
    print(f"Salida SQLite: {OUT_SQLITE}")
    print("=" * 100)

    for p in [OUT_PREPARADO, OUT_DIAGNOSTICO, DB_KEYS, OUT_SQLITE, OUT_MANIFEST]:
        if p.exists():
            p.unlink()

    # DB temporal para direcciones únicas.
    conn_keys = sqlite3.connect(DB_KEYS)
    conn_keys.execute("CREATE TABLE direccion_keys (direccion_key TEXT PRIMARY KEY)")
    conn_keys.commit()

    # DB preparado: será la fuente del robot y permite leer en ASC/DESC sin RAM.
    conn_out = sqlite3.connect(OUT_SQLITE)
    conn_out.execute("PRAGMA journal_mode=WAL")
    conn_out.execute("PRAGMA synchronous=NORMAL")

    resumen_tipos = {}
    resumen_calidad = {}
    total_procesado = 0
    primera_escritura = True
    columnas_out = columnas_salida()

    lector = pd.read_csv(
        INPUT_CSV,
        dtype=str,
        usecols=COLUMNAS_ENTRADA,
        chunksize=CHUNKSIZE,
        keep_default_na=False,
    )

    for numero_chunk, chunk in enumerate(lector, start=1):
        salida_chunk = []

        for _, row in chunk.iterrows():
            fila_id = total_procesado + len(salida_chunk) + 1
            bloque = 1 if fila_id <= corte_laptop1 else 2
            orden_laptop = (
                fila_id
                if bloque == 1
                else total_filas - fila_id + 1
            )

            info = construir_variantes(row, correcciones)

            key_text = "|".join([
                norm_key(row["Direccion"]),
                norm_key(row["Distrito"]),
                norm_key(row["Provincia"]),
                norm_key(row["Departamento"]),
            ])
            direccion_key = hashlib.sha1(key_text.encode("utf-8")).hexdigest()

            conn_keys.execute(
                "INSERT OR IGNORE INTO direccion_keys(direccion_key) VALUES (?)",
                (direccion_key,),
            )

            tipo = info["tipo_direccion"]
            calidad = info["calidad_inicial"]
            resumen_tipos[tipo] = resumen_tipos.get(tipo, 0) + 1
            resumen_calidad[calidad] = resumen_calidad.get(calidad, 0) + 1

            registro = {
                "FILA_ID": fila_id,
                "BLOQUE_LAPTOP": bloque,
                "ORDEN_LAPTOP": orden_laptop,
                "NumeroRuc": row["NumeroRuc"],
                "Nombre ó RazonSocial": row["Nombre ó RazonSocial"],
                "Departamento": row["Departamento"],
                "Provincia": row["Provincia"],
                "Distrito": row["Distrito"],
                "Direccion": row["Direccion"],
                "DIRECCION_KEY": direccion_key,
                "TIPO_DIRECCION": tipo,
                "CALIDAD_INICIAL": calidad,
                "DIRECCION_LIMPIA": info["direccion_limpia"],
                "REFERENCIA": info["referencia"],
                "TIPO_VIA": info["tipo_via"],
                "VIA_ORIGINAL": info["via_original"],
                "VIA_CORREGIDA": info["via_corregida"],
                "NUMERO_BUSCADO": info["numero_buscado"],
                "MANZANA": info["manzana"],
                "LOTE": info["lote"],
                "KM": info["km"],
                "ZONA_ORIGINAL": info["zona_original"],
                "ZONA_CORREGIDA": info["zona_corregida"],
                "HUBO_CORRECCION": info["hubo_correccion"],
            }

            for i in range(1, MAX_VARIANTES + 1):
                registro[f"Q{i}_TIPO"] = ""
                registro[f"Q{i}"] = ""

            for i, (tipo_query, query) in enumerate(info["variantes"], start=1):
                registro[f"Q{i}_TIPO"] = tipo_query
                registro[f"Q{i}"] = query

            salida_chunk.append(registro)

        df_chunk = pd.DataFrame(salida_chunk, columns=columnas_out)

        df_chunk.to_csv(
            OUT_PREPARADO,
            mode="w" if primera_escritura else "a",
            header=primera_escritura,
            index=False,
            encoding="utf-8-sig",
        )

        df_chunk.to_sql(
            "preparadas",
            conn_out,
            if_exists="replace" if primera_escritura else "append",
            index=False,
        )

        primera_escritura = False
        total_procesado += len(df_chunk)
        conn_keys.commit()
        conn_out.commit()

        print(f"Chunk {numero_chunk:,} | preparados: {total_procesado:,}/{total_filas:,}")

        del salida_chunk, df_chunk, chunk

    conn_out.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_preparadas_fila ON preparadas(FILA_ID)"
    )
    conn_out.execute(
        "CREATE INDEX IF NOT EXISTS idx_preparadas_bloque ON preparadas(BLOQUE_LAPTOP, ORDEN_LAPTOP)"
    )
    conn_out.execute(
        "CREATE INDEX IF NOT EXISTS idx_preparadas_ruc ON preparadas(NumeroRuc)"
    )
    conn_out.commit()
    conn_out.close()

    direcciones_unicas = conn_keys.execute(
        "SELECT COUNT(*) FROM direccion_keys"
    ).fetchone()[0]
    conn_keys.close()

    diagnostico_rows = []
    for tipo, cantidad in sorted(resumen_tipos.items(), key=lambda x: x[1], reverse=True):
        diagnostico_rows.append({
            "DIMENSION": "TIPO_DIRECCION",
            "CATEGORIA": tipo,
            "REGISTROS": cantidad,
            "PORCENTAJE": round(cantidad / total_filas * 100, 4) if total_filas else 0,
        })
    for calidad, cantidad in sorted(resumen_calidad.items(), key=lambda x: x[1], reverse=True):
        diagnostico_rows.append({
            "DIMENSION": "CALIDAD_INICIAL",
            "CATEGORIA": calidad,
            "REGISTROS": cantidad,
            "PORCENTAJE": round(cantidad / total_filas * 100, 4) if total_filas else 0,
        })

    pd.DataFrame(diagnostico_rows).to_csv(
        OUT_DIAGNOSTICO,
        index=False,
        encoding="utf-8-sig",
    )

    manifest = {
        "input_csv": str(INPUT_CSV),
        "total_filas": total_filas,
        "direcciones_unicas": direcciones_unicas,
        "max_variantes": MAX_VARIANTES,
        "laptop_1": {
            "desde": 1,
            "hasta": corte_laptop1,
            "sentido": "ASC",
            "cantidad": corte_laptop1,
        },
        "laptop_2": {
            "desde": total_filas,
            "hasta": inicio_laptop2,
            "sentido": "DESC",
            "cantidad": total_filas - corte_laptop1,
        },
        "archivo_sqlite": str(OUT_SQLITE),
        "archivo_csv": str(OUT_PREPARADO),
    }
    OUT_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print("=" * 100)
    print("PREPARACION TERMINADA")
    print("=" * 100)
    print(f"Registros             : {total_filas:,}")
    print(f"Direcciones únicas    : {direcciones_unicas:,}")
    print(f"Laptop 1              : 1 -> {corte_laptop1:,} (ASC)")
    print(f"Laptop 2              : {total_filas:,} -> {inicio_laptop2:,} (DESC)")
    print(f"SQLite preparado      : {OUT_SQLITE}")
    print(f"CSV preparado         : {OUT_PREPARADO}")
    print(f"Manifest              : {OUT_MANIFEST}")
    print(f"Diagnóstico           : {OUT_DIAGNOSTICO}")
    print("=" * 100)

    try:
        DB_KEYS.unlink()
    except Exception:
        pass


if __name__ == "__main__":
    main()
