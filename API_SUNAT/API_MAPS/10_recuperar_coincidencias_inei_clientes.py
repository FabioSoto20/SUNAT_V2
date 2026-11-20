# -*- coding: utf-8 -*-
"""
PASO 13 - Recuperación segura de distritos que no coincidieron con INEI.

ENTRADAS (ya generadas por el Paso 11):
  clientes_direcciones/catalogo_centros_representativos_inei.csv
  clientes_direcciones/clientes_base_geografica_provisional.csv

SALIDAS NUEVAS (no modifica los archivos del Paso 11):
  clientes_direcciones/clientes_base_geografica_corregida.csv
  clientes_direcciones/diagnostico_equivalencias_inei.csv
  clientes_direcciones/diagnostico_sin_geo_paso13.csv

Estrategia: equivalencias explícitas de escritura/abreviaturas,
que SOLO se aplican cuando el trío (departamento, provincia, distrito)
existe exactamente en el catálogo INEI y es único.
No usa fuzzy matching automático, no consulta Internet ni Google Maps.
El punto distrital NO representa el domicilio del cliente/empresa.
"""

from pathlib import Path
import difflib
import re
import unicodedata
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parent
DIR = ROOT / 'clientes_direcciones'
CATALOGO = DIR / 'catalogo_centros_representativos_inei.csv'
ENTRADA = DIR / 'clientes_base_geografica_provisional.csv'
SALIDA = DIR / 'clientes_base_geografica_corregida.csv'
REPORTE = DIR / 'diagnostico_equivalencias_inei.csv'
SIN_GEO = DIR / 'diagnostico_sin_geo_paso13.csv'

# Equivalencias normativas/ortográficas frecuentes presentes en el diagnóstico.
# Solo son candidatas; una coincidencia exacta con INEI es indispensable.
DEP_ALIASES = {
    'PROVINCIA CONSTITUCIONAL DEL CALLAO': 'CALLAO',
    'PROV CONST DEL CALLAO': 'CALLAO',
    'PROVINCIA CONST DEL CALLAO': 'CALLAO',
    'REGION CALLAO': 'CALLAO',
}
PROV_ALIASES = {
    'MSCAL NIETO': 'MARISCAL NIETO',
    'MRSCAL NIETO': 'MARISCAL NIETO',
    'GRAL SANCHEZ CERRO': 'GENERAL SANCHEZ CERRO',
    'MRSCAL RAMON CASTILLA': 'MARISCAL RAMON CASTILLA',
    'MSCAL RAMON CASTILLA': 'MARISCAL RAMON CASTILLA',
    'NAZCA': 'NASCA',
    'QUISPICANCHIS': 'QUISPICANCHI',
    'CARAVELLI': 'CARAVELI',
    'DANIEL A CARRION': 'DANIEL ALCIDES CARRION',
}
DIST_ALIASES = {
    'CORONEL GREGORIO ALBARRACIN L': 'CORONEL GREGORIO ALBARRACIN LANCHIPA',
    'ELIAS SOPLINVARGAS': 'ELIAS SOPLIN VARGAS',
    'NAZCA': 'NASCA',
    'SAPALLANCA': 'SAPALLANGA',
    'AYAVARI': 'AYAVIRI',
    'RAYMONDI': 'RAIMONDI',
    'CANPANILLA': 'CAMPANILLA',
}


def norm(value):
    if value is None or pd.isna(value):
        return ''
    v = str(value).strip().upper()
    v = ''.join(ch for ch in unicodedata.normalize('NFKD', v)
                if not unicodedata.combining(ch))
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9]+', ' ', v)).strip()


def canon_triplet(d, p, t):
    return (norm(d), norm(p), norm(t))


def candidato_triplet(d, p, t):
    dd, pp, tt = canon_triplet(d, p, t)
    return (DEP_ALIASES.get(dd, dd),
            PROV_ALIASES.get(pp, pp),
            DIST_ALIASES.get(tt, tt))


def sugerencias(clave, keys_catalogo):
    d, p, t = candidato_triplet(*clave)
    mismo_dep_prov = [k for k in keys_catalogo if k[0] == d and k[1] == p]
    if mismo_dep_prov:
        orden = sorted(mismo_dep_prov,
                       key=lambda k: difflib.SequenceMatcher(None, t, k[2]).ratio(),
                       reverse=True)
        return [('/'.join(k), round(difflib.SequenceMatcher(None, t, k[2]).ratio(), 3))
                for k in orden[:3]]
    # Si falla el nombre de provincia, sugerir solo provincias del departamento;
    # nunca asignar coordenadas automáticamente por este criterio.
    provincias = sorted({k[1] for k in keys_catalogo if k[0] == d})
    matches = sorted(provincias,
                     key=lambda v: difflib.SequenceMatcher(None, p, v).ratio(),
                     reverse=True)
    return [(f'PROVINCIA_SUGERIDA:{d}/{v}',
             round(difflib.SequenceMatcher(None, p, v).ratio(), 3))
            for v in matches[:3]]


def main():
    print('=' * 90)
    print('PASO 13 - CORRECCION SEGURA DE EQUIVALENCIAS DISTRITALES INEI')
    print('=' * 90)
    for path in [CATALOGO, ENTRADA]:
        if not path.is_file():
            raise FileNotFoundError(f'Archivo necesario no encontrado: {path}')

    cat = pd.read_csv(CATALOGO, sep=';', dtype=str, keep_default_na=False,
                      encoding='utf-8-sig')
    base = pd.read_csv(ENTRADA, sep=';', dtype=str, keep_default_na=False,
                       encoding='utf-8-sig')
    requeridos_cat = ['DEP_KEY', 'PROV_KEY', 'DIST_KEY',
                      'LATITUD_DISTRITO', 'LONGITUD_DISTRITO']
    requeridos_base = ['FILA_ORIGINAL', 'adr_department_name',
                       'adr_province_name', 'adr_district_name',
                       'LATITUD_FINAL', 'LONGITUD_FINAL', 'ESTADO_GEO']
    for nombre, frame, req in [('catálogo', cat, requeridos_cat),
                                ('base', base, requeridos_base)]:
        faltan = sorted(set(req).difference(frame.columns))
        if faltan:
            raise ValueError(f'Faltan columnas en {nombre}: {faltan}')

    if base['FILA_ORIGINAL'].duplicated().any():
        raise ValueError('FILA_ORIGINAL contiene duplicados; no se modificó nada.')

    cat['_KEY'] = [canon_triplet(*k) for k in zip(
        cat['DEP_KEY'], cat['PROV_KEY'], cat['DIST_KEY'])]
    if cat['_KEY'].duplicated().any():
        raise ValueError('Claves INEI duplicadas; no se puede asignar con seguridad.')
    mapa = dict(zip(cat['_KEY'], cat.to_dict(orient='records')))
    keys = set(mapa)

    print(f'Registros originales : {len(base):,}')
    print(f'Claves INEI únicas  : {len(cat):,}')

    sin_geo_ini = base['LATITUD_FINAL'].str.strip().eq('') | base['LONGITUD_FINAL'].str.strip().eq('')
    estados_pendientes = base['ESTADO_GEO'].eq('SIN_COINCIDENCIA_INEI')
    por_corregir = base.loc[sin_geo_ini & estados_pendientes].copy()

    clave_cols = ['adr_department_name', 'adr_province_name', 'adr_district_name']
    grupos = (por_corregir.groupby(clave_cols, dropna=False)
              .size().reset_index(name='REGISTROS')
              .sort_values('REGISTROS', ascending=False))
    print(f'Sin coordenadas inicialmente: {int(sin_geo_ini.sum()):,}')
    print(f'Pendientes por nombres INEI : {len(por_corregir):,} ({len(grupos)} combinaciones)')

    # Evaluar UNA VEZ por combinación; no hay aproximación automática de distritos.
    cambios = {}
    resumen = []
    for fila in grupos.itertuples(index=False, name=None):
        d, p, t, n = fila
        anterior = canon_triplet(d, p, t)
        corregida = candidato_triplet(d, p, t)
        exito = (corregida in keys and corregida != anterior)
        # Si cat cambió respecto del Paso 11, una coincidencia exacta también es segura.
        if corregida in keys and anterior == corregida:
            exito = True
        cambios[(d, p, t)] = corregida if exito else None
        posibles = [] if exito else sugerencias(anterior, keys)
        resumen.append({
            'DEP_ORIGEN': d, 'PROV_ORIGEN': p, 'DIST_ORIGEN': t,
            'REGISTROS': int(n),
            'DEP_INEI_PROPUESTO': corregida[0],
            'PROV_INEI_PROPUESTO': corregida[1],
            'DIST_INEI_PROPUESTO': corregida[2],
            'RESULTADO': 'RECUPERADO_EQUIVALENCIA_VERIFICADA' if exito else 'PENDIENTE_REVISION_MANUAL',
            'EQUIVALENCIA': ' / '.join(anterior) + ' => ' + ' / '.join(corregida),
            'SUGERENCIA_1': posibles[0][0] if posibles else '',
            'SIMILITUD_1': posibles[0][1] if posibles else '',
            'SUGERENCIA_2': posibles[1][0] if len(posibles) > 1 else '',
            'SIMILITUD_2': posibles[1][1] if len(posibles) > 1 else '',
        })

    # Resolver las filas con una correspondencia INEI válida.
    base['METODO_MATCH_INEI'] = 'SIN_ASIGNACION'
    tiene_ini = ~sin_geo_ini
    base.loc[tiene_ini, 'METODO_MATCH_INEI'] = 'CONSERVADO_PASO11'

    recuperados = 0
    for (d, p, t), key in cambios.items():
        if key is None:
            continue
        m = (base['adr_department_name'].eq(d) &
             base['adr_province_name'].eq(p) &
             base['adr_district_name'].eq(t) &
             base['ESTADO_GEO'].eq('SIN_COINCIDENCIA_INEI') &
             base['LATITUD_FINAL'].str.strip().eq('') &
             base['LONGITUD_FINAL'].str.strip().eq(''))
        if not m.any():
            continue
        r = mapa[key]
        for col, valor in {
            'LATITUD_FINAL': r['LATITUD_DISTRITO'],
            'LONGITUD_FINAL': r['LONGITUD_DISTRITO'],
            'NIVEL_GEO': 'DISTRITO',
            'FUENTE_GEO': 'INEI',
            'METODO_GEO': 'PUNTO_INTERIOR_POLIGONO_EQUIVALENCIA',
            'TIPO_COORDENADA': 'IMPUTADA_DISTRITO',
            'ESTADO_GEO': 'DISTRITO_INEI_RECUPERADO_EQUIVALENCIA',
            'PRECISION_GEO': 'BAJA',
            'CALIDAD_GEO': 'DISTRITO_IMPUTADO',
            'APTO_DISTANCIA_A_OFICINA': '0',
            'APTO_MAPA_TERRITORIAL': '1',
            'METODO_MATCH_INEI': 'ALIAS_VALIDADO_CON_CATALOGO',
        }.items():
            base.loc[m, col] = valor
        recuperados += int(m.sum())

    # Validación: todas las coordenadas antiguas permanecen intactas.
    assert int((base['METODO_MATCH_INEI'] == 'ALIAS_VALIDADO_CON_CATALOGO').sum()) == recuperados
    sin_geo_fin = base['LATITUD_FINAL'].str.strip().eq('') | base['LONGITUD_FINAL'].str.strip().eq('')
    assert len(base) >= recuperados
    assert int(sin_geo_fin.sum()) == int(sin_geo_ini.sum()) - recuperados
    assert not (base['METODO_MATCH_INEI'].eq('ALIAS_VALIDADO_CON_CATALOGO') &
                base['APTO_DISTANCIA_A_OFICINA'].ne('0')).any()

    diag = pd.DataFrame(resumen)
    pendientes = diag[diag['RESULTADO'] == 'PENDIENTE_REVISION_MANUAL'].copy()

    # Salidas: archivos NUEVOS, no se sobrescribe ningún archivo del Paso 11.
    base.to_csv(SALIDA, sep=';', index=False, encoding='utf-8-sig')
    diag.to_csv(REPORTE, sep=';', index=False, encoding='utf-8-sig')
    pendientes.to_csv(SIN_GEO, sep=';', index=False, encoding='utf-8-sig')

    print('\n' + '-' * 90)
    print(f'ANTES - Con coordenadas : {int((~sin_geo_ini).sum()):,} ({(~sin_geo_ini).mean():.2%})')
    print(f'RECUPERADOS POR EQUIVALENCIA VERIFICADA : {recuperados:,}')
    print(f'AHORA - Con coordenadas : {int((~sin_geo_fin).sum()):,} ({(~sin_geo_fin).mean():.2%})')
    print(f'SIN COORDENADAS : {int(sin_geo_fin.sum()):,}')
    print(f'  - Sin información administrativa: {int((base["ESTADO_GEO"] == "FALTAN_DATOS_ADMINISTRATIVOS").sum()):,}')
    print(f'  - Pendientes sin equivalencia verificada: {int((base["ESTADO_GEO"] == "SIN_COINCIDENCIA_INEI").sum()):,}')
    print('\nTOP 15 EQUIVALENCIAS Y SITUACIONES:')
    if not diag.empty:
        print(diag[['DEP_ORIGEN', 'PROV_ORIGEN', 'DIST_ORIGEN', 'REGISTROS',
                    'RESULTADO', 'PROV_INEI_PROPUESTO', 'DIST_INEI_PROPUESTO']]
              .head(15).to_string(index=False))
    print('\nSALIDAS:')
    print('Base corregida   :', SALIDA)
    print('Equivalencias    :', REPORTE)
    print('Pendientes       :', SIN_GEO)
    print('\nADVERTENCIA: coordenadas INEI son referencias DISTRITALES, no predios.')
    print('No usar para calcular la oficina bancaria más cercana.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR {type(exc).__name__}: {exc}', file=sys.stderr)
        raise
