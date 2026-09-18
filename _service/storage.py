"""文件存储层：raw（只追加不覆盖）/ extracts（可重做派生）/ wiki（写前留旧版）。

落盘到各库既有目录结构，不改动用户既有文件组织。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import config as config_mod


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_join(base: Path, rel: str) -> Path:
    """把用户提供的相对路径解析到 base 内，禁止 ../ 逃逸。

    解析后必须仍在 base 之内，否则抛 ValueError。文件名中的非法字符也一并规整。
    """
    # 去掉可能带的反斜杠，统一为 Path 语义
    rel = rel.replace("\\", "/").lstrip("/")
    candidate = (base / rel).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"路径越界：{rel!r} 逃出 {base_resolved}")
    return candidate


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _is_safe_identifier(s: str) -> bool:
    """校验目录/库标识：仅字母数字下划线连字符，杜绝路径分隔符与 ../。"""
    return bool(_IDENTIFIER_RE.match(s))


def normalize_hash(text: str) -> str:
    """规范化内容 hash（去空白/统一换行），用于去重。"""
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class StoredMaterial:
    kb_id: str
    raw_path: str
    content_hash: str
    is_new: bool  # False = 同内容已存在，复用旧快照


class Storage:
    def __init__(self, cfg: config_mod.Config):
        self.cfg = cfg
        self._kb_by_id = {b.id: b for b in cfg.knowledge_bases}

    def kb_path(self, kb_id: str) -> Path:
        kb = self._kb_by_id.get(kb_id)
        if not kb:
            # research-wiki 或其他未登记库，落在 research_wiki_path 下
            return Path(self.cfg.research_wiki_path)
        return Path(kb.path)

    def raw_dir(self, kb_id: str) -> Path:
        return self.kb_path(kb_id) / "raw"

    def extracts_dir(self, kb_id: str) -> Path:
        return self.kb_path(kb_id) / "extracts"

    def wiki_dir(self, kb_id: str) -> Path:
        return self.kb_path(kb_id) / "wiki"

    # ---------- 隔离区（inbox）与迁入闸门 ----------
    # 不变式：服务只在 research-wiki（隔离区）自由读写；
    # 对存量库 wiki 目录，只有显式 migrate 才写（先备份到 _local/recovery）。

    def inbox_root(self) -> Path:
        """隔离整理区根（research-wiki）。所有新收件与编译 draft 落这里。"""
        return Path(self.cfg.research_wiki_path)

    def inbox_raw_dir(self) -> Path:
        return self.inbox_root() / "raw"

    def inbox_wiki_dir(self, target_kb_id: str) -> Path:
        """待迁入的编译 draft，按目标库分子目录，避免不同库同名 slug 冲突。

        target_kb_id 必须是已登记的库 id（或隔离区本身），禁止路径分隔符/../ 逃逸。
        """
        if not target_kb_id or not _is_safe_identifier(target_kb_id):
            raise ValueError(f"非法目标库标识：{target_kb_id!r}")
        return _safe_join(self.inbox_root() / "_inbox", target_kb_id)

    def migrate_to_library(self, target_kb_id: str, rel_path: str, content: str) -> tuple[Path, Path | None]:
        """显式迁入：把隔离区 draft 写入目标库 wiki，先备份目标库旧版。

        仅当目标库登记在 knowledge_bases 里才允许；research-wiki 本身不算迁入目标。
        返回 (目标库新路径, 旧版备份路径或 None)。
        """
        if target_kb_id not in self._kb_by_id:
            raise ValueError(f"目标库未登记：{target_kb_id}")
        target = _safe_join(self.kb_path(target_kb_id) / "wiki", rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        backup_path = None
        if target.exists():
            backup_path = self._backup_wiki(target_kb_id, rel_path)
        target.write_text(content, encoding="utf-8")
        return target, backup_path

    # ---------- 收件原件与解析 ----------

    def save_raw(self, kb_id: str, filename: str, data: bytes) -> StoredMaterial:
        """保存原件，只追加不覆盖。同内容已存在则复用，返回 is_new=False。

        文件名冲突时加序号后缀；绝不覆盖旧文件（方案§4.1 raw 只追加）。
        """
        raw_dir = self.raw_dir(kb_id)
        raw_dir.mkdir(parents=True, exist_ok=True)
        digest = content_hash(data)

        # 同内容复用：扫 raw 下同 hash 的已有文件
        for existing in raw_dir.rglob("*"):
            if existing.is_file() and content_hash(existing.read_bytes()) == digest:
                return StoredMaterial(kb_id, str(existing), digest, is_new=False)

        target = self._unique_path(raw_dir, filename)
        target.write_bytes(data)
        return StoredMaterial(kb_id, str(target), digest, is_new=True)

    @staticmethod
    def _unique_path(directory: Path, filename: str) -> Path:
        base = _safe_join(directory, filename)
        if not base.exists():
            return base
        i = 1
        while True:
            cand = base.with_name(f"{base.stem}_{i}{base.suffix}")
            if not cand.exists():
                return cand
            i += 1

    def save_extract(self, kb_id: str, base_name: str, text: str) -> Path:
        """保存解析正文（可重做派生，覆盖写允许）。"""
        extracts_dir = self.extracts_dir(kb_id)
        extracts_dir.mkdir(parents=True, exist_ok=True)
        target = _safe_join(extracts_dir, f"{base_name}.txt")
        target.write_text(text, encoding="utf-8")
        return target

    def write_wiki(self, kb_id: str, rel_path: str, content: str) -> tuple[Path, Path | None]:
        """写 wiki 页，写前保留旧版到 _local 恢复副本。

        返回 (新路径, 旧版备份路径或 None)。
        """
        wiki_dir = self.wiki_dir(kb_id)
        wiki_dir.mkdir(parents=True, exist_ok=True)
        target = _safe_join(wiki_dir, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        backup_path = None
        if target.exists():
            backup_path = self._backup_wiki(kb_id, rel_path)

        target.write_text(content, encoding="utf-8")
        return target, backup_path

    def _backup_wiki(self, kb_id: str, rel_path: str) -> Path:
        backup_root = config_mod.LOCAL_DIR / "recovery" / kb_id / "wiki"
        backup_root.mkdir(parents=True, exist_ok=True)
        src = _safe_join(self.wiki_dir(kb_id), rel_path)
        # 用时间戳命名旧版，避免覆盖历史
        import time

        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = backup_root / f"{Path(rel_path).stem}_{ts}{Path(rel_path).suffix}"
        shutil.copy2(src, dest)
        return dest

    def load_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8", errors="replace")


def manifest_path(kb_id: str, cfg: config_mod.Config) -> Path:
    kb = next((b for b in cfg.knowledge_bases if b.id == kb_id), None)
    base = Path(kb.path) if kb else Path(cfg.research_wiki_path)
    return base / "raw" / ".manifest.json"


def read_manifest(kb_id: str, cfg: config_mod.Config) -> dict:
    p = manifest_path(kb_id, cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "files": {}}
    return {"version": 1, "files": {}}


def write_manifest(kb_id: str, cfg: config_mod.Config, manifest: dict) -> None:
    p = manifest_path(kb_id, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
