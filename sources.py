"""AI 日报 · 多源采集层

从 14 个一手信源抓取当天内容，统一归一化成同一种条目结构，供上层交给模型综述。

设计约定：
- 所有抓取器只负责「拿到条目」，不做任何 LLM 调用与渲染
- 网络异常一律向上抛出或降级为空列表，绝不让插件崩溃
- 使用 aiohttp 异步库（遵循 AstrBot 官方规范，不使用 requests）
- 单个信源失败不影响其他信源

条目结构：
    {
        "title": str,          # 标题
        "url": str,            # 原文链接
        "summary": str,        # 摘要（已去 HTML 标签、截断）
        "source": str,         # 信源 key
        "source_label": str,   # 信源展示名
        "published": str,      # 发布时间 "YYYY-MM-DD HH:MM"（可能为空）
        "_ts": float,          # 排序用时间戳（0 表示未知时间）
    }
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

try:
    import aiohttp
    _AIOHTTP_OK = True
except Exception:  # pragma: no cover - 缺依赖时插件仍可加载，命令会给出提示
    aiohttp = None
    _AIOHTTP_OK = False

try:
    from astrbot.api import logger
except Exception:  # 允许脱离 AstrBot 独立跑自测
    import logging

    logger = logging.getLogger("astrbot_plugin_ai_daily")


# ---------------------------------------------------------------------------
# 信源注册表：新增信源在这里登记即可，命令/抓取/统计自动适配。
# ---------------------------------------------------------------------------
SOURCES: Dict[str, Dict] = {
    # ---- 官方发布 ----
    "openai": {
        "label": "OpenAI", "emoji": "🟢", "group": "官方发布",
        "kind": "rss", "url": "https://openai.com/news/rss.xml",
        "desc": "OpenAI 官方新闻与发布",
    },
    "google_ai": {
        "label": "Google AI", "emoji": "🔵", "group": "官方发布",
        "kind": "rss", "url": "https://blog.google/technology/ai/rss/",
        "desc": "Google 官方 AI 博客",
    },
    "hf_blog": {
        "label": "HuggingFace Blog", "emoji": "🤗", "group": "官方发布",
        "kind": "rss", "url": "https://huggingface.co/blog/feed.xml",
        "desc": "HuggingFace 官方博客",
    },
    "deepmind": {
        "label": "DeepMind", "emoji": "🧠", "group": "官方发布",
        "kind": "rss", "url": "https://deepmind.google/blog/rss.xml",
        "desc": "Google DeepMind 官方博客",
    },
    # ---- 论文研究 ----
    "hf_papers": {
        "label": "HF 每日论文", "emoji": "📄", "group": "论文研究",
        "kind": "hf_papers", "url": "https://huggingface.co/api/daily_papers",
        "desc": "HuggingFace 每日精选论文",
    },
    "arxiv_ai": {
        "label": "arXiv cs.AI", "emoji": "📚", "group": "论文研究",
        "kind": "rss", "url": "https://export.arxiv.org/rss/cs.AI",
        "desc": "arXiv 人工智能分类最新预印本",
    },
    "google_research": {
        "label": "Google Research", "emoji": "🔬", "group": "论文研究",
        "kind": "rss", "url": "https://research.google/blog/rss/",
        "desc": "Google Research 官方博客",
    },
    # ---- 技术社区 ----
    "hn": {
        "label": "Hacker News", "emoji": "💬", "group": "技术社区",
        "kind": "rss", "url": "https://news.ycombinator.com/rss",
        "ai_filter": True, "desc": "Hacker News 头条（自动筛 AI 相关）",
    },
    "reddit_llm": {
        "label": "r/LocalLLaMA", "emoji": "🦙", "group": "技术社区",
        "kind": "rss", "url": "https://www.reddit.com/r/LocalLLaMA/.rss",
        "desc": "Reddit 本地大模型社区热帖",
    },
    "github_trend": {
        "label": "GitHub Trending", "emoji": "⭐", "group": "技术社区",
        "kind": "github_trend", "url": "https://github.com/trending?since=daily",
        "ai_filter": True, "desc": "GitHub 今日趋势仓库（自动筛 AI 相关）",
    },
    # ---- 中文媒体 ----
    "qbitai": {
        "label": "量子位", "emoji": "📮", "group": "中文媒体",
        "kind": "rss", "url": "https://www.qbitai.com/feed",
        "desc": "量子位中文 AI 资讯",
    },
    "infoq": {
        "label": "InfoQ", "emoji": "📘", "group": "中文媒体",
        "kind": "rss", "url": "https://www.infoq.cn/feed",
        "desc": "InfoQ 中文技术资讯",
    },
    # ---- 英文媒体 ----
    "techcrunch": {
        "label": "TechCrunch AI", "emoji": "📰", "group": "英文媒体",
        "kind": "rss", "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "desc": "TechCrunch 人工智能板块",
    },
    "nvidia": {
        "label": "NVIDIA Blog", "emoji": "🎮", "group": "英文媒体",
        "kind": "rss", "url": "https://blogs.nvidia.com/feed/",
        "desc": "NVIDIA 官方博客",
    },
}

# 默认启用集合（与 _conf_schema.json 的默认值保持一致）
DEFAULT_SOURCES = [
    "openai", "google_ai", "hf_blog", "hf_papers",
    "qbitai", "techcrunch", "hn", "github_trend",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 判定「AI 相关」的关键词（用于 HN / GitHub Trending 这类泛技术源的前置过滤）
_AI_KEYWORDS = (
    "ai", "a.i.", "llm", "gpt", "chatgpt", "claude", "gemini", "llama", "qwen",
    "deepseek", "mistral", "grok", "openai", "anthropic", "huggingface",
    "transformer", "diffusion", "stable diffusion", "midjourney", "sora",
    "neural", "deep learning", "machine learning", "ml", "nlp", "cv",
    "agent", "rag", "embedding", "vector", "fine-tune", "finetune",
    "inference", "quantization", "quantize", "vllm", "cuda", "pytorch",
    "tensorflow", "mcp", "prompt", "multimodal", "text-to-", "image gen",
    "copilot", "cursor", "whisper", "tts", "asr", "ocr", "rlhf",
    "大模型", "模型", "人工智能", "机器学习", "深度学习", "神经网络",
    "智能体", "生成式", "文生图", "文生视频", "多模态", "推理",
)

_TIMEOUT_FALLBACK = 15


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _check_deps() -> None:
    if not _AIOHTTP_OK:
        raise RuntimeError("缺少依赖 aiohttp，请执行：pip install -r requirements.txt")


def _decode(data: bytes) -> str:
    """尽量正确地把响应体解码成文本（兼容中文源常见的 gb18030）。"""
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


def _clean(text: Optional[str], limit: int = 320) -> str:
    """去 HTML 标签、反转义实体、压缩空白并截断。"""
    if not text:
        return ""
    s = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


# 不少源的 RSS 摘要是「点击查看原文」这类占位符，留着只会污染提示词
_JUNK_SUMMARY = re.compile(
    r"^(点击查看原文|查看原文|阅读全文|查看全文|原文链接|详情|更多|全文|read\s*more|"
    r"continue\s*reading|http\S*|>>?|»|→|\.{2,})[>》」』\)）\s]*$",
    re.IGNORECASE,
)


def _clean_summary(text: Optional[str], limit: int = 400) -> str:
    """摘要专用清洗：在通用清洗基础上丢掉无信息量的占位符。"""
    s = _clean(text, limit)
    if not s or len(s) < 8 or _JUNK_SUMMARY.match(s):
        return ""
    return s


def _tag(el) -> str:
    """取标签本地名（去掉命名空间）并转小写。"""
    t = el.tag if isinstance(el.tag, str) else ""
    return t.split("}")[-1].lower()


def _child_text(el, *names: str) -> str:
    """按标签名（不区分命名空间/大小写）取第一个非空子节点文本。"""
    wanted = {n.lower() for n in names}
    for c in list(el):
        if _tag(c) in wanted:
            txt = "".join(c.itertext()).strip()
            if txt:
                return txt
    return ""


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    """兼容 RFC822（RSS pubDate）与 ISO8601（Atom updated）两种时间格式。"""
    if not raw:
        return None
    s = raw.strip()
    try:
        return _to_utc(parsedate_to_datetime(s))
    except Exception:
        pass
    try:
        return _to_utc(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日",
    ):
        try:
            return _to_utc(datetime.strptime(s, fmt))
        except Exception:
            continue
    return None


def _fmt_time(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def _is_ai_related(text: str) -> bool:
    low = (text or "").lower()
    if not low:
        return False
    for kw in _AI_KEYWORDS:
        if kw.isascii():
            # 英文关键词按词边界匹配，避免 "ai" 命中 "said"、"main"
            if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", low):
                return True
        elif kw in low:
            return True
    return False


def _norm_title(t: str) -> str:
    """标题归一化，用于跨源去重（同一事件不同来源的报道合并）。"""
    s = re.sub(r"[\s\-_—–|·:：,，.。!！?？\"'“”‘’()（）\[\]【】]+", "", (t or "").lower())
    return s[:48]


# ---------------------------------------------------------------------------
# Feed 解析（RSS 2.0 / Atom / RDF）
# ---------------------------------------------------------------------------
def _entry_from(el, kind: str) -> Dict:
    title = _clean(_child_text(el, "title"), 200)
    summary = _clean_summary(
        _child_text(el, "description", "summary", "content", "encoded", "content:encoded"),
        400,
    )
    date = None
    link = ""

    if kind == "atom":
        for c in list(el):
            if _tag(c) == "link":
                rel = (c.attrib.get("rel") or "alternate").lower()
                href = c.attrib.get("href") or ""
                if href and (rel == "alternate" or not link):
                    link = href
        date = _parse_date(_child_text(el, "published") or _child_text(el, "updated"))
    else:
        link = _child_text(el, "link", "guid")
        date = _parse_date(_child_text(el, "pubDate", "date", "published", "updated"))

    link = (link or "").strip()
    if link and not link.startswith("http"):
        link = ""
    return {
        "title": title,
        "url": link,
        "summary": summary,
        "_dt": date,
    }


def parse_feed(text: str) -> List[Dict]:
    """解析 RSS / Atom / RDF，返回原始条目列表（未归一化）。"""
    if not text:
        return []
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError:
        # 有些源前面带 BOM 或杂字符，定位到第一个真正的根标签再试一次
        m = re.search(r"<\?xml|<rss|<feed|<rdf:RDF", text)
        if not m:
            return []
        try:
            root = ET.fromstring(text[m.start():])
        except Exception:
            return []

    root_tag = _tag(root)
    entries: List[Dict] = []
    if root_tag == "feed":
        for el in root.iter():
            if _tag(el) == "entry":
                entries.append(_entry_from(el, "atom"))
    else:  # rss / RDF
        for el in root.iter():
            if _tag(el) == "item":
                entries.append(_entry_from(el, "rss"))
    return entries


# ---------------------------------------------------------------------------
# 各类型抓取器
# ---------------------------------------------------------------------------
async def _http_get(session, url: str, timeout: int, retries: int) -> str:
    """带重试的 GET，返回文本。全部失败抛 RuntimeError。"""
    last_err = "unknown"
    for attempt in range(max(0, retries) + 1):
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    last_err = f"HTTP {resp.status}"
                else:
                    return _decode(await resp.read())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            await asyncio.sleep(0.8 * (attempt + 1))
    raise RuntimeError(last_err)


async def _fetch_rss(session, meta: Dict, limit: int, timeout: int, retries: int) -> List[Dict]:
    raw = await _http_get(session, meta["url"], timeout, retries)
    entries = parse_feed(raw)
    out: List[Dict] = []
    for e in entries:
        title = e.get("title") or ""
        if not title:
            continue
        blob = f"{title} {e.get('summary') or ''}"
        if meta.get("ai_filter") and not _is_ai_related(blob):
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


async def _fetch_hf_papers(session, meta: Dict, limit: int, timeout: int, retries: int) -> List[Dict]:
    """HuggingFace 每日论文：JSON API，字段是嵌套结构。"""
    url = f"{meta['url']}?limit={max(1, min(limit, 50))}"
    raw = await _http_get(session, url, timeout, retries)
    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"JSON 解析失败: {e}")
    if not isinstance(data, list):
        return []

    out: List[Dict] = []
    for row in data[:limit]:
        paper = row.get("paper") if isinstance(row, dict) else None
        if not isinstance(paper, dict):
            continue
        title = (paper.get("title") or "").strip()
        if not title:
            continue
        pid = paper.get("id") or ""
        summary = _clean_summary(paper.get("summary") or "", 400)
        date = _parse_date(paper.get("publishedAt"))
        out.append({
            "title": _clean(title, 200),
            "url": f"https://huggingface.co/papers/{pid}" if pid else "",
            "summary": summary,
            "_dt": date,
        })
    return out


# 必须按「单个仓库卡片」为块解析：部分仓库没有描述，
# 若把全页描述按顺序与仓库名配对，会从缺描述的那条开始整体错位。
_TREND_BLOCK = re.compile(
    r'<article[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>', re.DOTALL
)
_TREND_H2 = re.compile(
    r'<h2[^>]*class="[^"]*lh-condensed[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TREND_DESC = re.compile(
    r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>', re.DOTALL
)


async def _fetch_github_trend(session, meta: Dict, limit: int, timeout: int, retries: int) -> List[Dict]:
    """GitHub Trending 是 HTML：逐张卡片解析仓库名与描述，并前置筛掉非 AI 项目。"""
    raw = await _http_get(session, meta["url"], timeout, retries)

    repos: List[Dict] = []
    for block in _TREND_BLOCK.findall(raw):
        m = _TREND_H2.search(block)
        if not m:
            continue
        href = (m.group(1) or "").strip()
        name = _clean(m.group(2), 120)
        name = re.sub(r"\s*/\s*", "/", name).strip()
        if not href.startswith("/") or not name:
            continue
        d = _TREND_DESC.search(block)
        repos.append({
            "title": name,
            "url": f"https://github.com{href}",
            "summary": _clean(d.group(1), 300) if d else "",
            "_dt": None,
        })

    out: List[Dict] = []
    for r in repos:
        if meta.get("ai_filter") and not _is_ai_related(f"{r['title']} {r['summary']}"):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


_FETCHERS = {
    "rss": _fetch_rss,
    "hf_papers": _fetch_hf_papers,
    "github_trend": _fetch_github_trend,
}


async def _fetch_one(session, key: str, limit: int, timeout: int, retries: int) -> List[Dict]:
    meta = SOURCES[key]
    fetcher = _FETCHERS.get(meta["kind"])
    if fetcher is None:
        raise RuntimeError(f"未知信源类型: {meta['kind']}")
    return await fetcher(session, meta, limit, timeout, retries)


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def _normalize(key: str, raw: Dict) -> Dict:
    meta = SOURCES[key]
    dt = raw.get("_dt")
    return {
        "title": raw.get("title") or "",
        "url": raw.get("url") or "",
        "summary": raw.get("summary") or "",
        "source": key,
        "source_label": meta["label"],
        "published": _fmt_time(dt),
        "_ts": dt.timestamp() if dt else 0.0,
    }


def _dedupe(items: List[Dict]) -> List[Dict]:
    """跨信源去重：标题归一化后相同视为同一事件，保留信息更全的那条。"""
    best: Dict[str, Dict] = {}
    for it in items:
        key = _norm_title(it["title"])
        if not key:
            continue
        cur = best.get(key)
        if cur is None:
            best[key] = it
        elif len(it.get("summary") or "") > len(cur.get("summary") or ""):
            best[key] = it
    return list(best.values())


async def gather(
    keys: Optional[List[str]] = None,
    per_source_n: int = 12,
    lookback_hours: int = 36,
    timeout: int = 15,
    retries: int = 2,
) -> Tuple[List[Dict], List[Dict]]:
    """并发抓取多个信源。

    返回 (items, stats)：
        items -- 归一化并去重、按时间倒序的条目列表
        stats -- 每个信源的抓取情况，用于 /日报状态 与失败提示
    """
    _check_deps()

    wanted = [k for k in (keys or DEFAULT_SOURCES) if k in SOURCES]
    stats: List[Dict] = []
    if not wanted:
        return [], stats

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, lookback_hours))
    headers = {
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                  "application/json, text/html;q=0.8, */*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    connector = aiohttp.TCPConnector(limit=8, ssl=False)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        tasks = [
            asyncio.ensure_future(_fetch_one(session, k, per_source_n, timeout, retries))
            for k in wanted
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    items: List[Dict] = []
    for key, res in zip(wanted, results):
        meta = SOURCES[key]
        if isinstance(res, BaseException):
            err = f"{type(res).__name__}: {res}"
            logger.warning(f"[ai_daily] 信源 {meta['label']} 抓取失败: {err}")
            stats.append({"key": key, "label": meta["label"], "ok": False,
                          "n": 0, "raw": 0, "err": err[:160]})
            continue

        # 时间窗口过滤：有明确时间且太旧的丢掉；没时间的保留（很多源不给时间）
        kept = []
        for r in res:
            dt = r.get("_dt")
            if dt is not None and dt < cutoff:
                continue
            item = _normalize(key, r)
            if not item["title"]:
                continue
            kept.append(item)

        items.extend(kept)
        stats.append({"key": key, "label": meta["label"], "ok": True,
                      "n": len(kept), "raw": len(res), "err": ""})

    items = _dedupe(items)
    items.sort(key=lambda x: x["_ts"], reverse=True)
    return items, stats


def source_list() -> List[Dict]:
    """给命令层用的信源清单（含分组与说明）。"""
    return [
        {
            "key": k,
            "label": v["label"],
            "emoji": v["emoji"],
            "group": v["group"],
            "desc": v.get("desc", ""),
        }
        for k, v in SOURCES.items()
    ]


def source_label(key: str) -> str:
    v = SOURCES.get(key)
    return v["label"] if v else key


def resolve_key(text: str) -> Optional[str]:
    """把用户输入（key / 中文名 / 别名）解析成信源 key。"""
    if not text:
        return None
    t = text.strip().lower()
    if t in SOURCES:
        return t
    # 精确匹配展示名（含去空格）
    for k, v in SOURCES.items():
        label = v["label"].lower()
        if t == label or t == label.replace(" ", ""):
            return k
    # 模糊匹配：要求至少 3 个字符，否则 "ai" 会误命中 "openai"
    if len(t) >= 3:
        for k, v in SOURCES.items():
            label = v["label"].lower()
            if t in label or t in k:
                return k
    return None
