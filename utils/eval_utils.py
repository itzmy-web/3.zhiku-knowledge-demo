# utils/eval_utils.py
"""
轻量 RAG 质量评测（类 Ragas 思路，零新依赖）：
- faithfulness 忠实度：回答是否严格基于检索到的参考内容（幻觉越少分越高）
- relevance   相关度：回答与用户问题的匹配程度
每次问答结束后由前端上报 → LLM 一次性双指标打分 → 存 Mongo → 看板聚合展示
"""
import json
import time
import re

from tool.logger import logger

# 复用与答案生成同一个 LLM 客户端（bge/lkeap 那条链路）
from utils.llm_utils import get_llm_client
from utils.auth_utils import get_auth_tool

EVAL_PROMPT = """你是一名严格的 RAG 系统质量评估员。请根据以下信息打分（0~5 分，可以是小数）：

【用户问题】
{question}

【检索到的参考内容】
{contexts}

【系统回答】
{answer}

评分标准：
1. faithfulness（忠实度）：回答中的事实是否都能在参考内容中找到依据？
   5=完全有依据无幻觉；3=大部分有依据；1=大量编造；0=与参考内容矛盾
2. relevance（相关度）：回答是否切题、完整地回应了用户问题？
   5=精准完整；3=部分切题；1=答非所问

只输出 JSON，不要输出任何其他文字：
{{"faithfulness": <0-5>, "relevance": <0-5>, "reason": "<20字以内简要理由>"}}"""


def _clip(value, lo=0.0, hi=5.0):
    """把分数夹在 [0,5] 区间，防御 LLM 输出越界"""
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return None


def eval_qa(question: str, answer: str, contexts: list) -> dict:
    """
    调用 LLM 对单轮问答打分。
    :param contexts: 来源片段列表 [{"title":..., "snippet":...}] 或字符串列表
    :return: {"faithfulness": float, "relevance": float, "reason": str} 失败返回 None 字段
    """
    import time as _time
    from utils.security_utils import trace_llm
    _t0 = _time.time()
    ctx_text = "\n".join(
        f"[{i+1}] {(c.get('title') if isinstance(c, dict) else '') or '来源'}: "
        f"{(c.get('snippet') if isinstance(c, dict) else str(c)) or ''}"
        for i, c in enumerate(contexts or [])
    ) or "（无参考内容）"
    prompt = EVAL_PROMPT.format(question=question, contexts=ctx_text, answer=answer or "（空）")
    try:
        llm = get_llm_client()
        resp = llm.invoke(prompt)
        text = resp.content or ""
        # 容错：剥掉 ```json 包裹 / 前后杂文，抓第一个 {...}
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        result = {
            "faithfulness": _clip(data.get("faithfulness")),
            "relevance": _clip(data.get("relevance")),
            "reason": str(data.get("reason", ""))[:50],
        }
        trace_llm("eval", "eval", (_time.time() - _t0) * 1000,
                  prompt_chars=len(prompt), output_chars=len(text))
        return result
    except Exception as e:
        logger.error(f"RAG 评测打分失败: {e}")
        trace_llm("eval", "eval", (_time.time() - _t0) * 1000,
                  prompt_chars=len(prompt), error=str(e))
        return {"faithfulness": None, "relevance": None, "reason": f"评测失败: {e}"[:50]}


def _col():
    return get_auth_tool().db["qa_evals"]


_indexed = False


def _ensure_index():
    global _indexed
    if not _indexed:
        col = _col()
        col.create_index([("user_id", 1), ("ts", -1)])
        _indexed = True


def save_eval(user_id: str, session_id: str, question: str, answer: str,
              sources: list, scores: dict) -> str:
    """保存一条评测记录，返回记录 id"""
    _ensure_index()
    doc = {
        "user_id": user_id,
        "session_id": session_id,
        "question": (question or "")[:200],
        "answer": (answer or "")[:2000],
        "sources_count": len(sources or []),
        "faithfulness": scores.get("faithfulness"),
        "relevance": scores.get("relevance"),
        "reason": scores.get("reason", ""),
        "ts": time.time(),
    }
    return str(_col().insert_one(doc).inserted_id)


def list_evals(user_id: str, limit: int = 30) -> list:
    """最近 N 条评测记录（时间倒序）"""
    _ensure_index()
    docs = _col().find({"user_id": user_id}).sort("ts", -1).limit(limit)
    out = []
    for d in docs:
        out.append({
            "question": d.get("question", ""),
            "faithfulness": d.get("faithfulness"),
            "relevance": d.get("relevance"),
            "reason": d.get("reason", ""),
            "sources_count": d.get("sources_count", 0),
            "ts": d.get("ts", 0),
        })
    return out


def eval_stats(user_id: str) -> dict:
    """聚合统计：均值 + 打分覆盖率 + 总轮数"""
    _ensure_index()
    docs = list(_col().find({"user_id": user_id}, {"faithfulness": 1, "relevance": 1, "sources_count": 1}))
    total = len(docs)
    faith = [d["faithfulness"] for d in docs if d.get("faithfulness") is not None]
    rel = [d["relevance"] for d in docs if d.get("relevance") is not None]
    srcs = [d.get("sources_count", 0) for d in docs]
    with_src = sum(1 for s in srcs if s > 0)
    return {
        "total": total,
        "faithfulness_avg": round(sum(faith) / len(faith), 2) if faith else None,
        "relevance_avg": round(sum(rel) / len(rel), 2) if rel else None,
        "scored": len(faith),
        "retrieval_hit_rate": round(with_src / total, 2) if total else None,  # 有引用来源的问答占比
    }
