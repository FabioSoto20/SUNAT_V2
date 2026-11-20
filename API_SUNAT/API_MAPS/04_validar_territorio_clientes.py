# -*- coding: utf-8 -*-
"""
PASO 4 - Validacion territorial de candidatos por límites distritales (INEI/IGN).
- 100% local: NO consulta Google, NO modifica SQLite ni archivos de auditoria previos.
- Requiere una capa oficial .gpkg / .shp / .zip de límites distritales en:
  API_MAPS/clientes_direcciones/limites_inei/
- Salidas: auditoria_territorial_candidatos.csv y auditoria_territorial_resumen.csv
- Verificar estar dentro del distrito NO significa ubicacion exacta del predio.
"""
from pathlib import Path
import re
import sqlite3
import math
import unicodedata
import sys
import pandas as pd

try:
    import geopandas as gpd
    from shapely.geometry import Point
except ImportError as e:
    raise SystemExit(
        'Falta geopandas o shapely. Instalar en el entorno Jupyter: '
        '%pip install geopandas shapely pyogrio\n' + str(e)
    )

BASE = Path(__file__).resolve().parent
DIR = BASE / 'clientes_direcciones'
CAPAS = DIR / 'limites_inei'
DB_PREP = DIR / 'direcciones_preparadas_google_masivo.sqlite'
AUD_RES = DIR / 'auditoria_coordenadas_clientes.csv'
AUD_CAND = DIR / 'auditoria_candidatos_clientes.csv'
OUT_RES = DIR / 'auditoria_territorial_resumen.csv'
OUT_CAND = DIR / 'auditoria_territorial_candidatos.csv'


def norm(v):
    if v is None or pd.isna(v): return ''
    s = str(v).strip().upper()
    s = ''.join(x for x in unicodedata.normalize('NFD', s) if unicodedata.category(x) != 'Mn')
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9 ]+', ' ', s)).strip()


def norm_key(s):
    return re.sub('[^A-Z0-9]','',norm(s))


def campo(cols, opciones):
    nd = {norm_key(c): c for c in cols}
    for k in opciones:
        if norm_key(k) in nd:
            return nd[norm_key(k)]
    return None


def buscar_capa():
    if not CAPAS.exists():
        CAPAS.mkdir(parents=True, exist_ok=True)
    archivos = []
    for ext in ('*.gpkg', '*.shp', '*.zip', '*.geojson', '*.json'):
        archivos.extend(CAPAS.rglob(ext))
    # Evitar elegir ficheros auxiliares como json o .zip por encima de GPKG/SHP.
    archivos = sorted(set(archivos), key=lambda p: (
        {'.gpkg':0,'.shp':1,'.zip':2,'.geojson':3,'.json':4}.get(p.suffix.lower(),5),
        len(str(p))))
    if not archivos:
        raise SystemExit(
            '\nNO SE ENCONTRARON CAPAS DISTRITALES.\n'
            f'Descarga la capa oficial distrital desde https://ide.inei.gob.pe/ '
            '(seccion Descarga de capas cartograficas, Distrital), \n'
            'descomprime el .rar si corresponde y coloca su .gpkg o .shp '
            f'en esta carpeta:\n{CAPAS}\n'
            'No se han modificado tus archivos.'
        )
    for p in archivos:
        try:
            if p.suffix.lower() == '.zip':
                # GDAL puede leer .zip si contiene un SHP o GPKG válido.
                g = gpd.read_file('zip://' + str(p))
            else:
                g = gpd.read_file(p)
            # La existencia de geometría no demuestra que sea una capa distrital.
            if not g.empty and g.geometry.notna().any():
                return p, g
        except Exception as e:
            print(f'  No se pudo leer {p.name}: {str(e)[:180]}')
            continue
    raise SystemExit('No se pudo leer una capa geográfica válida. Extrae los .shp completos o el .gpkg dentro de limites_inei.')


def preparar_geo(g):
    dep = campo(g.columns, ('NOMBDEP','NOM_DEP','NOMBDPTO','NOMB_DEPARTAMENTO','DEPARTAMENTO','DEPARTAMEN','DEPART','DPTO','REGION','NOMREG'))
    prov = campo(g.columns, ('NOMBPROV','NOM_PROV','PROVINCIA','PROVINC','NOMPRO','NOMPROV'))
    dist = campo(g.columns, ('NOMBDIST','NOMB_DIST','NOMBDISTR','NOMBDISTRITO','DISTRITO','DISTRIT','NOMDIST','NOM_DIS','NOMDIS'))
    if not all([dep,prov,dist]):
        raise RuntimeError(
            'La capa encontrada no tiene columnas identificables de departamento/provincia/distrito.\n'
            'Columnas reales: ' + ', '.join(g.columns) + '\n'
            'Revisar si el archivo corresponde a LIMITES DISTRITALES, no departamentales. '
            'No se inventara una correspondencia.'
        )
    if g.crs is None:
        raise RuntimeError('La capa geografica no declara sistema de coordenadas (CRS). No puede validarse sin él.')
    if g.crs.to_epsg() != 4326:
        g = g.to_crs(epsg=4326)
    g = g[[dep,prov,dist,'geometry']].copy()
    g = g[g.geometry.notna() & ~g.geometry.is_empty].copy()
    g['_DEP'] = g[dep].map(norm)
    g['_PROV'] = g[prov].map(norm)
    g['_DIST'] = g[dist].map(norm)
    g['_KEY'] = list(zip(g['_DEP'],g['_PROV'],g['_DIST']))
    # Dentro de una misma división puede haber varias geometrías.
    groups = {k: v.geometry.union_all() for k,v in g.groupby('_KEY', sort=False)}
    return groups, {'Departamento':dep,'Provincia':prov,'Distrito':dist}


def numero(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError):
        return None


def ubicacion(lat,lon,key,groups):
    la,lo=numero(lat),numero(lon)
    if la is None or lo is None or not(-19.5 <= la <= 1.5 and -83.5 <= lo <= -67):
        return 'SIN_COORDENADAS_VALIDAS'
    poly=groups.get(key)
    if poly is None:
        return 'SIN_POLIGONO_CORRESPONDIENTE'
    return 'DENTRO_DISTRITO' if poly.covers(Point(lo,la)) else 'FUERA_DISTRITO_REVISAR'


def main():
    print('='*88)
    print('VALIDACION TERRITORIAL DE COORDENADAS - MODO SOLO LECTURA')
    print('='*88)
    for ruta in (DB_PREP,AUD_RES,AUD_CAND):
        if not ruta.is_file():
            raise SystemExit(f'Falta archivo de entrada: {ruta}')
    path,g=buscar_capa()
    print(f'Archivo geográfico: {path.name} | Registros de límites: {len(g):,} | CRS: {g.crs}')
    grupos, campos=preparar_geo(g)
    print('Campos de límites reconocidos:',campos)
    print(f'Divisiones territoriales disponibles: {len(grupos):,}')
    with sqlite3.connect(f'file:{DB_PREP.as_posix()}?mode=ro', uri=True) as conn:
        prep = pd.read_sql_query('SELECT FILA_ID, Departamento, Provincia, Distrito, Direccion FROM preparadas',conn)
    prep = prep.drop_duplicates(subset=['FILA_ID'])
    resumen=pd.read_csv(AUD_RES, sep=';', encoding='utf-8-sig', dtype={'FILA_ID':'int64'})
    candidatos=pd.read_csv(AUD_CAND, sep=';', encoding='utf-8-sig', dtype={'FILA_ID':'int64'})
    resumen=resumen.drop(columns=['Departamento','Provincia','Distrito'],errors='ignore').merge(prep,on='FILA_ID',how='left',validate='one_to_one')
    candidatos=candidatos.merge(prep,on='FILA_ID',how='left',validate='many_to_one')
    if candidatos.empty or resumen.empty:
        raise RuntimeError('No hay candidatos/resumen para validar.')
    mapa=prep.set_index('FILA_ID')[['Departamento','Provincia','Distrito']].to_dict('index')
    keys={int(i):(norm(d['Departamento']), norm(d['Provincia']), norm(d['Distrito'])) for i,d in mapa.items()}
    faltantes=sorted(set(keys[i] for i in resumen.FILA_ID if keys.get(i) not in grupos))
    if faltantes:
        print('\nAdvertencia: nombres administrativos sin coincidencia en la capa (hasta 15):')
        for z in faltantes[:15]: print(' -',' / '.join(z))
        print('Estas filas se marcaran SIN_POLIGONO, NO como fuera del distrito.')

    candidatos['ESTADO_TERRITORIAL']=[
        ubicacion(r.LATITUD,r.LONGITUD,keys.get(int(r.FILA_ID)),grupos)
        for r in candidatos.itertuples(index=False)
    ]
    resumen['TERRITORIO_ORIGINAL']=[
        ubicacion(r.LATITUD_ORIGINAL,r.LONGITUD_ORIGINAL,keys.get(int(r.FILA_ID)),grupos)
        for r in resumen.itertuples(index=False)
    ]
    resumen['TERRITORIO_PROPUESTA_ANTERIOR']=[
        ubicacion(r.LATITUD_PROPUESTA,r.LONGITUD_PROPUESTA,keys.get(int(r.FILA_ID)),grupos)
        for r in resumen.itertuples(index=False)
    ]

    candidatos['SCORE_AUDITORIA']=pd.to_numeric(candidatos['SCORE_AUDITORIA'],errors='coerce')
    aptos=candidatos[
        candidatos['ESTADO_TERRITORIAL'].eq('DENTRO_DISTRITO') &
        candidatos['SCORE_AUDITORIA'].gt(0) &
        ~candidatos['CLASIFICACION'].isin([
            'DIRECCION_INVALIDA','MANZANA_CONTRADICTORIA','LOTE_CONTRADICTORIO',
            'NOMBRE_GENERICO','SOLO_DISTRITO','SIN_EVIDENCIA_SUFICIENTE'
        ])
    ].copy()
    mejores=(aptos.sort_values(['FILA_ID','SCORE_AUDITORIA'],ascending=[True,False])
             .drop_duplicates('FILA_ID').set_index('FILA_ID'))

    resultado=[]
    for _,r in resumen.iterrows():
        id_=int(r['FILA_ID'])
        invalid=r.get('DECISION_PRELIMINAR') == 'DIRECCION_INVALIDA'
        encontrado=id_ in mejores.index and not invalid
        m=mejores.loc[id_] if encontrado else None
        nom='';lat=None;lon=None;clase=None
        if encontrado:
            nom=m['NOMBRE_GOOGLE'];lat=m['LATITUD'];lon=m['LONGITUD'];clase=m['CLASIFICACION']
        if invalid:
            decision='DIRECCION_INVALIDA_NO_USAR'
        elif keys.get(id_) not in grupos:
            decision='REVISAR_NOMBRES_UBIGEO_SIN_POLIGONO'
        elif encontrado:
            decision='CANDIDATO_EN_DISTRITO_NO_PREDIAL'
        elif r['TERRITORIO_ORIGINAL']=='DENTRO_DISTRITO':
            decision='ORIGINAL_EN_DISTRITO_SIN_EVIDENCIA_TEXTUAL'
        else:
            decision='SIN_CANDIDATO_CONFIABLE_EN_DISTRITO'
        resultado.append({
            'FILA_ID':id_, 'DIRECCION_ORIGINAL':r.get('DIRECCION_ORIGINAL',''),
            'Departamento':r.get('Departamento'), 'Provincia':r.get('Provincia'),
            'Distrito':r.get('Distrito'),
            'LATITUD_ORIGINAL':r.get('LATITUD_ORIGINAL'),
            'LONGITUD_ORIGINAL':r.get('LONGITUD_ORIGINAL'),
            'TERRITORIO_ORIGINAL':r['TERRITORIO_ORIGINAL'],
            'LATITUD_PROPUESTA_PREVIA':r.get('LATITUD_PROPUESTA'),
            'LONGITUD_PROPUESTA_PREVIA':r.get('LONGITUD_PROPUESTA'),
            'TERRITORIO_PROPUESTA_PREVIA':r['TERRITORIO_PROPUESTA_ANTERIOR'],
            'LATITUD_TERRITORIAL':lat, 'LONGITUD_TERRITORIAL':lon,
            'NOMBRE_CANDIDATO_TERRITORIAL':nom,
            'TIPO_CANDIDATO':clase,
            'DECISION_TERRITORIAL':decision,
            'NOTA_PRECISION':'Coordenada solo referencial. Poligono distrital no valida predio.'
        })
    final=pd.DataFrame(resultado).sort_values('FILA_ID')
    final.to_csv(OUT_RES, index=False, sep=';', encoding='utf-8-sig')
    candidatos.to_csv(OUT_CAND, index=False, sep=';', encoding='utf-8-sig')
    print('\nESTADO TERRITORIAL DE LOS CANDIDATOS (incluye rechazados textuales):')
    print(candidatos['ESTADO_TERRITORIAL'].value_counts(dropna=False).to_string())
    print('\nESTADO DE LAS DIRECCIONES:')
    print(final['DECISION_TERRITORIAL'].value_counts(dropna=False).to_string())
    print('\nCASOS CLAVE:')
    print(final[final['FILA_ID'].isin([4,9,13,16,19,20])][
        ['FILA_ID','Distrito','TERRITORIO_ORIGINAL','TERRITORIO_PROPUESTA_PREVIA',
         'NOMBRE_CANDIDATO_TERRITORIAL','DECISION_TERRITORIAL']
    ].to_string(index=False))
    print('\nSalidas:')
    print(' -',OUT_RES)
    print(' -',OUT_CAND)
    print('\nNota: Los limites distritales son referenciales y NO certifican la direccion predial.')

if __name__=='__main__':
    main()
