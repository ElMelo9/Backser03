; Instalador de CaronteSync para Windows Server 2003 / XP y versiones posteriores.
;
; Se compila con Inno Setup 5.6.1, la última versión cuyos instaladores funcionan
; en Windows 2000/XP/2003. Antes de compilar deben estar en instalador\descargas\:
;   python-3.4.4.msi   https://www.python.org/ftp/python/3.4.4/python-3.4.4.msi
;   cacert.pem         https://curl.se/ca/cacert.pem
; Los scripts construir_instalador.bat (Windows) y construir_instalador.sh (Linux con
; Wine) los descargan y compilan este archivo.

#define Version "1.0"

[Setup]
AppId={{9B3C7E21-5F4A-4D8E-A1C6-3E7B2D9F0C54}
AppName=CaronteSync
AppVersion={#Version}
AppVerName=CaronteSync {#Version}
AppPublisher=CaronteSync
AppComments=Cruza los respaldos de SQL Server hacia Dropbox o Google Drive
DefaultDirName={sd}\CaronteSync
DefaultGroupName=CaronteSync
DisableProgramGroupPage=yes
DisableWelcomePage=no
OutputDir=salida
OutputBaseFilename=CaronteSync-{#Version}-instalador
SetupIconFile=..\carontesync.ico
WizardImageFile=asistente_grande.bmp
WizardSmallImageFile=asistente_pequena.bmp
UninstallDisplayIcon={app}\carontesync.ico
UninstallDisplayName=CaronteSync {#Version}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
MinVersion=5.1

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Messages]
es.WelcomeLabel2=Este programa instalará CaronteSync {#Version}.%n%nComo Caronte, el barquero que cruzaba las almas de un mundo a otro, CaronteSync cruza sus respaldos de SQL Server desde este servidor hacia la nube (Dropbox o Google Drive).%n%nSi Python 3.4.4 no está instalado, se instalará también: es la última versión de Python que funciona en Windows Server 2003 y trae su propio TLS 1.2.

[Tasks]
Name: "escritorio"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"
Name: "inicio"; Description: "Mostrar CaronteSync junto al reloj al iniciar sesión (como Dropbox)"; GroupDescription: "Al iniciar Windows:"

[Files]
Source: "descargas\python-3.4.4.msi"; Flags: dontcopy
Source: "descargas\cacert.pem"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\carontesync.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\carontesync_gui.pyw"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\carontesync_bandeja.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\carontesync.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\carontesync.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\carontesync_subiendo.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\carontesync_logo.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\carontesync.ejemplo.ini"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\crear_job_sql.sql"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; DestName: "LEAME.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\CaronteSync"; Filename: "{code:Pythonw}"; Parameters: """{app}\carontesync_gui.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\carontesync.ico"; Comment: "Respaldos a la nube"
Name: "{group}\Instrucciones"; Filename: "{app}\LEAME.txt"
Name: "{group}\Desinstalar CaronteSync"; Filename: "{uninstallexe}"
Name: "{commondesktop}\CaronteSync"; Filename: "{code:Pythonw}"; Parameters: """{app}\carontesync_gui.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\carontesync.ico"; Comment: "Respaldos a la nube"; Tasks: escritorio

[Registry]
; Al iniciar sesión, abre CaronteSync oculto junto al reloj (se puede cambiar en Programación).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CaronteSync"; ValueData: """{code:Pythonw}"" ""{app}\carontesync_gui.pyw"" --bandeja"; Flags: uninsdeletevalue; Tasks: inicio

[Run]
Filename: "{code:Pythonw}"; Parameters: """{app}\carontesync_gui.pyw"""; WorkingDir: "{app}"; Description: "Abrir CaronteSync"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Quita la subida automática para que Windows no siga ejecutando un programa que ya no está.
Filename: "{sys}\schtasks.exe"; Parameters: "/delete /tn CaronteSync /f"; Flags: runhidden

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
              'Si va a reinstalar CaronteSync, responda No para conservarlos.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{app}'), True, True, True);
end;
