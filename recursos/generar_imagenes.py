# -*- coding: utf-8 -*-
"""Genera los iconos, el logo y las imágenes del instalador de CaronteSync.
Uso (en un equipo con Pillow): python recursos/generar_imagenes.py
Los .ico se escriben en formato BMP clásico porque Windows XP/2003 no muestra
iconos comprimidos en PNG."""
import os
import struct
from PIL import Image, ImageDraw, ImageFont

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
CIELO_ARRIBA = (56, 140, 255)     # el cielo / la nube
RIO_ABAJO = (12, 24, 64)          # la laguna Estigia
ORO = (245, 178, 51)              # la barca
BLANCO = (255, 255, 255)
VERDE = (34, 197, 94)


def degradado(ancho, alto, arriba, abajo):
    im = Image.new("RGBA", (ancho, alto))
    d = ImageDraw.Draw(im)
    for y in range(alto):
        t = y / max(1, alto - 1)
        d.line([(0, y), (ancho, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(arriba, abajo)) + (255,))
    return im


def dibujar_icono(t, badge=False):
    e = 4
    T = t * e
    s = T / 64.0
    mascara = Image.new("L", (T, T), 0)
    ImageDraw.Draw(mascara).rounded_rectangle([0, 0, T - 1, T - 1], radius=int(14 * s), fill=255)
    im = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    im.paste(degradado(T, T, CIELO_ARRIBA, RIO_ABAJO), (0, 0), mascara)
    d = ImageDraw.Draw(im)
    # Nube
    for cx, cy, r in ((22, 24, 9), (32, 18, 12), (43, 24, 9)):
        d.ellipse([(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s], fill=BLANCO)
    d.rounded_rectangle([13 * s, 22 * s, 52 * s, 33 * s], radius=5 * s, fill=BLANCO)
    # Flecha que sube de la barca a la nube
    d.polygon([(32 * s, 21 * s), (39 * s, 29 * s), (35 * s, 29 * s), (35 * s, 41 * s), (29 * s, 41 * s),
               (29 * s, 29 * s), (25 * s, 29 * s)], fill=(30, 90, 200, 255))
    # Barca de Caronte
    d.chord([10 * s, 34 * s, 54 * s, 56 * s], 0, 180, fill=ORO)
    d.rectangle([10 * s, 44 * s, 54 * s, 45.5 * s], fill=ORO)
    # Agua
    d.line([(6 * s, 55 * s), (58 * s, 55 * s)], fill=(120, 170, 255, 255), width=max(1, int(2 * s)))
    if badge:
        d.ellipse([38 * s, 38 * s, 62 * s, 62 * s], fill=VERDE, outline=BLANCO, width=max(1, int(2.5 * s)))
        d.polygon([(50 * s, 42 * s), (57 * s, 50 * s), (53 * s, 50 * s), (53 * s, 57 * s), (47 * s, 57 * s),
                   (47 * s, 50 * s), (43 * s, 50 * s)], fill=BLANCO)
    return im.resize((t, t), Image.LANCZOS)


def dib(im):
    t = im.size[0]
    px = im.load()
    cabecera = struct.pack("<IiiHHIIiiII", 40, t, t * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    color = b"".join(bytes((px[x, y][2], px[x, y][1], px[x, y][0], px[x, y][3]))
                     for y in range(t - 1, -1, -1) for x in range(t))
    fila = ((t + 31) // 32) * 4
    mascara = b""
    for y in range(t - 1, -1, -1):
        linea = bytearray(fila)
        for x in range(t):
            if px[x, y][3] < 128:
                linea[x // 8] |= 0x80 >> (x % 8)
        mascara += bytes(linea)
    return cabecera + color + mascara


def guardar_ico(ruta, badge=False):
    tamanos = (16, 24, 32, 48)
    datos = [dib(dibujar_icono(t, badge)) for t in tamanos]
    salida = struct.pack("<HHH", 0, 1, len(tamanos))
    desplazamiento = 6 + 16 * len(tamanos)
    for t, d in zip(tamanos, datos):
        salida += struct.pack("<BBBBHHII", t, t, 0, 0, 1, 32, len(d), desplazamiento)
        desplazamiento += len(d)
    with open(ruta, "wb") as f:
        f.write(salida + b"".join(datos))


def fuente(tamano, negrita=False):
    for nombre in ("DejaVuSans-Bold.ttf" if negrita else "DejaVuSans.ttf",):
        for carpeta in ("/usr/share/fonts/truetype/dejavu",):
            ruta = os.path.join(carpeta, nombre)
            if os.path.exists(ruta):
                return ImageFont.truetype(ruta, tamano)
    return ImageFont.load_default()


def imagenes_instalador():
    # Imagen grande del asistente (164x314) y pequeña (55x58), en BMP de 24 bits.
    grande = degradado(164, 314, (30, 64, 140), RIO_ABAJO).convert("RGB")
    logo = dibujar_icono(96)
    grande.paste(logo, (34, 70), logo)
    d = ImageDraw.Draw(grande)
    d.text((82, 190), "CaronteSync", font=fuente(19, True), fill=BLANCO, anchor="mm")
    d.text((82, 216), "Cruza sus respaldos", font=fuente(11), fill=(190, 210, 255), anchor="mm")
    d.text((82, 231), "hacia la nube", font=fuente(11), fill=(190, 210, 255), anchor="mm")
    grande.save(os.path.join(RAIZ, "instalador", "asistente_grande.bmp"))
    pequena = Image.new("RGB", (55, 58), BLANCO)
    logo = dibujar_icono(48)
    pequena.paste(logo, (4, 5), logo)
    pequena.save(os.path.join(RAIZ, "instalador", "asistente_pequena.bmp"))


if __name__ == "__main__":
    guardar_ico(os.path.join(RAIZ, "carontesync.ico"))
    guardar_ico(os.path.join(RAIZ, "carontesync_subiendo.ico"), badge=True)
    dibujar_icono(40).save(os.path.join(RAIZ, "carontesync_logo.png"))
    imagenes_instalador()
    print("Imágenes generadas.")
