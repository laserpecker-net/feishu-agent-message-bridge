"""consumers v9 → v10 迁移的判据（S3a PR-1）。

一条事实一个答案：全新安装的建表语句与迁移用的 ``ALTER`` 是两处写的，
``test_fresh_install_and_v9_migration_produce_identical_consumers_schema`` 把它们钉成同一个答案
（S1/S2 的教训：两处 DDL 各自演化一次，最后谁都不准）。
"""

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bridge.store import CONSUMERS_V10_COLUMNS, Store

ROOT = Path(__file__).resolve().parent.parent


def load_work_tool():
    spec = importlib.util.spec_from_file_location(
        'migrate_work_v10', ROOT / 'tools' / 'migrate-work-v10.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: v9 的 consumers 形状（7 列，没有 role）。演的是历史，不是"另一种当前写法"。
V9_CONSUMERS_DDL = '''
    CREATE TABLE consumers (
        name TEXT PRIMARY KEY, kind TEXT NOT NULL,
        created_at REAL NOT NULL, last_seen_at REAL NOT NULL,
        claims INTEGER NOT NULL DEFAULT 0, acks INTEGER NOT NULL DEFAULT 0,
        fails INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created_at REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        lease_until REAL, consumer TEXT, claimed_at REAL, acked_at REAL,
        next_due REAL NOT NULL DEFAULT 0, error TEXT, claim_token TEXT, used_ids TEXT
    );
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL,
        steps TEXT NOT NULL DEFAULT '[]', artifacts TEXT NOT NULL DEFAULT '[]',
        open_question TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        origin_mid TEXT, session_id TEXT, executor TEXT, hints TEXT NOT NULL DEFAULT '[]',
        approval_mid TEXT, approval_kind TEXT, approved_at REAL, started_at REAL
    );
'''

V9_SEED = (
    ('dsh-main', 'dsh', 30, 1, 0),      # 回话端（迁移后 role 应默认 replier）
    ('win-main', 'other', 1, 1, 0),
    ('s1-demo', 'dsh', 60, 39, 1),
)


class WorkV10MigrationCases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = self.home / 'state.sqlite3'
        self.tool = load_work_tool()

    def tearDown(self):
        self.temp.cleanup()

    # -- 夹具 ---------------------------------------------------------------
    def seed_v9(self, rows=V9_SEED):
        db = sqlite3.connect(str(self.path), isolation_level=None)
        db.executescript(V9_CONSUMERS_DDL)
        for name, kind, claims, acks, fails in rows:
            db.execute('INSERT INTO consumers VALUES (?,?,?,?,?,?,?)',
                       (name, kind, 100.0, 100.0, claims, acks, fails))
        db.execute("INSERT INTO events(kind,dedupe_key,payload,created_at) "
                   "VALUES ('message','m1','{}',100)")
        db.execute('''INSERT INTO tasks(id,chat_id,title,state,created_at,updated_at)
                   VALUES ('tsk_1','oc_a','把表汇总','open',100,100)''')
        db.execute('PRAGMA user_version=9')
        db.close()

    def columns(self, path=None):
        db = sqlite3.connect(str(path or self.path))
        try:
            return [tuple(row) for row in db.execute('PRAGMA table_info(consumers)')]
        finally:
            db.close()

    def version(self, path=None):
        db = sqlite3.connect(str(path or self.path))
        try:
            return db.execute('PRAGMA user_version').fetchone()[0]
        finally:
            db.close()

    def scalar(self, sql, params=()):
        db = sqlite3.connect(str(self.path))
        try:
            return db.execute(sql, params).fetchone()[0]
        finally:
            db.close()

    # -- 门禁 ---------------------------------------------------------------
    def test_fresh_database_is_current_version_with_role_column(self):
        # 全新库直接建到当前版本（v11 起）；这里只关心 role 这一列在不在、默认值对不对。
        store = Store(self.home / 'fresh.sqlite3')
        try:
            self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0],
                             Store.SCHEMA_VERSION)
            columns = [row[1] for row in store.connection.execute('PRAGMA table_info(consumers)')]
            for name, _ in CONSUMERS_V10_COLUMNS:
                self.assertIn(name, columns)
            consumer = store.register_consumer('dsh-main', 'dsh', 100)
            self.assertEqual(consumer['role'], 'replier')
        finally:
            store.close()

    def test_v9_database_is_refused_with_the_migration_command(self):
        self.seed_v9()
        with self.assertRaises(RuntimeError) as caught:
            Store(self.path)
        message = str(caught.exception)
        self.assertIn('migrate-work-v10.py', message)
        self.assertIn('v9', message)

    def test_newer_database_is_refused_by_older_code(self):
        # 上界拒绝：旧代码只认到它自己那一版，打开更新的库必须直接报错。
        Store(self.path).close()
        original = Store.SCHEMA_VERSION
        try:
            Store.SCHEMA_VERSION = 9          # 旧 release（S2）的视角
            with self.assertRaises(RuntimeError) as caught:
                Store(self.path)
            self.assertIn('Unsupported state schema version 11', str(caught.exception))
        finally:
            Store.SCHEMA_VERSION = original

    # -- 迁移 ---------------------------------------------------------------
    def test_fresh_install_and_v9_migration_produce_identical_consumers_schema(self):
        """两条路（全新 DDL / v9 + ALTER）必须给出同一张表，且历史行原样保留。"""
        self.seed_v9()
        before_rows = sqlite3.connect(str(self.path)).execute(
            'SELECT name,kind,claims,acks,fails FROM consumers ORDER BY name').fetchall()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)

        fresh_path = self.home / 'fresh.sqlite3'
        Store(fresh_path).close()
        self.assertEqual(self.columns(self.path), self.columns(fresh_path))
        self.assertEqual(self.version(self.path), 10)

        after_rows = sqlite3.connect(str(self.path)).execute(
            'SELECT name,kind,claims,acks,fails FROM consumers ORDER BY name').fetchall()
        self.assertEqual(after_rows, before_rows, '迁移前后 consumers 行必须一模一样')

    def test_migrated_rows_default_to_replier_role(self):
        """老库里的消费者不需要改任何东西：迁移后全是 replier（= 迁移前的行为）。"""
        self.seed_v9()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)
        roles = sqlite3.connect(str(self.path)).execute(
            'SELECT DISTINCT role FROM consumers').fetchall()
        self.assertEqual(roles, [('replier',)])

    def test_migration_does_not_touch_events_or_tasks(self):
        self.seed_v9()
        before = (self.scalar('SELECT COUNT(*) FROM events'), self.scalar('SELECT COUNT(*) FROM tasks'))
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)
        self.assertEqual((self.scalar('SELECT COUNT(*) FROM events'),
                          self.scalar('SELECT COUNT(*) FROM tasks')), before)

    def test_dry_run_writes_nothing(self):
        self.seed_v9()
        before = self.columns()
        self.assertEqual(self.tool.main(['--db', str(self.path), '--dry-run']), 0)
        self.assertEqual(self.columns(), before)
        self.assertEqual(self.version(), 9)

    def test_fault_injection_rolls_back_to_v9(self):
        """提交前炸掉 → 一列都不许留下、版本号还是 v9（半迁移的库比没迁移更危险）。"""
        self.seed_v9()
        before = self.columns()
        self.assertEqual(self.tool.main(['--db', str(self.path), '--inject-fault']), 1)
        self.assertEqual(self.columns(), before)
        self.assertEqual(self.version(), 9)

    def test_second_run_is_refused(self):
        self.seed_v9()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 1)

    def test_v8_database_is_refused_by_this_tool(self):
        self.seed_v9()
        raw = sqlite3.connect(str(self.path))
        raw.execute('PRAGMA user_version=8')
        raw.close()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 1)

    def test_verify_reports_columns_rows_and_roles(self):
        self.seed_v9()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)
        checks = self.tool.verify(str(self.path))
        self.assertEqual(checks['user_version'], 10)
        self.assertTrue(checks['has_all_v10_columns'])
        self.assertEqual(checks['rows'], len(V9_SEED))
        self.assertEqual(checks['column_count'], 8)
        self.assertEqual(checks['roles'], ['replier'])
        self.assertEqual(checks['integrity_check'], 'ok')
        self.assertEqual(checks['foreign_key_check'], [])


if __name__ == '__main__':
    unittest.main()
