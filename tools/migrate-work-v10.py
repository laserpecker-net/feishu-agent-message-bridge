#!/usr/bin/env python3
"""把状态库从 v9 迁到 v10（`consumers` 加一列 `role`，S3a 的角色路由）。

用法::

    python3 tools/migrate-work-v10.py --db <库文件> --dry-run
    python3 tools/migrate-work-v10.py --db <库文件>
    python3 tools/migrate-work-v10.py --db <库文件> --verify
    python3 tools/migrate-work-v10.py --db <库文件> --inject-fault   # 测试回滚用

安全边界：``--dry-run`` **不备份、不写库**；实迁先 ``VACUUM INTO`` 备份并把备份 chmod 0600，
再在一个 ``BEGIN IMMEDIATE`` 里 ``ALTER``。

三个和前两次迁移一样的要点：

1. **禁用 executescript**：它执行前会自动 COMMIT，中途失败会留下半个库。这里逐条 ``execute``。
2. 备份用 ``VACUUM INTO``：WAL 模式下 ``cp`` 拿到的不是一致快照。
3. 只处理 v9：已经是 v10 直接拒绝（重复 ALTER 会 duplicate column name，半迁移的库更危险）。

迁移只动 ``consumers``，不碰 ``events``/``tasks``/``outbox``。库迁完要在新代码下启动；
建议先 ``feishu-bridge stop``，与 S1/S2 的停机窗口同一个道理。
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.store import CONSUMERS_V10_COLUMNS, Store, migrate_consumers_to_v10  # noqa: E402

#: 这次迁移的目标版本（**不引用 Store.SCHEMA_VERSION**：那会随后续迁移一起变，
#: 让"v9 工具在第 10 版代码下报错"这种自伤发生——S2 的工具就踩过这个坑）。
TARGET_VERSION = 10

V9_CONSUMER_COLUMNS = ('name', 'kind', 'created_at', 'last_seen_at',
                       'claims', 'acks', 'fails')
#: v9 的 7 列 + v10 新加的第 8 列 = 8 列（迁移后的唯一期望值）。
V10_CONSUMER_COLUMN_COUNT = len(V9_CONSUMER_COLUMNS) + len(CONSUMERS_V10_COLUMNS)


class MigrationError(RuntimeError):
    pass


def _count(db, sql, params=()):
    return db.execute(sql, params).fetchone()[0]


def _table_count(db, table):
    """表不存在时返回 None（极老的库/最小夹具没有这张表），让"行数不许变"只对存在的表生效。"""
    exists = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone()[0]
    return _count(db, 'SELECT COUNT(*) FROM %s' % table) if exists else None


def _columns(db):
    return [row[1] for row in db.execute('PRAGMA table_info(consumers)')]


def preflight(db):
    """迁移前必须成立的前提，任何一条不满足就中止并说明下一步。"""
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version == TARGET_VERSION:
        raise MigrationError('库已经是 v10，不需要迁移')
    if version != 9:
        raise MigrationError(
            '库是 v%s，这个脚本只处理 v9（v7 先跑 tools/migrate-outbox-v8.py，'
            'v8 再跑 tools/migrate-tasks-v9.py）' % version)
    if _count(db, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='consumers'") != 1:
        raise MigrationError('找不到 consumers 表')
    columns = _columns(db)
    if [name for name in V9_CONSUMER_COLUMNS if name not in columns]:
        raise MigrationError('consumers 不像 v9 的形状（缺 %s），先跑 feishu-bridge doctor'
                            % ','.join(name for name in V9_CONSUMER_COLUMNS if name not in columns))
    already = [name for name, _ in CONSUMERS_V10_COLUMNS if name in columns]
    if already:
        raise MigrationError(
            'consumers 已经有 v10 的列（%s）但版本号还是 v9：库处于半迁移状态，'
            '从备份恢复后重跑，不要在这里猜' % ','.join(already))
    return {'rows': _count(db, 'SELECT COUNT(*) FROM consumers'),
            'events': _table_count(db, 'events'),
            'tasks': _table_count(db, 'tasks'),
            'columns': columns}


def backup(db, db_path):
    stamp = time.strftime('%Y%m%d%H%M%S')
    target = '%s.v9-%s' % (db_path, stamp)
    suffix = 1
    while os.path.exists(target):
        target = '%s.v9-%s-%d' % (db_path, stamp, suffix)
        suffix += 1
    db.execute('VACUUM INTO ?', (target,))
    # 消费者名里可能有机器名/人名，权限跟生产库一致（0600）。VACUUM INTO 按 umask 建文件。
    os.chmod(target, 0o600)
    return target


def migrate(db, inject_fault=False):
    """返回 (before, after) 两份快照；实迁在 ``BEGIN IMMEDIATE`` 里完成，失败整体回滚。"""
    before = preflight(db)
    db.execute('BEGIN IMMEDIATE')
    try:
        after = migrate_consumers_to_v10(db)
        if len(after['columns']) != V10_CONSUMER_COLUMN_COUNT:
            raise MigrationError('迁移后 consumers 有 %s 列（应为 %s）：%s'
                                 % (len(after['columns']), V10_CONSUMER_COLUMN_COUNT,
                                    ','.join(after['columns'])))
        if inject_fault:
            raise MigrationError('--inject-fault：故意在提交前失败，用来验证回滚')
        if before['events'] is not None and _table_count(db, 'events') != before['events']:
            raise MigrationError('迁移前后 events 行数不一致（迁移不该碰事件）')
        if before['tasks'] is not None and _table_count(db, 'tasks') != before['tasks']:
            raise MigrationError('迁移前后 tasks 行数不一致（迁移不该碰任务）')
        rows_after = _count(db, 'SELECT COUNT(*) FROM consumers')
        if rows_after != before['rows']:
            raise MigrationError('迁移前后 consumers 行数不一致：%s → %s' % (before['rows'], rows_after))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return before, {'rows': rows_after, 'added': after['added'], 'columns': after['columns']}


def verify(db_path):
    db = sqlite3.connect(db_path)
    try:
        columns = _columns(db)
        roles = {row[0] for row in db.execute('SELECT DISTINCT role FROM consumers')}
        return {
            'user_version': db.execute('PRAGMA user_version').fetchone()[0],
            'rows': _count(db, 'SELECT COUNT(*) FROM consumers'),
            'columns': columns,
            'column_count': len(columns),
            'has_all_v10_columns': all(name in columns for name, _ in CONSUMERS_V10_COLUMNS),
            'roles': sorted(roles),
            'foreign_key_check': db.execute('PRAGMA foreign_key_check').fetchall(),
            'integrity_check': db.execute('PRAGMA integrity_check').fetchone()[0],
        }
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='consumers v9 → v10 迁移（一次性，可回滚）')
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
    if not checks['has_all_v10_columns']:
        problems.append('consumers 缺 v10 的列')
    if checks['column_count'] != V10_CONSUMER_COLUMN_COUNT:
        problems.append('consumers 有 %s 列（应为 %s）'
                        % (checks['column_count'], V10_CONSUMER_COLUMN_COUNT))
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
