"""B 阶段（仓库快照 → WSL2 家目录）必须是**镜像**，不是合并。

2026-11-03 手工清理过一次「上游已删除却一直被打进 release」的残留；2026-11-05 同事那台机器上
`bridge/agent.py` 还在——因为 B 阶段是 `tar | tar -xf`（只加不删）。死文件进入 release 的代价：
`bridge/release.py` 的 release id 是 `bridge/*.py` 的哈希，多一个旧文件，同一份代码在 Windows 与
macOS 就报两个 id。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'dist' / 'wsl2' / 'wsl' / 'mirror-repo.py'


def _make_source(tmp_path):
    src = tmp_path / 'src'
    (src / 'dist' / 'wsl2').mkdir(parents=True)
    (src / 'dist' / 'wsl2' / 'setup.sh').write_text('#!/bin/sh\n')
    (src / 'bridge').mkdir()
    (src / 'bridge' / 'cli.py').write_text('def _system_scope_installed(): pass\n')
    (src / 'bridge' / 'new.py').write_text('new\n')
    (src / 'README.md').write_text('v2\n')
    return src


def _run(src, dst):
    return subprocess.run([sys.executable, str(SCRIPT), str(src), str(dst)],
                          capture_output=True, text=True)


def test_mirror_removes_files_the_source_no_longer_has(tmp_path):
    src = _make_source(tmp_path)
    dst = tmp_path / 'dst'
    (dst / 'bridge' / 'stale-empty').mkdir(parents=True)
    (dst / 'bridge' / 'agent.py').write_text('旧内置 agent（上游已删除）\n')
    (dst / 'bridge' / 'cli.py').write_text('old\n')
    (dst / 'cursor-consumer.py').write_text('旧引擎入口（上游已删除）\n')
    (dst / '.git').mkdir()
    (dst / '.git' / 'config').write_text('[core]\n')

    result = _run(src, dst)

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'COPY_OK' in result.stdout
    assert not (dst / 'bridge' / 'agent.py').exists()
    assert not (dst / 'cursor-consumer.py').exists()
    assert (dst / 'bridge' / 'new.py').exists()
    assert (dst / 'bridge' / 'cli.py').read_text() == (src / 'bridge' / 'cli.py').read_text()
    # git clone 装的那台机器：.git 不能动，否则升级等于毁掉用户历史
    assert (dst / '.git' / 'config').exists()
    # 空目录也不留壳：验收判据是"目录树与发行包逐一对齐"
    assert not (dst / 'bridge' / 'stale-empty').exists()


def test_mirror_refuses_an_incomplete_source_before_touching_the_target(tmp_path):
    src = _make_source(tmp_path)
    (src / 'bridge' / 'cli.py').unlink()
    dst = tmp_path / 'dst'
    dst.mkdir()
    (dst / 'keep.txt').write_text('keep\n')

    result = _run(src, dst)

    assert result.returncode == 9
    assert 'COPY_ERROR=source_unreadable' in result.stdout
    assert (dst / 'keep.txt').exists()
    assert sorted(path.name for path in dst.iterdir()) == ['keep.txt']


def test_b_phase_mirrors_instead_of_extracting_over_the_old_tree():
    """静态护栏：安装器 B 阶段必须调 mirror-repo.py（而不是解压覆盖目标树）。"""
    installer = (ROOT / 'dist' / 'wsl2' / 'install-windows.ps1').read_text()
    phase = installer[installer.index('function Install-PhaseCode'):
                      installer.index('function Install-PhaseBridge')]
    # 仍然从 /mnt 解到 ext4（服务不能跑在 /mnt），但落地方式必须是镜像
    assert 'tar -cf -' in phase
    assert 'mirror-repo.py' in phase
    assert 'COPY_OK' in phase


def test_package_ships_the_mirror_script():
    packer = (ROOT / 'dist' / 'wsl2' / 'pack-windows.sh').read_text()
    assert 'repo/dist/wsl2/wsl/mirror-repo.py' in packer
