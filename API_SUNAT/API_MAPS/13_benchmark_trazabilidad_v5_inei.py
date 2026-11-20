# -*- coding: utf-8 -*-
"""Benchmark de trazabilidad de Google Maps V5 + INEI.

Uso desde Jupyter con subprocess:
  python -u 13_benchmark_trazabilidad_v5_inei.py --laptop 1 --desde 31 --hasta 300 --max-filas 30

No cambia el algoritmo ni las fuentes. Ejecuta el robot como módulo en un subproceso,
interceptando únicamente las llamadas buscar_google() y goto_con_recuperacion()
para registrar consultas reales, tiempos, navegación y resultados.

Escribe logs y CSV localmente; contienen direcciones potencialmente sensibles.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "clientes_direcciones"
ROBOT = ROOT / "12_google_maps_clientes_v5_inei.py"
PREP = OUT / "direcciones_preparadas_google_masivo_v5_inei.sqlite"

CAMPOS = [
    "FILA_ID", "N_CONSULTA", "TIPO_CONSULTA", "QUERY", "FECHA_INICIO",
    "SEGUNDOS", "NAVEGACIONES", "FICHAS_ABIERTAS", "RESULTADOS_BRUTOS",
    "ESTADO_CONSULTA", "ERROR",
]


def leer_ids(db: Path) -> set[int]:
    if not db.is_file():
        return set()
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as conn:
        tablas = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "resultados" not in tablas:
            return set()
        return {int(x[0]) for x in conn.execute("SELECT FILA_ID FROM resultados")}


def ejecutar_hijo(args):
    """Carga el robot sin ejecutar su main al importarlo e instrumenta las llamadas reales."""
    if not ROBOT.is_file():
        raise FileNotFoundError(f"No existe robot: {ROBOT}")
    salida = Path(args.corrida)
    salida.mkdir(parents=True, exist_ok=True)
    csv_consultas = salida / "consultas_reales_crudas.csv"

    spec = importlib.util.spec_from_file_location("robot_v5_inei_auditado", ROBOT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se pudo cargar el robot: {ROBOT}")
    robot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(robot)

    for nombre in ("buscar_google", "goto_con_recuperacion", "procesar_direccion", "main"):
        if not hasattr(robot, nombre):
            raise RuntimeError(f"La versión actual del robot no permite instrumentar {nombre}")

    previo_buscar = robot.buscar_google
    previo_ir = robot.goto_con_recuperacion
    previo_procesar = robot.procesar_direccion
    contexto = {"fila": None, "row": None, "n": 0, "consulta_activa": None}

    with csv_consultas.open("w", encoding="utf-8-sig", newline="") as fh:
        escritor = csv.DictWriter(fh, fieldnames=CAMPOS, delimiter=";")
        escritor.writeheader()
        fh.flush()

        def procesar_instrumentado(context, page, row):
            contexto.update(fila=int(row["FILA_ID"]), row=row, n=0, consulta_activa=None)
            try:
                return previo_procesar(context, page, row)
            finally:
                contexto.update(fila=None, row=None, n=0, consulta_activa=None)

        def ir_instrumentado(page, url):
            rec = contexto["consulta_activa"]
            if rec is not None:
                rec["NAVEGACIONES"] += 1
                if "/maps/place/" in str(url):
                    rec["FICHAS_ABIERTAS"] += 1
            return previo_ir(page, url)

        def buscar_instrumentado(context, page, query, permitir_center_distrito=False):
            contexto["n"] += 1
            row = contexto["row"] or {}
            tipo = "DESCONOCIDO"
            for i in range(1, int(robot.MAX_VARIANTES) + 1):
                if str(row.get(f"Q{i}", "") or "").strip() == str(query).strip():
                    tipo = str(row.get(f"Q{i}_TIPO", "") or "").strip()
                    break

            rec = {
                "FILA_ID": contexto["fila"],
                "N_CONSULTA": contexto["n"],
                "TIPO_CONSULTA": tipo,
                "QUERY": query,
                "FECHA_INICIO": datetime.now().isoformat(timespec="seconds"),
                "SEGUNDOS": 0.0,
                "NAVEGACIONES": 0,
                "FICHAS_ABIERTAS": 0,
                "RESULTADOS_BRUTOS": 0,
                "ESTADO_CONSULTA": "OK",
                "ERROR": "",
            }
            inicio = time.perf_counter()
            contexto["consulta_activa"] = rec
            try:
                encontrados = previo_buscar(
                    context, page, query,
                    permitir_center_distrito=permitir_center_distrito
                )
                rec["RESULTADOS_BRUTOS"] = len(encontrados or [])
                return encontrados
            except Exception as exc:
                rec["ESTADO_CONSULTA"] = type(exc).__name__
                rec["ERROR"] = str(exc)[:250]
                raise
            finally:
                rec["SEGUNDOS"] = round(time.perf_counter() - inicio, 3)
                contexto["consulta_activa"] = None
                escritor.writerow(rec)
                fh.flush()  # se conserva incluso si el robot se detiene por CAPTCHA
                print(
                    f"    [TRAZA_Q] Fila={rec['FILA_ID']} Q={rec['N_CONSULTA']} "
                    f"tiempo={rec['SEGUNDOS']:.1f}s fichas={rec['FICHAS_ABIERTAS']} "
                    f"candidatos={rec['RESULTADOS_BRUTOS']} estado={rec['ESTADO_CONSULTA']}",
                    flush=True,
                )

        robot.procesar_direccion = procesar_instrumentado
        robot.goto_con_recuperacion = ir_instrumentado
        robot.buscar_google = buscar_instrumentado

        # argv del robot. --hijo nunca llega a su parser.
        sys.argv = [
            str(ROBOT), "--laptop", str(args.laptop),
            "--desde", str(args.desde), "--hasta", str(args.hasta),
            "--max-filas", str(args.max_filas),
        ]
        robot.main()


def construir_resumen(args, directorio: Path, antes: set[int], despues: set[int], segundos: float, codigo: int):
    import pandas as pd

    traza = directorio / "consultas_reales_crudas.csv"
    df = pd.read_csv(traza, sep=";", encoding="utf-8-sig", dtype={"FILA_ID": "Int64"})
    columnas_num = ("N_CONSULTA", "SEGUNDOS", "NAVEGACIONES", "FICHAS_ABIERTAS", "RESULTADOS_BRUTOS")
    for c in columnas_num:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Cruzar la traza real con el scoring AUDITADO del robot (sin recalcular criterios).
    db_resultados = OUT / f"google_resultados_laptop{args.laptop}_v5_inei.sqlite"
    if db_resultados.exists():
        with closing(sqlite3.connect(f"file:{db_resultados.as_posix()}?mode=ro", uri=True)) as conn:
            tablas = {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "auditoria_v5" in tablas:
                audit = pd.read_sql_query(
                    "SELECT FILA_ID, QUERY, TERRITORIO, CALIDAD, MOTIVO, ELEGIDO "
                    "FROM auditoria_v5 WHERE FILA_ID BETWEEN ? AND ?",
                    conn, params=(args.desde, args.hasta),
                )
            else:
                audit = pd.DataFrame(columns=["FILA_ID", "QUERY", "TERRITORIO", "CALIDAD", "MOTIVO", "ELEGIDO"])
            if "resultados" in tablas:
                resultados = pd.read_sql_query(
                    "SELECT FILA_ID, ESTADO, CALIDAD_GEO, USO_GEO "
                    "FROM resultados WHERE FILA_ID BETWEEN ? AND ?",
                    conn, params=(args.desde, args.hasta),
                )
            else:
                resultados = pd.DataFrame(columns=["FILA_ID", "ESTADO", "CALIDAD_GEO", "USO_GEO"])
    else:
        audit = pd.DataFrame(columns=["FILA_ID", "QUERY", "TERRITORIO", "CALIDAD", "MOTIVO", "ELEGIDO"])
        resultados = pd.DataFrame(columns=["FILA_ID", "ESTADO", "CALIDAD_GEO", "USO_GEO"])

    if not audit.empty:
        calidades = audit["CALIDAD"].fillna("").astype(str)
        territorial = audit["TERRITORIO"].eq("DENTRO_DISTRITO")
        audit["EVIDENCIA_UTIL"] = (
            territorial
            & (calidades.str.startswith("REFERENCIA_")
               | calidades.str.startswith("COINCIDENCIA_")
               | calidades.str.startswith("VIA_NUMERO_"))
        ).astype(int)
        audit["ELEGIDO"] = pd.to_numeric(audit["ELEGIDO"], errors="coerce").fillna(0).astype(int)
        agrupado = audit.groupby(["FILA_ID", "QUERY"], as_index=False).agg(
            CANDIDATOS_AUDITADOS=("QUERY", "size"),
            RESULTADOS_VALIDOS=("EVIDENCIA_UTIL", "sum"),
            ELEGIDA_FINAL=("ELEGIDO", "max"),
        )
        df = df.merge(agrupado, on=["FILA_ID", "QUERY"], how="left")
    else:
        for c in ("CANDIDATOS_AUDITADOS", "RESULTADOS_VALIDOS", "ELEGIDA_FINAL"):
            df[c] = 0
    for c in ("CANDIDATOS_AUDITADOS", "RESULTADOS_VALIDOS", "ELEGIDA_FINAL"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    df = df.merge(resultados, on="FILA_ID", how="left")
    # Una misma consulta exacta puede repetirse entre direcciones de distintos clientes.
    # Señala oportunidad de deduplicar búsquedas, no autoriza reutilizar resultados
    # sin revisar la clave de dirección y su distrito.
    df["QUERY_NORMALIZADA"] = (
        df["QUERY"].fillna("").astype(str).str.upper()
        .str.replace(r"\s+", " ", regex=True).str.strip()
    )
    recuentos_queries = df["QUERY_NORMALIZADA"].value_counts()
    df["REPETICIONES_QUERY_LOTE"] = df["QUERY_NORMALIZADA"].map(recuentos_queries).fillna(0).astype(int)
    df["RESULTADO_QUERY"] = "SOLO_RECHAZADOS"
    df.loc[df["RESULTADOS_BRUTOS"] == 0, "RESULTADO_QUERY"] = "SIN_CANDIDATOS"
    df.loc[df["RESULTADOS_VALIDOS"] > 0, "RESULTADO_QUERY"] = "CON_EVIDENCIA"
    df.loc[df["ELEGIDA_FINAL"] > 0, "RESULTADO_QUERY"] = "COORDENADA_ELEGIDA"
    df.loc[df["ESTADO_CONSULTA"] != "OK", "RESULTADO_QUERY"] = "ERROR_O_BLOQUEO"

    # Consultas posteriores a la que finalmente ganó = oportunidad de investigar,
    # NO demostración de que fueran innecesarias.
    if not df.empty:
        indice_ganador = df.loc[df.ELEGIDA_FINAL.gt(0)].groupby("FILA_ID")["N_CONSULTA"].min()
        df["Q_GANADORA"] = df["FILA_ID"].map(indice_ganador)
        df["DESPUES_DE_GANADORA"] = (df["N_CONSULTA"] > df["Q_GANADORA"]).fillna(False).astype(int)
    else:
        df["Q_GANADORA"] = []
        df["DESPUES_DE_GANADORA"] = []

    detalle_path = directorio / "traza_consultas_detalle.csv"
    df.to_csv(detalle_path, sep=";", index=False, encoding="utf-8-sig")

    if not df.empty:
        por_tipo = df.groupby("TIPO_CONSULTA", dropna=False).agg(
            CONSULTAS=("QUERY", "size"),
            SEGUNDOS_TOTALES=("SEGUNDOS", "sum"),
            SEGUNDOS_PROMEDIO=("SEGUNDOS", "mean"),
            FICHAS_ABIERTAS=("FICHAS_ABIERTAS", "sum"),
            CONSULTAS_CON_EVIDENCIA=("RESULTADOS_VALIDOS", lambda x: int((x > 0).sum())),
            CONSULTAS_ELEGIDAS=("ELEGIDA_FINAL", "sum"),
        ).reset_index().sort_values("SEGUNDOS_TOTALES", ascending=False)
        por_orden = df.groupby("N_CONSULTA", dropna=False).agg(
            CONSULTAS=("QUERY", "size"),
            SEGUNDOS_TOTALES=("SEGUNDOS", "sum"),
            SEGUNDOS_PROMEDIO=("SEGUNDOS", "mean"),
            CONSULTAS_CON_EVIDENCIA=("RESULTADOS_VALIDOS", lambda x: int((x > 0).sum())),
            CONSULTAS_ELEGIDAS=("ELEGIDA_FINAL", "sum"),
        ).reset_index().sort_values("N_CONSULTA")
    else:
        por_tipo = pd.DataFrame()
        por_orden = pd.DataFrame()
    por_tipo.to_csv(directorio / "resumen_tipos_consulta.csv", sep=";", index=False, encoding="utf-8-sig")
    por_orden.to_csv(directorio / "resumen_orden_consulta.csv", sep=";", index=False, encoding="utf-8-sig")

    nuevas = len((despues - antes) & set(range(args.desde, args.hasta + 1)))
    with closing(sqlite3.connect(f"file:{PREP.as_posix()}?mode=ro", uri=True)) as conn:
        total = conn.execute("SELECT COUNT(*) FROM preparadas").fetchone()[0]
    ids_otro = leer_ids(OUT / f"google_resultados_laptop{2 if args.laptop == 1 else 1}_v5_inei.sqlite")
    pendientes = max(0, total - len(despues | ids_otro))
    promedio = segundos / nuevas if nuevas else None
    horas = pendientes * promedio / 3600 if promedio else None
    resumen = {
        "CODIGO_SALIDA": codigo,
        "DIRECCIONES_NUEVAS": nuevas,
        "CONSULTAS_REALES": len(df),
        "FICHAS_ABIERTAS": int(df.FICHAS_ABIERTAS.sum()),
        "SEGUNDOS_TOTAL_EJECUCION": round(segundos, 2),
        "SEGUNDOS_POR_DIRECCION": round(promedio, 2) if promedio else None,
        "SEGUNDOS_POR_CONSULTA": round(float(df.SEGUNDOS.mean()), 2) if not df.empty else None,
        "CONSULTAS_POR_DIRECCION": round(len(df)/nuevas, 2) if nuevas else None,
        "CONSULTAS_SIN_CANDIDATOS": int((df.RESULTADOS_BRUTOS == 0).sum()),
        "CONSULTAS_SIN_EVIDENCIA": int((df.RESULTADOS_VALIDOS == 0).sum()),
        "CONSULTAS_POSTERIORES_A_GANADORA": int(df.DESPUES_DE_GANADORA.sum()),
        "SEGUNDOS_POSTERIORES_A_GANADORA": round(float(df.loc[df.DESPUES_DE_GANADORA.eq(1), "SEGUNDOS"].sum()), 2),
        "CONSULTAS_CON_QUERY_REPETIDA_EN_LOTE": int((df.REPETICIONES_QUERY_LOTE > 1).sum()),
        "PENDIENTES_UNIVERSO": pendientes,
        "HORAS_1_LAPTOP_24H": round(horas, 1) if horas else None,
        "DIAS_1_LAPTOP_24H": round(horas/24, 1) if horas else None,
        "DIAS_2_LAPTOPS_24H": round(horas/48, 1) if horas else None,
    }
    with (directorio / "resumen_general.json").open("w", encoding="utf-8") as fh:
        json.dump(resumen, fh, ensure_ascii=False, indent=2)

    print("\n" + "=" * 88)
    print("RESUMEN - BENCHMARK CON TRAZA REAL")
    print("=" * 88)
    for k, v in resumen.items():
        print(f"{k:42s}: {v}")
    print("\nCONSULTAS QUE MAS TIEMPO CONSUMIERON (10 PRIMERAS)")
    if len(df):
        print(df.sort_values("SEGUNDOS", ascending=False)[
            ["FILA_ID", "N_CONSULTA", "TIPO_CONSULTA", "SEGUNDOS", "FICHAS_ABIERTAS",
             "RESULTADOS_VALIDOS", "ELEGIDA_FINAL", "RESULTADO_QUERY"]
        ].head(10).to_string(index=False))
    print("\nRESUMEN POR TIPO DE CONSULTA")
    if len(por_tipo):
        print(por_tipo.to_string(index=False))
    print("\nRESUMEN POR ORDEN Q1-Q12")
    if len(por_orden):
        print(por_orden.to_string(index=False))
    print("\nARCHIVOS EN:", directorio)
    print("- traza_consultas_detalle.csv (cada consulta exacta, su duración y su utilidad)")
    print("- resumen_tipos_consulta.csv")
    print("- resumen_orden_consulta.csv")
    print("- tiempos_por_direccion.csv (tiempo aproximado de cada fila)")
    print("- resumen_general.json")
    print("- salida_completa.log")
    if codigo != 0:
        print("ATENCION: el robot terminó con error; revisar salida_completa.log")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--laptop", type=int, default=1, choices=[1,2])
    parser.add_argument("--desde", type=int, default=31)
    parser.add_argument("--hasta", type=int, default=300)
    parser.add_argument("--max-filas", type=int, default=30)
    parser.add_argument("--hijo", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--corrida", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.hijo:
        ejecutar_hijo(args)
        return
    if not ROBOT.is_file() or not PREP.is_file():
        raise FileNotFoundError(f"Falta robot ({ROBOT.exists()}) o SQLite preparada ({PREP.exists()}).")
    if args.max_filas < 1 or args.desde < 1 or args.hasta < args.desde:
        raise ValueError("La muestra y el rango deben ser positivos y DESDE <= HASTA")

    fecha = datetime.now().strftime("%Y%m%d_%H%M%S")
    directorio = OUT / "benchmarks_trazabilidad" / f"corrida_{fecha}"
    directorio.mkdir(parents=True, exist_ok=False)
    base_res = OUT / f"google_resultados_laptop{args.laptop}_v5_inei.sqlite"
    antes = leer_ids(base_res)
    print("="*88)
    print("BENCHMARK DE CONSULTAS REALES - GOOGLE MAPS V5 + INEI")
    print("="*88)
    print(f"Robot: {ROBOT}")
    print(f"Rango: {args.desde}-{args.hasta} | Máximo de direcciones NUEVAS: {args.max_filas}")
    print("Salida local:", directorio)
    print("No se cambia el algoritmo ni las SQLite históricas.", flush=True)

    comando = [sys.executable, "-u", str(Path(__file__).resolve()), "--hijo",
               "--laptop", str(args.laptop), "--desde", str(args.desde),
               "--hasta", str(args.hasta), "--max-filas", str(args.max_filas),
               "--corrida", str(directorio)]
    comienzo = time.perf_counter()
    codigo = None
    tiempos_filas = []
    fila_actual = None
    marca_inicio_fila = None
    try:
        with (directorio / "salida_completa.log").open("w", encoding="utf-8") as log:
            with subprocess.Popen(comando, cwd=str(ROOT), stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace", bufsize=1) as proceso:
                for linea in proceso.stdout:
                    log.write(linea)
                    log.flush()
                    m_fila = re.match(r"^\s*FILA\s+([\d,.]+)\s*\|", linea)
                    if m_fila:
                        fila_actual = int(m_fila.group(1).replace(",", ""))
                        marca_inicio_fila = time.perf_counter()
                    if fila_actual is not None and marca_inicio_fila is not None and (
                        "FINAL ->" in linea or "ERROR_TECNICO:" in linea
                    ):
                        tiempos_filas.append({
                            "FILA_ID": fila_actual,
                            "SEGUNDOS_FILA_APROX": round(time.perf_counter() - marca_inicio_fila, 3),
                            "FINALIZACION": linea.strip()[:180],
                        })
                        fila_actual = None
                        marca_inicio_fila = None
                    if ("[TRAZA_Q]" in linea
                        or re.match(r"^\s*FILA\s+[\d,.]+\s*\|", linea)
                        or "FINAL ->" in linea
                        or "ERROR_TECNICO" in linea
                        or "bloqueo" in linea.lower()
                        or "CAPTCHA" in linea.upper()
                        or "FIN DE ESTA EJECUCION" in linea):
                        print(linea.rstrip(), flush=True)
                codigo = proceso.wait()
    except KeyboardInterrupt:
        print("Se interrumpió la prueba. Consulta salida_completa.log para revisar lo registrado.")
        raise
    duracion = time.perf_counter() - comienzo
    import pandas as pd
    pd.DataFrame(tiempos_filas, columns=["FILA_ID", "SEGUNDOS_FILA_APROX", "FINALIZACION"]).to_csv(
        directorio / "tiempos_por_direccion.csv", sep=";", index=False, encoding="utf-8-sig"
    )
    despues = leer_ids(base_res)
    if not (directorio / "consultas_reales_crudas.csv").exists():
        raise RuntimeError("No se creó la traza; consulta salida_completa.log para identificar el error inicial")
    construir_resumen(args, directorio, antes, despues, duracion, codigo)


if __name__ == "__main__":
    main()
