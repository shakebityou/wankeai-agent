"""测试夹具（conftest）。

设计原则：
1. **测试绝不连真实依赖**——`WK_ENV=local` 强制走等价适配器（内存缓存 /
   numpy 向量库 / jieba BM25 / 本地媒体目录），CI 里没有 Redis/PG/Milvus 也能全绿。
2. **每个用例前重置单例**——适配器与 Settings 都是 `lru_cache` 单例，用例间
   必须隔离，否则前一个用例的配置会污染后一个（这类 bug 极难定位）。
3. **临时目录而非仓库 `.local/`**——避免测试往工作区写文件，也避免并发用例互踩。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 环境变量必须在 import app.* 之前设置：
# Settings 是 lru_cache 单例，一旦被某个模块 import 时就实例化，后面再改环境
# 变量也不生效。
# ---------------------------------------------------------------------------
os.environ.setdefault("WK_ENV", "local")
os.environ.setdefault("WK_DEBUG", "false")
os.environ.setdefault("WK_COMPLIANCE_GATE_ENABLED", "true")
os.environ.setdefault("WK_VERIFY_CALLBACK_SIGNATURE", "false")
os.environ.setdefault("WK_APP_ID", "dsh_agent_001")
os.environ.setdefault("WK_APP_SECRET", "test_app_secret")
os.environ.setdefault("WK_INTERNAL_TOKEN", "test_internal_token")


def pytest_configure(config: pytest.Config) -> None:
    """把 pytest 临时目录钉在仓库内，并修正 Windows 受限沙箱下的目录权限。

    三处环境适配（都只在 Windows 受限沙箱下需要，正常环境无副作用）：

    1. **临时目录位置**：系统临时目录（`%TEMP%`）在受限沙箱下不可写，会让所有
       用到 `tmp_path` 的用例在 setup 阶段就 PermissionError。改到
       `backend/.pytest-tmp`（已在 `.gitignore` 忽略）。
    2. **目录创建模式**：pytest 用 `mkdir(mode=0o700)` 建临时目录。在 DSH 的
       Windows 文件沙箱下，这个模式产生的 ACL 会让**目录自身连列举都被拒**
       （`os.listdir` → WinError 5），于是 `tmp_path` 一建出来就不可用，
       而且 session 结束时 `rmtree` 也会失败。改成 `0o755` 即可正常读写。
    3. **目录清理**：pytest 在 session 结束时会调 `cleanup_dead_symlinks()`，
       它对 basetemp 做 `iterdir()`，同样撞上沙箱拒绝，导致整个 session 以
       PermissionError 收尾——即使全部用例都已通过。换成空操作；
       `tmp_path` 的实际清理由 pytest 的 `tmp_path_retention_policy` 负责。

    Args:
        config: pytest 配置对象。
    """
    base = Path(__file__).resolve().parents[1] / ".pytest-work"
    base.mkdir(parents=True, exist_ok=True)
    if not config.option.basetemp:
        config.option.basetemp = str(base)
    # 让 TempPathFactory 的 temproot 也落在工作区内（同样绕开 %TEMP% 限制）
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(base.parent))

    import _pytest.pathlib as _pathlib
    import _pytest.tmpdir as _tmpdir

    # 修正 1：目录一律用可列举的模式创建
    _orig_numbered = _pathlib.make_numbered_dir

    def _numbered_dir(root: Path, prefix: str, mode: int = 0o755) -> Path:
        """与 pytest 原实现相同，但模式固定为 0o755。

        Args:
            root: 父目录。
            prefix: 目录名前缀。
            mode: 忽略；一律用 0o755。

        Returns:
            新建目录路径。
        """
        return _orig_numbered(root, prefix, mode=0o755)

    _tmpdir.make_numbered_dir = _numbered_dir  # type: ignore[assignment]

    # 修正 2：basetemp 不要由 pytest 建（它会 mkdir(mode=0o700)）
    _orig_getbasetemp = _tmpdir.TempPathFactory.getbasetemp

    def _getbasetemp(self):  # type: ignore[no-untyped-def]
        """复用已存在的 basetemp，跳过 pytest 的 rm_rf + mkdir(0o700)。

        Args:
            self: TempPathFactory 实例。

        Returns:
            basetemp 路径。
        """
        if self._basetemp is not None:
            return self._basetemp
        given = self._given_basetemp
        if given is not None:
            given.mkdir(parents=True, mode=0o755, exist_ok=True)
            self._basetemp = given.resolve()
            return self._basetemp
        return _orig_getbasetemp(self)

    _tmpdir.TempPathFactory.getbasetemp = _getbasetemp  # type: ignore[assignment]

    # 修正 3：关掉会撞沙箱的 dead-symlink 清理
    _tmpdir.cleanup_dead_symlinks = lambda *_a, **_kw: None  # type: ignore[assignment]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """仓库根目录（`D:\\learn\\homework\\agent`）。"""
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def merchant_package_dir(repo_root: Path) -> Path:
    """商家材料包目录（真实材料，导入类用例读它）。"""
    return repo_root / "商家材料包"


@pytest.fixture(autouse=True)
def _reset_singletons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """每个用例前后重置全部单例，并把本地目录指向 tmp_path。

    覆盖的工厂（`app/adapters/__init__.py` 导出的全部 `set_*`）：
        cache / vector_store / sparse_index / embedder / reranker / media_store

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 的环境变量与属性打补丁工具。

    Yields:
        None（用例执行期间生效）。
    """
    # 本地目录指向 tmp，防止测试污染 backend/.local/
    monkeypatch.setenv("WK_LOCAL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("WK_LOCAL_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("WK_LOCAL_MEDIA_DIR", str(tmp_path / "media"))
    # 商家材料包目录**必须显式钉住**，不能靠默认值。
    # 原因：`backend/.env`（Docker 部署用，已被 .gitignore 忽略）里写着
    # `WK_MERCHANT_PACKAGE_DIR=/data/商家材料包`，pydantic-settings 会从 cwd 读到它，
    # 在 Windows 上解析成 `D:\data\商家材料包` → 所有导入类用例报
    # 「源文件不存在：data/FAQ知识库.csv」，且**只有在这台机器上跑才红**。
    # 测试必须自洽：无论开发机有没有 .env，都指向仓库里真实的材料包。
    monkeypatch.setenv(
        "WK_MERCHANT_PACKAGE_DIR",
        str(Path(__file__).resolve().parents[2] / "商家材料包"),
    )

    from app.adapters import cache as cache_mod
    from app.adapters import embedding as embedding_mod
    from app.adapters import media_store as media_mod
    from app.adapters import merchant_ticket as merchant_ticket_mod
    from app.adapters import reranker as reranker_mod
    from app.adapters import sparse_index as sparse_mod
    from app.adapters import vector_store as vector_mod
    from app.adapters import asr as asr_mod
    from app.adapters import ocr as ocr_mod
    from app.adapters import vlm as vlm_mod
    from app.core.config import get_settings
    from app.services.handoff.routing import set_agent_router
    from app.services.handoff.rules import set_rule_engine
    from app.services.handoff.service import set_handoff_service
    from app.services.multimodal.pipeline import set_pipeline

    get_settings.cache_clear()

    yield

    for mod, setter in (
        (cache_mod, "set_cache"),
        (vector_mod, "set_vector_store"),
        (sparse_mod, "set_sparse_index"),
        (embedding_mod, "set_embedder"),
        (reranker_mod, "set_reranker"),
        (media_mod, "set_media_store"),
        (asr_mod, "set_asr"),
        (ocr_mod, "set_ocr"),
        (vlm_mod, "set_image_classifier"),
    ):
        getattr(mod, setter)(None)
    # 多模态流水线单例也要复位：它持有 asr/ocr/media_store 的惰性引用，
    # 前一个用例注入的替身会跟着活到下一个用例。
    set_pipeline(None)
    # 转人工链路的单例（规则引擎 / 坐席路由 / 建单服务 / 商家工单客户端）
    # 也要复位：它们会缓存 DB 加载结果与注入的替身，跨用例残留会让
    # 「规则改了」这类断言串味。
    set_handoff_service(None)
    set_rule_engine(None)
    set_agent_router(None)
    merchant_ticket_mod.set_ticket_client(None)
    # 编排器单例**必须**复位。它内部缓存会话状态（`_sessions`），不清就会
    # 把上一个用例的意图栈、未解决计数、负面连击带到下一个用例：曾经出现过
    # 「这个玩偶多高呀」被上一轮遗留状态判成「投诉维权/高风险」这种鬼故事，
    # 单跑绿、连跑红，排查成本极高。
    from app.services.orchestrator import set_orchestrator

    set_orchestrator(None)
    get_settings.cache_clear()


@pytest.fixture
def settings():
    """当前 Settings 实例（已按测试环境变量构造）。"""
    from app.core.config import get_settings

    return get_settings()


@pytest.fixture
def cache():
    """内存缓存（InMemoryCache），用例间独立。"""
    from app.adapters.cache import InMemoryCache

    return InMemoryCache()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """FastAPI 测试客户端。

    `raise_server_exceptions=False` 让 500 走真实的异常处理器，
    这样能测到「统一响应格式」而不是被 TestClient 直接抛出。

    Args:
        monkeypatch: pytest 打补丁工具。

    Returns:
        `fastapi.testclient.TestClient` 实例。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch):
    """带内部令牌头的测试客户端（访问 `/internal/v1/*` 用）。

    Args:
        monkeypatch: pytest 打补丁工具。

    Returns:
        `fastapi.testclient.TestClient` 实例，默认带 `X-Internal-Token`。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    token = os.environ["WK_INTERNAL_TOKEN"]
    c = TestClient(create_app(), raise_server_exceptions=False)
    c.headers.update({"X-Internal-Token": token})
    return c
