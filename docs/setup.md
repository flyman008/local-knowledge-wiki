# 从零安装与第一次入库

## 1. 准备

当前教程面向 Windows PowerShell、Python 3.12。需要能安装 Python 依赖；Git 可选，也可在 GitHub 下载 ZIP 并解压。不要覆盖现有 D:\Knowledge；首次尝试请使用独立空目录。

```powershell
git clone https://github.com/flyman008/local-knowledge-wiki.git
cd local-knowledge-wiki
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r _service/requirements.txt
```

没有 py 命令时，先安装 Python 3.12 并确认命令路径；不要把不同 Python 环境混着用。不必激活虚拟环境，下面直接使用其中的解释器。

## 2. 无后台模型启动（推荐）

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --app-dir _service --host 127.0.0.1 --port 8765
```

保持此窗口打开，在第二个 PowerShell 窗口进入同一仓库。浏览器访问 http://127.0.0.1:8765 ，接口说明位于 http://127.0.0.1:8765/docs 。不要把接口说明页面当权限控制。

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/capabilities
Invoke-RestMethod http://127.0.0.1:8765/api/status
```

预期 capabilities 含 storage=library 和 taxonomy=scopes-tags-v1。worker=stopped 是本模式预期，不是故障。无需创建 .env 或填写任何模型密钥。

注意：旧的 app.py 直接启动、CLI 自动拉起和 restart-service.ps1 路径仍有原部署假设，不能代替本教程的无模型启动方式。请先手动启动，再使用 CLI；CLI 当前固定访问 8765。端口已被其他知识库占用时，不要向它试投材料，应先确认实例归属。

## 3. 用仓库示例完成第一次入库

示例是虚构的纯文本说明，无图表，不代表真实商品能力。原件、解析稿、知识稿均在 examples/first-intake/。

```powershell
.\.venv\Scripts\python.exe _service/intake.py submit --intent prepared --file examples/first-intake/original.md --extract-file examples/first-intake/extract.md --wiki-file examples/first-intake/wiki.md --source-metadata-file examples/first-intake/source.json --title "演示知识库入门样本" --kb library --scope external --tag AI --parser "仓库自带纯文本示例；未调用模型"
.\.venv\Scripts\python.exe _service/intake.py search "演示知识库入门样本" --kb library
```

保留输出中的 receipt_id，将它替换到以下命令：

```powershell
.\.venv\Scripts\python.exe _service/intake.py status <receipt_id>
.\.venv\Scripts\python.exe _service/intake.py review list --kb library
```

验收：三层均保存；搜索有命中；网页能打开正文；能回查来源和版本；未调用后台模型。自动检查通过不意味着做过独立语义审查。

成稿原样归档示例（不需要 extract）：

```powershell
.\.venv\Scripts\python.exe _service/intake.py submit --intent archive --file examples/first-intake/wiki.md --title "演示成稿归档" --kb library --scope external --tag AI
```

始终显式填写 --intent：当前 CLI 未传时默认 compile，可能触发后台模型路径。只传链接不代表已经取得正文。二进制 archive 只存原件，不能当成正文已解析。

## 4. 可选后台模型模式

仅需要后台 compile/模型问答时使用。先停止无模型服务，复制 _service/config.example.yaml 到 _local/config.yaml，用编辑器填写实际兼容 Anthropic Messages 的端点和模型；密钥放 _local/.env：ANTHROPIC_AUTH_TOKEN=实际密钥。不要提交此文件。

再用 `.\.venv\Scripts\python.exe _service/app.py` 启动，此模式会校验密钥并启动 Worker。会处理已有排队任务，所以切换前检查队列。默认代码中的网关/模型名称只是历史配置，不保证可用。每日材料数不是金额硬上限，不能把它当严格预算控制。

普通入库不需要此模式。本仓库交付验证不调用付费模型。
