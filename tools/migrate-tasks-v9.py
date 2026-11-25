#!/usr/bin/env python3
"""把状态库的 tasks 从 v8 迁到 v9（加 8 列：origin_mid/session_id/executor/hints/
approval_mid/approval_kind/approved_at/started_at）。

用法::

    python3 tools/migrate-tasks-v9.py --db <库文件> --dry-run
    python3 tools/migrate-tasks-v9.py --db <库文件>
    python3 tools/migrate-tasks-v9.py --db <库文件> --verify
    python3 tools/migrate-tasks-v9.py --db <库文件> --inject-fault   # 测试回滚用

安全边界：``--dry-run`` **不备份、不写库**；实迁先 ``VACUUM INTO`` 备份并把备份 chmod 0600，
再在一个 ``BEGIN IMMEDIATE`` 里逐条 ``ALTER``。

三个和 S1 一样的要点：

1. **禁用 executescript**：它执行前会自动 COMMIT，中途失败会留下半个库。这里逐条 ``execute``。
2. 备份用 ``VACUUM INTO``：WAL 模式下 ``cp`` 拿到的不是一致快照。
3. 只处理 v8：已经是 v9 直接拒绝（重复 ALTER 会 duplicate column name，半迁移的库更危险）。

迁移只动 ``tasks``，不碰 ``consumers``（角色路由属 S3a）。库迁完要在新代码下启动；
建议先 ``feishu-bridge stop``，与 S1 的停机窗口同一个道理。
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.store import TASKS_V9_COLUMNS, migrate_tasks_to_v9  # noqa: E402

#: 这次迁移的目标版本。**不引用 Store.SCHEMA_VERSION**：那个数会随后续迁移一起涨，
#: 让"历史工具在最新代码下报错"——工具只对某一个版本负责。
TARGET_VERSION = 9

V8_TASK_COLUMNS = ('id', 'chat_id', 'title', 'state', 'steps', 'artifacts',
                   'open_question', 'attempts', 'created_at', 'updated_at')
#: v8 的 10 列 + v9 新加的 8 列 = 18 列（迁移后的唯一期望值）。
V9_TASK_COLUMN_COUNT = len(V8_TASK_COLUMNS) + len(TASKS_V9_COLUMNS)


class MigrationError(RuntimeError):
    pass


def _count(db, sql, params=()):
    return db.execute(sql, params).fetchone()[0]


def _columns(db):
    return [row[1] for row in db.execute('PRAGMA table_info(tasks)')]


def preflight(db):
    """迁移前必须成立的前提，任何一条不满足就中止并说明下一步。"""
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version == 9:
        raise MigrationError('库已经是 v9，不需要迁移')
    if version != 8:
        raise MigrationError(
            '库是 v%s，这个脚本只处理 v8（v7 先跑 tools/migrate-outbox-v8.py）' % version)
    if _count(db, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tasks'") != 1:
        raise MigrationError('找不到 tasks 表')
    columns = _columns(db)
    if [name for name in V8_TASK_COLUMNS if name not in columns]:
        raise MigrationError('tasks 不像 v8 的形状（缺 %s），先跑 feishu-bridge doctor'
                             % ','.join(name for name in V8_TASK_COLUMNS if name not in columns))
    already = [name for name, _ in TASKS_V9_COLUMNS if name in columns]
    if already:
        raise MigrationError(
            'tasks 已经有 v9 的列（%s）但版本号还是 v8：库处于半迁移状态，'
            '从备份恢复后重跑，不要在这里猜' % ','.join(already))
    return {'rows': _count(db, 'SELECT COUNT(*) FROM tasks'), 'columns': columns}


def backup(db, db_path):
    stamp = time.strftime('%Y%m%d%H%M%S')
    target = '%s.v8-%s' % (db_path, stamp)
    suffix = 1
    while os.path.exists(target):
        target = '%s.v8-%s-%d' % (db_path, stamp, suffix)
        suffix += 1
    db.execute('VACUUM INTO ?', (target,))
    # 任务标题里可能有人名/群名，权限跟生产库一致（0600）。VACUUM INTO 按 umask 建文件（常见 0644）。
    os.chmod(target, 0o600)
    return target


def migrate(db, inject_fault=False):
    """返回 (before, after) 两份快照；实迁在 ``BEGIN IMMEDIATE`` 里完成，失败整体回滚。"""
    before = preflight(db)
    db.execute('BEGIN IMMEDIATE')
    try:
        after = migrate_tasks_to_v9(db)
        if len(after['columns']) != V9_TASK_COLUMN_COUNT:
            raise MigrationError('迁移后 tasks 有 %s 列（应为 %s）：%s'
                                 % (len(after['columns']), V9_TASK_COLUMN_COUNT,
                                    ','.join(after['columns'])))
        if inject_fault:
            raise MigrationError('--inject-fault：故意在提交前失败，用来验证回滚')
        rows_after = _count(db, 'SELECT COUNT(*) FROM tasks')
        if rows_after != before['rows']:
            raise MigrationError('迁移前后 tasks 行数不一致：%s → %s' % (before['rows'], rows_after))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return before, {'rows': rows_after, 'added': after['added'], 'columns': after['columns']}


def verify(db_path):
    db = sqlite3.connect(db_path)
    try:
        columns = _columns(db)
        return {
            'user_version': db.execute('PRAGMA user_version').fetchone()[0],
            'rows': _count(db, 'SELECT COUNT(*) FROM tasks'),
            'columns': columns,
            'column_count': len(columns),
            'has_all_v9_columns': all(name in columns for name, _ in TASKS_V9_COLUMNS),
            'foreign_key_check': db.execute('PRAGMA foreign_key_check').fetchall(),
            'integrity_check': db.execute('PRAGMA integrity_check').fetchone()[0],
        }
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='tasks v8 → v9 迁移（一次性，可回滚）')
    parser.add_argument('--db', required=True, help='状态库路径（state.sqlite3）')
    parser.add_argument('--dry-run', action='store_true', help='只跑前置检查与断言，不写库')
    parser.add_argument('--verify', action='store_true', help='只做迁移后自检')
    parser.add_argument('--inject-fault', action='store_true',
                        help='故意在提交前抛错，用来验证回滚（测试用）')
    args = parser.parse_args(argv)

    db_path = str(Path(args.db).expanduser())
    if not os.path.exists(db_path):
        print('找不到库：%s' % db_path, file=sys.stderr)
        return 2
    if args.verify:
        print(json.dumps(verify(db_path), ensure_ascii=False, indent=2))
        return 0

    db = sqlite3.connect(db_path, isolation_level=None)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        if args.dry_run:
            before = preflight(db)
            print('dry-run 通过：%s' % (before,))
            return 0
        before = preflight(db)
        db.close()
        db = sqlite3.connect(db_path, isolation_level=None)
        db.execute('PRAGMA foreign_keys=ON')
        target = backup(db, db_path)
        print('备份：%s' % target)
        before2, after = migrate(db, inject_fault=args.inject_fault)
        print('迁移完成：%s -> %s' % (before2, after))
    except MigrationError as error:
        print('迁移中止：%s' % error, file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()

    checks = verify(db_path)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    problems = []
    if checks['user_version'] != TARGET_VERSION:
        problems.append('user_version=%s（应为 %s）' % (checks['user_version'], TARGET_VERSION))
    if not checks['has_all_v9_columns']:
        problems.append('tasks 缺 v9 的列')
    if checks['column_count'] != V9_TASK_COLUMN_COUNT:
        problems.append('tasks 有 %s 列（应为 %s）' % (checks['column_count'], V9_TASK_COLUMN_COUNT))
    if checks['foreign_key_check']:
        problems.append('foreign_key_check 非空')
    if checks['integrity_check'] != 'ok':
        problems.append('integrity_check=%s' % checks['integrity_check'])
    if problems:
        print('自检未通过：' + '；'.join(problems), file=sys.stderr)
        return 1
    print('自检通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
