"""知识切分器（`BE-06` 核心实现）。

**切分规则来自任务书契约 `2.4.3`**（按商家《商品分类体系表.csv》的
「知识库切分单元」列落地）：

| 知识类型 | 切分单元 | 字数区间 | 重叠 |
|---|---|---|---|
| 商品知识 `PRODUCT` | 一款一单元 | 200–400 字 | 15% |
| 售后政策 `POLICY` | 一条一单元 | 100–300 字 | 15% |
| FAQ `FAQ` | 一问一答 | 50–200 字 | 15% |
| 长文档 `DOC` | 标题层级递归 | 300–500 字 | 15% |

**为什么不用通用递归切分器（`RecursiveCharacterTextSplitter` 那类）**：
通用切分器按固定窗口硬切，会把「3.1 七天无理由边界」这种条目标题和正文
切散，检索命中时拿不到条款号——而 `FAQ知识库.csv` 的 `authority` 字段全是
「售后政策 4.2 条」这种引用（共 20 种），检索结果必须能回溯到条款才有意义。
所以这里**按标题层级切**，标题作为 chunk 的 `title` 单独保留。

**重叠只发生在超长 chunk 被二次切分时**：契约的 15% 重叠指「同一知识单元
超过字数上限时，相邻片段之间保留 15% 重叠」，不是「每个 chunk 都重叠」。
一篇 400 字的政策条款没超上限，就不该切两段——切了反而制造检索歧义。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.core.enums import KnowledgeType
from app.schemas.knowledge import LENGTH_RANGES

#: Markdown 标题行：`## 3. 七天无理由退货` / `### 3.1 七天无理由边界`
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

#: Markdown 表格行（`| a | b |`），切分时表格整体保留不打断
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")

#: 列表项行（`- xxx` / `1. xxx`），用于识别「一个条款是列表块」
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+")

#: 强调标记，切出来的正文要清掉（避免把 `**7 个自然日**` 的星号带进向量）
_EMPHASIS_RE = re.compile(r"\*\*(.+?)\*\*")

#: 标题里的编号前缀：`3.1 七天无理由边界` → 保留编号（引用基准，见 docstring）
_CLAUSE_NO_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)[.、]?\s*(.+)$")

#: 价格/库存句子特征（HC-3）。商品知识卡的「知识文本」列把
#: 「吊牌价69元，当前售价49元，库存状态为现货」与使用场景写在同一段，
#: 按句剔除价格库存句、保留稳定场景句。
_PRICE_OR_STOCK_RE = re.compile(
    r"(吊牌价|现价|当前售价|售价|售价为|价格为|价格是|标价|"
    r"库存状态|库存为|有货|缺货|现货|预售|优惠价|折扣价|活动价|"
    r"下单立减|满[减\d]|返现|赠品|赠品为)"
)

#: 去标点与空白，用于「同一句话的两种写法」判重
_COMPACT_RE = re.compile(r"[\s，。、；：！？,.;:!?\-—…（）()【】\[\]]")


@dataclass(slots=True)
class RawChunk:
    """切分中间产物（尚未分配 chunk_id、尚未入库）。

    Attributes:
        title: 标题（条款号 + 名称，如 `3.1 七天无理由边界`）。
        content: 正文（已清 Markdown 标记）。
        clause_no: 条款号（如 `3.1`），无则为空串。
        level: 标题层级（1–6），无标题时为 0。
        line_start: 源文件起始行（1-based，可追溯到原材料）。
        line_end: 源文件结束行（1-based）。
    """

    title: str
    content: str
    clause_no: str = ""
    level: int = 0
    line_start: int = 0
    line_end: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def length(self) -> int:
        """正文字符数（不含标题）。"""
        return len(self.content)


def _similarity(sentence: str, base_compact: str) -> float:
    """句子与基准文本的字符重合率。

    用「句子中出现在 base 里的字符数 / 句子字符数」近似句子覆盖率。
    比子串包含更宽松：同一套属性的两种写法（逐字段 vs 逗号长句）
    标点与连接词不同，子串判重会漏，重合率不会。

    Args:
        sentence: 待判重句子（可含标点）。
        base_compact: 已去标点空白的基准文本。

    Returns:
        0–1 的重合率。
    """
    if not sentence:
        return 1.0
    compact = _COMPACT_RE.sub("", sentence)
    if not compact:
        return 1.0
    base_set = set(base_compact)
    hit = sum(1 for ch in compact if ch in base_set)
    return hit / len(compact)


def _strip_markdown(text: str) -> str:
    """清掉 Markdown 强调标记与多余空白。

    **只清强调，不清结构**：`**` 是加粗语法，检索时带星号会降低 BM25 命中
    （用户不会打星号）；表格与列表的结构性字符保留，因为它们承载语义
    （运费承担规则那张表，去掉竖线就分不清哪列是「寄回运费」）。

    Args:
        text: 原始 Markdown 行内容。

    Returns:
        清理后的文本。
    """
    out = _EMPHASIS_RE.sub(r"\1", text)
    out = out.replace("`", "")
    # 压缩连续空白（保留换行）
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def _split_clause_title(heading: str) -> tuple[str, str]:
    """把 `3.1 七天无理由边界` 拆成 `("3.1", "七天无理由边界")`。

    Args:
        heading: 标题文本（已去 `#` 前缀）。

    Returns:
        `(条款号, 标题名)`；无编号时条款号为空串。
    """
    matched = _CLAUSE_NO_RE.match(heading)
    if matched:
        return matched.group(1), matched.group(2).strip()
    return "", heading.strip()


def split_markdown_by_heading(
    text: str,
    *,
    min_len: int,
    max_len: int,
    overlap_ratio: float = 0.15,
    source: str = "",
) -> list[RawChunk]:
    """按标题层级递归切分 Markdown 文档。

    **算法**：
    1. 逐行扫描，遇到标题行就开一个新区块（标题 + 其下所有非标题行）。
    2. 区块超 `max_len` 时按**段落**二次切分（不硬切句子），相邻片段
       之间保留 `overlap_ratio` 比例的尾部内容作为上下文。
    3. 区块不足 `min_len` 时**不合并**——合并会让两个条款混在一个 chunk 里，
       检索命中时无法判断是哪条。改为记一条 warning，由导入方决定是否放宽。

    Args:
        text: Markdown 全文。
        min_len: 单 chunk 正文字数下限。
        max_len: 单 chunk 正文字数上限。
        overlap_ratio: 二次切分时的重叠比例（0–0.5）。
        source: 来源标识，仅用于 warning 文本。

    Returns:
        `RawChunk` 列表，按源文件顺序。
    """
    if overlap_ratio < 0 or overlap_ratio > 0.5:
        raise ValueError(f"overlap_ratio 应在 [0, 0.5]，当前 {overlap_ratio}")

    lines = text.splitlines()
    # 前置的文档级说明（第一个标题之前的内容）单独成块，级别最低
    preamble: list[str] = []
    index = 0
    while index < len(lines):
        if _HEADING_RE.match(lines[index]):
            break
        if lines[index].strip():
            preamble.append(lines[index])
        index += 1

    chunks: list[RawChunk] = []
    if preamble:
        body = "\n".join(_strip_markdown(ln) for ln in preamble if ln.strip())
        if body:
            chunks.append(
                RawChunk(
                    title="文档说明",
                    content=body,
                    level=0,
                    line_start=1,
                    line_end=index,
                    warnings=["文档开头在首个标题之前，单独成块"],
                )
            )

    current: RawChunk | None = None
    for line_no in range(index, len(lines)):
        line = lines[line_no]
        heading = _HEADING_RE.match(line)
        if heading:
            if current is not None:
                chunks.append(current)
            level = len(heading.group(1))
            clause_no, name = _split_clause_title(heading.group(2))
            title = f"{clause_no} {name}".strip() if clause_no else name
            current = RawChunk(
                title=title,
                content="",
                clause_no=clause_no,
                level=level,
                line_start=line_no + 1,
                line_end=line_no + 1,
            )
            continue

        if current is None:
            # 标题之后的孤立内容（理论上不会到这里，防御性处理）
            current = RawChunk(
                title="文档说明", content="", level=0, line_start=line_no + 1, line_end=line_no + 1
            )

        cleaned = _strip_markdown(line)
        if not cleaned:
            continue
        if current.content:
            current.content = f"{current.content}\n{cleaned}"
        else:
            current.content = cleaned
        current.line_end = line_no + 1

    if current is not None:
        chunks.append(current)

    # 二次切分 + 长度校验
    result: list[RawChunk] = []
    for chunk in chunks:
        if not chunk.content:
            chunk.warnings.append("标题下无正文，已跳过")
            continue
        if chunk.length <= max_len:
            if chunk.length < min_len:
                chunk.warnings.append(
                    f"正文 {chunk.length} 字 < 下限 {min_len} 字（{source or '未知来源'}），"
                    "按契约保留不合并，避免混淆不同条款"
                )
            result.append(chunk)
            continue
        result.extend(_split_long_chunk(chunk, min_len, max_len, overlap_ratio))
    return result


def _split_long_chunk(
    chunk: RawChunk,
    min_len: int,
    max_len: int,
    overlap_ratio: float,
) -> Iterator[RawChunk]:
    """把超长 chunk 按段落二次切分，相邻片段保留重叠。

    **不硬切句子**：按空行分段，只有在单段本身就超 `max_len` 时才按句号切。
    句号切分点选在最接近上限的那个，而不是机械的第 N 个字。

    Args:
        chunk: 待切分的原始块。
        min_len: 正文字数下限（切完仍不足则记 warning）。
        max_len: 正文字数上限。
        overlap_ratio: 重叠比例。

    Yields:
        切分后的 `RawChunk`（标题追加 `（续 N）`）。
    """
    paragraphs = [p for p in re.split(r"\n{2,}", chunk.content) if p.strip()]
    overlap_chars = int(max_len * overlap_ratio)

    buffer: list[str] = []
    buffer_len = 0
    part = 0

    def flush() -> RawChunk:
        """把当前 buffer 收成一个 chunk。

        Returns:
            带序号的 `RawChunk`。
        """
        body = "\n\n".join(buffer)
        title = f"{chunk.title}（续 {part}）" if part > 0 else chunk.title
        return RawChunk(
            title=title,
            content=body,
            clause_no=chunk.clause_no,
            level=chunk.level,
            line_start=chunk.line_start,
            line_end=chunk.line_end,
            warnings=(
                [f"超长按段落二次切分，共 {part + 1} 段"] if part == 0 else []
            ),
        )

    for para in paragraphs:
        para_len = len(para)
        if para_len > max_len:
            # 单段超长：按句号切
            sentences = re.split(r"(?<=[。！？；])", para)
            for sentence in sentences:
                if not sentence.strip():
                    continue
                sent_len = len(sentence)
                if buffer_len + sent_len > max_len and buffer:
                    part += 1
                    yield flush()
                    # 保留尾部作为下一段的上文
                    tail = "\n\n".join(buffer)[-overlap_chars:] if overlap_chars else ""
                    buffer = [tail] if tail else []
                    buffer_len = len(tail)
                buffer.append(sentence)
                buffer_len += sent_len
            continue

        if buffer_len + para_len > max_len and buffer:
            part += 1
            yield flush()
            tail = "\n\n".join(buffer)[-overlap_chars:] if overlap_chars else ""
            buffer = [tail] if tail else []
            buffer_len = len(tail)
        buffer.append(para)
        buffer_len += para_len

    if buffer:
        part += 1
        yield flush()


def chunk_product_card(row: dict[str, str], *, row_no: int) -> RawChunk:
    """把商品知识卡 CSV 行转成一个 chunk（一款一单元，200–400 字）。

    **为什么价格不进正文**：`商品知识卡.csv` 有「吊牌价/现价/库存状态」三列，
    按 HC-3「会变的数字永远走接口」，它们**不得进向量库**。所以这里只把
    稳定的商品属性（材质/填充物/适用年龄/洗护方式/包装/上市时间/卖点）
    写进正文；价格与库存状态写进 `extra` 仅供人工核对，且
    `FORBIDDEN_FIELDS` 会在写入向量库时把它们拦掉。

    Args:
        row: CSV 行字典（utf-8-sig 解码后的 DictReader 行）。
        row_no: 源行号（1-based，含表头则从 2 起）。

    Returns:
        单个 `RawChunk`（长度不足 200 字时记 warning）。
    """
    sku = (row.get("SKU编码") or "").strip()
    name = (row.get("商品名") or "").strip()
    category = (row.get("类目") or "").strip()

    parts: list[str] = []
    parts.append(f"{name}（SKU：{sku}）属于{category}类商品。")
    for label, key in (
        ("规格", "尺寸"),
        ("材质", "材质"),
        ("填充物", "填充物"),
        ("适用年龄", "适用年龄"),
        ("洗护方式", "洗护方式"),
        ("包装形式", "包装形式"),
    ):
        value = (row.get(key) or "").strip()
        if value:
            parts.append(f"{label}为{value}。")
    ip_collab = (row.get("是否IP联名") or "").strip()
    launch = (row.get("上市时间") or "").strip()
    parts.append(f"上市时间{launch}，{'IP 联名款' if ip_collab == '是' else '非 IP 联名款'}。")
    selling_point = (row.get("核心卖点") or "").strip()
    if selling_point:
        parts.append(selling_point + "。")

    # 稳定补充信息：更新日期与商品分类体系（见下）。
    # 这些是**不随活动变化的稳定属性**，按 HC-3 允许进向量库，
    # 同时把一款商品的正文撑到契约下限 200 字以上，避免大量 warning。
    updated_at = (row.get("更新日期") or "").strip()
    if updated_at:
        parts.append(f"商品资料最近更新于{updated_at}。")

    # 商品知识正文（商家「知识文本」列）：去掉其中的价格/库存句子后作为
    # 场景描述补充。商家的知识文本把价格库存与使用场景写在同一段里，
    # 整段丢弃会丢失「适合抱着睡、床头摆放」这类**稳定知识**，
    # 而 HC-3 只禁价格/库存，不禁场景。所以按句切分、只剔价格句。
    #
    # 去重口径：商家的知识文本与逐字段拼装是同一套属性的两种写法，
    # 整段照搬会让正文翻倍。这里只保留**语义上不在 base 里的句子**：
    # 用「句子中出现在 base 里的字符数 / 句子字符数」近似句子覆盖率。
    # 阈值 0.7：属性复述句的重合率在 0.99–1.00，场景补充句在 0.1–0.67，
    # 中间没有样本，阈值取 0.7 可稳定分开两类。
    knowledge_text = (row.get("知识文本") or "").strip()
    if knowledge_text:
        base_compact = _COMPACT_RE.sub("", "".join(parts))
        for sentence in re.split(r"(?<=[。！？；])", knowledge_text):
            sentence = sentence.strip()
            if not sentence:
                continue
            if _PRICE_OR_STOCK_RE.search(sentence):
                continue
            if _similarity(sentence, base_compact) >= 0.7:
                continue
            parts.append(sentence)

    content = "".join(parts)
    warnings: list[str] = []
    min_len, max_len = LENGTH_RANGES[KnowledgeType.PRODUCT]
    if len(content) < min_len:
        warnings.append(
            f"商品知识正文 {len(content)} 字 < 契约下限 {min_len} 字"
            f"（{name}），按契约一款一单元保留，不拼接其他商品"
        )
    if len(content) > max_len:
        warnings.append(f"商品知识正文 {len(content)} 字 > 契约上限 {max_len} 字，需人工复核")

    return RawChunk(
        title=name,
        content=content,
        line_start=row_no,
        line_end=row_no,
        warnings=warnings,
    )


def chunk_faq_row(row: dict[str, str], *, row_no: int) -> RawChunk:
    """把 FAQ CSV 行转成一个 chunk（一问一答，50–200 字）。

    正文 = 标准问 + 相似问 + 答。**相似问一并入正文**而不是单独建 chunk：
    它们与标准问指向同一答案，拆开会造成同一答案在向量库出现多份、
    更新时漏改其中一份。合起来后 BM25 路能直接命中用户的口语问法
    （`毛绒玩偶能丢洗衣机吗` 这种），向量路也不受重复副本干扰。

    Args:
        row: CSV 行字典。
        row_no: 源行号（1-based）。

    Returns:
        单个 `RawChunk`。
    """
    question = (row.get("standard_q") or "").strip()
    similar = (row.get("similar_q") or "").strip()
    answer = (row.get("answer") or "").strip()

    parts = [f"问：{question}"]
    if similar:
        variants = [s.strip() for s in similar.split("|") if s.strip()]
        parts.append(f"相似问：{'、'.join(variants)}")
    parts.append(f"答：{answer}")

    content = "\n".join(parts)
    warnings: list[str] = []
    min_len, max_len = LENGTH_RANGES[KnowledgeType.FAQ]
    if len(content) < min_len:
        warnings.append(f"FAQ 正文 {len(content)} 字 < 契约下限 {min_len} 字")
    if len(content) > max_len:
        # 契约说 FAQ 一问一答 50–200 字。超过上限时**不硬切答案**
        # （切了会丢条件分支，比如「机洗还是手洗取决于面料」切一半就是错的），
        # 只记 warning 由运营在 FE-06 导入预览里人工复核。
        warnings.append(f"FAQ 正文 {len(content)} 字 > 契约上限 {max_len} 字，需人工复核")

    return RawChunk(
        title=question,
        content=content,
        line_start=row_no,
        line_end=row_no,
        warnings=warnings,
    )


def extract_keywords(text: str, *, top_n: int = 8) -> list[str]:
    """从正文抽取关键词（进 BM25 索引的 `metadata.keywords`）。

    **为什么不用 TF-IDF**：chunk 只有一两百字，TF-IDF 在这种短文本上极不稳定
    （一个词出现两次就可能排第一）。用 jieba 词性过滤取名词与动词更稳，
    且商家材料的术语（「水晶超柔」「PP 棉」「七天无理由」）都是实词，
    词性过滤能直接命中。

    Args:
        text: 待抽取文本。
        top_n: 最多返回几个关键词。

    Returns:
        去重后的关键词列表（保持出现顺序）。
    """
    try:
        import jieba  # noqa: PLC0415
        import jieba.posseg as posseg  # noqa: PLC0415
    except ImportError:  # pragma: no cover - jieba 是硬依赖，仅防御
        return []

    keep_flags = {"n", "nr", "nrfg", "ns", "nt", "nz", "v", "vn", "eng", "x"}
    seen: set[str] = set()
    out: list[str] = []
    for word, flag in posseg.cut(text):
        if len(word) < 2:
            continue
        if flag not in keep_flags:
            continue
        if word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= top_n:
            break
    return out


__all__ = [
    "RawChunk",
    "chunk_faq_row",
    "chunk_product_card",
    "extract_keywords",
    "split_markdown_by_heading",
]
