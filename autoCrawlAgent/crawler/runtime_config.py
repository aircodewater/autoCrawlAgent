"""从 crawl_runtime.json 加载运行参数，供 main.py 作为 argparse 默认值（命令行仍可覆盖）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# 与 main.py 同目录
DEFAULT_RUNTIME_JSON = Path(__file__).resolve().parent.parent / "crawl_runtime.json"


def resolve_runtime_config_path(argv: Optional[list[str]] = None) -> Optional[Path]:
    """
    在完整 argparse 之前解析是否加载配置文件。
    --no-runtime-config → 不加载；--runtime-config PATH → 指定路径；
    否则若存在 DEFAULT_RUNTIME_JSON 则使用。
    """
    args = argv if argv is not None else sys.argv[1:]
    if "--no-runtime-config" in args:
        return None
    for i, a in enumerate(args):
        if a == "--runtime-config" and i + 1 < len(args):
            return Path(args[i + 1]).expanduser()
        if a.startswith("--runtime-config="):
            return Path(a.split("=", 1)[1]).expanduser()
    return DEFAULT_RUNTIME_JSON if DEFAULT_RUNTIME_JSON.is_file() else None


def load_runtime_config(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"运行配置文件不存在: {path}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"运行配置须为 JSON 对象: {path}")
    return data


def argparse_defaults_from_runtime(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """将 crawl_runtime.json 字段映射为 main.py 的 argparse 默认值。"""
    defaults: Dict[str, Any] = {}

    url = cfg.get("start_url") or cfg.get("url")
    if url:
        defaults["url"] = str(url).strip()

    if cfg.get("topic_file"):
        defaults["topic_file"] = str(cfg["topic_file"])

    out = cfg.get("output_dir") if cfg.get("output_dir") is not None else cfg.get("output")
    if out is not None:
        defaults["output"] = str(out)

    for key, dest in (
        ("max_iter", "max_iter"),
        ("max_iterations", "max_iter"),
        ("max_home_retreats", "max_home_retreats"),
        ("skill_max_chars", "skill_max_chars"),
    ):
        if cfg.get(key) is not None:
            defaults[dest] = int(cfg[key])

    if cfg.get("search_threshold") is not None:
        defaults["search_threshold"] = float(cfg["search_threshold"])

    if cfg.get("llm"):
        defaults["llm"] = str(cfg["llm"]).strip().lower()

    if cfg.get("search_engine"):
        defaults["search_engine"] = str(cfg["search_engine"]).strip().lower()

    if cfg.get("headed") is True:
        defaults["headed"] = True

    if cfg.get("include_interactive_text") is True:
        defaults["include_interactive_text"] = True

    # natural_language_search：爬取结束后对未找到的 topic 做外搜（须 max_iter>=30）
    if cfg.get("natural_language_search") or cfg.get("enable_search"):
        defaults["enable_search"] = True

    if cfg.get("refine_results") is False:
        defaults["no_refine_results"] = True

    # export_url_fields：false 等价于 --no-url-fields
    if "export_url_fields" in cfg:
        enabled = bool(cfg["export_url_fields"])
        defaults["no_url_fields"] = not enabled
        defaults["url_fields"] = enabled

    # export_url_tree：false 等价于 --no-url-tree
    if "export_url_tree" in cfg:
        enabled = bool(cfg["export_url_tree"])
        defaults["no_url_tree"] = not enabled
        defaults["url_tree"] = enabled

    # on_site_search：预留，图内站点搜索尚未单独开关
    return defaults
