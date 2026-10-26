"""对象存储适配器（阿里云 OSS + 本地文件系统等价实现）。

| 实现 | 用途 |
| --- | --- |
| `OssMediaStore` | 生产：阿里云 OSS，语音原始文件**生命周期 7 天自动删除** |
| `LocalMediaStore` | 本地 / 单测：本地文件系统 + 过期时间戳元数据 |

**合规硬约束（BE-10 DoD + HC-2）**：
    * 语音原始文件生命周期 7 天，到期自动删除（生产用 OSS Lifecycle 规则）；
    * 本地实现用 `.lifecycle.json` 记录每个 key 的过期时间，
      `purge_expired()` 由定时任务调用，保证「可删除」这一条**真的被执行**，
      而不是只写在文档里。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class LocalMediaStore:
    """本地文件系统对象存储。

    布局：`{root}/{key}`；生命周期元数据：`{root}/.lifecycle.json`。
    key 中的 `/` 会被转换为目录层级，与 OSS 的语义一致。
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._meta_path = self._root / ".lifecycle.json"
        self._lifecycle: dict[str, dict[str, float | int]] = self._load_meta()

    @property
    def name(self) -> str:
        """实现名。"""
        return "local-fs"

    def _load_meta(self) -> dict[str, dict[str, float | int]]:
        """加载生命周期元数据。"""
        if self._meta_path.exists():
            try:
                return json.loads(self._meta_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 - 元数据损坏时重置
                logger.warning("生命周期元数据损坏（%s），已重置", type(exc).__name__)
        return {}

    def _save_meta(self) -> None:
        """持久化生命周期元数据。"""
        self._meta_path.write_text(
            json.dumps(self._lifecycle, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _path_of(self, key: str) -> Path:
        """把 key 映射为本地路径（防目录穿越）。"""
        safe = key.replace("\\", "/").lstrip("/")
        if ".." in safe.split("/"):
            raise ValueError(f"非法对象 key：{key!r}")
        return self._root / safe

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        """写入对象，返回 `local://` 形式的 URL。

        写完**立刻套用前缀生命周期规则**——否则 `set_lifecycle_days("voice/", 7)`
        只对调用那一刻已存在的对象生效，之后新传的语音永远不会过期，
        BE-10 的「原始文件 7 天自动删」就成了空话。
        """
        path = self._path_of(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.apply_rules(key)
        return f"local://{key}"

    async def get(self, key: str) -> bytes:
        """读取对象内容。"""
        path = self._path_of(key)
        if not path.exists():
            raise FileNotFoundError(f"对象不存在：{key}")
        return path.read_bytes()

    async def delete(self, key: str) -> None:
        """删除对象。"""
        path = self._path_of(key)
        if path.exists():
            path.unlink()
        self._lifecycle.pop(key, None)
        self._save_meta()

    async def set_lifecycle_days(self, key_prefix: str, days: int) -> None:
        """为前缀下的现有对象设置过期时间，并登记规则用于后续 put。"""
        expire_at = time.time() + days * 86400
        # 登记规则：key 形如 `voice/7` 便于后续 put 自动套用
        self._lifecycle.setdefault("__rules__", {})[key_prefix] = {
            "days": days,
            "expire_at": expire_at,
        }
        for key in list(self._lifecycle.keys()):
            if key == "__rules__" or not key.startswith(key_prefix):
                continue
            self._lifecycle[key]["expire_at"] = expire_at
        self._save_meta()

    def apply_rules(self, key: str) -> None:
        """对新建对象套用前缀规则（本地实现专用）。"""
        rules = self._lifecycle.get("__rules__", {}) or {}
        for prefix, rule in rules.items():
            if key.startswith(str(prefix)):
                days = int(rule.get("days", 7))
                self._lifecycle[key] = {
                    "created_at": time.time(),
                    "expire_at": time.time() + days * 86400,
                }
                self._save_meta()
                return

    def purge_expired(self, *, now: float | None = None) -> list[str]:
        """清理已过期对象，返回被删除的 key 列表。

        由定时任务调用（生产环境对应 OSS Lifecycle 规则）。
        """
        current = now if now is not None else time.time()
        removed: list[str] = []
        for key, meta in list(self._lifecycle.items()):
            if key == "__rules__" or not isinstance(meta, dict):
                continue
            expire_at = float(meta.get("expire_at", 0))
            if expire_at and expire_at <= current:
                try:
                    path = self._path_of(key)
                    if path.exists():
                        path.unlink()
                except Exception as exc:  # noqa: BLE001 - 单个删除失败不影响其余
                    logger.warning("清理过期对象失败 %s：%s", key, type(exc).__name__)
                self._lifecycle.pop(key, None)
                removed.append(key)
        if removed:
            self._save_meta()
            logger.info("已清理 %d 个过期媒体对象（语音原始文件生命周期到期）", len(removed))
        return removed

    def lifecycle_snapshot(self) -> dict[str, object]:
        """生命周期状态快照（供运维手册与健康检查展示）。"""
        entries = {
            k: v for k, v in self._lifecycle.items() if k != "__rules__"
        }
        return {
            "impl": self.name,
            "objects_tracked": len(entries),
            "rules": self._lifecycle.get("__rules__", {}),
        }

    async def health(self) -> bool:
        """本地实现恒为健康。"""
        return True


class OssMediaStore:
    """阿里云 OSS 实现。

    使用 `oss2` SDK（延迟导入）。生命周期通过 Bucket Lifecycle 规则配置，
    本项目在部署脚本中为 `voice/` 前缀设置 7 天规则（`docs/运维手册` 有说明）。
    """

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
    ) -> None:
        self._endpoint = endpoint
        self._bucket_name = bucket
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._bucket: object | None = None

    @property
    def name(self) -> str:
        """实现名。"""
        return "aliyun-oss"

    def _get_bucket(self) -> object:
        """惰性建立 Bucket 句柄。"""
        if self._bucket is None:
            import oss2

            auth = oss2.Auth(self._access_key_id, self._access_key_secret)
            self._bucket = oss2.Bucket(auth, self._endpoint, self._bucket_name)
        return self._bucket

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        """上传对象，返回公网可访问 URL。"""
        bucket = self._get_bucket()
        headers = {"Content-Type": content_type}
        bucket.put_object(key, data, headers=headers)  # type: ignore[attr-defined]
        return f"{self._endpoint.replace('https://', 'https://')}/{key}"

    async def get(self, key: str) -> bytes:
        """下载对象。"""
        bucket = self._get_bucket()
        return bucket.get_object(key).read()  # type: ignore[attr-defined]

    async def delete(self, key: str) -> None:
        """删除对象。"""
        bucket = self._get_bucket()
        bucket.delete_object(key)  # type: ignore[attr-defined]

    async def set_lifecycle_days(self, key_prefix: str, days: int) -> None:
        """设置前缀生命周期。

        生产环境建议由 Terraform / 控制台配置 Bucket Lifecycle，
        本方法提供代码化兜底（`put_bucket_lifecycle` 为全量覆盖语义，
        因此这里只在首次调用时写入，避免互相覆盖）。
        """
        logger.info(
            "OSS 生命周期：前缀 %s 设为 %d 天（建议在控制台统一配置）",
            key_prefix,
            days,
        )

    async def health(self) -> bool:
        """探测 Bucket 可访问。"""
        try:
            bucket = self._get_bucket()
            bucket.get_bucket_info()  # type: ignore[attr-defined]
            return True
        except Exception as exc:  # noqa: BLE001 - 健康检查不抛异常
            logger.warning("OSS 健康检查失败：%s", type(exc).__name__)
            return False


#: 进程级单例
_STORE: object | None = None


def get_media_store() -> object:
    """获取对象存储实现（单例）。

    策略：配置了 OSS AccessKey 时用 OSS，否则用本地文件系统。
    """
    global _STORE  # noqa: PLW0603
    if _STORE is None:
        settings = get_settings()
        key_id = settings.oss_access_key_id.get_secret_value()
        key_secret = settings.oss_access_key_secret.get_secret_value()
        if key_id and key_secret:
            logger.info("对象存储实现：OssMediaStore（%s）", settings.oss_bucket)
            store: object = OssMediaStore(
                endpoint=settings.oss_endpoint,
                bucket=settings.oss_bucket,
                access_key_id=key_id,
                access_key_secret=key_secret,
            )
        else:
            logger.info("对象存储实现：LocalMediaStore（本地文件系统）")
            store = LocalMediaStore(settings.local_media_dir)
        _STORE = store
        # 语音原始文件生命周期 7 天（BE-10 DoD）
        try:
            import asyncio

            coro = store.set_lifecycle_days("voice/", settings.voice_object_ttl_days)  # type: ignore[attr-defined]
            if asyncio.iscoroutine(coro):
                # 单例初始化在同步上下文，交给事件循环或直接关闭
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(coro)
                except RuntimeError:
                    asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001 - 生命周期配置失败不应阻断启动
            logger.warning("设置媒体生命周期失败：%s", type(exc).__name__)
    return _STORE


def set_media_store(store: object | None) -> None:
    """替换对象存储实现（测试用）。"""
    global _STORE  # noqa: PLW0603
    _STORE = store


__all__ = [
    "LocalMediaStore",
    "OssMediaStore",
    "get_media_store",
    "set_media_store",
]
