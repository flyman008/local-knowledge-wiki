"""archive_full：客户端已整理的完整三层入库（后台不调模型）。

场景：客户端（Codex/WorkBuddy/Claude）已经读完 PDF、整理好，主动要求入库。
客户端提交三件套：原件(raw) + 解析稿(extract) + 知识成果(wiki)。
服务只保存、登记版本、建立三层快照链、建检索，**不调用模型重写**。

关键正确性保证：
- 同 doc_id（标题）再次入库 = 版本递增（version+1），三层文件带版本号不可覆盖旧版。
- 去重按「三件套内容 + 来源身份」整体指纹，任一变化即新版本，不误判重复。
- 缺层或解析不完整时返回真实 saved_layers 状态，不提示"三层完整入库"。
- 文件先落盘（版本化命名，不覆盖），DB 一个事务提交；DB 失败不影响旧版本。
"""
from __future__ import annotations

import hashlib
import uuid
import json
import authority
import domains
from dataclasses import dataclass, field

import db as db_mod
import snapshot as snapshot_mod
import storage as storage_mod


@dataclass
class FullArchiveResult:
    receipt_id: str
    doc_id: str
    is_duplicate: bool
    version: int = 1
    raw_snap_id: int | None = None
    extract_snap_id: int | None = None
    wiki_snap_id: int | None = None
    saved_layers: dict = field(default_factory=dict)  # {raw:bool, extract:bool, wiki:bool}
    parse_complete: bool = True    # 客户端显式声明的解析完整性（缺页/识图失败=False）
    missing_pages: list = field(default_factory=list)  # 缺失/失败页码
    complete: bool = False  # 三层齐全 且 解析完整 才算 complete


def _layer_fingerprint(raw_data, extract_text, wiki_text, title, url, publisher, parse_complete, missing_pages) -> str:
    """三件套内容 + 来源身份 + 解析完整性 的整体指纹，用于去重。任一变化即不同指纹。"""
    parts = []
    parts.append("raw:" + storage_mod.content_hash(raw_data) if raw_data else "raw:None")
    parts.append("extract:" + storage_mod.normalize_hash(extract_text) if extract_text else "extract:None")
    parts.append("wiki:" + storage_mod.normalize_hash(wiki_text) if wiki_text else "wiki:None")
    parts.append(f"title:{title or ''}")
    parts.append(f"url:{url or ''}")
    parts.append(f"publisher:{publisher or ''}")
    parts.append(f"parse_complete:{parse_complete}")
    parts.append(f"missing_pages:{sorted(missing_pages or [])}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def archive_full(
    cfg,
    *,
    kb_id: str,
    title: str,
    # 三件套
    raw_data: bytes | None = None,
    raw_filename: str | None = None,
    extract_text: str | None = None,
    wiki_text: str | None = None,
    # 来源信息
    url: str | None = None,
    publisher: str | None = None,
    parser: str | None = None,   # 客户端解析用的工具/模型，如实记录
    parse_complete: bool = True,  # 客户端显式声明解析是否完整（缺页/识图失败传 False）
    missing_pages: list | None = None,  # 缺失/失败页码列表，如 [2]
    request_dedup_id: str | None = None,
    source_metadata: dict | None = None,
    classification: dict | None = None,
    correction_reason: str | None = None,
    base_version: int | None = None,
) -> FullArchiveResult:
    """客户端已整理的三层入库。后台 0 模型调用。

    parse_complete 由客户端显式声明，不靠正文关键词猜：
    - 三层都保存了，但解析稿标注「第 N 页未识别」且 parse_complete=False → complete=False。
    - saved_layers 记录三层是否落盘，parse_complete 记录解析是否完整，两者分开。
    """
    metadata = authority.normalize(source_metadata)
    if correction_reason is not None and not correction_reason.strip():
        raise ValueError('纠错原因不能为空')
    classification = domains.normalize(classification)
    conn = db_mod.get_conn()
    storage = storage_mod.Storage(cfg)

    kb_id = kb_id or "research-wiki"
    target_kb_id = kb_id  # 客户端已整理路径，直接入目标域

    missing_pages = sorted(int(p) for p in (missing_pages or []))
    doc_id = _slug(title) or f"prepared-{uuid.uuid4().hex[:6]}"

    # 去重：整体指纹（三件套 + 来源身份 + 解析完整性），任一变化即不重复
    if request_dedup_id:
        dedup_key = f"full:req:{kb_id}:{request_dedup_id}"
    else:
        fp = _layer_fingerprint(raw_data, extract_text, wiki_text, title, url, publisher, parse_complete, missing_pages)
        if source_metadata is not None:
            fp = hashlib.sha256((fp + json.dumps(metadata, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        dedup_key = f"full:{kb_id}:{fp}"
    dedup_key += ':' + hashlib.sha256(json.dumps(classification,sort_keys=True).encode()).hexdigest()
    if correction_reason:
        dedup_key += ':correction:' + hashlib.sha256(json.dumps([base_version,correction_reason]).encode()).hexdigest()

    existing = conn.execute(
        "SELECT id, saved_layers, parse_complete, missing_pages FROM receipts WHERE dedup_key=? ORDER BY created_at DESC LIMIT 1",
        (dedup_key,),
    ).fetchone()
    if existing:
        # 复用旧回执，读回已持久化的真实状态（不靠当前参数推导，避免掩盖不完整状态）
        sl, pc, mp = db_mod.decode_intake_result(
            existing["saved_layers"], existing["parse_complete"], existing["missing_pages"]
        )
        saved = sl or {"raw": bool(raw_data), "extract": bool(extract_text), "wiki": bool(wiki_text)}
        pc_final = pc if pc is not None else parse_complete
        mp_final = mp if mp is not None else missing_pages
        all_layers = saved.get("raw") and saved.get("extract") and saved.get("wiki")
        conn.close()
        return FullArchiveResult(
            existing["id"], doc_id, is_duplicate=True,
            saved_layers=saved,
            parse_complete=pc_final,
            missing_pages=mp_final,
            complete=all_layers and pc_final,
        )

    # 版本：同 doc_id 已存在则 version+1
    prev = conn.execute(
        "SELECT version,wiki_path FROM topics WHERE kb_id=? AND id=?", (target_kb_id, doc_id)
    ).fetchone()
    new_version = (prev["version"] + 1) if prev else 1
    correction_eligible = False
    correction_check = 'not_requested'
    if correction_reason:
        if not prev or base_version != prev['version']:
            conn.close()
            raise ValueError('纠错基准版本不存在或已变化，请重新读取后提交')
        # A client assertion is not a human verdict. Verify identity/version;
        # semantic correctness remains the client's responsibility.
        old = conn.execute('''SELECT r.raw_path FROM evidence_metadata e
            JOIN receipts r ON r.id=e.receipt_id
            WHERE e.kb_id=? AND e.doc_id=? AND e.topic_version=?''',
            (target_kb_id,doc_id,prev['version'] if prev else 0)).fetchone()
        if not old:
            # Legacy archives have classification/version bindings but no evidence_metadata.
            old = conn.execute('''SELECT r.raw_path FROM classifications c JOIN receipts r ON r.id=c.receipt_id
                WHERE c.kb_id=? AND c.doc_id=? AND c.topic_version=? ORDER BY r.created_at DESC LIMIT 1''',
                (target_kb_id,doc_id,prev['version'])).fetchone()
        from pathlib import Path
        same_raw = bool(old and old['raw_path'] and raw_data and Path(old['raw_path']).is_file()
                        and storage_mod.content_hash(Path(old['raw_path']).read_bytes()) == storage_mod.content_hash(raw_data))
        pending = conn.execute("SELECT count(*) FROM review_items WHERE kb_id=? AND doc_id=? AND status='pending'",
                               (target_kb_id,doc_id)).fetchone()[0]
        correction_eligible = bool(prev and base_version == prev['version'] and same_raw
                                   and not pending and parse_complete and not missing_pages and extract_text and wiki_text)
        correction_check = 'eligible' if correction_eligible else 'needs_review'

    now = db_mod.now_iso()
    saved_layers = {"raw": False, "extract": False, "wiki": False}

    # 已写文件的路径清单，用于 DB 失败时清理（版本化命名，天然不影响旧版）
    written_files: list[str] = []

    try:
        # 1. raw 原件落盘 + 快照（版本化命名，不覆盖旧版）
        raw_path = None
        raw_snap_id = None
        if raw_data and raw_filename:
            # 带版本号命名：原名 + _v{version}，保留历史
            stem, ext = _split_ext(raw_filename)
            ver_filename = f"{stem}_v{new_version}{ext}" if new_version > 1 else raw_filename
            mat = storage.save_raw(kb_id, ver_filename, raw_data)
            raw_path = mat.raw_path
            # 关键：只有本轮实际新建的文件才进清理清单；复用文件（is_new=False）回滚时不能删
            if mat.is_new:
                written_files.append(raw_path)
            raw_snap_id = snapshot_mod.record_raw_snapshot(
                conn, kb_id=target_kb_id, file_path=raw_path,
                source_name=raw_filename, source_url=url, parser=parser,
            )
            saved_layers["raw"] = True

        # 2. extract 解析稿落盘 + 快照（版本化命名）
        extract_snap_id = None
        if extract_text:
            base = f"{doc_id}_v{new_version}" if new_version > 1 else doc_id
            extract_path = storage.save_extract(target_kb_id, base, extract_text)
            written_files.append(str(extract_path))
            if raw_snap_id:
                extract_snap_id = snapshot_mod.record_extract_snapshot(
                    conn, kb_id=target_kb_id, raw_snapshot_id=raw_snap_id,
                    extract_path=str(extract_path), parser=parser or "client",
                )
            saved_layers["extract"] = True

        # 3. wiki 成果落盘（版本化命名）+ 快照 + FTS
        wiki_snap_id = None
        if wiki_text:
            frontmatter = (
                "---\n"
                f"type: prepared\n"
                f"source: {raw_filename or ''}\n"
                f"updated: {now[:10]}\n"
                f"version: {new_version}\n"
                "status: draft\n"
                f"parse_complete: {str(parse_complete).lower()}\n"
                + (f"missing_pages: {missing_pages}\n" if not parse_complete else "")
                + f"tags: [client-prepared, {target_kb_id}]\n"
                "---\n"
            )
            full_md = frontmatter + "\n" + wiki_text.strip() + "\n"
            inbox_dir = storage.inbox_wiki_dir(target_kb_id)
            inbox_dir.mkdir(parents=True, exist_ok=True)
            wiki_rel = f"{doc_id}_v{new_version}.md" if new_version > 1 else f"{doc_id}.md"
            wiki_path = inbox_dir / wiki_rel
            wiki_path.write_text(full_md, encoding="utf-8")
            written_files.append(str(wiki_path))

            # topics upsert（version 递增，wiki_path 指向最新）
            if prev:
                conn.execute(
                    "UPDATE topics SET version=?, wiki_path=?, updated_at=?, status='draft', topic_type='prepared' WHERE kb_id=? AND id=?",
                    (new_version, f"_inbox/{wiki_rel}", now, target_kb_id, doc_id),
                )
            else:
                conn.execute(
                    """INSERT INTO topics(id, kb_id, name, topic_type, domain, status, version, wiki_path, updated_at)
                       VALUES(?,?,?,?,?,?,1,?,?)""",
                    (doc_id, target_kb_id, title, "prepared", "", "draft",
                     f"_inbox/{wiki_rel}", now),
                )
            # FTS 更新（doc_id 稳定，body 更新为最新）
            conn.execute(
                "DELETE FROM wiki_fts WHERE kb_id=? AND doc_id=?", (target_kb_id, doc_id)
            )
            conn.execute(
                "INSERT INTO wiki_fts(doc_id, kb_id, title, body) VALUES(?,?,?,?)",
                (doc_id, target_kb_id, title, full_md),
            )
            if extract_snap_id:
                wiki_snap_id = snapshot_mod.record_wiki_snapshot(
                    conn, kb_id=target_kb_id, prev_snapshot_id=extract_snap_id,
                    file_path=str(wiki_path), parser=parser,
                )
            saved_layers["wiki"] = True

        # 收件回执：持久化入库结果（saved_layers / parse_complete / missing_pages），
        # 首次、重复、历史查询复用同一份真实状态。
        receipt_id = f"rcp_{uuid.uuid4().hex[:12]}"
        sl_raw, pc_raw, mp_raw = db_mod.encode_intake_result(saved_layers, parse_complete, missing_pages)
        conn.execute(
            """INSERT INTO receipts(id, kb_id, target_kb_id, source_url, status, dedup_key, raw_path,
                                     saved_layers, parse_complete, missing_pages, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (receipt_id, kb_id, target_kb_id, url, "done", dedup_key, raw_path,
             sl_raw, pc_raw, mp_raw, now, now),
        )
        if wiki_text:
            conn.execute("INSERT INTO evidence_metadata(kb_id,doc_id,topic_version,receipt_id,metadata) VALUES(?,?,?,?,?)",
                         (target_kb_id, doc_id, new_version, receipt_id,
                          json.dumps(metadata, ensure_ascii=False)))
            import review
            if not parse_complete:
                review.add(conn, target_kb_id, doc_id, new_version, 'uncertain',
                           '解析不完整；缺失部分不能视为已核实，需补材料或限定结论范围',
                           [{'statement': f'missing_pages={missing_pages}', 'locator': raw_path or receipt_id}])
            if prev and not correction_eligible:
                review.add(conn, target_kb_id, doc_id, new_version, 'uncertain',
                           '同标题已有版本：请确认是替代旧口径、不同范围并存，还是无实质变化；这不是已检测到冲突',
                           [{'statement': f'previous_version={new_version-1}', 'locator': prev['wiki_path']},
                            {'statement': f'new_version={new_version}', 'locator': str(wiki_path)}])
        domains.save(conn,receipt_id,classification,target_kb_id,doc_id if wiki_text else None,new_version if wiki_text else None)
        import quality
        checks = quality.inspect(wiki_text, extract_text, metadata, classification, parse_complete, missing_pages)
        checks['update'] = {'kind':'editorial_correction' if correction_reason else 'standard',
                            'base_version':base_version,'reason':correction_reason,
                            'check':correction_check,'version_review_skipped':correction_eligible,
                            'basis':'client_declaration_and_same_raw_check' if correction_eligible else None}
        conn.execute('INSERT INTO intake_quality(receipt_id,payload) VALUES(?,?)',
                     (receipt_id, json.dumps(checks, ensure_ascii=False)))
        conn.commit()

        # 完整 = 三层都保存 且 解析完整（两者分开记录，不混淆）
        all_layers = saved_layers["raw"] and saved_layers["extract"] and saved_layers["wiki"]
        complete = all_layers and parse_complete
        return FullArchiveResult(
            receipt_id, doc_id, is_duplicate=False, version=new_version,
            raw_snap_id=raw_snap_id, extract_snap_id=extract_snap_id, wiki_snap_id=wiki_snap_id,
            saved_layers=saved_layers, parse_complete=parse_complete, missing_pages=missing_pages,
            complete=complete,
        )
    except Exception:
        conn.rollback()
        # 清理本次已写的文件（版本化命名，不影响旧版）
        import os as _os
        for f in written_files:
            try:
                _os.remove(f)
            except OSError:
                pass
        raise
    finally:
        conn.close()


def _split_ext(filename: str) -> tuple[str, str]:
    import os

    stem, ext = os.path.splitext(filename)
    return stem, ext


def _slug(s: str) -> str:
    import re

    slug = re.sub(r"[^a-zA-Z0-9一-鿿]+", "-", s).strip("-").lower()
    return slug[:80]
