# -*- coding: utf-8 -*-
"""GEOLOCALIZACION CLIENTES - MODO 5 DIAS (2026)

Compatible con API_MAPS/12_google_maps_clientes_v5_inei.py y Paso 13.
NO modifica: direcciones.csv, SQLite preparada, SQLite V2-V5, base INEI.

Objetivo REALISTA: conservar respaldo distrital (98.79% ya obtenido) y mejorar
solo domicilios donde la consulta justifica su costo. No garantiza que Google
permita procesar 159,459 direcciones en 5 dias. Nunca certifica predios.

Modos:
    python 14_geolocalizador_definitivo_5dias.py --modo plan
    python 14_geolocalizador_definitivo_5dias.py --modo ejecutar --equipo 1 --equipos 1 --horas 110
    python 14_geolocalizador_definitivo_5dias.py --modo estado
    python 14_geolocalizador_definitivo_5dias.py --modo exportar

Dos equipos: transferir mismo proyecto con plan generado, ejecutar --equipo 1
--equipos 2 y --equipo 2 --equipos 2 respectivamente; para consolidar copiar
SQLite RESULTADOS de ambos equipos a clientes_direcciones/optimizacion_5dias/.

NO automatiza ni elude CAPTCHA. Respeta detencion ante bloqueo.
Datos y consultas de cliente permanecen locales; NO subir resultados a GitHub.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata
from urllib.parse import urlparse, unquote_plus

ROOT = Path(__file__).resolve().parent
DIR = ROOT / "clientes_direcciones"
TRABAJO = DIR / "optimizacion_5dias"
PLAN = TRABAJO / "plan_5dias.sqlite"
PREPARADAS = DIR / "direcciones_preparadas_google_masivo_v5_inei.sqlite"
BASE_GEO = DIR / "clientes_base_geografica_corregida.csv"
MODULO_ROBOT = ROOT / "12_google_maps_clientes_v5_inei.py"

# Patron deliberadamente conservador para no pedir a Google direcciones basura.
R_INVALIDO = re.compile(r"\b(?:XDEPURAR|DEPURAR|POR\s+DEPURAR|SIN\s+INFORMACION|DESCONOCIDA)\b|X{3,}|8{7,}", re.I)
R_TIPO_VIA = re.compile(r"\b(?:CAL|CALLE|AV|AVENIDA|AVDA|JR|JIRON|PSJ|PJE|PJ|PASAJE|CARRETERA|PROLONGACION)\b",re.I)
R_NUMERO = re.compile(r"\b(?:NRO|N°|NUM|NUMERO)?\s*\d{2,5}[A-Z]?\b",re.I)
R_MANZANA = re.compile(r"\b(?:MZ|MZA|MANZANA)\s*[A-Z0-9-]+", re.I)
R_ZONA = re.compile(r"\b(?:URB|URBANIZACION|ASOC|ASOCIACION|SECTOR|BARRIO|CASERIO|AAHH|A\.?H\.?|CENTRO POBLADO|COOP)\b",re.I)
R_GEN = re.compile(r"^(?:CAL|CALLE|AV|AVENIDA|JR|JIRON|PJE|PSJ|SIN NOMBRE|0+|S/N|SN|NA|N A)$",re.I)
R_POI = re.compile(r"\b(?:MUNICIPALIDAD|CENTRO COMERCIAL|MALL|HOSPITAL|CLINICA|COLEGIO|UNIVERSIDAD|IGLESIA|PARQUE|PLAZA DE ARMAS|FITNESS|GIMNASIO|GYM|CENTRO DE SALUD|HOTEL|HOSTAL|SUPERMERCADO|RESTAURANTE|BODEGA|MERCADO|FARMACIA|COMISARIA|MINIMARKET|CINE|ESTADIO|EMPRESA|GRIFO|TIENDA|BANCO|CAJERO)\b", re.I)
R_BASURA_ZONA = re.compile(r"^(?:NA|NO APLICA|A UNA CUADRA|A DOS CUADRAS|CENTRO|PRINCIPAL|SIN NOMBRE|OTROS|NINGUNO|NO TIENE)$",re.I)


def hoy():
    return datetime.now().isoformat(timespec="seconds")


def norm(s):
    s = unicodedata.normalize('NFKD', str(s or '').upper())
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    return ' '.join(re.sub(r'[^A-Z0-9]+',' ',s).split())


def cargar_robot():
    if not MODULO_ROBOT.is_file():
        raise FileNotFoundError(f"Falta robot del Paso 14: {MODULO_ROBOT}")
    spec = importlib.util.spec_from_file_location('cliente_google_v5_inei_5dias', MODULO_ROBOT)
    if spec is None or spec.loader is None:
        raise RuntimeError("No se pudo importar robot V5+INEI")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.PREP_DB = PREPARADAS
    m.MAX_CANDIDATOS_LISTA = 2  # configurable por --fichas
    m.ESPERA_INICIAL_MS = 1200
    m.ESPERA_ESTABILIDAD_MS = 450
    m.ESPERA_MAX_MS = 5000
    m.PAGE_TIMEOUT_MS = 18000
    m.REINTENTOS_NAVEGACION_CON_INTERNET = 1  # evitar bloqueos prolongados
    # La V5 esperaba indefinidamente cuando no había Internet. El presupuesto
    # de 5 días exige fallar rápido y conservar el trabajo ya guardado.
    def navegar_acotado(page,url):
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=m.PAGE_TIMEOUT_MS)
            if m.detectar_bloqueo(page):
                raise m.BloqueoGoogle('CAPTCHA/bloqueo detectado')
        except m.BloqueoGoogle:
            raise
        except Exception as e:
            raise m.ErrorNavegacion(f'Navegación agotó tiempo: {str(e)[:130]}') from e
    m.goto_con_recuperacion = navegar_acotado
    return m


def conectar(path):
    con = sqlite3.connect(str(path), timeout=60)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=60000')
    return con


def key_ubicacion(row):
    texto = '|'.join(norm(row.get(k,'')) for k in ('Departamento','Provincia','Distrito','Direccion'))
    return hashlib.sha256(texto.encode('utf-8')).hexdigest()


def _zona(row):
    # Comienza por columna preparada, pero evita referencias vagas (A UNA CUADRA).
    v = norm(row.get('ZONA_CORREGIDA','') or row.get('ZONA_ORIGINAL',''))
    if not v or R_BASURA_ZONA.fullmatch(v):
        v = ''
    if v:
        v = re.split(r'\b(?:MZ|MZA|LT|LOTE)\b', v, maxsplit=1)[0].strip()
    if not v:
        original = norm(row.get('Direccion',''))
        match = re.search(r'\b(?:URB|URBANIZACION|ASOC|ASOCIACION|SECTOR|BARRIO|CASERIO|AAHH|COOP)\s+(.+)', original)
        if match:
            v = re.split(r'\b(?:MZ|MZA|LT|LOTE)\b', match.group(1), maxsplit=1)[0].strip()
    if len(v) < 5 or R_BASURA_ZONA.fullmatch(v):
        return ''
    # Eliminar coletillas de distrito/provincia si están al final.
    for nombre in [row.get('Distrito',''),row.get('Provincia','')]:
        s = norm(nombre)
        if len(s) > 4 and v.endswith(' '+s):
            v = v[:-(len(s)+1)].strip()
    return v if len(v) >= 5 else ''


def plan_consultas(row, m=None):
    """1-2 consultas CONCRETAS. Nada de centros comerciales/municipalidades,
    ni preguntas de solo distrito. En caso de información insuficiente: INEI.
    """
    direc = norm(row.get('Direccion',''))
    if not direc or R_GEN.fullmatch(direc) or R_INVALIDO.search(direc):
        return 0, '', '', 'DIRECCION_INVALIDA_O_VAGA'
    if len(direc) < 8 or len(re.findall(r'[A-Z]{3,}',direc)) < 1:
        return 0, '', '', 'DIRECCION_SIN_COMPONENTES'
    if not all(norm(row.get(k,'')) for k in ('Departamento','Provincia','Distrito')):
        return 0, '', '', 'FALTAN_DATOS_ADMIN'
    zona = _zona(row)
    calle = bool(R_TIPO_VIA.search(direc))
    mz = bool(R_MANZANA.search(direc))
    numero = ''
    via = ''
    if m is not None:
        numero = str(m._numero_puerta_original(direc) or '').strip()
        via = norm(m._via_antes_del_numero_v5(direc) or '')
    else:
        num = re.findall(r'\b\d{2,5}\b',direc)
        numero = num[0] if len(num)==1 else ''
        match = re.search(r'\b(?:AV|CAL|JR|PSJ|PJE|PASAJE|AVENIDA|CALLE|JIRON)\s+(.+)',direc)
        if match:
            via = re.split(r'\b(?:MZ|LT|LOTE|URB|SECTOR|BARRIO|ASOC)\b',match.group(1))[0].strip()
    if len(via) < 4 or R_GEN.fullmatch(via):
        via = ''
    # No mandar a Google consultas de lugares/vías inexistentes.
    if not via and not zona:
        return 0, '', '', 'SIN_VIA_NI_ZONA_UTIL'
    pri = 0
    if via and numero: pri = 100
    elif zona and mz: pri = 90
    elif via and zona: pri = 80
    elif via: pri = 65
    elif zona: pri = 50
    # Dirección muy genérica (CAL SIN NOMBRE...): mejor solo zona/mz.
    if 'SIN NOMBRE' in direc:
        if zona: pri = min(pri,70)
        else: return 0, '', '', 'SIN_NOMBRE_SIN_ZONA'
    if 'S/N' in norm(row.get('Direccion','')) and not zona and not via:
        return 0, '', '', 'SIN_NUMERO_SIN_REFERENCIA'
    # Administrativos del INEI (corregidos) en TODAS las nuevas consultas.
    admin = []
    for campo in ('Distrito','Provincia','Departamento'):
        p = norm(row.get(campo,''))
        if p and p not in admin:
            admin.append(p)
    base = ', '.join(admin + ['PERU'])
    # Evita mandar nombre de distrito duplicado dentro de la consulta.
    cuerpo = direc
    for item in admin:
        if len(item) >= 5 and cuerpo.endswith(' '+item):
            cuerpo = cuerpo[:-(len(item)+1)].strip()
            break
    if R_POI.search(cuerpo) and not (via or zona):
        return 0, '', '', 'POI_NO_ES_DIRECCION'
    if via and numero:
        q1 = f'{via} {numero}, {base}'
        q2 = f'{cuerpo}, {base}' if zona else ''
    elif zona and mz:
        q1 = f'{zona}, {base}'  # No se busca 'Mz B' aislada
        q2 = f'{cuerpo}, {base}' if via else ''
    elif via:
        q1 = f'{cuerpo}, {base}'
        q2 = f'{via}, {base}' if norm(cuerpo) != norm(via) else ''
    else:
        q1 = f'{zona}, {base}'
        q2 = ''
    if norm(q1) == norm(q2):
        q2 = ''
    return pri, q1, q2, 'APTA_PRIORIZADA'


def crear_plan(m):
    TRABAJO.mkdir(parents=True, exist_ok=True)
    if not PREPARADAS.is_file():
        raise FileNotFoundError(f"No existe base preparada Paso 14: {PREPARADAS}")
    if PLAN.exists():
        with closing(conectar(PLAN)) as con:
            n = con.execute('SELECT COUNT(*) FROM trabajos').fetchone()[0]
        print(f"Plan ya existe: {n:,} direcciones únicas. No se modifica: {PLAN}")
        return
    tmp = TRABAJO / ('.plan_creando_%d.sqlite' % os.getpid())
    if tmp.exists():
        raise FileExistsError(tmp)
    con = conectar(tmp)
    try:
        con.executescript('''
        CREATE TABLE trabajos (
          KEY TEXT PRIMARY KEY, FILA_ID INTEGER, PRIORIDAD INTEGER, MOTIVO TEXT,
          QUERY1 TEXT, QUERY2 TEXT, GRUPO INTEGER, REPETICIONES INTEGER DEFAULT 1
        );
        CREATE TABLE relaciones (FILA_ID INTEGER PRIMARY KEY,
          FILA_ORIGINAL INTEGER NOT NULL, KEY TEXT NOT NULL);
        CREATE INDEX idx_rel_key ON relaciones(KEY);
        CREATE INDEX idx_trab_grupo_prior ON trabajos(GRUPO,PRIORIDAD DESC);
        ''')
        con.commit()
        with closing(conectar(PREPARADAS)) as src:
            cur = src.execute('SELECT * FROM preparadas ORDER BY FILA_ID')
            n = 0; stats = Counter()
            for x in cur:
                row = dict(x)
                fila_id = int(row['FILA_ID'])
                fila_original = int(row['FILA_ORIGINAL'])
                key = key_ubicacion(row)
                prioridad,q1,q2,motivo = plan_consultas(row,m)
                grupo = (int(key[:12],16) % 2) + 1
                registrado = con.execute('INSERT OR IGNORE INTO trabajos (KEY,FILA_ID,PRIORIDAD,MOTIVO,QUERY1,QUERY2,GRUPO) VALUES (?,?,?,?,?,?,?)',
                       (key,fila_id,prioridad,motivo,q1,q2,grupo)).rowcount
                if not registrado:
                    con.execute('UPDATE trabajos SET REPETICIONES=REPETICIONES+1 WHERE KEY=?',(key,))
                con.execute('INSERT INTO relaciones VALUES (?,?,?)',(fila_id,fila_original,key))
                stats[motivo if registrado else 'DUPLICADO'] += 1
                n += 1
                if n % 10000 == 0:
                    con.commit()
                    print(f'Plan: {n:,} filas leídas',flush=True)
            con.commit()
            nwork=con.execute('SELECT COUNT(*) FROM trabajos').fetchone()[0]
            elegibles=con.execute('SELECT COUNT(*) FROM trabajos WHERE PRIORIDAD>0').fetchone()[0]
            grupos=con.execute('SELECT GRUPO,COUNT(*) FROM trabajos GROUP BY GRUPO').fetchall()
        con.execute('PRAGMA integrity_check')
    except BaseException:
        con.close()
        try:tmp.unlink()
        except OSError:pass
        raise
    else:
        con.close()
        os.replace(tmp, PLAN)
        print('PLAN GENERADO:',PLAN)
        print(f'Filas preparadas: {n:,} | Direcciones únicas: {nwork:,} | Elegibles: {elegibles:,}')
        print('Duplicados evitados:',n-nwork)
        print('Distribución equipos:',[(r[0],r[1]) for r in grupos])
        print('Causas:')
        for k,v in stats.most_common(15):print(f'  {k}: {v:,}')


def abrir_resultados(equipo):
    path=TRABAJO / f'resultados_5dias_equipo{equipo}.sqlite'
    con=conectar(path)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS resultados (
      KEY TEXT PRIMARY KEY, FILA_ID INTEGER, ESTADO TEXT, FUENTE TEXT,
      CALIDAD_GEO TEXT, LATITUD REAL, LONGITUD REAL, TERRITORIO TEXT,
      NOMBRE TEXT, MOTIVO TEXT, CONSULTAS INTEGER, FICHAS INTEGER,
      SEGUNDOS REAL, FECHA TEXT);
    CREATE TABLE IF NOT EXISTS consultas (
      ID INTEGER PRIMARY KEY AUTOINCREMENT, KEY TEXT, FILA_ID INTEGER,
      NUMERO INTEGER, FUENTE TEXT, QUERY TEXT, SEGUNDOS REAL,
      FICHAS INTEGER, CANDIDATOS INTEGER, VALIDOS INTEGER,
      MEJOR_NOMBRE TEXT, CALIDAD TEXT, ERROR TEXT, FECHA TEXT);
    CREATE INDEX IF NOT EXISTS idx_consultas_key ON consultas(KEY);
    CREATE TABLE IF NOT EXISTS configuracion (CLAVE TEXT PRIMARY KEY, VALOR TEXT);
    ''')
    con.commit()
    return con


def guardar(con,key,fid,estado,fuente='',calidad='',lat=None,lon=None,
            territorio='',nombre='',motivo='',consultas=0,fichas=0,segundos=0):
    con.execute('''INSERT OR REPLACE INTO resultados
        (KEY,FILA_ID,ESTADO,FUENTE,CALIDAD_GEO,LATITUD,LONGITUD,TERRITORIO,NOMBRE,MOTIVO,
         CONSULTAS,FICHAS,SEGUNDOS,FECHA) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
       (key,fid,estado,fuente,calidad,lat,lon,territorio,nombre,motivo,consultas,fichas,round(segundos,3),hoy()))
    con.commit()


def _sin_poi(c,m):
    nombre=norm(c.get('nombre',''))
    direccion=norm(c.get('direccion',''))
    if R_POI.search(nombre):
        return False,'FICHA_COMERCIO_O_INSTITUCION_NO_ES_DIRECCION'
    if any(z in nombre for z in ('CENTRO COMERCIAL','MUNICIPALIDAD','ESTADIO')):
        return False,'FICHA_POI_NO_PREDIO'
    # Importante: nombre Google tipo "Av. X" sin dirección detallada sí es vía.
    if not nombre and not direccion:
        return False,'SIN_EVIDENCIA_VISIBLE'
    return True,''


def validar_google(row,candidato,q_tipo,query,m):
    res=m.evaluar_calidad_v5(row,m.evaluar_candidato(row,candidato,q_tipo,query))
    valido,motivo=_sin_poi(res,m)
    if not valido:
        res=dict(res, prioridad=0, nivel='POI_NO_RELEVANTE', motivo_auditoria=motivo)
    # Coincidencia de vía con tipo distinto exige revisión, NO sustituye punto INEI.
    if res.get('nivel') == 'VIA_NUMERO_TIPO_DISTINTO_REVISAR':
        res=dict(res,prioridad=0,motivo_auditoria='Tipo de vía distinto; revisar sin asignar')
    return res


def stop_ahora(best,row):
    if not best:return False
    clase=str(best.get('nivel',''))
    if clase=='COINCIDENCIA_VIA_NUMERO_POR_VERIFICAR':return True
    # Si no hay número: una vía suficientemente sólida es suficiente.
    n=norm(row.get('Direccion',''))
    tiene_num=bool(re.search(r'\b\d{2,5}\b',n))
    if not tiene_num and clase in {'REFERENCIA_VIA','REFERENCIA_VIA_POR_VERIFICAR'}:return True
    # Si solo tenemos la urbanización, evita repetir búsquedas irrelevantes.
    if not R_TIPO_VIA.search(n) and clase=='REFERENCIA_ZONA':return True
    return False


def usar_nominatim_local(url, row, q, m, session, timeout):
    """OPCIONAL: usa sólo servidor local, jamás servicio público masivo.
    Nominatim no devuelve certeza predial; clasifica calle/zona sin POIs.
    """
    from requests.exceptions import RequestException
    endpoint=url.rstrip('/')+'/search'
    try:
        r=session.get(endpoint,params={'q':q,'format':'json','addressdetails':1,'limit':3,'countrycodes':'pe'},timeout=timeout)
        r.raise_for_status()
        datos=r.json()
    except (RequestException,ValueError):
        return None,0,'ERROR_LOCAL'
    mejor=None
    via=m._via_objetivo(row.get('Direccion',''))
    zona=m._zona_objetivo(row)
    numero=m._numero_puerta_original(row.get('Direccion',''))
    validos=0
    for x in datos:
        if x.get('class') in {'shop','amenity','tourism','leisure','office','historic'}:
            continue
        try:lat,lon=float(x['lat']),float(x['lon'])
        except (TypeError,ValueError,KeyError):continue
        territorio=m._territorio(row,lat,lon)
        if territorio!='DENTRO_DISTRITO':continue
        a=x.get('address') or {}
        texto_via=norm(a.get('road') or a.get('pedestrian') or a.get('residential') or '')
        texto_zona=norm(' '.join(str(a.get(k) or '') for k in ('suburb','neighbourhood','quarter','hamlet','village')))
        nom=norm(x.get('name') or '')
        if R_POI.search(nom):continue
        rua=set(norm(texto_via).split()); vias=set(via)
        cobertura_via=len(vias & rua)/len(vias) if vias else 0
        zon=set(zona); zo=set(texto_zona.split())
        cobertura_zona=len(zon & zo)/len(zon) if zon else 0
        if cobertura_via >= .78 and len(via)>0:
            if numero and norm(a.get('house_number','')) == norm(numero):
                clase='COINCIDENCIA_VIA_NUMERO_POR_VERIFICAR'; rank=90
            else:clase='REFERENCIA_VIA_POR_VERIFICAR'; rank=60
        elif cobertura_zona >= .70 and zona:
            clase='REFERENCIA_ZONA';rank=55
        else:continue
        validos+=1
        dato={'lat':lat,'lon':lon,'nivel':clase,'territorio':territorio,'prioridad':rank,
              'nombre':str(x.get('display_name') or '')[:160]}
        if mejor is None or dato['prioridad']>mejor['prioridad']:
            mejor=dato
    return mejor,validos,'OK'


def ejecutar(args):
    if not PLAN.exists():raise FileNotFoundError('Primero ejecuta --modo plan')
    m=cargar_robot()
    m.cargar_limites_inei()
    m.MAX_CANDIDATOS_LISTA=max(1,min(2,args.fichas))
    # Filtra enlaces evidentes a restaurantes, malls, municipalidades, etc.
    # ANTES de entrar a la ficha (es donde más tiempo perdía V5).
    url_lista_original=m.urls_resultados_lista
    def links_direcciones_solamente(page):
        limite=m.MAX_CANDIDATOS_LISTA
        try:
            m.MAX_CANDIDATOS_LISTA=8  # mirar 8 títulos, visitar máximo 1-2
            lista=url_lista_original(page)
        finally:
            m.MAX_CANDIDATOS_LISTA=limite
        filtradas=[]
        for url in lista:
            titulo=norm(unquote_plus(urlparse(url).path.split('/maps/place/')[-1].split('/')[0]))
            if R_POI.search(titulo):
                continue
            filtradas.append(url)
            if len(filtradas)>=limite:
                break
        return filtradas
    m.urls_resultados_lista=links_direcciones_solamente
    con=abrir_resultados(args.equipo)
    session=None
    nom_url=(args.nominatim_local or '').strip()
    if nom_url:
        p=urlparse(nom_url)
        if p.hostname not in {'127.0.0.1','localhost','::1'}:
            raise ValueError('Nominatim debe ser SERVIDOR LOCAL; no usar servicio público masivo')
        import requests
        session=requests.Session()
        try:
            r=session.get(nom_url.rstrip('/')+'/search',params={'q':'Lima, Peru','format':'json','limit':1},timeout=2.5)
            r.raise_for_status()
            print('Nominatim local: OK (fuente complementaria rápida)')
        except Exception as ex:
            print('Nominatim local no accesible; se continúa sin él:',str(ex)[:90])
            nom_url=''
    # El presupuesto no se reinicia al reabrir Jupyter: se conserva la hora
    # de comienzo en la SQLite de resultados del equipo.
    primera = con.execute("SELECT VALOR FROM configuracion WHERE CLAVE='INICIO_EPOCH'").fetchone()
    if primera:
        inicio_global=float(primera[0])
    else:
        inicio_global=time.time()
        con.execute("INSERT INTO configuracion VALUES ('INICIO_EPOCH',?)",(str(inicio_global),))
        con.commit()
    maxseg=args.horas*3600 if args.horas>0 else float('inf')
    print('Presupuesto máximo:',args.horas,'h calendario desde',datetime.fromtimestamp(inicio_global).isoformat(timespec='seconds'))
    print('Tiempo ya transcurrido:',round((time.time()-inicio_global)/3600,2),'h',flush=True)
    inicio=time.monotonic(); procesados=0; log=Counter()
    with closing(conectar(PLAN)) as plan, closing(conectar(PREPARADAS)) as prep, closing(con):
        # Orden útil: primero vía+número y urbanización/manzana; luego vías;
        # las direcciones sin evidencia permanecen con INEI SIN consultar Google.
        grupos=[1,2] if args.equipos==1 else [args.equipo]
        grupos_sql=','.join('?' for _ in grupos)
        trabajos=plan.execute(f'SELECT * FROM trabajos WHERE GRUPO IN ({grupos_sql}) ORDER BY PRIORIDAD DESC, FILA_ID',grupos)
        # Evitar arrancar Chrome si no hay filas candidatas (solo ajustes o export).
        browser=context=page=None
        try:
            for r in trabajos:
                if procesados and args.max_trabajos>0 and procesados>=args.max_trabajos:break
                if time.time()-inicio_global>=maxseg:
                    print('Presupuesto de horas alcanzado; guarda lo realizado y exporta consolidado.');break
                key=r['KEY']; fila_id=int(r['FILA_ID']); pri=int(r['PRIORIDAD'])
                if pri<args.prioridad_minima:continue
                existe=con.execute('SELECT ESTADO FROM resultados WHERE KEY=?',(key,)).fetchone()
                if existe and not (args.reintentar and existe['ESTADO'] in {'ERROR_TECNICO','BLOQUEO'}):continue
                raw=prep.execute('SELECT * FROM preparadas WHERE FILA_ID=?',(fila_id,)).fetchone()
                if raw is None:raise RuntimeError(f'Fila preparada {fila_id} no existe')
                row=dict(raw); start=time.monotonic(); qlog=[]
                # Validaciones antes de la red
                if pri<=0 or m._k_administrativa(row) not in m.POLIGONOS_DISTRITALES:
                    guardar(con,key,fila_id,'SOLO_DISTRITO',motivo=r['MOTIVO'],segundos=time.monotonic()-start)
                    procesados+=1;log['SIN_GOOGLE']+=1;continue
                best=None;fuente='';nconsultas=0;nfichas=0
                qs=[r['QUERY1']]+([r['QUERY2']] if r['QUERY2'] and args.consultas>=2 else [])
                if nom_url and session is not None:
                    q=qs[0]; tq=time.monotonic()
                    hit,nvalido,error=usar_nominatim_local(nom_url,row,q,m,session,args.timeout_local)
                    dur=time.monotonic()-tq
                    con.execute('INSERT INTO consultas (KEY,FILA_ID,NUMERO,FUENTE,QUERY,SEGUNDOS,FICHAS,CANDIDATOS,VALIDOS,MEJOR_NOMBRE,CALIDAD,ERROR,FECHA) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (key,fila_id,0,'NOMINATIM_LOCAL',q,round(dur,3),0,0,nvalido,hit['nombre'] if hit else '',hit['nivel'] if hit else '',error,hoy()))
                    if hit:best=hit;fuente='NOMINATIM_LOCAL'
                # Google solo para búsquedas realmente útiles. Si Nominatim dio
                # vía o número, Google no aumenta cobertura lo suficiente.
                debe_google=(best is None or (pri>=95 and best['nivel'] != 'COINCIDENCIA_VIA_NUMERO_POR_VERIFICAR'))
                if debe_google and args.sin_google is False:
                    if context is None:
                        try:
                            from playwright.sync_api import sync_playwright
                            pw=sync_playwright().start()
                            browser,context,page=m.lanzar_navegador(pw)
                        except Exception:
                            try:pw.stop()
                            except Exception:pass
                            raise
                    for n,q in enumerate(qs,1):
                        tq=time.monotonic(); encontrados=[];error='';validos=0;fichas=0
                        try:
                            encontrados=m.buscar_google(context,page,q)
                            # La API devuelve candidatas detalladas; si la página fue
                            # lista de resultados, cada elemento representa ficha visitada.
                            fichas=min(len(encontrados),m.MAX_CANDIDATOS_LISTA)
                            lista=[validar_google(row,c,'ORIGINAL' if n==1 else 'SOLO_VIA',q,m) for c in encontrados]
                            validos=sum(1 for c in lista if int(c.get('prioridad',0))>0)
                            candidato=m.mejor_candidato(lista)
                            if candidato and (best is None or candidato['prioridad']>best['prioridad']):
                                best=candidato;fuente='GOOGLE_MAPS'
                        except m.BloqueoGoogle as exc:
                            error='BLOQUEO_CAPTCHA';
                            dur=time.monotonic()-tq
                            con.execute('INSERT INTO consultas (KEY,FILA_ID,NUMERO,FUENTE,QUERY,SEGUNDOS,FICHAS,CANDIDATOS,VALIDOS,MEJOR_NOMBRE,CALIDAD,ERROR,FECHA) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                              (key,fila_id,n,'GOOGLE_MAPS',q,round(dur,3),fichas,len(encontrados),validos,'','',error,hoy()))
                            con.commit()
                            print('BLOQUEO GOOGLE. Se detiene SIN intentar sortearlo. Última fila:',fila_id,flush=True)
                            return
                        except Exception as exc:
                            error='ERROR_NAVEGACION:'+str(exc)[:100]
                        dur=time.monotonic()-tq;nconsultas+=1;nfichas+=fichas
                        con.execute('INSERT INTO consultas (KEY,FILA_ID,NUMERO,FUENTE,QUERY,SEGUNDOS,FICHAS,CANDIDATOS,VALIDOS,MEJOR_NOMBRE,CALIDAD,ERROR,FECHA) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (key,fila_id,n,'GOOGLE_MAPS',q,round(dur,3),fichas,len(encontrados),validos,
                           best['nombre'][:120] if best else '',best['nivel'] if best else '',error,hoy()))
                        if best and stop_ahora(best,row):break
                        if n==1 and not r['QUERY2']:break
                tiempo=time.monotonic()-start
                if best:
                    guardar(con,key,fila_id,'ENCONTRADO_REFERENCIA',fuente,best['nivel'],best['lat'],best['lon'],
                        best.get('territorio','DENTRO_DISTRITO'),best.get('nombre','')[:160],
                        'REFERENCIA_PROVISIONAL_NO_PREDIAL',nconsultas,nfichas,tiempo)
                    log['ENCONTRADO_REFERENCIA']+=1
                else:
                    guardar(con,key,fila_id,'SOLO_DISTRITO',motivo='Sin evidencia suficiente; conservar INEI',
                            consultas=nconsultas,fichas=nfichas,segundos=tiempo)
                    log['SOLO_DISTRITO']+=1
                procesados+=1
                if procesados<=5 or procesados%100==0:
                    print(f"Equipo {args.equipo} | {procesados:,} nuevos | fila {fila_id:,} | {tiempo:.1f}s | "
                          f"Google Q={nconsultas} | calidad={best['nivel'] if best else 'INEI'} | "
                          f"horas={((time.monotonic()-inicio)/3600):.2f}",flush=True)
                if args.pausa>0 and nconsultas>0:time.sleep(args.pausa)
                if procesados%300==0 and browser is not None:
                    m.cerrar_navegador(browser,context)
                    browser=context=page=None
                    try:pw.stop()
                    except Exception:pass
        finally:
            if browser is not None:m.cerrar_navegador(browser,context)
            if 'pw' in locals():
                try:pw.stop()
                except Exception:pass
            if session is not None:session.close()
    print('EJECUCION FINALIZADA, nuevos trabajos:',procesados,'estados:',dict(log))
    print('Ahora: --modo exportar para integrar resultados + INEI, incluso los no consultados.')


def estado():
    print('PLAN:',PLAN)
    if not PLAN.exists():return
    with closing(conectar(PLAN)) as c:
        a=c.execute('SELECT COUNT(*),SUM(CASE WHEN PRIORIDAD>0 THEN 1 ELSE 0 END), SUM(REPETICIONES) FROM trabajos').fetchone()
        print(f'Únicas: {a[0]:,} | elegibles {a[1]:,} | registros {a[2]:,}')
        print('Prioridades:')
        for r in c.execute('SELECT PRIORIDAD,COUNT(*) FROM trabajos GROUP BY PRIORIDAD ORDER BY PRIORIDAD DESC'):
            print(r[0],f'{r[1]:,}')
    for equipo in (1,2):
        p=TRABAJO/f'resultados_5dias_equipo{equipo}.sqlite'
        if not p.exists():continue
        with closing(conectar(p)) as c:
            print(f'Equipo {equipo}:')
            for r in c.execute('SELECT ESTADO,COUNT(*),ROUND(SUM(SEGUNDOS)/3600,1) FROM resultados GROUP BY ESTADO'):
                print(' ',r[0],r[1], 'horas registradas',r[2])
            q=c.execute("SELECT COUNT(*),ROUND(SUM(SEGUNDOS)/3600,1) FROM consultas WHERE FUENTE='GOOGLE_MAPS'").fetchone()
            print(' Consultas Google:',q[0],'horas',q[1])


def exportar():
    import pandas as pd
    if not BASE_GEO.is_file():raise FileNotFoundError('Ejecuta Paso 13: '+str(BASE_GEO))
    if not PLAN.is_file():raise FileNotFoundError('Ejecuta --modo plan antes de exportar')
    print('Leyendo base distrital completa...',flush=True)
    df=pd.read_csv(BASE_GEO,sep=';',dtype=str,encoding='utf-8-sig',keep_default_na=False,low_memory=False)
    if df['FILA_ORIGINAL'].duplicated().any():raise RuntimeError('Duplicados en FILA_ORIGINAL, abortado')
    original_n=len(df)
    df['LATITUD_DISTRITO_RESPALDO']=df['LATITUD_FINAL']
    df['LONGITUD_DISTRITO_RESPALDO']=df['LONGITUD_FINAL']
    df['FUENTE_GEO_5DIAS']='INEI'
    df.loc[df['LATITUD_FINAL'].eq('') | df['LONGITUD_FINAL'].eq(''),'FUENTE_GEO_5DIAS']='SIN_COORDENADA'
    df['CALIDAD_GEO_5DIAS']=df['NIVEL_GEO']
    df['ESTADO_BUSQUEDA_5DIAS']='NO_CONSULTADA'
    df['APTO_DISTANCIA_A_OFICINA']='0'
    with closing(conectar(PLAN)) as con:
        rel=pd.read_sql_query('SELECT FILA_ORIGINAL,KEY FROM relaciones',con,dtype={'FILA_ORIGINAL':str})
    resultados=[]
    for e in (1,2):
        p=TRABAJO/f'resultados_5dias_equipo{e}.sqlite'
        if p.is_file():
            with closing(conectar(p)) as c:
                z=pd.read_sql_query('SELECT KEY,ESTADO,FUENTE,CALIDAD_GEO,LATITUD,LONGITUD,TERRITORIO,SEGUNDOS FROM resultados',c)
                resultados.append(z)
    if resultados:
        rs=pd.concat(resultados,ignore_index=True)
        rs['_VAL']=rs['LATITUD'].notna() & rs['LONGITUD'].notna() & rs['ESTADO'].eq('ENCONTRADO_REFERENCIA')
        # Si ambas máquinas terminaron mismo key, conservar referencia y fuente con mayor evidencia.
        rs['_RANGO']=rs['CALIDAD_GEO'].map({
            'COINCIDENCIA_VIA_NUMERO_POR_VERIFICAR':100,'VIA_NUMERO_GRAFIA_REVISAR':90,
            'REFERENCIA_VIA':80,'REFERENCIA_VIA_POR_VERIFICAR':75,
            'REFERENCIA_ZONA':60,'REFERENCIA_VIA_GRAFIA_REVISAR':55
        }).fillna(0)
        rs=rs.sort_values(['_VAL','_RANGO'],ascending=[False,False]).drop_duplicates('KEY')
        merges=rel.merge(rs,on='KEY',how='left')
        merges=merges[['FILA_ORIGINAL','ESTADO','FUENTE','CALIDAD_GEO','LATITUD','LONGITUD','TERRITORIO','SEGUNDOS']]
        df=df.merge(merges,on='FILA_ORIGINAL',how='left',validate='one_to_one')
        mask=df['ESTADO'].eq('ENCONTRADO_REFERENCIA') & df['LATITUD'].notna() & df['LONGITUD'].notna() & df['TERRITORIO'].eq('DENTRO_DISTRITO')
        df.loc[mask,'LATITUD_FINAL']=df.loc[mask,'LATITUD'].map(lambda x:format(x,'.8f'))
        df.loc[mask,'LONGITUD_FINAL']=df.loc[mask,'LONGITUD'].map(lambda x:format(x,'.8f'))
        df.loc[mask,'NIVEL_GEO']=df.loc[mask,'CALIDAD_GEO']
        df.loc[mask,'FUENTE_GEO_5DIAS']=df.loc[mask,'FUENTE']
        df.loc[mask,'CALIDAD_GEO_5DIAS']=df.loc[mask,'CALIDAD_GEO']
        df.loc[mask,'ESTADO_BUSQUEDA_5DIAS']='REFERENCIA_PROVISIONAL'
        df.loc[~mask & df['ESTADO'].notna(),'ESTADO_BUSQUEDA_5DIAS']=df.loc[~mask & df['ESTADO'].notna(),'ESTADO']
        df.drop(columns=['ESTADO','FUENTE','CALIDAD_GEO','LATITUD','LONGITUD','TERRITORIO','SEGUNDOS'],inplace=True)
    else:
        print('Sin resultados Google: se exporta la base distrital intacta con metadatos')
    # Mantener fuente base INEI consistente en campos que ya existían.
    df.loc[df['FUENTE_GEO_5DIAS'].isin(['GOOGLE_MAPS','NOMINATIM_LOCAL']),'FUENTE_GEO']=df['FUENTE_GEO_5DIAS']
    if len(df)!=original_n:raise RuntimeError(f'Integridad: filas originales {original_n}, salida {len(df)}')
    if df['FILA_ORIGINAL'].duplicated().any():raise RuntimeError('Claves duplicadas en exportación')
    out=TRABAJO/'clientes_geolocalizacion_final_5dias.csv'
    df.to_csv(out,sep=';',index=False,encoding='utf-8-sig')
    estados=df['FUENTE_GEO_5DIAS'].value_counts(dropna=False)
    print('SALIDA',out,'| registros:',len(df))
    for k,v in estados.items():print(k,format(v,','))
    print('FUENTE_GEO_5DIAS=NOMINATIM_LOCAL/GOOGLE_MAPS es referencia NO predial')
    print('APTO_DISTANCIA_A_OFICINA=0 siempre para coordenadas no verificadas')


def main():
    parser=argparse.ArgumentParser(description='Geolocalización eficiente con respaldo INEI y presupuesto 5 días')
    parser.add_argument('--modo',required=True,choices=['plan','ejecutar','estado','exportar'])
    parser.add_argument('--equipo',type=int,choices=[1,2],default=1)
    parser.add_argument('--equipos',type=int,choices=[1,2],default=1)
    parser.add_argument('--horas',type=float,default=110,help='Tiempo máximo en HORAS desde el inicio de esta ejecución')
    parser.add_argument('--max-trabajos',type=int,default=0,help='0 = sin límite; usar 100 para piloto')
    parser.add_argument('--prioridad-minima',type=int,default=80,help='80: vía+zona, mz+zona y vía+número; 50: incluye zona sola')
    parser.add_argument('--consultas',type=int,choices=[1,2],default=1,help='Máximo consultas Google por dirección. Por defecto UNA')
    parser.add_argument('--fichas',type=int,choices=[1,2],default=1,help='Máximo fichas de lista por búsqueda. Por defecto UNA')
    parser.add_argument('--pausa',type=float,default=0.5,help='Pausa entre direcciones Google; no reducir si hay bloqueos')
    parser.add_argument('--nominatim-local',type=str,default='http://127.0.0.1:8080',help='Servidor local propio, o cadena vacía para desactivar')
    parser.add_argument('--timeout-local',type=float,default=2.5)
    parser.add_argument('--sin-google',action='store_true',help='Solo Nominatim local/INEI, sin navegador')
    parser.add_argument('--reintentar',action='store_true')
    args=parser.parse_args()
    if args.modo=='plan':crear_plan(cargar_robot())
    elif args.modo=='ejecutar':ejecutar(args)
    elif args.modo=='estado':estado()
    else:exportar()

if __name__=='__main__':main()
