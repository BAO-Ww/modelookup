; ============================================================
;  型号查询 · 隆阳电器（公布版）—— Windows 安装包
;  用 Inno Setup 6 编译：
;     ISCC.exe modelookup.iss
;     ISCC.exe /DMyAppVer=2.1 /DMyAppVerNum=2.1.0.0 modelookup.iss    ← CI 用这个注入版本号
;  产物：dist_installer\ModelLookup-Setup-<版本>.exe
; ============================================================

; 版本号默认值；CI 用 /D 覆盖，平时本机直接编也会有一份完整可用的包
#ifndef MyAppVer
  #define MyAppVer "2.0"
#endif
#ifndef MyAppVerNum
  #define MyAppVerNum "2.0.0.0"
#endif

#define AppName     "型号查询"
#define AppNameFull "型号查询 · 隆阳电器"
#define AppVer      MyAppVer
#define AppVerNum   MyAppVerNum
#define AppPublisher "隆阳电器"
#define ExeName     "ModelLookup.exe"

[Setup]
AppId={{8F3C1A62-5D74-4E1B-9C0A-7B2E6D4F9A31}
AppName={#AppNameFull}
AppVersion={#AppVer}
AppVerName={#AppNameFull} v{#AppVer}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVerNum}
VersionInfoDescription={#AppNameFull} 安装程序
VersionInfoProductName={#AppNameFull}
VersionInfoProductVersion={#AppVerNum}

; 只装给当前用户，不需要管理员密码（装到别的电脑上最省事）
DefaultDirName={localappdata}\Programs\ModelLookup
DefaultGroupName={#AppName}
PrivilegesRequired=lowest

DisableProgramGroupPage=yes
DisableDirPage=auto
AllowNoIcons=yes
CloseApplications=yes
RestartApplications=no

OutputDir=..\dist_installer
OutputBaseFilename=ModelLookup-Setup-{#AppVer}
SetupIconFile=..\app\assets\icon.ico
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppNameFull} v{#AppVer}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
MinVersion=10.0
; 安装包本身就是简体中文界面
ShowLanguageDialog=no

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl,ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
; CI 和本机都构建到 app\dist\（同一个路径，两边行为一致）
Source: "..\app\dist\{#ExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\型号查询-公布版.html"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; WebView2 引导安装器：只在缺运行时的电脑上才装（2MB，随包分发）
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\型号查询"; Filename: "{app}\{#ExeName}"; Comment: "海尔系型号 / 价格 / 库存查询"
Name: "{group}\型号查询 · 离线网页版"; Filename: "{app}\型号查询-公布版.html"; Flags: createonlyiffileexists
Name: "{group}\卸载 型号查询"; Filename: "{uninstallexe}"
Name: "{userdesktop}\型号查询"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; \
    StatusMsg: "正在安装 Microsoft Edge WebView2 运行时（第一次装需要，约 1 分钟）…"; \
    Flags: waituntilterminated runhidden; Check: not WebView2Installed
Filename: "{app}\{#ExeName}"; Description: "立即打开 型号查询"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM {#ExeName} >nul 2>&1"; Flags: runhidden; RunOnceId: "KillApp"

[Code]
const
  WV2_KEY = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WV2_KEY2 = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasPv(Root: Integer): Boolean;
var
  pv: String;
begin
  Result := False;
  if RegQueryStringValue(Root, WV2_KEY, 'pv', pv) then
    Result := (pv <> '') and (pv <> '0.0.0.0');
  if (not Result) and RegQueryStringValue(Root, WV2_KEY2, 'pv', pv) then
    Result := (pv <> '') and (pv <> '0.0.0.0');
end;

function WebView2Installed(): Boolean;
begin
  Result := HasPv(HKLM32) or HasPv(HKLM64) or HasPv(HKCU32) or HasPv(HKCU64);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not WebView2Installed() then
      MsgBox('没有装上 WebView2 运行时，软件可能打不开界面。' + #13#10 +
             '可以手动装一下（免费）：https://go.microsoft.com/fwlink/p/?LinkId=2124703',
             mbInformation, MB_OK);
  end;
end;
