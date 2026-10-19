# utils/metrics_utils.py
# -*- coding: utf-8 -*-
"""汇思智藏 · 系统监控指标采集（管理员专属 /api/admin/metrics）

采集内容：
- 运行时长（进程 uptime）
- MongoDB 各核心集合文档数
- Milvus 向量集合实体数（chunks / item_name）
- MinIO 对象总数
- 逐项 fail-open：单个组件不可达时该项记 -1，并填入 errors 列表，不影响整体返回。

注意：本模块只读取计数，不改动任何数据；耗时操作（MinIO 列举）已在调用方按需触发。
"""
import time

# 进程启动时刻
_BOOT_TS = time.time()

# MongoDB 需统计的核心集合
_MONGO_COLLECTIONS = [
    "users", "kb_documents", "chat_sessions", "chat_message",
    "qa_evals", "llm_traces", "kb_spaces", "kb_audit_logs",
    "qa_cache", "qa_inspection", "kb_graph",
]


def uptime_seconds() -> int:
    return int(time.time() - _BOOT_TS)


def collect_metrics() -> dict:
    metrics = {
        "uptime_seconds": uptime_seconds(),
        "mongo": {},
        "milvus": {},
        "minio": {"object_count": -1},
        "errors": [],
    }

    # ---------- MongoDB ----------
    try:
        from utils.auth_utils import get_auth_tool
        db = get_auth_tool().db
        for name in _MONGO_COLLECTIONS:
            try:
                metrics["mongo"][name] = db[name].count_documents({})
            except Exception as e:  # 单集合异常不影响其余
                metrics["mongo"][name] = -1
                metrics["errors"].append(f"mongo:{name}:{e}")
    except Exception as e:
        metrics["errors"].append(f"mongo:{e}")

    # ---------- Milvus ----------
    try:
        from utils.milvus_utils import get_milvus_client
        from config.milvus_config import milvus_config
        client = get_milvus_client()
        for name in [milvus_config.chunks_collection, milvus_config.item_name_collection]:
            try:
                if client.has_collection(name):
                    stats = client.get_collection_stats(name) or {}
                    metrics["milvus"][name] = int(stats.get("row_count", -1))
                else:
                    metrics["milvus"][name] = 0
            except Exception as e:
                metrics["milvus"][name] = -1
                metrics["errors"].append(f"milvus:{name}:{e}")
    except Exception as e:
        metrics["errors"].append(f"milvus:{e}")

    # ---------- MinIO ----------
    try:
        from utils.minio_utils import get_minio_client
        from config.minio_config import minio_config
        mc = get_minio_client()
        count = 0
        for _ in mc.list_objects(minio_config.bucket_name, recursive=True):
            count += 1
        metrics["minio"]["object_count"] = count
    except Exception as e:
        metrics["minio"]["object_count"] = -1
        metrics["errors"].append(f"minio:{e}")

    return metrics
