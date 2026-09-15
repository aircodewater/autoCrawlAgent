"""
入口：根据 URL 与主题集合运行 LangGraph 多智能体爬虫（ReAct 式规划 + 浏览器交互）。
LLM：默认通义；可选 DeepSeek、Kimi/Moonshot（见 .env 与 LLM_PROVIDER）。
需执行 `playwright install chromium`。
待回答问题列表从本地文件读取（默认与 main.py 同目录下的 `topic`，每行一条）。
可选：`--skill` / `--skill-dir` 加载本地 SKILL.md，注入各步 LLM 系统提示。
导出流程图：`python main.py --export-graph docs/crawler.mmd`（无需 url；PNG 需 pygraphviz）。
问题与修改记录见 `docs/agentcrawler-issue-log.md`（改 bug 时请追加一条）。
可选：项目根目录 `crawl_runtime.json` 作为默认参数（`--no-runtime-config` 禁用）；
`export_url_fields` / `export_url_tree` 或 `EXPORT_URL_FIELDS` / `EXPORT_URL_TREE=0` 控制附属 JSON/HTML。
环境变量：`EXTRACT_TOPIC_BATCH_SIZE`（默认 12）、`LLM_MAX_OUTPUT_TOKENS`（默认 8192）；Kimi k2.5/2.6 快速模式：`KIMI_MODE=instant` 或 `MOONSHOT_THINKING=disabled`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from crawler.graph import run_crawl
from crawler.graph_export import export_crawl_graph
from crawler.runtime_config import (
    argparse_defaults_from_runtime,
    load_runtime_config,
    resolve_runtime_config_path,
)
from crawler.skill_loader import load_skills
from crawler.url_tree_report import write_url_fields_by_page, write_url_tree_artifacts

_DEFAULT_TOPIC_FILE = Path(__file__).resolve().parent / "topic"
# 未指定 -o 时落盘目录（主 JSON、url_fields、url_tree JSON/HTML）；可用 -o 覆盖
_DEFAULT_OUTPUT_DIR = r"D:\canada\dsv4pro"


def load_topics_from_file(path: Path) -> list[str]:
    """从文本文件读取 topic，每行一条；空行与 # 开头的注释行忽略。"""
    if not path.is_file():
        raise FileNotFoundError(f"topic 文件不存在: {path}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    out: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    if not out:
        raise ValueError(f"topic 文件无有效条目: {path}")
    return out


def _export_optional_artifact_enabled(
    *,
    disabled: bool,
    force_enable: bool,
    env_var: str,
    default_on: bool = True,
) -> bool:
    """CLI 显式关闭优先；显式开启次之；否则读环境变量（未设则用 default_on）。"""
    if disabled:
        return False
    if force_enable:
        return True
    raw = os.environ.get(env_var)
    if raw is None:
        return default_on
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _export_url_fields_enabled(*, no_url_fields: bool, url_fields: bool) -> bool:
    return _export_optional_artifact_enabled(
        disabled=no_url_fields,
        force_enable=url_fields,
        env_var="EXPORT_URL_FIELDS",
    )


def _export_url_tree_enabled(*, no_url_tree: bool, url_tree: bool) -> bool:
    return _export_optional_artifact_enabled(
        disabled=no_url_tree,
        force_enable=url_tree,
        env_var="EXPORT_URL_TREE",
    )


def _configure_stdio_utf8() -> None:
    """避免 Windows 下 stdout/stderr 接管道时默认 GBK，与父进程按 UTF-8 读子进程输出不一致而乱码。"""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main() -> None:
    _configure_stdio_utf8()
    load_dotenv()

    runtime_path = resolve_runtime_config_path()
    runtime_cfg: dict = {}
    if runtime_path is not None:
        try:
            runtime_cfg = load_runtime_config(runtime_path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(
                json.dumps({"error": f"读取运行配置失败: {e}"}, ensure_ascii=False),
                file=sys.stderr,
            )
            raise SystemExit(1) from e

    parser = argparse.ArgumentParser(
        description="主题驱动智能爬虫（LangGraph；LLM 可选通义 / DeepSeek）"
    )
    parser.add_argument(
        "--runtime-config",
        type=str,
        default=None,
        metavar="PATH",
        help="运行配置 JSON（默认：若存在则加载项目根目录 crawl_runtime.json）",
    )
    parser.add_argument(
        "--no-runtime-config",
        action="store_true",
        help="不读取 crawl_runtime.json（忽略默认配置文件）",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="起始页面 URL（使用 --export-graph 导出流程图时可省略）",
    )
    parser.add_argument(
        "--topic-file",
        type=str,
        default=str(_DEFAULT_TOPIC_FILE),
        metavar="PATH",
        help=f"待回答问题列表文件路径（UTF-8，每行一条；# 开头为注释）。默认: {_DEFAULT_TOPIC_FILE.name}",
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=6,
        help="最大页面内交互轮次（点击/导航）",
    )
    parser.add_argument(
        "--max-home-retreats",
        type=int,
        default=6,
        metavar="N",
        help="规划无有效交互时，从子页退回起始 URL 再规划的最多次数（0 关闭）",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="有头模式显示浏览器",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=_DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help=(
            "结果 JSON / URL 结构树的存放目录（不存在则自动创建）。"
            f"默认: {_DEFAULT_OUTPUT_DIR}；传空字符串可仅 stdout、不写文件"
        ),
    )
    parser.add_argument(
        "--include-interactive-text",
        action="store_true",
        help="在 JSON 中附带最后一帧可交互元素摘要（interactive_text）；使用 -o 时默认附带",
    )
    parser.add_argument(
        "--no-refine-results",
        action="store_true",
        help="跳过结束后对 results 的 LLM 精炼整合（默认会精炼多轮合并的长文）",
    )
    parser.add_argument(
        "--no-url-fields",
        action="store_true",
        help="使用 -o 时不写入 *_url_fields.json（各 URL 爬到了哪些字段；默认写入）",
    )
    parser.add_argument(
        "--url-fields",
        action="store_true",
        help="显式写入 *_url_fields.json（默认已开启；仅在与环境变量 EXPORT_URL_FIELDS=0 联用时需指定）",
    )
    parser.add_argument(
        "--no-url-tree",
        action="store_true",
        help="使用 -o 时不写入 *_url_tree.json / *_url_tree.html（默认写入）",
    )
    parser.add_argument(
        "--url-tree",
        action="store_true",
        help="显式写入 URL 结构树文件（默认已开启；仅在与 EXPORT_URL_TREE=0 联用时需指定）",
    )
    parser.add_argument(
        "--llm",
        type=str,
        choices=["tongyi", "deepseek", "kimi"],
        default=None,
        metavar="NAME",
        help="LLM 基座：tongyi、deepseek、kimi（Moonshot）；不指定则读 LLM_PROVIDER（默认 tongyi）",
    )
    parser.add_argument(
        "--skill",
        action="append",
        default=None,
        metavar="PATH",
        help="本地 Skill 文件路径（Markdown，如 SKILL.md）；可重复指定多个",
    )
    parser.add_argument(
        "--skill-dir",
        action="append",
        default=None,
        metavar="DIR",
        help="扫描目录下所有 SKILL.md（含子目录）；可重复指定多个目录",
    )
    parser.add_argument(
        "--skill-max-chars",
        type=int,
        default=32000,
        metavar="N",
        help="注入 LLM 的 Skill 正文总长度上限（默认 32000）",
    )
    parser.add_argument(
        "--export-graph",
        type=str,
        default=None,
        metavar="FILE",
        help="导出 LangGraph 流程图到该文件后退出；.mmd/.md 为 Mermaid（推荐）；.txt 为 ASCII（需 grandalf）；.png 需 pygraphviz+Graphviz",
    )
    parser.add_argument(
        "--export-graph-format",
        type=str,
        choices=["mermaid", "png", "ascii"],
        default=None,
        metavar="FMT",
        help="覆盖由后缀推断的导出格式（mermaid / png / ascii）",
    )
    parser.add_argument(
        "--enable-search",
        action="store_true",
        help=(
            "爬取结束后对仍未找到的 topic 做外搜（关键词：学校+专业+topic）；"
            "爬取过程中不搜索；须 --max-iter>=30；也可用 crawl_runtime.json 的 natural_language_search"
        ),
    )
    parser.add_argument(
        "--search-engine",
        type=str,
        choices=["google", "bing", "duckduckgo"],
        default=None,
        metavar="ENGINE",
        help="搜索引擎选择：google（需 GOOGLE_API_KEY + GOOGLE_SEARCH_ENGINE_ID）、bing（需 BING_API_KEY）、duckduckgo（免费，无需 API 密钥）",
    )
    parser.add_argument(
        "--search-threshold",
        type=float,
        default=0.5,
        metavar="THRESHOLD",
        help="（已废弃，爬中不再使用）保留以兼容旧脚本",
    )
    if runtime_cfg:
        parser.set_defaults(**argparse_defaults_from_runtime(runtime_cfg))
    args = parser.parse_args()
    if runtime_path is not None and runtime_cfg:
        print(
            f"[AgentCrawler] 已加载运行配置: {runtime_path.resolve()}",
            file=sys.stderr,
        )
    if isinstance(args.output, str) and not args.output.strip():
        args.output = None
    if args.llm:
        os.environ["LLM_PROVIDER"] = args.llm
    
    # 设置搜索引擎环境变量
    if args.search_engine:
        os.environ["SEARCH_ENGINE"] = args.search_engine
        print(f"[AgentCrawler] 搜索引擎: {args.search_engine}", file=sys.stderr)

    if args.export_graph:
        out = Path(args.export_graph).expanduser()
        try:
            saved = export_crawl_graph(out, fmt=args.export_graph_format)
        except (OSError, RuntimeError) as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(1) from e
        print(
            f"[AgentCrawler] 流程图已写入: {saved}（Mermaid 可用 https://mermaid.live 预览）",
            file=sys.stderr,
        )
        return

    if not args.url:
        parser.error("需要提供起始页面 url，或使用 --export-graph 导出流程图")

    topic_path = Path(args.topic_file).expanduser().resolve()
    try:
        topics = load_topics_from_file(topic_path)
    except (OSError, ValueError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from e
    print(
        f"[AgentCrawler] 已从文件加载 {len(topics)} 个 topic: {topic_path}",
        file=sys.stderr,
    )

    skill_files = list(args.skill or [])
    skill_dirs = list(args.skill_dir or [])
    skill_text = ""
    if skill_files or skill_dirs:
        try:
            skill_text = load_skills(
                files=skill_files,
                dirs=skill_dirs,
                max_total_chars=max(1000, args.skill_max_chars),
            )
        except (OSError, FileNotFoundError, NotADirectoryError) as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(1) from e
        if skill_text:
            print(
                f"[AgentCrawler] 已加载本地 Skill，约 {len(skill_text)} 字符",
                file=sys.stderr,
            )
        else:
            print(
                "[AgentCrawler] 已指定 --skill / --skill-dir 但未找到可读的 SKILL 内容",
                file=sys.stderr,
            )

    crawl_started_at = datetime.now().isoformat(timespec="seconds")
    t_crawl0 = time.perf_counter()
    try:
        out = run_crawl(
            args.url,
            topics,
            headless=not args.headed,
            max_iterations=args.max_iter,
            max_home_retreats=max(0, args.max_home_retreats),
            refine_results=not args.no_refine_results,
            skill_context=skill_text or None,
            enable_search=args.enable_search,
            search_threshold=args.search_threshold,
        )
    except Exception as e:
        elapsed = time.perf_counter() - t_crawl0
        print(
            f"[AgentCrawler] 爬取异常退出，耗时 {elapsed:.2f}s（自 run_crawl 起算）",
            file=sys.stderr,
        )
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from e
    crawl_elapsed_s = round(time.perf_counter() - t_crawl0, 3)
    crawl_finished_at = datetime.now().isoformat(timespec="seconds")
    print(
        f"[AgentCrawler] 本次爬取耗时 {crawl_elapsed_s}s（{crawl_started_at} → {crawl_finished_at}）",
        file=sys.stderr,
    )

    payload = {
        "url": args.url,
        "topic_file": str(topic_path),
        "skill_injected": bool(skill_text),
        "skill_chars": len(skill_text) if skill_text else 0,
        "crawl_started_at": crawl_started_at,
        "crawl_finished_at": crawl_finished_at,
        "crawl_elapsed_seconds": crawl_elapsed_s,
        "results": out.get("results"),
        "finish_reason": out.get("finish_reason"),
        "iteration": out.get("iteration"),
        "last_error": out.get("last_error"),
        "plan_reason": out.get("plan_reason"),
        "explore_worthy": out.get("explore_worthy"),
        "nav_stack_depth": len(out.get("nav_stack") or []),
        "home_retreat_count": out.get("home_retreat_count", 0),
        "max_home_retreats": out.get("max_home_retreats", 0),
        "llm_token_usage": out.get("llm_token_usage"),
    }
    if args.output or args.include_interactive_text:
        payload["interactive_text"] = out.get("interactive_text") or ""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        out_dir = Path(args.output)
        if out_dir.exists() and not out_dir.is_dir():
            print(
                json.dumps(
                    {"error": f"-o/--output 必须是文件夹路径: {out_dir}"},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            raise SystemExit(1)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"crawl_{ts}"
        saved = (out_dir / f"{stem}.json").resolve()
        saved.write_text(text, encoding="utf-8")
        print(f"结果已保存: {saved}", file=sys.stderr)
        try:
            url_node_topics = dict(out.get("url_node_topics") or {})
            url_display = dict(out.get("url_display") or {})
            if _export_url_fields_enabled(
                no_url_fields=args.no_url_fields,
                url_fields=args.url_fields,
            ):
                j_fields = write_url_fields_by_page(
                    out_dir=out_dir,
                    stem=stem,
                    start_url=args.url,
                    url_node_topics=url_node_topics,
                    url_display=url_display,
                )
                print(f"[AgentCrawler] URL→字段映射: {j_fields}", file=sys.stderr)
            if _export_url_tree_enabled(
                no_url_tree=args.no_url_tree,
                url_tree=args.url_tree,
            ):
                j_tree, h_tree = write_url_tree_artifacts(
                    out_dir=out_dir,
                    stem=stem,
                    start_url=args.url,
                    url_nav_edges=list(out.get("url_nav_edges") or []),
                    url_node_topics=url_node_topics,
                    url_display=url_display,
                )
                print(f"[AgentCrawler] URL 结构树 JSON: {j_tree}", file=sys.stderr)
                print(
                    f"[AgentCrawler] URL 结构树可视化（用浏览器打开）: {h_tree}",
                    file=sys.stderr,
                )
        except OSError as e:
            print(
                f"[AgentCrawler] 写入 URL 附属文件失败（可忽略）: {e}",
                file=sys.stderr,
            )
    print(text)


if __name__ == "__main__":
    main()
