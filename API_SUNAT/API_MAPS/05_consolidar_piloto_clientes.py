# -*- coding: utf-8 -*-
"""
PASO 5: Consolidación prudente de geolocalización piloto de clientes.

- Lee el resultado territorial y la preparación original (solo lectura).
- Conserva los identificadores originales.
- Exporta una fila por FILA_ID de la prueba.
- NO confunde punto dentro del distrito con domicilio exacto.
- NO sobrescribe SQLite ni los CSV de auditoría.
- NO realiza nuevas búsquedas ni llamadas de red.

Ejecutar dentro de API_MAPS:
    python -u 05_consolidar_piloto_clientes.py
"""
from pathlib import Path
import sqlite3
import pandas as pd

BASE = Path(__file__).resolve().parent
DIR = BASE / "clientes_direcciones"
SQLITE_PREP = DIR / "direcciones_preparadas_google_masivo.sqlite"
CSV_TERRITORIAL = DIR / "auditoria_territorial_resumen.csv"
CSV_SALIDA = DIR / "piloto_clientes_geolocalizacion_control_calidad.csv"
CSV_CONTROL = DIR / "piloto_clientes_resumen_calidad.csv"


def caracterizar(row):
    decision = str(row.get("DECISION_TERRITORIAL") or "")
    tipo = str(row.get("TIPO_CANDIDATO") or "")
    if decision == "DIRECCION_INVALIDA_NO_USAR":
        return "DIRECCION_INVALIDA", "NO_USAR", "No es una dirección susceptible de geocodificación"
    if decision == "CANDIDATO_EN_DISTRITO_NO_PREDIAL":
        if tipo == "NUMERO_COINCIDENTE_SIN_VALIDAR":
            return (
                "COINCIDENCIA_VIA_NUMERO_POR_VERIFICAR",
                "REFERENCIA_PROVISIONAL",
                "Coincidencia textual de vía y número en el distrito; no identifica predio certificado",
            )
        if tipo == "MANZANA_LOTE_SIN_VALIDAR":
            return (
                "COINCIDENCIA_MZ_LOTE_POR_VERIFICAR",
                "REFERENCIA_PROVISIONAL",
                "Manzana/lote textual en distrito; requiere verificación de la urbanización y predio",
            )
        if tipo == "REFERENCIA_VIA_SIN_VALIDAR":
            return (
                "REFERENCIA_VIA",
                "REFERENCIA_PROVISIONAL",
                "Ubicación de calle en distrito, no domicilio exacto",
            )
        if tipo == "REFERENCIA_ZONA_SIN_VALIDAR":
            return (
                "REFERENCIA_ZONA",
                "REFERENCIA_PROVISIONAL",
                "Ubicación de zona o urbanización en distrito, no domicilio exacto",
            )
        return "REFERENCIA_TERRITORIAL", "REFERENCIA_PROVISIONAL", "Candidato territorial no predial"
    if decision == "ORIGINAL_EN_DISTRITO_SIN_EVIDENCIA_TEXTUAL":
        return (
            "REVISION_SIN_EVIDENCIA_TEXTUAL",
            "NO_USAR",
            "La coordenada original cae en distrito, pero falta evidencia de la dirección",
        )
    if decision == "REVISAR_NOMBRES_UBIGEO_SIN_POLIGONO":
        return (
            "SIN_POLIGONO_IDENTIFICABLE",
            "NO_USAR",
            "No se pudo hacer correspondencia inequívoca con el límite distrital",
        )
    return (
        "SIN_CANDIDATO_CONFIABLE",
        "NO_USAR",
        "No se identificó un candidato con evidencia textual y territorial suficiente",
    )


def main():
    for f in (SQLITE_PREP, CSV_TERRITORIAL):
        if not f.is_file():
            raise FileNotFoundError(f"Falta archivo: {f}")

    territorial = pd.read_csv(CSV_TERRITORIAL, sep=";", encoding="utf-8-sig", dtype={"FILA_ID": "int64"})
    if territorial.empty:
        raise RuntimeError("El resumen territorial no contiene direcciones")
    if territorial["FILA_ID"].duplicated().any():
        raise RuntimeError("El resumen territorial tiene FILA_ID repetidos")

    with sqlite3.connect(f"file:{SQLITE_PREP.as_posix()}?mode=ro", uri=True) as conn:
        campos_disponibles = {r[1] for r in conn.execute("PRAGMA table_info(preparadas)")}
        requeridos = {"FILA_ID", "Direccion", "Departamento", "Provincia", "Distrito"}
        faltantes = requeridos - campos_disponibles
        if faltantes:
            raise RuntimeError(f"Faltan columnas obligatorias en SQLite: {sorted(faltantes)}")
        campos = ["FILA_ID", "FILA_ORIGINAL", "personal_id", "customer_id", "taxpayer_id", "ruc_id",
                  "Direccion", "Departamento", "Provincia", "Distrito"]
        seleccion = ", ".join(
            f'"{c}"' if c in campos_disponibles else f'NULL AS "{c}"' for c in campos
        )
        prep = pd.read_sql_query(f"SELECT {seleccion} FROM preparadas", conn)

    if prep["FILA_ID"].duplicated().any():
        raise RuntimeError("La base preparada tiene FILA_ID repetidos")

    # Los nombres administrativos de la fuente original prevalecen.
    territorial = territorial.drop(columns=["Direccion", "Departamento", "Provincia", "Distrito"], errors="ignore")
    df = prep.merge(territorial, on="FILA_ID", how="inner", validate="one_to_one")
    if len(df) != len(territorial):
        raise RuntimeError("No se recuperaron todas las filas territoriales mediante FILA_ID")

    resultados = df.apply(caracterizar, axis=1, result_type="expand")
    resultados.columns = ["CALIDAD_GEO", "USO_GEO", "MOTIVO_CALIDAD"]
    df = pd.concat([df.reset_index(drop=True), resultados.reset_index(drop=True)], axis=1)

    # Utilizar exclusivamente candidato con comprobación territorial + evidencia textual.
    apto = df["USO_GEO"].eq("REFERENCIA_PROVISIONAL")
    df["LATITUD_RESULTADO"] = pd.to_numeric(df["LATITUD_TERRITORIAL"], errors="coerce").where(apto)
    df["LONGITUD_RESULTADO"] = pd.to_numeric(df["LONGITUD_TERRITORIAL"], errors="coerce").where(apto)
    df["APTO_MAPA_REFERENCIAL"] = (apto & df["LATITUD_RESULTADO"].notna() & df["LONGITUD_RESULTADO"].notna()).astype(int)
    df["APTO_DISTANCIA_A_OFICINA"] = 0
    df["UBICACION_PREDIAL_VALIDADA"] = 0

    # Incluimos original y propuesta para auditoría y análisis posterior.
    cols = [
        "FILA_ID", "FILA_ORIGINAL", "personal_id", "customer_id", "taxpayer_id", "ruc_id",
        "Departamento", "Provincia", "Distrito", "Direccion", "CALIDAD_GEO", "USO_GEO",
        "LATITUD_RESULTADO", "LONGITUD_RESULTADO", "APTO_MAPA_REFERENCIAL",
        "APTO_DISTANCIA_A_OFICINA", "UBICACION_PREDIAL_VALIDADA",
        "NOMBRE_CANDIDATO_TERRITORIAL", "TIPO_CANDIDATO", "DECISION_TERRITORIAL",
        "LATITUD_ORIGINAL", "LONGITUD_ORIGINAL", "TERRITORIO_ORIGINAL",
        "LATITUD_PROPUESTA_PREVIA", "LONGITUD_PROPUESTA_PREVIA",
        "TERRITORIO_PROPUESTA_PREVIA", "MOTIVO_CALIDAD"
    ]
    cols = [c for c in cols if c in df.columns]
    salida = df[cols].sort_values("FILA_ID")
    salida.to_csv(CSV_SALIDA, sep=";", index=False, encoding="utf-8-sig")

    resumen = (salida.groupby(["CALIDAD_GEO", "USO_GEO"], dropna=False)
              .agg(DIRECCIONES=("FILA_ID", "count"),
                   CON_COORDENADA_PROVISIONAL=("APTO_MAPA_REFERENCIAL", "sum"))
              .reset_index().sort_values("DIRECCIONES", ascending=False))
    resumen.to_csv(CSV_CONTROL, sep=";", index=False, encoding="utf-8-sig")

    print("=" * 92)
    print("PASO 5 - CONSOLIDACION DE PILOTO CLIENTES (SIN SOBREESCRIBIR FUENTES)")
    print("=" * 92)
    print(f"Direcciones del piloto: {len(salida):,}")
    print(f"Referencias territoriales provisionales: {int(salida['APTO_MAPA_REFERENCIAL'].sum()):,}")
    print(f"Predios certificados: {int(salida['UBICACION_PREDIAL_VALIDADA'].sum())}")
    print("\nCLASIFICACION:")
    print(resumen.to_string(index=False))
    print("\nCASOS PARA VERIFICAR:")
    cols_m = ["FILA_ID", "Distrito", "Direccion", "CALIDAD_GEO", "LATITUD_RESULTADO", "LONGITUD_RESULTADO", "TERRITORIO_ORIGINAL"]
    print(salida[salida['FILA_ID'].isin([4,9,13,16,19,20])][cols_m].to_string(index=False))
    print("\nArchivo de direcciones:", CSV_SALIDA)
    print("Archivo resumen:", CSV_CONTROL)
    print("\nATENCION: los puntos son REFERENCIALES; no usarlos para distancia a oficina o ubicación exacta de empresa.")


if __name__ == "__main__":
    main()
