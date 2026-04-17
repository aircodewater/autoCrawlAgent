from __future__ import annotations

import json
import os
import re
import sys
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
    - deepseek：需 DEEPSEEK_API_KEY；可选 DEEPSEEK_MODEL（默认 deepseek-chat）、DEEPSEEK_BASE_URL
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
        f"未知的 LLM_PROVIDER={provider!r}，请使用 tongyi 或 deepseek"
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
