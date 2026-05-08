from __future__ import annotations

import json
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional, Type, TypeVar

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

load_dotenv()

try:
    from langchain_community.chat_models.tongyi import ChatTongyi
except ImportError as e:  # pragma: no cover
    raise ImportError("请安装 langchain-community 并配置阿里云 DashScope") from e

T = TypeVar("T", bound=BaseModel)

# get_chat_model 可能被多次调用，仅首次打印基座信息
_llm_load_logged: bool = False

# 单次爬虫进程内：各次结构化 LLM 调用的 token 累加（invoke_json / extract_with_schema）
_llm_usage_lock = threading.Lock()
_llm_usage_totals: Dict[str, int] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "llm_calls": 0,
}


def reset_llm_token_usage() -> None:
    """新一次爬取开始前清零（由 run_crawl 调用）。"""
    global _llm_usage_totals
    with _llm_usage_lock:
        _llm_usage_totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "llm_calls": 0,
        }


def get_llm_token_usage() -> Dict[str, Any]:
    """返回当前累加的 token 用量（浅拷贝）。"""
    with _llm_usage_lock:
        return dict(_llm_usage_totals)


def _usage_dict_from_message(msg: Any) -> Optional[Dict[str, int]]:
    """从 LangChain AIMessage 解析各提供商返回的 usage。"""
    prompt = completion = total = 0

    um = getattr(msg, "usage_metadata", None)
    if isinstance(um, dict):
        prompt = int(um.get("input_tokens") or um.get("prompt_tokens") or 0)
        completion = int(um.get("output_tokens") or um.get("completion_tokens") or 0)
        total = int(um.get("total_tokens") or 0)
        if total == 0 and (prompt or completion):
            total = prompt + completion
        if prompt or completion or total:
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
            }

    rm = getattr(msg, "response_metadata", None)
    if isinstance(rm, dict):
        tu = rm.get("token_usage")
        if not isinstance(tu, dict):
            tu = rm.get("usage")
        if isinstance(tu, dict):
            prompt = int(
                tu.get("prompt_tokens")
                or tu.get("input_tokens")
                or tu.get("prompt")
                or 0
            )
            completion = int(
                tu.get("completion_tokens")
                or tu.get("output_tokens")
                or tu.get("completion")
                or 0
            )
            total = int(tu.get("total_tokens") or 0)
            if total == 0 and (prompt or completion):
                total = prompt + completion
            if prompt or completion or total:
                return {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": total,
                }

    ak = getattr(msg, "additional_kwargs", None)
    if isinstance(ak, dict):
        tu = ak.get("usage")
        if isinstance(tu, dict):
            prompt = int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
            completion = int(tu.get("completion_tokens") or tu.get("output_tokens") or 0)
            total = int(tu.get("total_tokens") or 0)
            if total == 0 and (prompt or completion):
                total = prompt + completion
            if prompt or completion or total:
                return {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": total,
                }

    return None


def _accumulate_llm_usage_from_message(msg: Any) -> None:
    global _llm_usage_totals
    u = _usage_dict_from_message(msg)
    with _llm_usage_lock:
        _llm_usage_totals["llm_calls"] += 1
        if not u:
            return
        p, c, t = u["prompt_tokens"], u["completion_tokens"], u["total_tokens"]
        _llm_usage_totals["prompt_tokens"] += p
        _llm_usage_totals["completion_tokens"] += c
        if t > 0:
            _llm_usage_totals["total_tokens"] += t
        elif p or c:
            _llm_usage_totals["total_tokens"] += p + c


def _get_chat_model_deepseek():
    """DeepSeek 官方 API（OpenAI 兼容端点）。"""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:  # pragma: no cover
        raise ImportError("使用 DeepSeek 请安装：pip install langchain-openai") from e
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "已选择 DeepSeek 基座，请设置环境变量 DEEPSEEK_API_KEY（https://api.deepseek.com）"
        )
    model_name = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    base_url = (os.environ.get("DEEPSEEK_BASE_URL") or "").strip() or "https://api.deepseek.com/v1"
    base_url = base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    try:
        max_out = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "8192"))
    except ValueError:
        max_out = 8192
    max_out = max(1024, min(max_out, 65536))
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=0.2,
        max_tokens=max_out,
    )


def _moonshot_model_supports_thinking_switch(model_name: str) -> bool:
    """kimi-k2.5 / kimi-k2.6 可在请求体中开关 thinking（见 Moonshot 文档）。"""
    m = model_name.lower()
    return "kimi-k2.5" in m or "kimi-k2.6" in m


def _moonshot_thinking_disabled_requested(model_name: str) -> bool:
    """快速（非思考）模式：MOONSHOT_THINKING=disabled 或 KIMI_MODE=instant / fast。"""
    if not _moonshot_model_supports_thinking_switch(model_name):
        return False
    ex = (os.environ.get("MOONSHOT_THINKING") or "").strip().lower()
    if ex in ("enabled", "on", "1", "true", "yes"):
        return False
    if ex in ("disabled", "off", "0", "false", "no"):
        return True
    mode = (os.environ.get("KIMI_MODE") or os.environ.get("MOONSHOT_KIMI_MODE") or "").strip().lower()
    return mode in ("instant", "fast")


def _moonshot_sampling_temperature(
    model_name: str, *, thinking_disabled: Optional[bool] = None
) -> float:
    """
    kimi-k2.5 / k2.6：思考开启时 API 固定 temperature=1.0，关闭思考（快速）时固定 0.6。
    其它 kimi-k2：KIMI_MODE=instant/fast → 0.6，否则 1.0；非 k2 系默认 0.2。
    MOONSHOT_TEMPERATURE / KIMI_TEMPERATURE 若设置则优先（需与 thinking 组合自洽）。
    """
    raw = (os.environ.get("MOONSHOT_TEMPERATURE") or os.environ.get("KIMI_TEMPERATURE") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    if thinking_disabled is not None and _moonshot_model_supports_thinking_switch(model_name):
        return 0.6 if thinking_disabled else 1.0
    m = model_name.lower()
    is_k2 = m.startswith("kimi-k2") or "kimi-k2" in m
    if not is_k2:
        return 0.2
    mode = (os.environ.get("KIMI_MODE") or os.environ.get("MOONSHOT_KIMI_MODE") or "").strip().lower()
    if mode in ("instant", "fast"):
        return 0.6
    if mode in ("thinking", "think", "1", "true", "yes"):
        return 1.0
    return 1.0


def _get_chat_model_moonshot_kimi():
    """Kimi（Moonshot）OpenAI 兼容 API：https://platform.moonshot.cn/docs/api/chat"""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:  # pragma: no cover
        raise ImportError("使用 Kimi / Moonshot 请安装：pip install langchain-openai") from e
    api_key = (
        os.environ.get("MOONSHOT_API_KEY") or os.environ.get("KIMI_API_KEY") or ""
    ).strip()
    if not api_key:
        raise RuntimeError(
            "已选择 Kimi（Moonshot），请设置 MOONSHOT_API_KEY 或 KIMI_API_KEY（平台发放的 sk-…）"
        )
    model_name = (
        os.environ.get("MOONSHOT_MODEL") or os.environ.get("KIMI_MODEL") or "moonshot-v1-8k"
    ).strip()
    thinking_off = _moonshot_thinking_disabled_requested(model_name)
    model_kwargs: Dict[str, Any] = {}
    if thinking_off:
        model_kwargs["thinking"] = {"type": "disabled"}
    td: Optional[bool] = thinking_off if _moonshot_model_supports_thinking_switch(model_name) else None
    temperature = _moonshot_sampling_temperature(model_name, thinking_disabled=td)
    base_url = (os.environ.get("MOONSHOT_BASE_URL") or "").strip() or "https://api.moonshot.cn/v1"
    base_url = base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    try:
        max_out = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "8192"))
    except ValueError:
        max_out = 8192
    max_out = max(1024, min(max_out, 65536))
    kwargs: Dict[str, Any] = dict(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_out,
    )
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs
    return ChatOpenAI(**kwargs)


def _get_chat_model_tongyi():
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("请设置环境变量 DASHSCOPE_API_KEY（阿里云 DashScope）")
    model_name = os.environ.get("TONGYI_MODEL", "qwen-plus")
    try:
        max_out = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "8192"))
    except ValueError:
        max_out = 8192
    max_out = max(1024, min(max_out, 65536))
    return ChatTongyi(
        model=model_name,
        dashscope_api_key=api_key,
        temperature=0.2,
        model_kwargs={"max_tokens": max_out},
    )


def get_chat_model():
    """
    由环境变量 LLM_PROVIDER 选择基座：
    - tongyi（默认）：阿里云通义，需 DASHSCOPE_API_KEY
    - deepseek：需 DEEPSEEK_API_KEY；可选 DEEPSEEK_MODEL、DEEPSEEK_BASE_URL
    - kimi / moonshot：需 MOONSHOT_API_KEY 或 KIMI_API_KEY；可选 MOONSHOT_MODEL、MOONSHOT_BASE_URL；
      kimi-k2.5/k2.6：默认开启思考（temperature=1）；快速模式设 KIMI_MODE=instant 或 MOONSHOT_THINKING=disabled，
      将传 thinking=disabled 且 temperature=0.6（与官方一致）；可设 MOONSHOT_TEMPERATURE 覆盖
    """
    global _llm_load_logged
    provider = (os.environ.get("LLM_PROVIDER") or "tongyi").strip().lower()
    if provider in ("deepseek", "ds"):
        m = _get_chat_model_deepseek()
        if not _llm_load_logged:
            mn = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
            bu = (os.environ.get("DEEPSEEK_BASE_URL") or "").strip() or "https://api.deepseek.com/v1"
            print(
                f"[AgentCrawler] LLM 基座: DeepSeek | 模型: {mn} | API: {bu}",
                file=sys.stderr,
            )
            _llm_load_logged = True
        return m
    if provider in ("kimi", "moonshot", "moonshotai"):
        m = _get_chat_model_moonshot_kimi()
        if not _llm_load_logged:
            mn = (
                os.environ.get("MOONSHOT_MODEL")
                or os.environ.get("KIMI_MODEL")
                or "moonshot-v1-8k"
            ).strip()
            bu = (os.environ.get("MOONSHOT_BASE_URL") or "").strip() or "https://api.moonshot.cn/v1"
            mode_note = ""
            if _moonshot_model_supports_thinking_switch(mn):
                mode_note = (
                    " | 快速(非思考)"
                    if _moonshot_thinking_disabled_requested(mn)
                    else " | 思考(默认)"
                )
            print(
                f"[AgentCrawler] LLM 基座: Kimi (Moonshot) | 模型: {mn} | API: {bu}{mode_note}",
                file=sys.stderr,
            )
            _llm_load_logged = True
        return m
    if provider in ("tongyi", "dashscope", "qwen", ""):
        m = _get_chat_model_tongyi()
        if not _llm_load_logged:
            mn = os.environ.get("TONGYI_MODEL", "qwen-plus")
            print(
                f"[AgentCrawler] LLM 基座: 阿里云通义 | 模型: {mn}",
                file=sys.stderr,
            )
            _llm_load_logged = True
        return m
    raise RuntimeError(
        f"未知的 LLM_PROVIDER={provider!r}，请使用 tongyi、deepseek 或 kimi"
    )


def _parse_json_block(text: str) -> Any:
    raw = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if m:
        raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        st = raw.find("{")
        en = raw.rfind("}")
        if st >= 0 and en > st:
            return json.loads(raw[st : en + 1])
        raise


def invoke_json(system: str, user: str, model=None) -> Any:
    llm = model or get_chat_model()
    msg = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    _accumulate_llm_usage_from_message(msg)
    return _parse_json_block(msg.content)


class ExtractionPayload(BaseModel):
    """单轮抽取的结构化输出。"""

    topic_texts: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "用户给出的每一项 topic 是一条待回答的「问题」。仅当本页正文能**直接、具体地作答**时才写入；"
            "禁止使用「详见」「点击」「请参考」「了解更多」「click here」等导航套话代替实质正文；"
            "若只有栏目名、引导语、链接标题、一句泛化提示（如仅有 “For international students” 而无申请条件、流程、截止日期等实质内容）"
            "则**不得**写入该键或必须为空字符串，禁止用无关文案凑答案（含导航式套话的输出会被系统丢弃）"
        ),
    )
    pending_topics: list[str] = Field(
        default_factory=list,
        description="仍无法从本页得到实质性回答的问题（topic）；本页仅有线索或无关时应列入",
    )


class InteractionPlan(BaseModel):
    """下一步交互计划。"""

    element_index: Optional[int] = Field(
        default=None,
        description="可交互列表中的序号；若不应再交互则为 null",
    )
    reason: str = Field(
        default="",
        description="简要说明；有待完成主题时应点明主要服务哪一个尚未满足的主题",
    )


class ExploreWorthiness(BaseModel):
    """主题已均有内容时，当前页是否仍存在值得继续探索的信息入口。"""

    should_explore: bool = Field(
        description="是否存在值得点击以深化、补充主题信息的入口（忽略纯导航/登录/广告）",
    )
    reason: str = ""


class RefinedResultsPayload(BaseModel):
    """爬取结束后对多轮合并的 answers 做去重与精炼。"""

    topic_texts: dict[str, str] = Field(
        default_factory=dict,
        description="键与问题列表一致；值为精炼后的单条回答，无内容则为空字符串",
    )


class DomChangeDecision(BaseModel):
    """同 URL 下出现新可交互元素时，是否值得继续点击探索。"""

    should_seek_further_interaction: bool = Field(
        description="新增入口是否可能帮助收集用户主题相关信息；无待完成主题时用于决定是否继续点选",
    )
    reason: str = ""


def extract_with_schema(
    system: str,
    user: str,
    schema: Type[T],
    model=None,
) -> T:
    """解析为 Pydantic 模型。"""
    data = invoke_json(system, user, model)
    return schema.model_validate(data)


# 单题草稿过长时仅截取前段精炼，避免超出模型上下文
_MAX_REFINE_INPUT_PER_TOPIC = 8000


def refine_merged_results(
    topics: List[str],
    results: Dict[str, str],
    *,
    model=None,
    skill_context: Optional[str] = None,
) -> Dict[str, str]:
    """对 `results` 中各问题的合并长文去重、压缩为更精炼的回答；失败时返回原内容的副本。"""
    raw = {t: (results.get(t) or "").strip() for t in topics}
    if not any(raw.values()):
        return dict(raw)
    draft: Dict[str, str] = {}
    for t in topics:
        s = raw.get(t, "")
        if len(s) > _MAX_REFINE_INPUT_PER_TOPIC:
            s = s[:_MAX_REFINE_INPUT_PER_TOPIC] + "\n...[草稿过长已截断，请仅依据上文精炼，勿补全未展示部分]"
        draft[t] = s
    sk = (skill_context or "").strip()
    skill_block = ""
    if sk:
        skill_block = (
            "\n\n【附加：领域说明（本地 Skill，精炼时请遵守其中的字段与格式约束）】\n"
            f"{sk}\n"
        )
    sys_m = (
        "你是编辑助手。输入为多轮网页爬取后合并的「问题→长文本」草稿，同一问题下常见以 --- 分隔的重复段落。\n"
        "请为每个问题输出**一条**精炼答案，要求：\n"
        "1. 合并重复信息，删除冗余表述，保留关键事实、条件、步骤、数据与列表要点；\n"
        "2. 表述紧凑，避免堆砌同义句；\n"
        "3. 语言与输入一致（中文问题以中文为主，可保留必要英文专名）；\n"
        "4. 不得添加「详见」「点击」「请参考」等导航套话，不得编造草稿中不存在的内容；\n"
        "5. 某问题草稿为空或仅有无效碎片则对应值为空字符串 \"\"；\n"
        "6. topic_texts 的键必须与给定问题列表**完全一致**（逐字相同）。\n"
        "只输出 JSON：{\"topic_texts\": {...}}"
        + skill_block
    )
    user_m = (
        f"问题列表：{json.dumps(topics, ensure_ascii=False)}\n\n"
        f"合并草稿（JSON）：{json.dumps(draft, ensure_ascii=False)}"
    )
    try:
        out: RefinedResultsPayload = extract_with_schema(
            sys_m, user_m, RefinedResultsPayload, model=model
        )
        merged: Dict[str, str] = {}
        for t in topics:
            v = (out.topic_texts or {}).get(t)
            if v is not None:
                merged[t] = v.strip()
            else:
                merged[t] = raw.get(t, "")
        return merged
    except Exception:
        return dict(raw)
