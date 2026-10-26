"""知识库材料导入（`BE-06`）。

把商家材料包的真实材料转成 `KbChunk`：

| 来源文件 | 知识类型 | 切分单元 | 契约 DoD |
|---|---|---|---|
| `商品知识卡.csv`（74 行） | `PRODUCT` | 一款一单元 200–400 字 | 74 张全部入库 |
| `FAQ知识库.csv`（126 行） | `FAQ` | 一问一答 50–200 字 | 126 条全部入库 |
| `售后政策.md` | `POLICY` | 标题层级递归 100–300 字 | 31 条 |
| `物流发货SOP.md` | `POLICY` | 同上 | 25 条 |
| `营销活动规则.md` | `POLICY` | 同上 | 22 条 |
| `品牌与IP资料.md` | `POLICY` | 同上 | 18 条 |

**导入必须可核对**：DoD 说「74 卡 + 126 FAQ + 96 条款全部入库无丢块」，
所以每次导入都写一条 `KbImportJob` 账本（总行数/总 chunk/成功/失败/错误），
事后能定位是哪一行丢的。`dry_run=True` 只预览不落库，运营在 FE-06
「批量导入与切分预览」页看到的就是它。

**价格与库存绝不入库**（HC-3）：`商品知识卡.csv` 的「吊牌价/现价/库存状态」
只进 `extra` 供人工核对，且 `assert_no_forbidden_fields` 在写向量库时拦第二道。
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.core.enums import KnowledgeType, ReviewStatus
from app.schemas.knowledge import (
    ChunkMetadata,
    ImportPreviewResponse,
    ImportResult,
    LENGTH_RANGES,
)
from app.services.knowledge.splitter import (
    RawChunk,
    chunk_faq_row,
    chunk_product_card,
    extract_keywords,
    split_markdown_by_heading,
)

#: 政策文档 → (知识类型, authority, 分类名)
#: authority 1 最高、5 最低。售后政策与品牌安全直接关系赔付，给 1；
#: 物流 SOP 与营销规则是流程性约定，给 2。
POLICY_SOURCES: dict[str, dict[str, Any]] = {
    "售后政策.md": {
        "knowledge_type": KnowledgeType.POLICY,
        "category": "售后政策",
        "authority": 1,
    },
    "物流发货SOP.md": {
        "knowledge_type": KnowledgeType.POLICY,
        "category": "物流发货",
        "authority": 2,
    },
    "营销活动规则.md": {
        "knowledge_type": KnowledgeType.POLICY,
        "category": "营销活动",
        "authority": 2,
    },
    "品牌与IP资料.md": {
        "knowledge_type": KnowledgeType.POLICY,
        "category": "品牌信任",
        "authority": 1,
    },
}


#: 「条款索引」标题的特征串。四份政策文档末尾都有这张表，
#: 它是 FAQ 引用自洽性校验表，不是政策内容本身。
_CLAUSE_INDEX_MARK = "条款索引"


def _find_clause_index(text: str) -> int:
    """定位「条款索引」块在原文中的起始偏移。

    Args:
        text: Markdown 全文。

    Returns:
        起始字符偏移；未找到返回 -1。
    """
    pos = text.find(_CLAUSE_INDEX_MARK)
    if pos < 0:
        return -1
    # 回退到所在行的行首，保证截断后残留的 Markdown 结构完整
    line_start = text.rfind("\n", 0, pos) + 1
    return line_start


@dataclass(slots=True)
class ImportRow:
    """一条待入库的 chunk（尚未分配 chunk_id）。

    Attributes:
        title: 标题。
        content: 正文。
        knowledge_type: 知识类型。
        category: 分类。
        sku_ids: 关联 SKU。
        keywords: 关键词。
        source: 来源文件名。
        source_row: 源行号。
        authority: 权威度 1–5。
        warnings: 切分告警（长度越界等）。
    """

    title: str
    content: str
    knowledge_type: KnowledgeType
    category: str
    sku_ids: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    source: str = ""
    source_row: int | None = None
    authority: int = 2
    effective_from: date | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _read_csv_rows(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    """读 CSV 并逐行产出 `(行号, 行字典)`。

    行号从 **2** 起（第 1 行是表头），与 Excel 里看到的行号一致——
    运营拿着「第 37 行有问题」来报障时能直接对上。

    用 `utf-8-sig` 解码：商家导出的 CSV 带 BOM，用 `utf-8` 会让第一个列名
    变成 `\\ufeffSKU编码`，`row["SKU编码"]` 直接 KeyError。

    Args:
        path: CSV 文件路径。

    Yields:
        `(行号, 行字典)`。
    """
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for offset, row in enumerate(reader, start=2):
            yield offset, row


def load_product_cards(path: Path) -> list[ImportRow]:
    """读商品知识卡 CSV → `ImportRow` 列表。

    Args:
        path: `商品知识卡.csv` 路径。

    Returns:
        一行一 chunk 的导入行列表。
    """
    rows: list[ImportRow] = []
    for row_no, row in _read_csv_rows(path):
        raw = chunk_product_card(row, row_no=row_no)
        sku = (row.get("SKU编码") or "").strip()
        # 价格/库存只进 extra 人工核对用，不进 content（HC-3）
        extra: dict[str, Any] = {}
        for label, key in (("list_price", "吊牌价"), ("current_price", "现价")):
            value = (row.get(key) or "").strip()
            if value:
                extra[label] = value
        stock = (row.get("库存状态") or "").strip()
        if stock:
            extra["stock_status"] = stock

        rows.append(
            ImportRow(
                title=raw.title,
                content=raw.content,
                knowledge_type=KnowledgeType.PRODUCT,
                category=(row.get("类目") or "").strip() or "未分类",
                sku_ids=[sku] if sku else [],
                keywords=extract_keywords(raw.content),
                source=path.name,
                source_row=row_no,
                authority=1,
                extra=extra,
                warnings=raw.warnings,
            )
        )
    return rows


def load_faq(path: Path) -> list[ImportRow]:
    """读 FAQ CSV → `ImportRow` 列表。

    `authority` 字段是商家填的引用依据（如「售后政策 4.2 条」），
    共 20 种取值。它决定 `ChunkMetadata.authority`（权威度 1–5）：
    引用商品主数据表的视为 1（一手事实），引用内部 SOP 的视为 2。

    Args:
        path: `FAQ知识库.csv` 路径。

    Returns:
        一行一 chunk 的导入行列表。
    """
    rows: list[ImportRow] = []
    for row_no, row in _read_csv_rows(path):
        raw = chunk_faq_row(row, row_no=row_no)
        authority_text = (row.get("authority") or "").strip()
        rows.append(
            ImportRow(
                title=raw.title,
                content=raw.content,
                knowledge_type=KnowledgeType.FAQ,
                category=(row.get("category") or "").strip() or "未分类",
                keywords=extract_keywords(raw.content),
                source=path.name,
                source_row=row_no,
                authority=_faq_authority(authority_text),
                extra={"authority_source": authority_text},
                warnings=raw.warnings,
            )
        )
    return rows


def _faq_authority(authority_text: str) -> int:
    """把商家的 authority 引用文本映射成权威度 1–5。

    映射依据：引用**一手事实**（商品主数据表、对外公示的政策文件）给 1；
    引用内部流程文件（SOP、营销规则）给 2；无法识别给 3。
    这个值只影响重排时的次要排序（权重上限 0.15），不影响是否命中。

    Args:
        authority_text: 如「售后政策 4.2 条」「商品主数据表·洗护方式字段」。

    Returns:
        权威度（1 最高）。
    """
    if not authority_text:
        return 3
    if "商品主数据表" in authority_text:
        return 1
    for keyword in ("售后政策", "品牌与IP资料", "品牌与 IP 资料"):
        if keyword in authority_text:
            return 1
    for keyword in ("物流发货SOP", "物流发货 SOP", "营销活动规则"):
        if keyword in authority_text:
            return 2
    return 3


def load_policy_doc(path: Path, *, meta: dict[str, Any]) -> list[ImportRow]:
    """读政策 Markdown → `ImportRow` 列表（按标题层级切）。

    **「例外情形」小节刻意保留**：四份政策文档都有 `X.4 例外情形`，
    它们正是客服最容易答错的地方（用户问「我这情况能退吗」，
    答案往往在例外里）。把它们和正文一起切分入库，检索才有机会命中。

    **尾部「条款索引」块剔除**：四份政策文档末尾都有一张
    「条款索引（供 FAQ 知识库 authority 字段引用核对）」表格。
    它是对 FAQ 引用自洽性的**元数据校验表**，不是给用户看的政策内容，
    且与正文章节大面积重复——入库会让检索命中「条款号列表」而不是条款本身。
    这里的做法是：遇到该标题就截断，其后的内容一律不入库。
    等价于给每个源文件设一个 `# noqa` 语义边界。

    Args:
        path: Markdown 文件路径。
        meta: `POLICY_SOURCES[path.name]` 的配置项。

    Returns:
        一个条款一 chunk 的导入行列表。
    """
    text = path.read_text(encoding="utf-8")
    cut = _find_clause_index(text)
    if cut > 0:
        text = text[:cut]
    min_len, max_len = LENGTH_RANGES[KnowledgeType.POLICY]
    raw_chunks: list[RawChunk] = split_markdown_by_heading(
        text,
        min_len=min_len,
        max_len=max_len,
        overlap_ratio=0.15,
        source=path.name,
    )

    rows: list[ImportRow] = []
    for raw in raw_chunks:
        rows.append(
            ImportRow(
                title=raw.title,
                content=raw.content,
                knowledge_type=meta["knowledge_type"],
                category=meta["category"],
                keywords=extract_keywords(f"{raw.title}\n{raw.content}"),
                source=path.name,
                source_row=raw.line_start,
                authority=meta["authority"],
                extra={"clause_no": raw.clause_no, "level": raw.level},
                warnings=list(raw.warnings),
            )
        )
    return rows


def collect_merchant_package(
    package_dir: Path,
) -> tuple[list[ImportRow], list[str]]:
    """扫描商家材料包，返回全部待导入行与源文件清单。

    Args:
        package_dir: 商家材料包根目录（含 `data/` 与 `docs/`）。

    Returns:
        `(导入行列表, 未识别的源文件名列表)`。
    """
    data_dir = package_dir / "data"
    docs_dir = package_dir / "docs"
    rows: list[ImportRow] = []
    found: list[str] = []

    products_path = data_dir / "商品知识卡.csv"
    if products_path.exists():
        rows.extend(load_product_cards(products_path))
        found.append(products_path.name)

    faq_path = data_dir / "FAQ知识库.csv"
    if faq_path.exists():
        rows.extend(load_faq(faq_path))
        found.append(faq_path.name)

    for name, meta in POLICY_SOURCES.items():
        policy_path = docs_dir / name
        if policy_path.exists():
            rows.extend(load_policy_doc(policy_path, meta=meta))
            found.append(name)

    missing = [
        str(p)
        for p in (
            products_path,
            faq_path,
            *(docs_dir / name for name in POLICY_SOURCES),
        )
        if not p.exists()
    ]
    return rows, missing


def rows_to_metadata(rows: list[ImportRow]) -> list[ChunkMetadata]:
    """把导入行转成契约层的 `ChunkMetadata`（入库前用）。

    Args:
        rows: 导入行列表。

    Returns:
        与 `rows` 一一对应的元数据列表。
    """
    return [
        ChunkMetadata(
            category=row.category,
            sub_category="",
            sku_ids=list(row.sku_ids),
            knowledge_type=row.knowledge_type,
            source=row.source,
            authority=row.authority,
            channel=["all"],
            brand="玩可爱",
            keywords=list(row.keywords),
            updated_by="import",
            version=1,
            review_status=ReviewStatus.APPROVED,
            source_row=row.source_row,
        )
        for row in rows
    ]


def preview_response(
    source_path: str,
    rows: list[ImportRow],
    *,
    total_rows: int,
    duration_ms: int,
) -> ImportPreviewResponse:
    """组装导入预览响应（`POST /internal/v1/kb/import` 的 `dry_run` 结果）。

    预览块用**临时 `preview_chunk_id`**（`pv_来源_行号`）标识：此时还没有真正
    入库，不能占用正式的 `kb_{type}_{序号}` 编号，否则运营把预览当真去查会 404，
    真正的导入又会撞号。

    Args:
        source_path: 源文件标识。
        rows: 导入行列表。
        total_rows: 源文件数据行数。
        duration_ms: 解析耗时。

    Returns:
        `ImportPreviewResponse`。
    """
    warnings: list[str] = []
    out_of_range = 0
    for row in rows:
        warnings.extend(row.warnings)
        if row.warnings:
            out_of_range += 1

    return ImportPreviewResponse(
        source_path=source_path,
        total_rows=total_rows,
        total_chunks=len(rows),
        chunks=[
            {
                "chunk_id": f"pv_{row.source or source_path}_{row.source_row or idx}",
                "title": row.title,
                "content": row.content,
                "metadata": ChunkMetadata(
                    category=row.category,
                    knowledge_type=row.knowledge_type,
                    sku_ids=list(row.sku_ids),
                    keywords=list(row.keywords),
                    source=row.source or source_path,
                    source_row=row.source_row,
                    authority=row.authority,
                    effective_from=row.effective_from,
                ),
            }
            for idx, row in enumerate(rows, 1)
        ],
        warnings=warnings,
        length_out_of_range=out_of_range,
    )


__all__ = [
    "POLICY_SOURCES",
    "ImportRow",
    "collect_merchant_package",
    "load_faq",
    "load_policy_doc",
    "load_product_cards",
    "preview_response",
    "rows_to_metadata",
]

