# -*- coding: utf-8 -*-
"""Servidor HTTP local que imita las partes de Dropbox y Google Drive que usa
subir_nube.py, con inyección de fallos para probar reintentos y reanudaciones."""

import hashlib
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

MB = 1024 * 1024


def hash_dropbox(datos):
    """Implementación de referencia del content_hash de Dropbox."""
    bloques = b"".join(hashlib.sha256(datos[i:i + 4 * MB]).digest() for i in range(0, len(datos), 4 * MB))
    return hashlib.sha256(bloques).hexdigest()


class _Servidor(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class _Manejador(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _atender(self):
        self.server.nube.atender(self)

    do_GET = do_POST = do_PUT = do_PATCH = _atender


class NubeFalsa(object):
    def __init__(self):
        self.bloqueo = threading.Lock()
        self.peticiones = []          # (metodo, ruta)
        self.fallos = []              # dicts: metodo, ruta (prefijo), estado, veces, despues
        self.al_recibir = None        # función (metodo, ruta) llamada al recibir cada petición
        self.token_valido = None
        self.tokens_emitidos = 0
        # Dropbox
        self.dbx_archivos = {}        # ruta en minúsculas -> (ruta, bytes, client_modified)
        self.dbx_sesiones = {}
        # Google Drive
        self.gd_elementos = {}        # id -> dict(id, name, parents, mimeType, datos, modifiedTime)
        self.gd_sesiones = {}
        self.gd_pendientes_dispositivo = 1
        self._siguiente = 0
        self.servidor = _Servidor(("127.0.0.1", 0), _Manejador)
        self.servidor.nube = self
        self.base = "http://127.0.0.1:{0}".format(self.servidor.server_address[1])
        self.hilo = threading.Thread(target=self.servidor.serve_forever)
        self.hilo.daemon = True
        self.hilo.start()

    def cerrar(self):
        self.servidor.shutdown()
        self.servidor.server_close()

    def _nuevo_id(self, prefijo):
        self._siguiente += 1
        return "{0}{1}".format(prefijo, self._siguiente)

    def fallar(self, metodo, ruta, estado=503, veces=1, despues=False, cuerpo=None, cabeceras=None):
        """La próxima petición que coincida responde 'estado'. Con despues=True,
        primero se procesa (como si la respuesta se perdiera por el camino)."""
        self.fallos.append({"metodo": metodo, "ruta": ruta, "estado": estado, "veces": veces,
                            "despues": despues, "cuerpo": cuerpo, "cabeceras": cabeceras})

    def contar(self, metodo, prefijo):
        return sum(1 for m, r in self.peticiones if m == metodo and r.startswith(prefijo))

    # -- infraestructura --------------------------------------------------

    def atender(self, h):
        largo = int(h.headers.get("Content-Length") or 0)
        cuerpo = h.rfile.read(largo) if largo else b""
        partes = urllib.parse.urlsplit(h.path)
        ruta, consulta = partes.path, urllib.parse.parse_qs(partes.query)
        with self.bloqueo:
            self.peticiones.append((h.command, ruta))
            if self.al_recibir is not None:
                self.al_recibir(h.command, ruta)
            fallo = None
            for f in self.fallos:
                if f["veces"] > 0 and f["metodo"] == h.command and ruta.startswith(f["ruta"]):
                    f["veces"] -= 1
                    fallo = f
                    break
            if fallo and not fallo["despues"]:
                return self._enviar_fallo(h, fallo)
            estado, datos, cabeceras = self._procesar(h, ruta, consulta, cuerpo)
            if fallo:
                return self._enviar_fallo(h, fallo)
            return self._enviar(h, estado, datos, cabeceras)

    def _enviar_fallo(self, h, fallo):
        self._enviar(h, fallo["estado"], fallo["cuerpo"] or {"error": "fallo simulado"}, fallo["cabeceras"])

    def _enviar(self, h, estado, datos, cabeceras=None):
        cuerpo = json.dumps(datos).encode("utf-8") if datos is not None else b""
        h.send_response(estado, "Resume Incomplete" if estado == 308 else None)
        for nombre, valor in (cabeceras or {}).items():
            h.send_header(nombre, valor)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(cuerpo)))
        h.end_headers()
        h.wfile.write(cuerpo)

    def _autorizado(self, h):
        return h.headers.get("Authorization") == "Bearer {0}".format(self.token_valido)

    def _emitir_token(self, refresh=None):
        self.tokens_emitidos += 1
        self.token_valido = "acceso-{0}".format(self.tokens_emitidos)
        datos = {"access_token": self.token_valido, "expires_in": 14400, "token_type": "bearer"}
        if refresh:
            datos["refresh_token"] = refresh
        return 200, datos, None

    def _procesar(self, h, ruta, consulta, cuerpo):
        if ruta == "/oauth2/token":
            return self._dbx_token(urllib.parse.parse_qs(cuerpo.decode("ascii")))
        if ruta in ("/token", "/device/code"):
            return self._gd_oauth(ruta, urllib.parse.parse_qs(cuerpo.decode("ascii")))
        if not self._autorizado(h) and not ruta.startswith("/upload/sesion/"):
            return 401, {"error_summary": "expired_access_token/", "error": {"code": 401}}, None
        if ruta.startswith("/2/"):
            return self._dropbox(h, ruta[3:], cuerpo)
        return self._drive(h, ruta, consulta, cuerpo)

    # -- Dropbox ----------------------------------------------------------

    def _dbx_token(self, campos):
        if campos.get("client_id") != ["clave-app"] or campos.get("client_secret") != ["secreto-app"]:
            return 400, {"error": "invalid_client", "error_description": "credenciales incorrectas"}, None
        tipo = campos["grant_type"][0]
        if tipo == "authorization_code":
            if campos.get("code") != ["codigo-ok"]:
                return 400, {"error": "invalid_grant", "error_description": "código inválido"}, None
            return self._emitir_token("refresh-dropbox")
        if campos.get("refresh_token") != ["refresh-dropbox"]:
            return 400, {"error": "invalid_grant", "error_description": "refresh token is malformed"}, None
        return self._emitir_token()

    def _dbx_meta(self, ruta):
        ruta_display, datos, modificado = self.dbx_archivos[ruta.lower()]
        return {"name": ruta_display.rsplit("/", 1)[1], "path_display": ruta_display, "size": len(datos),
                "content_hash": hash_dropbox(bytes(datos)), "client_modified": modificado}

    def _dbx_guardar(self, commit, datos):
        self.dbx_archivos[commit["path"].lower()] = (commit["path"], bytes(datos), commit.get("client_modified"))
        return 200, self._dbx_meta(commit["path"]), None

    def _dropbox(self, h, ruta, cuerpo):
        if ruta == "users/get_current_account":
            return 200, {"name": {"display_name": "Usuario Prueba"}, "email": "prueba@ejemplo.com"}, None
        if ruta == "users/get_space_usage":
            return 200, {"used": 1024, "allocation": {".tag": "individual", "allocated": 2 * 1024 ** 3}}, None
        argumento = json.loads(h.headers["Dropbox-API-Arg"])
        if ruta == "files/upload":
            return self._dbx_guardar(argumento, cuerpo)
        if ruta == "files/upload_session/start":
            sesion = self._nuevo_id("sesion-")
            self.dbx_sesiones[sesion] = bytearray(cuerpo)
            return 200, {"session_id": sesion}, None
        cursor = argumento["cursor"]
        datos = self.dbx_sesiones.get(cursor["session_id"])
        if datos is None:
            error = {".tag": "not_found"}
            if ruta.endswith("finish"):
                error = {".tag": "lookup_failed", "lookup_failed": error}
            return 409, {"error_summary": "not_found/", "error": error}, None
        if cursor["offset"] != len(datos):
            error = {".tag": "incorrect_offset", "correct_offset": len(datos)}
            if ruta.endswith("finish"):
                error = {".tag": "lookup_failed", "lookup_failed": error}
            return 409, {"error_summary": "incorrect_offset/", "error": error}, None
        datos.extend(cuerpo)
        if ruta == "files/upload_session/append_v2":
            return 200, None, None
        return self._dbx_guardar(argumento["commit"], datos)

    # -- Google Drive -----------------------------------------------------

    def _gd_oauth(self, ruta, campos):
        if campos.get("client_id") != ["cliente-gd"]:
            return 401, {"error": "invalid_client", "error_description": "The OAuth client was not found."}, None
        if ruta == "/device/code":
            return 200, {"device_code": "dispositivo-1", "user_code": "ABCD-EFGH",
                         "verification_url": "https://www.google.com/device", "expires_in": 1800,
                         "interval": 5}, None
        tipo = campos["grant_type"][0]
        if tipo.endswith("device_code"):
            if self.gd_pendientes_dispositivo > 0:
                self.gd_pendientes_dispositivo -= 1
                return 428, {"error": "authorization_pending"}, None
            return self._emitir_token("refresh-gdrive")
        if campos.get("refresh_token") != ["refresh-gdrive"]:
            return 400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}, None
        return self._emitir_token()

    def _gd_meta(self, elemento, campos=None):
        meta = {"id": elemento["id"], "name": elemento["name"]}
        if elemento["mimeType"] != "application/vnd.google-apps.folder":
            meta["size"] = str(len(elemento["datos"]))
            meta["md5Checksum"] = hashlib.md5(elemento["datos"]).hexdigest()
        return meta

    def _gd_buscar(self, q):
        patron = (r"name = '((?:[^'\\]|\\.)*)' and '([^']+)' in parents and trashed = false "
                  r"and mimeType (=|!=) '([^']+)'$")
        m = re.match(patron, q)
        if not m:
            return 400, {"error": {"code": 400, "message": "consulta no soportada: " + q}}, None
        nombre = re.sub(r"\\(.)", r"\1", m.group(1))
        padre, operador, tipo = m.group(2), m.group(3), m.group(4)
        encontrados = [{"id": e["id"], "name": e["name"]} for e in self.gd_elementos.values()
                       if e["name"] == nombre and padre in e["parents"]
                       and (e["mimeType"] == tipo) == (operador == "=")]
        return 200, {"files": encontrados}, None

    def _drive(self, h, ruta, consulta, cuerpo):
        if ruta == "/drive/v3/about":
            return 200, {"user": {"displayName": "Usuario Drive", "emailAddress": "drive@ejemplo.com"},
                         "storageQuota": {"limit": str(15 * 1024 ** 3), "usage": "2048"}}, None
        if ruta == "/drive/v3/files" and h.command == "GET":
            return self._gd_buscar(consulta["q"][0])
        if ruta == "/drive/v3/files" and h.command == "POST":
            meta = json.loads(cuerpo.decode("utf-8"))
            identificador = self._nuevo_id("carpeta-")
            self.gd_elementos[identificador] = {"id": identificador, "name": meta["name"], "datos": b"",
                                                "parents": meta["parents"], "mimeType": meta["mimeType"]}
            return 200, {"id": identificador}, None
        if ruta.startswith("/drive/v3/files/") and h.command == "GET":
            return 200, self._gd_meta(self.gd_elementos[ruta.rsplit("/", 1)[1]]), None
        if ruta.startswith("/upload/drive/v3/files"):
            assert consulta["uploadType"] == ["resumable"]
            meta = json.loads(cuerpo.decode("utf-8"))
            sesion = self._nuevo_id("subida-")
            existente = ruta.rsplit("/", 1)[1] if h.command == "PATCH" else None
            self.gd_sesiones[sesion] = {"meta": meta, "existente": existente, "datos": bytearray(),
                                        "total": int(h.headers["X-Upload-Content-Length"])}
            return 200, None, {"Location": "{0}/upload/sesion/{1}?upload_id={1}".format(self.base, sesion)}
        if ruta.startswith("/upload/sesion/"):
            sesion = self.gd_sesiones.get(ruta.rsplit("/", 1)[1])
            if sesion is None:
                return 404, {"error": {"code": 404, "message": "No such upload"}}, None
            return self._gd_bloque(h, sesion, cuerpo)
        return 404, {"error": {"code": 404, "message": "ruta desconocida " + ruta}}, None

    def _gd_bloque(self, h, sesion, cuerpo):
        rango = h.headers["Content-Range"]
        datos = sesion["datos"]
        m = re.match(r"bytes (\d+)-(\d+)/(\d+)$", rango)
        if m:
            inicio, fin = int(m.group(1)), int(m.group(2))
            if inicio != len(datos) or fin - inicio + 1 != len(cuerpo):
                return 400, {"error": {"code": 400, "message": "rango incorrecto " + rango}}, None
            datos.extend(cuerpo)
        elif not re.match(r"bytes \*/\d+$", rango):
            return 400, {"error": {"code": 400, "message": "Content-Range inválido"}}, None
        if len(datos) < sesion["total"]:
            cabeceras = {"Range": "bytes=0-{0}".format(len(datos) - 1)} if datos else {}
            return 308, None, cabeceras
        if sesion["existente"]:
            elemento = self.gd_elementos[sesion["existente"]]
        else:
            identificador = self._nuevo_id("archivo-")
            elemento = {"id": identificador, "name": sesion["meta"]["name"], "parents": sesion["meta"]["parents"],
                        "mimeType": "application/octet-stream"}
            self.gd_elementos[identificador] = elemento
        elemento["datos"] = bytes(datos)
        elemento["modifiedTime"] = sesion["meta"].get("modifiedTime")
        return 200, self._gd_meta(elemento), None

    def gd_ruta(self, ruta):
        """Busca un elemento por ruta ('Respaldos/SQL/a.bak') y lo devuelve."""
        padre = "root"
        elemento = None
        for nombre in ruta.split("/"):
            candidatos = [e for e in self.gd_elementos.values() if e["name"] == nombre and padre in e["parents"]]
            assert len(candidatos) == 1, "se esperaba un único '{0}', hay {1}".format(nombre, len(candidatos))
            elemento = candidatos[0]
            padre = elemento["id"]
        return elemento
