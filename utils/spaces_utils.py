# utils/spaces_utils.py
"""
多知识库空间 + 邀请协作：
- 空间 = 一组文档（file_title 白名单）+ 成员列表。检索时按 file_title in [...] 过滤，无需改 Milvus schema。
- owner 建空间、添加文档；成员凭邀请码加入，共享空间内文档与问答。
"""
import secrets
import time

from tool.logger import logger

MAX_SPACES_PER_USER = 10


def _col():
    from utils.auth_utils import get_auth_tool
    return get_auth_tool().db["kb_spaces"]


def _public(s: dict, user_id: str) -> dict:
    return {
        "space_id": s.get("space_id"),
        "name": s.get("name"),
        "owner_id": s.get("owner_id"),
        "is_owner": s.get("owner_id") == user_id,
        "file_titles": s.get("file_titles") or [],
        "members": s.get("members") or [],
        "created_at": s.get("created_at", ""),
    }


def create_space(user_id: str, name: str) -> dict:
    name = (name or "").strip()[:30] or "未命名空间"
    if _col().count_documents({"owner_id": user_id}) >= MAX_SPACES_PER_USER:
        return {"ok": False, "message": f"最多创建 {MAX_SPACES_PER_USER} 个空间"}
    space_id = "sp-" + secrets.token_hex(5)
    _col().insert_one({
        "space_id": space_id, "name": name, "owner_id": user_id,
        "members": [user_id], "file_titles": [], "invite_code": secrets.token_hex(4),
        "created_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    return {"ok": True, "space_id": space_id}


def list_spaces(user_id: str) -> list:
    """返回我拥有的 + 我加入的空间"""
    docs = _col().find({"members": user_id}).sort("created_at", -1)
    return [_public(d, user_id) for d in docs]


def delete_space(user_id: str, space_id: str) -> dict:
    res = _col().delete_one({"space_id": space_id, "owner_id": user_id})
    return {"ok": res.deleted_count > 0}


def add_doc(user_id: str, space_id: str, file_title: str) -> dict:
    s = _col().find_one({"space_id": space_id, "members": user_id})
    if not s:
        return {"ok": False, "message": "空间不存在或无权限"}
    file_title = (file_title or "").strip()
    if not file_title:
        return {"ok": False, "message": "file_title 不能为空"}
    _col().update_one({"space_id": space_id}, {"$addToSet": {"file_titles": file_title}})
    return {"ok": True}


def remove_doc(user_id: str, space_id: str, file_title: str) -> dict:
    s = _col().find_one({"space_id": space_id, "owner_id": user_id})
    if not s:
        return {"ok": False, "message": "仅空间所有者可移除文档"}
    _col().update_one({"space_id": space_id}, {"$pull": {"file_titles": file_title}})
    return {"ok": True}


def get_invite_code(user_id: str, space_id: str) -> dict:
    """所有者获取/重置邀请码"""
    s = _col().find_one({"space_id": space_id, "owner_id": user_id})
    if not s:
        return {"ok": False, "message": "仅空间所有者可获取邀请码"}
    code = s.get("invite_code") or secrets.token_hex(4)
    _col().update_one({"space_id": space_id}, {"$set": {"invite_code": code}})
    return {"ok": True, "invite_code": code, "space_name": s.get("name")}


def join_space(user_id: str, code: str) -> dict:
    code = (code or "").strip()
    s = _col().find_one({"invite_code": code})
    if not s:
        return {"ok": False, "message": "邀请码无效"}
    if user_id in (s.get("members") or []):
        return {"ok": False, "message": "你已在该空间中"}
    _col().update_one({"space_id": s["space_id"]}, {"$addToSet": {"members": user_id}})
    return {"ok": True, "space_name": s.get("name")}


def get_space_titles(user_id: str, space_id: str) -> list:
    """查询链路用：校验成员身份并返回该空间的文档白名单"""
    s = _col().find_one({"space_id": space_id, "members": user_id})
    return (s.get("file_titles") or []) if s else None


def get_space_denied_titles(user_id: str) -> list:
    """查询链路用：返回当前用户【无权访问】的文档标题集合，用于默认检索时排除。

    逻辑：遍历全部知识空间——
      - 用户是成员的空间：其文档可见（不计入排除）
      - 用户不是成员的空间：其文档不可见（计入排除）
      - 未分配任何空间的文档：不会出现在任何空间的 file_titles 中 → 永远可见（公共文档）
    返回去重后的 file_title 列表；为空表示无需排除（= 全库可见，兼容旧单空间/无空间场景）。
    fail-open：任意异常返回 []，避免影响正常检索可用性。
    """
    try:
        denied = set()
        for s in _col().find({}):
            members = s.get("members") or []
            if user_id in members:
                continue  # 我有权限的空间，文档可见
            for t in (s.get("file_titles") or []):
                denied.add(t)
        return list(denied)
    except Exception as e:
        logger.warning(f"[访问控制] 计算排除列表失败（不过滤）: {e}")
        return []

