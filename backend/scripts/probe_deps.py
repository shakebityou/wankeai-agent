"""依赖自检：列出 requirements.txt 中各包在本机是否可导入及其版本。

用途：在无 Docker 的开发机上确认可跑通的子集，避免 import 失败导致的连锁报错。
"""

from __future__ import annotations

import importlib
from importlib import metadata

MODULES: list[tuple[str, str]] = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("gunicorn", "gunicorn"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("httpx", "httpx"),
    ("sqlalchemy", "sqlalchemy"),
    ("alembic", "alembic"),
    ("psycopg2", "psycopg2-binary"),
    ("redis", "redis"),
    ("celery", "celery"),
    ("pymilvus", "pymilvus"),
    ("elasticsearch", "elasticsearch"),
    ("jieba", "jieba"),
    ("numpy", "numpy"),
    ("pandas", "pandas"),
    ("openpyxl", "openpyxl"),
    ("PIL", "Pillow"),
    ("prometheus_client", "prometheus-client"),
    ("opentelemetry", "opentelemetry-api"),
    ("pytest", "pytest"),
    ("httpx", "httpx"),
]


def main() -> int:
    """打印各依赖的可用状态。"""
    missing: list[str] = []
    for module_name, dist_name in MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - 自检脚本，需吞掉任意导入异常
            missing.append(dist_name)
            print(f"[MISSING] {module_name:20s} ({type(exc).__name__})")
            continue
        try:
            version = metadata.version(dist_name)
        except metadata.PackageNotFoundError:
            version = "unknown"
        print(f"[OK]      {module_name:20s} {version}")

    print()
    print(f"缺失依赖 {len(missing)} 个: {', '.join(sorted(set(missing))) or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
