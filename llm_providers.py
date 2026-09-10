"""LLM Provider 抽象层。

目标：把"调用大模型"从具体 SDK 解耦出来，让应用支持多家模型 / 中转站。

支持：
- anthropic：官方 Anthropic SDK（Claude 系列）
- openai_compat：OpenAI Chat Completions 兼容协议，覆盖下面这些服务
    · Dashscope（Qwen 通义千问）
    · DeepSeek
    · 硅基流动 SiliconFlow
    · OpenAI 官方 GPT
    · 月之暗面 Kimi / 智谱 GLM / 任何 OpenAI 兼容中转
- gemini：Google Gemini（官方 SDK）

调用模型：
    low-level  call_llm_internal(provider_name, system, user_msg, max_tokens,
                                  api_key, model, base_url=None)
                  → (ok: bool, text_or_message: str)

调用方自己负责从 settings 取出字段再调用——本模块不依赖 Flask 配置。

错误统一通过 LLMError 抛出，外层 catch 后转化成用户可读的提示语。
"""

from __future__ import annotations
from typing import Optional


# --- Provider 名常量 ---
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI_COMPAT = "openai_compat"
PROVIDER_GEMINI = "gemini"
PROVIDER_DEMO = "demo"  # 内置示例：不联网、不消耗额度，用于验证流程/无 Key 时的演示

# 用户在 UI 里能直接点的预设（点一下填充 provider/model/base_url）
PROVIDER_PRESETS: list[dict] = [
    {
        "label": "智谱 GLM-4-Flash（免费 · 注册即用 0 元）",
        "provider": PROVIDER_OPENAI_COMPAT,
        "model": "glm-4-flash",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    {
        "label": "本地 Ollama（免费离线 · 无需 Key · 需先安装）",
        "provider": PROVIDER_OPENAI_COMPAT,
        "model": "qwen2.5:7b",
        "base_url": "http://127.0.0.1:11434/v1",
    },
    {
        "label": "阿里 Dashscope（Qwen-Turbo · 默认推荐 · 便宜快）",
        "provider": PROVIDER_OPENAI_COMPAT,
        "model": "qwen-turbo",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    {
        "label": "Dashscope · Qwen-Plus（更均衡）",
        "provider": PROVIDER_OPENAI_COMPAT,
        "model": "qwen-plus",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    {
        "label": "Dashscope · Qwen-Max（最顶，慢贵）",
        "provider": PROVIDER_OPENAI_COMPAT,
        "model": "qwen-max",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    {
        "label": "DeepSeek",
        "provider": PROVIDER_OPENAI_COMPAT,
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
    },
    {
        "label": "硅基流动 SiliconFlow",
        "provider": PROVIDER_OPENAI_COMPAT,
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "base_url": "https://api.siliconflow.cn/v1",
    },
    {
        "label": "OpenAI（GPT-4o-mini 兼容）",
        "provider": PROVIDER_OPENAI_COMPAT,
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
    },
    {
        "label": "月之暗面 Kimi",
        "provider": PROVIDER_OPENAI_COMPAT,
        "model": "moonshot-v1-8k",
        "base_url": "https://api.moonshot.cn/v1",
    },
    {
        "label": "智谱 GLM（开放平台）",
        "provider": PROVIDER_OPENAI_COMPAT,
        "model": "glm-4-flash",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    {
        "label": "Anthropic Claude（官方）",
        "provider": PROVIDER_ANTHROPIC,
        "model": "claude-3-5-sonnet-latest",
        "base_url": "",
    },
    {
        "label": "内置示例（demo · 无需 Key，用于演示/验证）",
        "provider": PROVIDER_DEMO,
        "model": "demo",
        "base_url": "",
    },
]

# 各 provider 的推荐默认模型
DEFAULT_MODELS: dict[str, str] = {
    PROVIDER_ANTHROPIC: "claude-3-5-sonnet-latest",
    PROVIDER_OPENAI_COMPAT: "qwen-turbo",
    PROVIDER_GEMINI: "gemini-2.0-flash",
    PROVIDER_DEMO: "demo",
}

# 各 provider 的 base_url 默认值（不传则用 SDK 原生）
PROVIDER_BASE_URL_DEFAULTS: dict[str, Optional[str]] = {
    PROVIDER_ANTHROPIC: None,
    PROVIDER_OPENAI_COMPAT: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    PROVIDER_GEMINI: None,
    PROVIDER_DEMO: None,
}


class LLMError(Exception):
    """统一的 LLM 错误。code 给程序看（auth/rate/net/api/import/refusal/model），message 给用户看。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------- Anthropic ----------

def _anthropic_chat(api_key: str, base_url: Optional[str], model: str,
                    system: str, user_msg: str, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise LLMError("import", "（未安装 anthropic 库，先 pip install anthropic）") from e
    if not api_key:
        raise LLMError("auth", "（尚未配置 API 密钥，请在「记录」页设置里填入）")
    kwargs: dict = {"api_key": api_key, "timeout": 60.0}
    if base_url:
        kwargs["base_url"] = base_url
    client = anthropic.Anthropic(**kwargs)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.AuthenticationError as e:
        raise LLMError("auth", "（API 密钥无效，请检查你填的 Key）") from e
    except anthropic.NotFoundError as e:
        raise LLMError("model", f"（模型 {model!r} 不可用：{e}）") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMError("auth", f"（无权访问 {model!r}：{e}）") from e
    except anthropic.RateLimitError as e:
        raise LLMError("rate", "（请求太频繁，稍等几秒再试）") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("net", "（网络连接失败，检查一下网络）") from e
    except Exception as e:  # noqa: BLE001
        raise LLMError("api", f"（AI 调用出错：{e}）") from e
    if getattr(resp, "stop_reason", None) == "refusal":
        raise LLMError("refusal", "（这条请求被安全策略拦下了，换个说法试试）")
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


# ---------- OpenAI Chat Completions 兼容协议 ----------

def _openai_compat_chat(api_key: str, base_url: Optional[str], model: str,
                        system: str, user_msg: str, max_tokens: int) -> str:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise LLMError("import", "（未安装 openai 库，先 pip install openai）") from e
    if not api_key:
        raise LLMError("auth", "（尚未配置 API 密钥，请在「记录」页设置里填入）")
    kwargs: dict = {"api_key": api_key, "timeout": 60.0}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception as e:
        # 不同 SDK 错误类名不太一致，按字符串判断更稳
        name = type(e).__name__
        if "Authentication" in name or "401" in str(e):
            raise LLMError("auth", "（API 密钥无效，请检查你填的 Key）") from e
        if "RateLimit" in name or "429" in str(e):
            raise LLMError("rate", "（请求太频繁，稍等几秒再试）") from e
        if "Connection" in name or "ConnectionError" in name or "Timeout" in name:
            raise LLMError("net", "（网络连接失败，检查一下网络）") from e
        if "NotFound" in name or "404" in str(e) or "model" in str(e).lower() and "not" in str(e).lower():
            raise LLMError("model", f"（模型 {model!r} 不可用，请检查 model 名或 base_url）") from e
        raise LLMError("api", f"（AI 调用出错：{e}）") from e
    if not resp.choices:
        raise LLMError("api", "（AI 没有返回任何内容）")
    content = resp.choices[0].message.content or ""
    return content


# ---------- Gemini ----------

def _gemini_chat(api_key: str, base_url: Optional[str], model: str,
                 system: str, user_msg: str, max_tokens: int) -> str:
    try:
        import google.generativeai as genai
    except ImportError as e:
        raise LLMError("import", "（未安装 google-generativeai，先 pip install google-generativeai）") from e
    if not api_key:
        raise LLMError("auth", "（尚未配置 Gemini API 密钥）")
    genai.configure(api_key=api_key)
    try:
        m = genai.GenerativeModel(model=model, system_instruction=system)
        resp = m.generate_content(user_msg, generation_config={"max_output_tokens": max_tokens})
    except Exception as e:
        name = type(e).__name__
        if "API_KEY" in str(e) or "401" in str(e) or "403" in str(e):
            raise LLMError("auth", "（Gemini 密钥无效）") from e
        if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
            raise LLMError("rate", "（请求太频繁）") from e
        if "UNAVAILABLE" in str(e) or "Connection" in name:
            raise LLMError("net", "（网络连接失败）") from e
        if "NOT_FOUND" in str(e) or "model" in str(e).lower() and "not" in str(e).lower():
            raise LLMError("model", f"（Gemini 模型 {model!r} 不可用）") from e
        raise LLMError("api", f"（AI 调用出错：{e}）") from e
    return resp.text or ""


# ---------- 内置示例（demo）：不联网，验证流程 / 无 Key 兜底 ----------

import re as _re


def _extract_direction(user_msg: str) -> str:
    m = _re.search(r"方向[：:]\s*([^\n]+)", user_msg)
    if m:
        return m.group(1).strip()[:50]
    m = _re.search(r"知识点[：:]\s*([^\n]+)", user_msg)
    if m:
        return m.group(1).strip()[:50]
    return user_msg.strip()[:50] or "学习"


def _demo_roadmap(direction: str) -> str:
    """无 Key 时的内置示例路线。结构固定，用于验证/演示；接上真 Key 即由模型生成。"""
    d = direction
    return (
        "> **演示模式示例**：当前未配置可用的模型 API Key，先展示标准的路线结构。\n"
        "> 在「记录 → 设置」里填入你的 Key（或在服务器配置环境变量）后，每次生成都会是真正为你定制的路线。\n\n"
        f"## 方向概述\n{d} 是一个方向明确、可以循序渐进的领域。按「兴趣全景 → 核心基础 → 专项进阶 → 实战输出」四步走，"
        f"大约 12~16 周（每天投入 1~2 小时）能建立起独立解决问题的扎实能力。\n\n"
        "## 前置知识\n- 零基础可以开始；有编程/数学基础会更快\n"
        "- 建议先掌握：查资料的方法（官方文档 + 搜索引擎 + AI 问答）\n\n"
        "## 分阶段路线\n"
        "### 阶段1：兴趣与全景（第 1~2 周）\n"
        f"- 学什么：弄清 {d} 到底是做什么的、解决什么问题；看 2~3 个科普视频/图解；了解 3 个真实应用案例\n"
        "- 学多久：2 周，每天约 1 小时\n"
        "- 过关标准：能用一句话向别人讲清 {d}，并说出 3 个它能落地的场景\n\n"
        "### 阶段2：核心基础（第 3~6 周）\n"
        f"- 学什么：围绕 {d} 的主干知识建立体系。找一本公认的入门教材或一门经典课程（如中文社区口碑最好的那个），"
        "按章节啃到 70% 以上；边学边记笔记，把每个新概念用自己的话写一遍\n"
        "- 学多久：4 周，每天 1~2 小时\n"
        "- 过关标准：能默写出该领域的主干知识图谱，遇到陌生术语能自己查懂并讲给别人\n\n"
        "### 阶段3：专项进阶（第 7~10 周）\n"
        f"- 学什么：选 {d} 里你最感兴趣的一个子方向深入（每个子方向都配一门进阶课/一本专著/一份官方文档）；"
        "读 2~3 篇入门论文或高质量博客，跟做一遍其中的实验\n"
        "- 学多久：4 周，每天 1~2 小时\n"
        "- 过关标准：能独立跑通一个进阶案例，并把过程整理成一篇可分享的技术笔记\n\n"
        "### 阶段4：实战输出（第 11~16 周）\n"
        f"- 学什么：用 {d} 做一个完整小项目（数据→方案→实现→上线/演示）；把成果写成文档并发出去，接受反馈再迭代\n"
        "- 学多久：4~6 周，每周至少 6 小时整块时间\n"
        "- 过关标准：项目能给别人演示并讲清每一步为什么这么做；简历/作品集上多一个能讲 10 分钟的项目\n\n"
        "## 推荐资源\n"
        "- 阶段1：B 站/YouTube 搜「{d} 入门 动画/科普」，选播放量高、评论区好评的 2~3 个\n"
        "- 阶段2：找该领域「公认入门第一课」——通常是大学公开课或头部教材；先跟一门课，不要同时开三门\n"
        "- 阶段3：官方文档 + 子方向经典论文/综述；在 GitHub 搜高质量开源项目，读其 README 与代码结构\n"
        "- 阶段4：参考真实项目做仿写（教程项目 → 换数据/换场景的改造），再逐步加入自己的想法\n\n"
        "## 动手项目\n"
        "- 阶段2 小项目：把学到的知识做一个 1 天的迷你练习（如小工具/小实验），验证主干概念\n"
        f"- 阶段3 项目：完成一个中型练习，要求使用 {d} 的核心能力并记录过程\n"
        "- 阶段4 项目：端到端完整项目（含文档、演示、可复跑），作为作品对外发布\n\n"
        "## 时间与节奏建议\n"
        f"- 总周期约 12~16 周；每周固定 5~7 次、每次 1~2 小时比周末突击更有效\n"
        f"- 给零基础/转行者的落地建议：先别追求「看懂全部」，以「完成小项目」驱动学习；每阶段结束做一次输出（讲给别人/写成文章），"
        "能输出才算真的学会。\n"
    )


def _demo_map_theme(direction: str) -> str:
    """主题知识地图的内置示例：任何方向都返回同一套结构骨架，标注由外层提示。"""
    import json as _json
    d = (direction or "这个方向").strip()[:20]
    nodes = [
        {"id": "n1", "label": f"{d} 是什么", "stage": 1},
        {"id": "n2", "label": "快速上手体验", "stage": 1},
        {"id": "n3", "label": "核心概念入门", "stage": 2},
        {"id": "n4", "label": "公认入门课/教材", "stage": 2},
        {"id": "n5", "label": "动手小练习", "stage": 3},
        {"id": "n6", "label": "常用工具链", "stage": 3},
        {"id": "n7", "label": "进阶专题一", "stage": 4},
        {"id": "n8", "label": "进阶专题二", "stage": 4},
        {"id": "n9", "label": "综合实战项目", "stage": 5},
    ]
    edges = [
        ("n1", "n3"), ("n1", "n2"), ("n3", "n4"), ("n2", "n4"),
        ("n4", "n5"), ("n4", "n6"), ("n5", "n7"), ("n6", "n7"),
        ("n7", "n8"), ("n7", "n9"), ("n8", "n9"), ("n6", "n9"),
    ]
    return _json.dumps({
        "topic": d,
        "nodes": nodes,
        "edges": [{"from": f, "to": t, "label": "前置"} for f, t in edges],
    }, ensure_ascii=False)


def _demo_chat(api_key: str, base_url: Optional[str], model: str,
               system: str, user_msg: str, max_tokens: int) -> str:
    """内置示例 provider：不联网，根据 prompt 类型返回结构化样例。"""
    if "学习路线图" in system:
        return _demo_roadmap(_extract_direction(user_msg))
    if "拆成一张知识地图" in system:
        return _demo_map_theme(_extract_direction(user_msg))
    if "出题老师" in system:
        return (
            "（演示模式示例，接入真实模型后为自动出题）\n"
            "1.（基础）用自己的话解释该知识点的核心定义，并举一个生活例子。\n"
            "2.（基础）指出初学者最容易搞错的一个误区，说明错在哪。\n"
            "3.（进阶）给出一个需要两步推理的小问题并作答。\n"
            "4.（进阶）把该知识点与相邻概念对比：相同点、不同点各两条。\n"
            "5.（挑战）设计一个能验证自己是否真正掌握的自测题。\n"
            "6.（挑战）设想一个实际场景，说明该知识点在那里是怎么被用到的。\n"
        )
    if "批改老师" in system:
        return (
            "（演示模式示例，接入真实模型后为逐题批改）\n"
            "整体印象：作答认真，方向正确。建议逐题对照标准答案自查：\n"
            "- 定义类题目：检查「关键词」是否完整（少了限定条件容易失分）；\n"
            "- 推理类题目：重看每一步推导是否都写明了依据；\n"
            "- 概念对比题：确保分别列出了「相同点」和「不同点」，不要只写一边；\n"
            "- 场景题：确认例子确实用到了该知识点，而不是泛泛而谈。\n"
            "改完错题后，把错因写进笔记，一周后再做一遍，能独立写对才算过关。\n"
        )
    # 默认：通用回答（问 AI / 总结 / 方法 / 拓展 / 复习 / 丢书 / 找资源等）
    return (
        "（当前为内置演示输出：未配置可用的模型 API Key，这是预先写好的示例回答。\n"
        "在「记录 → 设置」中填入 Key 并保存后，这里会变成真正的 AI 回答。）\n\n"
        f"关于「{_extract_direction(user_msg)}」，建议按这个套路自己动手弄清楚：\n"
        "1. 先用一句话写下你目前的理解；\n"
        "2. 查 2~3 个不同来源（官方文档、入门教程、视频），找出与你理解不一致的地方；\n"
        "3. 把概念用自己的话写一遍，再举一个例子验证；\n"
        "4. 找一道相关题目/小实验做掉，能独立做对才算真懂。\n"
    )


# ---------- 统一入口 ----------

_DISPATCH = {
    PROVIDER_ANTHROPIC: _anthropic_chat,
    PROVIDER_OPENAI_COMPAT: _openai_compat_chat,
    PROVIDER_GEMINI: _gemini_chat,
    PROVIDER_DEMO: _demo_chat,
}


def call_llm_internal(provider_name: str, system: str, user_msg: str,
                      max_tokens: int, api_key: str, model: str,
                      base_url: Optional[str] = None) -> tuple:
    """统一的 LLM 调用入口。

    返回 (ok: bool, text_or_error_message: str)。
    出错时 ok=False, text 字段是可向用户展示的友好错误信息。
    """
    fn = _DISPATCH.get(provider_name)
    if fn is None:
        return False, f"（未知 provider: {provider_name!r}，请在「记录」页设置里换一个）"
    try:
        text = fn(api_key, base_url or None, model, system, user_msg, max_tokens)
        return True, text
    except LLMError as e:
        return False, e.message
    except Exception as e:  # noqa: BLE001
        return False, f"（AI 调用出错：{e}）"


def list_presets() -> list[dict]:
    """返回 UI 用的预设列表。"""
    return list(PROVIDER_PRESETS)


def effective_config(provider: str | None = None) -> tuple[str, str, Optional[str]]:
    """根据 provider 名给出 (provider, model, base_url) 默认值。"""
    p = provider or PROVIDER_ANTHROPIC
    if p not in DEFAULT_MODELS:
        p = PROVIDER_ANTHROPIC
    return p, DEFAULT_MODELS[p], PROVIDER_BASE_URL_DEFAULTS.get(p)
