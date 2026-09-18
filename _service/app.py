"""本地知识服务：FastAPI 入口。

五区域（方案§6）：收件与来源、知识浏览与问答、最近变化、待确认与研究缺口、运行与设置。
HTTP 收件接口与 MCP 共用这一套服务逻辑（方案§3.1）。
"""
from __future__ import annotations

import sys
import json
from typing import Literal
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config as config_mod
import compile as compile_mod
import db as db_mod
import ingest as ingest_mod
import taskqueue as queue_mod
import search as search_mod
import storage as storage_mod
import domains
import review

# Windows 控制台中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

cfg = config_mod.load_config()
config_mod.ensure_dirs(cfg)

app = FastAPI(title="Knowledge 本地知识服务")
from library_web import make_router
app.include_router(make_router(cfg))

@app.exception_handler(ValueError)
async def bad_value(request, exc):
    return JSONResponse({'error':str(exc)},status_code=400)

@app.get('/api/capabilities')
def capabilities():
    return {'taxonomy':'scopes-tags-v1','storage':'library','scopes':['weimob','external'],'tags_multiselect':True,'quality':'intake-quality-v1'}
_worker: queue_mod.Worker | None = None


def _check_api_key_at_startup() -> None:
    """启动时 fail-fast：token 缺失立即报错，不等到编译时才静默失败。

    token 来源：项目自持的 _local/.env（config.load_env_file 已加载）或显式注入的环境变量。
    不在启动时校验，会在「服务看似健康、实则编译不了」的状态下无声运行。
    """
    if not cfg.llm.api_key:
        raise RuntimeError(
            "缺少 API token：请在 D:\\Knowledge\\_local\\.env 写入 ANTHROPIC_AUTH_TOKEN=<token>，"
            "或启动前注入同名环境变量。服务已拒绝启动，避免静默编译失败。"
        )


def _should_auto_compile(target_kb_id: str) -> bool:
    """auto_compile 控制是否自动进入模型编译（不是是否自动迁入正式库）。

    - research-wiki：隔离整理区，显式白名单允许自动编译（收进来就要整理）。
    - 已登记库：按 config 的 auto_compile 字段（如 whatworth 公开库 false=不自动编译）。
    - 其他未登记域（含拼错的 kb_id）：一律不自动编译，避免未知域误触模型烧 token。
    """
    kb = next((b for b in cfg.knowledge_bases if b.id == target_kb_id), None)
    if kb is None:
        return False  # 未登记域：不自动编译，待确认
    return bool(kb.auto_compile)


class SubmitBody(BaseModel):
    kb_id: Literal['library'] = 'library'
    target_kb_id: Literal['library'] | None = None
    classification: dict | None = None
    url: str | None = None
    text: str | None = None
    filename: str | None = None
    source_hint: str | None = None
    note: str | None = None
    domain: str | None = None
    request_dedup_id: str | None = None


class ArchiveBody(BaseModel):
    kb_id: Literal['library'] = 'library'
    target_kb_id: Literal['library'] | None = None
    classification: dict | None = None
    title: str | None = None
    text: str | None = None
    url: str | None = None
    publisher: str | None = None
    fetched_at: str | None = None
    request_dedup_id: str | None = None


# ---------- 收件与来源 ----------

@app.post("/api/archive")
def archive_content(body: ArchiveBody):
    """成稿原样归档：不重写正文，登记版本/来源，进检索。"""
    import archive as archive_mod

    try:
        r = archive_mod.archive_content(
            cfg,
            classification=body.classification,
            kb_id=body.kb_id,
            target_kb_id=body.target_kb_id or body.kb_id,
            title=body.title,
            text=body.text,
            url=body.url,
            publisher=body.publisher,
            fetched_at=body.fetched_at,
            request_dedup_id=body.request_dedup_id,
        )
        return r.__dict__
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/archive_file")
async def archive_file(
    kb_id: Literal['library'] = Form('library'),
    target_kb_id: Literal['','library'] = Form(''),
    classification: str = Form('{}'),
    title: str = Form(""),
    url: str = Form(""),
    publisher: str = Form(""),
    file: UploadFile = File(...),
):
    """成稿原样归档（文件上传）：文件正文原样保存可检索，不重写。"""
    import archive as archive_mod

    data = await file.read()
    try:
        r = archive_mod.archive_content(
            cfg,
            classification=json.loads(classification),
            kb_id=kb_id,
            target_kb_id=target_kb_id or kb_id,
            title=title or None,
            url=url or None,
            publisher=publisher or None,
            file_data=data,
            filename=file.filename,
        )
        return r.__dict__
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


class ArchiveFullBody(BaseModel):
    correction_reason: str | None = None
    base_version: int | None = None
    source_metadata: dict | None = None
    kb_id: Literal['library'] = 'library'
    classification: dict | None = None
    title: str
    extract_text: str | None = None
    wiki_text: str | None = None
    url: str | None = None
    publisher: str | None = None
    parser: str | None = None
    parse_complete: bool = True  # 客户端显式声明解析是否完整（缺页/识图失败=False）
    missing_pages: list[int] | None = None  # 缺失/失败页码
    request_dedup_id: str | None = None


@app.post("/api/archive_full")
def archive_full(body: ArchiveFullBody):
    """客户端已整理的三层入库（原件缺省，至少提交解析稿或成果），后台 0 模型调用。"""
    import archive_full as af

    try:
        r = af.archive_full(
            cfg,
            classification=body.classification,
            kb_id=body.kb_id,
            title=body.title,
            extract_text=body.extract_text,
            wiki_text=body.wiki_text,
            url=body.url,
            publisher=body.publisher,
            parser=body.parser,
            correction_reason=body.correction_reason,
            base_version=body.base_version,
            parse_complete=body.parse_complete,
            missing_pages=body.missing_pages,
            request_dedup_id=body.request_dedup_id,
            source_metadata=body.source_metadata,
        )
        return r.__dict__
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/archive_full_file")
async def archive_full_file(
    correction_reason: str = Form(""),
    base_version: int | None = Form(None),
    source_metadata: str = Form(""),
    kb_id: Literal['library'] = Form('library'),
    classification: str = Form('{}'),
    title: str = Form(...),
    extract_text: str = Form(""),
    wiki_text: str = Form(""),
    url: str = Form(""),
    publisher: str = Form(""),
    parser: str = Form(""),
    parse_complete: str = Form("true"),
    missing_pages: str = Form(""),
    file: UploadFile = File(...),  # 原件
):
    """客户端已整理的三层入库（带原件文件），后台 0 模型调用。"""
    import archive_full as af

    data = await file.read()
    # missing_pages 以逗号分隔的页码传入，如 "2,5"；parse_complete 为 "true"/"false"
    try:
        mp = [int(x.strip()) for x in missing_pages.split(",") if x.strip()] if missing_pages else None
    except ValueError:
        return JSONResponse({"error": "missing_pages 需为逗号分隔的整数页码"}, status_code=400)
    pc = parse_complete.strip().lower() not in ("false", "0", "no")
    try:
        import json
        metadata = json.loads(source_metadata) if source_metadata else None
        r = af.archive_full(
            cfg,
            classification=json.loads(classification),
            kb_id=kb_id,
            source_metadata=metadata,
            title=title,
            raw_data=data,
            raw_filename=file.filename,
            extract_text=extract_text or None,
            wiki_text=wiki_text or None,
            url=url or None,
            publisher=publisher or None,
            parser=parser or None,
            correction_reason=correction_reason or None,
            base_version=base_version,
            parse_complete=pc,
            missing_pages=mp,
        )
        return r.__dict__
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/submit")
def submit_material(body: SubmitBody):
    r = ingest_mod.submit_material(
        cfg,
        classification=body.classification,
        kb_id=body.kb_id,
        target_kb_id=body.target_kb_id,
        url=body.url,
        text=body.text,
        source_hint=body.source_hint,
        note=body.note,
        domain=body.domain,
        request_dedup_id=body.request_dedup_id,
    )
    # auto_compile 控制是否自动进入模型编译（不是是否自动迁入正式库）。
    # 有正文且非重复，且目标库 auto_compile=true 才入队；否则只收件、留待手动编译。
    if r.status in ("received",) and not r.is_duplicate and (body.text or body.url):
        if _should_auto_compile(body.target_kb_id or body.kb_id):
            queue_mod.TaskQueue(cfg).enqueue(r.receipt_id)
    return r.__dict__


@app.post("/api/submit_file")
async def submit_file(
    kb_id: Literal['library'] = Form('library'),
    target_kb_id: Literal['','library'] = Form(''),
    classification: str = Form('{}'),
    url: str = Form(""),
    file: UploadFile = File(...),
):
    data = await file.read()
    r = ingest_mod.submit_material(
        cfg,
        classification=json.loads(classification),
        kb_id=kb_id,
        target_kb_id=target_kb_id or None,
        url=url or None,
        file_data=data,
        filename=file.filename,
    )
    if r.status == "received" and not r.is_duplicate:
        if _should_auto_compile(target_kb_id or kb_id):
            queue_mod.TaskQueue(cfg).enqueue(r.receipt_id)
    return r.__dict__


@app.get("/api/receipt/{receipt_id}")
def get_receipt(receipt_id: str):
    r = ingest_mod.get_receipt(cfg, receipt_id)
    return r or JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/receipts")
def list_receipts(limit: int = 50):
    return ingest_mod.list_receipts(cfg, limit)


@app.get("/api/daily_status")
def daily_status(kb_id: str | None = None):
    conn = db_mod.get_conn()
    where = "WHERE 1=1"
    args: list = []
    if kb_id:
        where += " AND kb_id=?"
        args.append(kb_id)
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS n FROM receipts {where} GROUP BY status", args
    ).fetchall()
    conn.close()
    return {r["status"]: r["n"] for r in rows}


@app.get("/api/knowledge_bases")
def list_knowledge_bases():
    return domains.catalog()


# ---------- 知识浏览与问答 ----------

@app.get("/api/search")
def search(q: str, kb_id: str | None = None, limit: int = 10, scope: str | None = None, tag: str | None = None):
    return search_mod.search(cfg, q, kb_id, limit, scope, tag)


@app.get("/api/ask")
def ask(q: str, kb_id: str | None = None, scope: str | None = None, tag: str | None = None):
    return search_mod.answer(cfg, q, kb_id, scope, tag)


@app.get("/api/topics")
def list_topics(kb_id: str | None = None, limit: int = 100):
    conn = db_mod.get_conn()
    if kb_id:
        members = domains.members_for(kb_id)
        placeholders = ",".join("?" for _ in members)
        rows = conn.execute(
            f"SELECT * FROM topics WHERE kb_id IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
            (*members, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM topics ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    result = [dict(domains.describe_record(r),classification=domains.get(conn,r['kb_id'],r['id'])) for r in rows]
    conn.close()
    return result


# ---------- 隔离区 inbox 与迁入闸门 ----------

@app.get("/api/inbox")
def list_inbox(target_kb_id: str | None = None):
    """列出隔离区里待迁入的编译 draft。"""
    storage = storage_mod.Storage(cfg)
    inbox_root = storage.inbox_root() / "_inbox"
    result = []
    if inbox_root.exists():
        if target_kb_id:
            dirs = [target_kb_id]
        else:
            dirs = [d.name for d in inbox_root.iterdir() if d.is_dir()]
        for d in dirs:
            kb_dir = inbox_root / d
            for f in kb_dir.glob("*.md"):
                result.append({
                    "target_kb_id": d,
                    "slug": f.stem,
                    "path": str(f.relative_to(inbox_root)),
                    "size": f.stat().st_size,
                })
    return result


class MigrateBody(BaseModel):
    target_kb_id: str
    slug: str
    # 迁入后目标库内的相对路径（默认 entities/<slug>.md 或 summaries/<slug>.md 由前端/调用方决定）
    rel_path: str | None = None


@app.post("/api/migrate")
def migrate(body: MigrateBody):
    """显式迁入：把隔离区 draft 写入目标库 wiki，先备份目标库旧版。

    这是唯一允许写存量库 wiki 的入口。迁入后 draft 从 inbox 移除。
    """
    storage = storage_mod.Storage(cfg)
    try:
        inbox_dir = storage.inbox_wiki_dir(body.target_kb_id)
        src = storage_mod._safe_join(inbox_dir, f"{body.slug}.md")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not src.exists():
        return JSONResponse({"error": "draft 不存在"}, status_code=404)

    rel_path = body.rel_path or f"summaries/{body.slug}.md"
    content = src.read_text(encoding="utf-8", errors="replace")
    try:
        target, backup = storage.migrate_to_library(body.target_kb_id, rel_path, content)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # 迁入后：更新 topics 的 wiki_path 指向正式库，status 待 review；移出 inbox
    conn = db_mod.get_conn()
    conn.execute(
        "UPDATE topics SET wiki_path=?, updated_at=? WHERE kb_id=? AND id=?",
        (rel_path, db_mod.now_iso(), body.target_kb_id, body.slug),
    )
    conn.commit()
    conn.close()
    src.unlink(missing_ok=True)

    return {
        "migrated": True,
        "target": str(target),
        "backup": str(backup) if backup else None,
        "rel_path": rel_path,
    }


# ---------- 最近变化 / 待确认 / 研究缺口 ----------

@app.get("/api/changes")
def list_changes(limit: int = 50):
    conn = db_mod.get_conn()
    rows = conn.execute(
        "SELECT * FROM changes ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/pending")
def pending_confirm(limit: int = 50):
    conn = db_mod.get_conn()
    rows = conn.execute(
        "SELECT * FROM topics WHERE status='draft' ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get('/api/reviews')
def review_list(kb_id: str | None = None):
    return review.listing(kb_id)


@app.post('/api/reviews/{operation}')
def review_action(operation: str, body: dict):
    try:
        return review.execute(operation, body)
    except ValueError as e:
        return JSONResponse(status_code=400, content={'error':str(e)})


@app.get("/api/gaps")
def research_gaps():
    conn = db_mod.get_conn()
    rows = conn.execute(
        "SELECT * FROM receipts WHERE status IN ('failed','needs_body') ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- 运行与设置 ----------

@app.get("/api/status")
def service_status():
    conn = db_mod.get_conn()
    pending = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='pending'").fetchone()["n"]
    running = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='running'").fetchone()["n"]
    failed = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='failed'").fetchone()["n"]
    conn.close()
    return {
        "worker": "running" if (_worker and _worker._thread and _worker._thread.is_alive()) else "stopped",
        "pending": pending,
        "running": running,
        "failed": failed,
        "model": {"text": cfg.llm.text_model, "vision": cfg.llm.vision_model},
    }


# ---------- 管理页 ----------

@app.get("/")
def index():
    web = Path(__file__).parent / "web" / "index.html"
    return FileResponse(str(web))


def start_worker():
    global _worker
    from compile import compile_receipt

    def handler(receipt_id: str, task_id: int | None = None):
        r = compile_mod.compile_receipt(cfg, receipt_id, task_id)
        return r.ok, r.error

    _worker = queue_mod.Worker(cfg, queue_mod.TaskQueue(cfg), handler)
    _worker.start()
    return _worker


def main():
    import uvicorn

    # 启动即校验 token（fail-fast），缺 token 直接拒绝启动，不静默带病运行
    _check_api_key_at_startup()
    start_worker()
    uvicorn.run(app, host=cfg.service.host, port=cfg.service.port)


if __name__ == "__main__":
    main()
