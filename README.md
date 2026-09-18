# Local Knowledge Wiki

本地优先的个人知识库：Python/FastAPI + SQLite FTS5 + 文件存储 + 只读网页。把 Agent 的材料整理能力与可追溯的知识保存分开。

本仓库是现有 Windows 本地实现的脱敏交付副本，不含真实材料、数据库、密钥、日志或历史任务产物；不是已完成生产加固的云服务。

## 快速启动（Windows / Python 3.12）

在本仓库根目录运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r _service/requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:app --app-dir _service --host 127.0.0.1 --port 8765
```

浏览器打开 http://127.0.0.1:8765 。默认路径根据仓库位置生成，数据保存在根目录 `content/` 和 `_local/`，不进入 Git。安装依赖需要网络。此启动方式不启动后台编译 Worker，适用于 prepared、archive 和本地检索；状态中的 worker=stopped 属于预期。请保持终端打开。本次未在全新电脑重新安装依赖验证。

## 给第一次搭建的使用者

1. [工作原理与能力边界](docs/architecture.md)：先理解客户端、服务与模型的分工。
2. [从零安装与第一次入库](docs/setup.md)：包含可复制命令、仓库自带示例及验收标准。
3. [Claude Code / Codex / WorkBuddy 接入](docs/clients.md)：安装位置、路径调整与实际接通验证。
4. [维护、备份恢复与排错](docs/operations.md)：升级前保护数据，异常时不盲目重试。
5. [本次交付验证记录](docs/verification.md)：区分已验证和未验证。
6. [原材料日期](docs/material-dates.md)：日期入库、未知状态、网页筛选及保守历史补录。

代码采用 [MIT License](LICENSE)，可使用、修改和分发（包括商业用途），须保留版权及许可声明；不提供担保。第三方依赖及导入资料遵循各自许可，MIT 不授予它们的权利。

默认 prepared/archive/搜索浏览不需要服务端模型密钥。后台 compile/问答需要另行配置兼容 Anthropic Messages 的模型服务；默认网关与模型名仅保留既有实现背景，不保证可用。不需要后台模型时不要配置密钥、不要调用 compile。

## 三种入库方式

| 方式 | 执行方与用途 | 后台模型 |
|---|---|---|
| prepared（默认推荐） | 客户端阅读图文，提交原件 raw、忠实解析稿 extract、知识稿 wiki | 不调用 |
| archive | 原样保存已经写好的文本成稿；二进制附件只保存原件并标待解析 | 不调用 |
| compile（显式授权） | 服务端解析并调用模型整理 | 会调用，可能付费 |

prepared 没有后台模型调用，不代表客户端整理免费。Skill 是操作约定，真正的校验、保存和回执在服务端。

```powershell
py -3.12 _service/intake.py submit --intent prepared --file .\original.pdf --extract-file .\extract.md --wiki-file .\wiki.md --title "示例材料" --kb library --scope external --tag AI --parser "实际客户端及解析方式"
py -3.12 _service/intake.py search "示例" --kb library
```

命令示例中的文件需自行提供。客户端没看完图片或存在缺失时，按 Skill 传 `--parse-incomplete`，不要声明完整。退出码 3 表示需关注，不代表可以盲目重试。

## 实现结构

- `_service/app.py`：HTTP 接口；`intake.py`：多客户端共用 CLI。
- `archive_full.py`：prepared 三层保存、版本与同源修正；`archive.py`：成稿/原件归档。
- `storage.py`、`db.py`：路径边界、文件、SQLite；`search.py`：FTS 与中文召回。
- `authority.py`、`review.py`、`quality.py`：来源信息、待确认、轻量一致性检查。
- `parser.py`、`compile.py`、`merge.py`：可选后台解析和模型合并。
- `taskqueue.py`、`usage.py`：持久队列、暂停恢复、用量记录。
- `library_web.py`、`web/index.html`：知识正文、来源、版本和待确认状态的本地只读入口。
- `skills/knowledge-intake/`：客户端入库约定，包含详细确认流程。

唯一存储 ID 为 `library`。`weimob/external` 是资料范围，AI/SaaS 等是标签，不是独立仓库。当前分类带有原项目业务背景，复用时可按需调整，勿直接创建旧版 qifu/saas 等库。

## 多客户端接入

把 `_service/skills/knowledge-intake/` 安装到客户端支持的 Skills 目录。该 Skill 保留原部署路径 `D:\Knowledge`；若本仓库部署在其他路径，必须先把 Skill 及引用说明中的路径改成实际位置。安装后在新会话验证发现和调用，不把文件复制成功当作接通。WorkBuddy 的客户端注册配置需按实际版本验证，本仓库不承诺统一自动安装。

## 正确性边界

保存完成、客户端声明解析完整、自动一致性检查、人工内容核验是四件事。待确认事项不会因保存成功而自动通过；不替用户裁决价格、产品版本或来源冲突。旧知识稿留版本或恢复备份。来源优先级不等于无条件覆盖。

英文材料保留英文原件/解析稿，知识稿用中文，遵循信优先于达与雅。Excel 必须保留工作表与单元格定位，检查图片、公式和合并区域，不能用文件名占位冒充正文。

## 验证

以下回归使用临时数据库和模拟模型，不调用真实模型：

```powershell
py -3.12 _service/scripts/test_taxonomy.py
py -3.12 _service/scripts/test_raw_archive.py
py -3.12 _service/scripts/test_correction.py
```

其他 `test_*.py` 是历史分阶段测试，不能把它们存在视为全部已通过；部分旧库语义已变更。测试不会证明所有资料事实正确。

## 安全与部署限制

仅绑定 127.0.0.1。当前不具备面向公网的登录鉴权、多租户隔离和完整生产审计，不要直接映射公网端口。网页为只读，不代表所有 API 都只读。服务器部署前应另外完成鉴权、TLS、访问控制、备份恢复和并发验证。

运行态数据与配置必须留在忽略目录；上传 Git 前仍应检查暂存文件。公开的是实现代码，不是使用者导入的资料。MIT 不代表服务已完成安全加固。
