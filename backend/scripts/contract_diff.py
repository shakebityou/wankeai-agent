"""前后端 BFF 契约对拍（一次性诊断脚本，不是自检）。

把 `frontend/mock/handlers*.ts` 里 Mock 实际提供的路由，与后端
`/bff/v1` 的 OpenAPI 路径做双向差集，输出：
  1. 两边都有（对齐）
  2. 只有 Mock 有（后端缺，前端一旦切真 BFF 就会 404）
  3. 只有后端有（Mock 缺，联调时前端看不到，但不影响真环境）

用法：
    cd backend
    $env:PYTHONPATH='.'; python scripts/contract_diff.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
MOCK_DIR = ROOT / "frontend" / "mock"

# Mock 侧：静态路径 `path === '/v1/xxx'` 与正则 `= /^\/v1\/...$/`
_STATIC_RE = re.compile(r"method === '(\w+)' && path === '(/v1/[^']+)'")
_REGEX_RE = re.compile(r"const (\w+Match) = /\^(.*?)\$/\.exec\(path\)")
_REGEX_USE_RE = re.compile(r"method === '(\w+)' && (\w+Match)")
_MULTI_USE_RE = re.compile(r"\(method === '(\w+)' \|\| method === '(\w+)'\) && (\w+Match)")
_CHUNK_MULTI_RE = re.compile(r"if \((\w+Match) && \(method === '(\w+)' \|\| method === '(\w+)'\)\)")


def _regex_to_path(pattern: str) -> str:
    """把 `/^\\/v1\\/sessions\\/([^/]+)$/` 的中间部分转成 `/v1/sessions/{id}`。"""
    out = pattern.replace("\\/", "/")
    out = re.sub(r"\(\[\^/\]\+\)", "{id}", out)
    out = re.sub(r"\([^)]*\)", "{id}", out)
    return out


def _norm(path: str) -> str:
    """去掉 `/bff/v1` 与 `/v1` 前缀，参数统一成 `{}`，便于两边对拍。"""
    for prefix in ("/bff/v1", "/v1"):
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            path = path[len(prefix):]
            break
    return re.sub(r"\{[^}]*\}", "{}", path)


def load_mock_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    regex_map: dict[str, str] = {}
    for name in ("handlers.ts", "handlers-auth.ts"):
        path = MOCK_DIR / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for m in _REGEX_RE.finditer(text):
            regex_map[m.group(1)] = _regex_to_path(m.group(2))
        for m in _STATIC_RE.finditer(text):
            routes.add((m.group(1), m.group(2)))
        for m in _MULTI_USE_RE.finditer(text):
            target = regex_map.get(m.group(3))
            if target:
                routes.add((m.group(1), target))
                routes.add((m.group(2), target))
        for m in _REGEX_USE_RE.finditer(text):
            target = regex_map.get(m.group(2))
            if target:
                routes.add((m.group(1), target))
        for m in _CHUNK_MULTI_RE.finditer(text):
            target = regex_map.get(m.group(1))
            if target:
                routes.add((m.group(2), target))
                routes.add((m.group(3), target))
    # 归一：Mock 用 {id}，后端用 {param}，统一成 {}
    return {(m.upper(), _norm(p)) for m, p in routes}


def load_backend_routes() -> set[tuple[str, str]]:
    sys.path.insert(0, str(BACKEND))
    import os

    os.environ.setdefault("WK_ENV", "test")
    os.environ.setdefault("WK_INTERNAL_TOKEN", "test_internal_token")
    os.environ.setdefault("WK_LOCAL_DB_PATH", str(BACKEND / ".pytest-work" / "diff.db"))
    os.environ.setdefault("WK_LOCAL_INDEX_DIR", str(BACKEND / ".pytest-work" / "diff-index"))
    os.environ.setdefault("WK_LOCAL_MEDIA_DIR", str(BACKEND / ".pytest-work" / "diff-media"))
    from app.main import create_app

    spec = create_app().openapi()
    routes: set[tuple[str, str]] = set()
    for raw_path, item in spec.get("paths", {}).items():
        if not raw_path.startswith("/bff/v1"):
            continue
        short = _norm(raw_path)
        for method in item:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                routes.add((method.upper(), short))
    return routes


def main() -> int:
    mock = load_mock_routes()
    backend = load_backend_routes()
    both = sorted(mock & backend)
    only_mock = sorted(mock - backend)
    only_backend = sorted(backend - mock)

    print(f"Mock 路由 {len(mock)} 条 / 后端 BFF 路由 {len(backend)} 条")
    print(f"\n✅ 两边都有（{len(both)}）")
    for m, p in both:
        print(f"   {m:6} {p}")

    print(f"\n⚠️  只有 Mock 有 —— 切真 BFF 会 404（{len(only_mock)}）")
    for m, p in only_mock:
        print(f"   {m:6} {p}")

    print(f"\nℹ️  只有后端有 —— Mock 未实现（{len(only_backend)}）")
    for m, p in only_backend:
        print(f"   {m:6} {p}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
