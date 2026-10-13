# 掌柜智库 · 部署与运维文档（DEPLOY）

> 适用对象：自托管 / 私有化部署。云端演示站（`zhiku-knowledge-base.app.workbuddy.host`）为公开共享环境，**请勿上传真实或涉密资料**。
> 本文涵盖：架构、依赖、外部服务启停、`.env` 全量配置、启动、四大运维能力（限流 / 监控 / 备份恢复）、安全加固、常见运维命令。

---

## 1. 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│  掌柜智库（FastAPI 单端口 8000，web/api/kb_service.py）        │
│   前端页面（web/page/*.html） + 后端 API（导入/问答/管理）      │
└───────┬───────────────┬────────────────┬───────────────────┘
        │               │                │
   ┌────▼────┐    ┌─────▼──────┐   ┌─────▼──────┐
   │ MongoDB │    │  Milvus    │   │   MinIO    │
   │ 元数据  │    │ 向量库     │   │ 原文件存储 │
   └─────────┘    └────────────┘   └────────────┘
        ▲               ▲                ▲
   users / kb_*    kb_chunks /       pdf_files/*
   qa_* / llm_*    kb_item_names
```

- **进程**：单进程 `uvicorn web.api.kb_service:app --port 8000`，前端为静态 HTML，后端为同一 FastAPI 应用（导入 + 问答 + 管理合并）。
- **外部依赖**：MongoDB（元数据）、Milvus（向量检索）、MinIO（文档原件 / 图片）。
- **本地模型**：BGE-M3 向量化 + 重排（本地推理，见 `config/embedding_config.py`、`config/reranker_config.py`）。
- **LLM**：通过 `OPENAI_API_BASE` / `OPENAI_API_KEY` 接入兼容 OpenAI 协议的模型（如 SiliconFlow / 百炼 / 本地 vLLM）。

---

## 2. 环境要求

| 组件 | 版本建议 |
|------|----------|
| Python | 3.11+（推荐 3.12） |
| MongoDB | 5.0+ |
| Milvus | 2.4.x / 2.5.x（需与 `pymilvus` 版本匹配） |
| MinIO | 最新稳定版（或任意 S3 兼容对象存储） |
| 内存 | ≥ 16GB（BGE-M3 推理 + Milvus 较吃内存） |
| 系统 | Linux / Windows（WSL2 推荐用于生产） |

---

## 3. 安装依赖

```bash
cd rag智能智库
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 或用 uv：
uv sync
```

> 关键 Python 依赖：`fastapi uvicorn pymongo pymilvus minio python-dotenv bson`（`pymilvus` 已锁定 2.5.18，Milvus 服务端请用 2.5.x）。

---

## 4. 启动外部服务

### 4.1 MongoDB（裸跑或容器）
```bash
docker run -d --name zhiku-mongo -p 27017:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=root -e MONGO_INITDB_ROOT_PASSWORD=请改强密码 \
  mongo:7
```

### 4.2 MinIO（文档原件存储，端口 9000 / 控制台 9001）
```bash
docker run -d --name zhiku-minio -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=请改accesskey -e MINIO_ROOT_PASSWORD=请改secretkey \
  minio/minio server /data --console-address ":9001"
```

### 4.3 Milvus（向量库）
使用官方独立版 compose（已含 etcd + 内部 MinIO + Milvus）：
```bash
# 下载官方 standalone compose 后启动
wget https://github.com/milvus-io/milvus/releases/download/v2.5.13/milvus-standalone-docker-compose.yml -O docker-compose.yml
docker compose up -d
```
> ⚠️ Milvus 自带的 MinIO（默认 9001/9090）与 4.2 的**应用 MinIO（9000）相互独立**，请勿混淆：前者只给 Milvus 存向量数据，后者存你的文档原件。

---

## 5. `.env` 全量配置

在项目根目录创建 `.env`（已 gitignore），`load_dotenv(override=True)` 会覆盖同名环境变量。以下是完整清单：

| 变量 | 说明 | 示例 / 默认 |
|------|------|-------------|
| `AUTH_SECRET` | Token 签名密钥，**生产必须固定**（否则重启所有登录失效） | `openssl rand -hex 32` |
| `MONGO_URL` | MongoDB 连接串 | `mongodb://root:密码@127.0.0.1:27017` |
| `MONGO_DB_NAME` | 数据库名 | `zhigu` |
| `MILVUS_URL` | Milvus 地址 | `http://127.0.0.1:19530` |
| `CHUNKS_COLLECTION` | 切片向量集合名 | `kb_chunks` |
| `ITEM_NAME_COLLECTION` | 实体集合名 | `kb_item_names` |
| `MINIO_ENDPOINT` | MinIO 地址（不含 http） | `127.0.0.1:9000` |
| `MINIO_ACCESS_KEY` | MinIO 账号 | `请改accesskey` |
| `MINIO_SECRET_KEY` | MinIO 密码 | `请改secretkey` |
| `MINIO_BUCKET_NAME` | 桶名 | `knowledge-base` |
| `MINIO_IMG_DIR` | 图片目录前缀 | `images` |
| `OPENAI_API_BASE` | LLM 兼容端点 | `https://api.siliconflow.cn/v1` |
| `OPENAI_API_KEY` | LLM 密钥 | `sk-...` |
| `LLM_DEFAULT_MODEL` | 默认对话模型 | `Qwen/Qwen2.5-72B-Instruct` |
| `VL_MODEL` / `ITEM_MODEL` | 视觉 / 实体模型 | 按需 |
| `BGE_M3_PATH` | BGE-M3 本地模型路径 | `/models/bge-m3` |
| `BGE_DEVICE` | 推理设备 | `cuda` / `cpu` |
| `BGE_FP16` | 半精度 | `1` |
| `TEXT_RERANK_MODEL` | 重排模型 | 按需 |
| `DIGITAL_HUMAN_APP_ID` / `DIGITAL_HUMAN_APP_SECRET` | 魔珐星云数字人（可选） | 留空则功能关闭 |
| `DATA_BASED_ROOT_DIR` | 上传文件本地缓存根 | `/data/kb/doc` |
| `INSPECTION_HOUR` | 每日自动巡检小时（0-23，可选） | `3` |
| `INSPECTION_QUESTIONS` | 巡检问题集（可选） | 逗号分隔 |
| `API_RATE_LIMIT_MAX` | **开放 API 限流**：窗口内最大请求数 | `30` |
| `API_RATE_LIMIT_WINDOW` | **开放 API 限流**：窗口秒数 | `60` |
| `BACKUP_DIR` | 备份根目录（可选，默认 `./backups`） | `./backups` |

### 最小可用 `.env` 示例
```dotenv
AUTH_SECRET=替换为随机32位十六进制
MONGO_URL=mongodb://root:强密码@127.0.0.1:27017
MONGO_DB_NAME=zhigu
MILVUS_URL=http://127.0.0.1:19530
CHUNKS_COLLECTION=kb_chunks
ITEM_NAME_COLLECTION=kb_item_names
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_ACCESS_KEY=请改accesskey
MINIO_SECRET_KEY=请改secretkey
MINIO_BUCKET_NAME=knowledge-base
MINIO_IMG_DIR=images
OPENAI_API_BASE=https://api.siliconflow.cn/v1
OPENAI_API_KEY=sk-xxxxxxxx
LLM_DEFAULT_MODEL=Qwen/Qwen2.5-72B-Instruct
BGE_M3_PATH=/models/bge-m3
BGE_DEVICE=cuda
BGE_FP16=1
# 以下为可选 / 新功能
API_RATE_LIMIT_MAX=30
API_RATE_LIMIT_WINDOW=60
```

---

## 6. 启动应用

```bash
# 开发 / PyCharm 调试（热重载）
uvicorn web.api.kb_service:app --host 0.0.0.0 --port 8000 --reload

# 生产（多 worker 时建议 gunicorn + uvicorn worker；注意：本应用含内存态，多 worker 下限流为每进程独立计数）
gunicorn web.api.kb_service:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 -w 1
```

访问：
- 工作台：`http://<服务器IP>:8000/` → 先到登录页，验证码登录（演示模式验证码直接返回）。
- API 文档：`http://<服务器IP>:8000/docs`
- 开放接口：`POST /v1/chat`、`POST /v1/bot/webhook`

> 生产建议前置 Nginx（HTTPS + 域名）。示例 Nginx 片段见文末。

---

## 7. 四大运维能力（本次新增）

### 7.1 开放 API 限流（防爆破 / 防滥用）
- 作用端点：`/v1/chat`、`/v1/bot/webhook`。
- 维度：**按 `api_key` 滑动窗口**；`api_key` 缺失时回退到 IP。
- 参数：`API_RATE_LIMIT_MAX`（窗口内最大次数，默认 30）、`API_RATE_LIMIT_WINDOW`（窗口秒数，默认 60）。
- 超限返回 **HTTP 429** + `Retry-After` 头，正文含剩余等待秒数。
- **失败开放（fail-open）**：限流器自身异常时一律放行，绝不阻断业务。
- 调整示例：把每个 key 限制为 10 次/分钟 → `.env` 加 `API_RATE_LIMIT_MAX=10`。

### 7.2 系统监控面板（管理员专属）
- 入口：工作台侧栏「📡 系统监控」（仅 `role=admin` 可见）。
- 接口：`GET /api/admin/metrics`（管理员专属）。
- 采集内容：运行时长、MongoDB 各核心集合文档数（`users`/`kb_documents`/`chat_sessions`/`qa_evals`/`llm_traces`/`kb_spaces`/`kb_audit_logs` 等）、Milvus 向量集合实体数、MinIO 对象数。
- **逐项 fail-open**：某个组件不可达时该项显示「不可达」，其余照常展示，异常明细在页面底部红框列出。

### 7.3 备份与恢复（管理员专属）
- 入口：工作台侧栏「💾 备份恢复」（仅 admin）。
- 接口：
  - `POST /api/admin/backup` `{ "label": "v1" }` → 全量备份到 `./backups/<时间戳>_<标签>/`。
  - `GET /api/admin/backups` → 列举已有备份。
  - `POST /api/admin/restore` `{ "dir": "backups/20260101_120000_v1", "components": ["mongo","milvus","minio"] }` → 恢复。
- 备份内容：
  - **MongoDB**：全部核心集合导出为 JSON（`bson.json_util`，保留 ObjectId / 时间类型）。
  - **Milvus**：向量集合（`kb_chunks`/`kb_item_names`）全量实体（含向量）导出为 JSON。
  - **MinIO**：桶内全部原始文件按对象名原样下载。
- 恢复策略：
  - MongoDB：按 `_id` upsert（已存在覆盖，不删除无关文档）→ 可安全重跑。
  - Milvus：先按主键清空目标集合再插入 → **破坏性，仅还原目标集合**。
  - MinIO：按对象名重新上传（覆盖同名）。
- 命令行（在部署服务器、服务在线时执行）：
  ```bash
  python utils/backup_utils.py --backup --label v1
  python utils/backup_utils.py --list
  python utils/backup_utils.py --restore backups/20260101_120000_v1 --components mongo,milvus
  ```

### 7.4 部署文档（本文件）
完整的私有化部署、配置、运维说明。

---

## 8. 安全加固建议

1. **改默认密码**：MongoDB / MinIO 的默认账号密码务必改为强密码（见 §5）。
2. **固定 `AUTH_SECRET`**：生产环境必须写入 `.env`，否则重启后所有 Token 失效、且临时密钥仅演示用。
3. **HTTPS 前置**：Nginx / Caddy 终止 TLS，禁止公网明文 8000。
4. **开放 API 限流**：见 §7.1，按业务量级调 `API_RATE_LIMIT_MAX/WINDOW`。
5. **租户与权限**：`tenant_id` 隔离 + 三档角色（admin/editor/viewer）+ 知识空间成员隔离已内置；生产请按需分配，避免全员 admin。
6. **审计**：所有管理操作（改角色 / 改租户 / 导入 / 删除文档）已写入 `kb_audit_logs`，管理员可在「📜 审计日志」查看。
7. **备份**：将 `./backups/` 目录纳入离线 / 异地备份（如每日 rsync 到对象存储），etcd 为 Milvus 元数据关键组件，建议高可用部署并单独备份。
8. **演示模式关闭验证码回传**：正式上线若接入短信网关，验证码不再明文返回（当前 `auth_utils.send_code` 演示模式直接返回 `demo_code`，接短信后删除回传即可）。

---

## 9. 备份恢复操作步骤（实战）

**场景 A：计划内备份**
1. 管理员登录 → 侧栏「💾 备份恢复」→ 填标签（如 `春节前`）→ 执行备份。
2. 备份完成后目录示例：`./backups/20260120_143000_春节前/{manifest.json, mongo/*.json, milvus/*.json, minio/...}`。
3. 把 `./backups/` 整体拷贝到异地存储。

**场景 B：整机迁移 / 灾难恢复**
1. 在新服务器装好 MongoDB / Milvus / MinIO，导入 `.env` 与模型。
2. 把旧机 `./backups/<最新>` 拷到新机同路径。
3. 启动应用后，管理员进入「💾 备份恢复」→ 填 `dir` 为对应目录 → 勾选需恢复的组件 → 执行恢复。
4. 恢复后到「📡 系统监控」核对各集合 / 向量数是否一致。

---

## 10. 常见运维命令

```bash
# 给已有 Milvus 集合补 tenant_id 字段（幂等，旧切片自动回填 default）
python utils/migrate_tenant_id.py --collection kb_chunks

# 查看/触发备份
python utils/backup_utils.py --list
python utils/backup_utils.py --backup --label manual

# 健康检查
curl -s http://127.0.0.1:8000/api/auth/me
curl -s "http://127.0.0.1:8000/api/admin/metrics" -H "Authorization: Bearer <admin_token>"
```

---

## 11. 附录：Nginx 反代示例（HTTPS）

```nginx
server {
    listen 443 ssl;
    server_name kb.your-domain.com;
    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

> 文档与代码同步维护。任何破坏性操作（恢复 / 删库 / 改默认密码）前务必先完整备份。
