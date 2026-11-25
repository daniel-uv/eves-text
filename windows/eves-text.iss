; Установщик Eve's Text для Windows 11 (Inno Setup 6).
; Собирается после windows/build-portable.sh:
;   ISCC.exe /DMyAppVersion=0.6.64 windows\eves-text.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.6.64"
#endif

#define MyAppName "Eve's Text"
#define MyAppPublisher "Daniel Usov"
#define MyAppURL "https://github.com/daniel-uv/eves-text"

[Setup]
AppId={{B7B2E7C6-6C93-4A54-9D3D-2F5A0A2A7C11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\Eve's Text
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=dist-installer
OutputBaseFilename=EvesText-{#MyAppVersion}-win64-setup
SetupIconFile=assets\eves-text.ico
UninstallDisplayIcon={app}\eves-text.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
MinVersion=10.0.22000
CloseApplications=yes
RestartApplications=no
ChangesAssociations=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\Eve's Text\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID на ярлыке: Windows группирует окно и панель задач под
; приложением, а не под pythonw.exe. Иконка берётся из встроенной в .exe.
Name: "{group}\{#MyAppName}"; Filename: "{app}\Eve's Text.exe"; WorkingDir: "{app}"; IconFilename: "{app}\eves-text.ico"; AppUserModelID: "DanielUsov.EvesText"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\Eve's Text.exe"; WorkingDir: "{app}"; IconFilename: "{app}\eves-text.ico"; Tasks: desktopicon; AppUserModelID: "DanielUsov.EvesText"

[Registry]
; Приложение появляется в «Параметры → Приложения → Приложения по умолчанию»
; только если оно зарегистрировано через RegisteredApplications + Capabilities.
Root: HKCU; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "Eve's Text"; ValueData: "Software\Eve's Text\Capabilities"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Eve's Text\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Eve's Text\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Текстовый редактор с проверкой орфографии и ИИ"; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Eve's Text\Capabilities"; ValueType: string; ValueName: "ApplicationIcon"; ValueData: "{app}\Eve's Text.exe,0"; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Eve's Text\Capabilities"; ValueType: string; ValueName: "AppUserModelID"; ValueData: "DanielUsov.EvesText"; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Eve's Text\Capabilities\FileAssociations"; ValueType: string; ValueName: ".txt"; ValueData: "EvesText.txt"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Eve's Text\Capabilities\FileAssociations"; ValueType: string; ValueName: ".rtf"; ValueData: "EvesText.rtf"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Eve's Text"; ValueType: string; ValueName: "AppUserModelID"; ValueData: "DanielUsov.EvesText"; Flags: uninsdeletekeyifempty

; ProgID и команда открытия: настоящий .exe без параметров.
Root: HKCR; Subkey: "EvesText.txt"; ValueType: string; ValueData: "Документ Eve's Text"; Flags: uninsdeletekeyifempty
Root: HKCR; Subkey: "EvesText.txt\DefaultIcon"; ValueType: string; ValueData: "{app}\eves-text.ico,0"; Flags: uninsdeletekey
Root: HKCR; Subkey: "EvesText.txt\shell\open\command"; ValueType: string; ValueData: """{app}\Eve's Text.exe"" ""%1"""; Flags: uninsdeletekey
Root: HKCR; Subkey: "EvesText.txt"; ValueType: string; ValueName: "AppUserModelID"; ValueData: "DanielUsov.EvesText"; Flags: uninsdeletekeyifempty
Root: HKCR; Subkey: "EvesText.rtf"; ValueType: string; ValueData: "Форматированный документ Eve's Text"; Flags: uninsdeletekeyifempty
Root: HKCR; Subkey: "EvesText.rtf\DefaultIcon"; ValueType: string; ValueData: "{app}\eves-text.ico,0"; Flags: uninsdeletekey
Root: HKCR; Subkey: "EvesText.rtf\shell\open\command"; ValueType: string; ValueData: """{app}\Eve's Text.exe"" ""%1"""; Flags: uninsdeletekey
Root: HKCR; Subkey: "EvesText.rtf"; ValueType: string; ValueName: "AppUserModelID"; ValueData: "DanielUsov.EvesText"; Flags: uninsdeletekeyifempty

; Добавляем в список «Открыть с помощью», но не делаем приложение основным.
Root: HKCR; Subkey: ".txt\OpenWithProgids"; ValueType: string; ValueName: "EvesText.txt"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".rtf\OpenWithProgids"; ValueType: string; ValueName: "EvesText.rtf"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: "Applications\Eve's Text.exe\shell\open\command"; ValueType: string; ValueData: """{app}\Eve's Text.exe"" ""%1"""; Flags: uninsdeletekey
Root: HKCR; Subkey: "Applications\Eve's Text.exe"; ValueType: string; ValueName: "AppUserModelID"; ValueData: "DanielUsov.EvesText"; Flags: uninsdeletekeyifempty

[Run]
Filename: "{app}\Eve's Text.exe"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall runasoriginaluser
