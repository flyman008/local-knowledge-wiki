"""配置管理。

从 _local/config.yaml 读取；密钥从环境变量读取，绝不落盘、不打印。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 项目根 = 本文件上上级（_service/）
SERVICE_DIR = Path(__file__).resolve().parent
KNOWLEDGE_ROOT = SERVICE_DIR.parent
LOCAL_DIR = KNOWLEDGE_ROOT / "_local"
CONFIG_PATH = LOCAL_DIR / "config.yaml"
ENV_PATH = LOCAL_DIR / ".env"

# 缺省配置（config.yaml 不存在时兜底，便于先跑起来）
DEFAULTS: dict[str, Any] = {
    "llm": {
        "base_url": "https://api.tokenbay.me",
        "text_model": "deepseek-v4-flash",
        "vision_model": "deepseek-v4-flash-vision-exp",
        "api_key_env": "ANTHROPIC_AUTH_TOKEN",
        "max_tokens": 4096,
        "timeout_seconds": 120,
    },
    "service": {
        "host": "127.0.0.1",
        "port": 8765,
        "worker_concurrency": 1,
        "max_retries": 3,
        "daily_material_limit": 50,
    },
    "knowledge_bases": [{'id':'library','path':str(KNOWLEDGE_ROOT/'content'),'domain':'统一知识库','access':'private','auto_compile':True}],
    "research_wiki": {"path": str(KNOWLEDGE_ROOT / "content"), "domains": []},
}


@dataclass
class LLMConfig:
    base_url: str
    text_model: str
    vision_model: str
    api_key_env: str
    max_tokens: int
    timeout_seconds: int

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass
class ServiceConfig:
    host: str
    port: int
    worker_concurrency: int
    max_retries: int
    daily_material_limit: int


@dataclass
class KnowledgeBase:
    id: str
    path: str
    domain: str
    access: str
    auto_compile: bool
    rules_file: str | None


@dataclass
class Config:
    llm: LLMConfig
    service: ServiceConfig
    knowledge_bases: list[KnowledgeBase]
    research_wiki_path: str
    research_wiki_domains: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_env_file(path: Path | None = None) -> None:
    r"""加载项目自持的 .env（D:\Knowledge\_local\.env）到环境变量。

    优先级：已存在的环境变量 > .env 文件（不覆盖已注入的 token，方便临时覆盖）。
    只支持 KEY=VALUE 与 KEY="VALUE" 两种简单形式，忽略空行与 # 注释。
    这属于知识库服务自己的凭据来源，不寄生在 ~/.claude/settings.json（CC Switch 会重写它）。
    """
    path = path or ENV_PATH
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'") and len(val) >= 2:
            val = val[1:-1]
        os.environ.setdefault(key, val)


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    # 先加载项目自持的 .env，保证 api_key 在环境变量里可读
    load_env_file()
    raw: dict[str, Any] = dict(DEFAULTS)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        raw = _deep_merge(raw, user_cfg)

    llm = raw["llm"]
    svc = raw["service"]
    bases = [
        KnowledgeBase(
            id=b["id"],
            path=b["path"],
            domain=b.get("domain", ""),
            access=b.get("access", "private"),
            auto_compile=bool(b.get("auto_compile", False)),
            rules_file=b.get("rules_file"),
        )
        for b in raw.get("knowledge_bases", [])
    ]
    rw = raw.get("research_wiki", {})
    # Single storage; scopes/tags are not separate libraries.
    return Config(
        llm=LLMConfig(
            base_url=llm["base_url"],
            text_model=llm["text_model"],
            vision_model=llm["vision_model"],
            api_key_env=llm.get("api_key_env", "ANTHROPIC_AUTH_TOKEN"),
            max_tokens=int(llm.get("max_tokens", 4096)),
            timeout_seconds=int(llm.get("timeout_seconds", 120)),
        ),
        service=ServiceConfig(
            host=svc["host"],
            port=int(svc["port"]),
            worker_concurrency=int(svc.get("worker_concurrency", 1)),
            max_retries=int(svc.get("max_retries", 3)),
            daily_material_limit=int(svc.get("daily_material_limit", 50)),
        ),
        knowledge_bases=bases,
        research_wiki_path=rw.get("path", str(KNOWLEDGE_ROOT / "research-wiki")),
        research_wiki_domains=rw.get("domains", []),
        raw=raw,
    )


def ensure_dirs(cfg: Config) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    Path(cfg.research_wiki_path).mkdir(parents=True, exist_ok=True)
