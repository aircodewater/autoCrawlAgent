from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from crawler.browser_session import BrowserSession
from crawler.search_agent import SearchAgent, create_search_agent
from crawler.search_utils import extract_domain, is_valid_url


class SearchDrivenCrawler:
    """基于搜索驱动的爬虫：将搜索结果与现有爬虫集成"""
    
    def __init__(
        self,
        browser_session: BrowserSession,
        search_agent: Optional[SearchAgent] = None,
        enable_search: bool = True,
        max_search_results: int = 10,
    ):
        """
        初始化搜索驱动爬虫
        
        Args:
            browser_session: 浏览器会话实例
            search_agent: 搜索 Agent 实例（可选，默认自动创建）
            enable_search: 是否启用搜索功能
            max_search_results: 每个主题的最大搜索结果数
        """
        self.browser_session = browser_session
        self.search_agent = search_agent or create_search_agent()
        self.enable_search = enable_search
        self.max_search_results = max_search_results
        self.search_cache: Dict[str, List[Dict[str, Any]]] = {}
        self.visited_urls: set = set()
    
    def should_search(
        self,
        topic: str,
        current_results: Dict[str, str],
        pending_topics: List[str],
        iteration: int,
        max_iterations: int,
    ) -> bool:
        """
        判断是否需要执行搜索
        
        Args:
            topic: 当前主题
            current_results: 当前累积的结果
            pending_topics: 待处理的主题列表
            iteration: 当前迭代次数
            max_iterations: 最大迭代次数
        
        Returns:
            是否需要搜索
        """
        if not self.enable_search:
            return False
        
        # 如果还有待处理的主题，可能需要搜索
        if pending_topics:
            # 在迭代早期更倾向于搜索
            if iteration < max_iterations // 2:
                return True
        
        # 如果当前主题的结果为空或很短，可能需要搜索
        topic_result = current_results.get(topic, "")
        if not topic_result or len(topic_result) < 100:
            return True
        
        return False
    
    def search_and_crawl(
        self,
        topic: str,
        base_url: str,
        current_results: Dict[str, str],
        pending_topics: List[str],
        iteration: int,
        max_iterations: int,
    ) -> Dict[str, Any]:
        """
        执行搜索并爬取结果
        
        Args:
            topic: 待搜索的主题
            base_url: 基础 URL
            current_results: 当前累积的结果
            pending_topics: 待处理的主题列表
            iteration: 当前迭代次数
            max_iterations: 最大迭代次数
        
        Returns:
            包含搜索结果和爬取状态的字典
        """
        if not self.should_search(topic, current_results, pending_topics, iteration, max_iterations):
            return {
                "searched": False,
                "urls_added": [],
                "message": "不需要搜索",
            }
        
        # 检查缓存
        cache_key = f"{topic}_{base_url}"
        if cache_key in self.search_cache:
            print(f"[SearchCrawler] 使用缓存的搜索结果: {topic}", file=sys.stderr)
            search_results = self.search_cache[cache_key]
        else:
            # 执行搜索
            print(f"[SearchCrawler] 开始搜索: {topic}", file=sys.stderr)
            search_results = self.search_agent.search_by_topic(
                topic=topic,
                base_url=base_url,
                max_results=self.max_search_results,
            )
            self.search_cache[cache_key] = search_results
        
        # 提取 URL
        urls = self.search_agent.extract_urls(search_results)
        
        # 过滤已访问的 URL
        new_urls = [url for url in urls if url not in self.visited_urls]
        
        # 更新已访问集合
        self.visited_urls.update(new_urls)
        
        # 返回结果
        return {
            "searched": True,
            "urls_added": new_urls,
            "search_results": search_results,
            "message": f"找到 {len(new_urls)} 个新 URL",
        }
    
    def crawl_search_results(
        self,
        urls: List[str],
        topics: List[str],
        current_results: Dict[str, str],
        max_pages: int = 5,
    ) -> Dict[str, Any]:
        """
        爬取搜索结果中的页面
        
        Args:
            urls: 要爬取的 URL 列表
            topics: 待回答的主题列表
            current_results: 当前累积的结果
            max_pages: 最大爬取页面数
        
        Returns:
            包含爬取结果的字典
        """
        if not urls:
            return {
                "crawled": False,
                "pages_crawled": 0,
                "results": current_results,
                "message": "没有 URL 可爬取",
            }
        
        print(f"[SearchCrawler] 开始爬取 {len(urls)} 个搜索结果（最多 {max_pages} 个）", file=sys.stderr)
        
        # 限制爬取数量
        urls_to_crawl = urls[:max_pages]
        
        # 爬取每个页面
        for url in urls_to_crawl:
            try:
                # 导航到页面
                self.browser_session.goto(url)
                
                # 展开折叠内容
                self.browser_session.expand_collapsed_content()
                
                # 获取页面文本
                page_text = self.browser_session.visible_text()
                
                # 这里可以调用抽取逻辑来提取信息
                # 由于需要与 graph.py 中的抽取逻辑集成，
                # 这里只返回页面文本，由调用方处理抽取
                
                print(f"[SearchCrawler] 已爬取: {url}", file=sys.stderr)
                
            except Exception as e:
                print(f"[SearchCrawler] 爬取失败 {url}: {e}", file=sys.stderr)
                continue
        
        return {
            "crawled": True,
            "pages_crawled": len(urls_to_crawl),
            "results": current_results,
            "message": f"已爬取 {len(urls_to_crawl)} 个页面",
        }
    
    def get_search_summary(self, topic: str) -> str:
        """
        获取指定主题的搜索结果摘要
        
        Args:
            topic: 主题
        
        Returns:
            搜索结果摘要
        """
        # 查找所有相关的缓存结果
        summaries = []
        for cache_key, results in self.search_cache.items():
            if topic in cache_key:
                summary = self.search_agent.get_search_summary(results)
                summaries.append(f"=== 查询: {cache_key} ===\n{summary}")
        
        if not summaries:
            return "无搜索结果"
        
        return "\n\n".join(summaries)
    
    def clear_cache(self):
        """清空搜索缓存"""
        self.search_cache.clear()
        print("[SearchCrawler] 搜索缓存已清空", file=sys.stderr)
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        获取搜索统计信息
        
        Returns:
            统计信息字典
        """
        total_searches = len(self.search_cache)
        total_results = sum(len(results) for results in self.search_cache.values())
        total_urls_visited = len(self.visited_urls)
        
        return {
            "total_searches": total_searches,
            "total_results": total_results,
            "total_urls_visited": total_urls_visited,
            "cache_size": len(self.search_cache),
        }


class SearchDecisionMaker:
    """搜索决策器：决定何时以及如何进行搜索"""
    
    def __init__(
        self,
        enable_auto_search: bool = True,
        search_threshold: float = 0.7,
        min_page_results: int = 3,
        min_iteration_for_search: int = 2,
    ):
        """
        初始化搜索决策器
        
        Args:
            enable_auto_search: 是否启用自动搜索
            search_threshold: 搜索阈值（0-1），值越大越倾向于搜索
            min_page_results: 当前页面最少需要提取到多少结果才考虑搜索
            min_iteration_for_search: 最小迭代次数才考虑搜索
        """
        self.enable_auto_search = enable_auto_search
        self.search_threshold = search_threshold
        self.min_page_results = min_page_results
        self.min_iteration_for_search = min_iteration_for_search
    
    def decide_search(
        self,
        topics: List[str],
        current_results: Dict[str, str],
        pending_topics: List[str],
        iteration: int,
        max_iterations: int,
        base_url: str = "",
        current_page_results: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        决定是否需要搜索
        
        Args:
            topics: 所有主题列表
            current_results: 当前累积的结果
            pending_topics: 待处理的主题列表
            iteration: 当前迭代次数
            max_iterations: 最大迭代次数
            base_url: 基础 URL
            current_page_results: 当前页面提取到的结果
        
        Returns:
            决策结果字典
        """
        if not self.enable_auto_search:
            return {
                "should_search": False,
                "reason": "自动搜索已禁用",
                "topics_to_search": [],
            }
        
        # 检查1：迭代次数是否足够
        if iteration < self.min_iteration_for_search:
            return {
                "should_search": False,
                "reason": f"迭代次数不足（当前: {iteration}, 最小: {self.min_iteration_for_search}）",
                "topics_to_search": [],
            }
        
        # 检查2：当前页面是否已充分爬取
        current_page_results = current_page_results or {}
        current_page_completed = sum(1 for v in current_page_results.values() if v)
        if current_page_completed < self.min_page_results:
            return {
                "should_search": False,
                "reason": f"当前页面提取结果不足（当前: {current_page_completed}, 最小: {self.min_page_results}）",
                "topics_to_search": [],
            }
        
        # 检查3：是否还有待处理的主题
        if not pending_topics:
            return {
                "should_search": False,
                "reason": "没有待处理的主题",
                "topics_to_search": [],
            }
        
        # 计算完成度
        completed = sum(1 for t in topics if current_results.get(t, ""))
        completion_rate = completed / len(topics) if topics else 0
        
        # 计算搜索必要性分数（调整权重）
        search_score = 0.0
        
        # 1. 待处理主题比例（权重最高）
        pending_ratio = len(pending_topics) / len(topics) if topics else 0
        search_score += pending_ratio * 0.6
        
        # 2. 迭代进度（后期更倾向搜索，因为需要补充缺失信息）
        iteration_ratio = iteration / max_iterations if max_iterations > 0 else 0
        search_score += iteration_ratio * 0.2
        
        # 3. 完成度
        search_score += (1 - completion_rate) * 0.2
        
        # 决定是否搜索
        should_search = search_score >= self.search_threshold
        
        # 选择要搜索的主题（优先选择未找到的主题）
        topics_to_search = []
        if should_search:
            # 只选择完全未找到的主题进行搜索
            unfound_topics = [t for t in pending_topics if not current_results.get(t, "")]
            topics_to_search = unfound_topics[:2]  # 每次最多搜索2个主题
        
        return {
            "should_search": should_search,
            "reason": f"搜索分数: {search_score:.2f} (阈值: {self.search_threshold}), 页面完成: {current_page_completed}, 未找到: {len(topics_to_search)}",
            "topics_to_search": topics_to_search,
            "search_score": search_score,
        }
    
    def decide_search_strategy(
        self,
        topic: str,
        base_url: str,
        has_internal_links: bool = True,
    ) -> Dict[str, Any]:
        """
        决定搜索策略
        
        Args:
            topic: 主题
            base_url: 基础 URL
            has_internal_links: 是否有内部链接
        
        Returns:
            搜索策略字典
        """
        strategies = []
        
        # 策略 1: 站内搜索
        if base_url:
            strategies.append({
                "type": "site_search",
                "query": f"site:{extract_domain(base_url)} {topic}",
                "priority": 1,
            })
        
        # 策略 2: 通用搜索
        strategies.append({
            "type": "general_search",
            "query": topic,
            "priority": 2,
        })
        
        # 策略 3: 关键词搜索
        from crawler.search_utils import extract_keywords
        keywords = extract_keywords(topic)
        if keywords:
            strategies.append({
                "type": "keyword_search",
                "query": " ".join(keywords),
                "priority": 3,
            })
        
        # 按优先级排序
        strategies.sort(key=lambda x: x["priority"])
        
        return {
            "strategies": strategies,
            "recommended_strategy": strategies[0] if strategies else None,
        }


def create_search_driven_crawler(
    browser_session: BrowserSession,
    search_agent: Optional[SearchAgent] = None,
    enable_search: bool = True,
    max_search_results: int = 10,
) -> SearchDrivenCrawler:
    """
    创建搜索驱动爬虫的工厂函数
    
    Args:
        browser_session: 浏览器会话实例
        search_agent: 搜索 Agent 实例（可选）
        enable_search: 是否启用搜索功能
        max_search_results: 每个主题的最大搜索结果数
    
    Returns:
        SearchDrivenCrawler 实例
    """
    return SearchDrivenCrawler(
        browser_session=browser_session,
        search_agent=search_agent,
        enable_search=enable_search,
        max_search_results=max_search_results,
    )


def create_search_decision_maker(
    enable_auto_search: bool = True,
    search_threshold: float = 0.7,
    min_page_results: int = 3,
    min_iteration_for_search: int = 2,
) -> SearchDecisionMaker:
    """
    创建搜索决策器的工厂函数
    
    Args:
        enable_auto_search: 是否启用自动搜索
        search_threshold: 搜索阈值
        min_page_results: 当前页面最少需要提取到多少结果才考虑搜索
        min_iteration_for_search: 最小迭代次数才考虑搜索
    
    Returns:
        SearchDecisionMaker 实例
    """
    return SearchDecisionMaker(
        enable_auto_search=enable_auto_search,
        search_threshold=search_threshold,
        min_page_results=min_page_results,
        min_iteration_for_search=min_iteration_for_search,
    )
