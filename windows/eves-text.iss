; Установщик Eve's Text для Windows 11 (Inno Setup 6).
; Собирается после windows/build-portable.sh:
;   ISCC.exe /DMyAppVersion=0.6.2 windows\eves-text.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.6.2"
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

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\Eve's Text\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\bin\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\eves-text.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\bin\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\eves-text.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\bin\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
