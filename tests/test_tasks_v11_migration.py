"""tasks v10 → v11 迁移的判据（S3a 真机事故之后：任务书取自卡片，不取来源消息）。

一条事实一个答案：全新安装的建表语句与迁移用的 ``ALTER`` 是两处写的，
``test_fresh_install_and_v10_migration_produce_identical_tasks_schema`` 把它们钉成同一个答案。
"""

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bridge.store import TASKS_V11_COLUMNS, Store

ROOT = Path(__file__).resolve().parent.parent


def load_v11_tool():
    spec = importlib.util.spec_from_file_location(
        'migrate_tasks_v11', ROOT / 'tools' / 'migrate-tasks-v11.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: v10 的 tasks 形状（18 列，没有 card）。演的是历史，不是"另一种当前写法"。
V10_TASKS_DDL = '''
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL,
        steps TEXT NOT NULL DEFAULT '[]', artifacts TEXT NOT NULL DEFAULT '[]',
        open_question TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        origin_mid TEXT, session_id TEXT, executor TEXT, hints TEXT NOT NULL DEFAULT '[]',
        approval_mid TEXT, approval_kind TEXT, approved_at REAL, started_at REAL
    );
    CREATE TABLE consumers (
        name TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at REAL NOT NULL,
        last_seen_at REAL NOT NULL, claims INTEGER NOT NULL DEFAULT 0,
        acks INTEGER NOT NULL DEFAULT 0, fails INTEGER NOT NULL DEFAULT 0,
        role TEXT NOT NULL DEFAULT 'replier'
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created_at REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        lease_until REAL, consumer TEXT, claimed_at REAL, acked_at REAL,
        next_due REAL NOT NULL DEFAULT 0, error TEXT, claim_token TEXT, used_ids TEXT
    );
'''


class TasksV11MigrationCases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = self.home / 'state.sqlite3'
        self.tool = load_v11_tool()

    def tearDown(self):
        self.temp.cleanup()

    # -- 夹具 ---------------------------------------------------------------
    def seed_v10(self):
        db = sqlite3.connect(str(self.path), isolation_level=None)
        db.executescript(V10_TASKS_DDL)
        db.execute('''INSERT INTO tasks(id,chat_id,title,state,created_at,updated_at,hints)
                   VALUES ('tsk_1','oc_a','把表汇总','open',100,100,'[]')''')
        db.execute("INSERT INTO consumers(name,kind,created_at,last_seen_at,role) "
                   "VALUES ('dsh-main','dsh',100,100,'replier')")
        db.execute("INSERT INTO events(kind,dedupe_key,payload,created_at) "
                   "VALUES ('message','m1','{}',100)")
        db.execute('PRAGMA user_version=10')
        db.close()

    def columns(self, path=None):
        db = sqlite3.connect(str(path or self.path))
        try:
            return [tuple(row) for row in db.execute('PRAGMA table_info(tasks)')]
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
    def test_fresh_database_is_current_version_with_card_column(self):
        store = Store(self.home / 'fresh.sqlite3')
        try:
            self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0],
                             Store.SCHEMA_VERSION)
            columns = [row[1] for row in store.connection.execute('PRAGMA table_info(tasks)')]
            self.assertEqual(len(columns), 19, 'v10 的 18 列 + card')
            for name, _ in TASKS_V11_COLUMNS:
                self.assertIn(name, columns)
            self.assertEqual(columns[-1], 'card', '加列位置在表尾，两条路才对得上')
        finally:
            store.close()

    def test_v10_database_is_refused_with_the_migration_command(self):
        self.seed_v10()
        with self.assertRaises(RuntimeError) as caught:
            Store(self.path)
        message = str(caught.exception)
        self.assertIn('migrate-tasks-v11.py', message)
        self.assertIn('v10', message)

    def test_v9_database_is_refused_by_the_v11_tool(self):
        db = sqlite3.connect(str(self.path), isolation_level=None)
        db.executescript('CREATE TABLE tasks (id TEXT PRIMARY KEY);')
        db.execute('PRAGMA user_version=9')
        db.close()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 1)
        self.assertEqual(self.version(), 9)

    def test_v11_database_is_refused_by_the_tool(self):
        Store(self.path).close()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 1)
        self.assertEqual(self.version(), Store.SCHEMA_VERSION)

    # -- 迁移 ---------------------------------------------------------------
    def test_fresh_install_and_v10_migration_produce_identical_tasks_schema(self):
        self.seed_v10()
        before_rows = sqlite3.connect(str(self.path)).execute(
            'SELECT id,chat_id,title,state,created_at FROM tasks ORDER BY id').fetchall()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)

        fresh_path = self.home / 'fresh.sqlite3'
        Store(fresh_path).close()
        self.assertEqual(self.columns(self.path), self.columns(fresh_path))
        self.assertEqual(self.version(), 11)

        after_rows = sqlite3.connect(str(self.path)).execute(
            'SELECT id,chat_id,title,state,created_at FROM tasks ORDER BY id').fetchall()
        self.assertEqual(after_rows, before_rows, '迁移前后 tasks 行必须一模一样')

    def test_dry_run_does_not_write(self):
        self.seed_v10()
        before = self.columns()
        self.assertEqual(self.tool.main(['--db', str(self.path), '--dry-run']), 0)
        self.assertEqual(self.columns(), before)
        self.assertEqual(self.version(), 10)

    def test_fault_injection_rolls_back_to_v10(self):
        self.seed_v10()
        before = self.columns()
        self.assertEqual(self.tool.main(['--db', str(self.path), '--inject-fault']), 1)
        self.assertEqual(self.columns(), before)
        self.assertEqual(self.version(), 10)

    def test_verify_reports_card_column_and_row_count(self):
        self.seed_v10()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)
        checks = self.tool.verify(str(self.path))
        self.assertEqual(checks['user_version'], 11)
        self.assertEqual(checks['column_count'], 19)
        self.assertTrue(checks['has_all_v11_columns'])
        self.assertEqual(checks['rows'], 1)
        self.assertEqual(checks['tasks_with_card'], 0, '老任务没有卡片正文，迁移不伪造')

    def test_migrated_database_stores_a_card_and_work_event_uses_it(self):
        """迁移之后的库能正常记卡片，且批准派活时任务书来自卡片。"""
        self.seed_v10()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)
        store = Store(self.path)
        try:
            task = store.draft_task('oc_a', '写个说明文件', 200, origin_mid='om_src',
                                    card='卡片正文：只写 docs/x.md')
            store.approve_task(task['id'], approval_mid='om_ok', approval_kind='command',
                               hints_text='做', now=210)
            import json
            payload = json.loads(store.connection.execute(
                "SELECT payload FROM events WHERE kind='work'").fetchone()[0])
            self.assertEqual(payload['requirement'], '卡片正文：只写 docs/x.md')
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
