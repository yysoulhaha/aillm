; AI-LLM 智能网关 —— Inno Setup 安装脚本
; 产物: dist\AI-LLM-Setup-2.0.0.exe
; 设计: 安装目录只放代码（可升级/卸载），用户数据自动落到 %LocalAppData%\AI-LLM。
;       安装包默认零模型；首次启动自动下载免费模型。

#define MyAppName "AI-LLM 智能网关"
#define MyAppNameShort "AI-LLM"
#define MyAppVersion "2.1.1"
#define MyAppPublisher "AI-Local"
#define MyAppExeName "AI-LLM.exe"
#define MyAppExeArgs "--open"
#define SourceDir ".."
#define MyDist SourceDir + "\dist\AI-LLM"
#define MyIcon SourceDir + "\packaging\icon.ico"

[Setup]
AppId={{C1E9A3D8-4B0F-4F3E-9F2A-5A1B2C3D4E5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppNameShort}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#SourceDir}\dist
OutputBaseFilename=AI-LLM-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile={#MyIcon}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加任务："; Flags: checkedonce
Name: "autostart"; Description: "开机自动启动网关(&S)（登录 Windows 后静默运行，无需手动点开）"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
Source: "{#MyDist}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
; 开机自启（当前用户，免管理员；卸载时自动移除）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "AI-LLM"; ValueData: """{app}\AI-LLM.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Icons]
Name: "{group}\启动 {#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "{#MyAppExeArgs}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "{#MyAppExeArgs}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "{#MyAppExeArgs}"; WorkingDir: "{app}"; Description: "启动 {#MyAppName}（首次启动自动下载免费模型）"; Flags: nowait postinstall skipifsilent