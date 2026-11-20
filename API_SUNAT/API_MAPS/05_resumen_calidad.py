import pandas as pd
import config


def main():
    if not config.FINAL_CSV.exists():
        raise FileNotFoundError(
            f"Primero ejecuta 04_consolidar_resultados.py\n"
            f"No existe: {config.FINAL_CSV}"
        )

    df = pd.read_csv(
        config.FINAL_CSV,
        dtype=str,
        low_memory=False,
    ).fillna("")

    by_type = (
        df.groupby(
            ["TIPO_UBICACION_FINAL"],
            dropna=False,
        )
        .size()
        .reset_index(name="REGISTROS")
    )

    by_level = (
        df.groupby(
            [
                "TIPO_UBICACION_FINAL",
                "NIVEL_UBICACION_FINAL",
            ],
            dropna=False,
        )
        .size()
        .reset_index(name="REGISTROS")
    )

    by_source = (
        df.groupby(
            ["FUENTE_FINAL"],
            dropna=False,
        )
        .size()
        .reset_index(name="REGISTROS")
    )

    with pd.ExcelWriter(
        config.QUALITY_XLSX,
        engine="openpyxl",
    ) as writer:
        by_type.to_excel(
            writer,
            sheet_name="tipo_ubicacion",
            index=False,
        )

        by_level.to_excel(
            writer,
            sheet_name="nivel_ubicacion",
            index=False,
        )

        by_source.to_excel(
            writer,
            sheet_name="fuentes",
            index=False,
        )

    print("=" * 90)
    print("RESUMEN DE CALIDAD")
    print("=" * 90)
    print(by_type.to_string(index=False))
    print("\nArchivo:", config.QUALITY_XLSX)


if __name__ == "__main__":
    main()
