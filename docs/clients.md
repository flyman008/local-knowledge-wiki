# 三端客户端接入

先完成 setup.md 的手动入库，再接客户端。源 Skill 位于 `_service/skills/knowledge-intake/`，需整个目录复制，不能只复制 SKILL.md 而遗漏 references。

## 安装位置

| 客户端 | 本地用户目录下的候选安装位置 | 验收方式 |
|---|---|---|
| Claude Code | .claude/skills/knowledge-intake/ | 新会话确认发现，并实际入库示例 |
| Codex | .agents/skills/knowledge-intake/（用户目录或项目内） | 新会话确认发现，并实际入库示例 |
| WorkBuddy | .workbuddy/skills/knowledge-intake/ | 按当前客户端导入/注册要求安装，再实际调用 |

Codex 路径依据 [OpenAI 官方技能文档](https://developers.openai.com/zh-Hans/docs/build-skills)（2026-09-18 核对）；原环境 .codex/skills 曾能加载，不作为新安装的统一要求。其他客户端路径为原环境约定，不保证未来所有版本自动发现。WorkBuddy 注册清单格式可能随版本变化，本仓库未提供通用 workbuddy.json；请使用该客户端支持的本地 Skill 导入方式。复制文件不代表已接通。

## 路径必须适配

源 Skill 保留 D:\Knowledge 原部署路径。安装副本后，用编辑器将其及 references 中的 D:\Knowledge 替换为实际仓库绝对路径；将执行 Python 改为仓库 `.venv\Scripts\python.exe` 的绝对路径。保留来源、三层保存和确认规则，不只写一句“上传文件”。

不要覆盖已经安装的自定义版本：先备份，再比较更新。仓库升级后重新比对安装副本，不会自动同步。不要把自己的私密绝对路径配置回传到公共仓库。

## 新会话试跑话术

> 使用 knowledge-intake，把仓库 examples/first-intake 中的原件、解析稿和知识稿按 prepared 入库，来源使用 source.json，范围 external，标签 AI。服务已在本机启动。不要调用后台模型，不要直接写数据库；返回真实回执、三层保存情况和待确认项。

一般资料可以说：“读这份 PDF 后入库；图文都检查，读不到的明确标记。使用客户端解析后的 prepared，不授权后台 compile。”

完成标准：客户端发现 Skill → 使用正确路径脚本 → 请求正确服务 → 返回实际回执 → 搜索和原文回查成功。任何一环失败都不是三端接通。

## 定时执行

由客户端或操作系统触发指定范围的采集任务；采集器只取材料，读图文和整理仍按约定执行。每篇记来源、回执及最终状态；中断先查回执，不能把未回写进度当成未入库。不要自动裁决语义冲突，也不要开放无限采集范围。本交付未自动配置定时任务。
