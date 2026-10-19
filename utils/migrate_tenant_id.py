# utils/migrate_tenant_id.py
"""
汇思智藏 · 租户字段迁移脚本

作用：给已有的 Milvus 集合（默认 kb_chunks）补上 `tenant_id` 字段。
- 新集合在建表时已由 node_import_milvus 自动带 `tenant_id`（默认 "default"），无需本脚本。
- 仅当**启用多租户**（非 default 租户）时，才需要对历史集合执行本脚本，
  否则旧集合没有该字段，按 `tenant_id == "X"` 过滤会报错。

安全性：
- 幂等：字段已存在则跳过，不会重复添加。
- 默认值：新字段以 default_value="default" 回填历史切片，与查询侧
  `tenant_id=="default" 不下过滤` 的默认行为一致，单租户数据零影响。

用法（需在本地、Milvus 已启动、且能 import 项目的前提下执行）：
    cd <项目根>
    python utils/migrate_tenant_id.py                 # 迁移默认集合 kb_chunks
    python utils/migrate_tenant_id.py --collection my_coll
    python utils/migrate_tenant_id.py --dry-run       # 只检查不改动

注意：本脚本会连接真实 Milvus，请在确认环境后执行；沙箱无 Milvus 时无需运行。
"""
import argparse
import sys
import os

# 保证能从项目根 import（与 kb_service 同策略）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymilvus import DataType

from config.milvus_config import milvus_config
from utils.milvus_utils import get_milvus_client
from tool.logger import logger


def field_exists(client, collection_name: str, field_name: str) -> bool:
    try:
        schema = client.describe_collection(collection_name)
        fields = schema.get("fields") or schema.get("schema", {}).get("fields") or []
        for f in fields:
            if f.get("name") == field_name:
                return True
    except Exception as e:  # 集合不存在等情况
        logger.warning(f"describe_collection 失败（{collection_name}）: {e}")
    return False


def migrate(collection_name: str, dry_run: bool = False) -> bool:
    client = get_milvus_client()
    if not client:
        logger.error("Milvus 连接失败，无法迁移")
        return False

    if not client.has_collection(collection_name):
        logger.warning(f"集合 {collection_name} 不存在，跳过")
        return False

    if field_exists(client, collection_name, "tenant_id"):
        logger.info(f"集合 {collection_name} 已含 tenant_id 字段，无需迁移（幂等跳过）")
        return True

    if dry_run:
        logger.info(f"[dry-run] 集合 {collection_name} 缺少 tenant_id 字段，将添加（不执行）")
        return True

    client.add_field(
        collection_name=collection_name,
        field_name="tenant_id",
        datatype=DataType.VARCHAR,
        max_length=64,
        default_value="default",
    )
    logger.info(f"已为集合 {collection_name} 添加 tenant_id 字段（默认 'default'，历史切片自动回填）")
    return True


def main():
    parser = argparse.ArgumentParser(description="为 Milvus 集合补 tenant_id 字段")
    parser.add_argument(
        "--collection",
        default=milvus_config.chunks_collection,
        help=f"目标集合名（默认取 milvus_config.chunks_collection={milvus_config.chunks_collection}）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只检查不改动")
    args = parser.parse_args()

    ok = migrate(args.collection, dry_run=args.dry_run)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
