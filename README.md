# SubirNube: respaldos de SQL Server a la nube desde Windows Server 2003

SubirNube es una aplicación de escritorio que copia los archivos de una carpeta del
servidor a **Dropbox** o **Google Drive**. Está pensada para los respaldos `.bak`/`.trn`
de SQL Server, incluidos archivos grandes de 15 GB o más.

- **Se instala** con un instalador: `SubirNube-2.0-instalador.exe`.
- **Tiene una ventana** con cuatro pestañas:

  | Pestaña | Para qué sirve |
  |---|---|
  | Estado | Barra de progreso, velocidad, tiempo restante, botones *Subir ahora* y *Detener*, registro en vivo |
  | Configuración | Todos los parámetros |
  | Programación | Tarea de Windows que sube automáticamente |
  | Historial | Ejecuciones y archivos subidos |

- **Funciona en Windows Server 2003** (y en XP, 2008, 7 y posteriores).

## Por qué funciona en Windows 2003

Dropbox y Google solo aceptan conexiones **TLS 1.2**, que Windows Server 2003 no tiene.
Por eso no sirve nada que use la conexión segura de Windows: Internet Explorer, .NET,
PowerShell.

SubirNube funciona con **Python 3.4.4**, la última versión de Python que se instala en
Windows XP/2003. Python 3.4.4 trae su propio OpenSSL con TLS 1.2, y el instalador de
SubirNube lo instala si falta.

## Qué hace con un respaldo de 15 GB

- **Lo envía por bloques** (16 MB por defecto). Si se corta la red, reintenta y continúa
  desde el último bloque recibido.
- **Puede continuar en otra ejecución.** Si el servidor se reinicia o usted pulsa
  *Detener* a mitad de un archivo, la próxima ejecución sigue desde donde quedó, sin
  empezar de cero. Dropbox y Google guardan una subida a medias durante una semana.
- **Verifica cada archivo**: compara el tamaño y la suma de verificación de la nube con
  las del archivo local (`content_hash` en Dropbox, MD5 en Google Drive). Solo entonces
  lo da por subido.
- **Espera a que el respaldo termine de escribirse.** No sube archivos modificados hace
  menos de 10 minutos (es configurable).
- **Recuerda lo que ya subió** y en cada ejecución solo sube lo nuevo o lo modificado.
- **Borrado local opcional**: puede borrar el archivo del disco local, pero solo después
  de verificar la copia en la nube.

### Antes de empezar: espacio y tiempo

**Espacio en la nube.** Un respaldo de 15 GB no cabe en las cuentas gratuitas:

| Servicio | Espacio |
|---|---|
| Dropbox Basic | 2 GB |
| Google Drive gratis | 15 GB, compartidos con Gmail y Fotos |

Hace falta un plan de pago (por ejemplo, Dropbox Plus o Google One de 2 TB). Con un
respaldo diario de 15 GB se acumulan unos 450 GB al mes, así que hay que **borrar los
respaldos viejos de la nube** de vez en cuando.

**Tiempo de subida.** Depende de la velocidad de *subida* de su conexión, que suele ser
mucho menor que la de bajada (sin contar otros usos de la red):

| Velocidad de subida | 15 GB tardan aproximadamente |
|---|---|
| 5 Mbps | 7 h 9 min |
| 10 Mbps | 3 h 35 min |
| 20 Mbps | 1 h 47 min |
| 50 Mbps | 43 min |
| 100 Mbps | 21 min |

Programe la subida de noche, después de que terminen los respaldos. La pestaña *Estado*
muestra la velocidad real y el tiempo restante.

## Instalación

1. En un equipo moderno, obtenga `SubirNube-2.0-instalador.exe` (vea
   [Construir el instalador](#construir-el-instalador)). Cópielo al servidor con un
   pendrive o una carpeta compartida: el servidor 2003 no puede descargar desde Internet
   por HTTPS.
2. En el servidor, ejecute el instalador con una cuenta de administrador. El instalador:
   - instala Python 3.4.4 en `C:\Python34` si no está instalado;
   - instala SubirNube en `C:\SubirNube`;
   - crea los accesos directos en el menú Inicio y, si lo elige, en el escritorio.
3. Al terminar se abre la ventana en la pestaña **Configuración**.

Para desinstalar, use *Agregar o quitar programas*. Al desinstalar:
- se quita la tarea programada;
- se pregunta si también borrar la configuración, el permiso de la cuenta y el historial;
- Python 3.4.4 queda instalado, por si otros programas lo usan.

## Configuración (una sola vez)

### 1. Registrar la aplicación en Dropbox o en Google

#### Dropbox

1. Entre a <https://www.dropbox.com/developers/apps> y pulse **Create app**.
2. Elija **Scoped access**. En el tipo de acceso, **App folder** es lo recomendado: el
   programa solo ve su propia carpeta `/Aplicaciones/<nombre de la app>`.
3. En la pestaña **Permissions**, marque `files.content.write` y pulse **Submit**.
4. En la pestaña **Settings**, copie **App key** y **App secret** a la pestaña
   *Configuración* de SubirNube.

#### Google Drive

1. Entre a <https://console.cloud.google.com> y cree un proyecto.
2. En **APIs y servicios → Biblioteca**, habilite **Google Drive API**.
3. Configure la **pantalla de consentimiento de OAuth**.
   - Use el tipo *Externo* para cuentas @gmail.com.
   - **Publique la app** (*En producción*). Mientras esté en modo *Prueba*, Google vence
     el permiso a los 7 días y las subidas dejan de funcionar.
4. En **Credenciales**, vaya a **Crear credenciales → ID de cliente de OAuth** y elija el
   tipo **TVs y dispositivos de entrada limitada**. Copie el ID y el secreto a SubirNube.

SubirNube solo pide el permiso `drive.file`: ve únicamente los archivos y carpetas que
crea él mismo, no el resto de su Drive.

### 2. Completar la pestaña Configuración

- **Carpeta de origen**: donde SQL Server deja los respaldos (botón *Examinar...*).
- **Archivos**: por ejemplo `*.bak;*.trn`.
- **Servicio y carpeta en la nube**: por ejemplo `/Respaldos/SQL`. Se crea si no existe.
- Pulse **Guardar**.

### 3. Vincular la cuenta

Pulse **Vincular cuenta...**. No hace falta un navegador moderno en el servidor:

- **Dropbox**: la ventana muestra una dirección.
  1. Ábrala en otro equipo o en el teléfono. Si trabaja por Escritorio remoto, use el
     botón *Copiar* y péguela en el navegador de su equipo.
  2. Pulse *Permitir* en Dropbox.
  3. Pegue en la ventana el código que muestra Dropbox.
- **Google Drive**: la ventana muestra un código.
  1. En cualquier equipo o teléfono, abra <https://www.google.com/device>.
  2. Escriba el código y acepte. La ventana se cierra sola.

Luego pulse **Probar conexión**: muestra la cuenta y el espacio usado.

El permiso queda en `C:\SubirNube\tokens.json`. **Ese archivo da acceso a la cuenta en la
nube**: no lo comparta.

### 4. Programar la subida automática

En la pestaña **Programación**:

1. Marque *Subir automáticamente*.
2. Elija la frecuencia: todos los días, algunos días de la semana, o cada N horas (útil
   para los `.trn`).
3. Elija la hora.
4. Pulse **Aplicar programación**.

Esto crea la tarea `SubirNube` en el Programador de tareas de Windows. La tarea:
- se ejecuta aunque nadie haya iniciado sesión;
- usa la cuenta SYSTEM por defecto;
- si la carpeta de origen está en otra máquina de la red, debe usar una cuenta de dominio
  (la contraseña se pide al aplicar y no se guarda).

Alternativa: un trabajo del **Agente SQL Server**. Agregue un paso *CmdExec* que llame a
`C:\SubirNube\subir_nube.bat`, por ejemplo al final del trabajo de respaldo. El archivo
`crear_job_sql.sql` crea uno, y sirve para SQL Server 2000 y 2005. El paso queda en rojo
si algún archivo no se pudo subir.

## Uso diario

- **Estado** muestra lo que se está subiendo, aunque la subida la haya lanzado la tarea
  programada:
  - archivo actual y su progreso;
  - velocidad y tiempo restante;
  - total de la ejecución;
  - el registro en vivo.

  Se puede cerrar la ventana sin interrumpir la subida.
- **Subir ahora** lanza una subida en ese momento.
- **Detener** la interrumpe al terminar el trozo en curso. Lo ya subido no se pierde: la
  próxima ejecución continúa desde ahí.
- **Ver qué se subiría** lista los archivos pendientes sin subir nada.
- **Historial** muestra cada ejecución y cada archivo: fecha, tamaño, duración,
  velocidad, resultado y verificación. Los errores salen en rojo; doble clic en una fila
  muestra el detalle completo.

## Solución de problemas

- **«No se pudo verificar el certificado»**:
  - revise que la fecha y la hora del servidor sean correctas;
  - reemplace `C:\SubirNube\cacert.pem` por uno recién descargado de
    <https://curl.se/ca/cacert.pem>, o reinstale una versión nueva de SubirNube.
- **«La cuenta no está vinculada»** o **«no renovó el acceso»**: vuelva a vincular la
  cuenta. Con Google, si pasa cada 7 días, la app sigue en modo *Prueba*: publíquela.
- **«La app de Dropbox no tiene el permiso…»**: marque `files.content.write` en
  *Permissions*, pulse *Submit* y vuelva a vincular la cuenta.
- **Google responde `invalid_client`**: el ID de cliente no es del tipo *TVs y
  dispositivos de entrada limitada*.
- **La tarea programada no corre**: en la pestaña *Programación*, el recuadro *Tarea en
  Windows* indica si existe. Revise también la pestaña *Historial* y el registro
  `C:\SubirNube\subir_nube.log`.
- **Errores de red repetidos**: si la red sale por un proxy, configúrelo en
  *Configuración → Avanzado*.

## Archivos en C:\SubirNube

| Archivo | Contenido |
|---|---|
| `subir_nube_gui.pyw` | La ventana |
| `subir_nube.py` | El motor que sube; también se usa desde la consola (ver abajo) |
| `subir_nube.ini` | Configuración (la crea la ventana) |
| `tokens.json` | Permiso de acceso a la cuenta en la nube |
| `estado.json` | Archivos ya subidos y subidas a medias |
| `historial.jsonl`, `subir_nube.log` | Historial y registro |
| `actividad.json` | Avance de la ejecución en curso (lo lee la ventana) |
| `cacert.pem` | Certificados raíz para HTTPS |
| `subir_nube.bat`, `crear_job_sql.sql` | Para usarlo desde el Agente SQL Server |

## Uso desde la consola

```
C:\Python34\python.exe C:\SubirNube\subir_nube.py probar
C:\Python34\python.exe C:\SubirNube\subir_nube.py subir
C:\Python34\python.exe C:\SubirNube\subir_nube.py subir --simular
C:\Python34\python.exe C:\SubirNube\subir_nube.py autorizar
C:\Python34\python.exe C:\SubirNube\subir_nube.py programar
```

| Código de salida | Significado |
|---|---|
| 0 | Todo bien |
| 1 | Algún archivo no se pudo subir |
| 2 | Error de configuración, autorización o certificados |
| 3 | Error inesperado |
| 4 | Detenida por el usuario (continuará en la próxima ejecución) |

## Construir el instalador

El instalador se compila con **Inno Setup 5.6.1**, la última versión cuyos instaladores
funcionan en Windows 2000/XP/2003.

- **En Windows 7 o posterior:**
  1. Instale `innosetup-5.6.1-unicode.exe`
     (<https://files.jrsoftware.org/is/5/>).
  2. Ejecute `instalador\construir_instalador.bat`. Descarga Python 3.4.4 y un
     `cacert.pem` actualizado, y deja el resultado en `instalador\salida\`.
- **En Linux con Wine:** use `instalador/construir_instalador.sh`.

Conviene volver a construirlo de vez en cuando para que incluya certificados raíz
actualizados.

## Pruebas (para desarrollo)

```
python -m unittest discover -s tests -v
```

Las pruebas no usan Internet: levantan un servidor local que imita Dropbox y Google Drive.
Simulan:
- cortes de red, respuestas perdidas y límites de uso;
- tokens vencidos;
- subidas detenidas y reanudadas en otra ejecución;
- sesiones de subida vencidas.

Las pruebas de la ventana necesitan tkinter. Todo se probó con el Python 3.4.4 oficial de
Windows sobre Windows Server 2003 (emulado con Wine), y con Python 3.4.10 y 3.13 en Linux.
