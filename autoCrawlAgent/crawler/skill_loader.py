"""加载本地 Markdown 领域说明文件，供爬虫 LLM 注入系统提示。"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence


def strip_yaml_frontmatter(text: str) -> str:
    """去掉 SKILL.md 顶部的 YAML frontmatter（--- ... ---）。"""
    t = text.lstrip("\ufeff").strip()
    if not t.startswith("---"):
        return text
    lines = t.splitlines()
    if len(lines) < 2:
        return text
    end: Optional[int] = None
    for i in range(1, min(len(lines), 400)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return text
    return "\n".join(lines[end + 1 :]).strip()


def _collect_paths(
    files: Sequence[str],
    dirs: Sequence[str],
) -> List[Path]:
    paths: List[Path] = []
    seen: set[Path] = set()
    for f in files:
        p = Path(f).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Skill 文件不存在: {p}")
        if p not in seen:
            seen.add(p)
            paths.append(p)
    for d in dirs:
        root = Path(d).expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"Skill 目录不存在: {root}")
        for p in sorted(root.rglob("SKILL.md")):
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                paths.append(rp)
    return paths


def load_skills(
    *,
    files: Sequence[str] | None = None,
    dirs: Sequence[str] | None = None,
    max_total_chars: int = 32000,
) -> str:
    """
    读取若干 Skill 文件正文（UTF-8），去掉 frontmatter，按顺序拼接。
    总长度超过 max_total_chars 时截断并附加说明。
    """
    paths = _collect_paths(list(files or []), list(dirs or []))
    if not paths:
        return ""
    chunks: List[str] = []
    total = 0
    for p in paths:
        raw = p.read_text(encoding="utf-8", errors="replace")
        body = strip_yaml_frontmatter(raw)
        label = f"{p.name} @ {p.parent}"
        header = f"\n\n--- 来源: {label} ---\n"
        piece = header + body.strip()
        if total + len(piece) > max_total_chars:
            remain = max_total_chars - total - len(
                "\n...[Skill 已达长度上限，已截断]\n"
            )
            if remain > 400:
                piece = piece[:remain] + "\n...[Skill 已达长度上限，已截断]"
                chunks.append(piece)
            break
        chunks.append(piece)
        total += len(piece)
        if total >= max_total_chars:
            break
    return "".join(chunks).strip()
