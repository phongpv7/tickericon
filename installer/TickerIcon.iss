; Build dist\TickerIcon first with PyInstaller on Windows.
; Compile this file with Inno Setup 6.
; Pass the release version with /DMyAppVersion=x.y.z (falls back to 0.0.0-dev).

#define MyAppName "TickerIcon"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppPublisher "phongpv7"
#define MyAppExeName "TickerIcon.exe"

[Setup]
AppId={{564E4096-D5D8-4655-A483-CEF46C19B640}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=TickerIcon-Setup
SetupIconFile=..\assets\tickericon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "startup"; Description: "Start TickerIcon when I sign in to Windows"; GroupDescription: "Startup options:"

[Files]
Source: "..\dist\TickerIcon\*"; DestDir: "{app}"; Excludes: "*.cfg"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\TickerIcon\config.cfg"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: startup; IconFilename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
