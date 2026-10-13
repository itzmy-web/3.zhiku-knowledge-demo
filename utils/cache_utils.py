# utils/cache_utils.py
"""
语义缓存层：相似问题（dense 向量余弦相似度 > 阈值）直接复用历史答案。
- 存储：Mongo qa_cache（问题/向量/答案/来源/时间），滚动保留最近 500 条
- 命中：返回缓存答案并标注 ⚡，省去整条 RAG 链路（秒回 + 省 token）
"""
import json
import math
import time

from tool.logger import logger

CACHE_THRESHOLD = 0.95   # 余弦相似度命中阈值
CACHE_MAX_KEEP = 500     # 滚动保留条数


def _col():
    from utils.auth_utils import get_auth_tool
    return get_auth_tool().db["qa_cache"]


def _cosine(a: list, b: list) -> float:
    """纯 Python 余弦相似度（避免 numpy 依赖）"""
    try:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1e-9
        nb = math.sqrt(sum(y * y for y in b)) or 1e-9
        return dot / (na * nb)
    except Exception:
        return 0.0


def lookup_cache(query: str, threshold: float = CACHE_THRESHOLD) -> dict:
    """
    查语义缓存。
    :return: {"answer":..., "sources":[...], "similar_question":..., "score":...} 未命中返回 None
    """
    if not query or len(query) > 500:
        return None
    try:
        from utils.embedding_utils import generate_embeddings
        emb = generate_embeddings([query])
        dense = emb.get("dense")[0]
        # 只比最近 200 条（性能可控），滚动窗口足够覆盖近期热点
        docs = list(_col().find({}, {"question": 1, "question_vec": 1, "answer": 1,
                                     "sources": 1, "ts": 1}).sort("ts", -1).limit(200))
        best, best_score = None, 0.0
        for d in docs:
            vec = d.get("question_vec")
            if not vec or len(vec) != len(dense):
                continue
            score = _cosine(dense, vec)
            if score > best_score:
                best, best_score = d, score
        if best and best_score >= threshold:
            logger.info(f"[语义缓存] 命中（相似度 {best_score:.3f}）：{best.get('question', '')[:40]}")
            return {
                "answer": best.get("answer", ""),
                "sources": best.get("sources") or [],
                "similar_question": best.get("question", ""),
                "score": round(best_score, 3),
            }
        return None
    except Exception as e:
        logger.warning(f"[语义缓存] 查询失败（不影响主流程）: {e}")
        return None


def save_cache(query: str, answer: str, sources: list) -> None:
    """保存一条缓存（仅成功回答），滚动淘汰超出上限的旧记录"""
    if not query or not answer:
        return
    try:
        from utils.embedding_utils import generate_embeddings
        emb = generate_embeddings([query])
        dense = emb.get("dense")[0]
        col = _col()
        col.update_one(
            {"question": query},
            {"$set": {"question": query, "question_vec": dense, "answer": answer,
                      "sources": sources or [], "ts": time.time()}},
            upsert=True
        )
        # 滚动淘汰
        count = col.count_documents({})
        if count > CACHE_MAX_KEEP:
            for d in col.find({}, {"_id": 1}).sort("ts", 1).limit(count - CACHE_MAX_KEEP):
                col.delete_one({"_id": d["_id"]})
    except Exception as e:
        logger.warning(f"[语义缓存] 保存失败（不影响主流程）: {e}")


def clear_cache() -> int:
    """清空全部缓存，返回删除条数"""
    try:
        return _col().delete_many({}).deleted_count
    except Exception as e:
        logger.warning(f"[语义缓存] 清空失败: {e}")
        return 0
