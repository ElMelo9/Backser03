# -*- coding: utf-8 -*-
"""
SubirNube - ventana para configurar, programar y seguir las subidas a la nube.

Se abre con pythonw.exe (sin consola). La subida en sí la hace siempre el motor
(subir_nube.py) en un proceso aparte, tanto si la lanza esta ventana como si la
lanza el Programador de tareas de Windows: la ventana solo lee actividad.json,
historial.jsonl y el registro para mostrar el avance, así que se puede cerrar
sin interrumpir una subida.
"""

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

CARPETA = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CARPETA)

import subir_nube as sn  # noqa: E402

ORIGENES = {"programada": "tarea programada", "manual": "lanzada desde esta ventana", "consola": "desde la consola"}
CREATE_NO_WINDOW = 0x08000000


def python_sin_consola():
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pythonw if os.path.exists(pythonw) else sys.executable


def fecha_corta(texto):
    """'2026-10-15 03:30:12' -> '15/10/2026 03:30'"""
    try:
        return time.strftime("%d/%m/%Y %H:%M", time.strptime(texto, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return texto or ""


def leer_cola_archivo(ruta, lineas=300):
    """Últimas líneas de un archivo de texto (el registro puede ser grande)."""
    try:
        with open(ruta, "rb") as f:
            f.seek(0, 2)
            tamano = f.tell()
            f.seek(max(0, tamano - 96 * 1024))
            texto = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    partes = texto.splitlines()
    if tamano > 96 * 1024:
        partes = partes[1:]   # la primera puede estar cortada
    return "\n".join(partes[-lineas:])


class Aplicacion(object):
    def __init__(self, raiz, ruta_config):
        self.raiz = raiz
        self.ruta_config = os.path.abspath(ruta_config)
        self.base = os.path.dirname(self.ruta_config)
        self.cola = queue.Queue()
        self.proceso = None
        self.guardados = {}
        self.vars = {}
        self.deteniendo = False
        self._firma_registro = "sin leer"
        self._firma_historial = None
        self._ultima_actividad = {}
        self._temporizadores = {}

        raiz.title("SubirNube - respaldos a la nube")
        raiz.geometry("900x680")
        raiz.minsize(800, 600)
        try:
            raiz.iconbitmap(os.path.join(CARPETA, "subir_nube.ico"))
        except tk.TclError:
            pass
        estilo = ttk.Style()
        estilo.configure("Titulo.TLabel", font=("Tahoma", 12, "bold"))
        estilo.configure("Grande.TLabel", font=("Tahoma", 10, "bold"))
        estilo.configure("Gris.TLabel", foreground="#555555")

        self.cuaderno = ttk.Notebook(raiz)
        self.cuaderno.pack(fill="both", expand=True, padx=8, pady=8)
        self._crear_estado()
        self._crear_configuracion()
        self._crear_programacion()
        self._crear_historial()
        self.cuaderno.bind("<<NotebookTabChanged>>", self._al_cambiar_pestana)

        self.cargar_formulario()
        raiz.protocol("WM_DELETE_WINDOW", self.cerrar)
        self._atender_cola()
        self._refrescar()

    # ------------------------------------------------------------------
    # Utilidades generales
    # ------------------------------------------------------------------

    def ruta_datos(self, nombre):
        return os.path.join(self.base, nombre)

    def var(self, seccion, clave, booleana=False):
        clave_completa = (seccion, clave)
        if clave_completa not in self.vars:
            self.vars[clave_completa] = tk.BooleanVar() if booleana else tk.StringVar()
        return self.vars[clave_completa]

    def en_hilo(self, funcion, al_terminar):
        """Ejecuta funcion() en otro hilo y luego al_terminar(ok, resultado) en la ventana."""
        def trabajo():
            try:
                resultado = (True, funcion())
            except Exception as e:     # se muestra al usuario
                resultado = (False, e)
            self.cola.put(lambda: al_terminar(*resultado))
        hilo = threading.Thread(target=trabajo)
        hilo.daemon = True
        hilo.start()

    def _atender_cola(self):
        try:
            while True:
                self.cola.get_nowait()()
        except queue.Empty:
            pass
        self._temporizadores["cola"] = self.raiz.after(150, self._atender_cola)

    def detener_temporizadores(self):
        for identificador in self._temporizadores.values():
            self.raiz.after_cancel(identificador)
        self._temporizadores.clear()

    def error(self, titulo, e):
        texto = str(e) if isinstance(e, (sn.ErrorFatal, sn.ErrorNube)) else sn.describir_error(e)
        messagebox.showerror(titulo, texto, parent=self.raiz)

    def cerrar(self):
        actividad = sn.leer_actividad(self.ruta_datos("actividad.json"))
        if actividad.get("en_curso"):
            if not messagebox.askyesno(
                    "Cerrar", "Hay una subida en curso. Si cierra la ventana la subida sigue en segundo plano.\n\n"
                    "¿Cerrar la ventana?", parent=self.raiz):
                return
        self.detener_temporizadores()
        self.raiz.destroy()

    # ------------------------------------------------------------------
    # Pestaña Estado
    # ------------------------------------------------------------------

    def _crear_estado(self):
        marco = ttk.Frame(self.cuaderno, padding=12)
        self.cuaderno.add(marco, text="  Estado  ")
        self.lbl_estado = ttk.Label(marco, text="", style="Titulo.TLabel")
        self.lbl_estado.pack(anchor="w")
        self.lbl_archivo = ttk.Label(marco, text="")
        self.lbl_archivo.pack(anchor="w", pady=(8, 2))
        self.barra_archivo = ttk.Progressbar(marco, maximum=1000)
        self.barra_archivo.pack(fill="x")
        fila = ttk.Frame(marco)
        fila.pack(fill="x", pady=(2, 8))
        self.lbl_cantidad = ttk.Label(fila, text="", style="Grande.TLabel")
        self.lbl_cantidad.pack(side="left")
        self.lbl_tiempo = ttk.Label(fila, text="")
        self.lbl_tiempo.pack(side="right")
        self.lbl_lote = ttk.Label(marco, text="")
        self.lbl_lote.pack(anchor="w")
        self.barra_lote = ttk.Progressbar(marco, maximum=1000)
        self.barra_lote.pack(fill="x", pady=(2, 10))

        botones = ttk.Frame(marco)
        botones.pack(fill="x")
        self.btn_subir = ttk.Button(botones, text="Subir ahora", command=self.subir_ahora)
        self.btn_subir.pack(side="left")
        self.btn_detener = ttk.Button(botones, text="Detener", command=self.detener)
        self.btn_detener.pack(side="left", padx=6)
        ttk.Button(botones, text="Ver qué se subiría", command=self.simular).pack(side="left")
        ttk.Button(botones, text="Abrir carpeta de datos", command=self.abrir_carpeta).pack(side="right")

        resumen = ttk.LabelFrame(marco, text=" Resumen ", padding=8)
        resumen.pack(fill="x", pady=10)
        self.lbl_ultima = ttk.Label(resumen, text="", wraplength=820, justify="left")
        self.lbl_ultima.pack(anchor="w")
        self.lbl_proxima = ttk.Label(resumen, text="", wraplength=820, justify="left")
        self.lbl_proxima.pack(anchor="w", pady=(4, 0))

        registro = ttk.LabelFrame(marco, text=" Registro (subir_nube.log) ", padding=4)
        registro.pack(fill="both", expand=True)
        self.txt_registro = tk.Text(registro, height=10, wrap="none", font=("Courier New", 8), state="disabled")
        barra = ttk.Scrollbar(registro, orient="vertical", command=self.txt_registro.yview)
        self.txt_registro.configure(yscrollcommand=barra.set)
        barra.pack(side="right", fill="y")
        self.txt_registro.pack(fill="both", expand=True)

    def _refrescar(self):
        try:
            self._mostrar_actividad()
            self._mostrar_registro()
            if self.cuaderno.index("current") == 3:
                self.cargar_historial()
        finally:
            self._temporizadores["refresco"] = self.raiz.after(1000, self._refrescar)

    def _mostrar_actividad(self):
        a = sn.leer_actividad(self.ruta_datos("actividad.json"))
        en_curso = bool(a.get("en_curso"))
        if self.proceso is not None and self.proceso.poll() is not None:
            codigo, self.proceso = self.proceso.returncode, None
            if codigo not in (0, 4):
                self._avisar_fallo(codigo)
        if not en_curso:
            self.deteniendo = False

        if en_curso:
            fase = a.get("fase") or "Subiendo"
            texto = "● {0} ({1})".format(fase, ORIGENES.get(a.get("origen"), a.get("origen")))
            if self.deteniendo:
                texto = "● Deteniendo... (termina el trozo que está enviando)"
            self.lbl_estado.configure(text=texto, foreground="#0a6e0a")
        elif a.get("interrumpida"):
            self.lbl_estado.configure(text="▲ La última subida se interrumpió (¿se apagó el equipo?); "
                                           "continuará en la próxima ejecución.", foreground="#b05a00")
        elif not os.path.exists(self.ruta_config):
            self.lbl_estado.configure(text="Primero configure el programa en la pestaña Configuración.",
                                      foreground="#b05a00")
        else:
            self.lbl_estado.configure(text="En espera", foreground="#333333")

        total, hecho = a.get("archivo_total") or 0, a.get("archivo_hecho") or 0
        if en_curso and a.get("archivo"):
            self.lbl_archivo.configure(text="Archivo {0} de {1}:  {2}".format(
                a.get("indice"), a.get("cantidad"), a.get("archivo")))
            self.barra_archivo["value"] = 1000 * hecho / total if total else 0
            porcentaje = 100.0 * hecho / total if total else 0
            self.lbl_cantidad.configure(text="{0:.1f} %   ({1} de {2})".format(
                porcentaje, sn.legible(hecho), sn.legible(total)))
            velocidad = a.get("velocidad") or 0
            if velocidad > 0:
                restante = (total - hecho) / velocidad
                self.lbl_tiempo.configure(text="{0}/s   -   tiempo restante: {1}".format(
                    sn.legible(int(velocidad)), sn.duracion(restante)))
            else:
                self.lbl_tiempo.configure(text="calculando velocidad...")
            lote_total, lote_hecho = a.get("lote_total") or 0, a.get("lote_hecho") or 0
            self.lbl_lote.configure(text="Total de esta ejecución: {0} de {1}".format(
                sn.legible(lote_hecho), sn.legible(lote_total)))
            self.barra_lote["value"] = 1000 * lote_hecho / lote_total if lote_total else 0
        else:
            self.lbl_archivo.configure(text="No se está subiendo nada.")
            self.barra_archivo["value"] = 0
            self.barra_lote["value"] = 0
            self.lbl_cantidad.configure(text="")
            self.lbl_tiempo.configure(text="")
            self.lbl_lote.configure(text="")

        if en_curso:
            self.lbl_ultima.configure(text="Esta ejecución comenzó el {0} (hace {1}).".format(
                fecha_corta(sn.fecha_local(a.get("inicio"))), sn.duracion(time.time() - (a.get("inicio") or 0))))
        elif a.get("resultado"):
            fin = sn.fecha_local(a.get("fin")) if a.get("fin") else ""
            self.lbl_ultima.configure(text="Última ejecución ({0}, {1}): {2}. {3}".format(
                fecha_corta(fin), ORIGENES.get(a.get("origen"), ""), a.get("resultado"), a.get("detalle", "")))
        elif not a:
            self.lbl_ultima.configure(text="Todavía no se ha ejecutado ninguna subida.")
        self.btn_subir.configure(state="disabled" if en_curso or self.proceso is not None else "normal")
        self.btn_detener.configure(state="normal" if en_curso and not self.deteniendo else "disabled")
        if a != self._ultima_actividad and not en_curso and self._ultima_actividad.get("en_curso"):
            self.cargar_historial()
        self._ultima_actividad = a

    def _mostrar_registro(self):
        ruta = self.ruta_log()
        try:
            info = os.stat(ruta)
            firma = (info.st_size, info.st_mtime)
        except OSError:
            firma = None
        if firma == self._firma_registro:
            return
        self._firma_registro = firma
        texto = leer_cola_archivo(ruta) if firma else "(todavía no hay registro)"
        al_final = self.txt_registro.yview()[1] >= 0.999
        self.txt_registro.configure(state="normal")
        self.txt_registro.delete("1.0", "end")
        self.txt_registro.insert("end", texto)
        self.txt_registro.configure(state="disabled")
        if al_final:
            self.txt_registro.see("end")

    def ruta_log(self):
        valor = self.guardados.get(("general", "archivo_log")) or "subir_nube.log"
        return os.path.normpath(os.path.join(self.base, valor))

    def _avisar_fallo(self, codigo):
        lineas = [linea for linea in leer_cola_archivo(self.ruta_log(), 40).splitlines() if " ERROR " in linea]
        detalle = lineas[-1].split(" ERROR ", 1)[1].strip() if lineas else "Revise el registro."
        messagebox.showwarning("Subida", "La subida terminó con problemas (código {0}):\n\n{1}".format(
            codigo, detalle), parent=self.raiz)

    def subir_ahora(self):
        if not self.guardar(silencioso=True):
            return
        try:
            conf = sn.Configuracion(self.ruta_config)
            if not conf.origen or not os.path.isdir(conf.origen):
                raise sn.ErrorFatal("La carpeta de origen no existe: {0}".format(conf.origen or "(vacía)"))
            if not sn.AlmacenTokens(conf.archivo_tokens).obtener(conf.proveedor):
                raise sn.ErrorFatal("Primero vincule la cuenta en la pestaña Configuración.")
        except sn.ErrorFatal as e:
            self.error("Subir ahora", e)
            return
        argumentos = [python_sin_consola(), os.path.join(CARPETA, "subir_nube.py"), "subir",
                      "-c", self.ruta_config, "--origen", "manual"]
        opciones = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            self.proceso = subprocess.Popen(argumentos, cwd=CARPETA, **opciones)
        except OSError as e:
            self.error("Subir ahora", e)
            return
        self.btn_subir.configure(state="disabled")
        self.lbl_estado.configure(text="● Iniciando...", foreground="#0a6e0a")

    def detener(self):
        if not messagebox.askyesno("Detener", "¿Detener la subida?\n\nLo que ya se subió no se pierde: la próxima "
                                              "ejecución continúa desde ese punto.", parent=self.raiz):
            return
        try:
            open(self.ruta_datos("detener.solicitud"), "w").close()
        except OSError as e:
            self.error("Detener", e)
            return
        self.deteniendo = True

    def simular(self):
        if not self.guardar(silencioso=True):
            return

        def trabajo():
            mensajes = []
            manejador = ListaRegistro(mensajes)
            sn.log.addHandler(manejador)
            sn.log.setLevel("INFO")
            proveedor = None
            try:
                conf = sn.Configuracion(self.ruta_config)
                proveedor = sn.crear_proveedor(conf)
                resumen = sn.ejecutar_subida(conf, proveedor, simular=True)
            finally:
                sn.log.removeHandler(manejador)
                if proveedor is not None:
                    proveedor.http.cerrar()
            return "\n".join(mensajes + ["", resumen.texto(simular=True)])

        def al_terminar(ok, resultado):
            if ok:
                VentanaTexto(self.raiz, "Qué se subiría", resultado)
            else:
                self.error("Ver qué se subiría", resultado)
        self.en_hilo(trabajo, al_terminar)

    def abrir_carpeta(self):
        if hasattr(os, "startfile"):
            os.startfile(self.base)
        else:
            messagebox.showinfo("Carpeta de datos", self.base, parent=self.raiz)

    # ------------------------------------------------------------------
    # Pestaña Configuración
    # ------------------------------------------------------------------

    def _crear_configuracion(self):
        marco = ttk.Frame(self.cuaderno, padding=12)
        self.cuaderno.add(marco, text="  Configuración  ")
        marco.columnconfigure(0, weight=1)

        que = ttk.LabelFrame(marco, text=" Qué subir ", padding=8)
        que.grid(row=0, column=0, sticky="ew")
        que.columnconfigure(1, weight=1)
        ttk.Label(que, text="Carpeta de origen:").grid(row=0, column=0, sticky="w")
        ttk.Entry(que, textvariable=self.var("general", "origen")).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(que, text="Examinar...", command=self.elegir_origen).grid(row=0, column=2)
        ttk.Label(que, text="Archivos:").grid(row=1, column=0, sticky="w", pady=3)
        patrones = ttk.Frame(que)
        patrones.grid(row=1, column=1, columnspan=2, sticky="w", padx=4)
        ttk.Entry(patrones, textvariable=self.var("general", "patrones"), width=30).pack(side="left")
        ttk.Label(patrones, text="  separados por ';'  (por ejemplo *.bak;*.trn)", style="Gris.TLabel").pack(
            side="left")
        ttk.Checkbutton(que, text="Incluir subcarpetas", variable=self.var("general", "recursivo", True)).grid(
            row=2, column=1, sticky="w", padx=4)
        fila = ttk.Frame(que)
        fila.grid(row=3, column=1, columnspan=2, sticky="w", padx=4, pady=3)
        ttk.Label(fila, text="No subir archivos modificados hace menos de").pack(side="left")
        tk.Spinbox(fila, from_=0, to=1440, width=5,
                   textvariable=self.var("general", "antiguedad_minima_minutos")).pack(side="left", padx=4)
        ttk.Label(fila, text="minutos (SQL Server podría seguir escribiéndolos)").pack(side="left")
        ttk.Checkbutton(que, text="Borrar el archivo local después de subirlo y verificarlo en la nube",
                        variable=self.var("general", "borrar_local_tras_subir", True)).grid(
            row=4, column=1, columnspan=2, sticky="w", padx=4)

        donde = ttk.LabelFrame(marco, text=" Dónde subir ", padding=8)
        donde.grid(row=1, column=0, sticky="ew", pady=8)
        donde.columnconfigure(1, weight=1)
        ttk.Label(donde, text="Servicio:").grid(row=0, column=0, sticky="w")
        servicios = ttk.Frame(donde)
        servicios.grid(row=0, column=1, sticky="w", padx=4)
        proveedor = self.var("general", "proveedor")
        ttk.Radiobutton(servicios, text="Dropbox", value="dropbox", variable=proveedor,
                        command=self._mostrar_credenciales).pack(side="left")
        ttk.Radiobutton(servicios, text="Google Drive", value="gdrive", variable=proveedor,
                        command=self._mostrar_credenciales).pack(side="left", padx=12)
        ttk.Label(donde, text="Carpeta en la nube:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(donde, textvariable=self.var("general", "carpeta_remota")).grid(row=1, column=1, sticky="ew",
                                                                                  padx=4)

        # Credenciales: las de cada servicio ocupan las mismas filas y se muestra solo la elegida.
        self.credenciales = {}
        for servicio, campos in (("dropbox", (("App key:", "app_key", ""), ("App secret:", "app_secret", "*"))),
                                 ("gdrive", (("ID de cliente:", "client_id", ""),
                                             ("Secreto de cliente:", "client_secret", "*")))):
            widgets = []
            for fila, (texto, clave, oculto) in enumerate(campos, 2):
                etiqueta = ttk.Label(donde, text=texto)
                etiqueta.grid(row=fila, column=0, sticky="w", pady=2)
                entrada = ttk.Entry(donde, show=oculto, textvariable=self.var(servicio, clave))
                entrada.grid(row=fila, column=1, sticky="ew", padx=4, pady=2)
                widgets.extend((etiqueta, entrada))
            self.credenciales[servicio] = widgets

        cuenta = ttk.Frame(donde)
        cuenta.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.lbl_cuenta = ttk.Label(cuenta, text="", style="Grande.TLabel")
        self.lbl_cuenta.pack(side="left")
        ttk.Button(cuenta, text="Probar conexión", command=self.probar).pack(side="right")
        ttk.Button(cuenta, text="Vincular cuenta...", command=self.vincular).pack(side="right", padx=6)

        avanzado = ttk.LabelFrame(marco, text=" Avanzado ", padding=8)
        avanzado.grid(row=2, column=0, sticky="ew")
        avanzado.columnconfigure(1, weight=1)
        numeros = ttk.Frame(avanzado)
        numeros.grid(row=0, column=0, columnspan=3, sticky="w")
        for texto, clave, minimo, maximo in (("Bloque (MB):", "tamano_bloque_mb", 1, 128),
                                             ("Reintentos:", "reintentos", 0, 50),
                                             ("Espera máxima por petición (s):", "timeout_segundos", 10, 3600)):
            ttk.Label(numeros, text=texto).pack(side="left")
            tk.Spinbox(numeros, from_=minimo, to=maximo, width=5,
                       textvariable=self.var("general", clave)).pack(side="left", padx=(4, 16))
        ttk.Label(avanzado, text="Proxy:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(avanzado, textvariable=self.var("general", "proxy")).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Label(avanzado, text="host:puerto (vacío si no hay)", style="Gris.TLabel").grid(
            row=1, column=2, sticky="w")
        ttk.Label(avanzado, text="Certificados:").grid(row=2, column=0, sticky="w")
        ttk.Entry(avanzado, textvariable=self.var("general", "certificados")).grid(row=2, column=1, sticky="ew",
                                                                                   padx=4)
        ttk.Button(avanzado, text="Examinar...", command=self.elegir_certificados).grid(row=2, column=2,
                                                                                        sticky="w")

        acciones = ttk.Frame(marco)
        acciones.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(acciones, text="Guardar", command=self.guardar).pack(side="right")
        ttk.Button(acciones, text="Descartar cambios", command=self.cargar_formulario).pack(side="right", padx=6)
        self.lbl_guardado = ttk.Label(acciones, text="", style="Gris.TLabel")
        self.lbl_guardado.pack(side="left")

    def _mostrar_credenciales(self):
        elegido = "gdrive" if self.var("general", "proveedor").get() == "gdrive" else "dropbox"
        for servicio, widgets in self.credenciales.items():
            for widget in widgets:
                if servicio == elegido:
                    widget.grid()
                else:
                    widget.grid_remove()
        self._mostrar_cuenta()

    def _mostrar_cuenta(self):
        proveedor = self.var("general", "proveedor").get() or "dropbox"
        titulo = sn.PROVEEDORES.get(proveedor, sn.Dropbox).titulo
        ruta = os.path.normpath(os.path.join(self.base, self.var("general", "archivo_tokens").get() or "tokens.json"))
        datos = sn.leer_json(ruta).get(proveedor)
        if isinstance(datos, dict) and datos.get("refresh_token"):
            self.lbl_cuenta.configure(text="Cuenta de {0}: vinculada ({1})".format(
                titulo, fecha_corta(datos.get("fecha"))), foreground="#0a6e0a")
        else:
            self.lbl_cuenta.configure(text="Cuenta de {0}: sin vincular".format(titulo), foreground="#b00000")

    def elegir_origen(self):
        carpeta = filedialog.askdirectory(parent=self.raiz, initialdir=self.var("general", "origen").get() or None,
                                          title="Carpeta con los respaldos")
        if carpeta:
            self.var("general", "origen").set(os.path.normpath(carpeta))

    def elegir_certificados(self):
        ruta = filedialog.askopenfilename(parent=self.raiz, initialdir=self.base, title="Archivo de certificados",
                                          filetypes=[("Certificados", "*.pem"), ("Todos", "*.*")])
        if ruta:
            self.var("general", "certificados").set(os.path.normpath(ruta))

    def valores_formulario(self):
        valores = dict(self.guardados)
        for (seccion, clave), variable in self.vars.items():
            valor = variable.get()
            if isinstance(variable, tk.BooleanVar):
                valor = "si" if valor else "no"
            valores[(seccion, clave)] = "{0}".format(valor).strip()
        valores[("programacion", "dias")] = ",".join(d[0] for d in sn.DIAS_SEMANA if self.dias[d[0]].get())
        valores[("programacion", "hora")] = "{0:02d}:{1:02d}".format(self._numero(self.hora_h),
                                                                     self._numero(self.hora_m))
        if self.var_cuenta.get() == "sistema":
            valores[("programacion", "usuario")] = ""
        return valores

    @staticmethod
    def _numero(variable):
        try:
            return int(variable.get())
        except ValueError:
            return -1

    def cargar_formulario(self):
        self.guardados = sn.leer_valores(self.ruta_config)
        for (seccion, clave), valor in self.guardados.items():
            if seccion == "programacion" and clave in ("dias", "hora", "usuario"):
                continue
            booleana = clave in ("recursivo", "borrar_local_tras_subir", "activa")
            variable = self.var(seccion, clave, booleana)
            if booleana:
                variable.set(valor.lower() in sn.VALORES_SI)
            else:
                variable.set(valor)
        dias = [d.strip().upper() for d in self.guardados[("programacion", "dias")].split(",")]
        for abreviatura, _, _ in sn.DIAS_SEMANA:
            self.dias[abreviatura].set(abreviatura in dias)
        hora = self.guardados[("programacion", "hora")].split(":")
        self.hora_h.set(hora[0] if hora else "3")
        self.hora_m.set("{0:02d}".format(int(hora[1])) if len(hora) > 1 and hora[1].isdigit() else "30")
        usuario = self.guardados[("programacion", "usuario")]
        self.var_cuenta.set("usuario" if usuario else "sistema")
        self.var("programacion", "usuario").set(usuario)
        self._mostrar_credenciales()
        self._mostrar_programacion()
        self.lbl_guardado.configure(text="")

    def guardar(self, silencioso=False):
        """Valida y guarda el formulario. Devuelve True si quedó guardado."""
        valores = self.valores_formulario()
        if valores == self.guardados and os.path.exists(self.ruta_config):
            try:
                sn.Configuracion(self.ruta_config)
                return True
            except sn.ErrorFatal as e:
                self.error("Configuración", e)
                return False
        try:
            sn.guardar_configuracion(self.ruta_config, valores)
        except (sn.ErrorFatal, OSError) as e:
            self.error("No se guardó la configuración", e)
            return False
        self.guardados = sn.leer_valores(self.ruta_config)
        self.lbl_guardado.configure(text="Guardado a las " + time.strftime("%H:%M:%S"))
        if not silencioso:
            origen = self.var("general", "origen").get()
            if origen and not os.path.isdir(origen):
                messagebox.showwarning("Configuración", "Se guardó, pero la carpeta de origen no existe:\n" + origen,
                                       parent=self.raiz)
        self._mostrar_cuenta()
        self._mostrar_programacion()
        return True

    def _proveedor(self):
        conf = sn.Configuracion(self.ruta_config)
        return sn.crear_proveedor(conf)

    def probar(self):
        if not self.guardar(silencioso=True):
            return

        def trabajo():
            proveedor = self._proveedor()
            try:
                return proveedor.probar()
            finally:
                proveedor.http.cerrar()

        def al_terminar(ok, resultado):
            if ok:
                messagebox.showinfo("Probar conexión", "Conexión correcta.\n\n" + resultado, parent=self.raiz)
            else:
                self.error("Probar conexión", resultado)
        self.en_hilo(trabajo, al_terminar)

    def vincular(self):
        if not self.guardar(silencioso=True):
            return
        try:
            proveedor = self._proveedor()
            if proveedor.tipo_autorizacion == "codigo":
                proveedor.url_autorizacion()      # comprueba que estén las credenciales
            else:
                proveedor._credenciales()
        except sn.ErrorFatal as e:
            self.error("Vincular cuenta", e)
            return
        if proveedor.tipo_autorizacion == "codigo":
            DialogoDropbox(self, proveedor)
        else:
            DialogoGoogle(self, proveedor)

    def cuenta_vinculada(self, proveedor, refresh):
        """Guarda el permiso obtenido y comprueba la conexión."""
        proveedor.tokens.guardar(proveedor.nombre, refresh)
        try:
            return proveedor.probar()
        finally:
            proveedor.http.cerrar()

    # ------------------------------------------------------------------
    # Pestaña Programación
    # ------------------------------------------------------------------

    def _crear_programacion(self):
        marco = ttk.Frame(self.cuaderno, padding=12)
        self.cuaderno.add(marco, text="  Programación  ")
        ttk.Checkbutton(marco, text="Subir automáticamente con el Programador de tareas de Windows",
                        variable=self.var("programacion", "activa", True),
                        command=self._habilitar_programacion).pack(anchor="w")
        ttk.Label(marco, text="La tarea se ejecuta aunque nadie haya iniciado sesión en el servidor. Elija una hora "
                              "posterior al término de los respaldos de SQL Server.", style="Gris.TLabel",
                  wraplength=820).pack(anchor="w", pady=(2, 8))

        self.marco_cuando = ttk.LabelFrame(marco, text=" Cuándo ", padding=8)
        self.marco_cuando.pack(fill="x")
        frecuencia = self.var("programacion", "frecuencia")
        ttk.Radiobutton(self.marco_cuando, text="Todos los días", value="diaria", variable=frecuencia,
                        command=self._habilitar_programacion).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(self.marco_cuando, text="Algunos días de la semana:", value="semanal",
                        variable=frecuencia, command=self._habilitar_programacion).grid(row=1, column=0, sticky="w")
        dias = ttk.Frame(self.marco_cuando)
        dias.grid(row=1, column=1, sticky="w")
        self.dias = {}
        self.chk_dias = []
        for abreviatura, _, nombre in sn.DIAS_SEMANA:
            self.dias[abreviatura] = tk.BooleanVar()
            casilla = ttk.Checkbutton(dias, text=abreviatura.capitalize(), variable=self.dias[abreviatura])
            casilla.pack(side="left", padx=2)
            self.chk_dias.append(casilla)
        ttk.Radiobutton(self.marco_cuando, text="Cada", value="horas", variable=frecuencia,
                        command=self._habilitar_programacion).grid(row=2, column=0, sticky="w")
        horas = ttk.Frame(self.marco_cuando)
        horas.grid(row=2, column=1, sticky="w")
        self.spin_horas = tk.Spinbox(horas, from_=1, to=23, width=4,
                                     textvariable=self.var("programacion", "cada_horas"))
        self.spin_horas.pack(side="left")
        ttk.Label(horas, text="horas (útil para respaldos de registro de transacciones .trn)").pack(
            side="left", padx=4)
        ttk.Label(self.marco_cuando, text="Hora de inicio:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        hora = ttk.Frame(self.marco_cuando)
        hora.grid(row=3, column=1, sticky="w", pady=(8, 0))
        self.hora_h = tk.StringVar(value="3")
        self.hora_m = tk.StringVar(value="30")
        tk.Spinbox(hora, from_=0, to=23, width=3, format="%02.0f", textvariable=self.hora_h).pack(side="left")
        ttk.Label(hora, text=":").pack(side="left")
        tk.Spinbox(hora, from_=0, to=59, width=3, format="%02.0f", textvariable=self.hora_m).pack(side="left")

        self.marco_cuenta = ttk.LabelFrame(marco, text=" Con qué cuenta de Windows ", padding=8)
        self.marco_cuenta.pack(fill="x", pady=8)
        self.var_cuenta = tk.StringVar(value="sistema")
        ttk.Radiobutton(self.marco_cuenta, text="Cuenta del sistema (SYSTEM): no pide contraseña",
                        value="sistema", variable=self.var_cuenta,
                        command=self._habilitar_programacion).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(self.marco_cuenta, text="Esta cuenta:", value="usuario", variable=self.var_cuenta,
                        command=self._habilitar_programacion).grid(row=1, column=0, sticky="w")
        self.ent_usuario = ttk.Entry(self.marco_cuenta, width=30, textvariable=self.var("programacion", "usuario"))
        self.ent_usuario.grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(self.marco_cuenta, text="(DOMINIO\\usuario; necesaria si la carpeta de origen está en otra "
                                          "máquina de la red)", style="Gris.TLabel").grid(
            row=2, column=1, sticky="w", padx=4)

        acciones = ttk.Frame(marco)
        acciones.pack(fill="x")
        ttk.Button(acciones, text="Aplicar programación", command=self.aplicar_programacion).pack(side="right")
        estado = ttk.LabelFrame(marco, text=" Tarea en Windows ", padding=8)
        estado.pack(fill="x", pady=8)
        self.lbl_tarea = ttk.Label(estado, text="", wraplength=820, justify="left")
        self.lbl_tarea.pack(anchor="w")
        ttk.Label(marco, text="También puede ejecutar la subida desde un trabajo del Agente SQL Server (paso de tipo "
                              "CmdExec que llama a subir_nube.bat), por ejemplo al final del trabajo de respaldo.",
                  style="Gris.TLabel", wraplength=820).pack(anchor="w")

    def _habilitar_programacion(self):
        activa = self.var("programacion", "activa", True).get()
        frecuencia = self.var("programacion", "frecuencia").get()

        def estado(condicion):
            return "normal" if activa and condicion else "disabled"
        for hijo in self.marco_cuando.winfo_children() + self.marco_cuenta.winfo_children():
            if isinstance(hijo, (ttk.Radiobutton, ttk.Label)):
                hijo.configure(state=estado(True))
        for casilla in self.chk_dias:
            casilla.configure(state=estado(frecuencia == "semanal"))
        self.spin_horas.configure(state=estado(frecuencia == "horas"))
        self.ent_usuario.configure(state=estado(self.var_cuenta.get() == "usuario"))

    def _mostrar_programacion(self):
        self._habilitar_programacion()
        try:
            conf = sn.Configuracion(self.ruta_config)
        except sn.ErrorFatal:
            texto_conf = None
        else:
            texto_conf = conf.programacion
        existe = sn.tarea_existe()
        if os.name != "nt":
            texto = "La tarea programada solo existe en Windows."
        elif existe and texto_conf is not None and texto_conf.activa:
            texto = "Creada: " + texto_conf.descripcion()
        elif existe:
            texto = "Existe una tarea '{0}' en Windows, pero la configuración no tiene la subida automática " \
                    "activada. Pulse 'Aplicar programación' para quitarla.".format(sn.NOMBRE_TAREA)
        elif texto_conf is not None and texto_conf.activa:
            texto = "La configuración pide subida automática pero la tarea no existe en Windows: " \
                    "pulse 'Aplicar programación'."
        else:
            texto = "No hay subida automática."
        self.lbl_tarea.configure(text=texto)
        if texto_conf is not None:
            self.lbl_proxima.configure(text="Subida automática: " + texto_conf.descripcion()
                                       + ("" if existe or os.name != "nt" or not texto_conf.activa
                                          else " (falta aplicarla en la pestaña Programación)"))

    def aplicar_programacion(self):
        if not self.guardar(silencioso=True):
            return
        conf = sn.Configuracion(self.ruta_config)
        clave = None
        if conf.programacion.activa and conf.programacion.usuario:
            clave = simpledialog.askstring("Contraseña", "Contraseña de {0}:\n(no se guarda; Windows la usa para "
                                                         "ejecutar la tarea)".format(conf.programacion.usuario),
                                           show="*", parent=self.raiz)
            if not clave:
                return

        def al_terminar(ok, resultado):
            self._mostrar_programacion()
            if ok:
                messagebox.showinfo("Programación", resultado, parent=self.raiz)
            else:
                self.error("Programación", resultado)
        self.en_hilo(lambda: sn.aplicar_programacion(conf.programacion, conf.ruta, clave), al_terminar)

    # ------------------------------------------------------------------
    # Pestaña Historial
    # ------------------------------------------------------------------

    def _crear_historial(self):
        marco = ttk.Frame(self.cuaderno, padding=12)
        self.cuaderno.add(marco, text="  Historial  ")
        ejecuciones = ttk.LabelFrame(marco, text=" Ejecuciones ", padding=4)
        ejecuciones.pack(fill="x")
        self.arbol_ejecuciones = self._tabla(ejecuciones, 7, (
            ("fecha", "Inicio", 110), ("origen", "Origen", 150), ("resultado", "Resultado", 90),
            ("subidos", "Subidos", 60), ("bytes", "Datos", 80), ("detalle", "Detalle", 380)))
        archivos = ttk.LabelFrame(marco, text=" Archivos subidos ", padding=4)
        archivos.pack(fill="both", expand=True, pady=8)
        self.arbol_archivos = self._tabla(archivos, 12, (
            ("fecha", "Fecha", 110), ("archivo", "Archivo", 210), ("tamano", "Tamaño", 75),
            ("duracion", "Duración", 85), ("velocidad", "Velocidad", 80), ("resultado", "Resultado", 75),
            ("detalle", "Detalle", 230)))
        botones = ttk.Frame(marco)
        botones.pack(fill="x")
        ttk.Button(botones, text="Actualizar", command=lambda: self.cargar_historial(True)).pack(side="right")
        ttk.Label(botones, text="Los más recientes primero. Doble clic en una fila para ver el detalle completo.",
                  style="Gris.TLabel").pack(side="left")

    def _tabla(self, padre, alto, columnas):
        arbol = ttk.Treeview(padre, columns=[c[0] for c in columnas], show="headings", height=alto)
        for nombre, titulo, ancho in columnas:
            arbol.heading(nombre, text=titulo, anchor="w")
            arbol.column(nombre, width=ancho, anchor="w", stretch=nombre == "detalle")
        barra = ttk.Scrollbar(padre, orient="vertical", command=arbol.yview)
        arbol.configure(yscrollcommand=barra.set)
        barra.pack(side="right", fill="y")
        arbol.pack(fill="both", expand=True)
        arbol.tag_configure("error", foreground="#b00000")
        arbol.tag_configure("aviso", foreground="#b05a00")
        arbol.bind("<Double-1>", lambda evento: self._ver_fila(arbol))
        return arbol

    def _ver_fila(self, arbol):
        seleccion = arbol.selection()
        if seleccion:
            valores = arbol.item(seleccion[0], "values")
            titulos = [arbol.heading(c, "text") for c in arbol["columns"]]
            VentanaTexto(self.raiz, "Detalle", "\n".join("{0}: {1}".format(t, v) for t, v in zip(titulos, valores)))

    def cargar_historial(self, forzar=False):
        ruta = self.ruta_datos("historial.jsonl")
        try:
            info = os.stat(ruta)
            firma = (info.st_size, info.st_mtime)
        except OSError:
            firma = None
        if firma == self._firma_historial and not forzar:
            return
        self._firma_historial = firma
        entradas = sn.Historial.leer(ruta, 2000)
        for arbol in (self.arbol_ejecuciones, self.arbol_archivos):
            arbol.delete(*arbol.get_children())
        for e in reversed(entradas):
            etiqueta = ()
            if e.get("resultado") in ("error", "fallida"):
                etiqueta = ("error",)
            elif e.get("resultado") in ("detenido", "detenida", "con errores"):
                etiqueta = ("aviso",)
            if e.get("tipo") == "ejecucion":
                self.arbol_ejecuciones.insert("", "end", tags=etiqueta, values=(
                    fecha_corta(e.get("fecha")), ORIGENES.get(e.get("origen"), e.get("origen")), e.get("resultado"),
                    e.get("subidos", 0), sn.legible(e.get("bytes", 0)), e.get("detalle", "")))
            elif e.get("tipo") == "archivo":
                segundos = e.get("segundos") or 0
                velocidad = sn.legible(int(e.get("tamano", 0) / segundos)) + "/s" \
                    if segundos and e.get("resultado") == "subido" else ""
                detalle = e.get("detalle", "")
                if e.get("verificado") and not detalle:
                    detalle = "Suma de verificación correcta."
                self.arbol_archivos.insert("", "end", tags=etiqueta, values=(
                    fecha_corta(e.get("fecha")), e.get("archivo"), sn.legible(e.get("tamano", 0)),
                    sn.duracion(segundos), velocidad, e.get("resultado"), detalle))

    def _al_cambiar_pestana(self, evento):
        indice = self.cuaderno.index("current")
        if indice == 2:
            self._mostrar_programacion()
        elif indice == 3:
            self.cargar_historial(True)


# ---------------------------------------------------------------------------
# Ventanas auxiliares
# ---------------------------------------------------------------------------

class ListaRegistro(logging.Handler):
    def __init__(self, destino):
        logging.Handler.__init__(self)
        self.destino = destino

    def emit(self, registro):
        self.destino.append(registro.getMessage())


class VentanaTexto(tk.Toplevel):
    def __init__(self, padre, titulo, texto):
        tk.Toplevel.__init__(self, padre)
        self.title(titulo)
        self.geometry("700x400")
        caja = tk.Text(self, wrap="word", font=("Courier New", 9))
        caja.insert("end", texto)
        caja.configure(state="disabled")
        caja.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(self, text="Cerrar", command=self.destroy).pack(pady=(0, 8))
        self.transient(padre)


class DialogoDropbox(tk.Toplevel):
    def __init__(self, app, proveedor):
        tk.Toplevel.__init__(self, app.raiz)
        self.app = app
        self.proveedor = proveedor
        self.title("Vincular cuenta de Dropbox")
        self.resizable(False, False)
        marco = ttk.Frame(self, padding=14)
        marco.pack(fill="both", expand=True)
        url = proveedor.url_autorizacion()
        ttk.Label(marco, text="1. Abra esta dirección en el navegador de otro equipo o del teléfono\n"
                              "   (el Internet Explorer de Windows 2003 no puede abrir Dropbox). Si está\n"
                              "   conectado por Escritorio remoto, cópiela y péguela en su equipo:").pack(anchor="w")
        fila = ttk.Frame(marco)
        fila.pack(fill="x", pady=6)
        direccion = ttk.Entry(fila, width=70)
        direccion.insert(0, url)
        direccion.configure(state="readonly")
        direccion.pack(side="left", fill="x", expand=True)
        ttk.Button(fila, text="Copiar", command=lambda: self._copiar(url)).pack(side="left", padx=4)
        ttk.Label(marco, text="2. Inicie sesión en Dropbox y pulse «Permitir».").pack(anchor="w", pady=(4, 0))
        ttk.Label(marco, text="3. Copie el código que muestra Dropbox y péguelo aquí:").pack(anchor="w", pady=(4, 0))
        self.codigo = ttk.Entry(marco, width=50)
        self.codigo.pack(anchor="w", pady=6)
        self.lbl = ttk.Label(marco, text="")
        self.lbl.pack(anchor="w")
        botones = ttk.Frame(marco)
        botones.pack(fill="x", pady=(8, 0))
        ttk.Button(botones, text="Cancelar", command=self.destroy).pack(side="right")
        self.btn = ttk.Button(botones, text="Vincular", command=self.vincular)
        self.btn.pack(side="right", padx=6)
        self.transient(app.raiz)
        self.grab_set()
        self.codigo.focus_set()

    def _copiar(self, texto):
        self.clipboard_clear()
        self.clipboard_append(texto)
        self.lbl.configure(text="Dirección copiada.")

    def vincular(self):
        codigo = self.codigo.get().strip()
        if not codigo:
            self.lbl.configure(text="Pegue el código que muestra Dropbox.")
            return
        self.btn.configure(state="disabled")
        self.lbl.configure(text="Comprobando el código...")

        def trabajo():
            return self.app.cuenta_vinculada(self.proveedor, self.proveedor.completar_autorizacion(codigo))

        def al_terminar(ok, resultado):
            if not self.winfo_exists():
                return
            if ok:
                self.destroy()
                self.app._mostrar_cuenta()
                messagebox.showinfo("Cuenta vinculada", resultado, parent=self.app.raiz)
            else:
                self.btn.configure(state="normal")
                self.lbl.configure(text="")
                self.app.error("Vincular cuenta", resultado)
        self.app.en_hilo(trabajo, al_terminar)


class DialogoGoogle(tk.Toplevel):
    def __init__(self, app, proveedor):
        tk.Toplevel.__init__(self, app.raiz)
        self.app = app
        self.proveedor = proveedor
        self.cancelado = False
        self.title("Vincular cuenta de Google Drive")
        self.resizable(False, False)
        marco = ttk.Frame(self, padding=14)
        marco.pack(fill="both", expand=True)
        self.lbl_paso1 = ttk.Label(marco, text="Pidiendo un código a Google...")
        self.lbl_paso1.pack(anchor="w")
        self.lbl_codigo = ttk.Label(marco, text="", font=("Tahoma", 22, "bold"))
        self.lbl_codigo.pack(pady=10)
        ttk.Label(marco, text="2. Escriba ese código, elija la cuenta de Google y acepte los permisos.\n"
                              "   Esta ventana se cerrará sola cuando termine.").pack(anchor="w")
        self.barra = ttk.Progressbar(marco, mode="indeterminate", length=420)
        self.barra.pack(pady=10)
        self.barra.start(15)
        ttk.Button(marco, text="Cancelar", command=self.cancelar).pack(anchor="e")
        self.protocol("WM_DELETE_WINDOW", self.cancelar)
        self.transient(app.raiz)
        self.grab_set()
        app.en_hilo(proveedor.iniciar_autorizacion, self._con_codigo)

    def _con_codigo(self, ok, datos):
        if not self.winfo_exists():
            return
        if not ok:
            self.destroy()
            self.app.error("Vincular cuenta", datos)
            return
        self.lbl_paso1.configure(text="1. En cualquier equipo o teléfono abra  {0}  y escriba este código:".format(
            datos["direccion"]))
        self.lbl_codigo.configure(text=datos["user_code"])

        def trabajo():
            refresh = self.proveedor.esperar_autorizacion(datos, lambda: self.cancelado)
            return self.app.cuenta_vinculada(self.proveedor, refresh)
        self.app.en_hilo(trabajo, self._terminado)

    def _terminado(self, ok, resultado):
        if self.cancelado or not self.winfo_exists():
            return
        self.destroy()
        if ok:
            self.app._mostrar_cuenta()
            messagebox.showinfo("Cuenta vinculada", resultado, parent=self.app.raiz)
        else:
            self.app.error("Vincular cuenta", resultado)

    def cancelar(self):
        self.cancelado = True
        self.destroy()


def main():
    argumentos = sys.argv[1:]
    ruta = sn.ruta_config_predeterminada()
    pestana = None
    while argumentos:
        opcion = argumentos.pop(0)
        if opcion in ("-c", "--config") and argumentos:
            ruta = argumentos.pop(0)
        elif opcion == "--pestana" and argumentos:
            pestana = int(argumentos.pop(0))
    if pestana is None:
        # La primera vez se abre directamente la configuración.
        pestana = 0 if os.path.exists(ruta) else 1
    raiz = tk.Tk()
    app = Aplicacion(raiz, ruta)
    app.cuaderno.select(pestana)
    raiz.mainloop()


if __name__ == "__main__":
    main()
