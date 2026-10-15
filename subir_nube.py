# -*- coding: utf-8 -*-
"""
subir_nube.py - Copia los archivos de una carpeta local a una carpeta en
Dropbox o Google Drive.

Pensado para Windows Server 2003 (por ejemplo, un servidor con SQL Server
2000/2005 que deja sus respaldos .bak en disco). Windows 2003 no habla
TLS 1.2, que es lo que exigen Dropbox y Google, así que el programa se
ejecuta con Python 3.4.4 (la última versión con instalador para XP/2003),
que trae su propio OpenSSL. Solo usa la librería estándar de Python y
funciona igual con cualquier Python 3 más moderno.

Uso:
    python subir_nube.py autorizar        Vincula la cuenta de Dropbox o Google Drive.
    python subir_nube.py probar           Comprueba la conexión y muestra la cuenta.
    python subir_nube.py subir            Sube los archivos nuevos o modificados.
    python subir_nube.py subir --simular  Muestra qué se subiría, sin subir nada.

Opciones:
    -c / --config RUTA   Archivo de configuración (por defecto, subir_nube.ini
                         en la misma carpeta que este script).
    -v / --detallado     Registra también los mensajes de depuración.

Códigos de salida: 0 = todo bien, 1 = algún archivo no se pudo subir,
2 = error de configuración, autorización o certificados, 3 = error inesperado.
"""

import argparse
import base64
import configparser
import datetime
import fnmatch
import hashlib
import http.client
import json
import logging
import logging.handlers
import os
import re
import ssl
import sys
import time
import urllib.parse

VERSION = "1.0"
MB = 1024 * 1024

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


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

VALORES_SI = ("1", "si", "sí", "s", "yes", "y", "true", "verdadero", "on")
VALORES_NO = ("0", "no", "n", "false", "falso", "off", "")


def leer_texto(ruta):
    with open(ruta, "rb") as f:
        crudo = f.read()
    try:
        return crudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        # El Bloc de notas de Windows 2003 guarda en ANSI (cp1252) por defecto.
        return crudo.decode("cp1252", "replace")


class Configuracion(object):
    """Lee subir_nube.ini. Las rutas relativas se toman desde la carpeta del .ini,
    porque el Programador de tareas y el Agente SQL arrancan en otra carpeta."""

    def __init__(self, ruta):
        self.ruta = os.path.abspath(ruta)
        if not os.path.isfile(self.ruta):
            raise ErrorFatal(
                "No existe el archivo de configuración {0}. Copie subir_nube.ejemplo.ini "
                "como subir_nube.ini y edítelo.".format(self.ruta))
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
        self.tamano_bloque = self._entero("general", "tamano_bloque_mb", 8, 1, 128) * MB
        self.reintentos = self._entero("general", "reintentos", 5, 0, 50)
        self.timeout = self._entero("general", "timeout_segundos", 120, 10, 3600)
        self.certificados = self._ruta("general", "certificados", "cacert.pem")
        self.proxy = self._texto("general", "proxy", "")
        self.archivo_log = self._ruta("general", "archivo_log", "subir_nube.log")
        self.archivo_estado = self._ruta("general", "archivo_estado", "estado.json")
        self.archivo_tokens = self._ruta("general", "archivo_tokens", "tokens.json")

        if self.certificados and not os.path.isfile(self.certificados):
            raise ErrorFatal(
                "No se encuentra el archivo de certificados {0}. Descárguelo desde "
                "https://curl.se/ca/cacert.pem (en un equipo moderno) y cópielo junto a "
                "subir_nube.py, o deje 'certificados' vacío para usar los del sistema."
                .format(self.certificados))

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

    def credencial(self, seccion, clave):
        valor = self._texto(seccion, clave, "")
        if not valor:
            raise ErrorFatal("Falta '{0}' en la sección [{1}] de {2}.".format(clave, seccion, self.ruta))
        return valor

    def archivos_propios(self):
        """Archivos del propio programa que nunca deben subirse aunque estén en el origen."""
        rutas = [self.ruta, self.certificados, self.archivo_tokens, self.archivo_tokens + ".tmp",
                 self.archivo_estado, self.archivo_estado + ".tmp", self.archivo_estado + ".lock"]
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
        try:
            self.contexto = ssl.create_default_context(cafile=certificados or None)
        except OSError as e:
            raise ErrorFatal("No se pudo cargar el archivo de certificados {0}: {1}".format(certificados, e))
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

    def cerrar(self):
        for conexion in self._conexiones.values():
            conexion.close()
        self._conexiones.clear()

    def peticion(self, metodo, url, cuerpo=None, cabeceras=None):
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
            conexion.request(metodo, ruta, body=cuerpo, headers=cab)
            respuesta = conexion.getresponse()
            datos = respuesta.read()
        except (OSError, http.client.HTTPException, ssl.CertificateError) as e:
            conexion.close()
            del self._conexiones[clave]
            if es_error_certificado(e):
                raise ErrorFatal(MENSAJE_CERTIFICADO.format(host=partes.hostname, error=e))
            raise ErrorTransitorio("error de red con {0} ({1})".format(partes.hostname, describir_error(e)))
        log.debug("%s %s%s -> HTTP %d", metodo, partes.hostname, partes.path, respuesta.status)
        cabeceras_respuesta = dict((k.lower(), v) for k, v in respuesta.getheaders())
        return Respuesta(respuesta.status, cabeceras_respuesta, datos)


def reintentar(funcion, descripcion, reintentos):
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
            _dormir(espera)


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
    de verificación en orden, aunque haya que reenviar algún trozo."""

    def __init__(self, ruta, crear_hash):
        self.crear_hash = crear_hash
        self.hash = crear_hash()
        self.hasta = 0          # bytes ya incluidos en la suma
        self.en_orden = True
        self.archivo = open(ruta, "rb")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.archivo.close()

    def leer(self, posicion, cantidad):
        self.archivo.seek(posicion)
        datos = self.archivo.read(cantidad)
        if len(datos) != cantidad:
            raise ErrorNube("el archivo se acortó mientras se subía; se volverá a intentar en la próxima ejecución")
        if posicion > self.hasta:
            self.en_orden = False
        elif posicion + len(datos) > self.hasta:
            self.hash.update(memoryview(datos)[self.hasta - posicion:])
            self.hasta = posicion + len(datos)
        return datos

    def hash_hex(self, tamano):
        if not self.en_orden or self.hasta != tamano:
            # Se saltó algún trozo: se recalcula leyendo el archivo completo.
            self.hash = self.crear_hash()
            self.archivo.seek(0)
            restante = tamano
            while restante > 0:
                datos = self.archivo.read(min(8 * MB, restante))
                if not datos:
                    break
                self.hash.update(datos)
                restante -= len(datos)
        return self.hash.hexdigest()


class Progreso(object):
    """Registra el avance de un archivo grande cada 10 %."""

    def __init__(self, nombre, total):
        self.nombre = nombre
        self.total = total
        self.siguiente = 10

    def actualizar(self, hecho):
        if self.total <= 0:
            return
        porcentaje = hecho * 100 // self.total
        if self.siguiente <= porcentaje < 100:
            log.info("  %s: %d%% (%s de %s)", self.nombre, porcentaje, legible(hecho), legible(self.total))
            self.siguiente = (porcentaje // 10 + 1) * 10


# ---------------------------------------------------------------------------
# Proveedores
# ---------------------------------------------------------------------------

class Proveedor(object):
    nombre = ""
    titulo = ""

    def __init__(self, conf, http, tokens):
        self.conf = conf
        self.http = http
        self.tokens = tokens
        self._token = None
        self._expira = 0.0

    # Cada proveedor implementa: autorizar(), probar(), subir(), _pedir_token(), _mensaje_error().

    def descripcion_destino(self):
        return "{0}:{1}".format(self.titulo, normalizar_remota(self.conf.carpeta_remota))

    def _reintentar(self, funcion, descripcion):
        return reintentar(funcion, descripcion, self.conf.reintentos)

    def _token_acceso(self, renovar=False):
        if renovar or self._token is None or time.time() > self._expira - 300:
            refresh = self.tokens.obtener(self.nombre)
            if not refresh:
                raise ErrorFatal("La cuenta de {0} no está vinculada. Ejecute primero: "
                                 "python subir_nube.py autorizar".format(self.titulo))
            r = self._pedir_token(refresh)
            self._comprobar_respuesta(r)
            datos = r.json()
            if r.estado != 200 or not datos.get("access_token"):
                raise ErrorFatal("{0} no renovó el acceso (HTTP {1}: {2}). Si se revocó el permiso o "
                                 "cambiaron las credenciales, ejecute de nuevo: python subir_nube.py autorizar"
                                 .format(self.titulo, r.estado, self._mensaje_error(r)))
            self._token = datos["access_token"]
            self._expira = time.time() + int(datos.get("expires_in", 3600))
            if datos.get("refresh_token") and datos["refresh_token"] != refresh:
                self.tokens.guardar(self.nombre, datos["refresh_token"])
        return self._token

    def _llamar(self, metodo, url, cuerpo=None, cabeceras=None):
        """Petición autenticada: renueva el token una vez si el servicio lo rechaza
        y convierte los errores temporales en ErrorTransitorio."""
        renovar = False
        while True:
            cab = dict(cabeceras or {})
            cab["Authorization"] = "Bearer " + self._token_acceso(renovar)
            r = self.http.peticion(metodo, url, cuerpo, cab)
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
                    "https://www.dropbox.com/developers/apps, pulse Submit y vuelva a ejecutar: "
                    "python subir_nube.py autorizar".format(error.get("required_scope", "files.content.write")))

    def _pedir_token(self, refresh):
        app_key, app_secret = self._credenciales()
        campos = {"grant_type": "refresh_token", "refresh_token": refresh,
                  "client_id": app_key, "client_secret": app_secret}
        return self.http.peticion("POST", self.URL_TOKEN, formulario(campos), CABECERA_FORMULARIO)

    def autorizar(self):
        app_key, app_secret = self._credenciales()
        url = self.URL_AUTORIZAR + "?" + urllib.parse.urlencode(
            {"client_id": app_key, "response_type": "code", "token_access_type": "offline"})
        print()
        print("1. Abra esta dirección en un navegador (puede ser en otro equipo o en el teléfono):")
        print()
        print("   " + url)
        print()
        print("2. Inicie sesión en Dropbox, pulse 'Permitir' y copie el código que aparece.")
        codigo = input("3. Pegue aquí el código y pulse Enter: ").strip()
        if not codigo:
            raise ErrorFatal("No se ingresó ningún código.")
        campos = {"code": codigo, "grant_type": "authorization_code",
                  "client_id": app_key, "client_secret": app_secret}
        r = self.http.peticion("POST", self.URL_TOKEN, formulario(campos), CABECERA_FORMULARIO)
        datos = r.json()
        if r.estado != 200 or not datos.get("refresh_token"):
            raise ErrorFatal("Dropbox no aceptó el código: " + self._mensaje_error(r))
        return datos["refresh_token"]

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

    def _subir_contenido(self, ruta, argumento, datos):
        cabeceras = {"Content-Type": "application/octet-stream",
                     # Debe ir en ASCII: json.dumps escapa los acentos como \uXXXX.
                     "Dropbox-API-Arg": json.dumps(argumento)}
        return self._llamar("POST", self.URL_CONTENIDO + "/" + ruta, datos, cabeceras)

    def subir(self, ruta_local, relativa, tamano, mtime):
        destino = "/" + "/".join(partes_remotas(self.conf.carpeta_remota) + relativa.split("/"))
        commit = {"path": destino, "mode": "overwrite", "autorename": False, "mute": True,
                  "client_modified": fecha_iso(mtime)}
        descripcion = "Subiendo " + relativa
        with LectorArchivo(ruta_local, HashDropbox) as lector:
            if tamano <= self.conf.tamano_bloque:
                datos = lector.leer(0, tamano)
                r = self._reintentar(lambda: self._subir_contenido("files/upload", commit, datos), descripcion)
                meta = self._exigir_ok(r, descripcion)
            else:
                meta = self._subir_por_sesion(lector, tamano, commit, relativa)
            hash_local = lector.hash_hex(tamano)
        return {"tamano": meta.get("size"), "hash_remoto": meta.get("content_hash"), "hash_local": hash_local}

    def _subir_por_sesion(self, lector, tamano, commit, relativa):
        """Archivos grandes: sesión de subida por bloques (start, append_v2, finish)."""
        descripcion = "Subiendo " + relativa
        bloque = self.conf.tamano_bloque
        progreso = Progreso(relativa, tamano)
        datos = lector.leer(0, bloque)
        r = self._reintentar(lambda: self._subir_contenido("files/upload_session/start", {"close": False}, datos),
                             descripcion)
        sesion = self._exigir_ok(r, descripcion)["session_id"]
        posicion = len(datos)
        correcciones = 0
        while True:
            progreso.actualizar(posicion)
            final = tamano - posicion <= bloque
            datos = lector.leer(posicion, min(bloque, tamano - posicion))
            cursor = {"session_id": sesion, "offset": posicion}
            if final:
                ruta, argumento = "files/upload_session/finish", {"cursor": cursor, "commit": commit}
            else:
                ruta, argumento = "files/upload_session/append_v2", {"cursor": cursor, "close": False}
            r = self._reintentar(lambda: self._subir_contenido(ruta, argumento, datos), descripcion)
            correcta = self._posicion_correcta(r)
            if correcta is not None:
                # Dropbox ya tenía más (o menos) datos: p. ej., la respuesta anterior se perdió.
                correcciones += 1
                if correcciones > 10 or not 0 <= correcta <= tamano:
                    raise ErrorNube("{0}: Dropbox no acepta la posición de la subida.".format(descripcion))
                log.info("  %s: Dropbox indica continuar desde %s.", relativa, legible(correcta))
                posicion = correcta
                continue
            meta = self._exigir_ok(r, descripcion)
            if final:
                return meta
            posicion += len(datos)

    @staticmethod
    def _posicion_correcta(r):
        if r.estado != 409:
            return None
        error = r.json().get("error", {})
        if isinstance(error, dict) and error.get(".tag") == "lookup_failed":
            error = error.get("lookup_failed", {})
        if isinstance(error, dict) and error.get(".tag") == "incorrect_offset":
            return error.get("correct_offset")
        return None


class GoogleDrive(Proveedor):
    nombre = "gdrive"
    titulo = "Google Drive"
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

    def autorizar(self):
        """Flujo para dispositivos: se autoriza desde cualquier otro equipo o teléfono,
        así que no hace falta un navegador moderno en el servidor."""
        client_id, client_secret = self._credenciales()
        r = self.http.peticion("POST", self.URL_DISPOSITIVO,
                               formulario({"client_id": client_id, "scope": self.ALCANCE}), CABECERA_FORMULARIO)
        datos = r.json()
        if r.estado != 200 or not datos.get("device_code"):
            raise ErrorFatal("Google rechazó la solicitud ({0}). Compruebe que el ID de cliente OAuth sea del tipo "
                             "'TVs y dispositivos de entrada limitada' y que la API de Google Drive esté habilitada."
                             .format(self._mensaje_error(r)))
        print()
        direccion = datos.get("verification_url") or datos.get("verification_uri") or "https://www.google.com/device"
        print("1. En cualquier equipo o teléfono abra: " + direccion)
        print("2. Escriba este código: " + datos["user_code"])
        print("3. Elija la cuenta de Google y acepte los permisos.")
        print()
        print("Esperando la autorización (puede cancelar con Ctrl+C)...")
        intervalo = int(datos.get("interval", 5))
        limite = time.time() + int(datos.get("expires_in", 1800))
        campos = {"client_id": client_id, "client_secret": client_secret, "device_code": datos["device_code"],
                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
        while time.time() < limite:
            _dormir(intervalo)
            try:
                r = self.http.peticion("POST", self.URL_TOKEN, formulario(campos), CABECERA_FORMULARIO)
            except ErrorTransitorio as e:
                log.warning("Sin respuesta de Google (%s); se sigue esperando.", e)
                continue
            datos = r.json()
            if r.estado == 200 and datos.get("refresh_token"):
                return datos["refresh_token"]
            error = datos.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                intervalo += 5
                continue
            if error == "access_denied":
                raise ErrorFatal("Se rechazó el permiso en la cuenta de Google.")
            raise ErrorFatal("Google no completó la autorización: " + self._mensaje_error(r))
        raise ErrorFatal("El código expiró antes de que se autorizara. Ejecute 'autorizar' de nuevo.")

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

    def subir(self, ruta_local, relativa, tamano, mtime):
        partes = partes_remotas(self.conf.carpeta_remota) + relativa.split("/")
        padre = self._carpeta(tuple(partes[:-1]))
        nombre = partes[-1]
        existente = self._buscar(nombre, padre, False)
        descripcion = "Subiendo " + relativa
        sesion = self._reintentar(lambda: self._iniciar_sesion(nombre, padre, existente, tamano, mtime, descripcion),
                                  descripcion)
        with LectorArchivo(ruta_local, hashlib.md5) as lector:
            meta = self._subida_reanudable(sesion, lector, tamano, relativa)
            hash_local = lector.hash_hex(tamano)
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

    def _subida_reanudable(self, sesion, lector, tamano, relativa):
        """Envía el archivo por bloques. Si un bloque falla, pregunta a Google cuánto
        recibió y continúa desde ahí. La URL de la sesión ya autoriza la subida."""
        descripcion = "Subiendo " + relativa
        progreso = Progreso(relativa, tamano)
        posicion = 0
        fallos = 0
        sin_avance = 0
        while True:
            try:
                if fallos:
                    r = self.http.peticion("PUT", sesion, b"", {"Content-Range": "bytes */{0}".format(tamano)})
                else:
                    n = min(self.conf.tamano_bloque, tamano - posicion)
                    datos = lector.leer(posicion, n)
                    if n:
                        rango = "bytes {0}-{1}/{2}".format(posicion, posicion + n - 1, tamano)
                    else:
                        rango = "bytes */{0}".format(tamano)
                    r = self.http.peticion("PUT", sesion, datos, {"Content-Range": rango})
                self._comprobar_respuesta(r)
            except ErrorTransitorio as e:
                fallos += 1
                if fallos > self.conf.reintentos:
                    raise ErrorNube("{0}: sigue fallando tras {1} reintentos ({2})".format(
                        descripcion, self.conf.reintentos, e))
                espera = calcular_espera(e, fallos)
                log.warning("%s: %s. Reintento %d de %d en %d s.", descripcion, e, fallos, self.conf.reintentos, espera)
                _dormir(espera)
                continue
            consulta = fallos > 0
            fallos = 0
            if r.estado in (200, 201):
                return r.json()
            if r.estado == 308:
                anterior = posicion
                rango = r.cabeceras.get("range")  # "bytes=0-1048575": recibido hasta ese byte
                posicion = int(rango.rsplit("-", 1)[1]) + 1 if rango else 0
                if not consulta:
                    sin_avance = sin_avance + 1 if posicion <= anterior else 0
                    if sin_avance > self.conf.reintentos:
                        raise ErrorNube(descripcion + ": Google Drive no está aceptando los datos.")
                progreso.actualizar(posicion)
                continue
            if r.estado in (404, 410):
                raise ErrorNube(descripcion + ": la sesión de subida expiró; se volverá a intentar "
                                "en la próxima ejecución.")
            raise ErrorNube("{0}: HTTP {1} ({2})".format(descripcion, r.estado, self._mensaje_error(r)))


PROVEEDORES = {"dropbox": Dropbox, "gdrive": GoogleDrive}


# ---------------------------------------------------------------------------
# Estado, permisos guardados y bloqueo
# ---------------------------------------------------------------------------

def escribir_json(ruta, datos):
    """Escribe en un temporal y lo renombra, para no dejar el archivo a medias."""
    temporal = ruta + ".tmp"
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(temporal, ruta)


class Estado(object):
    """Recuerda qué archivos ya se subieron (tamaño y fecha) para no repetirlos."""

    def __init__(self, ruta):
        self.ruta = ruta
        self.archivos = {}
        if os.path.exists(ruta):
            try:
                with open(ruta, "r", encoding="utf-8") as f:
                    self.archivos = json.load(f).get("archivos", {})
            except (OSError, ValueError, AttributeError) as e:
                log.warning("No se pudo leer %s (%s); se revisarán todos los archivos de nuevo.", ruta, e)
                self.archivos = {}

    @staticmethod
    def clave(destino, relativa):
        return destino + "|" + relativa

    def ya_subido(self, clave, tamano, mtime):
        entrada = self.archivos.get(clave)
        return bool(entrada) and entrada.get("tamano") == tamano and entrada.get("mtime") == int(mtime)

    def marcar(self, clave, tamano, mtime):
        self.archivos[clave] = {"tamano": tamano, "mtime": int(mtime), "subido": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.guardar()

    def olvidar(self, clave):
        if self.archivos.pop(clave, None) is not None:
            self.guardar()

    def podar(self, destino, vigentes):
        """Quita las entradas de archivos que ya no existen en el origen."""
        prefijo = destino + "|"
        sobrantes = [c for c in self.archivos if c.startswith(prefijo) and c[len(prefijo):] not in vigentes]
        for clave in sobrantes:
            del self.archivos[clave]
        if sobrantes:
            self.guardar()

    def guardar(self):
        try:
            escribir_json(self.ruta, {"version": 1, "archivos": self.archivos})
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
        datos[proveedor] = {"refresh_token": refresh_token, "fecha": time.strftime("%Y-%m-%d %H:%M:%S")}
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

    def texto(self, simular=False):
        verbo = "se subirían" if simular else "subidos"
        partes = ["{0} {1} ({2})".format(self.subidos, verbo, legible(self.bytes)),
                  "{0} sin cambios".format(self.sin_cambios),
                  "{0} pendientes por ser recientes".format(self.recientes),
                  "{0} con error".format(self.errores)]
        if self.borrados:
            partes.append("{0} borrados del disco local".format(self.borrados))
        return "Resumen: " + ", ".join(partes) + "."


def ejecutar_subida(conf, proveedor, simular=False):
    if not conf.origen:
        raise ErrorFatal("Falta 'origen' en la sección [general] de {0}.".format(conf.ruta))
    if not os.path.isdir(conf.origen):
        raise ErrorFatal("La carpeta de origen no existe o no es accesible: {0}".format(conf.origen))
    estado = Estado(conf.archivo_estado)
    destino = "{0}:{1}".format(proveedor.nombre, normalizar_remota(conf.carpeta_remota))
    archivos = buscar_archivos(conf)
    log.info("Origen: %s | Destino: %s | %d archivo(s) con %s", conf.origen, proveedor.descripcion_destino(),
             len(archivos), ";".join(conf.patrones))
    resumen = Resumen()
    ahora = time.time()
    for ruta, relativa in archivos:
        try:
            info = os.stat(ruta)
        except OSError as e:
            log.error("No se puede leer %s: %s", relativa, e)
            resumen.errores += 1
            continue
        tamano, mtime = info.st_size, info.st_mtime
        clave = Estado.clave(destino, relativa)
        if estado.ya_subido(clave, tamano, mtime):
            log.debug("Sin cambios: %s", relativa)
            resumen.sin_cambios += 1
            continue
        if ahora - mtime < conf.antiguedad_minima:
            log.info("Queda para la próxima ejecución %s: se modificó hace menos de %d min (podría estar "
                     "escribiéndose).", relativa, conf.antiguedad_minima // 60)
            resumen.recientes += 1
            continue
        if simular:
            log.info("[simulación] Se subiría %s (%s)", relativa, legible(tamano))
            resumen.subidos += 1
            resumen.bytes += tamano
            continue

        log.info("Subiendo %s (%s)...", relativa, legible(tamano))
        inicio = time.time()
        try:
            resultado = proveedor.subir(ruta, relativa, tamano, mtime)
            verificado = comprobar_subida(resultado, ruta, tamano, mtime)
        except (ErrorNube, ErrorTransitorio, OSError) as e:
            log.error("No se pudo subir %s: %s", relativa, e)
            resumen.errores += 1
            continue
        estado.marcar(clave, tamano, mtime)
        resumen.subidos += 1
        resumen.bytes += tamano
        segundos = max(time.time() - inicio, 0.001)
        log.info("Subido %s en %s (%s/s)%s.", relativa, duracion(segundos), legible(int(tamano / segundos)),
                 ", suma de verificación correcta" if verificado else "")

        if conf.borrar_local:
            if not verificado:
                log.warning("No se borra %s: no se pudo verificar la suma en la nube.", relativa)
                continue
            try:
                os.remove(ruta)
            except OSError as e:
                log.warning("No se pudo borrar el archivo local %s: %s", relativa, e)
            else:
                estado.olvidar(clave)
                resumen.borrados += 1
                log.info("Borrado del disco local: %s", relativa)

    if not simular:
        estado.podar(destino, set(relativa for _, relativa in archivos))
    return resumen


def subir_con_bloqueo(conf, proveedor, simular):
    bloqueo = Bloqueo(conf.archivo_estado + ".lock")
    if not bloqueo.adquirir():
        log.warning("Ya hay otra ejecución en curso que usa %s; esta termina sin hacer nada.", conf.archivo_estado)
        return 0
    try:
        resumen = ejecutar_subida(conf, proveedor, simular)
    finally:
        bloqueo.liberar()
    log.info("%s", resumen.texto(simular))
    return 1 if resumen.errores else 0


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


def main(argv=None):
    analizador = argparse.ArgumentParser(
        prog="subir_nube.py", description="Copia los archivos de una carpeta local a Dropbox o Google Drive.")
    analizador.add_argument("accion", nargs="?", default="subir", choices=("subir", "autorizar", "probar"),
                            help="subir (por defecto), autorizar la cuenta o probar la conexión")
    analizador.add_argument("-c", "--config",
                            default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "subir_nube.ini"),
                            help="archivo de configuración (por defecto subir_nube.ini junto al script)")
    analizador.add_argument("--simular", action="store_true", help="muestra qué se subiría, sin subir nada")
    analizador.add_argument("-v", "--detallado", action="store_true", help="registra también la depuración")
    args = analizador.parse_args(argv)

    configurar_registro(args.detallado)
    http = None
    try:
        conf = Configuracion(args.config)
        agregar_archivo_registro(conf.archivo_log)
        log.debug("subir_nube %s, Python %s, %s", VERSION, sys.version.split()[0], ssl.OPENSSL_VERSION)
        http = ClienteHttp(conf.certificados, conf.proxy, conf.timeout)
        tokens = AlmacenTokens(conf.archivo_tokens)
        proveedor = PROVEEDORES[conf.proveedor](conf, http, tokens)

        if args.accion == "autorizar":
            refresh = proveedor.autorizar()
            tokens.guardar(proveedor.nombre, refresh)
            log.info("Cuenta de %s vinculada. El permiso se guardó en %s.", proveedor.titulo, conf.archivo_tokens)
            log.info("%s", proveedor.probar())
            return 0
        if args.accion == "probar":
            log.info("Python %s con %s", sys.version.split()[0], ssl.OPENSSL_VERSION)
            log.info("%s", proveedor.probar())
            log.info("Conexión correcta con %s.", proveedor.descripcion_destino())
            return 0
        return subir_con_bloqueo(conf, proveedor, args.simular)
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
        if http is not None:
            http.cerrar()


if __name__ == "__main__":
    sys.exit(main())
