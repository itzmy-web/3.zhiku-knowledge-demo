# utils/kb_docs_utils.py
# -*- coding: utf-8 -*-
"""掌柜智库 · 文档管理模块

kb_documents 集合：记录每次导入的文档（按用户隔离）
- record_document：上传时登记（status=processing）
- mark_completed / mark_failed：后台任务结束时更新状态
- list_by_user：文档管理页列表
- delete_document：删除 = Mongo 记录 + Milvus 向量(file_title) + MinIO 对象 三处清理
懒加载建连，不阻塞 import。
"""
import os
from datetime import datetime

from pymongo import MongoClient, ASCENDING

from tool.logger import logger


class KbDocsTool:
    def __init__(self):
        self.client = MongoClient(os.getenv("MONGO_URL"))
        self.db = self.client[os.getenv("MONGO_DB_NAME")]
        self.docs = self.db["kb_documents"]
        self.docs.create_index([("user_id", ASCENDING), ("created_at", -1)])
        self.docs.create_index([("task_id", ASCENDING)], unique=True)
        logger.info("KbDocsTool ready (kb_documents collection indexed)")

    def record_document(self, user_id: str, task_id: str, file_name: str, minio_object: str = "", minio_bucket: str = ""):
        """上传时登记文档（若同 task_id 重复上传则跳过）"""
        try:
            self.docs.update_one(
                {"task_id": task_id},
                {"$set": {
                    "user_id": user_id or "",
                    "file_name": file_name,
                    "minio_object": minio_object,
                    "minio_bucket": minio_bucket,
                    "status": "processing",
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }, "$setOnInsert": {"created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}},
                upsert=True,
            )
        except Exception as e:
            logger.warning(f"[{task_id}] kb_documents 登记失败（不影响导入）：{e}")

    def mark_completed(self, task_id: str, chunk_count: int = None):
        try:
            update = {"$set": {"status": "completed", "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}
            if chunk_count is not None:
                update["$set"]["chunk_count"] = chunk_count
            self.docs.update_one({"task_id": task_id}, update)
        except Exception as e:
            logger.warning(f"[{task_id}] kb_documents 完成标记失败：{e}")

    def mark_failed(self, task_id: str, error: str = ""):
        try:
            self.docs.update_one({"task_id": task_id}, {"$set": {
                "status": "failed", "error": str(error)[:300],
                "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}})
        except Exception as e:
            logger.warning(f"[{task_id}] kb_documents 失败标记异常：{e}")

    def list_by_user(self, user_id: str, limit: int = 100):
        out = []
        for doc in self.docs.find({"user_id": user_id}).sort("created_at", -1).limit(limit):
            out.append({
                "task_id": doc.get("task_id"),
                "file_name": doc.get("file_name"),
                "status": doc.get("status"),
                "chunk_count": doc.get("chunk_count"),
                "error": doc.get("error", ""),
                "created_at": doc.get("created_at"),
                "completed_at": doc.get("completed_at", ""),
            })
        return out

    def get_by_task(self, task_id: str):
        return self.docs.find_one({"task_id": task_id})


_kb_docs_tool = None


def get_kb_docs_tool() -> KbDocsTool:
    global _kb_docs_tool
    if _kb_docs_tool is None:
        _kb_docs_tool = KbDocsTool()
    return _kb_docs_tool
