# utils/backup_utils.py
# -*- coding: utf-8 -*-
"""掌柜智库 · 备份与恢复工具（管理员专属接口调用 / 命令行两用）

备份内容（全量落盘到 ./backups/<时间戳>_<标签>/）：
  1. MongoDB：所有核心集合导出为 JSON（bson.json_util 保留 ObjectId / 时间类型）
  2. Milvus：向量集合（chunks / item_name）全量实体导出为 JSON（含向量）
  3. MinIO：桶内全部原始文件按对象名原样下载

恢复策略：
  - MongoDB：按 _id upsert（已存在则覆盖，不删除无关文档）→ 安全可重跑
  - Milvus：先按主键清空集合，再重新插入 → 完整还原（破坏性，仅恢复目标集合）
  - MinIO：按对象名重新上传（覆盖同名）

fail-open：单组件失败不影响其它组件；每个组件独立 try 并记录结果。
命令示例（在部署服务器、服务均在线时执行）：
  python utils/backup_utils.py --backup --label v1
  python utils/backup_utils.py --list
  python utils/backup_utils.py --restore backups/20260101_120000_v1 --components mongo,milvus
"""
import argparse
import json
import os
import time
from pathlib import Path

# 项目根目录（backup_utils.py 位于 utils/ 下）
ROOT = Path(__file__).resolve().parent.parent
BACKUP_ROOT = Path(os.getenv("BACKUP_DIR", str(ROOT / "backups")))

# 需备份的 MongoDB 集合（与指标采集保持一致）
_MONGO_COLLECTIONS = [
    "users", "kb_documents", "chat_sessions", "chat_message",
    "qa_evals", "llm_traces", "kb_spaces", "kb_audit_logs",
    "qa_cache", "qa_inspection", "kb_graph",
]


def _now_label() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


# ============ 备份 ============
def backup_mongodb(out_dir: Path) -> dict:
    from utils.auth_utils import get_auth_tool
    from bson import json_util
    db = get_auth_tool().db
    mongo_dir = out_dir / "mongo"
    mongo_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name in _MONGO_COLLECTIONS:
        try:
            docs = list(db[name].find({}))
            (mongo_dir / f"{name}.json").write_text(
                json_util.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            counts[name] = len(docs)
        except Exception as e:
            counts[name] = f"ERR:{e}"
    return counts


def backup_milvus(out_dir: Path) -> dict:
    from utils.milvus_utils import get_milvus_client
    from config.milvus_config import milvus_config
    client = get_milvus_client()
    milvus_dir = out_dir / "milvus"
    milvus_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name in [milvus_config.chunks_collection, milvus_config.item_name_collection]:
        try:
            if not client.has_collection(name):
                counts[name] = 0
                continue
            entities = []
            offset = 0
            batch = 5000
            while True:
                page = client.query(name, filter="", output_fields=["*"], limit=batch, offset=offset)
                if not page:
                    break
                entities.extend(page)
                offset += len(page)
                if len(page) < batch:
                    break
            (milvus_dir / f"{name}.json").write_text(
                json.dumps(entities, ensure_ascii=False, default=str), encoding="utf-8"
            )
            counts[name] = len(entities)
        except Exception as e:
            counts[name] = f"ERR:{e}"
    return counts


def backup_minio(out_dir: Path) -> int:
    from utils.minio_utils import get_minio_client
    from config.minio_config import minio_config
    mc = get_minio_client()
    minio_dir = out_dir / "minio"
    minio_dir.mkdir(parents=True, exist_ok=True)
    bucket = minio_config.bucket_name
    n = 0
    for obj in mc.list_objects(bucket, recursive=True):
        try:
            rel = obj.object_name
            local = minio_dir / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            mc.fget_object(bucket, obj.object_name, str(local))
            n += 1
        except Exception:
            # 单个文件失败不中断整体
            continue
    return n


def run_backup(label: str = "") -> dict:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c for c in (label or "") if c.isalnum() or c in ("-", "_")) or "manual"
    out_dir = BACKUP_ROOT / f"{_now_label()}_{safe_label}"
    out_dir.mkdir(parents=True, exist_ok=True)

    mongo_counts = backup_mongodb(out_dir)
    milvus_counts = backup_milvus(out_dir)
    minio_count = backup_minio(out_dir)

    manifest = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "label": safe_label,
        "dir": str(out_dir),
        "components": {
            "mongo": mongo_counts,
            "milvus": milvus_counts,
            "minio": minio_count,
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "dir": str(out_dir),
        "label": safe_label,
        "manifest": manifest,
        "message": f"备份完成：Mongo {sum(v for v in mongo_counts.values() if isinstance(v,int))} 篇 / "
                   f"Milvus {sum(v for v in milvus_counts.values() if isinstance(v,int))} 条 / "
                   f"MinIO {minio_count} 个文件",
    }


# ============ 列举 ============
def list_backups() -> list:
    if not BACKUP_ROOT.exists():
        return []
    items = []
    for d in sorted(BACKUP_ROOT.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        info = {"dir": str(d), "name": d.name, "label": d.name.split("_", 2)[-1] if "_" in d.name else d.name}
        mf = d / "manifest.json"
        if mf.exists():
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
                info["created_at"] = m.get("created_at", "")
                info["components"] = m.get("components", {})
            except Exception:
                pass
        items.append(info)
    return items


# ============ 恢复 ============
def restore_mongodb(backup_dir: Path) -> dict:
    from utils.auth_utils import get_auth_tool
    from bson import json_util
    db = get_auth_tool().db
    mongo_dir = backup_dir / "mongo"
    counts = {}
    if not mongo_dir.exists():
        return {"skipped": True}
    for f in sorted(mongo_dir.glob("*.json")):
        name = f.stem
        try:
            docs = json_util.loads(f.read_text(encoding="utf-8"))
            col = db[name]
            ok = 0
            for doc in docs:
                if "_id" in doc:
                    col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                else:
                    col.insert_one(doc)
                ok += 1
            counts[name] = ok
        except Exception as e:
            counts[name] = f"ERR:{e}"
    return counts


def restore_milvus(backup_dir: Path) -> dict:
    from utils.milvus_utils import get_milvus_client
    from config.milvus_config import milvus_config
    client = get_milvus_client()
    milvus_dir = backup_dir / "milvus"
    counts = {}
    if not milvus_dir.exists():
        return {"skipped": True}
    for f in sorted(milvus_dir.glob("*.json")):
        name = f.stem
        try:
            entities = json.loads(f.read_text(encoding="utf-8"))
            if not client.has_collection(name):
                counts[name] = "ERR:集合不存在"
                continue
            # 先清空（按主键），再插入 → 完整还原（破坏性）
            schema = client.describe_collection(name)
            pk = None
            for field in schema.get("fields", []):
                if field.get("is_primary"):
                    pk = field["name"]
                    break
            if pk:
                existing = client.query(name, filter="", output_fields=[pk], limit=100000)
                pks = [e[pk] for e in existing]
                if pks:
                    client.delete(name, pks)
            if entities:
                client.insert(name, entities)
                client.flush([name])
            counts[name] = len(entities)
        except Exception as e:
            counts[name] = f"ERR:{e}"
    return counts


def restore_minio(backup_dir: Path) -> int:
    from utils.minio_utils import get_minio_client
    from config.minio_config import minio_config
    mc = get_minio_client()
    minio_dir = backup_dir / "minio"
    bucket = minio_config.bucket_name
    n = 0
    if not minio_dir.exists():
        return 0
    for local in minio_dir.rglob("*"):
        if local.is_file():
            rel = str(local.relative_to(minio_dir)).replace("\\", "/")
            mc.fput_object(bucket, rel, str(local))
            n += 1
    return n


def restore_from(backup_dir: str, components: list = None) -> dict:
    components = components or ["mongo", "milvus", "minio"]
    d = Path(backup_dir)
    if not d.exists():
        return {"ok": False, "message": f"备份目录不存在：{backup_dir}"}
    result = {"ok": True, "dir": str(d), "restored": {}}
    if "mongo" in components:
        result["restored"]["mongo"] = restore_mongodb(d)
    if "milvus" in components:
        result["restored"]["milvus"] = restore_milvus(d)
    if "minio" in components:
        result["restored"]["minio"] = restore_minio(d)
    return result


# ============ 命令行入口 ============
def _cli():
    ap = argparse.ArgumentParser(description="掌柜智库 备份/恢复工具")
    ap.add_argument("--backup", action="store_true", help="执行备份")
    ap.add_argument("--label", default="", help="备份标签")
    ap.add_argument("--list", action="store_true", help="列举已有备份")
    ap.add_argument("--restore", metavar="DIR", help="从指定备份目录恢复")
    ap.add_argument("--components", default="mongo,milvus,minio", help="恢复组件：mongo,milvus,minio")
    args = ap.parse_args()

    if args.list:
        for b in list_backups():
            print(b)
    elif args.backup:
        print(json.dumps(run_backup(args.label), ensure_ascii=False, indent=2))
    elif args.restore:
        print(json.dumps(restore_from(args.restore, args.components.split(",")), ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    _cli()
