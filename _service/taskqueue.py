"""持久任务队列 + 单 Worker 顺序处理。

- SQLite 持久队列：进程退出不丢任务。
- 单 Worker 顺序处理（方案§7），分阶段保存进度，有限重试，额度不足暂停。
- 启动时恢复未完成任务。
"""
from __future__ import annotations

import time
import threading
from typing import Callable

import db as db_mod


class TaskQueue:
    def __init__(self, cfg):
        self.cfg = cfg
        self.max_retries = cfg.service.max_retries

    def enqueue(self, receipt_id: str, stage: str = "ingest") -> int:
        conn = db_mod.get_conn()
        # 防重复排队：该回执已有未完成任务（pending/running）则不重复建任务
        existing = conn.execute(
            "SELECT id FROM tasks WHERE receipt_id=? AND status IN ('pending','running') LIMIT 1",
            (receipt_id,),
        ).fetchone()
        if existing:
            conn.close()
            return existing["id"]

        now = db_mod.now_iso()
        cur = conn.execute(
            """INSERT INTO tasks(receipt_id, stage, status, retries, created_at, updated_at)
               VALUES(?,?,?,0,?,?)""",
            (receipt_id, stage, "pending", now, now),
        )
        conn.commit()
        tid = cur.lastrowid
        conn.close()
        return tid

    def next_pending(self) -> dict | None:
        conn = db_mod.get_conn()
        row = conn.execute(
            "SELECT * FROM tasks WHERE status='pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def mark_running(self, task_id: int) -> None:
        self._update(task_id, status="running")

    def mark_done(self, task_id: int, stage: str = "compiled") -> None:
        self._update(task_id, status="done", stage=stage, error=None)

    def mark_failed(self, task_id: int, error: str) -> None:
        conn = db_mod.get_conn()
        row = conn.execute("SELECT retries FROM tasks WHERE id=?", (task_id,)).fetchone()
        retries = (row["retries"] if row else 0) + 1
        r = conn.execute("SELECT receipt_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        receipt_id = r["receipt_id"] if r else None
        conn.close()
        if retries >= self.max_retries:
            self._update(task_id, status="failed", retries=retries, error=error)
            # 重试耗尽即最终失败：回写 receipts，保持状态一致（D3）
            if receipt_id:
                self._update_receipt(receipt_id, status="failed", error=error)
        else:
            # 有限重试：回 pending
            self._update(task_id, status="pending", retries=retries, error=error)

    def mark_paused(self, task_id: int, error: str) -> None:
        self._update(task_id, status="paused", error=error)
        # 回写 receipts.status，避免回执永远停在 processing（D3：状态一致性）。
        # /api/receipt/{id} 与 intake.py 轮询都读 receipts，必须同步真实状态。
        conn = db_mod.get_conn()
        row = conn.execute("SELECT receipt_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE receipts SET status='paused', error=?, updated_at=? WHERE id=?",
                (error, db_mod.now_iso(), row["receipt_id"]),
            )
        conn.commit()
        conn.close()

    def _update_receipt(self, receipt_id: str, *, status: str, error: str | None = None) -> None:
        conn = db_mod.get_conn()
        conn.execute(
            "UPDATE receipts SET status=?, error=?, updated_at=? WHERE id=?",
            (status, error, db_mod.now_iso(), receipt_id),
        )
        conn.commit()
        conn.close()

    def requeue_interrupted(self) -> None:
        """启动时恢复：running 的视为中断回 pending；限额暂停且已跨天恢复 pending；
        鉴权/余额/限流等其他暂停不自动恢复（需人工处理）。"""
        conn = db_mod.get_conn()
        conn.execute(
            "UPDATE tasks SET status='pending', error='中断恢复' WHERE status='running'"
        )
        conn.commit()
        conn.close()
        self.recover_daily_limit_paused()

    def recover_daily_limit_paused(self) -> None:
        """把跨天的限额暂停任务恢复为 pending（幂等，仅在 updated_at 早于今天时生效）。

        鉴权/余额/限流等其他暂停不动。常驻服务运行循环里定期调用。
        """
        conn = db_mod.get_conn()
        conn.execute(
            """UPDATE tasks SET status='pending', error='限额暂停次日恢复'
               WHERE status='paused'
                 AND error LIKE 'PAUSE:每日材料上限%'
                 AND date(updated_at) < date('now')"""
        )
        conn.commit()
        conn.close()

    def _update(self, task_id: int, **fields) -> None:
        conn = db_mod.get_conn()
        sets = [f"{k}=?" for k in fields if k != "id"]
        vals = [fields[k] for k in fields if k != "id"]
        sets.append("updated_at=?")
        vals.append(db_mod.now_iso())
        vals.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
        conn.close()


class Worker:
    """单 Worker 顺序执行任务。handler(receipt) -> (ok, error)。"""

    def __init__(self, cfg, queue: TaskQueue, handler: Callable):
        self.cfg = cfg
        self.queue = queue
        self.handler = handler
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.queue.requeue_interrupted()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # 跨天限额恢复检查：常驻服务不重启也要在运行循环里定期恢复
        last_daily_check = 0.0
        DAILY_CHECK_INTERVAL = 60.0  # 每 60 秒检查一次是否跨天

        while not self._stop.is_set():
            try:
                self._run_once()
                # 空闲期：定期检查跨天限额恢复（幂等，仅在跨天时生效）
                now = time.monotonic()
                if now - last_daily_check >= DAILY_CHECK_INTERVAL:
                    self.queue.recover_daily_limit_paused()
                    last_daily_check = now
                time.sleep(1.0)
            except Exception as e:  # noqa: BLE001
                # 兜底：任何单个任务的异常（含 mark_failed 自身抛 database is locked）
                # 都不许逃出 while 循环。否则线程死、写锁泄漏、服务变僵尸。
                # 这里只记录不重抛，保活 worker。
                try:
                    print(f"[worker] 保活：处理异常被兜底捕获，继续循环：{type(e).__name__}: {e}", flush=True)
                except Exception:
                    pass
                time.sleep(1.0)

    def _run_once(self) -> None:
        task = self.queue.next_pending()
        if not task:
            return
        self.queue.mark_running(task["id"])
        try:
            receipt_id = task["receipt_id"]
            ok, error = self.handler(receipt_id, task["id"])
            if ok:
                self.queue.mark_done(task["id"])
            else:
                # 额度/权限类错误暂停，其余有限重试
                if error and error.startswith("PAUSE:"):
                    self.queue.mark_paused(task["id"], error)
                else:
                    self.queue.mark_failed(task["id"], error or "处理失败")
        except Exception as e:  # noqa: BLE001
            try:
                self.queue.mark_failed(task["id"], f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                # mark_failed 自身也可能因锁失败；这里不能让它逃出外层，保活优先
                pass
