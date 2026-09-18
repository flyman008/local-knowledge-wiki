"""全文检索 + 问答。

检索策略（方案§6：先全文检索+别名匹配，不预设向量库）：
- 英文/数字/带空格的查询 → FTS5 MATCH（需词法安全化，避免特殊字符触发语法错误）。
- 中文查询 → FTS5 默认 tokenizer 对中文不分词，改用 LIKE %词% 召回兜底，
  避免「会面」「AI会面是什么？」这类自然语言搜不到或报错。
- 两种都失败/空结果时，降级到标题 LIKE 匹配。
"""
from __future__ import annotations

import re
from typing import Any

import db as db_mod
import llm as llm_mod
import usage as usage_mod
import domains
import authority
import json
import review

# FTS5 查询里需要转义/剔除的特殊字符（否则触发 syntax error）
_FTS_UNSAFE = re.compile(r'[?*"\'()\[\]{}:,;!&|^~<>]')


def _tokenize_chinese(q: str) -> list[str]:
    """中文按 n-gram（2字为主）拆词，用于 LIKE 召回。"""
    # 提取中文连续串
    han = re.findall(r"[一-鿿]+", q)
    grams = set()
    for seg in han:
        if len(seg) >= 2:
            for i in range(len(seg) - 1):
                grams.add(seg[i:i + 2])
        if len(seg) == 1:
            grams.add(seg)
    return list(grams)


def _safe_fts_query(q: str) -> str:
    """把用户输入转成安全的 FTS5 MATCH 表达式。剔除特殊字符，保留可检索词。"""
    cleaned = _FTS_UNSAFE.sub(" ", q)
    terms = [t for t in cleaned.split() if t]
    if not terms:
        return ""
    # 用 OR 扩大召回（比 AND 宽松，避免全词都命中才返回）
    return " OR ".join(f'"{t}"' for t in terms)


def search(cfg, query: str, kb_id: str | None = None, limit: int = 10, scope=None, tag=None) -> list[dict[str, Any]]:
    label_where,label_args = domains.filter_sql(scope,tag)
    conn = db_mod.get_conn()
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_rows(rows):
        for r in rows:
            key = (r["kb_id"], r["doc_id"])
            if key not in seen:
                seen.add(key)
                item = domains.describe_record(r)
                item['classification'] = domains.get(conn,r['kb_id'],r['doc_id'])
                item["source_metadata"] = authority.get_metadata(conn, r["kb_id"], r["doc_id"])
                item['review_items'] = review.context(conn, r['kb_id'], r['doc_id'])
                topic = conn.execute('SELECT version FROM topics WHERE kb_id=? AND id=?',(r['kb_id'],r['doc_id'])).fetchone()
                item['topic_version'] = topic['version'] if topic else None
                item['review_status'] = 'pending' if any(i['status']=='pending' and i['applies_to_current_version'] for i in item['review_items']) else 'not_fully_verified'
                item['active_policies'] = review.policies(conn)
                results.append(item)

    members = domains.members_for(kb_id)
    base_where = "kb_id IN (" + ",".join("?" for _ in members) + ")" if members else "1=1"
    base_args: list[Any] = list(members)
    base_where += ' AND ' + label_where
    base_args.extend(label_args)

    # 1) FTS5 检索（英文/数字/词条，安全化后）
    fts_q = _safe_fts_query(query)
    if fts_q:
        try:
            if kb_id or scope or tag:
                rows = conn.execute(
                    f"""SELECT doc_id, kb_id, snippet(wiki_fts, 3, '[', ']', '…', 12) AS snip
                       FROM wiki_fts WHERE wiki_fts MATCH ? AND {base_where} ORDER BY rank LIMIT ?""",
                    (fts_q, *base_args, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT doc_id, kb_id, snippet(wiki_fts, 3, '[', ']', '…', 12) AS snip
                       FROM wiki_fts WHERE wiki_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (fts_q, limit),
                ).fetchall()
            add_rows(rows)
        except Exception:  # noqa: BLE001
            pass  # FTS 异常降级到 LIKE

    # 2) 中文 LIKE 召回（n-gram），补充 FTS 漏掉的中文
    grams = _tokenize_chinese(query)
    if grams and len(results) < limit:
        # 用最长的 2-gram 组合做 LIKE，避免短 gram 噪声
        for g in grams[:5]:
            if len(results) >= limit:
                break
            like = f"%{g}%"
            rows = conn.execute(
                f"""SELECT doc_id, kb_id, substr(body, 1, 120) AS snip
                    FROM wiki_fts WHERE {base_where} AND (title LIKE ? OR body LIKE ?) LIMIT ?""",
                (*base_args, like, like, limit - len(results)),
            ).fetchall()
            add_rows(rows)

    conn.close()
    return results


def answer(cfg, question: str, kb_id: str | None = None, scope=None, tag=None) -> dict[str, Any]:
    """问答：先检索 wiki，命中则交 LLM 综合引用来源；否则列缺口。"""
    conn = db_mod.get_conn()
    llm = llm_mod.LLM(cfg)

    hits = search(cfg, question, kb_id, limit=8, scope=scope, tag=tag)
    if not hits:
        conn.close()
        return {"answer": "", "sources": [], "gaps": ["无匹配知识，需补充资料"]}

    # 收集命中页正文
    contexts: list[dict] = []
    for h in hits:
        row = conn.execute(
            "SELECT title, body FROM wiki_fts WHERE kb_id=? AND doc_id=?",
            (h["kb_id"], h["doc_id"]),
        ).fetchone()
        if row:
            contexts.append({"doc_id": f"{h['kb_id']}:{h['doc_id']}", "body": row["body"][:6000],
                             "metadata": h["source_metadata"], "truncated": len(row["body"]) > 6000,
                             'review_items':h['review_items'], 'active_policies':h['active_policies']})

    system = (
        "你是知识问答引擎。仅根据提供的知识页内容回答，引用来源用 [[页面名]]。"
        "知识页没有的信息，明确说'知识库未覆盖'，不要编造。"
        "只看到截断片段时不能推断原文未涉及某项事实。"
        + authority.ANSWER_POLICY
    )
    ctx_text = json.dumps(contexts, ensure_ascii=False)
    try:
        res = llm.complete(
            [{"role": "user", "content": f"问题：{question}\n\n知识页：\n{ctx_text}"}],
            system=system,
        )
    except llm_mod.LLMError as e:
        conn.close()
        return {"answer": f"（问答调用失败：{e.kind}）", "sources": [h["doc_id"] for h in hits], "gaps": []}

    # 问答用量记账
    try:
        usage_mod.record_usage(
            model_id=res.model,
            tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            elapsed_ms=res.elapsed_ms,
            task_id=None,
        )
    except Exception:  # noqa: BLE001
        pass

    conn.close()
    return {
        "answer": ('【待核实提醒】命中材料有未解决确认项；以下回答不能视为已核实结论。\n' if any(h['review_status']=='pending' for h in hits) else '') + res.text,
        "sources": [h["doc_id"] for h in hits],
        "source_details": hits,
        "source_policy": "source-policy-v1",
        "gaps": [],
        "tokens_in": res.tokens_in,
        "tokens_out": res.tokens_out,
    }
