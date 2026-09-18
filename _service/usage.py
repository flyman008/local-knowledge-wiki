"""用量记账 + 预算/材料上限控制。

- 模型调用后写 usage_log（model_id、tokens_in/out、耗时、任务关联）。
- cost_estimate 仅在配置了可靠单价时填，未知不填零（方案§7）。
- daily_material_limit：每日处理材料数超限则暂停后续，保留队列。
"""
from __future__ import annotations

import db as db_mod


def record_usage(
    *,
    model_id: str,
    tokens_in: int,
    tokens_out: int,
    elapsed_ms: int,
    task_id: int | None = None,
    cost_estimate: float | None = None,
    conn=None,
) -> None:
    """写一条用量记录。cost_estimate 未知时传 None（落库为 NULL，不填 0）。

    conn 可选：调用方传入共享连接（如 compile 主连接），避免与主事务写锁冲突；
    不传则开新连接（用于独立记账场景）。
    """
    own_conn = conn is None
    if conn is None:
        conn = db_mod.get_conn()
    conn.execute(
        """INSERT INTO usage_log(task_id, model_id, tokens_in, tokens_out, cost_estimate, created_at)
           VALUES(?,?,?,?,?,?)""",
        (task_id, model_id, tokens_in, tokens_out, cost_estimate, db_mod.now_iso()),
    )
    if own_conn:
        conn.commit()
        conn.close()


def today_material_count(kb_id: str | None = None) -> int:
    """今日实际开始/完成处理的材料数（编译任务），不含原样归档。

    以 tasks 表当天 done 的任务数计（archive 不建 task，直接写 receipt done，
    所以 tasks.done 只统计真正走模型编译的材料）。
    """
    conn = db_mod.get_conn()
    if kb_id:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM tasks
               WHERE status='done' AND date(updated_at)=date('now')
                 AND receipt_id IN (SELECT id FROM receipts WHERE target_kb_id=?)""",
            (kb_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='done' AND date(updated_at)=date('now')"
        ).fetchone()
    conn.close()
    return row["n"] if row else 0


def material_limit_reached(cfg, kb_id: str | None = None) -> bool:
    """是否达到每日材料上限（按实际处理计数，不含归档）。达到则暂停后续任务。

    次日自动恢复：date(updated_at)=date('now') 只统计当天，隔天清零。
    """
    limit = cfg.service.daily_material_limit
    if limit <= 0:
        return False  # 0/负 = 不限制
    return today_material_count(kb_id) >= limit


def tokens_today(model_id: str | None = None) -> int:
    """今日累计 tokens（用于预算估算）。"""
    conn = db_mod.get_conn()
    if model_id:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens_in+tokens_out),0) AS n FROM usage_log WHERE model_id=? AND date(created_at)=date('now')",
            (model_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens_in+tokens_out),0) AS n FROM usage_log WHERE date(created_at)=date('now')"
        ).fetchone()
    conn.close()
    return row["n"] if row else 0
