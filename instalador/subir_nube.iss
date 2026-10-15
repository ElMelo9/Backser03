; Instalador de SubirNube para Windows Server 2003 / XP y versiones posteriores.
;
; Se compila con Inno Setup 5.6.1, la última versión cuyos instaladores funcionan
; en Windows 2000/XP/2003. Antes de compilar deben estar en instalador\descargas\:
;   python-3.4.4.msi   https://www.python.org/ftp/python/3.4.4/python-3.4.4.msi
;   cacert.pem         https://curl.se/ca/cacert.pem
; Los scripts construir_instalador.bat (Windows) y construir_instalador.sh (Linux con
; Wine) los descargan y compilan este archivo.

#define Version "2.0"

[Setup]
AppId={{6E0F3B52-8A4C-4C1E-9B7D-2F1A5C3E8D10}
AppName=SubirNube
AppVersion={#Version}
AppVerName=SubirNube {#Version}
AppPublisher=SubirNube
AppComments=Copia los respaldos de SQL Server a Dropbox o Google Drive
DefaultDirName={sd}\SubirNube
DefaultGroupName=SubirNube
DisableProgramGroupPage=yes
DisableWelcomePage=no
OutputDir=salida
OutputBaseFilename=SubirNube-{#Version}-instalador
SetupIconFile=..\subir_nube.ico
UninstallDisplayIcon={app}\subir_nube.ico
UninstallDisplayName=SubirNube {#Version}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
MinVersion=5.1

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Messages]
es.WelcomeLabel2=Este programa instalará SubirNube {#Version}, que copia los respaldos de SQL Server (u otros archivos) a Dropbox o Google Drive.%n%nSi Python 3.4.4 no está instalado, se instalará también: es la última versión de Python que funciona en Windows Server 2003 y trae su propio TLS 1.2.

[Tasks]
Name: "escritorio"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"

[Files]
Source: "descargas\python-3.4.4.msi"; Flags: dontcopy
Source: "descargas\cacert.pem"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\subir_nube.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\subir_nube_gui.pyw"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\subir_nube.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\subir_nube.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\subir_nube.ejemplo.ini"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\crear_job_sql.sql"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; DestName: "LEAME.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\SubirNube"; Filename: "{code:Pythonw}"; Parameters: """{app}\subir_nube_gui.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\subir_nube.ico"; Comment: "Respaldos a la nube"
Name: "{group}\Instrucciones"; Filename: "{app}\LEAME.txt"
Name: "{group}\Desinstalar SubirNube"; Filename: "{uninstallexe}"
Name: "{commondesktop}\SubirNube"; Filename: "{code:Pythonw}"; Parameters: """{app}\subir_nube_gui.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\subir_nube.ico"; Comment: "Respaldos a la nube"; Tasks: escritorio

[Run]
Filename: "{code:Pythonw}"; Parameters: """{app}\subir_nube_gui.pyw"""; WorkingDir: "{app}"; Description: "Abrir SubirNube"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Quita la subida automática para que Windows no siga ejecutando un programa que ya no está.
Filename: "{sys}\schtasks.exe"; Parameters: "/delete /tn SubirNube /f"; Flags: runhidden

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\actividad.json"
Type: files; Name: "{app}\detener.solicitud"

[Code]
const
  ClavePython = 'SOFTWARE\Python\PythonCore\3.4\InstallPath';

function PythonEn(Raiz: Integer; var Carpeta: String): Boolean;
begin
  Result := RegQueryStringValue(Raiz, ClavePython, '', Carpeta) and
            FileExists(AddBackslash(Carpeta) + 'pythonw.exe');
end;

{ Carpeta de un Python 3.4 ya instalado, o cadena vacía. }
function PythonInstalado(): String;
var
  Carpeta: String;
begin
  Result := '';
  if PythonEn(HKLM, Carpeta) or PythonEn(HKCU, Carpeta) or (IsWin64 and PythonEn(HKLM64, Carpeta)) then
    Result := RemoveBackslash(Carpeta)
  else if FileExists(ExpandConstant('{sd}\Python34\pythonw.exe')) then
    Result := ExpandConstant('{sd}\Python34');
end;

function CarpetaPython(): String;
begin
  Result := PythonInstalado();
  if Result = '' then
    Result := ExpandConstant('{sd}\Python34');
end;

function Pythonw(Param: String): String;
begin
  Result := CarpetaPython() + '\pythonw.exe';
end;

{ Instala Python 3.4.4 antes de copiar los archivos, si hace falta. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Msi: String;
  Codigo: Integer;
begin
  Result := '';
  if PythonInstalado() <> '' then
    Exit;
  ExtractTemporaryFile('python-3.4.4.msi');
  Msi := ExpandConstant('{tmp}\python-3.4.4.msi');
  if not Exec(ExpandConstant('{sys}\msiexec.exe'),
              '/i "' + Msi + '" /qb ALLUSERS=1 TARGETDIR="' + CarpetaPython() + '"',
              '', SW_SHOW, ewWaitUntilTerminated, Codigo) then
    Result := 'No se pudo ejecutar el instalador de Python 3.4.4.'
  else if (Codigo <> 0) and (Codigo <> 3010) then
    Result := 'La instalación de Python 3.4.4 falló (código ' + IntToStr(Codigo) + ').'
  else if PythonInstalado() = '' then
    Result := 'Python 3.4.4 no quedó instalado en ' + CarpetaPython() + '.';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    if MsgBox('¿Borrar también la configuración, el permiso de acceso a la cuenta en la nube, ' +
              'el historial y los registros?' + #13#10#13#10 +
              'Si va a reinstalar SubirNube, responda No para conservarlos.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{app}'), True, True, True);
end;
