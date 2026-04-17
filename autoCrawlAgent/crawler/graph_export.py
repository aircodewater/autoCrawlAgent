"""导出爬虫 LangGraph 结构图（Mermaid / ASCII / PNG），便于文档与演示。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from crawler.graph import build_graph


class _GraphBuildPlaceholder:
    """仅占位；`build_graph` 仅在编译时引用 session，导出图结构时不执行节点。"""


def get_crawl_graph_visual():
    """返回 langchain_core 的 Graph 对象，可调用 draw_mermaid / draw_ascii / draw_png。"""
    compiled = build_graph(_GraphBuildPlaceholder())  # type: ignore[arg-type]
    return compiled.get_graph()


def export_crawl_graph(
    output: Path | str,
    *,
    fmt: Optional[Literal["mermaid", "png", "ascii"]] = None,
) -> Path:
    """
    将当前爬虫状态图写入文件。

    - mermaid：默认；可用 GitHub / Notion / mermaid.live 渲染。
    - ascii：需 ``pip install grandalf``。
    - png：需 ``pip install pygraphviz``，且系统装有 Graphviz。

    若未指定 fmt，则根据文件后缀推断：.png → png，.txt → ascii，其余 → mermaid。
    返回写入路径的 resolve() 结果。
    """
    path = Path(output)
    resolved_fmt: Literal["mermaid", "png", "ascii"]
    if fmt is not None:
        resolved_fmt = fmt
    else:
        suf = path.suffix.lower()
        if suf == ".png":
            resolved_fmt = "png"
        elif suf in (".txt", ".ascii"):
            resolved_fmt = "ascii"
        else:
            resolved_fmt = "mermaid"

    path.parent.mkdir(parents=True, exist_ok=True)
    graph = get_crawl_graph_visual()

    if resolved_fmt == "mermaid":
        path.write_text(graph.draw_mermaid(), encoding="utf-8")
        return path.resolve()

    if resolved_fmt == "ascii":
        try:
            ascii_body = graph.draw_ascii()
        except ImportError as e:
            raise RuntimeError(
                "导出 ASCII 流程图需要安装 grandalf：`pip install grandalf`"
            ) from e
        path.write_text(ascii_body, encoding="utf-8")
        return path.resolve()

    # png
    try:
        graph.draw_png(output_file_path=str(path.resolve()))
    except ImportError as e:
        raise RuntimeError(
            "导出 PNG 需要安装 pygraphviz，并确保系统已安装 Graphviz："
            "`pip install pygraphviz`（Windows 可先装 Graphviz 并配置 PATH）"
        ) from e
    return path.resolve()
