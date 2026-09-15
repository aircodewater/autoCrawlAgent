from __future__ import annotations

import re
from typing import List, Optional, Set
from urllib.parse import urlparse


def infer_school_and_program(
    start_url: str,
    results: Optional[dict] = None,
) -> tuple[str, str]:
    """
    推断搜索用的学校名、专业名：优先 results 里已爬到的「院校名称」「专业名称」，
    否则从 URL 路径等启发式补全。
    """
    results = results or {}
    university = (
        (results.get("院校名称") or results.get("学校名称") or results.get("大学名称") or "")
        .strip()
    )
    program = (results.get("专业名称") or results.get("专业") or "").strip()

    domain = extract_domain(start_url)
    if not university:
        if "utoronto" in domain:
            university = "多伦多大学"
        elif "mcgill" in domain:
            university = "麦吉尔大学"
        elif domain:
            university = domain.split(".")[0].replace("-", " ").title()

    if not program:
        try:
            path = urlparse(start_url).path or ""
        except Exception:
            path = ""
        m = re.search(r"/program[s]?/([^/#?]+)", path, re.I)
        if m:
            slug = m.group(1).replace("-", " ").replace("_", " ")
            program = slug.strip()

    return university, program


def pick_best_search_url(urls: List[str], base_url: str = "") -> str:
    """从候选 URL 中选取最可能有用的一条（同域优先）。"""
    if not urls:
        return ""
    base_domain = extract_domain(base_url) if base_url else ""
    scored: List[tuple[str, int]] = []
    for url in urls:
        score = 0
        if extract_domain(url) == base_domain and base_domain:
            score += 10
        if 20 <= len(url) <= 150:
            score += 5
        for kw in (
            "admission",
            "requirement",
            "program",
            "course",
            "apply",
            "degree",
            "tuition",
            "scholarship",
        ):
            if kw in url.lower():
                score += 2
        scored.append((url, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0][0]


def generate_search_queries(topic: str, base_url: str = "", max_queries: int = 1, 
                          university: str = "", program: str = "") -> List[str]:
    """
    根据主题生成搜索查询（简化为单一精准查询）
    
    Args:
        topic: 待回答的问题/主题
        base_url: 基础网站 URL，用于限定搜索范围
        max_queries: 最大查询数量（默认1，简化为单一查询）
        university: 院校名称
        program: 专业名称
    
    Returns:
        搜索查询列表（通常只有1个）
    """
    # 如果有院校和专业信息，生成精准组合查询
    if university and program:
        # 格式：院校 专业 主题
        query = f"{university} {program} {topic}"
    elif university:
        # 只有院校
        query = f"{university} {topic}"
    else:
        # 没有院校信息，直接使用主题
        query = topic
    
    return [query] if query else []


def extract_keywords(text: str) -> List[str]:
    """
    从文本中提取关键词
    
    Args:
        text: 输入文本
    
    Returns:
        关键词列表
    """
    # 移除常见停用词
    stop_words = {
        "的", "是", "在", "和", "有", "我", "你", "他", "她", "它",
        "这", "那", "什么", "怎么", "如何", "为什么", "哪个", "哪些",
        "是否", "需要", "要求", "描述", "说明", "包括", "包含", "关于"
    }
    
    # 分词（简单按空格和标点分割）
    words = re.findall(r'[\w\u4e00-\u9fff]+', text)
    
    # 过滤停用词和短词
    keywords = [w for w in words if w not in stop_words and len(w) > 1]
    
    return keywords


def extract_domain(url: str) -> str:
    """
    从 URL 中提取域名
    
    Args:
        url: 完整 URL
    
    Returns:
        域名
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        # 移除 www. 前缀
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def normalize_url(url: str) -> str:
    """
    规范化 URL，用于去重
    
    Args:
        url: 原始 URL
    
    Returns:
        规范化后的 URL
    """
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "https").lower()
        netloc = (parsed.netloc or "").lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        path = path or "/"
        return f"{scheme}://{netloc}{path}".lower()
    except Exception:
        return (url or "").split("#")[0].lower().rstrip("/")


def deduplicate_urls(urls: List[str]) -> List[str]:
    """
    去重 URL 列表
    
    Args:
        urls: URL 列表
    
    Returns:
        去重后的 URL 列表
    """
    seen: Set[str] = set()
    unique_urls = []
    
    for url in urls:
        norm_url = normalize_url(url)
        if norm_url and norm_url not in seen:
            seen.add(norm_url)
            unique_urls.append(url)
    
    return unique_urls


def filter_search_results(
    results: List[dict],
    base_domain: str = "",
    exclude_patterns: Optional[List[str]] = None
) -> List[dict]:
    """
    过滤搜索结果
    
    Args:
        results: 搜索结果列表，每个结果包含 url, title, snippet 等字段
        base_domain: 基础域名，用于优先保留站内结果
        exclude_patterns: 要排除的 URL 模式列表
    
    Returns:
        过滤后的搜索结果
    """
    if not results:
        return []
    
    exclude_patterns = exclude_patterns or []
    
    # 默认排除模式
    default_exclude = [
        r'login', r'sign[\s-]?in', r'register', r'logout',
        r'cart', r'checkout', r'javascript:', r'mailto:',
        r'登录', r'注册', r'登出', r'购物车'
    ]
    all_exclude = default_exclude + exclude_patterns
    
    filtered = []
    
    for result in results:
        url = result.get('url', '')
        title = result.get('title', '')
        snippet = result.get('snippet', '')
        
        # 检查是否匹配排除模式
        should_exclude = False
        for pattern in all_exclude:
            if re.search(pattern, url, re.I) or re.search(pattern, title, re.I):
                should_exclude = True
                break
        
        if should_exclude:
            continue
        
        # 如果指定了基础域名，优先保留站内结果
        if base_domain:
            result_domain = extract_domain(url)
            result['is_internal'] = (result_domain == base_domain)
        else:
            result['is_internal'] = False
        
        filtered.append(result)
    
    # 排序：站内结果优先，然后按标题相关性排序
    filtered.sort(key=lambda x: (
        not x.get('is_internal', False),
        len(x.get('title', ''))
    ))
    
    return filtered


def rank_search_results(results: List[dict], query: str = "") -> List[dict]:
    """
    对搜索结果进行排序
    
    Args:
        results: 搜索结果列表
        query: 原始查询，用于计算相关性
    
    Returns:
        排序后的搜索结果
    """
    if not results:
        return []
    
    def calculate_relevance_score(result: dict) -> float:
        score = 0.0
        title = result.get('title', '').lower()
        snippet = result.get('snippet', '').lower()
        url = result.get('url', '').lower()
        
        # 站内结果加分
        if result.get('is_internal', False):
            score += 2.0
        
        # 如果有查询，计算关键词匹配
        if query:
            query_lower = query.lower()
            query_words = set(re.findall(r'\w+', query_lower))
            
            # 标题匹配加分
            for word in query_words:
                if word in title:
                    score += 1.0
            
            # 摘要匹配加分
            for word in query_words:
                if word in snippet:
                    score += 0.5
        
        # URL 简洁度加分（避免过长的 URL）
        if len(url) < 100:
            score += 0.3
        
        return score
    
    ranked = sorted(results, key=calculate_relevance_score, reverse=True)
    return ranked


def merge_search_results(
    all_results: List[List[dict]],
    max_results: int = 20
) -> List[dict]:
    """
    合并多个搜索结果列表并去重
    
    Args:
        all_results: 多个搜索结果列表
        max_results: 最大返回结果数
    
    Returns:
        合并后的搜索结果
    """
    seen_urls: Set[str] = set()
    merged = []
    
    for results in all_results:
        for result in results:
            url = result.get('url', '')
            norm_url = normalize_url(url)
            
            if norm_url and norm_url not in seen_urls:
                seen_urls.add(norm_url)
                merged.append(result)
                
                if len(merged) >= max_results:
                    return merged
    
    return merged


def is_valid_url(url: str) -> bool:
    """
    检查 URL 是否有效
    
    Args:
        url: 要检查的 URL
    
    Returns:
        是否有效
    """
    if not url or not isinstance(url, str):
        return False
    
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme in ('http', 'https') and parsed.netloc)
    except Exception:
        return False


def truncate_text(text: str, max_length: int = 500) -> str:
    """
    截断文本到指定长度
    
    Args:
        text: 原始文本
        max_length: 最大长度
    
    Returns:
        截断后的文本
    """
    if not text:
        return ""
    
    text = text.strip()
    if len(text) <= max_length:
        return text
    
    return text[:max_length-3] + "..."
