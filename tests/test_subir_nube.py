# -*- coding: utf-8 -*-
"""Pruebas de subir_nube.py contra un servidor falso de Dropbox y Google Drive.

Ejecutar desde la carpeta del proyecto:  python -m unittest discover -s tests -v
"""

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subir_nube  # noqa: E402
from tests.nube_falsa import MB, NubeFalsa, hash_dropbox  # noqa: E402

INI = u"""[general]
proveedor = {proveedor}
origen = origen
carpeta_remota = /Respaldos/SQL
patrones = *.bak; *.trn
recursivo = {recursivo}
antiguedad_minima_minutos = 0
borrar_local_tras_subir = {borrar}
tamano_bloque_mb = 1
reintentos = 3
certificados =

[dropbox]
app_key = clave-app
app_secret = secreto-app

[gdrive]
client_id = cliente-gd
client_secret = secreto-gd
"""


def datos_aleatorios(n, semilla=0):
    """Bytes pseudoaleatorios reproducibles (sin depender de random)."""
    bloques = []
    total = 0
    contador = semilla
    while total < n:
        bloque = hashlib.sha256(str(contador).encode("ascii")).digest() * 64
        bloques.append(bloque)
        total += len(bloque)
        contador += 1
    return b"".join(bloques)[:n]


class BasePrueba(unittest.TestCase):
    proveedor = "dropbox"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subir_nube_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.origen = os.path.join(self.dir, "origen")
        os.makedirs(self.origen)
        self.nube = NubeFalsa()
        self.addCleanup(self.nube.cerrar)
        base = self.nube.base
        for clase, atributo, valor in (
                (subir_nube.Dropbox, "URL_TOKEN", base + "/oauth2/token"),
                (subir_nube.Dropbox, "URL_API", base + "/2"),
                (subir_nube.Dropbox, "URL_CONTENIDO", base + "/2"),
                (subir_nube.GoogleDrive, "URL_TOKEN", base + "/token"),
                (subir_nube.GoogleDrive, "URL_DISPOSITIVO", base + "/device/code"),
                (subir_nube.GoogleDrive, "URL_API", base + "/drive/v3"),
                (subir_nube.GoogleDrive, "URL_SUBIDA", base + "/upload/drive/v3")):
            parche = mock.patch.object(clase, atributo, valor)
            parche.start()
            self.addCleanup(parche.stop)
        self.esperas = []
        parche = mock.patch.object(subir_nube, "_dormir", self.esperas.append)
        parche.start()
        self.addCleanup(parche.stop)
        self.escribir_ini()
        self.guardar_token()

    def escribir_ini(self, recursivo="no", borrar="no", extra=""):
        self.ini = os.path.join(self.dir, "subir_nube.ini")
        texto = INI.format(proveedor=self.proveedor, recursivo=recursivo, borrar=borrar) + extra
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write(texto)

    def guardar_token(self):
        refresh = "refresh-dropbox" if self.proveedor == "dropbox" else "refresh-gdrive"
        with open(os.path.join(self.dir, "tokens.json"), "w", encoding="utf-8") as f:
            json.dump({self.proveedor: {"refresh_token": refresh}}, f)

    def crear(self, relativa, datos, antiguedad=0):
        ruta = os.path.join(self.origen, *relativa.split("/"))
        if not os.path.isdir(os.path.dirname(ruta)):
            os.makedirs(os.path.dirname(ruta))
        with open(ruta, "wb") as f:
            f.write(datos)
        if antiguedad:
            marca = time.time() - antiguedad
            os.utime(ruta, (marca, marca))
        return ruta

    def ejecutar(self, *args):
        codigo = subir_nube.main(["-c", self.ini] + list(args))
        subir_nube.configurar_registro(False)  # cierra el archivo de registro
        return codigo

    def estado(self):
        ruta = os.path.join(self.dir, "estado.json")
        if not os.path.exists(ruta):
            return {}
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)["archivos"]


class PruebasHash(unittest.TestCase):
    def test_hash_dropbox_coincide_con_referencia(self):
        for tamano in (0, 1, 4 * MB - 1, 4 * MB, 4 * MB + 1, 9 * MB + 123):
            datos = datos_aleatorios(tamano)
            calculador = subir_nube.HashDropbox()
            # Trozos de tamaños irregulares para cruzar los límites de bloque.
            posicion, paso = 0, 1
            while posicion < tamano:
                calculador.update(datos[posicion:posicion + paso])
                posicion += paso
                paso = paso * 7 % (3 * MB) + 1
            self.assertEqual(calculador.hexdigest(), hash_dropbox(datos), tamano)

    def test_lector_calcula_hash_aunque_se_reenvien_trozos(self):
        datos = datos_aleatorios(1000)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(datos)
        self.addCleanup(os.remove, f.name)
        with subir_nube.LectorArchivo(f.name, hashlib.md5) as lector:
            lector.leer(0, 400)
            lector.leer(200, 400)    # reenvío parcial
            lector.leer(600, 400)
            self.assertTrue(lector.en_orden)
            self.assertEqual(lector.hash_hex(1000), hashlib.md5(datos).hexdigest())
        with subir_nube.LectorArchivo(f.name, hashlib.md5) as lector:
            lector.leer(0, 100)
            lector.leer(500, 500)    # salto: obliga a releer el archivo completo
            self.assertFalse(lector.en_orden)
            self.assertEqual(lector.hash_hex(1000), hashlib.md5(datos).hexdigest())


class PruebasDropbox(BasePrueba):
    proveedor = "dropbox"

    def test_sube_archivos_pequenos_y_grandes(self):
        pequeno = datos_aleatorios(300 * 1024, 1)
        grande = datos_aleatorios(5 * MB + 777, 2)
        self.crear("pequeno.bak", pequeno)
        self.crear("grande.BAK", grande)
        self.crear("notas.txt", b"no se sube")
        self.assertEqual(self.ejecutar(), 0)

        archivos = self.nube.dbx_archivos
        self.assertEqual(sorted(archivos), ["/respaldos/sql/grande.bak", "/respaldos/sql/pequeno.bak"])
        self.assertEqual(archivos["/respaldos/sql/pequeno.bak"][1], pequeno)
        self.assertEqual(archivos["/respaldos/sql/grande.bak"][1], grande)
        self.assertEqual(self.nube.contar("POST", "/2/files/upload_session/start"), 1)
        self.assertEqual(self.nube.contar("POST", "/2/files/upload_session/append_v2"), 4)
        self.assertEqual(self.nube.contar("POST", "/2/files/upload_session/finish"), 1)
        self.assertEqual(len(self.estado()), 2)

        # Segunda ejecución: nada cambió, no se sube nada.
        antes = len(self.nube.peticiones)
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(len(self.nube.peticiones), antes)

    def test_archivo_modificado_se_vuelve_a_subir(self):
        ruta = self.crear("base.bak", b"version 1", antiguedad=100)
        self.assertEqual(self.ejecutar(), 0)
        with open(ruta, "wb") as f:
            f.write(b"version 2, mas larga")
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.nube.dbx_archivos["/respaldos/sql/base.bak"][1], b"version 2, mas larga")

    def test_reintenta_error_temporal_del_servidor(self):
        datos = datos_aleatorios(3 * MB, 3)
        self.crear("a.bak", datos)
        self.nube.fallar("POST", "/2/files/upload_session/append_v2", estado=500, veces=2)
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.nube.dbx_archivos["/respaldos/sql/a.bak"][1], datos)
        self.assertEqual(self.esperas, [2, 4])

    def test_respuesta_perdida_se_corrige_con_incorrect_offset(self):
        datos = datos_aleatorios(3 * MB + 5, 4)
        self.crear("a.bak", datos)
        # Dropbox recibe el bloque pero la respuesta no llega: el reintento recibe incorrect_offset.
        self.nube.fallar("POST", "/2/files/upload_session/append_v2", estado=503, despues=True)
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.nube.dbx_archivos["/respaldos/sql/a.bak"][1], datos)

    def test_respeta_retry_after_en_429(self):
        self.crear("a.bak", b"x" * 10)
        self.nube.fallar("POST", "/2/files/upload", estado=429, cabeceras={"Retry-After": "7"},
                         cuerpo={"error_summary": "too_many_write_operations/"})
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.esperas, [7])
        self.assertIn("/respaldos/sql/a.bak", self.nube.dbx_archivos)

    def test_renueva_token_caducado(self):
        self.crear("a.bak", b"uno", antiguedad=100)
        self.crear("b.bak", b"dos")
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.nube.tokens_emitidos, 1)
        # El servicio invalida el token: la siguiente subida obtiene uno nuevo y sigue.
        self.crear("c.bak", b"tres")
        self.nube.token_valido = "otro"
        self.assertEqual(self.ejecutar(), 0)
        self.assertIn("/respaldos/sql/c.bak", self.nube.dbx_archivos)

    def test_sin_autorizacion_termina_con_codigo_2(self):
        with open(os.path.join(self.dir, "tokens.json"), "w") as f:
            json.dump({"dropbox": {"refresh_token": "revocado"}}, f)
        self.crear("a.bak", b"x")
        self.assertEqual(self.ejecutar(), 2)
        self.assertEqual(self.nube.dbx_archivos, {})

    def test_falta_permiso_en_la_app_detiene_todo(self):
        self.crear("a.bak", b"x")
        self.crear("b.bak", b"y")
        self.nube.fallar("POST", "/2/files/upload", estado=401, veces=10, cuerpo={
            "error_summary": "missing_scope/",
            "error": {".tag": "missing_scope", "required_scope": "files.content.write"}})
        self.assertEqual(self.ejecutar(), 2)
        self.assertEqual(self.nube.contar("POST", "/2/files/upload"), 2)  # un solo archivo, con un reintento

    def test_autorizar_guarda_refresh_token(self):
        os.remove(os.path.join(self.dir, "tokens.json"))
        salida = io.StringIO()
        with mock.patch("builtins.input", return_value=" codigo-ok "), mock.patch("sys.stdout", salida):
            self.assertEqual(self.ejecutar("autorizar"), 0)
        self.assertIn("token_access_type=offline", salida.getvalue())
        with open(os.path.join(self.dir, "tokens.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["dropbox"]["refresh_token"], "refresh-dropbox")

    def test_probar(self):
        self.assertEqual(self.ejecutar("probar"), 0)

    def test_simular_no_sube(self):
        self.crear("a.bak", b"x")
        self.assertEqual(self.ejecutar("subir", "--simular"), 0)
        self.assertEqual(self.nube.dbx_archivos, {})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "estado.json")))

    def test_archivos_recientes_quedan_para_despues(self):
        with open(self.ini, encoding="utf-8") as f:
            texto = f.read().replace("antiguedad_minima_minutos = 0", "antiguedad_minima_minutos = 10")
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write(texto)
        self.crear("viejo.bak", b"listo", antiguedad=3600)
        self.crear("nuevo.bak", b"escribiendose")
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(list(self.nube.dbx_archivos), ["/respaldos/sql/viejo.bak"])

    def test_recursivo_mantiene_subcarpetas(self):
        self.escribir_ini(recursivo="si")
        self.crear("raiz.bak", b"1")
        self.crear("Ventas/diario.trn", b"2")
        self.crear("Ventas/2026/enero.bak", "ñandú".encode("utf-8"))
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(sorted(self.nube.dbx_archivos), [
            "/respaldos/sql/raiz.bak", "/respaldos/sql/ventas/2026/enero.bak", "/respaldos/sql/ventas/diario.trn"])

    def test_borrar_local_tras_subir(self):
        self.escribir_ini(borrar="si")
        ruta = self.crear("a.bak", datos_aleatorios(2 * MB + 1))
        self.assertEqual(self.ejecutar(), 0)
        self.assertFalse(os.path.exists(ruta))
        self.assertEqual(self.estado(), {})

    def test_no_borra_si_la_suma_no_coincide(self):
        self.escribir_ini(borrar="si")
        ruta = self.crear("a.bak", b"contenido")
        with mock.patch.object(subir_nube.HashDropbox, "hexdigest", return_value="0" * 64):
            self.assertEqual(self.ejecutar(), 1)
        self.assertTrue(os.path.exists(ruta))
        self.assertEqual(self.estado(), {})

    def test_no_sube_sus_propios_archivos(self):
        self.escribir_ini()
        with open(self.ini, encoding="utf-8") as f:
            texto = f.read().replace("origen = origen", "origen = .")
            texto = texto.replace("patrones = *.bak; *.trn", "patrones = *")
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write(texto)
        with open(os.path.join(self.dir, "respaldo.bak"), "wb") as f:
            f.write(b"x")
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.ejecutar(), 0)  # ahora ya existen estado.json y el registro
        # Solo el respaldo: ni subir_nube.ini, ni tokens.json, ni el registro, ni el estado.
        self.assertEqual(sorted(self.nube.dbx_archivos), ["/respaldos/sql/respaldo.bak"])


class PruebasGoogleDrive(BasePrueba):
    proveedor = "gdrive"

    def test_sube_y_crea_carpetas_una_sola_vez(self):
        self.escribir_ini(recursivo="si")
        a = datos_aleatorios(2 * MB + 10, 5)
        self.crear("a.bak", a)
        self.crear("sub/b.bak", b"b")
        self.crear("sub/c.trn", b"")
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.nube.gd_ruta("Respaldos/SQL/a.bak")["datos"], a)
        self.assertEqual(self.nube.gd_ruta("Respaldos/SQL/sub/b.bak")["datos"], b"b")
        self.assertEqual(self.nube.gd_ruta("Respaldos/SQL/sub/c.trn")["datos"], b"")
        carpetas = [e for e in self.nube.gd_elementos.values() if e["mimeType"].endswith("folder")]
        self.assertEqual(sorted(e["name"] for e in carpetas), ["Respaldos", "SQL", "sub"])
        self.assertEqual(self.nube.gd_ruta("Respaldos/SQL/a.bak")["modifiedTime"][-1], "Z")

    def test_archivo_modificado_reemplaza_el_existente(self):
        ruta = self.crear("a.bak", b"version 1", antiguedad=100)
        self.assertEqual(self.ejecutar(), 0)
        identificador = self.nube.gd_ruta("Respaldos/SQL/a.bak")["id"]
        with open(ruta, "wb") as f:
            f.write(b"version 2")
        self.assertEqual(self.ejecutar(), 0)
        elemento = self.nube.gd_ruta("Respaldos/SQL/a.bak")
        self.assertEqual(elemento["id"], identificador)
        self.assertEqual(elemento["datos"], b"version 2")
        self.assertEqual(self.nube.contar("PATCH", "/upload/drive/v3/files/"), 1)

    def test_reanuda_bloque_cuya_respuesta_se_perdio(self):
        datos = datos_aleatorios(3 * MB + 100, 6)
        self.crear("a.bak", datos)
        self.nube.fallar("PUT", "/upload/sesion/", estado=503, despues=True)
        self.nube.fallar("PUT", "/upload/sesion/", estado=502, veces=1)
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.nube.gd_ruta("Respaldos/SQL/a.bak")["datos"], datos)

    def test_nombre_con_comilla(self):
        self.crear("O'Brien.bak", b"1", antiguedad=100)
        self.assertEqual(self.ejecutar(), 0)
        ruta = self.crear("O'Brien.bak", b"22")
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(self.nube.gd_ruta("Respaldos/SQL/O'Brien.bak")["datos"], b"22")
        self.assertTrue(os.path.exists(ruta))

    def test_limite_de_uso_403_se_reintenta(self):
        self.crear("a.bak", b"x")
        self.nube.fallar("GET", "/drive/v3/files", estado=403, cuerpo={
            "error": {"code": 403, "message": "Rate limit", "errors": [{"reason": "userRateLimitExceeded"}]}})
        self.assertEqual(self.ejecutar(), 0)
        self.assertEqual(len(self.esperas), 1)

    def test_error_permanente_no_detiene_los_demas(self):
        self.crear("a.bak", b"a")
        self.crear("b.bak", b"b")
        self.nube.fallar("POST", "/upload/drive/v3/files", estado=403, cuerpo={
            "error": {"code": 403, "message": "The user's Drive storage quota has been exceeded.",
                      "errors": [{"reason": "storageQuotaExceeded"}]}})
        self.assertEqual(self.ejecutar(), 1)
        self.assertEqual(len(self.estado()), 1)

    def test_autorizar_con_codigo_de_dispositivo(self):
        os.remove(os.path.join(self.dir, "tokens.json"))
        salida = io.StringIO()
        with mock.patch("sys.stdout", salida):
            self.assertEqual(self.ejecutar("autorizar"), 0)
        self.assertIn("ABCD-EFGH", salida.getvalue())
        with open(os.path.join(self.dir, "tokens.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["gdrive"]["refresh_token"], "refresh-gdrive")

    def test_probar(self):
        self.assertEqual(self.ejecutar("probar"), 0)


class PruebasConfiguracion(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subir_nube_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_ini_en_ansi_con_comillas_y_acentos(self):
        ruta = os.path.join(self.dir, "subir_nube.ini")
        texto = (u'[general]\r\nproveedor = Google Drive\r\norigen = "D:\\Copias de Año"\r\n'
                 u"recursivo = Sí\r\npatrones = *.bak,*.trn\r\ncertificados =\r\n")
        with open(ruta, "wb") as f:
            f.write(texto.encode("cp1252"))
        conf = subir_nube.Configuracion(ruta)
        self.assertEqual(conf.proveedor, "gdrive")
        self.assertTrue(conf.origen.endswith(u"Copias de Año"))
        self.assertTrue(conf.recursivo)
        self.assertEqual(conf.patrones, ["*.bak", "*.trn"])
        self.assertEqual(conf.certificados, "")
        self.assertEqual(conf.tamano_bloque, 8 * MB)

    def test_ini_de_ejemplo_es_valido(self):
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ruta = os.path.join(self.dir, "subir_nube.ini")
        shutil.copy(os.path.join(raiz, "subir_nube.ejemplo.ini"), ruta)
        open(os.path.join(self.dir, "cacert.pem"), "w").close()
        conf = subir_nube.Configuracion(ruta)
        self.assertEqual(conf.proveedor, "dropbox")
        self.assertEqual(conf.patrones, ["*.bak", "*.trn"])
        self.assertEqual(conf.antiguedad_minima, 600)
        self.assertFalse(conf.borrar_local)
        with self.assertRaises(subir_nube.ErrorFatal):
            conf.credencial("dropbox", "app_key")  # vacío en el ejemplo

    def test_valor_invalido(self):
        ruta = os.path.join(self.dir, "subir_nube.ini")
        with open(ruta, "w") as f:
            f.write("[general]\nproveedor = dropbox\nreintentos = muchos\ncertificados =\n")
        with self.assertRaises(subir_nube.ErrorFatal):
            subir_nube.Configuracion(ruta)

    def test_falta_cacert(self):
        ruta = os.path.join(self.dir, "subir_nube.ini")
        with open(ruta, "w") as f:
            f.write("[general]\nproveedor = dropbox\n")
        with self.assertRaises(subir_nube.ErrorFatal) as error:
            subir_nube.Configuracion(ruta)
        self.assertIn("curl.se", str(error.exception))

    def test_proxy(self):
        self.assertIsNone(subir_nube.analizar_proxy(""))
        host, puerto, cabeceras = subir_nube.analizar_proxy("http://ana:cl%40ve@proxy.local:3128")
        self.assertEqual((host, puerto), ("proxy.local", 3128))
        self.assertEqual(cabeceras["Proxy-Authorization"], "Basic YW5hOmNsQHZl")
        self.assertEqual(subir_nube.analizar_proxy("10.0.0.1:8080")[:2], ("10.0.0.1", 8080))

    def test_bloqueo_impide_dos_ejecuciones(self):
        ruta = os.path.join(self.dir, "estado.json.lock")
        primero = subir_nube.Bloqueo(ruta)
        self.assertTrue(primero.adquirir())
        self.assertFalse(subir_nube.Bloqueo(ruta).adquirir())
        primero.liberar()
        segundo = subir_nube.Bloqueo(ruta)
        self.assertTrue(segundo.adquirir())
        segundo.liberar()


if __name__ == "__main__":
    unittest.main()
