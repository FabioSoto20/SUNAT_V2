GEOLOCALIZACION RUC - DESDE CERO
================================

OBJETIVO
--------
Conservar SIEMPRE la ubicación original SUNAT y generar una ubicación final
clasificada como:

1. UBICACION_EXACTA
2. UBICACION_REFERENCIA
3. SIN_UBICACION


ARCHIVO FUENTE
--------------
Columnas obligatorias:

NumeroRuc
Nombre ó RazonSocial
Departamento
Provincia
Distrito
Direccion

Ruta principal esperada:
D:\API_SUNAT\API_SUNAT\archivos_ruc\final_results_completo_reducido.csv

Si esa ruta no existe, copia el CSV a:
input\final_results_completo_reducido.csv


PASO 0
------
PowerShell:

powershell -ExecutionPolicy Bypass -File .\00_instalar_dependencias.ps1

Luego:

python .\00_verificar_entorno.py


PASO 1
------
python .\01_preparar_direcciones.py

Salida:
output\01_direcciones_preparadas.csv

SIEMPRE conserva:
DEPARTAMENTO_ORIGINAL
PROVINCIA_ORIGINAL
DISTRITO_ORIGINAL
DIRECCION_ORIGINAL
UBICACION_ORIGINAL


PASO 2 - EXACTO
---------------
python .\02_buscar_ubicacion_exacta.py

Salida:
output\02_ubicaciones_exactas.csv

Reglas:
- NO usa centro del mapa.
- NO acepta listas.
- NO acepta coincidencia parcial.
- NO acepta MZ/LT contradictoria.
- Solo guarda coordenada cuando hay evidencia predial/lote.


PASO 3 - REFERENCIA MULTIFUENTE
-------------------------------
python .\03_investigar_ubicacion_referencia.py

Este paso NO vuelve a Google Maps.

Fuentes:
1. Nominatim / OpenStreetMap
2. ArcGIS World Geocoder
3. Overpass / OpenStreetMap
4. Centro del distrito como último fallback

Niveles:
MANZANA
VIA_ZONA
POI_ZONA
REFERENCIA_TEXTO
VIA
ZONA
NEGOCIO
POI_VIA_OSM
DISTRITO

Todas estas coordenadas quedan marcadas:
TIPO_UBICACION = UBICACION_REFERENCIA


PASO 4
------
python .\04_consolidar_resultados.py

Salidas:
output\04_resultado_final.csv
output\04_resultado_final.xlsx

Columnas principales:
UBICACION_ORIGINAL
LATITUD_FINAL
LONGITUD_FINAL
TIPO_UBICACION_FINAL
NIVEL_UBICACION_FINAL
CONFIANZA_FINAL
FUENTE_FINAL
DETALLE_FINAL


PASO 5
------
python .\05_resumen_calidad.py

Salida:
output\05_resumen_calidad.xlsx


MODO PRUEBA / MASIVO
--------------------
En config.py:

MODO = "PRUEBA"
N_PRUEBA = 50

Primero valida la prueba.

Después:
MODO = "MASIVO"


IMPORTANTE - NOMINATIM
----------------------
El Nominatim público NO debe usarse para geocodificación masiva.

Para prueba pequeña puede funcionar con pausas.

Para miles de pendientes, usa un Nominatim local y coloca en config.py:

NOMINATIM_LOCAL_URL = "http://localhost:8080"


LAPTOP CORPORATIVA
------------------
No se desactiva SSL.

Si requests falla por certificado corporativo, lo correcto es usar el
certificado CA corporativo / REQUESTS_CA_BUNDLE.

El Paso 02 intenta usar Edge o Chrome instalado y por eso no obliga a
descargar Chromium de Playwright.


ORDEN RECOMENDADO
-----------------
1. Descomprime toda la carpeta.
2. Ejecuta 00_instalar_dependencias.ps1.
3. Ejecuta 00_verificar_entorno.py.
4. Deja MODO = "PRUEBA".
5. Ejecuta 01.
6. Ejecuta 02.
7. Ejecuta 03.
8. Ejecuta 04.
9. Ejecuta 05.
10. Recién después evaluar MASIVO.
