"""导入任务的「切分预览 + 进度台账」（`FE-06`，契约 `POST /bff/v1/kb/import/preview`）。

**为什么放在服务层而不是接口层**：预览要解析三种文件（CSV / Excel / 文本）、
自动判别知识类型、按类型的字数区间切分，这段逻辑接口层写不下也测不动；
放服务层后接口只剩「读文件 → 调这里 → 包 ok()」。

**任务台账为什么在内存**：预览 → 人工确认 → 提交是**同一进程内的短事务**
（坐席点「开始导入」后几秒内完成），落库反而要清理过期记录。真多副本部署时
把 `_TASKS` 换成 Redis 即可，接口契约不变（`get_task` / `finish_task` 是唯二入口）。

**切分口径与 BE-06 完全一致**：复用 `splitter.split_markdown_by_heading`，
类型区间取自商家《商品分类体系表》「知识库切分单元」列
（商品 200–400 / 政策 100–300 / FAQ 50–200 / 长文档 300–500，重叠 15%）。
"""

from __future__ import annotations

import csv
import io
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.errors import ValidationError
from app.services.knowledge.splitter import split_markdown_by_heading

__all__ = [
    "ALLOWED_EXTENSIONS",
    "BACKEND_TYPE",
    "CHUNK_OVERLAP",
    "TYPE_RANGES",
    "build_preview",
    "detect_type",
    "finish_task",
    "get_task",
    "progress_view",
]

#: 允许上传的扩展名（与前端 `previewKbImport` 的 accept 一致）
ALLOWED_EXTENSIONS = (".csv", ".xlsx", ".xls", ".md", ".markdown", ".txt")

#: 切分重叠比例（商家口径 15%）
CHUNK_OVERLAP = 0.15

#: 各知识类型的正文字数区间（下限, 上限），与 `schemas.knowledge.LENGTH_RANGES` 同源
TYPE_RANGES: dict[str, tuple[int, int]] = {
    "PRODUCT": (200, 400),
    "POLICY": (100, 300),
    "FAQ": (50, 200),
    "LONG_DOC": (300, 500),
}

#: 前端 KbType → 后端 KnowledgeType（只有长文档不同名）
BACKEND_TYPE: dict[str, str] = {
    "PRODUCT": "PRODUCT",
    "POLICY": "POLICY",
    "FAQ": "FAQ",
    "LONG_DOC": "DOC",
}

_POLICY_WORDS = (
    "退货",
    "换货",
    "退款",
    "售后",
    "保修",
    "维修",
    "运费",
    "政策",
    "条款",
    "发票",
    "七天无理由",
)
_PRODUCT_HEADERS = ("商品名", "商品", "型号", "sku", "SKU", "尺码", "规格", "材质")
_FAQ_HEADERS = ("问题", "答案", "回答", "疑问")

#: 任务台账：`import_id -> 任务快照`。进程内共享，用锁保护（TestClient 多线程）
_TASKS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _now_iso() -> str:
    """当前时间（带时区的 ISO 8601，前端直接展示）。"""
    return datetime.now(UTC).isoformat()


def _decode(data: bytes) -> str:
    """按 UTF-8(BOM) → GBK → UTF-8(忽略错误) 的顺序解码文本。

    商家材料里 GBK 编码的 CSV 很常见，只按 UTF-8 解会整篇乱码；
    反过来先试 GBK 会把正常 UTF-8 解成乱码，所以顺序不能换。
    """
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _read_table(data: bytes, ext: str) -> tuple[list[str], list[list[str]]]:
    """读 CSV / Excel 为 `(表头, 数据行)`，单元格统一转字符串。"""
    if ext == ".csv":
        text = _decode(data)
        reader = csv.reader(io.StringIO(text))
        rows = [[str(cell).strip() for cell in row] for row in reader]
    else:
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheet = workbook.active
        rows = [
            ["" if cell is None else str(cell).strip() for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
        workbook.close()

    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        raise ValidationError("表格没有数据行，无法切分")
    header = rows[0]
    return header, rows[1:]


def _row_text(header: list[str], row: list[str]) -> str:
    """把一行表格拼成「列名：值；列名：值」的检索友好文本。"""
    parts: list[str] = []
    for idx, value in enumerate(row):
        if not value:
            continue
        name = header[idx] if idx < len(header) and header[idx] else f"第{idx + 1}列"
        parts.append(f"{name}：{value}")
    return "；".join(parts) if parts else "；".join(cell for cell in row if cell)


def detect_type(
    filename: str, header: list[str], text: str
) -> tuple[str, list[dict[str, Any]]]:
    """自动判别知识类型，返回 `(主类型, 候选列表)`。

    打分而非硬规则，是为了让「同一份 FAQ 用 CSV 或 Markdown 上传」都能识别对：
    表头有「问题/答案」直接判 FAQ（0.75 分），正文里售后词多则 POLICY 涨分，
    `.md` 给 LONG_DOC 打底 0.7 分。**主类型永远有值**（最低兜底 0.5），
    因为前端类型选择器需要一个初始值。
    """
    ext = Path(filename).suffix.lower()
    joined_header = " ".join(header)
    scores = {"PRODUCT": 0.0, "POLICY": 0.0, "FAQ": 0.0, "LONG_DOC": 0.0}

    if any(word in joined_header for word in _FAQ_HEADERS):
        scores["FAQ"] += 0.75
    if any(word in joined_header for word in _PRODUCT_HEADERS):
        scores["PRODUCT"] += 0.6

    policy_hits = sum(1 for word in _POLICY_WORDS if word in text)
    scores["POLICY"] += min(0.75, 0.15 * policy_hits)

    question_marks = text.count("？") + text.count("?")
    scores["FAQ"] += min(0.35, 0.02 * question_marks)

    if ext in (".md", ".markdown"):
        scores["LONG_DOC"] += 0.7
    if len(text) > 1500:
        scores["LONG_DOC"] += 0.3
    if ext in (".csv", ".xlsx", ".xls"):
        scores["LONG_DOC"] -= 0.2

    for key, value in scores.items():
        scores[key] = max(0.0, min(1.0, round(value, 2)))

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    primary, top_score = ranked[0]
    if top_score < 0.5:
        # 兜底：没有任何信号时按长文档处理（最宽松的切分区间），
        # 但置信度如实给 0.5，前端会提示「请人工确认类型」。
        primary = "LONG_DOC"
        scores["LONG_DOC"] = 0.5
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)

    candidates = [
        {"kb_type": kb_type, "confidence": score}
        for kb_type, score in ranked[:3]
        if score > 0
    ]
    return primary, candidates


def _make_chunk(
    index: int,
    title: str,
    content: str,
    source_ref: str,
    low: int,
    high: int,
) -> dict[str, Any]:
    """组装一个预览切分单元（前端 `ImportPreviewChunk`）。"""
    length = len(content)
    if length > high:
        flag = "TOO_LONG"
    elif length < low:
        flag = "TOO_SHORT"
    else:
        flag = "OK"
    return {
        "index": index,
        "title": title,
        "content": content,
        "char_count": length,
        "length_flag": flag,
        "source_ref": source_ref,
    }


def _chunks_from_rows(
    header: list[str], rows: list[list[str]], filename: str, kb_type: str
) -> list[dict[str, Any]]:
    """表格类文件：**一行一个切分单元**（商品卡 / FAQ 一问一答天然一行一条）。"""
    low, high = TYPE_RANGES[kb_type]
    chunks: list[dict[str, Any]] = []
    for row_no, row in enumerate(rows, start=1):
        content = _row_text(header, row).strip()
        if not content:
            continue
        title = next((cell for cell in row if cell), f"第 {row_no} 行")
        chunks.append(
            _make_chunk(
                len(chunks) + 1,
                title[:80],
                content,
                f"{filename}#L{row_no + 1}",
                low,
                high,
            )
        )
    return chunks


def _chunks_from_text(
    text: str, filename: str, kb_type: str
) -> list[dict[str, Any]]:
    """文本类文件：复用 BE-06 的标题层级递归切分（重叠 15%）。"""
    low, high = TYPE_RANGES[kb_type]
    raw_chunks = split_markdown_by_heading(
        text,
        min_len=low,
        max_len=high,
        overlap_ratio=CHUNK_OVERLAP,
        source=filename,
    )
    chunks: list[dict[str, Any]] = []
    for raw in raw_chunks:
        content = raw.content.strip()
        if not content:
            continue
        title = (raw.title or "").strip() or f"片段 {len(chunks) + 1}"
        ref = (
            f"{filename}#L{raw.line_start}"
            if raw.line_start
            else f"{filename}#{len(chunks) + 1}"
        )
        chunks.append(_make_chunk(len(chunks) + 1, title, content, ref, low, high))
    return chunks


def build_preview(data: bytes, filename: str) -> dict[str, Any]:
    """解析上传文件，生成切分预览并把任务登记进台账。

    Args:
        data: 文件字节。
        filename: 原始文件名（用于判扩展名与展示）。

    Returns:
        前端 `ImportPreviewResponse` 形状的字典（**未包装**，接口层再 `ok()`）。

    Raises:
        ValidationError: 空文件 / 不支持的扩展名 / 解析不出任何切分单元。
    """
    if not data:
        raise ValidationError("文件为空，请选择要导入的文件")

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"不支持的文件类型 {ext or '（无扩展名）'}，"
            "请上传 .csv / .xlsx / .xls / .md / .markdown / .txt"
        )

    if ext in (".csv", ".xlsx", ".xls"):
        header, rows = _read_table(data, ext)
        text = "\n".join(_row_text(header, row) for row in rows)
    else:
        header, rows = [], []
        text = _decode(data)

    if not text.strip():
        raise ValidationError("文件内容为空，无法切分")

    kb_type, candidates = detect_type(filename, header, text)
    chunks = (
        _chunks_from_rows(header, rows, filename, kb_type)
        if rows
        else _chunks_from_text(text, filename, kb_type)
    )
    if not chunks:
        raise ValidationError("未能从文件中切分出任何知识块，请检查文件内容")

    low, high = TYPE_RANGES[kb_type]
    too_long = sum(1 for chunk in chunks if chunk["length_flag"] == "TOO_LONG")
    too_short = sum(1 for chunk in chunks if chunk["length_flag"] == "TOO_SHORT")
    warnings: list[str] = []
    if too_long:
        warnings.append(
            f"{too_long} 个切分单元超过 {high} 字，建议在预览里剔除或手工拆分后重传"
        )
    if too_short:
        warnings.append(
            f"{too_short} 个切分单元不足 {low} 字，检索时可能召不回，建议合并"
        )

    import_id = f"IMP-{uuid4().hex[:10].upper()}"
    started_at = _now_iso()
    task = {
        "import_id": import_id,
        "file_name": filename,
        "detected_type": kb_type,
        "status": "RUNNING",
        "total": len(chunks),
        "succeeded": 0,
        "failed": 0,
        "failures": [],
        "started_at": started_at,
        "finished_at": None,
        "chunks": chunks,
    }
    with _LOCK:
        _TASKS[import_id] = task

    return {
        "import_id": import_id,
        "file_name": filename,
        "detected_type": kb_type,
        "total_chunks": len(chunks),
        "chunks": chunks,
        "overlap_ratio": CHUNK_OVERLAP,
        "type_candidates": candidates,
        "warnings": warnings,
    }


def get_task(import_id: str) -> dict[str, Any] | None:
    """取任务快照（**副本**，调用方改不动台账）。"""
    with _LOCK:
        task = _TASKS.get(import_id)
        return None if task is None else dict(task)


def progress_view(import_id: str) -> dict[str, Any] | None:
    """取前端 `ImportProgress` 形状（不含 `chunks` 大字段）。"""
    task = get_task(import_id)
    if task is None:
        return None
    return {
        "import_id": task["import_id"],
        "status": task["status"],
        "total": task["total"],
        "succeeded": task["succeeded"],
        "failed": task["failed"],
        "failures": list(task["failures"]),
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
    }


def finish_task(
    import_id: str,
    *,
    succeeded: int,
    failed: int,
    failures: list[dict[str, Any]],
    status: str,
) -> dict[str, Any] | None:
    """提交完成后回写任务状态，返回 `ImportProgress` 形状。"""
    with _LOCK:
        task = _TASKS.get(import_id)
        if task is None:
            return None
        task["succeeded"] = succeeded
        task["failed"] = failed
        task["failures"] = list(failures)
        task["status"] = status
        task["finished_at"] = _now_iso()
    return progress_view(import_id)
