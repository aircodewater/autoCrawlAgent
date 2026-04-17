from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

from langgraph.graph import END, START, StateGraph

from crawler.browser_session import BrowserSession
from crawler.llm_client import (
    DomChangeDecision,
    ExtractionPayload,
    ExploreWorthiness,
    InteractionPlan,
    extract_with_schema,
    get_chat_model,
    refine_merged_results,
)
from crawler.search_integration import (
    SearchDrivenCrawler,
    SearchDecisionMaker,
    create_search_driven_crawler,
    create_search_decision_maker,
)
from crawler.state import CrawlerState


def _extract_topic_batch_size() -> int:
    """每批送入抽取模型的 topic 数量；过大易触发 JSON 截断。"""
    try:
        n = int(os.environ.get("EXTRACT_TOPIC_BATCH_SIZE", "12"))
    except ValueError:
        n = 12
    return max(4, min(n, 40))


def _merge_two_payloads(a: ExtractionPayload, b: ExtractionPayload) -> ExtractionPayload:
    tt = {**(a.topic_texts or {}), **(b.topic_texts or {})}
    pend: List[str] = []
    seen: set[str] = set()
    for t in (a.pending_topics or []) + (b.pending_topics or []):
        if t and t not in seen:
            seen.add(t)
            pend.append(t)
    return ExtractionPayload(topic_texts=tt, pending_topics=pend)


def _extract_batch_recursive(
    sys_extract: str,
    batch: List[str],
    page_text: str,
    prior_json: str,
) -> ExtractionPayload:
    """单批抽取；JSON 解析失败时将本批对半拆分递归，降低单次输出长度。"""
    user = (
        f"【本批待回答 topic：topic_texts 的键必须且只能来自下列列表】\n{batch}\n\n"
        f"当前页正文：\n{page_text}\n\n"
        f"此前各问题已累积的回答（本页可补充；无补充则不要硬写）：\n{prior_json}\n"
    )
    try:
        return extract_with_schema(sys_extract, user, ExtractionPayload)
    except Exception:
        if len(batch) <= 1:
            raise
        mid = max(1, len(batch) // 2)
        left = _extract_batch_recursive(sys_extract, batch[:mid], page_text, prior_json)
        right = _extract_batch_recursive(sys_extract, batch[mid:], page_text, prior_json)
        return _merge_two_payloads(left, right)


def _skill_suffix(state: CrawlerState) -> str:
    """将本地 Skill 正文附加到系统提示末尾（未加载则为空）。"""
    sk = (state.get("skill_context") or "").strip()
    if not sk:
        return ""
    return "\n\n【附加：领域说明（本地 Skill 文件）】\n" + sk + "\n"


def _all_topics_non_empty(merged: Dict[str, str], topics: List[str]) -> bool:
    if not topics:
        return True
    return all((merged.get(t) or "").strip() for t in topics)


_JUNK_NAV = re.compile(
    r"(login|sign[\s-]?in|register|logout|购物车|cart|cookie|javascript:|mailto:)|"
    r"(登录|注册|登出)",
    re.I,
)

# 若本轮某问题的「答案」正文出现下列引导跳转/套话，视为无效摘录，不并入 results
_NAV_TEASER_IN_ANSWER = re.compile(
    r"(详见|"
    r"请点击|点击此处|点击链接|点击查看|点击进入|单击|点此|"
    r"请参考|参阅[：:]|请参见|参见[：:]|"
    r"了解更多|进一步了解|查看详情|阅读全文|更多详情|查看完整|"
    r"跳转至|前往查看|"
    r"click\s+here|learn\s+more|read\s+more|see\s+more|find\s+out\s+more)",
    re.I,
)


def _topic_answer_is_nav_teaser(text: str) -> bool:
    """正文是否主要为「让读者去别处看」的导航式套话（命中则本键本次不合并）。"""
    s = (text or "").strip()
    if not s:
        return False
    return bool(_NAV_TEASER_IN_ANSWER.search(s))


def _norm_url(u: str) -> str:
    """用于判断是否同一页面的规范化 URL（忽略 fragment，统一路径与主机）。"""
    u = (u or "").strip()
    if not u:
        return ""
    try:
        if "://" not in u and u.startswith("//"):
            u = "https:" + u
        elif "://" not in u:
            u = "https://" + u.lstrip("/")
        p = urlparse(u)
        scheme = (p.scheme or "https").lower()
        netloc = (p.netloc or "").lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = p.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        path = path or "/"
        return f"{scheme}://{netloc}{path}".lower()
    except Exception:
        return (u or "").split("#")[0].lower().rstrip("/")


def _merge_visited(state: CrawlerState, *urls: str) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for x in (state.get("visited_urls") or []) + list(urls):
        n = _norm_url(x)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _click_signature(it: Dict[str, Any]) -> str:
    """可交互元素稳定签名，用于识别「已点过但未跳转」的控件。"""
    role = (it.get("role") or "").strip()
    text = (it.get("text") or "").strip()[:200]
    href = (it.get("href") or "").strip()
    return f"{role}|{text}|{href}"


def _indices_avoid_href_revisit(
    items: List[Dict[str, Any]], page_url: str, visited_set: set[str]
) -> List[int]:
    """禁止选择指向「当前规范化 URL」或「已访问过的规范化 URL」的带 href 项，减轻反复打开同一页。"""
    curr_n = _norm_url(page_url)
    out: List[int] = []
    for i, it in enumerate(items):
        href = (it.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        try:
            sch = (urlparse(href).scheme or "").lower()
            if sch and sch not in ("http", "https"):
                continue
        except Exception:
            continue
        hn = _norm_url(href)
        if not hn:
            continue
        if hn == curr_n or hn in visited_set:
            out.append(i)
    return out


def _filter_nav_targets_dfs(
    targets: List[Dict[str, str]], page_url: str, visited_set: set[str]
) -> List[Dict[str, str]]:
    """排除当前页与已访问页，避免反复 goto 同一 URL。"""
    pn = _norm_url(page_url)
    out: List[Dict[str, str]] = []
    for t in targets:
        h = (t.get("href") or "").strip()
        if not h:
            continue
        hn = _norm_url(h)
        if not hn or hn == pn or hn in visited_set:
            continue
        out.append(t)
    return out


def _eligible_nav_targets(
    interactives: List[Dict[str, Any]], page_url: str, *, limit: int = 12
) -> List[Dict[str, str]]:
    """站内导航链接列表（去重），用于多导航 DFS。"""
    out: List[Dict[str, str]] = []
    seen_h: set[str] = set()
    try:
        base = urlparse(page_url)
        base_host = (base.netloc or "").lower()
    except Exception:
        return []
    for it in interactives:
        if not it.get("is_nav") or it.get("role") != "link":
            continue
        href = (it.get("href") or "").strip()
        text = (it.get("text") or "").strip()
        if not href or href.startswith("#"):
            continue
        if _JUNK_NAV.search(href) or _JUNK_NAV.search(text):
            continue
        try:
            u = urlparse(href)
            host = (u.netloc or "").lower()
            ok = False
            if host and base_host and host == base_host:
                ok = True
            elif href.startswith("/") and base_host:
                ok = True
            if not ok:
                continue
        except Exception:
            continue
        norm = href.split("#")[0]
        if norm in seen_h:
            continue
        seen_h.add(norm)
        out.append({"href": href, "text": text})
        if len(out) >= limit:
            break
    return out


def _nav_suggests_explore(interactives: List[Dict[str, Any]], page_url: str) -> bool:
    """存在指向本站、且非明显登录/注册的导航链接时，倾向继续进入子页整合信息。"""
    try:
        base = urlparse(page_url)
        base_host = (base.netloc or "").lower()
    except Exception:
        return False
    for it in interactives:
        if not it.get("is_nav") or it.get("role") != "link":
            continue
        href = (it.get("href") or "").strip()
        text = (it.get("text") or "").strip()
        if not href or href.startswith("#"):
            continue
        if _JUNK_NAV.search(href) or _JUNK_NAV.search(text):
            continue
        try:
            u = urlparse(href)
            host = (u.netloc or "").lower()
            if host and base_host and host == base_host:
                return True
            if href.startswith("/") and base_host:
                return True
            if not host and href.startswith("/"):
                return True
        except Exception:
            continue
    return False


def _merge_topic_text(old: str, new: str) -> str:
    new = (new or "").strip()
    if not new:
        return old
    old = (old or "").strip()
    if not old:
        return new
    if new in old:
        return old
    return old + "\n\n---\n\n" + new


def build_graph(session: BrowserSession, enable_search: bool = False, search_threshold: float = 0.5):
    """构建 ReAct 风格状态图：加载 → 快照 → LLM 抽取 → 条件分支 → 规划点击 → 循环。
    
    Args:
        session: 浏览器会话实例
        enable_search: 是否启用搜索功能
        search_threshold: 搜索阈值（0-1），值越大越倾向于搜索
    """
    # 初始化搜索相关组件
    search_crawler = None
    search_decision_maker = None
    if enable_search:
        search_crawler = create_search_driven_crawler(
            browser_session=session,
            enable_search=enable_search,
            max_search_results=10,
        )
        search_decision_maker = create_search_decision_maker(
            enable_auto_search=True,
            search_threshold=search_threshold,
        )

    def node_load(state: CrawlerState) -> Dict[str, Any]:
        url = state.get("url") or ""
        if not url:
            return {"finish_reason": "缺少 URL", "browser_ready": False}
        session.start()
        session.goto(url)
        return {"browser_ready": True, "last_error": None}

    def node_snapshot(state: CrawlerState) -> Dict[str, Any]:
        try:
            session.expand_collapsed_content()
            text = session.visible_text()
            items = session.collect_interactives()
            curr_n = _norm_url(session.current_url or state.get("url") or "")
            prev_n = state.get("snapshot_page_norm")
            page_changed = prev_n is not None and prev_n != curr_n
            no_nav = [] if page_changed else list(state.get("no_nav_signatures") or [])
            return {
                "page_text": text,
                "interactives": items,
                "interactive_text": session.interactives_as_text(items),
                "snapshot_page_norm": curr_n,
                "no_nav_signatures": no_nav,
            }
        except Exception as e:  # pragma: no cover
            return {"last_error": str(e), "page_text": "", "interactives": [], "interactive_text": ""}

    def node_extract(state: CrawlerState) -> Dict[str, Any]:
        topics: List[str] = state.get("topics") or []
        page_text = state.get("page_text") or ""
        prior: Dict[str, str] = dict(state.get("results") or {})
        sys_extract = (
            "你是网页信息抽取助手。用户给出的每一项 topic 都是一条**需要直接回答的问题**（不是关键词检索标签）。\n"
            "规则：\n"
            "1. **可写入 topic_texts 的内容**：必须能**具体作答**该问题——例如事实、条件、步骤、数据、定义、列表等；"
            "写成连贯摘要或摘录，去掉广告、导航、页脚、版权声明。\n"
            "2. **禁止当作答案写入**（须对应空字符串 \"\" 或**省略该键**，且将该 topic 放入 pending_topics）：\n"
            "   - 仅有与问题同主题的**板块标题、菜单名、按钮/链接文案**而无正文细节；\n"
            "   - 仅有**引导语、口号、营销句**（如 “Learn more…”、“What are the requirements?”）但**没有**要求、资格、流程、截止日期等实质信息；\n"
            "   - **反例**：问题为「中国学生申请要求」时，若正文只有 “For international students” 之类**栏目标签或入口提示**、"
            "而无语言成绩、学历材料、申请渠道、时间节点等，**不得**写入 topic_texts。\n"
            "3. 若本页提供了可作答的片段（即使不完整），可写入 topic_texts；仍缺的部分通过 pending_topics 标明需继续查找。\n"
            "4. topic_texts 的键必须严格来自用户消息中给出的**本批** topic 列表（系统可能分批提问）。\n"
            "5. pending_topics：列出在本页**仍未得到实质性回答**的 topic（含仅有线索、仅有入口提示的情况）。\n"
            "6. **禁止**在 topic_texts 的正文里使用「详见」「点击」「请参考」「了解更多」「click here」等引导跳转的套话代替实质内容；"
            "若只能写成这类句子，该 topic 请输出空字符串并列入 pending_topics。\n"
            "输出严格 JSON：{\"topic_texts\": {...}, \"pending_topics\": [...]}"
        ) + _skill_suffix(state)
        prior_json = json.dumps(prior, ensure_ascii=False)
        if len(prior_json) > 22000:
            prior_json = prior_json[:22000] + "\n...[此前回答过长已截断]"
        bs = _extract_topic_batch_size()
        acc = ExtractionPayload(topic_texts={}, pending_topics=[])
        try:
            if not topics:
                payload = acc
            elif len(topics) > bs:
                print(
                    f"[AgentCrawler] topic 抽取分 {(len(topics) + bs - 1) // bs} 批（每批最多 {bs} 条，"
                    f"可调环境变量 EXTRACT_TOPIC_BATCH_SIZE）",
                    file=sys.stderr,
                )
            if topics:
                for i in range(0, len(topics), bs):
                    batch = topics[i : i + bs]
                    part = _extract_batch_recursive(
                        sys_extract, batch, page_text, prior_json
                    )
                    acc = _merge_two_payloads(acc, part)
                payload = acc
            else:
                payload = acc
        except Exception as e:
            return {
                "last_error": f"抽取失败: {e}",
                "pending_topics": list(topics),
                "explore_worthy": False,
                "same_url_dom_changed": False,
                "dom_change_hint": None,
            }

        merged = dict(prior)
        skipped_teaser: List[str] = []
        for k, v in (payload.topic_texts or {}).items():
            if k not in topics:
                continue
            if _topic_answer_is_nav_teaser(v):
                skipped_teaser.append(k)
                continue
            merged[k] = _merge_topic_text(merged.get(k, ""), v)
        if skipped_teaser:
            print(
                f"[AgentCrawler] 本页抽取含导航引导语，已丢弃对应条目不合并：{skipped_teaser}",
                file=sys.stderr,
            )

        pending = list(payload.pending_topics or [])
        pending = [t for t in pending if t in topics]

        # 若模型未标 pending，但某主题仍为空，则自动视为待处理
        if not pending:
            pending = [t for t in topics if not (merged.get(t) or "").strip()]

        explore_worthy = False
        interactives = state.get("interactives") or []
        max_it = int(state.get("max_iterations") or 5)
        it = int(state.get("iteration") or 0)
        # 各主题均有非空结果时，由模型判断当前页是否仍有值得深化的信息入口
        if (
            not pending
            and _all_topics_non_empty(merged, topics)
            and interactives
            and it < max_it
        ):
            try:
                sys_e = (
                    "你是网页浏览策略助手。用户给出的每个 topic 都是一条**待回答问题**；当前累积文本应已能**实质性作答**（非仅有入口提示或标题）。\n"
                    "请根据「当前页可交互元素」判断：是否仍存在**值得点击进入**、可能**补全或深化答案**的入口"
                    "（例如：**导航/菜单中的站内栏目链接**、详情、正文全文、更多、相关文章、子栏目、下一页等）。\n"
                    "若列表含标注为「导航链接」且指向本站栏目/文档的入口，通常应倾向于继续探索（should_explore=true）。\n"
                    "若列表主要是登录、广告、语种切换、与问题无关的站外链接，应输出 should_explore=false。\n"
                    "输出严格 JSON：{\"should_explore\": true|false, \"reason\": \"简短说明\"}"
                ) + _skill_suffix(state)
                merged_preview = json.dumps(merged, ensure_ascii=False)[:4000]
                user_e = (
                    f"问题列表：{topics}\n"
                    f"已收集回答摘要：{merged_preview}\n\n"
                    f"可交互元素：\n{state.get('interactive_text') or ''}\n"
                )
                ew: ExploreWorthiness = extract_with_schema(sys_e, user_e, ExploreWorthiness)
                explore_worthy = bool(ew.should_explore)
            except Exception:
                explore_worthy = False
        elif not pending:
            explore_worthy = False

        # 存在可用的站内导航链接时，进入对应子页继续整合信息（避免模型过于保守）
        if (
            not pending
            and _all_topics_non_empty(merged, topics)
            and interactives
            and it < max_it
            and not explore_worthy
            and _nav_suggests_explore(interactives, session.current_url or state.get("url") or "")
        ):
            explore_worthy = True

        plan_note_new: Optional[str] = None
        dom_hint = (state.get("dom_change_hint") or "").strip()
        if state.get("same_url_dom_changed"):
            if pending:
                explore_worthy = True
                plan_note_new = (
                    "上一步点击后**网址未变**，但页面出现了新的可交互项（如展开菜单）。"
                    "请优先从**当前列表中尚未禁止的序号**里选择**子链接/子菜单项**，"
                    "不要再次选择仅用于展开同一菜单的同一触发按钮。"
                )
            elif (
                not pending
                and _all_topics_non_empty(merged, topics)
                and interactives
                and it < max_it
                and dom_hint
            ):
                try:
                    sys_d = (
                        "你是浏览策略助手。用户点击某控件后**页面 URL 未变**，但 DOM 上出现了新的可点击入口（见「新增摘要」）。\n"
                        "每个 topic 对应一个**待回答问题**；累积结果中应已有一定实质性文字。请判断：是否有必要再点击这些**新出现**的入口以**补全或深化答案**。\n"
                        "若新增项仅为重复导航、登录、社交图标或与问题无关，应输出 should_seek_further_interaction=false。\n"
                        "输出严格 JSON：{\"should_seek_further_interaction\": true|false, \"reason\": \"...\"}"
                    ) + _skill_suffix(state)
                    user_d = (
                        f"问题列表：{topics}\n"
                        f"新增可交互摘要：\n{dom_hint[:2500]}\n\n"
                        f"当前页可交互列表摘要：\n{(state.get('interactive_text') or '')[:2800]}\n"
                    )
                    dcd: DomChangeDecision = extract_with_schema(sys_d, user_d, DomChangeDecision)
                    if dcd.should_seek_further_interaction:
                        explore_worthy = True
                        plan_note_new = (dcd.reason or "模型建议继续点击新出现的入口。")[:600]
                except Exception:
                    pass

        plan_context_note_out = (
            plan_note_new
            if plan_note_new is not None
            else state.get("plan_context_note")
        )

        return {
            "results": merged,
            "pending_topics": pending,
            "explore_worthy": explore_worthy,
            "same_url_dom_changed": False,
            "dom_change_hint": None,
            "plan_context_note": plan_context_note_out,
            "last_error": None,
        }

    def node_nav_dfs(state: CrawlerState) -> Dict[str, Any]:
        """多导航深度优先：缓存父页 origin + 子链接列表，逐个进入子页整合后再回到父级试下一兄弟。"""
        if state.get("finish_reason"):
            return {"nav_route": "done"}
        it = int(state.get("iteration") or 0)
        max_it = int(state.get("max_iterations") or 5)
        if it >= max_it:
            return {"nav_route": "done"}

        page_url = session.current_url or state.get("url") or ""
        visited = _merge_visited(state, page_url)
        visited_set = set(visited)
        nav_stack = list(state.get("nav_stack") or [])

        raw = _eligible_nav_targets(state.get("interactives") or [], page_url)
        targets = _filter_nav_targets_dfs(raw, page_url, visited_set)

        # 当前页有 2 个及以上「未访问过」的导航：压栈并进入第一个子页（递归）
        if len(targets) >= 2 and it < max_it:
            first_href = targets[0]["href"]
            before_nav = _norm_url(session.current_url or page_url)
            nav_stack.append(
                {
                    "origin_url": page_url,
                    "children": targets,
                    "completed_upto": 0,
                }
            )
            try:
                session.goto(first_href)
            except Exception as e:
                nav_stack.pop()
                return {
                    "nav_stack": nav_stack,
                    "visited_urls": visited,
                    "last_error": str(e),
                    "nav_route": "fallthrough",
                }
            after_nav = _norm_url(session.current_url)
            # 若导航后仍停留在同一规范化页面，视为无效分叉，退栈并标记该 href，避免死循环
            if after_nav == before_nav:
                nav_stack.pop()
                visited = _merge_visited({"visited_urls": visited}, first_href)
                return {"nav_stack": nav_stack, "visited_urls": visited, "nav_route": "fallthrough"}
            visited = _merge_visited({"visited_urls": visited}, session.current_url)
            return {
                "nav_stack": nav_stack,
                "visited_urls": visited,
                "iteration": it + 1,
                "nav_route": "snapshot",
            }

        # 无法再分叉：回溯，进入下一兄弟或弹出父层（跳过已访问或与父页相同的链接）
        while nav_stack:
            visited_set = set(visited)
            top = nav_stack[-1]
            ch = top["children"]
            origin_n = _norm_url(top["origin_url"])
            top["completed_upto"] += 1
            cu = int(top["completed_upto"])
            while cu < len(ch):
                cand = ch[cu]
                hn = _norm_url(cand.get("href") or "")
                if not hn or hn in visited_set or hn == origin_n:
                    cu += 1
                    top["completed_upto"] = cu
                    continue
                try:
                    session.goto(top["origin_url"])
                    before_b = _norm_url(session.current_url)
                    session.goto(cand["href"])
                    after_b = _norm_url(session.current_url)
                    if after_b == before_b or after_b == origin_n:
                        visited = _merge_visited({"visited_urls": visited}, cand.get("href") or "")
                        visited_set = set(visited)
                        cu += 1
                        top["completed_upto"] = cu
                        continue
                    visited = _merge_visited({"visited_urls": visited}, session.current_url)
                    visited_set = set(visited)
                    return {
                        "nav_stack": nav_stack,
                        "visited_urls": visited,
                        "iteration": it + 1,
                        "nav_route": "snapshot",
                    }
                except Exception as e:
                    nav_stack.pop()
                    return {
                        "nav_stack": nav_stack,
                        "visited_urls": visited,
                        "last_error": str(e),
                        "nav_route": "fallthrough",
                    }
            nav_stack.pop()

        return {"nav_stack": [], "visited_urls": visited, "nav_route": "fallthrough"}

    def route_after_nav(state: CrawlerState) -> Literal["snapshot", "plan", "search", "end"]:
        nr = state.get("nav_route") or "fallthrough"
        if nr == "snapshot":
            return "snapshot"
        if nr == "done":
            return "end"
        # fallthrough：走原 ReAct 单步规划
        if state.get("finish_reason"):
            return "end"
        max_it = int(state.get("max_iterations") or 5)
        if int(state.get("iteration") or 0) >= max_it:
            return "end"
        items = state.get("interactives") or []
        if not items:
            return "end"
        pending: List[str] = state.get("pending_topics") or []
        if pending:
            # 如果启用搜索，检查是否需要搜索
            if search_crawler and search_decision_maker:
                decision = search_decision_maker.decide_search(
                    topics=state.get("topics") or [],
                    current_results=state.get("results") or {},
                    pending_topics=pending,
                    iteration=int(state.get("iteration") or 0),
                    max_iterations=max_it,
                    base_url=state.get("url") or "",
                )
                if decision.get("should_search"):
                    print(f"[AgentCrawler] 决定执行搜索: {decision.get('reason')}", file=sys.stderr)
                    return "search"
            return "plan"
        if state.get("explore_worthy"):
            return "plan"
        return "end"

    def node_search(state: CrawlerState) -> Dict[str, Any]:
        """搜索节点：基于待处理主题执行搜索，并返回搜索结果"""
        if not search_crawler:
            return {"last_error": "搜索功能未启用", "nav_route": "fallthrough"}
        
        topics: List[str] = state.get("topics") or []
        pending: List[str] = state.get("pending_topics") or []
        base_url = state.get("url") or ""
        current_results = state.get("results") or {}
        iteration = int(state.get("iteration") or 0)
        max_iterations = int(state.get("max_iterations") or 5)
        
        # 选择要搜索的主题（优先搜索前 3 个待处理主题）
        topics_to_search = pending[:3]
        
        if not topics_to_search:
            return {"nav_route": "fallthrough"}
        
        print(f"[AgentCrawler] 开始搜索 {len(topics_to_search)} 个主题", file=sys.stderr)
        
        # 对每个主题执行搜索
        all_search_results = []
        for topic in topics_to_search:
            try:
                search_result = search_crawler.search_and_crawl(
                    topic=topic,
                    base_url=base_url,
                    current_results=current_results,
                    pending_topics=pending,
                    iteration=iteration,
                    max_iterations=max_iterations,
                )
                
                if search_result.get("searched"):
                    print(
                        f"[AgentCrawler] 主题 '{topic}' 搜索完成: {search_result.get('message')}",
                        file=sys.stderr,
                    )
                    all_search_results.append({
                        "topic": topic,
                        "urls": search_result.get("urls_added", []),
                        "results": search_result.get("search_results", []),
                    })
            except Exception as e:
                print(f"[AgentCrawler] 主题 '{topic}' 搜索失败: {e}", file=sys.stderr)
                continue
        
        # 如果找到搜索结果，将第一个 URL 添加到待访问列表
        if all_search_results:
            # 选择第一个搜索结果的第一个 URL 进行访问
            first_result = all_search_results[0]
            urls = first_result.get("urls", [])
            if urls:
                print(f"[AgentCrawler] 选择搜索结果 URL: {urls[0]}", file=sys.stderr)
                # 导航到搜索结果页面
                try:
                    session.goto(urls[0])
                    return {
                        "nav_route": "snapshot",
                        "iteration": iteration + 1,
                    }
                except Exception as e:
                    print(f"[AgentCrawler] 导航到搜索结果失败: {e}", file=sys.stderr)
        
        # 如果没有搜索结果或导航失败，返回 fallthrough
        return {"nav_route": "fallthrough"}

    def node_plan(state: CrawlerState) -> Dict[str, Any]:
        topics: List[str] = state.get("topics") or []
        pending: List[str] = state.get("pending_topics") or []
        inter_lines = state.get("interactive_text") or ""
        results = state.get("results") or {}
        items = state.get("interactives") or []
        blocked_sigs = set(state.get("no_nav_signatures") or [])
        blocked_no_nav = [i for i, it in enumerate(items) if _click_signature(it) in blocked_sigs]
        page_url = session.current_url or state.get("url") or ""
        visited_set = set(state.get("visited_urls") or [])
        blocked_href_revisit = _indices_avoid_href_revisit(items, page_url, visited_set)
        forbidden_indices = sorted(set(blocked_no_nav) | set(blocked_href_revisit))
        ban_line = ""
        ctx = (state.get("plan_context_note") or "").strip()
        ctx_line = f"\n【上下文】{ctx}\n" if ctx else ""

        if blocked_no_nav:
            ban_line += (
                f"\n【重要】以下序号已在本页点击过且**页面 URL 未变化**（多为展开菜单、无导航的按钮），"
                f"**禁止**再选：{blocked_no_nav}\n"
            )
        if blocked_href_revisit:
            ban_line += (
                f"\n【重要】以下序号的链接目标为**当前页（重复打开/刷新）**或**历史上已访问过的页面**，"
                f"**禁止**再选（避免循环跳转）：{blocked_href_revisit}\n"
            )
        visited_preview = list(visited_set)[:18]
        if visited_preview:
            ban_line += f"\n已访问页面（规范化 URL 示例，勿重复打开）：{visited_preview}\n"
        # 已有摘录越短的主题越靠前，供深化阶段优先补弱
        topic_by_weak = sorted(
            topics,
            key=lambda t: len((results.get(t) or "").strip()),
        )
        if pending:
            plan_sys = (
                "你是网页浏览策略助手（ReAct 规划）。在以下可交互元素中选择**一步**操作。\n"
                "不得选择「已禁止序号」中的 element_index；若除禁止项外无更好选择，输出 null。\n"
                "用户给出的每个 topic 是一条**待回答问题**（不是关键词）。\n"
                "**优先级（必须遵守）**：\n"
                "1. **首先**选择最可能帮助「待回答问题」列表中某一问题得到**可作答的实质正文**（资格、步骤、数据、列表等）的入口；"
                "链接文案、栏目名与该问题语义越贴近越好。\n"
                "2. 多个入口都相关时，优先服务仍**完全得不到回答**的问题，其次再考虑已有部分答案但仍待补全的问题。\n"
                "3. 与待回答问题明显无关的导航/按钮（仅站点泛导航、登录等）不要选，除非列表中没有任何更优选项。\n"
                "4. 若存在「导航链接」且指向可能承载答案正文的子页，可优先于泛泛首页链接。\n"
                "5. 不得选择「禁止列表」中任一序号（含指向已访问页、当前页重复导航的链接）。\n"
                "在 reason 中简要说明该入口主要服务于哪一个待回答问题。\n"
                "只输出 JSON："
                '{"element_index": 数字或 null, "reason": "简短理由"}'
                "若当前列表中没有任何元素有助于回答问题，element_index 置为 null。"
                "（若当前不在起始 URL，系统可能在置 null 后自动退回起始页再规划，你只需诚实判断本页是否无可点入口。）"
            ) + _skill_suffix(state)
            user = (
                f"全部问题：{topics}\n"
                f"待回答问题（选择入口时必须优先围绕这些，尚未得到实质回答）：{pending}\n"
                f"{ctx_line}"
                f"{ban_line}\n"
                f"可交互元素：\n{inter_lines}\n"
            )
        else:
            preview = json.dumps(results, ensure_ascii=False)[:3500]
            plan_sys = (
                "你是网页浏览策略助手（ReAct 规划）。各问题均已有一段**实质性回答**，但系统判断当前页仍可能存在可深化的信息入口。\n"
                "不得选择「已禁止序号」中的 element_index；若除禁止项外无更好选择，输出 null。\n"
                "**优先级**：请优先选择能**补充、深化**下列「待补全优先顺序」中**靠前**问题的答案（顺序按当前已有文字从短到长，越短越优先补全）；"
                "其次再考虑泛泛的站点导航。\n"
                "若存在「导航链接」指向本站栏目/子页且与上述弱项相关，请优先考虑。\n"
                "不得选择「禁止列表」中任一序号（含指向已访问页、当前页重复导航的链接）。\n"
                "只输出 JSON：{\"element_index\": 数字或 null, \"reason\": \"简短理由\"}；若无合适入口则 element_index 为 null。"
                "（若当前不在起始 URL，系统可能在置 null 后自动退回起始页再规划。）"
            ) + _skill_suffix(state)
            user = (
                f"全部问题：{topics}\n"
                f"待补全优先顺序（已有回答越短越优先）：{topic_by_weak}\n"
                f"已有回答摘要：{preview}\n"
                f"{ctx_line}"
                f"{ban_line}\n"
                f"可交互元素：\n{inter_lines}\n"
            )
        try:
            plan = extract_with_schema(plan_sys, user, InteractionPlan)
        except Exception:
            return {"finish_reason": "规划交互失败", "planned_index": None}
        idx = plan.element_index
        reason = plan.reason or ""
        if idx is not None and idx in forbidden_indices:
            tags = []
            if idx in blocked_no_nav:
                tags.append("该序号已尝试且无页面跳转")
            if idx in blocked_href_revisit:
                tags.append("该链接指向已访问或当前页，避免循环")
            hint = "；".join(tags) if tags else "禁止的序号"
            idx = None
            reason = (reason + f" [已忽略：{hint}]").strip()
        return {
            "planned_index": idx,
            "plan_reason": reason,
            "plan_context_note": None,
        }

    def route_after_plan(
        state: CrawlerState,
    ) -> Literal["click", "done", "retreat_home"]:
        if state.get("finish_reason"):
            return "done"
        idx = state.get("planned_index")
        items = state.get("interactives") or []
        valid_click = (
            idx is not None
            and isinstance(idx, int)
            and 0 <= idx < len(items)
        )
        if valid_click:
            return "click"
        # 无有效交互：若不在起始 URL 且仍允许退回，则回到主页面再查
        start = _norm_url(state.get("url") or "")
        curr = _norm_url(session.current_url or state.get("url") or "")
        max_hr = int(state.get("max_home_retreats") or 0)
        hr = int(state.get("home_retreat_count") or 0)
        if max_hr <= 0 or hr >= max_hr or not start:
            return "done"
        if curr == start:
            return "done"
        it = int(state.get("iteration") or 0)
        max_it = int(state.get("max_iterations") or 5)
        if it >= max_it:
            return "done"
        pending = state.get("pending_topics") or []
        if pending:
            return "retreat_home"
        if state.get("explore_worthy"):
            return "retreat_home"
        return "done"

    def node_retreat_home(state: CrawlerState) -> Dict[str, Any]:
        """规划返回无有效交互时，回到起始 URL 清空本页点击禁令后重新快照。"""
        base = (state.get("url") or "").strip()
        if not base:
            return {"finish_reason": "无法退回：缺少起始 URL"}
        max_hr = int(state.get("max_home_retreats") or 0)
        hr = int(state.get("home_retreat_count") or 0) + 1
        try:
            session.goto(base)
        except Exception as e:
            return {
                "last_error": str(e),
                "finish_reason": f"退回起始页失败: {e}",
            }
        print(
            f"[AgentCrawler] 当前页无有效交互，已退回起始 URL 重试 ({hr}/{max_hr})",
            file=sys.stderr,
        )
        return {
            "home_retreat_count": hr,
            "iteration": int(state.get("iteration") or 0) + 1,
            "nav_stack": [],
            "no_nav_signatures": [],
            "planned_index": None,
            "plan_reason": None,
            "same_url_dom_changed": False,
            "dom_change_hint": None,
            "plan_context_note": (
                "系统已从子页退回**起始 URL**。请在当前（起始）页的可交互列表中重新选择入口，"
                "优先探索与待回答问题相关的、尚未尝试过的栏目链接。"
            ),
            "visited_urls": _merge_visited(state, session.current_url or base),
        }

    def node_click(state: CrawlerState) -> Dict[str, Any]:
        idx = state.get("planned_index")
        items = state.get("interactives") or []
        if idx is None or not isinstance(idx, int):
            return {"finish_reason": "无有效交互计划"}
        before = _norm_url(session.current_url or "")
        pre_sigs = {_click_signature(it) for it in items}
        try:
            session.click_index(items, idx)
        except Exception as e:
            return {
                "iteration": int(state.get("iteration") or 0) + 1,
                "last_error": str(e),
            }
        # 下拉/抽屉等动画后再采一次 DOM，便于对比可交互集合是否变化
        try:
            if session.page:
                session.page.wait_for_timeout(480)
        except Exception:
            pass
        post_items: List[Dict[str, Any]] = []
        try:
            post_items = session.collect_interactives()
        except Exception:
            post_items = list(items)
        post_sigs = {_click_signature(it) for it in post_items}
        after = _norm_url(session.current_url or "")
        nav_sigs = list(state.get("no_nav_signatures") or [])
        out: Dict[str, Any] = {
            "iteration": int(state.get("iteration") or 0) + 1,
            "visited_urls": _merge_visited(state, session.current_url or ""),
            "same_url_dom_changed": False,
            "dom_change_hint": None,
        }
        if after == before:
            if post_sigs != pre_sigs:
                added = post_sigs - pre_sigs
                new_items = [it for it in post_items if _click_signature(it) in added]
                hint_body = session.interactives_as_text(new_items[:30])
                out["same_url_dom_changed"] = True
                out["dom_change_hint"] = (
                    f"点击后 URL 未变；可交互签名集合变化（新增约 {len(added)} 项）。"
                    f"新增项列表：\n{hint_body[:2200]}"
                )
                out["no_nav_signatures"] = nav_sigs
            else:
                sig = _click_signature(items[idx])
                if sig not in nav_sigs:
                    nav_sigs.append(sig)
                out["no_nav_signatures"] = nav_sigs
        else:
            out["no_nav_signatures"] = []
        return out

    g = StateGraph(CrawlerState)
    g.add_node("load", node_load)
    g.add_node("snapshot", node_snapshot)
    g.add_node("extract", node_extract)
    g.add_node("nav_dfs", node_nav_dfs)
    g.add_node("search", node_search)
    g.add_node("plan", node_plan)
    g.add_node("retreat_home", node_retreat_home)
    g.add_node("click", node_click)

    g.add_edge(START, "load")
    g.add_edge("load", "snapshot")
    g.add_edge("snapshot", "extract")
    g.add_edge("extract", "nav_dfs")
    g.add_conditional_edges(
        "nav_dfs",
        route_after_nav,
        {"snapshot": "snapshot", "plan": "plan", "search": "search", "end": END},
    )
    g.add_conditional_edges(
        "search",
        lambda state: state.get("nav_route", "fallthrough"),
        {"snapshot": "snapshot", "fallthrough": "plan"},
    )
    g.add_conditional_edges(
        "plan",
        route_after_plan,
        {"click": "click", "done": END, "retreat_home": "retreat_home"},
    )
    g.add_edge("retreat_home", "snapshot")
    g.add_edge("click", "snapshot")

    return g.compile()


def run_crawl(
    url: str,
    topics: List[str],
    *,
    headless: bool = True,
    max_iterations: int = 6,
    max_home_retreats: int = 3,
    refine_results: bool = True,
    skill_context: Optional[str] = None,
    enable_search: bool = False,
    search_threshold: float = 0.5,
) -> CrawlerState:
    """同步运行爬虫图，返回最终状态。refine_results 为 True 时对合并后的 results 再经模型精炼去重。
    skill_context 为本地 Skill 正文时，会注入各步 LLM 系统提示与精炼阶段。
    max_home_retreats：规划判定无有效交互时，允许从子页退回起始 URL 再规划的次数；0 表示关闭。
    enable_search：是否启用搜索功能。
    search_threshold：搜索阈值（0-1），值越大越倾向于搜索。"""
    session = BrowserSession(headless=headless)
    graph = build_graph(session, enable_search=enable_search, search_threshold=search_threshold)
    init: CrawlerState = {
        "url": url,
        "topics": list(topics),
        "results": {},
        "page_text": "",
        "interactives": [],
        "interactive_text": "",
        "pending_topics": list(topics),
        "iteration": 0,
        "max_iterations": max_iterations,
        "max_home_retreats": max(0, int(max_home_retreats)),
        "home_retreat_count": 0,
        "browser_ready": False,
        "last_error": None,
        "finish_reason": None,
        "planned_index": None,
        "plan_reason": None,
        "explore_worthy": None,
        "nav_stack": [],
        "nav_route": None,
        "visited_urls": [_norm_url(url)],
        "snapshot_page_norm": None,
        "no_nav_signatures": [],
        "same_url_dom_changed": False,
        "dom_change_hint": None,
        "plan_context_note": None,
        "skill_context": (skill_context or "").strip() or None,
    }
    try:
        get_chat_model()
        out = graph.invoke(init)
    finally:
        session.stop()
    s = finalize_state(out)  # type: ignore[arg-type]
    if refine_results:
        tlist = list(s.get("topics") or topics)
        prev = dict(s.get("results") or {})
        if tlist and any((prev.get(x) or "").strip() for x in tlist):
            try:
                refined = refine_merged_results(
                    tlist,
                    prev,
                    skill_context=s.get("skill_context"),
                )
                s["results"] = refined
                if refined != prev:
                    print("[AgentCrawler] 已对合并结果做精炼整合", file=sys.stderr)
            except Exception:
                pass
    return s


def finalize_state(state: CrawlerState) -> CrawlerState:
    """根据路由提前结束时补充 finish_reason。"""
    s = dict(state)
    if s.get("finish_reason"):
        return s  # type: ignore[return-value]
    max_it = int(s.get("max_iterations") or 0)
    it = int(s.get("iteration") or 0)
    if it >= max_it:
        s["finish_reason"] = "达到最大交互轮次，停止"
        return s  # type: ignore[return-value]
    pending = s.get("pending_topics") or []
    if not pending:
        ew = s.get("explore_worthy")
        if ew is False:
            s["finish_reason"] = "全部问题均已得到累积回答，且当前页无值得继续探索的入口"
        elif ew is True:
            s["finish_reason"] = "全部问题均已得到累积回答；本轮未执行新的有效点击（或规划未选入口）"
        else:
            s["finish_reason"] = "全部问题均已得到累积回答"
        return s  # type: ignore[return-value]
    if not (s.get("interactives") or []):
        s["finish_reason"] = "仍有问题未回答但页面无可交互组件"
        return s  # type: ignore[return-value]
    s["finish_reason"] = "规划阶段未选出有效交互"
    return s  # type: ignore[return-value]
