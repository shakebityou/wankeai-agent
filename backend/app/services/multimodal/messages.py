"""多模态兜底话术（BE-10 / BE-11 / BE-12）。

⚠️ 三条硬约束在这里同样生效：
* **HC-1 零外链** —— 话术里不允许出现任何 URL / 「点击」「跳转」「扫码」。
* 不承诺时间 —— 不写「5 分钟内」「马上」。
* 单条 ≤ 120 字。

话术是**业务资产**，不是日志文案，所以集中在这里，方便商家运营改口径而不用动代码。
"""

from __future__ import annotations

#: 语音：转码失败 / 识别为空 / 置信度过低，统一用这一句。
#: 依据任务书 BE-10「失败话术：语音没听清，麻烦您再发一次或打字～」。
VOICE_UNHEARD = "语音没听清，麻烦您再发一次或打字～"

#: 语音：格式不支持（如未配 silk 解码器的微信语音）。
#: 单独一句的原因：让用户知道「换一种方式发」比「再说一遍」更有效。
VOICE_UNSUPPORTED = "这条语音的格式我这边放不出来，麻烦您重新发一条或直接打字～"

#: 语音：超长。
VOICE_TOO_LONG = "语音有点长，我没能听全，麻烦您把要紧的几句再说一遍或打字～"

#: 图片：体积超限 / 格式不支持 / 解码失败。
IMAGE_UNREADABLE = "图片没能加载出来，麻烦您重新发一张，或者直接用文字告诉我～"

#: 图片：判成表情包/无关图，礼貌引导回业务。
#: 依据 BE-11 五路里 `MEME_IRRELEVANT` 的处置：**不转人工**，轻引导。
IMAGE_IRRELEVANT = "这张图我没太看懂，您是想咨询哪件商品、或者订单的哪个问题呢？"

#: 订单截图：OCR 没读出 12 位订单号。
#: 依据任务书 BE-12「失败话术：麻烦您把订单号发我一下，在『我的订单』页面可以看到哦～」。
ORDER_NO_NOT_FOUND = (
    "麻烦您把订单号发我一下，在『我的订单』页面可以看到哦～"
)

#: 订单截图：读到了订单号但 OCR 置信度低于阈值（仍然会把号码交给订单工具，
#: 只是先跟用户确认一眼，避免查错单）。
ORDER_NO_LOW_CONFIDENCE = "我读到订单号是 {order_no}，如果不对麻烦您再发一次～"


#: 所有话术的注册表（自检脚本按 key 遍历，确保都不含外链）。
FALLBACK_TEXTS: dict[str, str] = {
    "VOICE_UNHEARD": VOICE_UNHEARD,
    "VOICE_UNSUPPORTED": VOICE_UNSUPPORTED,
    "VOICE_TOO_LONG": VOICE_TOO_LONG,
    "IMAGE_UNREADABLE": IMAGE_UNREADABLE,
    "IMAGE_IRRELEVANT": IMAGE_IRRELEVANT,
    "ORDER_NO_NOT_FOUND": ORDER_NO_NOT_FOUND,
}


#: 命中这些词就说明话术里混进了外链/跳转（自检脚本断言用）。
FORBIDDEN_LINK_WORDS: tuple[str, ...] = (
    "http",
    "www.",
    "点击",
    "跳转",
    "扫码",
    "二维码",
    "链接",
)


def fallback_text(key: str) -> str:
    """按 key 取话术，未知 key 退回「没看懂」的轻引导。"""
    return FALLBACK_TEXTS.get(key, IMAGE_IRRELEVANT)


__all__ = [
    "FALLBACK_TEXTS",
    "FORBIDDEN_LINK_WORDS",
    "IMAGE_IRRELEVANT",
    "IMAGE_UNREADABLE",
    "ORDER_NO_LOW_CONFIDENCE",
    "ORDER_NO_NOT_FOUND",
    "VOICE_TOO_LONG",
    "VOICE_UNHEARD",
    "VOICE_UNSUPPORTED",
    "fallback_text",
]
