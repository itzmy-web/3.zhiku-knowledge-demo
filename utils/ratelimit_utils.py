# utils/ratelimit_utils.py
# -*- coding: utf-8 -*-
"""掌柜智库 · 开放 API 限流（/v1/chat、/v1/bot/webhook 共用）

设计要点：
- 按 api_key 维度滑动窗口限流；api_key 缺失时回退到 IP，防止裸调爆破。
- 限流参数可由环境变量配置：
    API_RATE_LIMIT_MAX=30       窗口内最大请求数（默认 30）
    API_RATE_LIMIT_WINDOW=60    窗口大小（秒，默认 60）
- 失败开放（fail-open）：限流器自身异常时一律放行，绝不阻塞正常业务。
- 进程内内存计数（单实例部署足够）；多实例可换 Redis 后端，接口不变。
"""
import os
import threading
import time

try:
    API_RATE_LIMIT_MAX = int(os.getenv("API_RATE_LIMIT_MAX", "30"))
except (TypeError, ValueError):
    API_RATE_LIMIT_MAX = 30

try:
    API_RATE_LIMIT_WINDOW = int(os.getenv("API_RATE_LIMIT_WINDOW", "60"))
except (TypeError, ValueError):
    API_RATE_LIMIT_WINDOW = 60

# key -> 时间戳列表（仅保留窗口内的）
_lock = threading.Lock()
_buckets = {}

# 进程启动时刻（用于监控 uptime 之外的兜底）
_BOOT_TS = time.time()


def api_check_limit(key: str):
    """检查某 key 是否允许本次请求。

    返回 (allowed: bool, retry_after: int)
      allowed=False 时 retry_after 表示还需等待的秒数。
    key 为空（异常兜底）直接放行。
    """
    if not key:
        return True, 0
    try:
        now = time.time()
        with _lock:
            hits = [t for t in _buckets.get(key, []) if now - t < API_RATE_LIMIT_WINDOW]
            if len(hits) >= API_RATE_LIMIT_MAX:
                # 取最早一条，计算还要等多久窗口才滑出
                oldest = hits[0]
                retry_after = int(API_RATE_LIMIT_WINDOW - (now - oldest)) + 1
                _buckets[key] = hits  # 顺手清理过期时间戳
                return False, max(retry_after, 1)
            hits.append(now)
            _buckets[key] = hits
        return True, 0
    except Exception:
        # 失败开放：限流器异常绝不阻断业务
        return True, 0


def api_limit_headers(key: str):
    """生成限流相关的响应头（X-RateLimit-*），便于调用方感知配额。

    返回 dict；超限场景也会返回 X-RateLimit-Remaining=0 与 Retry-After。
    """
    if not key:
        return {}
    try:
        now = time.time()
        with _lock:
            hits = [t for t in _buckets.get(key, []) if now - t < API_RATE_LIMIT_WINDOW]
            remaining = max(0, API_RATE_LIMIT_MAX - len(hits))
            reset_in = API_RATE_LIMIT_WINDOW
            if hits:
                reset_in = int(API_RATE_LIMIT_WINDOW - (now - hits[0])) + 1
        return {
            "X-RateLimit-Limit": str(API_RATE_LIMIT_MAX),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(int(now) + max(reset_in, 1)),
        }
    except Exception:
        return {}
