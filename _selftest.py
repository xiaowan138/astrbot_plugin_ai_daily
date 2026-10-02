"""astrbot_plugin_ai_daily 离线冒烟测试。

stub 掉 astrbot.* 模块，在不启动 AstrBot、不联网的情况下验证：
- 模块可导入、装饰器包裹无异常
- 配置解析（时间格式、静默时段）正确
- 信源注册表与名称解析正确
- 提示词拼装与模型失败兜底
- 全部指令的完整流程（订阅 / 状态 / 信源开关 / 时间 / 追问）
- 推送、持久化回读、调度 due 判定
- 卡片渲染的 emoji 剥离与兜底
"""

import asyncio
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent          # astrbot_plugin_ai_daily/
PARENT = ROOT.parent                  # 让 astrbot_plugin_ai_daily 可被当作包导入
sys.path.insert(0, str(PARENT))

_DATA_ROOT = Path(tempfile.mkdtemp(prefix="aidaily_test_"))

# ---------------------------------------------------------------------------
# astrbot 模块 stub
# ---------------------------------------------------------------------------
astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")


class _Logger:
    def __getattr__(self, name):
        return lambda *a, **k: None


class AstrBotConfig(dict):
    pass


api_mod.logger = _Logger()
api_mod.AstrBotConfig = AstrBotConfig

event_mod = types.ModuleType("astrbot.api.event")


class AstrMessageEvent:
    pass


class MessageChain:
    def __init__(self, comps=None):
        self.comps = list(comps or [])
        self.text = ""

    def message(self, text):
        self.text = text
        return self


class _Filter:
    class PermissionType:
        ADMIN = "admin"

    def command(self, *a, **k):
        return lambda fn: fn

    def permission_type(self, *a, **k):
        return lambda fn: fn

    def event_message_type(self, *a, **k):
        return lambda fn: fn


event_mod.AstrMessageEvent = AstrMessageEvent
event_mod.MessageChain = MessageChain
event_mod.filter = _Filter()

star_mod = types.ModuleType("astrbot.api.star")


class Context:
    pass


class Star:
    def __init__(self, context=None):
        self.context = context


class StarTools:
    @staticmethod
    def get_data_dir(name):
        p = _DATA_ROOT / name
        p.mkdir(parents=True, exist_ok=True)
        return p


def register(*a, **k):
    return lambda cls: cls


star_mod.Context = Context
star_mod.Star = Star
star_mod.StarTools = StarTools
star_mod.register = register

mc_mod = types.ModuleType("astrbot.api.message_components")


class Image:
    def __init__(self, file=""):
        self.file = file


mc_mod.Image = Image

provider_mod = types.ModuleType("astrbot.api.provider")


class ProviderRequest:
    def __init__(self, **k):
        self.__dict__.update(k)


provider_mod.ProviderRequest = ProviderRequest

astrbot.api = api_mod
astrbot.api.event = event_mod
astrbot.api.star = star_mod
astrbot.api.message_components = mc_mod
astrbot.api.provider = provider_mod
for _n, _m in [
    ("astrbot", astrbot), ("astrbot.api", api_mod), ("astrbot.api.event", event_mod),
    ("astrbot.api.star", star_mod), ("astrbot.api.message_components", mc_mod),
    ("astrbot.api.provider", provider_mod),
]:
    sys.modules[_n] = _m

# ---------------------------------------------------------------------------
# 导入插件
# ---------------------------------------------------------------------------
import astrbot_plugin_ai_daily.main as plugin_mod  # noqa: E402
import astrbot_plugin_ai_daily.render as render_mod  # noqa: E402
import astrbot_plugin_ai_daily.sources as sources_mod  # noqa: E402

AiDaily = plugin_mod.AiDaily

PASS, FAIL = 0, 0
FEED = [
    {
        "title": "OpenAI 发布新推理模型 o5", "url": "https://openai.com/news/a",
        "summary": "官方称 AIME 从 78% 提升到 94%", "source": "openai",
        "source_label": "OpenAI", "published": "2026-09-27 08:00", "_ts": 1.8e9,
    },
    {
        "title": "谷歌 TPU 跑 Kimi 比英伟达快 57%", "url": "https://qbitai.com/b",
        "summary": "用的还是 DeepSeek 推理框架", "source": "qbitai",
        "source_label": "量子位", "published": "2026-09-27 07:30", "_ts": 1.7e9,
    },
]

REPLY = (
    "## 今日必看\n\n**1. OpenAI 发布 o5** —— 推理成本降了三分之二。\n"
    "💡 重点不是分数而是成本。\n🔗 https://openai.com/news/a\n\n"
    "## 其他速览\n\n- 谷歌 TPU 跑 Kimi 快 57%（量子位）→ https://qbitai.com/b\n\n"
    "## 一句话锐评\n\n今天的主线是降本。"
)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class FakeSender:
    def __init__(self, role="member"):
        self.role = role


class FakeMessageObj:
    def __init__(self, role="member"):
        self.sender = FakeSender(role)


class FakeEvent:
    def __init__(self, text, umo="group_100", admin=True):
        self.message_str = text
        self.unified_msg_origin = umo
        self.message_obj = FakeMessageObj("admin" if admin else "member")
        self._admin = admin

    def is_admin(self):
        return self._admin

    def plain_result(self, t):
        return ("plain", t)

    def chain_result(self, comps):
        return ("chain", comps)


class FakeContext:
    def __init__(self):
        self.sent = []
        self.fail_llm = False
        self.calls = 0

    async def get_current_chat_provider_id(self, umo=""):
        return "fake_provider"

    async def llm_generate(self, chat_provider_id=None, prompt=None, system_prompt=None):
        self.calls += 1
        if self.fail_llm:
            raise RuntimeError("mock llm failure")
        return types.SimpleNamespace(completion_text=REPLY)

    def get_using_provider(self):
        return None

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


async def drain(agen):
    out = []
    async for x in agen:
        out.append(x)
    return out


def join(results):
    parts = []
    for kind, payload in results:
        if kind == "plain":
            parts.append(payload)
        else:
            parts.append("[图片]")
    return "\n".join(parts)


def make_plugin(cfg=None):
    ctx = FakeContext()
    conf = AstrBotConfig(cfg or {})
    return AiDaily(ctx, conf), ctx


def fixed_gather(items=None, stats=None):
    async def _g(**kwargs):
        return (items if items is not None else FEED,
                stats if stats is not None else [
                    {"key": s, "label": sources_mod.source_label(s), "ok": True, "n": 1,
                     "raw": 1, "err": ""} for s in sources_mod.DEFAULT_SOURCES
                ])
    return _g


async def with_stub_gather(fn, items=None, stats=None):
    orig = sources_mod.gather
    sources_mod.gather = fixed_gather(items, stats)
    try:
        return await fn()
    finally:
        sources_mod.gather = orig


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
async def test_config_utils():
    print("[配置工具]")
    p, _ = make_plugin({"push_time": "08:30", "quiet_hours": "23:00-07:00"})
    check("HH:MM 校验-合法", p._valid_hhmm("09:00") and p._valid_hhmm("23:59"))
    check("HH:MM 校验-非法", not p._valid_hhmm("24:00") and not p._valid_hhmm("9:0") and not p._valid_hhmm(""))
    check("HH:MM 归一化", p._norm_hhmm("8:30") == "08:30" and p._norm_hhmm("坏") == "")
    p2, _ = make_plugin({"push_time": "乱填"})
    check("推送时间非法时回落默认", p2._cfg_push_time() == "09:00")
    p3, _ = make_plugin({})
    check("默认启用信源有效", all(k in sources_mod.SOURCES for k in p3._cfg_sources()))
    p4, _ = make_plugin({"enabled_sources": ["openai", "不存在的源"]})
    check("非法信源被过滤", p4._cfg_sources() == ["openai"])
    p5, _ = make_plugin({"enabled_sources": []})
    check("空信源回落默认集合", p5._cfg_sources() == list(sources_mod.DEFAULT_SOURCES))
    p6, _ = make_plugin({"lookback_hours": "不是数字"})
    check("数字配置非法时回落", p6._cfg_int("lookback_hours", 36, 1, 336) == 36)


async def test_quiet_hours():
    print("[静默时段]")
    p, _ = make_plugin({"quiet_hours": "23:00-07:00"})
    check("跨零点-凌晨在静默内", p._in_quiet(datetime(2026, 9, 27, 2, 0)))
    check("跨零点-正午不在静默内", not p._in_quiet(datetime(2026, 9, 27, 12, 0)))
    check("跨零点-边界起点在静默内", p._in_quiet(datetime(2026, 9, 27, 23, 0)))
    check("跨零点-边界终点不在静默内", not p._in_quiet(datetime(2026, 9, 27, 7, 0)))
    p2, _ = make_plugin({"quiet_hours": "01:00-06:00"})
    check("同日区间命中", p2._in_quiet(datetime(2026, 9, 27, 3, 0)))
    check("同日区间未命中", not p2._in_quiet(datetime(2026, 9, 27, 8, 0)))
    p3, _ = make_plugin({"quiet_hours": ""})
    check("留空表示不启用", not p3._in_quiet(datetime(2026, 9, 27, 3, 0)))
    p4, _ = make_plugin({"quiet_hours": "坏格式"})
    check("坏格式不崩且视为未启用", not p4._in_quiet(datetime(2026, 9, 27, 3, 0)))


async def test_sources():
    print("[信源注册表]")
    check("信源数量为 14", len(sources_mod.SOURCES) == 14, f"实际 {len(sources_mod.SOURCES)}")
    check("默认集合全部有效", all(k in sources_mod.SOURCES for k in sources_mod.DEFAULT_SOURCES))
    check("resolve_key-精确 key", sources_mod.resolve_key("openai") == "openai")
    check("resolve_key-忽略大小写", sources_mod.resolve_key("OpenAI") == "openai")
    check("resolve_key-中文名", sources_mod.resolve_key("量子位") == "qbitai")
    check("resolve_key-含空格展示名", sources_mod.resolve_key("TechCrunch AI") == "techcrunch")
    check("resolve_key-短词不误命中", sources_mod.resolve_key("ai") is None)
    check("resolve_key-不存在返回 None", sources_mod.resolve_key("不存在") is None)
    lst = sources_mod.source_list()
    check("source_list 字段完整", lst and all({"key", "label", "emoji", "group"} <= set(x) for x in lst))
    check("source_label 未知 key 回退原值", sources_mod.source_label("nope") == "nope")
    # 解析健壮性
    check("空文本解析不抛异常", sources_mod.parse_feed("") == [])
    check("非 XML 不抛异常", sources_mod.parse_feed("这根本不是 xml") == [])
    check("标准 RSS 可解析", len(sources_mod.parse_feed(
        "<rss><channel><item><title>A</title><link>https://x.com/a</link></item></channel></rss>")) == 1)
    check("BOM 前缀 RSS 可解析", len(sources_mod.parse_feed(
        "\ufeff<?xml version='1.0'?><rss><channel><item><title>B</title></item></channel></rss>")) == 1)
    check("Atom 可解析", len(sources_mod.parse_feed(
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>C</title>'
        '<link rel="alternate" href="https://x.com/c"/><updated>2026-09-27T01:00:00Z</updated>'
        '</entry></feed>')) == 1)
    check("AI 关键词过滤-命中", sources_mod._is_ai_related("New LLM released"))
    check("AI 关键词过滤-词边界", not sources_mod._is_ai_related("he said maybe"))
    check("AI 关键词过滤-中文", sources_mod._is_ai_related("大模型又便宜了"))
    check("标题归一化去重键一致",
          sources_mod._norm_title("OpenAI 发布 GPT-6!") == sources_mod._norm_title("openai发布gpt6"))
    check("摘要占位符被丢弃", sources_mod._clean_summary("点击查看原文>") == "")
    check("摘要阅读全文被丢弃", sources_mod._clean_summary("阅读全文") == "")
    check("摘要纯链接被丢弃", sources_mod._clean_summary("https://a.com/x") == "")
    check("摘要过短被丢弃", sources_mod._clean_summary("很短") == "")
    check("正常摘要保留", "新模型" in sources_mod._clean_summary("这是一个关于新模型的详细介绍"))
    check("摘要 HTML 被清理", "<" not in sources_mod._clean_summary("<p>正式<b>内容</b>说明</p>"))
    check("英文摘要保留", "framework" in sources_mod._clean_summary("An open source framework for AI"))


async def test_prompt_and_fallback():
    print("[提示词与兜底]")
    prompt = plugin_mod._build_user_prompt(FEED, 3, 6, "2026年09月27日", 2)
    check("提示词含全部标题", all(it["title"] in prompt for it in FEED))
    check("提示词含链接", all(it["url"] in prompt for it in FEED))
    check("提示词含三个板块", "今日必看" in prompt and "其他速览" in prompt and "一句话锐评" in prompt)
    check("提示词含条数要求", "挑 3 条" in prompt)
    p0 = plugin_mod._build_user_prompt(FEED, 3, 0, "d", 2)
    check("速览为 0 时不输出该板块", "## 其他速览" not in p0)
    fb = plugin_mod._fallback_report(FEED)
    check("兜底含条目与链接", FEED[0]["title"] in fb and FEED[0]["url"] in fb)
    check("兜底说明模型失败", "模型调用失败" in fb)
    check("风格补充文案齐全", set(plugin_mod._STYLE_EXTRA) == {"brief", "deep", "humor"})


async def test_extract_text():
    print("[模型返回解析]")
    ex = plugin_mod._extract_text
    check("completion_text", ex(types.SimpleNamespace(completion_text=" hi ")) == "hi")
    check("result_text 回退", ex(types.SimpleNamespace(result_text="ok")) == "ok")
    check("纯字符串", ex("直接字符串") == "直接字符串")
    check("result_chain 拼接", ex(types.SimpleNamespace(result_chain=[
        types.SimpleNamespace(text="A"), types.SimpleNamespace(text="B")])) == "AB")
    check("None 返回空", ex(None) == "")
    check("未知对象返回空", ex(types.SimpleNamespace(foo=1)) == "")


async def test_generate_and_push():
    print("[生成与推送]")

    async def run():
        p, ctx = make_plugin({"render_image": False})
        report, items, stats, err = await p._generate("g1", list(sources_mod.DEFAULT_SOURCES), "brief")
        check("生成成功无错误", err == "" and report and len(items) == 2)
        check("报告来自模型", "今日必看" in report)
        ok = await p._push_text("g1", report, items, stats)
        check("推送成功", ok and len(ctx.sent) == 1)
        check("推送目标正确", ctx.sent[0][0] == "g1")
        check("纯文本模式走 message", ctx.sent[0][1].text == report)
        check("推送后写入追问缓存", "g1" in p._cache and p._cache["g1"]["report"] == report)

    await with_stub_gather(run)


async def test_fail_paths():
    print("[异常路径]")

    async def no_items():
        p, ctx = make_plugin({})
        report, items, stats, err = await p._generate("g2", ["openai"], "brief")
        check("无素材时报错不崩", report is None and "没有抓到" in err)

    await with_stub_gather(no_items, items=[])

    async def llm_down():
        p, ctx = make_plugin({"render_image": False})
        ctx.fail_llm = True
        report, items, stats, err = await p._generate("g3", list(sources_mod.DEFAULT_SOURCES), "brief")
        check("模型失败时无 err 但走兜底", err == "" and report is not None)
        check("兜底内容含条目", FEED[0]["title"] in report)

    await with_stub_gather(llm_down)

    async def gather_raises():
        p, ctx = make_plugin({})
        orig = sources_mod.gather

        async def boom(**kwargs):
            raise RuntimeError("网络炸了")

        sources_mod.gather = boom
        try:
            report, items, stats, err = await p._generate("g4", ["openai"], "brief")
            check("抓取抛异常被捕获", report is None and "抓取信源失败" in err)
        finally:
            sources_mod.gather = orig

    await gather_raises()


async def test_commands():
    print("[指令流程]")

    async def run():
        p, ctx = make_plugin({"render_image": False})
        ev = FakeEvent("/日报", umo="g_cmd")

        res = await drain(p.cmd_daily(ev))
        text = join(res)
        check("主指令输出进度提示", "正在采集" in text)
        check("主指令输出报告", "今日必看" in text)

        ev2 = FakeEvent("/日报订阅", umo="g_cmd")
        res = await drain(p.cmd_subscribe(ev2))
        check("订阅成功", "订阅成功" in join(res))
        check("订阅状态已持久化", p._state["g_cmd"]["enabled"] is True)
        res = await drain(p.cmd_subscribe(FakeEvent("/日报订阅", umo="g_cmd")))
        check("重复订阅给出提示", "已经订阅" in join(res))

        res = await drain(p.cmd_status(FakeEvent("/日报状态", umo="g_cmd")))
        text = join(res)
        check("状态显示已订阅", "已订阅" in text)
        check("状态显示信源数与时间", "启用信源" in text and "推送时间" in text)

        res = await drain(p.cmd_sources(FakeEvent("/日报源", umo="g_cmd")))
        text = join(res)
        check("信源列表含全部 14 项", text.count("[") >= 14)
        check("信源列表标记分组", "官方发布" in text and "中文媒体" in text)

        res = await drain(p.cmd_sources(FakeEvent("/日报源 关闭 hn", umo="g_cmd")))
        check("关闭信源成功", "已关闭" in join(res) and "hn" not in p._state["g_cmd"]["sources"])
        res = await drain(p.cmd_sources(FakeEvent("/日报源 开启 hn", umo="g_cmd")))
        check("开启信源成功", "已开启" in join(res) and "hn" in p._state["g_cmd"]["sources"])
        res = await drain(p.cmd_sources(FakeEvent("/日报源 关闭 量子位", umo="g_cmd")))
        check("中文名可操作", "已关闭" in join(res) and "qbitai" not in p._state["g_cmd"]["sources"])
        res = await drain(p.cmd_sources(FakeEvent("/日报源 关闭 不存在的源", umo="g_cmd")))
        check("未知信源给提示", "没认出来" in join(res))

        # 非管理员不能改（拦截发生在「是否最后一个源」判断之前）
        p._state["g_cmd"]["sources"] = ["openai", "hn"]
        res = await drain(p.cmd_sources(FakeEvent("/日报源 关闭 openai", umo="g_cmd", admin=False)))
        check("非管理员被拦截", "管理员" in join(res) and "openai" in p._state["g_cmd"]["sources"])

        # 管理员可以关到只剩一个，但不能再关最后一个
        res = await drain(p.cmd_sources(FakeEvent("/日报源 关闭 hn", umo="g_cmd")))
        check("可关闭到只剩一个信源", "已关闭" in join(res) and p._state["g_cmd"]["sources"] == ["openai"])
        res = await drain(p.cmd_sources(FakeEvent("/日报源 关闭 openai", umo="g_cmd")))
        check("仅剩一个信源时拒绝关闭", "至少要保留一个" in join(res) and p._state["g_cmd"]["sources"] == ["openai"])

        res = await drain(p.cmd_time(FakeEvent("/日报时间", umo="g_cmd")))
        check("查看时间", "当前推送时间" in join(res))
        res = await drain(p.cmd_time(FakeEvent("/日报时间 08:30", umo="g_cmd")))
        check("设置时间成功", "08:30" in join(res) and p._state["g_cmd"]["time"] == "08:30")
        res = await drain(p.cmd_time(FakeEvent("/日报时间 25:99", umo="g_cmd")))
        check("非法时间被拒", "格式不对" in join(res))

        res = await drain(p.cmd_style(FakeEvent("/日报风格", umo="g_cmd")))
        check("查看风格", "当前风格" in join(res))
        res = await drain(p.cmd_style(FakeEvent("/日报风格 deep", umo="g_cmd")))
        check("切换风格成功", p._state["g_cmd"]["style"] == "deep")
        res = await drain(p.cmd_style(FakeEvent("/日报风格 乱填", umo="g_cmd")))
        check("非法风格被拒", "可选风格" in join(res))

        res = await drain(p.cmd_help(FakeEvent("/日报帮助", umo="g_cmd")))
        check("帮助含全部指令", all(x in join(res) for x in
                                ["/日报订阅", "/日报源", "/日报时间", "/日报追问"]))

        res = await drain(p.cmd_unsubscribe(FakeEvent("/日报退订", umo="g_cmd")))
        check("退订成功", "已退订" in join(res) and p._state["g_cmd"]["enabled"] is False)
        res = await drain(p.cmd_unsubscribe(FakeEvent("/日报退订", umo="g_cmd")))
        check("重复退订给提示", "本来就没有订阅" in join(res))

        # /日报 简写分发（回归：简写必须把剩余参数正确传给子逻辑）
        res = await drain(p.cmd_daily(FakeEvent("/日报 状态", umo="g_cmd")))
        check("/日报 状态下发到状态", "当前状态" in join(res))
        res = await drain(p.cmd_daily(FakeEvent("/日报 帮助", umo="g_cmd")))
        check("/日报 帮助下发到帮助", "AI 日报 · 多源综述" in join(res))
        res = await drain(p.cmd_daily(FakeEvent("/日报 源", umo="g_cmd")))
        check("/日报 源下发到信源列表", "启用" in join(res) and "[" in join(res))
        res = await drain(p.cmd_daily(FakeEvent("/日报 时间", umo="g_cmd")))
        check("/日报 时间下发到时间查看", "当前推送时间" in join(res))
        p._state["g_cmd"]["sources"] = ["openai", "qbitai"]
        res = await drain(p.cmd_daily(FakeEvent("/日报 源 关闭 量子位", umo="g_cmd")))
        check("/日报 源 关闭 可带参数生效",
              "已关闭" in join(res) and "qbitai" not in p._state["g_cmd"]["sources"])
        res = await drain(p.cmd_daily(FakeEvent("/日报 源 开启 量子位", umo="g_cmd")))
        check("/日报 源 开启 可带参数生效",
              "已开启" in join(res) and "qbitai" in p._state["g_cmd"]["sources"])
        res = await drain(p.cmd_daily(FakeEvent("/日报 时间 06:45", umo="g_cmd")))
        check("/日报 时间 可带参数生效",
              p._state["g_cmd"]["time"] == "06:45" and "06:45" in join(res))

    await with_stub_gather(run)


async def test_ask():
    print("[追问]")

    async def run():
        p, ctx = make_plugin({"render_image": False})
        umo = "g_ask"
        res = await drain(p.cmd_ask(FakeEvent("/日报追问 什么是 o5", umo=umo)))
        check("无缓存时提示先生成", "还没有生成过日报" in join(res))
        res = await drain(p.cmd_ask(FakeEvent("/日报追问", umo=umo)))
        check("无参数时提示用法", "用法" in join(res))

        await p._do_push(umo, p._entry(umo))
        before = ctx.calls
        res = await drain(p.cmd_ask(FakeEvent("/日报追问 o5 是什么", umo=umo)))
        text = join(res)
        check("有缓存时走模型", ctx.calls > before and "翻今天的素材" in text)
        check("追问返回模型答案", "今日必看" in text)

    await with_stub_gather(run)


async def test_persistence_and_schedule():
    print("[持久化与调度]")

    async def run():
        p, ctx = make_plugin({"render_image": False})
        umo = "g_persist"
        st = p._entry(umo)
        st["enabled"] = True
        st["time"] = "07:15"
        st["sources"] = ["openai", "hn"]
        st["style"] = "humor"
        p._save()

        p2, _ = make_plugin({})
        check("状态回读成功", p2._state[umo]["time"] == "07:15")
        check("信源回读成功", p2._state[umo]["sources"] == ["openai", "hn"])
        check("风格回读成功", p2._state[umo]["style"] == "humor")
        check("启用状态回读成功", p2._state[umo]["enabled"] is True)

        # 调度判定
        s = dict(p2._state[umo])
        check("到点应推送", p2._is_due(s, "07:15", "2026-09-27", False))
        check("非到点不推送", not p2._is_due(s, "07:16", "2026-09-27", False))
        s2 = dict(s); s2["last_push_date"] = "2026-09-27"
        check("当天已推不再重复", not p2._is_due(s2, "07:15", "2026-09-27", False))
        s3 = dict(s); s3["last_push_date"] = "2026-09-26"
        check("跨天恢复推送", p2._is_due(s3, "07:15", "2026-09-27", False))
        s4 = dict(s); s4["last_push_date"] = ""
        check("静默结束补发已过时间", p2._is_due(s4, "09:00", "2026-09-27", True))
        p3, _ = make_plugin({"quiet_catchup": False})
        p3._state[umo] = dict(s4)
        check("关闭补发后不补", not p3._is_due(s4, "09:00", "2026-09-27", True))

        # 损坏的状态文件不应导致崩溃
        p2.state_file.write_text("{ 这不是合法 json", encoding="utf-8")
        p4, _ = make_plugin({})
        check("状态文件损坏时不崩", isinstance(p4._state, dict) and p4._state == {})

    await run()


async def test_render():
    print("[卡片渲染]")
    check("渲染可用（Pillow+中文字体）", render_mod.available())
    png = render_mod.render_report(REPLY, subtitle="测试", theme="dark")
    check("渲染返回 PNG 字节", isinstance(png, bytes) and png[:8] == b"\x89PNG\r\n\x1a\n")
    check("emoji 被剥离", "🔥" not in render_mod._strip_emoji("🔥 今日必看"))
    check("分类-标题", render_mod._classify("## 🔥 今日必看") == ("h", "今日必看"))
    check("分类-点评", render_mod._classify("💡 重点是什么")[0] == "note")
    check("分类-链接", render_mod._classify("🔗 https://a.com")[0] == "link")
    check("分类-裸链接", render_mod._classify("https://a.com")[0] == "link")
    check("分类-列表", render_mod._classify("- 某条新闻")[0] == "li")
    check("分类-空行", render_mod._classify("   ")[0] == "space")
    check("markdown 链接被清洗", render_mod._strip_md("看[这里](https://a.com)") == "看这里")
    check("空文本渲染返回 None", render_mod.render_report("") is None)
    png2 = render_mod.render_report(REPLY, theme="light")
    check("浅色主题可渲染", isinstance(png2, bytes) and len(png2) > 1000)
    check("未知主题回落深色", render_mod._pal("zzz") == render_mod._pal("dark"))


async def main():
    await test_config_utils()
    await test_quiet_hours()
    await test_sources()
    await test_prompt_and_fallback()
    await test_extract_text()
    await test_generate_and_push()
    await test_fail_paths()
    await test_commands()
    await test_ask()
    await test_persistence_and_schedule()
    await test_render()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
