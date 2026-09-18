"""三层入库快照记录：raw 原件 / extract 解析稿 / wiki 知识成果。

用 snapshots 表的 layer 字段标记三层，prev_snapshot_id 建立层间链：
  raw 快照 → extract 快照（extract_path 指向解析稿）→ wiki 快照
每层记录 content_hash + parser（解析工具/模型）+ fetched_at，供版本恢复与溯源。
"""
from __future__ import annotations

import db as db_mod
import storage as storage_mod


def record_raw_snapshot(conn, *, kb_id: str, file_path: str, source_url: str | None = None,
                        source_name: str | None = None, parser: str | None = None) -> int:
    """记录 raw 原件快照（layer=raw），并登记 sources。返回 snapshot_id。

    幂等：同 kb + 同内容 + 同文件 的 raw 快照已存在则直接复用旧 id，不重复插入。
    （compile 在 LLM 调用前先写快照并 commit，PAUSE 后重跑会再次经过这里，
    若纯 INSERT 撞 UNIQUE(kb_id, content_hash, file_path) 会抛 IntegrityError 打死 worker。）
    """
    import hashlib
    from pathlib import Path

    digest = storage_mod.content_hash(Path(file_path).read_bytes())
    existing = conn.execute(
        "SELECT id FROM snapshots WHERE kb_id=? AND content_hash=? AND file_path=? AND layer='raw'",
        (kb_id, digest, file_path),
    ).fetchone()
    if existing:
        return existing["id"]

    # 登记来源
    cur = conn.execute(
        """INSERT INTO sources(kb_id, name, url, source_type, fetched_at, access)
           VALUES(?,?,?,?,?,'private')""",
        (kb_id, source_name, source_url, "article", db_mod.now_iso()),
    )
    source_id = cur.lastrowid

    cur = conn.execute(
        """INSERT INTO snapshots(source_id, kb_id, file_path, content_hash, layer, parser, fetched_at)
           VALUES(?,?,?,?,?,?,?)""",
        (source_id, kb_id, file_path, digest, "raw", parser, db_mod.now_iso()),
    )
    return cur.lastrowid


def record_extract_snapshot(conn, *, kb_id: str, raw_snapshot_id: int,
                            extract_path: str, parser: str) -> int:
    """记录 extract 解析稿快照（layer=extract），prev 指向 raw 快照。返回 snapshot_id。

    幂等：同 kb + 同内容 + 同文件 的 extract 快照已存在则复用旧 id。
    """
    from pathlib import Path

    digest = storage_mod.content_hash(Path(extract_path).read_bytes())
    existing = conn.execute(
        "SELECT id FROM snapshots WHERE kb_id=? AND content_hash=? AND file_path=? AND layer='extract'",
        (kb_id, digest, extract_path),
    ).fetchone()
    if existing:
        return existing["id"]

    cur = conn.execute(
        """INSERT INTO snapshots(source_id, kb_id, file_path, content_hash, prev_snapshot_id,
                                 layer, extract_path, parser, fetched_at)
           SELECT source_id, kb_id, ?, ?, ?, 'extract', ?, ?, ?
           FROM snapshots WHERE id=?""",
        (extract_path, digest, raw_snapshot_id, extract_path, parser, db_mod.now_iso(), raw_snapshot_id),
    )
    return cur.lastrowid


def record_wiki_snapshot(conn, *, kb_id: str, prev_snapshot_id: int,
                         file_path: str, parser: str | None = None) -> int:
    """记录 wiki 成果快照（layer=wiki），prev 指向 extract 快照。返回 snapshot_id。

    幂等：同 kb + 同内容 + 同文件 的 wiki 快照已存在则复用旧 id。
    """
    from pathlib import Path

    digest = storage_mod.content_hash(Path(file_path).read_bytes())
    existing = conn.execute(
        "SELECT id FROM snapshots WHERE kb_id=? AND content_hash=? AND file_path=? AND layer='wiki'",
        (kb_id, digest, file_path),
    ).fetchone()
    if existing:
        return existing["id"]

    cur = conn.execute(
        """INSERT INTO snapshots(source_id, kb_id, file_path, content_hash, prev_snapshot_id,
                                 layer, parser, fetched_at)
           SELECT source_id, kb_id, ?, ?, ?, 'wiki', ?, ?
           FROM snapshots WHERE id=?""",
        (file_path, digest, prev_snapshot_id, parser, db_mod.now_iso(), prev_snapshot_id),
    )
    return cur.lastrowid


def link_chain(conn, kb_id: str) -> list[dict]:
    """查某库的三层链（raw→extract→wiki），供溯源与版本恢复。"""
    rows = conn.execute(
        """SELECT id, layer, file_path, extract_path, parser, prev_snapshot_id, content_hash
           FROM snapshots WHERE kb_id=? ORDER BY id""",
        (kb_id,),
    ).fetchall()
    return [dict(r) for r in rows]
