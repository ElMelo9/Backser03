@echo off
rem Construye instalador\salida\CaronteSync-1.1-instalador.exe en un equipo con
rem Windows 7 o posterior (no en el servidor 2003: este no puede descargar por HTTPS).
rem Requiere Inno Setup 5.6.1: https://files.jrsoftware.org/is/5/innosetup-5.6.1-unicode.exe

setlocal
cd /d "%~dp0"
if not exist descargas mkdir descargas

if not exist descargas\python-3.4.4.msi (
    echo Descargando Python 3.4.4...
    powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest https://www.python.org/ftp/python/3.4.4/python-3.4.4.msi -OutFile descargas\python-3.4.4.msi" || goto error
)
echo Descargando los certificados raiz actuales (cacert.pem)...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest https://curl.se/ca/cacert.pem -OutFile descargas\cacert.pem" || goto error

set ISCC=%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles%\Inno Setup 5\ISCC.exe
if not exist "%ISCC%" (
    echo No se encuentra Inno Setup 5. Instale innosetup-5.6.1-unicode.exe.
    goto error
)
"%ISCC%" carontesync.iss || goto error
echo.
echo Listo: %~dp0salida
exit /b 0

:error
echo Hubo un error.
exit /b 1
