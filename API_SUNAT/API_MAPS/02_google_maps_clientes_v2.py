"""
02_GOOGLE_MAPS_DOS_LAPTOPS_V2.PY
==============================

Google Maps Web + Playwright, SIN API de pago.

V2: endurece MZ/LT, evita negocios que imitan nombres de calles y solo
cachea/corta exactos con contexto suficiente.

Pensado para procesar una base masiva en 2 laptops:

    Laptop 1: FILA_ID 1 -> mitad, ASC
    Laptop 2: FILA_ID N -> mitad+1, DESC

El progreso definitivo se guarda en SQLite DESPUES DE CADA DIRECCION.
Si se apaga la laptop, se cierra PowerShell o vuelve luego de un corte,
el mismo comando continúa por los FILA_ID que aún no existen en resultados.

NO evade CAPTCHA ni bloqueos de Google. Si aparecen, se detiene sin marcar
la fila actual como procesada.

Uso normal:
    python 02_google_maps_clientes_v2.py --laptop 1
    python 02_google_maps_clientes_v2.py --laptop 2

Prueba de 20:
    python 02_google_maps_clientes_v2.py --laptop 1 --max-filas 20

Estado:
    python 02_google_maps_clientes_v2.py --laptop 1 --estado

Exportar CSV en cualquier momento:
    python 02_google_maps_clientes_v2.py --laptop 1 --export-only
"""

from pathlib import Path
from difflib import SequenceMatcher
from urllib.parse import quote_plus, unquote_plus, urlsplit
from datetime import datetime
import argparse
import csv
import math
import os
import re
import sqlite3
import time
import unicodedata

import requests
from playwright.sync_api import sync_playwright


# ============================================================================
# CONFIGURACION
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "clientes_direcciones"

PREP_DB = OUT_DIR / "direcciones_preparadas_google_masivo.sqlite"
MANIFEST = OUT_DIR / "manifest_google_masivo.json"

MAX_VARIANTES = 18
MAX_CANDIDATOS_LISTA = 4

HEADLESS = False
PAGE_TIMEOUT_MS = 60000
ESPERA_INICIAL_MS = 2800
ESPERA_ESTABILIDAD_MS = 700
ESPERA_MAX_MS = 10000
PAUSA_ENTRE_RUC_SEG = 0.8
REINICIAR_NAVEGADOR_CADA = 200

# Si la red se cae, el robot espera y reintenta la MISMA fila.
ESPERA_SIN_INTERNET_SEG = 30
REINTENTOS_NAVEGACION_CON_INTERNET = 3

# Coordenadas del centro @lat,lon NO se usan para predio/via/zona.
# Solo se permiten como ultimo fallback del DISTRITO y quedan etiquetadas
# como referencia de confianza BAJA.
PERMITIR_MAP_CENTER_SOLO_DISTRITO = True

# Umbrales de corte temprano.
SCORE_CORTE_EXACTO = 82

# Limites amplios del Peru para descartar coordenadas imposibles.
PERU_LAT_MIN = -19.5
PERU_LAT_MAX = 1.5
PERU_LON_MIN = -83.5
PERU_LON_MAX = -67.0


# ============================================================================
# UTILIDADES DE TEXTO
# ============================================================================

def sin_tildes(texto):
    texto = "" if texto is None else str(texto)
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def norm(texto):
    texto = sin_tildes("" if texto is None else str(texto)).upper().strip()
    texto = re.sub(r"[^A-Z0-9\s]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


STOP = {
    "DE", "DEL", "LA", "LAS", "LOS", "EL", "Y", "EN",
    "AV", "AVENIDA", "AVDA", "CAL", "CALLE", "JR", "JIRON",
    "PASAJE", "PJE", "PSJ", "CARRETERA", "PROLONGACION",
    "ALAMEDA", "PARQUE", "PLAZA", "PERU",
    "MZA", "MANZANA", "LT", "LOTE", "NRO", "NUMERO",
}

NEGOCIO_STOP = {
    "SAC", "S", "A", "C", "EIRL", "E", "I", "R", "L", "SRL",
    "PERU", "DEL", "DE", "LA", "LOS", "LAS", "Y",
    "SERVICIO", "SERVICIOS", "GENERAL", "GENERALES",
    "EMPRESA", "CORPORACION", "GRUPO", "INVERSIONES",
    "MULTISERVICIOS", "COMERCIAL", "COMERCIO",
}


def tokens_utiles(texto, negocio=False):
    stop = NEGOCIO_STOP if negocio else STOP
    return [
        t for t in norm(texto).split()
        if len(t) >= 2 and t not in stop
    ]


def cobertura_tokens(objetivo, resultado, negocio=False):
    obj = tokens_utiles(objetivo, negocio=negocio)
    res = set(tokens_utiles(resultado, negocio=negocio))
    return sum(t in res for t in obj) / len(obj) if obj else 0.0


def similitud(a, b, negocio=False):
    a_n = norm(a)
    b_n = norm(b)
    if not a_n or not b_n:
        return 0.0
    return max(
        SequenceMatcher(None, a_n, b_n).ratio(),
        cobertura_tokens(a, b, negocio=negocio),
        cobertura_tokens(b, a, negocio=negocio),
    )


def sin_tipo_via(texto):
    t = norm(texto)
    return re.sub(
        r"^(AVENIDA|AV|CALLE|CAL|JIRON|JR|PASAJE|PJE|PSJ|CARRETERA|"
        r"PROLONGACION|ALAMEDA|MALECON|CAMINO|PASEO|PARQUE|PLAZA)\s+",
        "",
        t,
    ).strip()


def nombre_parece_via(nombre, via):
    """Evita que 'Hotel Asturias' valide 'Jirón Asturias', pero acepta
    nombres de vía como 'Huallanca' o 'Av. Túpac Amaru 6224'.
    """
    n = norm(nombre)
    v = norm(sin_tipo_via(via))
    if not n or not v:
        return False

    if re.match(
        r"^(AVENIDA|AV|CALLE|CAL|JIRON|JR|PASAJE|PJE|PSJ|CARRETERA|"
        r"PROLONGACION|ALAMEDA|MALECON|CAMINO|PASEO|PARQUE|PLAZA)\b",
        n,
    ):
        return True

    nt = [x for x in tokens_utiles(n) if not x.isdigit()]
    vt = [x for x in tokens_utiles(v) if not x.isdigit()]
    if not nt or not vt:
        return False

    # El nombre Google sin prefijo debe estar compuesto por tokens de la vía;
    # no se admiten palabras extra como HOTEL, RESTAURANTE, SERVICIOS, etc.
    return set(nt).issubset(set(vt)) and len(set(nt) & set(vt)) / len(set(vt)) >= 0.75


def _similitud_via_texto(objetivo, texto):
    """Compara la vía de forma DIRECCIONAL.

    La función genérica `similitud()` es deliberadamente flexible, pero para
    calles era demasiado permisiva: "Hotel Asturias" podía terminar validando
    "Jirón Asturias". Aquí exigimos que los tokens de la vía buscada aparezcan
    realmente en el texto candidato.
    """
    obj = tokens_utiles(sin_tipo_via(objetivo))
    res = set(tokens_utiles(sin_tipo_via(texto)))
    if not obj or not res:
        return 0.0

    cobertura = sum(t in res for t in obj) / len(obj)
    seq = SequenceMatcher(
        None,
        norm(sin_tipo_via(objetivo)),
        norm(sin_tipo_via(texto)),
    ).ratio()

    # La cobertura manda; SequenceMatcher solo ayuda cuando la grafía cambia
    # ligeramente, nunca para que una palabra de negocio valide una calle.
    return max(cobertura, seq if cobertura >= 0.50 else 0.0)


def similitud_via(via, nombre, direccion, direccion_url, query_tipo):
    if not norm(via):
        return 0.0

    objetivo = sin_tipo_via(via)
    textos = []

    # La dirección visible de Google es la evidencia de calle más confiable.
    if norm(direccion):
        textos.append(direccion)

    # El segmento /place/ muchas veces es un nombre de negocio. Solo usarlo
    # como evidencia vial cuando él mismo parece una vía.
    if norm(direccion_url) and nombre_parece_via(direccion_url, via):
        textos.append(direccion_url)

    # Aceptar nombres como "Huallanca" o "Av. Túpac Amaru 6224", pero no
    # "Hotel Asturias" / "Servicios Generales ...".
    if query_tipo in {
        "ORIGINAL", "DIRECCION_LIMPIA", "VIA_NUMERO", "VIA_NUMERO_ZONA",
        "VIA_MZA_LOTE_ZONA", "CORREGIDA_VIA_MZA_LOTE_ZONA",
        "CORREGIDA_VIA_NUMERO_ZONA", "VIA_ZONA", "VIA_REFERENCIA", "SOLO_VIA",
    } and nombre_parece_via(nombre, via):
        textos.append(nombre)

    return max(
        (_similitud_via_texto(objetivo, t) for t in textos if norm(t)),
        default=0.0,
    )


def numero_en_texto(numero, texto):
    if not numero:
        return False
    numero_n = norm(numero).replace(" ", "")
    return bool(
        re.search(
            rf"(?<![A-Z0-9]){re.escape(numero_n)}(?![A-Z0-9])",
            norm(texto),
        )
    )


def extraer_manzanas(texto):
    t = norm(texto)
    return {
        m.group(1).strip(" .'-")
        for m in re.finditer(r"\b(?:MZ|MZA|MANZANA)\s*([A-Z0-9\-]+)", t)
    }


def extraer_lotes(texto):
    t = norm(texto)
    return {
        m.group(1).strip(" .'-")
        for m in re.finditer(r"\b(?:LT|LOTE)\s*([A-Z0-9\-]+)", t)
    }


def coords_validas(lat, lon):
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return False

    return (
        PERU_LAT_MIN <= lat <= PERU_LAT_MAX
        and PERU_LON_MIN <= lon <= PERU_LON_MAX
    )


def extraer_coords_ficha(url):
    """Coordenada del objeto !3dLAT!4dLON. No usa el viewport @lat,lon."""
    if not url:
        return None, None, ""

    m = re.search(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", url)
    if m and coords_validas(m.group(1), m.group(2)):
        return float(m.group(1)), float(m.group(2)), "FICHA_URL_3D4D"

    return None, None, ""


def extraer_coords_center(url):
    """Solo se llama para fallback DISTRITO."""
    if not url:
        return None, None

    m = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", url)
    if m and coords_validas(m.group(1), m.group(2)):
        return float(m.group(1)), float(m.group(2))

    return None, None


def direccion_en_url(url):
    try:
        ruta = urlsplit(url).path
        m = re.search(r"/maps/place/([^/]+)", ruta)
        return unquote_plus(m.group(1)) if m else ""
    except Exception:
        return ""


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ============================================================================
# GOOGLE MAPS
# ============================================================================

class BloqueoGoogle(RuntimeError):
    pass


class ErrorNavegacion(RuntimeError):
    pass


def internet_disponible():
    try:
        r = requests.get(
            "https://www.google.com/generate_204",
            timeout=8,
        )
        return r.status_code in {200, 204}
    except Exception:
        return False


def texto_locator(locator):
    try:
        if locator.count() == 0:
            return ""
        loc = locator.first
        if not loc.is_visible():
            return ""
        texto = (loc.inner_text(timeout=1500) or "").strip()
        if not texto:
            texto = (loc.get_attribute("aria-label") or "").strip()
        return re.sub(r"^[\ue000-\uf8ff]\s*", "", texto).strip()
    except Exception:
        return ""


def detectar_bloqueo(page):
    try:
        body = page.locator("body").inner_text(timeout=2500).lower()
    except Exception:
        body = ""

    url = (page.url or "").lower()
    frases = (
        "tráfico inusual", "trafico inusual", "no eres un robot", "captcha",
        "unusual traffic", "verify you are human",
    )
    return "/sorry/" in url or any(x in body for x in frases)


def warning_no_precisa(page):
    try:
        body = norm(page.locator("body").inner_text(timeout=2000))
    except Exception:
        body = ""

    return int(
        "LA UBICACION QUE SE MUESTRA NO ES PRECISA" in body
        or "CONFIRMA O CORRIGE ESTA UBICACION" in body
    )


def leer_ficha(page, permitir_center_distrito=False):
    url = page.url or ""

    nombre = texto_locator(page.locator("h1.DUwDvf"))
    if not nombre:
        nombre = texto_locator(page.locator("h1"))
        if norm(nombre) in {"RESULTADOS", "GOOGLE MAPS"}:
            nombre = ""

    direccion = ""
    for selector in (
        "button[data-item-id='address']",
        "[data-item-id='address']",
        "button[aria-label^='Dirección']",
        "button[aria-label^='Direccion']",
    ):
        direccion = texto_locator(page.locator(selector))
        if direccion:
            direccion = re.sub(
                r"^(Dirección|Direccion)\s*:\s*", "", direccion, flags=re.I
            )
            break

    lat, lon, fuente = extraer_coords_ficha(url)

    if lat is None and permitir_center_distrito and PERMITIR_MAP_CENTER_SOLO_DISTRITO:
        lat, lon = extraer_coords_center(url)
        if lat is not None:
            fuente = "MAP_CENTER_SOLO_DISTRITO"

    return {
        "nombre": nombre,
        "direccion": direccion,
        "direccion_url": direccion_en_url(url),
        "lat": lat,
        "lon": lon,
        "fuente": fuente,
        "url": url,
        "warning_no_precisa": warning_no_precisa(page),
    }


def esperar_estable(page):
    page.wait_for_timeout(ESPERA_INICIAL_MS)
    limite = time.monotonic() + max(0, ESPERA_MAX_MS - ESPERA_INICIAL_MS) / 1000
    anterior = None
    iguales = 0

    while time.monotonic() < limite:
        if detectar_bloqueo(page):
            raise BloqueoGoogle("Google Maps mostró CAPTCHA/bloqueo")

        ficha = leer_ficha(page)
        firma = (page.url, ficha["nombre"], ficha["direccion"])

        if firma == anterior and any(firma):
            iguales += 1
        else:
            iguales = 0

        if iguales >= 2:
            break

        anterior = firma
        page.wait_for_timeout(ESPERA_ESTABILIDAD_MS)


def goto_con_recuperacion(page, url):
    intentos_con_red = 0

    while True:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            if detectar_bloqueo(page):
                raise BloqueoGoogle("Google Maps mostró CAPTCHA/bloqueo")
            return

        except BloqueoGoogle:
            raise

        except Exception as e:
            if not internet_disponible():
                print(
                    f"      [RED] Sin Internet. Esperando {ESPERA_SIN_INTERNET_SEG}s. "
                    "La fila actual NO se pierde..."
                )
                time.sleep(ESPERA_SIN_INTERNET_SEG)
                continue

            intentos_con_red += 1
            if intentos_con_red >= REINTENTOS_NAVEGACION_CON_INTERNET:
                raise ErrorNavegacion(repr(e)) from e

            espera = 5 * intentos_con_red
            print(f"      [MAPS] Error de navegación. Reintento {intentos_con_red} en {espera}s...")
            time.sleep(espera)


def urls_resultados_lista(page):
    urls = []
    vistos = set()

    try:
        feed = page.locator("div[role='feed']")
        if not (feed.count() and feed.first.is_visible()):
            return []

        links = feed.first.locator("a[href*='/maps/place/']")
        count = min(links.count(), 30)

        for i in range(count):
            href = links.nth(i).get_attribute("href") or ""
            if "/maps/place/" not in href:
                continue
            if href.startswith("/"):
                href = "https://www.google.com" + href
            # Quitar duplicados de tarjetas que repiten el mismo href.
            key = href.split("&")[0]
            if key in vistos:
                continue
            vistos.add(key)
            urls.append(href)
            if len(urls) >= MAX_CANDIDATOS_LISTA:
                break

    except Exception:
        pass

    return urls


def buscar_google(context, page, query, permitir_center_distrito=False):
    """Devuelve 0..N candidatos detallados para una consulta."""
    search_url = (
        "https://www.google.com/maps/search/?api=1&hl=es&query="
        + quote_plus(query)
    )

    goto_con_recuperacion(page, search_url)
    esperar_estable(page)

    if detectar_bloqueo(page):
        raise BloqueoGoogle("Google Maps mostró CAPTCHA/bloqueo")

    # Si Google ya abrió una ficha única.
    if "/maps/place/" in (page.url or ""):
        return [leer_ficha(page, permitir_center_distrito=permitir_center_distrito)]

    links = urls_resultados_lista(page)

    # Lista de resultados: no escoger ciegamente uno. Abrimos hasta 4 fichas
    # y el scoring decidirá cuál tiene mayor coherencia.
    if links:
        resultados = []
        detail = context.new_page()
        detail.set_default_timeout(PAGE_TIMEOUT_MS)

        try:
            for href in links:
                goto_con_recuperacion(detail, href)
                detail.wait_for_timeout(1600)
                if detectar_bloqueo(detail):
                    raise BloqueoGoogle("Google Maps mostró CAPTCHA/bloqueo")
                resultados.append(
                    leer_ficha(detail, permitir_center_distrito=permitir_center_distrito)
                )
        finally:
            try:
                detail.close()
            except Exception:
                pass

        return resultados

    # No hay ficha ni lista. Para distrito sí podemos usar el centro del mapa,
    # explícitamente marcado como aproximación administrativa.
    if permitir_center_distrito:
        ficha = leer_ficha(page, permitir_center_distrito=True)
        if ficha["lat"] is not None:
            if not ficha["nombre"]:
                ficha["nombre"] = query
            return [ficha]

    return []


# ============================================================================
# SCORING / CLASIFICACION
# ============================================================================

def evaluar_candidato(row, candidato, query_tipo, query):
    nombre = candidato.get("nombre", "")
    direccion = candidato.get("direccion", "")
    direccion_url = candidato.get("direccion_url", "")
    texto = " ".join([nombre, direccion, direccion_url])

    via = row.get("VIA_CORREGIDA", "") or row.get("VIA_ORIGINAL", "")
    numero = row.get("NUMERO_BUSCADO", "")
    manzana = row.get("MANZANA", "")
    lote = row.get("LOTE", "")
    zona = row.get("ZONA_CORREGIDA", "") or row.get("ZONA_ORIGINAL", "")
    referencia = row.get("REFERENCIA", "")
    distrito = row.get("Distrito", "")
    provincia = row.get("Provincia", "")
    departamento = row.get("Departamento", "")
    razon = row.get("Nombre ó RazonSocial", "")

    via_sim = similitud_via(via, nombre, direccion, direccion_url, query_tipo)
    distrito_cov = cobertura_tokens(distrito, texto)
    provincia_cov = cobertura_tokens(provincia, texto)
    departamento_cov = cobertura_tokens(departamento, texto)
    zona_sim = similitud(zona, texto) if zona else 0.0
    referencia_sim = similitud(referencia, texto) if referencia else 0.0
    negocio_sim = similitud(razon, nombre, negocio=True) if razon and nombre else 0.0

    numero_ok = numero_en_texto(numero, texto) if numero else False

    mz_values = {norm(x) for x in extraer_manzanas(texto)}
    lote_values = {norm(x) for x in extraer_lotes(texto)}
    mz_ok = bool(manzana and norm(manzana) in mz_values)
    lote_ok = bool(lote and norm(lote) in lote_values)
    mz_contradice = bool(manzana and mz_values and not mz_ok)
    lote_contradice = bool(lote and lote_values and not lote_ok)

    coords_ok = candidato.get("lat") is not None and candidato.get("lon") is not None
    warning = bool(candidato.get("warning_no_precisa"))

    score = 0.0
    if coords_ok:
        score += 15

    # Texto de distrito no siempre aparece en Google; suma si está, pero no
    # rechazamos automáticamente porque la validación fuerte vendrá con OSM.
    if distrito_cov >= 0.65:
        score += 18
    elif provincia_cov >= 0.65 or departamento_cov >= 0.65:
        score += 5

    if via:
        score += min(35, 35 * via_sim)

    if numero:
        if numero_ok:
            score += 35
        elif via_sim >= 0.65:
            # Penalizar otro número visible en una vía comparable.
            nums = re.findall(r"(?<!\d)\d{1,5}[A-Z]?(?!\d)", norm(texto))
            if nums:
                score -= 25

    if manzana:
        score += 20 if mz_ok else (-35 if mz_contradice else 0)

    if lote:
        score += 20 if lote_ok else (-35 if lote_contradice else 0)

    if zona:
        score += min(15, 15 * zona_sim)

    if referencia:
        score += min(10, 10 * referencia_sim)

    # Razón social ayuda, pero nunca puede convertir por sí sola un negocio
    # genérico en dirección exacta.
    if query_tipo == "RAZON_SOCIAL" and negocio_sim >= 0.88:
        distintivos = tokens_utiles(razon, negocio=True)
        if len(distintivos) >= 2 or any(len(t) >= 7 for t in distintivos):
            score += 18

    if warning:
        score -= 20

    # --------------------------------------------------------
    # Clasificación jerárquica
    # --------------------------------------------------------
    tipo_ubicacion = "SIN_UBICACION"
    nivel = "SIN_MATCH"
    confianza = "SIN_MATCH"
    prioridad = 0

    if query_tipo == "DISTRITO_FALLBACK" and coords_ok:
        tipo_ubicacion = "UBICACION_REFERENCIA"
        nivel = "DISTRITO"
        confianza = "BAJA"
        prioridad = 10

    elif (
        coords_ok and numero and numero_ok and via_sim >= 0.75
        and not warning and not mz_contradice and not lote_contradice
    ):
        tipo_ubicacion = "UBICACION_EXACTA"
        nivel = "PREDIAL"
        confianza = "ALTA"
        prioridad = 100

    elif (
        coords_ok and manzana and lote and mz_ok and lote_ok
        and not warning
        # Un MZ/LT puede repetirse incluso dentro del mismo distrito. Para
        # llamarlo EXACTO exigimos evidencia administrativa y, si la fuente
        # trae vía/zona, coherencia contextual adicional.
        and distrito_cov >= 0.65
        and (not via or via_sim >= 0.65)
        and (not zona or zona_sim >= 0.45)
    ):
        tipo_ubicacion = "UBICACION_EXACTA"
        nivel = "MZ_LOTE"
        confianza = "ALTA"
        prioridad = 98

    elif (
        coords_ok and manzana and lote and mz_ok and lote_ok
        and not warning
    ):
        # Guardamos la coordenada/candidato, pero NO la presentamos como
        # predio exacto hasta que OSM/Nominatim valide ubigeo y contexto.
        tipo_ubicacion = "UBICACION_REFERENCIA"
        nivel = "MZ_LOTE_PARA_VALIDAR"
        confianza = "MEDIA"
        prioridad = 52

    elif (
        coords_ok and numero and numero_ok and via_sim >= 0.72 and warning
    ):
        tipo_ubicacion = "UBICACION_REFERENCIA"
        nivel = "PREDIAL_GOOGLE_IMPRECISO"
        confianza = "MEDIA_ALTA"
        prioridad = 90

    elif coords_ok and via_sim >= 0.72 and zona_sim >= 0.55:
        tipo_ubicacion = "UBICACION_REFERENCIA"
        nivel = "VIA_ZONA"
        confianza = "MEDIA_ALTA"
        prioridad = 75

    elif coords_ok and via_sim >= 0.72:
        tipo_ubicacion = "UBICACION_REFERENCIA"
        nivel = "VIA"
        confianza = "MEDIA"
        prioridad = 65

    elif coords_ok and zona_sim >= 0.62:
        tipo_ubicacion = "UBICACION_REFERENCIA"
        nivel = "ZONA"
        confianza = "MEDIA"
        prioridad = 55

    elif coords_ok and referencia_sim >= 0.68:
        tipo_ubicacion = "UBICACION_REFERENCIA"
        nivel = "REFERENCIA_POI"
        confianza = "MEDIA_BAJA"
        prioridad = 48

    elif coords_ok and query_tipo == "RAZON_SOCIAL" and negocio_sim >= 0.90:
        tipo_ubicacion = "UBICACION_REFERENCIA"
        nivel = "NEGOCIO"
        confianza = "MEDIA_BAJA"
        prioridad = 42

    return {
        **candidato,
        "query": query,
        "query_tipo": query_tipo,
        "score": round(score, 2),
        "tipo_ubicacion": tipo_ubicacion,
        "nivel": nivel,
        "confianza": confianza,
        "prioridad": prioridad,
        "via_sim": round(via_sim, 3),
        "distrito_cov": round(distrito_cov, 3),
        "provincia_cov": round(provincia_cov, 3),
        "departamento_cov": round(departamento_cov, 3),
        "zona_sim": round(zona_sim, 3),
        "referencia_sim": round(referencia_sim, 3),
        "negocio_sim": round(negocio_sim, 3),
        "numero_ok": int(numero_ok),
        "mz_ok": int(mz_ok),
        "lote_ok": int(lote_ok),
        "mz_contradice": int(mz_contradice),
        "lote_contradice": int(lote_contradice),
    }


def mejor_candidato(candidatos):
    validos = [
        c for c in candidatos
        if int(c.get("prioridad", 0)) > 0
        and c.get("lat") is not None
        and c.get("lon") is not None
    ]

    if not validos:
        return None

    return max(
        validos,
        key=lambda c: (
            int(c.get("prioridad", 0)),
            float(c.get("score") or -999),
        ),
    )


# ============================================================================
# SQLITE RESULTADOS / CACHE
# ============================================================================

def conectar_resultados(path):
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # FULL: preferimos durabilidad sobre unos milisegundos por fila.
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=60000")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS resultados(
            FILA_ID INTEGER PRIMARY KEY,
            BLOQUE_LAPTOP INTEGER,
            NumeroRuc TEXT,
            DIRECCION_KEY TEXT,
            TIPO_DIRECCION TEXT,
            CALIDAD_INICIAL TEXT,
            DIRECCION_ORIGINAL TEXT,
            Departamento TEXT,
            Provincia TEXT,
            Distrito TEXT,
            VIA TEXT,
            NUMERO TEXT,
            MANZANA TEXT,
            LOTE TEXT,
            ZONA TEXT,
            REFERENCIA TEXT,

            ESTADO TEXT,
            TIPO_UBICACION_GOOGLE TEXT,
            NIVEL_UBICACION_GOOGLE TEXT,
            CONFIANZA_GOOGLE TEXT,
            SCORE_GOOGLE REAL,
            LATITUD REAL,
            LONGITUD REAL,
            FUENTE_COORDENADAS TEXT,

            QUERY_USADA TEXT,
            QUERY_TIPO TEXT,
            INTENTOS INTEGER,
            CANDIDATOS_EVALUADOS INTEGER,

            NOMBRE_GOOGLE TEXT,
            DIRECCION_GOOGLE TEXT,
            URL_GOOGLE TEXT,
            WARNING_NO_PRECISA INTEGER,

            VIA_SIM REAL,
            DISTRITO_COV REAL,
            ZONA_SIM REAL,
            REFERENCIA_SIM REAL,
            NEGOCIO_SIM REAL,
            FLAG_NUMERO_OK INTEGER,
            FLAG_MZ_OK INTEGER,
            FLAG_LOTE_OK INTEGER,

            MOTIVO TEXT,
            ERROR TEXT,
            PROCESADO_EN TEXT
        );

        CREATE TABLE IF NOT EXISTS candidatos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            FILA_ID INTEGER,
            NumeroRuc TEXT,
            QUERY_TIPO TEXT,
            QUERY TEXT,
            SCORE REAL,
            TIPO_UBICACION TEXT,
            NIVEL TEXT,
            CONFIANZA TEXT,
            LATITUD REAL,
            LONGITUD REAL,
            FUENTE_COORDENADAS TEXT,
            NOMBRE_GOOGLE TEXT,
            DIRECCION_GOOGLE TEXT,
            URL_GOOGLE TEXT,
            WARNING_NO_PRECISA INTEGER,
            VIA_SIM REAL,
            DISTRITO_COV REAL,
            ZONA_SIM REAL,
            REFERENCIA_SIM REAL,
            NEGOCIO_SIM REAL,
            FLAG_NUMERO_OK INTEGER,
            FLAG_MZ_OK INTEGER,
            FLAG_LOTE_OK INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_candidatos_fila
        ON candidatos(FILA_ID);

        CREATE TABLE IF NOT EXISTS cache_exactos(
            DIRECCION_KEY TEXT PRIMARY KEY,
            TIPO_UBICACION_GOOGLE TEXT,
            NIVEL_UBICACION_GOOGLE TEXT,
            CONFIANZA_GOOGLE TEXT,
            SCORE_GOOGLE REAL,
            LATITUD REAL,
            LONGITUD REAL,
            FUENTE_COORDENADAS TEXT,
            QUERY_USADA TEXT,
            QUERY_TIPO TEXT,
            NOMBRE_GOOGLE TEXT,
            DIRECCION_GOOGLE TEXT,
            URL_GOOGLE TEXT,
            WARNING_NO_PRECISA INTEGER,
            VIA_SIM REAL,
            DISTRITO_COV REAL,
            ZONA_SIM REAL,
            REFERENCIA_SIM REAL,
            NEGOCIO_SIM REAL,
            FLAG_NUMERO_OK INTEGER,
            FLAG_MZ_OK INTEGER,
            FLAG_LOTE_OK INTEGER,
            GUARDADO_EN TEXT
        );
        """
    )
    conn.commit()
    return conn


def conectar_preparadas():
    if not PREP_DB.exists():
        raise FileNotFoundError(
            f"No existe {PREP_DB}. Ejecuta primero 01_preparar_direcciones_clientes.py"
        )

    conn = sqlite3.connect(PREP_DB, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def resultado_ya_procesado(conn, fila_id, retry_errors=False):
    row = conn.execute(
        "SELECT ESTADO FROM resultados WHERE FILA_ID=?",
        (fila_id,),
    ).fetchone()

    if not row:
        return False

    if retry_errors and row["ESTADO"] == "ERROR_TECNICO":
        return False

    return True


def cargar_cache(conn, key):
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM cache_exactos WHERE DIRECCION_KEY=?",
        (key,),
    ).fetchone()
    return dict(row) if row else None


def guardar_cache_exacto(conn, key, best):
    if not key or not exacto_apto_corte_cache(best):
        return

    conn.execute(
        """
        INSERT INTO cache_exactos(
            DIRECCION_KEY, TIPO_UBICACION_GOOGLE, NIVEL_UBICACION_GOOGLE,
            CONFIANZA_GOOGLE, SCORE_GOOGLE, LATITUD, LONGITUD,
            FUENTE_COORDENADAS, QUERY_USADA, QUERY_TIPO,
            NOMBRE_GOOGLE, DIRECCION_GOOGLE, URL_GOOGLE,
            WARNING_NO_PRECISA, VIA_SIM, DISTRITO_COV, ZONA_SIM,
            REFERENCIA_SIM, NEGOCIO_SIM,
            FLAG_NUMERO_OK, FLAG_MZ_OK, FLAG_LOTE_OK, GUARDADO_EN
        ) VALUES (
            :key, :tipo, :nivel, :confianza, :score, :lat, :lon,
            :fuente, :query, :qtipo, :nombre, :direccion, :url,
            :warning, :via_sim, :distrito_cov, :zona_sim,
            :referencia_sim, :negocio_sim,
            :numero_ok, :mz_ok, :lote_ok, :guardado
        )
        ON CONFLICT(DIRECCION_KEY) DO UPDATE SET
            TIPO_UBICACION_GOOGLE=excluded.TIPO_UBICACION_GOOGLE,
            NIVEL_UBICACION_GOOGLE=excluded.NIVEL_UBICACION_GOOGLE,
            CONFIANZA_GOOGLE=excluded.CONFIANZA_GOOGLE,
            SCORE_GOOGLE=excluded.SCORE_GOOGLE,
            LATITUD=excluded.LATITUD,
            LONGITUD=excluded.LONGITUD,
            FUENTE_COORDENADAS=excluded.FUENTE_COORDENADAS,
            QUERY_USADA=excluded.QUERY_USADA,
            QUERY_TIPO=excluded.QUERY_TIPO,
            NOMBRE_GOOGLE=excluded.NOMBRE_GOOGLE,
            DIRECCION_GOOGLE=excluded.DIRECCION_GOOGLE,
            URL_GOOGLE=excluded.URL_GOOGLE,
            GUARDADO_EN=excluded.GUARDADO_EN
        """,
        {
            "key": key,
            "tipo": best.get("tipo_ubicacion", ""),
            "nivel": best.get("nivel", ""),
            "confianza": best.get("confianza", ""),
            "score": best.get("score"),
            "lat": best.get("lat"),
            "lon": best.get("lon"),
            "fuente": best.get("fuente", ""),
            "query": best.get("query", ""),
            "qtipo": best.get("query_tipo", ""),
            "nombre": best.get("nombre", ""),
            "direccion": best.get("direccion", ""),
            "url": best.get("url", ""),
            "warning": int(bool(best.get("warning_no_precisa"))),
            "via_sim": best.get("via_sim"),
            "distrito_cov": best.get("distrito_cov"),
            "zona_sim": best.get("zona_sim"),
            "referencia_sim": best.get("referencia_sim"),
            "negocio_sim": best.get("negocio_sim"),
            "numero_ok": int(bool(best.get("numero_ok"))),
            "mz_ok": int(bool(best.get("mz_ok"))),
            "lote_ok": int(bool(best.get("lote_ok"))),
            "guardado": now_iso(),
        },
    )


def guardar_candidatos(conn, row, candidatos, exacto):
    conn.execute("DELETE FROM candidatos WHERE FILA_ID=?", (row["FILA_ID"],))

    ordenados = sorted(
        candidatos,
        key=lambda c: (
            int(c.get("prioridad", 0)),
            float(c.get("score") or -999),
        ),
        reverse=True,
    )

    # Exactos: solo top 2. Difíciles: top 12 para poder mejorar después
    # sin volver a Google.
    conservar = ordenados[:2] if exacto else ordenados[:12]

    for c in conservar:
        conn.execute(
            """
            INSERT INTO candidatos(
                FILA_ID, NumeroRuc, QUERY_TIPO, QUERY, SCORE,
                TIPO_UBICACION, NIVEL, CONFIANZA,
                LATITUD, LONGITUD, FUENTE_COORDENADAS,
                NOMBRE_GOOGLE, DIRECCION_GOOGLE, URL_GOOGLE,
                WARNING_NO_PRECISA, VIA_SIM, DISTRITO_COV,
                ZONA_SIM, REFERENCIA_SIM, NEGOCIO_SIM,
                FLAG_NUMERO_OK, FLAG_MZ_OK, FLAG_LOTE_OK
            ) VALUES (
                :fila, :ruc, :qtipo, :query, :score,
                :tipo, :nivel, :confianza,
                :lat, :lon, :fuente,
                :nombre, :direccion, :url,
                :warning, :via_sim, :distrito_cov,
                :zona_sim, :referencia_sim, :negocio_sim,
                :numero_ok, :mz_ok, :lote_ok
            )
            """,
            {
                "fila": row["FILA_ID"],
                "ruc": row["NumeroRuc"],
                "qtipo": c.get("query_tipo", ""),
                "query": c.get("query", ""),
                "score": c.get("score"),
                "tipo": c.get("tipo_ubicacion", ""),
                "nivel": c.get("nivel", ""),
                "confianza": c.get("confianza", ""),
                "lat": c.get("lat"),
                "lon": c.get("lon"),
                "fuente": c.get("fuente", ""),
                "nombre": c.get("nombre", ""),
                "direccion": c.get("direccion", ""),
                "url": c.get("url", ""),
                "warning": int(bool(c.get("warning_no_precisa"))),
                "via_sim": c.get("via_sim"),
                "distrito_cov": c.get("distrito_cov"),
                "zona_sim": c.get("zona_sim"),
                "referencia_sim": c.get("referencia_sim"),
                "negocio_sim": c.get("negocio_sim"),
                "numero_ok": int(bool(c.get("numero_ok"))),
                "mz_ok": int(bool(c.get("mz_ok"))),
                "lote_ok": int(bool(c.get("lote_ok"))),
            },
        )


def guardar_resultado(conn, row, best, intentos, candidatos_evaluados, estado, motivo, error=""):
    best = best or {}

    conn.execute(
        """
        INSERT INTO resultados(
            FILA_ID, BLOQUE_LAPTOP, NumeroRuc, DIRECCION_KEY,
            TIPO_DIRECCION, CALIDAD_INICIAL, DIRECCION_ORIGINAL,
            Departamento, Provincia, Distrito,
            VIA, NUMERO, MANZANA, LOTE, ZONA, REFERENCIA,
            ESTADO, TIPO_UBICACION_GOOGLE, NIVEL_UBICACION_GOOGLE,
            CONFIANZA_GOOGLE, SCORE_GOOGLE,
            LATITUD, LONGITUD, FUENTE_COORDENADAS,
            QUERY_USADA, QUERY_TIPO, INTENTOS, CANDIDATOS_EVALUADOS,
            NOMBRE_GOOGLE, DIRECCION_GOOGLE, URL_GOOGLE,
            WARNING_NO_PRECISA, VIA_SIM, DISTRITO_COV, ZONA_SIM,
            REFERENCIA_SIM, NEGOCIO_SIM,
            FLAG_NUMERO_OK, FLAG_MZ_OK, FLAG_LOTE_OK,
            MOTIVO, ERROR, PROCESADO_EN
        ) VALUES (
            :FILA_ID, :BLOQUE, :RUC, :KEY,
            :TIPO_DIR, :CALIDAD, :DIR_ORIG,
            :DEP, :PROV, :DIST,
            :VIA, :NUM, :MZ, :LT, :ZONA, :REF,
            :ESTADO, :TIPO_UBI, :NIVEL, :CONF, :SCORE,
            :LAT, :LON, :FUENTE,
            :QUERY, :QT, :INTENTOS, :CANDS,
            :NOMBRE, :DIR_GOOGLE, :URL,
            :WARN, :VIA_SIM, :DIST_COV, :ZONA_SIM,
            :REF_SIM, :NEG_SIM,
            :NUM_OK, :MZ_OK, :LT_OK,
            :MOTIVO, :ERROR, :FECHA
        )
        ON CONFLICT(FILA_ID) DO UPDATE SET
            ESTADO=excluded.ESTADO,
            TIPO_UBICACION_GOOGLE=excluded.TIPO_UBICACION_GOOGLE,
            NIVEL_UBICACION_GOOGLE=excluded.NIVEL_UBICACION_GOOGLE,
            CONFIANZA_GOOGLE=excluded.CONFIANZA_GOOGLE,
            SCORE_GOOGLE=excluded.SCORE_GOOGLE,
            LATITUD=excluded.LATITUD,
            LONGITUD=excluded.LONGITUD,
            FUENTE_COORDENADAS=excluded.FUENTE_COORDENADAS,
            QUERY_USADA=excluded.QUERY_USADA,
            QUERY_TIPO=excluded.QUERY_TIPO,
            INTENTOS=excluded.INTENTOS,
            CANDIDATOS_EVALUADOS=excluded.CANDIDATOS_EVALUADOS,
            NOMBRE_GOOGLE=excluded.NOMBRE_GOOGLE,
            DIRECCION_GOOGLE=excluded.DIRECCION_GOOGLE,
            URL_GOOGLE=excluded.URL_GOOGLE,
            WARNING_NO_PRECISA=excluded.WARNING_NO_PRECISA,
            VIA_SIM=excluded.VIA_SIM,
            DISTRITO_COV=excluded.DISTRITO_COV,
            ZONA_SIM=excluded.ZONA_SIM,
            REFERENCIA_SIM=excluded.REFERENCIA_SIM,
            NEGOCIO_SIM=excluded.NEGOCIO_SIM,
            FLAG_NUMERO_OK=excluded.FLAG_NUMERO_OK,
            FLAG_MZ_OK=excluded.FLAG_MZ_OK,
            FLAG_LOTE_OK=excluded.FLAG_LOTE_OK,
            MOTIVO=excluded.MOTIVO,
            ERROR=excluded.ERROR,
            PROCESADO_EN=excluded.PROCESADO_EN
        """,
        {
            "FILA_ID": row["FILA_ID"],
            "BLOQUE": row["BLOQUE_LAPTOP"],
            "RUC": row["NumeroRuc"],
            "KEY": row["DIRECCION_KEY"],
            "TIPO_DIR": row["TIPO_DIRECCION"],
            "CALIDAD": row["CALIDAD_INICIAL"],
            "DIR_ORIG": row["Direccion"],
            "DEP": row["Departamento"],
            "PROV": row["Provincia"],
            "DIST": row["Distrito"],
            "VIA": row["VIA_CORREGIDA"] or row["VIA_ORIGINAL"],
            "NUM": row["NUMERO_BUSCADO"],
            "MZ": row["MANZANA"],
            "LT": row["LOTE"],
            "ZONA": row["ZONA_CORREGIDA"] or row["ZONA_ORIGINAL"],
            "REF": row["REFERENCIA"],
            "ESTADO": estado,
            "TIPO_UBI": best.get("tipo_ubicacion", "SIN_UBICACION"),
            "NIVEL": best.get("nivel", "SIN_MATCH"),
            "CONF": best.get("confianza", "SIN_MATCH"),
            "SCORE": best.get("score"),
            "LAT": best.get("lat"),
            "LON": best.get("lon"),
            "FUENTE": best.get("fuente", ""),
            "QUERY": best.get("query", ""),
            "QT": best.get("query_tipo", ""),
            "INTENTOS": intentos,
            "CANDS": candidatos_evaluados,
            "NOMBRE": best.get("nombre", ""),
            "DIR_GOOGLE": best.get("direccion", ""),
            "URL": best.get("url", ""),
            "WARN": int(bool(best.get("warning_no_precisa"))),
            "VIA_SIM": best.get("via_sim"),
            "DIST_COV": best.get("distrito_cov"),
            "ZONA_SIM": best.get("zona_sim"),
            "REF_SIM": best.get("referencia_sim"),
            "NEG_SIM": best.get("negocio_sim"),
            "NUM_OK": int(bool(best.get("numero_ok"))),
            "MZ_OK": int(bool(best.get("mz_ok"))),
            "LT_OK": int(bool(best.get("lote_ok"))),
            "MOTIVO": motivo,
            "ERROR": error,
            "FECHA": now_iso(),
        },
    )


# ============================================================================
# PROCESAMIENTO DE UNA DIRECCION
# ============================================================================

def exacto_apto_corte_cache(best):
    """Solo corta consultas / entra al cache si el exacto es suficientemente
    contextual para no propagar falsos positivos a direcciones repetidas.
    """
    if not best or best.get("tipo_ubicacion") != "UBICACION_EXACTA":
        return False

    if best.get("warning_no_precisa"):
        return False

    nivel = str(best.get("nivel", ""))
    distrito_ok = float(best.get("distrito_cov") or 0) >= 0.65
    zona_ok = float(best.get("zona_sim") or 0) >= 0.55
    negocio_ok = float(best.get("negocio_sim") or 0) >= 0.92

    if nivel == "MZ_LOTE":
        # La propia clasificación MZ_LOTE V2 ya exige distrito + contexto.
        return distrito_ok

    if nivel == "PREDIAL":
        # Para cortar/cachar exigimos una señal contextual extra. Si no la
        # hay, el candidato se conserva pero seguimos probando variantes.
        return distrito_ok or zona_ok or negocio_ok

    return False


def query_distrito(row):
    partes = []
    for valor in (row.get("Distrito", ""), row.get("Provincia", ""), row.get("Departamento", "")):
        n = str(valor or "").strip()
        if n and norm(n) not in {norm(x) for x in partes}:
            partes.append(n)
    partes.append("PERU")
    return ", ".join(partes)


def procesar_direccion(context, page, row):
    candidatos = []
    intentos = 0

    # Cache exacta por dirección normalizada+ubigeo.
    # Se evalúa fuera de esta función porque vive en SQLite resultados.

    for i in range(1, MAX_VARIANTES + 1):
        query = str(row.get(f"Q{i}", "") or "").strip()
        query_tipo = str(row.get(f"Q{i}_TIPO", "") or "").strip()

        if not query:
            continue

        intentos += 1
        print(f"    Q{intentos:02d} [{query_tipo}] {query}")

        try:
            encontrados = buscar_google(context, page, query)
        except ErrorNavegacion as e:
            print(f"      [AVISO] Query con error de navegación: {e}")
            continue

        for bruto in encontrados:
            actual = evaluar_candidato(row, bruto, query_tipo, query)
            candidatos.append(actual)
            print(
                f"      -> {actual['nivel']} | score={actual['score']} "
                f"| via={actual['via_sim']} | num={actual['numero_ok']} "
                f"| coords={'SI' if actual['lat'] is not None else 'NO'} "
                f"| {actual['nombre']} {actual['direccion']}"
            )

        best = mejor_candidato(candidatos)
        if (
            best
            and float(best.get("score") or -999) >= SCORE_CORTE_EXACTO
            and exacto_apto_corte_cache(best)
        ):
            break

    best = mejor_candidato(candidatos)

    # --------------------------------------------------------
    # ULTIMO FALLBACK: DISTRITO
    # Solo si no tenemos ninguna coordenada utilizable.
    # --------------------------------------------------------
    if best is None or best.get("lat") is None:
        q_dist = query_distrito(row)
        intentos += 1
        print(f"    Q{intentos:02d} [DISTRITO_FALLBACK] {q_dist}")

        try:
            distritos = buscar_google(
                context,
                page,
                q_dist,
                permitir_center_distrito=True,
            )
        except ErrorNavegacion:
            distritos = []

        for bruto in distritos:
            actual = evaluar_candidato(row, bruto, "DISTRITO_FALLBACK", q_dist)
            candidatos.append(actual)

        best = mejor_candidato(candidatos)

    if best and best.get("lat") is not None:
        if best.get("tipo_ubicacion") == "UBICACION_EXACTA":
            estado = "ENCONTRADO_EXACTO"
            motivo = "Google encontró evidencia predial/MZ-lote con coordenadas de ficha"
        else:
            estado = "ENCONTRADO_REFERENCIA"
            motivo = (
                "No se confirmó el predio exacto; se conserva la mejor referencia "
                f"disponible ({best.get('nivel')})"
            )
    else:
        estado = "SIN_UBICACION"
        motivo = "Google no devolvió coordenadas útiles ni siquiera para el fallback del distrito"

    return best, candidatos, intentos, estado, motivo


# ============================================================================
# NAVEGADOR
# ============================================================================

def lanzar_navegador(p):
    try:
        browser = p.chromium.launch(
            channel="chrome",
            headless=HEADLESS,
            slow_mo=80,
        )
    except Exception:
        browser = p.chromium.launch(
            headless=HEADLESS,
            slow_mo=80,
        )

    context = browser.new_context(
        locale="es-PE",
        viewport={"width": 1450, "height": 900},
    )
    page = context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    return browser, context, page


def cerrar_navegador(browser, context):
    try:
        context.close()
    except Exception:
        pass
    try:
        browser.close()
    except Exception:
        pass


# ============================================================================
# EXPORT / ESTADO
# ============================================================================

def paths_laptop(laptop):
    return {
        "db": OUT_DIR / f"google_resultados_laptop{laptop}_v2.sqlite",
        "csv": OUT_DIR / f"google_resultados_laptop{laptop}_v2.csv",
        "candidatos_csv": OUT_DIR / f"google_candidatos_laptop{laptop}_v2.csv",
        "progreso": OUT_DIR / f"progreso_google_laptop{laptop}_v2.txt",
        "bloqueo": OUT_DIR / f"bloqueo_google_laptop{laptop}.txt",
    }


def exportar_db(conn, tabla, path):
    cur = conn.execute(f"SELECT * FROM {tabla} ORDER BY FILA_ID")
    columnas = [x[0] for x in cur.description]

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columnas)
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            writer.writerows(rows)

    print("Exportado:", path)


def mostrar_estado(conn_res, conn_prep, laptop):
    total = conn_prep.execute(
        "SELECT COUNT(*) FROM preparadas WHERE BLOQUE_LAPTOP=?",
        (laptop,),
    ).fetchone()[0]

    procesados = conn_res.execute("SELECT COUNT(*) FROM resultados").fetchone()[0]

    print("=" * 100)
    print(f"ESTADO GOOGLE - LAPTOP {laptop}")
    print("=" * 100)
    print(f"Asignadas   : {total:,}")
    print(f"Procesadas  : {procesados:,}")
    print(f"Pendientes  : {max(0, total - procesados):,}")
    print()

    for estado, cantidad in conn_res.execute(
        "SELECT ESTADO, COUNT(*) FROM resultados GROUP BY ESTADO ORDER BY COUNT(*) DESC"
    ).fetchall():
        print(f"{estado:28s} {cantidad:>10,}")

    print()
    for nivel, cantidad in conn_res.execute(
        """
        SELECT NIVEL_UBICACION_GOOGLE, COUNT(*)
        FROM resultados
        GROUP BY NIVEL_UBICACION_GOOGLE
        ORDER BY COUNT(*) DESC
        """
    ).fetchall():
        print(f"{str(nivel):28s} {cantidad:>10,}")

    print("=" * 100)


def escribir_progreso(path, laptop, fila_id, procesados_run, conn_res, rango_texto):
    total_guardado = conn_res.execute("SELECT COUNT(*) FROM resultados").fetchone()[0]
    exactos = conn_res.execute(
        "SELECT COUNT(*) FROM resultados WHERE ESTADO='ENCONTRADO_EXACTO'"
    ).fetchone()[0]
    referencias = conn_res.execute(
        "SELECT COUNT(*) FROM resultados WHERE ESTADO='ENCONTRADO_REFERENCIA'"
    ).fetchone()[0]

    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        "\n".join([
            f"LAPTOP={laptop}",
            f"RANGO={rango_texto}",
            f"ULTIMA_FILA_GUARDADA={fila_id}",
            f"PROCESADOS_ESTA_EJECUCION={procesados_run}",
            f"TOTAL_GUARDADO={total_guardado}",
            f"EXACTOS={exactos}",
            f"REFERENCIAS={referencias}",
            f"ACTUALIZADO={now_iso()}",
        ]) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--laptop", type=int, choices=[1, 2], required=True)
    parser.add_argument("--max-filas", type=int, default=0)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--estado", action="store_true")
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument("--export-candidatos", action="store_true")
    parser.add_argument("--desde", type=int, default=0, help="Rango manual opcional")
    parser.add_argument("--hasta", type=int, default=0, help="Rango manual opcional")
    parser.add_argument("--sentido", choices=["ASC", "DESC"], default="")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    conn_prep = conectar_preparadas()
    paths = paths_laptop(args.laptop)
    conn_res = conectar_resultados(paths["db"])

    if args.estado:
        mostrar_estado(conn_res, conn_prep, args.laptop)
        return

    if args.export_only:
        exportar_db(conn_res, "resultados", paths["csv"])
        if args.export_candidatos:
            exportar_db(conn_res, "candidatos", paths["candidatos_csv"])
        return

    total_global = conn_prep.execute("SELECT COUNT(*) FROM preparadas").fetchone()[0]
    corte = (total_global + 1) // 2

    if args.desde and args.hasta:
        lo = min(args.desde, args.hasta)
        hi = max(args.desde, args.hasta)
        sentido = args.sentido or ("ASC" if args.desde <= args.hasta else "DESC")
        sql = (
            "SELECT * FROM preparadas WHERE FILA_ID BETWEEN ? AND ? "
            f"ORDER BY FILA_ID {sentido}"
        )
        params = (lo, hi)
        rango_texto = f"{args.desde}->{args.hasta} {sentido}"
    else:
        sentido = "ASC" if args.laptop == 1 else "DESC"
        sql = (
            "SELECT * FROM preparadas WHERE BLOQUE_LAPTOP=? "
            f"ORDER BY FILA_ID {sentido}"
        )
        params = (args.laptop,)
        if args.laptop == 1:
            rango_texto = f"1->{corte} ASC"
        else:
            rango_texto = f"{total_global}->{corte + 1} DESC"

    print("=" * 110)
    print("GOOGLE MAPS WEB V2 - 2 LAPTOPS / PROGRESO RESUMIBLE")
    print("=" * 110)
    print(f"Laptop       : {args.laptop}")
    print(f"Total global : {total_global:,}")
    print(f"Rango        : {rango_texto}")
    print(f"Resultados   : {paths['db']}")
    print("Guardado     : COMMIT despues de CADA RUC")
    print("Red          : si se cae, espera y reintenta la misma fila")
    print("CAPTCHA      : se detiene; NO se evade")
    print("=" * 110)

    procesados_run = 0
    browser = context = page = None

    with sync_playwright() as p:
        browser, context, page = lanzar_navegador(p)

        try:
            cur = conn_prep.execute(sql, params)

            for raw in cur:
                row = dict(raw)
                fila_id = int(row["FILA_ID"])

                if resultado_ya_procesado(conn_res, fila_id, args.retry_errors):
                    continue

                if args.max_filas and procesados_run >= args.max_filas:
                    break

                print()
                print("-" * 110)
                print(
                    f"FILA {fila_id:,} | RUC {row['NumeroRuc']} | "
                    f"{row['TIPO_DIRECCION']} | {row['Direccion']}"
                )
                print("-" * 110)

                # --------------------------------------------------------
                # CACHE SOLO PARA EXACTOS YA VERIFICADOS POR ESTE ROBOT
                # --------------------------------------------------------
                cache = cargar_cache(conn_res, row.get("DIRECCION_KEY", ""))

                if cache:
                    best = {
                        "tipo_ubicacion": cache["TIPO_UBICACION_GOOGLE"],
                        "nivel": cache["NIVEL_UBICACION_GOOGLE"],
                        "confianza": cache["CONFIANZA_GOOGLE"],
                        "score": cache["SCORE_GOOGLE"],
                        "lat": cache["LATITUD"],
                        "lon": cache["LONGITUD"],
                        "fuente": "CACHE_EXACTA",
                        "query": cache["QUERY_USADA"],
                        "query_tipo": "CACHE_EXACTA",
                        "nombre": cache["NOMBRE_GOOGLE"],
                        "direccion": cache["DIRECCION_GOOGLE"],
                        "url": cache["URL_GOOGLE"],
                        "warning_no_precisa": cache["WARNING_NO_PRECISA"],
                        "via_sim": cache["VIA_SIM"],
                        "distrito_cov": cache["DISTRITO_COV"],
                        "zona_sim": cache["ZONA_SIM"],
                        "referencia_sim": cache["REFERENCIA_SIM"],
                        "negocio_sim": cache["NEGOCIO_SIM"],
                        "numero_ok": cache["FLAG_NUMERO_OK"],
                        "mz_ok": cache["FLAG_MZ_OK"],
                        "lote_ok": cache["FLAG_LOTE_OK"],
                    }
                    candidatos = []
                    intentos = 0
                    estado = "ENCONTRADO_EXACTO"
                    motivo = "Reutilizado desde cache exacta de la misma direccion+ubigeo"
                    print("    CACHE EXACTA ->", best["lat"], best["lon"])

                else:
                    try:
                        best, candidatos, intentos, estado, motivo = procesar_direccion(
                            context, page, row
                        )

                    except BloqueoGoogle as e:
                        paths["bloqueo"].write_text(
                            "\n".join([
                                "BLOQUEO_GOOGLE",
                                f"LAPTOP={args.laptop}",
                                f"FILA_ID={fila_id}",
                                f"RUC={row['NumeroRuc']}",
                                f"ERROR={e}",
                                f"FECHA={now_iso()}",
                            ]) + "\n",
                            encoding="utf-8",
                        )
                        print()
                        print("=" * 110)
                        print("GOOGLE MOSTRO CAPTCHA/BLOQUEO.")
                        print(f"Se detiene SIN guardar FILA_ID {fila_id:,} como procesada.")
                        print("Cuando Maps vuelva a funcionar normalmente, ejecuta el MISMO comando.")
                        print("Continuara desde esta fila pendiente.")
                        print("=" * 110)
                        return

                    except KeyboardInterrupt:
                        print("\nInterrumpido por usuario. Las filas anteriores ya estan guardadas.")
                        raise

                    except Exception as e:
                        # Error técnico: se guarda para no trabar todo el masivo,
                        # pero puede reintentarse con --retry-errors.
                        best = None
                        candidatos = []
                        intentos = 0
                        estado = "ERROR_TECNICO"
                        motivo = "Error técnico; puede reintentarse con --retry-errors"
                        error = repr(e)
                        guardar_resultado(
                            conn_res, row, best, intentos, 0,
                            estado, motivo, error=error
                        )
                        conn_res.commit()
                        procesados_run += 1
                        escribir_progreso(
                            paths["progreso"], args.laptop, fila_id,
                            procesados_run, conn_res, rango_texto
                        )
                        print("    ERROR_TECNICO:", error)
                        continue

                exacto = bool(best and best.get("tipo_ubicacion") == "UBICACION_EXACTA")

                guardar_resultado(
                    conn_res,
                    row,
                    best,
                    intentos,
                    len(candidatos),
                    estado,
                    motivo,
                )
                guardar_candidatos(conn_res, row, candidatos, exacto)
                guardar_cache_exacto(conn_res, row.get("DIRECCION_KEY", ""), best)

                # PUNTO CRITICO: una transacción por RUC.
                conn_res.commit()

                procesados_run += 1
                escribir_progreso(
                    paths["progreso"],
                    args.laptop,
                    fila_id,
                    procesados_run,
                    conn_res,
                    rango_texto,
                )

                print(
                    "    FINAL ->",
                    estado,
                    "|",
                    best.get("nivel", "SIN_MATCH") if best else "SIN_MATCH",
                    "|",
                    f"{best.get('lat')}, {best.get('lon')}" if best else "SIN COORDENADAS",
                )

                if (
                    REINICIAR_NAVEGADOR_CADA
                    and procesados_run % REINICIAR_NAVEGADOR_CADA == 0
                ):
                    print(f"    Reiniciando Chrome tras {procesados_run:,} RUC nuevos...")
                    cerrar_navegador(browser, context)
                    browser, context, page = lanzar_navegador(p)

                if PAUSA_ENTRE_RUC_SEG > 0:
                    time.sleep(PAUSA_ENTRE_RUC_SEG)

        finally:
            if browser is not None:
                cerrar_navegador(browser, context)

    print()
    print("=" * 110)
    print(f"FIN DE ESTA EJECUCION. Nuevas filas guardadas: {procesados_run:,}")
    print("SQLite:", paths["db"])
    print("Progreso:", paths["progreso"])
    print("Para continuar: ejecuta exactamente el mismo comando.")
    print("=" * 110)


if __name__ == "__main__":
    main()
