# -*- coding: utf-8 -*-
"""
CaronteSync - ventana para configurar, programar y seguir las subidas a la nube.

Como Caronte, el barquero que cruzaba las almas de un mundo a otro, CaronteSync
cruza los respaldos desde el servidor hacia la nube.

Se abre con pythonw.exe (sin consola). La subida en sí la hace siempre el motor
(carontesync.py) en un proceso aparte, tanto si la lanza esta ventana como si la
lanza el Programador de tareas: la ventana solo lee actividad.json,
historial.jsonl y el registro para mostrar el avance. Al cerrarla queda como un
icono junto al reloj (como Dropbox), y la subida sigue aunque se salga del todo.

Opciones: -c RUTA (configuración), --bandeja (abrir oculta junto al reloj).
"""

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

CARPETA = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CARPETA)

import carontesync as cs  # noqa: E402

try:
    import carontesync_bandeja as bandeja
except (ImportError, OSError, AttributeError):    # fuera de Windows no hay bandeja
    bandeja = None

ORIGENES = {"programada": "tarea programada", "manual": "desde esta ventana", "consola": "desde la consola"}
CREATE_NO_WINDOW = 0x08000000
ICONO = os.path.join(CARPETA, "carontesync.ico")
ICONO_SUBIENDO = os.path.join(CARPETA, "carontesync_subiendo.ico")
CLAVE_INICIO = r"Software\Microsoft\Windows\CurrentVersion\Run"


class C(object):
    """Paleta: la laguna oscura de Caronte, el cielo azul de la nube y el oro de la barca."""
    FONDO = "#EEF2F7"
    LATERAL = "#0B1430"
    LATERAL_ACTIVO = "#18254D"
    LATERAL_TEXTO = "#A9B6D6"
    ORO = "#F5B233"
    ACENTO = "#2F7BFF"
    ACENTO_OSCURO = "#1D5FD8"
    ACENTO_SUAVE = "#DCE8FF"
    TARJETA = "#FFFFFF"
    PLACA = "#F5F7FB"
    BORDE = "#DCE3EE"
    TEXTO = "#0F172A"
    GRIS = "#64748B"
    VERDE = "#16A34A"
    NARANJA = "#D97706"
    ROJO = "#DC2626"
    CONSOLA = "#0B1430"
    CONSOLA_TEXTO = "#C7D2FE"


def python_sin_consola():
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pythonw if os.path.exists(pythonw) else sys.executable


def fecha_corta(texto):
    """'2026-10-15 03:30:12' -> '15/10/2026 03:30'"""
    try:
        return time.strftime("%d/%m/%Y %H:%M", time.strptime(texto, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return texto or ""


def verbo_fase(fase):
    """Palabra corta para la fase del archivo en curso (barra lateral y bandeja)."""
    fase = fase or ""
    if fase.startswith("Comprimiendo"):
        return "Comprimiendo"
    if fase.startswith("Verificando"):
        return "Verificando"
    return "Subiendo"


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


def comando_inicio():
    return '"{0}" "{1}" --bandeja'.format(python_sin_consola(), os.path.join(CARPETA, "carontesync_gui.pyw"))


def inicio_con_windows():
    """True si CaronteSync se abre junto al reloj al iniciar sesión (registro Run del usuario)."""
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLAVE_INICIO) as clave:
            return bool(winreg.QueryValueEx(clave, "CaronteSync")[0])
    except OSError:
        return False


def cambiar_inicio_con_windows(activo):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLAVE_INICIO) as clave:
        if activo:
            winreg.SetValueEx(clave, "CaronteSync", 0, winreg.REG_SZ, comando_inicio())
        else:
            try:
                winreg.DeleteValue(clave, "CaronteSync")
            except OSError:
                pass


class Spinbox(ttk.Entry):
    """ttk::spinbox de Tk 8.6 (Python 3.4 no trae el envoltorio ttk.Spinbox)."""

    def __init__(self, master=None, **opciones):
        ttk.Entry.__init__(self, master, "ttk::spinbox", **opciones)


class Aplicacion(object):
    def __init__(self, raiz, ruta_config, en_bandeja=False):
        self.raiz = raiz
        self.ruta_config = os.path.abspath(ruta_config)
        self.base = os.path.dirname(self.ruta_config)
        self.cola = queue.Queue()
        self.proceso = None
        self.guardados = {}
        self.vars = {}
        self.deteniendo = False
        self.icono_bandeja = None
        self.avisado_bandeja = False
        self.pagina_actual = None
        self._firma_registro = "sin leer"
        self._firma_historial = None
        self._ultima_actividad = {}
        self._temporizadores = {}

        raiz.title("CaronteSync")
        raiz.geometry("1000x700")
        raiz.minsize(900, 640)
        raiz.configure(bg=C.FONDO)
        try:
            raiz.iconbitmap(ICONO)
        except tk.TclError:
            pass
        self._estilos()
        self._crear_lateral()
        self.contenido = tk.Frame(raiz, bg=C.FONDO)
        self.contenido.pack(side="left", fill="both", expand=True)
        self.paginas = {}
        self._crear_estado()
        self._crear_configuracion()
        self._crear_programacion()
        self._crear_notificaciones()
        self._crear_historial()

        self.cargar_formulario()
        self._crear_bandeja()
        raiz.protocol("WM_DELETE_WINDOW", self.cerrar)
        raiz.bind("<Unmap>", self._al_minimizar)
        self.mostrar_pagina("estado")
        self._atender_cola()
        self._refrescar()
        if en_bandeja and self.icono_bandeja is not None:
            raiz.withdraw()

    # ------------------------------------------------------------------
    # Estilo
    # ------------------------------------------------------------------

    def _estilos(self):
        familias = set(tkfont.families(self.raiz))
        base = "Segoe UI" if "Segoe UI" in familias else "Tahoma"   # Tahoma en XP/2003
        mono = "Lucida Console" if "Lucida Console" in familias else "Courier New"
        self.f_normal = (base, 9)
        self.f_pequena = (base, 8)
        self.f_negrita = (base, 9, "bold")
        self.f_subtitulo = (base, 11, "bold")
        self.f_titulo = (base, 16, "bold")
        self.f_enorme = (base, 24, "bold")
        self.f_nav = (base, 10)
        self.f_nav_activo = (base, 10, "bold")
        self.f_mono = (mono, 8)
        for nombre in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(nombre).configure(family=base, size=9)
            except tk.TclError:
                pass

        e = ttk.Style(self.raiz)
        e.theme_use("clam")    # el tema "clam" deja cambiar todos los colores
        e.configure(".", background=C.FONDO, foreground=C.TEXTO, font=self.f_normal, bordercolor=C.BORDE,
                    lightcolor=C.TARJETA, darkcolor=C.BORDE, troughcolor=C.PLACA, focuscolor=C.ACENTO)
        e.configure("TFrame", background=C.FONDO)
        e.configure("Tarjeta.TFrame", background=C.TARJETA)
        e.configure("Placa.TFrame", background=C.PLACA)
        e.configure("TLabel", background=C.FONDO, foreground=C.TEXTO)
        e.configure("Titulo.TLabel", font=self.f_titulo, background=C.FONDO)
        e.configure("Subtitulo.TLabel", font=self.f_normal, foreground=C.GRIS, background=C.FONDO)
        e.configure("Tarjeta.TLabel", background=C.TARJETA)
        e.configure("TituloTarjeta.TLabel", background=C.TARJETA, font=self.f_subtitulo)
        e.configure("Gris.TLabel", background=C.TARJETA, foreground=C.GRIS, font=self.f_pequena)
        e.configure("Negrita.TLabel", background=C.TARJETA, font=self.f_negrita)
        e.configure("Enorme.TLabel", background=C.TARJETA, foreground=C.ACENTO, font=self.f_enorme)
        e.configure("PlacaTitulo.TLabel", background=C.PLACA, foreground=C.GRIS, font=self.f_pequena)
        e.configure("PlacaValor.TLabel", background=C.PLACA, font=self.f_subtitulo)
        for tipo in ("TCheckbutton", "TRadiobutton"):
            e.configure(tipo, background=C.TARJETA, foreground=C.TEXTO, indicatorbackground=C.TARJETA,
                        indicatorforeground=C.ACENTO, focuscolor=C.TARJETA)
            e.map(tipo, background=[("active", C.TARJETA)], indicatorbackground=[("selected", C.ACENTO_SUAVE)],
                  foreground=[("disabled", "#A0AEC0")])
        e.configure("TEntry", fieldbackground=C.TARJETA, padding=3, bordercolor=C.BORDE, lightcolor=C.BORDE)
        e.map("TEntry", bordercolor=[("focus", C.ACENTO)], lightcolor=[("focus", C.ACENTO)],
              fieldbackground=[("readonly", C.PLACA), ("disabled", C.PLACA)])
        e.configure("TSpinbox", fieldbackground=C.TARJETA, padding=4, arrowsize=11, bordercolor=C.BORDE,
                    lightcolor=C.BORDE, background=C.PLACA)
        e.map("TSpinbox", bordercolor=[("focus", C.ACENTO)], fieldbackground=[("disabled", C.PLACA)])

        e.configure("TButton", padding=(14, 6), borderwidth=0, background="#E6EBF3", foreground=C.TEXTO,
                    lightcolor="#E6EBF3", darkcolor="#E6EBF3", focusthickness=0)
        e.map("TButton", background=[("disabled", "#EEF1F6"), ("pressed", "#CDD6E4"), ("active", "#D9E0EC")],
              foreground=[("disabled", "#A0AEC0")])
        e.configure("Primario.TButton", background=C.ACENTO, foreground="#FFFFFF", font=self.f_negrita,
                    lightcolor=C.ACENTO, darkcolor=C.ACENTO)
        e.map("Primario.TButton", background=[("disabled", "#A9C5F7"), ("pressed", "#174DB0"),
                                              ("active", C.ACENTO_OSCURO)],
              foreground=[("disabled", "#F0F5FF")])
        e.configure("Peligro.TButton", background="#FDECEC", foreground=C.ROJO, font=self.f_negrita)
        e.map("Peligro.TButton", background=[("disabled", "#F5F6F8"), ("active", "#FAD7D7")],
              foreground=[("disabled", "#C9CED6")])

        e.configure("Grueso.Horizontal.TProgressbar", thickness=18, troughcolor="#E3E9F3", background=C.ACENTO,
                    lightcolor=C.ACENTO, darkcolor=C.ACENTO, bordercolor="#E3E9F3")
        e.configure("Fino.Horizontal.TProgressbar", thickness=6, troughcolor="#E3E9F3", background=C.ORO,
                    lightcolor=C.ORO, darkcolor=C.ORO, bordercolor="#E3E9F3")
        e.configure("Ocupado.Horizontal.TProgressbar", thickness=6, troughcolor="#E3E9F3", background=C.ACENTO,
                    lightcolor=C.ACENTO, darkcolor=C.ACENTO, bordercolor="#E3E9F3")

        e.configure("Treeview", background=C.TARJETA, fieldbackground=C.TARJETA, foreground=C.TEXTO,
                    rowheight=26, borderwidth=0)
        e.map("Treeview", background=[("selected", C.ACENTO_SUAVE)], foreground=[("selected", C.TEXTO)])
        e.configure("Treeview.Heading", background=C.PLACA, foreground=C.GRIS, font=self.f_negrita,
                    relief="flat", borderwidth=0, padding=(6, 6))
        e.map("Treeview.Heading", background=[("active", "#E9EEF6")])
        e.configure("Vertical.TScrollbar", background="#E3E9F3", troughcolor=C.TARJETA, borderwidth=0,
                    arrowcolor=C.GRIS, lightcolor="#E3E9F3", darkcolor="#E3E9F3")
        self.raiz.update_idletasks()   # aplica el cambio de tema ya (evita avisos si la ventana se cierra enseguida)

    def tarjeta(self, padre, titulo=None, descripcion=None, **pack):
        """Recuadro blanco con borde suave. Devuelve el marco interior."""
        borde = tk.Frame(padre, bg=C.TARJETA, highlightthickness=1, highlightbackground=C.BORDE,
                         highlightcolor=C.BORDE)
        opciones = {"fill": "x", "pady": (0, 10)}
        opciones.update(pack)
        borde.pack(**opciones)
        interior = ttk.Frame(borde, style="Tarjeta.TFrame", padding=(16, 10))
        interior.pack(fill="both", expand=True)
        if titulo:
            ttk.Label(interior, text=titulo, style="TituloTarjeta.TLabel").pack(anchor="w")
        if descripcion:
            ttk.Label(interior, text=descripcion, style="Gris.TLabel", wraplength=700,
                      justify="left").pack(anchor="w", pady=(2, 0))
        if titulo or descripcion:
            ttk.Frame(interior, style="Tarjeta.TFrame", height=8).pack()
        return interior

    def pagina(self, clave, titulo, subtitulo, desplazable=False):
        """Página con título, subtítulo y, a la derecha, un lugar para botones siempre visibles.
        El contenido va en marco.cuerpo; si es desplazable, tiene barra y rueda del ratón."""
        marco = ttk.Frame(self.contenido, padding=(24, 20, 24, 12))
        self.paginas[clave] = marco
        cabecera = ttk.Frame(marco)
        cabecera.pack(fill="x", pady=(0, 14))
        textos = ttk.Frame(cabecera)
        textos.pack(side="left")
        ttk.Label(textos, text=titulo, style="Titulo.TLabel").pack(anchor="w")
        ttk.Label(textos, text=subtitulo, style="Subtitulo.TLabel").pack(anchor="w")
        marco.acciones = ttk.Frame(cabecera)
        marco.acciones.pack(side="right", anchor="s")
        marco.cuerpo = marco
        if desplazable:
            caja = ttk.Frame(marco)
            caja.pack(fill="both", expand=True)
            lienzo = tk.Canvas(caja, bg=C.FONDO, highlightthickness=0, borderwidth=0)
            barra = ttk.Scrollbar(caja, orient="vertical", command=lienzo.yview)
            lienzo.configure(yscrollcommand=barra.set)
            barra.pack(side="right", fill="y")
            lienzo.pack(side="left", fill="both", expand=True)
            interior = ttk.Frame(lienzo)
            ventana = lienzo.create_window((0, 0), window=interior, anchor="nw")
            interior.bind("<Configure>", lambda e: lienzo.configure(scrollregion=lienzo.bbox("all")))
            lienzo.bind("<Configure>", lambda e: lienzo.itemconfigure(ventana, width=e.width - 6))

            def rueda(evento):
                lienzo.yview_scroll(int(-evento.delta / 120) or (-1 if evento.delta > 0 else 1), "units")
            lienzo.bind("<Enter>", lambda e: self.raiz.bind_all("<MouseWheel>", rueda))
            lienzo.bind("<Leave>", lambda e: self.raiz.unbind_all("<MouseWheel>"))
            marco.cuerpo = interior
            marco.lienzo = lienzo
        return marco

    # ------------------------------------------------------------------
    # Barra lateral
    # ------------------------------------------------------------------

    def _crear_lateral(self):
        lateral = tk.Frame(self.raiz, bg=C.LATERAL, width=212)
        lateral.pack(side="left", fill="y")
        lateral.pack_propagate(False)
        cabecera = tk.Frame(lateral, bg=C.LATERAL)
        cabecera.pack(fill="x", padx=18, pady=(22, 26))
        try:
            self.logo = tk.PhotoImage(file=os.path.join(CARPETA, "carontesync_logo.png"))
            tk.Label(cabecera, image=self.logo, bg=C.LATERAL).pack(side="left")
        except tk.TclError:
            self.logo = None
        textos = tk.Frame(cabecera, bg=C.LATERAL)
        textos.pack(side="left", padx=(10, 0))
        tk.Label(textos, text="CaronteSync", bg=C.LATERAL, fg="#FFFFFF", font=self.f_subtitulo).pack(anchor="w")
        tk.Label(textos, text="respaldos a la nube", bg=C.LATERAL, fg=C.LATERAL_TEXTO,
                 font=self.f_pequena).pack(anchor="w")

        self.navegacion = {}
        for clave, texto in (("estado", "Estado"), ("configuracion", "Configuración"),
                             ("programacion", "Programación"), ("notificaciones", "Notificaciones"),
                             ("historial", "Historial")):
            fila = tk.Frame(lateral, bg=C.LATERAL, cursor="hand2")
            fila.pack(fill="x")
            marca = tk.Frame(fila, bg=C.LATERAL, width=4)
            marca.pack(side="left", fill="y")
            etiqueta = tk.Label(fila, text=texto, bg=C.LATERAL, fg=C.LATERAL_TEXTO, font=self.f_nav,
                                anchor="w", padx=20, pady=11, cursor="hand2")
            etiqueta.pack(side="left", fill="x", expand=True)
            for widget in (fila, etiqueta):
                widget.bind("<Button-1>", lambda evento, c=clave: self.mostrar_pagina(c))
                widget.bind("<Enter>", lambda evento, c=clave: self._resaltar(c, True))
                widget.bind("<Leave>", lambda evento, c=clave: self._resaltar(c, False))
            self.navegacion[clave] = (fila, marca, etiqueta)

        pie = tk.Frame(lateral, bg=C.LATERAL)
        pie.pack(side="bottom", fill="x", padx=18, pady=16)
        pastilla = tk.Frame(pie, bg=C.LATERAL_ACTIVO)
        pastilla.pack(fill="x")
        self.punto_lateral = tk.Canvas(pastilla, width=10, height=10, bg=C.LATERAL_ACTIVO, highlightthickness=0)
        self.punto_lateral.pack(side="left", padx=(10, 6), pady=9)
        self.lbl_lateral = tk.Label(pastilla, text="", bg=C.LATERAL_ACTIVO, fg="#FFFFFF", font=self.f_pequena,
                                    anchor="w")
        self.lbl_lateral.pack(side="left", fill="x")
        tk.Label(pie, text="Versión {0}".format(cs.VERSION), bg=C.LATERAL, fg="#5B6B92",
                 font=self.f_pequena).pack(anchor="w", pady=(8, 0))

    def _resaltar(self, clave, encima):
        fila, marca, etiqueta = self.navegacion[clave]
        if clave == self.pagina_actual:
            return
        color = "#121C3D" if encima else C.LATERAL
        for widget in (fila, etiqueta, marca):
            widget.configure(bg=color)

    def mostrar_pagina(self, clave):
        self.pagina_actual = clave
        for nombre, marco in self.paginas.items():
            if nombre == clave:
                marco.pack(fill="both", expand=True)
            else:
                marco.pack_forget()
        for nombre, (fila, marca, etiqueta) in self.navegacion.items():
            activa = nombre == clave
            fondo = C.LATERAL_ACTIVO if activa else C.LATERAL
            fila.configure(bg=fondo)
            etiqueta.configure(bg=fondo, fg="#FFFFFF" if activa else C.LATERAL_TEXTO,
                               font=self.f_nav_activo if activa else self.f_nav)
            marca.configure(bg=C.ORO if activa else fondo)
        if clave == "programacion":
            self._mostrar_programacion()
        elif clave == "historial":
            self.cargar_historial(True)

    def _punto(self, lienzo, color, fondo):
        lienzo.delete("all")
        lienzo.configure(bg=fondo)
        lienzo.create_oval(1, 1, 9, 9, fill=color, outline=color)

    # ------------------------------------------------------------------
    # Bandeja (junto al reloj)
    # ------------------------------------------------------------------

    def _crear_bandeja(self):
        if bandeja is None:
            return

        def en_tk(funcion):
            # Windows llama a la bandeja desde dentro del bucle de Tk. Llamar a Tk desde ahí
            # (por ejemplo raiz.after) deja a _tkinter sin su estado de hilo y Python aborta
            # ("PyEval_RestoreThread: NULL tstate"): el programa se cerraba al hacer clic en el
            # icono o al abrir el acceso directo. Solo se deja la tarea en la cola, que la
            # ventana atiende cada 150 ms con un temporizador normal de Tk.
            return lambda: self.cola.put(funcion)
        try:
            self.icono_bandeja = bandeja.IconoBandeja(ICONO, "CaronteSync", en_tk(self.mostrar), [
                ("Abrir CaronteSync", en_tk(self.mostrar), None),
                ("Subir ahora", en_tk(self.subir_ahora), lambda: not self._en_curso()),
                ("Detener la subida", en_tk(self.detener), self._en_curso),
                None,
                ("Salir", en_tk(self.salir), None)])
        except OSError:
            self.icono_bandeja = None     # sin bandeja: la ventana se comporta como siempre

    def _en_curso(self):
        return bool(self._ultima_actividad.get("en_curso"))

    def mostrar(self):
        self.raiz.deiconify()
        self.raiz.state("normal")
        self.raiz.lift()
        self.raiz.focus_force()

    def ocultar(self):
        self.raiz.withdraw()
        if self.icono_bandeja is not None and not self.avisado_bandeja:
            self.avisado_bandeja = True
            self.icono_bandeja.aviso("CaronteSync sigue aquí",
                                     "Sigue funcionando junto al reloj. Haga clic en el icono para abrirlo; "
                                     "con el botón derecho puede salir.")

    def _al_minimizar(self, evento):
        if evento.widget is self.raiz and self.icono_bandeja is not None and self.raiz.state() == "iconic":
            self.raiz.after(10, self.ocultar)

    def cerrar(self):
        if self.icono_bandeja is not None:
            self.ocultar()      # como Dropbox: cerrar la ventana la deja junto al reloj
            return
        if self._en_curso():
            if not messagebox.askyesno(
                    "Cerrar", "Hay una subida en curso. Si cierra la ventana la subida sigue en segundo plano.\n\n"
                    "¿Cerrar la ventana?", parent=self.raiz):
                return
        self.salir()

    def salir(self):
        self.detener_temporizadores()
        if self.icono_bandeja is not None:
            self.icono_bandeja.quitar()
            self.icono_bandeja = None
        self.raiz.destroy()

    def _actualizar_bandeja(self, a, en_curso):
        if self.icono_bandeja is None:
            return
        if en_curso:
            total = a.get("archivo_total") or 0
            porcentaje = 100.0 * (a.get("archivo_hecho") or 0) / total if total else 0
            verbo = verbo_fase(a.get("fase")).lower()
            texto = "CaronteSync: {0} {1} ({2:.0f} %, {3}/s)".format(
                verbo, a.get("archivo") or "", porcentaje, cs.legible(int(a.get("velocidad") or 0)))
            if len(texto) > 127:
                texto = "CaronteSync: {0} {1:.0f} % ({2}/s)".format(
                    verbo, porcentaje, cs.legible(int(a.get("velocidad") or 0)))
        elif a.get("resultado"):
            texto = "CaronteSync: última subida {0} ({1})".format(
                a.get("resultado"), fecha_corta(cs.fecha_local(a.get("fin"))) if a.get("fin") else "")
        else:
            texto = "CaronteSync: en espera"
        self.icono_bandeja.actualizar(texto, ICONO_SUBIENDO if en_curso else ICONO)
        if self._ultima_actividad.get("en_curso") and not en_curso and a.get("resultado"):
            resultado = a.get("resultado")
            tipo = {"correcta": bandeja.NIIF_INFO, "detenida": bandeja.NIIF_INFO,
                    "con errores": bandeja.NIIF_WARNING}.get(resultado, bandeja.NIIF_ERROR)
            titulo = {"correcta": "Respaldos en la nube", "detenida": "Subida detenida",
                      "con errores": "Subida con errores"}.get(resultado, "La subida falló")
            self.icono_bandeja.aviso(titulo, a.get("detalle") or resultado, tipo)

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
        texto = str(e) if isinstance(e, (cs.ErrorFatal, cs.ErrorNube)) else cs.describir_error(e)
        messagebox.showerror(titulo, texto, parent=self.raiz)

    # ------------------------------------------------------------------
    # Página Estado
    # ------------------------------------------------------------------

    def _crear_estado(self):
        marco = self.pagina("estado", "Estado", "Lo que CaronteSync está cruzando hacia la nube.")
        principal = self.tarjeta(marco)
        fila = ttk.Frame(principal, style="Tarjeta.TFrame")
        fila.pack(fill="x")
        self.punto_estado = tk.Canvas(fila, width=10, height=10, bg=C.TARJETA, highlightthickness=0)
        self.punto_estado.pack(side="left", padx=(0, 8))
        self.lbl_estado = ttk.Label(fila, text="", style="TituloTarjeta.TLabel")
        self.lbl_estado.pack(side="left")
        self.lbl_origen = ttk.Label(fila, text="", style="Gris.TLabel")
        self.lbl_origen.pack(side="right")

        self.lbl_archivo = ttk.Label(principal, text="", style="Negrita.TLabel")
        self.lbl_archivo.pack(anchor="w", pady=(14, 0))
        self.lbl_indice = ttk.Label(principal, text="", style="Gris.TLabel")
        self.lbl_indice.pack(anchor="w")
        fila = ttk.Frame(principal, style="Tarjeta.TFrame")
        fila.pack(fill="x", pady=(6, 4))
        self.lbl_porcentaje = ttk.Label(fila, text="", style="Enorme.TLabel")
        self.lbl_porcentaje.pack(side="left")
        self.lbl_cantidad = ttk.Label(fila, text="", style="Gris.TLabel")
        self.lbl_cantidad.pack(side="right", anchor="s", pady=(0, 6))
        self.barra_archivo = ttk.Progressbar(principal, maximum=1000, style="Grueso.Horizontal.TProgressbar")
        self.barra_archivo.pack(fill="x")

        placas = ttk.Frame(principal, style="Tarjeta.TFrame")
        placas.pack(fill="x", pady=(14, 14))
        self.lbl_velocidad = self._placa(placas, "VELOCIDAD")
        self.lbl_restante = self._placa(placas, "TIEMPO RESTANTE")
        self.lbl_lote = self._placa(placas, "TOTAL DE ESTA EJECUCIÓN", ultima=True)
        self.barra_lote = ttk.Progressbar(self.lbl_lote.master, maximum=1000, style="Fino.Horizontal.TProgressbar")
        self.barra_lote.pack(fill="x", pady=(6, 0))

        botones = ttk.Frame(principal, style="Tarjeta.TFrame")
        botones.pack(fill="x")
        self.btn_subir = ttk.Button(botones, text="Subir ahora", style="Primario.TButton", command=self.subir_ahora)
        self.btn_subir.pack(side="left")
        self.btn_detener = ttk.Button(botones, text="Detener", style="Peligro.TButton", command=self.detener)
        self.btn_detener.pack(side="left", padx=8)
        ttk.Button(botones, text="Ver qué se subiría", command=self.simular).pack(side="left")
        ttk.Button(botones, text="Abrir carpeta de datos", command=self.abrir_carpeta).pack(side="right")

        resumen = self.tarjeta(marco)
        self.lbl_ultima = ttk.Label(resumen, text="", style="Tarjeta.TLabel", wraplength=700, justify="left")
        self.lbl_ultima.pack(anchor="w")
        self.lbl_proxima = ttk.Label(resumen, text="", style="Gris.TLabel", wraplength=700, justify="left")
        self.lbl_proxima.pack(anchor="w", pady=(4, 0))

        registro = self.tarjeta(marco, "Registro", fill="both", expand=True)
        caja = tk.Frame(registro, bg=C.CONSOLA)
        caja.pack(fill="both", expand=True)
        self.txt_registro = tk.Text(caja, height=8, wrap="none", font=self.f_mono, state="disabled",
                                    bg=C.CONSOLA, fg=C.CONSOLA_TEXTO, relief="flat", borderwidth=0,
                                    padx=10, pady=8, insertbackground=C.CONSOLA_TEXTO)
        barra = ttk.Scrollbar(caja, orient="vertical", command=self.txt_registro.yview)
        self.txt_registro.configure(yscrollcommand=barra.set)
        barra.pack(side="right", fill="y")
        self.txt_registro.pack(fill="both", expand=True)

    def _placa(self, padre, titulo, ultima=False):
        placa = ttk.Frame(padre, style="Placa.TFrame", padding=(12, 9))
        placa.pack(side="left", fill="both", expand=True, padx=(0, 0 if ultima else 10))
        ttk.Label(placa, text=titulo, style="PlacaTitulo.TLabel").pack(anchor="w")
        valor = ttk.Label(placa, text="-", style="PlacaValor.TLabel")
        valor.pack(anchor="w", pady=(2, 0))
        return valor

    def _refrescar(self):
        try:
            self._mostrar_actividad()
            self._mostrar_registro()
            if self.pagina_actual == "historial":
                self.cargar_historial()
        finally:
            self._temporizadores["refresco"] = self.raiz.after(1000, self._refrescar)

    def _mostrar_actividad(self):
        a = cs.leer_actividad(self.ruta_datos("actividad.json"))
        en_curso = bool(a.get("en_curso"))
        if self.proceso is not None and self.proceso.poll() is not None:
            codigo, self.proceso = self.proceso.returncode, None
            if codigo not in (0, 4):
                self._avisar_fallo(codigo)
        if not en_curso:
            self.deteniendo = False

        if en_curso:
            fase = a.get("fase") or "Subiendo"
            texto, color = fase, C.VERDE
            if self.deteniendo:
                texto, color = "Deteniendo... (termina el trozo que está enviando)", C.NARANJA
            self.lbl_origen.configure(text=ORIGENES.get(a.get("origen"), a.get("origen") or ""))
        elif a.get("interrumpida"):
            texto, color = "La última subida se interrumpió; continuará en la próxima ejecución", C.NARANJA
        elif not os.path.exists(self.ruta_config):
            texto, color = "Primero configure el programa en Configuración", C.NARANJA
        else:
            texto, color = "En espera", "#94A3B8"
        if not en_curso:
            self.lbl_origen.configure(text="")
        self.lbl_estado.configure(text=texto)
        self._punto(self.punto_estado, color, C.TARJETA)
        self._punto(self.punto_lateral, color, C.LATERAL_ACTIVO)

        total, hecho = a.get("archivo_total") or 0, a.get("archivo_hecho") or 0
        if en_curso and a.get("archivo"):
            porcentaje = 100.0 * hecho / total if total else 0
            self.lbl_archivo.configure(text=a.get("archivo"))
            self.lbl_indice.configure(text="Archivo {0} de {1}".format(a.get("indice"), a.get("cantidad")))
            self.barra_archivo["value"] = 10 * porcentaje
            self.lbl_porcentaje.configure(text="{0:.1f} %".format(porcentaje))
            self.lbl_cantidad.configure(text="{0} de {1}".format(cs.legible(hecho), cs.legible(total)))
            velocidad = a.get("velocidad") or 0
            if velocidad > 0:
                self.lbl_velocidad.configure(text="{0}/s".format(cs.legible(int(velocidad))))
                self.lbl_restante.configure(text=cs.duracion((total - hecho) / velocidad))
            else:
                self.lbl_velocidad.configure(text="midiendo...")
                self.lbl_restante.configure(text="calculando...")
            lote_total, lote_hecho = a.get("lote_total") or 0, a.get("lote_hecho") or 0
            self.lbl_lote.configure(text="{0} de {1}".format(cs.legible(lote_hecho), cs.legible(lote_total)))
            self.barra_lote["value"] = 1000 * lote_hecho / lote_total if lote_total else 0
            self.lbl_lateral.configure(text="{0} {1:.0f} %".format(verbo_fase(a.get("fase")), porcentaje))
        else:
            self.lbl_archivo.configure(text="No se está subiendo nada")
            self.lbl_indice.configure(text="Use «Subir ahora» o programe la subida automática.")
            self.barra_archivo["value"] = 0
            self.barra_lote["value"] = 0
            self.lbl_porcentaje.configure(text="")
            self.lbl_cantidad.configure(text="")
            for etiqueta in (self.lbl_velocidad, self.lbl_restante, self.lbl_lote):
                etiqueta.configure(text="-")
            self.lbl_lateral.configure(text="En espera" if not a.get("interrumpida") else "Interrumpida")

        if en_curso:
            self.lbl_ultima.configure(text="Esta ejecución comenzó el {0} (hace {1}).".format(
                fecha_corta(cs.fecha_local(a.get("inicio"))), cs.duracion(time.time() - (a.get("inicio") or 0))))
        elif a.get("resultado"):
            fin = cs.fecha_local(a.get("fin")) if a.get("fin") else ""
            self.lbl_ultima.configure(text="Última ejecución ({0}, {1}): {2}. {3}".format(
                fecha_corta(fin), ORIGENES.get(a.get("origen"), ""), a.get("resultado"), a.get("detalle", "")))
        elif not a:
            self.lbl_ultima.configure(text="Todavía no se ha ejecutado ninguna subida.")
        self.btn_subir.configure(state="disabled" if en_curso or self.proceso is not None else "normal")
        self.btn_detener.configure(state="normal" if en_curso and not self.deteniendo else "disabled")
        self._actualizar_bandeja(a, en_curso)
        if not en_curso and self._ultima_actividad.get("en_curso"):
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
        valor = self.guardados.get(("general", "archivo_log")) or "carontesync.log"
        return os.path.normpath(os.path.join(self.base, valor))

    def _avisar_fallo(self, codigo):
        lineas = [linea for linea in leer_cola_archivo(self.ruta_log(), 40).splitlines() if " ERROR " in linea]
        detalle = lineas[-1].split(" ERROR ", 1)[1].strip() if lineas else "Revise el registro."
        messagebox.showwarning("Subida", "La subida terminó con problemas (código {0}):\n\n{1}".format(
            codigo, detalle), parent=self.raiz)

    def subir_ahora(self):
        if self._en_curso() or self.proceso is not None:
            return
        if not self.guardar(silencioso=True):
            return
        try:
            conf = cs.Configuracion(self.ruta_config)
            if not conf.origen or not os.path.isdir(conf.origen):
                raise cs.ErrorFatal("La carpeta de origen no existe: {0}".format(conf.origen or "(vacía)"))
            if not cs.AlmacenTokens(conf.archivo_tokens).obtener(conf.proveedor):
                raise cs.ErrorFatal("Primero vincule la cuenta en Configuración.")
        except cs.ErrorFatal as e:
            self.error("Subir ahora", e)
            return
        argumentos = [python_sin_consola(), os.path.join(CARPETA, "carontesync.py"), "subir",
                      "-c", self.ruta_config, "--origen", "manual"]
        opciones = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            self.proceso = subprocess.Popen(argumentos, cwd=CARPETA, **opciones)
        except OSError as e:
            self.error("Subir ahora", e)
            return
        self.btn_subir.configure(state="disabled")
        self.lbl_estado.configure(text="Iniciando...")

    def detener(self):
        if not self._en_curso():
            return
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
            cs.log.addHandler(manejador)
            cs.log.setLevel("INFO")
            proveedor = None
            try:
                conf = cs.Configuracion(self.ruta_config)
                proveedor = cs.crear_proveedor(conf)
                resumen = cs.ejecutar_subida(conf, proveedor, simular=True)
            finally:
                cs.log.removeHandler(manejador)
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
    # Página Configuración
    # ------------------------------------------------------------------

    def _fila(self, padre, fila, texto):
        ttk.Label(padre, text=texto, style="Tarjeta.TLabel").grid(row=fila, column=0, sticky="w", pady=3,
                                                                  padx=(0, 12))

    def _crear_configuracion(self):
        pagina = self.pagina("configuracion", "Configuración", "Qué subir, a dónde, y con qué cuenta.",
                             desplazable=True)
        marco = pagina.cuerpo
        que = self.tarjeta(marco, "Qué subir")
        cuadricula = ttk.Frame(que, style="Tarjeta.TFrame")
        cuadricula.pack(fill="x")
        cuadricula.columnconfigure(1, weight=1)
        self._fila(cuadricula, 0, "Carpeta de origen")
        ttk.Entry(cuadricula, textvariable=self.var("general", "origen")).grid(row=0, column=1, sticky="ew")
        ttk.Button(cuadricula, text="Examinar...", command=self.elegir_origen).grid(row=0, column=2, padx=(8, 0))
        self._fila(cuadricula, 1, "Archivos")
        patrones = ttk.Frame(cuadricula, style="Tarjeta.TFrame")
        patrones.grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Entry(patrones, textvariable=self.var("general", "patrones"), width=28).pack(side="left")
        ttk.Label(patrones, text="  separados por ';', por ejemplo *.bak;*.trn", style="Gris.TLabel").pack(
            side="left")
        opciones = ttk.Frame(que, style="Tarjeta.TFrame")
        opciones.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(opciones, text="Incluir subcarpetas",
                        variable=self.var("general", "recursivo", True)).pack(anchor="w", pady=2)
        espera = ttk.Frame(opciones, style="Tarjeta.TFrame")
        espera.pack(anchor="w", pady=2)
        ttk.Label(espera, text="No subir archivos modificados hace menos de", style="Tarjeta.TLabel").pack(
            side="left")
        Spinbox(espera, from_=0, to=1440, width=5,
                textvariable=self.var("general", "antiguedad_minima_minutos")).pack(side="left", padx=6)
        ttk.Label(espera, text="minutos: podrían estar escribiéndose. ", style="Tarjeta.TLabel").pack(side="left")
        ttk.Checkbutton(opciones, text="Borrar el archivo local después de subirlo y verificarlo en la nube",
                        variable=self.var("general", "borrar_local_tras_subir", True)).pack(anchor="w", pady=2)

        compresion = self.tarjeta(marco, "Compresión", "Ahorra espacio en la nube y tiempo de subida. Antes de subir, "
                                  "CaronteSync descomprime el .gz y comprueba que es idéntico al original (tamaño y "
                                  "SHA-256); si no lo es, no lo sube ni borra nada.")
        ttk.Checkbutton(compresion, text="Comprimir con gzip antes de subir (los .zip, .7z, .rar y .gz se suben "
                                         "tal cual)", variable=self.var("general", "comprimir", True)).pack(anchor="w")
        cuadricula = ttk.Frame(compresion, style="Tarjeta.TFrame")
        cuadricula.pack(fill="x", pady=(6, 0))
        cuadricula.columnconfigure(1, weight=1)
        self._fila(cuadricula, 0, "Nivel")
        nivel = ttk.Frame(cuadricula, style="Tarjeta.TFrame")
        nivel.grid(row=0, column=1, columnspan=2, sticky="w")
        Spinbox(nivel, from_=1, to=9, width=4, textvariable=self.var("general", "nivel_compresion")).pack(side="left")
        ttk.Label(nivel, text="  1 = más rápido, 9 = archivo más pequeño (6 es un buen equilibrio)",
                  style="Gris.TLabel").pack(side="left")
        self._fila(cuadricula, 1, "Carpeta temporal")
        ttk.Entry(cuadricula, textvariable=self.var("general", "carpeta_temporal")).grid(row=1, column=1, sticky="ew")
        ttk.Button(cuadricula, text="Examinar...", command=self.elegir_temporal).grid(row=1, column=2, padx=(8, 0))
        ttk.Label(cuadricula, text="Vacía = la subcarpeta _carontesync_temporal dentro del origen. Necesita espacio "
                                   "para el archivo comprimido.", style="Gris.TLabel").grid(row=2, column=1,
                                                                                            columnspan=2, sticky="w")

        donde = self.tarjeta(marco, "Dónde subir")
        cuadricula = ttk.Frame(donde, style="Tarjeta.TFrame")
        cuadricula.pack(fill="x")
        cuadricula.columnconfigure(1, weight=1)
        self._fila(cuadricula, 0, "Servicio")
        servicios = ttk.Frame(cuadricula, style="Tarjeta.TFrame")
        servicios.grid(row=0, column=1, sticky="w")
        proveedor = self.var("general", "proveedor")
        ttk.Radiobutton(servicios, text="Dropbox", value="dropbox", variable=proveedor,
                        command=self._mostrar_credenciales).pack(side="left")
        ttk.Radiobutton(servicios, text="Google Drive", value="gdrive", variable=proveedor,
                        command=self._mostrar_credenciales).pack(side="left", padx=16)
        self._fila(cuadricula, 1, "Carpeta en la nube")
        ttk.Entry(cuadricula, textvariable=self.var("general", "carpeta_remota")).grid(row=1, column=1,
                                                                                       sticky="ew")
        self.credenciales = {}
        for servicio, campos in (("dropbox", (("App key", "app_key", ""), ("App secret", "app_secret", "*"))),
                                 ("gdrive", (("ID de cliente", "client_id", ""),
                                             ("Secreto de cliente", "client_secret", "*")))):
            widgets = []
            for fila, (texto, clave, oculto) in enumerate(campos, 2):
                etiqueta = ttk.Label(cuadricula, text=texto, style="Tarjeta.TLabel")
                etiqueta.grid(row=fila, column=0, sticky="w", pady=3)
                entrada = ttk.Entry(cuadricula, show=oculto, textvariable=self.var(servicio, clave))
                entrada.grid(row=fila, column=1, sticky="ew", pady=3)
                widgets.extend((etiqueta, entrada))
            self.credenciales[servicio] = widgets

        cuenta = ttk.Frame(donde, style="Placa.TFrame", padding=(12, 8))
        cuenta.pack(fill="x", pady=(8, 0))
        self.punto_cuenta = tk.Canvas(cuenta, width=10, height=10, bg=C.PLACA, highlightthickness=0)
        self.punto_cuenta.pack(side="left", padx=(0, 8))
        self.lbl_cuenta = ttk.Label(cuenta, text="", style="PlacaValor.TLabel")
        self.lbl_cuenta.pack(side="left")
        ttk.Button(cuenta, text="Probar conexión", command=self.probar).pack(side="right")
        ttk.Button(cuenta, text="Vincular cuenta...", style="Primario.TButton",
                   command=self.vincular).pack(side="right", padx=8)

        avanzado = self.tarjeta(marco, "Avanzado")
        numeros = ttk.Frame(avanzado, style="Tarjeta.TFrame")
        numeros.pack(fill="x")
        for texto, clave, minimo, maximo in (("Bloque (MB)", "tamano_bloque_mb", 1, 128),
                                             ("Reintentos", "reintentos", 0, 50),
                                             ("Espera por petición (s)", "timeout_segundos", 10, 3600)):
            ttk.Label(numeros, text=texto, style="Tarjeta.TLabel").pack(side="left")
            Spinbox(numeros, from_=minimo, to=maximo, width=5,
                    textvariable=self.var("general", clave)).pack(side="left", padx=(6, 22))
        cuadricula = ttk.Frame(avanzado, style="Tarjeta.TFrame")
        cuadricula.pack(fill="x", pady=(8, 0))
        cuadricula.columnconfigure(1, weight=1)
        self._fila(cuadricula, 0, "Proxy")
        ttk.Entry(cuadricula, textvariable=self.var("general", "proxy")).grid(row=0, column=1, sticky="ew")
        ttk.Label(cuadricula, text="host:puerto, vacío si no hay", style="Gris.TLabel").grid(
            row=0, column=2, padx=(8, 0))
        self._fila(cuadricula, 1, "Certificados")
        ttk.Entry(cuadricula, textvariable=self.var("general", "certificados")).grid(row=1, column=1, sticky="ew")
        ttk.Button(cuadricula, text="Examinar...", command=self.elegir_certificados).grid(row=1, column=2,
                                                                                          padx=(8, 0))

        acciones = pagina.acciones
        ttk.Button(acciones, text="Guardar", style="Primario.TButton", command=self.guardar).pack(side="right")
        ttk.Button(acciones, text="Descartar cambios", command=self.cargar_formulario).pack(side="right", padx=8)
        self.lbl_guardado = ttk.Label(acciones, text="", style="Subtitulo.TLabel")
        self.lbl_guardado.pack(side="right", padx=8)

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
        titulo = cs.PROVEEDORES.get(proveedor, cs.Dropbox).titulo
        ruta = os.path.normpath(os.path.join(self.base, self.var("general", "archivo_tokens").get() or "tokens.json"))
        datos = cs.leer_json(ruta).get(proveedor)
        if isinstance(datos, dict) and datos.get("refresh_token"):
            self.lbl_cuenta.configure(text="Cuenta de {0} vinculada ({1})".format(
                titulo, fecha_corta(datos.get("fecha"))))
            self._punto(self.punto_cuenta, C.VERDE, C.PLACA)
        else:
            self.lbl_cuenta.configure(text="Cuenta de {0} sin vincular".format(titulo))
            self._punto(self.punto_cuenta, C.ROJO, C.PLACA)

    def elegir_origen(self):
        carpeta = filedialog.askdirectory(parent=self.raiz, initialdir=self.var("general", "origen").get() or None,
                                          title="Carpeta con los respaldos")
        if carpeta:
            self.var("general", "origen").set(os.path.normpath(carpeta))

    def elegir_temporal(self):
        carpeta = filedialog.askdirectory(parent=self.raiz, title="Carpeta temporal para comprimir",
                                          initialdir=self.var("general", "carpeta_temporal").get() or None)
        if carpeta:
            self.var("general", "carpeta_temporal").set(os.path.normpath(carpeta))

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
        valores[("programacion", "dias")] = ",".join(d[0] for d in cs.DIAS_SEMANA if self.dias[d[0]].get())
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
        self.guardados = cs.leer_valores(self.ruta_config)
        for (seccion, clave), valor in self.guardados.items():
            if seccion == "programacion" and clave in ("dias", "hora", "usuario"):
                continue
            booleana = clave in ("recursivo", "borrar_local_tras_subir", "activa", "comprimir", "activo",
                                 "avisar_exito", "avisar_errores", "avisar_sin_novedades")
            variable = self.var(seccion, clave, booleana)
            if booleana:
                variable.set(valor.lower() in cs.VALORES_SI)
            else:
                variable.set(valor)
        dias = [d.strip().upper() for d in self.guardados[("programacion", "dias")].split(",")]
        for abreviatura, _, _ in cs.DIAS_SEMANA:
            self.dias[abreviatura].set(abreviatura in dias)
        hora = self.guardados[("programacion", "hora")].split(":")
        self.hora_h.set("{0:02d}".format(int(hora[0])) if hora and hora[0].isdigit() else "03")
        self.hora_m.set("{0:02d}".format(int(hora[1])) if len(hora) > 1 and hora[1].isdigit() else "30")
        usuario = self.guardados[("programacion", "usuario")]
        self.var_cuenta.set("usuario" if usuario else "sistema")
        self.var("programacion", "usuario").set(usuario)
        self.var_inicio.set(inicio_con_windows())
        self._mostrar_credenciales()
        self._mostrar_programacion()
        self.lbl_guardado.configure(text="")

    def guardar(self, silencioso=False):
        """Valida y guarda el formulario. Devuelve True si quedó guardado."""
        valores = self.valores_formulario()
        if valores == self.guardados and os.path.exists(self.ruta_config):
            try:
                cs.Configuracion(self.ruta_config)
                return True
            except cs.ErrorFatal as e:
                self.error("Configuración", e)
                return False
        try:
            cs.guardar_configuracion(self.ruta_config, valores)
        except (cs.ErrorFatal, OSError) as e:
            self.error("No se guardó la configuración", e)
            return False
        self.guardados = cs.leer_valores(self.ruta_config)
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
        return cs.crear_proveedor(cs.Configuracion(self.ruta_config))

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
        except cs.ErrorFatal as e:
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
    # Página Programación
    # ------------------------------------------------------------------

    def _crear_programacion(self):
        marco = self.pagina("programacion", "Programación",
                            "Windows ejecuta la subida a la hora elegida, aunque nadie haya iniciado sesión.")
        activa = self.tarjeta(marco)
        ttk.Checkbutton(activa, text="Subir automáticamente con el Programador de tareas de Windows",
                        variable=self.var("programacion", "activa", True),
                        command=self._habilitar_programacion).pack(anchor="w")
        ttk.Label(activa, text="Elija una hora posterior al término de los respaldos de SQL Server.",
                  style="Gris.TLabel").pack(anchor="w", padx=(22, 0))

        self.marco_cuando = self.tarjeta(marco, "Cuándo")
        frecuencia = self.var("programacion", "frecuencia")
        cuadricula = ttk.Frame(self.marco_cuando, style="Tarjeta.TFrame")
        cuadricula.pack(fill="x")
        ttk.Radiobutton(cuadricula, text="Todos los días", value="diaria", variable=frecuencia,
                        command=self._habilitar_programacion).grid(row=0, column=0, sticky="w", pady=3)
        ttk.Radiobutton(cuadricula, text="Algunos días:", value="semanal", variable=frecuencia,
                        command=self._habilitar_programacion).grid(row=1, column=0, sticky="w", pady=3)
        dias = ttk.Frame(cuadricula, style="Tarjeta.TFrame")
        dias.grid(row=1, column=1, sticky="w")
        self.dias = {}
        self.chk_dias = []
        for abreviatura, _, nombre in cs.DIAS_SEMANA:
            self.dias[abreviatura] = tk.BooleanVar()
            casilla = ttk.Checkbutton(dias, text=abreviatura.capitalize(), variable=self.dias[abreviatura])
            casilla.pack(side="left", padx=(0, 8))
            self.chk_dias.append(casilla)
        ttk.Radiobutton(cuadricula, text="Cada", value="horas", variable=frecuencia,
                        command=self._habilitar_programacion).grid(row=2, column=0, sticky="w", pady=3)
        horas = ttk.Frame(cuadricula, style="Tarjeta.TFrame")
        horas.grid(row=2, column=1, sticky="w")
        self.spin_horas = Spinbox(horas, from_=1, to=23, width=4, textvariable=self.var("programacion", "cada_horas"))
        self.spin_horas.pack(side="left")
        ttk.Label(horas, text="horas (útil para los respaldos del registro de transacciones .trn)",
                  style="Gris.TLabel").pack(side="left", padx=8)
        ttk.Label(cuadricula, text="Hora de inicio", style="Negrita.TLabel").grid(row=3, column=0, sticky="w",
                                                                                  pady=(12, 0))
        hora = ttk.Frame(cuadricula, style="Tarjeta.TFrame")
        hora.grid(row=3, column=1, sticky="w", pady=(12, 0))
        self.hora_h = tk.StringVar(value="03")
        self.hora_m = tk.StringVar(value="30")
        Spinbox(hora, from_=0, to=23, width=3, format="%02.0f", textvariable=self.hora_h, wrap=True).pack(
            side="left")
        ttk.Label(hora, text=" : ", style="Negrita.TLabel").pack(side="left")
        Spinbox(hora, from_=0, to=59, width=3, format="%02.0f", textvariable=self.hora_m, wrap=True).pack(
            side="left")

        self.marco_cuenta = self.tarjeta(marco, "Cuenta de Windows que ejecuta la tarea")
        self.var_cuenta = tk.StringVar(value="sistema")
        ttk.Radiobutton(self.marco_cuenta, text="Cuenta del sistema (SYSTEM): no pide contraseña",
                        value="sistema", variable=self.var_cuenta,
                        command=self._habilitar_programacion).pack(anchor="w", pady=2)
        fila = ttk.Frame(self.marco_cuenta, style="Tarjeta.TFrame")
        fila.pack(anchor="w", pady=2)
        ttk.Radiobutton(fila, text="Esta cuenta:", value="usuario", variable=self.var_cuenta,
                        command=self._habilitar_programacion).pack(side="left")
        self.ent_usuario = ttk.Entry(fila, width=30, textvariable=self.var("programacion", "usuario"))
        self.ent_usuario.pack(side="left", padx=8)
        ttk.Label(self.marco_cuenta, text="DOMINIO\\usuario. Necesaria si la carpeta de origen está en otra máquina "
                                          "de la red.", style="Gris.TLabel").pack(anchor="w", padx=(22, 0))

        tarea = self.tarjeta(marco, "Tarea en Windows")
        fila = ttk.Frame(tarea, style="Tarjeta.TFrame")
        fila.pack(fill="x")
        self.punto_tarea = tk.Canvas(fila, width=10, height=10, bg=C.TARJETA, highlightthickness=0)
        self.punto_tarea.pack(side="left", padx=(0, 8))
        self.lbl_tarea = ttk.Label(fila, text="", style="Tarjeta.TLabel", wraplength=560, justify="left")
        self.lbl_tarea.pack(side="left")
        ttk.Button(fila, text="Aplicar programación", style="Primario.TButton",
                   command=self.aplicar_programacion).pack(side="right")
        self.var_inicio = tk.BooleanVar(value=False)
        self.chk_inicio = ttk.Checkbutton(tarea, text="Mostrar CaronteSync junto al reloj al iniciar sesión en "
                                                      "Windows", variable=self.var_inicio,
                                          command=self._cambiar_inicio)
        self.chk_inicio.pack(anchor="w", pady=(10, 0))
        if os.name != "nt":
            self.chk_inicio.configure(state="disabled")
        ttk.Label(tarea, text="También puede lanzar la subida desde un trabajo del Agente SQL Server (paso CmdExec "
                              "que llama a carontesync.bat), por ejemplo al final del trabajo de respaldo.",
                  style="Gris.TLabel", wraplength=700, justify="left").pack(anchor="w", pady=(8, 0))

    def _cambiar_inicio(self):
        try:
            cambiar_inicio_con_windows(self.var_inicio.get())
        except OSError as e:
            self.var_inicio.set(inicio_con_windows())
            self.error("Inicio con Windows", e)

    def _habilitar_programacion(self):
        activa = self.var("programacion", "activa", True).get()
        frecuencia = self.var("programacion", "frecuencia").get()

        def estado(condicion):
            return "normal" if activa and condicion else "disabled"

        def recorrer(widget):
            for hijo in widget.winfo_children():
                if isinstance(hijo, (ttk.Radiobutton, ttk.Label)):
                    hijo.configure(state=estado(True))
                recorrer(hijo)
        recorrer(self.marco_cuando)
        recorrer(self.marco_cuenta)
        for casilla in self.chk_dias:
            casilla.configure(state=estado(frecuencia == "semanal"))
        self.spin_horas.configure(state=estado(frecuencia == "horas"))
        self.ent_usuario.configure(state=estado(self.var_cuenta.get() == "usuario"))

    def _mostrar_programacion(self):
        self._habilitar_programacion()
        try:
            programacion = cs.Configuracion(self.ruta_config).programacion
        except cs.ErrorFatal:
            programacion = None
        existe = cs.tarea_existe()
        color = "#94A3B8"
        if os.name != "nt":
            texto = "La tarea programada solo existe en Windows."
        elif existe and programacion is not None and programacion.activa:
            texto, color = "Creada. " + programacion.descripcion(), C.VERDE
        elif existe:
            texto, color = ("Existe una tarea '{0}' en Windows, pero la subida automática está desactivada. "
                            "Pulse «Aplicar programación» para quitarla.".format(cs.NOMBRE_TAREA), C.NARANJA)
        elif programacion is not None and programacion.activa:
            texto, color = ("La configuración pide subida automática pero la tarea no existe en Windows: "
                            "pulse «Aplicar programación».", C.NARANJA)
        else:
            texto = "No hay subida automática."
        self.lbl_tarea.configure(text=texto)
        self._punto(self.punto_tarea, color, C.TARJETA)
        if programacion is not None:
            self.lbl_proxima.configure(text="Subida automática: " + programacion.descripcion()
                                       + ("" if existe or os.name != "nt" or not programacion.activa
                                          else " (falta aplicarla en Programación)"))

    def aplicar_programacion(self):
        if not self.guardar(silencioso=True):
            return
        conf = cs.Configuracion(self.ruta_config)
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
        self.en_hilo(lambda: cs.aplicar_programacion(conf.programacion, conf.ruta, clave), al_terminar)

    # ------------------------------------------------------------------
    # Página Notificaciones (Telegram)
    # ------------------------------------------------------------------

    def _crear_notificaciones(self):
        marco = self.pagina("notificaciones", "Notificaciones",
                            "Reciba en Telegram un mensaje al terminar cada subida.", desplazable=True).cuerpo
        tarjeta = self.tarjeta(marco, "Telegram")
        ttk.Checkbutton(tarjeta, text="Enviar avisos por Telegram",
                        variable=self.var("telegram", "activo", True)).pack(anchor="w")
        cuadricula = ttk.Frame(tarjeta, style="Tarjeta.TFrame")
        cuadricula.pack(fill="x", pady=(8, 0))
        cuadricula.columnconfigure(1, weight=1)
        self._fila(cuadricula, 0, "Token del bot")
        ttk.Entry(cuadricula, show="*", textvariable=self.var("telegram", "token")).grid(row=0, column=1,
                                                                                         sticky="ew")
        self._fila(cuadricula, 1, "Chat ID")
        ttk.Entry(cuadricula, textvariable=self.var("telegram", "chat_id")).grid(row=1, column=1, sticky="ew")
        ttk.Button(cuadricula, text="Detectar", command=self.detectar_chat).grid(row=1, column=2, padx=(8, 0))
        self._fila(cuadricula, 2, "Nombre del servidor")
        ttk.Entry(cuadricula, textvariable=self.var("telegram", "nombre_servidor")).grid(row=2, column=1,
                                                                                         sticky="ew")
        ttk.Label(cuadricula, text="vacío = nombre del equipo", style="Gris.TLabel").grid(row=2, column=2,
                                                                                          padx=(8, 0))
        cuando = ttk.Frame(tarjeta, style="Tarjeta.TFrame")
        cuando.pack(fill="x", pady=(10, 0))
        ttk.Label(cuando, text="Avisar:", style="Negrita.TLabel").pack(anchor="w")
        for clave, texto in (("avisar_exito", "Cuando la subida termina bien"),
                             ("avisar_errores", "Cuando hay errores, la subida falla o se detiene"),
                             ("avisar_sin_novedades", "También cuando no había nada nuevo que subir")):
            ttk.Checkbutton(cuando, text=texto, variable=self.var("telegram", clave, True)).pack(anchor="w", pady=1)
        botones = ttk.Frame(tarjeta, style="Tarjeta.TFrame")
        botones.pack(fill="x", pady=(10, 0))
        ttk.Button(botones, text="Enviar mensaje de prueba", style="Primario.TButton",
                   command=self.probar_telegram).pack(side="left")
        ttk.Button(botones, text="Guardar", command=self.guardar).pack(side="right")

        ayuda = self.tarjeta(marco, "Cómo crear el bot (una sola vez)")
        for paso in ("1. En Telegram (en el teléfono o en la PC), busque @BotFather, envíele /newbot y siga los "
                     "pasos. Al final le da un token parecido a 123456789:AAH...",
                     "2. Pegue ese token arriba.",
                     "3. Abra el chat con su bot y envíele cualquier mensaje (o agréguelo a un grupo y escriba "
                     "algo en el grupo, para que avise a todo el equipo).",
                     "4. Pulse «Detectar»: CaronteSync encuentra el chat ID.",
                     "5. Marque «Enviar avisos por Telegram», pulse «Enviar mensaje de prueba» y luego «Guardar»."):
            ttk.Label(ayuda, text=paso, style="Tarjeta.TLabel", wraplength=680, justify="left").pack(anchor="w",
                                                                                                     pady=2)

    def _telegram(self):
        """Cliente de Telegram con lo que está escrito en el formulario (aunque no esté guardado)."""
        token = self.var("telegram", "token").get().strip()
        if not token:
            raise cs.ErrorFatal("Primero pegue el token del bot (se lo da @BotFather).")
        certificados = self.var("general", "certificados").get().strip()
        if certificados:
            certificados = os.path.normpath(os.path.join(self.base, certificados))
        http = cs.ClienteHttp(certificados, self.var("general", "proxy").get().strip(), 30)
        return cs.Telegram(http, token, self.var("telegram", "chat_id").get().strip())

    def detectar_chat(self):
        try:
            telegram = self._telegram()
        except cs.ErrorFatal as e:
            self.error("Telegram", e)
            return

        def trabajo():
            try:
                return telegram.chats()
            finally:
                telegram.http.cerrar()

        def al_terminar(ok, chats):
            if not ok:
                self.error("Telegram", chats)
            elif not chats:
                messagebox.showinfo("Telegram", "El bot todavía no recibió mensajes.\n\nAbra el chat con su bot en "
                                                "Telegram, envíele cualquier mensaje y vuelva a pulsar «Detectar».",
                                    parent=self.raiz)
            elif len(chats) == 1:
                self.var("telegram", "chat_id").set(chats[0][0])
                messagebox.showinfo("Telegram", "Chat encontrado: {0} ({1}).".format(chats[0][1], chats[0][0]),
                                    parent=self.raiz)
            else:
                ElegirChat(self, chats)
        self.en_hilo(trabajo, al_terminar)

    def probar_telegram(self):
        try:
            telegram = self._telegram()
            if not telegram.chat_id:
                raise cs.ErrorFatal("Falta el chat ID: pulse «Detectar».")
        except cs.ErrorFatal as e:
            self.error("Telegram", e)
            return
        servidor = self.var("telegram", "nombre_servidor").get().strip() or __import__("socket").gethostname()

        def trabajo():
            try:
                telegram.enviar(u"\U0001f514 Prueba de CaronteSync desde {0}: los avisos funcionan.".format(servidor))
            finally:
                telegram.http.cerrar()

        def al_terminar(ok, resultado):
            if ok:
                messagebox.showinfo("Telegram", "Mensaje enviado. Revise Telegram.", parent=self.raiz)
            else:
                self.error("Telegram", resultado)
        self.en_hilo(trabajo, al_terminar)

    def descomprimir(self):
        origen = filedialog.askopenfilename(parent=self.raiz, title="Respaldo comprimido (.gz) a restaurar",
                                            filetypes=[("Respaldos comprimidos", "*.gz"), ("Todos", "*.*")])
        if not origen:
            return
        sugerido = os.path.basename(origen)[:-3] if origen.lower().endswith(".gz") else os.path.basename(origen) + \
            ".descomprimido"
        destino = filedialog.asksaveasfilename(parent=self.raiz, title="Guardar el respaldo descomprimido como",
                                               initialdir=os.path.dirname(origen), initialfile=sugerido)
        if destino:
            DialogoDescompresion(self, origen, os.path.normpath(destino))

    # ------------------------------------------------------------------
    # Página Historial
    # ------------------------------------------------------------------

    def _crear_historial(self):
        marco = self.pagina("historial", "Historial", "Cada ejecución y cada archivo cruzado a la nube, "
                                                      "los más recientes primero.")
        ejecuciones = self.tarjeta(marco, "Ejecuciones")
        self.arbol_ejecuciones = self._tabla(ejecuciones, 6, (
            ("fecha", "Inicio", 115), ("origen", "Origen", 130), ("resultado", "Resultado", 95),
            ("subidos", "Subidos", 65), ("bytes", "Datos", 80), ("detalle", "Detalle", 300)))
        archivos = self.tarjeta(marco, "Archivos", fill="both", expand=True)
        self.arbol_archivos = self._tabla(archivos, 10, (
            ("fecha", "Fecha", 115), ("archivo", "Archivo", 200), ("tamano", "Tamaño", 75),
            ("duracion", "Duración", 85), ("velocidad", "Velocidad", 80), ("resultado", "Resultado", 75),
            ("detalle", "Detalle", 200)))
        ttk.Button(marco.acciones, text="Actualizar", command=lambda: self.cargar_historial(True)).pack(
            side="right")
        ttk.Button(marco.acciones, text="Descomprimir un .gz...", command=self.descomprimir).pack(side="right",
                                                                                                  padx=8)
        ttk.Label(marco.acciones, text="Doble clic en una fila: detalle completo", style="Subtitulo.TLabel").pack(
            side="right", padx=10)

    def _tabla(self, padre, alto, columnas):
        caja = ttk.Frame(padre, style="Tarjeta.TFrame")
        caja.pack(fill="both", expand=True)
        arbol = ttk.Treeview(caja, columns=[c[0] for c in columnas], show="headings", height=alto)
        for nombre, titulo, ancho in columnas:
            arbol.heading(nombre, text=titulo, anchor="w")
            arbol.column(nombre, width=ancho, anchor="w", stretch=nombre == "detalle")
        barra = ttk.Scrollbar(caja, orient="vertical", command=arbol.yview)
        arbol.configure(yscrollcommand=barra.set)
        barra.pack(side="right", fill="y")
        arbol.pack(fill="both", expand=True)
        arbol.tag_configure("par", background=C.PLACA)
        arbol.tag_configure("error", foreground=C.ROJO)
        arbol.tag_configure("aviso", foreground=C.NARANJA)
        arbol.tag_configure("bien", foreground=C.VERDE)
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
        entradas = cs.Historial.leer(ruta, 2000)
        for arbol in (self.arbol_ejecuciones, self.arbol_archivos):
            arbol.delete(*arbol.get_children())
        filas = {"ejecucion": 0, "archivo": 0}
        for e in reversed(entradas):
            tipo = e.get("tipo")
            if tipo not in filas:
                continue
            etiquetas = ["par"] if filas[tipo] % 2 else []
            filas[tipo] += 1
            if e.get("resultado") in ("error", "fallida"):
                etiquetas.append("error")
            elif e.get("resultado") in ("detenido", "detenida", "con errores"):
                etiquetas.append("aviso")
            if tipo == "ejecucion":
                self.arbol_ejecuciones.insert("", "end", tags=etiquetas, values=(
                    fecha_corta(e.get("fecha")), ORIGENES.get(e.get("origen"), e.get("origen")), e.get("resultado"),
                    e.get("subidos", 0), cs.legible(e.get("bytes", 0)), e.get("detalle", "")))
            else:
                segundos = e.get("segundos") or 0
                velocidad = cs.legible(int(e.get("tamano", 0) / segundos)) + "/s" \
                    if segundos and e.get("resultado") == "subido" else ""
                detalle = e.get("detalle", "")
                if e.get("verificado") and not detalle:
                    detalle = "Suma de verificación correcta."
                self.arbol_archivos.insert("", "end", tags=etiquetas, values=(
                    fecha_corta(e.get("fecha")), e.get("archivo"), cs.legible(e.get("tamano", 0)),
                    cs.duracion(segundos), velocidad, e.get("resultado"), detalle))


# ---------------------------------------------------------------------------
# Ventanas auxiliares
# ---------------------------------------------------------------------------

class ListaRegistro(logging.Handler):
    def __init__(self, destino):
        logging.Handler.__init__(self)
        self.destino = destino

    def emit(self, registro):
        self.destino.append(registro.getMessage())


class Dialogo(tk.Toplevel):
    def __init__(self, padre, titulo):
        tk.Toplevel.__init__(self, padre)
        self.title(titulo)
        self.configure(bg=C.TARJETA)
        try:
            self.iconbitmap(ICONO)
        except tk.TclError:
            pass
        self.transient(padre)
        self.cuerpo = ttk.Frame(self, style="Tarjeta.TFrame", padding=20)
        self.cuerpo.pack(fill="both", expand=True)


class VentanaTexto(Dialogo):
    def __init__(self, padre, titulo, texto):
        Dialogo.__init__(self, padre, titulo)
        self.geometry("720x420")
        caja = tk.Text(self.cuerpo, wrap="word", relief="flat", bg=C.PLACA, padx=10, pady=8,
                       font=("Courier New", 9), borderwidth=0)
        caja.insert("end", texto)
        caja.configure(state="disabled")
        caja.pack(fill="both", expand=True)
        ttk.Button(self.cuerpo, text="Cerrar", command=self.destroy).pack(anchor="e", pady=(12, 0))


class ElegirChat(Dialogo):
    def __init__(self, app, chats):
        Dialogo.__init__(self, app.raiz, "Elegir el chat de Telegram")
        self.app = app
        self.chats = chats
        ttk.Label(self.cuerpo, text="El bot recibió mensajes de varios chats. ¿A cuál se envían los avisos?",
                  style="Tarjeta.TLabel").pack(anchor="w")
        self.lista = tk.Listbox(self.cuerpo, height=min(8, len(chats)), width=50, relief="flat",
                                highlightthickness=1, highlightbackground=C.BORDE)
        for chat_id, nombre in chats:
            self.lista.insert("end", u"{0}  ({1})".format(nombre or "sin nombre", chat_id))
        self.lista.selection_set(0)
        self.lista.pack(fill="x", pady=10)
        ttk.Button(self.cuerpo, text="Usar este chat", style="Primario.TButton", command=self.elegir).pack(anchor="e")
        self.grab_set()

    def elegir(self):
        seleccion = self.lista.curselection()
        if seleccion:
            self.app.var("telegram", "chat_id").set(self.chats[seleccion[0]][0])
        self.destroy()


class DialogoDescompresion(Dialogo):
    """Descomprime un .gz para restaurarlo; gzip comprueba el CRC-32 y el tamaño."""

    def __init__(self, app, origen, destino):
        Dialogo.__init__(self, app.raiz, "Descomprimir respaldo")
        self.app = app
        self.resizable(False, False)
        ttk.Label(self.cuerpo, text=u"{0}\n\u2192 {1}".format(origen, destino), style="Tarjeta.TLabel",
                  wraplength=520, justify="left").pack(anchor="w")
        self.barra = ttk.Progressbar(self.cuerpo, mode="indeterminate", length=520,
                                     style="Ocupado.Horizontal.TProgressbar")
        self.barra.pack(pady=12)
        self.barra.start(15)
        self.lbl = ttk.Label(self.cuerpo, text="Descomprimiendo...", style="Gris.TLabel")
        self.lbl.pack(anchor="w")
        self.protocol("WM_DELETE_WINDOW", lambda: None)    # espera a que termine
        self.grab_set()

        def avance(total):
            app.cola.put(lambda: self.winfo_exists() and self.lbl.configure(
                text="{0} descomprimidos...".format(cs.legible(total))))

        def al_terminar(ok, resultado):
            self.destroy()
            if ok:
                messagebox.showinfo("Descomprimir", "Listo: {0} restaurados y comprobados (CRC-32 y tamaño "
                                                    "correctos).\n\n{1}".format(cs.legible(resultado), destino),
                                    parent=app.raiz)
            else:
                app.error("Descomprimir", resultado)
        app.en_hilo(lambda: cs.descomprimir(origen, destino, avance), al_terminar)


class DialogoDropbox(Dialogo):
    def __init__(self, app, proveedor):
        Dialogo.__init__(self, app.raiz, "Vincular cuenta de Dropbox")
        self.app = app
        self.proveedor = proveedor
        self.resizable(False, False)
        cuerpo = self.cuerpo
        url = proveedor.url_autorizacion()
        ttk.Label(cuerpo, text="Vincular Dropbox", style="TituloTarjeta.TLabel").pack(anchor="w")
        ttk.Label(cuerpo, text="1. Abra esta dirección en el navegador de otro equipo o del teléfono (el Internet "
                               "Explorer de Windows 2003 no puede abrir Dropbox). Si está conectado por Escritorio "
                               "remoto, cópiela y péguela en su equipo:", style="Tarjeta.TLabel", wraplength=520,
                  justify="left").pack(anchor="w", pady=(10, 4))
        fila = ttk.Frame(cuerpo, style="Tarjeta.TFrame")
        fila.pack(fill="x", pady=4)
        direccion = ttk.Entry(fila, width=62)
        direccion.insert(0, url)
        direccion.configure(state="readonly")
        direccion.pack(side="left", fill="x", expand=True)
        ttk.Button(fila, text="Copiar", command=lambda: self._copiar(url)).pack(side="left", padx=(8, 0))
        ttk.Label(cuerpo, text="2. Inicie sesión en Dropbox y pulse «Permitir».", style="Tarjeta.TLabel").pack(
            anchor="w", pady=(10, 0))
        ttk.Label(cuerpo, text="3. Copie el código que muestra Dropbox y péguelo aquí:", style="Tarjeta.TLabel").pack(
            anchor="w", pady=(6, 4))
        self.codigo = ttk.Entry(cuerpo, width=50)
        self.codigo.pack(anchor="w")
        self.lbl = ttk.Label(cuerpo, text="", style="Gris.TLabel")
        self.lbl.pack(anchor="w", pady=(6, 0))
        botones = ttk.Frame(cuerpo, style="Tarjeta.TFrame")
        botones.pack(fill="x", pady=(12, 0))
        ttk.Button(botones, text="Cancelar", command=self.destroy).pack(side="right")
        self.btn = ttk.Button(botones, text="Vincular", style="Primario.TButton", command=self.vincular)
        self.btn.pack(side="right", padx=8)
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


class DialogoGoogle(Dialogo):
    def __init__(self, app, proveedor):
        Dialogo.__init__(self, app.raiz, "Vincular cuenta de Google Drive")
        self.app = app
        self.proveedor = proveedor
        self.cancelado = False
        self.resizable(False, False)
        cuerpo = self.cuerpo
        ttk.Label(cuerpo, text="Vincular Google Drive", style="TituloTarjeta.TLabel").pack(anchor="w")
        self.lbl_paso1 = ttk.Label(cuerpo, text="Pidiendo un código a Google...", style="Tarjeta.TLabel",
                                   wraplength=480, justify="left")
        self.lbl_paso1.pack(anchor="w", pady=(10, 0))
        self.lbl_codigo = tk.Label(cuerpo, text="", font=app.f_enorme, bg=C.PLACA, fg=C.ACENTO, padx=24, pady=8)
        self.lbl_codigo.pack(pady=12)
        ttk.Label(cuerpo, text="2. Escriba ese código, elija la cuenta de Google y acepte los permisos. Esta ventana "
                               "se cerrará sola cuando termine.", style="Tarjeta.TLabel", wraplength=480,
                  justify="left").pack(anchor="w")
        self.barra = ttk.Progressbar(cuerpo, mode="indeterminate", length=480, style="Ocupado.Horizontal.TProgressbar")
        self.barra.pack(pady=12)
        self.barra.start(15)
        ttk.Button(cuerpo, text="Cancelar", command=self.cancelar).pack(anchor="e")
        self.protocol("WM_DELETE_WINDOW", self.cancelar)
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
    ruta = cs.ruta_config_predeterminada()
    pagina = None
    en_bandeja = False
    while argumentos:
        opcion = argumentos.pop(0)
        if opcion in ("-c", "--config") and argumentos:
            ruta = argumentos.pop(0)
        elif opcion == "--pagina" and argumentos:
            pagina = argumentos.pop(0)
        elif opcion == "--bandeja":
            en_bandeja = True
    if bandeja is not None and bandeja.mostrar_instancia_abierta():
        return     # ya estaba abierto (quizá junto al reloj): se mostró esa ventana
    raiz = tk.Tk()
    app = Aplicacion(raiz, ruta, en_bandeja)
    if pagina is None and not os.path.exists(ruta):
        pagina = "configuracion"     # la primera vez se abre directamente la configuración
    if pagina:
        app.mostrar_pagina(pagina)
    raiz.mainloop()


if __name__ == "__main__":
    main()
