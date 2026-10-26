"""渠道与授权配置模型（`BE-01` / `FE-11`）。

`channels` 表存 5 个渠道的接入状态。**为什么配置要入库而不是只放环境变量**：
`FE-11` 渠道与授权管理页需要让运营看到「哪个渠道授权快过期了」并能启停单个
渠道（渠道授权过期 40102 时降级为纯 FAQ，见商家说明书附录 C）。授权到期时间
是随时间变化的运营数据，放配置文件里每次都要改文件重启。

**不含任何密钥**：`app_secret` 与 `internal_token` 是全局商家凭证，一律走
KMS/环境变量，**禁止入库**（商家 S-20 强制要求）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import dt_col, json_col, str_col, text_col


class ChannelConfig(TimestampMixin, Base):
    """渠道接入配置。

    Attributes:
        channel: 渠道枚举（TAOBAO / TMALL / PDD / DOUYIN / WECHAT）。
        name: 中文名（淘宝千牛 / 天猫 / 拼多多 / 抖音小店 / 微信客服）。
        enabled: 是否启用（停用后该渠道回调直接拒绝）。
        authorized: 授权是否有效。
        auth_expire_at: 授权到期时间（临期告警依据）。
        callback_url: 本渠道回调地址（需与平台配置一致）。
        app_key: 平台侧应用标识（非密钥，可入库）。
        note: 备注。
        sort_order: 展示顺序。
        permission_packages: 平台授予的权限包（如「订单查询」「发货」「退款」）。
        ip_whitelist_count: 出口 IP 白名单条数（0 表示未配置）。
        service_provider_certified: 服务商认证状态（抖音小店关键路径，None 表示不适用）。
        service_provider_note: 认证备注（如「审核中，预计 10 个工作日」）。
        updated_by: 最近一次操作人（FE-11 要求认证状态变更留痕）。
    """

    __tablename__ = "channels"
    __table_args__ = (
        Index("ix_channels_enabled_auth", "enabled", "authorized"),
        {"comment": "渠道接入配置（BE-01 / FE-11）"},
    )

    channel: Mapped[str] = mapped_column(str_col(16), primary_key=True)
    name: Mapped[str] = mapped_column(str_col(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auth_expire_at: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)
    callback_url: Mapped[str] = mapped_column(str_col(500), default="", nullable=False)
    app_key: Mapped[str] = mapped_column(str_col(128), default="", nullable=False)
    note: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    permission_packages: Mapped[list] = mapped_column(json_col(), default=list, nullable=False)
    ip_whitelist_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    service_provider_certified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    service_provider_note: Mapped[str] = mapped_column(str_col(255), default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)

    def __repr__(self) -> str:
        return f"<ChannelConfig {self.channel} enabled={self.enabled} auth={self.authorized}>"


__all__ = ["ChannelConfig"]
