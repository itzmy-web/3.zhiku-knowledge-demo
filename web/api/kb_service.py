# -*- coding: utf-8 -*-
"""汇思智藏 · 一体化服务（导入 + 对话 合并到 8000 端口）

访问地址：
  http://127.0.0.1:8000/            → 导航首页（标签页切换 导入/对话）
  http://127.0.0.1:8000/import.html → 直接进导入页
  http://127.0.0.1:8000/chat.html   → 直接进对话页
  http://127.0.0.1:8000/docs        → 全部接口的 Swagger 文档

原理：import_service 和 query_service 的路由互不冲突，
     用 include_router 合并到同一个 FastAPI 应用上。
"""
import os

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(override=True)

from pathlib import Path
import json
import time
import uvicorn
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi import Body
from pydantic import BaseModel
from starlette.responses import FileResponse, RedirectResponse
from starlette.middleware.cors import CORSMiddleware

from web.api.import_service import app as import_app
from web.api.query_service import app as query_app

# ============ 合并两个应用 ============
app = FastAPI(
    title="汇思智藏-一体化服务",
    description="导入流程 + 查询流程 合并版（端口 8000）"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # token 走 header 而非 cookie；与 allow_origins=["*"] 共存时必须关掉 credentials
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注意：导航首页必须先注册，否则会被子应用里的 "/" 跳转抢先匹配
@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def home():
    # 已登录（演示模式：localStorage 有 token）直接进工作台，否则先到登录页
    html_path = Path(__file__).absolute().parent.parent / "page" / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在：{html_path}")
    return FileResponse(html_path)


# 登录页（样式复刻自「问题自检平台」）
@app.get("/login.html", include_in_schema=False)
async def login_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "login.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在：{html_path}")
    return FileResponse(html_path)


# 数字人助手组件（柜柜）
@app.get("/assistant.js", include_in_schema=False)
async def assistant_js():
    js_path = Path(__file__).absolute().parent.parent / "page" / "assistant.js"
    if not js_path.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在：{js_path}")
    return FileResponse(js_path, media_type="application/javascript")


@app.get("/avatar.js", include_in_schema=False)
async def avatar_js():
    js_path = Path(__file__).absolute().parent.parent / "page" / "avatar.js"
    if not js_path.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在：{js_path}")
    return FileResponse(js_path, media_type="application/javascript")


# 数字人 Demo 页（魔珐星云）
@app.get("/dh.html", include_in_schema=False)
async def dh_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "dh.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在：{html_path}")
    return FileResponse(html_path)


# ============ 数字人（魔珐星云）——A5：app_secret 不下发浏览器 ============
# 背景：魔珐网关用「签名」鉴权，不是简单的密钥字段。SDK 源码（xmovAvatar@latest.js）里
# headersNeedSign(appId, appSecret, method, url, data) 的做法是：
#   ts   = 秒级时间戳
#   body = 递归按 Unicode 码点排序 key → JSON.stringify → 非 ASCII 转 \uXXXX → 去掉所有空格
#   token= md5( path.toLowerCase() + method.toLowerCase() + body + appSecret + ts )
#   请求头带 X-APP-ID / X-TOKEN / X-TIMESTAMP
# 因此「只往代理里塞一个 X-App-Secret 头」是无效的（实测网关一律返回 签名错误 error_code=4）。
# 正确做法：浏览器把 appSecret 置空、gatewayServer 指向本代理；代理在服务端用真密钥
# 「重算签名」后转发 —— 密钥全程不进浏览器。
DH_DEFAULT_GATEWAY = "https://nebula-agent.xingyun3d.com/user/v1/ttsa/session"


def _dh_gateway():
    """从 .env 的 DIGITAL_HUMAN_GATEWAY_SERVER 拆出 (base, path)，供代理转发与重算签名"""
    import urllib.parse
    raw = os.getenv("DIGITAL_HUMAN_GATEWAY_SERVER", DH_DEFAULT_GATEWAY)
    parts = urllib.parse.urlsplit(raw)
    base = f"{parts.scheme}://{parts.netloc}"
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return base, path


def _dh_deepsort(obj):
    """递归按 key 的 Unicode 码点排序（复刻 SDK 的 compareUnicodeCodePoints）"""
    if isinstance(obj, dict):
        return {k: _dh_deepsort(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_dh_deepsort(v) for v in obj]
    return obj


def _dh_normalize_body(data) -> str:
    """复刻 SDK：JSON.stringify(排序后) → 非 ASCII 转 \\uXXXX → 去空格。

    Python 的 ensure_ascii=True + separators=(',',':') 与 JS 的
    escapeNonAsciiForPythonJson(JSON.stringify(x)).replace(/ /g,'') 等价。
    """
    return json.dumps(_dh_deepsort(data), ensure_ascii=True, separators=(",", ":"))


def _dh_sign(path_with_query: str, method: str, data, app_secret: str, ts: int) -> str:
    """复刻 SDK 的 headersNeedSign：md5(path + method + body + appSecret + ts)"""
    raw = f"{path_with_query.lower()}{method.lower()}{_dh_normalize_body(data)}{app_secret}{ts}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# 数字人（魔珐星云）凭证——A5：密钥只存后端 .env，绝不下发浏览器。
# 说明：本接口不强制登录 —— web/page/dh.html 与 avatar.js 的 fetch 均未带 token
# （前端错误文案里也写了「可能未登录」），强制鉴权会让数字人 401 直接起不来；
# 本接口只回 app_id 与布尔状态，不含任何密钥，故放开访问不构成泄露。
@app.get("/api/digital-human/credentials", include_in_schema=False)
async def digital_human_credentials():
    app_id = os.getenv("DIGITAL_HUMAN_APP_ID", "")
    secret = os.getenv("DIGITAL_HUMAN_APP_SECRET", "")
    return {
        "app_id": app_id,
        "secret_configured": bool(secret),
        "gateway_server": os.getenv("DIGITAL_HUMAN_GATEWAY_SERVER", DH_DEFAULT_GATEWAY),
        "proxy_endpoint": "/api/digital-human/proxy",
        "configured": bool(app_id) and bool(secret),
    }


# A5 会话代理：SDK 把 gatewayServer 指到这里（同源），代理重算签名后转发真网关。
# 兼容 POST（开会话）与 DELETE（结束会话）——SDK 只对 gatewayServer 发这两种请求。
# 说明：同样不强制登录 —— 是 SDK（浏览器内）在调用本端点，它无法携带本站 token；
# 本端点的安全边界是「密钥不出服务端」，而非登录态。
@app.api_route("/api/digital-human/proxy", methods=["POST", "DELETE", "OPTIONS"], include_in_schema=False)
async def digital_human_proxy(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        })
    import urllib.error
    import urllib.request

    app_id = os.getenv("DIGITAL_HUMAN_APP_ID", "")
    app_secret = os.getenv("DIGITAL_HUMAN_APP_SECRET", "")
    if not app_id or not app_secret:
        raise HTTPException(status_code=503, detail="服务端未配置 DIGITAL_HUMAN_APP_ID / DIGITAL_HUMAN_APP_SECRET")

    raw = await request.body()
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        data = {}

    base, path = _dh_gateway()
    ts = int(time.time())
    headers = {
        "Content-Type": request.headers.get("content-type", "application/json"),
        "Accept": "application/json",
        "X-APP-ID": app_id,
        "X-TOKEN": _dh_sign(path, request.method, data, app_secret, ts),
        "X-TIMESTAMP": str(ts),
    }
    req = urllib.request.Request(base + path, data=raw or None, headers=headers, method=request.method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return Response(content=body, status_code=resp.status,
                            media_type=resp.headers.get("content-type", "application/json"))
    except urllib.error.HTTPError as e:
        return Response(content=e.read(), status_code=e.code,
                        media_type=e.headers.get("content-type", "application/json"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"数字人网关代理失败: {e}")


# 数字人远程诊断：前端把 SDK 容器内的元素/播放状态 POST 上来，落盘供排查
@app.post("/api/digital-human/report", include_in_schema=False)
async def digital_human_report(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {"raw": "invalid json"}
    data["_received_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out = Path(__file__).absolute().parents[2] / "_dh_diag.json"
    try:
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True}

# 两个子应用的路由互不冲突，直接合并
app.include_router(import_app.router)
app.include_router(query_app.router)


# ============ 真登录 · 多用户体系（/api/auth/*） ============
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from utils.auth_utils import get_auth_tool, verify_token, has_permission


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def get_current_user(request: Request):
    """依赖：解析当前用户；未登录返回 None。

    兼容三种取 token 方式（云端 CDN 会改写 Authorization 头，前端统一补发
    X-Auth-Token，第三方回调只能用 ?token= 查询参数）：
    Authorization: Bearer → X-Auth-Token 头 → ?token= 查询参数。
    注意：判定逻辑是"校验失败才降级"，不能是"头为空才回退"（CDN 总会填头）。
    """
    token = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    user_id = verify_token(token) if token else ""
    if not user_id:
        # Authorization 校验失败 → 尝试 X-Auth-Token
        token = (request.headers.get("x-auth-token") or "").strip()
        user_id = verify_token(token) if token else ""
    if not user_id:
        # 仍失败 → 尝试 ?token= 查询参数
        token = (request.query_params.get("token") or "").strip()
        user_id = verify_token(token) if token else ""
    if not user_id:
        return None
    return get_auth_tool().get_user(user_id)


@app.post("/api/auth/send-code", include_in_schema=False)
async def auth_send_code(request: Request):
    tool = get_auth_tool()
    if not tool.allow(_client_ip(request)):
        return {"ok": False, "message": "请求太频繁，请稍后再试"}
    body = await request.json()
    phone = str(body.get("phone", "")).strip()
    if len(phone) != 11 or not phone.isdigit():
        return {"ok": False, "message": "手机号格式不正确"}
    code = tool.send_code(phone)
    # 演示模式：验证码直接返回（生产环境接短信服务商，绝不回传）
    return {"ok": True, "demo_code": code, "message": "验证码已发送（演示模式）"}


@app.post("/api/auth/phone-login", include_in_schema=False)
async def auth_phone_login(request: Request):
    tool = get_auth_tool()
    if not tool.allow(_client_ip(request)):
        return {"ok": False, "message": "请求太频繁，请稍后再试"}
    body = await request.json()
    user, err = tool.phone_login(str(body.get("phone", "")).strip(), str(body.get("code", "")).strip())
    if err:
        return {"ok": False, "message": err}
    from utils.auth_utils import issue_token
    return {"ok": True, "token": issue_token(user["user_id"]), "user": user}


@app.post("/api/auth/guest", include_in_schema=False)
async def auth_guest(request: Request):
    tool = get_auth_tool()
    if not tool.allow(_client_ip(request)):
        return {"ok": False, "message": "请求太频繁，请稍后再试"}
    user, err = tool.guest()
    if err:
        return {"ok": False, "message": err}
    from utils.auth_utils import issue_token
    return {"ok": True, "token": issue_token(user["user_id"]), "user": user}


@app.post("/api/auth/third", include_in_schema=False)
async def auth_third(request: Request):
    tool = get_auth_tool()
    if not tool.allow(_client_ip(request)):
        return {"ok": False, "message": "请求太频繁，请稍后再试"}
    body = await request.json()
    provider = str(body.get("provider", "wx"))
    if provider not in ("wx", "ali"):
        return {"ok": False, "message": "不支持的登录方式"}
    user, err = tool.third_login(provider, str(body.get("nickname", "")) or ("微信用户" if provider == "wx" else "支付宝用户"))
    if err:
        return {"ok": False, "message": err}
    from utils.auth_utils import issue_token
    return {"ok": True, "token": issue_token(user["user_id"]), "user": user}


@app.get("/api/auth/me", include_in_schema=False)
async def auth_me(request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    return {"ok": True, "user": user}


# ============ 团队权限管理（管理员专属） ============
class _RoleUpdate(BaseModel):
    role: str = Field(..., description="目标角色：admin / editor / viewer")


class _TenantUpdate(BaseModel):
    tenant_id: str = Field(..., description="目标租户ID；'default' 表示不隔离")


def _require_admin(request: Request):
    """校验当前用户是否为管理员，不是则返回 (None, 错误响应 dict)"""
    user = get_current_user(request)
    if not user:
        return None, {"ok": False, "code": 401, "message": "未登录或登录已过期"}
    if not has_permission(user, "admin"):
        return None, {"ok": False, "code": 403, "message": "仅管理员可操作"}
    return user, None


@app.get("/api/admin/users", include_in_schema=False)
async def admin_list_users(request: Request):
    """列出全部用户（管理员专属）"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        users = []
        for doc in get_auth_tool().users.find({}):
            users.append({
                "user_id": str(doc.get("_id")),
                "phone": doc.get("phone", ""),
                "nickname": doc.get("nickname", "用户"),
                "role": doc.get("role", "admin"),
                "tenant_id": doc.get("tenant_id", "default"),
                "created_at": doc.get("created_at", ""),
            })
        return {"ok": True, "users": users}
    except Exception as e:
        logger.warning(f"[/api/admin/users] 列举失败: {e}")
        return {"ok": False, "code": 500, "message": f"列举失败: {e}"}


@app.post("/api/admin/users/{user_id}/role", include_in_schema=False)
async def admin_set_role(user_id: str, body: _RoleUpdate, request: Request):
    """修改指定用户角色（管理员专属）"""
    admin_user, err = _require_admin(request)
    if err:
        return err
    role = str(body.role or "").lower()
    if role not in ("admin", "editor", "viewer"):
        return {"ok": False, "code": 400, "message": "角色必须是 admin / editor / viewer"}
    try:
        from utils.audit_utils import record_audit, ACTION_ROLE_CHANGE
        _oid = __import__("bson").ObjectId(user_id)
        old = get_auth_tool().users.find_one({"_id": _oid}, {"role": 1})
        old_role = (old or {}).get("role", "admin") if old else None
        res = get_auth_tool().users.update_one(
            {"_id": _oid},
            {"$set": {"role": role}},
        )
        if res.matched_count == 0:
            return {"ok": False, "code": 404, "message": "用户不存在"}
        record_audit(ACTION_ROLE_CHANGE, actor_id=admin_user.get("user_id"),
                     target_id=user_id, detail={"from": old_role, "to": role},
                     tenant_id=admin_user.get("tenant_id"))
        return {"ok": True, "message": f"已将用户角色设为 {role}"}
    except Exception as e:
        logger.warning(f"[/api/admin/users/role] 更新失败: {e}")
        return {"ok": False, "code": 500, "message": f"更新失败: {e}"}


@app.post("/api/admin/users/{user_id}/tenant", include_in_schema=False)
async def admin_set_tenant(user_id: str, body: _TenantUpdate, request: Request):
    """修改指定用户所属租户（管理员专属）"""
    admin_user, err = _require_admin(request)
    if err:
        return err
    tenant_id = str(body.tenant_id or "default").strip() or "default"
    try:
        from utils.audit_utils import record_audit, ACTION_TENANT_CHANGE
        _oid = __import__("bson").ObjectId(user_id)
        old = get_auth_tool().users.find_one({"_id": _oid}, {"tenant_id": 1})
        old_tenant = (old or {}).get("tenant_id", "default") if old else None
        res = get_auth_tool().users.update_one(
            {"_id": _oid},
            {"$set": {"tenant_id": tenant_id}},
        )
        if res.matched_count == 0:
            return {"ok": False, "code": 404, "message": "用户不存在"}
        record_audit(ACTION_TENANT_CHANGE, actor_id=admin_user.get("user_id"),
                     target_id=user_id, detail={"from": old_tenant, "to": tenant_id},
                     tenant_id=tenant_id)
        return {"ok": True, "message": f"已将用户租户设为 {tenant_id}"}
    except Exception as e:
        logger.warning(f"[/api/admin/users/tenant] 更新失败: {e}")
        return {"ok": False, "code": 500, "message": f"更新失败: {e}"}


@app.get("/members.html", include_in_schema=False)
async def members_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "members.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="members.html 不存在")
    return FileResponse(html_path)


@app.get("/api/admin/audit", include_in_schema=False)
async def admin_audit_list(request: Request, limit: int = 100):
    """审计日志列表（管理员专属）"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        col = get_auth_tool().db["kb_audit_logs"]
        rows = list(col.find({}).sort("ts", -1).limit(max(1, min(500, int(limit)))))
        logs = [{
            "ts": r.get("ts", ""),
            "action": r.get("action", ""),
            "actor_id": str(r.get("actor_id") or ""),
            "target_id": str(r.get("target_id") or ""),
            "detail": r.get("detail") or {},
            "tenant_id": r.get("tenant_id", "default"),
        } for r in rows]
        return {"ok": True, "logs": logs}
    except Exception as e:
        logger.warning(f"[/api/admin/audit] 列举失败: {e}")
        return {"ok": False, "code": 500, "message": f"列举失败: {e}"}


@app.get("/audit.html", include_in_schema=False)
async def audit_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "audit.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="audit.html 不存在")
    return FileResponse(html_path)


@app.get("/api/admin/metrics", include_in_schema=False)
async def admin_metrics(request: Request):
    """系统监控指标（管理员专属）：Mongo 集合数 / Milvus 实体数 / MinIO 对象数 / 运行时长"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        from utils.metrics_utils import collect_metrics
        return {"ok": True, "metrics": collect_metrics()}
    except Exception as e:
        logger.warning(f"[/api/admin/metrics] 采集失败: {e}")
        return {"ok": False, "code": 500, "message": f"采集指标失败: {e}"}


@app.get("/monitor.html", include_in_schema=False)
async def monitor_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "monitor.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="monitor.html 不存在")
    return FileResponse(html_path)


# ============ 备份与恢复（管理员专属） ============
class _BackupBody(BaseModel):
    label: str = ""          # 备份标签（backup 时）/ 备份目录名或路径（restore 时）
    dir: str = ""            # 恢复目标目录（与 label 二选一；dir 优先）
    components: list = ["mongo", "milvus", "minio"]


@app.post("/api/admin/backup", include_in_schema=False)
async def admin_backup(request: Request, body: _BackupBody = Body(default=_BackupBody())):
    """触发一次全量备份（Mongo / Milvus / MinIO 落盘到 ./backups/<时间戳>_<标签>/）"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        from utils.backup_utils import run_backup
        res = run_backup(label=str(body.label or ""))
        return {"ok": True, **res}
    except Exception as e:
        logger.warning(f"[/api/admin/backup] 失败: {e}")
        return {"ok": False, "code": 500, "message": f"备份失败: {e}"}


@app.get("/api/admin/backups", include_in_schema=False)
async def admin_list_backups(request: Request):
    """列举已有备份目录与概要"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        from utils.backup_utils import list_backups
        return {"ok": True, "backups": list_backups()}
    except Exception as e:
        logger.warning(f"[/api/admin/backups] 失败: {e}")
        return {"ok": False, "code": 500, "message": f"列举备份失败: {e}"}


@app.post("/api/admin/restore", include_in_schema=False)
async def admin_restore(request: Request, body: _BackupBody = Body(default=_BackupBody())):
    """从指定备份目录恢复（破坏性：Milvus 目标集合会被先清空再插入）"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        from utils.backup_utils import restore_from
        dir_arg = str(body.dir or body.label or "")
        if not dir_arg:
            return {"ok": False, "code": 400, "message": "请提供恢复目标目录（dir 或 label）"}
        res = restore_from(dir_arg, components=body.components or ["mongo", "milvus", "minio"])
        return {"ok": True, **res}
    except Exception as e:
        logger.warning(f"[/api/admin/restore] 失败: {e}")
        return {"ok": False, "code": 500, "message": f"恢复失败: {e}"}


@app.get("/backup.html", include_in_schema=False)
async def backup_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "backup.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="backup.html 不存在")
    return FileResponse(html_path)


# ============ 定时自动备份（后台线程调度；状态持久化到 Mongo kb_config） ============
import threading

_auto_backup_state = {
    "enabled": False,
    "interval_hours": 24,
    "last_run": None,
    "next_run": None,
    "_thread": None,
}


def _auto_backup_tick():
    """后台线程：按 interval_hours 周期触发全量备份（失败开放，异常不退出循环）"""
    while True:
        try:
            time.sleep(60)
            st = _auto_backup_state
            if not st["enabled"]:
                st["next_run"] = None
                continue
            now = time.time()
            if not st.get("next_run"):
                st["next_run"] = now + st["interval_hours"] * 3600
            if now >= st["next_run"]:
                try:
                    from utils.backup_utils import run_backup
                    run_backup(label="auto")
                    st["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
                except Exception as e:
                    logger.warning(f"[自动备份] 执行失败: {e}")
                st["next_run"] = time.time() + st["interval_hours"] * 3600
        except Exception:
            time.sleep(60)


def _auto_backup_ensure_started():
    """恢复持久化状态并启动后台线程（仅启动一次）"""
    try:
        col = get_auth_tool().db["kb_config"]
        doc = col.find_one({"key": "auto_backup"})
        if doc:
            _auto_backup_state["enabled"] = bool(doc.get("enabled", False))
            _auto_backup_state["interval_hours"] = max(1, int(doc.get("interval_hours", 24)))
            _auto_backup_state["last_run"] = doc.get("last_run")
    except Exception as e:
        logger.warning(f"[自动备份] 恢复状态失败（使用默认）: {e}")
    if _auto_backup_state["_thread"] is None:
        t = threading.Thread(target=_auto_backup_tick, daemon=True)
        t.start()
        _auto_backup_state["_thread"] = t


class _AutoBackupBody(BaseModel):
    enabled: bool = False
    interval_hours: int = 24


@app.get("/api/admin/backup/auto", include_in_schema=False)
async def admin_backup_auto_get(request: Request):
    """读取定时自动备份配置"""
    _, err = _require_admin(request)
    if err:
        return err
    st = _auto_backup_state
    return {"ok": True, "state": {k: v for k, v in st.items() if k != "_thread"}}


@app.post("/api/admin/backup/auto", include_in_schema=False)
async def admin_backup_auto_set(request: Request, body: _AutoBackupBody = Body(default=_AutoBackupBody())):
    """开启/关闭定时自动备份，并设定间隔小时数（管理员专属）"""
    _, err = _require_admin(request)
    if err:
        return err
    _auto_backup_state["enabled"] = bool(body.enabled)
    _auto_backup_state["interval_hours"] = max(1, int(body.interval_hours))
    if _auto_backup_state["enabled"]:
        _auto_backup_state["next_run"] = time.time() + _auto_backup_state["interval_hours"] * 3600
    else:
        _auto_backup_state["next_run"] = None
    # 持久化，重启后自动恢复
    try:
        col = get_auth_tool().db["kb_config"]
        col.update_one(
            {"key": "auto_backup"},
            {"$set": {"enabled": _auto_backup_state["enabled"], "interval_hours": _auto_backup_state["interval_hours"]}},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"[自动备份] 持久化失败: {e}")
    return {"ok": True, "state": {k: v for k, v in _auto_backup_state.items() if k != "_thread"}}


# 应用启动时拉起自动备份调度线程（失败开放：Mongo 暂不可达也不影响启动）
try:
    _auto_backup_ensure_started()
except Exception as _e:
    logger.warning(f"[自动备份] 调度线程启动失败（忽略）: {_e}")



# ============ 会话历史侧栏（/api/sessions/*） ============
from pymongo import ASCENDING as _ASC

_sessions_indexed = False


def _sessions_col():
    """会话列表集合（chat_sessions）：session_id 唯一，按 user_id 归属"""
    return get_auth_tool().db["chat_sessions"]


@app.post("/api/sessions", include_in_schema=False)
async def sessions_upsert(request: Request):
    """前端发起提问时上报会话：不存在则创建（标题=首问），存在则刷新更新时间"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    body = await request.json()
    session_id = str(body.get("session_id", "")).strip()
    title = str(body.get("title", "")).strip()[:40] or "新会话"
    if not session_id:
        return {"ok": False, "message": "session_id 不能为空"}
    global _sessions_indexed
    if not _sessions_indexed:
        col = _sessions_col()
        col.create_index([("session_id", _ASC)], unique=True)
        col.create_index([("user_id", _ASC), ("updated_at", _ASC)])
        _sessions_indexed = True
    # 过滤条件带 user_id：既是归属校验，upsert 插入时也会带上 user_id
    _sessions_col().update_one(
        {"session_id": session_id, "user_id": user["user_id"]},
        {"$set": {"title": title, "updated_at": time.time()},
         "$setOnInsert": {"created_at": time.time()}},
        upsert=True
    )
    return {"ok": True}


@app.get("/api/sessions", include_in_schema=False)
async def sessions_list(request: Request):
    """列出当前用户的会话（按更新时间倒序，最多 50 条）"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    docs = _sessions_col().find({"user_id": user["user_id"]}).sort("updated_at", -1).limit(50)
    items = [{"session_id": d["session_id"], "title": d.get("title", "新会话"),
              "updated_at": d.get("updated_at", 0)} for d in docs]
    return {"ok": True, "sessions": items}


@app.delete("/api/sessions/{session_id}", include_in_schema=False)
async def sessions_delete(session_id: str, request: Request):
    """删除会话：会话记录 + 全部聊天消息一起清理"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    res = _sessions_col().delete_one({"session_id": session_id, "user_id": user["user_id"]})
    deleted = 0
    if res.deleted_count:
        from utils.mongo_history_utils import clear_history
        deleted = clear_history(session_id)
    return {"ok": True, "removed": res.deleted_count, "messages": deleted}


# ============ RAG 质量评测（/api/eval/*，轻量 Ragas） ============

@app.post("/api/eval/record", include_in_schema=False)
async def eval_record(request: Request):
    """前端问答完成后上报：LLM 双指标打分（忠实度/相关度）并落库"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    body = await request.json()
    question = str(body.get("question", "")).strip()
    answer = str(body.get("answer", "")).strip()
    sources = body.get("sources") or []
    if not question or not answer:
        return {"ok": False, "message": "question/answer 不能为空"}
    from utils.eval_utils import eval_qa, save_eval
    scores = eval_qa(question, answer, sources)
    record_id = save_eval(user["user_id"], str(body.get("session_id", "")), question, answer, sources, scores)
    return {"ok": True, "record_id": record_id, "scores": scores}


@app.get("/api/eval/list", include_in_schema=False)
async def eval_list(request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.eval_utils import list_evals
    return {"ok": True, "records": list_evals(user["user_id"])}


@app.get("/api/eval/stats", include_in_schema=False)
async def eval_stats_api(request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.eval_utils import eval_stats
    return {"ok": True, "stats": eval_stats(user["user_id"])}


# ============ 可观测（/api/trace/*，LLM 调用追踪） ============

@app.get("/api/trace/stats", include_in_schema=False)
async def trace_stats_api(request: Request):
    """LLM 调用聚合统计：总次数 / 成功率 / 平均耗时（运维视角）"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.security_utils import trace_stats
    return {"ok": True, "stats": trace_stats()}


# ============ 对比问答（文档对比 / 参数 A/B） ============

def _doc_retrieve(question: str, file_title: str, top_k: int = 4) -> list:
    """单文档定向检索：file_title 过滤 + 混合检索，返回 [{content,...}]"""
    from utils.embedding_utils import generate_embeddings
    from utils.milvus_utils import create_hybrid_search_requests, get_milvus_client, hybrid_search, escape_milvus_string
    from config.milvus_config import milvus_config
    embs = generate_embeddings([question])
    reqs = create_hybrid_search_requests(
        dense_vector=embs["dense"][0], sparse_vector=embs["sparse"][0],
        expr=f'file_title == "{escape_milvus_string(file_title)}"', limit=max(top_k * 2, 6)
    )
    client = get_milvus_client()
    res = hybrid_search(client=client, collection_name=milvus_config.chunks_collection,
                        reqs=reqs, ranker_weights=(0.8, 0.2),
                        output_fields=["chunk_id", "content"])
    hits = res[0] if res else []
    docs = []
    for h in hits:
        if isinstance(h, dict):
            e = h.get("entity") or h
        else:
            try:
                e = h["entity"]
            except Exception:
                e = None
        if isinstance(e, dict) and isinstance(e.get("entity"), dict):
            e = e["entity"]
        content = (e.get("content") or "").strip() if isinstance(e, dict) else ""
        if content:
            docs.append({"content": content})
    return docs[:top_k]


def _doc_answer(question: str, docs: list) -> str:
    """基于给定片段直接生成回答（走主 LLM）"""
    from utils.llm_utils import get_llm_client
    context = "\n\n".join(f"[{i+1}] {d['content'][:800]}" for i, d in enumerate(docs)) or "（无参考内容）"
    prompt = (f"请仅根据以下参考内容回答问题；若参考内容不足以回答，请明确说明\"参考内容中未提及\"。\n\n"
              f"【参考内容】\n{context}\n\n【问题】{question}")
    return get_llm_client().invoke(prompt).content


class CompareBody(BaseModel):
    question: str
    doc_a: str
    doc_b: str
    top_k: int = 4


@app.post("/api/compare/docs", include_in_schema=False)
async def compare_docs(body: CompareBody, request: Request):
    """文档对比问答：同一问题分别基于两个文档独立检索+回答"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    q = (body.question or "").strip()
    if not q or not body.doc_a or not body.doc_b:
        raise HTTPException(status_code=400, detail="question/doc_a/doc_b 不能为空")
    result = {}
    for tag, title in (("a", body.doc_a), ("b", body.doc_b)):
        try:
            docs = _doc_retrieve(q, title, body.top_k)
            answer = _doc_answer(q, docs) if docs else "该文档中未检索到相关内容。"
            result[tag] = {"doc": title, "answer": answer, "chunks": len(docs)}
        except Exception as e:
            result[tag] = {"doc": title, "answer": f"处理失败：{e}", "chunks": 0}
    return {"ok": True, "question": q, "a": result["a"], "b": result["b"]}


class ABBody(BaseModel):
    question: str
    params_a: dict = {}
    params_b: dict = {}


@app.post("/api/compare/params", include_in_schema=False)
async def compare_params(body: ABBody, request: Request):
    """参数 A/B 对比：同一问题用两组检索参数各跑一次完整 RAG（同步）"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="question 不能为空")
    import uuid as _uuid
    from web.api.query_service import run_query_graph
    from utils.task_utils import get_task_result
    result = {}
    for tag, params in (("a", body.params_a or {}), ("b", body.params_b or {})):
        sid = f"ab-{_uuid.uuid4().hex[:8]}"
        try:
            run_query_graph(sid, q, is_stream=False, search_params=params or None)
            result[tag] = {
                "params": params or {},
                "answer": get_task_result(sid, "answer", ""),
                "sources": json.loads(get_task_result(sid, "sources", "[]") or "[]"),
            }
        except Exception as e:
            result[tag] = {"params": params or {}, "answer": f"处理失败：{e}", "sources": []}
    return {"ok": True, "question": q, "a": result["a"], "b": result["b"]}

@app.post("/api/cache/clear", include_in_schema=False)
async def cache_clear(request: Request):
    """清空语义缓存（缓存命中答案将重新检索生成）"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.cache_utils import clear_cache
    return {"ok": True, "deleted": clear_cache()}

@app.get("/api/stats/hot-questions", include_in_schema=False)
async def stats_hot_questions(request: Request):
    """全站热门问题 TOP10（Mongo 聚合 chat_message 中用户提问）"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.auth_utils import get_auth_tool
    col = get_auth_tool().db["chat_message"]
    pipeline = [
        {"$match": {"role": "user", "text": {"$exists": True, "$ne": ""}}},
        {"$group": {"_id": "$text", "count": {"$sum": 1}, "last_ts": {"$max": "$ts"}}},
        {"$sort": {"count": -1, "last_ts": -1}},
        {"$limit": 10},
    ]
    items = [{"question": d["_id"], "count": d["count"], "last_ts": d.get("last_ts", 0)}
             for d in col.aggregate(pipeline)]
    return {"ok": True, "items": items}


@app.get("/api/stats/gaps", include_in_schema=False)
async def stats_gaps(request: Request):
    """知识缺口：当前用户检索无来源（sources_count=0）的问答记录 → 该补资料了"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.auth_utils import get_auth_tool
    col = get_auth_tool().db["qa_evals"]
    docs = col.find({"user_id": user["user_id"], "sources_count": 0}) \
              .sort("ts", -1).limit(20)
    items = [{"question": d.get("question", ""), "ts": d.get("ts", 0),
              "answer_head": (d.get("answer", "") or "")[:80]} for d in docs]
    return {"ok": True, "items": items, "total": col.count_documents(
        {"user_id": user["user_id"], "sources_count": 0})}


@app.get("/api/stats/usage", include_in_schema=False)
async def stats_usage(request: Request):
    """用量统计：按天聚合 LLM 调用（次数/错误/平均耗时），近14天 + 明细汇总"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.auth_utils import get_auth_tool
    from datetime import datetime, timedelta
    col = get_auth_tool().db["llm_traces"]
    since = time.time() - 14 * 86400
    docs = list(col.find({"ts": {"$gte": since}}, {"ts": 1, "ok": 1, "duration_ms": 1, "kind": 1}))
    days = {}
    for d in docs:
        day = datetime.fromtimestamp(d["ts"]).strftime("%m-%d")
        rec = days.setdefault(day, {"total": 0, "errors": 0, "sum_ms": 0.0})
        rec["total"] += 1
        if not d.get("ok", True):
            rec["errors"] += 1
        rec["sum_ms"] += d.get("duration_ms", 0) or 0
    daily = [{"day": k, "total": v["total"], "errors": v["errors"],
              "avg_ms": round(v["sum_ms"] / v["total"], 1) if v["total"] else 0}
             for k, v in sorted(days.items())]
    total = len(docs)
    errors = sum(1 for d in docs if not d.get("ok", True))
    kinds = {}
    for d in docs:
        kinds[d.get("kind", "other")] = kinds.get(d.get("kind", "other"), 0) + 1
    return {"ok": True, "daily": daily, "total": total, "errors": errors, "kinds": kinds}


# ============ 知识图谱（LLM 三元组抽取 + ECharts 渲染） ============

@app.post("/api/graph/build", include_in_schema=False)
async def graph_build(request: Request):
    """对指定文档构建知识图谱（LLM 抽三元组，约 20~60 秒）"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    body = await request.json()
    file_title = str(body.get("file_title", "")).strip()
    if not file_title:
        raise HTTPException(status_code=400, detail="file_title 不能为空")
    from utils.graph_utils import build_graph_for_doc
    result = build_graph_for_doc(user["user_id"], file_title)
    return result


@app.get("/api/graph/get", include_in_schema=False)
async def graph_get(request: Request, file_title: str = ""):
    """读取图谱（nodes/edges），不传 file_title 则合并该用户最近构建的图谱"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.graph_utils import get_graph
    return {"ok": True, "graph": get_graph(user["user_id"], file_title or None)}


# ============ 知识巡检（预设问题集批量体检） ============

@app.post("/api/inspection/run", include_in_schema=False)
async def inspection_run(request: Request):
    """手动触发一轮知识巡检（每题检索+回答+评分，约 1~2 分钟）"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.inspection_utils import run_inspection
    try:
        result = run_inspection(user["user_id"])
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "message": f"巡检失败: {e}"}


@app.get("/api/inspection/list", include_in_schema=False)
async def inspection_list(request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.inspection_utils import list_inspections
    return {"ok": True, "records": list_inspections(user["user_id"])}


# ============ 多知识库空间 + 邀请协作 ============

@app.post("/api/spaces", include_in_schema=False)
async def spaces_create(request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    body = await request.json()
    from utils.spaces_utils import create_space
    return create_space(user["user_id"], str(body.get("name", "")))


@app.get("/api/spaces", include_in_schema=False)
async def spaces_list(request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.spaces_utils import list_spaces
    return {"ok": True, "spaces": list_spaces(user["user_id"])}


@app.delete("/api/spaces/{space_id}", include_in_schema=False)
async def spaces_delete(space_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.spaces_utils import delete_space
    return delete_space(user["user_id"], space_id)


@app.post("/api/spaces/{space_id}/docs", include_in_schema=False)
async def spaces_add_doc(space_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    body = await request.json()
    from utils.spaces_utils import add_doc
    return add_doc(user["user_id"], space_id, str(body.get("file_title", "")))


@app.delete("/api/spaces/{space_id}/docs", include_in_schema=False)
async def spaces_remove_doc(space_id: str, request: Request, file_title: str = ""):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.spaces_utils import remove_doc
    return remove_doc(user["user_id"], space_id, file_title)


@app.post("/api/spaces/{space_id}/invite", include_in_schema=False)
async def spaces_invite(space_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.spaces_utils import get_invite_code
    return get_invite_code(user["user_id"], space_id)


@app.post("/api/spaces/join", include_in_schema=False)
async def spaces_join(request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    body = await request.json()
    from utils.spaces_utils import join_space
    return join_space(user["user_id"], str(body.get("code", "")))


# ============ API 开放（/v1/chat + api_key，复用建号时自动生成的 zk- Key） ============
import secrets as _secrets


@app.get("/api/me/api-key", include_in_schema=False)
async def get_api_key(request: Request):
    """查看当前用户的 API Key（注册时已自动生成）"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    return {"ok": True, "api_key": user.get("api_key") or ""}


@app.post("/api/me/api-key", include_in_schema=False)
async def rotate_api_key(request: Request):
    """轮换 API Key（每次调用都换新，旧的立即失效）"""
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    key = "zk-" + _secrets.token_hex(12)
    from bson import ObjectId
    get_auth_tool().users.update_one(
        {"_id": ObjectId(user["user_id"])},
        {"$set": {"api_key": key, "api_key_created_at": time.time()}}
    )
    return {"ok": True, "api_key": key, "message": "已生成新 Key，旧 Key 已失效"}


class V1ChatBody(BaseModel):
    api_key: str
    query: str
    session_id: str = ""


@app.post("/v1/chat", include_in_schema=False)
async def v1_chat(body: V1ChatBody, background_tasks: BackgroundTasks):
    """
    开放 API：第三方用 api_key 调用知识库问答（同步返回，无流式）。
    请求：{"api_key": "sk-kb-xxx", "query": "问题", "session_id": "可选"}
    响应：{"ok": true, "answer": "...", "session_id": "..."}
    """
    body.api_key = (body.api_key or "").strip()
    body.query = (body.query or "").strip()
    if not body.api_key or not body.query:
        raise HTTPException(status_code=400, detail="api_key 和 query 不能为空")
    # [开放 API 限流] 按 api_key 维度滑动窗口；超限直接 429（失败开放由工具内部兜底）
    from utils.ratelimit_utils import api_check_limit, API_RATE_LIMIT_MAX, API_RATE_LIMIT_WINDOW
    _allowed, _retry = api_check_limit(body.api_key)
    if not _allowed:
        raise HTTPException(
            status_code=429,
            detail=f"API 调用过于频繁，请 {_retry} 秒后再试（限流 {API_RATE_LIMIT_MAX}/{API_RATE_LIMIT_WINDOW}s）",
            headers={"Retry-After": str(_retry)},
        )
    user = get_auth_tool().users.find_one({"api_key": body.api_key})
    if not user:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    session_id = body.session_id.strip() or "api-" + _secrets.token_hex(8)
    # 复用与页面问答完全相同的图谱链路（含注入拦截 / 历史上下文 / 评测）
    from web.api.query_service import run_query_graph
    from utils.task_utils import get_task_result
    run_query_graph(session_id, body.query, is_stream=False)
    answer = get_task_result(session_id, "answer", "")
    return {"ok": True, "session_id": session_id, "answer": answer}


class BotWebhookBody(BaseModel):
    """群机器人通用消息体"""
    msg: str
    key: str = ""          # 用户的 api_key（zk- 开头），也支持 ?key= 查询参数
    session_id: str = ""


@app.post("/v1/bot/webhook", include_in_schema=False)
async def bot_webhook(body: BotWebhookBody, request: Request):
    """
    群机器人接入端点（企业微信/钉钉机器人中转服务调用此端点）。
    用法：
      curl -X POST http://localhost:8000/v1/bot/webhook?key=<api_key> \\
           -H "Content-Type: application/json" -d '{"msg": "HAK180怎么设置温度"}'
    返回：{"reply": "答案文本"} —— 中转服务拿到 reply 后按企微/钉钉机器人格式发回群里。
    企微机器人对接：自建一个 10 行的中转 Flask/函数计算，收企微 POST → 调本端点 → POST 企业微信
    webhook 地址（https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=机器人key），body 为
    {"msgtype":"text","text":{"content": reply}}。钉钉同理（加 sign 验签）。
    """
    key = (body.key or "").strip() or request.query_params.get("key", "")
    msg = (body.msg or "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="缺少 key（api_key）")
    if not msg:
        raise HTTPException(status_code=400, detail="msg 不能为空")
    # [开放 API 限流] 按 key（api_key）维度滑动窗口；超限直接 429
    from utils.ratelimit_utils import api_check_limit, API_RATE_LIMIT_MAX, API_RATE_LIMIT_WINDOW
    _allowed, _retry = api_check_limit(key)
    if not _allowed:
        raise HTTPException(
            status_code=429,
            detail=f"API 调用过于频繁，请 {_retry} 秒后再试（限流 {API_RATE_LIMIT_MAX}/{API_RATE_LIMIT_WINDOW}s）",
            headers={"Retry-After": str(_retry)},
        )
    user = get_auth_tool().users.find_one({"api_key": key})
    if not user:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    session_id = body.session_id.strip() or f"bot-{user['phone'][:12]}-{_secrets.token_hex(4)}"
    from web.api.query_service import run_query_graph
    from utils.task_utils import get_task_result
    run_query_graph(session_id, msg, is_stream=False)
    answer = get_task_result(session_id, "answer", "")
    return {"reply": answer or "抱歉，本次未能生成回答。"}


# ============ 文档管理（/api/docs/* + /docs.html） ============
from tool.logger import logger

@app.get("/docs.html", include_in_schema=False)
async def docs_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "docs.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在：{html_path}")
    return FileResponse(html_path)


@app.get("/eval.html", include_in_schema=False)
async def eval_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "eval.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在：{html_path}")
    return FileResponse(html_path)


@app.get("/data.html", include_in_schema=False)
async def data_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "data.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在：{html_path}")
    return FileResponse(html_path)


@app.get("/compare.html", include_in_schema=False)
async def compare_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "compare.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在：{html_path}")
    return FileResponse(html_path)


@app.get("/graph.html", include_in_schema=False)
async def graph_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "graph.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在：{html_path}")
    return FileResponse(html_path)


@app.get("/spaces.html", include_in_schema=False)
async def spaces_page():
    html_path = Path(__file__).absolute().parent.parent / "page" / "spaces.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在：{html_path}")
    return FileResponse(html_path)


@app.get("/api/docs/list", include_in_schema=False)
async def docs_list(request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.kb_docs_utils import get_kb_docs_tool
    return {"ok": True, "documents": get_kb_docs_tool().list_by_user(user["user_id"])}


@app.delete("/api/docs/{task_id}", include_in_schema=False)
async def docs_delete(task_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    from utils.kb_docs_utils import get_kb_docs_tool
    tool = get_kb_docs_tool()
    doc = tool.get_by_task(task_id)
    if not doc:
        return {"ok": False, "message": "文档不存在"}
    if doc.get("user_id") != user["user_id"]:
        return {"ok": False, "message": "无权删除他人文档"}

    file_name = doc.get("file_name", "")
    deleted = {"mongo": False, "milvus": False, "minio": False}

    # 1) Milvus 向量删除（按 file_title）
    try:
        from utils.milvus_utils import get_milvus_client, escape_milvus_string
        from config.milvus_config import milvus_config
        get_milvus_client().delete(
            collection_name=milvus_config.chunks_collection,
            filter=f"file_title=='{escape_milvus_string(file_name)}'")
        deleted["milvus"] = True
    except Exception as e:
        logger.warning(f"Milvus 向量删除失败（{file_name}）：{e}")

    # 2) MinIO 原文件删除
    try:
        from utils.minio_utils import get_minio_client
        from config.minio_config import minio_config
        if doc.get("minio_object"):
            get_minio_client().remove_object(doc.get("minio_bucket") or minio_config.bucket_name,
                                             doc["minio_object"])
            deleted["minio"] = True
    except Exception as e:
        logger.warning(f"MinIO 文件删除失败（{file_name}）：{e}")

    # 3) Mongo 记录删除
    try:
        tool.docs.delete_one({"task_id": task_id})
        deleted["mongo"] = True
    except Exception as e:
        logger.warning(f"Mongo 记录删除失败（{file_name}）：{e}")

    # [审计] 记录文档删除（fail-open）
    try:
        from utils.audit_utils import record_audit, ACTION_DOC_DELETE
        record_audit(ACTION_DOC_DELETE, actor_id=user.get("user_id"),
                     target_id=task_id, detail={"file_name": file_name},
                     tenant_id=user.get("tenant_id"))
    except Exception:
        pass

    return {"ok": True, "deleted": deleted,
            "message": f"已删除（向量:{'✓' if deleted['milvus'] else '✗'} 文件:{'✓' if deleted['minio'] else '✗'} 记录:{'✓' if deleted['mongo'] else '✗'}）"}


if __name__ == "__main__":
    # [知识巡检] 如配置了 INSPECTION_HOUR，启动每日定时巡检守护线程
    try:
        from utils.inspection_utils import start_scheduler
        start_scheduler()
    except Exception as _e:
        print(f"巡检定时线程启动失败（不影响服务）: {_e}")
    # 2 秒后自动用默认浏览器打开首页（等 uvicorn 先起来）
    import threading, time, webbrowser
    def _open_browser():
        time.sleep(2)
        webbrowser.open("http://127.0.0.1:8000/login.html")
    threading.Thread(target=_open_browser, daemon=True).start()
    print("汇思智藏一体化服务启动中: http://127.0.0.1:8000/")
    uvicorn.run(app, host="127.0.0.1", port=8000)
