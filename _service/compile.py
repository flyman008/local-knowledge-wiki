"""raw → wiki 编译流水线。

方案§5.1 的 8 步：收件去重(ingest) → 原文保存(storage) → 解析(parser) →
资料理解(LLM) → 主题匹配 → 知识合并 → 校验保存 → 影响追踪。

首版落点：解析后交指定 LLM 产出摘要/关键事实/引用，写成 wiki 草稿页，
检索现有同名主题做增量合并；无变化不重写。草稿 status=draft 待人工 review。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import db as db_mod
import llm as llm_mod
import merge as merge_mod
import parser as parser_mod
import storage as storage_mod
import usage as usage_mod


UNDERSTAND_SYSTEM = (
    "你是企业知识库的编译引擎。基于给定的资料原文，产出结构化知识，"
    "不得编造原文没有的数字、金额、日期或能力。"
    "严格区分：厂商宣传 / 事实 / 推断。"
    "输出 Markdown，第一行必须是主题标题（用 `# 主题名` 格式，主题名是资料所指对象/产品/事件的稳定名称），"
    "随后包含以下小节：一句话、核心结论、关键数据/事实、适用版本/时间、来源引用。"
)


@dataclass
class CompileResult:
    ok: bool
    wiki_path: str | None = None
    error: str | None = None
    topic_id: str | None = None
    merged: bool = False


def compile_receipt(cfg, receipt_id: str, task_id: int | None = None, llm=None) -> CompileResult:
    """处理一个回执：解析→理解→写 wiki。返回是否成功。

    task_id：关联的任务 ID，用于用量记账；独立调用（非队列）时可为 None。
    llm：可注入的 LLM 实例（测试用模拟），默认 None 用真实模型。
    """
    conn = db_mod.get_conn()
    try:
        return _compile_receipt_inner(cfg, receipt_id, task_id, llm, conn)
    finally:
        # 兜底：任何未预期异常也保证连接关闭，释放写锁。
        # 否则 handler 抛异常（如 IntegrityError）时连接不关，写锁泄漏，
        # worker 线程若再死在线程里，外部连接都会被拒（database is locked）。
        try:
            conn.close()
        except Exception:
            pass


def _compile_receipt_inner(cfg, receipt_id: str, task_id: int | None, llm, conn) -> CompileResult:
    """compile_receipt 的实现体，共享外层连接。异常由外层 finally 兜底 close。"""
    storage = storage_mod.Storage(cfg)
    if llm is None:
        llm = llm_mod.LLM(cfg)

    receipt = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if not receipt:
        conn.close()
        return CompileResult(False, error="回执不存在")
    kb_id = receipt["kb_id"]
    target_kb_id = receipt["target_kb_id"] or kb_id

    # 每日材料上限闸门：达到则暂停，保留队列（方案§7）
    if usage_mod.material_limit_reached(cfg, target_kb_id):
        conn.close()
        return CompileResult(False, error=f"PAUSE:每日材料上限已到（{cfg.service.daily_material_limit}）")

    # 精确绑定：只用回执记下的 raw_path。缺失即待补正文，绝不取最近文件兜底
    # （否则只提交链接会误编译其他材料并标记 done，污染知识来源关系）。
    raw_path = receipt["raw_path"]
    if not raw_path or not Path(raw_path).exists():
        _record_failure(conn, receipt_id, "无原文文件（待补正文），不编译其他材料")
        conn.close()
        return CompileResult(False, error="无原文文件（待补正文）")
    conn.execute(
        "UPDATE receipts SET status='processing', updated_at=? WHERE id=?",
        (db_mod.now_iso(), receipt_id),
    )
    conn.commit()

    # 3. 解析
    parse = parser_mod.parse_file(raw_path, cfg, llm)
    if not parse.ok:
        # 解析失败不能覆盖旧 wiki；记录错误
        _record_failure(conn, receipt_id, parse.error or "解析失败")
        conn.close()
        return CompileResult(False, error=parse.error or "解析失败")

    # 部分成功（如部分扫描页 OCR 失败）也如实记录，不静默当完全成功
    if parse.needs_vision or parse.error:
        _record_failure(
            conn, receipt_id,
            parse.error or "部分内容待视觉识别，编译不完整",
        )
        conn.close()
        return CompileResult(
            False,
            error=parse.error or "部分内容待视觉识别（needs_vision）",
        )

    # 3.5 三层入库：解析稿落盘 + 三层快照链（raw → extract）
    import snapshot as snapshot_mod
    from pathlib import Path as _P

    parser_name = f"parser:{parse.kind}"
    raw_snap_id = snapshot_mod.record_raw_snapshot(
        conn, kb_id=target_kb_id, file_path=raw_path,
        source_name=_P(raw_path).name, parser=parser_name,
    )
    # 解析稿落盘（extracts/），内容 = parse.text（含页码/表格/图片说明）
    extract_base = _P(raw_path).stem
    extract_path = storage.save_extract(target_kb_id, extract_base, parse.text)
    extract_snap_id = snapshot_mod.record_extract_snapshot(
        conn, kb_id=target_kb_id, raw_snapshot_id=raw_snap_id,
        extract_path=str(extract_path), parser=parser_name,
    )
    # 关键：LLM 调用（understand）耗时长，先把三层快照提交释放写锁，
    # 避免并发请求（如 archive_full）撞 database is locked
    conn.commit()

    # 4. 资料理解
    try:
        res = llm.understand(parse.text, system=UNDERSTAND_SYSTEM)
    except llm_mod.LLMError as e:
        if e.kind in ("quota", "rate_limit", "auth"):
            conn.close()
            return CompileResult(False, error=f"PAUSE:{e.kind}:{e}")
        _record_failure(conn, receipt_id, f"LLM: {e.kind}: {e}")
        conn.close()
        return CompileResult(False, error=f"LLM {e.kind}")

    # 用量记账（复用主连接，避免与 compile 事务写锁冲突）
    usage_mod.record_usage(
        model_id=res.model,
        tokens_in=res.tokens_in,
        tokens_out=res.tokens_out,
        elapsed_ms=res.elapsed_ms,
        task_id=task_id,
        conn=conn,
    )

    # 5. 主题身份：优先用 LLM 理解的标题，提取不到才回退文件名（解耦文件名漂移）
    title = _title_from_text(res.text)
    topic_slug = _slug(title) or _slug_from_filename(raw_path)

    # 候选已有主题：隔离区 inbox 里同 slug 的 draft
    inbox_dir = storage.inbox_wiki_dir(target_kb_id)
    existing_draft = None
    draft_rel = f"{topic_slug}.md"
    if (inbox_dir / draft_rel).exists():
        existing_draft = (inbox_dir / draft_rel).read_text(encoding="utf-8", errors="replace")

    # 6. 语义合并决策（LLM 判断：add/nochange/revise/conflict）
    decision = merge_mod.merge_topic(llm, res.text, existing_draft)

    # 7. 按决策写 draft
    frontmatter = _build_frontmatter(target_kb_id, raw_path, merged=decision.action in ("revise", "conflict"))

    if decision.action == "nochange":
        import domains
        domains.bind(conn,receipt_id,target_kb_id,topic_slug)
        # 无新信息：完全不写文件、不增版本、不更新 FTS，只登记变化说明
        _record_change(
            conn, target_kb_id, topic_slug,
            merged=False, backup=None,
            merge_action="nochange", merge_summary=decision.summary or "无变化，未重写",
        )
        conn.execute(
            "UPDATE receipts SET status='done', updated_at=? WHERE id=?",
            (db_mod.now_iso(), receipt_id),
        )
        conn.commit()
        conn.close()
        return CompileResult(True, wiki_path=f"_inbox/{draft_rel}", topic_id=topic_slug, merged=False)

    if decision.action == "add":
        # 全新主题：但如果 slug 已存在（模型判无关新主题却撞名），另建 slug，不覆盖旧稿
        final_slug = topic_slug
        final_rel = draft_rel
        existing_topic_row = conn.execute(
            "SELECT * FROM topics WHERE kb_id=? AND id=?", (target_kb_id, topic_slug)
        ).fetchone()
        if existing_topic_row or existing_draft is not None:
            # slug 被占：另建带后缀的新主题，保护旧成果
            final_slug = f"{topic_slug}-{uuid4().hex[:6]}"
            final_rel = f"{final_slug}.md"
        wiki_content = frontmatter + "\n" + res.text.strip() + "\n"
        topic_id = final_slug
        write_rel = final_rel
    else:
        # revise / conflict：写回原 slug，覆盖前备份旧稿到 recovery
        if existing_draft is not None:
            cur_v = conn.execute(
                "SELECT version FROM topics WHERE kb_id=? AND id=?", (target_kb_id, topic_slug)
            ).fetchone()
            _backup_draft_to_recovery(cfg, target_kb_id, topic_slug, version=cur_v["version"] if cur_v else None)
        merged_body = decision.merged_text or _merge_wiki_body(existing_draft or "", res.text)
        conflict_note = ""
        if decision.action == "conflict":
            conflict_note = "\n\n## 冲突（待确认）\n\n" + "\n".join(
                f"- {c}" for c in decision.conflicts
            ) + "\n"
        wiki_content = frontmatter + "\n" + merged_body.rstrip() + conflict_note + "\n"
        topic_id = topic_slug
        write_rel = draft_rel

    draft_path = inbox_dir / write_rel
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(wiki_content, encoding="utf-8")

    # 更新主题表（topic 记录隔离区 draft）+ FTS 索引
    existing_topic = conn.execute(
        "SELECT * FROM topics WHERE kb_id=? AND id=?", (target_kb_id, topic_id)
    ).fetchone()
    if existing_topic:
        conn.execute(
            "UPDATE topics SET version=version+1, updated_at=? WHERE kb_id=? AND id=?",
            (db_mod.now_iso(), target_kb_id, topic_id),
        )
    else:
        conn.execute(
            """INSERT INTO topics(id, kb_id, name, topic_type, domain, status, version, wiki_path, updated_at)
               VALUES(?,?,?,?,?,?,1,?,?)""",
            (topic_id, target_kb_id, title or topic_id, "summary",
             _kb_domain(cfg, target_kb_id), "draft", f"_inbox/{write_rel}", db_mod.now_iso()),
        )

    _index_fts(conn, target_kb_id, topic_id, f"_inbox/{write_rel}", wiki_content)
    import domains
    domains.bind(conn,receipt_id,target_kb_id,topic_id)

    # 三层入库：wiki 成果快照（layer=wiki，prev 指向 extract 快照）
    import snapshot as snapshot_mod2
    from pathlib import Path as _P2

    snapshot_mod2.record_wiki_snapshot(
        conn, kb_id=target_kb_id, prev_snapshot_id=extract_snap_id,
        file_path=str(draft_path), parser=f"llm:{res.model}",
    )

    conn.execute(
        "UPDATE receipts SET status='done', updated_at=? WHERE id=?",
        (db_mod.now_iso(), receipt_id),
    )

    # 8. 影响追踪：登记变更（含合并判定与冲突）
    _record_change(
        conn, target_kb_id, topic_id,
        merged=decision.action in ("revise", "conflict"),
        backup=None,
        merge_action=decision.action,
        merge_summary=decision.summary,
        conflicts=decision.conflicts,
    )

    conn.commit()
    conn.close()

    return CompileResult(
        True,
        wiki_path=f"_inbox/{write_rel}",
        topic_id=topic_id,
        merged=decision.action in ("revise", "conflict"),
    )


def _slug_from_filename(path: str) -> str:
    import pathlib

    stem = pathlib.Path(path).stem
    slug = re.sub(r"[^a-zA-Z0-9一-鿿]+", "-", stem).strip("-").lower()
    return slug[:80] or "untitled"


def _title_from_text(text: str) -> str:
    """从 LLM 理解结果的第一行提取主题标题（`# 标题`），返回干净标题或空串。"""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            return re.sub(r"^#+\s*", "", line).strip()
        # 容错：第一段非 # 也当标题（去掉 markdown 标记）
        if not line.startswith(("一句话", "核心结论", "关键数据", "适用", "来源")):
            return line.strip(" #*")
    return ""


def _slug(s: str) -> str:
    s = s.strip()
    slug = re.sub(r"[^a-zA-Z0-9一-鿿]+", "-", s).strip("-").lower()
    return slug[:80] or ""


def _kb_domain(cfg, kb_id: str) -> str:
    kb = next((b for b in cfg.knowledge_bases if b.id == kb_id), None)
    return kb.domain if kb else ""


def _backup_draft_to_recovery(cfg, target_kb_id: str, slug: str, version: int | None = None) -> None:
    """把隔离区 inbox 里的旧 draft 备份到 recovery（revise/conflict 覆盖前调用）。

    备份名含版本号 + 时间戳 + 唯一 ID，避免同一秒连续修订互相覆盖。
    """
    import shutil
    import time

    import config as config_mod

    inbox = Path(cfg.research_wiki_path) / "_inbox" / target_kb_id
    src = inbox / f"{slug}.md"
    if not src.exists():
        return
    backup_root = config_mod.LOCAL_DIR / "recovery" / target_kb_id / "draft"
    backup_root.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    vtag = f"_v{version}" if version is not None else ""
    uniq = uuid4().hex[:6]
    shutil.copy2(src, backup_root / f"{slug}{vtag}_{ts}_{uniq}.md")


def _build_frontmatter(kb_id: str, raw_path: str, merged: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d")
    return (
        "---\n"
        f"type: summary\n"
        f"source: {raw_path}\n"
        f"updated: {now}\n"
        "status: draft\n"
        f"tags: [auto-compiled, {kb_id}]\n"
        f"merged: {str(merged).lower()}\n"
        "---\n"
    )


def _merge_wiki_body(old_content: str, new_text: str) -> str:
    """降级合并：保留旧正文，追加新证据章节（当 LLM 未返回 merged_text 时用）。"""
    body = old_content
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]
    return (
        body.rstrip()
        + "\n\n## 新增证据（自动编译）\n\n"
        + new_text.strip()
        + "\n"
    )


def _index_fts(conn, kb_id: str, doc_id: str, wiki_path: str, content: str) -> None:
    # FTS 范围按 (kb_id, doc_id) 删除，避免跨库同 doc_id 误删
    conn.execute("DELETE FROM wiki_fts WHERE kb_id=? AND doc_id=?", (kb_id, doc_id))
    conn.execute(
        "INSERT INTO wiki_fts(doc_id, kb_id, title, body) VALUES(?,?,?,?)",
        (doc_id, kb_id, doc_id, content),
    )


def _record_change(
    conn, kb_id: str, topic_id: str, merged: bool, backup,
    merge_action: str = "add", merge_summary: str = "", conflicts: list | None = None,
) -> None:
    """登记变更。merge_action ∈ add/nochange/revise/conflict；conflict 时 needs_confirm=1。"""
    action_label = {"add": "新建", "nochange": "无变化", "revise": "修正", "conflict": "冲突"}.get(
        merge_action, merge_action
    )
    summary = merge_summary or ("自动编译（" + action_label + "）")
    conn.execute(
        """INSERT INTO changes(topic_id, kb_id, from_version, to_version, summary, conflicts, needs_confirm, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (topic_id, kb_id, None, None, summary,
         "\n".join(conflicts) if conflicts else None,
         1 if merge_action == "conflict" else 0, db_mod.now_iso()),
    )
    if merge_action == 'conflict':
        import review
        topic = conn.execute('SELECT version,wiki_path FROM topics WHERE kb_id=? AND id=?',(kb_id,topic_id)).fetchone()
        if topic:
            review.add(conn,kb_id,topic_id,topic['version'],'conflict',summary,
                       [{'statement': c, 'locator': topic['wiki_path']} for c in (conflicts or [summary])])


def _record_failure(conn, receipt_id: str, error: str) -> None:
    conn.execute(
        "UPDATE receipts SET status='failed', error=?, updated_at=? WHERE id=?",
        (error, db_mod.now_iso(), receipt_id),
    )
    conn.commit()
