# -*- coding: utf-8 -*-
"""Pruebas de lo que solo pasa en Windows Server: rutas largas y con acentos, archivos
bloqueados por SQL Server, atributos de solo lectura, archivos abiertos por otro
proceso, bloqueo entre procesos, el Programador de tareas, pythonw sin consola y el
icono de la bandeja. Se omiten fuera de Windows; aquí se ejecutan en Windows Server
2003 emulado con Wine y el Python 3.4.4 oficial."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carontesync  # noqa: E402
from tests.test_carontesync import BasePrueba, datos_aleatorios  # noqa: E402

EN_WINDOWS = os.name == "nt"
if EN_WINDOWS:
    import ctypes
    import ctypes.wintypes
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(EN_WINDOWS, "solo en Windows")
class PruebasArchivosWindows(BasePrueba):
    proveedor = "dropbox"

    def test_rutas_de_mas_de_260_caracteres(self):
        self.escribir_ini(recursivo="si")
        carpeta = self.origen
        for i in range(6):
            carpeta = os.path.join(carpeta, "Respaldos del servidor de produccion nivel {0:02d}".format(i))
        ruta = os.path.join(carpeta, "Base_de_datos_ventas_completa_2026.bak")
        self.assertGreater(len(ruta), 300)
        os.makedirs(carontesync.ruta_extendida(carpeta))
        with open(carontesync.ruta_extendida(ruta), "wb") as f:
            f.write(b"respaldo profundo")
        self.assertEqual(self.ejecutar(), 0)
        subidos = list(self.nube.dbx_archivos.values())
        self.assertEqual(len(subidos), 1)
        self.assertEqual(subidos[0][1], b"respaldo profundo")
        self.assertTrue(subidos[0][0].endswith("/Base_de_datos_ventas_completa_2026.bak"))

    def test_rutas_con_espacios_y_acentos(self):
        self.escribir_ini(recursivo="si")
        self.crear(u"Copias de Seguridad/Año 2026 - Diseño/Contabilidad ñandú.bak", b"x" * 1000)
        self.assertEqual(self.ejecutar(), 0)
        self.assertIn(u"/respaldos/sql/copias de seguridad/año 2026 - diseño/contabilidad ñandú.bak",
                      self.nube.dbx_archivos)

    def test_archivo_bloqueado_por_otro_programa(self):
        """SQL Server abre el .bak en exclusiva mientras lo escribe."""
        ruta = self.crear("en_uso.bak", b"escribiendo")
        self.crear("listo.bak", b"terminado")
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = ctypes.c_void_p
        GENERIC_WRITE, OPEN_EXISTING = 0x40000000, 3
        manejador = kernel32.CreateFileW(ruta, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)  # sin compartir
        self.assertNotEqual(manejador, ctypes.c_void_p(-1).value)
        try:
            self.assertEqual(self.ejecutar(), 1)        # error en ese archivo, sin romperse
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(manejador))
        self.assertEqual(list(self.nube.dbx_archivos), ["/respaldos/sql/listo.bak"])
        errores = [e for e in self.historial("archivo") if e["resultado"] == "error"]
        self.assertEqual(errores[0]["archivo"], "en_uso.bak")
        self.assertEqual(self.ejecutar(), 0)            # liberado: se sube en la siguiente ejecución
        self.assertIn("/respaldos/sql/en_uso.bak", self.nube.dbx_archivos)

    def test_borrar_archivo_de_solo_lectura(self):
        self.escribir_ini(borrar="si")
        ruta = self.crear("solo_lectura.bak", datos_aleatorios(3000))
        os.chmod(ruta, stat.S_IREAD)
        self.assertEqual(self.ejecutar(), 0)
        self.assertFalse(os.path.exists(ruta))

    def test_estado_abierto_un_momento_por_otro_proceso(self):
        """En Windows, renombrar sobre un archivo abierto falla: se reintenta."""
        ruta = os.path.join(self.dir, "estado.json")
        carontesync.escribir_json(ruta, {"v": 1})
        abierto = open(ruta, "r")
        threading.Timer(0.3, abierto.close).start()
        carontesync.escribir_json(ruta, {"v": 2})
        with open(ruta) as f:
            self.assertEqual(json.load(f), {"v": 2})

    def test_estado_bloqueado_mucho_tiempo_no_detiene_la_subida(self):
        self.crear("a.bak", b"uno")
        self.assertEqual(self.ejecutar(), 0)
        self.crear("b.bak", b"dos")
        abierto = open(os.path.join(self.dir, "estado.json"), "r")
        try:
            self.assertEqual(self.ejecutar(), 0)       # avisa en el registro, pero sube
        finally:
            abierto.close()
        self.assertIn("/respaldos/sql/b.bak", self.nube.dbx_archivos)


@unittest.skipUnless(EN_WINDOWS, "solo en Windows")
class PruebasSistemaWindows(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="carontesync_win_")

    def test_rutas_unc(self):
        self.assertEqual(carontesync.ruta_extendida("\\\\servidor\\respaldos\\a.bak"),
                         "\\\\?\\UNC\\servidor\\respaldos\\a.bak")
        self.assertEqual(carontesync.clave_ruta("\\\\?\\UNC\\Servidor\\Respaldos\\A.bak"),
                         carontesync.clave_ruta("\\\\servidor\\respaldos\\a.bak"))
        self.assertEqual(carontesync.clave_ruta("\\\\?\\C:\\CaronteSync\\Tokens.json"),
                         carontesync.clave_ruta("c:\\caronteSync\\tokens.json"))

    def test_bloqueo_entre_procesos(self):
        ruta = os.path.join(self.dir, "estado.json.lock")
        codigo = ("import sys, time; sys.path.insert(0, {0!r}); import carontesync as c; "
                  "b = c.Bloqueo({1!r}); print(b.adquirir()); sys.stdout.flush(); time.sleep(60)").format(RAIZ, ruta)
        otro = subprocess.Popen([sys.executable, "-c", codigo], stdout=subprocess.PIPE)
        try:
            self.assertEqual(otro.stdout.readline().strip(), b"True")
            self.assertFalse(carontesync.Bloqueo(ruta).adquirir())
        finally:
            otro.kill()
            otro.wait()
        bloqueo = carontesync.Bloqueo(ruta)     # Windows lo libera al morir el proceso
        self.assertTrue(bloqueo.adquirir())
        bloqueo.liberar()

    def test_comando_de_la_tarea(self):
        comando = carontesync.comando_tarea(os.path.join(self.dir, "carontesync.ini"))
        self.assertTrue(comando.startswith('"'))
        self.assertIn("pythonw.exe", comando.lower())
        self.assertLessEqual(len(comando), carontesync.LIMITE_COMANDO_TAREA)
        # Así le llega a schtasks: las comillas internas escapadas, como pide su documentación.
        linea = subprocess.list2cmdline(["schtasks", "/tr", comando])
        self.assertIn('\\"', linea)

    def test_ruta_de_instalacion_demasiado_larga(self):
        p = carontesync.Programacion(activa=True)
        with mock.patch.object(carontesync, "comando_tarea", return_value="x" * 300), \
                mock.patch.object(carontesync, "tarea_existe", return_value=False):
            with self.assertRaises(carontesync.ErrorFatal) as error:
                carontesync.aplicar_programacion(p, os.path.join(self.dir, "c.ini"))
        self.assertIn("demasiado larga", str(error.exception))

    def test_schtasks_se_ejecuta_sin_ventana_y_se_decodifica(self):
        codigo, texto = carontesync.ejecutar_schtasks(["schtasks", "/?"])
        self.assertIsInstance(codigo, int)
        self.assertIsInstance(texto, str)

    def test_motor_con_pythonw_sin_consola(self):
        """La tarea programada usa pythonw.exe: sin consola, sys.stderr es None."""
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        ini = os.path.join(self.dir, "no_existe.ini")
        resultado = subprocess.call([pythonw, os.path.join(RAIZ, "carontesync.py"), "subir", "-c", ini],
                                    creationflags=0x08000000)
        self.assertEqual(resultado, 2)      # falta la configuración: código 2, sin colgarse ni fallar

    def test_version_de_windows_y_formato_de_hora(self):
        if sys.getwindowsversion().major < 6:
            self.assertTrue(carontesync.es_windows_antiguo())
            argumentos = carontesync.argumentos_crear_tarea(carontesync.Programacion(activa=True), "x", True)
            self.assertIn("03:30:00", argumentos)
            self.assertNotIn("/f", argumentos)


@unittest.skipUnless(EN_WINDOWS, "solo en Windows")
class PruebasBandeja(unittest.TestCase):
    def setUp(self):
        import carontesync_bandeja
        self.b = carontesync_bandeja
        self.llamadas = []
        self.icono = carontesync_bandeja.IconoBandeja(
            os.path.join(RAIZ, "carontesync.ico"), "CaronteSync", lambda: self.llamadas.append("abrir"),
            [("Abrir", lambda: self.llamadas.append("abrir"), None),
             ("Detener", lambda: self.llamadas.append("detener"), lambda: False),
             None,
             ("Salir", lambda: self.llamadas.append("salir"), None)])
        self.addCleanup(self.icono.quitar)

    def bombear(self):
        """Procesa los mensajes de Windows pendientes (lo que en la aplicación hace Tk)."""
        mensaje = ctypes.wintypes.MSG()
        user32 = ctypes.windll.user32
        while user32.PeekMessageW(ctypes.byref(mensaje), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(mensaje))
            user32.DispatchMessageW(ctypes.byref(mensaje))

    def test_icono_descripcion_y_aviso(self):
        self.assertTrue(self.icono.hwnd)
        self.icono.actualizar("CaronteSync: subiendo base.bak (45 %, 1.3 MB/s)",
                              os.path.join(RAIZ, "carontesync_subiendo.ico"))
        self.assertIn("45 %", self.icono.descripcion)
        self.assertTrue(self.icono.aviso("Respaldos en la nube", "Resumen: 1 subidos (15.2 GB)."))
        self.icono.actualizar("x" * 300)            # Windows admite 127 caracteres: se recorta
        self.assertEqual(len(self.icono.descripcion), 127)

    def test_menu(self):
        menu = self.icono.crear_menu()
        try:
            self.assertEqual(ctypes.windll.user32.GetMenuItemCount(menu), 4)
            MF_BYPOSITION, MF_GRAYED = 0x400, 0x1
            estado = ctypes.windll.user32.GetMenuState(menu, 1, MF_BYPOSITION)
            self.assertTrue(estado & MF_GRAYED)      # "Detener" deshabilitado
        finally:
            ctypes.windll.user32.DestroyMenu(menu)
        self.icono.ejecutar_elemento(4)
        self.icono.ejecutar_elemento(3)              # separador: no hace nada
        self.assertEqual(self.llamadas, ["salir"])

    def test_clic_en_el_icono_abre_la_ventana(self):
        ctypes.windll.user32.PostMessageW(self.icono.hwnd, self.b.WM_BANDEJA, 1, self.b.WM_LBUTTONUP)
        self.bombear()
        self.assertEqual(self.llamadas, ["abrir"])

    def test_segunda_instancia_muestra_la_primera(self):
        self.assertTrue(self.b.mostrar_instancia_abierta())
        self.bombear()
        self.assertEqual(self.llamadas, ["abrir"])

    def test_explorador_reiniciado_vuelve_a_poner_el_icono(self):
        mensaje = ctypes.windll.user32.RegisterWindowMessageW("TaskbarCreated")
        with mock.patch.object(self.icono, "_notificar", wraps=self.icono._notificar) as notificar:
            ctypes.windll.user32.PostMessageW(self.icono.hwnd, mensaje, 0, 0)
            self.bombear()
        self.assertEqual(notificar.call_args[0][0], self.b.NIM_ADD)


class PruebasRed(BasePrueba):
    """También fuera de Windows."""

    def test_conexion_con_bufer_grande(self):
        cliente = carontesync.ClienteHttp(timeout=10)
        self.addCleanup(cliente.cerrar)
        r = cliente.peticion("POST", self.nube.base + "/2/users/get_current_account", b"null",
                             {"Authorization": "Bearer nada"})
        self.assertEqual(r.estado, 401)
        conexion = list(cliente._conexiones.values())[0]
        envio = conexion.sock.getsockopt(carontesync.socket.SOL_SOCKET, carontesync.socket.SO_SNDBUF)
        # Windows da los 4 MB pedidos; Linux (y Wine sobre Linux) los recorta a su máximo.
        self.assertGreaterEqual(envio, 200 * 1024)


if __name__ == "__main__":
    unittest.main()
