import pandas as pd

archivo = r"D:\API_SUNAT\API_SUNAT\API_MAPS\direcciones_preparadas.csv"

df = pd.read_csv(
    archivo,
    dtype=str,
    low_memory=False
).fillna("")

ruc = "20551762727"

caso = df[
    df["NumeroRuc"] == ruc
]

columnas = [
    "NumeroRuc",
    "Direccion",
    "TIPO_DIRECCION",
    "VIA_ORIGINAL",
    "VIA_CORREGIDA",
    "NUMERO_BUSCADO",
    "MANZANA",
    "LOTE",
    "ZONA_ORIGINAL",
    "ZONA_CORREGIDA",
    "HUBO_CORRECCION",
]

for i in range(1, 11):
    columnas.append(f"Q{i}_TIPO")
    columnas.append(f"Q{i}")

print(
    caso[columnas]
    .T
    .to_string()
)