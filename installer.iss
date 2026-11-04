; Установщик LAN Chat для Windows (Inno Setup).
; Ставит программу без прав администратора, добавляет значки и, при необходимости,
; доустанавливает Microsoft WebView2, без которого интерфейс не открывается.

#ifndef AppVer
  #define AppVer "2.2"
#endif

[Setup]
AppId={{8C4C2B7E-6E4B-4C69-9C2A-6B0D6E3B1A51}
AppName=LAN Chat
AppVersion={#AppVer}
AppVerName=LAN Chat {#AppVer}
AppPublisher=LAN Chat
DefaultDirName={localappdata}\Programs\LANChat
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=LANChat-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\LANChat.exe
UninstallDisplayName=LAN Chat
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать значок на рабочем столе"
Name: "autostart"; Description: "Запускать LAN Chat при входе в Windows"

[Files]
Source: "dist\LANChat\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "build\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2

[InstallDelete]
; старые файлы предыдущей версии
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\LAN Chat"; Filename: "{app}\LANChat.exe"
Name: "{autodesktop}\LAN Chat"; Filename: "{app}\LANChat.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LANChat"; ValueData: """{app}\LANChat.exe"" --minimized"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Устанавливаю Microsoft WebView2…"; Check: NeedsWebView2; Flags: waituntilterminated
Filename: "{app}\LANChat.exe"; Description: "Запустить LAN Chat"; Flags: nowait postinstall skipifsilent
; после обновления из самой программы (тихая установка) — запустить новую версию
Filename: "{app}\LANChat.exe"; Flags: nowait; Check: ShouldRelaunch

[Code]
const
  WV2 = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasVersion(Root: Integer; Key: String): Boolean;
var
  V: String;
begin
  Result := RegQueryStringValue(Root, Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

function NeedsWebView2(): Boolean;
begin
  Result := not (HasVersion(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WV2) or
                 HasVersion(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WV2) or
                 HasVersion(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WV2));
end;

function ShouldRelaunch(): Boolean;
begin
  Result := WizardSilent() and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { автозапуск мог быть включён из самой программы }
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'LANChat');
end;
