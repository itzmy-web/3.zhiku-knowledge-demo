# utils/auth_utils.py
# -*- coding: utf-8 -*-
"""掌柜智库 · 真登录认证模块（多用户体系地基）

能力：
- 用户注册/登录/游客身份入库（MongoDB users 集合）
- PBKDF2-SHA256 密码派生 + HMAC-SHA256 签名 Token（30 天有效）
- 手机号验证码登录（演示模式：验证码由后端签发并核销；接短信服务商后无缝切换）
- 接口限流（内存滑动窗口，防爆破）
设计原则：不引入任何新第三方依赖（仅标准库 + pymongo），懒加载建连。
"""
import base64
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime

from bson import ObjectId
from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

from tool.logger import logger

# 安全：绝不内置可猜测的默认密钥。未配置 AUTH_SECRET 时每次启动生成随机密钥
# （仅演示用，重启即失效）；生产环境必须在 .env 固定 AUTH_SECRET。
AUTH_SECRET = os.getenv("AUTH_SECRET")
if not AUTH_SECRET:
    AUTH_SECRET = secrets.token_hex(32)
    logger.warning("AUTH_SECRET 未配置，已生成临时随机密钥（重启失效，仅演示用）；生产环境请在 .env 固定配置")
TOKEN_TTL_SECONDS = 30 * 24 * 3600          # Token 有效期 30 天
PBKDF2_ROUNDS = 120000
RATE_LIMIT_WINDOW = 60                       # 限流窗口（秒）
RATE_LIMIT_MAX = 12                          # 窗口内最大认证请求数


def _hash_password(password: str, salt: str = None) -> str:
    """PBKDF2-SHA256 派生密码，返回 salt$hex 格式"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS)
    return f"{salt}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _sign(payload: str) -> str:
    return hmac.new(AUTH_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_token(user_id: str) -> str:
    """签发 Token：base64(user_id|exp).hmac 签名"""
    payload = f"{user_id}|{int(time.time()) + TOKEN_TTL_SECONDS}"
    raw = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return f"{raw}.{_sign(raw)}"


def verify_token(token: str):
    """校验 Token，返回 user_id；无效/过期返回 None"""
    if not token or token.count(".") != 1:
        return None
    raw, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(raw), sig):
        return None
    try:
        user_id, exp = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8").split("|")
        if int(exp) < time.time():
            return None
        return user_id
    except Exception:
        return None


class AuthTool:
    """认证工具（懒加载单例）：users 集合 + 验证码 + 接口限流"""

    def __init__(self):
        self.client = MongoClient(os.getenv("MONGO_URL"))
        self.db = self.client[os.getenv("MONGO_DB_NAME")]
        self.users = self.db["users"]
        # 唯一索引：同一手机号只允许一个账号（guest 用随机号，不冲突）
        self.users.create_index([("phone", ASCENDING)], unique=True)
        self._codes = {}        # phone -> (code, expire_ts)
        self._rate = {}         # ip -> [timestamps]
        logger.info("AuthTool ready (users collection indexed)")

    # ---------- 限流 ----------
    def allow(self, ip: str) -> bool:
        now = time.time()
        bucket = [t for t in self._rate.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
        if len(bucket) >= RATE_LIMIT_MAX:
            self._rate[ip] = bucket
            return False
        bucket.append(now)
        self._rate[ip] = bucket
        return True

    # ---------- 验证码（演示模式；接短信后替换此函数实现即可） ----------
    def send_code(self, phone: str) -> str:
        code = str(secrets.randbelow(900000) + 100000)
        self._codes[phone] = (code, time.time() + 300)   # 5 分钟有效
        return code

    def check_code(self, phone: str, code: str) -> bool:
        item = self._codes.get(phone)
        if not item:
            return False
        real, expire = item
        if time.time() > expire or not hmac.compare_digest(real, str(code)):
            return False
        del self._codes[phone]     # 一次性核销
        return True

    # ---------- 用户 ----------
    def _public(self, doc: dict) -> dict:
        return {
            "user_id": str(doc["_id"]),
            "phone": doc.get("phone", ""),
            "nickname": doc.get("nickname", "用户"),
            "provider": doc.get("provider", "phone"),
            "api_key": doc.get("api_key", ""),
            "created_at": doc.get("created_at", ""),
            "role": doc.get("role", "admin"),             # 角色：admin/editor/viewer（缺省视为 admin，兼容旧账户）
            "tenant_id": doc.get("tenant_id", "default"),   # 租户：default 表示不隔离（兼容旧单租户数据）
        }

    def _create(self, phone: str, nickname: str, provider: str, password: str = None) -> dict:
        doc = {
            "phone": phone,
            "nickname": nickname,
            "provider": provider,
            "role": "admin",                  # 新用户默认管理员（自托管场景下即所有者）
            "tenant_id": "default",            # 默认单租户，不隔离
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "api_key": "zk-" + secrets.token_hex(12),
        }
        if password:
            doc["password_hash"] = _hash_password(password)
        doc["_id"] = self.users.insert_one(doc).inserted_id
        logger.info(f"user created: {phone} provider={provider}")
        return self._public(doc)

    def phone_login(self, phone: str, code: str):
        """验证码登录：首次自动注册（无密码账户），之后直接登入"""
        if not self.check_code(phone, code):
            return None, "验证码不正确或已过期"
        doc = self.users.find_one({"phone": phone})
        if doc:
            return self._public(doc), None
        return self._create(phone, "用户" + phone[-4:], "phone"), None

    def register(self, phone: str, password: str, nickname: str = None):
        """账号密码注册（预留能力，前端当前用验证码流程）"""
        try:
            return self._create(phone, nickname or ("用户" + phone[-4:]), "phone", password=password), None
        except DuplicateKeyError:
            return None, "该手机号已注册，请直接登录"

    def password_login(self, phone: str, password: str):
        doc = self.users.find_one({"phone": phone})
        if not doc or "password_hash" not in doc:
            return None, "账号不存在或未设置密码"
        if not _verify_password(password, doc["password_hash"]):
            return None, "密码不正确"
        return self._public(doc), None

    def guest(self) -> dict:
        """游客身份：随机号入库，与正式账号同权隔离"""
        for _ in range(5):     # 撞唯一索引则重试
            phone = "guest-" + secrets.token_hex(5)
            try:
                return self._create(phone, "游客" + secrets.token_hex(2).upper(), "guest"), None
            except DuplicateKeyError:
                continue
        return None, "游客创建失败，请重试"

    def third_login(self, provider: str, nickname: str) -> dict:
        """微信/支付宝扫码仿真登录：按昵称+供应商入库（演示可重复）"""
        phone = f"{provider}-{secrets.token_hex(6)}"
        return self._create(phone, nickname, provider), None

    def get_user(self, user_id: str):
        try:
            doc = self.users.find_one({"_id": ObjectId(user_id)})
            return self._public(doc) if doc else None
        except Exception:
            return None


_auth_tool = None


def get_auth_tool() -> AuthTool:
    global _auth_tool
    if _auth_tool is None:
        _auth_tool = AuthTool()
    return _auth_tool


# ---------- 角色与权限（admin > editor > viewer） ----------
ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}


def resolve_role(user: dict) -> str:
    """解析用户角色，缺省视为 admin（兼容未设角色的旧账户）"""
    if not isinstance(user, dict):
        return "admin"
    return str(user.get("role") or "admin").lower()


def has_permission(user: dict, required_role: str) -> bool:
    """判断用户是否拥有 required_role 及以上权限"""
    return ROLE_RANK.get(resolve_role(user), 3) >= ROLE_RANK.get(str(required_role or "viewer").lower(), 1)
