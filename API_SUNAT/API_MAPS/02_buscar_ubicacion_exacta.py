from pathlib import Path
from urllib.parse import quote_plus
import csv
import os
import re
import unicodedata

import pandas as pd
from playwright.sync_api import sync_playwright

import config


OUTPUT_COLUMNS = [
    "NumeroRuc", "UBICACION_ORIGINAL", "ESTADO_EXACTO",
    "TIPO_UBICACION", "NIVEL_UBICACION", "CONFIANZA",
    "LATITUD_EXACTA", "LONGITUD_EXACTA", "FUENTE_COORDENADAS",
    "QUERY_USADA", "QUERY_TIPO", "INTENTOS",
    "NOMBRE_GOOGLE", "DIRECCION_GOOGLE", "URL_GOOGLE",
    "ESTADO_UI_GOOGLE", "RAZON_DECISION", "ERROR",
]


def norm(text):
    text = "" if text is None else str(text)
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    ).upper()
    text = re.sub(r"[^A-Z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text):
    stop = {
        "DE", "DEL", "LA", "LAS", "LOS", "EL", "Y",
        "AV", "AVENIDA", "CALLE", "JR", "JIRON", "PERU",
    }
    return [x for x in norm(text).split() if len(x) >= 2 and x not in stop]


def coverage(target, result):
    a = tokens(target)
    b = set(tokens(result))
    if not a:
        return 0.0
    return sum(1 for x in a if x in b) / len(a)


def contains_number(number, text):
    if not number:
        return False
    n = re.escape(norm(number))
    return bool(re.search(rf"(?<![A-Z0-9]){n}(?![A-Z0-9])", norm(text)))


def parse_mz(text):
    return {
        m.group(1).strip("'- ")
        for m in re.finditer(
            r"\b(?:MZ|MZA|MANZANA)\s*([A-Z0-9\-]+)",
            norm(text),
        )
    }


def parse_lt(text):
    return {
        m.group(1).strip("'- ")
        for m in re.finditer(
            r"\b(?:LT|LOTE)\s*([A-Z0-9\-]+)",
            norm(text),
        )
    }


def browser_executable():
    if config.BROWSER_EXECUTABLE:
        p = Path(config.BROWSER_EXECUTABLE)
        if p.exists():
            return str(p)

    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]

    for p in candidates:
        if p.exists():
            return str(p)

    return ""


def launch_browser(playwright):
    exe = browser_executable()
    kwargs = {"headless": config.GOOGLE_HEADLESS, "slow_mo": 100}
    if exe:
        kwargs["executable_path"] = exe

    browser = playwright.chromium.launch(**kwargs)
    context = browser.new_context(
        locale="es-PE",
        viewport={"width": 1440, "height": 950},
    )
    page = context.new_page()
    page.set_default_timeout(config.GOOGLE_TIMEOUT_MS)
    return browser, context, page


def detect_block(page):
    try:
        body = norm(page.locator("body").inner_text(timeout=2500))
    except Exception:
        return False

    signals = [
        "NUESTROS SISTEMAS HAN DETECTADO TRAFICO INUSUAL",
        "CAPTCHA", "UNUSUAL TRAFFIC", "NO SOY UN ROBOT",
    ]
    return any(x in body for x in signals)


class GoogleBlocked(RuntimeError):
    pass


def wait_stable(page):
    elapsed = 0
    previous = ""
    while elapsed < config.GOOGLE_WAIT_MAX_MS:
        page.wait_for_timeout(config.GOOGLE_WAIT_STABLE_MS)
        elapsed += config.GOOGLE_WAIT_STABLE_MS
        current = page.url
        if elapsed >= config.GOOGLE_WAIT_MIN_MS and current == previous:
            return
        previous = current


def text_of(locator):
    try:
        if locator.count() > 0 and locator.first.is_visible():
            return locator.first.inner_text().strip()
    except Exception:
        pass
    return ""


def detect_ui(page):
    try:
        body = norm(page.locator("body").inner_text(timeout=2500))
    except Exception:
        body = ""

    if "COINCIDENCIA PARCIAL" in body:
        return "COINCIDENCIA_PARCIAL"

    try:
        feed = page.locator("div[role='feed']")
        if feed.count() > 0 and feed.first.is_visible():
            cards = feed.first.locator("div.Nv2PK")
            links = feed.first.locator("a[href*='/maps/place/']")
            if cards.count() > 1 or links.count() > 1:
                return "LISTA_RESULTADOS"
    except Exception:
        pass

    try:
        h1 = page.locator("h1.DUwDvf")
        if h1.count() > 0 and h1.first.is_visible():
            return "DETALLE_UNICO"
    except Exception:
        pass

    try:
        address = page.locator("[data-item-id='address']")
        if address.count() > 0 and address.first.is_visible():
            return "DETALLE_UNICO"
    except Exception:
        pass

    if "/maps/place/" in page.url:
        return "DETALLE_UNICO"

    return "SIN_DETALLE"


def open_search(page, query):
    url = (
        "https://www.google.com/maps/search/"
        "?api=1&hl=es&query=" + quote_plus(query)
    )

    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=config.GOOGLE_TIMEOUT_MS,
    )
    wait_stable(page)

    if detect_block(page):
        raise GoogleBlocked("Google mostró CAPTCHA/bloqueo.")

    try:
        feed = page.locator("div[role='feed']")
        if feed.count() > 0 and feed.first.is_visible():
            links = feed.first.locator("a[href*='/maps/place/']")
            if links.count() == 1:
                links.first.click(timeout=10000)
                page.wait_for_timeout(2200)
                wait_stable(page)
    except Exception:
        pass

    state = detect_ui(page)

    name = ""
    for selector in ["h1.DUwDvf", "h1"]:
        name = text_of(page.locator(selector))
        if name:
            break

    address = ""
    for selector in [
        "button[data-item-id='address']",
        "[data-item-id='address']",
        "button[aria-label^='Dirección']",
        "button[aria-label^='Direccion']",
    ]:
        address = text_of(page.locator(selector))
        if address:
            address = re.sub(
                r"^(Dirección|Direccion)\s*:\s*",
                "",
                address,
                flags=re.I,
            )
            break

    return {
        "estado_ui": state,
        "nombre": name,
        "direccion": address,
        "url": page.url,
    }


def exact_coords_from_url(url):
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", str(url or ""))
    if not m:
        return None, None

    lat = float(m.group(1))
    lon = float(m.group(2))

    if -19.5 <= lat <= 1.5 and -83.5 <= lon <= -67:
        return lat, lon

    return None, None


def evaluate(row, result):
    if result["estado_ui"] != "DETALLE_UNICO":
        return False, "La interfaz no muestra un punto individual."

    text = f"{result['nombre']} {result['direccion']}"

    road = str(row.get("VIA_CORREGIDA", "") or "")
    number = str(row.get("NUMERO_BUSCADO", "") or "")
    mz = str(row.get("MANZANA", "") or "")
    lt = str(row.get("LOTE", "") or "")
    address_type = str(row.get("TIPO_DIRECCION", "") or "")

    road_cov = coverage(road, text) if road else 0.0
    number_ok = contains_number(number, text) if number else False

    found_mz = parse_mz(text)
    found_lt = parse_lt(text)

    if mz and found_mz and norm(mz) not in found_mz:
        return False, f"Google muestra otra manzana: {sorted(found_mz)}."

    if lt and found_lt and norm(lt) not in found_lt:
        return False, f"Google muestra otro lote: {sorted(found_lt)}."

    if address_type == "VIA_NUMERO":
        if number_ok and road_cov >= 0.75:
            return True, "Detalle individual confirma vía y número."
        return False, "No confirma simultáneamente vía y número."

    if address_type == "VIA_MZA_LOTE":
        mz_ok = (not mz) or (norm(mz) in found_mz)
        lt_ok = (not lt) or (norm(lt) in found_lt)
        if road_cov >= 0.65 and mz_ok and lt_ok and (found_mz or found_lt):
            return True, "Detalle individual confirma vía y MZ/LT."
        return False, "No confirma los componentes de MZ/LT."

    if address_type == "MZA_LOTE_SIN_VIA":
        mz_ok = (not mz) or (norm(mz) in found_mz)
        lt_ok = (not lt) or (norm(lt) in found_lt)
        if mz_ok and lt_ok and (found_mz or found_lt):
            return True, "Detalle individual confirma MZ/LT."
        return False, "No confirma MZ/LT."

    return False, "Este tipo pasa al Paso 03 de referencia."


def append_csv(path, row):
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=OUTPUT_COLUMNS,
            extrasaction="ignore",
        )
        if not exists:
            writer.writeheader()
        writer.writerow({c: row.get(c, "") for c in OUTPUT_COLUMNS})
        f.flush()
        os.fsync(f.fileno())


def load_done():
    if not config.EXACT_CSV.exists():
        return set()
    df = pd.read_csv(
        config.EXACT_CSV,
        dtype=str,
        low_memory=False,
    ).fillna("")
    return set(df["NumeroRuc"].astype(str))


def choose_rows(df):
    if config.MODO.upper() == "MASIVO":
        return df

    pieces = []
    for _, group in df.groupby("TIPO_DIRECCION"):
        n = min(8, len(group))
        pieces.append(
            group.sample(
                n=n,
                random_state=config.SEMILLA_PRUEBA,
            )
        )

    return (
        pd.concat(pieces, ignore_index=True)
        .drop_duplicates(subset=["NumeroRuc"])
        .head(config.N_PRUEBA)
    )


def main():
    if not config.PREPARED_CSV.exists():
        raise FileNotFoundError(
            f"Primero ejecuta 01_preparar_direcciones.py\n"
            f"No existe: {config.PREPARED_CSV}"
        )

    df = pd.read_csv(
        config.PREPARED_CSV,
        dtype=str,
        low_memory=False,
    ).fillna("")

    df = choose_rows(df)

    done = load_done()
    df = df[~df["NumeroRuc"].astype(str).isin(done)].copy()

    print("=" * 100)
    print("02 - BUSCAR UBICACION EXACTA")
    print("=" * 100)
    print("Modo:", config.MODO)
    print("Pendientes:", len(df))

    if df.empty:
        print("No hay pendientes.")
        return

    with sync_playwright() as p:
        browser, context, page = launch_browser(p)

        try:
            for pos, (_, row) in enumerate(df.iterrows(), start=1):
                print(
                    f"\n{pos}/{len(df)} | RUC {row['NumeroRuc']} "
                    f"| {row['DIRECCION_ORIGINAL']}"
                )

                accepted = None
                attempts = 0
                best_reason = ""

                try:
                    for i in range(1, 9):
                        query = str(row.get(f"Q{i}", "") or "").strip()
                        qtype = str(row.get(f"Q{i}_TIPO", "") or "").strip()

                        if not query:
                            continue

                        attempts += 1
                        print(f"  Q{attempts} [{qtype}] {query}")

                        result = open_search(page, query)
                        ok, reason = evaluate(row, result)

                        print(
                            f"     UI={result['estado_ui']} "
                            f"| aceptar={ok} | {reason}"
                        )

                        best_reason = reason

                        if ok:
                            lat, lon = exact_coords_from_url(result["url"])
                            if lat is not None:
                                accepted = {
                                    **result,
                                    "lat": lat,
                                    "lon": lon,
                                    "query": query,
                                    "qtype": qtype,
                                    "reason": reason,
                                }
                                break

                        page.wait_for_timeout(config.GOOGLE_BETWEEN_QUERIES_MS)

                except GoogleBlocked as e:
                    print("GOOGLE BLOQUEADO/CAPTCHA. Se detiene.")
                    print(e)
                    return

                if accepted:
                    out = {
                        "NumeroRuc": row["NumeroRuc"],
                        "UBICACION_ORIGINAL": row["UBICACION_ORIGINAL"],
                        "ESTADO_EXACTO": "ENCONTRADA",
                        "TIPO_UBICACION": "UBICACION_EXACTA",
                        "NIVEL_UBICACION": "PREDIAL_LOTE",
                        "CONFIANZA": "ALTA",
                        "LATITUD_EXACTA": accepted["lat"],
                        "LONGITUD_EXACTA": accepted["lon"],
                        "FUENTE_COORDENADAS": "GOOGLE_MAPS_URL_PLACE",
                        "QUERY_USADA": accepted["query"],
                        "QUERY_TIPO": accepted["qtype"],
                        "INTENTOS": attempts,
                        "NOMBRE_GOOGLE": accepted["nombre"],
                        "DIRECCION_GOOGLE": accepted["direccion"],
                        "URL_GOOGLE": accepted["url"],
                        "ESTADO_UI_GOOGLE": accepted["estado_ui"],
                        "RAZON_DECISION": accepted["reason"],
                        "ERROR": "",
                    }
                else:
                    out = {
                        "NumeroRuc": row["NumeroRuc"],
                        "UBICACION_ORIGINAL": row["UBICACION_ORIGINAL"],
                        "ESTADO_EXACTO": "PENDIENTE_REFERENCIA",
                        "TIPO_UBICACION": "",
                        "NIVEL_UBICACION": "",
                        "CONFIANZA": "",
                        "LATITUD_EXACTA": "",
                        "LONGITUD_EXACTA": "",
                        "FUENTE_COORDENADAS": "",
                        "QUERY_USADA": "",
                        "QUERY_TIPO": "",
                        "INTENTOS": attempts,
                        "NOMBRE_GOOGLE": "",
                        "DIRECCION_GOOGLE": "",
                        "URL_GOOGLE": "",
                        "ESTADO_UI_GOOGLE": "",
                        "RAZON_DECISION": best_reason,
                        "ERROR": "",
                    }

                append_csv(config.EXACT_CSV, out)
                print(
                    f"  FINAL -> {out['ESTADO_EXACTO']} "
                    f"{out['LATITUD_EXACTA']}, {out['LONGITUD_EXACTA']}"
                )

                page.wait_for_timeout(config.GOOGLE_BETWEEN_ROWS_MS)

        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    print("\nOK:", config.EXACT_CSV)

if __name__ == "__main__":
    main()
