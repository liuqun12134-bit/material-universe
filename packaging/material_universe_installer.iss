#ifndef MyAppVersion
  #define MyAppVersion "1.1.0"
#endif

#define MyAppName "素材万象"
#define MyAppExeName "素材万象.exe"
#define BuildRoot "..\.build\installer\dist\素材万象"

[Setup]
AppId={{9E53ED7B-1914-4F5A-9794-58F42EB7A86B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=素材万象
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany=素材万象
VersionInfoDescription=素材万象 Windows 安装程序
VersionInfoProductName=素材万象
DefaultDirName={localappdata}\Programs\MaterialUniverse
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=..\发行版
OutputBaseFilename=素材万象安装程序
SetupIconFile=..\skills\video-product-swap\assets\material-universe.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
DirExistsWarning=no
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "{#BuildRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{userdocs}\素材万象\output"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\素材万象"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{userdocs}\素材万象"
Name: "{group}\素材万象工作区"; Filename: "{userdocs}\素材万象"
Name: "{group}\卸载素材万象"; Filename: "{uninstallexe}"
Name: "{autodesktop}\素材万象"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{userdocs}\素材万象"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动素材万象"; Flags: nowait postinstall skipifsilent
