from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
INPUT_DIR = PROJECT_DIR / "input"
OUTPUT_DIR = PROJECT_DIR / "output"
CACHE_DIR = PROJECT_DIR / "cache"
LOG_DIR = PROJECT_DIR / "logs"

for _p in [INPUT_DIR, OUTPUT_DIR, CACHE_DIR, LOG_DIR]:
    _p.mkdir(parents=True, exist_ok=True)

INPUT_CANDIDATES = [
    Path(r"D:\API_SUNAT\API_SUNAT\archivos_ruc\final_results_completo_reducido.csv"),
    INPUT_DIR / "final_results_completo_reducido.csv",
]

COLUMN_RUC = "NumeroRuc"
COLUMN_NAME = "Nombre ó RazonSocial"
COLUMN_DEPARTMENT = "Departamento"
COLUMN_PROVINCE = "Provincia"
COLUMN_DISTRICT = "Distrito"
COLUMN_ADDRESS = "Direccion"

MODO = "PRUEBA"
N_PRUEBA = 50
SEMILLA_PRUEBA = 42

GOOGLE_HEADLESS = False
GOOGLE_TIMEOUT_MS = 60000
GOOGLE_WAIT_MIN_MS = 3500
GOOGLE_WAIT_MAX_MS = 8500
GOOGLE_WAIT_STABLE_MS = 700
GOOGLE_BETWEEN_QUERIES_MS = 1000
GOOGLE_BETWEEN_ROWS_MS = 1300
GOOGLE_RESTART_EVERY = 300
BROWSER_MODE = "AUTO"
BROWSER_EXECUTABLE = ""

NOMINATIM_LOCAL_URL = ""
NOMINATIM_PUBLIC_URL = "https://nominatim.openstreetmap.org"
NOMINATIM_USER_AGENT = "Geolocalizacion-RUC-Referencia/1.0"
NOMINATIM_DELAY_SECONDS = 1.1

ARCGIS_URL = (
    "https://geocode.arcgis.com/arcgis/rest/services/"
    "World/GeocodeServer/findAddressCandidates"
)
ARCGIS_DELAY_SECONDS = 0.4

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_DELAY_SECONDS = 1.0
OVERPASS_RADIUS_METERS = 8000

ALLOW_DISTRICT_CENTROID_FALLBACK = True
REQUEST_TIMEOUT_SECONDS = 35

PREPARED_CSV = OUTPUT_DIR / "01_direcciones_preparadas.csv"
EXACT_CSV = OUTPUT_DIR / "02_ubicaciones_exactas.csv"
REFERENCE_CSV = OUTPUT_DIR / "03_ubicaciones_referencia.csv"
FINAL_CSV = OUTPUT_DIR / "04_resultado_final.csv"
FINAL_XLSX = OUTPUT_DIR / "04_resultado_final.xlsx"
QUALITY_XLSX = OUTPUT_DIR / "05_resumen_calidad.xlsx"
