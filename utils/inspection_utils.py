# utils/inspection_utils.py
"""
定时知识巡检：用预设问题集批量走"定向检索+回答+评分"，监控知识库健康度。
- run_inspection(user_id)：跑一轮巡检（每题：检索该用户最近文档 → 回答 → eval_qa 打分）
- 结果存 Mongo qa_inspection，保留最近 30 轮
- 定时：后台线程每天 INSPECTION_HOUR 点自动执行（未配置则只支持手动触发）
"""
import threading
import time
from datetime import datetime

from tool.logger import logger

# 默认巡检问题集（可通过 .env 的 INSPECTION_QUESTIONS 用 | 分隔覆盖）
DEFAULT_QUESTIONS = [
    "HAK 180 烫金机的安全注意事项有哪些？",
    "HAK 180 烫金机怎么设置温度？",
    "设备日常维护保养有哪些要求？",
]

_pass_thread = None   # 巡检后台线程（守护），保证全局只起一个


def _col():
    from utils.auth_utils import get_auth_tool
    return get_auth_tool().db["qa_inspection"]


def _get_questions() -> list:
    import os
    raw = os.getenv("INSPECTION_QUESTIONS", "")
    qs = [q.strip() for q in raw.split("|") if q.strip()] if raw else DEFAULT_QUESTIONS
    return qs


def _user_docs(user_id: str, limit: int = 2) -> list:
    """取该用户最近导入成功的文档标题，作为巡检定向检索范围"""
    try:
        from utils.kb_docs_utils import get_kb_docs_tool
        docs = get_kb_docs_tool().list_by_user(user_id)
        return [d["file_name"] for d in docs if d.get("status") == "completed"][:limit]
    except Exception:
        return []


def run_inspection(user_id: str) -> dict:
    """跑一轮巡检并落库，返回本轮摘要"""
    from web.api.kb_service import _doc_answer, _doc_retrieve   # 复用对比问答的检索+回答
    from utils.eval_utils import eval_qa
    questions = _get_questions()
    docs = _user_docs(user_id)
    items = []
    scores_f, scores_r = [], []
    for q in questions:
        answer, faith, rel, src_count = "", None, None, 0
        try:
            if docs:
                merged = []
                for d in docs[:2]:
                    merged.extend(_doc_retrieve(q, d, top_k=2))
                src_count = len(merged)
                answer = _doc_answer(q, merged) if merged else "知识库中未检索到相关内容。"
            else:
                answer = "（巡检跳过：该用户暂无已导入文档）"
            scores = eval_qa(q, answer, [d["content"][:200] for d in merged[:3]] if docs and merged else [])
            faith, rel = scores.get("faithfulness"), scores.get("relevance")
            if faith is not None:
                scores_f.append(faith)
            if rel is not None:
                scores_r.append(rel)
        except Exception as e:
            logger.warning(f"[巡检] 题目失败: {q} → {e}")
            answer = f"巡检出错: {e}"[:120]
        items.append({"question": q, "answer": answer[:300], "sources_count": src_count,
                      "faithfulness": faith, "relevance": rel})
    record = {
        "user_id": user_id,
        "ts": time.time(),
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "doc_count": len(docs),
        "items": items,
        "faithfulness_avg": round(sum(scores_f) / len(scores_f), 2) if scores_f else None,
        "relevance_avg": round(sum(scores_r) / len(scores_r), 2) if scores_r else None,
        "passed": bool(scores_f) and (sum(scores_f) / len(scores_f)) >= 3.5,
    }
    _col().insert_one(record)
    # 滚动保留最近 30 轮
    cnt = _col().count_documents({"user_id": user_id})
    if cnt > 30:
        for d in _col().find({"user_id": user_id}, {"_id": 1}).sort("ts", 1).limit(cnt - 30):
            _col().delete_one({"_id": d["_id"]})
    logger.info(f"[巡检] 完成：忠实度均值 {record['faithfulness_avg']}，通过={record['passed']}")
    return {k: record[k] for k in ("ts", "date", "items", "faithfulness_avg", "relevance_avg", "passed", "doc_count")}


def list_inspections(user_id: str, limit: int = 10) -> list:
    docs = _col().find({"user_id": user_id}).sort("ts", -1).limit(limit)
    out = []
    for d in docs:
        out.append({k: d.get(k) for k in ("ts", "date", "faithfulness_avg", "relevance_avg", "passed", "doc_count")})
    return out


def start_scheduler():
    """启动每日定时巡检守护线程（INSPECTION_HOUR 未配置则不启动）"""
    global _pass_thread
    import os
    hour_raw = os.getenv("INSPECTION_HOUR", "")
    if not hour_raw or _pass_thread is not None:
        return
    try:
        hour = int(hour_raw)
    except ValueError:
        return

    def _loop():
        from utils.auth_utils import get_auth_tool
        while True:
            now = datetime.now()
            # 每天到了指定小时且今天还没跑过就执行（对最近活跃用户跑一轮）
            if now.hour == hour:
                try:
                    users = get_auth_tool().users.find({}, {"user_id": 1}).limit(50)
                    for u in users:
                        uid = u.get("user_id")
                        if not uid:
                            continue
                        last = list(_col().find({"user_id": uid}).sort("ts", -1).limit(1))
                        if last and last[0].get("date", "").startswith(now.strftime("%Y-%m-%d")):
                            continue   # 今天已巡检
                        run_inspection(uid)
                except Exception as e:
                    logger.warning(f"[巡检] 定时任务异常: {e}")
            time.sleep(1800)   # 半小时查一次钟点

    _pass_thread = threading.Thread(target=_loop, daemon=True, name="kb-inspection")
    _pass_thread.start()
    logger.info(f"[巡检] 定时线程已启动：每天 {hour} 点自动巡检")
