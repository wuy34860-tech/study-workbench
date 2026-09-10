"""学习工作台 —— 一个陪你走完整个大学阶段的本地学习助手。

功能：
  1. 首页 6 步学习闭环
  2. 学习路线：点一个方向，AI 生成课程 + 学习路径 + 规划
  3. Markdown 笔记：新建 / 上传 / 查看 / 编辑
  4. 统一搜索：全网（DuckDuckGo）+ 本地笔记
  5. AI 助手：解释概念、查报错、总结搜索结果
  6. 一键整理：AI 自动给笔记分类 + 打标签
  7. 丢书检索：传一本书，AI 读完提炼成知识卡片
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from functools import wraps
from html import escape
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, send_file, session
from werkzeug.security import generate_password_hash, check_password_hash
import markdown as md

from parsing import extract_text
from bilibili import bilibili_search

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    anthropic = None
    _HAS_ANTHROPIC = False

try:
    import openai  # OpenAI Chat Completions 兼容协议（覆盖 Qwen/DeepSeek/硅基流动 等）
    _HAS_OPENAI = True
except ImportError:
    openai = None
    _HAS_OPENAI = False

try:
    import ddgs as _ddgs  # 新包
    _HAS_DDG = True
except ImportError:
    try:
        from ddgs import DDGS  # 兼容旧别名
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # 旧包名兼容
        except ImportError:
            DDGS = None
            _HAS_DDG = False
    else:
        _HAS_DDG = True

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE = DATA_DIR / "users.json"

from llm_providers import (
    PROVIDER_ANTHROPIC, PROVIDER_OPENAI_COMPAT, PROVIDER_GEMINI, PROVIDER_DEMO,
    DEFAULT_MODELS, PROVIDER_BASE_URL_DEFAULTS, list_presets,
    call_llm_internal, effective_config,
)

# 默认 provider / model（用户在 settings 里可改）
DEFAULT_PROVIDER = PROVIDER_OPENAI_COMPAT
DEFAULT_MODEL = DEFAULT_MODELS[DEFAULT_PROVIDER]
DEFAULT_BASE_URL = PROVIDER_BASE_URL_DEFAULTS[DEFAULT_PROVIDER]
DEFAULT_WIKI_DIR = r"E:\llm wiki"  # LLM wiki 默认路径（用户可在「记录」页设置里覆盖）

CHUNK_SIZE = 8000   # 丢书检索：每段字数
MAX_CHUNKS = 15     # 丢书检索：最多提炼几段


def effective_settings(username=None) -> dict:
    """合并默认 + 用户 settings，缺省字段用默认值。"""
    from flask import has_request_context
    if username:
        saved = load_settings(username)
    elif has_request_context():
        saved = load_settings() if current_user() else {}
    else:
        saved = {}
    base = {
        "api_key": "",
        "provider": DEFAULT_PROVIDER,
        "model": DEFAULT_MODEL,
        "base_url": DEFAULT_BASE_URL or "",
        "wiki_dir": DEFAULT_WIKI_DIR,
        "record_enabled": True,
        "cleanup_period": "off",
        "last_cleanup": 0,
    }
    base.update(saved or {})
    # 校正类型
    base["api_key"] = (base.get("api_key") or "").strip()
    base["provider"] = base.get("provider") or DEFAULT_PROVIDER
    base["model"] = base.get("model") or DEFAULT_MODELS.get(base["provider"], DEFAULT_MODEL)
    base["base_url"] = (base.get("base_url") or "").strip() or (
        PROVIDER_BASE_URL_DEFAULTS.get(base["provider"], "") or ""
    )
    base["wiki_dir"] = (base.get("wiki_dir") or DEFAULT_WIKI_DIR).strip()
    return base


def current_wiki_dir(username=None) -> Path:
    """根据 settings 取 WIKI_DIR 真实路径（用户配置了优先）。"""
    s = effective_settings(username)
    raw = s.get("wiki_dir") or DEFAULT_WIKI_DIR
    try:
        return Path(raw).expanduser()
    except Exception:
        return Path(DEFAULT_WIKI_DIR)


def _load_dotenv():
    """极简 .env 加载器。"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 上传上限 100MB


def _get_secret():
    secret_file = DATA_DIR / ".secret"
    if secret_file.exists():
        return secret_file.read_text().strip()
    s = os.urandom(24).hex()
    secret_file.write_text(s)
    return s


app.secret_key = _get_secret()


# ---------- 用户账号 ----------

def load_users() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_users(data: dict):
    USERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def register_user(username: str, password: str):
    users = load_users()
    if username in users:
        return False, "用户名已存在"
    users[username] = {"password": generate_password_hash(password)}
    save_users(users)
    return True, "注册成功"


def authenticate(username: str, password: str) -> bool:
    users = load_users()
    u = users.get(username)
    return bool(u and check_password_hash(u.get("password", ""), password))


def _migrate_legacy_notes(username: str):
    """把旧的单用户 notes/ 目录迁移到新用户（只迁移一次）。"""
    legacy = BASE_DIR / "notes"
    if not legacy.exists():
        return
    flag = DATA_DIR / ".migrated"
    if flag.exists():
        return
    target = note_dir(username)
    moved = 0
    for p in legacy.glob("*.md"):
        try:
            p.rename(target / p.name)
            moved += 1
        except Exception:
            pass
    if moved:
        flag.write_text("1")
    try:
        legacy.rmdir()
    except OSError:
        pass


# ---------- 当前用户数据路径 ----------

def current_user():
    return session.get("username")


def user_dir(username=None):
    u = username or current_user()
    d = DATA_DIR / "users" / u
    d.mkdir(parents=True, exist_ok=True)
    return d


def note_dir(username=None):
    d = user_dir(username) / "notes"
    d.mkdir(exist_ok=True)
    return d


def user_file(name: str, username=None):
    return user_dir(username) / name


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def api_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return jsonify({"ok": False, "message": "请先登录"}), 401
        return f(*args, **kwargs)
    return wrapper


# ---------- 匿名访问 & 限流（公开站点用） ----------
from collections import defaultdict

_RATE_HITS: dict = defaultdict(int)


def _client_ip() -> str:
    # 反向代理部署时取 X-Forwarded-For 最右侧真实 IP
    raw = request.headers.get("X-Forwarded-For", "") or request.remote_addr or "?"
    return raw.split(",")[-1].strip()


def _check_ai_rate(limit_anon: int = 40, limit_user: int = 800) -> tuple:
    """AI 类接口限流。登录用户按用户名限，匿名按 IP 限。
    返回 (ok: bool, message: str)。"""
    u = current_user()
    hour = time.strftime("%Y%m%d%H")
    key = (u or "anon:" + _client_ip()) + "|" + hour
    limit = limit_user if u else limit_anon
    hits = _RATE_HITS[key] + 1
    _RATE_HITS[key] = hits
    # 防止 dict 无限膨胀：超过阈值时清理过期桶
    if len(_RATE_HITS) > 10000:
        for k in [k for k in _RATE_HITS if not k.endswith(hour)]:
            _RATE_HITS.pop(k, None)
    if hits > limit:
        return False, f"请求太频繁，请稍后再试（当前每小时最多 {limit} 次）"
    return True, ""


def ai_api(f):
    """公开 AI 类接口：免登录 + 限流。登录用户可存自己的 Key 获得个性化结果；
    匿名用户/未配 Key 时自动走内置 demo 或服务器环境变量 Key。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        ok, msg = _check_ai_rate()
        if not ok:
            return jsonify({"ok": False, "message": msg}), 429
        return f(*args, **kwargs)
    return wrapper

SYSTEM_PROMPT = (
    "你是一位耐心、深入浅出的学习助手，服务一名 AI 专业的大学新生。"
    "用「费曼学习法」帮他把知识真正弄懂：先用简单的话讲清概念，"
    "再点出最常见的误解，最后给一个能立刻动手的小例子。"
    "解释编程和数学时多用类比和具体例子，少堆术语。"
    "如果用户贴的是报错信息，先定位原因，再解释为什么，最后给修复方法。"
    "全程用中文回答。"
)

ROADMAP_PROMPT = (
    "你是一位资深的 AI/计算机学习规划师，擅长把大方向拆成可执行的小步。"
    "用户想学一个方向，请为 TA 生成一份系统、可执行的学习路线图。\n"
    "输出要求：只用中文 Markdown，结构必须严格包含下面 6 个标题段落，顺序不变：\n"
    "## 方向概述（两句话：这个方向做什么、解决什么问题、学完能到什么水平）\n"
    "## 前置知识（学之前需要先会什么；没有则写“无需前置，零基础可学”）\n"
    "## 分阶段路线（按难度递进分成 3~5 个阶段。每个阶段必须用“### 阶段N：名称”作小标题，并写清三行：\n"
    "- 学什么：列出 3~8 个具体知识点/主题\n"
    "- 学多久：给出合理的学习周期（如“2 周，每天 1~2 小时”）\n"
    "- 过关标准：写清楚学到什么程度算过关（能做/能说出什么）\n"
    "## 推荐资源（每个阶段配 1~2 个真实、经典、容易获取的资源：课程/书/视频/文档，如吴恩达《机器学习》、CS229、"
    "李宏毅机器学习、3Blue1Brown、CS231n、花书《深度学习》、李沐《动手学深度学习》等，附一句话说明为什么推荐）\n"
    "## 动手项目（每个阶段配 1 个能检验成果的小项目，写明项目名 + 验收标准）\n"
    "## 时间与节奏建议（给出总周期，以及每周固定投入建议；再给一条针对零基础/转行者的落地建议）\n"
    "全程用中文。不要输出路线图以外的解释或问候语。"
)

ORGANIZE_PROMPT = (
    "你是一个知识库管理员。下面是若干篇学习笔记（编号 + 标题 + 正文摘要）。"
    "请为每篇笔记：① 归到一个简短主题分类（如「机器学习」「数学基础」「Python编程」等）；"
    "② 打 2~4 个简短标签（帮助以后检索）。\n"
    "只输出一个 JSON 对象，不要输出任何解释、代码块标记或多余文字，格式："
    '{"results": [{"index": 0, "category": "分类", "tags": ["标签1", "标签2"]}]}'
)

BOOK_PROMPT = (
    "你是一位高效的知识提炼助手。请阅读下面的书籍内容，把它整理成一份结构化的知识卡片，"
    "用中文 Markdown 输出，包含：\n"
    "## 一句话概括\n"
    "## 核心观点 / 关键概念（分点，保留重要术语）\n"
    "## 章节脉络（如果内容里有章节结构，按章节列出要点）\n"
    "## 值得记住的要点\n"
    "忠于原文，不要编造，抓重点。"
)

PRACTICE_GENERATE_PROMPT = (
    "你是一位出题老师。请根据用户要练的知识点，出 4~6 道由易到难的练习题，用中文 Markdown 输出。"
    "只出题目，不要给答案（用户做完后会单独来对答案）。"
    "题目要贴合该知识点、难度递进，能真正检验理解，不只考记忆。"
)

PRACTICE_CHECK_PROMPT = (
    "你是批改老师。下面是学生要练的知识点、题目、以及学生自己写的答案。"
    "请逐题批改：判断对错、指出错在哪、给出正确解法和思路。用中文 Markdown 输出。"
    "如果学生某题没写或明显没做，温和提醒即可，不要嘲讽。"
)

EXTEND_PROMPT = (
    "你是一位学习拓展顾问。用户已经学过一个知识点，请帮 TA 从三个角度拓展，用中文 Markdown 输出：\n"
    "## 学更深（往更进阶、更深入的方向延伸，给出具体方向和推荐资源）\n"
    "## 学更广（相关领域、交叉学科，开阔视野）\n"
    "## 联系实际（这个知识在真实世界、工程、科研里怎么用，给具体例子）\n"
    "要具体可执行，不要泛泛而谈。"
)

RESOURCES_PROMPT = (
    "你是一位学习资源导航员。请根据用户要学的方向，用中文 Markdown 列出最值得关注的资源，包含：\n"
    "## 核心资料（经典教材、书籍、文档，附网址）\n"
    "## 重要网址（官方文档、学习站点、在线工具，附网址）\n"
    "## 社区（论坛、GitHub 仓库、知乎/Reddit/Discord 等讨论区，附网址）\n"
    "每个给 3~6 个，附一句话说明。要真实、经典、容易访问。"
)

METHOD_PROMPT = (
    "你是一位资深的学习方法专家，精通各学科的高效学习法。请针对用户选择的学科，"
    "生成一套**该学科专属**的学习方法，用中文 Markdown 输出，结构如下：\n"
    "## 学科特点（这个学科的本质、和别的学科最大的不同、常见误区）\n"
    "## 专属学习方法（核心打法：这个学科该怎么学才高效，分点写清可操作步骤）\n"
    "## 分阶段学习路径（按难度递进，每阶段：学什么 / 怎么学 / 怎么检验效果）\n"
    "## 后续学习建议（基础学完后如何进阶、如何复习巩固、如何应用到实际）\n\n"
    "必须体现学科差异性，例如：数学重「理解概念→刷题→错题复盘」；"
    "英语重「可理解输入+间隔复习+大量输出」；编程重「项目驱动+动手+读文档」。"
    "不要给放之四海皆准的通用套路，要这个学科真正高效、可执行、可检验的专属打法。"
)

THEME_MAP_PROMPT = (
    "你是一位学习规划师。用户想学一个方向，请把该方向**拆成一张知识地图（有向图）**：\n"
    "节点 = 学习这条路线必须掌握的核心知识点；边 = 学习依赖（学 B 前必须先掌握 A，就画 A→B）。\n"
    "要求：\n"
    "- 节点 8~14 个，覆盖「入门→基础→进阶→深入→实战」全程；label 用简洁中文（不超过 14 字）\n"
    "- 每个节点给 stage：1=入门 2=基础 3=进阶 4=深入 5=实战（整数 1~5）\n"
    "- 边 8~18 条，只画真实存在的依赖/前置关系，不要画成完全连通；label 用「前置」或「进阶」\n"
    "只输出一个 JSON 对象，不要 Markdown 代码块标记、不要任何解释文字，格式：\n"
    '{"topic":"方向名","nodes":[{"id":"n1","label":"知识点","stage":1}],'
    '"edges":[{"from":"n1","to":"n2","label":"前置"}]}'
)

NOTES_MAP_PROMPT = (
    "你是一位知识图谱构建器。下面是某位学习者保存的笔记列表（编号/标题/分类/标签/掌握程度）。\n"
    "请判断这些知识点之间的**依赖或相关关系**，输出学习地图 JSON：\n"
    '- nodes：每篇笔记一个节点，{"id":"编号(直接用上面的编号)","label":"标题精简版(≤14字)"}\n'
    "- edges：表示关系的有向边 {\"from\":\"编号\",\"to\":\"编号\",\"label\":\"前置\"或\"相关\"}\n"
    "限制：边 0~12 条；只画有把握的真实关系，没有把握就不画边，绝不乱连；也不要在同一篇笔记间画环。\n"
    "只输出一个 JSON 对象，不要代码块标记、不要解释：{\"nodes\":[...],\"edges\":[...]}"
)

REVIEW_PROMPT = (
    "你是学习复盘教练。下面是用户的学习情况（每条标注了掌握程度）。"
    "请生成一份阶段学习梳理报告，用中文 Markdown，包含：\n"
    "## 已掌握的知识（分类列出）\n"
    "## 需要重点复习/巩固的（指出薄弱点）\n"
    "## 下一步学习建议（基于现状给具体方向）\n"
    "## 阶段性里程碑小结（这个阶段达成了什么、下个阶段冲什么）\n"
    "客观准确，不要夸大。"
)


# ---------- 工具函数 ----------

def slugify(text: str) -> str:
    text = re.sub(r"[^\w一-鿿-]+", "-", text.strip().lower())
    return text.strip("-") or "note"


def note_files(username=None):
    return sorted(note_dir(username).glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)


def parse_note(path: Path):
    text = path.read_text(encoding="utf-8")
    title, body = "未命名", text
    m = re.match(r"^#\s+(.+?)\s*$", text, re.M)
    if m:
        title = m.group(1)
        body = text[m.end():].lstrip("\n")
    return title, body


def render_markdown(text: str) -> str:
    return md.markdown(text, extensions=["tables", "fenced_code"])


def create_note(title: str, body: str, username=None) -> str:
    filename = f"{int(time.time())}-{slugify(title)}.md"
    (note_dir(username) / filename).write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    return filename


def local_search(query: str):
    q = query.lower()
    results = []
    for p in note_files():
        title, body = parse_note(p)
        hay = (title + "\n" + body).lower()
        if q in hay:
            idx = hay.find(q)
            start = max(0, idx - 40)
            snippet = "…" + hay[start:idx + len(q) + 80].replace("\n", " ") + "…"
            results.append({"filename": p.name, "title": title, "snippet": snippet})
    return results


def web_search(query: str, max_results: int = 8):
    """全网搜索（DuckDuckGo）。失败时返回 (results, error_msg) 兼容结构。"""
    if not _HAS_DDG:
        return [], "（ddgs 未安装，无法全网搜索）"
    try:
        with DDGS() as ddgs:
            # 新版 ddgs.text(...) 接受 region/time_limit 等参数；这里给 5s 超时，防止页面卡死
            try:
                raw = list(ddgs.text(query, max_results=max_results, timeout=5))
            except TypeError:
                raw = list(ddgs.text(query, max_results=max_results))
        results = [
            {"title": r.get("title", ""), "href": r.get("href", ""), "body": r.get("body", "")}
            for r in raw
        ]
        return results, "" if results else "（全网搜索没结果，可能是 DuckDuckGo 临时限流，重试一下）"
    except Exception as e:
        return [], f"（全网搜索失败：{type(e).__name__}）"


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
    return text


def _wiki_title(text: str, fallback: str) -> str:
    m = re.search(r"^title:\s*(.+)$", text, re.M)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    m2 = re.match(r"^#\s+(.+?)\s*$", _strip_frontmatter(text), re.M)
    if m2:
        return m2.group(1)
    return fallback


def _render_wiki_markdown(text: str) -> str:
    body = _strip_frontmatter(text)
    body = re.sub(r"\[\[([^\]]+)\]\]", r"\1", body)  # [[wikilink]] -> wikilink
    return render_markdown(body)


def llm_wiki_search(q: str, max_results: int = 15, username=None):
    """在 LLM wiki 知识库里搜 .md 笔记。返回 (results, error_msg)。

    wiki 路径从 settings 里读（用户在「记录」页设置里可改）。
    """
    wiki_dir = current_wiki_dir(username)
    if not wiki_dir.exists():
        return [], f"（LLM wiki 目录不存在：{wiki_dir}，可去「记录」页修改）"
    ql = q.lower()
    results = []
    skip_parts = {"raw", "media", "agent-workspace"}
    try:
        candidates = list(wiki_dir.rglob("*.md"))
    except Exception as e:
        return [], f"（遍历 wiki 目录失败：{e}）"
    for p in candidates:
        rel = p.relative_to(wiki_dir)
        parts = rel.parts
        if any(part.startswith(".") or part.startswith("_") or part in skip_parts for part in parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if ql in (rel.stem + "\n" + text).lower():
            idx = text.lower().find(ql)
            if idx >= 0:
                start = max(0, idx - 40)
                snippet = "…" + text[start:idx + len(q) + 80].replace("\n", " ") + "…"
            else:
                snippet = text[:120].replace("\n", " ")
            results.append({
                "relpath": str(rel).replace("\\", "/"),
                "title": _wiki_title(text, rel.stem),
                "snippet": snippet,
            })
            if len(results) >= max_results:
                break
    return results, "" if results else "（wiki 里没搜到；检查路径配置）"


def crossref_search(q: str, max_results: int = 8) -> list:
    """用 Crossref 搜学术论文。"""
    url = (
        "https://api.crossref.org/works?query=" + urllib.parse.quote(q)
        + "&rows=" + str(max_results) + "&select=title,author,issued,container-title,DOI"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "StudyWorkbench/1.0 (mailto:student@example.com)"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception:
        return []
    results = []
    for it in data.get("message", {}).get("items", []):
        title = (it.get("title") or [""])[0]
        issued = it.get("issued", {}).get("date-parts") or [[None]]
        year = str(issued[0][0]) if issued and issued[0] and issued[0][0] else ""
        venue = (it.get("container-title") or [""])[0]
        doi = it.get("DOI", "")
        authors = ", ".join(a.get("family", "") for a in (it.get("author") or [])[:3])
        results.append({
            "title": title,
            "year": year,
            "venue": venue,
            "authors": authors,
            "url": f"https://doi.org/{doi}" if doi else "",
        })
    return results


def save_to_wiki(title: str, body: str) -> str:
    """把一条工作台笔记存进 LLM wiki 的「工作台笔记」文件夹，返回相对路径。"""
    if not WIKI_DIR.exists():
        raise FileNotFoundError("LLM wiki 目录不存在")
    target_dir = WIKI_DIR / "工作台笔记"
    target_dir.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    frontmatter = f"---\ntype: note\ntitle: {title}\ncreated: {today}\nupdated: {today}\n---\n\n"
    content = frontmatter + f"# {title}\n\n{body}\n"
    base = slugify(title) or "note"
    path = target_dir / f"{base}.md"
    if path.exists():
        path = target_dir / f"{base}-{int(time.time())}.md"
    path.write_text(content, encoding="utf-8")
    return str(path.relative_to(WIKI_DIR)).replace("\\", "/")


def _save_upload(file):
    """保存上传的文件到 uploads/，返回 (路径, 原始文件名)。"""
    original = file.filename or "file"
    ext = Path(original).suffix.lower()
    path = UPLOADS_DIR / f"{int(time.time() * 1000)}{ext}"
    file.save(path)
    return path, original


def load_tags(username=None) -> dict:
    if user_file("notes_index.json", username).exists():
        try:
            return json.loads(user_file("notes_index.json", username).read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_tags(data: dict, username=None):
    user_file("notes_index.json", username).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 掌握情况 / 成就 / 里程碑 ----------

MASTERY_LEVELS = {1: "未掌握", 2: "学习中", 3: "已掌握"}
MILESTONES = [1, 3, 5, 10, 20, 50]


def load_mastery(username=None) -> dict:
    if user_file("mastery.json", username).exists():
        try:
            return json.loads(user_file("mastery.json", username).read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_mastery(data: dict, username=None):
    user_file("mastery.json", username).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 使用记录 / 设置 / 清理 ----------


def load_activity(username=None) -> list:
    if user_file("activity.json", username).exists():
        try:
            return json.loads(user_file("activity.json", username).read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def log_activity(action: str, detail: str = "", username=None):
    """记录一条使用记录（若开启了记录）。"""
    try:
        if not load_settings(username).get("record_enabled", True):
            return
        records = load_activity(username)
        records.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, "detail": detail})
        user_file("activity.json", username).write_text(json.dumps(records[-500:], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def load_settings(username=None) -> dict:
    if user_file("settings.json", username).exists():
        try:
            return json.loads(user_file("settings.json", username).read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_settings(data: dict, username=None):
    user_file("settings.json", username).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cleanup_cache() -> int:
    """清理缓存：uploads 临时文件 + 后台任务记录。返回清理的文件数。"""
    removed = 0
    for p in UPLOADS_DIR.glob("*"):
        try:
            if p.is_file():
                p.unlink()
                removed += 1
        except Exception:
            pass
    with JOBS_LOCK:
        JOBS.clear()
    return removed


def _cleanup_loop():
    """后台每小时检查一次，按设置周期自动清理缓存。"""
    while True:
        time.sleep(3600)
        try:
            s = load_settings()
            period = s.get("cleanup_period", "off")
            if period == "off":
                continue
            now = time.time()
            last = s.get("last_cleanup", 0)
            interval = 86400 if period == "daily" else (604800 if period == "weekly" else 0)
            if interval and now - last >= interval:
                cleanup_cache()
                s["last_cleanup"] = now
                save_settings(s)
        except Exception:
            pass


def compute_stats(username=None) -> dict:
    existing = {p.name for p in note_dir(username).glob("*.md")}
    mastery = load_mastery(username)
    mastered = sum(1 for fn, lvl in mastery.items() if fn in existing and lvl == 3)
    books = roadmaps = extends = 0
    for p in note_dir(username).glob("*.md"):
        title, _ = parse_note(p)
        if "知识卡片" in title:
            books += 1
        elif "学习路线" in title:
            roadmaps += 1
        elif "拓展" in title:
            extends += 1
    return {"notes": len(existing), "mastered": mastered, "books": books, "roadmaps": roadmaps, "extends": extends}


ACHIEVEMENTS = [
    {"id": "first-note", "name": "初出茅庐", "desc": "创建第一篇笔记", "icon": "✍️", "cond": lambda s: s["notes"] >= 1},
    {"id": "ten-notes", "name": "勤学不辍", "desc": "累计 10 篇笔记", "icon": "📝", "cond": lambda s: s["notes"] >= 10},
    {"id": "first-master", "name": "融会贯通", "desc": "掌握第一个知识点", "icon": "🧠", "cond": lambda s: s["mastered"] >= 1},
    {"id": "five-master", "name": "小有所成", "desc": "掌握 5 个知识点", "icon": "⭐", "cond": lambda s: s["mastered"] >= 5},
    {"id": "ten-master", "name": "学富五车", "desc": "掌握 10 个知识点", "icon": "🎓", "cond": lambda s: s["mastered"] >= 10},
    {"id": "book", "name": "博览群书", "desc": "丢书提炼一本书", "icon": "📚", "cond": lambda s: s["books"] >= 1},
    {"id": "roadmap", "name": "规划大师", "desc": "生成一份学习路线", "icon": "🗺️", "cond": lambda s: s["roadmaps"] >= 1},
    {"id": "extend", "name": "触类旁通", "desc": "做一次知识拓展", "icon": "🧭", "cond": lambda s: s["extends"] >= 1},
]


# ---------- LLM 调用（透过 llm_providers 抽象层） ----------


def _is_local_ollama_url(base_url: str) -> bool:
    """判断 base_url 是否指向本机 Ollama（Ollama 不校验 API Key）。"""
    try:
        from urllib.parse import urlparse
        p = urlparse(base_url)
        host = (p.hostname or "").lower()
        port = p.port or (443 if p.scheme == "https" else 80)
        return host in ("127.0.0.1", "localhost", "::1") and port == 11434
    except Exception:
        return False


def _has_model_key(username=None) -> bool:
    """当前调用方是否真的能走真实模型（用户 Key / 服务器环境变量 / 本地 Ollama）。"""
    s = effective_settings(username)
    api_key = (s.get("api_key") or "").strip()
    provider = s.get("provider") or DEFAULT_PROVIDER
    base_url = (s.get("base_url") or "").strip() or None
    if not api_key:
        if provider == PROVIDER_ANTHROPIC:
            api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
        elif provider == PROVIDER_OPENAI_COMPAT:
            for var in ("OPENAI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY"):
                v = os.environ.get(var)
                if v:
                    api_key = v
                    break
        elif provider == PROVIDER_GEMINI:
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    if not api_key and base_url and _is_local_ollama_url(base_url):
        return True  # 本机 Ollama：不校验 Key 也能用
    return bool(api_key)


def _call_llm(system, user_msg, max_tokens=8000, username=None):
    """统一的 LLM 调用入口。返回 (ok, text_or_error_message)。

    优先级：
    1. 用户 settings 中的 api_key / provider / model / base_url（登录用户）
    2. 环境变量（服务器级 Key：ANTHROPIC_API_KEY / OPENAI_API_KEY / DASHSCOPE_API_KEY / DEEPSEEK_API_KEY 等）
    3. 都没有 → 内置演示(demo)：不联网、有明确标注的示例输出，保证页面链路可用
    """
    s = effective_settings(username)
    api_key = (s.get("api_key") or "").strip()
    provider = s.get("provider") or DEFAULT_PROVIDER
    model = s.get("model") or DEFAULT_MODELS.get(provider, DEFAULT_MODEL)
    base_url = (s.get("base_url") or "").strip() or None

    # 用户没填 key 就尝试环境变量（向后兼容旧 .env 写法；也是「服务器统一 Key」的注入点）
    if not api_key:
        if provider == PROVIDER_ANTHROPIC:
            api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
        elif provider == PROVIDER_OPENAI_COMPAT:
            for var in ("OPENAI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY"):
                v = os.environ.get(var)
                if v:
                    api_key = v
                    break
        elif provider == PROVIDER_GEMINI:
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        api_key = api_key.strip()

    # 本机 Ollama：不需要任何 API Key（占位即可）
    if not api_key and base_url and _is_local_ollama_url(base_url):
        return call_llm_internal(PROVIDER_OPENAI_COMPAT, system, user_msg, max_tokens,
                                  api_key="ollama", model=model, base_url=base_url)

    # 有 key 且 provider 是真实服务 → 正常调用
    if api_key:
        return call_llm_internal(provider, system, user_msg, max_tokens,
                                  api_key=api_key, model=model, base_url=base_url)
    # 没有 key：显式 demo，或自动降级为 demo（保证体验不断）
    if provider == PROVIDER_DEMO:
        return call_llm_internal(PROVIDER_DEMO, system, user_msg, max_tokens,
                                  api_key="", model="demo", base_url=None)
    return call_llm_internal(PROVIDER_DEMO, system, user_msg, max_tokens,
                              api_key="", model="demo", base_url=None)


def _parse_json_lenient(text: str):
    """从文本里提取并解析 JSON（模型偶尔会在 JSON 前后夹带一点文字）。"""
    try:
        return json.loads(text)
    except Exception:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == opener:
                    depth += 1
                elif text[i] == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except Exception:
                            break
            start = text.find(opener, start + 1)
    raise ValueError("no JSON found")


# 向后兼容：旧代码里仍有人 ask_claude(...) 调用
def ask_claude(system, user_msg, max_tokens=8000) -> str:
    _, result = _call_llm(system, user_msg, max_tokens=max_tokens)
    return result


def generate_roadmap(direction: str, username=None) -> tuple:
    return _call_llm(ROADMAP_PROMPT, f"我想学的方向：{direction}", max_tokens=8000, username=username)


def organize_notes(username=None) -> str:
    """让 AI 给每篇笔记分类+打标签，写入 notes_index.json。返回错误信息（None 表示成功）。"""
    notes = []
    for i, p in enumerate(note_files(username)):
        title, body = parse_note(p)
        notes.append({"index": i, "filename": p.name, "title": title, "body": body[:600]})
    if not notes:
        return "还没有笔记，先写几张或上传一些吧"
    lines = "\n".join(f"[{n['index']}] {n['title']}：{n['body']}" for n in notes)
    ok, result = _call_llm(ORGANIZE_PROMPT, lines, max_tokens=4000, username=username)
    if not ok:
        return result
    try:
        data = _parse_json_lenient(result)
    except Exception:
        return "（AI 返回的内容无法解析，请重试一次）"
    tags = load_tags(username)
    for item in data.get("results", []):
        idx = item.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(notes):
            continue
        tags[notes[idx]["filename"]] = {
            "category": item.get("category", ""),
            "tags": item.get("tags", []) or [],
        }
    save_tags(tags, username)
    return None


def chunk_text(text: str, size: int = CHUNK_SIZE) -> list:
    text = re.sub(r"\s+", " ", text).strip()
    return [text[i:i + size] for i in range(0, len(text), size)]


def summarize_book(text: str, title: str, username=None) -> str:
    """把一本书的文字提炼成知识卡片。长文档分段提炼再汇总。"""
    text = text.strip()
    if not text:
        return "（没能从文件里提取到文字；扫描版 PDF 暂不支持 OCR）"
    if len(text) <= CHUNK_SIZE:
        _, result = _call_llm(BOOK_PROMPT, f"书名：{title}\n\n内容：\n{text}", max_tokens=6000, username=username)
        return result
    chunks = chunk_text(text)
    total = len(chunks)
    truncated = total > MAX_CHUNKS
    chunks = chunks[:MAX_CHUNKS]
    summaries = []
    for i, ch in enumerate(chunks, start=1):
        ok, s = _call_llm(
            "把下面这段书籍节选提炼成要点，200 字以内，中文，保留关键术语和概念。",
            f"（第 {i}/{len(chunks)} 段）\n{ch}",
            max_tokens=600,
            username=username,
        )
        summaries.append(f"## 第 {i} 段要点\n{s if ok else '（本段提炼失败）'}")
    head = ""
    if truncated:
        head = f"> ⚠️ 这本书较长（共约 {total} 段），这里只提炼了前 {MAX_CHUNKS} 段（书籍开头部分）。\n\n"
    ok, final = _call_llm(
        "把下面这些分段要点整合成一份完整、有条理的知识卡片，用中文 Markdown，保留关键概念，忠于原文不编造。",
        f"书名：{title}\n\n分段要点：\n" + "\n\n".join(summaries),
        max_tokens=6000,
        username=username,
    )
    return head + (final if ok else ("（整合失败）\n\n" + "\n\n".join(summaries)))


# ---------- 后台任务 ----------

JOBS = {}
JOBS_LOCK = threading.Lock()


def _job_new(title: str) -> str:
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {
            "id": jid, "title": title, "status": "running", "result": None,
            "created": time.strftime("%H:%M:%S"),
            "owner": current_user() or ("anon:" + _client_ip()),
        }
    return jid


def _job_set(jid: str, **kw):
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid].update(kw)


def _submit_job(title: str, fn, *args) -> str:
    """在后台线程里跑一个任务，返回 job_id。任务结果存到 JOBS 里。"""
    log_activity("任务", title)
    jid = _job_new(title)
    username = current_user()

    def _run():
        try:
            result = fn(username, *args)
            _job_set(jid, status="done", result=result)
        except Exception as e:  # noqa: BLE001
            _job_set(jid, status="error", result={"message": f"任务出错：{e}"})

    threading.Thread(target=_run, daemon=True).start()
    return jid


def _work_roadmap(username, direction):
    ok, roadmap = generate_roadmap(direction, username)
    if not ok:
        return {"message": roadmap}
    return {"roadmap": roadmap, "html": render_markdown(roadmap)}


def _work_practice_generate(username, topic, difficulty):
    ok, problems = _call_llm(PRACTICE_GENERATE_PROMPT, f"知识点：{topic}\n难度：{difficulty}", max_tokens=4000, username=username)
    if not ok:
        return {"message": problems}
    return {"problems": problems, "html": render_markdown(problems)}


def _work_practice_check(username, topic, problems, answer):
    prompt = f"知识点：{topic}\n\n题目：\n{problems}\n\n学生答案：\n{answer if answer else '（未作答）'}"
    ok, feedback = _call_llm(PRACTICE_CHECK_PROMPT, prompt, max_tokens=4000, username=username)
    return {"feedback": feedback, "html": render_markdown(feedback)}


def _work_extend(username, topic):
    ok, result = _call_llm(EXTEND_PROMPT, f"我已学过的知识点：{topic}", max_tokens=4000, username=username)
    if not ok:
        return {"message": result}
    return {"extend": result, "html": render_markdown(result)}


def _work_resources(username, topic):
    papers = crossref_search(topic)
    papers_html = ""
    if papers:
        items = []
        for p in papers:
            title = escape(p["title"])
            meta = escape(" · ".join(x for x in [p["authors"], p["venue"], p["year"]] if x))
            link = f'<a href="{escape(p["url"], quote=True)}" target="_blank" rel="noopener">{title}</a>' if p["url"] else title
            items.append(f'<div class="result-item">{link}<div class="snippet">{meta}</div></div>')
        papers_html = f'<div class="result-section"><h2>📄 相关论文（{len(papers)}）</h2>' + "".join(items) + "</div>"
    ok, resources = _call_llm(RESOURCES_PROMPT, f"我想学的方向：{topic}", max_tokens=4000, username=username)
    if not ok:
        resources = f"（资源整理失败）\n\n{resources}"
    return {"papers_html": papers_html, "html": render_markdown(resources)}


def _work_method(username, subject, level, goal):
    ok, result = _call_llm(
        METHOD_PROMPT, f"学科：{subject}\n当前水平：{level}\n学习目标：{goal}", max_tokens=5000, username=username
    )
    if not ok:
        return {"message": result}
    return {"method": result, "html": render_markdown(result)}


def _work_review(username):
    mastery = load_mastery(username)
    lines = []
    for p in note_files(username):
        title, _ = parse_note(p)
        lvl = mastery.get(p.name, 1)
        lines.append(f"- [{MASTERY_LEVELS.get(lvl, '未掌握')}] {title}")
    if not lines:
        return {"message": "还没有笔记，先去记一些或丢书提炼吧"}
    ok, result = _call_llm(REVIEW_PROMPT, "我的学习情况：\n" + "\n".join(lines), max_tokens=5000, username=username)
    if not ok:
        return {"message": result}
    return {"html": render_markdown(result)}


# ---------- 学习地图 / 知识图谱 ----------


def _sanitize_graph(data: dict, node_count_hint: int = 0) -> dict:
    """清洗 LLM 返回的图谱 JSON：保证 id 存在、边两端都有对应节点、无自环与重复。"""
    nodes = []
    seen = set()
    for n in (data.get("nodes") or [])[:30]:
        nid = str(n.get("id") or "").strip()
        if not nid or nid in seen:
            continue
        seen.add(nid)
        nodes.append({
            "id": nid,
            "label": str(n.get("label") or nid)[:18],
            "stage": min(5, max(1, int(n.get("stage") or 1))),
        })
    edges, seen_e = [], set()
    for e in (data.get("edges") or [])[:30]:
        f, t = str(e.get("from") or "").strip(), str(e.get("to") or "").strip()
        key = f + ">" + t
        if not f or not t or f == t or key in seen_e or f not in seen or t not in seen:
            continue
        seen_e.add(key)
        edges.append({"from": f, "to": t, "label": (e.get("label") or "前置")[:6]})
    if not nodes:
        return {}
    if not edges:
        edges = []
    return {"topic": (data.get("topic") or "").strip(), "nodes": nodes, "edges": edges}


def _map_text(source: str, topic: str, graph: dict) -> str:
    """把图谱转成可保存的 Markdown 文字版。"""
    out = [f"# {topic or '我的学习'} · 学习地图\n"]
    if source == "notes":
        out.append("> 由我的笔记自动生成：节点=笔记，箭头=依赖/相关关系，颜色=掌握程度。\n")
    else:
        out.append("> 节点=核心知识点（编号越大越进阶），箭头=学习依赖（先学起点再学终点）。\n")
    out.append("\n## 知识点清单\n")
    for i, n in enumerate(graph.get("nodes", []), 1):
        if source == "theme":
            out.append(f"{i}. **{n['label']}**（阶段{n.get('stage', 1)}："
                       f"{['入门', '基础', '进阶', '深入', '实战'][n['stage'] - 1]}）")
        else:
            out.append(f"{i}. **{n['label']}**")
    out.append("\n## 依赖关系\n")
    for e in graph.get("edges", []):
        def _label(nid):
            for n in graph.get("nodes", []):
                if n["id"] == nid:
                    return n["label"]
            return nid
        out.append(f"- {_label(e['from'])} → {_label(e['to'])}（{e.get('label', '前置')}）")
    if not graph.get("edges"):
        out.append("（暂无明确依赖关系）")
    return "\n".join(out)


def _work_map(username, source, topic):
    """source: theme=按主题生成地图（公开）；notes=按我的笔记生成（需登录）。"""
    if source == "notes":
        if not username:
            return {"message": "「我的笔记」地图需要登录后使用"}
        notes = []
        tags = load_tags(username)
        mastery = load_mastery(username)
        for i, p in enumerate(note_files(username)):
            title, body = parse_note(p)
            meta = tags.get(p.name) or {}
            notes.append({
                "id": str(i),
                "filename": p.name,
                "title": title or p.name,
                "category": meta.get("category", ""),
                "tags": meta.get("tags", []) or [],
                "level": mastery.get(p.name, 1),
                "snippet": body[:300],
            })
        if len(notes) < 2:
            return {"message": "至少需要 2 篇笔记才能连成图谱。先去「笔记」页写几张，或用学习路线/练习生成后「保存为笔记」。"}
        if _has_model_key(username):
            lines = "\n".join(
                f"[{n['id']}]《{n['title']}》分类:{n['category'] or '无'} 标签:{'、'.join(n['tags']) or '无'} "
                f"掌握:{MASTERY_LEVELS.get(n['level'], '未掌握')}"
                for n in notes)
            ok, result = _call_llm(NOTES_MAP_PROMPT, lines, max_tokens=3000, username=username)
            graph = _sanitize_graph(_parse_json_lenient(result)) if ok else {}
            if not graph.get("nodes"):
                return {"message": ("AI 未能生成图谱：" + result) if not ok else "AI 返回的图谱无法解析，请重试一次"}
            demo = False
        else:  # 无 Key：用本地规则兜底（同分类/同标签连成"相关"），不调用模型
            graph = {"topic": "我的笔记", "nodes": [], "edges": []}
            by_cat: dict = {}
            for n in notes:
                cat = n["category"] or "未分类"
                by_cat.setdefault(cat, []).append(n)
                graph["nodes"].append({"id": n["id"], "label": n["title"][:18], "stage": 1})
            for cat, items in by_cat.items():
                items = items[:12]
                for a, b in zip(items, items[1:]):
                    graph["edges"].append({"from": a["id"], "to": b["id"], "label": "相关"})
            demo = True
        for n in notes:  # 附上客户端渲染需要的本地元数据
            for gn in graph["nodes"]:
                if gn["id"] == n["id"]:
                    gn["filename"] = n["filename"]
                    gn["level"] = n["level"]
                    gn["category"] = n["category"] or ""
                    gn["tags"] = n["tags"][:3]
        return {"source": "notes", "topic": "我的笔记", "demo": demo, "graph": graph,
                "text": _map_text("notes", "我的笔记", graph), "html": render_markdown(_map_text("notes", "我的笔记", graph))}

    # ---- theme ----
    ok, result = _call_llm(THEME_MAP_PROMPT, f"我想学的方向：{topic}", max_tokens=4000, username=username)
    if not ok:
        return {"message": result}
    try:
        graph = _sanitize_graph(_parse_json_lenient(result))
    except Exception:
        return {"message": "（AI 返回的图谱无法解析，请重试一次）"}
    if not graph.get("nodes"):
        return {"message": "（AI 返回的图谱为空，请重试一次）"}
    graph["topic"] = topic
    text = _map_text("theme", topic, graph)
    return {"source": "theme", "topic": topic, "demo": not _has_model_key(username),
            "graph": graph, "text": text, "html": render_markdown(text)}


def _find_font():
    for p in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc"]:
        if os.path.exists(p):
            return p
    return None


def share_text(stats):
    unlocked = [a for a in ACHIEVEMENTS if a["cond"](stats)]
    return (
        "我在「学习工作台」的学习成果：\n"
        f"📝 累计笔记 {stats['notes']} 篇\n"
        f"🧠 已掌握 {stats['mastered']} 个知识点\n"
        f"🏅 解锁 {len(unlocked)} 枚成就\n"
        "继续加油，学而不辍！🎉 #学习打卡"
    )


def share_image(stats):
    from PIL import Image, ImageDraw, ImageFont
    W, H = 600, 800
    img = Image.new("RGB", (W, H), "#1f2a26")
    d = ImageDraw.Draw(img)
    font_path = _find_font()

    def font(size):
        if font_path:
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    unlocked = [a for a in ACHIEVEMENTS if a["cond"](stats)]
    d.text((40, 60), "学习工作台 · 成就卡", font=font(40), fill="#e8f0ec")
    d.text((40, 140), f"累计笔记 {stats['notes']} 篇", font=font(30), fill="#c4d3cd")
    d.text((40, 190), f"已掌握知识点 {stats['mastered']} 个", font=font(30), fill="#c4d3cd")
    d.text((40, 240), f"解锁成就 {len(unlocked)} 枚", font=font(30), fill="#c4d3cd")
    y = 320
    for a in unlocked[:6]:
        d.text((40, y), f"{a['icon']} {a['name']}", font=font(28), fill="#ffd700")
        y += 55
    d.text((40, 720), "#学习打卡", font=font(24), fill="#7d8f88")
    out = UPLOADS_DIR / f"share-{int(time.time())}.png"
    img.save(out)
    return out


def _work_organize(username):
    err = organize_notes(username)
    if err:
        return {"message": err}
    return {"message": "整理完成，已自动分类并打标签 ✓"}


def _work_import(username, path, title):
    text = extract_text(path)
    if not text.strip():
        return {"message": "没能从文件提取文字（扫描版 PDF 暂不支持 OCR）"}
    result = summarize_book(text, title)
    filename = create_note(f"《{title}》知识卡片", result, username)
    return {"note_url": f"/notes/{filename}", "title": f"《{title}》知识卡片"}


# ---------- 页面路由 ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if authenticate(username, password):
            session["username"] = username
            return redirect(url_for("index"))
        return render_template("login.html", error="用户名或密码错误", mode="login")
    return render_template("login.html", error=None, mode="login")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(username) < 2:
            return render_template("login.html", error="用户名至少 2 个字符", mode="register")
        if len(password) < 4:
            return render_template("login.html", error="密码至少 4 位", mode="register")
        ok, msg = register_user(username, password)
        if not ok:
            return render_template("login.html", error=msg, mode="register")
        _migrate_legacy_notes(username)
        session["username"] = username
        return redirect(url_for("index"))
    return render_template("login.html", error=None, mode="register")


@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))


@app.route("/")
def index():
    # 公开站点：未登录访客先看落地介绍页；登录用户进入学习工作台首页
    if not current_user():
        return render_template("landing.html")
    return render_template("index.html")


@app.route("/roadmap")
def roadmap():
    return render_template("roadmap.html")


@app.route("/practice")
def practice():
    topic = request.args.get("topic", "")
    return render_template("practice.html", topic=topic)


@app.route("/extend")
def extend():
    topic = request.args.get("topic", "")
    return render_template("extend.html", topic=topic)


@app.route("/resources")
def resources():
    topic = request.args.get("topic", "")
    return render_template("resources.html", topic=topic)


@app.route("/method")
def method():
    return render_template("method.html")


@app.route("/map")
def map_page():
    return render_template("map.html")


@app.route("/achievements")
@login_required
def achievements():
    stats = compute_stats()
    badges = []
    for a in ACHIEVEMENTS:
        b = dict(a)
        b["unlocked"] = a["cond"](stats)
        badges.append(b)
    unlocked_count = sum(1 for b in badges if b["unlocked"])
    reached = sum(1 for m in MILESTONES if stats["mastered"] >= m)
    next_milestone = next((m for m in MILESTONES if m > stats["mastered"]), None)
    progress_pct = min(100, int(stats["mastered"] / next_milestone * 100)) if next_milestone else 100
    mastery = load_mastery()
    notes = []
    for p in note_files():
        title, _ = parse_note(p)
        lvl = mastery.get(p.name, 1)
        notes.append({"filename": p.name, "title": title, "level": lvl, "level_label": MASTERY_LEVELS.get(lvl, "未掌握")})
    return render_template(
        "achievements.html", stats=stats, badges=badges, unlocked_count=unlocked_count,
        reached=reached, next_milestone=next_milestone or stats["mastered"],
        progress_pct=progress_pct, notes=notes,
    )


@app.route("/tasks")
@login_required
def tasks():
    return render_template("tasks.html")


@app.route("/activity")
@login_required
def activity():
    return render_template("activity.html", settings=load_settings())


def _job_owner():
    return current_user() or ("anon:" + _client_ip())


def _job_allowed(job: dict) -> bool:
    return bool(job) and job.get("owner") == _job_owner()


@app.route("/api/jobs")
@api_required
def api_jobs():
    with JOBS_LOCK:
        items = [j for j in JOBS.values() if _job_allowed(j)]
    items.sort(key=lambda j: j["id"], reverse=True)
    return jsonify({"ok": True, "jobs": items})


@app.route("/api/jobs/<jid>")
def api_job_status(jid):
    with JOBS_LOCK:
        job = JOBS.get(jid)
    if not job or not _job_allowed(job):
        return jsonify({"ok": False, "message": "任务不存在或无权查看"})
    return jsonify({"ok": True, "job": job})


@app.route("/ask")
def ask():
    return render_template("ask.html")


@app.route("/notes")
@login_required
def notes_list():
    tags = load_tags()
    notes = []
    for p in note_files():
        title, body = parse_note(p)
        t = tags.get(p.name, {})
        notes.append({
            "filename": p.name,
            "title": title,
            "preview": body[:120].replace("\n", " "),
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
            "category": t.get("category", ""),
            "tags": t.get("tags", []),
        })
    return render_template("notes.html", notes=notes)


@app.route("/notes/new", methods=["GET", "POST"])
@login_required
def note_new():
    if request.method == "POST":
        title = request.form.get("title", "").strip() or "未命名"
        body = request.form.get("body", "").strip()
        filename = create_note(title, body)
        log_activity("新建笔记", title)
        return redirect(url_for("note_view", filename=filename))
    return render_template("note_edit.html", mode="new", title="", body="")


@app.route("/notes/upload", methods=["GET", "POST"])
@login_required
def notes_upload():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            return render_template("upload.html", error="请选择一个文件")
        path, original = _save_upload(file)
        text = extract_text(path)
        if not text.strip():
            return render_template("upload.html", error="没能从文件提取文字（扫描版 PDF 暂不支持 OCR）")
        title = Path(original).stem or "上传笔记"
        max_len = 50000
        truncated = len(text) > max_len
        body = text[:max_len]
        if truncated:
            body += "\n\n> ⚠️ 内容太长，只保存了前 5 万字；整本书建议用「丢书检索」提炼。"
        filename = create_note(title, body)
        log_activity("上传笔记", original)
        return redirect(url_for("note_view", filename=filename))
    return render_template("upload.html", error=None)


@app.route("/notes/import", methods=["GET", "POST"])
@login_required
def notes_import():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            return render_template("import.html", error="请选择一个文件")
        path, original = _save_upload(file)
        title = Path(original).stem or "读一本书"
        _submit_job(f"提炼《{title}》", _work_import, path, title)
        return redirect(url_for("tasks"))
    return render_template("import.html", error=None)


@app.route("/notes/<path:filename>")
@login_required
def note_view(filename):
    path = _resolve_note(filename)
    text = path.read_text(encoding="utf-8")
    title, _ = parse_note(path)
    html = render_markdown(text)
    return render_template("note_view.html", filename=filename, title=title, html=html)


@app.route("/notes/<path:filename>/edit", methods=["GET", "POST"])
@login_required
def note_edit(filename):
    path = _resolve_note(filename)
    if request.method == "POST":
        title = request.form.get("title", "").strip() or "未命名"
        body = request.form.get("body", "").strip()
        path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        return redirect(url_for("note_view", filename=filename))
    title, body = parse_note(path)
    return render_template("note_edit.html", mode="edit", filename=filename, title=title, body=body)


def _resolve_note(filename: str) -> Path:
    path = (note_dir() / filename).resolve()
    if note_dir().resolve() not in path.parents or not path.is_file():
        abort(404)
    return path


@app.route("/wiki/<path:relpath>")
@login_required
def wiki_view(relpath):
    if not WIKI_DIR.exists():
        abort(404)
    p = (WIKI_DIR / relpath).resolve()
    if WIKI_DIR.resolve() not in p.parents or not p.is_file() or p.suffix.lower() != ".md":
        abort(404)
    text = p.read_text(encoding="utf-8", errors="ignore")
    title = _wiki_title(text, p.stem)
    html = _render_wiki_markdown(text)
    return render_template("wiki_view.html", title=title, html=html, relpath=relpath)


@app.route("/api/note/towiki", methods=["POST"])
@api_required
def api_note_towiki():
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    path = (note_dir() / filename).resolve()
    if note_dir().resolve() not in path.parents or not path.is_file():
        return jsonify({"ok": False, "message": "笔记不存在"})
    title, body = parse_note(path)
    try:
        save_to_wiki(title, body)
    except Exception as e:
        return jsonify({"ok": False, "message": f"保存失败：{e}"})
    return jsonify({"ok": True, "message": "已存到 LLM Wiki ✓"})


@app.route("/api/wiki/tonote", methods=["POST"])
@api_required
def api_wiki_tonote():
    data = request.get_json(silent=True) or {}
    relpath = data.get("relpath", "")
    if not WIKI_DIR.exists():
        return jsonify({"ok": False, "message": "LLM wiki 目录不存在"})
    p = (WIKI_DIR / relpath).resolve()
    if WIKI_DIR.resolve() not in p.parents or not p.is_file() or p.suffix.lower() != ".md":
        return jsonify({"ok": False, "message": "文件不存在"})
    text = p.read_text(encoding="utf-8", errors="ignore")
    title = _wiki_title(text, p.stem)
    body = _strip_frontmatter(text)
    filename = create_note(title, body)
    return jsonify({"ok": True, "url": url_for("note_view", filename=filename)})


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    logged = bool(current_user())
    if not q:
        return render_template("search.html", q="", local=[], web=[], web_msg="", videos=[], wiki=[], wiki_msg="", searched=False)
    if logged:
        log_activity("搜索", q)
    # 匿名访客不检索本地笔记 / 本地 Wiki（那属于用户私人数据）
    local = local_search(q) if logged else []
    web, web_msg = web_search(q)
    videos = bilibili_search(q)
    wiki, wiki_msg = (llm_wiki_search(q) if logged else [], "" if logged else "登录后可搜索你的 LLM Wiki 笔记")
    return render_template("search.html", q=q, local=local, web=web, web_msg=web_msg,
                           videos=videos, wiki=wiki, wiki_msg=wiki_msg, searched=True)


# ---------- API（前端 fetch 调用） ----------

@app.route("/api/ask", methods=["POST"])
@ai_api
def api_ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "answer": "问题不能为空"})
    if len(question) > 2000:
        return jsonify({"ok": False, "answer": "问题太长了，精简到 2000 字以内"})
    log_activity("问AI", question[:50])
    return jsonify({"ok": True, "answer": ask_claude(SYSTEM_PROMPT, question)})


@app.route("/api/summarize", methods=["POST"])
@ai_api
def api_summarize():
    data = request.get_json(silent=True) or {}
    query = data.get("query", "")
    results = data.get("results", []) or []
    items = "\n".join(f"- {r.get('title', '')}: {r.get('body', '')[:200]}" for r in results[:8])
    prompt = (
        f"搜索词：{query}\n\n搜索到的信息：\n{items}\n\n"
        "请用中文简要总结这些信息，并指出哪些与搜索意图最相关、哪些可以跳过。"
    )
    return jsonify({"ok": True, "answer": ask_claude(SYSTEM_PROMPT, prompt)})


@app.route("/api/roadmap", methods=["POST"])
@ai_api
def api_roadmap():
    data = request.get_json(silent=True) or {}
    direction = (data.get("direction") or "").strip()
    if not direction:
        return jsonify({"ok": False, "message": "请填写想学的方向"})
    if len(direction) > 60:
        return jsonify({"ok": False, "message": "方向太长了，请用 60 字以内概括（如：机器学习 / Python后端 / 前端入门）"})
    jid = _submit_job(f"学习路线：{direction}", _work_roadmap, direction)
    return jsonify({"ok": True, "job_id": jid})


@app.route("/api/roadmap/save", methods=["POST"])
@api_required
def api_roadmap_save():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "学习路线").strip()
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "message": "内容为空"})
    filename = create_note(title, content)
    return jsonify({"ok": True, "url": url_for("note_view", filename=filename)})


@app.route("/api/practice/generate", methods=["POST"])
@ai_api
def api_practice_generate():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    difficulty = (data.get("difficulty") or "进阶").strip()
    if not topic:
        return jsonify({"ok": False, "message": "请先输入知识点"})
    jid = _submit_job(f"出题：{topic}", _work_practice_generate, topic, difficulty)
    return jsonify({"ok": True, "job_id": jid})


@app.route("/api/practice/check", methods=["POST"])
@ai_api
def api_practice_check():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    problems = (data.get("problems") or "").strip()
    answer = (data.get("answer") or "").strip()
    if not problems:
        return jsonify({"ok": False, "feedback": "还没有题目，先出题"})
    jid = _submit_job(f"批改：{topic}", _work_practice_check, topic, problems, answer)
    return jsonify({"ok": True, "job_id": jid})


@app.route("/api/practice/find", methods=["POST"])
@ai_api
def api_practice_find():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"ok": False, "results": []})
    results, _msg = web_search(f"{topic} 练习题 题库 讲解", max_results=8)
    return jsonify({"ok": True, "results": results})


@app.route("/api/extend", methods=["POST"])
@ai_api
def api_extend():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"ok": False, "message": "请填写知识点"})
    jid = _submit_job(f"拓展：{topic}", _work_extend, topic)
    return jsonify({"ok": True, "job_id": jid})


@app.route("/api/extend/save", methods=["POST"])
@api_required
def api_extend_save():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "拓展").strip()
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "message": "内容为空"})
    filename = create_note(title, content)
    return jsonify({"ok": True, "url": url_for("note_view", filename=filename)})


@app.route("/api/resources", methods=["POST"])
@ai_api
def api_resources():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"ok": False, "message": "请填写方向"})
    jid = _submit_job(f"找资源：{topic}", _work_resources, topic)
    return jsonify({"ok": True, "job_id": jid})


@app.route("/api/method", methods=["POST"])
@ai_api
def api_method():
    data = request.get_json(silent=True) or {}
    subject = (data.get("subject") or "").strip()
    level = (data.get("level") or "基础").strip()
    goal = (data.get("goal") or "综合").strip()
    if not subject:
        return jsonify({"ok": False, "message": "请选择学科"})
    jid = _submit_job(f"学习方法：{subject}", _work_method, subject, level, goal)
    return jsonify({"ok": True, "job_id": jid})


@app.route("/api/method/save", methods=["POST"])
@api_required
def api_method_save():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "学习方法").strip()
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "message": "内容为空"})
    filename = create_note(title, content)
    return jsonify({"ok": True, "url": url_for("note_view", filename=filename)})


@app.route("/api/map", methods=["POST"])
@ai_api
def api_map():
    data = request.get_json(silent=True) or {}
    source = data.get("source", "theme")
    topic = (data.get("topic") or "").strip()
    if source not in ("theme", "notes"):
        source = "theme"
    if source == "theme":
        if not topic:
            return jsonify({"ok": False, "message": "请填写想生成地图的方向"})
        if len(topic) > 60:
            return jsonify({"ok": False, "message": "方向太长了，请用 60 字以内概括"})
    else:
        if not current_user():
            return jsonify({"ok": False, "message": "「我的笔记」地图需要登录后使用"})
    jid = _submit_job(f"学习地图：{topic or '我的笔记'}", _work_map, source, topic)
    return jsonify({"ok": True, "job_id": jid})


@app.route("/api/mastery", methods=["POST"])
@api_required
def api_mastery():
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    level = data.get("level", 1)
    if level not in (1, 2, 3):
        level = 1
    path = (note_dir() / filename).resolve()
    if note_dir().resolve() not in path.parents or not path.is_file():
        return jsonify({"ok": False, "message": "笔记不存在"})
    mastery = load_mastery()
    mastery[filename] = level
    save_mastery(mastery)
    title, _ = parse_note(path)
    log_activity("掌握状态", f"{MASTERY_LEVELS.get(level, '未掌握')} - {title}")
    return jsonify({"ok": True, "label": MASTERY_LEVELS.get(level, "未掌握")})


@app.route("/api/review", methods=["POST"])
@api_required
def api_review():
    jid = _submit_job("一键梳理知识", _work_review)
    return jsonify({"ok": True, "job_id": jid})


@app.route("/api/share")
@api_required
def api_share():
    stats = compute_stats()
    return jsonify({"ok": True, "text": share_text(stats), "image_url": "/api/share/image"})


@app.route("/api/share/image")
@api_required
def api_share_image():
    try:
        path = share_image(compute_stats())
        return send_file(path, mimetype="image/png", as_attachment=True, download_name="学习成就.png")
    except Exception as e:
        return jsonify({"ok": False, "message": f"图片生成失败：{e}"}), 500


@app.route("/api/organize", methods=["POST"])
@api_required
def api_organize():
    jid = _submit_job("一键整理笔记", _work_organize)
    return jsonify({"ok": True, "job_id": jid})


@app.route("/api/activity")
@api_required
def api_activity():
    return jsonify({"ok": True, "records": load_activity(), "settings": load_settings()})


@app.route("/api/activity/delete", methods=["POST"])
@api_required
def api_activity_delete():
    data = request.get_json(silent=True) or {}
    index = data.get("index")
    records = load_activity()
    if isinstance(index, int) and 0 <= index < len(records):
        records.pop(index)
        user_file("activity.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "message": "记录不存在"})


@app.route("/api/activity/clear", methods=["POST"])
@api_required
def api_activity_clear():
    user_file("activity.json").write_text("[]", encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/activity/toggle", methods=["POST"])
@api_required
def api_activity_toggle():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    settings = load_settings()
    settings["record_enabled"] = enabled
    save_settings(settings)
    return jsonify({"ok": True, "record_enabled": enabled})


@app.route("/api/settings", methods=["POST"])
@api_required
def api_settings():
    data = request.get_json(silent=True) or {}
    settings = load_settings()
    warnings: list = []

    # 简单标量
    if "cleanup_period" in data:
        settings["cleanup_period"] = data["cleanup_period"]
    if "record_enabled" in data:
        settings["record_enabled"] = bool(data["record_enabled"])

    # AI 接入四件套
    if "api_key" in data:
        settings["api_key"] = (data.get("api_key") or "").strip()
    if "provider" in data:
        provider = (data.get("provider") or PROVIDER_ANTHROPIC).strip()
        settings["provider"] = provider
        # 切 provider 时若用户没填自定义 model，回退成该 provider 的默认
        if not (data.get("model") or "").strip():
            settings.setdefault("model", DEFAULT_MODELS.get(provider, DEFAULT_MODEL))
    if "model" in data:
        settings["model"] = (data.get("model") or "").strip()
    if "base_url" in data:
        settings["base_url"] = (data.get("base_url") or "").strip()
    # wiki 路径
    if "wiki_dir" in data:
        settings["wiki_dir"] = (data.get("wiki_dir") or "").strip()

    # —— 配置自愈：纠正常见的 base_url / model 填错 ——
    provider = settings.get("provider") or PROVIDER_ANTHROPIC
    model = (settings.get("model") or "").strip()
    base_url = (settings.get("base_url") or "").strip()
    settings.setdefault("base_url", "")

    if provider == PROVIDER_OPENAI_COMPAT:
        # DeepSeek 常见错误：把官网控制台地址当 API 地址
        if "platform.deepseek.com" in base_url:
            settings["base_url"] = "https://api.deepseek.com"
            warnings.append("DeepSeek 的 API 地址不是 platform.deepseek.com（那是官网控制台），已自动改为 https://api.deepseek.com")
        # base_url 误带完整请求路径
        if settings["base_url"].endswith("/chat/completions"):
            settings["base_url"] = settings["base_url"].rsplit("/chat/completions", 1)[0]
            warnings.append("Base URL 不应以 /chat/completions 结尾，已自动去掉")
        # DeepSeek：model 名纠正 + 缺 base_url 自动补
        low_model = model.lower()
        if "deepseek" in low_model:
            if model not in ("deepseek-chat", "deepseek-reasoner"):
                fixed = "deepseek-reasoner" if ("r1" in low_model or "reasoner" in low_model) else "deepseek-chat"
                settings["model"] = fixed
                warnings.append(f"DeepSeek 公开 API 没有模型 {model!r}，已自动改为 {fixed}")
            if not settings["base_url"]:
                settings["base_url"] = "https://api.deepseek.com"
    # model 为空则补 provider 默认
    if not settings.get("model"):
        settings["model"] = DEFAULT_MODELS.get(provider, DEFAULT_MODEL)

    save_settings(settings)

    # 回包：用 effective_settings 校准后返回（含默认值）
    eff = effective_settings()
    resp = {"ok": True, "settings": eff}
    if warnings:
        resp["warnings"] = warnings
    return jsonify(resp)


@app.route("/api/llm/presets")
@api_required
def api_llm_presets():
    """返回 LLM 提供商预设列表，前端填充表单用。"""
    return jsonify({"ok": True, "presets": list_presets()})


@app.route("/api/cleanup", methods=["POST"])
@api_required
def api_cleanup():
    removed = cleanup_cache()
    settings = load_settings()
    settings["last_cleanup"] = time.time()
    save_settings(settings)
    return jsonify({"ok": True, "message": f"已清理 {removed} 个临时文件，任务记录已清空"})


if __name__ == "__main__":
    import threading
    import webbrowser
    import os as _os
    import argparse

    ap = argparse.ArgumentParser(description="学习工作台 / 公开站点")
    ap.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1；--public 或提供公网部署时可设 0.0.0.0")
    ap.add_argument("--port", type=int, default=None, help="端口，默认 5000（可用环境变量 PORT 覆盖）")
    ap.add_argument("--public", action="store_true", help="监听 0.0.0.0 并关闭自动开浏览器（公网服务器/容器用）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    host = args.host or ("0.0.0.0" if args.public else "127.0.0.1")
    port = args.port or int(_os.environ.get("PORT", "5000"))
    no_browser = args.no_browser or args.public or _os.environ.get("STUDY_NO_BROWSER") == "1"

    # 后台定时清理线程（按设置周期自动清理缓存）
    threading.Thread(target=_cleanup_loop, daemon=True).start()

    # 启动时同时打一行醒目提示，避免用户看不到命令行窗口以为是卡死
    def _open_browser():
        url = f"http://127.0.0.1:{port}"
        if no_browser:
            print(f"\n>>> 服务已启动：http://127.0.0.1:{port}（监听 {host}）\n")
            return
        try:
            webbrowser.open(url)
            print(f"\n>>> 已尝试自动打开浏览器：{url}\n>>> 如果没开，手动复制这个地址进浏览器。\n")
        except Exception as e:
            print(f"\n>>> 自动打开浏览器失败（{e}）。请手动访问：{url}\n")

    # 延迟 1.5 秒自动开浏览器（公网模式不自动开）
    threading.Timer(1.5, _open_browser).start()
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
