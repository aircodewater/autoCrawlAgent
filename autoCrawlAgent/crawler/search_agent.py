from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

import httpx

from crawler.search_utils import (
    deduplicate_urls,
    extract_domain,
    filter_search_results,
    generate_search_queries,
    is_valid_url,
    rank_search_results,
)


class SearchEngine:
    """搜索引擎基类"""
    
    def search(self, query: str, num_results: int = 10) -> List[Dict[str, Any]]:
        """
        执行搜索
        
        Args:
            query: 搜索查询
            num_results: 返回结果数量
        
        Returns:
            搜索结果列表，每个结果包含 url, title, snippet 等字段
        """
        raise NotImplementedError


class GoogleSearchEngine(SearchEngine):
    """Google Custom Search API 实现"""
    
    def __init__(self, api_key: str, search_engine_id: str):
        self.api_key = api_key
        self.search_engine_id = search_engine_id
        self.base_url = "https://www.googleapis.com/customsearch/v1"
    
    def search(self, query: str, num_results: int = 10) -> List[Dict[str, Any]]:
        """
        使用 Google Custom Search API 执行搜索
        
        Args:
            query: 搜索查询
            num_results: 返回结果数量（最多 10）
        
        Returns:
            搜索结果列表
        """
        if not self.api_key or not self.search_engine_id:
            print("[SearchAgent] Google Search API 未配置，跳过搜索", file=sys.stderr)
            return []
        
        try:
            params = {
                "key": self.api_key,
                "cx": self.search_engine_id,
                "q": query,
                "num": min(num_results, 10),
            }
            
            with httpx.Client(timeout=30.0) as client:
                response = client.get(self.base_url, params=params)
                response.raise_for_status()
                data = response.json()
            
            results = []
            items = data.get("items", [])
            
            for item in items:
                results.append({
                    "url": item.get("link", ""),
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                })
            
            return results
            
        except Exception as e:
            print(f"[SearchAgent] Google Search 失败: {e}", file=sys.stderr)
            return []


class BingSearchEngine(SearchEngine):
    """Bing Search API 实现"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.bing.microsoft.com/v7.0/search"
    
    def search(self, query: str, num_results: int = 10) -> List[Dict[str, Any]]:
        """
        使用 Bing Search API 执行搜索
        
        Args:
            query: 搜索查询
            num_results: 返回结果数量
        
        Returns:
            搜索结果列表
        """
        if not self.api_key:
            print("[SearchAgent] Bing Search API 未配置，跳过搜索", file=sys.stderr)
            return []
        
        try:
            headers = {
                "Ocp-Apim-Subscription-Key": self.api_key,
            }
            params = {
                "q": query,
                "count": min(num_results, 10),
            }
            
            with httpx.Client(timeout=30.0) as client:
                response = client.get(self.base_url, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()
            
            results = []
            items = data.get("webPages", {}).get("value", [])
            
            for item in items:
                results.append({
                    "url": item.get("url", ""),
                    "title": item.get("name", ""),
                    "snippet": item.get("snippet", ""),
                })
            
            return results
            
        except Exception as e:
            print(f"[SearchAgent] Bing Search 失败: {e}", file=sys.stderr)
            return []


class DuckDuckGoSearchEngine(SearchEngine):
    """DuckDuckGo 搜索引擎实现（无需 API 密钥）"""
    
    def __init__(self):
        self.base_url = "https://duckduckgo.com/html/"
    
    def search(self, query: str, num_results: int = 10) -> List[Dict[str, Any]]:
        """
        使用 DuckDuckGo 执行搜索（免费，无需 API 密钥）
        
        Args:
            query: 搜索查询
            num_results: 返回结果数量
        
        Returns:
            搜索结果列表
        """
        try:
            params = {
                "q": query,
            }
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            
            with httpx.Client(timeout=30.0) as client:
                response = client.get(self.base_url, params=params, headers=headers)
                response.raise_for_status()
                html = response.text
            
            # 简单解析 HTML（生产环境建议使用 BeautifulSoup）
            results = []
            
            # 提取搜索结果的简单正则表达式
            import re
            pattern = r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>'
            
            matches = re.findall(pattern, html)
            
            for url, title in matches[:num_results]:
                # DuckDuckGo 返回的 URL 可能是重定向链接，需要解码
                if url.startswith("/l/?uddg="):
                    try:
                        from urllib.parse import unquote
                        url = unquote(url.split("uddg=")[1].split("&")[0])
                    except Exception:
                        pass
                
                if is_valid_url(url):
                    results.append({
                        "url": url,
                        "title": title,
                        "snippet": "",
                    })
            
            return results
            
        except Exception as e:
            print(f"[SearchAgent] DuckDuckGo Search 失败: {e}", file=sys.stderr)
            return []


class SearchAgent:
    """搜索 Agent：统一接口的搜索引擎客户端"""
    
    def __init__(
        self,
        engine: Optional[str] = None,
        api_key: Optional[str] = None,
        search_engine_id: Optional[str] = None,
    ):
        """
        初始化搜索 Agent
        
        Args:
            engine: 搜索引擎类型（google, bing, duckduckgo）
            api_key: API 密钥（Google 或 Bing 需要）
            search_engine_id: Google Custom Search Engine ID（仅 Google 需要）
        """
        self.engine = engine or self._detect_engine()
        self.api_key = api_key
        self.search_engine_id = search_engine_id
        self._search_engine = self._create_search_engine()
    
    def _detect_engine(self) -> str:
        """自动检测可用的搜索引擎"""
        # 优先级：环境变量 > Google > Bing > DuckDuckGo
        env_engine = os.environ.get("SEARCH_ENGINE", "").lower()
        if env_engine in ("google", "bing", "duckduckgo"):
            return env_engine
        if os.environ.get("GOOGLE_API_KEY") and os.environ.get("GOOGLE_SEARCH_ENGINE_ID"):
            return "google"
        elif os.environ.get("BING_API_KEY"):
            return "bing"
        else:
            return "duckduckgo"
    
    def _create_search_engine(self) -> SearchEngine:
        """创建搜索引擎实例"""
        if self.engine == "google":
            api_key = self.api_key or os.environ.get("GOOGLE_API_KEY", "")
            search_engine_id = self.search_engine_id or os.environ.get("GOOGLE_SEARCH_ENGINE_ID", "")
            return GoogleSearchEngine(api_key, search_engine_id)
        elif self.engine == "bing":
            api_key = self.api_key or os.environ.get("BING_API_KEY", "")
            return BingSearchEngine(api_key)
        else:
            return DuckDuckGoSearchEngine()
    
    def search_by_topic(
        self,
        topic: str,
        base_url: str = "",
        max_queries: int = 3,
        max_results: int = 10,
        filter_results: bool = True,
        university: str = "",
        program: str = "",
    ) -> List[Dict[str, Any]]:
        """
        根据主题执行搜索
        
        Args:
            topic: 待回答的问题/主题
            base_url: 基础网站 URL，用于限定搜索范围
            max_queries: 最大查询数量
            max_results: 每个查询的最大结果数
            filter_results: 是否过滤结果
            university: 院校名称
            program: 专业名称
        
        Returns:
            搜索结果列表
        """
        # 生成搜索查询
        queries = generate_search_queries(topic, base_url, max_queries, university, program)
        
        print(f"[SearchAgent] 为主题生成 {len(queries)} 个搜索查询: {queries}", file=sys.stderr)
        
        # 执行所有查询
        all_results = []
        for query in queries:
            results = self._search_engine.search(query, max_results)
            all_results.append(results)
        
        # 合并结果
        merged = self._merge_results(all_results)
        
        # 过滤结果
        if filter_results:
            base_domain = extract_domain(base_url) if base_url else ""
            merged = filter_search_results(merged, base_domain)
        
        # 排序结果
        merged = rank_search_results(merged, topic)
        
        # 去重
        merged_urls = deduplicate_urls([r.get("url", "") for r in merged])
        url_set = set(merged_urls)
        merged = [r for r in merged if r.get("url", "") in url_set]
        
        print(f"[SearchAgent] 搜索完成，返回 {len(merged)} 个结果", file=sys.stderr)
        
        return merged
    
    def _merge_results(self, all_results: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """合并多个搜索结果列表"""
        seen_urls = set()
        merged = []
        
        for results in all_results:
            for result in results:
                url = result.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged.append(result)
        
        return merged
    
    def extract_urls(self, results: List[Dict[str, Any]]) -> List[str]:
        """
        从搜索结果中提取 URL 列表
        
        Args:
            results: 搜索结果列表
        
        Returns:
            URL 列表
        """
        return [r.get("url", "") for r in results if is_valid_url(r.get("url", ""))]
    
    def get_search_summary(self, results: List[Dict[str, Any]]) -> str:
        """
        获取搜索结果的摘要文本
        
        Args:
            results: 搜索结果列表
        
        Returns:
            摘要文本
        """
        if not results:
            return "无搜索结果"
        
        lines = []
        for i, result in enumerate(results[:10], 1):
            title = result.get("title", "")
            url = result.get("url", "")
            snippet = result.get("snippet", "")
            
            lines.append(f"{i}. {title}")
            lines.append(f"   URL: {url}")
            if snippet:
                lines.append(f"   摘要: {snippet}")
            lines.append("")
        
        return "\n".join(lines)


def create_search_agent(
    engine: Optional[str] = None,
    api_key: Optional[str] = None,
    search_engine_id: Optional[str] = None,
) -> SearchAgent:
    """
    创建搜索 Agent 的工厂函数
    
    Args:
        engine: 搜索引擎类型（google, bing, duckduckgo）
        api_key: API 密钥
        search_engine_id: Google Custom Search Engine ID
    
    Returns:
        SearchAgent 实例
    """
    return SearchAgent(engine, api_key, search_engine_id)
