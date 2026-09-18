# -*- coding: utf-8 -*-
"""三层入库六场景补测：首次入库、同主题更新、旧版恢复、仅原件变化、缺层、失败一致性。

用临时文件 + 文件 DB，不碰存量库、不调真实模型。
"""
import sys, tempfile, shutil, sqlite3
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as config_mod
import db as db_mod
import storage as storage_mod
import archive_full as af_mod
import snapshot as snapshot_mod

TMP = tempfile.mkdtemp(prefix="af_test_")
_db_path = Path(TMP) / "test.db"


def _make_cfg():
    return config_mod.Config(
        llm=config_mod.LLMConfig("http://f", "t", "v", "NOKEY", 100, 5),
        service=config_mod.ServiceConfig("127.0.0.1", 8765, 1, 3, 50),
        knowledge_bases=[],
        research_wiki_path=str(Path(TMP) / "research-wiki"),
        research_wiki_domains=[],
    )


def _get_conn(db_path=None):
    c = sqlite3.connect(str(_db_path))
    c.row_factory = sqlite3.Row
    c.executescript(db_mod.SCHEMA)
    db_mod._migrate(c)
    return c


db_mod.get_conn = _get_conn
cfg = _make_cfg()

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


def run(title="主题", raw=b"raw-v1", raw_name="doc.pdf",
        extract="解析稿v1", wiki="成果v1", parser="手写"):
    return af_mod.archive_full(
        cfg, kb_id="research-wiki", title=title, raw_data=raw, raw_filename=raw_name,
        extract_text=extract, wiki_text=wiki, parser=parser,
    )


# S1 首次入库三层完整
r1 = run()
check("S1 首次入库三层完整", r1.complete and r1.version == 1,
      f"complete={r1.complete} v={r1.version}")

# S2 同主题更新版本递增 + 旧文件不覆盖
r2 = run(title="主题", raw=b"raw-v2", extract="解析稿v2", wiki="成果v2")
check("S2 同主题更新 version=2 非重复", r2.version == 2 and not r2.is_duplicate,
      f"v={r2.version} dup={r2.is_duplicate}")
inbox = Path(cfg.research_wiki_path) / "_inbox" / "research-wiki"
files = [f.name for f in inbox.glob("*.md")]
check("S2 旧版 v1 与新版 v2 文件并存", "主题.md" in files and "主题_v2.md" in files, str(files))

# S3 旧版可恢复（三层各保留 2 版快照）
conn = db_mod.get_conn()
raw_cnt = conn.execute("SELECT COUNT(*) n FROM snapshots WHERE kb_id='research-wiki' AND layer='raw'").fetchone()["n"]
ext_cnt = conn.execute("SELECT COUNT(*) n FROM snapshots WHERE kb_id='research-wiki' AND layer='extract'").fetchone()["n"]
wiki_cnt = conn.execute("SELECT COUNT(*) n FROM snapshots WHERE kb_id='research-wiki' AND layer='wiki'").fetchone()["n"]
conn.close()
check("S3 三层各保留 2 版快照", raw_cnt >= 2 and ext_cnt >= 2 and wiki_cnt >= 2,
      f"raw={raw_cnt} ext={ext_cnt} wiki={wiki_cnt}")

# S4 仅原件变化（wiki 不变）也要新版本
r4 = run(title="主题", raw="raw-v3-不同".encode(), extract="解析稿v3", wiki="成果v2")
check("S4 仅原件变化不误判重复", not r4.is_duplicate and r4.version == 3,
      f"dup={r4.is_duplicate} v={r4.version}")

# S5 缺层返回真实状态
r5 = af_mod.archive_full(
    cfg, kb_id="research-wiki", title="缺层主题",
    extract_text="只有解析稿", wiki_text="只有成果", parser="手写",
)
check("S5 缺 raw 层不报完整", not r5.complete and r5.saved_layers == {"raw": False, "extract": True, "wiki": True},
      f"complete={r5.complete} layers={r5.saved_layers}")

# S6 失败一致性（DB 失败文件不残留）
orig_wiki = snapshot_mod.record_wiki_snapshot
def boom(*a, **k):
    raise RuntimeError("模拟 DB 失败")
snapshot_mod.record_wiki_snapshot = boom
try:
    try:
        af_mod.archive_full(
            cfg, kb_id="research-wiki", title="失败主题",
            raw_data=b"raw-fail", raw_filename="fail.pdf",
            extract_text="解析稿fail", wiki_text="成果fail", parser="手写",
        )
        check("S6 失败应抛异常", False, "未抛")
    except RuntimeError:
        inbox_fail = Path(cfg.research_wiki_path) / "_inbox" / "research-wiki"
        fail_files = [f.name for f in inbox_fail.glob("失败主题*.md")]
        check("S6 失败后文件不残留", len(fail_files) == 0, str(fail_files))
        conn = db_mod.get_conn()
        n = conn.execute("SELECT COUNT(*) n FROM topics WHERE id='失败主题'").fetchone()["n"]
        conn.close()
        check("S6 失败后 topics 无残留", n == 0, f"n={n}")
finally:
    snapshot_mod.record_wiki_snapshot = orig_wiki

# S7 解析完整性独立于保存状态：三层都存了但 parse_complete=False，complete 应为 False
r7 = af_mod.archive_full(
    cfg, kb_id="research-wiki", title="解析不完整主题",
    raw_data=b"raw-inc", raw_filename="inc.pdf",
    extract_text="解析稿v1\n第2页图片：未识别（待补）", wiki_text="成果v1", parser="手写",
    parse_complete=False, missing_pages=[2],
)
check("S7 三层保存但解析不完整 complete=False",
      not r7.complete and r7.saved_layers == {"raw": True, "extract": True, "wiki": True} and r7.parse_complete is False and r7.missing_pages == [2],
      f"complete={r7.complete} parse_complete={r7.parse_complete} missing={r7.missing_pages} layers={r7.saved_layers}")

# S7b 解析完整时 complete=True（回归：正常路径不受影响）
r7b = af_mod.archive_full(
    cfg, kb_id="research-wiki", title="解析完整主题",
    raw_data=b"raw-ok", raw_filename="ok.pdf",
    extract_text="解析稿v1", wiki_text="成果v1", parser="手写",
    parse_complete=True,
)
check("S7b 解析完整 complete=True", r7b.complete and r7b.parse_complete is True,
      f"complete={r7b.complete} parse_complete={r7b.parse_complete}")

# S8 回滚不删复用原件：同内容原件先入库，再同内容触发复用后 DB 失败，复用文件应保留
# 先正常入库一次，让 raw 文件落盘
r8_pre = af_mod.archive_full(
    cfg, kb_id="research-wiki", title="复用主题",
    raw_data=b"raw-reuse", raw_filename="reuse.pdf",
    extract_text="解析稿reuse", wiki_text="成果reuse", parser="手写",
)
check("S8-pre 首次入库成功", r8_pre.complete, f"v={r8_pre.version}")
# 找到该主题的 raw 文件路径
conn = db_mod.get_conn()
raw_row = conn.execute(
    "SELECT file_path FROM snapshots WHERE kb_id='research-wiki' AND layer='raw' AND file_path LIKE '%reuse%' ORDER BY id DESC LIMIT 1"
).fetchone()
conn.close()
reuse_raw_path = raw_row["file_path"]
import os
check("S8-pre raw 文件存在", os.path.exists(reuse_raw_path), reuse_raw_path)

# 模拟失败：同标题再次提交（version 会 +1），但同内容 raw 会被 save_raw 复用（is_new=False）
# 让 wiki 快照失败，触发回滚——此时复用的 raw 文件不能被删
snapshot_mod.record_wiki_snapshot = boom
try:
    try:
        af_mod.archive_full(
            cfg, kb_id="research-wiki", title="复用主题",
            raw_data=b"raw-reuse", raw_filename="reuse.pdf",
            extract_text="解析稿reuse-v2", wiki_text="成果reuse-v2", parser="手写",
        )
        check("S8 失败应抛异常", False, "未抛")
    except RuntimeError:
        # 复用文件必须还在
        check("S8 回滚后复用 raw 文件仍存在", os.path.exists(reuse_raw_path),
              f"path={reuse_raw_path}")
finally:
    snapshot_mod.record_wiki_snapshot = orig_wiki

# S9 重复提交不掩盖不完整状态：缺页材料重复提交，complete 仍应为 False（复用旧回执的真实状态）
r9_first = af_mod.archive_full(
    cfg, kb_id="research-wiki", title="重复缺页主题",
    raw_data=b"raw-dup-inc", raw_filename="dup_inc.pdf",
    extract_text="解析稿\n第2页图片：未识别", wiki_text="成果", parser="手写",
    parse_complete=False, missing_pages=[2],
)
r9_dup = af_mod.archive_full(
    cfg, kb_id="research-wiki", title="重复缺页主题",
    raw_data=b"raw-dup-inc", raw_filename="dup_inc.pdf",
    extract_text="解析稿\n第2页图片：未识别", wiki_text="成果", parser="手写",
    parse_complete=False, missing_pages=[2],
)
check("S9 首次缺页 complete=False", not r9_first.complete and r9_first.parse_complete is False,
      f"complete={r9_first.complete} pc={r9_first.parse_complete}")
check("S9 重复提交 is_duplicate 且 complete=False（不掩盖不完整）",
      r9_dup.is_duplicate and not r9_dup.complete and r9_dup.parse_complete is False and r9_dup.missing_pages == [2],
      f"dup={r9_dup.is_duplicate} complete={r9_dup.complete} pc={r9_dup.parse_complete} mp={r9_dup.missing_pages}")

# S10 回执查询链路带出缺页信息：get_receipt 还原 saved_layers / parse_complete / missing_pages
import ingest as ingest_mod
rec = ingest_mod.get_receipt(cfg, r9_first.receipt_id)
check("S10 回执能查到缺页信息",
      rec is not None and rec.get("parse_complete") is False and rec.get("missing_pages") == [2]
      and rec.get("saved_layers") == {"raw": True, "extract": True, "wiki": True},
      f"parse_complete={rec.get('parse_complete') if rec else None} missing_pages={rec.get('missing_pages') if rec else None}")

# S10b 回执查询完整材料：parse_complete=True
rec_ok = ingest_mod.get_receipt(cfg, r7b.receipt_id)
check("S10b 回执查询完整材料 parse_complete=True",
      rec_ok is not None and rec_ok.get("parse_complete") is True and rec_ok.get("missing_pages") in (None, []),
      f"parse_complete={rec_ok.get('parse_complete') if rec_ok else None}")


print("\n===== 汇总 =====")
passed = sum(1 for _, c, _ in results if c)
print(f"{passed}/{len(results)} 通过")
for name, c, d in results:
    if not c:
        print("  失败:", name, d)

shutil.rmtree(TMP, ignore_errors=True)
