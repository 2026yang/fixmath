; Inno Setup script for FixMath (P0).
; 1) build the exe:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
; 2) open this file in Inno Setup Compiler and press Compile.

#define AppName "FixMath"
#define AppVersion "0.1.1"
#define AppExe "FixMath.exe"

[Setup]
AppId={{8F1E2C64-3A1B-4E8E-9A0B-6F7C2D5A1E33}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=FixMath
DefaultDirName={autopf}\FixMath
DefaultGroupName=FixMath
DisableProgramGroupPage=yes
OutputBaseFilename=FixMath-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayName={#AppName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "autostart"; Description: "开机自动启动 FixMath"; GroupDescription: "附加任务:"

[Files]
Source: "..\dist\FixMath\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\FixMath"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 FixMath"; Filename: "{uninstallexe}"
Name: "{autodesktop}\FixMath"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "FixMath"; ValueData: """{app}\{#AppExe}"""; \
  Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "启动 FixMath"; Flags: nowait postinstall skipifsilent
