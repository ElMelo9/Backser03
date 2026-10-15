# -*- coding: utf-8 -*-
"""Pruebas de la ventana (carontesync_gui.pyw). Necesitan tkinter y una pantalla;
si no las hay, se omiten."""

import importlib.machinery
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

import carontesync as sn  # noqa: E402

try:
    import tkinter
    _raiz = tkinter.Tk()
    _raiz.destroy()
    gui = importlib.machinery.SourceFileLoader("carontesync_gui",
                                               os.path.join(RAIZ, "carontesync_gui.pyw")).load_module()
    MOTIVO = None
except Exception as e:      # sin tkinter o sin pantalla
    gui = None
    MOTIVO = "sin interfaz gráfica: {0}".format(e)


@unittest.skipIf(gui is None, MOTIVO)
class PruebasVentana(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="carontesync_gui_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ini = os.path.join(self.dir, "carontesync.ini")
        open(os.path.join(self.dir, "cacert.pem"), "w").close()
        self.raiz = tkinter.Tk()
        self.app = gui.Aplicacion(self.raiz, self.ini)
        self.addCleanup(self.app.salir)      # también quita el icono de la bandeja
        self.raiz.update()

    def test_formulario_nuevo_se_guarda_y_valida(self):
        self.assertFalse(os.path.exists(self.ini))
        self.assertEqual(self.app.var("general", "patrones").get(), "*.bak;*.trn")
        self.app.var("general", "origen").set(self.dir)
        self.app.var("general", "proveedor").set("gdrive")
        self.app.var("gdrive", "client_id").set("mi-cliente")
        self.app.var("programacion", "activa", True).set(True)
        self.app.var("programacion", "frecuencia").set("semanal")
        for dia in ("LUN", "JUE"):
            self.app.dias[dia].set(True)
        for dia in ("MAR", "MIE", "VIE"):
            self.app.dias[dia].set(False)
        self.app.hora_h.set("22")
        self.app.hora_m.set("5")
        self.assertTrue(self.app.guardar(silencioso=True))
        conf = sn.Configuracion(self.ini)
        self.assertEqual(conf.proveedor, "gdrive")
        self.assertEqual(conf.credencial("gdrive", "client_id"), "mi-cliente")
        self.assertEqual(conf.programacion.dias, ["LUN", "JUE"])
        self.assertEqual(conf.programacion.hora, "22:05")
        # Se vuelve a cargar igual.
        self.app.cargar_formulario()
        self.assertEqual(self.app.var("general", "proveedor").get(), "gdrive")
        self.assertTrue(self.app.dias["JUE"].get())
        self.assertFalse(self.app.dias["MAR"].get())

    def test_valor_invalido_no_se_guarda(self):
        self.app.var("general", "reintentos").set("muchos")
        with mock.patch.object(gui.messagebox, "showerror") as error:
            self.assertFalse(self.app.guardar(silencioso=True))
        self.assertTrue(error.called)
        self.assertFalse(os.path.exists(self.ini))

    def test_muestra_avance_e_historial(self):
        GB = 1024 ** 3
        sn.escribir_json(os.path.join(self.dir, "actividad.json"), {
            "activo": True, "actualizado": time.time(), "inicio": time.time() - 60, "origen": "programada",
            "fase": "Subiendo", "archivo": "base.bak", "indice": 1, "cantidad": 1,
            "archivo_hecho": 3 * GB, "archivo_total": 15 * GB, "lote_hecho": 3 * GB, "lote_total": 15 * GB,
            "velocidad": 2 * 1024 * 1024})
        with open(os.path.join(self.dir, "historial.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"tipo": "archivo", "fecha": "2026-10-14 03:30:00", "archivo": "a.bak",
                                "tamano": GB, "segundos": 512, "resultado": "subido", "verificado": True}) + "\n")
            f.write(json.dumps({"tipo": "ejecucion", "fecha": "2026-10-14 03:30:00", "origen": "programada",
                                "resultado": "correcta", "subidos": 1, "bytes": GB, "detalle": "ok"}) + "\n")
        self.app._mostrar_actividad()
        self.app.cargar_historial(True)
        self.assertEqual(self.app.lbl_porcentaje.cget("text"), "20.0 %")
        self.assertEqual(self.app.lbl_cantidad.cget("text"), "3.0 GB de 15.0 GB")
        self.assertEqual(self.app.lbl_velocidad.cget("text"), "2.0 MB/s")
        self.assertEqual(self.app.lbl_restante.cget("text"), "1 h 42 min")
        self.assertEqual(self.app.lbl_lateral.cget("text"), "Subiendo 20 %")
        self.assertEqual(str(self.app.btn_subir.cget("state")), "disabled")
        self.assertEqual(str(self.app.btn_detener.cget("state")), "normal")
        self.assertEqual(len(self.app.arbol_archivos.get_children()), 1)
        fila = self.app.arbol_archivos.item(self.app.arbol_archivos.get_children()[0], "values")
        self.assertEqual(fila[1], "a.bak")
        self.assertEqual(fila[4], "2.0 MB/s")

        # Detener deja la solicitud que atiende el motor.
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            self.app.detener()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "detener.solicitud")))

    def test_subir_ahora_lanza_el_motor(self):
        os.makedirs(os.path.join(self.dir, "origen"))
        self.app.var("general", "origen").set(os.path.join(self.dir, "origen"))
        self.assertTrue(self.app.guardar(silencioso=True))
        sn.AlmacenTokens(os.path.join(self.dir, "tokens.json")).guardar("dropbox", "x")
        with mock.patch.object(gui.subprocess, "Popen") as popen:
            self.app.subir_ahora()
        argumentos = popen.call_args[0][0]
        self.assertEqual(argumentos[1:], [os.path.join(gui.CARPETA, "carontesync.py"), "subir", "-c",
                                          os.path.abspath(self.ini), "--origen", "manual"])
        self.assertTrue(argumentos[0].lower().endswith(("pythonw.exe", "python.exe", "python", "python3")))

    def test_subir_ahora_sin_cuenta_avisa(self):
        os.makedirs(os.path.join(self.dir, "origen"))
        self.app.var("general", "origen").set(os.path.join(self.dir, "origen"))
        with mock.patch.object(gui.messagebox, "showerror") as error, \
                mock.patch.object(gui.subprocess, "Popen") as popen:
            self.app.subir_ahora()
        self.assertFalse(popen.called)
        self.assertIn("vincule la cuenta", error.call_args[0][1])

    def escribir_actividad(self, **datos):
        base = {"activo": True, "actualizado": time.time(), "inicio": time.time() - 60, "origen": "programada",
                "fase": "Subiendo", "archivo": "base.bak", "indice": 1, "cantidad": 1, "archivo_hecho": 50,
                "archivo_total": 100, "lote_hecho": 50, "lote_total": 100, "velocidad": 1024.0}
        base.update(datos)
        sn.escribir_json(os.path.join(self.dir, "actividad.json"), base)

    def test_navegacion_lateral(self):
        for clave in ("configuracion", "programacion", "historial", "estado"):
            self.app.mostrar_pagina(clave)
            self.raiz.update()
            self.assertEqual(self.app.pagina_actual, clave)
            visibles = [n for n, marco in self.app.paginas.items() if marco.winfo_manager()]
            self.assertEqual(visibles, [clave])

    def test_sin_subida_detener_desactivado(self):
        self.app._mostrar_actividad()
        self.assertEqual(str(self.app.btn_detener.cget("state")), "disabled")
        self.assertEqual(self.app.lbl_lateral.cget("text"), "En espera")

    @unittest.skipUnless(gui.bandeja is not None if gui else False, "solo en Windows")
    def test_cerrar_deja_el_icono_junto_al_reloj(self):
        self.assertIsNotNone(self.app.icono_bandeja)
        with mock.patch.object(self.app.icono_bandeja, "aviso") as aviso:
            self.app.cerrar()
            self.raiz.update()
        self.assertEqual(self.raiz.state(), "withdrawn")
        self.assertIn("sigue", aviso.call_args[0][0])
        self.app.mostrar()
        self.raiz.update()
        self.assertEqual(self.raiz.state(), "normal")

    @unittest.skipUnless(gui.bandeja is not None if gui else False, "solo en Windows")
    def test_bandeja_muestra_el_avance_y_avisa_al_terminar(self):
        icono = self.app.icono_bandeja
        self.escribir_actividad()
        self.app._mostrar_actividad()
        self.assertIn("subiendo base.bak (50 %", icono.descripcion)
        self.assertEqual(icono.hicon, icono._icono(gui.ICONO_SUBIENDO))
        self.escribir_actividad(activo=False, resultado="correcta", detalle="Resumen: 1 subidos.",
                                fin=time.time())
        with mock.patch.object(icono, "aviso") as aviso:
            self.app._mostrar_actividad()
        self.assertEqual(aviso.call_args[0][0], "Respaldos en la nube")
        self.assertIn("última subida correcta", icono.descripcion)
        self.assertEqual(icono.hicon, icono._icono(gui.ICONO))
        # El menú de la bandeja: "Detener" solo se habilita durante una subida.
        nombres = [e[0] for e in icono.elementos if e]
        self.assertEqual(nombres, ["Abrir CaronteSync", "Subir ahora", "Detener la subida", "Salir"])
        self.assertFalse(icono.elementos[2][2]())

    @unittest.skipUnless(os.name == "nt", "solo en Windows")
    def test_inicio_con_windows(self):
        anterior = gui.inicio_con_windows()
        self.addCleanup(gui.cambiar_inicio_con_windows, anterior)
        gui.cambiar_inicio_con_windows(True)
        self.assertTrue(gui.inicio_con_windows())
        self.app.cargar_formulario()
        self.assertTrue(self.app.var_inicio.get())
        gui.cambiar_inicio_con_windows(False)
        self.assertFalse(gui.inicio_con_windows())


if __name__ == "__main__":
    unittest.main()
