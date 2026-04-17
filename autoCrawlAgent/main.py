"""
入口：根据 URL 与主题集合运行 LangGraph 多智能体爬虫（ReAct 式规划 + 浏览器交互）。
LLM：默认阿里云通义（DASHSCOPE_API_KEY）；可选 DeepSeek（LLM_PROVIDER=deepseek + DEEPSEEK_API_KEY）。
需执行 `playwright install chromium`。
待回答问题列表从本地文件读取（默认与 main.py 同目录下的 `topic`，每行一条）。
可选：`--skill` / `--skill-dir` 加载本地 SKILL.md，注入各步 LLM 系统提示。
导出流程图：`python main.py --export-graph docs/crawler.mmd`（无需 url；PNG 需 pygraphviz）。
问题与修改记录见 `docs/agentcrawler-issue-log.md`（改 bug 时请追加一条）。
环境变量：`EXTRACT_TOPIC_BATCH_SIZE`（默认 12）、`LLM_MAX_OUTPUT_TOKENS`（默认 8192）缓解抽取 JSON 截断。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from crawler.graph import run_crawl
from crawler.graph_export import export_crawl_graph
from crawler.skill_loader import load_skills

_DEFAULT_TOPIC_FILE = Path(__file__).resolve().parent / "topic"


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


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="主题驱动智能爬虫（LangGraph；LLM 可选通义 / DeepSeek）"
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
        default=None,
        metavar="DIR",
        help="结果 JSON 的存放目录（不存在则自动创建；每次运行生成带时间戳的文件名）",
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
        "--llm",
        type=str,
        choices=["tongyi", "deepseek"],
        default=None,
        metavar="NAME",
        help="LLM 基座：tongyi（阿里云通义）或 deepseek；不指定则读环境变量 LLM_PROVIDER（默认 tongyi）",
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
        help="启用基于自然语言搜索的功能（自动为待处理主题生成搜索查询）",
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
        help="搜索阈值（0-1），值越大越倾向于执行搜索（默认 0.5）",
    )
    args = parser.parse_args()
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
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from e

    payload = {
        "url": args.url,
        "topic_file": str(topic_path),
        "topics": topics,
        "skill_injected": bool(skill_text),
        "skill_chars": len(skill_text) if skill_text else 0,
        "results": out.get("results"),
        "pending_topics": out.get("pending_topics"),
        "finish_reason": out.get("finish_reason"),
        "iteration": out.get("iteration"),
        "last_error": out.get("last_error"),
        "plan_reason": out.get("plan_reason"),
        "explore_worthy": out.get("explore_worthy"),
        "nav_stack_depth": len(out.get("nav_stack") or []),
        "home_retreat_count": out.get("home_retreat_count", 0),
        "max_home_retreats": out.get("max_home_retreats", 0),
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
        name = f"crawl_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        saved = (out_dir / name).resolve()
        saved.write_text(text, encoding="utf-8")
        print(f"结果已保存: {saved}", file=sys.stderr)
    print(text)


if __name__ == "__main__":
    main()
