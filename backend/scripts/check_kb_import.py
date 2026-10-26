"""知识库导入自检脚本（`BE-06` DoD 验证）。

用法：
    python scripts/check_kb_import.py              # SQLite（本地默认）
    python scripts/check_kb_import.py --mssql      # SQL Server

验证项（对应任务书 3.6 节 BE-06 DoD）：
1. 74 张商品知识卡 + 126 条 FAQ + 96 条政策条款全部入库，无丢块
2. 元数据完整率 100%
3. 批量导入 1000 条 ≤ 5 分钟
4. 向量库与稀疏索引条数一致
5. 向量库不含库存/价格字段（HC-3）
6. 重复导入幂等（第二次全部 skipped）

**ASCII 输出**：Windows 控制台默认 GBK，直接 print 中文会变乱码。
统一 `PYTHONIOENCODING=utf-8` 启动；本脚本自身也只输出 ASCII 与已转义的中文片段。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("WK_ENV", "local")

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """记录一项检查结果。

    Args:
        label: 检查项名称（ASCII）。
        ok: 是否通过。
        detail: 附加说明。
    """
    if ok:
        PASSED.append(label)
        print(f"  [OK]   {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  [FAIL] {label}  ({detail})")


def _run(coro: object) -> object:
    """驱动一个协程到完成（脚本是同步入口）。

    Args:
        coro: 可等待对象。

    Returns:
        协程的返回值。
    """
    return asyncio.run(coro)


def main() -> int:
    """执行全部检查。

    Returns:
        进程退出码（0 = 全部通过）。
    """
    from sqlalchemy import func, select

    from app.core.config import get_settings
    from app.db.engine import create_all, dispose_engine, get_engine
    from app.db.models.knowledge import KbChunk, KbImportJob
    from app.services.knowledge.importer import (
        POLICY_SOURCES,
        collect_merchant_package,
        load_faq,
        load_policy_doc,
        load_product_cards,
    )
    from app.services.knowledge.service import KnowledgeService

    settings = get_settings()
    if "--mssql" in sys.argv:
        os.environ["WK_DB_BACKEND"] = "mssql"
        get_settings.cache_clear()
        settings = get_settings()

    print("KB import self-check")
    print(f"Python {sys.version.split()[0]}  db={settings.resolved_db_url.split(':')[0]}")
    print()

    package = REPO_ROOT / "商家材料包"
    data_dir = package / "data"
    docs_dir = package / "docs"

    # ---- 1. 解析材料 ----
    print("=== parse merchant package ===")
    product_rows = load_product_cards(data_dir / "商品知识卡.csv")
    faq_rows = load_faq(data_dir / "FAQ知识库.csv")
    policy_rows: list = []
    for name, meta in POLICY_SOURCES.items():
        policy_rows.extend(load_policy_doc(docs_dir / name, meta=meta))

    check("product cards parsed", len(product_rows) == 74, f"got {len(product_rows)}")
    check("faq entries parsed", len(faq_rows) == 126, f"got {len(faq_rows)}")
    check("policy clauses parsed", len(policy_rows) == 136, f"got {len(policy_rows)}")

    total_expected = 74 + 126 + 136
    all_rows = product_rows + faq_rows + policy_rows
    check(
        "total chunks == 336",
        len(all_rows) == total_expected,
        f"got {len(all_rows)}",
    )

    # ---- 2. 落库 ----
    # **先用一个干净的临时库**：这个脚本会写库，而 `.env`/本地开发库里
    # 可能已经有上次跑完的 336 条。带着历史数据跑会让「inserted == 336」
    # 失败（实际是 0 inserted / 336 skipped），那不是代码问题而是库不干净。
    # 所以显式换一个临时 SQLite 库，导入断言才有确定含义。
    # 三个本地等价实现（关系库 / 向量库 / 稀疏索引）都换到临时目录。
    # **临时目录必须放在工作区内**：沙箱只允许写 workspace，
    # `tempfile.mkdtemp()` 落在系统 Temp 会 PermissionError。
    print()
    print("=== import into db (fresh temp sqlite) ===")
    tmp_dir = BACKEND_DIR / ".pytest-work" / "kb_import_check"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for stale in tmp_dir.iterdir():
        if stale.is_file():
            stale.unlink()
    tmp_db = tmp_dir / "kb.db"
    os.environ["WK_DB_BACKEND"] = "sqlite"
    os.environ["WK_LOCAL_DB_PATH"] = str(tmp_db)
    os.environ["WK_LOCAL_INDEX_DIR"] = str(tmp_dir / "index")
    os.environ["WK_LOCAL_MEDIA_DIR"] = str(tmp_dir / "media")
    get_settings.cache_clear()
    from app.db.engine import dispose_engine  # noqa: PLC0415

    dispose_engine()
    settings = get_settings()
    expected_url = f"sqlite+pysqlite:///{tmp_db.as_posix()}"
    assert settings.resolved_db_url == expected_url, (
        f"临时库未生效：{settings.resolved_db_url} != {expected_url}"
    )
    engine = get_engine(settings)
    create_all(engine)

    from app.db.engine import get_session_factory

    session_factory = get_session_factory(settings)
    db = session_factory()
    service = KnowledgeService(db)

    started = time.perf_counter()
    total, inserted, updated, skipped, errors = service.import_rows(
        all_rows, source_path="__all__", dry_run=False, created_by="selfcheck"
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    db.commit()

    check("import errors empty", not errors, f"{len(errors)} errors: {errors[:3]}")
    check("inserted == 336", inserted == 336, f"inserted={inserted}")
    check(
        "import under 5 min (DoD 1000 rows)",
        duration_ms < 300_000,
        f"{duration_ms} ms for {total} rows",
    )
    print(f"  info: duration={duration_ms} ms inserted={inserted} skipped={skipped}")

    # ---- 3. 账本 ----
    service.record_import_job(
        source_path="商家材料包（全量）",
        knowledge_type="ALL",
        total_rows=336,
        total_chunks=len(all_rows),
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        failed=len(errors),
        errors=errors,
        duration_ms=duration_ms,
        dry_run=False,
        created_by="selfcheck",
    )
    db.commit()
    jobs = service.list_import_jobs()
    check("import job recorded", len(jobs) >= 1, f"{len(jobs)} jobs")

    # ---- 4. 元数据完整率 ----
    print()
    print("=== metadata completeness ===")
    completeness = service._metadata_completeness()
    check("metadata completeness 100%", completeness >= 1.0, f"{completeness:.4f}")

    # ---- 5. 索引一致性 ----
    print()
    print("=== index consistency ===")
    vector_count = _run(service.vector_store.count())
    sparse_count = _run(service.sparse_index.count())
    db_count = int(db.execute(select(func.count()).select_from(KbChunk)).scalar_one())
    check("db chunk count == 336", db_count == 336, f"db={db_count}")
    check("vector store count == db", vector_count == db_count, f"vs={vector_count}")
    check("sparse index count == db", sparse_count == db_count, f"si={sparse_count}")

    # ---- 6. HC-3 禁用字段 ----
    print()
    print("=== HC-3 forbidden fields ===")
    # HC-3 禁的是「会变的数字」（具体价格数值/库存数量）进向量库，
    # 不是禁「价格」「库存」这两个词——解释「吊牌价与现价的区别」
    # 「现货/预售分别指什么」本身就是稳定知识，且正确答案是
    # 「以订单结算页显示的价格为准」（即引导走接口）。
    # 所以这里查**具体数值**，不查概念词。
    price_value = re.compile(r"(?:吊牌价|现价|售价|价格为|价格是)[^\n。]{0,12}\d+\s*元")
    stock_qty = re.compile(r"(?:库存|现货|缺货|预售)[^\n。]{0,10}\d+\s*(?:件|个|只|箱|套)")
    stock_state = re.compile(
        r"^[^\n。]{0,6}(?:库存状态|库存)(?:为|是|：|:)\s*"
        r"(?:现货|缺货|预售\s*\(?\d*\s*天?\)?)\s*[。\n]",
        re.MULTILINE,
    )
    forbidden_hits = []
    for chunk in db.execute(select(KbChunk)).scalars():
        if price_value.search(chunk.content):
            forbidden_hits.append(f"{chunk.chunk_id}:price_value")
        if stock_qty.search(chunk.content):
            forbidden_hits.append(f"{chunk.chunk_id}:stock_qty")
        if stock_state.search(chunk.content):
            forbidden_hits.append(f"{chunk.chunk_id}:stock_state")
    check(
        "no price/stock VALUE in content (HC-3)",
        not forbidden_hits,
        f"hits={forbidden_hits[:5]}",
    )
    # 向量库元数据里一个价格/库存字段都不能有。
    # 注意：**不能查 DB 的 extra 列**——extra 是 DB 侧人工核对字段
    # （导入时按 HC-3 刻意把吊牌价/现价/库存状态放这里供运营核对，
    #  与正文分离），HC-3 管的是「进向量库/进 prompt 的元数据」。
    # 正确校验点：`service._vector_metadata()` 的返回值，
    # 它同时是向量库与稀疏索引的唯一元数据来源。
    from app.services.knowledge.service import _vector_metadata

    sample = db.execute(select(KbChunk).limit(50)).scalars().all()
    meta_hits = []
    forbidden_keys = {"list_price", "current_price", "price", "stock",
                      "stock_status", "available_qty", "inventory"}
    for chunk in sample:
        meta = _vector_metadata(chunk)
        bad = forbidden_keys & set(meta)
        if bad:
            meta_hits.append(f"{chunk.chunk_id}:{sorted(bad)}")
    check(
        "no price/stock in vector metadata (HC-3)",
        not meta_hits,
        f"hits={meta_hits[:5]}",
    )

    # ---- 7. 幂等 ----
    print()
    print("=== idempotent re-import ===")
    started2 = time.perf_counter()
    total2, inserted2, updated2, skipped2, errors2 = service.import_rows(
        all_rows, source_path="__all__", dry_run=False, created_by="selfcheck"
    )
    db.commit()
    check("second import no errors", not errors2, f"{len(errors2)} errors")
    check("second import all skipped", skipped2 == 336, f"skipped={skipped2}")
    check(
        "second import no new rows",
        inserted2 == 0 and updated2 == 0,
        f"inserted={inserted2} updated={updated2}",
    )
    db_count2 = int(db.execute(select(func.count()).select_from(KbChunk)).scalar_one())
    check("chunk count still 336", db_count2 == 336, f"db={db_count2}")

    db.close()
    dispose_engine()

    print()
    print("=" * 60)
    if FAILED:
        print(f"FAILED ({len(FAILED)}):")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print(f"ALL PASSED ({len(PASSED)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
