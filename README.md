# 掌柜智库 · 知识问答平台

企业私有化的 RAG 知识问答平台：文档导入 → 解析切分 → 向量化 → 混合检索 → 重排 → 溯源回答。

## 两种运行形态

| 形态 | 入口 | 外部依赖 | 用途 |
|---|---|---|---|
| **完整本地栈** | `web/api/kb_service.py`（FastAPI，端口 8000） | MongoDB + Milvus + MinIO + BGE-M3（GPU） | 正式自托管 / 私有化部署 |
| **云端精简版** | `zhiku_cloud/server.py`（FastAPI，单端口） | 无（仅 fastapi / uvicorn / python-dotenv） | 演示站，零基础设施依赖 |

云端精简版面向「单端口可发布」场景：只暴露一个 HTTP 端口，数字人网关密钥不出服务端（由 `server.py` 内部重算签名后代理转发）。

## 目录结构

```
.
├── web/
│   ├── api/            # 完整版后端：kb_service（问答/管理）、import_service、query_service
│   └── page/           # 完整版前端页面
├── zhiku_cloud/
│   ├── server.py       # 精简版单端口服务（含数字人签名代理）
│   ├── page/           # 精简版页面（login/index/chat + 11 个功能模块页）
│   └── requirements.txt
├── processor/          # 处理链：import_processor（导入）/ query_processor（查询）
├── utils/              # 工具层：milvus_utils、auth_utils、日志等
├── config/             # embedding / reranker 配置
├── tool/               # 工具集
├── test/               # 测试
├── DEPLOY.md           # 部署与运维文档（架构、外部服务、全量 .env、限流/监控/备份）
└── pyproject.toml      # uv 依赖声明（Python 3.11）
```

## 架构

```
Frontend (静态 HTML)
        │
        ▼
FastAPI 单端口服务
        │
        ├── MongoDB    元数据（users / kb_* / qa_* / llm_*）
        ├── Milvus     向量库（kb_chunks / kb_item_names）
        └── MinIO      文档原件与图片
```

- **向量 / 重排**：BGE-M3 本地推理（`config/embedding_config.py`、`config/reranker_config.py`）
- **LLM**：通过 `OPENAI_API_BASE` / `OPENAI_API_KEY` 接入任意兼容 OpenAI 协议的模型服务
- **检索链路**：商品名确认 → 混合检索 → HyDE → 联网检索 → RRF 融合 → 重排 → 生成

## 快速开始

### 云端精简版（无外部依赖）

```bash
cd zhiku_cloud
pip install -r requirements.txt
PORT=8002 python server.py
```

### 完整本地栈

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt                       # 或 uv sync
uvicorn web.api.kb_service:app --port 8000
```

启动前需先拉起 MongoDB / Milvus / MinIO，并复制 `.env.example` 为 `.env` 填写实际配置。详见 **DEPLOY.md**。

## 配置

所有配置集中在 `.env`（**已被 .gitignore 排除，不入库**）。字段清单见 `.env.example`，涵盖：

- Milvus / MongoDB / MinIO 连接
- BGE-M3 向量与重排模型路径、设备（`cuda` / `cpu`）
- LLM 端点与模型名
- 数字人（APP_ID / APP_SECRET / GATEWAY_SERVER）

> 提示：`BGE_DEVICE` 只接受 `cuda` / `cpu`；`ITEM_MODEL` 需与所选 LLM 平台的模型命名一致。

## 文档

- `DEPLOY.md` —— 架构、依赖、外部服务启停、`.env` 全量说明、启动、限流 / 监控 / 备份恢复、安全加固

## 说明

仓库内**不包含**任何真实密钥；`.env` 已在 `.gitignore` 中排除，仅保留 `.env.example` 占位模板。部署到公开环境时请勿上传真实或涉密资料。
