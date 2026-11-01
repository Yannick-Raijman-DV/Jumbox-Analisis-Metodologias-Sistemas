"""Convierte el dump de MySQL (phpMyAdmin) del proyecto Java a datos para SQLite.

Uso (una sola vez, desde la carpeta supermercado/):
    python migracion/convertir_dump.py

Qué hace:
    1. Lee migracion/supermercado.sql y parsea cada INSERT INTO ... VALUES (...).
    2. Los BLOB de `productos.foto_producto` (literales 0x...) se decodifican y se
       guardan como archivos en migracion/fotos/<id_producto>.<ext>. En la nueva
       versión las imágenes viven en AWS S3 y en la base solo queda la "key".
    3. Escribe migracion/seed.json con todas las filas, que es lo que usa
       app/seed.py para poblar la base SQLite.

No usa librerías externas: se puede correr sin el entorno virtual.
"""
import json
import re
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
DUMP = CARPETA / "supermercado.sql"
SALIDA = CARPETA / "seed.json"
FOTOS = CARPETA / "fotos"

PATRON_INSERT = re.compile(r"INSERT INTO `(\w+)` \(([^)]*)\) VALUES\s*", re.IGNORECASE)


def extension_por_magic_bytes(datos: bytes) -> str:
    """Detecta el formato de la imagen mirando los primeros bytes."""
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return ".webp"
    if datos[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if datos[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if datos[:4] in (b"GIF8",):
        return ".gif"
    return ".bin"


def _leer_valor(texto: str, i: int):
    """Lee un valor SQL a partir de la posición i. Devuelve (valor, nueva_posición)."""
    while texto[i] in " \r\n\t":
        i += 1

    if texto[i] == "'":  # string con escapes estilo MySQL
        i += 1
        partes = []
        while True:
            c = texto[i]
            if c == "\\":
                siguiente = texto[i + 1]
                partes.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(siguiente, siguiente))
                i += 2
            elif c == "'":
                if texto[i + 1] == "'":  # '' = comilla escapada
                    partes.append("'")
                    i += 2
                else:
                    return "".join(partes), i + 1
            else:
                partes.append(c)
                i += 1

    fin = i
    while texto[fin] not in ",)":
        fin += 1
    crudo = texto[i:fin].strip()

    if crudo.upper() == "NULL":
        return None, fin
    if crudo.lower().startswith("0x"):
        return bytes.fromhex(crudo[2:]), fin
    try:
        return (float(crudo) if "." in crudo else int(crudo)), fin
    except ValueError:
        return crudo, fin


def parsear_inserts(sql: str) -> dict[str, list[dict]]:
    tablas: dict[str, list[dict]] = {}
    for m in PATRON_INSERT.finditer(sql):
        tabla = m.group(1)
        columnas = [c.strip(" `") for c in m.group(2).split(",")]
        i = m.end()
        filas = tablas.setdefault(tabla, [])
        while True:
            while sql[i] in " \r\n\t,":
                i += 1
            if sql[i] != "(":
                break  # llegamos al ';'
            i += 1
            valores = []
            while True:
                valor, i = _leer_valor(sql, i)
                valores.append(valor)
                while sql[i] in " \r\n\t":
                    i += 1
                if sql[i] == ",":
                    i += 1
                    continue
                if sql[i] == ")":
                    i += 1
                    break
            filas.append(dict(zip(columnas, valores)))
            while sql[i] in " \r\n\t":
                i += 1
            if sql[i] == ";":
                break
    return tablas


def convertir(dump: Path = DUMP, salida: Path = SALIDA, carpeta_fotos: Path = FOTOS) -> dict:
    sql = dump.read_text(encoding="utf-8")
    tablas = parsear_inserts(sql)

    carpeta_fotos.mkdir(parents=True, exist_ok=True)
    for producto in tablas.get("productos", []):
        blob = producto.pop("foto_producto", None)
        producto["foto_archivo"] = None
        if isinstance(blob, bytes) and blob:
            nombre = f"{producto['id_producto']}{extension_por_magic_bytes(blob)}"
            (carpeta_fotos / nombre).write_bytes(blob)
            producto["foto_archivo"] = nombre

    salida.write_text(json.dumps(tablas, ensure_ascii=False, indent=2), encoding="utf-8")
    return tablas


if __name__ == "__main__":
    resultado = convertir()
    for nombre, filas in sorted(resultado.items()):
        print(f"{nombre:32} {len(filas):>4} filas")
    print(f"\nGenerado {SALIDA.relative_to(CARPETA.parent)} y fotos en {FOTOS.relative_to(CARPETA.parent)}/")
