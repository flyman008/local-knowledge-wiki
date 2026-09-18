"""收件与去重：submit_material / get_receipt。

方案§3.1 工具契约。重复提交相同请求返回原回执，不重复生成任务。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import db as db_mod
import storage as storage_mod
import domains
from media_retention import serialized


@dataclass
class SubmitResult:
    receipt_id: str
    status: str
    is_duplicate: bool
    needs_body: bool = False


def _dedup_key(kb_id: str, target_kb_id: str, url: str | None, text: str | None, file_data: bytes | None) -> str:
    """去重键含知识域维度（target_kb_id）：同一内容提交到不同库应各自独立入库。"""
    scope = f"{kb_id}:{target_kb_id}"
    if file_data:
        return f"file:{scope}:{storage_mod.content_hash(file_data)}"
    if text:
        return f"text:{scope}:{storage_mod.normalize_hash(text)}"
    if url:
        return f"url:{scope}:{url}"
    return f"none:{uuid.uuid4().hex}"


@serialized
def submit_material(
    cfg,
    *,
    kb_id: str,
    url: str | None = None,
    text: str | None = None,
    file_data: bytes | None = None,
    filename: str | None = None,
    source_hint: str | None = None,
    note: str | None = None,
    domain: str | None = None,
    request_dedup_id: str | None = None,
    target_kb_id: str | None = None,
    classification: dict | None = None,
) -> SubmitResult:
    """收件。返回回执。校验是否有正文/文件，无则标记待补。

    kb_id：收件落点，默认 research-wiki（隔离区）。
    target_kb_id：知识域归属 / 将来迁入哪个库（默认与 kb_id 一致）。
    所有材料原件只落隔离区 raw，不直接写存量库。
    """
    classification = domains.normalize(classification)
    conn = db_mod.get_conn()
    storage = storage_mod.Storage(cfg)

    target_kb_id = target_kb_id or kb_id or "research-wiki"
    kb_id = kb_id or "research-wiki"

    # request_dedup_id 是客户端显式去重键，但也必须限定知识域，否则跨库串回执
    if request_dedup_id:
        dedup_key = f"req:{kb_id}:{target_kb_id}:{request_dedup_id}"
    else:
        dedup_key = _dedup_key(kb_id, target_kb_id, url, text, file_data)

    import json
    dedup_key += ':' + storage_mod.normalize_hash(json.dumps(classification,sort_keys=True))
    # 去重：同 dedup_key 已有回执则复用
    existing = conn.execute(
        "SELECT id, status FROM receipts WHERE dedup_key=? ORDER BY created_at DESC LIMIT 1",
        (dedup_key,),
    ).fetchone()
    if existing:
        conn.close()
        return SubmitResult(existing["id"], existing["status"], is_duplicate=True)

    has_body = bool(text) or bool(file_data)
    receipt_id = f"rcp_{uuid.uuid4().hex[:12]}"

    status = "received"
    needs_body = False
    if not has_body and not url:
        status = "needs_body"
        needs_body = True
    elif not has_body and url:
        # 只有链接：可尝试抓取，先标记待抓
        status = "received"

    now = db_mod.now_iso()
    conn.execute(
        """INSERT INTO receipts(id, kb_id, target_kb_id, source_url, status, dedup_key, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (receipt_id, kb_id, target_kb_id, url, status, dedup_key, now, now),
    )
    domains.save(conn,receipt_id,classification,target_kb_id)
    conn.commit()

    # 落盘 raw（有内容才落），并把路径写回回执做精确绑定
    raw_path = None
    if file_data and filename:
        raw_path = storage.save_raw(kb_id, filename, file_data).raw_path
    elif text:
        raw_path = storage.save_raw(
            kb_id, _derive_filename(url, filename, note), text.encode("utf-8")
        ).raw_path

    if raw_path:
        conn.execute(
            "UPDATE receipts SET raw_path=? WHERE id=?",
            (raw_path, receipt_id),
        )
        conn.commit()

    conn.close()
    return SubmitResult(receipt_id, status, is_duplicate=False, needs_body=needs_body)


def _derive_filename(url: str | None, filename: str | None, note: str | None) -> str:
    if filename:
        return filename
    if url:
        base = url.rstrip("/").split("/")[-1] or "material"
        return f"{base}.txt"
    return f"paste_{uuid.uuid4().hex[:8]}.txt"


def get_receipt(cfg, receipt_id: str) -> dict[str, Any] | None:
    conn = db_mod.get_conn()
    row = conn.execute(
        "SELECT * FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    # 解码入库结果三列，让查询链路带出真实状态（缺页信息不丢失）
    sl, pc, mp = db_mod.decode_intake_result(
        d.get("saved_layers"), d.get("parse_complete"), d.get("missing_pages")
    )
    d["saved_layers"] = sl
    d["parse_complete"] = pc
    d["missing_pages"] = mp
    evidence = conn_evidence = None
    conn_evidence = db_mod.get_conn()
    try:
        import json
        classified = conn_evidence.execute('SELECT payload FROM classifications WHERE receipt_id=?',(receipt_id,)).fetchone()
        d['classification'] = json.loads(classified['payload']) if classified else domains.normalize()
        evidence = conn_evidence.execute("SELECT metadata,kb_id,doc_id,topic_version FROM evidence_metadata WHERE receipt_id=?", (receipt_id,)).fetchone()
        if evidence:
            import review
            d['review_items'] = review.context(conn_evidence,evidence['kb_id'],evidence['doc_id'])
            d['topic_version'] = evidence['topic_version']
        import quality
        d['quality'] = quality.receipt_report(conn_evidence, d)
        import media_retention
        d['media'] = media_retention.read(conn_evidence, receipt_id) or {'state':'unmanaged'}
    finally:
        conn_evidence.close()
    if evidence:
        import json
        d["source_metadata"] = json.loads(evidence["metadata"])
    import archive
    return domains.describe_record(archive.describe_content(d))


def list_receipts(cfg, limit: int = 50) -> list[dict[str, Any]]:
    conn = db_mod.get_conn()
    rows = conn.execute(
        "SELECT * FROM receipts ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
