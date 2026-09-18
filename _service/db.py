"""SQLite 任务库 schema 与连接层。

按方案§4.1 最小记录建模：来源 / 快照 / 任务 / 主题 / 变更，外加收件回执与
全文检索（FTS5 派生索引，可从 wiki+extracts 重建）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import config as config_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_quality (
    receipt_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

-- 来源：ID、名称、原始URL/路径、发布者、类型、发布/获取时间、访问边界
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id TEXT NOT NULL,
    name TEXT,
    url TEXT,
    publisher TEXT,
    source_type TEXT,            -- article / pdf / docx / pptx / xlsx / image / note / audio / video
    published_at TEXT,
    fetched_at TEXT,
    access TEXT DEFAULT 'private',
    UNIQUE(kb_id, url)
);

-- 快照：来源ID、文件位置、内容指纹、上一版本、获取时间
-- layer 标记三层入库：raw(原件) / extract(解析稿) / wiki(知识成果)
-- extract_path：解析稿路径（layer=extract 时有值）
-- parser：解析工具或模型标识（layer=extract 时记录，如 pymupdf / deepseek-v4-flash-vision-exp）
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES sources(id),
    kb_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    prev_snapshot_id INTEGER,
    fetched_at TEXT,
    layer TEXT DEFAULT 'raw',     -- raw / extract / wiki
    extract_path TEXT,
    parser TEXT,
    UNIQUE(kb_id, content_hash, file_path)
);

-- 收件回执：去重 + 状态跟踪
CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    kb_id TEXT NOT NULL,         -- 收件落点（隔离区 research-wiki 的 raw）
    target_kb_id TEXT,           -- 知识域归属 / 将来迁入哪个库（默认 research-wiki）
    source_url TEXT,
    status TEXT NOT NULL,        -- received / queued / processing / done / failed / needs_body
    error TEXT,
    dedup_key TEXT,              -- 规范化内容 hash 或 URL 去重键
    raw_path TEXT,               -- 本回执落盘的 raw 文件路径（精确绑定）
    saved_layers TEXT,           -- JSON：三层保存状态 {raw,extract,wiki}
    parse_complete INTEGER,      -- 解析完整性（1=完整 0=不完整，客户端显式声明）
    missing_pages TEXT,          -- JSON：缺失/失败页码列表
    created_at TEXT,
    updated_at TEXT
);

-- 任务：收件ID、阶段、重试次数、错误、模型/规则版本、用量、关联快照与主题
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT REFERENCES receipts(id),
    stage TEXT NOT NULL,         -- ingest / parsed / compiled / confirmed / published
    status TEXT NOT NULL,        -- pending / running / done / failed / paused
    retries INTEGER DEFAULT 0,
    error TEXT,
    model_id TEXT,
    rules_version TEXT,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    snapshot_id INTEGER REFERENCES snapshots(id),
    created_at TEXT,
    updated_at TEXT
);

-- 主题：稳定ID、名称/别名、类型、知识域、状态、版本、来源定位、关联主题
-- 主键为 (kb_id, id)：同一主题名在不同库可独立存在，避免跨库撞主键
CREATE TABLE IF NOT EXISTS topics (
    id TEXT NOT NULL,
    kb_id TEXT NOT NULL,
    name TEXT NOT NULL,
    aliases TEXT,                -- JSON 数组
    topic_type TEXT,             -- concept / entity / summary / relationship / ...
    domain TEXT,
    status TEXT DEFAULT 'draft', -- draft / verified / stale
    version INTEGER DEFAULT 1,
    wiki_path TEXT,
    updated_at TEXT,
    PRIMARY KEY (kb_id, id)
);

-- 变更：目标主题、前后版本、变化摘要、冲突、是否需确认及受影响输出
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id TEXT,
    kb_id TEXT NOT NULL,
    from_version INTEGER,
    to_version INTEGER,
    summary TEXT,
    conflicts TEXT,
    needs_confirm INTEGER DEFAULT 0,
    affected_outputs TEXT,
    created_at TEXT
);

-- 用量与预算
CREATE TABLE IF NOT EXISTS classifications (
    receipt_id TEXT PRIMARY KEY,
    kb_id TEXT NOT NULL,
    doc_id TEXT,
    topic_version INTEGER,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_items (
    id TEXT PRIMARY KEY,
    kb_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    topic_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    question TEXT NOT NULL,
    evidence TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    revision INTEGER NOT NULL DEFAULT 1,
    decision TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_proposals (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_metadata (
    kb_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    topic_version INTEGER NOT NULL,
    receipt_id TEXT NOT NULL,
    metadata TEXT NOT NULL,
    PRIMARY KEY (kb_id, doc_id, topic_version)
);

-- 用量与预算
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    model_id TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_estimate REAL,          -- 仅在配置了可靠单价时填，未知不填零
    created_at TEXT
);

-- 全文检索派生索引（可从 wiki + extracts 重建）
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    doc_id UNINDEXED,
    kb_id UNINDEXED,
    title,
    body
);
"""


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    cfg = config_mod.load_config()
    db_path = db_path or (config_mod.LOCAL_DIR / "knowledge.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库结构升级，保留已有数据。CREATE TABLE IF NOT EXISTS 不改已有表，需显式迁移。

    迁移项（各自独立，互不阻塞）：
    1. topics 旧主键 id → 复合主键 (kb_id, id)。
    2. snapshots 加 layer / extract_path / parser 列（三层入库）。
    3. receipts 加 saved_layers / parse_complete / missing_pages 列（持久化入库结果）。
    """
    _migrate_topics_pk(conn)
    _migrate_snapshots_columns(conn)
    _migrate_receipts_columns(conn)


def _migrate_topics_pk(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='topics'"
    ).fetchone()
    if not row:
        return
    sql = row["sql"]
    if "PRIMARY KEY (kb_id, id)" in sql:
        return  # 已是新结构

    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE topics RENAME TO topics_old")
        conn.execute(
            """CREATE TABLE topics (
                id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                name TEXT NOT NULL,
                aliases TEXT,
                topic_type TEXT,
                domain TEXT,
                status TEXT DEFAULT 'draft',
                version INTEGER DEFAULT 1,
                wiki_path TEXT,
                updated_at TEXT,
                PRIMARY KEY (kb_id, id)
            )"""
        )
        conn.execute(
            """INSERT OR IGNORE INTO topics (id, kb_id, name, aliases, topic_type, domain,
                                             status, version, wiki_path, updated_at)
               SELECT id, kb_id, name, aliases, topic_type, domain,
                      status, version, wiki_path, updated_at
               FROM topics_old"""
        )
        conn.execute("DROP TABLE topics_old")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _migrate_snapshots_columns(conn: sqlite3.Connection) -> None:
    """snapshots 加 layer / extract_path / parser 列（若旧表缺这些列）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
    if {"layer", "extract_path", "parser"}.issubset(cols):
        return  # 已是新结构
    # ALTER TABLE ADD COLUMN 逐个补缺失列（不重建表，保留数据）
    if "layer" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN layer TEXT DEFAULT 'raw'")
    if "extract_path" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN extract_path TEXT")
    if "parser" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN parser TEXT")
    conn.commit()


def _migrate_receipts_columns(conn: sqlite3.Connection) -> None:
    """receipts 加 saved_layers / parse_complete / missing_pages 列（持久化入库结果）。

    旧回执无这三列：saved_layers 补 NULL（表示未知，非空即未持久化），
    parse_complete 补 NULL，missing_pages 补 NULL。查询端用默认值兜底。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(receipts)").fetchall()}
    if {"saved_layers", "parse_complete", "missing_pages"}.issubset(cols):
        return
    if "saved_layers" not in cols:
        conn.execute("ALTER TABLE receipts ADD COLUMN saved_layers TEXT")
    if "parse_complete" not in cols:
        conn.execute("ALTER TABLE receipts ADD COLUMN parse_complete INTEGER")
    if "missing_pages" not in cols:
        conn.execute("ALTER TABLE receipts ADD COLUMN missing_pages TEXT")
    conn.commit()


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------- 入库结果持久化（三层保存状态 + 解析完整性） ----------

def encode_intake_result(saved_layers: dict | None, parse_complete: bool | None, missing_pages: list | None):
    """把入库结果编码为 receipts 表三列（JSON / INTEGER / JSON）。"""
    return (
        json.dumps(saved_layers, ensure_ascii=False) if saved_layers is not None else None,
        1 if parse_complete else 0 if parse_complete is not None else None,
        json.dumps(missing_pages, ensure_ascii=False) if missing_pages is not None else None,
    )


def decode_intake_result(saved_layers_raw, parse_complete_raw, missing_pages_raw):
    """把 receipts 表三列还原为原生类型。旧回执三列为 NULL 时返回 (None, None, None)。"""
    saved_layers = json.loads(saved_layers_raw) if saved_layers_raw else None
    parse_complete = None if parse_complete_raw is None else bool(parse_complete_raw)
    missing_pages = json.loads(missing_pages_raw) if missing_pages_raw else None
    return saved_layers, parse_complete, missing_pages
