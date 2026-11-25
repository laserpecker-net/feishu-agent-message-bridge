"""记忆检索（L2-a）：把教材从"每轮全量注入"改成"按这条消息检索相关的那几条"。

三层事实，各归各的：

1. **真身**是 ``home/teachings.jsonl``（追加日志，只由 ``bridge/teach.py`` 读写）；
2. **派生索引**是 ``home/memory.sqlite3``：可以随时删掉、下次自动重建，删掉不丢任何记忆；
3. **这一轮注入的内容**是 ``retrieve()`` 的返回值，形状仍是 ``teachings`` 块（charter/notes），
   所以 `agent_prompt.teaching_section` 的渲染和"六个渲染点共用同一段文本"的约束不变。

为什么检索而不是全量：教材会越攒越多，全量注入会把 prompt 预算吃光，还会让不相关的旧条目
干扰判断。检索的硬规则是——**准则（principle/style）必须全给**，资料（fact/pointer）只给
和这条消息相关的；查不到相关资料的轮次，就一条资料都不给（不做"象征性塞几条"）。

索引为什么用自造的中文分词：SQLite 的 FTS5 默认分词器把一串汉字当成**一个** token，
"本月收入合计" 和查询 "收入" 匹配不上；装 trigram 分词器又依赖 SQLite 版本（Windows 侧
未验证，见设计稿 §8.3）。所以这里自己切：汉字按二元组、英文数字按词，存进 ``tokens`` 列。
没有 FTS5 模块的 SQLite 会退化成普通表 + Python 扫描（``mode`` 字段会如实报出来）。
"""

import hashlib
import math
import re
import sqlite3
import time
from pathlib import Path

from agent_prompt import TEACHING_MAX_CHARS

from . import teach as teach_log

INDEX_NAME = 'memory.sqlite3'
#: 一次检索最多给多少条资料；再多就该本人自己精简了。
NOTE_LIMIT = 8
#: 准则条数上限（设计稿 §5.3「最多 20 条」）。
CHARTER_LIMIT = 20
#: 置信度低于这个值的条目不进必修段，只在检索命中时出现。
MIN_CHARTER_CONFIDENCE = 0.5
#: 检索命中后按「相关性 × 置信度 × 新近度」排序；新近度的半衰期（天）。
RECENCY_HALF_LIFE_DAYS = 180.0
#: FTS 先粗筛多少个候选，再在 Python 里精排。200 条教材时等于"全进"，不影响结果。
CANDIDATE_LIMIT = 80

_CJK_RUN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]+')
_ASCII_WORD = re.compile(r'[a-z0-9_]+')
_INDEX_SCHEMA = 1


def cache_path(home):
    return Path(home) / INDEX_NAME


def tokens(text):
    """把一段任意文本切成"可被 LIKE/MATCH 命中的词"：汉字二元组 + 英文数字词。

    单字成词（"钱"）保留，因为中文里单字查询确实存在；二元组让"收入"能命中"本月收入合计"。
    """
    if not isinstance(text, str) or not text:
        return []
    lowered = text.lower()
    words = _ASCII_WORD.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            words.append(run)
        else:
            words.extend(run[index:index + 2] for index in range(len(run) - 1))
    return words


def _token_text(text):
    return ' '.join(tokens(text))


def stamp_of(entries):
    """真身的指纹：条目内容 + 数量的哈希。索引里的 stamp 与它不一致就重建。"""
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(('%s\x1f%s\x1f%s\x1f%s\x1e' % (entry.get('id'), entry.get('kind'),
                                                     entry.get('status'), entry.get('text'))
                       ).encode('utf-8'))
    return '%s:%d' % (digest.hexdigest()[:32], len(entries))


def _recency_weight(at_ms, now_ms):
    try:
        age_days = max(0.0, (now_ms - float(at_ms)) / 86400000.0)
    except (TypeError, ValueError):
        return 1.0
    return math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)


class Index:
    """``memory.sqlite3``：教材的派生索引。损坏/过期就整表重建；从不回写真身。"""

    def __init__(self, path):
        self.path = Path(path)
        # 连接保持默认的「只在本线程使用」：这个 Index 每次都是「开→用→关」的短命对象，
        # 每个都在自己线程里用完即弃；真被跨线程误用要抛 ProgrammingError，不是段错误。
        self.connection = sqlite3.connect(str(self.path), timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.mode = 'fts5'
        try:
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS ticker(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            self.connection.execute('CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5(id UNINDEXED, kind UNINDEXED, tokens)')
            self.mode = 'fts5'
        except sqlite3.OperationalError:
            # 这个 SQLite 没编 FTS5（Windows 侧未验证过）：退化成普通表 + Python 打分。
            # 不是静默降级——mode 会出现在 `feishu-bridge teach status` 里。
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS entries_scan(id TEXT PRIMARY KEY, kind TEXT NOT NULL, tokens TEXT NOT NULL);
            ''')
            self.mode = 'scan'
        self.connection.commit()

    def close(self):
        self.connection.close()

    def _read_meta(self, key, default=None):
        row = self.connection.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row is not None else default

    def _write_meta(self, key, value):
        self.connection.execute('INSERT INTO meta(key,value) VALUES(?,?) '
                                'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def fresh(self, stamp):
        return (self._read_meta('stamp') == stamp and self._read_meta('schema') == str(_INDEX_SCHEMA))

    def rebuild(self, entries, stamp):
        """整表重建。真身是 jsonl，所以这里可以放心 DELETE 一切。"""
        self.connection.execute('DELETE FROM entries' if self.mode == 'fts5' else 'DELETE FROM entries_scan')
        if self.mode == 'fts5':
            rows = [(entry['id'], entry['kind'], _token_text(entry.get('text')))
                    for entry in entries if isinstance(entry.get('text'), str)]
            self.connection.executemany('INSERT INTO entries(id,kind,tokens) VALUES(?,?,?)', rows)
        else:
            rows = [(entry['id'], entry['kind'], _token_text(entry.get('text')))
                    for entry in entries if isinstance(entry.get('text'), str)]
            self.connection.executemany('INSERT INTO entries_scan(id,kind,tokens) VALUES(?,?,?)', rows)
        self._write_meta('stamp', stamp)
        self._write_meta('schema', str(_INDEX_SCHEMA))
        # ticker 是"上次重建时真身长什么样"的公开记录，供 `teach status` 和排查用。
        self.connection.execute('INSERT INTO ticker(key,value) VALUES(?,?) '
                                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                ('stamp', stamp))
        self.connection.commit()
        return len(rows)

    def candidates(self, terms, kinds, limit=CANDIDATE_LIMIT):
        """粗筛：先把可能相关的条目取出来（FTS5 用 bm25，退化模式用命中数）。"""
        if not terms:
            return {}
        if self.mode == 'fts5':
            match = ' OR '.join('"%s"' % term.replace('"', '') for term in terms)
            placeholders = ','.join('?' for _ in kinds)
            try:
                rows = self.connection.execute(
                    'SELECT id, bm25(entries) AS rank FROM entries '
                    'WHERE entries MATCH ? AND kind IN (%s) ORDER BY rank LIMIT ?' % placeholders,
                    (match,) + tuple(kinds) + (int(limit),)).fetchall()
            except sqlite3.OperationalError:
                return {}
            return {row['id']: -float(row['rank']) for row in rows}
        wanted = set(terms)
        counts = {}
        for row in self.connection.execute(
                'SELECT id,kind,tokens FROM entries_scan'):
            if row['kind'] not in kinds:
                continue
            hits = len(wanted & set(row['tokens'].split()))
            if hits:
                counts[row['id']] = hits
        return dict(sorted(counts.items(), key=lambda item: -item[1])[:int(limit)])


def index_entries(home):
    """索引覆盖的条目：``status=active`` 的**可检索条目**（资料 fact/pointer + 教训 lesson）。

    两条边界，``status()``/``rebuild()``/``retrieve()`` 共用这一套口径——三处各算一遍就会
    对同一份教材给出两种答案（真机上踩过：status 说 in_sync=false，检索其实一直是同步的）：

    - 撤回/被归并的条目不是记忆，进索引会让"撤回"在检索层看起来没生效；
    - 准则（principle/style/feedback）每轮必给，不参与检索，也不需要进索引。
    """
    return [entry for entry in teach_log.entries(home)
            if entry['status'] == 'active' and entry['kind'] in teach_log.SEARCH_KINDS]


def _open_index(home, entries):
    path = cache_path(home)
    try:
        index = Index(path)
    except sqlite3.Error:
        # 索引文件坏了：删掉重建。绝不能把"索引读不出来"当成"记忆没了"。
        try:
            path.unlink()
        except OSError:
            pass
        index = Index(path)
    stamp = stamp_of(entries)
    if not index.fresh(stamp):
        try:
            index.rebuild(entries, stamp)
        except sqlite3.Error:
            index.close()
            try:
                path.unlink()
            except OSError:
                pass
            index = Index(path)
            index.rebuild(entries, stamp)
    return index


def _rank(entries, candidate_scores, query_terms, kinds, now_ms):
    """精排：词面覆盖率 × 置信度 × 新近度。返回按分数降序的条目列表。"""
    total = max(1, len(entries))
    document_frequency = {}
    token_cache = {}
    for entry in entries:
        token_cache[entry['id']] = set(tokens(entry.get('text')))
    for term in query_terms:
        document_frequency[term] = sum(1 for values in token_cache.values() if term in values)
    wanted = set(query_terms)
    scored = []
    for entry in entries:
        if entry['kind'] not in kinds:
            continue
        if entry['id'] not in candidate_scores:
            continue
        present = token_cache[entry['id']]
        matched = wanted & present
        if not matched:
            continue
        weight = sum(math.log(1.0 + total / (1.0 + document_frequency.get(term, 0)))
                     for term in matched)
        coverage = len(matched) / float(len(wanted))
        score = weight * (0.5 + 0.5 * coverage) * float(entry.get('confidence') or 0.0)
        score *= _recency_weight(entry.get('at_ms'), now_ms)
        scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1].get('id') or ''))
    return [entry for _score, entry in scored]


def _view(entry):
    return {'id': entry['id'], 'kind': entry['kind'], 'expose': entry.get('expose'),
            'text': entry['text'], 'at_ms': entry.get('at_ms')}


def retrieve(home, text, max_chars=None, now=None):
    """这一轮要注入的教材块。返回值形状与 ``teach.payload_block`` 相同，便于直接渲染。

    规则（设计稿 §5.3）：
    - 必修：``principle``/``style``/``feedback`` 里 status=active 且 confidence ≥ 0.5 的，
      按「置信度 × 新近度」取前 20 条；
    - 选修：拿消息正文去检索 ``fact``/``pointer``，最多 8 条；
    - 预算：整段不超过 ``TEACHING_MAX_CHARS``，超了先砍资料、再砍置信度最低的准则，
      并把"这一轮注入了 N 条 / 共 M 条"如实写进 ``omitted``，不假装全都给了。
    """
    budget = TEACHING_MAX_CHARS if max_chars is None else int(max_chars)
    now_ms = (time.time() if now is None else now) * 1000.0
    if not teach_log.enabled_chat(home):
        return {'enabled': False}
    current = teach_log.state(home)
    block = {'enabled': True, 'rev': current['rev'], 'charter': [], 'notes': [],
             'error': current['error'], 'used_ids': [], 'omitted': 0,
             'index_mode': 'idle'}
    active = [entry for entry in teach_log.entries(home) if entry['status'] == 'active']
    charter = [entry for entry in active
               if entry['kind'] in teach_log.CHARTER_KINDS
               and float(entry.get('confidence') or 0) >= MIN_CHARTER_CONFIDENCE]
    charter.sort(key=lambda entry: (-float(entry.get('confidence') or 0.0),
                                    -float(entry.get('at_ms') or 0.0)))
    charter = charter[:CHARTER_LIMIT]
    used = 0
    for entry in charter:
        length = len(entry['text'])
        if used + length > budget:
            continue
        used += length
        block['charter'].append(_view(entry))
        block['used_ids'].append(entry['id'])
    dropping = [entry for entry in active if entry['kind'] in teach_log.SEARCH_KINDS]
    block['index_mode'] = 'idle' if not dropping else block['index_mode']
    query_terms = tokens(text if isinstance(text, str) else '')
    remaining = budget - used
    if query_terms and dropping and remaining > 0:
        index = None
        try:
            index = _open_index(home, dropping)
            block['index_mode'] = index.mode
            kind_list = tuple(teach_log.SEARCH_KINDS)
            scores = index.candidates(sorted(set(query_terms)), kind_list)
            ranked = _rank(dropping, scores, query_terms, kind_list, now_ms)[:NOTE_LIMIT]
        except (sqlite3.Error, OSError):
            # 索引不可用不该让整轮没有教材：准则已经拿到了，资料这一段这轮就空着，
            # 并且把原因写进 error（doctor/teach status 里看得见）。
            block['error'] = block['error'] or '记忆索引不可用（删掉 memory.sqlite3 会自动重建）'
            ranked = []
        finally:
            if index is not None:
                index.close()
        for entry in ranked:
            length = len(entry['text'])
            if used + length > budget:
                continue
            used += length
            block['notes'].append(_view(entry))
            block['used_ids'].append(entry['id'])
    # 一条事实一个答案：没注入的条数 = 激活条目总数 - 这一轮真给了的条数。
    block['omitted'] = max(0, len(active) - len(block['charter']) - len(block['notes']))
    return block


def status(home):
    """索引的现状：条目数、模式、是否与真身一致。`teach status` 里直接读这一份。"""
    entries = index_entries(home)
    stamp = stamp_of(entries)
    path = cache_path(home)
    report = {'path': str(path), 'present': path.exists(), 'entries': len(entries),
              'records': len(teach_log.entries(home)),
              'stamp': stamp, 'index_mode': None, 'in_sync': False}
    if not path.exists():
        # 没有可检索的资料时，索引文件不存在**就是**正确状态：别把一个正常的空索引
        # 报成"不同步"（真机上只有准则、没有资料，status 会一直红着）。
        report['in_sync'] = not entries
        report['index_mode'] = None if entries else 'idle'
        return report
    index = None
    try:
        index = Index(path)
        report['index_mode'] = index.mode
        report['in_sync'] = index.fresh(stamp)
        report['indexed'] = (index.connection.execute(
            'SELECT COUNT(*) AS n FROM entries').fetchone()['n'] if index.mode == 'fts5'
            else index.connection.execute('SELECT COUNT(*) AS n FROM entries_scan').fetchone()['n'])
    except sqlite3.Error as error:
        report['error'] = '%s: %s' % (type(error).__name__, str(error)[:200])
    finally:
        if index is not None:
            index.close()
    return report


def rebuild(home):
    """强制重建索引（删文件再建）。返回新的状态报告。"""
    path = cache_path(home)
    try:
        path.unlink()
    except OSError:
        pass
    entries = index_entries(home)
    if entries:
        index = _open_index(home, entries)
        index.close()
    return status(home)

