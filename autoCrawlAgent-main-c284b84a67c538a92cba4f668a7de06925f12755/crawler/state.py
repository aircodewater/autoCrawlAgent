from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class CrawlerState(TypedDict, total=False):
    """图状态：URL、待回答问题列表、累积答案与页面上下文。"""

    url: str
    topics: List[str]
    # 每个问题（topic 字符串）已融合的答案正文（可能多轮累积）
    results: Dict[str, str]
    # 当前页可见文本（截断后）
    page_text: str
    # 可交互元素摘要，供模型选择
    interactives: List[Dict[str, Any]]
    interactive_text: str
    # 模型判断尚未得到实质回答的问题
    pending_topics: List[str]
    iteration: int
    max_iterations: int
    # 浏览器单例句柄在图外持有，此处仅标记
    browser_ready: bool
    last_error: Optional[str]
    # 任务结束原因
    finish_reason: Optional[str]
    # 规划交互临时字段
    planned_index: Optional[int]
    plan_reason: Optional[str]
    # 各问题已有一定答案时，是否仍值得点击入口做深化（由模型判断）
    explore_worthy: Optional[bool]
    # 多导航递归：每层缓存父页 URL 与待遍历的子导航（深度优先）
    nav_stack: List[Dict[str, Any]]
    # nav_dfs 节点产生的路由：snapshot | fallthrough | done
    nav_route: Optional[str]
    # 已访问页面 URL 规范化字符串，避免 DFS/规划重复打开同一页
    visited_urls: List[str]
    # 上次快照时的页面 URL（规范化），用于检测是否换页
    snapshot_page_norm: Optional[str]
    # 在当前页已点击但 URL 未变的控件签名，规划时禁止再选同一控件
    no_nav_signatures: List[str]
    # 点击后 URL 未变但 DOM 可交互集合发生变化（如展开菜单）
    same_url_dom_changed: Optional[bool]
    # 给规划/模型的简短说明：出现了哪些新入口（由 click 写入，extract 消费）
    dom_change_hint: Optional[str]
    # extract 生成、plan 使用一次后清除：提示本轮优先点的交互
    plan_context_note: Optional[str]
    # 本地 SKILL.md 拼接正文，注入各 LLM 系统提示（可选）
    skill_context: Optional[str]
    # 规划判定无可用交互时退回起始 URL 的次数上限与计数
    max_home_retreats: int
    home_retreat_count: int
