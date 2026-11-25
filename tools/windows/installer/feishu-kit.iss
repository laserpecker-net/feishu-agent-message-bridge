; feishu-kit 安装向导（Inno Setup 6）
;
; 为什么用 Inno：单文件 exe、要管理员权限、要「填配置 → 一步步装 → 重启续跑」，
; 这些是它天生会做的事；用 PowerShell 自绘界面反而要多写一千行还没有进度条。
;
; 编译：dist/windows/installer/build-installer.sh（它会用 /D 传内网参数，别手敲 ISCC）
; 脚本里**不允许**出现内网地址/CA 名/渠道名/真 key —— 这些只能从 /D 传进来（仓库有脱敏门禁）。

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef GatewayHost
  #define GatewayHost ""
#endif
#ifndef GatewayBaseUrl
  #define GatewayBaseUrl ""
#endif
#ifndef Provider
  #define Provider ""
#endif
#ifndef ChannelVar
  #define ChannelVar ""
#endif
#ifndef DshModel
  #define DshModel ""
#endif
#ifndef DshVersion
  #define DshVersion "0.2.2-rc.1"
#endif
#ifndef DistroName
  #define DistroName "Ubuntu"
#endif
; 留空 = 用 Windows 用户名推一个（见 DefaultLinuxUserFromWindows）。
; 只有我们自己的机器要固定名字时才用 /DDefaultLinuxUser=xxx 覆盖。
#ifndef DefaultLinuxUser
  #define DefaultLinuxUser ""
#endif
#ifndef ApiKeyDefault
  #define ApiKeyDefault ""
#endif
#ifndef AdminPubKeyDefault
  #define AdminPubKeyDefault ""
#endif
; 构建产物目录：由 build-installer.ps1 用 /D 传绝对路径，避免把 800 MB 载荷写进仓库目录
#ifndef StageDir
  #define StageDir "."
#endif
#ifndef OutputDir
  #define OutputDir "Output"
#endif

[Setup]
AppId={{6F1C7A2E-9B4D-4C31-8E77-1D2A5B3C9E10}
AppName=飞书 AI 助手 环境包
AppVersion={#AppVersion}
AppPublisher=feishu-kit
DefaultDirName={autopf}\feishu-kit
DefaultGroupName=feishu-kit
DisableProgramGroupPage=yes
DisableWelcomePage=no
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
; 载荷是已经压过的 .wsl/.msi/.deb/zip，再压一遍只会让 exe 更大更慢：
; 实测 Inno 的 lzma 对 775 MB 的载荷要几分钟，只换来几 MB。
Compression=none
SolidCompression=no
OutputDir={#OutputDir}
OutputBaseFilename=setup
Uninstallable=no
; [Run] 里的程序返回 3010 时，完成页要提供「立即重启」
RestartIfNeededByRun=yes
CloseApplications=no
DisableDirPage=no
UsePreviousAppDir=yes

[Files]
; apply.ps1 是编排器；其余都是「唯一实现」的副本，安装时按顺序调用。
Source: "apply.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\payload\*"; DestDir: "{app}\offline"; Flags: ignoreversion recursesubdirs createallsubdirs nocompression

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\apply.ps1"" -ConfigPath ""{tmp}\feishu-kit.json"" -AppDir ""{app}"""; StatusMsg: "正在安装（约 10-20 分钟，请勿关闭本窗口）…"; Flags: runhidden waituntilterminated

[Code]
var
  PageLinux: TInputQueryWizardPage;
  PageAi: TInputQueryWizardPage;
  PageSummary: TInputQueryWizardPage;
  SummaryMemo: TMemo;
  ConfigPath: String;

function JsonEscape(S: String): String;
var
  I: Integer;
  C: Char;
  R: String;
begin
  R := '';
  for I := 1 to Length(S) do
  begin
    C := S[I];
    if C = '\' then R := R + '\\'
    else if C = '"' then R := R + '\"'
    else if C = #13 then R := R + ''
    else if C = #10 then R := R + '\n'
    else if C = #9 then R := R + ' '
    else R := R + C;
  end;
  Result := R;
end;

function DefaultLinuxUserFromWindows(): String;
{ Windows 账号名 → 合法 Linux 用户名：小写，只留 a-z 0-9 _，去掉点/空格/中文，
  不以数字或 _ 开头（useradd 会拒绝）。推不出来就退回 dsh。

  为什么默认值要推：向导里这一格是「WSL 里那个 Linux 账号」，和 Windows 账号无关；
  但我们自己的机器习惯用同一个名字。写死某个人的名字当默认值，别人装的时候会
  莫名其妙得到一个别人的账号名 —— 上一版就是写死 "demo"。 }
var
  Raw, R: String;
  I: Integer;
  C: Char;
begin
  Raw := Lowercase(GetUserNameString());
  R := '';
  for I := 1 to Length(Raw) do
  begin
    C := Raw[I];
    if ((C >= 'a') and (C <= 'z')) or ((C >= '0') and (C <= '9')) or (C = '_') then
      R := R + C;
  end;
  if (R = '') or ((R[1] >= '0') and (R[1] <= '9')) or (R[1] = '_') then
    R := 'dsh' + R;
  Result := R;
end;

procedure InitializeWizard();
begin
  ConfigPath := ExpandConstant('{tmp}\feishu-kit.json');

  PageLinux := CreateInputQueryPage(wpSelectDir,
    'Linux 账号与远程接入',
    '这些值只写在这台机器上：Linux 账号建在 WSL 里，auth key 交给 Tailscale。',
    'Linux 密码至少 8 位（WSL 的 passwd 会拒绝更短的）。auth key 可以留空，装完再手动登录一次。');
  // 注意：Inno 的 TInputQueryWizardPage.Add 第二个参数是**布尔**（APassword，是不是密码框），
  // 不是默认值。传字符串能编译通过，但运行时抛 "Type Mismatch"，向导一开就崩。
  // 默认值要用 Values[Index] 赋值。
  PageLinux.Add('Linux 用户名（WSL 里的账号，和 Windows 账号无关）：', False);
  if '{#DefaultLinuxUser}' <> '' then
    PageLinux.Values[0] := '{#DefaultLinuxUser}'
  else
    PageLinux.Values[0] := DefaultLinuxUserFromWindows();
  // 写进 /LOG：这个值是推导出来的（Windows 用户名 → Linux 用户名），
  // 出错时要能一眼看到推导结果，而不是靠猜。
  Log('向导默认 Linux 用户名：' + PageLinux.Values[0]);
  PageLinux.Add('Linux 密码（至少 8 位）：', True);
  PageLinux.Add('Tailscale auth key（tskey-auth-…，留空则改为浏览器登录）：', False);

  PageAi := CreateInputQueryPage(PageLinux.ID,
    'AI 渠道',
    '网关地址和 key 用来让这台机器上的 DSH 调模型。',
    '网关地址填根地址（不要带 /v1）；key 会写进本机 root 0600 的文件，不会进日志。');
  PageAi.Add('AI 网关地址：', False);
  PageAi.Values[0] := '{#GatewayHost}';
  PageAi.Add('渠道 API key（与 DSH 共用，桥接也要读）：', False);
  PageAi.Values[1] := '{#ApiKeyDefault}';
  PageAi.Add('管理员公钥（Mac 的 ~/.ssh/id_ed25519.pub 内容）：', False);
  PageAi.Values[2] := '{#AdminPubKeyDefault}';

  PageSummary := CreateInputQueryPage(PageAi.ID,
    '确认安装内容',
    '点「安装」以后会依次做四步，中途失败会停下并告诉你哪一步、看哪个日志。',
    '');
  SummaryMemo := TMemo.Create(WizardForm);
  SummaryMemo.Parent := PageSummary.Surface;
  SummaryMemo.SetBounds(0, ScaleY(40), PageSummary.SurfaceWidth, ScaleY(170));
  SummaryMemo.ReadOnly := True;
  SummaryMemo.ScrollBars := ssVertical;
  SummaryMemo.TabStop := False;
end;

function LinuxUserValue(): String;
begin
  Result := Trim(PageLinux.Values[0]);
end;

function BuildConfigJson(): String;
var
  S: String;
begin
  S := '{' + #13#10;
  S := S + '  "Distro": "' + JsonEscape('{#DistroName}') + '",' + #13#10;
  S := S + '  "LinuxUser": "' + JsonEscape(LinuxUserValue()) + '",' + #13#10;
  S := S + '  "LinuxPassword": "' + JsonEscape(PageLinux.Values[1]) + '",' + #13#10;
  S := S + '  "TailscaleAuthKey": "' + JsonEscape(Trim(PageLinux.Values[2])) + '",' + #13#10;
  S := S + '  "TailscaleHostName": "' + JsonEscape(Lowercase(GetComputerNameString())) + '",' + #13#10;
  S := S + '  "GatewayHost": "' + JsonEscape(Trim(PageAi.Values[0])) + '",' + #13#10;
  S := S + '  "BaseUrl": "' + JsonEscape('{#GatewayBaseUrl}') + '",' + #13#10;
  S := S + '  "Provider": "' + JsonEscape('{#Provider}') + '",' + #13#10;
  S := S + '  "ChannelVar": "' + JsonEscape('{#ChannelVar}') + '",' + #13#10;
  S := S + '  "Model": "' + JsonEscape('{#DshModel}') + '",' + #13#10;
  S := S + '  "DshVersion": "' + JsonEscape('{#DshVersion}') + '",' + #13#10;
  S := S + '  "ApiKey": "' + JsonEscape(Trim(PageAi.Values[1])) + '",' + #13#10;
  S := S + '  "AdminPublicKey": "' + JsonEscape(Trim(PageAi.Values[2])) + '"' + #13#10;
  S := S + '}';
  Result := S;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Memo: String;
begin
  Result := True;
  if CurPageID = PageLinux.ID then
  begin
    if LinuxUserValue() = '' then
    begin
      MsgBox('请填 Linux 用户名。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if Length(PageLinux.Values[1]) < 8 then
    begin
      MsgBox('Linux 密码至少 8 位。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
  if CurPageID = PageAi.ID then
  begin
    if Trim(PageAi.Values[0]) = '' then
    begin
      MsgBox('请填 AI 网关地址。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if Trim(PageAi.Values[1]) = '' then
    begin
      if MsgBox('没填渠道 API key：装完 DSH 能起来，但调模型会失败。要继续吗？', mbConfirmation, MB_YESNO) = IDNO then
      begin
        Result := False;
        Exit;
      end;
    end;
  end;
  if CurPageID = PageSummary.ID then
  begin
    Memo := '将在这台机器上安装：' + #13#10 + #13#10;
    Memo := Memo + '1) 前置环境：WSL2 + {#DistroName} 发行版 + Linux 用户 ' + LinuxUserValue() + ' + apt/Node/lark-cli' + #13#10;
    Memo := Memo + '2) 远程接入：Tailscale 入网 + OpenSSH Server + 管理员公钥 + 端口自检' + #13#10;
    Memo := Memo + '3) 网关证书：Windows / WSL / Node 三处信任 {#GatewayHost}' + #13#10;
    Memo := Memo + '4) DSH {#DshVersion}：程序 + 三项配置 + key 文件' + #13#10 + #13#10;
    Memo := Memo + '安装日志：C:\ProgramData\feishu-kit\install.log' + #13#10;
    Memo := Memo + '完成后会告诉你 Mac 上该敲的 ssh 命令。';
    SummaryMemo.Text := Memo;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    SaveStringToFile(ConfigPath, BuildConfigJson(), False);
end;

procedure CurPageChanged(CurPageID: Integer);
var
  StateText, SshTarget, LogPath: String;
  SshTargetRaw, FailRaw: AnsiString;
begin
  if CurPageID <> wpFinished then Exit;
  LogPath := ExpandConstant('{commonappdata}\feishu-kit\install.log');
  SshTarget := '';
  #ifdef UNICODE
  SshTargetRaw := '';
  if LoadStringFromFile(ExpandConstant('{commonappdata}\feishu-kit\ssh-target.txt'), SshTargetRaw) then
    SshTarget := Trim(String(SshTargetRaw));
  #endif
  SshTarget := Trim(SshTarget);

  if FileExists(ExpandConstant('{commonappdata}\feishu-kit\install-ok.flag')) then
  begin
    StateText := '这台机器已经装好，管理员现在就能连进来。';
    if SshTarget <> '' then
      StateText := StateText + #13#10 + '在 Mac 上执行：' + SshTarget
    else
      StateText := StateText + #13#10 + 'ssh 命令见桌面上的安装报告。';
    StateText := StateText + #13#10 + '接下来由管理员通过 SSH 部署 bridge。';
    // 运营同事习惯用官方 DSH 桌面端：证书/渠道/key 已经按 Windows 账号配好，
    // 但环境变量是进程启动时读的，所以装完必须整个退出再打开。
    StateText := StateText + #13#10 + #13#10 +
      '用官方 DSH 桌面端的话：完全退出它再重新打开（证书和 key 在启动时读）。';
  end
  else
  begin
    // 失败时必须一眼看到「哪一步、为什么」，否则同事只会看到一句「没完成」然后来问。
    // apply.ps1 失败时会写 last-failure.txt（阶段 + 退出码 + 最后 14 行），这里读它。
    FailRaw := '';
    #ifdef UNICODE
    LoadStringFromFile(ExpandConstant('{commonappdata}\feishu-kit\last-failure.txt'), FailRaw);
    #endif
    StateText := '安装没有全部完成。' + #13#10 + #13#10;
    if Trim(String(FailRaw)) <> '' then
      StateText := StateText + Trim(String(FailRaw)) + #13#10 + #13#10;
    StateText := StateText + '完整日志：' + LogPath + #13#10 +
      '把 install.log 和 state.json 发给管理员，不要自己重装。';
  end;
  WizardForm.FinishedLabel.Caption := StateText;
end;
