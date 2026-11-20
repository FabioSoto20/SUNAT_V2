# -*- coding: utf-8 -*-
"""Auditoría CONSERVADORA, de solo lectura, de candidatos Google Maps existentes.

No hace consultas externas, no modifica SQLite y NO certifica ubicación predial.
Propone referencias para revisión. La validez territorial requiere polígonos oficiales.
Uso: python 03_auditar_candidatos_clientes.py
"""
from pathlib import Path
import math
import re
import sqlite3
import unicodedata
import pandas as pd

BASE = Path(__file__).resolve().parent
DIR = BASE / 'clientes_direcciones'
DB_PREP = DIR / 'direcciones_preparadas_google_masivo.sqlite'
DB_RESULTS = DIR / 'google_resultados_laptop1_v2.sqlite'
CSV_RESUMEN = DIR / 'auditoria_coordenadas_clientes.csv'
CSV_CANDIDATOS = DIR / 'auditoria_candidatos_clientes.csv'

STOP = {
    'AV','AVENIDA','AVDA','CAL','CALLE','CL','JR','JIRON','PJ','PJE','PSJ','PASAJE',
    'MANZANA','MZA','MZ','LT','LOTE','URB','URBANIZACION','ASOC','ASOCIACION','AH',
    'SECTOR','BARRIO','PUEBLO','CENTRO','POBLADO','CARRETERA','CERCADO','NRO','NUMERO',
    'DE','DEL','LA','LAS','LOS','EL','EN','Y','PERU','LIMA','PROVINCIA','DISTRITO',
    'SAN','SANTA','DR','DPTO','KM','N','S','NA','NO','APLICA','ZONA','UNIDAD'
}
VIA = {'AV','AVENIDA','AVDA','CAL','CALLE','JR','JIRON','PJ','PJE','PSJ','PASAJE','CARRETERA'}
ETIQUETAS_ZONA = ('URBANIZACION','ASENTAMIENTO','BARRIO','CENTRO POBLADO','PUEBLO JOVEN', 'ASOCIACION')
GENERIC = re.compile(r'^(?:MZ|MZA|MANZANA|LT|LOTE)\s+[A-Z0-9-]+$', re.I)
INVALID = re.compile(r'\b(XDEPURAR|DEPURAR|88888888|99999999|SIN\s+DIRECCION|NO\s+REGISTRA|DIRECCION\s+FICTICIA)\b')
MZ = re.compile(r'\b(?:MZ|MZA|MANZANA)\s*([A-Z0-9-]+)\b')
LT = re.compile(r'\b(?:LT|LOTE)\s*([A-Z0-9-]+)\b')


def norm(s):
    s = '' if s is None else str(s)
    if s.lower() in ('nan','none','null','nat'): return ''
    s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
    s = re.sub(r'[^A-Z0-9 ]+', ' ', s.upper())
    return re.sub(r'\s+', ' ', s).strip()


def tok(s, local_stop=None):
    sw = STOP | (local_stop or set())
    return {t for t in norm(s).split() if len(t) >= 3 and t not in sw and not t.isdigit()}


def indice_ubicacion(lat, lon):
    try:
        a, b = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(a) and math.isfinite(b) and -19.5 <= a <= 1.5 and -83.5 <= b <= -67


def distancia_km(lat1, lon1, lat2, lon2):
    try:
        a1, o1, a2, o2 = map(float, (lat1, lon1, lat2, lon2))
    except (TypeError, ValueError):
        return None
    p1, p2 = math.radians(a1), math.radians(a2)
    dp, dl = p2-p1, math.radians(o2-o1)
    z = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return round(6371.0088 * 2 * math.asin(min(1.0, math.sqrt(z))), 2)


def primer_valor(s, pattern):
    m = pattern.search(norm(s))
    return m.group(1) if m else ''


def numero_probable_original(s):
    s = norm(s)
    # Eliminar primero bloques que tienen números que NO son puertas.
    s = re.sub(r'\b(?:MZ|MZA|MANZANA|LT|LOTE|KM|DPTO|INT|INTERIOR|PISO|OFICINA)\s*[A-Z0-9-]+', ' ', s)
    m = re.search(r'\b(?:NUMERO|NRO)\s*(\d{1,5}[A-Z]?)\b', s)
    if m: return m.group(1)
    encontrados = re.findall(r'\b\d{2,5}[A-Z]?\b', s)
    return encontrados[0] if len(encontrados) == 1 else ''


def nombre_via(s):
    t = norm(s)
    # No considerar "CAL MZ B" como nombre real de calle.
    m = re.search(r'\b(?:AV|AVENIDA|AVDA|CAL|CALLE|JR|JIRON|PJE|PSJ|PJ|PASAJE)\s+(.+)', t)
    if not m: return set()
    resto = m.group(1)
    resto = re.split(r'\b(?:MZ|MZA|MANZANA|LT|LOTE|URB|URBANIZACION|ASOC|BARRIO|SECTOR)\b|\b\d{2,5}\b', resto)[0]
    return tok(resto)


def zona_tokens(p):
    original = norm(p.get('Direccion',''))
    z = norm(p.get('ZONA_CORREGIDA','') or p.get('ZONA_ORIGINAL',''))
    # Preferir la zona explícitamente extraída; conservarla como evidencia, no coordenada.
    if not z:
        m = re.search(r'\b(?:URB|URBANIZACION|BARRIO|ASOC|ASOCIACION|SECTOR)\s+(.+)', original)
        if m: z = m.group(1)
    # Agregar las zonas explícitas del texto original, incluso si el preparador
    # retuvo solo la última URB o truncó una urbanización compuesta.
    zonas = [z] if z else []
    for m in re.finditer(r'\b(?:URB|URBANIZACION|BARRIO|ASOC|ASOCIACION|SECTOR)\s+(.+?)(?=\b(?:MZ|MZA|MANZANA|LT|LOTE|NRO|NUMERO|URB|URBANIZACION)\b|$)', original):
        zonas.append(m.group(1))
    local_stop = tok(p.get('Distrito','')) | tok(p.get('Provincia','')) | tok(p.get('Departamento',''))
    tokens = set()
    for texto in zonas:
        texto = re.split(r'\b(?:MZ|MZA|MANZANA|LT|LOTE|NUMERO|NRO)\b|\b\d{2,5}\b', texto)[0]
        tokens |= tok(texto, local_stop=local_stop)
    return tokens


def auditar(p, c):
    original = norm(p.get('Direccion',''))
    if INVALID.search(original):
        return -999, 'DIRECCION_INVALIDA', 'Marcador de depuración / dato inválido'
    if not indice_ubicacion(c.get('LATITUD'), c.get('LONGITUD')):
        return -999, 'COORDENADAS_INVALIDAS', 'Falta coordenada o está fuera de límites de Perú'
    if norm(c.get('QUERY_TIPO','')) == 'DISTRITO_FALLBACK':
        return -200, 'SOLO_DISTRITO', 'Centro distrital no identifica domicilio ni negocio'

    nombre = norm(c.get('NOMBRE_GOOGLE',''))
    direccion = norm(c.get('DIRECCION_GOOGLE',''))
    evidencia = ' '.join([nombre, direccion])
    mz_original = primer_valor(original, MZ)
    lt_original = primer_valor(original, LT)
    mz_result = primer_valor(evidencia, MZ)
    lt_result = primer_valor(evidencia, LT)

    if mz_original and mz_result and mz_original != mz_result:
        return -180, 'MANZANA_CONTRADICTORIA', f'Se busca Mz {mz_original} pero Google muestra Mz {mz_result}'
    if lt_original and lt_result and lt_original != lt_result:
        return -180, 'LOTE_CONTRADICTORIO', f'Se busca Lt {lt_original} pero Google muestra Lt {lt_result}'

    if GENERIC.fullmatch(nombre):
        return -120, 'NOMBRE_GENERICO', f'"{nombre}" no es evidencia de calle ni de ubicación empresarial'

    # NO usar el texto de QUERY como evidencia (se lo suministramos a Google).
    # NO usar DISTRITO_COV para afirmar contención geográfica.
    zona = zona_tokens(p)
    objetivo_via = nombre_via(original)
    nombre_tokens = tok(evidencia)
    coincidencias_zona = zona & nombre_tokens
    coincidencias_via = objetivo_via & nombre_tokens

    numero = numero_probable_original(original)
    es_num = bool(numero and re.search(rf'(?<![A-Z0-9]){re.escape(numero)}(?![A-Z0-9])', evidencia))
    es_zona_etiqueta = any(t in nombre for t in ETIQUETAS_ZONA)
    via_fuerte = bool(objetivo_via and (len(coincidencias_via)/len(objetivo_via)) >= 0.7)
    zona_fuerte = bool(zona and (len(coincidencias_zona)/len(zona)) >= 0.50)
    if zona and len(zona) >= 3 and len(coincidencias_zona) < 2:
        zona_fuerte = False
    texto_via = bool(re.match(r'^(?:AV|AVENIDA|AVDA|CA|CAL|CALLE|JR|JIRON|PJE|PASAJE)\b', nombre))

    score = 0
    motivos = []
    if zona_fuerte:
        score += 45
        motivos.append('coincide zona/urbanización')
        if es_zona_etiqueta:
            score += 12
            motivos.append('ficha explícita de zona')
    if via_fuerte and texto_via:
        score += 37
        motivos.append('coincide vía en ficha')
    if es_num and via_fuerte:
        score += 38
        motivos.append('coincide número de puerta')
    if mz_original and mz_result == mz_original:
        score += 16
        motivos.append('coincide manzana')
    if lt_original and lt_result == lt_original:
        score += 12
        motivos.append('coincide lote')
    # Ligera preferencia por rótulos de zona frente a nombres comerciales.
    if re.search(r'\b(BODEGA|PANADERIA|RESTAURANTE|EMPRESA|COLEGIO|MOTOS|EDIPESA)\b', nombre) and not (es_num and via_fuerte):
        score -= 12
    if int(float(c.get('WARNING_NO_PRECISA') or 0)):
        score -= 15
        motivos.append('Google advierte falta de precisión')

    # Marcamos conflictos entre tipo de calle visible y tipo declarado,
    # aunque la coincidencia nominal/numérica sea fuerte.
    if via_fuerte and es_num and norm(original).startswith('AV ') and nombre.startswith(('CAL ', 'CALLE ', 'CA ')):
        score -= 20
        motivos.append('tipo de vía diferente (AV vs CAL)')

    if es_num and via_fuerte:
        etiqueta = 'NUMERO_COINCIDENTE_SIN_VALIDAR'
    elif zona_fuerte:
        etiqueta = 'REFERENCIA_ZONA_SIN_VALIDAR'
    elif via_fuerte:
        etiqueta = 'REFERENCIA_VIA_SIN_VALIDAR'
    elif mz_original and mz_result == mz_original and lt_original and lt_result == lt_original:
        etiqueta = 'MANZANA_LOTE_SIN_VALIDAR'
    else:
        etiqueta = 'SIN_EVIDENCIA_SUFICIENTE'
        score -= 25

    motivos.append('pendiente de validación espacial por polígono distrital')
    return score, etiqueta, '; '.join(motivos)


def leer():
    if not DB_PREP.is_file() or not DB_RESULTS.is_file():
        raise FileNotFoundError(f'Se necesitan las dos bases SQLite: {DB_PREP} y {DB_RESULTS}')
    with sqlite3.connect(f'file:{DB_PREP.as_posix()}?mode=ro', uri=True) as conn:
        prep = pd.read_sql_query(
            'SELECT FILA_ID, Direccion, Distrito, Provincia, Departamento, '
            'ZONA_ORIGINAL, ZONA_CORREGIDA FROM preparadas', conn
        )
    with sqlite3.connect(f'file:{DB_RESULTS.as_posix()}?mode=ro', uri=True) as conn:
        res = pd.read_sql_query('SELECT * FROM resultados ORDER BY FILA_ID', conn)
        cand = pd.read_sql_query('SELECT * FROM candidatos ORDER BY FILA_ID, id', conn)
    return prep, res, cand


def main():
    prep, res, cand = leer()
    if res.empty:
        print('La base no contiene resultados para auditar.')
        return
    campos = prep.set_index('FILA_ID').to_dict(orient='index')
    antiguos = res.set_index('FILA_ID').to_dict(orient='index')
    grupos = {int(k): g for k, g in cand.groupby('FILA_ID', sort=False)}
    salida = []
    detalle = []

    for fila_id, viejo in antiguos.items():
        fila_id = int(fila_id)
        p = campos.get(fila_id)
        if p is None:
            continue
        original_invalid = bool(INVALID.search(norm(p.get('Direccion',''))))
        revisados = []
        for _, raw in grupos.get(fila_id, pd.DataFrame()).iterrows():
            c = raw.to_dict()
            score, etiqueta, razon = auditar(p, c)
            d = dict(
                FILA_ID=fila_id,
                CANDIDATO_ID=c.get('id'),
                NOMBRE_GOOGLE=c.get('NOMBRE_GOOGLE'),
                DIRECCION_GOOGLE=c.get('DIRECCION_GOOGLE'),
                QUERY_TIPO=c.get('QUERY_TIPO'),
                LATITUD=c.get('LATITUD'), LONGITUD=c.get('LONGITUD'),
                SCORE_ANTERIOR=c.get('SCORE'), SCORE_AUDITORIA=score,
                CLASIFICACION=etiqueta, OBSERVACION=razon
            )
            revisados.append(d)
            detalle.append(d)

        aptos = [r for r in revisados if r['SCORE_AUDITORIA'] > 0 and r['CLASIFICACION'] not in (
            'DIRECCION_INVALIDA','MANZANA_CONTRADICTORIA','LOTE_CONTRADICTORIO','NOMBRE_GENERICO',
            'SOLO_DISTRITO','SIN_EVIDENCIA_SUFICIENTE')]
        elegido = sorted(aptos, key=lambda x: (x['SCORE_AUDITORIA'],
                             x['CLASIFICACION']=='REFERENCIA_ZONA_SIN_VALIDAR'), reverse=True)[0] if aptos else None
        km = distancia_km(viejo.get('LATITUD'), viejo.get('LONGITUD'),
                          elegido['LATITUD'], elegido['LONGITUD']) if elegido else None
        if original_invalid:
            estado_aud = 'DIRECCION_INVALIDA'
        elif elegido is None:
            estado_aud = 'REVISAR_SIN_ALTERNATIVA_SEGURA'
        elif km is not None and km > 2:
            estado_aud = 'ALTERNATIVA_DIFIERE_MAS_2KM'
        elif km is not None and km > 0.1:
            estado_aud = 'ALTERNATIVA_DIFERENTE_REVISAR'
        else:
            estado_aud = 'COINCIDE_O_MUY_PROXIMA'

        salida.append(dict(
            FILA_ID=fila_id,
            DIRECCION_ORIGINAL=p.get('Direccion'),
            Distrito=p.get('Distrito'),
            ESTADO_ORIGINAL=viejo.get('ESTADO'),
            NIVEL_ORIGINAL=viejo.get('NIVEL_UBICACION_GOOGLE'),
            LATITUD_ORIGINAL=viejo.get('LATITUD'),
            LONGITUD_ORIGINAL=viejo.get('LONGITUD'),
            LATITUD_PROPUESTA=elegido['LATITUD'] if elegido else None,
            LONGITUD_PROPUESTA=elegido['LONGITUD'] if elegido else None,
            NOMBRE_CANDIDATO_PROPUESTO=elegido['NOMBRE_GOOGLE'] if elegido else None,
            CLASIFICACION_PROPUESTA=elegido['CLASIFICACION'] if elegido else None,
            SCORE_AUDITORIA=elegido['SCORE_AUDITORIA'] if elegido else None,
            DISTANCIA_CAMBIO_KM=km,
            DECISION_PRELIMINAR=estado_aud,
            OBSERVACION=elegido['OBSERVACION'] if elegido else (
                'Marcador inválido: no geolocalizar' if original_invalid else
                'No hay candidato sustentado por el texto del resultado'),
        ))

    resumen = pd.DataFrame(salida).sort_values('FILA_ID')
    candidatos = pd.DataFrame(detalle).sort_values(['FILA_ID','SCORE_AUDITORIA'], ascending=[True,False])
    resumen.to_csv(CSV_RESUMEN, index=False, encoding='utf-8-sig', sep=';')
    candidatos.to_csv(CSV_CANDIDATOS, index=False, encoding='utf-8-sig', sep=';')

    print('='*90)
    print('AUDITORIA PRELIMINAR DE GEOLOCALIZACION - SIN CONSULTAR GOOGLE Y SIN MODIFICAR SQLITE')
    print('='*90)
    print(f'Filas evaluadas: {len(resumen):,} | Candidatos conservados: {len(candidatos):,}')
    print('\nRESUMEN DE DECISIONES:')
    print(resumen['DECISION_PRELIMINAR'].value_counts().to_string())
    print('\nCASOS IMPORTANTES:')
    cols = ['FILA_ID','Distrito','NIVEL_ORIGINAL','NOMBRE_CANDIDATO_PROPUESTO',
            'CLASIFICACION_PROPUESTA','DISTANCIA_CAMBIO_KM','DECISION_PRELIMINAR']
    mostrar = resumen[resumen['FILA_ID'].isin([4,9,13,16,19,20])]
    print(mostrar[cols].to_string(index=False))
    print(f'\nArchivo resumen: {CSV_RESUMEN}')
    print(f'Archivo candidatos: {CSV_CANDIDATOS}')
    print('\nIMPORTANTE: propuestas NO verificadas por polígonos/visita; no usar todavía como predios exactos.')

if __name__ == '__main__':
    main()
