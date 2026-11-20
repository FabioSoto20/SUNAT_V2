# -*- coding: utf-8 -*-
"""Paso 14 — sincronizar equivalencias INEI del Paso 13 con la entrada V5.

SOLO crea copias nuevas:
- clientes_direcciones/direcciones_preparadas_google_masivo_v5_inei.sqlite
- 12_google_maps_clientes_v5_inei.py
- clientes_direcciones/reporte_integracion_inei_v5.csv

NO modifica direcciones.csv, ninguna SQLite histórica, ni V5 original.
NO navega por Google Maps. Únicamente prepara la base para el robot.

Conserva el texto de la dirección original y las consultas Q1..Q18 ya preparadas;
corrige exclusivamente Departamento, Provincia y Distrito cuando la equivalencia
fue registrada como RECUPERADO_EQUIVALENCIA_VERIFICADA y existe en el catálogo INEI.
"""

from pathlib import Path
import csv
import os
import re
import sqlite3
import unicodedata
from collections import Counter

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'clientes_direcciones'
DB_ORIG = OUT / 'direcciones_preparadas_google_masivo.sqlite'
DB_NEW = OUT / 'direcciones_preparadas_google_masivo_v5_inei.sqlite'
DB_TMP = OUT / 'direcciones_preparadas_google_masivo_v5_inei.creando.sqlite'
DIAG = OUT / 'diagnostico_equivalencias_inei.csv'
CAT = OUT / 'catalogo_centros_representativos_inei.csv'
ROBOT_SRC = ROOT / '08_google_maps_clientes_v5.py'
ROBOT_OUT = ROOT / '12_google_maps_clientes_v5_inei.py'
REPORTE = OUT / 'reporte_integracion_inei_v5.csv'


def norm(s):
    s = str(s or '').strip().upper()
    s = ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9]+', ' ', s)).strip()


def leer_csv(path):
    with path.open('r', encoding='utf-8-sig', newline='') as fh:
        return list(csv.DictReader(fh, delimiter=';'))


def main():
    print('=' * 87)
    print('PASO 14 — PREPARAR GOOGLE MAPS V5 CON EQUIVALENCIAS INEI VALIDADAS')
    print('=' * 87)
    for ruta in (DB_ORIG, DIAG, CAT, ROBOT_SRC):
        if not ruta.is_file():
            raise FileNotFoundError(f'Falta archivo requerido: {ruta}')
    if DB_NEW.exists() or ROBOT_OUT.exists():
        raise FileExistsError('Ya existe una salida del Paso 14. No se sobrescribe.\n'
                              f'SQLite: {DB_NEW} (existe={DB_NEW.exists()})\n'
                              f'Robot: {ROBOT_OUT} (existe={ROBOT_OUT.exists()})')
    if DB_TMP.exists():
        raise FileExistsError(f'Hay una copia temporal anterior: {DB_TMP}. Revisar antes de continuar.')

    catalogo = leer_csv(CAT)
    catalog_keys = {(norm(r['DEP_KEY']), norm(r['PROV_KEY']), norm(r['DIST_KEY'])) for r in catalogo}
    if len(catalog_keys) != len(catalogo):
        raise RuntimeError('Catálogo con claves duplicadas: no se ejecuta para evitar asignaciones ambiguas.')
    diagnostico = leer_csv(DIAG)
    aliases = {}
    for r in diagnostico:
        if r['RESULTADO'] != 'RECUPERADO_EQUIVALENCIA_VERIFICADA':
            continue
        origen = (norm(r['DEP_ORIGEN']), norm(r['PROV_ORIGEN']), norm(r['DIST_ORIGEN']))
        destino = (norm(r['DEP_INEI_PROPUESTO']), norm(r['PROV_INEI_PROPUESTO']), norm(r['DIST_INEI_PROPUESTO']))
        if origen in aliases and aliases[origen] != destino:
            raise RuntimeError(f'Equivalencia conflictiva para {origen}')
        if destino not in catalog_keys:
            raise RuntimeError(f'El destino de una equivalencia no existe en INEI: {destino}')
        aliases[origen] = destino
    if not aliases:
        raise RuntimeError('No hay equivalencias verificadas; comprobar Paso 13.')

    robot = ROBOT_SRC.read_text(encoding='utf-8-sig')
    antes = 'PREP_DB = OUT_DIR / "direcciones_preparadas_google_masivo.sqlite"'
    despues = 'PREP_DB = OUT_DIR / "direcciones_preparadas_google_masivo_v5_inei.sqlite"'
    if robot.count(antes) != 1:
        raise RuntimeError('No se encontró exactamente una asignación PREP_DB en la V5; archivo no cambiado.')
    robot = robot.replace(antes, despues, 1)
    # Aislar TODAS las salidas operativas de V5 (dos laptops, CSV, logs y bloqueos).
    rutas = [
        'google_resultados_laptop{laptop}_v5.sqlite',
        'google_resultados_laptop{laptop}_v5.csv',
        'google_candidatos_laptop{laptop}_v5.csv',
        'progreso_google_laptop{laptop}_v5.txt',
        'bloqueo_google_laptop{laptop}_v5.txt',
    ]
    for salida in rutas:
        if robot.count(salida) != 1:
            raise RuntimeError(f'No es seguro modificar la salida V5: {salida}')
        robot = robot.replace(salida, salida.replace('_v5.', '_v5_inei.'), 1)
    # Mantener archivos de auditoría histórica inalterados.
    compile(robot, str(ROBOT_OUT), 'exec')

    contador = Counter()
    ejemplos = {}
    try:
        print(f'Equivalencias verificadas disponibles: {len(aliases):,}', flush=True)
        print('Creando copia transaccional de SQLite original...', flush=True)
        with sqlite3.connect(f'file:{DB_ORIG.as_posix()}?mode=ro', uri=True) as origen:
            with sqlite3.connect(DB_TMP) as destino:
                origen.backup(destino, pages=1500)

        con = sqlite3.connect(DB_TMP)
        try:
            cols = {r[1] for r in con.execute('PRAGMA table_info(preparadas)')}
            obligatorias = {'FILA_ID', 'FILA_ORIGINAL', 'Departamento', 'Provincia', 'Distrito'}
            if not obligatorias.issubset(cols):
                raise RuntimeError(f'Faltan campos preparados: {sorted(obligatorias-cols)}')
            for nuevo in ['Departamento_Original', 'Provincia_Original', 'Distrito_Original', 'MATCH_INEI_V5']:
                if nuevo not in cols:
                    con.execute(f'ALTER TABLE preparadas ADD COLUMN "{nuevo}" TEXT')
            rows = con.execute('''
                SELECT rowid, FILA_ID, FILA_ORIGINAL, Departamento, Provincia, Distrito
                FROM preparadas
            ''').fetchall()
            if not rows:
                raise RuntimeError('Tabla preparadas vacía.')
            actualizaciones = []
            for rowid, fila_id, fila_original, dep, prov, dist in rows:
                clave = (norm(dep), norm(prov), norm(dist))
                if clave not in aliases:
                    continue
                target = aliases[clave]
                if target == clave:
                    continue
                actualizaciones.append((*target, str(dep or ''), str(prov or ''), str(dist or ''),
                                       'EQUIVALENCIA_INEI_VERIFICADA', rowid))
                contador[clave] += 1
                ejemplos.setdefault(clave, {'FILA_ID': fila_id, 'FILA_ORIGINAL': fila_original})
            con.executemany('''
                UPDATE preparadas SET
                    Departamento=?, Provincia=?, Distrito=?,
                    Departamento_Original=?, Provincia_Original=?, Distrito_Original=?,
                    MATCH_INEI_V5=?
                WHERE rowid=?
            ''', actualizaciones)
            con.commit()
            total = con.execute('SELECT COUNT(*) FROM preparadas').fetchone()[0]
            recuperadas = con.execute('''
                SELECT COUNT(*) FROM preparadas
                WHERE MATCH_INEI_V5='EQUIVALENCIA_INEI_VERIFICADA'
            ''').fetchone()[0]
            integrity = con.execute('PRAGMA integrity_check').fetchone()[0]
            if total != len(rows) or recuperadas != len(actualizaciones) or integrity != 'ok':
                raise RuntimeError('La comprobación de la nueva SQLite falló.')
            # Las consultas Q1-Q18 continúan tal como las preparó el Paso 1.
            con.execute('PRAGMA journal_mode=DELETE')
        finally:
            con.close()

        with REPORTE.open('w', encoding='utf-8-sig', newline='') as fh:
            writer = csv.writer(fh, delimiter=';')
            writer.writerow(['DEP_ORIGEN', 'PROV_ORIGEN', 'DIST_ORIGEN',
                             'DEP_INEI', 'PROV_INEI', 'DIST_INEI', 'FILAS_PREPARADAS_ACTUALIZADAS',
                             'EJEMPLO_FILA_ID', 'EJEMPLO_FILA_ORIGINAL'])
            for clave, num in contador.most_common():
                destino = aliases[clave]
                writer.writerow([*clave, *destino, num,
                                 ejemplos[clave]['FILA_ID'], ejemplos[clave]['FILA_ORIGINAL']])
        # Sólo publicar resultados después de verificar la integridad.
        DB_TMP.replace(DB_NEW)
        ROBOT_OUT.write_text(robot, encoding='utf-8')
    except Exception:
        if DB_TMP.exists():
            DB_TMP.unlink()
        raise

    print(f'Filas preparadas conservadas: {total:,}')
    print(f'Filas con nombres INEI corregidos: {recuperadas:,}')
    print(f'Combinaciones INEI aplicadas en direcciones: {len(contador):,}')
    print(f'Integridad SQLite: {integrity}')
    print('BASE PARA BUSQUEDA:', DB_NEW)
    print('ROBOT AISLADO:', ROBOT_OUT)
    print('REPORTE:', REPORTE)
    print('\nIMPORTANTE: en Paso 13 se recuperaron 8,435 registros originales, incluidos')
    print('registros sin dirección; aquí solo corregimos los que están en preparadas.')
    print('No se ejecutaron consultas web y no se modificaron archivos anteriores.')


if __name__ == '__main__':
    main()
