"""L2 多模态预处理（BE-10 / BE-11 / BE-12）。

三层职责严格分开，避免把「转码」「理解」「业务动作」搅在一起：

* `audio.py` —— **BE-10 语音接入转码**。只做「任意格式 → 16k/16bit/单声道 wav」，
  不关心识别结果。
* `image.py` —— **BE-11 图片校验 + 五路分流**。只做「图片 → 路由 + 一句话语义」。
* `order_screenshot.py` —— **BE-12 订单截图 OCR**。只做「图片 → 订单号」，
  拿到订单号后**交给 BE-07 订单工具**，本层不查订单。
* `pipeline.py` —— **统一入口**。把文本 / 语音 / 图片三条输入归一成**同一个结构**
  （`NormalizedMessage`），保证后续意图识别、检索、工具、转人工**完全同构**——
  这是 BE-11 DoD 里「与文本/语音路径结构同构」的字面要求。
"""

from app.services.multimodal.pipeline import (
    MultimodalPipeline,
    NormalizedMessage,
    get_pipeline,
    set_pipeline,
)

__all__ = [
    "MultimodalPipeline",
    "NormalizedMessage",
    "get_pipeline",
    "set_pipeline",
]
