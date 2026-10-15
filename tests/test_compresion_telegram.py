# -*- coding: utf-8 -*-
"""Pruebas de la compresión verificada y de los avisos por Telegram."""

import gzip
import hashlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carontesync  # noqa: E402
from tests.nube_falsa import MB  # noqa: E402
from tests.test_carontesync import BasePrueba, datos_aleatorios  # noqa: E402


def datos_comprimibles(n):
    """Como un respaldo de SQL: texto repetitivo con algo de variación."""
    fila = "INSERT INTO clientes VALUES ({0}, 'Cliente numero {1}', 'Activo', 12345.67);\n"
    partes = []
    total = 0
    i = 0
    while total < n:
        linea = fila.format(i, i * 7).encode("ascii")     # bytes % (...) no existe en Python 3.4
        partes.append(linea)
        total += len(linea)
        i += 1
    return b"".join(partes)[:n]


class AjustesIni(object):
    def ajustar(self, general=None, telegram=None):
        with open(self.ini, encoding="utf-8") as f:
            texto = f.read()
        for clave, valor in (general or {}).items():
            lineas = texto.split("\n")
            existentes = [i for i, l in enumerate(lineas) if l.split("=")[0].strip() == clave]
            if existentes:
                lineas[existentes[0]] = "{0} = {1}".format(clave, valor)
                texto = "\n".join(lineas)
            else:
                texto = texto.replace("[general]\n", "[general]\n{0} = {1}\n".format(clave, valor), 1)
        if telegram:
            texto += "\n[telegram]\n" + "".join("{0} = {1}\n".format(c, v) for c, v in telegram.items())
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write(texto)

    def temporales(self):
        carpeta = os.path.join(self.origen, carontesync.CARPETA_TEMPORAL)
        return sorted(os.listdir(carpeta)) if os.path.isdir(carpeta) else []


class PruebasCompresionDropbox(BasePrueba, AjustesIni):
    proveedor = "dropbox"

    def setUp(self):
        BasePrueba.setUp(self)
        self.ajustar(general={"comprimir": "si", "nivel_compresion": "6"})

    def test_comprime_verifica_y_sube_el_gz(self):
        datos = datos_comprimibles(3 * MB)
        self.crear("TVCable_20261015_010000.bak", datos)
        self.assertEqual(self.ejecutar(), 0)
        subidos = self.nube.dbx_archivos
        self.assertEqual(list(subidos), ["/respaldos/sql/tvcable_20261015_010000.bak.gz"])
        comprimido = subidos["/respaldos/sql/tvcable_20261015_010000.bak.gz"][1]
        self.assertLess(len(comprimido), len(datos) // 5)
        self.assertEqual(gzip.decompress(comprimido), datos)       # en la nube se recupera idéntico
        self.assertEqual(self.temporales(), [])                    # no deja temporales
        detalle = self.historial("archivo")[-1]["detalle"]
        self.assertIn("compresión verificada", detalle)
        self.assertIn("Suma de verificación correcta", detalle)
        self.assertEqual(self.actividad()["lote_hecho"], self.actividad()["lote_total"])
        self.assertEqual(self.ejecutar(), 0)                       # sin cambios: no vuelve a subir
        self.assertEqual(self.nube.contar("POST", "/2/files/upload"), 1)

    def test_reanuda_sin_volver_a_comprimir(self):
        datos = os.urandom(4 * MB + 11)                            # incompresible: el .gz ocupa >1 bloque
        self.crear("grande.bak", datos)
        self.detener_al_recibir("POST", "/2/files/upload_session/append_v2", 1)
        with mock.patch.object(carontesync, "comprimir_verificado",
                               wraps=carontesync.comprimir_verificado) as comprimir:
            self.assertEqual(self.ejecutar(), 4)
            self.assertEqual(len([t for t in self.temporales() if t.endswith(".gz")]), 1)   # queda listo
            self.nube.al_recibir = None
            self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(comprimir.call_count, 1)                  # la segunda vez no comprimió de nuevo
        self.assertEqual(gzip.decompress(self.nube.dbx_archivos["/respaldos/sql/grande.bak.gz"][1]), datos)
        self.assertEqual(self.nube.contar("POST", "/2/files/upload_session/start"), 1)
        self.assertEqual(self.temporales(), [])

    def test_compresion_que_no_se_verifica_no_se_sube(self):
        ruta = self.crear("a.bak", datos_comprimibles(MB))
        abrir = gzip.open

        class Dañado(object):
            def __init__(self, archivo):
                self.archivo = archivo
                self.primero = True

            def read(self, n):
                datos = self.archivo.read(n)
                if self.primero and datos:
                    self.primero = False
                    return b"X" + datos[1:]          # un byte distinto al descomprimir
                return datos

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.archivo.close()

        with mock.patch.object(carontesync.gzip, "open", lambda ruta, modo: Dañado(abrir(ruta, modo))):
            self.escribir_ini(borrar="si")
            self.ajustar(general={"comprimir": "si"})
            self.assertEqual(self.ejecutar(), 1)
        self.assertEqual(self.nube.dbx_archivos, {})               # no se subió nada
        self.assertTrue(os.path.exists(ruta))                      # y el original no se borró
        self.assertEqual(self.temporales(), [])
        self.assertIn("verificación de la compresión falló", self.historial("archivo")[-1]["detalle"])

    def test_archivos_ya_comprimidos_se_suben_tal_cual(self):
        self.ajustar(general={"patrones": "*.zip"})
        self.crear("respaldo.zip", b"PK" + datos_aleatorios(5000))
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(list(self.nube.dbx_archivos), ["/respaldos/sql/respaldo.zip"])

    def test_sin_espacio_para_comprimir(self):
        ruta = self.crear("a.bak", datos_comprimibles(MB))
        uso = carontesync.shutil.disk_usage(self.dir)
        with mock.patch.object(carontesync.shutil, "disk_usage", return_value=uso._replace(free=1000)):
            self.assertEqual(self.ejecutar(), 1)
        self.assertIn("no hay espacio", self.historial("archivo")[-1]["detalle"])
        self.assertTrue(os.path.exists(ruta))

    def test_borrar_local_solo_despues_de_verificar_todo(self):
        self.escribir_ini(borrar="si")
        self.ajustar(general={"comprimir": "si"})
        ruta = self.crear("a.bak", datos_comprimibles(2 * MB))
        self.assertEqual(self.ejecutar(), 0)
        self.assertFalse(os.path.exists(ruta))
        self.assertEqual(gzip.decompress(self.nube.dbx_archivos["/respaldos/sql/a.bak.gz"][1]),
                         datos_comprimibles(2 * MB))

    def test_la_carpeta_temporal_nunca_se_sube(self):
        self.escribir_ini(recursivo="si")
        self.ajustar(general={"comprimir": "si", "patrones": "*"})
        carpeta = os.path.join(self.origen, carontesync.CARPETA_TEMPORAL)
        os.makedirs(carpeta)
        with open(os.path.join(carpeta, "resto.gz"), "wb") as f:
            f.write(b"x")
        self.crear("a.bak", b"hola")
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(list(self.nube.dbx_archivos), ["/respaldos/sql/a.bak.gz"])

    def test_simular_no_comprime(self):
        self.crear("a.bak", datos_comprimibles(MB))
        self.assertEqual(self.ejecutar("subir", "--simular"), 0)
        self.assertEqual(self.temporales(), [])
        self.assertEqual(self.nube.dbx_archivos, {})

    def test_carpeta_temporal_igual_al_origen_no_se_acepta(self):
        self.ajustar(general={"carpeta_temporal": "origen"})
        self.crear("a.bak", b"x")
        self.assertEqual(self.ejecutar(), 2)


class PruebasCompresionDrive(BasePrueba, AjustesIni):
    proveedor = "gdrive"

    def test_comprime_y_sube_a_google_drive(self):
        self.ajustar(general={"comprimir": "si"})
        datos = datos_comprimibles(3 * MB)
        self.crear("base.bak", datos)
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(gzip.decompress(self.nube.gd_ruta("Respaldos/SQL/base.bak.gz")["datos"]), datos)


class PruebasDescomprimir(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.dir = tempfile.mkdtemp(prefix="carontesync_gz_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_comprimir_y_descomprimir_es_identico(self):
        original = os.path.join(self.dir, "base.bak")
        datos = datos_comprimibles(2 * MB) + datos_aleatorios(MB)
        with open(original, "wb") as f:
            f.write(datos)
        gz = os.path.join(self.dir, "base.bak.gz")
        resultado = carontesync.comprimir_verificado(original, gz, 1)
        self.assertEqual(resultado["sha256"], hashlib.sha256(datos).hexdigest())
        restaurado = os.path.join(self.dir, "restaurado.bak")
        self.assertEqual(carontesync.descomprimir(gz, restaurado), len(datos))
        with open(restaurado, "rb") as f:
            self.assertEqual(f.read(), datos)

    def test_descomprimir_un_gz_dañado_avisa_y_no_deja_nada(self):
        original = os.path.join(self.dir, "base.bak")
        with open(original, "wb") as f:
            f.write(datos_comprimibles(MB))
        gz = os.path.join(self.dir, "base.bak.gz")
        carontesync.comprimir_verificado(original, gz)
        with open(gz, "r+b") as f:
            f.seek(os.path.getsize(gz) // 2)
            f.write(b"\x00\x00\x00\x00")
        destino = os.path.join(self.dir, "restaurado.bak")
        with self.assertRaises(carontesync.ErrorNube):
            carontesync.descomprimir(gz, destino)
        self.assertFalse(os.path.exists(destino))
        self.assertFalse(os.path.exists(destino + ".parcial"))


class PruebasTelegram(BasePrueba, AjustesIni):
    proveedor = "dropbox"

    def activar(self, **extra):
        opciones = {"activo": "si", "token": self.nube.tg_token, "chat_id": "987654",
                    "nombre_servidor": "SRV-SQL2003"}
        opciones.update(extra)
        self.ajustar(telegram=opciones)

    def test_aviso_al_terminar_bien(self):
        self.activar()
        self.crear("TVCable_20261015.bak", datos_aleatorios(3000))
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(len(self.nube.tg_mensajes), 1)
        chat, texto = self.nube.tg_mensajes[0]
        self.assertEqual(chat, "987654")
        self.assertIn(u"✅ CaronteSync: Respaldos en la nube", texto)
        self.assertIn("Servidor: SRV-SQL2003", texto)
        self.assertIn("TVCable_20261015.bak", texto)
        self.assertIn("verificado", texto)

    def test_aviso_con_compresion_muestra_el_ahorro(self):
        self.activar()
        self.ajustar(general={"comprimir": "si"})
        self.crear("a.bak", datos_comprimibles(2 * MB))
        self.assertEqual(self.ejecutar(), 0)
        self.assertIn(u"a.bak.gz (2.0 MB →", self.nube.tg_mensajes[0][1])

    def test_aviso_de_errores(self):
        self.activar()
        self.crear("a.bak", b"uno")
        self.crear("b.bak", b"dos")
        self.nube.fallar("POST", "/2/files/upload", estado=409, veces=1, cuerpo={
            "error_summary": "path/insufficient_space/", "error": {".tag": "path"}})
        self.assertEqual(self.ejecutar(), 1)
        texto = self.nube.tg_mensajes[0][1]
        self.assertIn("Subida con errores", texto)
        self.assertIn("insufficient_space", texto)

    def test_aviso_cuando_la_subida_falla(self):
        self.activar()
        with open(os.path.join(self.dir, "tokens.json"), "w") as f:
            f.write('{"dropbox": {"refresh_token": "revocado"}}')
        self.crear("a.bak", b"x")
        self.assertEqual(self.ejecutar(), 2)
        self.assertIn(u"❌ CaronteSync: La subida falló", self.nube.tg_mensajes[0][1])

    def test_sin_novedades_solo_avisa_si_se_pide(self):
        self.activar()
        self.crear("a.bak", b"x")
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.ejecutar(), 0)                 # nada nuevo
        self.assertEqual(len(self.nube.tg_mensajes), 1)
        self.escribir_ini()
        self.activar(avisar_sin_novedades="si")
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(len(self.nube.tg_mensajes), 2)

    def test_solo_errores(self):
        self.activar(avisar_exito="no")
        self.crear("a.bak", b"x")
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.nube.tg_mensajes, [])

    def test_si_telegram_falla_la_subida_sigue_bien(self):
        self.activar(chat_id="111")                           # chat inexistente
        self.crear("a.bak", b"x")
        self.assertEqual(self.ejecutar(), 0)
        self.assertIn("/respaldos/sql/a.bak", self.nube.dbx_archivos)
        with open(os.path.join(self.dir, "carontesync.log"), encoding="utf-8") as f:
            self.assertIn("No se pudo enviar el aviso de Telegram", f.read())

    def test_el_token_no_queda_en_el_registro(self):
        self.activar()
        self.crear("a.bak", b"x")
        self.assertEqual(self.ejecutar("subir", "-v"), 0)
        with open(os.path.join(self.dir, "carontesync.log"), encoding="utf-8") as f:
            registro = f.read()
        self.assertIn("/bot***/sendMessage", registro)
        self.assertNotIn(self.nube.tg_token, registro)

    def test_detectar_chats_y_token_invalido(self):
        http = carontesync.ClienteHttp(timeout=10)
        self.addCleanup(http.cerrar)
        chats = carontesync.Telegram(http, self.nube.tg_token).chats()
        self.assertEqual(chats, [("987654", "Ana Soporte"), ("-100222", "Sistemas")])
        with self.assertRaises(carontesync.ErrorFatal):
            carontesync.Telegram(http, "mal:token").chats()

    def test_activo_sin_chat_id_no_se_puede_guardar(self):
        self.ajustar(telegram={"activo": "si", "token": "x"})
        with self.assertRaises(carontesync.ErrorFatal):
            carontesync.Configuracion(self.ini)


if __name__ == "__main__":
    unittest.main()
