"""端到端演练：真实抓取信源 → 真实拼装提示词 → 真实渲染卡片。

只跳过「调用模型」这一步（需要 AstrBot 里的模型商配置），
用于验证除模型外的整条链路在真实网络下可用。

运行：python _e2e.py
产出：控制台报告 + _e2e_card.png
"""

import asyncio
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT.parent))

# ---- 最小 astrbot 桩（仅为导入 main.py 用，不参与业务逻辑）----
_astrbot = types.ModuleType("astrbot")
_api = types.ModuleType("astrbot.api")
_ev = types.ModuleType("astrbot.api.event")
_star = types.ModuleType("astrbot.api.star")
_mc = types.ModuleType("astrbot.api.message_components")
_prov = types.ModuleType("astrbot.api.provider")


class _Log:
    def __getattr__(self, n):
        return lambda *a, **k: None


_api.logger = _Log()
_api.AstrBotConfig = dict
_ev.AstrMessageEvent = type("AstrMessageEvent", (), {})
_ev.MessageChain = type("MessageChain", (), {"__init__": lambda s, *a, **k: None,
                                             "message": lambda s, t: s})
_ev.filter = type("filter", (), {
    "PermissionType": type("P", (), {"ADMIN": "admin"}),
    "command": lambda *a, **k: (lambda f: f),
    "permission_type": lambda *a, **k: (lambda f: f),
})()
_star.Context = type("Context", (), {})
_star.Star = type("Star", (), {"__init__": lambda s, c=None: setattr(s, "context", c)})
_star.StarTools = type("StarTools", (), {
    "get_data_dir": staticmethod(lambda n: Path(tempfile.mkdtemp(prefix="aie2e_")))
})
_star.register = lambda *a, **k: (lambda c: c)
_mc.Image = type("Image", (), {"__init__": lambda s, file="": None})
_prov.ProviderRequest = type("ProviderRequest", (), {"__init__": lambda s, **k: None})

_astrbot.api = _api
for _n, _m in [("astrbot", _astrbot), ("astrbot.api", _api), ("astrbot.api.event", _ev),
               ("astrbot.api.star", _star), ("astrbot.api.message_components", _mc),
               ("astrbot.api.provider", _prov)]:
    sys.modules[_n] = _m

import astrbot_plugin_ai_daily.main as pm  # noqa: E402
import astrbot_plugin_ai_daily.render as render  # noqa: E402
import astrbot_plugin_ai_daily.sources as sources  # noqa: E402


async def main():
    print("=" * 78)
    print("端到端演练：真实抓取 → 拼装提示词 → 渲染卡片")
    print("=" * 78)

    keys = list(sources.SOURCES.keys())
    items, stats = await sources.gather(keys=keys, per_source_n=12,
                                       lookback_hours=72, timeout=20, retries=1)

    print("\n【1】信源抓取（全部 14 个源，时间窗口 72 小时）")
    live = 0
    for s in stats:
        tag = "✅" if s["ok"] and s["n"] else ("⬜" if s["ok"] else "❌")
        if s["ok"] and s["n"]:
            live += 1
        print(f"  {tag} {s['label']:<18} 命中 {s['n']:>3} / 原始 {s['raw']:>3}  {s['err'][:44]}")
    print(f"  → 有内容的信源 {live}/{len(stats)}，去重后素材 {len(items)} 条")

    if not items:
        print("\n⚠️ 本次没有抓到任何素材，无法继续演练（网络或时间窗口问题）。")
        return

    print("\n【2】素材样本（时间倒序前 8 条）")
    for it in items[:8]:
        print(f"  · [{it['source_label']}] {it['title'][:64]}")

    print("\n【3】提示词拼装")
    prompt = pm._build_user_prompt(items[:60], 3, 6, "2026年09月27日",
                                  len({i["source"] for i in items}))
    print(f"  长度 {len(prompt)} 字，含 {min(len(items), 60)} 条素材")
    print("  " + "-" * 70)
    for line in prompt.splitlines()[:14]:
        print("  " + line[:96])
    print("  ...（略）...")
    print("  " + "-" * 70)
    for key in ("## 今日必看", "## 其他速览", "## 一句话锐评"):
        print(f"  含格式要求 {key}: {key in prompt}")

    print("\n【4】模型失败时的兜底文本")
    fb = pm._fallback_report(items)
    print(f"  {len(fb)} 字，首行：{fb.splitlines()[0]}")

    print("\n【5】卡片渲染")
    for theme in ("dark", "light"):
        png = render.render_report(
            fb, subtitle=f"2026-09-27 09:00 · {live} 个信源 · {len(items)} 条素材", theme=theme)
        out = ROOT / f"_e2e_card_{theme}.png"
        out.write_bytes(png or b"")
        print(f"  {theme}: {len(png or b'')} bytes -> {out.name}")


asyncio.run(main())
