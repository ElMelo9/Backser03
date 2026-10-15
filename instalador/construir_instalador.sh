#!/bin/sh
# Construye instalador/salida/CaronteSync-1.0-instalador.exe en Linux con Wine.
# Requiere Wine (con soporte de 32 bits) e Inno Setup 5.6.1 instalado en Wine en
# C:\InnoSetup5 (wine innosetup-5.6.1-unicode.exe /VERYSILENT /DIR="C:\InnoSetup5").
set -e
cd "$(dirname "$0")"
mkdir -p descargas
[ -f descargas/python-3.4.4.msi ] || curl -fL -o descargas/python-3.4.4.msi \
    https://www.python.org/ftp/python/3.4.4/python-3.4.4.msi
curl -fL -o descargas/cacert.pem https://curl.se/ca/cacert.pem
wine 'C:\InnoSetup5\ISCC.exe' carontesync.iss
ls -l salida
