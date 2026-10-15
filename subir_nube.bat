@echo off
rem Lanzador de subir_nube.py para el Programador de tareas o un trabajo del
rem Agente SQL Server (paso de tipo "Sistema operativo (CmdExec)").
rem Ajuste PYTHON si instalo Python 3.4 en otra carpeta.
rem Acepta argumentos extra, por ejemplo:  subir_nube.bat --simular

set PYTHON=C:\Python34\python.exe

"%PYTHON%" "%~dp0subir_nube.py" subir -c "%~dp0subir_nube.ini" %*
exit /b %ERRORLEVEL%
