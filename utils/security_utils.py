# utils/security_utils.py
"""
安全加固 + 可观测记录（零新依赖）：
1. detect_injection：提示词注入规则检测（弱演示防护，详见下方说明）
2. trace_llm：LLM 调用追踪落 Mongo（llm_traces 集合），供后续看板/排查

⚠️ 关于 detect_injection 的诚实说明（务必知悉）：
本检测仅是"演示级"的轻量正则拦截，绝非真实安全屏障。
- 正则可被空格、全角字符、换种说法轻松绕过；
- 命中即拒答也必然带来误伤（正常提问被误判）；
- 真实系统应把防护放在「模型侧系统提示约束 + 输出内容校验 + 权限边界」上，
  本函数只作为第一道低成本过滤网，拦截最常见的"指令劫持"模板。
因此下方规则刻意只保留"指令式越权"特征，移除了一切易误伤正常提问的泛化规则。
"""
import re
import time

from tool.logger import logger

# ============ 1. 提示词注入检测（弱演示防护） ============
# 仅保留"指令式越权/角色劫持"特征，宁可漏报也不误伤正常提问：
#   - 英文：ignore/disregard previous instructions、you are now a、new instructions、jailbreak/DAN、pretend
#   - 中文：忽略/无视/忘记 + 指令/提示/规则、突破限制、越狱模式/开发者模式
# 已移除的泛化规则（误伤高）：system prompt/developer message、reveal/show/print、
#   你现在是…、告诉我/显示 + 提示词 等——正常文档问答极易中招。
_INJECTION_PATTERNS = [
    # 英文经典指令劫持
    r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)",
    r"you\s+are\s+now\s+(a|an)\s+",                    # 角色劫持 "you are now a ..."
    r"new\s+instructions?:",
    r"jailbreak|\bDAN\s+mode\b",
    r"pretend\s+(that\s+)?you\s+(are|have\s+no)",
    # 中文指令劫持
    r"忽略.{0,8}(指令|提示|规则|设定)",
    r"(无视|忘记).{0,8}(指令|提示|规则|设定)",
    r"(解除|突破).{0,4}(限制|封印|审查)",
    r"越狱模式|开发者模式",
]

_COMPILED = [re.compile(p, re.I) for p in _INJECTION_PATTERNS]


def detect_injection(text: str) -> list:
    """
    检测用户输入中的提示词注入意图（弱演示防护，可被简单变形绕过，非安全屏障）。
    :return: 命中的模式描述列表（空列表 = 未检出）
    """
    if not text or len(text) > 2000:
        # 超长输入不跑正则（DoS 防御），正常提问不会超 2000 字
        return []
    hits = []
    for i, pat in enumerate(_COMPILED):
        if pat.search(text):
            hits.append(_INJECTION_PATTERNS[i])
    return hits


# ============ 2. LLM 调用追踪 ============
_traces_indexed = False


def _traces_col():
    from utils.auth_utils import get_auth_tool
    return get_auth_tool().db["llm_traces"]


def trace_llm(session_id: str, kind: str, duration_ms: float,
              prompt_chars: int = 0, output_chars: int = 0,
              error: str = "", user_id: str = "") -> None:
    """
    记录一次 LLM 调用到 Mongo（失败不影响主链路）。
    :param kind: 调用类型：answer（问答生成）/ eval（质量评测）/ 其他自定义
    :param duration_ms: 耗时毫秒
    """
    global _traces_indexed
    try:
        if not _traces_indexed:
            from pymongo import ASCENDING
            _traces_col().create_index([("ts", ASCENDING)])
            _traces_col().create_index([("kind", ASCENDING), ("ts", ASCENDING)])
            _traces_indexed = True
        _traces_col().insert_one({
            "session_id": session_id,
            "user_id": user_id,
            "kind": kind,
            "duration_ms": round(float(duration_ms), 1),
            "prompt_chars": int(prompt_chars),
            "output_chars": int(output_chars),
            "error": (error or "")[:200],
            "ok": not error,
            "ts": time.time(),
        })
    except Exception as e:
        logger.warning(f"写入 llm_traces 失败（不影响主链路）: {e}")


def trace_stats() -> dict:
    """LLM 调用聚合统计：总次数/成功率/平均耗时（全站维度，供运维看）"""
    try:
        col = _traces_col()
        total = col.count_documents({})
        ok = col.count_documents({"ok": True})
        errs = col.count_documents({"ok": False})
        # 平均耗时（聚合管道）
        pipeline = [{"$group": {"_id": None, "avg_ms": {"$avg": "$duration_ms"}}}]
        rows = list(col.aggregate(pipeline))
        avg_ms = round(rows[0]["avg_ms"], 1) if rows else None
        return {"total": total, "ok": ok, "errors": errs,
                "success_rate": round(ok / total, 3) if total else None,
                "avg_ms": avg_ms}
    except Exception as e:
        logger.warning(f"统计 llm_traces 失败: {e}")
        return {"total": 0, "ok": 0, "errors": 0, "success_rate": None, "avg_ms": None}
