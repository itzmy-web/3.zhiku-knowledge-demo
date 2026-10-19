# utils/audit_utils.py
"""
汇思智藏 · 审计日志

集中记录安全相关动作（改角色 / 改租户 / 导入文档 / 删除文档 等），
供管理员在「📜 审计日志」页追溯。

设计原则：
- **fail-open**：写入失败只告警、绝不影响主业务流程（审计不能拖垮业务）。
- 数据落 MongoDB 集合 `kb_audit_logs`（与用户同库，无需额外配置）。
- 字段：action(动作类型) / actor_id(操作者) / target_id(被操作对象) /
  detail(详情 dict) / tenant_id(租户) / ts(时间戳)。
"""
import time

from tool.logger import logger


def record_audit(action: str, actor_id: str = None, target_id: str = None,
                 detail: dict = None, tenant_id: str = "default"):
    """写入一条审计日志；任何异常都吞掉，只告警。"""
    try:
        from utils.auth_utils import get_auth_tool
        col = get_auth_tool().db["kb_audit_logs"]
        col.insert_one({
            "action": action,
            "actor_id": actor_id or "unknown",
            "target_id": target_id,
            "detail": detail or {},
            "tenant_id": tenant_id or "default",
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as e:
        logger.warning(f"[审计] 写入失败（不影响主流程）: {e}")


# 动作类型常量（便于埋点处引用，避免拼写漂移）
ACTION_ROLE_CHANGE = "role_change"      # 管理员修改用户角色
ACTION_TENANT_CHANGE = "tenant_change"  # 管理员修改用户租户
ACTION_DOC_IMPORT = "doc_import"        # 文档导入
ACTION_DOC_DELETE = "doc_delete"        # 文档删除
