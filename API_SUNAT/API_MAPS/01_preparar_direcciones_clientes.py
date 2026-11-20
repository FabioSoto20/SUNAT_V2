# -*- coding: utf-8 -*-
"""Prepara direcciones.csv para el buscador Google Maps V2 existente.
Ubicar este archivo junto a 01_preparar_direcciones_masivo.py en API_MAPS.
No modifica la base histórica ni los resultados existentes.
"""
from pathlib import Path
import importlib.util
import hashlib
import json
import sqlite3
import pandas as pd

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent
ENTRADA = BASE / 'direcciones.csv'
SALIDA = ROOT / 'clientes_direcciones'
PREP_DB = SALIDA / 'direcciones_preparadas_google_masivo.sqlite'
PREP_CSV = SALIDA / 'direcciones_preparadas_clientes.csv'
MANIFEST = SALIDA / 'manifest_google_masivo.json'
DIAGNOSTICO = SALIDA / 'diagnostico_clientes.csv'
ORIGEN = ROOT / '01_preparar_direcciones_masivo.py'
COLUMNAS_ORIGEN = ['personal_id','customer_id','taxpayer_id','ruc_id',
                    'adr_department_name','adr_province_name','adr_district_name','Direccion Completa']
CHUNK_SIZE = 10000


def leer():
    return pd.read_csv(ENTRADA, sep=';', dtype=str, keep_default_na=False,
                       encoding='utf-8-sig', chunksize=CHUNK_SIZE)


def main():
    if not ENTRADA.is_file():
        raise FileNotFoundError(f'No existe el CSV: {ENTRADA}')
    if not ORIGEN.is_file():
        raise FileNotFoundError(f'Falta el preparador original: {ORIGEN}')
    spec = importlib.util.spec_from_file_location('preparador_sunat_original', ORIGEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.CORRECCIONES_CSV = ROOT / "correcciones_aprendidas.csv"
    with open(ENTRADA, encoding='utf-8-sig') as f:
        cabecera = f.readline().rstrip('\r\n').split(';')
    faltan = sorted(set(COLUMNAS_ORIGEN) - set(cabecera))
    if faltan:
        raise ValueError(f'Columnas no encontradas: {faltan}')

    SALIDA.mkdir(exist_ok=True, parents=True)
    if PREP_DB.exists() or PREP_CSV.exists():
        raise FileExistsError('Ya existe una preparación de clientes. Para proteger los resultados, '
                              f'no se sobrescribirá: {PREP_DB} / {PREP_CSV}')
    correcciones = mod.cargar_correcciones()
    total, buscables = 0, 0
    for chunk in leer():
        total += len(chunk)
        buscables += int(chunk['Direccion Completa'].str.strip().ne('').sum())
    corte = (buscables + 1) // 2
    print(f'Registros originales: {total:,} | Con dirección: {buscables:,} | Sin dirección: {total-buscables:,}')
    print(f'Destino (sin tocar la base antigua): {SALIDA}')

    columnas = mod.columnas_salida() + ['personal_id','customer_id','taxpayer_id','ruc_id', 'FILA_ORIGINAL']
    tipos, calidades, ids = {}, {}, set()
    n = 0
    primera = True
    db = sqlite3.connect(PREP_DB)
    try:
        for num_chunk, chunk in enumerate(leer(), 1):
            chunk = chunk.copy()
            chunk['FILA_ORIGINAL'] = range((num_chunk-1)*CHUNK_SIZE+1, (num_chunk-1)*CHUNK_SIZE+len(chunk)+1)
            chunk = chunk.loc[chunk['Direccion Completa'].str.strip().ne('')]
            registros = []
            for _, orig in chunk.iterrows():
                # Traducimos a columnas usadas por las reglas existentes.
                row = {
                    'NumeroRuc': orig['personal_id'],  # identificador técnico; NO es un RUC
                    'Nombre ó RazonSocial': '',
                    'Departamento': orig['adr_department_name'],
                    'Provincia': orig['adr_province_name'],
                    'Distrito': orig['adr_district_name'],
                    'Direccion': orig['Direccion Completa'],
                }
                info = mod.construir_variantes(row, correcciones)
                fila = n + len(registros) + 1
                texto = '|'.join(mod.norm_key(row[k]) for k in ('Direccion','Distrito','Provincia','Departamento'))
                clave = hashlib.sha1(texto.encode('utf-8')).hexdigest()
                ids.add(clave)
                tipos[info['tipo_direccion']] = tipos.get(info['tipo_direccion'], 0) + 1
                calidades[info['calidad_inicial']] = calidades.get(info['calidad_inicial'], 0) + 1
                r = dict(
                    FILA_ID=fila, BLOQUE_LAPTOP=1 if fila <= corte else 2,
                    ORDEN_LAPTOP=fila if fila <= corte else buscables-fila+1,
                    **row, DIRECCION_KEY=clave,
                    TIPO_DIRECCION=info['tipo_direccion'], CALIDAD_INICIAL=info['calidad_inicial'],
                    DIRECCION_LIMPIA=info['direccion_limpia'], REFERENCIA=info['referencia'],
                    TIPO_VIA=info['tipo_via'], VIA_ORIGINAL=info['via_original'],
                    VIA_CORREGIDA=info['via_corregida'], NUMERO_BUSCADO=info['numero_buscado'],
                    MANZANA=info['manzana'], LOTE=info['lote'], KM=info['km'],
                    ZONA_ORIGINAL=info['zona_original'], ZONA_CORREGIDA=info['zona_corregida'],
                    HUBO_CORRECCION=info['hubo_correccion'],
                    personal_id=orig['personal_id'], customer_id=orig['customer_id'],
                    taxpayer_id=orig['taxpayer_id'], ruc_id=orig['ruc_id'],
                    FILA_ORIGINAL=orig['FILA_ORIGINAL'],
                )
                for i in range(1, mod.MAX_VARIANTES+1):
                    r[f'Q{i}_TIPO'] = ''
                    r[f'Q{i}'] = ''
                for i, (tipo, consulta) in enumerate(info['variantes'][:mod.MAX_VARIANTES], 1):
                    r[f'Q{i}_TIPO'] = tipo
                    r[f'Q{i}'] = consulta
                registros.append(r)
            if registros:
                data = pd.DataFrame(registros, columns=columnas)
                data.to_sql('preparadas', db, if_exists='replace' if primera else 'append', index=False)
                data.to_csv(PREP_CSV, index=False, header=primera, mode='w' if primera else 'a', encoding='utf-8-sig')
                n += len(data)
                primera = False
            print(f'Bloque {num_chunk}: {n:,}/{buscables:,} direcciones preparadas')
        if n:
            db.execute('CREATE UNIQUE INDEX idx_preparadas_fila ON preparadas(FILA_ID)')
            db.execute('CREATE INDEX idx_preparadas_bloque ON preparadas(BLOQUE_LAPTOP, ORDEN_LAPTOP)')
            db.execute('CREATE INDEX idx_preparadas_key ON preparadas(DIRECCION_KEY)')
            db.commit()
    finally:
        db.close()
    diag = ([{'DIMENSION':'TIPO_DIRECCION','CATEGORIA':k,'REGISTROS':v} for k,v in tipos.items()]
          + [{'DIMENSION':'CALIDAD_INICIAL','CATEGORIA':k,'REGISTROS':v} for k,v in calidades.items()])
    pd.DataFrame(diag).to_csv(DIAGNOSTICO, index=False, encoding='utf-8-sig')
    MANIFEST.write_text(json.dumps({'input_csv':str(ENTRADA),'total_filas':n,
        'registros_originales':total,'sin_direccion':total-n,'direcciones_unicas':len(ids),
        'max_variantes':mod.MAX_VARIANTES,
        'laptop_1':{'desde':1,'hasta':corte,'sentido':'ASC','cantidad':corte},
        'laptop_2':{'desde':n,'hasta':corte+1,'sentido':'DESC','cantidad':n-corte},
        'archivo_sqlite':str(PREP_DB),'archivo_csv':str(PREP_CSV)},
        ensure_ascii=False,indent=2), encoding='utf-8')
    print('\nPREPARACIÓN TERMINADA')
    print(f'Originales: {total:,} | Preparadas: {n:,} | Únicas: {len(ids):,} | Sin dirección: {total-n:,}')
    print('SQLite:', PREP_DB)
    print('CSV:', PREP_CSV)
    print('Diagnóstico:', DIAGNOSTICO)
    print('IMPORTANTE: el robot V2 aún NO apunta a esta carpeta nueva; no ejecutarlo sin adaptarlo.')

if __name__ == '__main__':
    main()
