"""口令哈希与令牌工具。

**为什么用 scrypt**（与前端 `frontend/mock/db/password.ts` 同一套参数）：
* bcrypt 是原生模块，Windows 上要编译；scrypt 在标准库 `hashlib` 里（RFC 7914）；
* 口令哈希的核心指标是「算得慢、占内存」，scrypt 的 `N=16384` 约需 16MB，
  让离线爆破的性价比低到没有意义；SHA-256 每秒几十亿次，彩虹表一查就中。

**存储格式自描述**：`scrypt$N$r$p$saltBase64$hashBase64`。将来调参数时，
老哈希仍能按自己记录的参数验证，不需要强制全员改密。

**哈希格式与前端逐字节兼容**：前端种子账号的哈希后端能直接校验，反之亦然——
联调时不用重新造一套账号。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import unicodedata
from base64 import b64decode, b64encode

from app.db.models.auth import (
    SCRYPT_KEY_LEN,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    SCRYPT_SALT_LEN,
)

#: scrypt 内存上限。`128 * N * r` = 16MB，给到 64MB 留余量，
#: 否则部分 OpenSSL 构建会因默认 maxmem 太小抛 `Memory limit exceeded`。
_SCRYPT_MAXMEM = 64 * 1024 * 1024

#: 口令最小长度（与前端 `MIN_PASSWORD_LENGTH` 一致）
MIN_PASSWORD_LENGTH = 8

#: 登录态 token 长度（字节）。32 字节 = 256 位熵，token_urlsafe 后 43 字符。
TOKEN_BYTES = 32


def _scrypt(password: str, salt: bytes, key_len: int, *, n: int, r: int, p: int) -> bytes:
    """派生密钥。

    Args:
        password: 明文口令（先做 NFKC 归一，与前端一致）。
        salt: 盐。
        key_len: 输出长度。
        n: CPU/内存代价。
        r: 块大小。
        p: 并行度。

    Returns:
        派生出的字节串。
    """
    return hashlib.scrypt(
        unicodedata.normalize("NFKC", password).encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=_SCRYPT_MAXMEM,
        dklen=key_len,
    )


def hash_password(password: str) -> str:
    """生成 `scrypt$N$r$p$salt$hash` 形式的哈希串。

    每个账号单独生成盐——同口令共用盐会让「撞库一次、全站生效」。

    Args:
        password: 明文口令。

    Returns:
        自描述的哈希串。
    """
    salt = secrets.token_bytes(SCRYPT_SALT_LEN)
    derived = _scrypt(
        password,
        salt,
        SCRYPT_KEY_LEN,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    )
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            b64encode(salt).decode("ascii"),
            b64encode(derived).decode("ascii"),
        )
    )


def verify_password(password: str, stored: str) -> bool:
    """校验口令。

    **恒时间比较**：用 `hmac.compare_digest` 而不是 `==`，否则攻击者能靠
    响应时间逐字节猜出哈希。解析失败一律返回 False——库里的脏数据不该变成
    登录漏洞。

    Args:
        password: 用户提交的明文口令。
        stored: 库里的哈希串。

    Returns:
        是否匹配。
    """
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = b64decode(parts[4], validate=True)
        expected = b64decode(parts[5], validate=True)
    except (ValueError, TypeError):
        return False
    if n <= 0 or r <= 0 or p <= 0 or not salt or not expected:
        return False
    try:
        actual = _scrypt(password, salt, len(expected), n=n, r=r, p=p)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


#: 账号不存在时也跑一次同等代价的校验，让响应时间与「密码错」一致，
#: 否则攻击者能靠计时区分「账号存在但密码错」和「账号不存在」。
DECOY_HASH = (
    "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
)


def hash_token(token: str) -> str:
    """登录态 token 的哈希。

    token 本身已是 256 位随机数，无需慢哈希；sha256 的唯一目的是
    「库文件泄露后不能直接冒用会话」。

    Args:
        token: 原始 token。

    Returns:
        十六进制 sha256。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """生成登录态 token（URL 安全，可直接放 Cookie）。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def validate_password_strength(password: str) -> tuple[bool, str]:
    """校验口令强度（与前端 `validatePasswordStrength` 同口径）。

    只要求「≥8 位且同时含字母和数字」——更强的规则（大小写符号）会让运营
    在演示环境里频繁改密，收益有限；真正的强度保障靠 scrypt 与锁定策略。

    Args:
        password: 待校验口令。

    Returns:
        `(是否通过, 不通过时的提示语)`。
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"密码至少 {MIN_PASSWORD_LENGTH} 位"
    has_alpha = any(ch.isalpha() and ch.isascii() for ch in password)
    has_digit = any(ch.isdigit() for ch in password)
    if not has_alpha or not has_digit:
        return False, "密码需同时包含字母和数字"
    return True, ""


__all__ = [
    "DECOY_HASH",
    "MIN_PASSWORD_LENGTH",
    "TOKEN_BYTES",
    "generate_token",
    "hash_password",
    "hash_token",
    "validate_password_strength",
    "verify_password",
]
