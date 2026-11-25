#!/usr/bin/env python3
"""把状态库的 outbox 从 v7（一条消息一行）迁到 v8（一条消息 N 行，带 seq/kind）。

用法::

    python3 tools/migrate-outbox-v8.py --db ~/.feishu-bridge/state.db --dry-run
    python3 tools/migrate-outbox-v8.py --db ~/.feishu-bridge/state.db
    python3 tools/migrate-outbox-v8.py --db ~/.feishu-bridge/state.db --verify
    python3 tools/migrate-outbox-v8.py --db ~/.feishu-bridge/state.db --park-inflight

安全边界：``--dry-run`` **不备份、不写库**；实迁先 ``VACUUM INTO`` 备份并把备份 chmod 0600
（里面有消息正文），再在一个 ``BEGIN IMMEDIATE`` 里逐条执行。

设计依据：docs/design-s1-outbox-v8.md §7。三个关键点，都是实测踩过的：

1. ``ALTER TABLE ... RENAME`` 会把索引和触发器一起搬走，所以必须先按名字 DROP，
   否则第 6 步建 ``outbox_due`` 会报 ``already exists``。
2. **禁用 executescript**：它执行前会自动 COMMIT，中途失败会留下半个库。这里逐条
   ``execute``，整段包在一个 ``BEGIN IMMEDIATE`` 里。
3. 备份用 ``VACUUM INTO``：WAL 模式下 ``cp`` 拿到的不是一致快照（``-wal`` 里可能有
   还没 checkpoint 的提交）。
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.store import OUTBOX_V8_DDL, migrate_outbox_to_v8  # noqa: E402

IN_FLIGHT = ('send_started', 'verifying')
V7_STATES = ('queued', 'send_started', 'verifying', 'verify_pending', 'retry_wait',
             'needs_attention', 'outcome_unknown', 'reply_verified', 'dead', 'dropped')


class MigrationError(RuntimeError):
    pass


def _count(db, sql, params=()):
    return db.execute(sql, params).fetchone()[0]


def preflight(db, allow_in_flight=False):
    """迁移前必须成立的前提，任何一条不满足就中止并说明下一步。

    ``allow_in_flight`` 只给 ``--park-inflight`` 用：in-flight 行不在这里抛错，而是作为
    ``inflight`` 随快照返回，由 :func:`migrate` 在**同一个事务**里改成 ``needs_attention``。
    """
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version == 8:
        raise MigrationError('库已经是 v8，不需要迁移')
    if version != 7:
        raise MigrationError('库是 v%s，这个脚本只处理 v7（先跑 feishu-bridge doctor）' % version)
    if _count(db, 'SELECT COUNT(*) FROM sqlite_master WHERE type="table" AND name="outbox"') != 1:
        raise MigrationError('找不到 outbox 表')
    in_flight = [{'id': row[0], 'original_id': row[1]} for row in db.execute(
        'SELECT id,original_id FROM outbox WHERE state IN (%s)'
        % ','.join('?' * len(IN_FLIGHT)), IN_FLIGHT).fetchall()]
    if in_flight and not allow_in_flight:
        raise MigrationError(
            '还有 %d 条正在发送（send_started/verifying）：%s。\n'
            '两种处理方式，选一种：\n'
            '  1) 先在跑着旧服务的状态下等它自己收尾（租约过期后由 claim_outbox 回收），再重跑迁移；\n'
            '  2) 停服后确认这些行确实需要人工核对，加 --park-inflight：把它们改成 needs_attention'
            '（**不会**改成 queued——那可能重复发一条已经发出去的消息）。'
            % (len(in_flight), ','.join(row['id'] for row in in_flight[:5])))
    orphans = db.execute(
        'SELECT o.id FROM outbox o LEFT JOIN inbox i ON i.id = o.original_id WHERE i.id IS NULL'
    ).fetchall()
    if orphans:
        raise MigrationError(
            '有 %d 条 outbox 的原消息不在 inbox 里（foreign_keys=ON 会让整条 INSERT 失败）：%s'
            % (len(orphans), ','.join(row[0] for row in orphans[:5])))
    missing_chat = db.execute(
        'SELECT o.id FROM outbox o JOIN inbox i ON i.id = o.original_id WHERE i.chat_id IS NULL'
    ).fetchall()
    if missing_chat:
        raise MigrationError(
            '有 %d 条原消息没有 chat_id，回填不出来：%s。先补 chat_id 再重跑（不猜默认值）'
            % (len(missing_chat), ','.join(row[0] for row in missing_chat[:5])))
    return {'rows': _count(db, 'SELECT COUNT(*) FROM outbox'),
            'states': dict(db.execute('SELECT state,COUNT(*) FROM outbox GROUP BY state').fetchall()),
            'inflight': in_flight}


def backup(db, db_path):
    stamp = time.strftime('%Y%m%d%H%M%S')
    target = '%s.v7-%s' % (db_path, stamp)
    suffix = 1
    while os.path.exists(target):
        target = '%s.v7-%s-%d' % (db_path, stamp, suffix)
        suffix += 1
    db.execute("VACUUM INTO ?", (target,))
    # 备份里是消息正文，权限必须跟生产库一致（0600）。VACUUM INTO 默认按 umask 建文件（常见 0644）。
    os.chmod(target, 0o600)
    return target


def park_inflight(db, rows):
    """把停服后残留的在途行停成 ``needs_attention``，**绝不回退成 queued**。

    为什么不能改成 queued：``send_started`` 意味着请求可能已经打到飞书了，回退成 queued 会让
    sender 重发一条已经发出去的消息。这里只保留事实与人工核对入口。
    """
    error = '迁移时仍在发送中；未确认是否已发出，需人工核对'
    for row in rows:
        db.execute('UPDATE outbox SET state=?,error=?,attempt_id=NULL,lease_until=NULL WHERE id=?',
                   ('needs_attention', error, row['id']))
        print('park-inflight: id=%s original_id=%s → needs_attention（人工核对是否已发出）'
              % (row['id'], row['original_id']), file=sys.stderr)
    return len(rows)


def migrate(db, dry_run=False, inject_fault=False, park=False):
    """返回 (before, after) 两份快照；dry_run 时只跑前置检查与断言，不写库。"""
    before = preflight(db, allow_in_flight=park)
    inflight = before.pop('inflight', [])
    if dry_run:
        for row in inflight:
            print('dry-run: 在途行 id=%s original_id=%s（实迁需要 --park-inflight，'
                  '或先让旧服务把它收尾）' % (row['id'], row['original_id']), file=sys.stderr)
        return before, dict(before, dry_run=True, parked=0)

    db.execute('PRAGMA foreign_keys=ON')
    db.execute('BEGIN IMMEDIATE')
    try:
        parked = park_inflight(db, inflight) if inflight else 0
        after = migrate_outbox_to_v8(db)
        if inject_fault:
            raise MigrationError('--inject-fault：故意在提交前失败，用来验证回滚')
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return before, dict(after, parked=parked)


def verify(db_path):
    db = sqlite3.connect(db_path)
    try:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        checks = {
            'user_version': version,
            'rows': _count(db, 'SELECT COUNT(*) FROM outbox'),
            'one_row_per_message': _count(
                db, 'SELECT COUNT(*) FROM (SELECT inbox_id FROM outbox '
                    'GROUP BY inbox_id HAVING COUNT(*)>1)') == 0,
            'chat_id_not_null': _count(db, 'SELECT COUNT(*) FROM outbox WHERE chat_id IS NULL') == 0,
            'foreign_key_check': db.execute('PRAGMA foreign_key_check').fetchall(),
            'integrity_check': db.execute('PRAGMA integrity_check').fetchone()[0],
            'triggers': [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='outbox'")],
            'indexes': [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='outbox' "
                "AND name NOT LIKE 'sqlite_%'")],
        }
        return checks
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='outbox v7 → v8 迁移（一次性，可回滚）')
    parser.add_argument('--db', required=True, help='状态库路径（bridge.db）')
    parser.add_argument('--dry-run', action='store_true', help='只跑前置检查与断言，不写库')
    parser.add_argument('--verify', action='store_true', help='只做迁移后自检')
    parser.add_argument('--inject-fault', action='store_true',
                        help='故意在提交前抛错，用来验证回滚（测试用）')
    parser.add_argument('--park-inflight', action='store_true',
                        help='停服后若有 send_started/verifying 残留：把它们停成 needs_attention'
                             '（不回退成 queued），再迁移')
    args = parser.parse_args(argv)

    db_path = str(Path(args.db).expanduser())
    if not os.path.exists(db_path):
        print('找不到库：%s' % db_path, file=sys.stderr)
        return 2
    if args.verify:
        import json
        print(json.dumps(verify(db_path), ensure_ascii=False, indent=2))
        return 0

    db = sqlite3.connect(db_path, isolation_level=None)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        if not args.dry_run:
            before = preflight(db, allow_in_flight=args.park_inflight)
            db.close()
            db = None
            db = sqlite3.connect(db_path, isolation_level=None)
            target = backup(db, db_path)
            print('备份：%s' % target)
            before2, after = migrate(db, inject_fault=args.inject_fault,
                                     park=args.park_inflight)
            print('迁移完成：%s -> %s' % (before2, after))
        else:
            # dry-run 是「报告」，不是「闸门」：在途行照旧列出来并说明下一步，不因此退出非零。
            # 真正的闸门在实迁那一步（没有 --park-inflight 就中止）。
            before, _ = migrate(db, dry_run=True, park=True)
            print('dry-run 通过：%s' % (before,))
            return 0
    except MigrationError as error:
        print('迁移中止：%s' % error, file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()

    import json
    checks = verify(db_path)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    problems = []
    if checks['user_version'] != 8:
        problems.append('user_version=%s（应为 8）' % checks['user_version'])
    if not checks['one_row_per_message']:
        problems.append('同一消息被迁成多行')
    if not checks['chat_id_not_null']:
        problems.append('有 chat_id 为空的行')
    if checks['foreign_key_check']:
        problems.append('foreign_key_check 非空')
    if checks['integrity_check'] != 'ok':
        problems.append('integrity_check=%s' % checks['integrity_check'])
    if sorted(checks['triggers']) != ['outbox_immutable', 'outbox_keep_rows']:
        problems.append('触发器不对：%s' % checks['triggers'])
    if sorted(checks['indexes']) != ['outbox_due', 'outbox_seq']:
        problems.append('索引不对：%s' % checks['indexes'])
    if problems:
        print('自检未通过：' + '；'.join(problems), file=sys.stderr)
        return 1
    print('自检通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
