#Requires -Version 5.1
<#
apply.ps1（setup.exe 的编排器）的纯函数测试。跑法：
  pwsh -NoProfile -File tools/windows/installer/apply.Tests.ps1
  powershell -NoProfile -File tools\windows\installer\apply.Tests.ps1

不测「装不装得上」（那要真机），只钉三件容易回归的事：
  1. 配置校验：缺什么必须说清缺什么，密码太短要拦住；
  2. 路径换算：Windows 盘符 → /mnt/c/...（wsl 里唯一的路径契约）；
  3. 续跑语义：state.json 读出来是 Hashtable，done 的步骤必须被跳过。
#>
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$scriptPath = Join-Path $PSScriptRoot 'apply.ps1'
if (-not (Test-Path -LiteralPath $scriptPath)) { throw "缺少 apply.ps1：$scriptPath" }
. $scriptPath

$script:failures = 0
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { $script:failures++; Write-Host "FAIL: $Message" -ForegroundColor Red }
    else { Write-Host "ok: $Message" }
}
function Assert-Equal($Expected, $Actual, [string]$Message) {
    if ($Expected -ne $Actual) { Write-Host ("  expected: {0}`n  actual:   {1}" -f $Expected, $Actual) -ForegroundColor Yellow }
    Assert-True ($Expected -eq $Actual) $Message
}

function New-TestConfig {
    [pscustomobject]@{
        Distro = 'Ubuntu'; LinuxUser = 'someone'; LinuxPassword = 'TestPass123!'
        TailscaleAuthKey = 'tskey-auth-test'; TailscaleHostName = 'box-win'
        GatewayHost = '10.11.12.13'; BaseUrl = 'https://10.11.12.13/v1'
        Provider = 'chan'; ChannelVar = 'CHAN_KEY'; Model = 'model-x'
        DshVersion = '0.2.2-rc.1'; ApiKey = 'sk-test'; AdminPublicKey = 'ssh-ed25519 AAAA test'
    }
}

# --- 配置校验 ---
$good = New-TestConfig
$threw = $false
try { Assert-Config $good } catch { $threw = $true }
Assert-True (-not $threw) '完整配置通过校验'

$bad = New-TestConfig
$bad.LinuxUser = ''
$bad.GatewayHost = '   '
$message = ''
try { Assert-Config $bad } catch { $message = $_.Exception.Message }
Assert-True ($message -match 'Linux 用户名') '缺 LinuxUser 时报出字段名'
Assert-True ($message -match 'AI 网关地址') '空白字符串也算缺（不是只有 null）'

$shortPw = New-TestConfig
$shortPw.LinuxPassword = 'short'
$message = ''
try { Assert-Config $shortPw } catch { $message = $_.Exception.Message }
Assert-True ($message -match '至少 8 位') '密码短于 8 位被拦住（WSL passwd 的要求）'

$noModel = New-TestConfig
$noModel.Model = ''
$message = ''
try { Assert-Config $noModel } catch { $message = $_.Exception.Message }
Assert-True ($message -match '默认模型') '缺默认模型时报出字段名'

# --- 路径换算 ---
Assert-Equal '/mnt/c/Users/someone/x.sh' (ConvertTo-WslPath 'C:\Users\someone\x.sh') 'C 盘路径换算成 /mnt/c/...（用中性占位名，不带真用户名）'
Assert-Equal '/mnt/d/a b/y.sh' (ConvertTo-WslPath 'D:\a b\y.sh') '带空格的路径不做转义（wsl.exe 只吃一个参数）'

# --- ssh 目标 ---
# ssh 登录的是 **Windows 账号**（Windows 的 sshd 认本机账号，不认 WSL 里的），
# 所以完成页那条命令里的用户名必须取 Windows 账号，不是 Config.LinuxUser。
Assert-Equal ('ssh {0}@box-win' -f $env:USERNAME) (Get-SshTarget -Config $good) 'ssh 目标用 Windows 账号 + TailscaleHostName'
$differ = New-TestConfig
$differ.LinuxUser = 'a-different-linux-name'
Assert-Equal ('ssh {0}@box-win' -f $env:USERNAME) (Get-SshTarget -Config $differ) 'Linux 用户名和 Windows 账号不同名时仍用 Windows 账号（否则 ssh 会连到不存在的账号）'
$noName = New-TestConfig
$noName.TailscaleHostName = ''
$target = Get-SshTarget -Config $noName
Assert-True ($target -match ('^ssh ' + [regex]::Escape($env:USERNAME) + '@' + [regex]::Escape($env:COMPUTERNAME.ToLower()) + '$')) '没给设备名时退化成计算机名'

# --- 桌面端的 profile patch ---
# desktop 和 web 是**两套 profile**：官方桌面端只读 profiles\desktop，Windows 原生 dsh web 读 profiles\web。
# 真机踩过：配置写进 web，桌面端一个 provider 都不多（dump 对照拿到的）。
$yaml = (Get-DshProfilePatchYaml -Config $good) -replace "`r", ''   # 写文件时是 CRLF，断言前统一成 LF
Assert-True ($yaml -match '(?m)^- id: llm-pi-ai$') 'patch 里有 llm-pi-ai 段'
Assert-True ($yaml -match '(?m)^        apiKeyEnv: CHAN_KEY$') 'apiKeyEnv 用渠道变量名（不复述 key 值）'
Assert-True ($yaml -match [regex]::Escape('baseURL: ' + $good.BaseUrl)) 'baseURL 来自配置'
Assert-True ($yaml -match '(?m)^    model: model-x$') '默认模型来自配置'
Assert-True ($yaml -notmatch [regex]::Escape([string]$good.ApiKey)) 'patch 里不出现 key 本身（key 走环境变量）'
Assert-Equal (Get-WindowsCaPemPath) (Join-Path $script:StateDir 'certs\gateway-root.crt') 'PEM 路径只在一处定义'

Assert-True ((Get-DshProfilePatchBlock -Config $good) -match [regex]::Escape($script:DshPatchMarkerBegin)) '写进文件的这一段带开始标记（重跑靠它定位）'

# --- 模型清单：不是「配置里那一个」，而是网关 /v1/models 给的全部（踩坑 22） ---
# 真机踩过：网关实际服务 3 个模型，安装程序只写了配置里的默认模型，同事的模型选择器里就只有一个。
$yaml3 = (Get-DshProfilePatchYaml -Config $good -Models @('model-a', 'model-b', 'model-x')) -replace "`r", ''
Assert-Equal 3 ([regex]::Matches($yaml3, '(?m)^          - id: ').Count) '传了 3 个模型就写 3 条'
Assert-True ($yaml3 -match '(?m)^          - id: model-a$') '模型清单第一条在'
Assert-True ($yaml3 -match '(?m)^            name: model-b$') '每条都带 name（选择器显示用）'
Assert-True ($yaml3 -match '(?m)^    model: model-x$') '默认模型仍然来自 Config.Model'
Assert-Equal -1 $yaml3.IndexOf('- id: model-z') '清单里没有的模型不会被凭空写进去'
Assert-Equal 'sequence' (Get-DshPatchRootKind -Existing $yaml3) '多模型仍是同一个根序列（不是两个根节点）'

# 去重 + 「默认模型必须在清单里」：不在就补到最前面，在就保持网关顺序
Assert-Equal 'model-a,model-x' ((Merge-ModelIds -Models @('model-a', 'model-x') -Default 'model-x') -join ',') '默认模型已在清单里时保持网关顺序'
Assert-Equal 'model-z,model-a' ((Merge-ModelIds -Models @('model-a') -Default 'model-z') -join ',') '默认模型不在清单里时补到最前面'
Assert-Equal 'model-a' ((Merge-ModelIds -Models @('model-a', 'model-a', '') -Default '') -join ',') '清单去重且忽略空值'
Assert-Equal '' ((Merge-ModelIds -Models @() -Default '') -join ',') '全空时返回空（调用方自己决定退回什么）'

# 网关取不到清单时退回默认模型（网络不测，测的是「退回的那一个一定在」这条语义）
$noModels = (Get-DshProfilePatchYaml -Config $good) -replace "`r", ''
Assert-Equal 1 ([regex]::Matches($noModels, '(?m)^          - id: ').Count) '不传模型清单时只写默认模型（网关不支持 /v1/models 的兜底）'

Assert-Equal 'desktop,web' ((Select-DshPatchProfileNames -ExistingProfiles @('web', 'desktop', 'headless')) -join ',') '两个 profile 都在时桌面端排前面，别的 profile 不碰'
Assert-Equal 'web' ((Select-DshPatchProfileNames -ExistingProfiles @('web', 'headless')) -join ',') '只有 web（桌面端没跑过）时只写 web'
Assert-Equal '' ((Select-DshPatchProfileNames -ExistingProfiles @('headless')) -join ',') '两个都没有时一个都不写（交给待用文件那条路）'

$block = Get-DshProfilePatchBlock -Config $good
$desktopOwn = (@('# Your patch layer for this dsh profile, applied after every bundle layer:', '- id: ui-chat', '  config:', '    transcriptView: standard') -join "`r`n")
$mergedOnce = Merge-DshProfilePatch -Existing $desktopOwn -Block $block
$mergedOnceLf = $mergedOnce -replace "`r", ''   # 文件是 CRLF，断言前统一成 LF（否则 (?m)$ 对不上）
Assert-True ($mergedOnceLf -match '(?m)^- id: ui-chat$') '并进去时桌面端原有的条目还在（不整份覆盖）'
Assert-True ($mergedOnceLf -match '(?m)^- id: llm-pi-ai$') '并进去后渠道段在'
$mergedTwice = Merge-DshProfilePatch -Existing $mergedOnce -Block $block
Assert-Equal $mergedOnce $mergedTwice '重跑两次结果相同（标记段整段替换，不会越写越长）'
Assert-Equal 1 ([regex]::Matches(($mergedTwice -replace "`r", ''), [regex]::Escape($script:DshPatchMarkerBegin)).Count) '标记只出现一次'
$changed = New-TestConfig
$changed.Model = 'model-y'
$mergedAgain = (Merge-DshProfilePatch -Existing $mergedTwice -Block (Get-DshProfilePatchBlock -Config $changed)) -replace "`r", ''
Assert-True ($mergedAgain -match '(?m)^    model: model-y$') '换配置重跑会替换成新模型'
Assert-True ($mergedAgain -notmatch '(?m)^    model: model-x$') '旧模型的段被整段换掉（不留两份）'
Assert-True ($mergedAgain -match '(?m)^- id: ui-chat$') '换配置也不动桌面端自己的条目'
Assert-Equal ($block -replace "`r", '') ((Merge-DshProfilePatch -Existing '' -Block $block) -replace "`r", '') '文件不存在（空内容）时就是这一段'

# --- 踩坑 21：桌面端刚建好的 profile，cordis.patch.yml 的正文就是一行 [] ---
# 真机（同事机，桌面端 0.2.2-rc.1）：装完后 dsh 桌面端起不来，窗口不出现，日志是
#   DesktopHostFatalError: dsh: failed to parse overlay …cordis.patch.yml:
#   YAMLException: end of the stream or a document separator is expected (9:1)
# 原因：`[]` 那一行已经把文档结束掉了，后面追加/替换出的 `- id:` 成了**第二个根节点**。
$freshDesktop = (@(
    '# Your patch layer for this dsh profile, applied after every bundle layer:',
    '# overrides, disables, and insert lists; `!!js` expressions allowed.',
    '[]'
) -join "`r`n")
Assert-Equal 'empty-flow' (Get-DshPatchRootKind -Existing $freshDesktop) '桌面端刚建好的 profile 根是 []'
$freshMerged = (Merge-DshProfilePatch -Existing $freshDesktop -Block $block) -replace "`r", ''
Assert-Equal -1 $freshMerged.IndexOf('[]') '合并结果里不留那行 []（留着就是两个根节点）'
Assert-True ($freshMerged -match '(?m)^- id: llm-pi-ai$') '根是 [] 时也能并进渠道段'
Assert-True ($freshMerged -match [regex]::Escape('# Your patch layer for this dsh profile')) '桌面端原有的注释头还在'
Assert-Equal 'sequence' (Get-DshPatchRootKind -Existing $freshMerged) '合并后根是块序列（YAML 能解析）'

# 旧版本已经写坏的机器（[] + 我们的标记段）重跑要能自愈，不能越修越坏
$broken = $freshDesktop + "`r`n`r`n" + $block
$healed = (Merge-DshProfilePatch -Existing $broken -Block $block) -replace "`r", ''
Assert-Equal 'sequence' (Get-DshPatchRootKind -Existing $healed) '标记段重跑时那行 [] 也被清掉（写坏的机器能自愈）'
Assert-Equal 1 ([regex]::Matches($healed, [regex]::Escape($script:DshPatchMarkerBegin)).Count) '自愈后标记仍只出现一次'

# 根形状判定（决定「能追加」还是「必须拒绝」）
Assert-Equal 'empty' (Get-DshPatchRootKind -Existing "# 只有注释`r`n") '只有注释算空文档'
Assert-Equal 'empty-flow' (Get-DshPatchRootKind -Existing "`r`n[]`r`n") '空白 + [] 算空数组根'
Assert-Equal 'sequence' (Get-DshPatchRootKind -Existing $desktopOwn) '桌面端写过条目后是块序列'
Assert-Equal 'other' (Get-DshPatchRootKind -Existing 'foo: bar') '根是映射时判 other'
$threwMerge = $false
try { Merge-DshProfilePatch -Existing 'foo: bar' -Block $block | Out-Null } catch { $threwMerge = $true }
Assert-True $threwMerge '根不是数组时抛错：宁可不写，也不写出一份解析不了的文件'

# --- 续跑语义：state.json → Hashtable，done 的步骤要被识别 ---
$script:StateDir = Join-Path ([IO.Path]::GetTempPath()) ('feishu-kit-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $script:StateDir -Force | Out-Null
$script:StatePath = Join-Path $script:StateDir 'state.json'
try {
    Assert-Equal 0 ((Read-State).stages.Count) '没有 state.json 时返回空表（不炸）'

    '{"stages":{"P":"done","S":"reboot_pending"}}' | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
    $state = Read-State
    Assert-True ($state.stages -is [hashtable]) 'state.stages 是 Hashtable（不是 PSCustomObject）'
    Assert-Equal 'done' $state.stages['P'] '读到 P=done'
    Assert-Equal 'reboot_pending' $state.stages['S'] '读到 S=reboot_pending'
    Assert-True (-not $state.stages.ContainsKey('D')) '没跑过的步骤不伪造状态'

    Save-State -Stages $state.stages -Extra '测试'
    $roundTrip = (Get-Content -LiteralPath $script:StatePath -Raw -Encoding UTF8 | ConvertFrom-Json)
    Assert-Equal 'done' $roundTrip.stages.P 'Save-State 之后还能读回 P=done'

    '{ this is not json' | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
    Assert-Equal 0 ((Read-State).stages.Count) 'state.json 坏了也不炸（当成第一次装）'
}
finally { Remove-Item -LiteralPath $script:StateDir -Recurse -Force -ErrorAction SilentlyContinue }

# --- 配置快照：续跑时 -ConfigPath 指的就是这份快照本身 ---
# 真机踩过：需要重启的机器重启后由 RunOnce 续跑，传进来的 config 就是
# C:\ProgramData\feishu-kit\install-config.json，而 Save-ConfigSnapshot 又把它复制到它自己，
# Copy-Item 抛「无法使用项 … 其自身覆盖该项」，安装在一进 Invoke-Main 时就中止了。
$script:StateDir = Join-Path ([IO.Path]::GetTempPath()) ('feishu-kit-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $script:StateDir -Force | Out-Null
try {
    $snapshot = Join-Path $script:StateDir 'install-config.json'
    (New-TestConfig | ConvertTo-Json) | Set-Content -LiteralPath $snapshot -Encoding UTF8
    $threw = $false
    $returned = ''
    try { $returned = Save-ConfigSnapshot -Config $good -SourcePath $snapshot } catch { $threw = $true; $message = $_.Exception.Message }
    Assert-True (-not $threw) '源和目标同一个文件时不报错（续跑第一关）'
    Assert-Equal $snapshot $returned '同一个文件时返回的就是它自己'
    Assert-True (Test-Path -LiteralPath $snapshot) '同一个文件时不会被删/被搬走'

    # 正常首次安装：从 {tmp}\feishu-kit.json 复制过来
    $source = Join-Path ([IO.Path]::GetTempPath()) ('feishu-kit-src-' + [guid]::NewGuid().ToString('N') + '.json')
    (New-TestConfig | ConvertTo-Json) | Set-Content -LiteralPath $source -Encoding UTF8
    try {
        $copied = Save-ConfigSnapshot -Config $good -SourcePath $source
        Assert-Equal $snapshot $copied '首次安装复制到固定位置'
        Assert-True ((Get-Content -LiteralPath $snapshot -Raw) -match 'model-x') '快照内容来自配置'
    }
    finally { Remove-Item -LiteralPath $source -Force -ErrorAction SilentlyContinue }

    Assert-True (Test-SamePath 'C:\ProgramData\feishu-kit\install-config.json' 'c:\programdata\FEISHU-KIT\install-config.json') '同一路径大小写不同也算同一个文件'
    Assert-True (-not (Test-SamePath 'C:\a\x.json' 'C:\a\y.json')) '不同路径不算同一个文件'
}
finally { Remove-Item -LiteralPath $script:StateDir -Recurse -Force -ErrorAction SilentlyContinue }

if ($script:failures -gt 0) { Write-Host "$($script:failures) test(s) failed" -ForegroundColor Red; exit 1 }
Write-Host 'ALL INSTALLER-APPLY TESTS PASSED'
exit 0
