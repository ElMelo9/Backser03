# -*- coding: utf-8 -*-
"""
subir_nube.py - Motor de SubirNube: copia los archivos de una carpeta local a
una carpeta en Dropbox o Google Drive.

Pensado para Windows Server 2003 (por ejemplo, un servidor con SQL Server
2000/2005 que deja sus respaldos .bak en disco). Windows 2003 no habla
TLS 1.2, que es lo que exigen Dropbox y Google, así que el programa se
ejecuta con Python 3.4.4 (la última versión con instalador para XP/2003),
que trae su propio OpenSSL. Solo usa la librería estándar de Python y
funciona igual con cualquier Python 3 más moderno.

La ventana (subir_nube_gui.pyw) usa este módulo; también se puede usar solo:
    python subir_nube.py autorizar        Vincula la cuenta de Dropbox o Google Drive.
    python subir_nube.py probar           Comprueba la conexión y muestra la cuenta.
    python subir_nube.py subir            Sube los archivos nuevos o modificados.
    python subir_nube.py subir --simular  Muestra qué se subiría, sin subir nada.
    python subir_nube.py programar        Crea o quita la tarea programada de Windows.

Opciones:
    -c / --config RUTA   Archivo de configuración (por defecto, subir_nube.ini
                         en la misma carpeta que este script).
    -v / --detallado     Registra también los mensajes de depuración.

Códigos de salida: 0 = todo bien, 1 = algún archivo no se pudo subir,
2 = error de configuración, autorización o certificados, 3 = error inesperado,
4 = el usuario detuvo la subida (continuará en la próxima ejecución).
"""

import argparse
import base64
import collections
import configparser
import datetime
import fnmatch
import getpass
import hashlib
import http.client
import json
import logging
import logging.handlers
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse

VERSION = "2.0"
MB = 1024 * 1024
NOMBRE_TAREA = "SubirNube"
TROZO_ENVIO = 256 * 1024    # cada bloque se envía en trozos para informar el avance

log = logging.getLogger("subir_nube")

# Las pruebas lo reemplazan para no esperar de verdad entre reintentos.
_dormir = time.sleep


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------

class ErrorFatal(Exception):
    """Impide continuar: configuración, autorización o certificados."""


class ErrorNube(Exception):
    """Error del servicio con un archivo; los demás archivos siguen subiéndose."""


class ErrorTransitorio(Exception):
    """Error de red o del servidor que puede desaparecer si se reintenta."""

    def __init__(self, mensaje, espera=None):
        Exception.__init__(self, mensaje)
        self.espera = espera


class Detenido(Exception):
    """El usuario pidió detener la subida; continuará en la próxima ejecución."""


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def legible(n):
    """Tamaño en bytes en formato legible (1.5 GB)."""
    if n < 1024:
        return "{0} B".format(n)
    valor = float(n)
    for unidad in ("KB", "MB", "GB", "TB"):
        valor /= 1024
        if valor < 1024 or unidad == "TB":
            return "{0:.1f} {1}".format(valor, unidad)


def duracion(segundos):
    segundos = int(segundos)
    if segundos < 60:
        return "{0} s".format(segundos)
    if segundos < 3600:
        return "{0} min {1} s".format(segundos // 60, segundos % 60)
    return "{0} h {1} min".format(segundos // 3600, segundos % 3600 // 60)


def fecha_iso(marca):
    """Fecha UTC sin fracciones de segundo, como la aceptan Dropbox y Google Drive."""
    fecha = datetime.datetime.fromtimestamp(int(marca), datetime.timezone.utc)
    return fecha.strftime("%Y-%m-%dT%H:%M:%SZ")


def fecha_local(marca=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(marca))


def formulario(campos):
    return urllib.parse.urlencode(campos).encode("ascii")


def json_bytes(objeto):
    return json.dumps(objeto).encode("utf-8")


CABECERA_FORMULARIO = {"Content-Type": "application/x-www-form-urlencoded"}
CABECERA_JSON = {"Content-Type": "application/json; charset=UTF-8"}


def partes_remotas(carpeta):
    """'/Respaldos/SQL' o 'Respaldos\\SQL' -> ['Respaldos', 'SQL']"""
    return [p.strip() for p in carpeta.replace("\\", "/").split("/") if p.strip()]


def normalizar_remota(carpeta):
    return "/" + "/".join(partes_remotas(carpeta))


def describir_error(e):
    texto = str(e)
    return "{0}: {1}".format(type(e).__name__, texto) if texto else type(e).__name__


def borrar_si_existe(ruta):
    try:
        os.remove(ruta)
    except OSError:
        pass


def escribir_json(ruta, datos):
    """Escribe en un temporal y lo renombra, para no dejar el archivo a medias."""
    temporal = ruta + ".tmp"
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(temporal, ruta)


def leer_json(ruta):
    """Contenido de un archivo JSON, o {} si no existe o no se puede leer."""
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

VALORES_SI = ("1", "si", "sí", "s", "yes", "y", "true", "verdadero", "on")
VALORES_NO = ("0", "no", "n", "false", "falso", "off", "")

# Días para la tarea programada: (abreviatura en español, la que usa schtasks, nombre).
DIAS_SEMANA = (("LUN", "MON", "lunes"), ("MAR", "TUE", "martes"), ("MIE", "WED", "miércoles"),
               ("JUE", "THU", "jueves"), ("VIE", "FRI", "viernes"), ("SAB", "SAT", "sábados"),
               ("DOM", "SUN", "domingos"))

# Todos los parámetros del .ini: (sección, clave, valor por defecto, comentario).
CAMPOS_CONFIG = (
    ("general", "proveedor", "dropbox", "Servicio de destino: dropbox o gdrive (Google Drive)."),
    ("general", "origen", "", "Carpeta local con los archivos a subir (por ejemplo, los respaldos de SQL Server)."),
    ("general", "carpeta_remota", "/Respaldos/SQL", "Carpeta de destino en la nube. Se crea si no existe."),
    ("general", "patrones", "*.bak;*.trn", "Qué archivos subir, separados por ';'. Ejemplos: *.bak;*.trn  *.zip  *"),
    ("general", "recursivo", "no", "si = incluye las subcarpetas del origen y las recrea en la nube."),
    ("general", "antiguedad_minima_minutos", "10",
     "No sube archivos modificados hace menos de estos minutos: SQL Server podría estar escribiéndolos."),
    ("general", "borrar_local_tras_subir", "no",
     "si = borra el archivo local después de subirlo y verificar su suma en la nube."),
    ("general", "tamano_bloque_mb", "16", "Tamaño de cada bloque enviado (1-128 MB)."),
    ("general", "reintentos", "5", "Reintentos ante cortes de red o errores temporales del servicio."),
    ("general", "timeout_segundos", "120", "Tiempo máximo de espera de cada petición."),
    ("general", "certificados", "cacert.pem",
     "Certificados raíz para HTTPS (los de Windows 2003 están desactualizados). Vacío = los de Windows."),
    ("general", "proxy", "", "Proxy de salida, si hace falta: host:puerto  o  http://usuario:clave@host:puerto"),
    ("general", "archivo_log", "subir_nube.log", ""),
    ("general", "archivo_estado", "estado.json", ""),
    ("general", "archivo_tokens", "tokens.json", "Aquí se guarda el permiso de acceso a la cuenta: protéjalo."),
    ("dropbox", "app_key", "", "De https://www.dropbox.com/developers/apps (pestaña Settings de su app)."),
    ("dropbox", "app_secret", "", ""),
    ("gdrive", "client_id", "",
     "De Google Cloud Console. El cliente OAuth debe ser del tipo 'TVs y dispositivos de entrada limitada'."),
    ("gdrive", "client_secret", "", ""),
    ("programacion", "activa", "no", "si = Windows ejecuta la subida automáticamente (tarea programada)."),
    ("programacion", "frecuencia", "diaria", "diaria, semanal o horas."),
    ("programacion", "hora", "03:30", "Hora de inicio (HH:MM, 24 horas)."),
    ("programacion", "dias", "LUN,MAR,MIE,JUE,VIE", "Para la frecuencia semanal: LUN,MAR,MIE,JUE,VIE,SAB,DOM"),
    ("programacion", "cada_horas", "4", "Para la frecuencia 'horas': cada cuántas horas."),
    ("programacion", "usuario", "",
     "Cuenta que ejecuta la tarea. Vacío = SYSTEM (no necesita contraseña ni que haya alguien conectado)."),
)


def leer_texto(ruta):
    with open(ruta, "rb") as f:
        crudo = f.read()
    try:
        return crudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        # El Bloc de notas de Windows 2003 guarda en ANSI (cp1252) por defecto.
        return crudo.decode("cp1252", "replace")


def leer_valores(ruta):
    """Valores del .ini como textos {(sección, clave): valor}, con los valores por
    defecto donde falten. No valida nada: la ventana lo usa para llenar el formulario."""
    valores = dict(((s, c), d) for s, c, d, _ in CAMPOS_CONFIG)
    if os.path.isfile(ruta):
        ini = configparser.ConfigParser(interpolation=None)
        try:
            ini.read_string(leer_texto(ruta), source=ruta)
        except (configparser.Error, OSError):
            return valores
        for seccion, clave, _, _ in CAMPOS_CONFIG:
            if ini.has_option(seccion, clave):
                valores[(seccion, clave)] = ini.get(seccion, clave).strip()
    return valores


def texto_configuracion(valores):
    """Contenido completo del .ini, con un comentario para cada parámetro."""
    lineas = ["; Configuración de SubirNube. Se edita desde la ventana del programa,",
              "; o a mano con el Bloc de notas (los comentarios van en su propia línea)."]
    seccion_actual = None
    for seccion, clave, defecto, comentario in CAMPOS_CONFIG:
        if seccion != seccion_actual:
            lineas.extend(["", "[{0}]".format(seccion)])
            seccion_actual = seccion
        valor = "{0}".format(valores.get((seccion, clave), defecto)).strip()
        if "\n" in valor or "\r" in valor:
            raise ErrorFatal("El valor de '{0}' no puede tener saltos de línea.".format(clave))
        if comentario:
            lineas.append("; " + comentario)
        lineas.append("{0} = {1}".format(clave, valor))
    return "\n".join(lineas) + "\n"


def guardar_configuracion(ruta, valores):
    """Escribe el .ini completo, comprueba que sea válido y solo entonces reemplaza
    el anterior. Devuelve la Configuracion; lanza ErrorFatal si no es válida."""
    texto = texto_configuracion(valores)
    temporal = ruta + ".nuevo"
    with open(temporal, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(texto)
    try:
        Configuracion(temporal)
    except ErrorFatal:
        borrar_si_existe(temporal)
        raise
    os.replace(temporal, ruta)
    return Configuracion(ruta)


class Configuracion(object):
    """Lee subir_nube.ini. Las rutas relativas se toman desde la carpeta del .ini,
    porque el Programador de tareas y el Agente SQL arrancan en otra carpeta."""

    def __init__(self, ruta):
        self.ruta = os.path.abspath(ruta)
        if not os.path.isfile(self.ruta):
            raise ErrorFatal("No existe el archivo de configuración {0}. Configure el programa desde su "
                             "ventana o copie subir_nube.ejemplo.ini como subir_nube.ini.".format(self.ruta))
        self.base = os.path.dirname(self.ruta)
        self._ini = configparser.ConfigParser(interpolation=None)
        try:
            self._ini.read_string(leer_texto(self.ruta), source=self.ruta)
        except configparser.Error as e:
            raise ErrorFatal("Error de formato en {0}: {1}".format(self.ruta, e))

        proveedor = self._texto("general", "proveedor", "dropbox").lower()
        if proveedor in ("google", "drive", "googledrive", "google drive", "google_drive"):
            proveedor = "gdrive"
        if proveedor not in PROVEEDORES:
            raise ErrorFatal("Proveedor desconocido '{0}'. Use 'dropbox' o 'gdrive'.".format(proveedor))
        self.proveedor = proveedor

        self.origen = self._ruta("general", "origen", "")
        self.carpeta_remota = self._texto("general", "carpeta_remota", "/")
        patrones = re.split("[;,]", self._texto("general", "patrones", "*"))
        self.patrones = [p.strip() for p in patrones if p.strip()] or ["*"]
        self.recursivo = self._booleano("general", "recursivo", False)
        self.antiguedad_minima = self._entero("general", "antiguedad_minima_minutos", 10, 0, 100000) * 60
        self.borrar_local = self._booleano("general", "borrar_local_tras_subir", False)
        self.tamano_bloque = self._entero("general", "tamano_bloque_mb", 16, 1, 128) * MB
        self.reintentos = self._entero("general", "reintentos", 5, 0, 50)
        self.timeout = self._entero("general", "timeout_segundos", 120, 10, 3600)
        self.certificados = self._ruta("general", "certificados", "cacert.pem")
        self.proxy = self._texto("general", "proxy", "")
        self.archivo_log = self._ruta("general", "archivo_log", "subir_nube.log")
        self.archivo_estado = self._ruta("general", "archivo_estado", "estado.json")
        self.archivo_tokens = self._ruta("general", "archivo_tokens", "tokens.json")
        # Archivos que comparten el motor y la ventana.
        self.archivo_historial = os.path.join(self.base, "historial.jsonl")
        self.archivo_actividad = os.path.join(self.base, "actividad.json")
        self.archivo_detener = os.path.join(self.base, "detener.solicitud")

        if self.proxy:
            analizar_proxy(self.proxy)
        if self.certificados and not os.path.isfile(self.certificados):
            raise ErrorFatal(
                "No se encuentra el archivo de certificados {0}. Descárguelo desde "
                "https://curl.se/ca/cacert.pem (en un equipo moderno) y cópielo junto a "
                "subir_nube.py, o deje 'certificados' vacío para usar los del sistema."
                .format(self.certificados))
        self.programacion = self._programacion()

    def _texto(self, seccion, clave, defecto):
        valor = self._ini.get(seccion, clave, fallback=defecto).strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1].strip()
        return valor

    def _ruta(self, seccion, clave, defecto):
        valor = self._texto(seccion, clave, defecto)
        if not valor:
            return ""
        return os.path.normpath(os.path.join(self.base, valor))

    def _booleano(self, seccion, clave, defecto):
        valor = self._texto(seccion, clave, "si" if defecto else "no").lower()
        if valor in VALORES_SI:
            return True
        if valor in VALORES_NO:
            return False
        raise ErrorFatal("'{0}' en [{1}] debe ser 'si' o 'no' (se leyó '{2}').".format(clave, seccion, valor))

    def _entero(self, seccion, clave, defecto, minimo, maximo):
        valor = self._texto(seccion, clave, str(defecto))
        try:
            numero = int(valor)
        except ValueError:
            raise ErrorFatal("'{0}' en [{1}] debe ser un número entero (se leyó '{2}').".format(clave, seccion, valor))
        if not minimo <= numero <= maximo:
            raise ErrorFatal("'{0}' en [{1}] debe estar entre {2} y {3}.".format(clave, seccion, minimo, maximo))
        return numero

    def _programacion(self):
        frecuencia = self._texto("programacion", "frecuencia", "diaria").lower()
        if frecuencia not in ("diaria", "semanal", "horas"):
            raise ErrorFatal("'frecuencia' en [programacion] debe ser diaria, semanal u horas.")
        hora = self._texto("programacion", "hora", "03:30")
        m = re.match(r"^(\d{1,2}):(\d{2})$", hora)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ErrorFatal("'hora' en [programacion] debe tener el formato HH:MM (se leyó '{0}').".format(hora))
        validos = [d[0] for d in DIAS_SEMANA]
        dias = [d.strip().upper() for d in re.split("[;, ]+", self._texto("programacion", "dias", "")) if d.strip()]
        dias = [d.replace("MIÉ", "MIE").replace("SÁB", "SAB") for d in dias]
        for dia in dias:
            if dia not in validos:
                raise ErrorFatal("Día desconocido '{0}' en [programacion]. Use {1}.".format(dia, ",".join(validos)))
        programacion = Programacion(
            activa=self._booleano("programacion", "activa", False), frecuencia=frecuencia,
            hora="{0:02d}:{1}".format(int(m.group(1)), m.group(2)),
            dias=[d for d in validos if d in dias],
            cada_horas=self._entero("programacion", "cada_horas", 4, 1, 23),
            usuario=self._texto("programacion", "usuario", ""))
        if programacion.activa and frecuencia == "semanal" and not programacion.dias:
            raise ErrorFatal("Elija al menos un día de la semana para la subida automática.")
        return programacion

    def credencial(self, seccion, clave):
        valor = self._texto(seccion, clave, "")
        if not valor:
            raise ErrorFatal("Falta '{0}' en la sección [{1}] de la configuración.".format(clave, seccion))
        return valor

    def archivos_propios(self):
        """Archivos del propio programa que nunca deben subirse aunque estén en el origen."""
        rutas = [self.ruta, self.certificados, self.archivo_tokens, self.archivo_tokens + ".tmp",
                 self.archivo_estado, self.archivo_estado + ".tmp", self.archivo_estado + ".lock",
                 self.archivo_historial, self.archivo_actividad, self.archivo_actividad + ".tmp",
                 self.archivo_detener]
        if self.archivo_log:
            rutas.append(self.archivo_log)
            rutas.extend("{0}.{1}".format(self.archivo_log, i) for i in range(1, 10))
        return set(os.path.normcase(os.path.abspath(r)) for r in rutas if r)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

MENSAJE_CERTIFICADO = (
    "No se pudo verificar el certificado de {host} ({error}). Compruebe que la fecha y hora "
    "del servidor sean correctas y descargue un cacert.pem actualizado desde "
    "https://curl.se/ca/cacert.pem (en un equipo moderno); cópielo junto a subir_nube.py.")


def analizar_proxy(texto):
    """'host:puerto' o 'http://usuario:clave@host:puerto' -> (host, puerto, cabeceras)."""
    if not texto:
        return None
    if "://" not in texto:
        texto = "http://" + texto
    partes = urllib.parse.urlsplit(texto)
    try:
        puerto = partes.port or 8080
    except ValueError:
        puerto = None
    if not partes.hostname or not puerto:
        raise ErrorFatal("Proxy inválido: '{0}'. Use el formato host:puerto.".format(texto))
    cabeceras = {}
    if partes.username:
        usuario = "{0}:{1}".format(urllib.parse.unquote(partes.username),
                                   urllib.parse.unquote(partes.password or ""))
        cabeceras["Proxy-Authorization"] = "Basic " + base64.b64encode(usuario.encode("utf-8")).decode("ascii")
    return partes.hostname, puerto, cabeceras


def es_error_certificado(e):
    if isinstance(e, ssl.CertificateError):
        return True
    return isinstance(e, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(e)


def crear_contexto_ssl(certificados):
    if certificados:
        try:
            return ssl.create_default_context(cafile=certificados)
        except (OSError, ValueError) as e:
            raise ErrorFatal("No se pudo cargar el archivo de certificados {0}: {1}".format(certificados, e))
    try:
        return ssl.create_default_context()
    except (OSError, ValueError) as e:
        # Python 3.4 en Windows falla si alguno de los almacenes de certificados está vacío.
        log.warning("No se pudieron cargar los certificados de Windows (%s); configure 'certificados'.", e)
        contexto = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        contexto.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3
        contexto.verify_mode = ssl.CERT_REQUIRED
        contexto.check_hostname = True
        return contexto


class Respuesta(object):
    def __init__(self, estado, cabeceras, datos):
        self.estado = estado
        self.cabeceras = cabeceras  # nombres en minúsculas
        self.datos = datos

    def json(self):
        try:
            # Python 3.4 no acepta bytes en json.loads.
            datos = json.loads(self.datos.decode("utf-8"))
        except ValueError:
            return {}
        return datos if isinstance(datos, dict) else {}

    def texto(self, limite=300):
        return self.datos[:limite].decode("utf-8", "replace").strip() or "(sin detalle)"


class ClienteHttp(object):
    """Peticiones HTTPS con el OpenSSL de Python (no el de Windows) y conexiones
    reutilizables por servidor."""

    def __init__(self, certificados=None, proxy=None, timeout=120):
        self.contexto = crear_contexto_ssl(certificados)
        self.proxy = analizar_proxy(proxy)
        self.timeout = timeout
        self._conexiones = {}

    def _conexion(self, esquema, host, puerto):
        clave = (esquema, host, puerto)
        conexion = self._conexiones.get(clave)
        if conexion is None:
            if self.proxy:
                destino, destino_puerto = self.proxy[0], self.proxy[1]
            else:
                destino, destino_puerto = host, puerto
            if esquema == "https":
                conexion = http.client.HTTPSConnection(destino, destino_puerto, timeout=self.timeout,
                                                       context=self.contexto)
            else:
                conexion = http.client.HTTPConnection(destino, destino_puerto, timeout=self.timeout)
            if self.proxy:
                conexion.set_tunnel(host, puerto, headers=self.proxy[2])
            self._conexiones[clave] = conexion
        return clave, conexion

    def _descartar(self, clave):
        conexion = self._conexiones.pop(clave, None)
        if conexion is not None:
            conexion.close()

    def cerrar(self):
        for clave in list(self._conexiones):
            self._descartar(clave)

    def peticion(self, metodo, url, cuerpo=None, cabeceras=None, al_enviar=None):
        """Hace la petición y devuelve la Respuesta. Si se indica al_enviar, el cuerpo se
        envía en trozos y se llama al_enviar(bytes_enviados) después de cada uno."""
        partes = urllib.parse.urlsplit(url)
        puerto = partes.port or (443 if partes.scheme == "https" else 80)
        ruta = partes.path or "/"
        if partes.query:
            ruta += "?" + partes.query
        cab = {"User-Agent": "subir_nube/" + VERSION}
        cab.update(cabeceras or {})
        if cuerpo is None and metodo != "GET":
            cuerpo = b""
        if cuerpo is not None:
            cab["Content-Length"] = str(len(cuerpo))
        clave, conexion = self._conexion(partes.scheme, partes.hostname, puerto)
        try:
            conexion.putrequest(metodo, ruta)
            for nombre, valor in cab.items():
                conexion.putheader(nombre, valor)
            conexion.endheaders()
            if cuerpo:
                for inicio in range(0, len(cuerpo), TROZO_ENVIO):
                    conexion.send(cuerpo[inicio:inicio + TROZO_ENVIO])
                    if al_enviar is not None:
                        al_enviar(min(inicio + TROZO_ENVIO, len(cuerpo)))
            respuesta = conexion.getresponse()
            datos = respuesta.read()
        except (OSError, http.client.HTTPException, ssl.CertificateError) as e:
            self._descartar(clave)
            if es_error_certificado(e):
                raise ErrorFatal(MENSAJE_CERTIFICADO.format(host=partes.hostname, error=e))
            raise ErrorTransitorio("error de red con {0} ({1})".format(partes.hostname, describir_error(e)))
        except BaseException:
            # Detenido (o Ctrl+C) a mitad de la petición: la conexión queda inservible.
            self._descartar(clave)
            raise
        log.debug("%s %s%s -> HTTP %d", metodo, partes.hostname, partes.path, respuesta.status)
        cabeceras_respuesta = dict((k.lower(), v) for k, v in respuesta.getheaders())
        return Respuesta(respuesta.status, cabeceras_respuesta, datos)


def reintentar(funcion, descripcion, reintentos, esperar=None):
    """Ejecuta funcion() y la repite con espera creciente si lanza ErrorTransitorio."""
    intento = 0
    while True:
        try:
            return funcion()
        except ErrorTransitorio as e:
            intento += 1
            if intento > reintentos:
                raise ErrorNube("{0}: sigue fallando tras {1} reintentos ({2})".format(descripcion, reintentos, e))
            espera = calcular_espera(e, intento)
            log.warning("%s: %s. Reintento %d de %d en %d s.", descripcion, e, intento, reintentos, espera)
            (esperar or _dormir)(espera)


def calcular_espera(error, intento):
    if error.espera is not None:
        return min(max(error.espera, 1), 300)
    return min(2 ** intento, 120)


def segundos_retry_after(respuesta):
    valor = respuesta.cabeceras.get("retry-after")
    try:
        return int(valor) if valor else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Lectura de archivos con suma de verificación
# ---------------------------------------------------------------------------

class HashDropbox(object):
    """'content_hash' de Dropbox: SHA-256 de la concatenación de los SHA-256 de
    cada bloque de 4 MB."""

    BLOQUE = 4 * MB

    def __init__(self):
        self._total = hashlib.sha256()
        self._bloque = hashlib.sha256()
        self._en_bloque = 0

    def update(self, datos):
        vista = memoryview(datos)
        posicion = 0
        while posicion < len(vista):
            if self._en_bloque == self.BLOQUE:
                self._total.update(self._bloque.digest())
                self._bloque = hashlib.sha256()
                self._en_bloque = 0
            n = min(self.BLOQUE - self._en_bloque, len(vista) - posicion)
            self._bloque.update(vista[posicion:posicion + n])
            self._en_bloque += n
            posicion += n

    def hexdigest(self):
        total = self._total.copy()
        if self._en_bloque:
            total.update(self._bloque.digest())
        return total.hexdigest()


class LectorArchivo(object):
    """Lee un archivo por bloques en cualquier posición y va calculando su suma
    de verificación en orden. Si hay que saltar hacia adelante (al reanudar una
    subida), lee la parte intermedia solo para la suma."""

    def __init__(self, ruta, crear_hash, latido=None):
        self.hash = crear_hash()
        self.hasta = 0          # bytes ya incluidos en la suma
        self.latido = latido
        self.archivo = open(ruta, "rb")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.archivo.close()

    def _ponerse_al_dia(self, posicion):
        if posicion - self.hasta > 64 * MB:
            log.info("  Calculando la suma de la parte ya subida (%s)...", legible(posicion - self.hasta))
        self.archivo.seek(self.hasta)
        while self.hasta < posicion:
            datos = self.archivo.read(min(8 * MB, posicion - self.hasta))
            if not datos:
                raise ErrorNube("el archivo se acortó mientras se subía; se volverá a intentar en la próxima ejecución")
            self.hash.update(datos)
            self.hasta += len(datos)
            if self.latido is not None:
                self.latido()

    def leer(self, posicion, cantidad):
        if posicion > self.hasta:
            self._ponerse_al_dia(posicion)
        self.archivo.seek(posicion)
        datos = self.archivo.read(cantidad)
        if len(datos) != cantidad:
            raise ErrorNube("el archivo se acortó mientras se subía; se volverá a intentar en la próxima ejecución")
        if posicion + len(datos) > self.hasta:
            self.hash.update(memoryview(datos)[self.hasta - posicion:])
            self.hasta = posicion + len(datos)
        return datos

    def hash_hex(self, tamano):
        if self.hasta < tamano:
            self._ponerse_al_dia(tamano)
        return self.hash.hexdigest()


# ---------------------------------------------------------------------------
# Avance, historial y reanudación
# ---------------------------------------------------------------------------

class MonitorNulo(object):
    """Monitor que no publica nada (simulaciones y pruebas de conexión)."""

    def latido(self):
        pass

    def comprobar_detener(self):
        pass

    def fase(self, texto):
        pass

    def avance(self, hecho):
        pass

    def esperar(self, segundos):
        _dormir(segundos)

    def inicio_lote(self, destino, cantidad, total):
        pass

    def inicio_archivo(self, relativa, tamano, indice):
        pass

    def fin_archivo(self):
        pass


class Actividad(MonitorNulo):
    """Publica en actividad.json qué se está subiendo y cuánto falta, para que la
    ventana lo muestre aunque la subida la haya lanzado el Programador de tareas.
    También atiende la solicitud de detener que deja la ventana (detener.solicitud)."""

    INTERVALO = 1.0

    def __init__(self, conf, origen):
        self.ruta = conf.archivo_actividad
        self.ruta_detener = conf.archivo_detener
        borrar_si_existe(self.ruta_detener)
        ahora = time.time()
        self.datos = {"activo": True, "pid": os.getpid(), "origen": origen, "inicio": ahora, "fin": None,
                      "fase": "Buscando archivos", "destino": "", "archivo": "", "indice": 0, "cantidad": 0,
                      "archivo_hecho": 0, "archivo_total": 0, "lote_hecho": 0, "lote_total": 0,
                      "velocidad": 0.0, "resultado": "", "detalle": ""}
        self._terminados = 0          # bytes de los archivos ya procesados en esta ejecución
        self._muestras = collections.deque()
        self._siguiente_registro = 10
        self._guardado = 0.0
        self._guardar(True)

    def _guardar(self, forzar=False):
        ahora = time.time()
        if not forzar and ahora - self._guardado < self.INTERVALO:
            return
        self._guardado = ahora
        self.datos["actualizado"] = ahora
        try:
            escribir_json(self.ruta, self.datos)
        except OSError:
            pass  # la ventana es opcional (o tiene el archivo abierto): la subida sigue

    def comprobar_detener(self):
        if os.path.exists(self.ruta_detener):
            raise Detenido()

    def latido(self):
        self.comprobar_detener()
        self._guardar()

    def fase(self, texto):
        self.datos["fase"] = texto
        self._guardar(True)

    def esperar(self, segundos):
        anterior = self.datos["fase"]
        self.fase("Esperando para reintentar")
        restante = segundos
        while restante > 0:
            self.latido()
            paso = min(5, restante)
            _dormir(paso)
            restante -= paso
        self.latido()
        self.fase(anterior)

    def inicio_lote(self, destino, cantidad, total):
        self.datos.update(destino=destino, cantidad=cantidad, lote_total=total, fase="Subiendo")
        self._guardar(True)

    def inicio_archivo(self, relativa, tamano, indice):
        self.datos.update(archivo=relativa, archivo_total=tamano, archivo_hecho=0, indice=indice,
                          velocidad=0.0, fase="Subiendo", lote_hecho=self._terminados)
        self._muestras.clear()
        self._siguiente_registro = 10
        self._guardar(True)

    def avance(self, hecho):
        ahora = time.time()
        if self._muestras and hecho < self._muestras[-1][1]:
            self._muestras.clear()  # se reenvía una parte: la velocidad se mide de nuevo
        self._muestras.append((ahora, hecho))
        while len(self._muestras) > 2 and ahora - self._muestras[0][0] > 30:
            self._muestras.popleft()
        transcurrido = ahora - self._muestras[0][0]
        if transcurrido >= 1:
            self.datos["velocidad"] = (hecho - self._muestras[0][1]) / transcurrido
        total = self.datos["archivo_total"]
        self.datos["archivo_hecho"] = hecho
        self.datos["lote_hecho"] = self._terminados + hecho
        if total > 64 * MB:
            porcentaje = hecho * 100 // total
            if self._siguiente_registro <= porcentaje < 100:
                log.info("  %s: %d%% (%s de %s)", self.datos["archivo"], porcentaje, legible(hecho), legible(total))
                self._siguiente_registro = (porcentaje // 10 + 1) * 10
        self.latido()

    def fin_archivo(self):
        self._terminados += self.datos["archivo_total"]
        self.datos["lote_hecho"] = self._terminados
        self._guardar(True)

    def terminar(self, resultado, detalle):
        self.datos.update(activo=False, fin=time.time(), resultado=resultado, detalle=detalle, velocidad=0.0,
                          fase="Terminada")
        self._guardar(True)
        borrar_si_existe(self.ruta_detener)


def leer_actividad(ruta):
    """Estado de la última ejecución, con 'en_curso' calculado a partir del latido."""
    datos = leer_json(ruta)
    if datos:
        reciente = time.time() - float(datos.get("actualizado") or 0) < 300
        datos["en_curso"] = bool(datos.get("activo")) and reciente
        datos["interrumpida"] = bool(datos.get("activo")) and not reciente
    return datos


class Historial(object):
    """Registro de subidas y ejecuciones (una línea JSON por evento) que muestra la ventana."""

    MAXIMO_BYTES = 2 * MB
    CONSERVAR = 3000

    def __init__(self, ruta):
        self.ruta = ruta

    def _agregar(self, entrada):
        try:
            with open(self.ruta, "a", encoding="utf-8") as f:
                f.write(json.dumps(entrada, ensure_ascii=False, sort_keys=True) + "\n")
            if os.path.getsize(self.ruta) > self.MAXIMO_BYTES:
                with open(self.ruta, "r", encoding="utf-8", errors="replace") as f:
                    lineas = f.readlines()[-self.CONSERVAR:]
                with open(self.ruta, "w", encoding="utf-8") as f:
                    f.writelines(lineas)
        except OSError as e:
            log.warning("No se pudo escribir el historial %s: %s", self.ruta, e)

    def archivo(self, relativa, destino, tamano, segundos, resultado, detalle="", verificado=False):
        self._agregar({"tipo": "archivo", "fecha": fecha_local(), "archivo": relativa, "destino": destino,
                       "tamano": tamano, "segundos": round(segundos, 1), "resultado": resultado,
                       "detalle": detalle, "verificado": verificado})

    def ejecucion(self, inicio, origen, resumen, resultado, detalle):
        self._agregar({"tipo": "ejecucion", "fecha": fecha_local(inicio), "fin": fecha_local(),
                       "origen": origen, "resultado": resultado, "detalle": detalle,
                       "subidos": resumen.subidos, "bytes": resumen.bytes, "errores": resumen.errores})

    @staticmethod
    def leer(ruta, limite=1000):
        """Últimas entradas (las más recientes al final)."""
        entradas = []
        try:
            with open(ruta, "r", encoding="utf-8", errors="replace") as f:
                lineas = f.readlines()[-limite:]
        except OSError:
            return entradas
        for linea in lineas:
            try:
                entrada = json.loads(linea)
            except ValueError:
                continue
            if isinstance(entrada, dict):
                entradas.append(entrada)
        return entradas


class SinReanudacion(object):
    datos = None

    def guardar(self, **campos):
        pass

    def borrar(self):
        pass


class Reanudacion(object):
    """Sesión de subida a medias guardada en estado.json, para continuar un archivo
    grande en la próxima ejecución en lugar de empezar de cero."""

    VIGENCIA = 6 * 86400   # Dropbox y Google mantienen las sesiones de subida una semana

    def __init__(self, estado, clave, tamano, mtime):
        self.estado = estado
        self.clave = clave
        self.tamano = tamano
        self.mtime = int(mtime)
        datos = estado.sesiones.get(clave)
        if datos and (datos.get("tamano") != tamano or datos.get("mtime") != self.mtime
                      or time.time() - datos.get("creada", 0) > self.VIGENCIA):
            self.datos = datos
            self.borrar()   # el archivo cambió o la sesión venció
            datos = None
        self.datos = datos

    def guardar(self, **campos):
        if self.datos is None:
            self.datos = {"tamano": self.tamano, "mtime": self.mtime, "creada": time.time()}
        self.datos.update(campos)
        self.estado.sesiones[self.clave] = self.datos
        self.estado.guardar()

    def borrar(self):
        self.datos = None
        if self.estado.sesiones.pop(self.clave, None) is not None:
            self.estado.guardar()


# ---------------------------------------------------------------------------
# Proveedores
# ---------------------------------------------------------------------------

class Proveedor(object):
    nombre = ""
    titulo = ""
    tipo_autorizacion = ""

    def __init__(self, conf, http, tokens):
        self.conf = conf
        self.http = http
        self.tokens = tokens
        self.monitor = MonitorNulo()
        self._token = None
        self._expira = 0.0

    # Cada proveedor implementa: probar(), subir(), _pedir_token(), _mensaje_error() y la autorización.

    def descripcion_destino(self):
        return "{0}:{1}".format(self.titulo, normalizar_remota(self.conf.carpeta_remota))

    def vinculada(self):
        return bool(self.tokens.obtener(self.nombre))

    def _reintentar(self, funcion, descripcion):
        return reintentar(funcion, descripcion, self.conf.reintentos, self.monitor.esperar)

    def _avance(self, base):
        return lambda enviados: self.monitor.avance(base + enviados)

    def _token_acceso(self, renovar=False):
        if renovar or self._token is None or time.time() > self._expira - 300:
            refresh = self.tokens.obtener(self.nombre)
            if not refresh:
                raise ErrorFatal("La cuenta de {0} no está vinculada. Use 'Vincular cuenta' en la ventana o "
                                 "ejecute: python subir_nube.py autorizar".format(self.titulo))
            r = self._pedir_token(refresh)
            self._comprobar_respuesta(r)
            datos = r.json()
            if r.estado != 200 or not datos.get("access_token"):
                raise ErrorFatal("{0} no renovó el acceso (HTTP {1}: {2}). Si se revocó el permiso o "
                                 "cambiaron las credenciales, vuelva a vincular la cuenta."
                                 .format(self.titulo, r.estado, self._mensaje_error(r)))
            self._token = datos["access_token"]
            self._expira = time.time() + int(datos.get("expires_in", 3600))
            if datos.get("refresh_token") and datos["refresh_token"] != refresh:
                self.tokens.guardar(self.nombre, datos["refresh_token"])
        return self._token

    def _llamar(self, metodo, url, cuerpo=None, cabeceras=None, al_enviar=None):
        """Petición autenticada: renueva el token una vez si el servicio lo rechaza
        y convierte los errores temporales en ErrorTransitorio."""
        renovar = False
        while True:
            cab = dict(cabeceras or {})
            cab["Authorization"] = "Bearer " + self._token_acceso(renovar)
            r = self.http.peticion(metodo, url, cuerpo, cab, al_enviar)
            if r.estado == 401 and not renovar:
                renovar = True
                continue
            self._comprobar_respuesta(r)
            return r

    def _comprobar_respuesta(self, r):
        """Convierte los errores temporales (429, 5xx) en ErrorTransitorio."""
        if r.estado == 429 or r.estado >= 500:
            raise ErrorTransitorio("HTTP {0} ({1})".format(r.estado, self._mensaje_error(r)),
                                   espera=segundos_retry_after(r))

    def _exigir_ok(self, r, descripcion):
        if r.estado not in (200, 201):
            raise ErrorNube("{0}: HTTP {1} ({2})".format(descripcion, r.estado, self._mensaje_error(r)))
        return r.json()


class Dropbox(Proveedor):
    nombre = "dropbox"
    titulo = "Dropbox"
    tipo_autorizacion = "codigo"
    URL_AUTORIZAR = "https://www.dropbox.com/oauth2/authorize"
    URL_TOKEN = "https://api.dropboxapi.com/oauth2/token"
    URL_API = "https://api.dropboxapi.com/2"
    URL_CONTENIDO = "https://content.dropboxapi.com/2"

    def _credenciales(self):
        return self.conf.credencial("dropbox", "app_key"), self.conf.credencial("dropbox", "app_secret")

    def _mensaje_error(self, r):
        datos = r.json()
        for campo in ("error_summary", "error_description", "error"):
            if isinstance(datos.get(campo), str):
                return datos[campo]
        return r.texto()

    def _comprobar_respuesta(self, r):
        Proveedor._comprobar_respuesta(self, r)
        if r.estado == 401:
            error = r.json().get("error")
            if isinstance(error, dict) and error.get(".tag") == "missing_scope":
                raise ErrorFatal(
                    "La app de Dropbox no tiene el permiso '{0}'. Márquelo en la pestaña Permissions de "
                    "https://www.dropbox.com/developers/apps, pulse Submit y vuelva a vincular la cuenta."
                    .format(error.get("required_scope", "files.content.write")))

    def _pedir_token(self, refresh):
        app_key, app_secret = self._credenciales()
        campos = {"grant_type": "refresh_token", "refresh_token": refresh,
                  "client_id": app_key, "client_secret": app_secret}
        return self.http.peticion("POST", self.URL_TOKEN, formulario(campos), CABECERA_FORMULARIO)

    # -- Autorización: se abre una dirección en cualquier equipo y se pega el código.

    def url_autorizacion(self):
        app_key = self._credenciales()[0]
        return self.URL_AUTORIZAR + "?" + urllib.parse.urlencode(
            {"client_id": app_key, "response_type": "code", "token_access_type": "offline"})

    def completar_autorizacion(self, codigo):
        app_key, app_secret = self._credenciales()
        codigo = codigo.strip()
        if not codigo:
            raise ErrorFatal("No se ingresó ningún código.")
        campos = {"code": codigo, "grant_type": "authorization_code",
                  "client_id": app_key, "client_secret": app_secret}
        r = self.http.peticion("POST", self.URL_TOKEN, formulario(campos), CABECERA_FORMULARIO)
        datos = r.json()
        if r.estado != 200 or not datos.get("refresh_token"):
            raise ErrorFatal("Dropbox no aceptó el código: " + self._mensaje_error(r))
        return datos["refresh_token"]

    def autorizar(self):
        url = self.url_autorizacion()
        print()
        print("1. Abra esta dirección en un navegador (puede ser en otro equipo o en el teléfono):")
        print()
        print("   " + url)
        print()
        print("2. Inicie sesión en Dropbox, pulse 'Permitir' y copie el código que aparece.")
        return self.completar_autorizacion(input("3. Pegue aquí el código y pulse Enter: "))

    def _rpc(self, ruta, descripcion):
        r = self._reintentar(lambda: self._llamar("POST", self.URL_API + "/" + ruta, b"null", CABECERA_JSON),
                             descripcion)
        return self._exigir_ok(r, descripcion)

    def probar(self):
        cuenta = self._rpc("users/get_current_account", "Consultando la cuenta")
        uso = self._rpc("users/get_space_usage", "Consultando el espacio")
        texto = "Cuenta de Dropbox: {0} <{1}>. Espacio usado: {2}".format(
            cuenta.get("name", {}).get("display_name", "?"), cuenta.get("email", "?"), legible(uso.get("used", 0)))
        asignado = uso.get("allocation", {}).get("allocated")
        if asignado:
            texto += " de " + legible(asignado)
        return texto

    def _subir_contenido(self, ruta, argumento, datos, base):
        cabeceras = {"Content-Type": "application/octet-stream",
                     # Debe ir en ASCII: json.dumps escapa los acentos como \uXXXX.
                     "Dropbox-API-Arg": json.dumps(argumento)}
        return self._llamar("POST", self.URL_CONTENIDO + "/" + ruta, datos, cabeceras, self._avance(base))

    def subir(self, ruta_local, relativa, tamano, mtime, reanudacion=None):
        reanudacion = reanudacion or SinReanudacion()
        destino = "/" + "/".join(partes_remotas(self.conf.carpeta_remota) + relativa.split("/"))
        commit = {"path": destino, "mode": "overwrite", "autorename": False, "mute": True,
                  "client_modified": fecha_iso(mtime)}
        descripcion = "Subiendo " + relativa
        with LectorArchivo(ruta_local, HashDropbox, self.monitor.latido) as lector:
            if tamano <= self.conf.tamano_bloque:
                datos = lector.leer(0, tamano)
                r = self._reintentar(lambda: self._subir_contenido("files/upload", commit, datos, 0), descripcion)
                meta = self._exigir_ok(r, descripcion)
            else:
                meta = self._subir_por_sesion(lector, tamano, commit, relativa, reanudacion)
            self.monitor.fase("Verificando")
            hash_local = lector.hash_hex(tamano)
        reanudacion.borrar()
        return {"tamano": meta.get("size"), "hash_remoto": meta.get("content_hash"), "hash_local": hash_local}

    def _subir_por_sesion(self, lector, tamano, commit, relativa, reanudacion):
        """Archivos grandes: sesión de subida por bloques (start, append_v2, finish).
        La sesión se guarda para poder continuarla en otra ejecución."""
        descripcion = "Subiendo " + relativa
        bloque = self.conf.tamano_bloque
        previa = reanudacion.datos if reanudacion.datos and reanudacion.datos.get("proveedor") == self.nombre else None
        if previa:
            sesion, posicion = previa["sesion"], int(previa.get("posicion", 0))
            log.info("  Se reanuda %s desde %s de %s.", relativa, legible(posicion), legible(tamano))
            self.monitor.fase("Reanudando")
        else:
            datos = lector.leer(0, bloque)
            r = self._reintentar(lambda: self._subir_contenido("files/upload_session/start", {"close": False},
                                                               datos, 0), descripcion)
            sesion = self._exigir_ok(r, descripcion)["session_id"]
            posicion = len(datos)
            reanudacion.guardar(proveedor=self.nombre, sesion=sesion, posicion=posicion)
        correcciones = 0
        while True:
            self.monitor.avance(posicion)
            final = tamano - posicion <= bloque
            datos = lector.leer(posicion, min(bloque, tamano - posicion))
            self.monitor.fase("Subiendo")
            cursor = {"session_id": sesion, "offset": posicion}
            if final:
                ruta, argumento = "files/upload_session/finish", {"cursor": cursor, "commit": commit}
            else:
                ruta, argumento = "files/upload_session/append_v2", {"cursor": cursor, "close": False}
            r = self._reintentar(lambda: self._subir_contenido(ruta, argumento, datos, posicion), descripcion)
            correcta = self._posicion_correcta(r)
            if correcta is not None:
                # Dropbox ya tenía más (o menos) datos: p. ej., la respuesta anterior se perdió.
                correcciones += 1
                if correcciones > 10 or not 0 <= correcta <= tamano:
                    raise ErrorNube("{0}: Dropbox no acepta la posición de la subida.".format(descripcion))
                log.info("  %s: Dropbox indica continuar desde %s.", relativa, legible(correcta))
                posicion = correcta
                reanudacion.guardar(posicion=posicion)
                continue
            if previa and self._sesion_invalida(r):
                log.warning("  La subida pendiente de %s ya no es válida en Dropbox; se empieza de nuevo.", relativa)
                reanudacion.borrar()
                return self._subir_por_sesion(lector, tamano, commit, relativa, reanudacion)
            meta = self._exigir_ok(r, descripcion)
            if final:
                return meta
            posicion += len(datos)
            reanudacion.guardar(posicion=posicion)

    @staticmethod
    def _error_sesion(r):
        if r.estado != 409:
            return {}
        error = r.json().get("error", {})
        if isinstance(error, dict) and error.get(".tag") == "lookup_failed":
            error = error.get("lookup_failed", {})
        return error if isinstance(error, dict) else {}

    @classmethod
    def _posicion_correcta(cls, r):
        error = cls._error_sesion(r)
        return error.get("correct_offset") if error.get(".tag") == "incorrect_offset" else None

    @classmethod
    def _sesion_invalida(cls, r):
        return cls._error_sesion(r).get(".tag") in ("not_found", "closed", "not_closed")


class GoogleDrive(Proveedor):
    nombre = "gdrive"
    titulo = "Google Drive"
    tipo_autorizacion = "dispositivo"
    # drive.file: solo da acceso a los archivos y carpetas que crea este programa.
    ALCANCE = "https://www.googleapis.com/auth/drive.file"
    URL_DISPOSITIVO = "https://oauth2.googleapis.com/device/code"
    URL_TOKEN = "https://oauth2.googleapis.com/token"
    URL_API = "https://www.googleapis.com/drive/v3"
    URL_SUBIDA = "https://www.googleapis.com/upload/drive/v3"
    TIPO_CARPETA = "application/vnd.google-apps.folder"
    CAMPOS = "id,name,size,md5Checksum"
    RAZONES_TRANSITORIAS = ("rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError")

    def __init__(self, conf, http, tokens):
        Proveedor.__init__(self, conf, http, tokens)
        self._carpetas = {}

    def _credenciales(self):
        return self.conf.credencial("gdrive", "client_id"), self.conf.credencial("gdrive", "client_secret")

    def _mensaje_error(self, r):
        datos = r.json()
        error = datos.get("error")
        if isinstance(error, dict):
            return error.get("message") or r.texto()
        if isinstance(error, str):
            return datos.get("error_description") or error
        return r.texto()

    def _comprobar_respuesta(self, r):
        Proveedor._comprobar_respuesta(self, r)
        if r.estado == 403:
            error = r.json().get("error")
            errores = error.get("errors") if isinstance(error, dict) else None
            if errores and isinstance(errores[0], dict) and errores[0].get("reason") in self.RAZONES_TRANSITORIAS:
                raise ErrorTransitorio("límite de uso de Google Drive ({0})".format(errores[0]["reason"]))

    def _pedir_token(self, refresh):
        client_id, client_secret = self._credenciales()
        campos = {"grant_type": "refresh_token", "refresh_token": refresh,
                  "client_id": client_id, "client_secret": client_secret}
        return self.http.peticion("POST", self.URL_TOKEN, formulario(campos), CABECERA_FORMULARIO)

    # -- Autorización para dispositivos: se escribe un código en google.com/device desde
    #    cualquier equipo o teléfono, así que no hace falta un navegador moderno en el servidor.

    def iniciar_autorizacion(self):
        client_id = self._credenciales()[0]
        r = self.http.peticion("POST", self.URL_DISPOSITIVO,
                               formulario({"client_id": client_id, "scope": self.ALCANCE}), CABECERA_FORMULARIO)
        datos = r.json()
        if r.estado != 200 or not datos.get("device_code"):
            raise ErrorFatal("Google rechazó la solicitud ({0}). Compruebe que el ID de cliente OAuth sea del tipo "
                             "'TVs y dispositivos de entrada limitada' y que la API de Google Drive esté habilitada."
                             .format(self._mensaje_error(r)))
        datos["direccion"] = (datos.get("verification_url") or datos.get("verification_uri")
                              or "https://www.google.com/device")
        return datos

    def esperar_autorizacion(self, datos, cancelado=None):
        client_id, client_secret = self._credenciales()
        intervalo = int(datos.get("interval", 5))
        limite = time.time() + int(datos.get("expires_in", 1800))
        campos = {"client_id": client_id, "client_secret": client_secret, "device_code": datos["device_code"],
                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
        while time.time() < limite:
            for _ in range(intervalo):
                if cancelado is not None and cancelado():
                    raise ErrorFatal("Se canceló la vinculación.")
                _dormir(1)
            try:
                r = self.http.peticion("POST", self.URL_TOKEN, formulario(campos), CABECERA_FORMULARIO)
            except ErrorTransitorio as e:
                log.warning("Sin respuesta de Google (%s); se sigue esperando.", e)
                continue
            respuesta = r.json()
            if r.estado == 200 and respuesta.get("refresh_token"):
                return respuesta["refresh_token"]
            error = respuesta.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                intervalo += 5
                continue
            if error == "access_denied":
                raise ErrorFatal("Se rechazó el permiso en la cuenta de Google.")
            raise ErrorFatal("Google no completó la autorización: " + self._mensaje_error(r))
        raise ErrorFatal("El código expiró antes de que se autorizara. Vuelva a intentarlo.")

    def autorizar(self):
        datos = self.iniciar_autorizacion()
        print()
        print("1. En cualquier equipo o teléfono abra: " + datos["direccion"])
        print("2. Escriba este código: " + datos["user_code"])
        print("3. Elija la cuenta de Google y acepte los permisos.")
        print()
        print("Esperando la autorización (puede cancelar con Ctrl+C)...")
        return self.esperar_autorizacion(datos)

    def probar(self):
        url = self.URL_API + "/about?" + urllib.parse.urlencode(
            {"fields": "user(displayName,emailAddress),storageQuota(limit,usage)"})
        r = self._reintentar(lambda: self._llamar("GET", url), "Consultando la cuenta")
        datos = self._exigir_ok(r, "Consultando la cuenta")
        usuario = datos.get("user", {})
        cuota = datos.get("storageQuota", {})
        texto = "Cuenta de Google Drive: {0} <{1}>. Espacio usado: {2}".format(
            usuario.get("displayName", "?"), usuario.get("emailAddress", "?"), legible(int(cuota.get("usage", 0))))
        if cuota.get("limit"):
            texto += " de " + legible(int(cuota["limit"]))
        return texto

    @staticmethod
    def _escapar(nombre):
        return nombre.replace("\\", "\\\\").replace("'", "\\'")

    def _buscar(self, nombre, padre, es_carpeta):
        consulta = "name = '{0}' and '{1}' in parents and trashed = false and mimeType {2} '{3}'".format(
            self._escapar(nombre), padre, "=" if es_carpeta else "!=", self.TIPO_CARPETA)
        url = self.URL_API + "/files?" + urllib.parse.urlencode(
            {"q": consulta, "fields": "files(id,name)", "spaces": "drive", "pageSize": "10"})
        descripcion = "Buscando '{0}' en Google Drive".format(nombre)
        r = self._reintentar(lambda: self._llamar("GET", url), descripcion)
        encontrados = self._exigir_ok(r, descripcion).get("files", [])
        if len(encontrados) > 1:
            log.warning("Hay %d elementos llamados '%s' en la misma carpeta de Google Drive; se usa el primero.",
                        len(encontrados), nombre)
        return encontrados[0]["id"] if encontrados else None

    def _carpeta(self, partes):
        """ID de la carpeta remota (la crea si no existe). partes = ('Respaldos', 'SQL')."""
        if not partes:
            return "root"
        clave = "/".join(partes)
        if clave not in self._carpetas:
            padre = self._carpeta(partes[:-1])
            identificador = self._buscar(partes[-1], padre, True)
            if identificador is None:
                meta = {"name": partes[-1], "mimeType": self.TIPO_CARPETA, "parents": [padre]}
                descripcion = "Creando la carpeta " + clave
                r = self._reintentar(lambda: self._llamar("POST", self.URL_API + "/files?fields=id",
                                                          json_bytes(meta), CABECERA_JSON), descripcion)
                identificador = self._exigir_ok(r, descripcion)["id"]
                log.info("Carpeta creada en Google Drive: /%s", clave)
            self._carpetas[clave] = identificador
        return self._carpetas[clave]

    def subir(self, ruta_local, relativa, tamano, mtime, reanudacion=None):
        reanudacion = reanudacion or SinReanudacion()
        descripcion = "Subiendo " + relativa
        previa = reanudacion.datos if reanudacion.datos and reanudacion.datos.get("proveedor") == self.nombre else None
        with LectorArchivo(ruta_local, hashlib.md5, self.monitor.latido) as lector:
            meta = None
            if previa:
                log.info("  Se reanuda la subida pendiente de %s.", relativa)
                self.monitor.fase("Reanudando")
                meta = self._subida_reanudable(previa["sesion"], lector, tamano, relativa, consultar=True)
                if meta is None:
                    log.warning("  La subida pendiente de %s venció en Google Drive; se empieza de nuevo.", relativa)
                    reanudacion.borrar()
            if meta is None:
                partes = partes_remotas(self.conf.carpeta_remota) + relativa.split("/")
                padre = self._carpeta(tuple(partes[:-1]))
                existente = self._buscar(partes[-1], padre, False)
                sesion = self._reintentar(
                    lambda: self._iniciar_sesion(partes[-1], padre, existente, tamano, mtime, descripcion), descripcion)
                reanudacion.guardar(proveedor=self.nombre, sesion=sesion)
                meta = self._subida_reanudable(sesion, lector, tamano, relativa, consultar=False)
                if meta is None:
                    raise ErrorNube(descripcion + ": la sesión de subida expiró; se volverá a intentar "
                                    "en la próxima ejecución.")
            self.monitor.fase("Verificando")
            hash_local = lector.hash_hex(tamano)
        reanudacion.borrar()
        if meta.get("id") and (meta.get("size") is None or not meta.get("md5Checksum")):
            url = self.URL_API + "/files/" + meta["id"] + "?fields=" + self.CAMPOS
            r = self._reintentar(lambda: self._llamar("GET", url), descripcion)
            meta = self._exigir_ok(r, descripcion)
        tamano_remoto = int(meta["size"]) if meta.get("size") is not None else None
        return {"tamano": tamano_remoto, "hash_remoto": meta.get("md5Checksum"), "hash_local": hash_local}

    def _iniciar_sesion(self, nombre, padre, existente, tamano, mtime, descripcion):
        meta = {"modifiedTime": fecha_iso(mtime)}
        parametros = urllib.parse.urlencode({"uploadType": "resumable", "fields": self.CAMPOS})
        if existente:
            # Reemplaza el contenido del archivo que ya existe (queda como nueva versión).
            metodo, url = "PATCH", self.URL_SUBIDA + "/files/" + existente + "?" + parametros
        else:
            metodo, url = "POST", self.URL_SUBIDA + "/files?" + parametros
            meta["name"] = nombre
            meta["parents"] = [padre]
        cabeceras = dict(CABECERA_JSON)
        cabeceras["X-Upload-Content-Type"] = "application/octet-stream"
        cabeceras["X-Upload-Content-Length"] = str(tamano)
        r = self._llamar(metodo, url, json_bytes(meta), cabeceras)
        self._exigir_ok(r, descripcion)
        if not r.cabeceras.get("location"):
            raise ErrorNube(descripcion + ": Google Drive no devolvió la dirección de la sesión de subida.")
        return r.cabeceras["location"]

    def _subida_reanudable(self, sesion, lector, tamano, relativa, consultar):
        """Envía el archivo por bloques. Si un bloque falla (o se reanuda una subida de
        otra ejecución), pregunta a Google cuánto recibió y continúa desde ahí.
        Devuelve los metadatos del archivo, o None si la sesión ya no existe.
        La URL de la sesión ya autoriza la subida."""
        descripcion = "Subiendo " + relativa
        posicion = 0
        fallos = 0
        sin_avance = 0
        while True:
            try:
                if consultar or fallos:
                    r = self.http.peticion("PUT", sesion, b"", {"Content-Range": "bytes */{0}".format(tamano)})
                else:
                    self.monitor.avance(posicion)
                    n = min(self.conf.tamano_bloque, tamano - posicion)
                    datos = lector.leer(posicion, n)
                    self.monitor.fase("Subiendo")
                    if n:
                        rango = "bytes {0}-{1}/{2}".format(posicion, posicion + n - 1, tamano)
                    else:
                        rango = "bytes */{0}".format(tamano)
                    r = self.http.peticion("PUT", sesion, datos, {"Content-Range": rango}, self._avance(posicion))
                self._comprobar_respuesta(r)
            except ErrorTransitorio as e:
                fallos += 1
                if fallos > self.conf.reintentos:
                    raise ErrorNube("{0}: sigue fallando tras {1} reintentos ({2})".format(
                        descripcion, self.conf.reintentos, e))
                espera = calcular_espera(e, fallos)
                log.warning("%s: %s. Reintento %d de %d en %d s.", descripcion, e, fallos, self.conf.reintentos, espera)
                self.monitor.esperar(espera)
                continue
            era_consulta = consultar or fallos > 0
            consultar = False
            fallos = 0
            if r.estado in (200, 201):
                return r.json()
            if r.estado == 308:
                anterior = posicion
                rango = r.cabeceras.get("range")  # "bytes=0-1048575": recibido hasta ese byte
                posicion = int(rango.rsplit("-", 1)[1]) + 1 if rango else 0
                if not era_consulta:
                    sin_avance = sin_avance + 1 if posicion <= anterior else 0
                    if sin_avance > self.conf.reintentos:
                        raise ErrorNube(descripcion + ": Google Drive no está aceptando los datos.")
                elif posicion:
                    log.info("  Google Drive ya tiene %s de %s; se continúa desde ahí.", legible(posicion),
                             legible(tamano))
                continue
            if r.estado in (404, 410):
                return None
            raise ErrorNube("{0}: HTTP {1} ({2})".format(descripcion, r.estado, self._mensaje_error(r)))


PROVEEDORES = {"dropbox": Dropbox, "gdrive": GoogleDrive}


def crear_proveedor(conf):
    """Proveedor configurado, listo para usar. Recuerde cerrar proveedor.http al terminar."""
    http = ClienteHttp(conf.certificados, conf.proxy, conf.timeout)
    return PROVEEDORES[conf.proveedor](conf, http, AlmacenTokens(conf.archivo_tokens))


# ---------------------------------------------------------------------------
# Estado, permisos guardados y bloqueo
# ---------------------------------------------------------------------------

class Estado(object):
    """Recuerda qué archivos ya se subieron (tamaño y fecha) para no repetirlos, y
    las subidas que quedaron a medias para continuarlas."""

    def __init__(self, ruta):
        self.ruta = ruta
        self.archivos = {}
        self.sesiones = {}
        if os.path.exists(ruta):
            try:
                with open(ruta, "r", encoding="utf-8") as f:
                    datos = json.load(f)
                self.archivos = datos.get("archivos", {})
                self.sesiones = datos.get("sesiones", {})
            except (OSError, ValueError, AttributeError) as e:
                log.warning("No se pudo leer %s (%s); se revisarán todos los archivos de nuevo.", ruta, e)
                self.archivos, self.sesiones = {}, {}

    @staticmethod
    def clave(destino, relativa):
        return destino + "|" + relativa

    def ya_subido(self, clave, tamano, mtime):
        entrada = self.archivos.get(clave)
        return bool(entrada) and entrada.get("tamano") == tamano and entrada.get("mtime") == int(mtime)

    def marcar(self, clave, tamano, mtime):
        self.archivos[clave] = {"tamano": tamano, "mtime": int(mtime), "subido": fecha_local()}
        self.guardar()

    def olvidar(self, clave):
        if self.archivos.pop(clave, None) is not None:
            self.guardar()

    def podar(self, destino, vigentes):
        """Quita las entradas de archivos que ya no existen en el origen."""
        prefijo = destino + "|"
        cambio = False
        for tabla in (self.archivos, self.sesiones):
            for clave in [c for c in tabla if c.startswith(prefijo) and c[len(prefijo):] not in vigentes]:
                del tabla[clave]
                cambio = True
        if cambio:
            self.guardar()

    def guardar(self):
        try:
            escribir_json(self.ruta, {"version": 2, "archivos": self.archivos, "sesiones": self.sesiones})
        except OSError as e:
            # Sin estado guardado solo se pierde eficiencia: se volverá a subir en la próxima ejecución.
            log.warning("No se pudo guardar %s: %s", self.ruta, e)


class AlmacenTokens(object):
    """Guarda el permiso de larga duración (refresh token) de cada servicio."""

    def __init__(self, ruta):
        self.ruta = ruta

    def _leer(self):
        if not os.path.exists(self.ruta):
            return {}
        try:
            with open(self.ruta, "r", encoding="utf-8") as f:
                datos = json.load(f)
        except (OSError, ValueError) as e:
            raise ErrorFatal("No se pudo leer {0}: {1}".format(self.ruta, e))
        return datos if isinstance(datos, dict) else {}

    def obtener(self, proveedor):
        entrada = self._leer().get(proveedor)
        return entrada.get("refresh_token") if isinstance(entrada, dict) else None

    def guardar(self, proveedor, refresh_token):
        datos = self._leer()
        datos[proveedor] = {"refresh_token": refresh_token, "fecha": fecha_local()}
        escribir_json(self.ruta, datos)


class Bloqueo(object):
    """Evita que dos ejecuciones suban a la vez. Windows libera el bloqueo solo
    si el proceso termina de forma inesperada."""

    def __init__(self, ruta):
        self.ruta = ruta
        self.descriptor = None

    def adquirir(self):
        self.descriptor = os.open(self.ruta, os.O_RDWR | os.O_CREAT)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.descriptor)
            self.descriptor = None
            return False
        return True

    def liberar(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


# ---------------------------------------------------------------------------
# Sincronización
# ---------------------------------------------------------------------------

def coincide(nombre, patrones):
    nombre = nombre.lower()
    return any(fnmatch.fnmatchcase(nombre, patron.lower()) for patron in patrones)


def buscar_archivos(conf):
    """Lista de (ruta_absoluta, ruta_relativa_con_barras) que coinciden con los patrones."""
    propios = conf.archivos_propios()
    encontrados = []

    def al_fallar(error):
        log.warning("No se pudo leer la carpeta %s: %s", error.filename, error.strerror)

    for raiz, carpetas, archivos in os.walk(conf.origen, onerror=al_fallar):
        if conf.recursivo:
            carpetas.sort()
        else:
            del carpetas[:]
        for nombre in sorted(archivos):
            if not coincide(nombre, conf.patrones):
                continue
            ruta = os.path.join(raiz, nombre)
            if os.path.normcase(os.path.abspath(ruta)) in propios:
                continue
            relativa = os.path.relpath(ruta, conf.origen).replace(os.sep, "/")
            encontrados.append((ruta, relativa))
    return encontrados


def comprobar_subida(resultado, ruta, tamano, mtime):
    """Devuelve True si se verificó la suma del archivo en la nube."""
    if resultado.get("tamano") != tamano:
        raise ErrorNube("el tamaño en la nube ({0}) no coincide con el local ({1})".format(
            resultado.get("tamano"), tamano))
    remoto, local = resultado.get("hash_remoto"), resultado.get("hash_local")
    if remoto and local and remoto.lower() != local.lower():
        raise ErrorNube("la suma de verificación en la nube no coincide con la local; "
                        "se volverá a subir en la próxima ejecución")
    info = os.stat(ruta)
    if info.st_size != tamano or int(info.st_mtime) != int(mtime):
        raise ErrorNube("el archivo cambió mientras se subía; se volverá a subir en la próxima ejecución")
    return bool(remoto and local)


class Resumen(object):
    def __init__(self):
        self.subidos = 0
        self.bytes = 0
        self.sin_cambios = 0
        self.recientes = 0
        self.borrados = 0
        self.errores = 0
        self.detenido = False

    def texto(self, simular=False):
        verbo = "se subirían" if simular else "subidos"
        partes = ["{0} {1} ({2})".format(self.subidos, verbo, legible(self.bytes)),
                  "{0} sin cambios".format(self.sin_cambios),
                  "{0} pendientes por ser recientes".format(self.recientes),
                  "{0} con error".format(self.errores)]
        if self.borrados:
            partes.append("{0} borrados del disco local".format(self.borrados))
        texto = "Resumen: " + ", ".join(partes) + "."
        if self.detenido:
            texto += " Detenida por el usuario: continuará en la próxima ejecución."
        return texto


def ejecutar_subida(conf, proveedor, simular=False, actividad=None, historial=None):
    actividad = actividad or MonitorNulo()
    if not conf.origen:
        raise ErrorFatal("Falta la carpeta de origen en la configuración.")
    if not os.path.isdir(conf.origen):
        raise ErrorFatal("La carpeta de origen no existe o no es accesible: {0}".format(conf.origen))
    estado = Estado(conf.archivo_estado)
    destino = "{0}:{1}".format(proveedor.nombre, normalizar_remota(conf.carpeta_remota))
    archivos = buscar_archivos(conf)
    log.info("Origen: %s | Destino: %s | %d archivo(s) con %s", conf.origen, proveedor.descripcion_destino(),
             len(archivos), ";".join(conf.patrones))
    resumen = Resumen()
    ahora = time.time()
    pendientes = []
    for ruta, relativa in archivos:
        try:
            info = os.stat(ruta)
        except OSError as e:
            log.error("No se puede leer %s: %s", relativa, e)
            resumen.errores += 1
            continue
        clave = Estado.clave(destino, relativa)
        if estado.ya_subido(clave, info.st_size, info.st_mtime):
            log.debug("Sin cambios: %s", relativa)
            resumen.sin_cambios += 1
        elif ahora - info.st_mtime < conf.antiguedad_minima:
            log.info("Queda para la próxima ejecución %s: se modificó hace menos de %d min (podría estar "
                     "escribiéndose).", relativa, conf.antiguedad_minima // 60)
            resumen.recientes += 1
        elif simular:
            log.info("[simulación] Se subiría %s (%s)", relativa, legible(info.st_size))
            resumen.subidos += 1
            resumen.bytes += info.st_size
        else:
            pendientes.append((ruta, relativa, info.st_size, info.st_mtime, clave))
    if simular:
        return resumen

    actividad.inicio_lote(proveedor.descripcion_destino(), len(pendientes), sum(p[2] for p in pendientes))
    for indice, (ruta, relativa, tamano, mtime, clave) in enumerate(pendientes, 1):
        inicio = time.time()
        try:
            actividad.comprobar_detener()
            actividad.inicio_archivo(relativa, tamano, indice)
            log.info("Subiendo %s (%s)...", relativa, legible(tamano))
            reanudacion = Reanudacion(estado, clave, tamano, mtime)
            resultado = proveedor.subir(ruta, relativa, tamano, mtime, reanudacion)
            verificado = comprobar_subida(resultado, ruta, tamano, mtime)
        except Detenido:
            log.warning("Subida detenida por el usuario en %s; continuará en la próxima ejecución.", relativa)
            if historial:
                historial.archivo(relativa, destino, tamano, time.time() - inicio, "detenido",
                                  "Continuará en la próxima ejecución.")
            resumen.detenido = True
            break
        except (ErrorNube, ErrorTransitorio, OSError) as e:
            log.error("No se pudo subir %s: %s", relativa, e)
            if historial:
                historial.archivo(relativa, destino, tamano, time.time() - inicio, "error", str(e))
            resumen.errores += 1
            actividad.fin_archivo()
            continue
        estado.marcar(clave, tamano, mtime)
        resumen.subidos += 1
        resumen.bytes += tamano
        segundos = max(time.time() - inicio, 0.001)
        log.info("Subido %s en %s (%s/s)%s.", relativa, duracion(segundos), legible(int(tamano / segundos)),
                 ", suma de verificación correcta" if verificado else "")
        detalle = "Suma de verificación correcta." if verificado else ""

        if conf.borrar_local:
            if not verificado:
                log.warning("No se borra %s: no se pudo verificar la suma en la nube.", relativa)
            else:
                try:
                    os.remove(ruta)
                except OSError as e:
                    log.warning("No se pudo borrar el archivo local %s: %s", relativa, e)
                else:
                    estado.olvidar(clave)
                    resumen.borrados += 1
                    detalle += " Borrado del disco local."
                    log.info("Borrado del disco local: %s", relativa)
        if historial:
            historial.archivo(relativa, destino, tamano, segundos, "subido", detalle.strip(), verificado)
        actividad.fin_archivo()

    estado.podar(destino, set(relativa for _, relativa in archivos))
    return resumen


def subir_con_bloqueo(conf, proveedor, simular=False, origen="consola"):
    bloqueo = Bloqueo(conf.archivo_estado + ".lock")
    if not bloqueo.adquirir():
        log.warning("Ya hay otra subida en curso que usa %s; esta termina sin hacer nada.", conf.archivo_estado)
        return 0
    if simular:
        try:
            resumen = ejecutar_subida(conf, proveedor, simular=True)
        finally:
            bloqueo.liberar()
        log.info("%s", resumen.texto(simular=True))
        return 0

    inicio = time.time()
    historial = Historial(conf.archivo_historial)
    actividad = Actividad(conf, origen)
    proveedor.monitor = actividad
    resumen = Resumen()
    try:
        resumen = ejecutar_subida(conf, proveedor, actividad=actividad, historial=historial)
    except BaseException as e:
        detalle = str(e) if isinstance(e, ErrorFatal) else describir_error(e)
        historial.ejecucion(inicio, origen, resumen, "fallida", detalle)
        actividad.terminar("fallida", detalle)
        raise
    finally:
        bloqueo.liberar()
    detalle = resumen.texto()
    log.info("%s", detalle)
    if resumen.detenido:
        resultado, codigo = "detenida", 4
    elif resumen.errores:
        resultado, codigo = "con errores", 1
    else:
        resultado, codigo = "correcta", 0
    historial.ejecucion(inicio, origen, resumen, resultado, detalle)
    actividad.terminar(resultado, detalle)
    return codigo


# ---------------------------------------------------------------------------
# Tarea programada de Windows (schtasks)
# ---------------------------------------------------------------------------

class Programacion(object):
    def __init__(self, activa=False, frecuencia="diaria", hora="03:30", dias=None, cada_horas=4, usuario=""):
        self.activa = activa
        self.frecuencia = frecuencia
        self.hora = hora
        self.dias = list(dias or [])
        self.cada_horas = cada_horas
        self.usuario = usuario

    def descripcion(self):
        if not self.activa:
            return "Sin subida automática."
        if self.frecuencia == "diaria":
            texto = "Todos los días a las {0}".format(self.hora)
        elif self.frecuencia == "semanal":
            nombres = [d[2] for d in DIAS_SEMANA if d[0] in self.dias]
            unidos = nombres[0] if len(nombres) == 1 else ", ".join(nombres[:-1]) + " y " + nombres[-1]
            texto = "Los {0} a las {1}".format(unidos, self.hora)
        else:
            texto = "Cada {0} hora(s) a partir de las {1}".format(self.cada_horas, self.hora)
        return texto + ", con la cuenta {0}.".format(self.usuario or "SYSTEM")


def es_windows_antiguo():
    """XP/2003: schtasks usa otro formato de hora y no tiene /f ni /query /tn."""
    return os.name == "nt" and sys.getwindowsversion().major < 6


def comando_tarea(ruta_config):
    """Línea que ejecuta la tarea: pythonw (sin ventana) con este script."""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return '"{0}" "{1}" subir -c "{2}" --origen programada'.format(
        pythonw, os.path.abspath(__file__), os.path.abspath(ruta_config))


def argumentos_crear_tarea(programacion, comando, antiguo, clave=None):
    hora = programacion.hora + (":00" if antiguo else "")
    argumentos = ["schtasks", "/create", "/tn", NOMBRE_TAREA, "/tr", comando]
    if programacion.frecuencia == "diaria":
        argumentos += ["/sc", "daily", "/st", hora]
    elif programacion.frecuencia == "semanal":
        dias = [d[1] for d in DIAS_SEMANA if d[0] in programacion.dias]
        argumentos += ["/sc", "weekly", "/d", ",".join(dias), "/st", hora]
    else:
        argumentos += ["/sc", "hourly", "/mo", str(programacion.cada_horas), "/st", hora]
    if programacion.usuario:
        argumentos += ["/ru", programacion.usuario, "/rp", clave or ""]
    else:
        argumentos += ["/ru", "SYSTEM"]
    if not antiguo:
        argumentos.append("/f")
    return argumentos


def ejecutar_schtasks(argumentos):
    """Ejecuta schtasks sin abrir ninguna ventana. Devuelve (código, salida)."""
    opciones = {}
    codificacion = "latin-1"
    if os.name == "nt":
        import ctypes
        opciones["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
        codificacion = "cp{0}".format(ctypes.windll.kernel32.GetOEMCP())
    try:
        proceso = subprocess.Popen(argumentos, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, **opciones)
        salida = proceso.communicate(timeout=60)[0]
    except (OSError, subprocess.SubprocessError) as e:
        return -1, describir_error(e)
    try:
        texto = salida.decode(codificacion, "replace")
    except LookupError:
        texto = salida.decode("latin-1")
    return proceso.returncode, texto.strip()


def tarea_existe():
    if os.name != "nt":
        return False
    if es_windows_antiguo():
        carpeta = os.environ.get("SystemRoot", r"C:\WINDOWS")
        return os.path.exists(os.path.join(carpeta, "Tasks", NOMBRE_TAREA + ".job"))
    return ejecutar_schtasks(["schtasks", "/query", "/tn", NOMBRE_TAREA])[0] == 0


def aplicar_programacion(programacion, ruta_config, clave=None):
    """Crea, reemplaza o quita la tarea programada de Windows. Devuelve un texto para
    mostrar; lanza ErrorFatal si Windows la rechaza."""
    if os.name != "nt":
        raise ErrorFatal("La tarea programada solo se puede crear en Windows.")
    if tarea_existe():
        codigo, salida = ejecutar_schtasks(["schtasks", "/delete", "/tn", NOMBRE_TAREA, "/f"])
        if codigo != 0:
            raise ErrorFatal("Windows no pudo quitar la tarea anterior: " + salida)
    if not programacion.activa:
        return "Se quitó la subida automática."
    if programacion.usuario and not clave:
        raise ErrorFatal("Falta la contraseña de la cuenta {0}.".format(programacion.usuario))
    argumentos = argumentos_crear_tarea(programacion, comando_tarea(ruta_config), es_windows_antiguo(), clave)
    codigo, salida = ejecutar_schtasks(argumentos)
    if codigo != 0:
        raise ErrorFatal("Windows no pudo crear la tarea programada: " + (salida or "código {0}".format(codigo)))
    return "Subida automática programada: " + programacion.descripcion()


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

FORMATO_REGISTRO = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")


def configurar_registro(detallado):
    for manejador in list(log.handlers):
        log.removeHandler(manejador)
        manejador.close()
    log.setLevel(logging.DEBUG if detallado else logging.INFO)
    log.propagate = False
    if sys.stderr is not None:
        consola = logging.StreamHandler()
        consola.setFormatter(FORMATO_REGISTRO)
        log.addHandler(consola)


def agregar_archivo_registro(ruta):
    if not ruta:
        return
    try:
        carpeta = os.path.dirname(ruta)
        if carpeta:
            os.makedirs(carpeta, exist_ok=True)
        archivo = logging.handlers.RotatingFileHandler(ruta, maxBytes=5 * MB, backupCount=5, encoding="utf-8")
    except OSError as e:
        log.warning("No se puede escribir el registro en %s: %s", ruta, e)
        return
    archivo.setFormatter(FORMATO_REGISTRO)
    log.addHandler(archivo)


def ruta_config_predeterminada():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "subir_nube.ini")


def main(argv=None):
    analizador = argparse.ArgumentParser(
        prog="subir_nube.py", description="Copia los archivos de una carpeta local a Dropbox o Google Drive.")
    analizador.add_argument("accion", nargs="?", default="subir",
                            choices=("subir", "autorizar", "probar", "programar"),
                            help="subir (por defecto), autorizar la cuenta, probar la conexión o programar la tarea")
    analizador.add_argument("-c", "--config", default=ruta_config_predeterminada(),
                            help="archivo de configuración (por defecto subir_nube.ini junto al script)")
    analizador.add_argument("--simular", action="store_true", help="muestra qué se subiría, sin subir nada")
    analizador.add_argument("-v", "--detallado", action="store_true", help="registra también la depuración")
    analizador.add_argument("--origen", default="consola", choices=("consola", "manual", "programada"),
                            help=argparse.SUPPRESS)
    args = analizador.parse_args(argv)

    configurar_registro(args.detallado)
    proveedor = None
    try:
        conf = Configuracion(args.config)
        agregar_archivo_registro(conf.archivo_log)
        log.debug("subir_nube %s, Python %s, %s", VERSION, sys.version.split()[0], ssl.OPENSSL_VERSION)
        if args.accion == "programar":
            clave = None
            if conf.programacion.activa and conf.programacion.usuario:
                clave = getpass.getpass("Contraseña de {0}: ".format(conf.programacion.usuario))
            log.info("%s", aplicar_programacion(conf.programacion, conf.ruta, clave))
            return 0
        proveedor = crear_proveedor(conf)
        if args.accion == "autorizar":
            refresh = proveedor.autorizar()
            proveedor.tokens.guardar(proveedor.nombre, refresh)
            log.info("Cuenta de %s vinculada. El permiso se guardó en %s.", proveedor.titulo, conf.archivo_tokens)
            log.info("%s", proveedor.probar())
            return 0
        if args.accion == "probar":
            log.info("Python %s con %s", sys.version.split()[0], ssl.OPENSSL_VERSION)
            log.info("%s", proveedor.probar())
            log.info("Conexión correcta con %s.", proveedor.descripcion_destino())
            return 0
        return subir_con_bloqueo(conf, proveedor, args.simular, args.origen)
    except ErrorFatal as e:
        log.error("%s", e)
        return 2
    except (ErrorNube, ErrorTransitorio) as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        log.warning("Interrumpido por el usuario.")
        return 130
    except Exception:
        log.exception("Error inesperado")
        return 3
    finally:
        if proveedor is not None:
            proveedor.http.cerrar()


if __name__ == "__main__":
    sys.exit(main())
