#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库入库 CLI（三个客户端共用执行体）。

封装「读材料 → 提交 HTTP → 记回执 → 查最终结果」，区分两种意图：
  --intent archive  原样归档（成稿入库，不调模型重写正文）
  --intent compile  资料整理（保存原文，进入模型编译）

用法：
  py -3.12 intake.py --intent compile --file D:/path/doc.xlsx --kb qifu
  py -3.12 intake.py --intent archive --text "成稿正文" --title "某成稿" --kb qifu
  py -3.12 intake.py --intent compile --url https://... --kb qifu
  py -3.12 intake.py --status <receipt_id>          # 查最终处理结果

服务地址、知识域见 config（默认 http://127.0.0.1:8765）。
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.request
import urllib.error

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SERVICE = "http://127.0.0.1:8765"
KB_DEFAULT = 'library'


def _post_json(path: str, payload: dict) -> dict:
    import json

    req = urllib.request.Request(
        SERVICE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_file(path: str, filename: str, data: bytes, fields: dict) -> dict:
    import uuid

    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
    body += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    body += data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        SERVICE + path,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return __import__("json").loads(resp.read().decode("utf-8"))


def _get(path: str) -> dict:
    import json

    with urllib.request.urlopen(SERVICE + path, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------- 服务按需拉起 ----------

def _service_alive() -> bool:
    """探测本地服务是否在跑（/api/status 200 即视为存活）。"""
    try:
        with urllib.request.urlopen(SERVICE + "/api/status", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_service() -> None:
    """后台拉起服务进程（detached，不阻塞本脚本、不依赖登录会话）。

    token 由服务自己从 _local/.env 读（见 config.load_env_file），这里无需注入。
    stdout/stderr 落到 _local/logs/server_auto.log，便于排查启动失败原因。
    """
    import os
    import subprocess

    here = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(here, "..", "_local", "logs"))
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "server_auto.log")
    logf = open(log_path, "a", encoding="utf-8")

    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)

    subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=here,
        stdout=logf,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )


def ensure_service(timeout: float = 25.0) -> bool:
    """确保服务在跑：探测失败则后台拉起并等就绪。返回最终是否可用。

    知识库服务是被动响应型，不常驻；入库时按需拉起，拉起失败（如缺 token
    fail-fast 退出）会轮询超时返回 False，由调用方报错退出。
    """
    if _service_alive():
        return True
    _start_service()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _service_alive():
            return True
        time.sleep(0.5)
    return False


def submit(args) -> int:
    """提交材料，返回回执，轮询到最终结果（done/failed），不以回执当完成。"""
    import json
    import domains
    classification = domains.normalize({'scopes':args.scope or [],'tags':args.tag or []})
    metadata = None
    if getattr(args, "source_metadata_file", None):
        if args.intent != "prepared":
            print("source-metadata-file 当前仅支持 prepared，不会静默丢弃来源字段", file=sys.stderr)
            return 1
        import authority
        try:
            with open(args.source_metadata_file, encoding="utf-8") as f:
                metadata = authority.normalize(json.load(f))
        except (ValueError, OSError) as e:
            print(f"来源元数据无效：{e}", file=sys.stderr)
            return 1
    # 服务按需拉起：探测失败则后台启动并等就绪；起不来（如缺 token）则报错退出
    if not ensure_service():
        print("错误：本地知识库服务无法启动或未就绪（可能缺 token，见 _local/logs/server_auto.log）",
              file=sys.stderr)
        return 1

    try:
        if _get('/api/capabilities').get('taxonomy') != 'scopes-tags-v1':
            raise ValueError('旧版服务')
    except (ValueError, OSError):
        print('服务未支持新版分类，请双击重启知识库.bat；未提交材料。',file=sys.stderr)
        return 1
    # prepared 意图：客户端已整理，三件套入库，后台 0 模型调用
    if args.intent == "prepared":
        # 长解析稿/成果从文件读（避免塞命令行）
        extract_text = args.extract
        wiki_text = args.wiki
        if args.extract_file:
            extract_text = open(args.extract_file, encoding="utf-8").read()
        if args.wiki_file:
            wiki_text = open(args.wiki_file, encoding="utf-8").read()

        # 解析完整性：客户端显式声明，缺页/识图失败传 --parse-incomplete + --missing-pages
        parse_complete = not args.parse_incomplete
        missing_pages = args.missing_pages  # list[int]

        if args.file and extract_text:
            # 带原件文件：archive_full_file
            data = open(args.file, "rb").read()
            import os

            r = _post_file("/api/archive_full_file", os.path.basename(args.file), data, {
                'classification': json.dumps(classification,ensure_ascii=False),
                "kb_id": args.kb,
                "title": args.title or "",
                "extract_text": extract_text,
                "wiki_text": wiki_text or "",
                "url": args.url or "",
                "publisher": args.publisher or "",
                "parser": args.parser or "",
                "correction_reason": args.correction_reason or "",
                **({'base_version':str(args.base_version)} if args.base_version is not None else {}),
                "parse_complete": "true" if parse_complete else "false",
                "missing_pages": ",".join(str(p) for p in (missing_pages or [])),
                "source_metadata": json.dumps(metadata, ensure_ascii=False) if metadata else "",
            })
        else:
            r = _post_json("/api/archive_full", {
                'classification': classification,
                "kb_id": args.kb,
                "title": args.title,
                "extract_text": extract_text,
                "wiki_text": wiki_text,
                "url": args.url,
                "publisher": args.publisher,
                "parser": args.parser,
                "correction_reason": args.correction_reason,
                "base_version": args.base_version,
                "parse_complete": parse_complete,
                "missing_pages": missing_pages,
                "source_metadata": metadata,
            })
        if r.get("error"):
            print(f"[prepared] 失败：{r['error']}", file=sys.stderr)
            return 1
        doc_id = r.get("doc_id")
        if not doc_id:
            print(f"[prepared] 失败：未拿到 doc_id，响应={r}", file=sys.stderr)
            return 1
        # 真实状态：哪层存了哪层缺 + 解析是否完整，两者分开，不谎报"三层完整"
        # 重复提交也复用服务返回的真实状态（不因 is_duplicate 跳过缺页提示）
        saved = r.get("saved_layers") or {}
        parse_complete_resp = r.get("parse_complete", True)
        complete = r.get("complete", False)
        dup_tag = "（重复提交，复用旧回执）" if r.get("is_duplicate") else ""
        if complete:
            print(f"[prepared] doc_id={doc_id} 三层已保存；客户端声明解析完整（后台 0 模型调用；不代表内容核验通过）{dup_tag}")
        else:
            missing = [k for k in ("raw", "extract", "wiki") if not saved.get(k)]
            msg = f"[prepared] doc_id={doc_id} 未完整入库{dup_tag}"
            if missing:
                msg += f"（缺层：{', '.join(missing)}）"
            if not parse_complete_resp:
                mp = r.get("missing_pages") or []
                msg += f"（解析不完整：缺失/失败页码 {mp or '未标注'}）"
            print(msg)
        print(f"[prepared] receipt_id={r['receipt_id']}")
        receipt = _get('/api/receipt/' + r['receipt_id'])
        print('分层验收：' + json.dumps(receipt.get('quality', {'automatic_check':'not_checked'}), ensure_ascii=False, indent=2))
        review_items = receipt.get('review_items', [])
        print('材料已保存不等于事实已确认。待确认项：')
        print(__import__('json').dumps(review_items, ensure_ascii=False, indent=2))
        return 3 if not complete or receipt.get('quality', {}).get('automatic_check') != 'passed' else 0

    if args.intent == "archive":
        # archive 意图：支持 --file（上传真实文件）或 --text/--url
        if args.file:
            data = open(args.file, "rb").read()
            import os

            r = _post_file("/api/archive_file", os.path.basename(args.file), data, {
                'classification': json.dumps(classification,ensure_ascii=False),
                "kb_id": args.kb,
                "target_kb_id": args.target_kb or args.kb,
                "title": args.title or "",
                "url": args.url or "",
                "publisher": args.publisher or "",
            })
        else:
            r = _post_json("/api/archive", {
                'classification': classification,
                "kb_id": args.kb,
                "target_kb_id": args.target_kb or args.kb,
                "title": args.title,
                "text": args.text,
                "url": args.url,
                "publisher": args.publisher,
            })
        # 检查是否真的归档成功
        if r.get("error"):
            print(f"[archive] 失败：{r['error']}", file=sys.stderr)
            return 1
        doc_id = r.get("doc_id")
        if not doc_id:
            print(f"[archive] 失败：未拿到 doc_id，响应={r}", file=sys.stderr)
            return 1
        if r.get('content_status') == 'needs_parse':
            print(f"[archive] receipt_id={r.get('receipt_id')} doc_id={doc_id} 原件已保存，正文待解析；未生成知识稿。不要重复提交，请先解析再 prepared。")
            return 3
        print(f"[archive] receipt_id={r.get('receipt_id')} doc_id={doc_id} 已归档可检索")
        return 0

    # compile 意图
    if args.file:
        data = open(args.file, "rb").read()
        import os

        r = _post_file("/api/submit_file", os.path.basename(args.file), data, {
            'classification': json.dumps(classification,ensure_ascii=False),
            "kb_id": args.kb,
            "target_kb_id": args.target_kb or args.kb,
            "url": args.url or "",
        })
    elif args.text:
        r = _post_json("/api/submit", {
            'classification': classification,
            "kb_id": args.kb,
            "target_kb_id": args.target_kb or args.kb,
            "text": args.text,
            "url": args.url,
        })
    elif args.url:
        r = _post_json("/api/submit", {
            'classification': classification,
            "kb_id": args.kb,
            "target_kb_id": args.target_kb or args.kb,
            "url": args.url,
        })
    else:
        print("错误：compile 意图需要 --file / --text / --url 之一", file=sys.stderr)
        return 2

    receipt_id = r.get("receipt_id")
    if not receipt_id:
        print(f"错误：未拿到回执，响应={r}", file=sys.stderr)
        return 1

    if r.get("is_duplicate"):
        print(f"[dup] 复用旧回执 {receipt_id}（不重复编译）")
    else:
        print(f"[submit] 回执 {receipt_id}，等待编译...")

    # 轮询最终结果，不能拿到回执就说完成
    for _ in range(args.wait):
        time.sleep(2)
        try:
            st = _get(f"/api/receipt/{receipt_id}")
        except urllib.error.URLError as e:
            print(f"[poll] 查询失败：{e}", file=sys.stderr)
            continue
        status = st.get("status")
        if status in ("done", "failed"):
            print(f"[final] 状态={status}")
            if status == "failed":
                print(f"  失败原因：{st.get('error', '未知')}", file=sys.stderr)
                return 1
            # done：查主题
            if args.verbose:
                topics = _get(f"/api/topics?kb_id={args.target_kb or args.kb}")
                for t in topics:
                    if t.get("id"):
                        print(f"  主题：{t['id']} (version={t.get('version')})")
            return 0
        if status == "received":
            print(f"[poll] 已收件，排队中...")
    # 超时：仍在处理中，返回非成功退出码，保留回执供 --status 后续查询
    print(f"[poll] 超时：回执 {receipt_id} 仍在处理中，可用 --status 稍后查", file=sys.stderr)
    return 2


def status(args) -> int:
    if not ensure_service():
        print("错误：本地知识库服务无法启动或未就绪", file=sys.stderr)
        return 1
    r = _get(f"/api/receipt/{args.receipt_id}")
    print(f"回执 {args.receipt_id}: status={r.get('status')} error={r.get('error')}")
    if r.get('content_status') == 'needs_parse':
        print('  原件已保存，正文待解析；尚未生成可阅读知识。')
        return 3
    # 带出入库结果：三层保存状态 + 解析完整性 + 缺失页码（若有）
    saved = r.get("saved_layers")
    pc = r.get("parse_complete")
    mp = r.get("missing_pages")
    if saved is not None:
        missing = [k for k in ("raw", "extract", "wiki") if not saved.get(k)]
        print(f"  三层保存：{saved}" + (f"（缺层：{', '.join(missing)}）" if missing else ""))
    if pc is not None:
        print(f"  解析完整性（客户端声明，非独立验证）：{'完整' if pc else '不完整'}")
        if not pc and mp:
            print(f"  缺失/失败页码：{mp}")
    print('  分层验收：' + __import__('json').dumps(r.get('quality', {'automatic_check':'not_checked'}), ensure_ascii=False, indent=2))
    if r.get('quality', {}).get('automatic_check') == 'attention':
        return 3
    if r.get("status") == "done":
        return 0
    return 1


def search_material(args) -> int:
    import json
    import urllib.parse
    if not ensure_service():
        return 1
    params = {"q": args.query}
    if args.kb:
        params["kb_id"] = args.kb
    if args.scope:
        params['scope'] = args.scope
    if args.tag:
        params['tag'] = args.tag
    print(json.dumps(_get("/api/search?" + urllib.parse.urlencode(params)), ensure_ascii=False, indent=2))
    return 0


def review_command(args) -> int:
    import json
    import urllib.parse
    if not ensure_service():
        return 1
    try:
        if args.operation == 'list':
            result = _get('/api/reviews' + ('?' + urllib.parse.urlencode({'kb_id':args.kb}) if args.kb else ''))
        else:
            if not args.file:
                raise ValueError('操作需要 --file JSON 文件')
            with open(args.file, encoding='utf-8-sig') as f:
                payload = json.load(f)
            result = _post_json('/api/reviews/' + args.operation, payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if 'error' in result else 0
    except (ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 1


def main() -> int:
    p = argparse.ArgumentParser(description="知识库入库 CLI")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("submit", help="提交材料入库")
    sp.add_argument("--intent", choices=["archive", "compile", "prepared"], default="compile",
                    help="archive=成稿原样归档(不重写) / compile=资料整理(进模型编译) / prepared=客户端已整理(三件套,0模型)")
    sp.add_argument("--file", help="文件路径")
    sp.add_argument("--text", help="正文文本")
    sp.add_argument("--url", help="链接")
    sp.add_argument("--title", help="标题（archive/prepared 用）")
    sp.add_argument("--publisher", help="发布者（archive 用）")
    sp.add_argument("--extract", help="解析稿文本（prepared 用）")
    sp.add_argument("--wiki", help="知识成果文本（prepared 用）")
    sp.add_argument("--extract-file", help="解析稿文件路径（prepared 用，长稿从文件读）")
    sp.add_argument("--wiki-file", help="知识成果文件路径（prepared 用，长稿从文件读）")
    sp.add_argument("--parser", help="客户端解析工具/模型（prepared 用，如实记录）")
    sp.add_argument('--correction-reason', help='同一原件纯整理纠错原因及原文定位；非业务口径裁决')
    sp.add_argument('--base-version', type=int, help='纠错前已读取的当前知识稿版本')
    sp.add_argument("--source-metadata-file", help="prepared 来源身份/业务版本/适用范围 JSON 文件")
    sp.add_argument("--parse-incomplete", action="store_true",
                    help="声明解析不完整（prepared 用；缺页/识图失败时加此旗标）")
    sp.add_argument("--missing-pages", type=int, nargs="*", default=None,
                    help="缺失/失败页码列表，如 --missing-pages 2 5（prepared 用）")
    sp.add_argument('--kb', default=KB_DEFAULT, choices=['library'], help='统一存储，默认 library')
    sp.add_argument('--target-kb', choices=['library'])
    sp.add_argument('--scope', action='append', choices=['weimob','external'], help='资料讲谁，可重复；不填待分类')
    sp.add_argument('--tag', action='append', help='内容标签，可重复，例如 AI、SaaS、导购')
    sp.add_argument("--wait", type=int, default=60, help="轮询次数（每次 2s），默认 60")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=submit)

    st = sub.add_parser("status", help="查最终处理结果")
    st.add_argument("receipt_id")
    st.set_defaults(func=status)
    sr = sub.add_parser("search", help="本地检索，返回来源身份与版本，不调用后台问答模型")
    sr.add_argument("query")
    sr.add_argument('--kb', default=None, choices=['library'])
    sr.add_argument('--scope', choices=['weimob','external'])
    sr.add_argument('--tag', help='按一个标签过滤，可与 scope 组合')
    sr.set_defaults(func=search_material)
    rv = sub.add_parser('review', help='列出冲突/不确定项、提交人工裁决或策略建议；不调用模型')
    rv.add_argument('operation', choices=['list','raise','limitation','decide','repair','reopen-repair','propose-policy','policy-state'])
    rv.add_argument('--file', help='操作 JSON 文件')
    rv.add_argument('--kb', default=None)
    rv.set_defaults(func=review_command)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
