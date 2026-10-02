"""AI 日报 · 卡片渲染层

把模型输出的日报文本渲染成一张图片卡片（明暗两套主题）。
没有可用中文字体或 Pillow 缺失时返回 None，由上层回退纯文本推送。

注意：中文字体普遍不含彩色 emoji 字形，直接绘制会变成"豆腐块"，
因此渲染前统一剥离 emoji，只用颜色和字号区分层级。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except Exception:  # pragma: no cover
    _PIL_OK = False

try:
    from astrbot.api import logger
except Exception:  # 允许脱离 AstrBot 独立跑自测
    import logging

    logger = logging.getLogger("astrbot_plugin_ai_daily")


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",     # 黑体
    "C:/Windows/Fonts/simsun.ttc",     # 宋体
    "/System/Library/Fonts/PingFang.ttc",          # macOS 苹方
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",          # Linux 文泉驿
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]
_FONT_CACHE: Dict[int, object] = {}
_FONT_PATH_RESOLVED: Optional[str] = None
_FONT_PATH_CHECKED = False


def _find_font_path() -> Optional[str]:
    global _FONT_PATH_RESOLVED, _FONT_PATH_CHECKED
    if not _FONT_PATH_CHECKED:
        for p in _FONT_CANDIDATES:
            if os.path.exists(p):
                _FONT_PATH_RESOLVED = p
                break
        _FONT_PATH_CHECKED = True
    return _FONT_PATH_RESOLVED


def _get_font(size: int):
    """取指定字号的中文字体（带缓存）。无中文字体时返回 None。"""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    path = _find_font_path()
    if path:
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            font = None
    _FONT_CACHE[size] = font
    return font


def available() -> bool:
    """渲染能力是否可用（Pillow 与中文字体都在）。"""
    if not _PIL_OK:
        return False
    return _find_font_path() is not None


# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------
# 彩色 emoji 在中文 TTF 里没有字形，会渲染成方框，统一剥掉
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
# 装饰性符号（中文字体大多能画，保留但去掉过度的）
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")


def _strip_emoji(s: str) -> str:
    return _EMOJI_RE.sub("", s or "")


def _strip_md(s: str) -> str:
    s = _MD_LINK_RE.sub(r"\1", s or "")
    s = s.replace("`", "").replace("**", "").replace("__", "")
    return s.strip()


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------
_THEMES = {
    "dark": {
        "bg": "#1a1a2e",
        "panel": "#16213e",
        "title": "#ffffff",
        "accent": "#5dc9a5",
        "accent2": "#7aa2f7",
        "text": "#e8e8f0",
        "muted": "#9aa0b5",
        "link": "#6cb6ff",
        "line": "#2b3557",
    },
    "light": {
        "bg": "#f4f6fb",
        "panel": "#ffffff",
        "title": "#1a1a2e",
        "accent": "#0f6e56",
        "accent2": "#2563eb",
        "text": "#242433",
        "muted": "#6b7280",
        "link": "#1d4ed8",
        "line": "#dde3ef",
    },
}


def _pal(theme: str) -> Dict[str, str]:
    return _THEMES.get((theme or "dark").lower(), _THEMES["dark"])


# ---------------------------------------------------------------------------
# 结构化：把日报文本拆成可绘制的行
# ---------------------------------------------------------------------------
def _classify(raw: str) -> Tuple[str, str]:
    """判断一行属于哪种元素，并返回 (kind, text)。"""
    s = (raw or "").strip()
    if not s:
        return ("space", "")

    if s.startswith("#"):
        return ("h", _strip_md(_strip_emoji(s.lstrip("# "))))

    if s.startswith("💡"):
        return ("note", _strip_md(_strip_emoji(s[1:])).lstrip(" ：:").strip())

    if s.startswith("🔗"):
        return ("link", _strip_md(_strip_emoji(s[1:])).strip())

    if s.startswith("http://") or s.startswith("https://"):
        return ("link", s)

    if re.match(r"^[-*•]\s", s):
        return ("li", _strip_md(_strip_emoji(s[2:])).strip())

    if s.startswith("**") and s.rstrip("*").count("**") >= 1:
        return ("h2", _strip_md(_strip_emoji(s).strip()).lstrip("*").strip())

    return ("p", _strip_md(_strip_emoji(s)))


def _wrap(draw, text: str, font, max_w: int) -> List[str]:
    """按可用宽度逐字换行（中文场景足够，避免英文单词被硬切得太碎）。"""
    if not text:
        return [""]
    lines: List[str] = []
    cur = ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
            continue
        if cur:
            lines.append(cur)
        cur = ch
    if cur:
        lines.append(cur)
    return lines


# 每种元素的字号、行距、缩进、段前空白
_STYLE = {
    "h":     {"size": 21, "gap": 14, "indent": 0,  "before": 18, "color": "accent"},
    "h2":    {"size": 17, "gap": 10, "indent": 0,  "before": 10, "color": "title"},
    "p":     {"size": 15, "gap": 9,  "indent": 0,  "before": 4,  "color": "text"},
    "li":    {"size": 15, "gap": 9,  "indent": 16, "before": 3,  "color": "text"},
    "note":  {"size": 15, "gap": 9,  "indent": 16, "before": 2,  "color": "accent2"},
    "link":  {"size": 13, "gap": 8,  "indent": 16, "before": 2,  "color": "link"},
    "space": {"size": 8,  "gap": 4,  "indent": 0,  "before": 0,  "color": "muted"},
}


def render_report(
    text: str,
    subtitle: str = "",
    theme: str = "dark",
    width: int = 900,
    title_text: str = "AI 日报",
) -> Optional[bytes]:
    """把日报文本渲染成 PNG。失败返回 None（调用方回退纯文本）。"""
    if not _PIL_OK or not text:
        return None
    if not available():
        logger.warning("[ai_daily] 未找到可用中文字体，跳过图片渲染，回退纯文本")
        return None

    pal = _pal(theme)
    pad = 34
    content_w = width - pad * 2

    try:
        measure = Image.new("RGB", (16, 16))
        md = ImageDraw.Draw(measure)

        # ---- 头条区 ----
        title_font = _get_font(30)
        sub_font = _get_font(14)
        head_lines = _wrap(md, subtitle, sub_font, content_w) if subtitle else []
        head_h = 30 + 14 + 16 + len(head_lines) * (sub_font.size + 7) + 22

        # ---- 正文行 ----
        rows: List[Dict] = []
        for raw in text.splitlines():
            kind, content = _classify(raw)
            st = _STYLE[kind]
            font = _get_font(st["size"])
            if font is None:
                return None
            lines = _wrap(md, content, font, content_w - st["indent"]) if content else [""]
            rows.append({"kind": kind, "st": st, "font": font, "lines": lines,
                         "color": pal[st["color"]]})

        body_h = sum(r["st"]["before"] + len(r["lines"]) * (r["font"].size + r["st"]["gap"])
                     for r in rows)
        height = pad + head_h + body_h + pad

        img = Image.new("RGB", (width, height), pal["bg"])
        d = ImageDraw.Draw(img)

        # 顶部标题栏（panel 色块）
        d.rectangle([0, 0, width, pad + head_h - 16], fill=pal["panel"])
        d.text((pad, pad - 8), title_text, font=title_font, fill=pal["title"])
        y = pad - 8 + 30 + 10
        if subtitle:
            for ln in head_lines:
                d.text((pad, y), ln, font=sub_font, fill=pal["muted"])
                y += sub_font.size + 7
        d.line([pad, pad + head_h - 16, width - pad, pad + head_h - 16], fill=pal["line"], width=1)

        # 正文
        y = pad + head_h
        for r in rows:
            st = r["st"]
            y += st["before"]
            if r["kind"] == "h":
                # 章节标题配一条强调色竖线
                d.rectangle([pad, y + 3, pad + 3, y + r["font"].size + 3], fill=pal["accent"])
            for i, ln in enumerate(r["lines"]):
                if r["kind"] == "li" and i == 0:
                    # 列表项画圆点，比纯缩进更易读
                    cy = y + r["font"].size // 2
                    d.ellipse([pad + 4, cy - 2, pad + 8, cy + 2], fill=pal["accent"])
                d.text((pad + st["indent"], y), ln, font=r["font"], fill=r["color"])
                y += r["font"].size + st["gap"]

        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.error(f"[ai_daily] 渲染日报卡片失败: {e}")
        return None


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
