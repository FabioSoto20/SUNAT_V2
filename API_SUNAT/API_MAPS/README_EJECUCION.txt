ROBOT GOOGLE MAPS - ORDEN DE EJECUCION
========================================

CARPETA DE TRABAJO LOCAL ESPERADA:
D:\API_SUNAT\API_SUNAT\API_MAPS

ARCHIVO DE ENTRADA:
D:\API_SUNAT\API_SUNAT\archivos_ruc\final_results_completo_reducido.csv


1) EJECUTAR:
python 01_preparar_direcciones.py

GENERA:
- API_MAPS\direcciones_preparadas.csv
- API_MAPS\diagnostico_tipos_direccion.csv
- API_MAPS\correcciones_aprendidas.csv

El archivo correcciones_aprendidas.csv se puede ampliar con nuevas
correcciones manualmente verificadas. El ejemplo AL DEL ROMEDO ->
ALAMEDA DEL REMERO y HUERTOS DE VILLACRUC -> LOS HUERTOS DE VILLA
ya viene incluido porque fue verificado manualmente.


2) EJECUTAR:
python 02_robot_google_maps.py

IMPORTANTE:
- Primero recomiendo poner MAX_FILAS_EJECUCION = 100 para validar.
- Si todo se ve bien, poner MAX_FILAS_EJECUCION = None.
- Guarda API_MAPS\avance_maps.csv DESPUES DE CADA RUC.
- Por eso si se apaga la laptop, se vuelve a ejecutar el mismo script
  y continúa con los RUC que todavía no están en avance_maps.csv.
- Genera una copia Excel periódica:
  API_MAPS\avance_maps.xlsx
- Usa cache_maps.csv para no volver a buscar una dirección idéntica.
- Si Google muestra CAPTCHA/bloqueo, el script se DETIENE. No intenta
  saltarlo. El RUC actual queda pendiente para la siguiente ejecución.


3) CUANDO QUIERAS CONSOLIDAR:
python 03_consolidar_resultados.py

GENERA:
- API_MAPS\final_results_geocodificado.csv
- API_MAPS\final_results_geocodificado.xlsx
- API_MAPS\resumen_geocodificacion.csv


NIVELES
=======

ALTA / PREDIAL
- Vía coincide
- Número coincide
- Distrito coincide

MEDIA / VIA
- Vía y distrito coinciden
- No hay confirmación exacta del número

MEDIA / NEGOCIO
- La razón social coincide fuertemente
- Distrito compatible

BAJA / ZONA
- Solo hay evidencia de urbanización/zona

SIN_MATCH
- No se alcanzó un score suficientemente confiable


NOTA DE ESCALA
==============
Con más de 300 mil direcciones, un navegador automatizado puede tardar
muchos días o semanas y Google puede mostrar límites o CAPTCHA.
El script prioriza seguridad de avance y se detiene ante bloqueo.
Para producción masiva, una API oficial de geocodificación es más
estable que automatizar la interfaz web.
