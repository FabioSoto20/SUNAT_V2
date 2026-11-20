# -*- coding: utf-8 -*-
"""
PASO 11 | Catálogo INEI y geolocalización distrital provisional de clientes.

- Lee API_SUNAT/direcciones.csv (todas las filas, incluso sin dirección).
- Construye un punto REPRESENTATIVO INTERIOR por distrito a partir de DISTRITO.gpkg.
- Cruza estrictamente DEPARTAMENTO + PROVINCIA + DISTRITO, normalizando
  acentos, mayúsculas, espacios y puntuación; NO usa coincidencia difusa.
- NO consulta Google Maps ni modifica ninguna SQLite, CSV original ni auditoría.
- Genera catálogo, base provisional completa y diagnóstico de no coincidentes.

La coordenada distrital no es la localización del predio/empresa.
NO apta para distancias a oficinas ni para afirmar que una empresa está en ese punto.
"""

from pathlib import Path
from collections import Counter
import re
import unicodedata
import sys
import pandas as pd

try:
    import geopandas as gpd
    from shapely.ops import unary_union
    from shapely.validation import make_valid
except ImportError as exc:
    raise SystemExit(
        "Falta una dependencia geográfica. En Jupyter ejecuta: "
        "%pip install geopandas shapely pyogrio\n" + str(exc)
    )

ROOT = Path(__file__).resolve().parent
PROYECTO = ROOT.parent
CARPETA_CLIENTES = ROOT / "clientes_direcciones"
CARPETA_INEI = CARPETA_CLIENTES / "limites_inei"
ENTRADA_CSV = PROYECTO / "direcciones.csv"

SALIDA_CATALOGO = CARPETA_CLIENTES / "catalogo_centros_representativos_inei.csv"
SALIDA_BASE = CARPETA_CLIENTES / "clientes_base_geografica_provisional.csv"
SALIDA_PENDIENTES = CARPETA_CLIENTES / "diagnostico_distritos_sin_coincidencia.csv"

COLUMNAS_ORIGEN = [
    "personal_id", "customer_id", "taxpayer_id", "ruc_id",
    "adr_department_name", "adr_province_name", "adr_district_name",
    "Direccion Completa",
]
CHUNK = 20000


def normalizar(valor):
    """Solo equivalencias tipográficas seguras; no corrige distritos por similitud."""
    if valor is None or pd.isna(valor):
        return ""
    s = str(valor).strip().upper()
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalizar_departamento(valor):
    s = normalizar(valor)
    # Diferencias de escritura comunes para la misma región administrativa.
    if s in {"PROVINCIA CONSTITUCIONAL DEL CALLAO", "PROV CONST DEL CALLAO",
             "PROVINCIA CONST DEL CALLAO", "REGION CALLAO"}:
        return "CALLAO"
    return s


def columna(coleccion, candidatas):
    canon = {normalizar(x).replace(" ", ""): x for x in coleccion}
    for propuesta in candidatas:
        encontrado = canon.get(normalizar(propuesta).replace(" ", ""))
        if encontrado is not None:
            return encontrado
    return None


def localizar_capa():
    if not CARPETA_INEI.is_dir():
        raise FileNotFoundError(f"No existe carpeta de límites INEI: {CARPETA_INEI}")
    capas = sorted(CARPETA_INEI.rglob("*.gpkg"))
    if not capas:
        capas = sorted(CARPETA_INEI.rglob("*.shp"))
    if not capas:
        raise FileNotFoundError("No hay .gpkg o .shp dentro de " + str(CARPETA_INEI))
    preferida = [c for c in capas if "DIST" in c.stem.upper()]
    return (preferida or capas)[0]


def unir_geometrias(geometrias):
    validas = []
    for geom in geometrias:
        if geom is None or geom.is_empty:
            continue
        if not geom.is_valid:
            geom = make_valid(geom)
        if geom is not None and not geom.is_empty:
            validas.append(geom)
    if not validas:
        return None
    if len(validas) == 1:
        return validas[0]
    return unary_union(validas)


def construir_catalogo():
    ruta = localizar_capa()
    print(f"Capa geográfica: {ruta}")
    geo = gpd.read_file(ruta)
    if geo.empty or geo.crs is None:
        raise RuntimeError("La capa INEI está vacía o no declara su CRS.")
    dep = columna(geo.columns, ["nombdep", "departamento", "nom_dep", "nomdpto"])
    prov = columna(geo.columns, ["nombprov", "provincia", "nom_prov"])
    dist = columna(geo.columns, ["nombdist", "distrito", "nom_dist"])
    if not (dep and prov and dist):
        raise RuntimeError(
            "No se identificaron los campos administrativos. "
            f"Columnas reales: {list(geo.columns)}"
        )
    if geo.crs.to_epsg() != 4326:
        geo = geo.to_crs("EPSG:4326")
    print(f"Polígonos INEI leídos: {len(geo):,} | CRS: {geo.crs}")
    geo = geo[[dep, prov, dist, "geometry"]].copy()
    geo["_DEP"] = geo[dep].map(normalizar_departamento)
    geo["_PROV"] = geo[prov].map(normalizar)
    geo["_DIST"] = geo[dist].map(normalizar)

    resultado = []
    problemas = []
    for key, grupo in geo.groupby(["_DEP", "_PROV", "_DIST"], sort=True):
        d, p, t = key
        if not all(key):
            problemas.append((key, "Nombre administrativo vacío"))
            continue
        try:
            geom = unir_geometrias(grupo.geometry.tolist())
            if geom is None:
                problemas.append((key, "Geometría ausente"))
                continue
            # Punto en el interior. Un centroide geométrico puede caer fuera
            # del distrito; representative_point() sí cae dentro.
            punto = geom.representative_point()
            if punto.is_empty or not geom.covers(punto):
                problemas.append((key, "No pudo calcularse punto interior"))
                continue
            resultado.append({
                "Departamento_INEI": str(grupo.iloc[0][dep]),
                "Provincia_INEI": str(grupo.iloc[0][prov]),
                "Distrito_INEI": str(grupo.iloc[0][dist]),
                "DEP_KEY": d, "PROV_KEY": p, "DIST_KEY": t,
                "LATITUD_DISTRITO": round(float(punto.y), 7),
                "LONGITUD_DISTRITO": round(float(punto.x), 7),
                "METODO": "PUNTO_INTERIOR_POLIGONO_INEI",
                "PRECISION": "DISTRITAL_BAJA",
                "NOTA": "Punto representativo; no es domicilio ni necesariamente zona urbana",
            })
        except Exception as exc:
            problemas.append((key, str(exc)[:150]))

    catalogo = pd.DataFrame(resultado)
    if catalogo.empty:
        raise RuntimeError("No pudo construirse el catálogo distrital.")
    keys = ["DEP_KEY", "PROV_KEY", "DIST_KEY"]
    if catalogo.duplicated(keys).any():
        raise RuntimeError("Hay claves administrativas duplicadas en el catálogo.")
    catalogo.to_csv(SALIDA_CATALOGO, sep=";", index=False,
                    encoding="utf-8-sig")
    print(f"Distritos con punto interior: {len(catalogo):,}")
    if problemas:
        print(f"ADVERTENCIA: {len(problemas)} divisiones sin punto. Ejemplos:")
        for k, err in problemas[:5]:
            print(" -", k, err)
    return catalogo


def procesar_clientes(catalogo):
    if not ENTRADA_CSV.is_file():
        raise FileNotFoundError(f"No existe: {ENTRADA_CSV}")
    indices = {
        (r.DEP_KEY, r.PROV_KEY, r.DIST_KEY):
        (r.LATITUD_DISTRITO, r.LONGITUD_DISTRITO)
        for r in catalogo.itertuples(index=False)
    }
    total = 0
    n_asignadas = 0
    n_con_dir = 0
    n_sin_dir = 0
    con_dir_asignadas = 0
    sin_dir_asignadas = 0
    estados = Counter()
    faltantes = Counter()
    primera = True

    lector = pd.read_csv(
        ENTRADA_CSV, sep=";", dtype=str, keep_default_na=False,
        encoding="utf-8-sig", chunksize=CHUNK
    )
    for numero_bloque, bloque in enumerate(lector, 1):
        if any(c not in bloque.columns for c in COLUMNAS_ORIGEN):
            raise RuntimeError("El CSV no tiene las 8 columnas esperadas: " +
                               str(list(bloque.columns)))
        # FILA_ORIGINAL: empieza en 1 y conserva la posición del CSV original.
        bloque = bloque[COLUMNAS_ORIGEN].copy()
        n = len(bloque)
        bloque.insert(0, "FILA_ORIGINAL", range(total + 1, total + n + 1))
        claves = list(zip(
            bloque["adr_department_name"].map(normalizar_departamento),
            bloque["adr_province_name"].map(normalizar),
            bloque["adr_district_name"].map(normalizar),
        ))
        coord = [indices.get(k) for k in claves]
        presentes = [all(k) for k in claves]
        ok = [c is not None for c in coord]
        estados_fila = [
            "DISTRITO_INEI_ENCONTRADO" if v else
            "FALTAN_DATOS_ADMINISTRATIVOS" if not tiene else
            "SIN_COINCIDENCIA_INEI"
            for v, tiene in zip(ok, presentes)
        ]
        con_dir = bloque["Direccion Completa"].str.strip().ne("")
        n_con_dir += int(con_dir.sum())
        n_sin_dir += int((~con_dir).sum())
        n_asignadas += sum(ok)
        con_dir_asignadas += sum(v and bool(c) for v, c in zip(ok, con_dir))
        sin_dir_asignadas += sum(v and not bool(c) for v, c in zip(ok, con_dir))
        estados.update(estados_fila)
        faltantes.update(k for k, st in zip(claves, estados_fila)
                         if st == "SIN_COINCIDENCIA_INEI")

        bloque["LATITUD_FINAL"] = [c[0] if c else None for c in coord]
        bloque["LONGITUD_FINAL"] = [c[1] if c else None for c in coord]
        bloque["NIVEL_GEO"] = ["DISTRITO" if v else "SIN_COORDENADA" for v in ok]
        bloque["FUENTE_GEO"] = ["INEI" if v else "" for v in ok]
        bloque["METODO_GEO"] = ["PUNTO_INTERIOR_POLIGONO" if v else "" for v in ok]
        bloque["TIPO_COORDENADA"] = ["IMPUTADA_DISTRITO" if v else "NO_DISPONIBLE" for v in ok]
        bloque["ESTADO_GEO"] = estados_fila
        bloque["PRECISION_GEO"] = ["BAJA" if v else "NO_DISPONIBLE" for v in ok]
        bloque["CALIDAD_GEO"] = ["DISTRITO_IMPUTADO" if v else "SIN_COORDENADA" for v in ok]
        bloque["PENDIENTE_BUSQUEDA_DETALLE"] = con_dir.astype(int)
        bloque["APTO_DISTANCIA_A_OFICINA"] = 0
        bloque["APTO_MAPA_TERRITORIAL"] = [1 if v else 0 for v in ok]

        bloque.to_csv(SALIDA_BASE, sep=";", index=False,
                      mode="w" if primera else "a", header=primera,
                      encoding="utf-8-sig" if primera else "utf-8")
        primera = False
        total += n
        print(f"Bloque {numero_bloque}: {total:,} registros procesados | "
              f"{n_asignadas:,} con respaldo distrital", flush=True)

    diagnostico = pd.DataFrame([
        {"DEP_KEY": k[0], "PROV_KEY": k[1], "DIST_KEY": k[2],
         "REGISTROS": cantidad, "MOTIVO": "SIN_COINCIDENCIA_INEI"}
        for k, cantidad in faltantes.most_common()
    ], columns=["DEP_KEY", "PROV_KEY", "DIST_KEY", "REGISTROS", "MOTIVO"])
    diagnostico.to_csv(SALIDA_PENDIENTES, sep=";", index=False,
                       encoding="utf-8-sig")
    print("\n" + "=" * 87)
    print("RESUMEN DEL RESPALDO DISTRITAL - BASE PROVISIONAL")
    print("=" * 87)
    print(f"Total registros originales:                  {total:,}")
    print(f"Con dirección informada:                    {n_con_dir:,}")
    print(f"Sin dirección informada:                    {n_sin_dir:,}")
    print(f"Con coordenada distrital provisional:       {n_asignadas:,}")
    print(f"Con dirección y respaldo distrital:         {con_dir_asignadas:,}")
    print(f"Sin dirección y con respaldo distrital:     {sin_dir_asignadas:,}")
    print(f"Sin respaldo distrital:                    {total-n_asignadas:,}")
    print(f"Cobertura provisional:                     {n_asignadas/total:.2%}" if total else "Sin registros")
    print("\nEstados:")
    for estado, cantidad in estados.most_common():
        print(f"  {estado:<37} {cantidad:,}")
    print("\nArchivos generados:")
    print("  Catálogo INEI: ", SALIDA_CATALOGO)
    print("  Base clientes:", SALIDA_BASE)
    print("  No coinciden: ", SALIDA_PENDIENTES)
    print("\nIMPORTANTE: estas son COORDENADAS DISTRITALES IMPUTADAS, NO ubicaciones")
    print("de empresas. No usarlas para calcular distancias o asignar oficinas cercanas.")


def main():
    print("=" * 87)
    print("PASO 11 - CATÁLOGO INEI Y RESPALDO GEO PARA TODAS LAS DIRECCIONES")
    print("=" * 87)
    CARPETA_CLIENTES.mkdir(parents=True, exist_ok=True)
    for path in (SALIDA_CATALOGO, SALIDA_BASE, SALIDA_PENDIENTES):
        if path.exists():
            raise FileExistsError(
                f"Para no sobrescribir salidas previas, no se ejecuta: {path}\n"
                "Renombra o mueve el archivo anterior si deseas regenerarlo."
            )
    catalogo = construir_catalogo()
    procesar_clientes(catalogo)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\nERROR:", type(e).__name__, str(e), file=sys.stderr)
        sys.exit(1)
