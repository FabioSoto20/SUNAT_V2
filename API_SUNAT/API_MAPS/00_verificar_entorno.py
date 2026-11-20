import importlib
import platform
import sys
import config

def buscar_input():
    for p in config.INPUT_CANDIDATES:
        if p.exists():
            return p
    return None

def main():
    print("=" * 90)
    print("VERIFICACION DE ENTORNO")
    print("=" * 90)
    print("Python :", sys.version.replace("\n", " "))
    print("SO     :", platform.platform())
    print("Proyecto:", config.PROJECT_DIR)

    faltantes = []
    for nombre in ["pandas", "openpyxl", "requests", "playwright"]:
        try:
            importlib.import_module(nombre)
            print(f"  OK  {nombre}")
        except Exception:
            faltantes.append(nombre)
            print(f"  FALTA {nombre}")

    archivo = buscar_input()
    print("\nArchivo fuente:")
    if archivo:
        print("  OK ", archivo)
    else:
        print("  NO ENCONTRADO")
        for p in config.INPUT_CANDIDATES:
            print("   -", p)

    print("\n" + "=" * 90)
    if faltantes:
        print("Faltan dependencias. Ejecuta 00_instalar_dependencias.ps1")
    elif not archivo:
        print("Falta el CSV fuente.")
    else:
        print("ENTORNO LISTO PARA EJECUTAR EL PASO 01.")
    print("=" * 90)

if __name__ == "__main__":
    main()
