# Backser03: subir respaldos a la nube desde Windows Server 2003

`subir_nube.py` copia los archivos de una carpeta local a una carpeta en **Dropbox** o
**Google Drive**. Un ejemplo son los respaldos `.bak` y `.trn` de SQL Server. Está pensado
para servidores **Windows Server 2003** y se puede programar con el Agente SQL Server o
con el Programador de tareas.

## Por qué Python 3.4

Dropbox y Google solo aceptan conexiones **TLS 1.2**, y Windows Server 2003 no lo soporta.
Por eso nada que use la conexión segura de Windows puede comunicarse con ellos:
Internet Explorer, .NET, PowerShell, `bitsadmin`, etc.

**Python 3.4.4** es la última versión de Python con instalador para Windows XP/2003, y
trae **su propio OpenSSL con TLS 1.2**. El programa usa solo la librería estándar de
Python, así que no hay que instalar nada más.

## Qué hace

- Sube los archivos que coinciden con los patrones (por ejemplo `*.bak;*.trn`).
  Opcionalmente incluye las subcarpetas.
- Recuerda lo que ya subió (`estado.json`): en cada ejecución solo sube lo nuevo o lo
  modificado.
- No sube archivos modificados hace menos de N minutos, porque SQL Server puede seguir
  escribiéndolos. Quedan para la siguiente ejecución.
- Sube por bloques (8 MB por defecto), así que sirve para respaldos de muchos GB.
  Si se corta la red, reintenta y continúa desde donde quedó.
- **Verifica cada archivo subido**: compara el tamaño y la suma de verificación
  (`content_hash` en Dropbox, MD5 en Google Drive) con las del archivo local.
- Opcionalmente borra el archivo local, pero solo después de verificar la copia en la nube.
- Si el archivo ya existe en la nube, lo reemplaza.
- Deja un registro en `subir_nube.log`, que se rota a los 5 MB.
- Devuelve códigos de salida que el Agente SQL entiende: si algo falla, el trabajo queda
  en rojo.
- Si una ejecución anterior sigue subiendo, la nueva termina sin hacer nada.

## Instalación

### 1. Descargas (en un equipo moderno)

El servidor 2003 no puede abrir estos sitios. Descárguelos en otro equipo y cópielos al
servidor con un pendrive o una carpeta compartida.

| Qué | Dónde |
|---|---|
| Python 3.4.4 | <https://www.python.org/downloads/release/python-344/>: `python-3.4.4.msi` (32 bits) o `python-3.4.4.amd64.msi` (Windows 2003 x64) |
| Certificados raíz | <https://curl.se/ca/cacert.pem> |
| Este programa | Botón **Code → Download ZIP** de este repositorio |

### 2. En el servidor

1. Instale Python 3.4.4. Por defecto queda en `C:\Python34`.
2. Cree la carpeta `C:\SubirNube` y copie en ella `subir_nube.py`, `subir_nube.bat`,
   `subir_nube.ejemplo.ini` y `cacert.pem`.
3. Copie `subir_nube.ejemplo.ini` como `subir_nube.ini` y edítelo con el Bloc de notas.
   Indique al menos `proveedor`, `origen`, `carpeta_remota` y `patrones`.
4. Compruebe que Python trae TLS 1.2:

   ```
   C:\Python34\python.exe -c "import ssl; print(ssl.OPENSSL_VERSION)"
   ```

   Debe mostrar `OpenSSL 1.0.2...` o una versión posterior.

### 3. Registrar la aplicación en Dropbox o Google (una sola vez)

#### Dropbox

1. Entre a <https://www.dropbox.com/developers/apps> y pulse **Create app**.
2. Elija **Scoped access**. En el tipo de acceso, **App folder** es lo recomendado: el
   programa solo ve su propia carpeta `/Aplicaciones/<nombre de la app>`. La otra opción
   es **Full Dropbox**.
3. En la pestaña **Permissions**, marque `files.content.write` y pulse **Submit**.
   Hágalo *antes* de vincular la cuenta.
4. En la pestaña **Settings**, copie **App key** y **App secret** en la sección
   `[dropbox]` de `subir_nube.ini`.

#### Google Drive

1. Entre a <https://console.cloud.google.com> y cree un proyecto.
2. En **APIs y servicios → Biblioteca**, busque **Google Drive API** y pulse
   **Habilitar**.
3. Configure la **pantalla de consentimiento de OAuth** (Google Auth Platform).
   - Use el tipo *Externo* para cuentas @gmail.com o *Interno* para Google Workspace.
   - Si eligió *Externo*, **publique la app** con **Publicar app / En producción**.
     Mientras siga en modo *Prueba*, Google vence el permiso a los 7 días y la subida deja
     de funcionar.
4. En **Credenciales**, vaya a **Crear credenciales → ID de cliente de OAuth** y elija el
   tipo **TVs y dispositivos de entrada limitada**. Copie el ID y el secreto en la sección
   `[gdrive]`.

El programa solo pide el permiso `drive.file`. Con él puede ver y modificar únicamente
los archivos y carpetas que crea él mismo, no el resto de su Drive. Por eso, si usted ya
creó a mano una carpeta con el mismo nombre que el destino, el programa no la ve y crea
la suya.

### 4. Vincular la cuenta (una sola vez)

```
cd C:\SubirNube
C:\Python34\python.exe subir_nube.py autorizar
```

- **Dropbox**: el programa muestra un enlace. Ábralo en cualquier equipo o teléfono,
  pulse **Permitir** y pegue en la consola el código que aparece.
  - Para copiar en la consola de Windows 2003: clic derecho → *Marcar*, seleccione el
    texto y pulse Enter.
  - Para pegar: clic derecho → *Pegar*.
- **Google Drive**: el programa muestra un código. En cualquier equipo o teléfono abra
  <https://www.google.com/device>, escriba el código y acepte. El programa detecta la
  autorización por su cuenta.

El permiso queda guardado en `tokens.json`. **Ese archivo da acceso a la cuenta en la
nube**: limite sus permisos NTFS a los administradores y a la cuenta que ejecuta la tarea.

### 5. Probar

```
C:\Python34\python.exe subir_nube.py probar
C:\Python34\python.exe subir_nube.py subir --simular
C:\SubirNube\subir_nube.bat
```

- `probar` muestra la versión de OpenSSL, la cuenta conectada y el espacio usado.
- `--simular` lista lo que se subiría, sin subir nada.
- El `.bat` hace una subida real.

### 6. Programar la ejecución

**Opción A: Agente SQL Server.** Abra `crear_job_sql.sql`, ajuste la ruta y la hora, y
ejecútelo en el Analizador de consultas (SQL 2000) o en Management Studio (SQL 2005).
Crea un trabajo diario con un paso de tipo *CmdExec* que llama a `subir_nube.bat`.
Otra posibilidad es agregar ese mismo paso al final del trabajo que ya hace los
respaldos, para que la subida empiece en cuanto terminen.

**Opción B: Programador de tareas.**

```
schtasks /create /tn "Subir respaldos a la nube" /tr "C:\SubirNube\subir_nube.bat" /sc daily /st 03:30:00 /ru "System"
```

La cuenta que ejecuta la tarea necesita leer la carpeta de origen y `C:\SubirNube`.
`System` no puede leer carpetas compartidas de la red. Si el origen es una ruta
`\\servidor\...`, use una cuenta de dominio con `/ru DOMINIO\usuario`: `schtasks` pedirá
la contraseña.

## Configuración (`subir_nube.ini`)

| Opción | Por defecto | Descripción |
|---|---|---|
| `proveedor` | `dropbox` | `dropbox` o `gdrive` |
| `origen` | (obligatorio) | Carpeta local con los archivos a subir |
| `carpeta_remota` | `/` | Carpeta de destino en la nube; se crea si no existe |
| `patrones` | `*` | Archivos a subir, separados por `;` (p. ej. `*.bak;*.trn`) |
| `recursivo` | `no` | `si` = incluye subcarpetas y las recrea en la nube |
| `antiguedad_minima_minutos` | `10` | No sube archivos modificados hace menos de estos minutos |
| `borrar_local_tras_subir` | `no` | `si` = borra el archivo local tras verificar la copia en la nube |
| `tamano_bloque_mb` | `8` | Tamaño de cada bloque enviado (1-128) |
| `reintentos` | `5` | Reintentos ante cortes de red o errores temporales del servicio |
| `timeout_segundos` | `120` | Tiempo máximo de espera de cada petición |
| `certificados` | `cacert.pem` | Certificados raíz; vacío = los de Windows (desactualizados en 2003) |
| `proxy` | (vacío) | `host:puerto` o `http://usuario:clave@host:puerto` |
| `archivo_log`, `archivo_estado`, `archivo_tokens` | `subir_nube.log`, `estado.json`, `tokens.json` | Rutas relativas a la carpeta del `.ini` |

Puede tener varias configuraciones (por ejemplo, una por base de datos o por servicio)
y elegir cuál usar con `-c`:
`python subir_nube.py subir -c otra.ini`. Si dos configuraciones comparten carpeta, use un
`archivo_estado` distinto en cada una.

## Códigos de salida

| Código | Significado |
|---|---|
| 0 | Todo bien (o no había nada que subir) |
| 1 | Algún archivo no se pudo subir; se reintentará en la próxima ejecución |
| 2 | Error de configuración, de autorización o de certificados: no se subió nada |
| 3 | Error inesperado (el detalle queda en `subir_nube.log`) |

## Solución de problemas

- **«No se pudo verificar el certificado»**: revise que la fecha y la hora del servidor
  sean correctas, y reemplace `cacert.pem` por uno recién descargado de
  <https://curl.se/ca/cacert.pem>.
- **«La cuenta no está vinculada»** o **«no renovó el acceso»**: ejecute
  `subir_nube.py autorizar`. Con Google, si esto pasa cada 7 días, la app sigue en modo
  *Prueba*: publíquela (paso 3).
- **«La app de Dropbox no tiene el permiso…»**: marque el permiso en la pestaña
  *Permissions* de la app, pulse *Submit* y vuelva a ejecutar `autorizar`. Los permisos
  nuevos solo valen para las autorizaciones hechas después del cambio.
- **Google responde `invalid_client` al autorizar**: el ID de cliente no es del tipo
  *TVs y dispositivos de entrada limitada*.
- **«Queda para la próxima ejecución … se modificó hace menos de N min»**: es normal si
  el respaldo acaba de terminar. Ajuste `antiguedad_minima_minutos` si hace falta.
- **Errores de red repetidos**: si la red sale por un proxy, configure `proxy`.
- Para ver más detalle, ejecute el programa con `-v`.

## Pruebas (para desarrollo)

```
python -m unittest discover -s tests -v
```

Las pruebas no usan Internet. Levantan un servidor local que imita Dropbox y Google
Drive, y simulan cortes de red, respuestas perdidas, límites de uso y tokens vencidos.
Funcionan con Python 3.4 y con versiones más modernas.
