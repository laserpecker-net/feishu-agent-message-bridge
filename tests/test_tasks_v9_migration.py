"""tasks v8 → v9 迁移的判据（计划 §4 第 13–17 条）。

一条事实一个答案：全新安装的建表语句与迁移用的 ``ALTER`` 是两处写的，
``test_fresh_install_and_v8_migration_produce_identical_tasks_schema`` 把它们钉成同一个答案
（S1 的教训：两处 DDL 各自演化一次，最后谁都不准）。
"""

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bridge.store import Store, TASKS_V9_COLUMNS

ROOT = Path(__file__).resolve().parent.parent


def load_tasks_tool():
    spec = importlib.util.spec_from_file_location(
        'migrate_tasks_v9', ROOT / 'tools' / 'migrate-tasks-v9.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: v8 的 tasks 形状（10 列）。演的是历史，不是"另一种当前写法"：迁移就是往它上面 ALTER。
V8_TASKS_DDL = '''
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        title TEXT NOT NULL,
        state TEXT NOT NULL,
        steps TEXT NOT NULL DEFAULT '[]',
        artifacts TEXT NOT NULL DEFAULT '[]',
        open_question TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX tasks_chat ON tasks(chat_id,state,updated_at);
    CREATE TABLE task_messages (
        message_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id)
    );
'''

V8_SEED = (
    ('tsk_000000000001', 'oc_a', '把三张表汇总成一张', 'open', 1),
    ('tsk_000000000002', 'oc_b', '把上周的工单导出来', 'running', 2),
    ('tsk_000000000003', 'oc_a', '这活我不做', 'cancelled', 3),
)


class TasksV9MigrationCases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = self.home / 'state.sqlite3'
        self.tool = load_tasks_tool()

    def tearDown(self):
        self.temp.cleanup()

    # -- 夹具 ---------------------------------------------------------------
    def seed_v8(self, rows=V8_SEED):
        db = sqlite3.connect(str(self.path), isolation_level=None)
        db.executescript(V8_TASKS_DDL)
        for task_id, chat_id, title, state, index in rows:
            db.execute('''INSERT INTO tasks(id,chat_id,title,state,steps,artifacts,
                open_question,attempts,created_at,updated_at)
                VALUES (?,?,?,?,'[]','[]',NULL,0,?,?)''',
                       (task_id, chat_id, title, state, 100.0 + index, 100.0 + index))
        db.execute('PRAGMA user_version=8')
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

    # -- 13 -----------------------------------------------------------------
    def test_fresh_database_is_v9_with_new_columns(self):
        store = Store(self.home / 'fresh.sqlite3')
        try:
            self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0],
                             Store.SCHEMA_VERSION)
            columns = [row[1] for row in store.connection.execute('PRAGMA table_info(tasks)')]
            self.assertEqual(len(columns), 19, 'v11 起 tasks 是 19 列（v9 的 18 列 + card）')
            for name, _ in TASKS_V9_COLUMNS:
                self.assertIn(name, columns)
            task = store.draft_task('oc_a', '新活', 100)
            self.assertEqual(task['hints'], [])
        finally:
            store.close()

    # -- 14 -----------------------------------------------------------------
    def test_v8_database_is_refused_with_the_migration_command(self):
        self.seed_v8()
        with self.assertRaises(RuntimeError) as caught:
            Store(self.path)
        message = str(caught.exception)
        self.assertIn('migrate-tasks-v9.py', message)
        self.assertIn('v8', message)

    # -- 15 -----------------------------------------------------------------
    def test_v9_database_is_refused_by_v8_code(self):
        # 上界拒绝：S2 那版代码只认到 v9，打开 v10 库必须直接报错而不是带着缺列跑。
        store = Store(self.path)
        store.close()
        original = Store.SCHEMA_VERSION
        try:
            Store.SCHEMA_VERSION = 9          # 旧 release（S2）的视角
            with self.assertRaises(RuntimeError) as caught:
                Store(self.path)
            self.assertIn('Unsupported state schema version 11', str(caught.exception))
        finally:
            Store.SCHEMA_VERSION = original

    # -- 16 -----------------------------------------------------------------
    def test_fresh_install_and_v8_migration_produce_identical_tasks_schema(self):
        """两条路（全新 DDL / v8 + ALTER）必须给出同一张表；行也一样多、一样的内容。"""
        self.seed_v8()
        before_rows = sqlite3.connect(str(self.path)).execute(
            'SELECT id,chat_id,title,state,created_at FROM tasks ORDER BY id').fetchall()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)

        fresh_path = self.home / 'fresh.sqlite3'
        Store(fresh_path).close()
        # 全新库直接建到当前版本（v11），而 v9 工具只把库推到 v9——
        # 所以要比的是"把 v11 之后的列摘掉"的同一张表；多出来的列属于后续迁移，不归本工具管。
        fresh_columns = [row for row in self.columns(fresh_path) if row[1] != 'card']
        self.assertEqual(self.columns(self.path), fresh_columns)
        self.assertEqual(self.version(self.path), 9)

        after_rows = sqlite3.connect(str(self.path)).execute(
            'SELECT id,chat_id,title,state,created_at FROM tasks ORDER BY id').fetchall()
        self.assertEqual(after_rows, before_rows, '迁移前后 tasks 行必须一模一样')

    # -- 17 -----------------------------------------------------------------
    def test_fault_injection_rolls_back_to_v8(self):
        """提交前炸掉 → 一列都不许留下、版本号还是 v8（半迁移的库比没迁移更危险）。"""
        self.seed_v8()
        before = self.columns()
        code = self.tool.main(['--db', str(self.path), '--inject-fault'])
        self.assertEqual(code, 1)
        self.assertEqual(self.columns(), before)
        self.assertEqual(self.version(), 8)

    def test_verify_reports_columns_and_row_count(self):
        self.seed_v8()
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)
        checks = self.tool.verify(str(self.path))
        self.assertEqual(checks['user_version'], 9)
        self.assertTrue(checks['has_all_v9_columns'])
        self.assertEqual(checks['rows'], len(V8_SEED))
        self.assertEqual(checks['integrity_check'], 'ok')
        self.assertEqual(checks['foreign_key_check'], [])


if __name__ == '__main__':
    unittest.main()
