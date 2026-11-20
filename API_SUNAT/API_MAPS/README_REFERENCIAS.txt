ORDEN DE EJECUCION - EXACTO + REFERENCIA
=========================================

ARCHIVOS NUEVOS
---------------
03_buscar_ubicacion_referencia.py
04_consolidar_geolocalizacion.py


FLUJO DE PRUEBA
---------------

1. Tener listo:
   direcciones_preparadas_v4.csv

2. Ejecutar robot exacto:
   02_robot_google_maps_v6.py

3. Ejecutar:
   03_buscar_ubicacion_referencia.py

   Este script detecta automáticamente los RUC sin latitud/longitud
   del robot exacto y trata de conseguir una referencia.

   Jerarquía de referencia:
   - MANZANA
   - VIA_ZONA
   - POI_ZONA
   - NEGOCIO
   - VIA
   - ZONA
   - DISTRITO (último recurso)

   Si todo falla, el último fallback es el centro del distrito.
   Esa coordenada SIEMPRE queda marcada como:
       TIPO_UBICACION = UBICACION_REFERENCIA
       NIVEL_REFERENCIA = DISTRITO
       CONFIANZA_REFERENCIA = BAJA

4. Ejecutar:
   04_consolidar_geolocalizacion.py

   Produce:
   - resultado_geocodificacion_prueba.csv
   - resultado_geocodificacion_prueba.xlsx

   Y solamente tres estados finales:
   - UBICACION_EXACTA
   - UBICACION_REFERENCIA
   - SIN_UBICACION


PASO A MASIVO
-------------

Cuando la prueba quede validada:

03_buscar_ubicacion_referencia.py
    MODO_PRUEBA = False

04_consolidar_geolocalizacion.py
    MODO_PRUEBA = False

Y el robot exacto también debe estar en su modo masivo.

IMPORTANTE
----------
No borres los CSV de avance durante una ejecución.
Los robots guardan checkpoint incremental.
