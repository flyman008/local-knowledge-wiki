"""archive_content：成稿原样归档（可检索，不重写正文）。

与 submit 的区别：submit 走「解析→LLM 理解→编译 wiki」；
archive 是「成稿原样保存」——正文不交给 LLM 重写，直接登记版本/来源/获取时间，
并把原文进 FTS 索引，保存后即可检索（方案 v0.3：归档成果必须可检索，不能只落 raw）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import db as db_mod
import storage as storage_mod
import domains
import json


@dataclass
class ArchiveResult:
    receipt_id: str
    doc_id: str
    is_duplicate: bool
    version: int = 1
    conflict: bool = False
    content_status: str = 'readable'
    saved_layers: dict = field(default_factory=dict)
    complete: bool = True


def is_placeholder(body):
    import re
    return bool(re.fullmatch(r'\[[^\n]+ 文件：[^\n]+，\d+ 字节，正文需解析后检索\]', (body or '').strip()))


def describe_content(receipt):
    """Derive legacy raw-only status without rewriting historical receipts."""
    from pathlib import Path
    saved = receipt.get('saved_layers')
    if isinstance(saved, str):
        saved = json.loads(saved)
    raw_only = receipt.get('status') == 'needs_parse' or (
        not saved and (receipt.get('dedup_key') or '').startswith('archive:file:')
        and _decode_body(b'', Path(receipt.get('raw_path') or '').name) is None)
    if raw_only:
        receipt.update(content_status='needs_parse', saved_layers={'raw':True,'extract':False,'wiki':False}, complete=False)
    else:
        receipt['content_status'] = 'readable' if (saved or {}).get('wiki') else 'unknown'
    return receipt


def archive_content(
    cfg,
    *,
    kb_id: str,
    target_kb_id: str,
    title: str | None = None,
    text: str | None = None,
    file_data: bytes | None = None,
    filename: str | None = None,
    url: str | None = None,
    publisher: str | None = None,
    fetched_at: str | None = None,
    request_dedup_id: str | None = None,
    classification: dict | None = None,
) -> ArchiveResult:
    """原样归档一份成稿。正文/文件原样保存，进 FTS 索引可检索，不重写。

    稳定 doc_id 由标题/文件名推导；同 doc_id 再次归档 = 新版本（version+1），
    旧正文备份到 recovery，登记变更与冲突确认。返回版本号。
    """
    classification = domains.normalize(classification)
    conn = db_mod.get_conn()
    storage = storage_mod.Storage(cfg)

    kb_id = kb_id or "research-wiki"
    target_kb_id = target_kb_id or kb_id

    # 去重键加 archive: 前缀，与 submit 的 text:/file:/req: 分离 ——
    # 两种处理意图不能互相抵消（原件可复用，回执/成果各自独立）。
    if request_dedup_id:
        dedup_key = f"archive:req:{kb_id}:{target_kb_id}:{request_dedup_id}"
    elif file_data:
        dedup_key = f"archive:file:{kb_id}:{target_kb_id}:{storage_mod.content_hash(file_data)}"
    elif text:
        dedup_key = f"archive:text:{kb_id}:{target_kb_id}:{storage_mod.normalize_hash(text)}"
    else:
        dedup_key = f"archive:none:{uuid.uuid4().hex}"

    dedup_key += ':' + storage_mod.normalize_hash(json.dumps(classification,sort_keys=True))
    existing = conn.execute(
        "SELECT id, status, saved_layers FROM receipts WHERE dedup_key=? ORDER BY created_at DESC LIMIT 1",
        (dedup_key,),
    ).fetchone()
    if existing:
        link = conn.execute('SELECT doc_id,topic_version FROM classifications WHERE receipt_id=?', (existing['id'],)).fetchone()
        raw_only = existing['status'] == 'needs_parse' or (file_data is not None and _decode_body(file_data, filename or '') is None)
        conn.close()
        doc_id = link['doc_id'] if link else _slug(title) or _slug(filename) or ""
        return ArchiveResult(existing["id"], doc_id, is_duplicate=True,
                             version=link['topic_version'] if link else 1,
                             content_status='needs_parse' if raw_only else 'readable',
                             saved_layers=json.loads(existing['saved_layers']) if existing['saved_layers'] else {'raw':True,'extract':False,'wiki':not raw_only}, complete=not raw_only)

    receipt_id = f"rcp_{uuid.uuid4().hex[:12]}"

    # 原样落盘（raw 只追加）
    raw_path = None
    body_text = None
    if file_data and filename:
        raw_path = storage.save_raw(kb_id, filename, file_data).raw_path
        body_text = _decode_body(file_data, filename)
    elif text:
        raw_path = storage.save_raw(
            kb_id, _derive_filename(url, filename, title), text.encode("utf-8")
        ).raw_path
        body_text = text

    if raw_path is None:
        conn.close()
        raise ValueError("archive_content 需要正文或文件")

    # Binary archive means raw storage only. Never turn a placeholder into knowledge,
    # and never replace an existing readable topic with an unparsed attachment.
    if body_text is None:
        now = db_mod.now_iso()
        doc_id = _slug(title) or _slug(filename) or f'raw-{uuid.uuid4().hex[:8]}'
        if conn.execute('SELECT 1 FROM topics WHERE kb_id=? AND id=?', (target_kb_id,doc_id)).fetchone():
            doc_id += '-raw-' + uuid.uuid4().hex[:8]
        saved = {'raw':True,'extract':False,'wiki':False}
        conn.execute('''INSERT INTO receipts(id,kb_id,target_kb_id,source_url,status,dedup_key,raw_path,saved_layers,parse_complete,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                     (receipt_id,kb_id,target_kb_id,url,'needs_parse',dedup_key,raw_path,json.dumps(saved),0,now,now))
        conn.execute('''INSERT INTO topics(id,kb_id,name,topic_type,domain,status,version,wiki_path,updated_at)
                        VALUES(?,?,?,?,?,?,1,NULL,?)''', (doc_id,target_kb_id,title or filename or doc_id,'raw',_kb_domain(cfg,target_kb_id),'needs_parse',now))
        domains.save(conn,receipt_id,classification,target_kb_id,doc_id,1)
        conn.commit()
        conn.close()
        return ArchiveResult(receipt_id,doc_id,False,content_status='needs_parse',saved_layers=saved,complete=False)

    now = db_mod.now_iso()
    conn.execute(
        """INSERT INTO receipts(id, kb_id, target_kb_id, source_url, status, dedup_key, raw_path, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (receipt_id, kb_id, target_kb_id, url, "done", dedup_key, raw_path, now, now),
    )

    # 稳定 doc_id（由标题/文件名推导，跨版本稳定）
    doc_id = _slug(title) or _slug(filename) or f"archive-{uuid.uuid4().hex[:8]}"

    # 版本链：同 doc_id 已存在 = 新版本；否则 version=1
    prev = conn.execute(
        "SELECT version, wiki_path FROM topics WHERE kb_id=? AND id=?", (target_kb_id, doc_id)
    ).fetchone()
    new_version = (prev["version"] + 1) if prev else 1
    conflict = prev is not None  # 同标题再次归档 → 视为潜在冲突，需人确认

    if prev:
        # 旧成稿正文备份到 recovery（版本链，archive 的稿在隔离区 inbox，不走目标库 wiki）
        _backup_archive_doc(cfg, target_kb_id, doc_id, prev["version"])
        conn.execute(
            "UPDATE topics SET version=?, wiki_path=?, updated_at=? WHERE kb_id=? AND id=?",
            (new_version, f"_inbox/{doc_id}.md", now, target_kb_id, doc_id),
        )
    else:
        conn.execute(
            """INSERT INTO topics(id, kb_id, name, topic_type, domain, status, version, wiki_path, updated_at)
               VALUES(?,?,?,?,?,?,1,?,?)""",
            (doc_id, target_kb_id, title or doc_id, "archive", _kb_domain(cfg, target_kb_id),
             "draft", f"_inbox/{doc_id}.md", now),
        )

    # 变更登记：同标题新版本 = 冲突确认
    conn.execute(
        """INSERT INTO changes(topic_id, kb_id, from_version, to_version, summary, conflicts, needs_confirm, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (doc_id, target_kb_id, (prev["version"] if prev else None), new_version,
         "原样归档" + ("（同标题新版本）" if prev else "（新建）"),
         "同标题存在不同正文版本，需确认是否替换" if prev else None,
         1 if prev else 0, now),
    )

    # 原文进 FTS 索引（更新为新版本正文）
    _index_archive(conn, doc_id, target_kb_id, title or doc_id, body_text)

    # 隔离区 inbox 存成稿 markdown（带版本号 + 来源头）
    inbox_dir = storage.inbox_wiki_dir(target_kb_id)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    archive_md = _build_archive_md(title, url, publisher, fetched_at or now, body_text, new_version)
    (inbox_dir / f"{doc_id}.md").write_text(archive_md, encoding="utf-8")

    domains.save(conn,receipt_id,classification,target_kb_id,doc_id,new_version)
    conn.commit()
    conn.close()
    return ArchiveResult(receipt_id, doc_id, is_duplicate=False, version=new_version, conflict=conflict)


def _decode_body(data: bytes, filename: str) -> str | None:
    """按扩展名解码文本；二进制返回 None，仅保存原件，不生成占位正文。"""
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    if ext in ("txt", "md", "html", "htm", "csv", "json", "xml"):
        for enc in ("utf-8", "gbk", "utf-8-sig"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
    # 二进制/办公/图片：不转文本，返回说明（可检索性交给后续解析）
    return None


def _derive_filename(url, filename, title):
    if filename:
        return filename
    if title:
        return f"{title}.txt"
    if url:
        base = url.rstrip("/").split("/")[-1] or "material"
        return f"{base}.txt"
    return f"archive_{uuid.uuid4().hex[:8]}.txt"


def _slug(s: str | None) -> str:
    import re

    if not s:
        return ""
    slug = re.sub(r"[^a-zA-Z0-9一-鿿]+", "-", s).strip("-").lower()
    return slug[:80]


def _kb_domain(cfg, kb_id: str) -> str:
    kb = next((b for b in cfg.knowledge_bases if b.id == kb_id), None)
    return kb.domain if kb else ""


def _build_archive_md(title, url, publisher, fetched_at, body, version: int) -> str:
    head = (
        "---\n"
        f"type: archive\n"
        f"title: {title or ''}\n"
        f"source_url: {url or ''}\n"
        f"publisher: {publisher or ''}\n"
        f"fetched_at: {fetched_at}\n"
        f"version: {version}\n"
        "status: draft\n"
        "---\n\n"
    )
    return head + body


def _index_archive(conn, doc_id: str, kb_id: str, title: str, body: str) -> None:
    conn.execute("DELETE FROM wiki_fts WHERE kb_id=? AND doc_id=?", (kb_id, doc_id))
    conn.execute(
        "INSERT INTO wiki_fts(doc_id, kb_id, title, body) VALUES(?,?,?,?)",
        (doc_id, kb_id, title, body),
    )


def _backup_archive_doc(cfg, target_kb_id: str, doc_id: str, old_version: int) -> None:
    """把旧版本成稿从隔离区 inbox 备份到 recovery，保留版本链。备份名含唯一 ID 防覆盖。"""
    import config as config_mod
    import time
    from pathlib import Path

    inbox = Path(cfg.research_wiki_path) / "_inbox" / target_kb_id
    src = inbox / f"{doc_id}.md"
    if not src.exists():
        return
    backup_root = config_mod.LOCAL_DIR / "recovery" / target_kb_id / "archive"
    backup_root.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    uniq = uuid.uuid4().hex[:6]
    dest = backup_root / f"{doc_id}_v{old_version}_{ts}_{uniq}.md"
    import shutil

    shutil.copy2(src, dest)
