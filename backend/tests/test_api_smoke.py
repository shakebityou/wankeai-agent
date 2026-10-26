"""API 冒烟测试：应用必须能起来，探针必须可用（TODO 20 号 DoD）。

对应验收口径：
- 任务书 3.10：`docker-compose` 起栈后 `/health` 必须返回 200
  （Dockerfile 的 HEALTHCHECK 与 compose 的 healthcheck 都依赖它）。
- 3.10：接口文档（OpenAPI）由 FastAPI 自动生成 → `/openapi.json` 可访问。
- NFR-06：所有接口携带 `X-Trace-Id`。
- HC-1：`/metrics` 与 `/health` 不得暴露任何链接类内容。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """模块级 TestClient（触发 lifespan：日志初始化 + 适配器选择）。

    Returns:
        运行中的 TestClient。
    """
    with TestClient(create_app()) as c:
        yield c


class TestHealthProbes:
    """运维探针（Docker HEALTHCHECK 的判定对象）。"""

    def test_health_ok(self, client: TestClient) -> None:
        """`/health` 返回 200 且带统一响应壳。"""
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 0
        assert body["request_id"]
        assert body["data"]["status"] in {"UP", "DEGRADED", "DOWN"}

    def test_liveness_no_dependency(self, client: TestClient) -> None:
        """`/health/live` 只答「进程活着」，不查任何依赖。"""
        r = client.get("/health/live")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "UP"

    def test_readiness_reports_dependencies(self, client: TestClient) -> None:
        """`/health/ready` 只探关键依赖（缓存），并给出实现类名。"""
        r = client.get("/health/ready")
        assert r.status_code == 200
        deps = r.json()["data"]["dependencies"]
        names = {d["name"] for d in deps}
        assert names == {"cache"}
        # 本地环境必须用等价实现，且实现类名可读（便于运维核对）
        assert deps[0]["impl"] == "InMemoryCache"

    def test_deps_health_lists_all_adapters(self, client: TestClient) -> None:
        """`/health/deps` 探测全部适配器（深检）。"""
        r = client.get("/health/deps")
        assert r.status_code == 200
        deps = r.json()["data"]["dependencies"]
        impls = {d["name"]: d["impl"] for d in deps}
        assert {"cache", "vector_store", "sparse_index"} <= set(impls)
        assert impls["cache"] == "InMemoryCache"
        assert impls["vector_store"] == "NumpyVectorStore"
        assert impls["sparse_index"] == "Bm25SparseIndex"
        assert impls["embedder"] == "LocalHashEmbedder"

    def test_deps_health_shape(self, client: TestClient) -> None:
        """`/health/deps` 返回 HealthReport 结构。"""
        r = client.get("/health/deps")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["version"] == "1.0.0"
        assert isinstance(data["dependencies"], list)
        assert isinstance(data["circuit_breakers"], list)

    def test_deps_health_impl_names_readable(self, client: TestClient) -> None:
        """`/health/deps` 的实现类名可读（运维核对生产/本地实现）。"""
        r = client.get("/health/deps")
        impls = {d["name"]: d["impl"] for d in r.json()["data"]["dependencies"]}
        assert impls["cache"] == "InMemoryCache"
        assert impls["vector_store"] == "NumpyVectorStore"

    def test_trace_header_present(self, client: TestClient) -> None:
        """NFR-06：所有接口响应带 X-Trace-Id。"""
        r = client.get("/health")
        assert r.headers["X-Trace-Id"].startswith("trace_")

    def test_trace_id_echoed(self, client: TestClient) -> None:
        """客户端传入 X-Trace-Id 时必须沿用（全链路串联）。"""
        r = client.get("/health", headers={"X-Trace-Id": "trace_fixed_0001"})
        assert r.headers["X-Trace-Id"] == "trace_fixed_0001"
        assert r.json()["request_id"]


class TestOpenApi:
    """接口文档（任务书 3.10 交付物）。"""

    def test_openapi_available(self, client: TestClient) -> None:
        """`/openapi.json` 可访问且含三个契约分区说明。"""
        r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert spec["info"]["version"] == "1.0.0"
        assert "契约 C" in spec["info"]["description"]

    def test_docs_available(self, client: TestClient) -> None:
        """Swagger UI 可访问。"""
        r = client.get("/docs")
        assert r.status_code == 200


class TestMetrics:
    """Prometheus 指标（X-08 可观测性）。"""

    def test_metrics_exposed(self, client: TestClient) -> None:
        """`/metrics` 返回 Prometheus 文本格式。"""
        r = client.get("/metrics")
        assert r.status_code == 200
        text = r.text
        assert "wk_http_requests_total" in text
        assert "wk_app_info" in text

    def test_metrics_has_no_links(self, client: TestClient) -> None:
        """HC-1：指标输出不得含任何可点击链接。"""
        r = client.get("/metrics")
        assert "http://" not in r.text
        assert "https://" not in r.text


class TestUnifiedResponse:
    """统一响应格式（契约 C 2.5.5）。"""

    def test_404_uses_bff_envelope(self, client: TestClient) -> None:
        """未知路径必须返回统一错误壳（而非 Starlette 默认纯文本）。"""
        r = client.get("/nonexistent-path")
        assert r.status_code == 404
        body = r.json()
        assert body["code"] == 10004
        assert body["data"] is None
        assert body["request_id"]

    def test_405_uses_bff_envelope(self, client: TestClient) -> None:
        """方法不允许同样走统一壳。"""
        r = client.request("DELETE", "/health")
        assert r.status_code == 405
        body = r.json()
        assert body["code"] == 10001


class TestToolsRoutes:
    """BE-07/08/09 的四个读接口（契约 B）必须真的挂上了。"""

    def test_tools_routes_registered(self, client: TestClient) -> None:
        """`/internal/v1/tools/*` 四个路由出现在 OpenAPI。"""
        spec = client.get("/openapi.json").json()
        paths = set(spec["paths"])
        for suffix in ("order", "logistics", "inventory", "product"):
            assert f"/internal/v1/tools/{suffix}" in paths

    def test_tools_requires_internal_token(self, api_client: TestClient) -> None:
        """未带 X-Internal-Token → 403（凭证无效，不泄露是否配置）。"""
        r = api_client.get(
            "/internal/v1/tools/order",
            params={"order_no": "202601010001", "buyer_id": "buyer_001"},
            headers={"X-Internal-Token": ""},
        )
        assert r.status_code == 403

    def test_order_route_returns_ok(self, api_client: TestClient) -> None:
        """带 token 查询订单 → 200 且订单号脱敏后回显。"""
        r = api_client.get(
            "/internal/v1/tools/order",
            params={"order_no": "202601010001", "buyer_id": "buyer_001"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 0
        assert body["data"]["data"]["order_no"] == "202601010001"
        assert body["data"]["data"]["receiver"]["phone"] == "138****8888"

    def test_order_route_rejects_other_buyer(self, api_client: TestClient) -> None:
        """越权（BE-08 DoD①）→ 403 + 话术。"""
        r = api_client.get(
            "/internal/v1/tools/order",
            params={"order_no": "202601010001", "buyer_id": "buyer_evil"},
        )
        assert r.status_code == 403
        assert "下单账号" in r.json()["message"]

    def test_order_route_rejects_short_order_no(self, api_client: TestClient) -> None:
        """订单号非 12 位 → 400。"""
        r = api_client.get(
            "/internal/v1/tools/order",
            params={"order_no": "123", "buyer_id": "buyer_001"},
        )
        assert r.status_code == 400

    def test_inventory_route_returns_ok(self, api_client: TestClient) -> None:
        """库存接口走中文口径（BE-09）。"""
        r = api_client.get(
            "/internal/v1/tools/inventory", params={"sku_ids": "WK-BEAR-20"}
        )
        assert r.status_code == 200
        assert "现货" in r.json()["data"]["text"]

    def test_inventory_route_rejects_too_many_skus(self, api_client: TestClient) -> None:
        """超过 50 个 SKU → 400。"""
        r = api_client.get(
            "/internal/v1/tools/inventory",
            params={"sku_ids": ",".join(f"S{i}" for i in range(51))},
        )
        assert r.status_code == 400

    def test_logistics_route_requires_one_key(self, api_client: TestClient) -> None:
        """tracking_no 与 order_no 都不传 → 400。"""
        r = api_client.get("/internal/v1/tools/logistics")
        assert r.status_code == 400

    def test_product_route_requires_exactly_one_key(
        self, api_client: TestClient
    ) -> None:
        """sku_id 与 category 都不传 → 400。"""
        r = api_client.get("/internal/v1/tools/product")
        assert r.status_code == 400
        r2 = api_client.get(
            "/internal/v1/tools/product",
            params={"sku_id": "WK-BEAR-20", "category": "毛绒玩偶"},
        )
        assert r2.status_code == 400
