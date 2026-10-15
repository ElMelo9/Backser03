@echo off
rem Lanzador de carontesync.py para el Programador de tareas o un trabajo del
rem Agente SQL Server (paso de tipo "Sistema operativo (CmdExec)").
rem Ajuste PYTHON si instalo Python 3.4 en otra carpeta.
rem Acepta argumentos extra, por ejemplo:  carontesync.bat --simular

set PYTHON=C:\Python34\python.exe

"%PYTHON%" "%~dp0carontesync.py" subir -c "%~dp0carontesync.ini" %*
exit /b %ERRORLEVEL%
