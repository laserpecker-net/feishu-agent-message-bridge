"""Release identity: sha256 over the sorted runtime files (relative path + bytes).

The id is the first 16 hex characters of that digest. This module ships **inside** every
release (``releases/<id>/bridge/release.py``), so the installer, the running service, the
CLI (rollback) and the tests all compute the same id from the same code instead of each
carrying its own copy.

"Runtime files" = ``bridge/*.py`` **plus** the root consumer modules in ``CONSUMER_FILES``
(exactly what ``install.py`` always copies into the release). Covering them is not cosmetic:
the reply text is produced by ``agent_prompt.py`` / ``dsh-consumer.py``, so a bridge-only
digest left the release id — and therefore ``installed == running`` — unchanged while the
code that writes replies had changed. That is how a hardcoded identity survived several
upgrades unnoticed ([docs/HISTORY.md](../docs/HISTORY.md) §4).

The set must be one that exists in **both** the source tree and ``releases/<id>/``, because
``feishu-bridge rollback`` recomputes the id from the release dir and refuses a mismatch;
that is why the optional agentkit layer (``agentkit-consumer*.py`` + ``agentkit/``) is not
part of the digest — its own staleness check lives in ``install.py``/``consumerctl``.
"""
import hashlib
from pathlib import Path


#: 发布副本里除 `bridge/` 包之外必须一起跑起来的根级模块（`install.py` 据此复制）。
#: 与指纹共用这一份清单：清单和"实际会跑的代码"必须是同一个集合。
CONSUMER_FILES = ('dsh-consumer.py', 'dsh-worker.py', 'codex-consumer.py', 'consumer_runtime.py',
                  'consumer_safety.py', 'agent_prompt.py')


def bridge_files(source):
    """Sorted ``bridge/*.py`` for a repo root, a release dir, or any dir with a bridge/."""
    bridge = Path(source) / 'bridge'
    return sorted(bridge.glob('*.py')) if bridge.is_dir() else []


def runtime_files(source):
    """Sorted runtime files (``bridge/*.py`` + existing ``CONSUMER_FILES``) for a root/release."""
    root = Path(source)
    paths = [path for path in bridge_files(root) if path.is_file()]
    paths += [root / name for name in CONSUMER_FILES if (root / name).is_file()]
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _label(path, root):
    """指纹/receipt 里的键：给了 root 用相对路径（bridge/x.py 与根级 x.py 不会撞名）。"""
    path = Path(path)
    return path.relative_to(root).as_posix() if root is not None else path.name


def file_hashes(paths, root=None):
    return {_label(path, root): sha256(path) for path in paths}


def digest(paths, root=None):
    value = hashlib.sha256()
    for path in paths:
        path = Path(path)
        value.update(_label(path, root).encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def release_digest(source):
    return digest(runtime_files(source), root=Path(source))


def release_id(source):
    return release_digest(source)[:16]
