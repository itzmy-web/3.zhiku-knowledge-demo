import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

from starlette.responses import FileResponse, RedirectResponse

from config.minio_config import minio_config
from processor.import_processor.main_graph import KBImportWorkflow
from tool.logger import logger
import uvicorn
from fastapi import Request, FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from starlette.middleware.cors import CORSMiddleware

from utils.minio_utils import get_minio_client
import asyncio
import json as _json
from starlette.responses import StreamingResponse
from utils.task_utils import update_task_status, add_done_task, add_running_task, get_task_status, get_done_task_list, \
    get_running_task_list

# 1. 创建应用
# 标题和描述会在Swagger文档中展示
app = FastAPI(
    title="汇思智藏-导入API",
    description="此文档是汇思智藏导入流程的API接口说明"
)

# 2. 跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许的源
    allow_credentials=False,  # token 走 header 而非 cookie；与 allow_origins=["*"] 共存时必须关掉 credentials
    allow_methods=["*"],  # 允许的请求方法
    allow_headers=["*"],  # 允许的请求头
)


# 3. 静态页面路由：返回文件导入前端页面
# 访问地址：http://localhost:8000/import.html
@app.get("/", include_in_schema=False)  # 根路径自动跳转到导入页（浏览器只输 IP 也能进）
async def root_redirect():
    return RedirectResponse(url="/import.html")

@app.get("/import.html")  # 对外访问地址
async def get_import_page():
    # 拼接HTML文件绝对路径
    current_dir_parent_path = Path(__file__).absolute().parent.parent
    html_path = current_dir_parent_path / "page" / "import.html"
    # 如果不存在，抛出404异常
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"没有查询到页面，地址为：{html_path}")
    return FileResponse(html_path)


# 4. 后台任务：LangGraph全流程执行
# 独立于主请求线程，由BackgroundTasks触发，避免阻塞接口响应
def run_graph_task(task_id: str, file_dir: str, import_file_path: str, tenant_id: str = "default"):
    """
    LangGraph全流程执行后台任务
    核心流程：初始化状态 → 流式执行图节点 → 实时更新任务状态 → 异常捕获
    任务状态更新：pending → processing → completed/failed
    节点进度更新：每完成一个节点，将节点名加入done_list，供前端轮询查看

    :param task_id: 全局唯一任务ID，关联单个文件的全流程处理
    :param file_dir: 该任务的本地文件存储目录（含临时文件/解析结果）
    :param import_file_path: 上传文件的本地绝对路径
    :param tenant_id: 切片归属租户；"default" 表示不隔离（兼容旧单租户数据）
    """
    try:
        # 1. 更新任务全局状态为：处理中
        update_task_status(task_id, "processing")

        # 2. 初始化LangGraph状态
        init_state = {
            "task_id": task_id, # 任务ID
            "file_dir": file_dir, # 输出路径
            "import_file_path": import_file_path, # 导入路径
            "tenant_id": tenant_id or "default",  # [租户隔离] 传给导入图，切片打标用
        }

        # 3. 流式执行LangGraph全流程（stream模式：实时获取每个节点的执行结果）
        workflow = KBImportWorkflow() # 创建工作流 调你整个 导入流程的 工作流
        for event in workflow.run(init_state, stream=True):# 让整个导入工作流 跑起来 流式输出
            for node_name, node_result in event.items():
                # 将完成的节点名加入【已完成列表】，前端轮询/status/{task_id}可实时获取
                add_done_task(task_id, node_name)

        # 4. 全流程执行完成，更新任务全局状态为：已完成
        update_task_status(task_id, "completed")

        # 4.1 同步文档管理记录为完成，并尝试统计该文件的向量块数
        try:
            from utils.kb_docs_utils import get_kb_docs_tool
            from utils.milvus_utils import get_milvus_client
            from config.milvus_config import milvus_config
            chunk_count = None
            try:
                file_title = os.path.basename(import_file_path)
                rows = get_milvus_client().query(
                    collection_name=milvus_config.chunks_collection,
                    filter=f"file_title=='{file_title}'",
                    output_fields=["count(*)"])
                chunk_count = int(rows[0].get("count(*)", 0)) if rows else 0
            except Exception as count_err:
                logger.warning(f"[{task_id}] 统计向量块数失败（不影响完成状态）：{count_err}")
            get_kb_docs_tool().mark_completed(task_id, chunk_count=chunk_count)
        except Exception as doc_err:
            logger.warning(f"[{task_id}] kb_documents 完成登记失败：{doc_err}")

    except Exception as e:
        # 5. 捕获全流程异常，更新任务全局状态为：失败，并记录错误日志（含堆栈）
        update_task_status(task_id, "failed")
        try:
            from utils.kb_docs_utils import get_kb_docs_tool
            get_kb_docs_tool().mark_failed(task_id, error=str(e))
        except Exception:
            pass
        logger.info(f"[{task_id}] LangGraph全流程执行失败，异常信息：{str(e)}", exc_info=True)


# 上传防护：白名单扩展名 + 单文件大小上限（防 DoS / 恶意文件落盘）
_UPLOAD_ALLOWED_EXT = {".pdf", ".md", ".txt", ".docx", ".pptx"}
_UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


# 5. 核心接口：文件上传接口
# 支持多文件上传，核心流程：接收文件 → 本地保存 → MinIO上传 → 启动后台任务
# 访问地址：http://localhost:8000/upload （POST请求，form-data格式传参）
@app.post("/upload", summary="文件上传接口", description="支持多文件批量上传，自动触发知识库导入全流程")
async def upload_files(background_tasks: BackgroundTasks, request: Request, files: List[UploadFile] = File(...)):
    """
    文件上传核心接口
    1. 接收前端上传的多文件（PDF/MD为主）
    2. 按「日期/任务ID」分层保存到本地输出目录，避免文件冲突
    3. 将文件上传至MinIO对象存储，做持久化保存
    4. 为每个文件生成唯一TaskID，启动独立的LangGraph后台处理任务
    5. 实时更新任务状态，供前端轮询监控进度

    :param background_tasks: FastAPI后台任务对象，用于异步执行LangGraph流程
    :param files: 前端上传的文件列表（form-data格式）
    :return: 包含上传结果和所有任务ID的JSON响应
    """
    # 0. 解析当前登录用户（未登录允许上传，但文档不归属任何用户）
    current_user_id = None
    current_tenant_id = "default"
    try:
        auth_header = request.headers.get("authorization", "")
        token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        from utils.auth_utils import verify_token, get_auth_tool
        current_user_id = verify_token(token) or None
        if current_user_id:
            _u = get_auth_tool().get_user(current_user_id)
            if _u:
                current_tenant_id = _u.get("tenant_id") or "default"
    except Exception:
        pass

    # 1. 构建本地存储根目录：项目根目录/doc/YYYYMMDD（按日期分层，方便管理）
    # 优先读取环境变量，未配置则使用项目内默认中转目录
    data_based_root_dir = os.getenv("DATA_BASED_ROOT_DIR", r"D:\knowledge_base\doc")
    data_dir = os.path.join(data_based_root_dir, datetime.now().strftime("%Y%m%d"))
    # 初始化任务ID列表，用于返回给前端（一个文件对应一个TaskID）
    task_ids = []

    # 2. 遍历处理每个上传的文件（多文件批量处理，各自独立生成TaskID）
    for file in files:
        # 生成全局唯一TaskID（UUID4），作为单个文件的全流程标识
        task_id = str(uuid.uuid4())# 会为每一个 PDF上传的流程 生成唯一的任务ID （追踪）
        task_ids.append(task_id)
        logger.info(f"[{task_id}] 开始处理上传文件，文件名：{file.filename}，文件类型：{file.content_type}")

        # 3. 标记「文件上传」阶段为「运行中」，前端轮询可查   添加一个标记
        add_running_task(task_id, "upload_file")# 什么都没做 就是标记一下当前的进度

        # 2.5 登记到文档管理（kb_documents）：关联当前登录用户（未登录则为空）
        try:
            from utils.kb_docs_utils import get_kb_docs_tool
            get_kb_docs_tool().record_document(
                user_id=current_user_id,
                task_id=task_id,
                file_name=file.filename,
                minio_object=f"pdf_files/{datetime.now().strftime('%Y%m%d')}/{file.filename}",
            )
            # [审计] 记录文档导入（fail-open）
            from utils.audit_utils import record_audit, ACTION_DOC_IMPORT
            record_audit(ACTION_DOC_IMPORT, actor_id=current_user_id,
                         target_id=task_id, detail={"file_name": file.filename},
                         tenant_id=current_tenant_id)
        except Exception as doc_err:
            logger.warning(f"[{task_id}] 文档登记失败（不影响上传）：{doc_err}")

        # 4. 构建该任务的本地独立目录：output/YYYYMMDD/TaskID，避免多文件重名冲突
        file_dir = os.path.join(data_dir, task_id)
        os.makedirs(file_dir, exist_ok=True)  # 目录不存在则创建，存在则不做处理
        # 构建上传文件的本地保存绝对路径
        import_file_path = os.path.join(file_dir, file.filename)

        # 5. 将上传的文件保存到本地临时目录（后续MinIO上传/文件解析均基于此文件）
        # 上传防护：先校验扩展名白名单与大小上限，再落盘（避免超大/非法文件直接占满磁盘）
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in _UPLOAD_ALLOWED_EXT:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型：{ext or '无扩展名'}，仅允许 {sorted(_UPLOAD_ALLOWED_EXT)}",
            )
        _raw = await file.read()
        if len(_raw) > _UPLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大：{len(_raw) / 1024 / 1024:.1f} MB，上限 {_UPLOAD_MAX_BYTES / 1024 / 1024:.0f} MB",
            )
        with open(import_file_path, "wb") as file_buffer:
            file_buffer.write(_raw)
        logger.info(f"[{task_id}] 文件已保存至本地，路径：{import_file_path}")

        # 以上解决一个问题 把用户上传的文件 先缓存到 指定目录
        # 紧接着 进行下一步  缓存目录  用户上传的文件  上传至MinIO

        # 6. 将本地文件上传至MinIO对象存储，做持久化保存
        # 构建MinIO中的文件对象名：pdf_files/YYYYMMDD/文件名（按日期分层，和本地一致） MINIO上的路径
        minio_object_name = f"pdf_files/{datetime.now().strftime('%Y%m%d')}/{file.filename}"
        try:
            # 获取MinIO客户端实例
            minio_client = get_minio_client()

            # 从环境变量获取MinIO的桶名配置
            minio_bucket_name = minio_config.bucket_name

            # 本地文件上传至MinIO（同名文件会自动覆盖，保证文件最新）
            minio_client.fput_object(
                bucket_name=minio_bucket_name,# 桶
                object_name=minio_object_name,# 文件名 'pdf_files/20260728/H3C LA2608室内无线网关 用户手册-6W100-整本手册.pdf'
                file_path=import_file_path,# 导入路径 D:\file\20260728\d41d7662-506e-4732-8ae1-7d5613a91ffb
                content_type=file.content_type  # 传递文件原始MIME类型
            )
            logger.info(f"[{task_id}] 文件已成功上传至MinIO，桶名：{minio_bucket_name}，对象名：{minio_object_name}")
        except Exception as e:
            # MinIO上传失败，记录警告日志（不中断后续流程，本地文件仍可继续处理）
            logger.warning(f"[{task_id}] 文件上传MinIO失败，将继续执行本地处理流程，异常信息：{str(e)}", exc_info=True)

        # 先把文件接收到 本地缓存  从本地缓存里面 上传 minio
        # 7. 标记「文件上传」阶段为「已完成」，前端轮询可查
        add_done_task(task_id, "upload_file") # 标记当前进展到哪一步

        # 8. 将LangGraph全流程处理加入FastAPI后台任务（异步执行，不阻塞当前接口响应）
        # [租户隔离] 把当前用户所属租户传入导入流程，使切片打上 tenant_id
        background_tasks.add_task(run_graph_task, task_id, file_dir, import_file_path, current_tenant_id) # 添加后台 任务
        logger.info(f"[{task_id}] 已将LangGraph全流程加入后台任务，任务已启动")

    # 9. 所有文件处理完毕，返回上传成功信息和所有TaskID（前端基于TaskID轮询进度）
    logger.info(f"多文件上传处理完毕，共处理{len(files)}个文件，生成TaskID列表：{task_ids}")
    return {
        "code": 200,
        "message": f" 文件上传成功, total: {len(files)}",
        "task_ids": task_ids
    }


# 6. 核心接口：任务状态查询接口
# 前端轮询此接口获取单个任务的处理进度和状态
# 访问地址：http://localhost:8000/status/{task_id} （GET请求）
@app.get("/status/{task_id}", summary="任务状态查询", description="根据TaskID查询单个文件的处理进度和全局状态")
async def get_task_progress(task_id: str):
    """
    任务状态查询接口
    前端轮询此接口（如每秒1次），获取任务的实时处理进度
    返回数据均来自内存中的任务管理字典（task_utils.py），高性能无IO

    :param task_id: 全局唯一任务ID（由/upload接口返回）
    :return: 包含任务全局状态、已完成节点、运行中节点的JSON响应
    """
    # 构造任务状态返回体  获取你当前的进度
    task_status_info: Dict[str, Any] = {
        "code": 200,
        "task_id": task_id,
        "status": get_task_status(task_id),  # 任务全局状态：pending/processing/completed/failed
        "done_list": get_done_task_list(task_id),  # 已完成的节点/阶段列表
        "running_list": get_running_task_list(task_id)  # 正在运行的节点/阶段列表
    }
    # 记录状态查询日志，方便追踪前端轮询情况
    logger.info(
        f"[{task_id}] 任务状态查询，当前状态：{task_status_info['status']}，已完成节点：{task_status_info['done_list']}")
    return task_status_info


# 7. SSE 实时进度接口：浏览器 EventSource 订阅，每秒推送一次阶段进度
# 访问地址：http://localhost:8000/api/import/progress/{task_id}
@app.get("/api/import/progress/{task_id}", summary="SSE 实时进度推送")
async def import_progress_sse(task_id: str):
    async def event_gen():
        # 首条立即推送，之后每秒一条；任务终态后自动结束流
        while True:
            info = {
                "status": get_task_status(task_id),
                "done_list": get_done_task_list(task_id),
                "running_list": get_running_task_list(task_id),
            }
            yield "data: " + _json.dumps(info, ensure_ascii=False) + "\n\n"
            if info["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(1)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    """服务启动入口：本地开发环境直接运行"""
    logger.info("汇思智藏服务启动中...")
    # 启动uvicorn服务，绑定本地IP和8000端口，关闭自动重载（生产环境建议用workers多进程）
    uvicorn.run(
        app=app,
        host="127.0.0.1",  # 仅本地访问，生产环境改为0.0.0.0（允许所有IP访问）
        port=8000  # 服务端口
    )
