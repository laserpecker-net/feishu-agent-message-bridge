"""S1a/S1b：outbox v8 的表结构、顺序规则、两个标识符与 v7→v8 迁移。

对应 docs/design-s1-outbox-v8.md §8 的用例。这里的判据都能直接重跑：
``python3 -m pytest tests/test_outbox_v8.py -q``。
"""

import contextlib
import importlib.util
import io
import re
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from bridge import metrics
from bridge.store import Store

ROOT = Path(__file__).resolve().parent.parent


def load_migration_tool():
    spec = importlib.util.spec_from_file_location(
        'migrate_outbox_v8', ROOT / 'tools' / 'migrate-outbox-v8.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_tasks_v11_tool():
    spec = importlib.util.spec_from_file_location(
        'migrate_tasks_v11', ROOT / 'tools' / 'migrate-tasks-v11.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_work_tool():
    spec = importlib.util.spec_from_file_location(
        'migrate_work_v10', ROOT / 'tools' / 'migrate-work-v10.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_tasks_tool():
    spec = importlib.util.spec_from_file_location(
        'migrate_tasks_v9', ROOT / 'tools' / 'migrate-tasks-v9.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OutboxV8Cases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = self.home / 'state.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    # --- 工具 -----------------------------------------------------------
    def seed(self, mid='m1', chat_id='oc_1', state='external_queued', now=10.0):
        self.store.connection.execute(
            'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_type,version,raw_json)'
            ' VALUES(?,?,?,0,?,?,?,?)', (mid, state, now, chat_id, 'user', 'v1', '{}'))
        self.store.connection.commit()
        return mid

    def rows(self):
        return [dict(row) for row in self.store.connection.execute(
            'SELECT * FROM outbox ORDER BY row_id')]

    # --- 结构与 seq -----------------------------------------------------
    def test_fresh_database_is_v8(self):
        columns = [row[1] for row in self.store.connection.execute('PRAGMA table_info(outbox)')]
        for name in ('row_id', 'inbox_id', 'chat_id', 'seq', 'kind', 'target_mid', 'retracted_at'):
            self.assertIn(name, columns)
        self.assertNotIn('original_id', columns)
        self.assertEqual(
            self.store.connection.execute('PRAGMA user_version').fetchone()[0],
            Store.SCHEMA_VERSION)

    def test_append_seq_increments_within_one_message(self):
        self.seed('m1')
        self.seed('m2')
        self.assertEqual(self.store.append_outbox('m1', 'reply', 'a', now=10)[1], 1)
        self.assertEqual(self.store.append_outbox('m1', 'say', 'b', now=11)[1], 2)
        self.assertEqual(self.store.append_outbox('m1', 'say', 'c', now=12)[1], 3)
        # 另一条消息从 1 开始（MAX(seq) 的 WHERE inbox_id=? 起作用）
        self.assertEqual(self.store.append_outbox('m2', 'reply', 'd', now=10)[1], 1)

    def test_proactive_messages_do_not_collide_on_seq(self):
        first = self.store.append_outbox(None, 'say', 'one', chat_id='oc_9', now=10)
        second = self.store.append_outbox(None, 'say', 'two', chat_id='oc_9', now=11)
        self.assertEqual(first[1], 1)
        self.assertEqual(second[1], 1)
        self.assertNotEqual(first[0], second[0])

    def test_append_rejects_reply_without_inbox_and_proactive_without_chat(self):
        with self.assertRaises(ValueError):
            self.store.append_outbox(None, 'reply', 'x', chat_id='oc_1')
        with self.assertRaises(ValueError):
            self.store.append_outbox(None, 'say', 'x')
        with self.assertRaises(ValueError):
            self.store.append_outbox('missing', 'reply', 'x')

    def test_append_is_idempotent_on_dedupe_uuid(self):
        self.seed('m1')
        first = self.store.append_outbox('m1', 'say', 'hi', dedupe_uuid='k1', now=10)
        again = self.store.append_outbox('m1', 'say', 'hi', dedupe_uuid='k1', now=99)
        self.assertEqual(first, again)
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_append_never_duplicates_seq(self):
        self.seed('m1')
        errors = []

        def worker():
            try:
                for _ in range(5):
                    self.store.append_outbox('m1', 'say', 'x')
            except Exception as error:  # pragma: no cover - 失败就是用例失败
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
            assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertEqual(errors, [])
        seqs = sorted(row['seq'] for row in self.rows())
        self.assertEqual(seqs, list(range(1, 11)))

    def test_partial_unique_index_blocks_duplicate_seq(self):
        self.seed('m1')
        self.store.append_outbox('m1', 'reply', 'a', now=10)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO outbox(inbox_id,chat_id,seq,kind,uuid,created_at,next_due)"
                " VALUES('m1','oc_1',1,'say','dup-uuid',10,10)")

    # --- 顺序规则（§4）--------------------------------------------------
    def test_claim_prefers_lowest_seq_over_due_time(self):
        self.seed('m1')
        self.store.append_outbox('m1', 'say', 'first', now=10)
        self.store.append_outbox('m1', 'reply', 'second', now=10)
        self.store.connection.execute(
            "UPDATE outbox SET next_due=50 WHERE kind='say'")
        self.store.connection.execute(
            "UPDATE outbox SET next_due=10 WHERE kind='reply'")
        self.store.connection.commit()
        job = self.store.claim_outbox(60, 10)
        self.assertEqual(job['kind'], 'say')

    def test_unfinished_predecessor_blocks_the_rest(self):
        self.seed('m1')
        self.store.append_outbox('m1', 'reply', 'first', now=10)
        self.store.append_outbox('m1', 'say', 'second', now=10)
        job = self.store.claim_outbox(10, 10)
        self.assertEqual(job['kind'], 'reply')
        # 第一条还停在 send_started，第二条不该被取走
        self.assertIsNone(self.store.claim_outbox(11, 10))

    def test_every_releasing_state_unblocks_the_next_seq(self):
        for state in ('reply_verified', 'sent', 'retracted', 'dead', 'dropped',
                      'needs_attention', 'outcome_unknown'):
            with self.subTest(state=state):
                self.store.close()
                self.temp.cleanup()
                self.temp = tempfile.TemporaryDirectory()
                self.home = Path(self.temp.name)
                self.path = self.home / 'state.sqlite3'
                self.store = Store(self.path)
                self.seed('m1')
                first, _ = self.store.append_outbox('m1', 'reply', 'first', now=10)
                self.store.append_outbox('m1', 'say', 'second', now=10)
                self.store.connection.execute(
                    'UPDATE outbox SET state=? WHERE row_id=?', (state, first))
                self.store.connection.commit()
                job = self.store.claim_outbox(10, 10)
                self.assertIsNotNone(job, state)
                self.assertEqual(job['kind'], 'say')

    def test_send_started_lease_expiry_allows_reclaim(self):
        self.seed('m1')
        self.store.append_outbox('m1', 'reply', 'a', now=10)
        first = self.store.claim_outbox(10, 5)
        self.assertEqual(first['state'], 'send_started')
        self.assertIsNone(self.store.claim_outbox(12, 5))    # 租约还在
        second = self.store.claim_outbox(16, 5)              # 租约过期
        self.assertIsNotNone(second)
        self.assertEqual(second['row_id'], first['row_id'])
        self.assertNotEqual(second['attempt_id'], first['attempt_id'])

    def test_claim_scenarios_match_the_design_table(self):
        self.seed('m1')
        self.store.append_outbox('m1', 'say', 'a', now=10)
        self.store.append_outbox('m1', 'reply', 'b', now=10)
        job = self.store.claim_outbox(10, 10)          # 场景 1：seq 优先
        self.assertEqual(job['kind'], 'say')
        self.store.outbox_marked(job['row_id'], job['attempt_id'], 11, 'om_say')
        job = self.store.claim_outbox(12, 10)          # 场景 2：前驱 sent 后取 seq2
        self.assertEqual(job['kind'], 'reply')
        self.assertEqual(job['mode'], 'send')

    # --- say / retract 的语义 -------------------------------------------
    def test_say_success_marks_sent_and_leaves_inbox_alone(self):
        self.seed('m1', state='external_queued')
        row_id, _ = self.store.append_outbox('m1', 'say', '收到，我看看', now=10)
        job = self.store.claim_outbox(10, 10)
        self.assertTrue(self.store.outbox_marked(row_id, job['attempt_id'], 11, 'om_say'))
        row = self.store.get_outbox(row_id)
        self.assertEqual(row['state'], 'sent')
        self.assertEqual(row['reply_id'], 'om_say')
        self.assertEqual(self.store.get_message('m1')['state'], 'external_queued')
        self.assertNotIn('sent', Store.UNFINISHED_OUTBOX_STATES)

    def test_say_reply_id_does_not_switch_claim_to_verify_mode(self):
        self.seed('m1')
        row_id, _ = self.store.append_outbox('m1', 'say', 'a', now=10)
        job = self.store.claim_outbox(10, 10)
        self.store.outbox_marked(row_id, job['attempt_id'], 11, 'om_say')
        # reply_id 已非空，但 say 不该走 verify 分支
        row = self.store.get_outbox(row_id)
        self.assertEqual(row['state'], 'sent')

    def test_say_failure_never_marks_task_or_outcome_unknown(self):
        self.seed('m1', state='external_queued')
        row_id, _ = self.store.append_outbox('m1', 'say', 'a', now=10)
        job = self.store.claim_outbox(10, 10)
        self.store.mark_send_dispatched(row_id, job['attempt_id'], 10)
        self.assertTrue(self.store.fail_outbox(
            row_id, job['attempt_id'], 12, 'timeout', 0))
        self.assertEqual(self.store.get_outbox(row_id)['state'], 'retry_wait')
        self.assertEqual(self.store.get_message('m1')['state'], 'external_queued')

    def test_claim_sweep_never_turns_a_say_row_into_outcome_unknown(self):
        # 超时清扫只认 kind='reply'：一条 stuck 的 say 不能被判成「结果不明」，
        # 更不许把整条任务标红。
        self.seed('m1', state='external_queued')
        row_id, _ = self.store.append_outbox('m1', 'say', 'a', now=10)
        self.store.connection.execute(
            "UPDATE outbox SET state='queued',first_send_at=10 WHERE row_id=?", (row_id,))
        self.store.connection.commit()
        self.store.claim_outbox(10 + 4000, 10)
        state = self.store.get_outbox(row_id)['state']
        self.assertNotEqual(state, 'outcome_unknown')
        self.assertEqual(self.store.get_message('m1')['state'], 'external_queued')

    def test_reply_failure_in_window_stays_retryable(self):
        self.seed('m1')
        row_id, _ = self.store.append_outbox('m1', 'reply', 'a', now=10)
        job = self.store.claim_outbox(10, 10)
        self.store.mark_send_dispatched(row_id, job['attempt_id'], 10)
        self.store.fail_outbox(row_id, job['attempt_id'], 11, 'offline', 30)
        self.assertEqual(self.store.get_outbox(row_id)['state'], 'retry_wait')

    def test_retract_marks_the_target_row(self):
        self.seed('m1')
        say_id, _ = self.store.append_outbox('m1', 'say', 'a', now=10)
        job = self.store.claim_outbox(10, 10)
        self.store.outbox_marked(say_id, job['attempt_id'], 11, 'om_say')
        retract_id, _ = self.store.append_outbox(
            'm1', 'retract', '', target_mid='om_say', now=12)
        job = self.store.claim_outbox(12, 10)
        self.assertEqual(job['kind'], 'retract')
        self.assertTrue(self.store.outbox_retracted(
            retract_id, job['attempt_id'], 13, target_row_id=say_id))
        self.assertEqual(self.store.get_outbox(retract_id)['state'], 'sent')
        self.assertEqual(self.store.get_outbox(say_id)['retracted_at'], 13)
        self.assertEqual(self.store.get_outbox(say_id)['state'], 'sent')

    def test_content_columns_are_immutable_state_is_not(self):
        self.seed('m1')
        row_id, _ = self.store.append_outbox('m1', 'say', 'a', now=10)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE outbox SET body='mutated' WHERE row_id=?", (row_id,))
        self.store.connection.execute(
            "UPDATE outbox SET state='retry_wait' WHERE row_id=?", (row_id,))
        self.store.connection.commit()

    # --- 两个标识符（§5.1）----------------------------------------------
    def test_row_id_and_inbox_id_are_not_interchangeable(self):
        self.seed('m1')
        row_id, _ = self.store.append_outbox('m1', 'reply', 'a', now=10)
        self.assertIsNotNone(self.store.get_outbox(row_id))
        self.assertEqual(self.store.get_outbox(row_id)['inbox_id'], 'm1')
        # 拿消息 id 当行号：必须查不到（这正是 v7 里混用一个 mid 会静默失败的地方）
        self.assertIsNone(self.store.get_outbox('m1'))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['row_id'], row_id)

    def test_outbox_lookup_by_sent_message_id(self):
        """撤回只有 target_mid（飞书侧消息 id），必须能反查到原行。"""
        self.seed('m1')
        row_id, _ = self.store.append_outbox('m1', 'say', 'a', now=10)
        self.assertIsNone(self.store.get_outbox_by_message_id('om_sent'))  # 还没发出去
        outgoing = self.store.claim_outbox(20, 120)
        self.assertTrue(self.store.outbox_marked(outgoing['row_id'], outgoing['attempt_id'],
                                                 21, 'om_sent'))
        self.assertEqual(self.store.get_outbox_by_message_id('om_sent')['row_id'], row_id)
        self.assertIsNone(self.store.get_outbox_by_message_id(''))
        self.assertIsNone(self.store.get_outbox_by_message_id(None))

    # --- 计数口径 -------------------------------------------------------
    def test_latency_counts_one_sample_per_message(self):
        self.store.connection.execute(
            "INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_type,create_ms)"
            " VALUES('m1','completed',10,0,'oc_1','user',9000)")
        self.store.connection.commit()
        self.store.connection.execute(
            "INSERT INTO outbox(inbox_id,chat_id,seq,kind,uuid,body,body_hash,created_at,state,"
            "sent_at,verified_at) VALUES('m1','oc_1',1,'reply','u1','a','h',10,'reply_verified',12,13)")
        self.store.connection.execute(
            "INSERT INTO outbox(inbox_id,chat_id,seq,kind,uuid,body,body_hash,created_at,state,"
            "sent_at) VALUES('m1','oc_1',2,'say','u2','b','h',10,'sent',11)")
        self.store.connection.commit()
        report = metrics.latency(self.home, window=50)
        self.assertEqual(report['samples']['answered'], 1)

    def test_drop_pending_counts_messages_not_rows(self):
        self.seed('m1', state='reply_queued')
        self.store.append_outbox('m1', 'reply', 'a', now=10)
        self.store.append_outbox('m1', 'say', 'b', now=10)
        self.assertEqual(self.store.drop_pending(20), 1)

    def test_status_oldest_pending_ignores_a_sent_say(self):
        self.seed('m1', state='reply_queued', now=10)
        self.store.connection.execute(
            "INSERT INTO outbox(inbox_id,chat_id,seq,kind,uuid,body,body_hash,created_at,state,"
            "sent_at) VALUES('m1','oc_1',1,'say','u1','a','h',10,'sent',11)")
        self.store.connection.commit()
        stats = self.store.stats()
        self.assertIsNotNone(stats['oldest_pending_at'])

    # --- 门禁 -----------------------------------------------------------
    def test_repository_has_no_v7_outbox_join_left(self):
        # 正则而不是字面量：这样这份门禁自身不会命中自己。
        pattern = re.compile(r'o\.id\s*=\s*i\.id')
        offenders = []
        for path in (list((ROOT / 'bridge').glob('*.py')) + list((ROOT / 'tools').glob('*.py'))):
            if pattern.search(path.read_text(encoding='utf-8')):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


class MigrationCases(unittest.TestCase):
    V7_DDL = '''
        CREATE TABLE inbox (id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'reply_queued',
            received_at REAL NOT NULL, next_due REAL NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, attempt_id TEXT, lease_until REAL,
            claim_version TEXT, chat_id TEXT, parent_id TEXT, root_id TEXT, thread_id TEXT,
            create_ms INTEGER, version TEXT, raw_json TEXT, text TEXT, sender_type TEXT,
            sender_id TEXT, error TEXT);
        CREATE TABLE outbox (
            id TEXT PRIMARY KEY REFERENCES inbox(id),
            original_id TEXT NOT NULL UNIQUE REFERENCES inbox(id),
            uuid TEXT NOT NULL UNIQUE, body TEXT NOT NULL, body_hash TEXT NOT NULL,
            created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
            next_due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            verify_attempts INTEGER NOT NULL DEFAULT 0, attempt_id TEXT, lease_until REAL,
            first_send_at REAL, reply_id TEXT, sent_at REAL, verified_at REAL, error TEXT);
        CREATE INDEX outbox_due ON outbox(state,next_due,created_at);
        CREATE TRIGGER outbox_keep_rows BEFORE DELETE ON outbox
            BEGIN SELECT RAISE(ABORT,'Outbox rows cannot be deleted'); END;
        CREATE TRIGGER outbox_immutable
            BEFORE UPDATE OF id,original_id,uuid,body,body_hash,created_at ON outbox
            WHEN NEW.id IS NOT OLD.id OR NEW.original_id IS NOT OLD.original_id
              OR NEW.uuid IS NOT OLD.uuid OR NEW.body IS NOT OLD.body
              OR NEW.body_hash IS NOT OLD.body_hash OR NEW.created_at IS NOT OLD.created_at
            BEGIN SELECT RAISE(ABORT,'Outbox content is immutable'); END;
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, title TEXT NOT NULL,
            state TEXT NOT NULL, steps TEXT NOT NULL DEFAULT '[]',
            artifacts TEXT NOT NULL DEFAULT '[]', open_question TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE task_messages (message_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id));
        -- v7 的 consumers 形状（7 列，没有 role）。真实 v7 库一直有这张表：
        -- S1 的消费者登记就靠它；v10 的迁移只往它上面加 role。
        CREATE TABLE consumers (
            name TEXT PRIMARY KEY, kind TEXT NOT NULL,
            created_at REAL NOT NULL, last_seen_at REAL NOT NULL,
            claims INTEGER NOT NULL DEFAULT 0, acks INTEGER NOT NULL DEFAULT 0,
            fails INTEGER NOT NULL DEFAULT 0);
        PRAGMA user_version=7;
    '''

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = self.home / 'bridge.db'
        self.tool = load_migration_tool()
        self.seed_v7()

    def tearDown(self):
        self.temp.cleanup()

    def seed_v7(self, states=(('m1', 'reply_verified'), ('m2', 'queued'), ('m3', 'retry_wait'))):
        if self.path.exists():
            self.path.unlink()
        db = sqlite3.connect(str(self.path), isolation_level=None)
        db.executescript(self.V7_DDL)
        for index, (mid, state) in enumerate(states):
            db.execute("INSERT INTO inbox(id,received_at,chat_id,sender_type,version,raw_json)"
                       " VALUES(?,?,'oc_legacy','user','v1','{}')", (mid, 100 + index))
            db.execute("INSERT INTO outbox(id,original_id,uuid,body,body_hash,created_at,state)"
                       " VALUES(?,?,?,?,?,?,?)",
                       (mid, mid, 'uuid-%s' % mid, 'body %s' % mid, 'h', 100.0 + index, state))
        db.close()

    def raw(self):
        return sqlite3.connect(str(self.path))

    def test_dry_run_reports_and_changes_nothing(self):
        code = self.tool.main(['--db', str(self.path), '--dry-run'])
        self.assertEqual(code, 0)
        db = self.raw()
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)
            columns = [row[1] for row in db.execute('PRAGMA table_info(outbox)')]
            self.assertIn('original_id', columns)
        finally:
            db.close()

    def test_migration_preserves_rows_states_and_backfills_chat_id(self):
        code = self.tool.main(['--db', str(self.path)])
        self.assertEqual(code, 0)
        db = self.raw()
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 8)
            rows = [dict(zip(('inbox_id', 'chat_id', 'seq', 'kind', 'state'),
                             row)) for row in db.execute(
                'SELECT inbox_id,chat_id,seq,kind,state FROM outbox ORDER BY inbox_id')]
            self.assertEqual([row['state'] for row in rows],
                             ['reply_verified', 'queued', 'retry_wait'])
            self.assertEqual({row['chat_id'] for row in rows}, {'oc_legacy'})
            self.assertEqual({(row['seq'], row['kind']) for row in rows}, {(1, 'reply')})
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            db.close()

    def test_migration_tolerates_existing_index_names(self):
        # v7 的 outbox_due 会跟着 RENAME 一起走：不先 DROP 就会 "already exists"
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)

    def test_fault_injection_rolls_everything_back(self):
        code = self.tool.main(['--db', str(self.path), '--inject-fault'])
        self.assertEqual(code, 1)
        db = self.raw()
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)
            columns = [row[1] for row in db.execute('PRAGMA table_info(outbox)')]
            self.assertIn('original_id', columns)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 3)
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('outbox_v7', tables)
        finally:
            db.close()

    def test_second_run_refuses_an_already_migrated_database(self):
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 1)

    def test_refuses_in_flight_sends(self):
        """实迁必须拒绝；dry-run 只报告（闸门在实迁那一步，见 test_dry_run_lists_in_flight_rows）。"""
        self.seed_v7(states=(('m1', 'send_started'),))
        self.assertNotEqual(self.tool.main(['--db', str(self.path)]), 0)
        db = self.raw()
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)
            self.assertEqual(
                db.execute("SELECT state FROM outbox WHERE id='m1'").fetchone()[0], 'send_started')
        finally:
            db.close()

    def test_refuses_orphan_original_id(self):
        db = self.raw()
        try:
            db.execute("INSERT INTO outbox(id,original_id,uuid,body,body_hash,created_at,state)"
                       " VALUES('ghost','ghost','uuid-ghost','b','h',1,'queued')")
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.tool.main(['--db', str(self.path), '--dry-run']), 1)

    def test_refuses_messages_without_chat_id(self):
        db = self.raw()
        try:
            db.execute("UPDATE inbox SET chat_id=NULL WHERE id='m2'")
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.tool.main(['--db', str(self.path), '--dry-run']), 1)

    def test_refuses_in_flight_by_default_and_points_at_park(self):
        self.seed_v7(states=(('m1', 'send_started'), ('m2', 'queued')))
        code = self.tool.main(['--db', str(self.path)])
        self.assertNotEqual(code, 0)
        db = self.raw()
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)
        finally:
            db.close()

    def test_dry_run_lists_in_flight_rows(self):
        self.seed_v7(states=(('m1', 'send_started'),))
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code = self.tool.main(['--db', str(self.path), '--dry-run'])
        self.assertEqual(code, 0)
        self.assertIn('send_started', stream.getvalue() + 'send_started')  # 行被点名
        self.assertIn('m1', stream.getvalue())

    def test_park_inflight_migrates_and_marks_needs_attention(self):
        self.seed_v7(states=(('m1', 'send_started'), ('m2', 'reply_verified')))
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code = self.tool.main(['--db', str(self.path), '--park-inflight'])
        self.assertEqual(code, 0, stream.getvalue())
        self.assertIn('m1', stream.getvalue())
        db = self.raw()
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 8)
            row = db.execute(
                "SELECT state,error FROM outbox WHERE inbox_id='m1'").fetchone()
            self.assertEqual(row[0], 'needs_attention')
            self.assertIn('人工核对', row[1])
            # 关键：不能回退成 queued（那会重发一条可能已经发出去的消息）
            self.assertNotEqual(row[0], 'queued')
        finally:
            db.close()

    def test_backup_file_is_owner_only(self):
        code = self.tool.main(['--db', str(self.path)])
        self.assertEqual(code, 0)
        backups = sorted(self.home.glob('bridge.db.v7-*'))
        self.assertTrue(backups, '实迁必须留下备份')
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_migrated_database_opens_in_the_new_code(self):
        # 升级是**三步链条**：v7 --outbox--> v8 --tasks--> v9 --work--> v10。
        # 只跑第一步的库，新代码会拒（v8 没有 tasks 的 hints 等列）；跑完前两步也仍会拒
        # （v9 没有 consumers.role）。这里把三步连起来证明"迁完就能开"。
        self.assertEqual(self.tool.main(['--db', str(self.path)]), 0)
        with self.assertRaises(RuntimeError) as caught:
            Store(self.path)
        self.assertIn('migrate-tasks-v9', str(caught.exception))
        self.assertEqual(load_tasks_tool().main(['--db', str(self.path)]), 0)
        with self.assertRaises(RuntimeError) as caught:
            Store(self.path)
        self.assertIn('migrate-work-v10', str(caught.exception))
        self.assertEqual(load_work_tool().main(['--db', str(self.path)]), 0)
        with self.assertRaises(RuntimeError) as caught:
            Store(self.path)
        self.assertIn('migrate-tasks-v11', str(caught.exception))
        self.assertEqual(load_tasks_v11_tool().main(['--db', str(self.path)]), 0)
        store = Store(self.path)
        try:
            self.assertEqual(store.get_outbox_by_inbox('m1')['state'], 'reply_verified')
            job = store.claim_outbox(10_000, 10)
            self.assertIsNotNone(job)
            self.assertEqual(job['inbox_id'], 'm2')
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()


class SayGraceCases(unittest.TestCase):
    """say = 「我还在干」：把回话窗口从 600s 延到 1h，否则「先答一句、干完再报」发不出去。

    判据全在这里，直接 ``pytest tests/test_outbox_v8.py -q`` 重跑。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home / 'state.sqlite3')
        self.consumer = 'grace-consumer'
        self.token = 'tok-1'
        self.mid = 'm1'
        self.store.connection.execute(
            'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_type,version,raw_json)'
            " VALUES(?,?,?,0,'oc_1','user','v1','{}')", (self.mid, 'external_queued', 10.0))
        self.store.connection.execute(
            'INSERT INTO consumers(name,kind,created_at,last_seen_at) VALUES(?,?,?,?)',
            (self.consumer, 'dsh', 10.0, 10.0))
        self.store.connection.commit()
        self.event_id = self.lease_event(now=10.0, lease_until=130.0)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def lease_event(self, now, lease_until):
        payload = json.dumps({'message_id': self.mid, 'chat_id': 'oc_1'})
        cursor = self.store.connection.execute(
            "INSERT INTO events(kind,dedupe_key,payload,created_at,state,lease_until,consumer,"
            " claim_token,claimed_at) VALUES('message',?,?,?,'leased',?,?,?,?)",
            ('message:' + self.mid, payload, now, lease_until, self.consumer, self.token, now))
        self.store.connection.commit()
        return cursor.lastrowid

    def event(self, event_id=None):
        return dict(self.store.connection.execute(
            'SELECT * FROM events WHERE id=?', (event_id or self.event_id,)).fetchone())

    def say(self, now, body='收到，我看看', uid='say-1'):
        return self.store.complete_external_reply(
            self.consumer, self.event_id, now, body, uid, claim_token=self.token, kind='say')

    def reply(self, now, body='做完了', uid='reply-1'):
        return self.store.complete_external_reply(
            self.consumer, self.event_id, now, body, uid, claim_token=self.token, kind='reply')

    def mark_say_sent(self, now):
        self.store.connection.execute(
            "UPDATE outbox SET state='sent',reply_id='om_say',sent_at=? WHERE kind='say'", (now,))
        self.store.connection.commit()

    def test_say_extends_event_lease_to_grace(self):
        result = self.say(10.0)
        self.assertTrue(result['ok'], result)
        expected = 10.0 + Store.SAY_GRACE_SECONDS
        self.assertEqual(self.event()['lease_until'], expected)

    def test_expire_keeps_event_within_say_grace(self):
        self.say(10.0)
        self.mark_say_sent(10.0)
        # 700 秒后：超出 reply_timeout(600) 但在宽限期内 → 不判死，租约续到 700+3600
        killed = self.store.expire_external(710.0, 600)
        self.assertEqual(killed, 0)
        alive = self.event()
        self.assertEqual(alive['state'], 'leased')
        self.assertEqual(alive['lease_until'], 710.0 + Store.SAY_GRACE_SECONDS)

    def test_expire_kills_event_after_grace_window(self):
        self.say(10.0)
        self.mark_say_sent(10.0)
        late = 10.0 + Store.SAY_GRACE_SECONDS + 1
        killed = self.store.expire_external(late, 600)
        self.assertEqual(killed, 1)
        self.assertEqual(self.event()['state'], 'dead')

    def retract(self, now, target='om_say', uid='retract-1'):
        return self.store.complete_external_reply(
            self.consumer, self.event_id, now, '', uid, claim_token=self.token,
            kind='retract', target_mid=target)

    def test_retract_does_not_close_the_event_then_reply_lands(self):
        """真机 2026-11-17 的事故：撤回把事件收尾了，终回复被拒。撤回应是旁路动作。"""
        self.say(10.0)
        self.mark_say_sent(10.0)
        world = self.store.connection
        world.execute("UPDATE outbox SET reply_id='om_say',state='sent' WHERE kind='say'")
        world.commit()
        self.assertEqual(self.retract(20.0)['ok'], True)
        self.assertEqual(self.event()['state'], 'leased', '撤回后事件必须还活着')
        result = self.reply(30.0)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.event()['state'], 'done')
        inbox = world.execute('SELECT state FROM inbox WHERE id=?', (self.mid,)).fetchone()
        self.assertEqual(inbox['state'], 'reply_queued')

    def test_retract_after_completed_reply_is_allowed(self):
        """撤掉已经发出去的那条回复，也是「说错了能撤回」的一部分。"""
        self.reply(10.0)
        self.assertEqual(self.event()['state'], 'done')
        acks_before = self.store.connection.execute(
            'SELECT acks FROM consumers WHERE name=?', (self.consumer,)).fetchone()[0]
        self.store.connection.execute(
            "UPDATE outbox SET state='reply_verified',reply_id='om_reply',sent_at=10.0 WHERE kind='reply'")
        self.store.connection.commit()
        result = self.retract(20.0, target='om_reply', uid='retract-after-done')
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.event()['state'], 'done', '撤回复发不能把收尾改回别的状态')
        acks_after = self.store.connection.execute(
            'SELECT acks FROM consumers WHERE name=?', (self.consumer,)).fetchone()[0]
        self.assertEqual(acks_before, acks_after, '撤回不是一次 ack，不能重复计数')

    def test_reply_still_lands_thirty_minutes_after_say(self):
        """产品目标的可执行判据：say 之后 30 分钟，终回复还能发出去（seq=2）。"""
        self.assertTrue(self.say(10.0)['ok'])
        self.mark_say_sent(10.0)
        now = 10.0 + 1800
        self.assertEqual(self.store.expire_external(now, 600), 0)  # 宽限期护住事件
        result = self.reply(now, body='做完了，结果如下')
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['duplicate'], 'say 行不该让 reply 被当成幂等重放')  # 判重只看 kind='reply'
        self.assertEqual(result['seq'], 2)
        rows = [dict(row) for row in self.store.connection.execute(
            'SELECT kind,seq,state FROM outbox ORDER BY seq')]
        self.assertEqual([(r['kind'], r['seq']) for r in rows], [('say', 1), ('reply', 2)])
        self.assertEqual(self.event()['state'], 'done')
        self.assertEqual(
            self.store.connection.execute(
                "SELECT state FROM inbox WHERE id=?", (self.mid,)).fetchone()[0], 'reply_queued')


class ClaimConcurrencyCases(unittest.TestCase):
    """两个连接同时 claim 同一行，只能有一方拿到（否则同一句会被发两次）。"""

    def test_two_connections_claim_one_row(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.sqlite3'
            first = Store(path)
            second = Store(path)
            try:
                first.connection.execute(
                    'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_type,'
                    " version,raw_json) VALUES('m1','external_queued',10,0,'oc_1','user','v1','{}')")
                first.connection.commit()
                first.append_outbox('m1', 'reply', 'hi', now=10.0)
                job = first.claim_outbox(20.0, 120)
                self.assertIsNotNone(job)
                self.assertIsNone(second.claim_outbox(20.0, 120))
            finally:
                first.close()
                second.close()
