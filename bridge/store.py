"""Durable, fenced work queues for the personal Feishu bridge.

Only successful complete search pages advance cursors. All acknowledgements are
conditional on both the attempt token and a live lease, including after restart.
"""

from contextlib import contextmanager
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path


REPLY_UUID_PREFIX = 'feishu-personal-agent-v1:'
_OPEN_CONSUMER_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')


def reply_uuid(message_id, purpose='answer'):
    """Deterministic reply UUID shared by the built-in agent and external consumers.

    幂等键必须带**用途**：同一条消息可能要被回好几次，而且内容不一样——私聊里的判读回执
    （purpose='teach'）、真正的答案（'answer'）、挂起通知（'control'）。只按消息 id 生成时，
    飞书会把第二次当作重复投递直接丢掉，并返回第一次那条消息（真机 16:11 就这样让答案
    永远发不出去，还顺手报了个 "Reply readback mismatch"）。
    """
    return hashlib.sha256((REPLY_UUID_PREFIX + purpose + ':' + message_id).encode()).hexdigest()[:40]


#: outbox v8 的建表语句。**唯一来源**：``Store.__init__`` 与
#: ``tools/migrate-outbox-v8.py`` 都从这里 import，不许写第二份
#: （AGENTS.md §0.3「一条事实一个答案」）。
#:
#: 与 v7 的差别：主键从 ``id``（就是 inbox.id）换成自增 ``row_id``，新增
#: ``inbox_id``（可空=主动消息）/``chat_id``/``seq``/``kind``/``target_mid``/``retracted_at``。
OUTBOX_V8_DDL = """
CREATE TABLE IF NOT EXISTS outbox (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbox_id TEXT REFERENCES inbox(id),
    chat_id TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL CHECK (kind IN ('reply','say','retract')),
    body TEXT NOT NULL DEFAULT '',
    body_hash TEXT NOT NULL DEFAULT '',
    target_mid TEXT,
    uuid TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    next_due REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    verify_attempts INTEGER NOT NULL DEFAULT 0,
    attempt_id TEXT, lease_until REAL, first_send_at REAL,
    reply_id TEXT, sent_at REAL, verified_at REAL, retracted_at REAL, error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS outbox_seq ON outbox(inbox_id, seq) WHERE inbox_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, next_due, created_at);
CREATE TRIGGER IF NOT EXISTS outbox_keep_rows BEFORE DELETE ON outbox
    BEGIN SELECT RAISE(ABORT, 'Outbox rows cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS outbox_immutable
    BEFORE UPDATE OF inbox_id,seq,kind,chat_id,body,body_hash,target_mid,uuid,created_at ON outbox
    WHEN NEW.inbox_id IS NOT OLD.inbox_id OR NEW.seq IS NOT OLD.seq OR NEW.kind IS NOT OLD.kind
      OR NEW.chat_id IS NOT OLD.chat_id OR NEW.body IS NOT OLD.body
      OR NEW.body_hash IS NOT OLD.body_hash OR NEW.target_mid IS NOT OLD.target_mid
      OR NEW.uuid IS NOT OLD.uuid OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT, 'Outbox content is immutable'); END;
"""


def _split_statements(script):
    """把 DDL 拆成单条语句（``executescript`` 会自动 COMMIT，迁移路径上不能用它）。"""
    statements, buf = [], []
    for line in script.splitlines():
        head = line.strip().upper()
        if head.startswith(('CREATE TABLE', 'CREATE UNIQUE INDEX', 'CREATE INDEX', 'CREATE TRIGGER')):
            if buf:
                statements.append('\n'.join(buf).strip())
            buf = [line]
            continue
        buf.append(line)
        if buf and buf[-1].strip().upper() == 'END;':
            statements.append('\n'.join(buf).strip())
            buf = []
    if '\n'.join(buf).strip():
        statements.append('\n'.join(buf).strip())
    return [s for s in statements if s]


def migrate_outbox_to_v8(db):
    """把 v7 形状的 outbox 就地换成 v8（调用方负责事务与备份）。

    ``Store.__init__`` 的版本链（v1..v6 老库）与 ``tools/migrate-outbox-v8.py``（v7 库）
    都调这一个函数：迁移逻辑只有一份。
    """
    columns = [row[1] for row in db.execute('PRAGMA table_info(outbox)')]
    if not columns:
        # 老库可能连 outbox 表都还没有（v4 以前的 fixture）：直接建 v8。
        for statement in _split_statements(OUTBOX_V8_DDL):
            db.execute(statement)
        db.execute('PRAGMA user_version=8')
        return {'rows': 0, 'states': {}}
    if 'row_id' in columns:
        db.execute('PRAGMA user_version=8')
        return {'rows': db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 'states': {}}
    rows = db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
    states = dict(db.execute('SELECT state,COUNT(*) FROM outbox GROUP BY state').fetchall())
    db.execute('DROP TRIGGER IF EXISTS outbox_keep_rows')
    db.execute('DROP TRIGGER IF EXISTS outbox_immutable')
    db.execute('DROP INDEX IF EXISTS outbox_due')
    db.execute('ALTER TABLE outbox RENAME TO outbox_v7')
    for statement in _split_statements(OUTBOX_V8_DDL):
        db.execute(statement)
    missing_chat = db.execute(
        'SELECT o.id FROM outbox_v7 o LEFT JOIN inbox i ON i.id = o.original_id '
        'WHERE i.chat_id IS NULL').fetchall()
    if missing_chat:
        raise RuntimeError('这些消息没有 chat_id，迁移后 outbox.chat_id 会是空：%s'
                           % ','.join(row[0] for row in missing_chat[:5]))
    mismatched = db.execute('SELECT COUNT(*) FROM outbox_v7 WHERE id IS NOT original_id').fetchone()[0]
    if mismatched:
        raise RuntimeError('v7 里有 %d 行 id != original_id，迁移规则不成立' % mismatched)
    db.execute(
        'INSERT INTO outbox (inbox_id,chat_id,seq,kind,body,body_hash,target_mid,uuid,'
        'created_at,state,next_due,attempts,verify_attempts,attempt_id,lease_until,'
        'first_send_at,reply_id,sent_at,verified_at,retracted_at,error) '
        "SELECT o.original_id,i.chat_id,1,'reply',o.body,o.body_hash,NULL,o.uuid,"
        'o.created_at,o.state,o.next_due,o.attempts,o.verify_attempts,NULL,NULL,'
        'o.first_send_at,o.reply_id,o.sent_at,o.verified_at,NULL,o.error '
        'FROM outbox_v7 o JOIN inbox i ON i.id = o.original_id')
    if db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] != rows:
        raise RuntimeError('迁移前后 outbox 行数不一致')
    if dict(db.execute('SELECT state,COUNT(*) FROM outbox GROUP BY state').fetchall()) != states:
        raise RuntimeError('迁移前后 outbox 状态分布不一致')
    if db.execute('SELECT COUNT(*) FROM (SELECT inbox_id FROM outbox '
                  'GROUP BY inbox_id HAVING COUNT(*)>1)').fetchone()[0]:
        raise RuntimeError('有消息被迁成了多行')
    if db.execute('SELECT COUNT(*) FROM outbox WHERE chat_id IS NULL').fetchone()[0]:
        raise RuntimeError('迁移后有 chat_id 为空的行')
    db.execute('DROP TABLE outbox_v7')
    db.execute('PRAGMA user_version=8')
    return {'rows': rows, 'states': states}


#: v9 给 tasks 加的 8 列（设计 §5.2）。全新库的建表语句里也写着同样 8 列——
#: **两处写、一处测**：``tests/test_tasks_v9_migration.py`` 的 schema 一致性用例
#: 把「全新安装」与「v8 迁移」两条路的 ``PRAGMA table_info(tasks)`` 钉成同一个答案。
TASKS_V9_COLUMNS = (
    ('origin_mid', 'TEXT'),
    ('session_id', 'TEXT'),
    ('executor', 'TEXT'),
    ('hints', "TEXT NOT NULL DEFAULT '[]'"),
    ('approval_mid', 'TEXT'),
    ('approval_kind', 'TEXT'),
    ('approved_at', 'REAL'),
    ('started_at', 'REAL'),
)


def migrate_tasks_to_v9(db):
    """v8 → v9：只给 ``tasks`` 补 8 列（**不碰 consumers**，角色路由属 S3a）。调用方负责事务。

    按 ``PRAGMA table_info`` 逐列去重：全新库的 DDL 已经含这些列、v1..v4 老库的 tasks 也是
    这次新建的，两种情况都会走到这里，不该因为"列已存在"报 duplicate column name。

    迁移后立刻断言列齐全：半迁移的库比没迁移更危险——老代码读不到列会当成空数组。
    """
    added = []
    for name, ddl in TASKS_V9_COLUMNS:
        have = {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
        if name not in have:
            db.execute('ALTER TABLE tasks ADD COLUMN %s %s' % (name, ddl))
            added.append(name)
    columns = [row[1] for row in db.execute('PRAGMA table_info(tasks)')]
    missing = [name for name, _ in TASKS_V9_COLUMNS if name not in columns]
    if missing:
        raise RuntimeError('迁移后 tasks 仍缺列：%s' % ','.join(missing))
    db.execute('PRAGMA user_version=9')
    return {'added': added, 'columns': columns}


#: v10 给 consumers 加的一列（S3a 设计 §5.3）：**谁领什么，由角色决定，不看消费者名字**。
#: 默认 ``replier``（回话端），干活端注册时写 ``worker``——老消费者不需要改任何东西，
#: 它们注册进来的都是 replier，与 S3a 之前的行为完全一致。
CONSUMERS_V10_COLUMNS = (
    ('role', "TEXT NOT NULL DEFAULT 'replier'"),
)

#: 角色 → 允许领取的事件类型（**一处定义**，Store.claim_events / CLI / gateway 共用）。
#: 为什么必须收死：S3a 之前任何消费者都能领任何事件，于是回话端会抢到要动手的活，
#: 240 秒后被租约砍断，记账记成"失败"——那是最容易出、也最难查的一类事故。
ROLE_EVENT_KINDS = {
    'replier': ('message', 'attention'),
    'worker': ('work',),
}


#: 库版本落后时该跑哪一步、以及这一步之后还有什么（下界拒绝文案的唯一来源）。
#: 链：v7 →(outbox v8)→ v8 →(tasks v9)→ v9 →(consumers v10)→ v10 →(tasks v11)→ v11。
_MIGRATION_CHAIN = {
    7: ('tools/migrate-outbox-v8.py',
        '（迁到 v8 之后依次跑 python3 tools/migrate-tasks-v9.py、python3 tools/migrate-work-v10.py）'),
    8: ('tools/migrate-tasks-v9.py', '（迁到 v9 之后跑 python3 tools/migrate-work-v10.py）'),
    9: ('tools/migrate-work-v10.py', ''),
    10: ('tools/migrate-tasks-v11.py', ''),
}


def migrate_consumers_to_v10(db):
    """v9 → v10：只给 ``consumers`` 补 ``role``（**不碰 events/tasks**）。调用方负责事务。

    按 ``PRAGMA table_info`` 去重：全新库的 DDL 已经含这一列，v1..v4 老库的 consumers 是
    这次新建的，两种情况都会走到这里，不该因为"列已存在"报 duplicate column name。

    加列的位置是表尾——全新库的建表语句必须**同样把它写在最后一列**，否则两条路的
    ``PRAGMA table_info`` 不一致（``tests/test_work_v10_migration.py`` 钉住这件事）。
    """
    added = []
    for name, ddl in CONSUMERS_V10_COLUMNS:
        have = {row[1] for row in db.execute('PRAGMA table_info(consumers)')}
        if name not in have:
            db.execute('ALTER TABLE consumers ADD COLUMN %s %s' % (name, ddl))
            added.append(name)
    columns = [row[1] for row in db.execute('PRAGMA table_info(consumers)')]
    missing = [name for name, _ in CONSUMERS_V10_COLUMNS if name not in columns]
    if missing:
        raise RuntimeError('迁移后 consumers 仍缺列：%s' % ','.join(missing))
    db.execute('PRAGMA user_version=10')
    return {'added': added, 'columns': columns}


#: v11：任务卡正文。批准之后干活端要拿**主人批的那张卡**当任务书，而不是群里那条来源消息——
#: 来源消息可能是几分钟前毫不相干的一句话（2026-11-19 真机：模型照着它去干了另一件活）。
TASKS_V11_COLUMNS = (('card', 'TEXT'),)


def migrate_tasks_to_v11(db):
    """v10 → v11：只给 ``tasks`` 补 ``card``（卡片正文）。调用方负责事务。

    加列位置在表尾；全新库的建表语句必须同样写在最后一列，否则两条路的
    ``PRAGMA table_info`` 不一致（``tests/test_tasks_v11_migration.py`` 钉住）。
    """
    added = []
    have = {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
    for name, ddl in TASKS_V11_COLUMNS:
        if name not in have:
            db.execute('ALTER TABLE tasks ADD COLUMN %s %s' % (name, ddl))
            added.append(name)
    columns = [row[1] for row in db.execute('PRAGMA table_info(tasks)')]
    missing = [name for name, _ in TASKS_V11_COLUMNS if name not in columns]
    if missing:
        raise RuntimeError('迁移后 tasks 仍缺列：%s' % ','.join(missing))
    db.execute('PRAGMA user_version=11')
    return {'added': added, 'columns': columns}


class Store:
    UNCERTAIN_SEND_SECONDS = 3300
    APP_REPLY_BLOCKED_ERROR = 'Application reply-loop budget reached; human review needed'
    #: L1 任务账本的状态机。terminal 之外的都算"还在进行"，会出现在事件载荷里给消费者看。
    TASK_STATES = ('open', 'running', 'blocked', 'done', 'failed', 'cancelled')
    TASK_ACTIVE_STATES = ('open', 'running', 'blocked')
    #: 批准的两档（设计 §7 + 2026-11-17 真机数据）：引用回复，或「只有一张待批」时的
    #: 唯一性批准。第三档不存在——没有唯一性又没引用的「做」是歧义的，不许猜。
    #: 批准是靠哪一档进来的：引用那张卡 / 唯一待批 / 显式命令「执行」。
    #: 审计要能回答「他是怎么批的」，所以这条记在 tasks.approval_kind 单独一列。
    #: 批准是靠哪一档进来的：引用那张卡 / 唯一待批 / 显式命令「执行」 / **主人自己下的写活**
    #: （``owner_direct``：本人下令不必再点头，但必须走干活端——工作树隔离 + 账本 + 审计，
    #: 这是 2026-11-19 事故后补的口子：主人自己的写活曾被回话端直接干，改完仓库还没有任何痕迹）。
    APPROVAL_KINDS = ('quoted', 'unique_pending', 'command', 'owner_direct')
    EVENT_KINDS = ('message', 'attention', 'work')
    EVENT_STATES = ('pending', 'leased', 'done', 'dead')
    #: 当前 schema 版本。加迁移时这里和 ``PRAGMA user_version`` 必须一起改：
    #: 忘了改这里，新库写的版本号会被自己的门禁拒掉（v6 就差点这样）。
    SCHEMA_VERSION = 11
    CONSUMER_KINDS = ('dsh', 'codex', 'other')
    #: 消费者角色（S3a）：回话端 / 干活端。可取的事件类型见模块级 ``ROLE_EVENT_KINDS``。
    CONSUMER_ROLES = ('replier', 'worker')

    DETAIL_FIELDS = frozenset(('chat_id', 'parent_id', 'root_id', 'thread_id',
                              'create_ms', 'version', 'raw_json', 'text',
                              'sender_type', 'sender_id'))

    def __init__(self, path):
        self._lock = threading.RLock()
        #: 每线程一条连接。SQLite 在 ``sqlite3.threadsafety == 1``（本机 CLT 的 Python 3.9.6
        #: 就是这样）下**不允许同一个连接跨线程使用**；共用一条会让 libsqlite3 的堆被写坏，
        #: 实测表现是测试进程随机 SIGSEGV（rc=139），而不是抛异常。``connection`` 因此改成
        #: 只读属性：返回当前线程的那条，第一次访问才创建。
        self._path = str(path)
        self._local = threading.local()
        self._connections = []
        #: 归因日志（turns.jsonl）和状态库放在同一个 home 下；由 store 单独写，
        #: 因为"事件到底成了还是死了"只有这里有权威答案（见 _flush_turns）。
        self.home = Path(path).resolve().parent
        self._pending_turns = []
        self._closed = False
        with self._lock:
            # Reject newer schemas before even changing SQLite's journal mode.
            version = self.connection.execute('PRAGMA user_version').fetchone()[0]
            if version > self.SCHEMA_VERSION:
                self.close()
                raise RuntimeError('Unsupported state schema version %s' % version)
            if version in (7, 8, 9, 10):
                # 下界拒绝：v7 没有 outbox 的 row_id/inbox_id，v8 没有 tasks 的 hints 等列，
                # v9 没有 consumers.role——继续跑会在第一条查询/INSERT 上炸成 "no such column"
                # 或把「没迁移」伪装成「代码有 bug」。v1..v6 不挡：它们在版本链里就地升级
                # （只可能出现在开发机上）。
                self.close()
                step, tail = _MIGRATION_CHAIN[version]
                raise RuntimeError(
                    '状态库还是 v%s，代码要 v%s：先跑 python3 %s --db <库文件>%s' %
                    (version, self.SCHEMA_VERSION, step, tail))
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            self.connection.execute('PRAGMA foreign_keys=ON')
            try:
                self.connection.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id TEXT PRIMARY KEY, name TEXT,
                    present INTEGER NOT NULL DEFAULT 1,
                    miss_count INTEGER NOT NULL DEFAULT 0,
                    last_seen REAL NOT NULL, checked_at REAL,
                    access_ok INTEGER, error TEXT
                );
                CREATE TABLE IF NOT EXISTS scans (
                    name TEXT PRIMARY KEY, watermark_ms INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    start_ms INTEGER, end_ms INTEGER, page_token TEXT
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'details_pending',
                    received_at REAL NOT NULL, next_due REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    attempt_id TEXT, lease_until REAL, claim_version TEXT,
                    chat_id TEXT, parent_id TEXT, root_id TEXT, thread_id TEXT,
                    create_ms INTEGER, version TEXT, raw_json TEXT, text TEXT,
                    sender_type TEXT, sender_id TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS inbox_due ON inbox(state,next_due,received_at);
                CREATE INDEX IF NOT EXISTS inbox_app ON inbox(chat_id,sender_id);
                CREATE TRIGGER IF NOT EXISTS inbox_keep_ids BEFORE DELETE ON inbox
                    BEGIN SELECT RAISE(ABORT, 'Inbox IDs cannot be deleted'); END;
                CREATE TABLE IF NOT EXISTS consumers (
                    name TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    created_at REAL NOT NULL, last_seen_at REAL NOT NULL,
                    claims INTEGER NOT NULL DEFAULT 0, acks INTEGER NOT NULL DEFAULT 0,
                    fails INTEGER NOT NULL DEFAULT 0,
                    -- v10：角色。必须写在表尾，与 migrate_consumers_to_v10 的 ALTER 追加位置
                    -- 一致；两条路（全新 DDL / v9 + ALTER）的 PRAGMA table_info 有用例钉成同一答案。
                    role TEXT NOT NULL DEFAULT 'replier'
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    state TEXT NOT NULL,
                    steps TEXT NOT NULL DEFAULT '[]',
                    artifacts TEXT NOT NULL DEFAULT '[]',
                    open_question TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    -- v9：任务卡与审批（设计 §5.2）。顺序必须与 TASKS_V9_COLUMNS 一致——
                    -- 迁移是 ALTER 追加，追加完的列顺序要与这里逐项相等（有用例钉住）。
                    origin_mid TEXT,
                    session_id TEXT,
                    executor TEXT,
                    hints TEXT NOT NULL DEFAULT '[]',
                    approval_mid TEXT,
                    approval_kind TEXT,
                    approved_at REAL,
                    started_at REAL,
                    -- v11：卡片正文。干活端的任务书取这一列，不取来源消息。
                    card TEXT
                );
                CREATE INDEX IF NOT EXISTS tasks_chat ON tasks(chat_id,state,updated_at);
                CREATE TABLE IF NOT EXISTS task_messages (
                    message_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fire_at_ms INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    name TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    source_message_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'pending',
                    fired_at_ms INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS reminders_due ON reminders(state,fire_at_ms);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                    created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL,
                    consumer TEXT, claim_token TEXT, claimed_at REAL, acked_at REAL,
                    next_due REAL NOT NULL DEFAULT 0, error TEXT
                );
                CREATE INDEX IF NOT EXISTS events_claimable ON events(state,next_due,id);
                CREATE INDEX IF NOT EXISTS events_consumer ON events(consumer);
                ''')
                # outbox 的建表语句单独一份（OUTBOX_V8_DDL），迁移脚本 import 同一份。
                # 只有全新库在这里建；老库的 outbox 在版本链里由 migrate_outbox_to_v8 换掉——
                # 先跑这里会在 v7 形状的旧表上建索引，报 "no such column: inbox_id"。
                if version == 0:
                    for statement in _split_statements(OUTBOX_V8_DDL):
                        self.connection.execute(statement)
                if version < 3:
                    event_columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(events)')}
                    if 'claim_token' not in event_columns:
                        self.connection.execute('ALTER TABLE events ADD COLUMN claim_token TEXT')
                    outbox_columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(outbox)')}
                    if 'verify_attempts' not in outbox_columns:
                        self.connection.execute('ALTER TABLE outbox ADD COLUMN verify_attempts INTEGER NOT NULL DEFAULT 0')
                    # Old leases have no capability token and cannot be acknowledged safely.
                    # Preserve attempt counters and completed events while making them claimable.
                    self.connection.execute("""UPDATE events SET state='pending',lease_until=NULL,
                        consumer=NULL,claimed_at=NULL WHERE state='leased' AND claim_token IS NULL""")
                    self.connection.execute('PRAGMA user_version=3')
                if version < 4:
                    # 内置 analyst 已删除：'analysis_completed' 这个名字说的是不存在的一步。
                    # 语义不变（回复已写入 outbox、等 sender 发出），只把状态名改成它真正的含义。
                    self.connection.execute(
                        "UPDATE inbox SET state='reply_queued' WHERE state='analysis_completed'")
                    self.connection.execute('PRAGMA user_version=4')
                if version < 5:
                    # L1 任务账本：tasks/task_messages 在上面的 executescript 里 CREATE IF NOT EXISTS
                    # 建好（老库也在同一次事务里补上），这里只推进版本号。
                    self.connection.execute('PRAGMA user_version=5')
                if version < 6:
                    # L2-b 归因：claim 时把注入的教材 id 落到事件行上。放在事件行而不是
                    # turns.jsonl，是因为"模型看到了哪几条"是**claim 那一刻**的事实；
                    # 回复回来时再查一遍会跟中途变化的教材对不上。
                    columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(events)')}
                    if 'used_ids' not in columns:
                        self.connection.execute('ALTER TABLE events ADD COLUMN used_ids TEXT')
                    self.connection.execute('PRAGMA user_version=6')
                if version < 7:
                    # 提醒：一次性的到点提醒（本人私聊里说"一小时后提醒我…"）。
                    # 放 SQLite 而不是 jsonl，是因为"到点只发一次"要靠事务抢（fire_at 可能被
                    # 多个线程/进程同时看到），教材那种只追加日志没有这个需求。
                    # 表与索引只在上面的 executescript 里定义一次（CREATE IF NOT EXISTS，老库
                    # 同一次事务里补上），这里只推进版本号——DDL 写两遍就是两处事实。
                    self.connection.execute('PRAGMA user_version=7')
                if version < 8:
                    # 新库（version=0）的 outbox 已由 OUTBOX_V8_DDL 建成 v8；
                    # v1..v6 的老库在版本链里就地换成 v8（迁移逻辑与迁移脚本共用一份）。
                    # **v7 库不在这里动**：它由下界门禁挡住，要求先跑
                    # tools/migrate-outbox-v8.py（那里有停服断言与备份）。
                    if version > 0:
                        migrate_outbox_to_v8(self.connection)
                    self.connection.execute('PRAGMA user_version=8')
                if version < 9:
                    # v8→v9：任务账本加 8 列。
                    # 全新库（version=0）的 tasks 已由上面的 DDL 建全，这里是空操作。
                    migrate_tasks_to_v9(self.connection)
                if version < 10:
                    # v9→v10：consumers 加 role（S3a 的角色路由）。v7/v8 库在门禁处已经被拦下，
                    # 不会走到这里；能到这一步的只有全新库与 v1..v6 老库。
                    migrate_consumers_to_v10(self.connection)
                if version < 11:
                    # v10→v11：tasks 加 card（卡片正文）。v7..v10 库在门禁处已经被拦下，
                    # 能到这一步的只有全新库与 v1..v6 老库。
                    migrate_tasks_to_v11(self.connection)
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                self.close()
                raise

    @property
    def connection(self):
        """当前线程的数据库连接（每线程一条；首次访问时创建）。

        ``check_same_thread`` 保持默认 True：真有跨线程误用会抛 ProgrammingError——
        那是安全网，比段错误好排查一万倍。
        """
        if self._closed:
            raise RuntimeError('store is closed')
        connection = getattr(self._local, 'connection', None)
        if connection is None:
            connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA busy_timeout=30000')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA foreign_keys=ON')
            self._local.connection = connection
            with self._lock:
                self._connections.append(connection)
        return connection

    @contextmanager
    def _transaction(self):
        with self._lock:
            db = self.connection
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                # 事务没成，排队的"这一轮结局"就不成立（事件可能还是 leased），不能记进归因。
                self._pending_turns = []
                raise
            else:
                # 提交之后才写 turns.jsonl：文件 IO 不在事务里，归因失败也不影响数据。
                self._flush_turns()

    @staticmethod
    def _dict(row):
        return dict(row) if row is not None else None

    @staticmethod
    def _live(row, attempt, now, states):
        return (row is not None and row['state'] in states
                and row['attempt_id'] == attempt
                and row['lease_until'] is not None and row['lease_until'] > now)

    @staticmethod
    def _lease_duration(seconds):
        if seconds <= 0:
            raise ValueError('Lease duration must be positive')

    def close(self):
        """关掉**所有**线程的连接；重复调用无害，关掉后访问 connection 会抛 RuntimeError。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connections, self._connections = self._connections, []
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def meta(self, key, default=None):
        with self._lock:
            row = self.connection.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row is not None else default

    def set_meta(self, key, value):
        value_json = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self._transaction() as db:
            db.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, value_json))

    def register_groups(self, groups, now):
        # Materialize and validate before marking anything missing: this method
        # represents exactly one successfully completed inventory.
        inventory = {group['chat_id']: group.get('name') for group in groups}
        if any(not isinstance(chat_id, str) or not chat_id for chat_id in inventory):
            raise ValueError('Group chat_id must be nonempty')
        with self._transaction() as db:
            for row in db.execute('SELECT chat_id,miss_count FROM groups').fetchall():
                if row['chat_id'] not in inventory:
                    misses = row['miss_count'] + 1
                    db.execute('UPDATE groups SET miss_count=?,present=? WHERE chat_id=?',
                               (misses, int(misses < 2), row['chat_id']))
            for chat_id, name in inventory.items():
                db.execute('''INSERT INTO groups(chat_id,name,last_seen) VALUES (?,?,?)
                    ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name,
                    last_seen=excluded.last_seen,present=1,miss_count=0''', (chat_id, name, now))

    def groups(self):
        with self._lock:
            return [dict(row) for row in self.connection.execute('SELECT * FROM groups ORDER BY chat_id')]

    def group_access(self, chat_id, ok, error, now):
        with self._transaction() as db:
            db.execute('UPDATE groups SET checked_at=?,access_ok=?,error=? WHERE chat_id=?',
                       (now, int(bool(ok)), error, chat_id))

    def next_group_probe(self, now, age=3600):
        with self._lock:
            return self._dict(self.connection.execute('''SELECT * FROM groups
                WHERE present=1 AND (checked_at IS NULL OR checked_at<=?)
                ORDER BY checked_at IS NOT NULL,checked_at,chat_id LIMIT 1''', (now - age,)).fetchone())

    def get_scan(self, name):
        with self._lock:
            return self._dict(self.connection.execute('SELECT * FROM scans WHERE name=?', (name,)).fetchone())

    def begin_scan(self, name, start_ms, end_ms):
        if end_ms < start_ms:
            raise ValueError('Scan end precedes start')
        with self._transaction() as db:
            db.execute('''INSERT INTO scans(name,watermark_ms,active,start_ms,end_ms,page_token)
                VALUES (?,?,1,?,?,NULL) ON CONFLICT(name) DO UPDATE SET active=1,
                start_ms=excluded.start_ms,end_ms=excluded.end_ms,page_token=NULL''',
                       (name, start_ms, start_ms, end_ms))

    def ingest_search_page(self, name, ids, next_token, now):
        ids = list(ids)
        if any(not isinstance(mid, str) or not mid for mid in ids):
            raise ValueError('Message IDs must be nonempty strings')
        with self._transaction() as db:
            scan = db.execute('SELECT * FROM scans WHERE name=? AND active=1', (name,)).fetchone()
            if scan is None:
                raise ValueError('Cannot ingest without an active scan')
            db.executemany('INSERT OR IGNORE INTO inbox(id,received_at,next_due) VALUES (?,?,?)',
                           [(mid, now, now) for mid in ids])
            if next_token is None:
                db.execute('UPDATE scans SET watermark_ms=end_ms,active=0,page_token=NULL WHERE name=?', (name,))
            else:
                db.execute('UPDATE scans SET page_token=? WHERE name=?', (next_token, name))

    def ingest_partial_ids(self, name, ids, now):
        """Record IDs from an incomplete search page without advancing its cursor."""
        ids = list(ids)
        if any(not isinstance(mid, str) or not mid for mid in ids):
            raise ValueError('Message IDs must be nonempty strings')
        with self._transaction() as db:
            scan = db.execute('SELECT 1 FROM scans WHERE name=? AND active=1', (name,)).fetchone()
            if scan is None:
                raise ValueError('Cannot ingest without an active scan')
            before = db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]
            db.executemany('INSERT OR IGNORE INTO inbox(id,received_at,next_due) VALUES (?,?,?)',
                           [(mid, now, now) for mid in ids])
            after = db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]
            return after - before

    def enqueue_messages(self, ids, now):
        """把一批消息 id 直接放进 inbox（推送型身份用：长连接把事件送来，不需要搜索扫描）。

        与 :meth:`ingest_partial_ids` 的区别：**不要求有活跃 scan**——推送没有"页"和"游标"，
        它的幂等键就是消息 id 本身（``INSERT OR IGNORE``），重复投递不会重复处理。
        返回 ``{'queued': n, 'duplicates': m}``。
        """
        ids = [mid for mid in (ids or []) if isinstance(mid, str) and mid.startswith('om_')]
        if not ids:
            return {'queued': 0, 'duplicates': 0}
        with self._transaction() as db:
            before = db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]
            db.executemany('INSERT OR IGNORE INTO inbox(id,received_at,next_due) VALUES (?,?,?)',
                           [(mid, now, now) for mid in ids])
            after = db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]
        queued = after - before
        return {'queued': queued, 'duplicates': len(ids) - queued}

    def inbox_pending_count(self):
        """还没走完的消息数（推送通道的健康检查用：入队了却一直不减少 = 没人在消费）。"""
        with self._transaction() as db:
            return db.execute("""SELECT COUNT(*) FROM inbox
                                 WHERE state IN ('details_pending','details_running','external_queued')"""
                              ).fetchone()[0]

    def reset_scan(self, name):
        with self._transaction() as db:
            db.execute('UPDATE scans SET active=0,start_ms=NULL,end_ms=NULL,page_token=NULL WHERE name=?', (name,))

    # --- 挂起开关（bridge.hold）------------------------------------------------
    # 挂起 = 不参与。下面两个方法只做两件事：进入挂起时作废未完成的工作；退出挂起时
    # 把时间下界前移，让关闭窗口在后续回溯里不可能被重新采集。
    #: 没有内置 analyst 了：一条消息处理完就在`external_queued`等外部消费者领取。
    UNFINISHED_INBOX_STATES = ('details_pending', 'details_running', 'external_queued',
                               'needs_attention')
    UNFINISHED_OUTBOX_STATES = ('queued', 'send_started', 'verifying', 'verify_pending',
                                'retry_wait', 'needs_attention', 'outcome_unknown')

    #: 同一条消息的多条出站记录按 ``seq`` 发；**前一条在阻塞集里，后一条不发**。
    #: 放行集 = 到没到终态，不是「成功还是失败」——``needs_attention``/``outcome_unknown``
    #: 也要放行，否则一次校验失败会把这条消息的后续几句永久饿死（HY4 第 1 轮阻断 3）。
    OUTBOX_BLOCKING_STATES = ('queued', 'retry_wait', 'send_started', 'verify_pending', 'verifying')
    OUTBOX_RELEASING_STATES = ('reply_verified', 'sent', 'retracted', 'dead', 'dropped',
                               'needs_attention', 'outcome_unknown')

    #: ``say`` 是「我还在干」，它把这条消息的回话窗口从 ``reply_timeout_seconds``（默认 600s）
    #: 延长到 1 小时：没有它，第 10 分钟说一句「收到」、第 30 分钟回来的终回复必然撞上
    #: 事件已 ``dead`` 或租约已过期，S1 的产品目标「先答一句、干完再报」直接失效。
    #:
    #: 为什么是**机制常量**而不是运维旋钮：它是「一句话能把窗口延长多久」的语义边界，
    #: 跟着 ``reply_timeout_seconds`` 走会让两处配置互相放大（把总超时调大，say 的宽限也跟着
    #: 变成不可预期）；宽限期过后仍无人 reply 就照旧判死，不无限续命。
    SAY_GRACE_SECONDS = 3600.0

    @staticmethod
    def _placeholders(values):
        return ','.join('?' * len(values))

    def drop_pending(self, now, error='Bridge held'):
        """作废全部未完成的工作，返回**作废的条数**（一条消息算一条，不重复计数）。

        挂起语义要求"关闭期间不参与"：残留待办理项若保留，恢复后会集中补答过期消息，
        所以一律清空；数量记入 meta，供回执与状态展示。

        口径（2026-11-04 统一）：条数 = 未完成的 inbox 行（一条消息一行）+ 没有 inbox 行的
        待办事件（attention 类，一件事一行）。**不再把 outbox/events 行数加进来** ——
        一条消息同时有 inbox 行、outbox 行和 events 行，加总会把 15 条说成 30 条
        （`feishu-bridge status` 的 `dropped:15` 与回执里的 30 互相矛盾）。
        回执与明细分开：条数给回执，三类明细记 meta，想看细节查 `hold.dropped_during_hold_detail`。
        """
        with self._transaction() as db:
            states = self.UNFINISHED_INBOX_STATES
            # 一条消息算一条：未完成的 inbox 行，或者"回复已排队、还没发出去"的消息
            # （它们的 inbox 行已是 reply_queued，但 outbox 行仍是未完成 —— 老实现靠
            # outbox 行数把它们算进去，代价是同一条消息被数了两遍）。
            messages = db.execute(
                '''SELECT COUNT(*) FROM inbox i
                   WHERE i.state IN (%s)
                      OR EXISTS (SELECT 1 FROM outbox o WHERE o.inbox_id = i.id
                                  AND o.kind = 'reply' AND o.state IN (%s))'''
                % (self._placeholders(states), self._placeholders(self.UNFINISHED_OUTBOX_STATES)),
                tuple(states) + tuple(self.UNFINISHED_OUTBOX_STATES)).fetchone()[0]
            db.execute('''UPDATE inbox SET state='dropped',error=?,next_due=0,
                lease_until=NULL,attempt_id=NULL WHERE state IN (%s)''' % self._placeholders(states),
                       (error,) + states)
            states = self.UNFINISHED_OUTBOX_STATES
            replies = db.execute('SELECT COUNT(*) FROM outbox WHERE state IN (%s)'
                                 % self._placeholders(states), states).fetchone()[0]
            db.execute('''UPDATE outbox SET state='dropped',error=?,next_due=0,
                lease_until=NULL,attempt_id=NULL WHERE state IN (%s)''' % self._placeholders(states),
                       (error,) + states)
            # attention 事件没有 inbox 行：它们才是"另一类待办"，按事件算一条。
            notices = db.execute("""SELECT COUNT(*) FROM events WHERE state IN ('pending','leased')
                AND kind <> 'message'""").fetchone()[0]
            db.execute("""UPDATE events SET state='dead',error=?,next_due=0,lease_until=NULL
                WHERE state IN ('pending','leased')""", (error,))
        self.set_meta('hold_dropped_detail', {'messages': messages, 'replies': replies, 'notices': notices})
        return messages + notices

    def prune_expired(self, now, older_than_s=7 * 86400, include_done=False, dry_run=False,
                      note='过期清理'):
        """清理过期消息与死信。只动状态和 events 行，绝不删除 inbox/outbox（有触发器守卫）。

        规则：
          - events state='dead' 早于 cutoff → 物理删除（永久失败，不会再重试，纯垃圾）
          - events state='done' 早于 cutoff 且 include_done → 物理删除
            （默认保留：dedupe_key 是幂等依据，采集回溯窗口最长 24 小时，正常情况不必删）
          - inbox 未完成状态早于 cutoff → 'dropped'（和老消息补答一个道理：过期就不该再发作答）
          - outbox 未完成状态早于 cutoff → 'dropped'
          - inbox state='reply_queued' 且对应 outbox 已是 'dead' 早于 cutoff → 'ignored'
            （回复已确定不发，但状态停在 reply_queued 会一直挂在 stats.oldest_pending_at 上）
        """
        cutoff = now - older_than_s
        inbox_states = self.UNFINISHED_INBOX_STATES
        outbox_states = self.UNFINISHED_OUTBOX_STATES
        report = {'cutoff': cutoff, 'cutoff_at': cutoff, 'older_than_s': older_than_s,
                  'dry_run': bool(dry_run), 'note': note,
                  'events_dead_deleted': 0, 'events_done_deleted': 0,
                  'inbox_dropped': 0, 'outbox_dropped': 0, 'inbox_closed_unsent': 0}
        with self._transaction() as db:
            dead_ids = [row['id'] for row in db.execute(
                "SELECT id FROM events WHERE state='dead' AND created_at < ?", (cutoff,))]
            report['events_dead_deleted'] = len(dead_ids)
            done_ids = []
            if include_done:
                done_ids = [row['id'] for row in db.execute(
                    "SELECT id FROM events WHERE state='done' AND created_at < ?", (cutoff,))]
            report['events_done_deleted'] = len(done_ids)

            inbox_ph = self._placeholders(inbox_states)
            stale_inbox = [row['id'] for row in db.execute(
                'SELECT id FROM inbox WHERE state IN (%s) AND received_at < ?' % inbox_ph,
                inbox_states + (cutoff,))]
            outbox_ph = self._placeholders(outbox_states)
            stale_outbox = [row['row_id'] for row in db.execute(
                'SELECT row_id FROM outbox WHERE state IN (%s) AND created_at < ?' % outbox_ph,
                outbox_states + (cutoff,))]
            unsent = [row['id'] for row in db.execute(
                """SELECT i.id FROM inbox i JOIN outbox o ON o.inbox_id=i.id AND o.kind='reply'
                   WHERE i.state='reply_queued' AND o.state='dead' AND i.received_at < ?""",
                (cutoff,))]
            report['inbox_dropped'] = len(stale_inbox)
            report['outbox_dropped'] = len(stale_outbox)
            report['inbox_closed_unsent'] = len(unsent)

            if not dry_run:
                for event_id in dead_ids + done_ids:
                    db.execute('DELETE FROM events WHERE id=?', (event_id,))
                for mid in stale_inbox:
                    db.execute("""UPDATE inbox SET state='dropped',error=?,next_due=0,
                        lease_until=NULL,attempt_id=NULL WHERE id=?""", (note, mid))
                for row_id in stale_outbox:
                    db.execute("""UPDATE outbox SET state='dropped',error=?,next_due=0,
                        lease_until=NULL,attempt_id=NULL WHERE row_id=?""", (note, row_id))
                for mid in unsent:
                    db.execute("""UPDATE inbox SET state='ignored',error=?,lease_until=NULL,
                        attempt_id=NULL WHERE id=?""",
                               (note + '：回复已确定不发送（outbox=dead）', mid))
        return report

    def apply_release_floor(self, floor_ms):
        """恢复：把采集/回溯的时间下界前移到 floor_ms，并重置两张扫描的游标。

        这是"关闭窗口永久不参与"的关键：服务启动时会立刻做一次 24h 回溯
        （``next_backfill = 0``），只有把 epoch 与 backfill 水位一起前移，那段窗口
        才不会被重新捞回来。
        """
        floor_ms = int(floor_ms)
        with self._transaction() as db:
            for key in ('service_epoch_ms', 'backfill_target_ms', 'backfill_position_ms'):
                db.execute('''INSERT INTO meta(key,value) VALUES (?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                           (key, json.dumps(floor_ms)))
            db.execute('''UPDATE scans SET watermark_ms=?,active=0,start_ms=NULL,
                end_ms=NULL,page_token=NULL''', (floor_ms,))

    def claim_message(self, stage, now, lease_seconds):
        if stage != 'details':
            raise ValueError('Unknown message stage')
        self._lease_duration(lease_seconds)
        queue = 'details_pending'
        with self._transaction() as db:
            row = db.execute('''SELECT * FROM inbox WHERE state=? AND next_due<=?
                ORDER BY next_due,received_at,id LIMIT 1''', (queue, now)).fetchone()
            if row is None:
                return None
            db.execute('''UPDATE inbox SET state=?,attempt_id=?,lease_until=?,
                attempts=attempts+1,claim_version=version WHERE id=?''',
                       (stage + '_running', uuid.uuid4().hex, now + lease_seconds, row['id']))
            return dict(db.execute('SELECT * FROM inbox WHERE id=?', (row['id'],)).fetchone())

    def recover(self, now):
        with self._transaction() as db:
            for running, queued in [('details_running', 'details_pending')]:
                db.execute('''UPDATE inbox SET state=?,attempt_id=NULL,lease_until=NULL,
                    claim_version=NULL,next_due=? WHERE state=? AND lease_until<=?''',
                           (queued, now, running, now))
            db.execute('''UPDATE outbox SET state='verify_pending',attempt_id=NULL,
                lease_until=NULL,next_due=? WHERE state='verifying' AND lease_until<=?''', (now, now))
            db.execute('''UPDATE outbox SET state=CASE WHEN first_send_at<=?
                THEN 'outcome_unknown' ELSE 'retry_wait' END,attempt_id=NULL,
                lease_until=NULL,next_due=? WHERE state='send_started' AND lease_until<=?''',
                       (now - self.UNCERTAIN_SEND_SECONDS, now, now))
            # A dispatched send whose window expired can no longer be retried safely;
            # surface it as needing a human instead of leaving the message pending.
            db.execute('''UPDATE inbox SET state='needs_attention',
                error='Send outcome unknown after crash recovery'
                WHERE id IN (SELECT inbox_id FROM outbox
                             WHERE state='outcome_unknown' AND kind='reply' AND inbox_id IS NOT NULL)''')

    def complete_details(self, mid, attempt, now, fields, state='external_queued', error=None, event=None):
        if set(fields) - self.DETAIL_FIELDS:
            raise ValueError('Unsupported detail fields')
        if state not in ('external_queued', 'ignored', 'needs_attention'):
            raise ValueError('Unsupported detail completion state')
        if 'raw_json' in fields and not isinstance(fields['raw_json'], str):
            raise ValueError('raw_json must be a serialized string')
        event_row = self._validate_event(event) if event is not None else None
        with self._transaction() as db:
            row = db.execute('SELECT * FROM inbox WHERE id=?', (mid,)).fetchone()
            if not self._live(row, attempt, now, ('details_running',)):
                return False
            columns = [key + '=?' for key in fields]
            columns += ['state=?', 'error=?', 'next_due=?', 'attempt_id=NULL', 'lease_until=NULL', 'claim_version=NULL']
            db.execute('UPDATE inbox SET ' + ','.join(columns) + ' WHERE id=?',
                       list(fields.values()) + [state, error, now, mid])
            if event_row is not None:
                self._insert_event(db, event_row, now)
            return True

    def retry_message(self, mid, attempt, now, error, delay, permanent=False):
        with self._transaction() as db:
            row = db.execute('SELECT * FROM inbox WHERE id=?', (mid,)).fetchone()
            if not self._live(row, attempt, now, ('details_running',)):
                return False
            queued = 'details_pending'
            db.execute('''UPDATE inbox SET state=?,error=?,next_due=?,attempt_id=NULL,
                lease_until=NULL,claim_version=NULL WHERE id=?''',
                       ('needs_attention' if permanent else queued, error, now + max(0, delay), mid))
            return True

    def append_outbox(self, inbox_id, kind, body='', *, chat_id=None, target_mid=None,
                      dedupe_uuid=None, now=None):
        """写一条出站记录，返回 ``(row_id, seq)``。

        - ``inbox_id`` 为 None 表示**主动消息**（不回复某条消息），此时 ``chat_id`` 必填。
        - ``dedupe_uuid`` 是调用方幂等键：同一个键重放返回既有行，不会多发一条。
        - ``chat_id`` 在有 ``inbox_id`` 时**一律以 inbox 为准**。
        """
        if kind not in ('reply', 'say', 'retract'):
            raise ValueError('Unsupported outbox kind %r' % (kind,))
        if now is None:
            now = time.time()
        with self._transaction() as db:
            if inbox_id is None:
                if kind == 'reply':
                    raise ValueError('A reply needs an inbox message')
                if not isinstance(chat_id, str) or not chat_id:
                    raise ValueError('A proactive message needs chat_id')
            else:
                message = db.execute('SELECT * FROM inbox WHERE id=?', (inbox_id,)).fetchone()
                if message is None:
                    raise ValueError('Unknown inbox message %r' % (inbox_id,))
                chat_id = message['chat_id']
                if not chat_id:
                    raise ValueError('Inbox message %r has no chat_id' % (inbox_id,))
            body = body or ''
            body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
            # 幂等键：有 inbox 时按 (消息,seq,kind) 派生；主动消息没有天然身份，
            # 每次生成唯一键（要幂等就由调用方传 dedupe_uuid）。
            if dedupe_uuid:
                uid = dedupe_uuid
            elif inbox_id is not None:
                uid = '%s:%s:%s' % (inbox_id, self._next_outbox_seq(db, inbox_id), kind)
            else:
                uid = 'proactive:%s' % uuid.uuid4().hex
            return self._insert_outbox_row(db, inbox_id=inbox_id, chat_id=chat_id, kind=kind,
                                           body=body, body_hash=body_hash, uid=uid, now=now,
                                           target_mid=target_mid)

    @staticmethod
    def _next_outbox_seq(db, inbox_id):
        """同一条消息的下一句：``seq`` 只在同一个 ``inbox_id`` 里自增（主动消息不在顺序域里）。"""
        return db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM outbox WHERE inbox_id=?',
                          (inbox_id,)).fetchone()[0]

    def _insert_outbox_row(self, db, *, inbox_id, chat_id, kind, body, body_hash, uid, now,
                           target_mid=None):
        """在一个已开启的事务里写一行 outbox，返回 ``(row_id, seq)``。

        发布路径（``complete_external_reply`` / ``publish_say``）与 ``append_outbox`` 共用这一个
        插入实现：seq 算法与 uuid 幂等冲突的兜底只在这里说一次。
        """
        seq = 1 if inbox_id is None else self._next_outbox_seq(db, inbox_id)
        cursor = db.execute(
            'INSERT INTO outbox(inbox_id,chat_id,seq,kind,body,body_hash,target_mid,uuid,'
            'created_at,next_due) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(uuid) DO NOTHING',
            (inbox_id, chat_id, seq, kind, body, body_hash, target_mid, uid, now, now))
        if cursor.rowcount == 1:
            return (cursor.lastrowid, seq)
        existing = db.execute('SELECT row_id,seq FROM outbox WHERE uuid=?', (uid,)).fetchone()
        return (existing['row_id'], existing['seq'])

    def claim_outbox(self, now, lease_seconds):
        """取一条可以发的出站记录：同一条消息里 ``seq`` 最小的那条，且它的前驱已到终态。"""
        self._lease_duration(lease_seconds)
        with self._transaction() as db:
            exhausted = [row['row_id'] for row in db.execute(
                "SELECT row_id FROM outbox WHERE state='verify_pending' AND verify_attempts>=8").fetchall()]
            if exhausted:
                db.executemany(
                    "UPDATE outbox SET state='needs_attention',"
                    "error='Verification retry limit reached',attempt_id=NULL,lease_until=NULL"
                    " WHERE row_id=?", [(row_id,) for row_id in exhausted])
                db.executemany(
                    "UPDATE inbox SET state='needs_attention',error=?"
                    " WHERE id IN (SELECT inbox_id FROM outbox"
                    "              WHERE row_id=? AND kind='reply' AND inbox_id IS NOT NULL)",
                    [('Verification retry limit reached', row_id) for row_id in exhausted])
            db.execute(
                "UPDATE outbox SET state='outcome_unknown',attempt_id=NULL,lease_until=NULL"
                " WHERE kind='reply' AND state IN ('queued','retry_wait') AND reply_id IS NULL"
                "   AND first_send_at IS NOT NULL AND first_send_at<=?",
                (now - self.UNCERTAIN_SEND_SECONDS,))
            db.execute(
                "UPDATE inbox SET state='needs_attention',"
                " error='Send outcome unknown after idempotency window'"
                " WHERE id IN (SELECT inbox_id FROM outbox"
                "              WHERE state='outcome_unknown' AND kind='reply' AND inbox_id IS NOT NULL)")
            blocking = self.OUTBOX_BLOCKING_STATES
            row = db.execute(
                "SELECT o.* FROM outbox o WHERE o.state IN (%s) AND o.next_due<=?"
                " AND (o.state<>'send_started' OR o.lease_until IS NULL OR o.lease_until<?)"
                " AND (o.inbox_id IS NULL OR NOT EXISTS ("
                "      SELECT 1 FROM outbox p WHERE p.inbox_id=o.inbox_id AND p.seq<o.seq"
                "       AND p.state IN (%s)))"
                " ORDER BY o.next_due, COALESCE(o.inbox_id,''), o.seq LIMIT 1"
                % (self._placeholders(blocking), self._placeholders(blocking)),
                blocking + (now, now) + blocking).fetchone()
            if row is None:
                return None
            mode = 'verify' if (row['kind'] == 'reply'
                                and (row['state'] == 'verify_pending'
                                     or row['reply_id'] is not None)) else 'send'
            if mode == 'send':
                # first_send_at is stamped only when a request is actually dispatched
                # (see mark_send_dispatched); pre-dispatch failures must never look uncertain.
                db.execute(
                    "UPDATE outbox SET state='send_started',attempt_id=?,lease_until=?,"
                    "attempts=attempts+1 WHERE row_id=?",
                    (uuid.uuid4().hex, now + lease_seconds, row['row_id']))
            else:
                db.execute(
                    "UPDATE outbox SET state='verifying',attempt_id=?,lease_until=?,"
                    "attempts=attempts+1,verify_attempts=verify_attempts+1 WHERE row_id=?",
                    (uuid.uuid4().hex, now + lease_seconds, row['row_id']))
            result = dict(db.execute('SELECT * FROM outbox WHERE row_id=?', (row['row_id'],)).fetchone())
            result['mode'] = mode
            return result

    def mark_send_dispatched(self, row_id, attempt, now):
        """Fence the exact moment a send request is about to leave this process."""
        with self._transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
            if not self._live(row, attempt, now, ('send_started',)):
                return False
            updated = db.execute(
                "UPDATE outbox SET first_send_at=COALESCE(first_send_at,?)"
                " WHERE row_id=? AND state='send_started' AND attempt_id=? AND lease_until>?",
                (now, row_id, attempt, now))
            return updated.rowcount == 1

    def outbox_sent(self, row_id, attempt, now, reply_id):
        """终回复（kind='reply'）已发出、等地里回读校验。"""
        if not isinstance(reply_id, str) or not reply_id:
            raise ValueError('Reply ID must be nonempty')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
            if row is None or row['kind'] != 'reply':
                return False
            if not self._live(row, attempt, now, ('send_started',)):
                return False
            db.execute(
                "UPDATE outbox SET state='verify_pending',reply_id=?,sent_at=?,next_due=?,"
                "attempt_id=NULL,lease_until=NULL,error=NULL WHERE row_id=?",
                (reply_id, now, now, row_id))
            return True

    def outbox_marked(self, row_id, attempt, now, message_id):
        """say / retract 的成功终态：不校验可见性，**不碰 inbox**。"""
        if not isinstance(message_id, str) or not message_id:
            raise ValueError('Message ID must be nonempty')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
            if row is None or row['kind'] not in ('say', 'retract'):
                return False
            if not self._live(row, attempt, now, ('send_started',)):
                return False
            db.execute(
                "UPDATE outbox SET state='sent',reply_id=?,sent_at=?,"
                "attempt_id=NULL,lease_until=NULL,error=NULL WHERE row_id=?",
                (message_id, now, row_id))
            return True

    def outbox_retracted(self, row_id, attempt, now, target_row_id=None):
        """撤回成功：retract 行进 ``sent``，被撤回的原行记 ``retracted_at``（同一事务）。"""
        with self._transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
            if row is None or row['kind'] != 'retract':
                return False
            if not self._live(row, attempt, now, ('send_started',)):
                return False
            db.execute(
                "UPDATE outbox SET state='sent',sent_at=?,attempt_id=NULL,"
                "lease_until=NULL,error=NULL WHERE row_id=?", (now, row_id))
            if target_row_id is not None:
                updated = db.execute('UPDATE outbox SET retracted_at=? WHERE row_id=?',
                                     (now, target_row_id))
                if updated.rowcount != 1:
                    raise ValueError('Retract target row does not exist')
            return True

    def outbox_verified(self, row_id, attempt, now):
        with self._transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
            if row is None or row['kind'] != 'reply':
                return False
            if not self._live(row, attempt, now, ('verifying',)) or not row['reply_id']:
                return False
            db.execute(
                "UPDATE outbox SET state='reply_verified',verified_at=?,"
                "attempt_id=NULL,lease_until=NULL,error=NULL WHERE row_id=?", (now, row_id))
            if row['inbox_id']:
                db.execute("UPDATE inbox SET state='completed',error=NULL WHERE id=?",
                           (row['inbox_id'],))
            return True

    def fail_outbox(self, row_id, attempt, now, error, delay, permanent=False):
        with self._transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
            if not self._live(row, attempt, now, ('send_started', 'verifying')):
                return False
            if row['kind'] == 'reply':
                if row['reply_id'] is not None:
                    state = 'needs_attention' if permanent else 'verify_pending'
                elif (row['first_send_at'] is not None
                      and now - row['first_send_at'] >= self.UNCERTAIN_SEND_SECONDS):
                    state = 'outcome_unknown'
                else:
                    state = 'needs_attention' if permanent else 'retry_wait'
            else:
                # say/retract 没有「回读校验」这一步：发出去就是成功。重试用尽或超过
                # 幂等窗口都只能交给人工，**绝不把整条任务标成 needs_attention**。
                uncertain = (row['first_send_at'] is not None and not row['reply_id']
                             and now - row['first_send_at'] >= self.UNCERTAIN_SEND_SECONDS)
                state = 'needs_attention' if (permanent or uncertain) else 'retry_wait'
            db.execute(
                "UPDATE outbox SET state=?,error=?,next_due=?,attempt_id=NULL,"
                "lease_until=NULL WHERE row_id=?", (state, error, now + max(0, delay), row_id))
            if state in ('needs_attention', 'outcome_unknown') and row['kind'] == 'reply' and row['inbox_id']:
                db.execute("UPDATE inbox SET state='needs_attention',error=? WHERE id=?",
                           (error, row['inbox_id']))
            return True

    def drop_outbox(self, row_id, attempt, now, error, inbox_error):
        """明确的终态：这条回复本来就不该发（来源消息被撤回或改写）。

        与 ``fail_outbox(permanent=True)`` 的差别只有一点，但很关键：**不产生待办**。
        消息被撤回之后没有「人还能做点什么」——挂进待人工只会让 ``doctor`` 永远 warn，
        而操作者每次看到都得再判断一遍「这条是不是早就没救了」。
        """
        with self._transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
            if not self._live(row, attempt, now, ('send_started', 'verifying')):
                return False
            db.execute(
                "UPDATE outbox SET state='dropped',error=?,next_due=0,attempt_id=NULL,"
                "lease_until=NULL WHERE row_id=?", (error[:500], row_id))
            if row['inbox_id']:
                # 来源消息也必须落终态：否则它还挂在「未完成」里，重试扫描会再挑一次。
                db.execute(
                    "UPDATE inbox SET state='completed',error=?,next_due=0 WHERE id=?",
                    (inbox_error[:500], row['inbox_id']))
            return True

    #: 只有这两类错误是「判据错了」而不是「消息真有问题」：含 @ 的回复曾被逐字比较误判
    #: （见 docs/HISTORY.md 2026-11-18）。修复后由 ``feishu-bridge outbox reverify`` 重判。
    REVERIFY_ERRORS = ('Reply readback', 'Reply mentions')

    def outbox_attention_replies(self, limit=None):
        """列出「发出去但被判成待人工」的终回复，供 reverify 重判（只读）。"""
        query = ("SELECT * FROM outbox WHERE kind='reply' AND state='needs_attention'"
                 " AND reply_id IS NOT NULL AND (error LIKE 'Reply readback%'"
                 " OR error LIKE 'Reply mentions%') ORDER BY row_id")
        with self._transaction() as db:
            rows = db.execute(query + (' LIMIT %d' % int(limit) if limit else '')).fetchall()
            return [dict(row) for row in rows]

    def reverify_outbox(self, row_id, now):
        """把一条被误判的终回复改回 ``reply_verified``（并让来源消息回到 completed）。

        判据是「重判之后确实通过」，不是「人工说它没问题」：所以本方法只做状态迁移，
        **校验必须在外面用同一份 ``verify_reply_reason`` 跑过**。只接受上面那两类错误，
        别的 needs_attention（比如正文真的被改）不许借这个口子洗白。
        """
        with self._transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
            if row is None or row['kind'] != 'reply' or row['state'] != 'needs_attention':
                return False
            if not row['reply_id']:
                return False
            error = row['error'] or ''
            if not error.startswith(self.REVERIFY_ERRORS):
                return False
            db.execute("UPDATE outbox SET state='reply_verified',verified_at=?,error=NULL,"
                       "attempt_id=NULL,lease_until=NULL WHERE row_id=?", (now, row_id))
            if row['inbox_id']:
                db.execute("UPDATE inbox SET state='completed',error=NULL WHERE id=?",
                           (row['inbox_id'],))
                # 修好之后那份待办就没有意义了：同事务关掉，而不是让操作者再清一遍。
                self._close_attention_for(
                    db, row['inbox_id'], now, 'auto-closed: reply verified on reverify')
            return True

    #: 每条需要人工的 inbox 行都对应一条 ``attention`` 事件（dedupe_key 就是这个前缀 + 消息 id）。
    ATTENTION_KEY_PREFIX = 'attention:'

    def _close_attention_for(self, db, inbox_id, now, error):
        """关掉某条 inbox 的配套待办（pending/dead → done）。返回关了几条。

        ``leased`` 不动：那是有消费者正在处理，关掉会把它的 ack 变成非法操作。
        """
        return db.execute(
            """UPDATE events SET state='done',acked_at=?,lease_until=NULL,error=?
                WHERE kind='attention' AND dedupe_key=? AND state IN ('pending','dead')""",
            (now, error[:500], self.ATTENTION_KEY_PREFIX + inbox_id)).rowcount

    def close_stale_attention_events(self, now, dry_run=False):
        """关掉「来源消息已经处理完、待办还挂着」的僵尸事件（历史遗留用）。

        只认来源 ``inbox.state='completed'`` 的：来源仍是 ``needs_attention`` 的待办不许借这个
        口子消失——那是在消灭证据，不是在清理。
        """
        with self._transaction() as db:
            rows = db.execute(
                """SELECT e.id FROM events e JOIN inbox i ON e.dedupe_key='attention:'||i.id
                   WHERE e.kind='attention' AND e.state IN ('pending','dead')
                     AND i.state='completed' ORDER BY e.id""").fetchall()
            if not dry_run:
                for row in rows:
                    db.execute(
                        "UPDATE events SET state='done',acked_at=?,lease_until=NULL,error=?"
                        " WHERE id=? AND state IN ('pending','dead')",
                        (now, 'auto-closed: source inbox already completed', row['id']))
            remaining = db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='attention' AND state IN ('pending','dead')"
            ).fetchone()[0]
            return {'closed': len(rows), 'remaining': remaining}

    def attention_items(self):
        """列出所有待人工项：``needs_attention`` 的消息/出站行 + ``pending/dead`` 的事件。

        顺序是固定的（inbox 按收到时间、事件按 id），因为操作者要照着这张表把 id 粘回来。
        """
        with self._transaction() as db:
            inbox = db.execute(
                "SELECT id,chat_id,error,received_at,text FROM inbox"
                " WHERE state='needs_attention' ORDER BY received_at,id").fetchall()
            # 事件侧：凡是 dead（任何 kind）都要人看一眼；pending 的只有待办通知。
            # dead 的 message/work 事件过去既不列也关不掉，等于在账本上凭空躺着。
            events = db.execute(
                "SELECT id,kind,state,error,created_at,payload,dedupe_key FROM events"
                " WHERE state='dead' OR (kind='attention' AND state='pending') ORDER BY id").fetchall()
            outbox = db.execute(
                "SELECT row_id,kind,state,error,created_at,body FROM outbox"
                " WHERE state='needs_attention' ORDER BY row_id").fetchall()
            return {'inbox': [dict(row) for row in inbox],
                    'outbox': [dict(row) for row in outbox],
                    'events': [dict(row) for row in events]}

    def dismiss_inbox(self, message_id, note, now):
        """把一条待人工消息关成 ``completed``（**明确不是「已回复」**），并关掉配套待办。

        关单要留理由：``error`` 写成 ``dismissed by operator: <note>``，以后看账本能分清
        「这条是回复成功了」还是「人看过了，不值得再管」。
        """
        with self._transaction() as db:
            row = db.execute('SELECT state,error FROM inbox WHERE id=?', (message_id,)).fetchone()
            if row is None:
                return {'ok': False, 'error': '找不到这条消息：%s' % message_id}
            if row['state'] != 'needs_attention':
                return {'ok': False,
                        'error': '这条消息不是待人工状态（当前 %s），不需要关单' % row['state']}
            error = 'dismissed by operator: ' + note
            db.execute("UPDATE inbox SET state='completed',error=? WHERE id=?", (error[:500], message_id))
            closed = self._close_attention_for(db, message_id, now, error)
            return {'ok': True, 'ref': 'inbox:' + message_id, 'before': row['state'],
                    'after': 'completed', 'error_before': row['error'], 'error_after': error,
                    'events_closed': closed}

    def close_outbox(self, row_id, note, now):
        """把一条待人工的出站行关成 ``dropped``，并让来源消息也落终态（**不发、不改 reply_id**）。

        为什么必须连带关来源：来源消息若还留在 ``needs_attention``，``attention list`` 会
        再列一次同一条消息——操作者关掉一条、长出两条，永远不会收敛。
        """
        with self._transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
            if row is None:
                return {'ok': False, 'error': '找不到这条出站行：%s' % row_id}
            if row['state'] != 'needs_attention':
                return {'ok': False,
                        'error': '这条出站行不是待人工状态（当前 %s），不需要关单' % row['state']}
            error = 'closed by operator: ' + note
            db.execute(
                "UPDATE outbox SET state='dropped',error=?,next_due=0,attempt_id=NULL,"
                "lease_until=NULL WHERE row_id=?", (error[:500], row_id))
            closed = 0
            if row['inbox_id']:
                inbox_error = 'no reply will be sent: ' + note
                db.execute("UPDATE inbox SET state='completed',error=?,next_due=0 WHERE id=?",
                           (inbox_error[:500], row['inbox_id']))
                closed = self._close_attention_for(db, row['inbox_id'], now, inbox_error)
            return {'ok': True, 'ref': 'outbox:%d' % row_id, 'before': row['state'],
                    'after': 'dropped', 'error_before': row['error'], 'error_after': error,
                    'events_closed': closed}

    def close_attention_event(self, event_id, note, now):
        """把一条待办事件关成 ``done``（只动事件，不动来源消息）。

        ``dead`` 的事件不论 kind 都能关：message/work 事件重试到放弃之后，同样没有自动
        恢复路径，只能由人决定「到此为止」。``pending`` 仍只认待办通知。
        """
        with self._transaction() as db:
            row = db.execute('SELECT kind,state,error FROM events WHERE id=?', (event_id,)).fetchone()
            if row is None:
                return {'ok': False, 'error': '找不到这个事件：%s' % event_id}
            if row['state'] == 'pending' and row['kind'] != 'attention':
                return {'ok': False, 'error': '事件 %s 不是待办通知（kind=%s）' % (event_id, row['kind'])}
            if row['state'] not in ('pending', 'dead'):
                return {'ok': False,
                        'error': '事件 %s 已经是终态（%s），不需要关单' % (event_id, row['state'])}
            error = 'closed by operator: ' + note
            db.execute("UPDATE events SET state='done',acked_at=?,lease_until=NULL,error=?"
                       " WHERE id=?", (now, error[:500], event_id))
            return {'ok': True, 'ref': 'event:%s' % event_id, 'before': row['state'],
                    'after': 'done', 'error_before': row['error'], 'error_after': error}

    # --- external subscription channel (competing consumers, at-least-once) ---

    @staticmethod
    def _validate_event(event):
        if not isinstance(event, dict) or set(event) != {'kind', 'dedupe_key', 'payload'}:
            raise ValueError('event must be {kind, dedupe_key, payload}')
        kind, dedupe_key, payload = event['kind'], event['dedupe_key'], event['payload']
        if kind not in Store.EVENT_KINDS:
            raise ValueError('Unsupported event kind')
        if not isinstance(dedupe_key, str) or not dedupe_key or len(dedupe_key) > 200:
            raise ValueError('event dedupe_key must be a 1-200 char string')
        if not isinstance(payload, dict):
            raise ValueError('event payload must be an object')
        return kind, dedupe_key, json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _insert_event(db, row, now):
        kind, dedupe_key, payload = row
        return db.execute('''INSERT OR IGNORE INTO events(kind,dedupe_key,payload,created_at)
            VALUES (?,?,?,?)''', (kind, dedupe_key, payload, now)).rowcount == 1

    @staticmethod
    def _event_message_id(payload):
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
        except ValueError:
            return None
        mid = data.get('message_id') if isinstance(data, dict) else None
        return mid if isinstance(mid, str) and mid else None

    def _event_attention(self, db, payload, error, now):
        mid = self._event_message_id(payload)
        if mid:
            db.execute("UPDATE inbox SET state='needs_attention',error=? WHERE id=?", (error[:500], mid))

    def register_consumer(self, name, kind, now, role='replier'):
        """登记（或刷新）一个消费者身份；``role`` 决定它能领哪种事件（默认回话端）。

        重复注册会**同时更新 kind 与 role**：一个消费者改行当干活端时，身份只有一处，
        不需要"先删再注册"（那会丢掉它领过事件的历史）。
        """
        if not isinstance(name, str) or not _OPEN_CONSUMER_RE.match(name):
            raise ValueError('consumer name must match [a-z0-9][a-z0-9._-]{0,63}')
        if kind not in self.CONSUMER_KINDS:
            raise ValueError('Unsupported consumer kind')
        if role not in self.CONSUMER_ROLES:
            raise ValueError('Unsupported consumer role: %s（可用：%s）'
                             % (role, ','.join(self.CONSUMER_ROLES)))
        with self._transaction() as db:
            db.execute('''INSERT INTO consumers(name,kind,created_at,last_seen_at,role) VALUES (?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET kind=excluded.kind,role=excluded.role,
                last_seen_at=excluded.last_seen_at''',
                       (name, kind, now, now, role))
            return self._dict(db.execute('SELECT * FROM consumers WHERE name=?', (name,)).fetchone())

    def consumers(self):
        with self._lock:
            return [dict(row) for row in self.connection.execute('SELECT * FROM consumers ORDER BY name')]

    def forget_consumer(self, name):
        """删掉一行消费者身份（死行清理的正规出口）。

        只删 ``consumers`` 这一个登记表，**不动它领过的事件与回执**：那些是历史，
        删了等于把"谁在什么时候答过什么"抹掉。活跃度判断在 CLI 层（``consumer forget``）。
        """
        with self._transaction() as db:
            return db.execute('DELETE FROM consumers WHERE name=?', (name,)).rowcount == 1

    def emit_event(self, kind, dedupe_key, payload, now):
        row = self._validate_event({'kind': kind, 'dedupe_key': dedupe_key, 'payload': payload})
        with self._transaction() as db:
            return self._insert_event(db, row, now)

    def claim_events(self, consumer, now, limit=5, lease_seconds=120, kind=None, next_attempt_cap=5):
        """领事件。**能领什么由消费者角色决定**（S3a 设计 §5.3），不看消费者名字。

        角色规则只有一处：模块级 ``ROLE_EVENT_KINDS``。``kind`` 是调用方的"我这次只想要
        这一种"；它与角色不允许的类型冲突时**明确失败**——静默返回空会让调用方一直空转，
        是比报错更难查的坏结果。
        """
        if not isinstance(consumer, str) or not _OPEN_CONSUMER_RE.match(consumer):
            raise ValueError('consumer name must match [a-z0-9][a-z0-9._-]{0,63}')
        self._lease_duration(lease_seconds)
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError('limit must be 1..50')
        if kind is not None and kind not in self.EVENT_KINDS:
            raise ValueError('Unsupported event kind')
        with self._transaction() as db:
            row = db.execute('SELECT role FROM consumers WHERE name=?', (consumer,)).fetchone()
            if row is None:
                raise LookupError('consumer is not registered: ' + consumer)
            role = row['role']
            allowed = ROLE_EVENT_KINDS.get(role, ROLE_EVENT_KINDS['replier'])
            if kind is not None and kind not in allowed:
                owners = [name for name, kinds in ROLE_EVENT_KINDS.items() if kind in kinds]
                raise ValueError('角色不匹配：%s 事件要 role=%s，你是 role=%s'
                                 % (kind, '/'.join(owners), role))
            # 领取范围：调用方点名一种就只领那种，否则领本角色可领的所有类型。
            kinds = (kind,) if kind is not None else tuple(allowed)
            marks = ','.join('?' for _ in kinds)
            # Reclaim attempts of a crashed consumer, but stop infinite lease churn.
            # 只算本角色能领的类型：回话端不该把干活端的事件判成"重试到头"。
            exhausted = db.execute('''SELECT id,payload FROM events WHERE state='leased'
                AND lease_until<=? AND attempts>=? AND kind IN (%s)''' % marks,
                                   (now, next_attempt_cap) + kinds).fetchall()
            for row in exhausted:
                db.execute("UPDATE events SET state='dead',error=?,lease_until=NULL WHERE id=?",
                           ('Attempt limit reached before ack', row['id']))
                self._event_attention(db, row['payload'], 'External consumer attempt limit reached', now)
                self._queue_turn(row, 'failed', 'Attempt limit reached before ack')
            query = '''SELECT * FROM events WHERE ((state='pending' AND next_due<=?)
                OR (state='leased' AND lease_until<=?)) AND kind IN (%s)''' % marks
            args = [now, now] + list(kinds)
            query += ' ORDER BY id LIMIT ?'
            args.append(limit)
            rows = db.execute(query, args).fetchall()
            claimed = []
            for row in rows:
                claim_token = uuid.uuid4().hex
                db.execute('''UPDATE events SET state='leased',consumer=?,attempts=attempts+1,
                    lease_until=?,claimed_at=?,claim_token=? WHERE id=?''',
                           (consumer, now + lease_seconds, now, claim_token, row['id']))
                item = dict(row)
                item.update(state='leased', consumer=consumer, attempts=row['attempts'] + 1,
                            claimed_at=now, lease_until=now + lease_seconds, claim_token=claim_token)
                claimed.append(item)
            db.execute('UPDATE consumers SET last_seen_at=?,claims=claims+? WHERE name=?',
                       (now, len(claimed), consumer))
            return claimed

    def set_event_used_ids(self, event_id, used_ids, now=None):
        """claim 时记下"这一轮注入了哪几条教材"（归因的原始事实，见 bridge/turns.py）。

        允许覆盖：同一事件被重试时以**最近一次 claim** 为准——回复用的是最后一次看到的教材。
        """
        payload = json.dumps([str(item) for item in (used_ids or [])], ensure_ascii=False)
        with self._transaction() as db:
            result = db.execute('UPDATE events SET used_ids=? WHERE id=?', (payload, event_id))
            return result.rowcount == 1

    def event_used_ids(self, event_id):
        """事件行上记的教材 id；没记过（老事件/未启用教材）返回空列表。"""
        row = self.connection.execute('SELECT used_ids FROM events WHERE id=?',
                                      (event_id,)).fetchone()
        if row is None or not row['used_ids']:
            return []
        try:
            value = json.loads(row['used_ids'])
        except ValueError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []

    def _queue_turn(self, event, outcome, error=None, task=None):
        """把这一轮的结局排进队列，事务提交后再落盘（不在事务里做文件 IO）。"""
        if event is None:
            return
        payload = self._event_payload_dict(event)
        try:
            used = event['used_ids']
        except (KeyError, IndexError):
            used = None
        try:
            used_ids = json.loads(used) if isinstance(used, str) and used else (used or [])
        except ValueError:
            used_ids = []
        task_id = task.get('id') if isinstance(task, dict) else None
        try:
            consumer = event['consumer']
        except (KeyError, IndexError):
            consumer = None
        self._pending_turns.append({'message_id': payload.get('message_id'),
                                    'event_id': event['id'],
                                    'consumer': consumer,
                                    'chat_id': payload.get('chat_id'),
                                    'outcome': outcome,
                                    'error': error,
                                    'used_ids': used_ids if isinstance(used_ids, list) else [],
                                    'task_id': task_id or (payload.get('task') or {}).get('id')
                                    if isinstance(payload.get('task'), dict) else task_id})

    @staticmethod
    def _event_payload_dict(event):
        try:
            payload = json.loads(event['payload']) if isinstance(event['payload'], str) else {}
        except ValueError:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _flush_turns(self):
        """把攒下的结局写进 turns.jsonl。写不进去只记日志：归因是旁证，不能拖垮主流程。"""
        pending, self._pending_turns = self._pending_turns, []
        if not pending:
            return
        try:
            from . import turns as turn_log
        except ImportError:  # pragma: no cover - 单文件部署时可能没有 bridge 包
            return
        for item in pending:
            try:
                turn_log.record(self.home, **item)
            except Exception:  # noqa: BLE001 - 同上
                logging.getLogger(__name__).warning('turn log write failed', exc_info=True)

    @staticmethod
    def _event_live(event, consumer, claim_token, now):
        return (event is not None and event['state'] == 'leased'
                and bool(claim_token) and event['claim_token'] == claim_token
                and event['consumer'] == consumer and event['lease_until'] is not None
                and event['lease_until'] > now)

    def ack_event(self, consumer, event_id, now, *, claim_token=None):
        """收掉一个"没有回复义务"的事件：``attention`` 与 ``work``。

        ``message`` 不在这里——消息事件的终态只能由"真的回了"来达成（``complete_external_reply``
        或失败路径），否则一条消息会被"确认完成"掉却从头到尾没有回过话。``work`` 恰恰相反：
        它的结论落在 **tasks**（``record_work_result``），不需要往群里发东西（发不发是 PR-4 的事）。
        """
        if not isinstance(consumer, str) or not _OPEN_CONSUMER_RE.match(consumer):
            raise ValueError('consumer name must match [a-z0-9][a-z0-9._-]{0,63}')
        with self._transaction() as db:
            result = db.execute('''UPDATE events SET state='done',acked_at=?,lease_until=NULL,error=NULL
                WHERE id=? AND state='leased' AND kind IN ('attention','work') AND consumer=?
                AND claim_token=? AND lease_until>?''', (now, event_id, consumer, claim_token, now))
            if result.rowcount != 1:
                return False
            db.execute('UPDATE consumers SET acks=acks+1,last_seen_at=? WHERE name=?', (now, consumer))
            return True

    def fail_event(self, consumer, event_id, now, error, retry_delay=30, permanent=False, max_attempts=5,
                   *, claim_token=None):
        if not isinstance(consumer, str) or not _OPEN_CONSUMER_RE.match(consumer):
            raise ValueError('consumer name must match [a-z0-9][a-z0-9._-]{0,63}')
        if not isinstance(error, str) or not error:
            raise ValueError('error text is required')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if not self._event_live(row, consumer, claim_token, now):
                return ''
            dead = bool(permanent) or row['attempts'] >= max_attempts
            db.execute('''UPDATE events SET state=?,error=?,next_due=?,lease_until=NULL
                WHERE id=?''', ('dead' if dead else 'pending', error[:500], now + max(0, retry_delay), event_id))
            db.execute('UPDATE consumers SET fails=fails+1,last_seen_at=? WHERE name=?', (now, consumer))
            if dead and row['kind'] == 'message':
                self._event_attention(db, row['payload'], 'External consumer failed: ' + error[:200], now)
                self._apply_task_step(db, self._event_message_id(row['payload']), now,
                                      {'state': 'failed'}, 'failed', error)
            self._queue_turn(row, 'failed', error)
            return 'dead' if dead else 'pending'

    def complete_external_reply(self, consumer, event_id, now, body, external_uuid, *, claim_token=None,
                                app_daily_cap=20, task=None, kind='reply', target_mid=None):
        """消费者把一条**事件相关**的出站消息写进 outbox（reply / say / retract 共用这条发布路径）。

        差异全部由 ``kind`` 决定，不另写第二条发布逻辑：

        ========  =========================  ==========================  ============  ================
        kind      校验                        判重/幂等                    配额          inbox / 事件
        ========  =========================  ==========================  ============  ================
        reply     claim_token + consumer     同一消息已有 reply 行         app_daily_cap  reply_queued / 事件 done
        say       claim_token + consumer     同一 uuid 即幂等重放           app_daily_cap  **都不动**
        retract   claim_token + consumer     同一 uuid 即幂等重放           不受配额       **都不动**（旁路动作，事件收尾后也能撤）
        ========  =========================  ==========================  ============  ================

        ``say`` 不结束事件：它只是「收到，我还在干」，事件得继续挂在这个消费者名下，否则后面那句
        终回复就没有 claim_token 可用了（设计稿 §1 要的「先答一句、干完再报」会直接做不到）。
        要在一段活干完之后再补话，走 :meth:`publish_say` 的 ``--reply-to`` 那条路。

        ``retract`` 的目标必须能按 ``reply_id`` 反查到本桥发出去的那条，否则返回
        ``code='retract_target_not_found'``（CLI 映射成退出码 4，网关映射成 404）。
        """
        if kind not in ('reply', 'say', 'retract'):
            raise ValueError('Unsupported outbox kind %r' % (kind,))
        if not isinstance(consumer, str) or not _OPEN_CONSUMER_RE.match(consumer):
            raise ValueError('consumer name must match [a-z0-9][a-z0-9._-]{0,63}')
        if not isinstance(external_uuid, str) or not external_uuid.strip():
            raise ValueError('Outbox UUID must be nonempty')
        body = body or ''
        if kind == 'retract':
            if not isinstance(target_mid, str) or not target_mid.strip():
                raise ValueError('A retract needs the target message id')
            body = ''
        elif not isinstance(body, str) or not body.strip():
            raise ValueError('Reply body must be nonempty')
        body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
        with self._transaction() as db:
            event = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            owned_done = (event is not None and event['state'] == 'done'
                          and bool(claim_token) and event['claim_token'] == claim_token
                          and event['consumer'] == consumer)
            if (event is None or event['kind'] != 'message'
                    or not (owned_done or self._event_live(event, consumer, claim_token, now))):
                return {'ok': False, 'reason': 'event is not leased by this consumer'}
            mid = self._event_message_id(event['payload'])
            if not mid:
                return {'ok': False, 'reason': 'event payload has no message_id'}
            message = db.execute('SELECT * FROM inbox WHERE id=?', (mid,)).fetchone()
            if message is None:
                return {'ok': False, 'reason': 'message not found'}
            if kind == 'reply':
                # uuid 被别的消息（或别的 kind）占用了：这是内容冲突，不是幂等重放。
                by_uuid = db.execute('SELECT * FROM outbox WHERE uuid=?',
                                     (external_uuid,)).fetchone()
                if by_uuid is not None and (by_uuid['inbox_id'] != mid
                                            or by_uuid['kind'] != kind):
                    return {'ok': False, 'reason': 'reply UUID already used by another message'}
                existing = db.execute(
                    "SELECT * FROM outbox WHERE inbox_id=? AND kind='reply' ORDER BY seq LIMIT 1",
                    (mid,)).fetchone()
            else:
                # say/retract 的幂等键是调用方给的 uuid：同一条命令重放不多写一行。
                existing = db.execute('SELECT * FROM outbox WHERE uuid=?',
                                      (external_uuid,)).fetchone()
                if existing is not None and existing['kind'] != kind:
                    return {'ok': False,
                            'reason': '%s UUID already used by another outbox row' % kind}
            if existing is not None:
                if kind == 'reply':
                    if (existing['body'] != body or existing['body_hash'] != body_hash
                            or existing['uuid'] != external_uuid):
                        return {'ok': False, 'reason': 'reply content conflicts with immutable outbox'}
                elif kind == 'say' and (existing['body'] != body
                                        or existing['body_hash'] != body_hash):
                    return {'ok': False,
                            'reason': 'say UUID already used with different content'}
                if kind == 'reply':
                    # 幂等重放不再计一次 ack：这个分支在 owned_done 检查之前，重放时事件早已收尾。
                    if not owned_done:
                        db.execute("UPDATE events SET state='done',acked_at=?,lease_until=NULL WHERE id=?", (now, event_id))
                        db.execute('UPDATE consumers SET acks=acks+1,last_seen_at=? WHERE name=?', (now, consumer))
                    self._apply_task_step(db, mid, now, task, 'sent')
                    self._queue_turn(event, 'sent', task=task)
                return {'ok': True, 'duplicate': True, 'message_id': mid, 'kind': kind,
                        'inbox_id': mid, 'row_id': existing['row_id'], 'seq': existing['seq'],
                        'outbox_state': existing['state']}
            if kind == 'retract':
                # 先判目标再判事件状态：事件已经收尾时，人问的其实是「这条到底撤没撤」。
                target = db.execute('SELECT row_id FROM outbox WHERE reply_id=? ORDER BY row_id LIMIT 1',
                                    (target_mid,)).fetchone()
                if target is None:
                    return {'ok': False, 'code': 'retract_target_not_found',
                            'reason': 'target message %s is not a message this bridge sent' % target_mid}
                if db.execute("SELECT 1 FROM outbox WHERE kind='retract' AND target_mid=?"
                              " AND state NOT IN ('dropped','needs_attention','dead')",
                              (target_mid,)).fetchone() is not None:
                    return {'ok': False, 'reason': 'target message has already been retracted'}
            if owned_done and kind != 'retract':
                # retract 是旁路动作：它不改 event / inbox，所以在事件已经收尾之后也必须能撤
                # （「说错了能撤回」包括撤掉已经发出去的那条回复）。say/reply 仍然只能在活事件上发。
                if kind == 'say':
                    return {'ok': False, 'code': 'event_completed',
                            'reason': 'event %s is already completed; add a note with '
                                      '"events say --reply-to <message_id>" instead' % event_id}
                return {'ok': False, 'reason': 'completed event has no durable reply'}
            # BEGIN IMMEDIATE serializes this check with every competing publisher's
            # outbox insert, including consumers using a separate SQLite connection.
            if kind != 'retract' and self.app_reply_blocked(dict(message), now, daily_cap=app_daily_cap):
                db.execute("""UPDATE events SET state='dead',error=?,lease_until=NULL,
                    claim_token=NULL WHERE id=?""", (self.APP_REPLY_BLOCKED_ERROR, event_id))
                self._event_attention(db, event['payload'], self.APP_REPLY_BLOCKED_ERROR, now)
                self._queue_turn(event, 'failed', self.APP_REPLY_BLOCKED_ERROR)
                return {'ok': False, 'reason': self.APP_REPLY_BLOCKED_ERROR}
            if not message['chat_id']:
                return {'ok': False, 'reason': 'message has no chat_id'}
            row_id, seq = self._insert_outbox_row(
                db, inbox_id=mid, chat_id=message['chat_id'], kind=kind, body=body,
                body_hash=body_hash, uid=external_uuid, now=now,
                target_mid=target_mid if kind == 'retract' else None)
            if kind == 'reply':
                db.execute('''UPDATE inbox SET state='reply_queued',attempt_id=NULL,lease_until=NULL,
                    claim_version=NULL,error=NULL WHERE id=?''', (mid,))
            if kind == 'say':
                # say 只是「我还在干」的一句，事件必须继续挂在这个消费者名下：收尾了就没法再 reply，
                # 而设计稿 §1 要的正是「先答一句、干完再报」。
                # 续租只作用于 ``leased``：``pending`` 归别人抢，动它的租约等于把事件藏起来。
                db.execute("UPDATE events SET lease_until=? WHERE id=? AND state='leased'",
                           (now + self.SAY_GRACE_SECONDS, event_id))
            elif kind == 'reply':
                # 只有 reply 才算「这段活干完了」。retract 是旁路纠正动作（撤回自己刚说错的一句），
                # 必须让事件继续挂在原消费者名下，否则「先说一句 → 发现说错 → 撤回 → 干完再报」
                # 这条链在第 3 步就断了（真机 2026-11-17 就是这么断的）。
                db.execute("UPDATE events SET state='done',acked_at=?,lease_until=NULL WHERE id=?", (now, event_id))
                db.execute('UPDATE consumers SET acks=acks+1,last_seen_at=? WHERE name=?', (now, consumer))
                self._apply_task_step(db, mid, now, task, 'sent')
                self._queue_turn(event, 'sent', task=task)
            return {'ok': True, 'duplicate': False, 'message_id': mid, 'kind': kind,
                    'inbox_id': mid, 'row_id': row_id, 'seq': seq, 'outbox_state': 'queued'}

    def publish_say(self, consumer, now, body, external_uuid, *, chat_id=None, inbox_id=None):
        """不经过事件的 ``say``：主动消息（``--chat``）或对已有消息补一句（``--reply-to``）。

        为什么单列一条：没有事件就没有 claim_token 可校验，但发布权一样要落在「已注册的消费者」
        身上；并且它不能碰 inbox 状态（say 只是补充说明，不是对这条消息的答复）。
        """
        if not isinstance(consumer, str) or not _OPEN_CONSUMER_RE.match(consumer):
            raise ValueError('consumer name must match [a-z0-9][a-z0-9._-]{0,63}')
        with self._transaction() as db:
            self._require_consumer(db, consumer)
            return self._publish_say(db, now, body, external_uuid,
                                     chat_id=chat_id, inbox_id=inbox_id)

    def announce(self, now, body, *, chat_id=None, inbox_id=None, uuid=None):
        """桥自己发的一句话：批准回执、卡片过点作废这类**服务进程内**的告知。

        与 :meth:`publish_say` 同一张表、同一条发送路径（出口仍然唯一），差别只有一个：
        不要求"已注册的消费者"。理由是发布权门禁是为了挡住外部调用方；服务进程就是桥本身，
        让它为了说一句话去注册一个假消费者，只会在 ``consumer status`` 里多一个永远不领件的幽灵。

        ``uuid`` 不给就按"同一句话 + 同一目标"派生：重放不会在群里说两遍。
        """
        if not isinstance(body, str) or not body.strip():
            raise ValueError('Announce body must be nonempty')
        scope = inbox_id if inbox_id is not None else chat_id
        if not isinstance(scope, str) or not scope:
            raise ValueError('An announce needs exactly one of chat_id / inbox_id')
        if uuid is None:
            digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
            uuid = hashlib.sha256(('bridge-announce:%s:%s' % (scope, digest))
                                  .encode('utf-8')).hexdigest()[:40]
        with self._transaction() as db:
            return self._publish_say(db, now, body, uuid, chat_id=chat_id, inbox_id=inbox_id)

    def _publish_say(self, db, now, body, external_uuid, *, chat_id, inbox_id):
        """``publish_say`` / ``announce`` 的共用主体：校验目标、幂等、写一行 say。"""
        if not isinstance(body, str) or not body.strip():
            raise ValueError('Say body must be nonempty')
        if not isinstance(external_uuid, str) or not external_uuid.strip():
            raise ValueError('Outbox UUID must be nonempty')
        if (chat_id is None) == (inbox_id is None):
            raise ValueError('A say needs exactly one of chat_id / inbox_id')
        if inbox_id is not None:
            message = db.execute('SELECT * FROM inbox WHERE id=?', (inbox_id,)).fetchone()
            if message is None:
                return {'ok': False, 'code': 'inbox_not_found',
                        'reason': 'message %s is not in this bridge' % inbox_id}
            chat_id = message['chat_id']
            if not chat_id:
                return {'ok': False, 'reason': 'message has no chat_id'}
        elif not isinstance(chat_id, str) or not chat_id:
            return {'ok': False, 'code': 'chat_required', 'reason': 'a proactive say needs chat_id'}
        existing = db.execute('SELECT * FROM outbox WHERE uuid=?', (external_uuid,)).fetchone()
        if existing is not None:
            if existing['kind'] != 'say':
                return {'ok': False, 'reason': 'say UUID already used by another outbox row'}
            return {'ok': True, 'duplicate': True, 'kind': 'say', 'chat_id': existing['chat_id'],
                    'inbox_id': existing['inbox_id'], 'row_id': existing['row_id'],
                    'seq': existing['seq'], 'outbox_state': existing['state']}
        body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
        row_id, seq = self._insert_outbox_row(
            db, inbox_id=inbox_id, chat_id=chat_id, kind='say', body=body,
            body_hash=body_hash, uid=external_uuid, now=now)
        return {'ok': True, 'duplicate': False, 'kind': 'say', 'chat_id': chat_id,
                'inbox_id': inbox_id, 'row_id': row_id, 'seq': seq, 'outbox_state': 'queued'}

    @staticmethod
    def _require_consumer(db, consumer):
        """发布权：只有注册过的消费者能写 outbox（没有事件的路径靠这一条兜住）。"""
        if not isinstance(consumer, str) or not _OPEN_CONSUMER_RE.match(consumer):
            raise ValueError('consumer name must match [a-z0-9][a-z0-9._-]{0,63}')
        if db.execute('SELECT name FROM consumers WHERE name=?', (consumer,)).fetchone() is None:
            raise LookupError(consumer)

    def complete_external_skip(self, consumer, event_id, now, reason, *, claim_token=None):
        """消费者判定「这条不用回」：事件收尾，但**不建 outbox、不发任何消息**。

        为什么需要这条路：群消息里有一大类跟 AI 无关——别人 @ 本人只是「好的/哦哦/收到」
        这类承接。``reply`` 要求正文非空，模型只能编一句，于是拿本人的账号在群里刷客套话
        （2026-11-10 真机：对方 @ 本人「哦哦」，回复「好的，宝哥。有需要随时跟我说」）。
        给它一个诚实的出口，比逼它说废话便宜。

        与 ``reply`` 的差别只有一处：不写 outbox。事件、消息、消费者计数按同一套收尾，
        所以同一个 claim_token 重放是幂等的；已经排了回复的消息不允许再改成不回。
        """
        if not isinstance(consumer, str) or not _OPEN_CONSUMER_RE.match(consumer):
            raise ValueError('consumer name must match [a-z0-9][a-z0-9._-]{0,63}')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('skip reason is required')
        reason = ' '.join(reason.split())[:200]
        with self._transaction() as db:
            event = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            owned_done = (event is not None and event['state'] == 'done'
                          and bool(claim_token) and event['claim_token'] == claim_token
                          and event['consumer'] == consumer)
            if (event is None or event['kind'] != 'message'
                    or not (owned_done or self._event_live(event, consumer, claim_token, now))):
                return {'ok': False, 'reason': 'event is not leased by this consumer'}
            mid = self._event_message_id(event['payload'])
            if not mid:
                return {'ok': False, 'reason': 'event payload has no message_id'}
            message = db.execute('SELECT * FROM inbox WHERE id=?', (mid,)).fetchone()
            if message is None:
                return {'ok': False, 'reason': 'message not found'}
            if db.execute("SELECT 1 FROM outbox WHERE inbox_id=? AND kind='reply' AND state<>'dropped'",
                          (mid,)).fetchone() is not None:
                return {'ok': False, 'reason': '这条消息已经排了回复，不能再标记为不回'}
            if message['state'] == 'ignored':
                return {'ok': True, 'duplicate': True, 'message_id': mid}
            db.execute('''UPDATE inbox SET state='ignored',error=?,next_due=0,attempt_id=NULL,
                lease_until=NULL,claim_version=NULL WHERE id=?''', ('未回复：' + reason, mid))
            db.execute("UPDATE events SET state='done',acked_at=?,lease_until=NULL WHERE id=?",
                       (now, event_id))
            db.execute('UPDATE consumers SET acks=acks+1,last_seen_at=? WHERE name=?', (now, consumer))
            self._queue_turn(event, 'skipped', reason)
            return {'ok': True, 'duplicate': False, 'message_id': mid}

    def _say_extends(self, db, message_id, now, say_grace):
        """这条消息已经被桥 ``say`` 过、且还在宽限期内吗？"""
        if not message_id or not say_grace:
            return False
        return db.execute(
            "SELECT 1 FROM outbox WHERE inbox_id=? AND kind='say' AND state='sent'"
            " AND COALESCE(sent_at,created_at)>? LIMIT 1",
            (message_id, now - say_grace)).fetchone() is not None

    def expire_external(self, now, timeout_seconds, max_attempts=5, say_grace=None):
        """把超时没人回的事件判死；被 ``say`` 续过命的消息在宽限期内不判死。

        ``say_grace`` 默认取 :attr:`SAY_GRACE_SECONDS`。判据是「这条消息有没有一条**已发出**的
        say 落在宽限期内」——只看 ``state='sent'``，排着队还没发出去的不算续命依据。
        宽限期一过，同一段逻辑照旧判死（不无限续命）。
        """
        cutoff = now - max(0.0, float(timeout_seconds))
        grace = self.SAY_GRACE_SECONDS if say_grace is None else max(0.0, float(say_grace))
        with self._transaction() as db:
            rows = db.execute('''SELECT id,payload,attempts FROM events WHERE kind='message'
                AND state IN ('pending','leased') AND created_at<=?''', (cutoff,)).fetchall()
            kept = 0
            for row in rows:
                if self._say_extends(db, self._event_message_id(row['payload']), now, grace):
                    db.execute("UPDATE events SET lease_until=? WHERE id=? AND state='leased'",
                               (now + grace, row['id']))
                    kept += 1
                    continue
                db.execute('''UPDATE events SET state='dead',error=?,lease_until=NULL,next_due=0
                    WHERE id=?''', ('External reply timeout', row['id']))
                self._event_attention(db, row['payload'], 'No external consumer replied within timeout', now)
                self._apply_task_step(db, self._event_message_id(row['payload']), now,
                                      {'state': 'failed'}, 'timeout', 'External reply timeout')
                self._queue_turn(row, 'timeout', 'External reply timeout')
            # 返回值是**判死的条数**（被 say 续命跳过的不算）：调用方只看这个数。
            return len(rows) - kept

    def emit_attention_events(self, now):
        with self._transaction() as db:
            rows = db.execute('''SELECT i.id,i.error,i.state FROM inbox i
                LEFT JOIN events e ON e.dedupe_key='attention:'||i.id
                WHERE i.state='needs_attention' AND e.id IS NULL''').fetchall()
            count = 0
            for row in rows:
                payload = json.dumps({'message_id': row['id'], 'error': row['error'],
                                      'state': row['state']}, ensure_ascii=False)
                count += db.execute('''INSERT OR IGNORE INTO events(kind,dedupe_key,payload,created_at)
                    VALUES ('attention',?,?,?)''', ('attention:' + row['id'], payload, now)).rowcount
            return count

    # ---- L1 任务账本（tasks / task_messages）------------------------------------
    #
    # 为什么不把"这条消息属于哪个任务"塞进 tasks.steps 的 JSON 里查：JSON 里查等于每次全表扫，
    # 而且会出现"steps 说属于 A、映射表说属于 B"两套事实。所以 message→task 只有一个答案：
    # task_messages 表；tasks.steps 只记录"这一步发生了什么"。

    @staticmethod
    def _task(dbc, row):
        """把一行 tasks 变成对外 dict（steps/artifacts 解成对象）。"""
        if row is None:
            return None
        task = dict(row)
        for key in ('steps', 'artifacts', 'hints'):
            try:
                value = json.loads(task.get(key) or '[]')
            except ValueError:
                value = []
            task[key] = value if isinstance(value, list) else []
        # 批准方式只住在列里（v9 的 approval_kind）：``dict(row)`` 原样投影，SQL 查得到。
        # 不要再从 hints 元素里推——那是把审计事实塞进"主人原话"，截断时会被污染。
        return task

    def draft_task(self, chat_id, title, now, *, origin_mid=None, message_id=None, card=None):
        """起草一张任务卡：建任务，状态 ``open``，等主人批准。返回 task dict。

        ``origin_mid`` 是哪条飞书消息提出来的（审计里回答"这活是谁的话引出来的"）；
        ``message_id`` 是消息→任务的映射（同一条消息重复起草不重复挂靠）。
        ``hints`` 从空数组开始：主人还没说话，账本上不能先有"他说的"。
        ``card`` 是**发出去的那张卡片的正文**：批准之后干活端的任务书取它，不取来源消息
        （2026-11-19 真机：来源消息跟卡片无关，模型照着来源消息干了另一件活）。
        """
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError('task chat_id is required')
        if not isinstance(title, str) or not title.strip():
            raise ValueError('task title is required')
        task_id = 'tsk_' + uuid.uuid4().hex[:12]
        with self._transaction() as db:
            db.execute("""INSERT INTO tasks(id,chat_id,title,state,steps,artifacts,hints,
                origin_mid,open_question,attempts,created_at,updated_at,card)
                VALUES (?,?,?,'open','[]','[]','[]',?,NULL,0,?,?,?)""",
                       (task_id, chat_id, title.strip()[:200], origin_mid, now, now,
                        (card or None) if isinstance(card, str) else None))
            if isinstance(message_id, str) and message_id:
                db.execute('INSERT OR IGNORE INTO task_messages(message_id,task_id) VALUES (?,?)',
                           (message_id, task_id))
            return self._task(db, db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def _approval_result(self, db, task_id, *, ok, applied, duplicate):
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        return {'ok': ok, 'applied': applied, 'duplicate': duplicate, 'task': self._task(db, row)}

    def approve_task(self, task_id, *, approval_mid, approval_kind, hints_text, now, executor=None):
        """主人批准：``open → running``，并把他的原话**整段**追加进 ``hints``。

        返回 ``{'ok', 'applied', 'duplicate', 'task'}``：

        - 首次批准：写 ``approval_mid/approval_kind/approved_at/started_at``；
        - 同一条 ``approval_mid`` 重放：``duplicate=True``，不长出第二份 hints、不刷新时间；
        - 任务已不在 ``open``（已批/已拒/已结案）：``ok=False``，一个字都不改。
          状态机只有一个入口，第二次"补充"要走 note，不许在这里偷偷改状态。

        ``hints`` 只追加、不改写、不摘要：元素是 ``{"text": 原话, "at": 时间, "mid": 私聊消息 id}``。
        """
        if approval_kind not in self.APPROVAL_KINDS:
            raise ValueError('Unsupported approval kind')
        if not isinstance(approval_mid, str) or not approval_mid:
            raise ValueError('approval mid is required')
        text = (hints_text or '').strip()[:2000]
        with self._transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return {'ok': False, 'applied': False, 'duplicate': False, 'task': None}
            if row['state'] == 'running' and row['approval_mid'] == approval_mid:
                result = self._approval_result(db, task_id, ok=True, applied=False, duplicate=True)
                # 重放同一条批准：活早就在队列里了。如实回答"这次没有新插"，
                # 不假装又派了一次（否则调用方会以为有两件活在跑）。
                result['work_event_id'], result['work_queued'] = self._work_event_id(db, task_id), False
                return result
            if row['state'] != 'open':
                return self._approval_result(db, task_id, ok=False, applied=False, duplicate=False)
            hints = self._task(db, row)['hints']
            #: 审计字段单独一列：hints 元素只装主人原话 ``{text, at, mid}``，不夹带 kind。
            if not any(isinstance(item, dict) and item.get('mid') == approval_mid for item in hints):
                hints.append({'text': text, 'at': now, 'mid': approval_mid})
            cursor = db.execute("""UPDATE tasks SET state='running',hints=?,approval_mid=?,
                approval_kind=?,approved_at=?,started_at=?,executor=COALESCE(?,executor),
                open_question=NULL,updated_at=?,attempts=attempts+1
                WHERE id=? AND state='open'""",
                                (json.dumps(hints, ensure_ascii=False), approval_mid,
                                 approval_kind, now, now, executor, now, task_id))
            if cursor.rowcount != 1:
                # CAS 没命中（并发下被别人先批了）：不改任何东西，按"没批成"回答。
                return self._approval_result(db, task_id, ok=False, applied=False, duplicate=False)
            # 批准即派活：work 事件和状态改动同一个事务，避免"账上 running、却没人有活可领"。
            event_id, queued = self._enqueue_work(db, task_id, now)
            result = self._approval_result(db, task_id, ok=True, applied=True, duplicate=False)
            result['work_event_id'], result['work_queued'] = event_id, queued
            return result

    #: 活派给干活端时用的去重键：**每次派活一条**，重复批准/重投不会派第二次。
    @staticmethod
    def work_dedupe_key(task_id, attempt=1):
        """第 N 次派活的幂等键。

        ``attempt <= 1`` 用短键（``work:<task_id>``，与 PR-3 已落库的历史事件兼容）；
        被拦住后主人回「执行」会重派，那是**新的一次派活**，所以带上次序号。
        """
        attempt = int(attempt or 1)
        return 'work:' + task_id if attempt <= 1 else 'work:%s:%d' % (task_id, attempt)

    def _work_event_id(self, db, task_id, attempt=1):
        row = db.execute('SELECT id FROM events WHERE kind=\'work\' AND dedupe_key=?',
                         (self.work_dedupe_key(task_id, attempt),)).fetchone()
        return row['id'] if row is not None else None

    def _enqueue_work(self, db, task_id, now):
        """批准时派活：``work`` 事件落库（幂等）。返回 ``(event_id, queued)``。

        payload 是一份**完整的任务包**：干活端不该再去翻群消息或者猜主人补了什么。

        ``requirement`` 取**卡片正文**（``tasks.card``，没有就退回标题）：主人批准的是那张卡，
        卡片才说清了要干什么、分几步、怎么算做完。``origin_text`` 是群里那条来源消息的原文，
        **只供人排查**，不喂给模型当任务书——2026-11-19 真机就出过"模型照着来源消息干了另一件事"。
        ``hints`` 取主人批卡时说的话（``tasks.hints`` 里的 ``text``）。
        """
        # 必须重读一次：调用方手里那行是 UPDATE 之前的快照，hints 刚被追加过。
        task_row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        card = (task_row['card'] or '').strip() if 'card' in task_row.keys() else ''
        requirement = card or (task_row['title'] or '').strip()
        origin_mid = task_row['origin_mid']
        origin_text = ''
        if origin_mid:
            source = db.execute('SELECT text FROM inbox WHERE id=?', (origin_mid,)).fetchone()
            origin_text = (source['text'] if source is not None else '') or ''
        try:
            hints = [item.get('text') for item in json.loads(task_row['hints'] or '[]')
                     if isinstance(item, dict) and item.get('text')]
        except ValueError:
            hints = []
        attempt = int(task_row['attempts'] or 1)
        payload = json.dumps({'task_id': task_id, 'chat_id': task_row['chat_id'],
                              'origin_mid': origin_mid, 'requirement': requirement,
                              'origin_text': origin_text,
                              'hints': hints,
                              'attempt': attempt, 'card_source': 'card' if card else 'title'},
                             ensure_ascii=False)
        key = self.work_dedupe_key(task_id, attempt)
        queued = db.execute('''INSERT OR IGNORE INTO events(kind,dedupe_key,payload,created_at)
            VALUES ('work',?,?,?)''', (key, payload, now)).rowcount
        return self._work_event_id(db, task_id, attempt), bool(queued)

    def set_task_executor(self, task_id, executor, now):
        """领走时记一笔"谁在干"（机器名 + 工作台形态），不动状态。"""
        with self._transaction() as db:
            cursor = db.execute('UPDATE tasks SET executor=?,updated_at=? WHERE id=? AND state=\'running\'',
                                (executor, now, task_id))
            return cursor.rowcount == 1

    def record_work_result(self, task_id, *, state, now, note, artifacts=None,
                           open_question=None):
        """干活端交结果：状态 + 一笔交代（steps）+ 产物，**同一个事务**。

        ``state`` 只能是 ``done`` / ``failed`` / ``blocked``：这三条是"活有结论"的三种落法。
        只从 ``running`` 出发（CAS），迟到或重复的结论不许把已经结案的任务翻回来。
        """
        if state not in ('done', 'failed', 'blocked'):
            raise ValueError('Unsupported work result state: %s' % state)
        note = (note or '').strip()[:500]
        with self._transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return None
            if row['state'] != 'running':
                return None
            steps = self._task(db, row)['steps']
            if note:
                steps.append({'n': len(steps) + 1, 'message_id': 'note:' + uuid.uuid4().hex[:8],
                              'outcome': 'note', 'note': note, 'at_ms': int(now * 1000)})
            paths = json.dumps(list(artifacts or []), ensure_ascii=False)
            question = open_question if state == 'blocked' else None
            db.execute("""UPDATE tasks SET state=?,steps=?,artifacts=?,open_question=?,updated_at=?
                WHERE id=? AND state='running'""",
                       (state, json.dumps(steps, ensure_ascii=False), paths, question, now, task_id))
            return self._task(db, db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def reject_task(self, task_id, *, approval_mid, now):
        """主人说「不做」：``open → cancelled``。返回与 :meth:`approve_task` 同形状。

        拒绝也写 ``approval_mid``（审计要回答"谁叫停的"），但 ``approved_at`` 留空——
        他没批准，账本里不能留下批准的痕迹。重放同一条 mid 是幂等空操作。
        """
        if not isinstance(approval_mid, str) or not approval_mid:
            raise ValueError('approval mid is required')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return {'ok': False, 'applied': False, 'duplicate': False, 'task': None}
            if row['state'] == 'cancelled' and row['approval_mid'] == approval_mid:
                return self._approval_result(db, task_id, ok=True, applied=False, duplicate=True)
            if row['state'] != 'open':
                return self._approval_result(db, task_id, ok=False, applied=False, duplicate=False)
            db.execute("""UPDATE tasks SET state='cancelled',approval_mid=?,updated_at=?
                WHERE id=? AND state='open'""", (approval_mid, now, task_id))
            return self._approval_result(db, task_id, ok=True, applied=True, duplicate=False)

    def note_task(self, task_id, text, now):
        """在账本上记一笔（**不推进状态**）：主人补充说明、或干活端的中途交代。

        落在 ``steps`` 里（那是"发生了什么"的账），``outcome='note'``；
        不动 ``state``、不动 ``hints``（hints 只装主人批准时说的原话）。
        """
        note = (text or '').strip()[:500]
        if not note:
            raise ValueError('task note is required')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return None
            steps = self._task(db, row)['steps']
            steps.append({'n': len(steps) + 1, 'message_id': 'note:' + uuid.uuid4().hex[:8],
                          'outcome': 'note', 'note': note, 'at_ms': int(now * 1000)})
            db.execute('UPDATE tasks SET steps=?,updated_at=? WHERE id=?',
                       (json.dumps(steps, ensure_ascii=False), now, task_id))
            return self._task(db, db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def get_task(self, task_id):
        with self._lock:
            return self._task(self.connection,
                              self.connection.execute('SELECT * FROM tasks WHERE id=?',
                                                      (task_id,)).fetchone())

    def task_by_message(self, message_id):
        """这条消息属于哪个任务（没有就 None）。"""
        if not isinstance(message_id, str) or not message_id:
            return None
        with self._lock:
            row = self.connection.execute('''SELECT t.* FROM tasks t
                JOIN task_messages m ON m.task_id=t.id WHERE m.message_id=?''',
                                          (message_id,)).fetchone()
            return self._task(self.connection, row)

    def task_for_origin(self, origin_mid, states=None):
        """按来源消息找任务（同一条消息重复派活只产生一个任务）。

        ``states`` 给了就只在这些状态里找；没给就找所有未终结的（``TASK_ACTIVE_STATES``）。
        返回 task dict 或 None。
        """
        if not isinstance(origin_mid, str) or not origin_mid:
            return None
        wanted = tuple(states) if states else tuple(self.TASK_ACTIVE_STATES)
        marks = ','.join('?' * len(wanted))
        with self._transaction() as db:
            row = db.execute(
                'SELECT * FROM tasks WHERE origin_mid=? AND state IN (%s) '
                'ORDER BY created_at DESC LIMIT 1' % marks, (origin_mid,) + wanted).fetchone()
        return self._task(db, row) if row else None

    def attach_task_message(self, task_id, message_id, now, chat_id=None):
        """把一条消息挂到已有任务上（幂等）。给出 chat_id 时校验同会话，跨会话串任务拒绝。"""
        if not isinstance(task_id, str) or not task_id:
            raise ValueError('task id is required')
        if not isinstance(message_id, str) or not message_id:
            raise ValueError('message id is required')
        with self._transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if task is None:
                return False
            if chat_id is not None and task['chat_id'] != chat_id:
                return False
            db.execute('INSERT OR IGNORE INTO task_messages(message_id,task_id) VALUES (?,?)',
                       (message_id, task_id))
            db.execute('UPDATE tasks SET updated_at=? WHERE id=?', (now, task_id))
            return True

    # ------------------------------------------------------------------ 提醒
    #: 仍然"活着"的提醒状态：pending 等到点、sending 已抢到还没发完、sent 发过。
    REMINDER_LIVE = ('pending', 'sending')
    #: 上面那个元组展开成 SQL 占位符：状态白名单只写一次，四个查询都引用它。
    _LIVE_MARKS = ','.join('?' * len(REMINDER_LIVE))

    def add_reminder(self, fire_at_ms, text, name, chat_id, source_message_id, created_at_ms):
        """登记一条一次性提醒；同一个来源消息只登记一次（重放不重复）。返回 (行, 是否重复)。"""
        with self._transaction() as db:
            row = db.execute('SELECT * FROM reminders WHERE source_message_id=?',
                             (source_message_id,)).fetchone()
            if row is not None:
                return self._dict(row), True
            cursor = db.execute(
                'INSERT INTO reminders(fire_at_ms,text,name,chat_id,source_message_id,'
                'created_at_ms,state) VALUES(?,?,?,?,?,?,?)',
                (int(fire_at_ms), text, name, chat_id, source_message_id,
                 int(created_at_ms), 'pending'))
            row = db.execute('SELECT * FROM reminders WHERE id=?', (cursor.lastrowid,)).fetchone()
            return self._dict(row), False

    def due_reminders(self, now_ms, limit=20):
        """到点且还没发的提醒（老→新）。"""
        with self._lock:
            rows = self.connection.execute(
                'SELECT * FROM reminders WHERE state=? AND fire_at_ms<=? '
                'ORDER BY fire_at_ms ASC, id ASC LIMIT ?',
                ('pending', int(now_ms), int(limit))).fetchall()
            return [self._dict(row) for row in rows]

    def claim_reminder(self, reminder_id):
        """把一条 pending 抢成 sending；抢不到返回 None（另一个线程/进程已经拿走了）。"""
        with self._transaction() as db:
            cursor = db.execute(
                "UPDATE reminders SET state='sending', attempts=attempts+1 WHERE id=? AND state='pending'",
                (int(reminder_id),))
            if cursor.rowcount != 1:
                return None
            row = db.execute('SELECT * FROM reminders WHERE id=?', (int(reminder_id),)).fetchone()
            return self._dict(row)

    def recover_reminders(self):
        """把卡在 sending 的提醒放回 pending（服务重启时做一次）。

        抢到（sending）之后、标记 sent 之前进程被杀，这条会永远停在 sending：`due_reminders`
        只看 pending，于是它既不响也不报错——静默丢一条提醒，正是本仓不允许的那种降级。
        只有服务会 claim（CLI 只读写不抢），所以启动时无条件放回是安全的。
        """
        with self._transaction() as db:
            cursor = db.execute("UPDATE reminders SET state='pending' WHERE state='sending'")
            return cursor.rowcount

    def finish_reminder(self, reminder_id, state, fired_at_ms=None, error=None):
        with self._transaction() as db:
            db.execute('UPDATE reminders SET state=?, fired_at_ms=?, last_error=? WHERE id=?',
                       (state, None if fired_at_ms is None else int(fired_at_ms),
                        None if error is None else str(error)[:500], int(reminder_id)))

    def reminders(self, live_only=True, chat_id=None, limit=50):
        query = 'SELECT * FROM reminders'
        clauses, params = [], []
        if live_only:
            clauses.append('state IN (%s)' % self._LIVE_MARKS)
            params.extend(self.REMINDER_LIVE)
        if chat_id:
            clauses.append('chat_id=?')
            params.append(chat_id)
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY fire_at_ms ASC, id ASC LIMIT ?'
        params.append(int(limit))
        with self._lock:
            return [self._dict(row) for row in self.connection.execute(query, params).fetchall()]

    def reminder(self, key):
        """按编号或名称找一条还活着的提醒；名称重名时取最早到点的那条。"""
        key = str(key)
        with self._lock:
            if key.isdigit():
                row = self.connection.execute(
                    'SELECT * FROM reminders WHERE id=? AND state IN (%s)' % self._LIVE_MARKS,
                    (int(key),) + self.REMINDER_LIVE).fetchone()
                if row is not None:
                    return self._dict(row)
            row = self.connection.execute(
                'SELECT * FROM reminders WHERE name=? AND state IN (%s) '
                'ORDER BY fire_at_ms ASC, id ASC' % self._LIVE_MARKS,
                (key,) + self.REMINDER_LIVE).fetchone()
            return self._dict(row)

    def cancel_reminder(self, reminder_id, now_ms=None):
        with self._transaction() as db:
            cursor = db.execute(
                "UPDATE reminders SET state='cancelled', fired_at_ms=? WHERE id=? "
                'AND state IN (%s)' % self._LIVE_MARKS,
                (int(now_ms if now_ms is not None else 0), int(reminder_id)) + self.REMINDER_LIVE)
            return cursor.rowcount == 1

    def active_tasks(self, chat_id, limit=5):
        """这个会话里还在进行中的任务（新→旧），给事件载荷用。"""
        if not isinstance(chat_id, str) or not chat_id:
            return []
        placeholders = self._placeholders(self.TASK_ACTIVE_STATES)
        with self._lock:
            rows = self.connection.execute(
                'SELECT * FROM tasks WHERE chat_id=? AND state IN (%s) '
                'ORDER BY updated_at DESC LIMIT ?' % placeholders,
                (chat_id,) + tuple(self.TASK_ACTIVE_STATES) + (int(limit),)).fetchall()
            return [self._task(self.connection, row) for row in rows]

    def tasks(self, state=None, limit=50):
        with self._lock:
            if state is None:
                rows = self.connection.execute(
                    'SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?', (int(limit),)).fetchall()
            else:
                if state not in self.TASK_STATES:
                    raise ValueError('Unsupported task state')
                rows = self.connection.execute(
                    'SELECT * FROM tasks WHERE state=? ORDER BY updated_at DESC LIMIT ?',
                    (state, int(limit))).fetchall()
            return [self._task(self.connection, row) for row in rows]

    @staticmethod
    def _clean_task_update(update):
        """校验消费者报上来的账本更新；坏字段丢掉，而不是让整条回复失败。"""
        if not isinstance(update, dict):
            return {}
        clean = {}
        state = update.get('state')
        if state in Store.TASK_STATES:
            clean['state'] = state
        note = update.get('note')
        if isinstance(note, str) and note.strip():
            clean['note'] = ' '.join(note.split())[:500]
        question = update.get('open_question')
        if isinstance(question, str) and question.strip():
            clean['open_question'] = ' '.join(question.split())[:300]
        artifacts = []
        for item in (update.get('artifacts') or [])[:20]:
            if isinstance(item, str) and item.strip():
                artifacts.append({'path': item.strip()[:500]})
            elif isinstance(item, dict) and isinstance(item.get('path'), str) and item['path'].strip():
                entry = {'path': item['path'].strip()[:500]}
                for key in ('kind', 'sha256'):
                    if isinstance(item.get(key), str) and item[key].strip():
                        entry[key] = item[key].strip()[:128]
                artifacts.append(entry)
        if artifacts:
            clean['artifacts'] = artifacts
        if isinstance(update.get('id'), str) and update['id'].startswith('tsk_'):
            clean['task_id'] = update['id']
        return clean

    def _apply_task_step(self, db, message_id, now, update, outcome, error=None):
        """在一个已开启的事务里记账：只有"还在进行中"的任务才吃这一步。

        已 done/failed/cancelled 的任务不再变化——否则一条迟到的事件能把结案的任务翻回 running。
        """
        if not isinstance(message_id, str) or not message_id:
            return None
        row = db.execute('''SELECT t.* FROM tasks t JOIN task_messages m ON m.task_id=t.id
            WHERE m.message_id=?''', (message_id,)).fetchone()
        if row is None:
            clean_hint = self._clean_task_update(update or {})
            task_id = clean_hint.get('task_id')
            if not task_id:
                return None
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return None
            db.execute('INSERT OR IGNORE INTO task_messages(message_id,task_id) VALUES (?,?)',
                       (message_id, task_id))
        if row['state'] not in self.TASK_ACTIVE_STATES:
            return None
        clean = self._clean_task_update(update or {})
        if outcome != 'sent':
            clean['state'] = clean.get('state') or 'failed'
        return self._append_step(db, row, message_id, now, clean, outcome, error)

    def record_task_step(self, message_id, now, update=None, outcome='sent', error=None):
        """把一条消息的处理结果写进它所属任务的账本（一个事务，失败即回滚）。

        ``outcome`` 是桥自己观察到的事实（sent/failed/timeout），``update`` 是消费者报上来的
        判断（state/note/artifacts/open_question/task_id）。没有任务的消息返回 None。
        默认语义：这一步回复发出去了 = 这个任务做完了；要继续的任务，消费者必须显式报
        state='running' 或 'blocked'。
        """
        clean = self._clean_task_update(update or {})
        with self._transaction() as db:
            row = db.execute('''SELECT t.* FROM tasks t JOIN task_messages m ON m.task_id=t.id
                WHERE m.message_id=?''', (message_id,)).fetchone()
            if row is None:
                task_id = clean.get('task_id')
                if not task_id:
                    return None
                row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
                if row is None:
                    return None
                db.execute('INSERT OR IGNORE INTO task_messages(message_id,task_id) VALUES (?,?)',
                           (message_id, task_id))
            return self._append_step(db, row, message_id, now, clean, outcome, error)

    def _append_step(self, db, task, message_id, now, clean, outcome, error):
        task_id = task['id']
        decoded = self._task(db, task)
        steps = decoded['steps']
        prior_steps = len(steps)
        step = {'n': len(steps) + 1, 'message_id': message_id, 'outcome': outcome,
                'note': clean.get('note') or (error[:500] if error else None),
                'at_ms': int(now * 1000)}
        # 幂等：同一条消息的这一步已经记过就改写它，不再追加（消费者重放、失败后重试都会到这里）。
        for index, existing in enumerate(steps):
            if existing.get('message_id') == message_id:
                step['n'] = existing.get('n') or index + 1
                steps[index] = step
                break
        else:
            steps.append(step)
        artifacts = decoded['artifacts']
        seen = {item.get('path') for item in artifacts if isinstance(item, dict)}
        for item in clean.get('artifacts') or []:
            if item['path'] not in seen:
                artifacts.append(item)
                seen.add(item['path'])
        state = clean.get('state')
        if state is None:
            # 消费者没有回填台账时，桥不替它猜"这活做完了"：
            # - 失败的步骤：这一步确实失败了，任务进 failed（事实）；
            # - 只有一个消息的单步任务且回复发出去了：这句话就是整个活，收口成 done；
            # - 其余（多步任务的中途一步）：状态原样不动，交给 recover_tasks 按停滞处理，
            #   宁可显示"还在做"也不要谎报做完（真机事故：默认 done 会让长活被悄悄结案）。
            if outcome != 'sent':
                state = 'failed'
            elif prior_steps == 0 and task['state'] == 'open':
                state = 'done'
            else:
                state = None
        question = clean.get('open_question')
        if state == 'blocked' and not question:
            question = task['open_question']
        if state != 'blocked':
            question = None
        if state is None:
            db.execute('UPDATE tasks SET steps=?,artifacts=?,updated_at=? WHERE id=?',
                       (json.dumps(steps, ensure_ascii=False),
                        json.dumps(artifacts, ensure_ascii=False), now, task_id))
        else:
            db.execute('''UPDATE tasks SET steps=?,artifacts=?,state=?,open_question=?,updated_at=?,
                attempts=attempts+? WHERE id=?''',
                       (json.dumps(steps, ensure_ascii=False), json.dumps(artifacts, ensure_ascii=False),
                        state, question, now, 1 if state == 'running' else 0, task_id))
        return self._task(db, db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def set_task_state(self, task_id, state, now, open_question=None):
        if state not in self.TASK_STATES:
            raise ValueError('Unsupported task state')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return None
            if state == 'running' and row['state'] not in ('open', 'blocked'):
                # 批准/恢复之外没有"再进 running"的路：已经在跑的、以及 done/failed/cancelled
                # 都不能被一次迟到或重复的调用翻回来。批准本身要走 approve_task（它写审计字段）。
                return None
            question = open_question if state == 'blocked' else None
            db.execute('UPDATE tasks SET state=?,open_question=?,updated_at=? WHERE id=?',
                       (state, question, now, task_id))
            return self._task(db, db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def recover_tasks(self, now, lease_seconds=900, max_attempts=3):
        """停滞的 running 任务回到 open；试太多次转 blocked 等本人拍板。

        回合制里"进程被杀"是常态而不是异常：消费者领走事件后被 kill，任务就会永远停在
        running。这里按 updated_at 判死，attempts 记"一共起过几轮"，不给无限重试。
        """
        cutoff = now - max(0.0, float(lease_seconds))
        with self._transaction() as db:
            rows = db.execute("SELECT * FROM tasks WHERE state='running' AND updated_at<=?",
                              (cutoff,)).fetchall()
            for row in rows:
                attempts = int(row['attempts'] or 0) + 1
                if attempts >= max_attempts:
                    db.execute('''UPDATE tasks SET state='blocked',attempts=?,updated_at=?,
                        open_question=COALESCE(open_question,?) WHERE id=?''',
                               (attempts, now, '这个任务连续 %d 轮没做完，要本人决定怎么继续' % attempts,
                                row['id']))
                else:
                    db.execute('UPDATE tasks SET state=?,attempts=?,updated_at=? WHERE id=?',
                               ('open', attempts, now, row['id']))
            return len(rows)

    def approval_queue(self, limit=20):
        """等主人批的卡，**按发出先后排**（``created_at`` 升序，同刻按 id）。

        编号只有这一个来源：``状态`` 列出来的 1/2/3 和 ``执行 2`` 认的 2 必须是同一张卡，
        否则"第 2 张"就成了两套说法。所以谁都不许自己 ``ORDER BY`` 一遍。
        """
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM tasks WHERE state='open' ORDER BY created_at ASC, id ASC LIMIT ?",
                (int(limit),)).fetchall()
            return [self._task(self.connection, row) for row in rows]

    def blocked_queue(self, limit=20):
        """等主人重新派活的活（``state='blocked'``），排序与 :meth:`approval_queue` 同一口径。

        编号必须和 ``状态`` 列出来的那个 1/2/3 是同一张——否则「执行 2」就有两套说法。
        """
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM tasks WHERE state='blocked' ORDER BY created_at ASC, id ASC LIMIT ?",
                (int(limit),)).fetchall()
            return [self._task(self.connection, row) for row in rows]

    def redispatch_blocked(self, task_id, *, now, max_attempts=3):
        """被拦住的活重派一次：``blocked → running`` + 新的 ``work`` 事件。

        * ``blocked`` 是"人还能做点什么"的状态，所以重派由主人触发（回「执行」），不自动重试；
        * 最多 ``max_attempts`` 次（含首次），到顶就把任务落 ``failed`` 并说清"别派了"——
          反复撞同一堵墙只会烧时间；
        * 返回 ``{'ok','applied','exhausted','attempt','work_event_id','work_queued','task'}``。
        """
        max_attempts = max(1, int(max_attempts or 3))
        with self._transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return {'ok': False, 'applied': False, 'exhausted': False, 'task': None}
            if row['state'] != 'blocked':
                return {'ok': False, 'applied': False, 'exhausted': False,
                        'task': self._task(db, row)}
            if int(row['attempts'] or 0) >= max_attempts:
                note = '重派到第 %d 次仍然卡住，先别派了' % max_attempts
                steps = self._task(db, row)['steps']
                steps.append({'n': len(steps) + 1, 'message_id': 'note:' + uuid.uuid4().hex[:8],
                              'outcome': 'note', 'note': note, 'at_ms': int(now * 1000)})
                db.execute("""UPDATE tasks SET state='failed',steps=?,open_question=NULL,updated_at=?
                    WHERE id=? AND state='blocked'""",
                           (json.dumps(steps, ensure_ascii=False), now, task_id))
                return {'ok': True, 'applied': False, 'exhausted': True,
                        'attempt': int(row['attempts'] or 0),
                        'task': self._task(db, db.execute('SELECT * FROM tasks WHERE id=?',
                                                          (task_id,)).fetchone())}
            cursor = db.execute("""UPDATE tasks SET state='running',open_question=NULL,
                started_at=?,updated_at=?,attempts=attempts+1
                WHERE id=? AND state='blocked'""", (now, now, task_id))
            if cursor.rowcount != 1:
                return {'ok': False, 'applied': False, 'exhausted': False,
                        'task': self._task(db, row)}
            event_id, queued = self._enqueue_work(db, task_id, now)
            fresh = self._task(db, db.execute('SELECT * FROM tasks WHERE id=?',
                                             (task_id,)).fetchone())
            return {'ok': True, 'applied': True, 'exhausted': False,
                    'attempt': int(fresh['attempts'] or 1),
                    'work_event_id': event_id, 'work_queued': queued, 'task': fresh}

    def task_counts(self):
        with self._lock:
            rows = self.connection.execute('SELECT state,COUNT(*) AS n FROM tasks GROUP BY state')
            counts = {row['state']: row['n'] for row in rows}
            return {'states': counts,
                    'active': sum(counts.get(s, 0) for s in self.TASK_ACTIVE_STATES),
                    'blocked': counts.get('blocked', 0)}

    def message_row(self, mid):
        """inbox 里的一条消息（read-only）；不存在返回 None。

        问答回路要用它判断「app 回复配额还剩不剩」——重放事件前先算一次，
        免得排上一个注定被配额挡掉的事件。
        """
        with self._lock:
            return self._dict(self.connection.execute('SELECT * FROM inbox WHERE id=?', (mid,)).fetchone())

    def get_event(self, event_id):
        with self._lock:
            return self._dict(self.connection.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone())

    def list_events(self, consumer=None, states=None, limit=50, newest_first=True):
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError('limit must be 1..200')
        clauses, params = [], []
        if consumer is not None:
            clauses.append('consumer=?')
            params.append(consumer)
        if states:
            states = list(states)
            unknown = [state for state in states if state not in self.EVENT_STATES]
            if unknown:
                raise ValueError('Unsupported event state')
            clauses.append('state IN (%s)' % ','.join('?' * len(states)))
            params.extend(states)
        query = 'SELECT * FROM events'
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY id ' + ('DESC' if newest_first else 'ASC') + ' LIMIT ?'
        with self._lock:
            return [dict(row) for row in self.connection.execute(query, params + [limit])]

    def event_stats(self, now):
        with self._lock:
            states = {row['state']: row['n'] for row in self.connection.execute(
                'SELECT state,COUNT(*) AS n FROM events GROUP BY state')}
            kinds = {row['kind']: row['n'] for row in self.connection.execute(
                'SELECT kind,COUNT(*) AS n FROM events GROUP BY kind')}
            consumers = [dict(row) for row in self.connection.execute(
                'SELECT * FROM consumers ORDER BY name')]
            return {
                'states': states,
                'kinds': kinds,
                'consumers': consumers,
                'oldest_pending_at': self.connection.execute(
                    "SELECT MIN(created_at) FROM events WHERE state IN ('pending','leased')").fetchone()[0],
                'external_queued': self.connection.execute(
                    "SELECT COUNT(*) FROM inbox WHERE state='external_queued'").fetchone()[0],
                'oldest_external_at': self.connection.execute(
                    "SELECT MIN(received_at) FROM inbox WHERE state='external_queued'").fetchone()[0],
            }

    def get_message(self, mid):
        with self._lock:
            return self._dict(self.connection.execute('SELECT * FROM inbox WHERE id=?', (mid,)).fetchone())

    def get_outbox(self, row_id):
        """按 outbox 行号取行；不知道行号时用 :meth:`get_outbox_by_inbox`。"""
        with self._lock:
            return self._dict(self.connection.execute(
                'SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone())

    def get_outbox_by_inbox(self, inbox_id, kind='reply'):
        """按飞书消息 id 取它的某类出站记录（seq 最小的那条）。"""
        with self._lock:
            return self._dict(self.connection.execute(
                'SELECT * FROM outbox WHERE inbox_id=? AND kind=? ORDER BY seq LIMIT 1',
                (inbox_id, kind)).fetchone())

    def get_outbox_by_message_id(self, message_id):
        """按**发出去的**飞书 message_id 反查本库那条出站行（撤回要对账到原行）。

        与 :meth:`get_outbox_by_inbox` 的区别：那个按"来源消息"查，这个按"自己的产出"查。
        撤回流程只有 ``target_mid``（飞书侧消息 id），没有行号，所以必须走这里。
        """
        if not isinstance(message_id, str) or not message_id:
            return None
        with self._lock:
            return self._dict(self.connection.execute(
                'SELECT * FROM outbox WHERE reply_id=? ORDER BY row_id LIMIT 1',
                (message_id,)).fetchone())

    def app_reply_blocked(self, job, now, daily_cap=20):
        """Shared app loop guard; callers publishing replies hold the transaction lock."""
        if job['sender_type'] != 'app':
            return False
        key = (job.get('thread_id') or job.get('root_id') or job.get('parent_id')
               or job.get('message_id') or job['id'])
        with self._lock:
            return (self.app_recent(job['chat_id'], job['sender_id'], now - 600) >= 3
                    or self.app_recent(job['chat_id'], job['sender_id'], now - 86400) >= daily_cap
                    or self.conversation_replies(job['chat_id'], job['sender_id'], key) >= 2)

    def app_recent(self, chat_id, sender_id, since):
        with self._lock:
            return self.connection.execute('''SELECT COUNT(*) FROM inbox i
                JOIN outbox o ON o.inbox_id=i.id AND o.kind='reply'
                WHERE i.chat_id=? AND i.sender_id=? AND o.created_at>=?''', (chat_id, sender_id, since)).fetchone()[0]

    def conversation_replies(self, chat_id, sender_id, conversation_key):
        """Count durable reply intentions across the conversation's full lifetime."""
        with self._lock:
            return self.connection.execute('''SELECT COUNT(*) FROM inbox i
                JOIN outbox o ON o.inbox_id=i.id AND o.kind='reply'
                WHERE i.chat_id=? AND i.sender_id=?
                AND COALESCE(NULLIF(i.thread_id,''),NULLIF(i.root_id,''),NULLIF(i.parent_id,''),i.id)=?''',
                                           (chat_id, sender_id, conversation_key)).fetchone()[0]

    def stats(self):
        with self._lock:
            result = {}
            for table in ('inbox', 'outbox'):
                result[table] = {row['state']: row['n'] for row in self.connection.execute(
                    'SELECT state,COUNT(*) AS n FROM ' + table + ' GROUP BY state')}
            rows = self.connection.execute('SELECT access_ok,COUNT(*) AS n FROM groups WHERE present=1 GROUP BY access_ok').fetchall()
            counts = {row['access_ok']: row['n'] for row in rows}
            result['tasks'] = self.task_counts()
            result['groups'] = {'listed': sum(counts.values()), 'readable': counts.get(1, 0),
                                'unreadable': counts.get(0, 0), 'unchecked': counts.get(None, 0)}
            # dropped = 明确作废（挂起期间/过期清理），不属于「待处理」；否则一次 hold 之后
            # 这个指针会被钉死在一个永远处理不了的旧时间上，看起来像有陈旧堆积。
            result['oldest_pending_at'] = self.connection.execute('''SELECT MIN(i.received_at) FROM inbox i
                LEFT JOIN outbox o ON o.inbox_id=i.id AND o.kind='reply'
                WHERE i.state NOT IN ('ignored','completed','dropped','needs_attention')
                AND COALESCE(o.state,'') NOT IN ('needs_attention','outcome_unknown','dead','dropped','sent')''').fetchone()[0]
            result['attention'] = {
                'inbox_needs_attention': self.connection.execute(
                    "SELECT COUNT(*) FROM inbox WHERE state='needs_attention'").fetchone()[0],
                'outbox_needs_attention': self.connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE state='needs_attention'").fetchone()[0],
                'outcome_unknown': self.connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE state='outcome_unknown'").fetchone()[0],
                'oldest_attention_at': self.connection.execute('''SELECT MIN(t) FROM (
                    SELECT received_at AS t FROM inbox WHERE state='needs_attention'
                    UNION ALL
                    SELECT COALESCE(sent_at,created_at) AS t FROM outbox
                    WHERE state IN ('needs_attention','outcome_unknown'))''').fetchone()[0],
                'oldest_outcome_unknown_at': self.connection.execute(
                    "SELECT MIN(COALESCE(sent_at,created_at)) FROM outbox WHERE state='outcome_unknown'").fetchone()[0],
                'external_queued': self.connection.execute(
                    "SELECT COUNT(*) FROM inbox WHERE state='external_queued'").fetchone()[0],
                'events_dead': self.connection.execute(
                    "SELECT COUNT(*) FROM events WHERE state='dead'").fetchone()[0],
            }
            result['scans'] = [dict(row) for row in self.connection.execute('SELECT * FROM scans ORDER BY name')]
            result['events'] = self.event_stats(time.time())
            return result
