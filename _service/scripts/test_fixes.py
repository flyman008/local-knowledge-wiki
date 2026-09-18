# -*- coding: utf-8 -*-
"""隔离复现测试：验证 review 报的 7 个 bug 已修复。

只用内存 DB + 模拟 LLM，不碰存量库、不调付费模型。
"""
import sys, os, tempfile, shutil, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 用一个临时目录当 research-wiki / 存量库，不碰真实 D:\Knowledge
TMP = tempfile.mkdtemp(prefix="knowledge_test_")
os.environ["KNOWLEDGE_TEST_DIR"] = TMP

# 重写 config 里的路径，指向临时目录
import config as config_mod
from pathlib import Path

# 打补丁：让 db.get_conn 用共享的临时文件 DB（:memory: 每个连接独立，会拆散外键）
import db as db_mod
_db_path = Path(TMP) / "test.db"


def _get_conn(db_path=None):
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(db_mod.SCHEMA)
    return conn


db_mod.get_conn = _get_conn


# 打补丁：Storage 用临时目录
import storage as storage_mod

def _make_cfg():
    return config_mod.Config(
        llm=config_mod.LLMConfig("http://fake", "t", "v", "NOKEY", 100, 5),
        service=config_mod.ServiceConfig("127.0.0.1", 8765, 1, 3, 50),
        knowledge_bases=[
            config_mod.KnowledgeBase("qifu", str(Path(TMP)/"qifu"), "企服", "private", True, None),
            config_mod.KnowledgeBase("saas", str(Path(TMP)/"saas"), "saas", "private", True, None),
        ],
        research_wiki_path=str(Path(TMP)/"research-wiki"),
        research_wiki_domains=[],
    )


# 模拟 LLM：返回固定文本，记录调用次数
class FakeLLM:
    def __init__(self, title="测试主题"):
        self.calls = 0
        self.title = title
    def understand(self, text, system=None):
        self.calls += 1
        return llm_mod.LLMResult(
            f"# {self.title}\n一句话：测试。核心结论：测试。", "fake", 10, 10, 1
        )
    def describe_image(self, b64, mime, prompt=None):
        self.calls += 1
        return llm_mod.LLMResult("OCR 内容", "fake", 10, 10, 1)
    def complete(self, messages, **kw):
        self.calls += 1
        return llm_mod.LLMResult("答案", "fake", 10, 10, 1)


# 语义合并决策模拟：按预设 action 返回 JSON
class MergeLLM(FakeLLM):
    def __init__(self, action, merged_text="合并正文", conflicts=None, title="测试主题"):
        super().__init__(title=title)
        self.action = action
        self.merged_text = merged_text
        self.conflicts = conflicts or []
    def complete(self, messages, **kw):
        self.calls += 1
        import json
        payload = {
            "action": self.action,
            "merged_text": self.merged_text,
            "summary": f"{self.action} 判定",
            "conflicts": self.conflicts,
        }
        return llm_mod.LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 10, 10, 1)


import llm as llm_mod
import ingest as ingest_mod
import compile as compile_mod
import taskqueue as taskqueue_mod
import search as search_mod
import parser as parser_mod
import merge as merge_mod

cfg = _make_cfg()
storage = storage_mod.Storage(cfg)
fake = FakeLLM()

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


# === #19 只提交链接不能编译其他材料 ===
def test_19():
    # 先落一个 raw 文件（模拟之前已有材料）
    storage.save_raw("research-wiki", "旧材料.txt", "旧内容".encode())
    # 提交一个只有 url 没正文的链接
    r = ingest_mod.submit_material(cfg, kb_id="research-wiki", url="http://x/a", target_kb_id="qifu")
    # 手动调用编译（不经过队列）
    res = compile_mod.compile_receipt(cfg, r.receipt_id)
    # 应该失败（无原文），而不是误编译旧材料
    check("#19 链接无正文不误编译", res.ok == False and "待补正文" in (res.error or ""), res.error)


# === #20 路径逃逸 ===
def test_20():
    try:
        storage.save_raw("research-wiki", "../outside.txt", b"x")
        check("#20 文件名 ../ 逃逸被拦", False, "未拦截")
    except ValueError:
        check("#20 文件名 ../ 逃逸被拦", True)
    try:
        storage.migrate_to_library("qifu", "../../outside.md", "x")
        check("#20 迁入 ../../ 逃逸被拦", False, "未拦截")
    except ValueError:
        check("#20 迁入 ../../ 逃逸被拦", True)


# === #21 跨知识域去重 ===
def test_21():
    r1 = ingest_mod.submit_material(cfg, kb_id="research-wiki", target_kb_id="qifu", text="同内容")
    r2 = ingest_mod.submit_material(cfg, kb_id="research-wiki", target_kb_id="saas", text="同内容")
    check("#21 跨库同内容各自独立", r1.receipt_id != r2.receipt_id and not r2.is_duplicate,
          f"r1={r1.receipt_id} r2={r2.receipt_id} dup={r2.is_duplicate}")


# === #22 主题ID跨库撞主键 ===
def test_22():
    # 直接插入两个库的同名主题
    conn = db_mod.get_conn()
    try:
        conn.execute("INSERT INTO topics(id,kb_id,name) VALUES('产品介绍','qifu','a')")
        conn.execute("INSERT INTO topics(id,kb_id,name) VALUES('产品介绍','saas','b')")
        conn.commit()
        check("#22 跨库同名主题可共存", True)
    except Exception as e:
        check("#22 跨库同名主题可共存", False, str(e))
    finally:
        conn.close()


# === #23 重复排队 ===
def test_23():
    conn = db_mod.get_conn()
    conn.execute("INSERT INTO receipts(id,kb_id,target_kb_id,status,dedup_key,created_at,updated_at) VALUES('r1','research-wiki','qifu','received','k','t','t')")
    conn.commit()
    conn.close()
    q = taskqueue_mod.TaskQueue(cfg)
    t1 = q.enqueue("r1")
    t2 = q.enqueue("r1")
    check("#23 同回执不重复排队", t1 == t2, f"t1={t1} t2={t2}")


# === #24 扫描PDF视觉解析 ===
def test_24():
    import fitz
    pdf = Path(TMP) / "scan.pdf"
    doc = fitz.open()
    page = doc.new_page()  # 空白页 = 无文本层 = 模拟扫描页
    doc.save(str(pdf))
    doc.close()
    r = parser_mod.parse_file(str(pdf), cfg, fake)
    # 有 vision 模型时应 OCR 成功，ok=True，且调用了 vision
    check("#24 扫描PDF进视觉解析", r.ok == True and fake.calls > 0,
          f"ok={r.ok} vision_calls={fake.calls}")


# === #25 中文检索 ===
def test_25():
    conn = db_mod.get_conn()
    conn.execute("INSERT INTO wiki_fts(doc_id,kb_id,title,body) VALUES('d1','qifu','AI会面','AI会面是一种应用场景')")
    conn.commit()
    conn.close()
    r1 = search_mod.search(cfg, "会面", "qifu")
    r2 = search_mod.search(cfg, "AI会面是什么？", "qifu")
    r3 = search_mod.search(cfg, "foo?", "qifu")  # 特殊字符不报错
    check("#25 中文'会面'召回", len(r1) > 0, f"命中{len(r1)}")
    check("#25 自然语言'AI会面是什么？'召回", len(r2) > 0, f"命中{len(r2)}")
    check("#25 'foo?'不报错", isinstance(r3, list), "返回列表")


# === #26 target_kb_id 目录逃逸 ===
def test_26():
    try:
        storage.inbox_wiki_dir("../../outside")
        check("#26 target_kb_id ../../ 逃逸被拦", False, "未拦截")
    except ValueError:
        check("#26 target_kb_id ../../ 逃逸被拦", True)
    try:
        storage.inbox_wiki_dir("a/b")
        check("#26 target_kb_id 含路径分隔符被拦", False, "未拦截")
    except ValueError:
        check("#26 target_kb_id 含路径分隔符被拦", True)


# === #27 request_dedup_id 跨库串回执 ===
def test_27():
    r1 = ingest_mod.submit_material(
        cfg, kb_id="research-wiki", target_kb_id="qifu", text="正文A", request_dedup_id="SAME"
    )
    r2 = ingest_mod.submit_material(
        cfg, kb_id="research-wiki", target_kb_id="saas", text="正文B", request_dedup_id="SAME"
    )
    check("#27 同 request_dedup_id 跨库不串回执", r1.receipt_id != r2.receipt_id and not r2.is_duplicate,
          f"r1={r1.receipt_id} r2={r2.receipt_id} dup={r2.is_duplicate}")


# === #28 OCR 失败仍判成功 ===
def test_28():
    import fitz
    pdf = Path(TMP) / "scan_fail.pdf"
    doc = fitz.open()
    doc.new_page()  # 空白页 = 扫描页
    doc.save(str(pdf))
    doc.close()

    class FailVision:
        def describe_image(self, b64, mime, prompt=None):
            raise llm_mod.LLMError("quota", "额度不足")
        def understand(self, *a, **k):
            return llm_mod.LLMResult("x", "f", 1, 1, 1)

    r = parser_mod.parse_file(str(pdf), cfg, FailVision())
    check("#28 OCR 失败不被判成功", r.ok == False and r.needs_vision == True,
          f"ok={r.ok} needs_vision={r.needs_vision} err={r.error}")


# === #29 旧库迁移（保留数据） ===
def test_29():
    # 用旧 schema（单主键 id）建库，插跨库数据，再跑迁移
    old_db = Path(TMP) / "old.db"
    conn = sqlite3.connect(str(old_db))
    conn.executescript("""
        CREATE TABLE topics (
            id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, name TEXT NOT NULL,
            aliases TEXT, topic_type TEXT, domain TEXT,
            status TEXT DEFAULT 'draft', version INTEGER DEFAULT 1,
            wiki_path TEXT, updated_at TEXT
        );
        INSERT INTO topics(id,kb_id,name,version) VALUES('产品介绍','qifu','a',1);
        INSERT INTO topics(id,kb_id,name,version) VALUES('xingyuan','qifu','星元',2);
    """)
    conn.commit()
    conn.close()

    # 用新 get_conn 逻辑跑迁移（打补丁指向 old.db）
    def _get_conn_old(path=None):
        c = sqlite3.connect(str(old_db))
        c.row_factory = sqlite3.Row
        c.executescript(db_mod.SCHEMA)
        db_mod._migrate(c)
        return c
    db_mod.get_conn = _get_conn_old

    c = db_mod.get_conn()
    # 迁移后：主键是复合 (kb_id,id)，且能插跨库同名主题
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name='topics'").fetchone()["sql"]
    migrated = "PRIMARY KEY (kb_id, id)" in sql
    # 数据保留
    cnt = c.execute("SELECT COUNT(*) AS n FROM topics").fetchone()["n"]
    # 跨库同名可共存
    try:
        c.execute("INSERT INTO topics(id,kb_id,name) VALUES('产品介绍','saas','b')")
        c.commit()
        coexist = True
    except Exception:
        coexist = False
    c.close()

    check("#29 旧库迁移为复合主键", migrated, sql)
    check("#29 迁移保留数据", cnt >= 2, f"rows={cnt}")
    check("#29 迁移后可跨库同名", coexist)


# === #30 archive_content 原样归档（可检索） ===
def test_30():
    import archive as archive_mod
    # 恢复 get_conn 到主测试库（test_29 改过）
    db_mod.get_conn = _get_conn

    r = archive_mod.archive_content(
        cfg, kb_id="research-wiki", target_kb_id="qifu",
        title="微盟星元成稿", text="这是星元的完整成稿正文，包含六大Agent和ChatBI能力说明。",
        url="https://example.com/xingyuan", publisher="微盟",
    )
    # 可检索：直接搜成稿正文关键词
    hits = search_mod.search(cfg, "六大Agent", "qifu")
    check("#30 成稿归档后可检索", len(hits) > 0, f"命中{len(hits)}")
    check("#30 归档返回 doc_id", bool(r.doc_id), r.doc_id)
    # 重复归档同内容应去重
    r2 = archive_mod.archive_content(
        cfg, kb_id="research-wiki", target_kb_id="qifu",
        title="微盟星元成稿", text="这是星元的完整成稿正文，包含六大Agent和ChatBI能力说明。",
    )
    check("#30 同内容重复归档去重", r2.is_duplicate == True, f"dup={r2.is_duplicate}")


# === #31 usage_log 用量记账 ===
def test_31():
    import usage as usage_mod
    db_mod.get_conn = _get_conn
    usage_mod.record_usage(model_id="t", tokens_in=10, tokens_out=20, elapsed_ms=100, task_id=1)
    conn = db_mod.get_conn()
    n = conn.execute("SELECT COUNT(*) AS n FROM usage_log").fetchone()["n"]
    conn.close()
    check("#31 用量写入 usage_log", n >= 1, f"rows={n}")


# === #32 每日材料上限（按实际处理计数，不含归档） ===
def test_32():
    import usage as usage_mod
    db_mod.get_conn = _get_conn
    # 上限为 0 = 不限制
    cfg2 = config_mod.Config(
        llm=cfg.llm, service=config_mod.ServiceConfig("127.0.0.1", 8765, 1, 3, 0),
        knowledge_bases=cfg.knowledge_bases, research_wiki_path=cfg.research_wiki_path,
        research_wiki_domains=[],
    )
    check("#32 上限为0不限制", usage_mod.material_limit_reached(cfg2, "qifu") == False)

    # 上限为 1：插一条"当天完成的编译任务"（tasks.done），应判定已满
    now = db_mod.now_iso()
    conn = db_mod.get_conn()
    conn.execute("INSERT INTO receipts(id,kb_id,target_kb_id,status,dedup_key,created_at,updated_at) VALUES('m1','research-wiki','qifu','done','k',?,?)", (now, now))
    conn.execute("INSERT INTO tasks(receipt_id,stage,status,retries,created_at,updated_at) VALUES('m1','compiled','done',0,?,?)", (now, now))
    conn.commit()
    conn.close()
    cfg3 = config_mod.Config(
        llm=cfg.llm, service=config_mod.ServiceConfig("127.0.0.1", 8765, 1, 3, 1),
        knowledge_bases=cfg.knowledge_bases, research_wiki_path=cfg.research_wiki_path,
        research_wiki_domains=[],
    )
    check("#32 有done编译任务则达上限", usage_mod.material_limit_reached(cfg3, "qifu") == True)

    # 归档不建 task，所以归档后不应触发上限
    conn = db_mod.get_conn()
    conn.execute("INSERT INTO receipts(id,kb_id,target_kb_id,status,dedup_key,created_at,updated_at) VALUES('arch1','research-wiki','qifu','done','ak','t','t')")
    conn.commit()
    conn.close()
    # 只有 1 个 done task，仍达上限（因为那个 task 在）；再验证归档 receipt 不计入
    check("#32 归档不占编译计数", usage_mod.today_material_count("qifu") == 1)


# === #33 归档版本链 + 冲突 ===
def test_33():
    import archive as archive_mod
    db_mod.get_conn = _get_conn
    r1 = archive_mod.archive_content(
        cfg, kb_id="research-wiki", target_kb_id="qifu",
        title="星元成稿", text="正文版本A：六大Agent",
    )
    r2 = archive_mod.archive_content(
        cfg, kb_id="research-wiki", target_kb_id="qifu",
        title="星元成稿", text="正文版本B：六大Agent + ChatBI",
    )
    check("#33 同标题新版本 version 递增", r1.version == 1 and r2.version == 2,
          f"v1={r1.version} v2={r2.version}")
    check("#33 同标题不同正文标记冲突", r2.conflict == True, f"conflict={r2.conflict}")
    # 版本链：topics 里 version 应为 2
    conn = db_mod.get_conn()
    v = conn.execute("SELECT version FROM topics WHERE kb_id='qifu' AND id='星元成稿'").fetchone()["version"]
    conn.close()
    check("#33 topics 版本=2", v == 2, f"v={v}")


# === #34 submit/archive 去重互跳 ===
def test_34():
    import archive as archive_mod
    db_mod.get_conn = _get_conn
    # 先 submit 一份材料（会建 text: 去重键）
    s = ingest_mod.submit_material(cfg, kb_id="research-wiki", target_kb_id="qifu", text="同一份正文内容")
    # 再 archive 同一正文（应独立，不被 submit 的去重键跳过）
    a = archive_mod.archive_content(cfg, kb_id="research-wiki", target_kb_id="qifu", title="某成稿", text="同一份正文内容")
    check("#34 submit后archive不被跳过", a.is_duplicate == False and a.doc_id != "",
          f"dup={a.is_duplicate} doc_id={a.doc_id}")


# === #36 auto_compile 语义 ===
def test_36():
    db_mod.get_conn = _get_conn
    from app import _should_auto_compile
    # qifu auto_compile=true
    check("#36 auto_compile=true 库自动编译", _should_auto_compile("qifu") == True)
    # research-wiki 白名单：隔离整理区自动编译
    check("#36 research-wiki 白名单自动编译", _should_auto_compile("research-wiki") == True)
    # 未登记域：不自动编译（防拼错 kb_id 烧 token）
    check("#36 未登记域不自动编译", _should_auto_compile("typo-domain") == False)


# === #37 语义合并四流程（端到端行为测试，检查正文/主题数/版本） ===
def test_37():
    # 纯函数层：四种 action 判定
    d_add = merge_mod.merge_topic(MergeLLM("add"), "新证据", None)
    check("#37 无已有主题=add", d_add.action == "add", d_add.action)
    d_nc = merge_mod.merge_topic(MergeLLM("nochange"), "新证据", "已有主题")
    check("#37 模拟nochange", d_nc.action == "nochange", d_nc.action)
    d_rev = merge_mod.merge_topic(MergeLLM("revise", "合并正文"), "新证据", "已有主题")
    check("#37 模拟revise", d_rev.action == "revise", d_rev.action)
    d_conf = merge_mod.merge_topic(MergeLLM("conflict", "合并正文", ["价格冲突"]), "新证据", "已有主题")
    check("#37 模拟conflict", d_conf.action == "conflict" and "价格冲突" in d_conf.conflicts, d_conf.action)

    # 集成层：compile 端到端
    db_mod.get_conn = _get_conn

    # add：全新主题
    conn = db_mod.get_conn()
    n_before = conn.execute("SELECT COUNT(*) AS n FROM topics WHERE kb_id='qifu'").fetchone()["n"]
    conn.close()
    r = ingest_mod.submit_material(cfg, kb_id="research-wiki", target_kb_id="qifu", text="新主题正文")
    storage.save_raw("research-wiki", "新主题.txt", "新主题正文".encode())
    res_add = compile_mod.compile_receipt(cfg, r.receipt_id, llm=MergeLLM("add", title="星元"))
    check("#37 add 成功", res_add.ok == True, res_add.error)
    check("#37 add 主题id来自标题非文件名", res_add.topic_id == "星元", res_add.topic_id)

    # 主题数应 +1（相对计数，DB 里可能有之前测试的累积主题）
    conn = db_mod.get_conn()
    n_after = conn.execute("SELECT COUNT(*) AS n FROM topics WHERE kb_id='qifu'").fetchone()["n"]
    conn.close()
    check("#37 add 后主题数+1", n_after == n_before + 1, f"{n_before}->{n_after}")


# === #38 主题身份解耦文件名 + add 不覆盖 ===
def test_38():
    db_mod.get_conn = _get_conn
    conn = db_mod.get_conn()
    n_before = conn.execute("SELECT COUNT(*) AS n FROM topics WHERE kb_id='qifu'").fetchone()["n"]
    conn.close()

    # 两次提交同名文件 same.txt，但 LLM 标题不同（=不同主题），不应因文件名漂移成 same/same-1
    r1 = ingest_mod.submit_material(cfg, kb_id="research-wiki", target_kb_id="qifu", text="内容A")
    storage.save_raw("research-wiki", "same.txt", "内容A".encode())
    res1 = compile_mod.compile_receipt(cfg, r1.receipt_id, llm=MergeLLM("add", title="主题A"))
    check("#38 第一次编译 ok", res1.ok == True, res1.error)

    # 同名文件再提交（raw 会存成 same_1.txt，但 LLM 标题是主题B）
    r2 = ingest_mod.submit_material(cfg, kb_id="research-wiki", target_kb_id="qifu", text="内容B")
    storage.save_raw("research-wiki", "same.txt", "内容B".encode())
    res2 = compile_mod.compile_receipt(cfg, r2.receipt_id, llm=MergeLLM("add", title="主题B"))
    check("#38 第二次编译 ok", res2.ok == True, res2.error)
    check("#38 主题id不跟文件名漂移", res2.topic_id == "主题b", res2.topic_id)

    # 主题数应 +2（两个不同主题），不是 same/same-1
    conn = db_mod.get_conn()
    n_mid = conn.execute("SELECT COUNT(*) AS n FROM topics WHERE kb_id='qifu'").fetchone()["n"]
    conn.close()
    check("#38 两个不同主题独立", n_mid == n_before + 2, f"{n_before}->{n_mid}")

    # add 撞已有主题（模型判 add 但 slug 已被占）：应另建 slug，不覆盖旧稿
    r3 = ingest_mod.submit_material(cfg, kb_id="research-wiki", target_kb_id="qifu", text="内容C")
    storage.save_raw("research-wiki", "third.txt", "内容C".encode())
    res3 = compile_mod.compile_receipt(cfg, r3.receipt_id, llm=MergeLLM("add", title="主题A"))
    conn = db_mod.get_conn()
    n_after3 = conn.execute("SELECT COUNT(*) AS n FROM topics WHERE kb_id='qifu'").fetchone()["n"]
    conn.close()
    check("#38 add撞slug另建主题不覆盖", res3.topic_id != "主题a" and n_after3 == n_before + 3,
          f"topic_id={res3.topic_id} n={n_after3}")


# === #39 nochange 不写文件不增版本 ===
def test_39():
    db_mod.get_conn = _get_conn
    # 先建一个主题（revise 建立稳定 draft）
    r1 = ingest_mod.submit_material(cfg, kb_id="research-wiki", target_kb_id="qifu", text="正文v1")
    storage.save_raw("research-wiki", "doc.txt", "正文v1".encode())
    res1 = compile_mod.compile_receipt(cfg, r1.receipt_id, llm=MergeLLM("add", title="稳定主题"))
    conn = db_mod.get_conn()
    v_before = conn.execute("SELECT version FROM topics WHERE kb_id='qifu' AND id='稳定主题'").fetchone()["version"]
    conn.close()

    # 记录 draft 文件 mtime
    import os
    inbox = Path(TMP) / "research-wiki" / "_inbox" / "qifu" / "稳定主题.md"
    mtime_before = os.path.getmtime(str(inbox))

    # 同主题再编译，模型判 nochange：不应写文件、不应增版本
    r2 = ingest_mod.submit_material(cfg, kb_id="research-wiki", target_kb_id="qifu", text="正文v1重复")
    storage.save_raw("research-wiki", "doc2.txt", "正文v1重复".encode())
    res2 = compile_mod.compile_receipt(cfg, r2.receipt_id, llm=MergeLLM("nochange", title="稳定主题"))
    conn = db_mod.get_conn()
    v_after = conn.execute("SELECT version FROM topics WHERE kb_id='qifu' AND id='稳定主题'").fetchone()["version"]
    conn.close()
    mtime_after = os.path.getmtime(str(inbox))

    check("#39 nochange 不增版本", v_before == v_after, f"v={v_before}->{v_after}")
    check("#39 nochange 不写文件(mtime不变)", mtime_before == mtime_after, f"{mtime_before}->{mtime_after}")


# === #41 限额暂停次日恢复 ===
def test_41():
    db_mod.get_conn = _get_conn
    # 模拟昨日限额暂停的任务
    yesterday = "2026-09-13T10:00:00+00:00"  # 早于今天
    conn = db_mod.get_conn()
    conn.execute("INSERT INTO receipts(id,kb_id,target_kb_id,status,dedup_key,created_at,updated_at) VALUES('rq','research-wiki','qifu','done','kq','t','t')")
    conn.execute("INSERT INTO tasks(receipt_id,stage,status,retries,error,created_at,updated_at) VALUES('rq','ingest','paused',0,'PAUSE:每日材料上限已到（50）','t',?)", (yesterday,))
    # 再插一个 auth 暂停（不恢复）
    conn.execute("INSERT INTO receipts(id,kb_id,target_kb_id,status,dedup_key,created_at,updated_at) VALUES('ra','research-wiki','qifu','done','ka','t','t')")
    conn.execute("INSERT INTO tasks(receipt_id,stage,status,retries,error,created_at,updated_at) VALUES('ra','ingest','paused',0,'PAUSE:auth:鉴权失败','t',?)", (yesterday,))
    conn.commit()
    conn.close()

    # 调 requeue_interrupted
    q = taskqueue_mod.TaskQueue(cfg)
    q.requeue_interrupted()

    conn = db_mod.get_conn()
    limit_task = conn.execute("SELECT status FROM tasks WHERE receipt_id='rq'").fetchone()["status"]
    auth_task = conn.execute("SELECT status FROM tasks WHERE receipt_id='ra'").fetchone()["status"]
    conn.close()
    check("#41 限额暂停次日恢复pending", limit_task == "pending", limit_task)
    check("#41 auth暂停不恢复", auth_task == "paused", auth_task)


# === #42 常驻服务跨天限额自动恢复（不重启） ===
def test_42():
    db_mod.get_conn = _get_conn
    # 模拟昨日限额暂停的任务（今天尚未重启，只有运行循环）
    yesterday = "2026-09-13T10:00:00+00:00"
    conn = db_mod.get_conn()
    conn.execute("INSERT INTO receipts(id,kb_id,target_kb_id,status,dedup_key,created_at,updated_at) VALUES('rw','research-wiki','qifu','done','kw','t','t')")
    conn.execute("INSERT INTO tasks(receipt_id,stage,status,retries,error,created_at,updated_at) VALUES('rw','ingest','paused',0,'PAUSE:每日材料上限已到（50）','t',?)", (yesterday,))
    conn.commit()
    conn.close()

    # 直接调 recover_daily_limit_paused（等价于运行循环里的定期调用，不重启 Worker）
    q = taskqueue_mod.TaskQueue(cfg)
    q.recover_daily_limit_paused()

    conn = db_mod.get_conn()
    status = conn.execute("SELECT status FROM tasks WHERE receipt_id='rw'").fetchone()["status"]
    conn.close()
    check("#42 运行循环跨天恢复pending", status == "pending", status)


# === #43 备份名唯一化（同秒两次修订留两个备份） ===
def test_43():
    import config as config_mod
    import glob as globmod
    db_mod.get_conn = _get_conn

    # 直接调 _backup_draft_to_recovery 两次（同 slug），验证产生两个不同备份文件
    inbox = Path(cfg.research_wiki_path) / "_inbox" / "qifu"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "备份主题.md").write_text("旧稿内容", encoding="utf-8")

    # 测试前清空该 slug 的备份，避免历史累积干扰
    backup_dir = config_mod.LOCAL_DIR / "recovery" / "qifu" / "draft"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for old in globmod.glob(str(backup_dir / "备份主题*.md")):
        os.remove(old)

    compile_mod._backup_draft_to_recovery(cfg, "qifu", "备份主题", version=1)
    compile_mod._backup_draft_to_recovery(cfg, "qifu", "备份主题", version=2)

    backups = globmod.glob(str(backup_dir / "备份主题*.md"))
    check("#43 同主题两次备份不覆盖", len(backups) >= 2, f"backups={len(backups)}")


# === #44 快照幂等（D1：PAUSE 后重跑不撞唯一约束） ===
def test_44():
    import snapshot as snapshot_mod
    db_mod.get_conn = _get_conn
    # 落一个 raw 文件
    raw_path = storage.save_raw("research-wiki", "幂等.txt", "快照幂等内容".encode()).raw_path
    conn = db_mod.get_conn()
    sid1 = snapshot_mod.record_raw_snapshot(
        conn, kb_id="qifu", file_path=raw_path, source_name="幂等.txt", parser="parser:txt"
    )
    # 第二次记录同文件同内容：应返回相同 id，不重复插入
    sid2 = snapshot_mod.record_raw_snapshot(
        conn, kb_id="qifu", file_path=raw_path, source_name="幂等.txt", parser="parser:txt"
    )
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM snapshots WHERE kb_id='qifu' AND file_path=? AND layer='raw'", (raw_path,)
    ).fetchone()["n"]
    conn.close()
    check("#44 raw 快照幂等复用旧 id", sid1 == sid2, f"{sid1} vs {sid2}")
    check("#44 raw 快照不重复插入", n == 1, f"n={n}")


# === #45 PAUSE(auth/quota) 后重跑不崩（D1 端到端） ===
def test_45():
    import snapshot as snapshot_mod
    db_mod.get_conn = _get_conn

    class AuthFailLLM:
        def understand(self, text, system=None):
            raise llm_mod.LLMError("auth", "缺少 API key")
        def describe_image(self, b64, mime, prompt=None):
            raise llm_mod.LLMError("auth", "缺少 API key")
        def complete(self, messages, **kw):
            raise llm_mod.LLMError("auth", "缺少 API key")

    r = ingest_mod.submit_material(cfg, kb_id="research-wiki", target_kb_id="qifu", text="PAUSE重跑测试正文")
    # 第一次：走到快照 commit 后，LLM 抛 auth → 返回 PAUSE（快照已落库）
    res1 = compile_mod.compile_receipt(cfg, r.receipt_id, llm=AuthFailLLM())
    check("#45 首次 auth 触发 PAUSE", res1.ok == False and (res1.error or "").startswith("PAUSE:auth"),
          res1.error)

    # 第二次：换正常 LLM 重跑同一回执，不应再撞唯一约束抛 IntegrityError
    try:
        res2 = compile_mod.compile_receipt(cfg, r.receipt_id, llm=MergeLLM("add", title="PAUSE重跑主题"))
        check("#45 重跑成功不抛 IntegrityError", res2.ok == True, res2.error)
    except Exception as e:
        check("#45 重跑成功不抛 IntegrityError", False, f"{type(e).__name__}: {e}")


# === #46 mark_paused 回写 receipts（D3：回执不停在 processing） ===
def test_46():
    db_mod.get_conn = _get_conn
    conn = db_mod.get_conn()
    conn.execute(
        "INSERT INTO receipts(id,kb_id,target_kb_id,status,dedup_key,created_at,updated_at) "
        "VALUES('rp','research-wiki','qifu','processing','kp','t','t')"
    )
    conn.execute(
        "INSERT INTO tasks(receipt_id,stage,status,retries,error,created_at,updated_at) "
        "VALUES('rp','ingest','paused',0,'PAUSE:auth:鉴权失败','t','t')"
    )
    conn.commit()
    conn.close()

    q = taskqueue_mod.TaskQueue(cfg)
    task_id = conn = db_mod.get_conn()
    row = task_id.execute("SELECT id FROM tasks WHERE receipt_id='rp'").fetchone()
    tid = row["id"]
    task_id.close()
    q.mark_paused(tid, "PAUSE:auth:鉴权失败")

    conn = db_mod.get_conn()
    rec_status = conn.execute("SELECT status, error FROM receipts WHERE id='rp'").fetchone()
    conn.close()
    check("#46 mark_paused 回写 receipts 状态", rec_status["status"] == "paused",
          f"status={rec_status['status']}")
    check("#46 receipts 带出错误信息", rec_status["error"] == "PAUSE:auth:鉴权失败",
          rec_status["error"])


# === #47 配置来源：项目自持 .env（方案D）加载 + 不覆盖已注入 ===
def test_47():
    db_mod.get_conn = _get_conn
    # 用临时 .env 文件验证 load_env_file，不碰真实 _local/.env
    tmp_env = Path(TMP) / "test.env"
    tmp_env.write_text(
        'ANTHROPIC_AUTH_TOKEN="secret-token-value"\n# 注释行\nEMPTY=\nSINGLE=\'abc\'\n',
        encoding="utf-8",
    )
    # 清掉环境变量，模拟干净启动
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    os.environ.pop("EMPTY", None)
    os.environ.pop("SINGLE", None)
    config_mod.load_env_file(tmp_env)
    check("#47 .env 双引号值加载", os.environ.get("ANTHROPIC_AUTH_TOKEN") == "secret-token-value",
          os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    check("#47 .env 单引号值加载", os.environ.get("SINGLE") == "abc", os.environ.get("SINGLE"))
    check("#47 .env 空值不覆盖", "EMPTY" not in os.environ or os.environ.get("EMPTY") == "",
          os.environ.get("EMPTY", "(未设置)"))

    # 已注入的环境变量不被 .env 覆盖（临时覆盖优先）
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "already-injected"
    config_mod.load_env_file(tmp_env)
    check("#47 已注入变量不被 .env 覆盖", os.environ.get("ANTHROPIC_AUTH_TOKEN") == "already-injected",
          os.environ.get("ANTHROPIC_AUTH_TOKEN"))


test_19()
test_20()
test_21()
test_22()
test_23()
test_24()
test_25()
test_26()
test_27()
test_28()
test_29()
test_30()
test_31()
test_32()
test_33()
test_34()
test_36()
test_37()
test_38()
test_39()
test_41()
test_42()
test_43()
test_44()
test_45()
test_46()
test_47()

print("\n===== 汇总 =====")
passed = sum(1 for _, c, _ in results if c)
print(f"{passed}/{len(results)} 通过")
for name, c, d in results:
    if not c:
        print("  失败:", name, d)

shutil.rmtree(TMP, ignore_errors=True)
