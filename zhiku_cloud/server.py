# -*- coding: utf-8 -*-
"""掌柜智库 · 云端轻量版（掌柜智库界面 + 魔珐星云数字人）

说明：
- 云端沙箱没有 MongoDB / Milvus / BGE 模型，知识库导入与 RAG 问答需在本地跑完整版
- 本服务提供：页面 + 数字人凭证下发 + 诊断上报 + 演示模式登录（内存态，重启即清）
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("zhiku_cloud")

load_dotenv(override=True)

BASE = Path(__file__).absolute().parent
PAGE = BASE / "page"
PAGES = {"login.html", "index.html", "chat.html", "import.html", "dh.html"}

app = FastAPI(title="掌柜智库-云端版")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,   # token 走头，不依赖 Cookie；避免 "*" + credentials 规范冲突
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_pages(request: Request, call_next):
    """HTML 页面禁止缓存：避免重新部署后浏览器仍显示旧版（用户反复误以为没改）"""
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def home():
    return FileResponse(PAGE / "index.html")


@app.get("/login.html", include_in_schema=False)
async def login_page():
    return FileResponse(PAGE / "login.html")


@app.get("/chat.html", include_in_schema=False)
async def chat_page():
    return FileResponse(PAGE / "chat.html")


@app.get("/import.html", include_in_schema=False)
async def import_page():
    return FileResponse(PAGE / "import.html")


@app.get("/docs.html", include_in_schema=False)
async def docs_page():
    return FileResponse(PAGE / "docs.html")


@app.get("/eval.html", include_in_schema=False)
async def eval_page():
    return FileResponse(PAGE / "eval.html")


@app.get("/dh.html", include_in_schema=False)
async def dh_page():
    return FileResponse(PAGE / "dh.html")


@app.get("/data.html", include_in_schema=False)
async def data_page():
    return FileResponse(PAGE / "data.html")


@app.get("/compare.html", include_in_schema=False)
async def compare_page():
    return FileResponse(PAGE / "compare.html")


@app.get("/graph.html", include_in_schema=False)
async def graph_page():
    return FileResponse(PAGE / "graph.html")


@app.get("/spaces.html", include_in_schema=False)
async def spaces_page():
    return FileResponse(PAGE / "spaces.html")


@app.get("/demo.html", include_in_schema=False)
async def demo_page():
    return FileResponse(PAGE / "demo.html")


@app.get("/pricing.html", include_in_schema=False)
async def pricing_page():
    return FileResponse(PAGE / "pricing.html")


@app.get("/security.html", include_in_schema=False)
async def security_page():
    return FileResponse(PAGE / "security.html")


@app.get("/about.html", include_in_schema=False)
async def about_page():
    return FileResponse(PAGE / "about.html")


@app.get("/avatar.js", include_in_schema=False)
async def avatar_js():
    return FileResponse(PAGE / "avatar.js", media_type="application/javascript")


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "# 掌柜智库云端演示站：登录页 / 产品演示页 / 工作台外壳均可被索引\n"
    )
    return Response(body, media_type="text/plain; charset=utf-8")


# ============ 数字人（魔珐星云）——A5：app_secret 不下发浏览器 ============
# 背景：魔珐网关用「签名」鉴权，不是简单的密钥字段。SDK 源码（xmovAvatar@latest.js）里
# headersNeedSign(appId, appSecret, method, url, data) 的做法是：
#   ts   = 秒级时间戳
#   body = 递归按 Unicode 码点排序 key → JSON.stringify → 非 ASCII 转 \uXXXX → 去掉所有空格
#   token= md5( path.toLowerCase() + method.toLowerCase() + body + appSecret + ts )
#   请求头带 X-APP-ID / X-TOKEN / X-TIMESTAMP
# 因此「只往代理里塞一个 X-App-Secret 头」是无效的（网关实测一律返回 签名错误 error_code=4）。
# 正确做法：浏览器把 appSecret 置空、把 gatewayServer 指向本代理；代理在服务端用真密钥
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
    escapeNonAsciiForPythonJson(JSON.stringify(x)).replace(/ /g,'') 等价：
    两者都用 \\b\\t\\n\\f\\r 短转义、都用小写 \\uXXXX、都不加空格。
    """
    return json.dumps(_dh_deepsort(data), ensure_ascii=True, separators=(",", ":"))


def _dh_sign(path_with_query: str, method: str, data, app_secret: str, ts: int) -> str:
    """复刻 SDK 的 headersNeedSign：md5(path + method + body + appSecret + ts)"""
    raw = f"{path_with_query.lower()}{method.lower()}{_dh_normalize_body(data)}{app_secret}{ts}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


@app.get("/api/digital-human/credentials", include_in_schema=False)
async def digital_human_credentials():
    """只回 app_id 与显隐状态，绝不下发 app_secret（前端已改为读 secret_configured）"""
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
@app.api_route("/api/digital-human/proxy", methods=["POST", "DELETE", "OPTIONS"], include_in_schema=False)
async def digital_human_proxy(request: Request):
    if request.method == "OPTIONS":
        # 同源请求通常不会触发预检；留着兜底（自定义头会引发 preflight）
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
        # 透传网关的原始错误体，便于前端日志定位（如鉴权失败 / 并发超限）
        return Response(content=e.read(), status_code=e.code,
                        media_type=e.headers.get("content-type", "application/json"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"数字人网关代理失败: {e}")


@app.post("/api/digital-human/report", include_in_schema=False)
async def digital_human_report(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {"raw": "invalid json"}
    data["_received_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        out = Path(os.getenv("TMPDIR", "/tmp")) / "zhiku_dh_diag.json"
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True}


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok", "edition": "cloud-lite"}


# ============ 演示模式登录（/api/auth/*，内存态：重启即清，与本地版接口形状一致） ============
# 安全红线：绝不兜底「公开/可猜」的默认密钥。未配置时生成一次性随机密钥（重启失效），
# 杜绝「任何人按默认串伪造 token」的致命漏洞；生产请在环境变量固定 AUTH_SECRET。
_auth_secret = os.getenv("AUTH_SECRET")
if not _auth_secret:
    _auth_secret = secrets.token_hex(32)
    logger.warning("AUTH_SECRET 未配置，已生成临时随机密钥（重启失效，仅演示用）；生产请固定配置")
_codes = {}          # phone -> {"code": str, "exp": float}
_users = {}          # user_id -> user dict
_phone_index = {}    # phone -> user_id
_last_req = {}       # ip -> ts（简易限频：3秒内重复请求拒绝）


def _issue_token(user: dict) -> str:
    """无状态 token：用户信息直接编码进 payload（云端可能多实例，内存态不可靠）"""
    exp = int(time.time()) + 30 * 24 * 3600
    raw = "|".join([
        user["user_id"],
        str(user.get("nickname", "")).replace("|", " "),
        str(user.get("phone", "")),
        str(user.get("provider", "")),
        str(exp),
    ])
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    sig = hmac.new(_auth_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def _verify_token(token: str):
    """校验通过则直接从 token 还原用户信息，不依赖内存状态"""
    try:
        payload, sig = (token or "").split(".", 1)
        if hmac.new(_auth_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32] != sig:
            return None
        pad = "=" * (-len(payload) % 4)
        parts = base64.urlsafe_b64decode(payload + pad).decode().split("|")
        if len(parts) != 5 or int(parts[4]) < time.time():
            return None
        return {"user_id": parts[0], "nickname": parts[1], "phone": parts[2],
                "provider": parts[3], "api_key": "", "role": "admin"}
    except Exception:
        return None


def _public(user: dict) -> dict:
    return {k: user.get(k) for k in ("user_id", "phone", "nickname", "provider", "api_key", "created_at", "role")}


def _create_user(phone: str, nickname: str, provider: str) -> dict:
    user = {
        "user_id": secrets.token_hex(12),
        "phone": phone,
        "nickname": nickname,
        "provider": provider,
        "role": "admin",   # 云端演示：所有登录用户均为管理员，便于体验管理后台
        "api_key": "zk-" + secrets.token_hex(12),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _users[user["user_id"]] = user
    _phone_index[phone] = user["user_id"]
    return user


async def _json_body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _throttled(request: Request) -> bool:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    if now - _last_req.get(ip, 0) < 3:
        return True
    _last_req[ip] = now
    return False


@app.post("/api/auth/send-code", include_in_schema=False)
async def auth_send_code(request: Request):
    if _throttled(request):
        return {"ok": False, "message": "请求太频繁，请稍后再试"}
    body = await _json_body(request)
    phone = str(body.get("phone", "")).strip()
    if len(phone) != 11 or not phone.isdigit():
        return {"ok": False, "message": "手机号格式不正确"}
    code = f"{secrets.randbelow(900000) + 100000}"
    _codes[phone] = {"code": code, "exp": time.time() + 300}
    # 演示模式：验证码直接回传（云端无短信服务商）
    return {"ok": True, "demo_code": code, "message": "验证码已发送（演示模式）"}


@app.post("/api/auth/phone-login", include_in_schema=False)
async def auth_phone_login(request: Request):
    body = await _json_body(request)
    phone = str(body.get("phone", "")).strip()
    code = str(body.get("code", "")).strip()
    rec = _codes.get(phone)
    if not rec or rec["exp"] < time.time() or rec["code"] != code:
        return {"ok": False, "message": "验证码不正确或已过期"}
    _codes.pop(phone, None)
    uid = _phone_index.get(phone)
    user = _users[uid] if uid else _create_user(phone, "用户" + phone[-4:], "phone")
    return {"ok": True, "token": _issue_token(user), "user": _public(user)}


@app.post("/api/auth/guest", include_in_schema=False)
async def auth_guest(request: Request):
    user = _create_user("guest-" + secrets.token_hex(5),
                        "游客" + secrets.token_hex(2).upper(), "guest")
    return {"ok": True, "token": _issue_token(user), "user": _public(user)}


@app.post("/api/auth/third", include_in_schema=False)
async def auth_third(request: Request):
    body = await _json_body(request)
    provider = str(body.get("provider", "wx"))
    if provider not in ("wx", "ali"):
        return {"ok": False, "message": "不支持的登录方式"}
    nickname = str(body.get("nickname", "")) or ("微信用户" if provider == "wx" else "支付宝用户")
    user = _create_user(f"{provider}-{secrets.token_hex(6)}", nickname, provider)
    return {"ok": True, "token": _issue_token(user), "user": _public(user)}


@app.get("/api/auth/me", include_in_schema=False)
async def auth_me(request: Request):
    # CDN 边缘节点会改写 Authorization 头（塞入它自己的 JWT），因此依次尝试：
    # 1) Authorization 头 2) X-Auth-Token 头（前端已加发，绕开 CDN） 3) ?token= 查询参数
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    user = _verify_token(token) if token else None
    if not user:
        token = request.headers.get("x-auth-token", "")
        user = _verify_token(token) if token else None
    if not user:
        token = request.query_params.get("token", "")
        user = _verify_token(token) if token else None
    if not user:
        return {"ok": False, "message": "未登录或登录已过期"}
    return {"ok": True, "user": _public(user), "demo": True}


@app.get("/api/auth/debug", include_in_schema=False)
async def auth_debug(request: Request):
    """诊断端点：看请求头到底长什么样（反向代理是否吞 Authorization）"""
    return {"header_keys": list(request.headers.keys()),
            "authorization": request.headers.get("authorization", "")[:40]}


# ============ 管理后台示例接口（云端演示：返回示例数据，无真实后端） ============
# 说明：云端轻量版没有 MongoDB / Milvus / MinIO，以下接口返回「示例数据」，
# 仅用于让同事预览管理后台 UI（系统监控 / 备份恢复 / 团队管理 / 审计日志）。
@app.get("/members.html", include_in_schema=False)
async def members_page():
    return FileResponse(PAGE / "members.html")


@app.get("/audit.html", include_in_schema=False)
async def audit_page():
    return FileResponse(PAGE / "audit.html")


@app.get("/monitor.html", include_in_schema=False)
async def monitor_page():
    return FileResponse(PAGE / "monitor.html")


@app.get("/backup.html", include_in_schema=False)
async def backup_page():
    return FileResponse(PAGE / "backup.html")


@app.get("/api/admin/users", include_in_schema=False)
async def demo_admin_users():
    users = [
        {"user_id": "u_demo_1", "phone": "13800000001", "nickname": "土豆（ owner）", "role": "admin", "tenant_id": "default", "created_at": "2026-09-01 10:22:11"},
        {"user_id": "u_demo_2", "phone": "13800000002", "nickname": "小李", "role": "editor", "tenant_id": "default", "created_at": "2026-09-12 14:03:55"},
        {"user_id": "u_demo_3", "phone": "13800000003", "nickname": "小王", "role": "viewer", "tenant_id": "tenant_a", "created_at": "2026-09-20 09:41:02"},
        {"user_id": "u_demo_4", "phone": "guest-a1b2c", "nickname": "游客AB12", "role": "viewer", "tenant_id": "default", "created_at": "2026-10-01 20:15:30"},
    ]
    return {"ok": True, "demo": True, "users": users}


@app.post("/api/admin/users/{user_id}/role", include_in_schema=False)
async def demo_admin_role(user_id: str):
    return {"ok": True, "demo": True, "message": "示例环境：角色已（模拟）更新"}


@app.post("/api/admin/users/{user_id}/tenant", include_in_schema=False)
async def demo_admin_tenant(user_id: str):
    return {"ok": True, "demo": True, "message": "示例环境：租户已（模拟）更新"}


@app.get("/api/admin/audit", include_in_schema=False)
async def demo_admin_audit():
    logs = [
        {"ts": "2026-10-12 11:02:31", "action": "role_change", "actor_id": "u_demo_1", "target_id": "u_demo_3", "detail": {"from": "viewer", "to": "editor"}, "tenant_id": "default"},
        {"ts": "2026-10-12 10:50:09", "action": "doc_import", "actor_id": "u_demo_1", "target_id": "task_8841", "detail": {"file": "HAK180说明书.pdf"}, "tenant_id": "default"},
        {"ts": "2026-10-11 18:33:47", "action": "tenant_change", "actor_id": "u_demo_1", "target_id": "u_demo_3", "detail": {"from": "default", "to": "tenant_a"}, "tenant_id": "tenant_a"},
        {"ts": "2026-10-11 09:12:20", "action": "doc_delete", "actor_id": "u_demo_2", "target_id": "task_7710", "detail": {"file": "旧版FAQ.docx"}, "tenant_id": "default"},
    ]
    return {"ok": True, "demo": True, "logs": logs}


@app.get("/api/admin/metrics", include_in_schema=False)
async def demo_admin_metrics():
    return {"ok": True, "demo": True, "metrics": {
        "uptime_seconds": 86400 * 3 + 3600 * 7,
        "mongo": {
            "users": 128, "kb_documents": 342, "chat_sessions": 1567,
            "chat_message": 28934, "qa_evals": 1203, "llm_traces": 33120,
            "kb_spaces": 12, "kb_audit_logs": 564, "qa_cache": 842,
            "qa_inspection": 18, "kb_graph": 76,
        },
        "milvus": {"kb_chunks": 58213, "kb_item_names": 1932},
        "minio": {"object_count": 341},
        "errors": [],
    }}


@app.get("/api/admin/backups", include_in_schema=False)
async def demo_admin_backups():
    return {"ok": True, "demo": True, "backups": [
        {"name": "20261012_030000_auto", "created_at": "2026-10-12 03:00:00", "label": "auto",
         "components": {"mongo": {"users": 128, "kb_documents": 342}, "milvus": {"kb_chunks": 58213, "kb_item_names": 1932}, "minio": 341}},
        {"name": "20261011_143000_v1", "created_at": "2026-10-11 14:30:00", "label": "v1",
         "components": {"mongo": {"users": 127, "kb_documents": 338}, "milvus": {"kb_chunks": 57900, "kb_item_names": 1920}, "minio": 338}},
    ]}


@app.post("/api/admin/backup", include_in_schema=False)
async def demo_admin_backup(request: Request):
    return {"ok": True, "demo": True,
            "dir": "backups/demo_" + time.strftime("%Y%m%d_%H%M%S"),
            "label": "demo",
            "message": "示例环境：已（模拟）生成备份",
            "manifest": {"label": "demo"}}


@app.post("/api/admin/restore", include_in_schema=False)
async def demo_admin_restore(request: Request):
    return {"ok": True, "demo": True, "restored": {"mongo": {"users": 128}, "milvus": {"kb_chunks": 58213}, "minio": 341}}


@app.get("/api/admin/backup/auto", include_in_schema=False)
async def demo_admin_backup_auto_get():
    return {"ok": True, "demo": True, "state": {
        "enabled": True, "interval_hours": 24,
        "last_run": "2026-10-12 03:00:00",
        "next_run": time.time() + 3600 * 9,
    }}


@app.post("/api/admin/backup/auto", include_in_schema=False)
async def demo_admin_backup_auto_set(request: Request):
    return {"ok": True, "demo": True, "state": {
        "enabled": True, "interval_hours": 24, "last_run": "—", "next_run": time.time() + 3600 * 24,
    }}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
