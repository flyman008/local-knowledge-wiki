"""语义级主题合并决策。

方案§5.1 第 5-6 步：主题匹配 + 知识合并。核心是把「合并决策」做成纯函数协议，
LLM 是可替换的决策者——真实模型与模拟模型喂进来同一套接口。

四种判定：
- add：新证据对应全新主题，直接新建。
- nochange：新证据没有带来新信息，不重写主题（方案铁律：无变化不重写）。
- revise：新证据补充/修正已有主题，产出合并后的新版本。
- conflict：新证据与已有主题冲突（价格/版本/口径不同），保留双方依据，标记待确认。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class MergeDecision:
    action: str  # add | nochange | revise | conflict
    merged_text: str = ""
    summary: str = ""
    conflicts: list[str] = field(default_factory=list)


MERGE_SYSTEM = (
    "你是企业知识库的主题合并引擎。给定「已有主题内容」和「新证据」，判断应如何合并。"
    "四种可能，只能选其一：\n"
    "1. add：新证据是一个全新主题，与已有主题无关，应新建。\n"
    "2. nochange：新证据没有带来任何新信息（与已有主题实质重复），不应重写。\n"
    "3. revise：新证据补充或修正了已有主题的事实，应产出合并后的新版本，保留旧正确结论。\n"
    "4. conflict：新证据与已有主题在关键事实上冲突（价格、版本、口径、时间等），"
    "应保留双方依据与口径，标记待确认，不强行给唯一答案。\n"
    "严格区分厂商宣传/事实/推断；不得编造；无变化不要为了重写而重写。\n"
    '输出 JSON：{"action": "add|nochange|revise|conflict", '
    '"merged_text": "合并后的完整 markdown（add 时可为空）", '
    '"summary": "一句话变化说明", "conflicts": ["冲突点1", "冲突点2"]}'
)


def merge_topic(llm, new_evidence: str, existing_topic: str | None) -> MergeDecision:
    """调用决策者（LLM 或模拟）判断如何合并。

    existing_topic 为 None 时直接 add（无需模型判断）。
    """
    if not existing_topic:
        return MergeDecision(action="add")

    prompt = f"已有主题内容：\n{existing_topic[:4000]}\n\n新证据：\n{new_evidence[:4000]}"
    try:
        res = llm.complete(
            [{"role": "user", "content": prompt}],
            system=MERGE_SYSTEM,
            max_tokens=2000,
        )
    except Exception:  # noqa: BLE001
        # 模型不可用降级：按 revise 追加（保守，保留旧内容），不静默失败
        return MergeDecision(action="revise", merged_text="", summary="合并失败降级为追加")

    return _parse_decision(res.text)


def _parse_decision(raw: str) -> MergeDecision:
    """解析 LLM 返回的 JSON；解析失败降级为 revise。"""
    try:
        # 容忍 markdown 代码块包裹
        txt = raw.strip()
        if txt.startswith("```"):
            txt = txt.strip("`")
            if txt.startswith("json"):
                txt = txt[4:]
        data = json.loads(txt)
        action = data.get("action", "revise")
        if action not in ("add", "nochange", "revise", "conflict"):
            action = "revise"
        return MergeDecision(
            action=action,
            merged_text=data.get("merged_text", ""),
            summary=data.get("summary", ""),
            conflicts=data.get("conflicts", []),
        )
    except (json.JSONDecodeError, AttributeError):
        return MergeDecision(action="revise", merged_text="", summary="决策解析失败降级为追加")
