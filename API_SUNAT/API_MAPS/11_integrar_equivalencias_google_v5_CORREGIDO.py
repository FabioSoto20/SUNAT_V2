# -*- coding: utf-8 -*-
"""Paso 14 corregido: genera copia SQLite V5+INEI y robot aislado.

No modifica SQLite original, robots anteriores ni consultas Q1-Q18.
Cierra explícitamente toda conexión SQLite antes de publicar el resultado
para evitar WinError 32 en Windows. Utiliza temporal con nombre único para
no reutilizar un .creando.sqlite bloqueado por ejecuciones anteriores.
"""
from pathlib import Path
from contextlib import closing
from collections import Counter
from datetime import datetime
import csv
import os
import re
import sqlite3
import time
import unicodedata

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "clientes_direcciones"
DB_ORIG = OUT / "direcciones_preparadas_google_masivo.sqlite"
DB_NEW = OUT / "direcciones_preparadas_google_masivo_v5_inei.sqlite"
DIAG = OUT / "diagnostico_equivalencias_inei.csv"
CAT = OUT / "catalogo_centros_representativos_inei.csv"
ROBOT_SRC = ROOT / "08_google_maps_clientes_v5.py"
ROBOT_OUT = ROOT / "12_google_maps_clientes_v5_inei.py"
REPORTE = OUT / "reporte_integracion_inei_v5.csv"


def norm(value):
    s = str(value or "").strip().upper()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", s)).strip()


def cargar_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def main():
    print("=" * 85)
    print("PASO 14 CORREGIDO - BASE GOOGLE MAPS V5 CON EQUIVALENCIAS INEI")
    print("=" * 85)
    for file in (DB_ORIG, DIAG, CAT, ROBOT_SRC):
        if not file.is_file():
            raise FileNotFoundError(f"Falta el archivo: {file}")

    if DB_NEW.exists() or ROBOT_OUT.exists():
        raise FileExistsError(
            "Ya existe una salida del Paso 14. No se sobrescribe para proteger datos. "
            f"Base existente={DB_NEW.exists()}; robot existente={ROBOT_OUT.exists()}."
        )

    catalogo = cargar_csv(CAT)
    claves = {(norm(r["DEP_KEY"]), norm(r["PROV_KEY"]), norm(r["DIST_KEY"])) for r in catalogo}
    if len(claves) != len(catalogo):
        raise RuntimeError("Hay claves duplicadas en catálogo INEI. Se detuvo por seguridad.")

    aliases = {}
    for r in cargar_csv(DIAG):
        if r["RESULTADO"] != "RECUPERADO_EQUIVALENCIA_VERIFICADA":
            continue
        origen = tuple(norm(r[x]) for x in ("DEP_ORIGEN", "PROV_ORIGEN", "DIST_ORIGEN"))
        destino = tuple(norm(r[x]) for x in ("DEP_INEI_PROPUESTO", "PROV_INEI_PROPUESTO", "DIST_INEI_PROPUESTO"))
        if destino not in claves:
            raise RuntimeError(f"Equivalencia con destino INEI inexistente: {destino}")
        if origen in aliases and aliases[origen] != destino:
            raise RuntimeError(f"Equivalencia ambigua: {origen}")
        aliases[origen] = destino
    if not aliases:
        raise RuntimeError("No existen equivalencias INEI verificadas del Paso 13")

    robot = ROBOT_SRC.read_text(encoding="utf-8-sig")
    a = 'PREP_DB = OUT_DIR / "direcciones_preparadas_google_masivo.sqlite"'
    b = 'PREP_DB = OUT_DIR / "direcciones_preparadas_google_masivo_v5_inei.sqlite"'
    if robot.count(a) != 1:
        raise RuntimeError("No se reconoce la configuración PREP_DB del robot V5")
    robot = robot.replace(a, b, 1)
    for filename in (
        "google_resultados_laptop{laptop}_v5.sqlite",
        "google_resultados_laptop{laptop}_v5.csv",
        "google_candidatos_laptop{laptop}_v5.csv",
        "progreso_google_laptop{laptop}_v5.txt",
        "bloqueo_google_laptop{laptop}_v5.txt",
    ):
        if robot.count(filename) != 1:
            raise RuntimeError(f"No se pudo aislar la salida del robot: {filename}")
        robot = robot.replace(filename, filename.replace("_v5.", "_v5_inei."), 1)
    compile(robot, str(ROBOT_OUT), "exec")

    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    temporal = OUT / f"direcciones_preparadas_google_masivo_v5_inei.creando_{marca}_{os.getpid()}.sqlite"
    contador = Counter()
    ejemplos = {}
    print(f"Equivalencias verificadas: {len(aliases):,}", flush=True)
    print("Copiando SQLite original en archivo temporal NUEVO...", flush=True)

    try:
        # Importante: contextlib.closing SI cierra la conexión.
        # 'with sqlite3.connect(...)' por sí solo NO lo hace.
        with closing(sqlite3.connect(f"file:{DB_ORIG.as_posix()}?mode=ro", uri=True)) as fuente:
            with closing(sqlite3.connect(str(temporal), timeout=90)) as destino:
                fuente.backup(destino, pages=1500, sleep=0.10)
                destino.commit()

        print("Copia terminada. Conexiones de respaldo cerradas.", flush=True)
        with closing(sqlite3.connect(str(temporal), timeout=90)) as conn:
            columnas = {r[1] for r in conn.execute('PRAGMA table_info("preparadas")')}
            necesarias = {"FILA_ID", "FILA_ORIGINAL", "Departamento", "Provincia", "Distrito"}
            if not necesarias.issubset(columnas):
                raise RuntimeError(f"Faltan columnas: {sorted(necesarias-columnas)}")

            for campo in ("Departamento_Original", "Provincia_Original", "Distrito_Original", "MATCH_INEI_V5"):
                if campo not in columnas:
                    conn.execute(f'ALTER TABLE preparadas ADD COLUMN "{campo}" TEXT')

            actualizaciones = []
            numero_filas = 0
            for rowid, fila_id, fila_original, dep, prov, dist in conn.execute(
                'SELECT rowid, FILA_ID, FILA_ORIGINAL, Departamento, Provincia, Distrito FROM preparadas'
            ):
                numero_filas += 1
                origen = (norm(dep), norm(prov), norm(dist))
                if origen not in aliases or aliases[origen] == origen:
                    continue
                destino = aliases[origen]
                actualizaciones.append((*destino, str(dep or ""), str(prov or ""), str(dist or ""),
                                        "EQUIVALENCIA_INEI_VERIFICADA", rowid))
                contador[origen] += 1
                ejemplos.setdefault(origen, (fila_id, fila_original))

            conn.executemany('''
                UPDATE preparadas SET Departamento=?, Provincia=?, Distrito=?,
                    Departamento_Original=?, Provincia_Original=?, Distrito_Original=?,
                    MATCH_INEI_V5=? WHERE rowid=?
            ''', actualizaciones)
            conn.commit()
            total = conn.execute("SELECT COUNT(*) FROM preparadas").fetchone()[0]
            corregidas = conn.execute(
                "SELECT COUNT(*) FROM preparadas WHERE MATCH_INEI_V5='EQUIVALENCIA_INEI_VERIFICADA'"
            ).fetchone()[0]
            estado = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if total != numero_filas or corregidas != len(actualizaciones) or estado != "ok":
                raise RuntimeError("Integridad SQLite inconsistente, no se publicará la copia.")
            modo = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            conn.commit()

        print(f"Validación: {total:,} filas; {corregidas:,} corregidas; integridad={estado}; journal={modo}")
        print("Todas las conexiones SQLite cerradas. Publicando archivos...", flush=True)
        # Si antivirus/indexador mantiene bloqueo breve tras el cierre, esperar y reintentar.
        for intento in range(1, 7):
            try:
                os.replace(temporal, DB_NEW)
                break
            except PermissionError:
                if intento == 6:
                    raise
                time.sleep(1)

        ROBOT_OUT.write_text(robot, encoding="utf-8")
        with REPORTE.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["DEP_ORIGEN", "PROV_ORIGEN", "DIST_ORIGEN", "DEP_INEI", "PROV_INEI", "DIST_INEI",
                             "FILAS_PREPARADAS_ACTUALIZADAS", "EJEMPLO_FILA_ID", "EJEMPLO_FILA_ORIGINAL"])
            for clave, n in contador.most_common():
                writer.writerow([*clave, *aliases[clave], n, *ejemplos[clave]])

    except Exception:
        # Conservar la copia temporal si Windows la mantiene bloqueada: no ocultar el error original.
        if temporal.exists():
            try:
                temporal.unlink()
            except PermissionError:
                print(f"Aviso: quedó un temporal bloqueado que puede revisarse después: {temporal}")
        raise

    print("=" * 85)
    print("PASO 14 COMPLETADO")
    print(f"Filas preparadas: {total:,}")
    print(f"Direcciones con datos INEI corregidos: {corregidas:,}")
    print(f"Combinaciones aplicadas: {len(contador):,}")
    print("Nueva base:", DB_NEW)
    print("Nuevo robot:", ROBOT_OUT)
    print("Reporte:", REPORTE)
    print("Ninguna base histórica fue modificada; no se consultó Google Maps.")


if __name__ == "__main__":
    main()
