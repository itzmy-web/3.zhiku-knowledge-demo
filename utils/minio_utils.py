# utils/minio_utils.py

import json
import logging

from minio import Minio

from config.minio_config import minio_config

# 注意：不再在此处调用 setup_logging()
# 原因：setup_logging() 内部会执行 logger.handlers.clear() 清空所有已注册的 handler，
#       这会与 tool/logger.py 中配置的彩色日志 handler 冲突，导致日志输出异常。
#       日志配置统一由 tool/logger.py 在模块加载时完成。

# 懒加载改造：模块导入阶段不再创建连接 / 检查 bucket（否则 MinIO 服务没起时整个应用都 import 不了）
# 客户端实例缓存：首次调用 get_minio_client() 时才真正建连
_minio_client = None


def _create_minio_client():
    """创建 MinIO 客户端并确保 bucket 存在、配置访问权限"""
    # 1.创建客户端链接对象
    client = Minio(
        endpoint=minio_config.endpoint,# 地址
        access_key=minio_config.access_key,# 两个密钥
        secret_key=minio_config.secret_key,
        #是否强制启用HTTPS的加密链接方式，False：使用http；True：使用https
        secure=False
    )

    # 2.判断bucket是否存在，不存在则创建
    # 拿到 knowledge-base bucket（桶）
    found = client.bucket_exists(minio_config.bucket_name)
    if not found:
        client.make_bucket(minio_config.bucket_name)# 创建

    # 3. 定义当前bucket的访问权限
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": "*"},
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{minio_config.bucket_name}/*",
            },
        ],
    }
    # 4. 设置当前bucket的访问权限
    client.set_bucket_policy(minio_config.bucket_name, json.dumps(policy))
    return client


def get_minio_client():
    """获取 MinIO 客户端单例（懒加载：首次调用时才建立连接）"""
    global _minio_client
    if _minio_client is None:
        try:
            _minio_client = _create_minio_client()
        except Exception as e:
            logging.error(f"MinIO连接失败，错误原因：{e}")
            raise
    return _minio_client

if __name__ == '__main__':
    get_minio_client()