"""爬取结束后的自然语言搜索：对每个仍未找到的 topic 执行一次「学校 + 专业 + topic」搜索并补抽。"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from crawler.browser_session import BrowserSession
from crawler.graph import (
    _merge_url_nav_edge,
    _norm_url,
    extract_topics_from_page_text,
)
from crawler.search_agent import create_search_agent
from crawler.search_utils import (
    deduplicate_urls,
    infer_school_and_program,
    pick_best_search_url,
)
from crawler.state import CrawlerState

# 仅当 max_iterations 达到该值时才执行爬后搜索（与 natural_language_search 联用）
POST_CRAWL_SEARCH_MIN_MAX_ITER = 30


def _unfound_topics(topics: List[str], results: Dict[str, str]) -> List[str]:
    return [t for t in topics if not (results.get(t) or "").strip()]


def run_post_crawl_natural_language_search(
    session: BrowserSession,
    state: CrawlerState,
    *,
    max_search_results: int = 8,
) -> CrawlerState:
    """
    主图结束后：对每个未找到的 topic 搜索一次，打开最佳结果页并抽取合并。
    调用方需保证 session 仍可用且 enable_search / max_iter 条件已满足。
    """
    topics: List[str] = list(state.get("topics") or [])
    results: Dict[str, str] = dict(state.get("results") or {})
    base_url = (state.get("url") or "").strip()
    skill_context = state.get("skill_context")

    unfound = _unfound_topics(topics, results)
    if not unfound:
        print("[AgentCrawler] 爬后搜索：所有 topic 已有答案，跳过", file=sys.stderr)
        return state

    university, program = infer_school_and_program(base_url, results)
    print(
        f"[AgentCrawler] 爬后搜索：待补全 {len(unfound)} 个 topic；"
        f"学校={university!r} 专业={program!r}",
        file=sys.stderr,
    )

    search_agent = create_search_agent()
    url_node_topics = dict(state.get("url_node_topics") or {})
    url_display = dict(state.get("url_display") or {})

    for i, topic in enumerate(unfound, 1):
        if (results.get(topic) or "").strip():
            continue

        print(
            f"[AgentCrawler] 爬后搜索 ({i}/{len(unfound)}): {topic}",
            file=sys.stderr,
        )
        try:
            search_results = search_agent.search_by_topic(
                topic=topic,
                base_url=base_url,
                max_results=max_search_results,
                university=university,
                program=program,
            )
        except Exception as e:
            print(f"[AgentCrawler] 搜索失败 '{topic}': {e}", file=sys.stderr)
            continue

        urls = deduplicate_urls(search_agent.extract_urls(search_results))
        best = pick_best_search_url(urls, base_url)
        if not best:
            print(f"[AgentCrawler] 无可用搜索结果 URL: {topic}", file=sys.stderr)
            continue

        try:
            session.start()
            before_n = _norm_url(session.current_url or base_url)
            session.goto(best)
            session.expand_collapsed_content()
            page_text = session.visible_text()
        except Exception as e:
            print(f"[AgentCrawler] 打开搜索结果页失败: {e}", file=sys.stderr)
            continue

        still_empty = _unfound_topics(topics, results)
        if not still_empty:
            break

        try:
            part = extract_topics_from_page_text(
                page_text,
                still_empty,
                results,
                skill_context=skill_context,
            )
        except Exception as e:
            print(f"[AgentCrawler] 搜索结果页抽取失败: {e}", file=sys.stderr)
            continue

        for k, v in (part.get("results") or {}).items():
            if k in topics and (v or "").strip():
                results[k] = v

        page_n = _norm_url(session.current_url or best)
        raw_u = (session.current_url or best).strip()
        if page_n and raw_u:
            url_display[page_n] = raw_u
        updated_keys = list(part.get("updated_topic_keys") or [])
        if page_n and updated_keys:
            cur = list(url_node_topics.get(page_n) or [])
            for k in updated_keys:
                if k not in cur:
                    cur.append(k)
            url_node_topics[page_n] = cur

        edge_stub: Dict[str, Any] = {
            "url_nav_edges": list(state.get("url_nav_edges") or []),
            "url_display": url_display,
        }
        edge_extra = _merge_url_nav_edge(
            edge_stub,
            _norm_url(base_url),
            session.current_url or best,
        )
        if edge_extra.get("url_nav_edges"):
            state["url_nav_edges"] = edge_extra["url_nav_edges"]
        if edge_extra.get("url_display"):
            url_display.update(edge_extra["url_display"])

    state["results"] = results
    state["pending_topics"] = _unfound_topics(topics, results)
    state["url_node_topics"] = url_node_topics
    state["url_display"] = url_display
    found_after = len(topics) - len(state["pending_topics"])
    print(
        f"[AgentCrawler] 爬后搜索结束：已回答 {found_after}/{len(topics)} 个 topic",
        file=sys.stderr,
    )
    return state
