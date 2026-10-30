; Inno Setup script for the Windows installer — wraps the PyInstaller onedir
; bundle (out\qeth-windows, from build.sh) into qeth-<version>-setup.exe.
; Compile with installer.sh (or `scripts/wine.sh installer`), which passes
; /DAppVersion from qeth/__init__.py.
;
; Per-user by default (%LOCALAPPDATA%\Programs\qeth, no UAC prompt); the
; privileges dialog or /ALLUSERS still allows a machine-wide install.
; Uninstalling removes only what was installed: the user's ~\.qeth (keystores,
; config, caches) is never touched.

#ifndef AppVersion
  #error Pass /DAppVersion=<version> (installer.sh does)
#endif

[Setup]
; NEVER change the AppId: it is how Windows recognises an upgrade of the same
; app (and finds its uninstaller). The doubled brace is Inno's escape.
AppId={{534FA4A8-C763-43F6-994E-C0FA09DC21E3}
AppName=qeth
AppVersion={#AppVersion}
AppVerName=qeth {#AppVersion}
AppPublisher=qeth
AppPublisherURL=https://github.com/qeth-wallet/qeth
AppSupportURL=https://github.com/qeth-wallet/qeth/issues
AppUpdatesURL=https://github.com/qeth-wallet/qeth/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\qeth
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=qeth.ico
UninstallDisplayIcon={app}\qeth.exe
UninstallDisplayName=qeth
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
OutputDir=out
OutputBaseFilename=qeth-{#AppVersion}-setup

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; A onedir bundle must not mix two versions' runtimes: drop the previous
; _internal before copying, so no stale DLL or module from an older build
; can shadow a newer one.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "out\qeth-windows\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\qeth"; Filename: "{app}\qeth.exe"
Name: "{autodesktop}\qeth"; Filename: "{app}\qeth.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\qeth.exe"; Description: "{cm:LaunchProgram,qeth}"; Flags: nowait postinstall skipifsilent
