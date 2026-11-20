import pandas as pd
import config


def final_location(row):
    lat_exact = str(row.get("LATITUD_EXACTA", "") or "").strip()
    lon_exact = str(row.get("LONGITUD_EXACTA", "") or "").strip()

    if lat_exact and lon_exact:
        return pd.Series(
            {
                "LATITUD_FINAL": lat_exact,
                "LONGITUD_FINAL": lon_exact,
                "TIPO_UBICACION_FINAL": "UBICACION_EXACTA",
                "NIVEL_UBICACION_FINAL": row.get(
                    "NIVEL_UBICACION_EXACTA",
                    "PREDIAL_LOTE",
                ),
                "CONFIANZA_FINAL": row.get(
                    "CONFIANZA_EXACTA",
                    "ALTA",
                ),
                "FUENTE_FINAL": row.get(
                    "FUENTE_COORDENADAS",
                    "GOOGLE_MAPS",
                ),
                "DETALLE_FINAL": row.get(
                    "RAZON_DECISION",
                    "",
                ),
            }
        )

    lat_ref = str(row.get("LATITUD_REFERENCIA", "") or "").strip()
    lon_ref = str(row.get("LONGITUD_REFERENCIA", "") or "").strip()

    if lat_ref and lon_ref:
        return pd.Series(
            {
                "LATITUD_FINAL": lat_ref,
                "LONGITUD_FINAL": lon_ref,
                "TIPO_UBICACION_FINAL": "UBICACION_REFERENCIA",
                "NIVEL_UBICACION_FINAL": row.get(
                    "NIVEL_UBICACION_REFERENCIA",
                    "",
                ),
                "CONFIANZA_FINAL": row.get(
                    "CONFIANZA_REFERENCIA",
                    "",
                ),
                "FUENTE_FINAL": row.get(
                    "FUENTE_REFERENCIA",
                    "",
                ),
                "DETALLE_FINAL": row.get(
                    "EVIDENCIA_REFERENCIA",
                    "",
                ),
            }
        )

    return pd.Series(
        {
            "LATITUD_FINAL": "",
            "LONGITUD_FINAL": "",
            "TIPO_UBICACION_FINAL": "SIN_UBICACION",
            "NIVEL_UBICACION_FINAL": "",
            "CONFIANZA_FINAL": "",
            "FUENTE_FINAL": "",
            "DETALLE_FINAL": "",
        }
    )


def main():
    if not config.PREPARED_CSV.exists():
        raise FileNotFoundError(config.PREPARED_CSV)

    prep = pd.read_csv(
        config.PREPARED_CSV,
        dtype=str,
        low_memory=False,
    ).fillna("")

    exact = pd.DataFrame(columns=["NumeroRuc"])
    reference = pd.DataFrame(columns=["NumeroRuc"])

    if config.EXACT_CSV.exists():
        exact = pd.read_csv(
            config.EXACT_CSV,
            dtype=str,
            low_memory=False,
        ).fillna("")

        exact = exact.drop_duplicates(
            subset=["NumeroRuc"],
            keep="last",
        )

        exact = exact.rename(
            columns={
                "NIVEL_UBICACION": "NIVEL_UBICACION_EXACTA",
                "CONFIANZA": "CONFIANZA_EXACTA",
            }
        )

    if config.REFERENCE_CSV.exists():
        reference = pd.read_csv(
            config.REFERENCE_CSV,
            dtype=str,
            low_memory=False,
        ).fillna("")

        reference = reference.drop_duplicates(
            subset=["NumeroRuc"],
            keep="last",
        )

        reference = reference.rename(
            columns={
                "NIVEL_UBICACION": "NIVEL_UBICACION_REFERENCIA",
                "CONFIANZA": "CONFIANZA_REFERENCIA",
            }
        )

    base_cols = [
        "NumeroRuc",
        "Nombre ó RazonSocial",
        "DEPARTAMENTO_ORIGINAL",
        "PROVINCIA_ORIGINAL",
        "DISTRITO_ORIGINAL",
        "DIRECCION_ORIGINAL",
        "UBICACION_ORIGINAL",
        "TIPO_DIRECCION",
        "VIA_ORIGINAL",
        "VIA_CORREGIDA",
        "NUMERO_BUSCADO",
        "MANZANA",
        "LOTE",
        "KM",
        "ZONA_ORIGINAL",
        "ZONA_CORREGIDA",
        "POI_DIRECCION",
        "REFERENCIA_ORIGINAL",
    ]

    df = prep[base_cols].copy()

    if not exact.empty:
        exact_cols = [
            c
            for c in [
                "NumeroRuc",
                "ESTADO_EXACTO",
                "NIVEL_UBICACION_EXACTA",
                "CONFIANZA_EXACTA",
                "LATITUD_EXACTA",
                "LONGITUD_EXACTA",
                "FUENTE_COORDENADAS",
                "QUERY_USADA",
                "NOMBRE_GOOGLE",
                "DIRECCION_GOOGLE",
                "URL_GOOGLE",
                "RAZON_DECISION",
            ]
            if c in exact.columns
        ]

        df = df.merge(
            exact[exact_cols],
            on="NumeroRuc",
            how="left",
        )

    if not reference.empty:
        ref_cols = [
            c
            for c in [
                "NumeroRuc",
                "ESTADO_REFERENCIA",
                "NIVEL_UBICACION_REFERENCIA",
                "CONFIANZA_REFERENCIA",
                "LATITUD_REFERENCIA",
                "LONGITUD_REFERENCIA",
                "FUENTE_REFERENCIA",
                "REFERENCIA_ENCONTRADA",
                "QUERY_INVESTIGACION",
                "SCORE_REFERENCIA",
                "EVIDENCIA_REFERENCIA",
                "URL_FUENTE",
            ]
            if c in reference.columns
        ]

        df = df.merge(
            reference[ref_cols],
            on="NumeroRuc",
            how="left",
        )

    df = df.fillna("")

    final = df.apply(
        final_location,
        axis=1,
    )

    df = pd.concat(
        [df, final],
        axis=1,
    )

    front = [
        "NumeroRuc",
        "Nombre ó RazonSocial",
        "DEPARTAMENTO_ORIGINAL",
        "PROVINCIA_ORIGINAL",
        "DISTRITO_ORIGINAL",
        "DIRECCION_ORIGINAL",
        "UBICACION_ORIGINAL",
        "LATITUD_FINAL",
        "LONGITUD_FINAL",
        "TIPO_UBICACION_FINAL",
        "NIVEL_UBICACION_FINAL",
        "CONFIANZA_FINAL",
        "FUENTE_FINAL",
        "DETALLE_FINAL",
    ]

    remaining = [
        c
        for c in df.columns
        if c not in front
    ]

    df = df[front + remaining]

    df.to_csv(
        config.FINAL_CSV,
        index=False,
        encoding="utf-8-sig",
    )

    df.to_excel(
        config.FINAL_XLSX,
        index=False,
    )

    print("=" * 100)
    print("04 - CONSOLIDACION FINAL")
    print("=" * 100)
    print("Registros:", f"{len(df):,}")
    print()
    print(
        df[
            [
                "TIPO_UBICACION_FINAL",
                "NIVEL_UBICACION_FINAL",
            ]
        ]
        .value_counts(
            dropna=False
        )
        .to_string()
    )
    print("\nCSV :", config.FINAL_CSV)
    print("XLSX:", config.FINAL_XLSX)


if __name__ == "__main__":
    main()
