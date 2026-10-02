"""AI 日报 · 多源综述 - AstrBot 插件

不做资讯的搬运工：直接从 14 个一手信源采集当天内容，交给大模型通读后
**从零撰写**一份中文日报（合并同事件报道、判断重要性、给出有观点的点评）。

与「订阅别人的 AI 日报再转发」类插件的区别在信源层：
本插件的原料是自己采的，不是别人二手加工过的。

工程惯例：
- 持久化存放在 AstrBot data 目录的插件子目录 state.json（原子写 + 线程锁）
- 全程 try/except，任何异常只记录日志，绝不让 AstrBot 崩溃
- 使用 astrbot 的 logger 记录日志
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register

try:  # 兼容新旧版本的 data 目录获取方式
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover
    StarTools = None  # type: ignore

from . import render, sources

PermissionType = filter.PermissionType

PLUGIN_ID = "astrbot_plugin_ai_daily"
PLUGIN_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
_SYSTEM_BASE = """你是一名资深的 AI 行业编辑，负责把当天散落在各个信源里的资讯，写成一份中文日报，发到技术社群里。

硬性要求：
1. 通读全部候选条目，把报道同一件事的多条合并成一条，保留信息量最大的那次报道。
2. 按重要性排序：官方发布（新模型/新产品/重要政策）> 重大技术突破与论文 > 有实际影响的产品与商业动态 > 社区热议。
3. 淘汰纯营销稿、标题党、与 AI 无关的内容。宁可少写，不要凑数。
4. 每条必须严格基于给定条目，绝不编造原文没有的事实、数字或链接；链接只能从给定条目里原样复制，不得自行拼接。
5. 说人话，不堆术语。禁止使用"值得关注""引发热议""意义重大"这类空话套话。
6. 严格按用户给出的格式输出，不要添加任何开场白、结束语或额外说明。"""

_STYLE_EXTRA = {
    "brief": "\n\n风格：简洁的行业简报。每条只讲清「是什么」和「为什么重要」，不展开背景。",
    "deep": "\n\n风格：深度解读。在点评里多写一到两句分析——这件事对行业格局、开发者或普通用户意味着什么。",
    "humor": "\n\n风格：轻松的吐槽风。保持信息准确的前提下，语气可以俏皮、有梗，但不阴阳怪气、不过度玩梗。",
}

_STYLE_LABEL = {"brief": "简洁简报", "deep": "深度解读", "humor": "轻松吐槽"}


def _build_user_prompt(
    items: List[Dict], must_see_n: int, brief_n: int, date_str: str, src_count: int
) -> str:
    lines: List[str] = []
    for i, it in enumerate(items, 1):
        summary = (it.get("summary") or "").strip()
        if len(summary) > 220:
            summary = summary[:219] + "…"
        lines.append(f"[{i}] {it['title']}")
        lines.append(f"    来源：{it['source_label']} | 时间：{it.get('published') or '未知'}")
        if summary:
            lines.append(f"    摘要：{summary}")
        if it.get("url"):
            lines.append(f"    链接：{it['url']}")
        lines.append("")

    brief_part = ""
    if brief_n > 0:
        brief_part = (
            "\n## 其他速览\n\n"
            f"挑 {max(1, brief_n - 2)}-{brief_n} 条次要但仍有价值的，每条一行：\n"
            "- {标题}（{来源}）→ {链接}\n\n"
        )

    return (
        f"以下是 {date_str} 收集到的 {len(items)} 条 AI 资讯候选"
        f"（来自 {src_count} 个信源，已按发布时间倒序）：\n\n"
        + "\n".join(lines)
        + "\n"
        + "请输出今日 AI 日报，严格使用以下格式（不要改动标题文字，不要加额外说明）：\n\n"
        + "## 今日必看\n\n"
        + f"挑 {must_see_n} 条最重要的，每条三行：\n"
        + "**1. {标题}** —— {一句话说清这是什么、谁做的、有什么变化}\n"
        + "💡 {一句话点评，要有自己的判断，不要复述事实}\n"
        + "🔗 {链接}\n\n"
        + "（第 2、3 条同上，序号递增）\n"
        + brief_part
        + "## 一句话锐评\n\n"
        + "{对今天 AI 圈的一句话总结，要有观点和记忆点，不超过 60 字}\n"
    )


def _fallback_report(items: List[Dict]) -> str:
    """模型不可用时的兜底：至少把条目列表发出去，不让用户什么都收不到。"""
    out = ["## 其他速览", ""]
    for it in items[:20]:
        tail = f"（{it['source_label']}）" if it.get("source_label") else ""
        url = it.get("url") or ""
        out.append(f"- {it['title']}{tail} → {url}" if url else f"- {it['title']}{tail}")
    out.append("")
    out.append("## 一句话锐评")
    out.append("")
    out.append("模型调用失败，以上为原始条目列表（未经过综述加工）。")
    return "\n".join(out)


def _extract_text(resp) -> str:
    """从各版本 AstrBot 的模型返回值里提取纯文本。"""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp.strip()
    for attr in ("completion_text", "result_text", "text"):
        val = getattr(resp, attr, None)
        if val:
            return str(val).strip()
    chain = getattr(resp, "result_chain", None)
    if chain:
        try:
            return "".join(str(getattr(c, "text", "") or "") for c in chain).strip()
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------
@register(
    PLUGIN_ID,
    "xiaowan138",
    "AI 日报 · 多源综述：聚合 14 个信源，让模型写一份独家日报",
    PLUGIN_VERSION,
)
class AiDaily(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = self._resolve_data_dir()
        self.state_file = self.data_dir / "state.json"
        self._state: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self._quiet_prev = False
        self._last_attempt: Dict[str, float] = {}
        # 最近一次日报的素材缓存，供 /日报追问 使用：umo -> {...}
        self._cache: Dict[str, Dict] = {}
        self._load()

    async def initialize(self):
        """插件加载完毕即启动调度器，重启 AstrBot 后定时推送不失效。"""
        self._ensure_task()

    async def terminate(self):
        if self._task:
            self._task.cancel()
        self._save()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _resolve_data_dir(self) -> Path:
        try:
            if StarTools is not None:
                p = Path(StarTools.get_data_dir(PLUGIN_ID))
                p.mkdir(parents=True, exist_ok=True)
                return p
        except Exception:
            pass
        try:
            mod = __import__(
                "astrbot.core.utils.astrbot_path",
                fromlist=["get_astrbot_plugin_data_path"],
            )
            p = Path(mod.get_astrbot_plugin_data_path()) / PLUGIN_ID
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            fallback = Path(__file__).resolve().parent / "data"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _load(self) -> None:
        try:
            if self.state_file.exists():
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            self._normalize(v)
                    self._state = data
                    return
            self._state = {}
        except Exception as e:
            logger.error(f"[ai_daily] 读取状态失败: {e}")
            self._state = {}

    @staticmethod
    def _normalize(st: Dict) -> None:
        st.setdefault("enabled", False)
        st.setdefault("time", "")
        st.setdefault("sources", None)
        st.setdefault("style", "")
        st.setdefault("last_push_date", "")
        st.setdefault("last_push", "")

    def _save(self) -> None:
        """原子写，避免写一半被中断导致配置损坏。"""
        try:
            with self._lock:
                data = json.dumps(self._state, ensure_ascii=False, indent=2)
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.error(f"[ai_daily] 保存状态失败: {e}")

    def _entry(self, umo: str) -> Dict:
        st = self._state.get(umo)
        if st is None:
            st = {"umo": umo}
            self._normalize(st)
            self._state[umo] = st
        return st

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default):
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    def _cfg_int(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(self._cfg(key, default))))
        except Exception:
            return default

    def _cfg_sources(self) -> List[str]:
        raw = self._cfg("enabled_sources", sources.DEFAULT_SOURCES)
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        if not isinstance(raw, (list, tuple)):
            raw = sources.DEFAULT_SOURCES
        valid = [k for k in raw if k in sources.SOURCES]
        return valid or list(sources.DEFAULT_SOURCES)

    def _cfg_push_time(self) -> str:
        t = str(self._cfg("push_time", "09:00")).strip()
        return t if self._valid_hhmm(t) else "09:00"

    @staticmethod
    def _valid_hhmm(s: str) -> bool:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", (s or "").strip())
        if not m:
            return False
        h, mi = int(m.group(1)), int(m.group(2))
        return 0 <= h <= 23 and 0 <= mi <= 59

    @staticmethod
    def _norm_hhmm(s: str) -> str:
        """归一化 HH:MM，格式或数值越界都返回空串。"""
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", (s or "").strip())
        if not m:
            return ""
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return ""
        return f"{h:02d}:{mi:02d}"

    def _quiet_hours(self) -> str:
        return str(self._cfg("quiet_hours", "") or "").strip()

    def _in_quiet(self, now: datetime) -> bool:
        q = self._quiet_hours()
        if not q or "-" not in q:
            return False
        try:
            a, b = q.split("-", 1)
            st = self._norm_hhmm(a.strip())
            et = self._norm_hhmm(b.strip())
            if not st or not et:
                return False
            sh, sm = (int(x) for x in st.split(":"))
            eh, em = (int(x) for x in et.split(":"))
        except Exception:
            return False
        cur, s, e = now.hour * 60 + now.minute, sh * 60 + sm, eh * 60 + em
        if s == e:
            return False
        return (s <= cur < e) if s < e else (cur >= s or cur < e)

    # ------------------------------------------------------------------
    # 模型调用
    # ------------------------------------------------------------------
    async def _chat_provider_id(self, umo: str) -> str:
        try:
            pid = await self.context.get_current_chat_provider_id(umo=umo)
            return pid or ""
        except Exception:
            return ""

    async def _llm(self, umo: str, system: str, user: str) -> Optional[str]:
        """调用模型生成文本。所有路径都失败时返回 None，由调用方降级。"""
        retries = self._cfg_int("max_retry_llm", 1, 0, 3)

        # 路径一：llm_generate（需指定模型商）
        provider_id = str(self._cfg("llm_provider_id", "") or "").strip()
        if not provider_id:
            provider_id = await self._chat_provider_id(umo)
        if provider_id:
            for attempt in range(retries + 1):
                try:
                    resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=user,
                        system_prompt=system,
                    )
                    text = _extract_text(resp)
                    if text:
                        return text
                    logger.warning(f"[ai_daily] 模型返回空内容（第 {attempt + 1} 次）")
                except Exception as e:
                    logger.warning(f"[ai_daily] llm_generate 失败（第 {attempt + 1} 次）: {e}")
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        else:
            logger.warning(f"[ai_daily] 会话 {umo} 没有可用的模型商，尝试全局默认模型")

        # 路径二：全局当前启用的模型商
        try:
            provider = self.context.get_using_provider()
            if provider:
                resp = None
                try:
                    from astrbot.api.provider import ProviderRequest

                    req = ProviderRequest(prompt=user, contexts=[], system_prompt=system)
                    resp = await provider.text_chat(req)
                except ImportError:
                    resp = None
                except TypeError:
                    resp = None
                except Exception as e:
                    logger.warning(f"[ai_daily] text_chat 调用失败: {e}")
                if resp is None:
                    for kwargs in (
                        {"prompt": user, "contexts": [], "system_prompt": system},
                        {"prompt": user, "system_prompt": system},
                        {"prompt": user},
                    ):
                        try:
                            resp = await provider.text_chat(**kwargs)
                            break
                        except TypeError:
                            continue
                        except Exception as e:
                            logger.warning(f"[ai_daily] text_chat 回退失败: {e}")
                            break
                text = _extract_text(resp)
                if text:
                    return text
        except Exception as e:
            logger.warning(f"[ai_daily] 全局模型调用失败: {e}")

        return None

    # ------------------------------------------------------------------
    # 核心：生成日报
    # ------------------------------------------------------------------
    async def _generate(
        self, umo: str, source_keys: List[str], style: str
    ) -> Tuple[Optional[str], List[Dict], List[Dict], str]:
        """抓取 + 综述。返回 (日报文本, 条目, 抓取统计, 错误信息)。"""
        per_source_n = self._cfg_int("per_source_n", 12, 1, 40)
        lookback = self._cfg_int("lookback_hours", 36, 1, 24 * 14)
        timeout = self._cfg_int("fetch_timeout", 15, 5, 60)
        retries = self._cfg_int("retry_times", 2, 0, 5)
        max_candidates = self._cfg_int("max_candidates", 60, 10, 150)

        try:
            items, stats = await sources.gather(
                keys=source_keys,
                per_source_n=per_source_n,
                lookback_hours=lookback,
                timeout=timeout,
                retries=retries,
            )
        except Exception as e:
            logger.error(f"[ai_daily] 抓取失败: {e}")
            return None, [], [], f"抓取信源失败：{e}"

        if not items:
            ok_src = [s for s in stats if s.get("ok")]
            detail = "、".join(s["label"] for s in ok_src[:6]) or "无"
            return None, [], stats, (
                f"所有信源都没有抓到符合时间窗口的内容（成功连通：{detail}）。\n"
                "可尝试调大「内容时间窗口」或更换信源。"
            )

        items = items[:max_candidates]
        src_count = len({it["source"] for it in items})

        must_see_n = self._cfg_int("must_see_n", 3, 1, 6)
        brief_n = self._cfg_int("brief_n", 6, 0, 12)
        style = style if style in _STYLE_EXTRA else "brief"
        system = _SYSTEM_BASE + _STYLE_EXTRA[style]
        user = _build_user_prompt(
            items, must_see_n, brief_n, datetime.now().strftime("%Y年%m月%d日"), src_count
        )

        report = await self._llm(umo, system, user)
        if not report:
            return _fallback_report(items), items, stats, ""

        return report.strip(), items, stats, ""

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------
    @staticmethod
    def _img_comp(img_bytes: bytes) -> Image:
        """图片字节转 AstrBot Image 组件（需要 base64:// 前缀）。"""
        return Image(file="base64://" + base64.b64encode(img_bytes).decode("utf-8"))

    def _subtitle(self, items: List[Dict], stats: List[Dict]) -> str:
        src_ok = len([s for s in stats if s.get("ok") and s.get("n")])
        return (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')} · "
            f"{src_ok} 个信源 · {len(items)} 条素材 · 由 AI 综述生成"
        )

    def _remember(self, umo: str, report: str, items: List[Dict]) -> None:
        """缓存最近一次日报的正文与素材，供 /日报追问 使用。"""
        self._cache[umo] = {
            "report": report,
            "items": items,
            "ts": datetime.now().timestamp(),
        }

    async def _push_text(self, umo: str, report: str, items: List[Dict], stats: List[Dict]) -> bool:
        img = None
        if bool(self._cfg("render_image", True)):
            img = render.render_report(
                report,
                subtitle=self._subtitle(items, stats),
                theme=str(self._cfg("theme", "dark")),
            )
        try:
            if img:
                await self.context.send_message(umo, MessageChain([self._img_comp(img)]))
            else:
                await self.context.send_message(umo, MessageChain().message(report))
            self._remember(umo, report, items)
            return True
        except Exception as e:
            logger.error(f"[ai_daily] 推送失败: {e}")
            return False

    async def _do_push(self, umo: str, st: Dict, style_override: str = "") -> bool:
        """生成并推送，成功返回 True。"""
        keys = st.get("sources") or self._cfg_sources()
        style = style_override or st.get("style") or str(self._cfg("style", "brief"))
        report, items, stats, err = await self._generate(umo, keys, style)
        if err:
            logger.warning(f"[ai_daily] {umo} 生成失败: {err}")
            return False
        if not report:
            return False
        return await self._push_text(umo, report, items, stats)

    # ------------------------------------------------------------------
    # 指令工具
    # ------------------------------------------------------------------
    @staticmethod
    def _strip_cmd(event: AstrMessageEvent, *names: str) -> str:
        pattern = r"^[/！!]?\s*(?:" + "|".join(re.escape(n) for n in names) + r")\s*"
        return re.sub(pattern, "", event.message_str.strip()).strip()

    def _session_sources(self, st: Dict) -> List[str]:
        keys = st.get("sources")
        if not isinstance(keys, list) or not keys:
            keys = self._cfg_sources()
        return [k for k in keys if k in sources.SOURCES]

    def _help_text(self) -> str:
        return (
            "📰 AI 日报 · 多源综述\n"
            "直接采信源、让模型写日报（不是转发别人的日报）\n"
            "\n"
            "【常用】\n"
            "  /日报              立即生成并推送一份今日 AI 日报\n"
            "  /日报订阅          本会话每天自动推送\n"
            "  /日报退订          关闭本会话自动推送\n"
            "  /日报追问 <问题>   就今天日报的内容继续追问\n"
            "\n"
            "【配置】\n"
            "  /日报源            查看信源开关状态\n"
            "  /日报源 开启 <源>  开启某个信源（管理员）\n"
            "  /日报源 关闭 <源>  关闭某个信源（管理员）\n"
            "  /日报时间 08:30    设置本会话推送时间（管理员）\n"
            "  /日报风格 deep     切换综述风格：brief/deep/humor\n"
            "\n"
            "【其他】\n"
            "  /日报状态          查看当前配置与最近推送\n"
            "  /日报帮助          显示本说明"
        )

    # ------------------------------------------------------------------
    # 指令：日报（主入口）
    # ------------------------------------------------------------------
    @filter.command("日报", alias={"ainews", "ai日报", "今日ai", "aidaily"})
    async def cmd_daily(self, event: AstrMessageEvent):
        """生成并推送今日 AI 日报。"""
        arg = self._strip_cmd(event, "日报", "ainews", "ai日报", "今日ai", "aidaily")

        # 允许 /日报 订阅、/日报 状态 之类的简写
        if arg in ("订阅", "开启"):
            async for r in self.cmd_subscribe(event):
                yield r
            return
        if arg in ("退订", "取消", "关闭"):
            async for r in self.cmd_unsubscribe(event):
                yield r
            return
        if arg in ("状态", "status"):
            async for r in self.cmd_status(event):
                yield r
            return
        if arg in ("帮助", "help", "说明", "?"):
            yield event.plain_result(self._help_text())
            return
        # 简写分发：把剩余参数原样传给对应的处理逻辑
        # （不能直接调命令方法，因为它们是按完整消息串重新解析参数的）
        if arg.startswith("源"):
            async for r in self._logic_sources(event, arg[1:].strip()):
                yield r
            return
        if arg.startswith("时间"):
            async for r in self._logic_time(event, arg[2:].strip()):
                yield r
            return
        if arg.startswith("风格"):
            async for r in self._logic_style(event, arg[2:].strip()):
                yield r
            return
        if arg.startswith("追问"):
            async for r in self._logic_ask(event, arg[2:].strip()):
                yield r
            return

        umo = event.unified_msg_origin
        st = self._entry(umo)

        yield event.plain_result("⏳ 正在采集信源并生成日报，请稍候（通常 10-30 秒）…")

        report, items, stats, err = await self._generate(
            umo, self._session_sources(st), st.get("style") or str(self._cfg("style", "brief"))
        )
        if err:
            yield event.plain_result(f"⚠️ {err}")
            return
        if not report:
            yield event.plain_result("⚠️ 日报生成失败，请稍后重试。")
            return

        self._remember(umo, report, items)

        img = None
        if bool(self._cfg("render_image", True)):
            img = render.render_report(
                report, subtitle=self._subtitle(items, stats), theme=str(self._cfg("theme", "dark"))
            )
        if img:
            yield event.chain_result([self._img_comp(img)])
        else:
            yield event.plain_result(report)

    # ------------------------------------------------------------------
    # 指令：订阅 / 退订
    # ------------------------------------------------------------------
    @filter.command("日报订阅", alias={"订阅日报"})
    async def cmd_subscribe(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        st = self._entry(umo)
        if st.get("enabled"):
            yield event.plain_result(
                f"✅ 本会话已经订阅了，每天 {st.get('time') or self._cfg_push_time()} 自动推送。"
            )
            return
        max_subs = self._cfg_int("max_subs", 50, 1, 10000)
        active = len([1 for v in self._state.values() if isinstance(v, dict) and v.get("enabled")])
        if active >= max_subs:
            yield event.plain_result(f"⚠️ 订阅数已达上限（{max_subs}），请联系管理员调整。")
            return

        st["enabled"] = True
        self._save()
        keys = self._session_sources(st)
        yield event.plain_result(
            f"✅ 订阅成功！\n"
            f"推送时间：每天 {st.get('time') or self._cfg_push_time()}\n"
            f"启用信源：{len(keys)} 个\n"
            f"可发送 /日报 立即生成一份，或 /日报源 调整信源。"
        )

    @filter.command("日报退订", alias={"退订日报"})
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        st = self._entry(event.unified_msg_origin)
        if not st.get("enabled"):
            yield event.plain_result("本会话本来就没有订阅。发送 /日报订阅 可开启。")
            return
        st["enabled"] = False
        self._save()
        yield event.plain_result("已退订，本会话不再自动推送。发送 /日报订阅 可重新开启。")

    # ------------------------------------------------------------------
    # 指令：时间 / 风格 / 信源
    # ------------------------------------------------------------------
    @filter.command("日报时间")
    @filter.permission_type(PermissionType.ADMIN)
    async def cmd_time(self, event: AstrMessageEvent):
        arg = self._strip_cmd(event, "日报时间", "时间").strip()
        async for r in self._logic_time(event, arg):
            yield r

    async def _logic_time(self, event: AstrMessageEvent, arg: str):
        st = self._entry(event.unified_msg_origin)
        if not arg:
            cur = st.get("time") or self._cfg_push_time()
            src = "本会话单独设置" if st.get("time") else "全局默认"
            yield event.plain_result(f"⏰ 当前推送时间：{cur}（{src}）\n修改：/日报时间 08:30")
            return
        t = self._norm_hhmm(arg)
        if not t:
            yield event.plain_result("格式不对，请用 24 小时制 HH:MM，例如 /日报时间 08:30")
            return
        st["time"] = t
        self._save()
        yield event.plain_result(f"✅ 本会话推送时间已设为每天 {t}。")

    @filter.command("日报风格")
    async def cmd_style(self, event: AstrMessageEvent):
        arg = self._strip_cmd(event, "日报风格", "风格").strip().lower()
        async for r in self._logic_style(event, arg):
            yield r

    async def _logic_style(self, event: AstrMessageEvent, arg: str):
        arg = (arg or "").strip().lower()
        st = self._entry(event.unified_msg_origin)
        if not arg:
            cur = st.get("style") or str(self._cfg("style", "brief"))
            opts = "、".join(f"{k}（{v}）" for k, v in _STYLE_LABEL.items())
            yield event.plain_result(
                f"✍️ 当前风格：{cur}（{_STYLE_LABEL.get(cur, cur)}）\n可选：{opts}\n修改：/日报风格 deep"
            )
            return
        alias = {"简报": "brief", "简洁": "brief", "深度": "deep", "幽默": "humor", "吐槽": "humor"}
        arg = alias.get(arg, arg)
        if arg not in _STYLE_EXTRA:
            yield event.plain_result("可选风格：brief（简洁简报）/ deep（深度解读）/ humor（轻松吐槽）")
            return
        st["style"] = arg
        self._save()
        yield event.plain_result(f"✅ 本会话日报风格已切换为：{_STYLE_LABEL[arg]}")

    @filter.command("日报源", alias={"日报信源"})
    async def cmd_sources(self, event: AstrMessageEvent):
        arg = self._strip_cmd(event, "日报源", "日报信源", "源").strip()
        async for r in self._logic_sources(event, arg):
            yield r

    async def _logic_sources(self, event: AstrMessageEvent, arg: str):
        umo = event.unified_msg_origin
        st = self._entry(umo)
        keys = self._session_sources(st)

        if not arg:
            groups: Dict[str, List[str]] = {}
            for s in sources.source_list():
                mark = "✅" if s["key"] in keys else "⬜"
                groups.setdefault(s["group"], []).append(f"{mark} {s['emoji']} {s['label']}  [{s['key']}]")
            lines = [f"📡 本会话启用 {len(keys)}/{len(sources.SOURCES)} 个信源", ""]
            for g, rows in groups.items():
                lines.append(f"【{g}】")
                lines.extend("  " + r for r in rows)
                lines.append("")
            lines.append("开启：/日报源 开启 量子位　　关闭：/日报源 关闭 hn")
            yield event.plain_result("\n".join(lines))
            return

        # 开启 / 关闭 需要管理员
        m = re.match(r"^(开启|打开|启用|关闭|禁用|停用)\s*(.+)$", arg)
        if not m:
            yield event.plain_result("用法：/日报源 开启 <信源> 或 /日报源 关闭 <信源>\n查看全部：/日报源")
            return
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 修改信源需要管理员权限。")
            return

        action, target = m.group(1), m.group(2).strip()
        key = sources.resolve_key(target)
        if not key:
            yield event.plain_result(f"❓ 没认出来「{target}」是哪个信源。发送 /日报源 查看全部。")
            return

        turn_on = action in ("开启", "打开", "启用")
        cur = list(keys)
        if turn_on:
            if key in cur:
                yield event.plain_result(f"「{sources.source_label(key)}」本来就是开启的。")
                return
            cur.append(key)
        else:
            if key not in cur:
                yield event.plain_result(f"「{sources.source_label(key)}」本来就是关闭的。")
                return
            if len(cur) <= 1:
                yield event.plain_result("⚠️ 至少要保留一个信源，否则日报没有内容可写。")
                return
            cur.remove(key)

        # 按注册顺序整理，展示更稳定
        order = {k: i for i, k in enumerate(sources.SOURCES)}
        cur.sort(key=lambda x: order.get(x, 999))
        st["sources"] = cur
        self._save()
        yield event.plain_result(
            f"✅ 已{'开启' if turn_on else '关闭'}「{sources.source_label(key)}」，"
            f"当前启用 {len(cur)} 个信源。"
        )

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """机器人管理员 / 群管理员（含群主）均可改本会话配置。"""
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        sender = getattr(event.message_obj, "sender", None)
        return str(getattr(sender, "role", "") or "").lower() in ("admin", "owner")

    # ------------------------------------------------------------------
    # 指令：状态 / 帮助
    # ------------------------------------------------------------------
    @filter.command("日报状态", alias={"日报信息"})
    async def cmd_status(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        st = self._entry(umo)
        keys = self._session_sources(st)
        keys_disp = "、".join(sources.source_label(k) for k in keys)
        if len(keys_disp) > 160:
            keys_disp = keys_disp[:159] + "…"
        enabled = "已订阅" if st.get("enabled") else "未订阅"
        style = st.get("style") or str(self._cfg("style", "brief"))
        yield event.plain_result(
            "📊 AI 日报 · 当前状态\n"
            f"  推送状态：{enabled}\n"
            f"  推送时间：{st.get('time') or self._cfg_push_time()}（{'本会话' if st.get('time') else '全局默认'}）\n"
            f"  综述风格：{style}（{_STYLE_LABEL.get(style, style)}）\n"
            f"  启用信源：{len(keys)} 个 —— {keys_disp}\n"
            f"  上次推送：{st.get('last_push') or '（无记录）'}\n"
            f"  时间窗口：最近 {self._cfg_int('lookback_hours', 36, 1, 336)} 小时\n"
            f"  静默时段：{self._quiet_hours() or '未启用'}"
        )

    @filter.command("日报帮助", alias={"日报说明"})
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._help_text())

    # ------------------------------------------------------------------
    # 指令：追问
    # ------------------------------------------------------------------
    @filter.command("日报追问", alias={"追问日报"})
    async def cmd_ask(self, event: AstrMessageEvent):
        """带着当天日报的素材继续追问。"""
        arg = self._strip_cmd(event, "日报追问", "追问日报", "追问").strip()
        async for r in self._logic_ask(event, arg):
            yield r

    async def _logic_ask(self, event: AstrMessageEvent, arg: str):
        umo = event.unified_msg_origin
        if not arg:
            yield event.plain_result("用法：/日报追问 <你的问题>\n例如：/日报追问 那个 GLM 跑在笔记本上是怎么回事")
            return

        cached = self._cache.get(umo)
        if not cached:
            yield event.plain_result("还没有生成过日报。先发送 /日报 生成一份，再追问。")
            return

        items = cached.get("items") or []
        lines = []
        for i, it in enumerate(items[:60], 1):
            s = (it.get("summary") or "")[:200]
            lines.append(f"[{i}] {it['title']}（{it['source_label']}）{('｜' + s) if s else ''}")
        context = "\n".join(lines)

        system = (
            "你是一名 AI 行业分析师。用户正在阅读今天的一份 AI 日报，"
            "并就其中的内容向你提问。请**只依据下面提供的素材**回答，"
            "素材里没有的信息要明确说「今天的素材里没有提到」，绝不编造。"
            "回答简洁、说人话，必要时给出处（信源名）。"
        )
        user = (
            f"今日素材：\n{context}\n\n"
            f"今日日报正文：\n{cached.get('report', '')[:2500]}\n\n"
            f"用户的问题：{arg}\n"
        )

        yield event.plain_result("🤔 正在翻今天的素材…")
        answer = await self._llm(umo, system, user)
        if not answer:
            yield event.plain_result("⚠️ 模型调用失败，稍后再试试。")
            return
        yield event.plain_result(answer)

    # ------------------------------------------------------------------
    # 调度器
    # ------------------------------------------------------------------
    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._scheduler())
            except RuntimeError:
                logger.warning("[ai_daily] 事件循环未就绪，调度器将在下次推送时启动")

    def _is_due(self, st: Dict, hhmm: str, today: str, quiet_exit: bool) -> bool:
        """判断某个会话此刻是否该推送。抽出来便于单测。"""
        if st.get("last_push_date") == today:
            return False
        t = st.get("time") or self._cfg_push_time()
        if t == hhmm:
            return True
        # 静默时段结束后的补发
        return bool(quiet_exit and self._cfg("quiet_catchup", True) and t < hhmm)

    async def _scheduler(self):
        while True:
            try:
                await asyncio.sleep(30)
                now = datetime.now()
                hhmm = now.strftime("%H:%M")
                today = now.strftime("%Y-%m-%d")
                in_quiet = self._in_quiet(now)
                quiet_exit = self._quiet_prev and not in_quiet
                self._quiet_prev = in_quiet
                if in_quiet:
                    continue

                with self._lock:
                    entries = [(k, v) for k, v in self._state.items()
                               if isinstance(v, dict) and v.get("enabled")]

                for umo, st in entries:
                    try:
                        if not self._is_due(st, hhmm, today, quiet_exit):
                            continue
                        # 失败重试限流：同一会话 10 分钟内只尝试一次，避免反复打接口
                        last = self._last_attempt.get(umo, 0.0)
                        if now.timestamp() - last < 600:
                            continue
                        self._last_attempt[umo] = now.timestamp()

                        ok = await self._do_push(umo, st)
                        if ok:
                            st["last_push_date"] = today
                            st["last_push"] = now.strftime("%Y-%m-%d %H:%M")
                            self._save()
                            logger.info(f"[ai_daily] 已向 {umo} 推送日报")
                        else:
                            logger.warning(f"[ai_daily] 向 {umo} 推送失败，稍后重试")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(f"[ai_daily] 处理 {umo} 时异常: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[ai_daily] 调度异常: {e}")
