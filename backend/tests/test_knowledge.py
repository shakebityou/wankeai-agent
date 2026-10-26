"""BE-06 知识库单测（切分器 + 导入器）。

任务书 3.6 节 DoD 的关键点：
* 商品知识一款一单元 200–400 字；售后政策一条一单元 100–300 字；
  FAQ 一问一答 50–200 字；长文档标题层级递归 300–500 字；重叠 15%。
* **库存/价格字段不得进向量库**（HC-3 硬约束），这是本项目最容易
  被"顺手多加一个字段"破坏的地方，必须有单测把住。
* 切分必须**不丢块**——导入时少一块等于线上少一条知识，且不报错。

设计约定：
* 切分器是纯函数，用例直接喂构造好的行/文档，不读真实材料（读真实材料
  的完整性校验在 `scripts/check_kb_import.py`，那是 DoD 自检不是单测）。
* 断言用「区间」而非精确字数：字数会随文案微调变化，区间才是契约。
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 商品知识卡切分
# ---------------------------------------------------------------------------


def _product_row(**overrides: str) -> dict[str, str]:
    """构造一行商品知识卡（列名与商家 CSV 完全一致）。

    Args:
        **overrides: 覆盖默认值的列。

    Returns:
        CSV 行字典。
    """
    row = {
        "SKU编码": "WK-BEAR-20",
        "商品名": "奶油小熊 20cm",
        "类目": "毛绒玩偶",
        "尺寸": "20cm",
        "材质": "水晶超柔",
        "填充物": "PP棉",
        "适用年龄": "6岁以上",
        "洗护方式": "手洗，30℃以下温水轻揉，不可机洗",
        "包装形式": "卡纸吊牌+PE袋",
        "上市时间": "2025-03-01",
        "是否IP联名": "否",
        "吊牌价": "129",
        "现价": "99",
        "库存状态": "IN_STOCK",
        "核心卖点": "超柔面料，抱感蓬松",
        "知识文本": "奶油小熊是店内销量最好的基础款，适合作为礼物。",
        "更新日期": "2026-10-15",
    }
    row.update(overrides)
    return row


class TestChunkProductCard:
    """商品知识一款一单元。"""

    def test_single_row_single_chunk(self) -> None:
        """一行商品卡切出恰好一个块。"""
        from app.services.knowledge.splitter import chunk_product_card

        chunk = chunk_product_card(_product_row(), row_no=2)
        assert chunk.title
        assert chunk.content

    def test_contains_sku_and_name(self) -> None:
        """正文必须含 SKU 与商品名（检索靠它们命中）。"""
        from app.services.knowledge.splitter import chunk_product_card

        chunk = chunk_product_card(_product_row(), row_no=2)
        assert "WK-BEAR-20" in chunk.content
        assert "奶油小熊" in chunk.content

    def test_price_not_in_content(self) -> None:
        """**价格不得进正文**（HC-3：会变的数字永远走接口）。

        价格进知识库的直接后果是：调价后模型仍按旧价回答，
        而且向量库无法感知调价——这是合规一票否决项。
        """
        from app.services.knowledge.splitter import chunk_product_card

        chunk = chunk_product_card(_product_row(), row_no=2)
        assert "99" not in chunk.content.replace("6岁以上", "")
        assert "129" not in chunk.content

    def test_stock_not_in_content(self) -> None:
        """**库存状态不得进正文**（同上，库存走接口）。"""
        from app.services.knowledge.splitter import chunk_product_card

        chunk = chunk_product_card(_product_row(), row_no=2)
        assert "IN_STOCK" not in chunk.content

    def test_sku_id_in_content_for_traceability(self) -> None:
        """SKU 必须进正文（检索靠它命中；按商品筛选靠 metadata.sku_ids）。

        `RawChunk` 只有 title/content/clause_no 等字段，**没有 extra**：
        价格/库存这类禁用字段根本不该出现在切分产物里，
        所以 `chunk_product_card` 的 docstring 说"写进 extra"是过时描述，
        实际是**整列丢弃**，比放 extra 更安全。
        """
        from app.services.knowledge.splitter import chunk_product_card

        chunk = chunk_product_card(_product_row(), row_no=2)
        assert "WK-BEAR-20" in chunk.content


# ---------------------------------------------------------------------------
# FAQ 切分
# ---------------------------------------------------------------------------


def _faq_row(**overrides: str) -> dict[str, str]:
    """构造一行 FAQ。

    Args:
        **overrides: 覆盖默认值的列。

    Returns:
        CSV 行字典。
    """
    row = {
        "standard_q": "毛绒玩偶可以机洗吗",
        "similar_q": "毛绒玩偶能丢洗衣机吗|玩偶可以机洗吗",
        "answer": "不建议机洗。水晶超柔、仿兔毛面料机洗会打结掉毛，建议手洗或干洗。",
        "category": "洗护保养",
        "authority": "商品主数据表·洗护方式字段",
        "review_status": "APPROVED",
        "reviewer": "李敏",
    }
    row.update(overrides)
    return row


class TestChunkFaqRow:
    """FAQ 一问一答一单元。"""

    def test_single_row_single_chunk(self) -> None:
        """一行 FAQ 切出一个块（相似问并入，不单独建块）。"""
        from app.services.knowledge.splitter import chunk_faq_row

        chunk = chunk_faq_row(_faq_row(), row_no=2)
        assert chunk.title == "毛绒玩偶可以机洗吗"

    def test_similar_questions_merged(self) -> None:
        """相似问必须在正文里（否则用户换个说法就检索不到）。"""
        from app.services.knowledge.splitter import chunk_faq_row

        chunk = chunk_faq_row(_faq_row(), row_no=2)
        assert "洗衣机" in chunk.content

    def test_answer_present(self) -> None:
        """答案正文必须在块里。"""
        from app.services.knowledge.splitter import chunk_faq_row

        chunk = chunk_faq_row(_faq_row(), row_no=2)
        assert "不建议机洗" in chunk.content

    def test_empty_answer_tolerated(self) -> None:
        """空答案不抛异常（材料质量问题要在导入报告里体现，不能崩）。"""
        from app.services.knowledge.splitter import chunk_faq_row

        chunk = chunk_faq_row(_faq_row(answer=""), row_no=2)
        assert chunk.title


# ---------------------------------------------------------------------------
# Markdown 标题层级切分
# ---------------------------------------------------------------------------


class TestSplitMarkdown:
    """长文档按标题层级递归切分。"""

    DOC = """# 售后政策

## 1. 退货

### 1.1 七天无理由

自签收之日起 7 天内，商品不影响二次销售可申请无理由退货。
""" + ("补充说明。" * 20) + """

### 1.2 质量问题

质量问题由商家承担运费，需提供瑕疵照片。
"""

    def test_splits_by_heading(self) -> None:
        """一级/二级标题各自成块。"""
        from app.services.knowledge.splitter import split_markdown_by_heading

        chunks = split_markdown_by_heading(self.DOC, min_len=50, max_len=400)
        assert len(chunks) >= 2

    def test_titles_captured(self) -> None:
        """块标题必须来自标题层级（否则无法追溯条款出处）。"""
        from app.services.knowledge.splitter import split_markdown_by_heading

        chunks = split_markdown_by_heading(self.DOC, min_len=50, max_len=400)
        titles = " ".join(c.title for c in chunks)
        assert "退货" in titles or "七天无理由" in titles

    def test_long_section_split(self) -> None:
        """超长小节要二次切分，且每块不超过 max_len 太多。"""
        from app.services.knowledge.splitter import split_markdown_by_heading

        long_doc = "# 政策\n\n## 1. 规则\n\n" + ("这是一条很长的规则说明。" * 80)
        chunks = split_markdown_by_heading(long_doc, min_len=50, max_len=300)
        assert len(chunks) > 1
        # 重叠 15% 允许略超上限，但不该出现 3 倍超限的块
        assert all(c.length <= 300 * 3 for c in chunks)

    def test_no_content_lost(self) -> None:
        """切分不得丢正文（丢块是静默的数据损失）。

        注意：标题文字不进 `content`（只在 `title` 里），所以这里断言
        正文句子而不是标题词。这也是为什么检索要走 title+content 拼接。
        """
        from app.services.knowledge.splitter import split_markdown_by_heading

        chunks = split_markdown_by_heading(self.DOC, min_len=50, max_len=400)
        joined = "".join(c.content for c in chunks)
        assert "7 天内" in joined
        assert "质量问题由商家承担运费" in joined
        # 标题必须被保留在 title 里，否则条款无法追溯
        titles = " ".join(c.title for c in chunks)
        assert "七天无理由" in titles

    def test_empty_input(self) -> None:
        """空文档返回空列表，不抛异常。"""
        from app.services.knowledge.splitter import split_markdown_by_heading

        assert split_markdown_by_heading("", min_len=10, max_len=100) == []


# ---------------------------------------------------------------------------
# 关键词提取
# ---------------------------------------------------------------------------


class TestExtractKeywords:
    """关键词提取（jieba 词性过滤）。"""

    def test_returns_terms(self) -> None:
        """能提取出实词。"""
        from app.services.knowledge.splitter import extract_keywords

        kws = extract_keywords("毛绒玩偶可以机洗吗，水晶超柔面料不建议机洗", top_n=5)
        assert kws
        assert len(kws) <= 5

    def test_empty_text(self) -> None:
        """空文本返回空列表。"""
        from app.services.knowledge.splitter import extract_keywords

        assert extract_keywords("") == []


# ---------------------------------------------------------------------------
# HC-3：禁用字段守卫
# ---------------------------------------------------------------------------


class TestForbiddenFieldsGuard:
    """向量库元数据禁用字段（HC-3 一票否决项）。"""

    def test_price_and_stock_forbidden(self) -> None:
        """价格/库存类字段必须在禁用清单里。"""
        from app.adapters.vector_store import FORBIDDEN_FIELDS

        for field in ("price", "current_price", "list_price", "stock_status", "stock"):
            assert field in FORBIDDEN_FIELDS, f"{field} 未列入禁用字段"

    def test_assert_raises_on_forbidden(self) -> None:
        """写入带禁用字段的元数据必须直接抛错（不能只告警）。"""
        from app.adapters.vector_store import assert_no_forbidden_fields

        with pytest.raises(ValueError):
            assert_no_forbidden_fields({"category": "毛绒玩偶", "price": 99})

    def test_assert_passes_clean_metadata(self) -> None:
        """干净元数据不抛错。"""
        from app.adapters.vector_store import assert_no_forbidden_fields

        assert_no_forbidden_fields(
            {"category": "毛绒玩偶", "sku_ids": ["WK-BEAR-20"], "authority": 1}
        )
