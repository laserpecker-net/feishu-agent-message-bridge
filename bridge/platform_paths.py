"""WSL ↔ Windows 路径换算：**只做换算与校验，不猜**。

为什么单独一个模块：Windows 上的 bridge 跑在 WSL2 里（POSIX），而真正干活的工作台是
Windows 侧的桌面版 CLI（`cmd.exe /c …\\dsh.cmd`）。同一个文件在两边有两个写法，
所有跨边界的参数（CLI 路径、overlay 路径、`DSH_HOME`、cwd）都必须显式换算。

**报错不猜**：UNC（`\\\\server\\share`）、`\\\\wsl$…`、`\\\\?\\…`、`\\\\.\\…`、空串、含 NUL、
未挂载的盘符，一律抛 :class:`PathMappingError` 并说明原因。拼错的路径如果被"猜"过去，
表现会是"文件找不到"，而真正的原因是路径从来就不成立——那类错最难查。
"""
import os


class PathMappingError(ValueError):
    """路径没法换算（或不该换算）。``reason`` 是给人看的一句话。"""

    def __init__(self, reason, path=None):
        self.reason = reason
        self.path = path
        super().__init__(reason if path is None else '%s：%r' % (reason, path))


#: WSL 里 Windows 盘符的挂载根。
WSL_MOUNT_ROOT = '/mnt'

UNC_PREFIX = '\\\\'
#: 明确不收的 Windows 路径前缀 → 各自的具体原因（顺序有意义：先说具体的，再兜底）。
SPECIAL_PREFIXES = (
    ('\\\\wsl$', '不能换算 \\\\wsl$ 共享路径'),
    ('\\\\wsl.localhost', '不能换算 \\\\wsl.localhost 共享路径'),
    ('\\\\?\\', '不能换算 \\\\?\\ 设备路径'),
    ('\\\\.\\', '不能换算 \\\\.\\ 设备路径'),
)


def _require_text(path):
    if not isinstance(path, str):
        raise PathMappingError('路径必须是字符串', path)
    if not path:
        raise PathMappingError('路径是空的', path)
    if '\x00' in path:
        raise PathMappingError('路径里含 NUL 字节', path)
    return path


def _is_windows_form(path):
    return len(path) >= 2 and path[1] == ':' and path[0].isalpha()


def _is_wsl_mount(path):
    return path == WSL_MOUNT_ROOT or path.startswith(WSL_MOUNT_ROOT + '/')


def is_windows_visible(path):
    """这个 POSIX 路径在 Windows 侧能不能看见。

    只有 ``/mnt/<盘符>/…`` 是 Windows 盘；WSL 自己的 ext4（``~``、``/tmp``）在 Windows
    看来是 ``\\wsl.localhost\…`` 的 UNC 路径，**``cmd.exe`` 不接受 UNC 作当前目录**——
    2026-11-20 真机就栽在这里（``dsh --version`` 退出码 1）。
    """
    text = str(path).rstrip('/') or '/'
    return _is_wsl_mount(text) or text.startswith(WSL_MOUNT_ROOT + '/')


def _reject_specials(path):
    lowered = path.lower()
    for prefix, reason in SPECIAL_PREFIXES:
        if lowered.startswith(prefix):
            raise PathMappingError(reason, path)


def _default_drive_checker(letter, mount_root=WSL_MOUNT_ROOT):
    return os.path.isdir(os.path.join(mount_root, letter.lower()))


def _check_drive(letter, check_drive, drive_checker):
    if not check_drive:
        return
    checker = drive_checker or _default_drive_checker
    if not checker(letter):
        raise PathMappingError('这个盘符没有挂载：%s' % letter.upper(), letter)


def _split(parts):
    return [segment for segment in parts if segment]


def wsl_to_win(path, *, check_drive=False, drive_checker=None):
    """`/mnt/c/a b/c` → `C:\\a b\\c`；已是 Windows 形态则原样（规范化）返回。"""
    path = _require_text(path)
    if _is_windows_form(path):
        return _normalize_win(path)
    if path.startswith(UNC_PREFIX):
        raise PathMappingError('不能换算 UNC 路径', path)
    if not _is_wsl_mount(path):
        raise PathMappingError('不是 WSL 挂载路径（应以 /mnt/<盘符>/ 开头）', path)
    segments = _split(path.split('/'))
    # ['mnt', 'c', 'a', ...]
    if len(segments) < 2 or segments[0] != 'mnt':
        raise PathMappingError('WSL 挂载路径缺少盘符', path)
    letter = segments[1]
    if len(letter) != 1 or not letter.isalpha():
        raise PathMappingError('盘符不合法（应为单个字母）', path)
    _check_drive(letter, check_drive, drive_checker)
    rest = segments[2:]
    drive = letter.upper() + ':'
    return drive + ('\\' if not rest else '\\' + '\\'.join(rest))


def win_to_wsl(path, *, check_drive=False, drive_checker=None):
    """`C:\\a b\\c` → `/mnt/c/a b/c`；已是 WSL 形态则原样（规范化）返回。"""
    path = _require_text(path)
    if path.startswith(UNC_PREFIX):
        _reject_specials(path)   # \\wsl$ / \\?\ / \\.\ 给具体原因，其余兜底
        raise PathMappingError('不能换算 UNC 路径（共享路径没有对应的 WSL 位置）', path)
    if _is_wsl_mount(path):
        return _normalize_wsl(path)
    if not _is_windows_form(path):
        raise PathMappingError('不是 Windows 盘符路径（应形如 C:\\\\…）', path)
    if len(path) > 2 and path[2] not in ('\\', '/'):
        raise PathMappingError('盘符后面必须是路径分隔符', path)
    letter = path[0].lower()
    _check_drive(letter, check_drive, drive_checker)
    rest = _split((path[3:] if len(path) > 2 else '').replace('\\', '/').split('/'))
    base = '%s/%s' % (WSL_MOUNT_ROOT, letter)
    return base if not rest else base + '/' + '/'.join(rest)


def _normalize_win(path):
    """统一成 `C:\\a\\b`：盘符大写、分隔符统一、去掉尾部多余分隔。"""
    rest = _split(path[2:].replace('/', '\\').split('\\'))
    base = path[0].upper() + ':'
    return base if not rest else base + '\\' + '\\'.join(rest)


def _normalize_wsl(path):
    segments = _split(path.split('/'))
    if not segments or segments[0] != 'mnt' or len(segments) < 2:
        raise PathMappingError('不是 WSL 挂载路径（应以 /mnt/<盘符>/ 开头）', path)
    return '/' + '/'.join(segments)


def normalize_for(platform, path, **kwargs):
    """按目标平台取那个形态：``windows`` → `C:\\…`；其余（wsl/linux/posix/mac）→ `/…`。"""
    if platform is None:
        raise PathMappingError('没有给出目标平台', path)
    wanted = str(platform).strip().lower()
    if wanted in ('windows', 'win', 'dsh-desktop', 'cmd-interop'):
        return wsl_to_win(path, **kwargs)
    if wanted in ('wsl', 'linux', 'posix', 'macos', 'mac', 'direct'):
        if _is_windows_form(path):
            return win_to_wsl(path, **kwargs)
        if _is_wsl_mount(path):
            return _normalize_wsl(_require_text(path))
        return path
    raise PathMappingError('不知道这个平台：%r' % (platform,), path)


def looks_like_windows_path(path):
    """给调用方判断"这个参数要不要换算"用（例如 `--patch` 的可选参数）。"""
    if not isinstance(path, str) or not path:
        return False
    return _is_windows_form(path) or path.startswith(UNC_PREFIX)


def looks_like_wsl_path(path):
    if not isinstance(path, str) or not path:
        return False
    return _is_wsl_mount(path)
