"""Windows 发行包的打包/解包契约。

这三条断言来自 2026-11-04 真机事故：`bridge-remote.sh upgrade` 在同事机上连跑三次都
"成功"返回，机器上什么都没变。三个原因叠在一起：

1. `zip -qr`（Info-ZIP）不给非 ASCII 文件名置 UTF-8 标志位 → Windows 解压端报
   `Invalid empty pathname`（文件名全是中文的 `.cmd` 就在包里）；
2. 第一次改用 `Expand-Archive` → 模块内部清理已展开项时抛"找不到路径"的噪声，把真正的
   失败淹掉；
3. 生成的临时 `.ps1` 没有 UTF-8 BOM → PowerShell 5.1 按 ANSI 解码中文注释，
   跨行字节错位把下一行代码并进注释（`$tar = ...` 整行被吞），脚本却只报参数为 null。
"""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAKE_ZIP = ROOT / 'dist' / 'wsl2' / 'make-zip.py'
PACK_WINDOWS = (ROOT / 'dist' / 'wsl2' / 'pack-windows.sh').read_text()
PACK_OFFLINE = (ROOT / 'dist' / 'wsl2' / 'pack-offline.sh').read_text()
REMOTE = (ROOT / 'tools' / 'bridge-remote.sh').read_text()


def test_make_zip_marks_non_ascii_names_as_utf8(tmp_path):
    stage = tmp_path / 'stage'
    top = stage / 'feishu-bridge-windows-20261104'
    (top / 'repo' / 'dist').mkdir(parents=True)
    name = '02-验收（不需要管理员运行）.cmd'
    (top / 'repo' / 'dist' / name).write_bytes(b'@echo off\r\nexit /b 0\r\n')
    out = tmp_path / 'pkg.zip'
    subprocess.run([sys.executable, str(MAKE_ZIP), '--stage', str(stage),
                    '--name', top.name, '--out', str(out)], check=True, capture_output=True)
    archive = zipfile.ZipFile(out)
    entry = archive.getinfo('%s/repo/dist/%s' % (top.name, name))
    assert entry.flag_bits & 0x800, '非 ASCII 文件名没有 UTF-8 标志位：Windows 解压会乱码/失败'
    assert entry.compress_type == zipfile.ZIP_DEFLATED, '压缩方式退回 STORED 会让包大一倍'
    assert archive.read(entry) == b'@echo off\r\nexit /b 0\r\n'


def test_make_zip_refuses_a_missing_stage_directory(tmp_path):
    result = subprocess.run([sys.executable, str(MAKE_ZIP), '--stage', str(tmp_path),
                             '--name', 'nope', '--out', str(tmp_path / 'x.zip')],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert '没有目录' in result.stderr


def test_both_packers_use_the_shared_utf8_zip_helper():
    # 打包只能有一份实现：两处各写一遍，迟早一处置位一处不置位。
    assert 'dist/wsl2/make-zip.py' in PACK_WINDOWS
    assert 'dist/wsl2/make-zip.py' in PACK_OFFLINE
    # 真正要禁的是"调用 Info-ZIP 打包"，不是提到它的注释。
    assert '( cd "$STAGE" && zip -qr' not in PACK_WINDOWS
    assert '&& zip -qr "$ZIPNAME"' not in PACK_OFFLINE


def test_remote_upgrade_extracts_with_bsdtar_and_rejects_partial_packages():
    assert 'System32\\tar.exe' in REMOTE        # 用 Windows 自带的 bsdtar
    assert 'Expand-Archive -Force' not in REMOTE  # 它的 -Force 是合并覆盖，噪声会淹掉真失败
    assert 'ZipFile]::ExtractToDirectory' not in REMOTE  # PS 5.1 不一定加载得到这个类型
    assert 'INSTALLER=missing' in REMOTE          # 残包直接判失败（exit 4）
    assert 'TAR=failed' in REMOTE


def test_generated_powershell_helper_is_written_with_a_utf8_bom():
    # 没有 BOM：中文注释会被按 ANSI 解码，字节错位把下一行代码并进注释。
    marker = "printf '\\xef\\xbb\\xbf' > \"$tmp_ps\""
    assert marker in REMOTE
    assert REMOTE.index(marker) < REMOTE.index('cat >>"$tmp_ps" <<PS')


def test_non_ascii_powershell_files_keep_their_utf8_bom():
    """含非 ASCII 的 .ps1 必须带 UTF-8 BOM，否则 Windows PowerShell 5.1 按 ANSI 解码 → ParserError。

    2026-11-05 真机：编辑器重写 `install-windows.ps1` 时掉了 BOM，升级在**解析阶段**就失败
    （`[string[]]$ArgumentList = @(),` 报"缺少表达式"），机器上什么都没装，包看起来是好的。
    同一条断言在 `WindowsDistribution.Tests.ps1` 里有，但它只在 Windows 上跑；这条让 macOS 也拦住。
    """
    tracked = subprocess.run(['git', 'ls-files', '*.ps1'], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
    offenders = []
    for name in tracked:
        path = ROOT / name
        data = path.read_bytes()
        if data[:3] == b'\xef\xbb\xbf':
            continue
        if any(byte > 0x7f for byte in data):
            offenders.append(name)
    assert not offenders, '含非 ASCII 的 .ps1 必须带 UTF-8 BOM：%s' % ', '.join(offenders)


def test_packer_refuses_non_ascii_ps1_without_bom():
    assert 'UTF-8 BOM' in PACK_WINDOWS
    assert 'rglob' in PACK_WINDOWS


def test_pack_refuses_a_verifier_that_hardcodes_the_worker_list():
    """worker 名单的唯一事实是 `bridge/service.py:WORKER_NAMES`。

    旧包 `verify.sh` 断言 4 个 worker（含已删除的 analyst），新代码只有 3 个 ——
    用旧包的文件验新代码必假红。打包口子必须拦住"又在 verifier 里抄一份名单"的包，
    否则这个缺陷会跟着每个 release 重新长出来。
    """
    assert 'WORKER_NAMES' in PACK_WINDOWS
    assert "('collector', 'details', 'sender')" in PACK_WINDOWS   # 只允许作为"写死名单"的禁止模式出现
    verifier = (ROOT / 'dist' / 'wsl2' / 'wsl' / 'verify.sh').read_text()
    assert "('collector', 'details', 'sender')" not in verifier


def test_check_mode_answers_sending_enabled_the_same_way_verify_does():
    """同一事实两条通道只能一个结论：verify.sh 判 bad，`-Mode Check` 就不能只 Write-Warn。

    2026-11-05 清单 §10-②：新装默认 `sending_enabled=false`，F 阶段会红（rc=3），
    而 `-Mode Check` 只警告 —— 同一台机器两条通道两个结论。
    """
    installer = (ROOT / 'dist' / 'wsl2' / 'install-windows.ps1').read_text(encoding='utf-8-sig')
    assert '已安装但没 resume' in installer
    assert '已安装但处于暂停' not in installer
    # 该分支必须计为 problem（Write-Err 后面紧跟 $problems++）
    start = installer.index('已安装但没 resume')
    tail = installer[start:start + 400]
    assert '$problems++' in tail
