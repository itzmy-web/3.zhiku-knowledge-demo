# web/api/query_service.py
"""
查询流程的接口定义
"""
import uuid
import json
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, StreamingResponse, RedirectResponse

from processor.query_processor.main_graph_v2 import KBQueryWorkflowV2
from utils.mongo_history_utils import clear_history, get_recent_messages, get_last_messages
from utils.sse_utils import create_sse_queue, SSEEvent, push_to_session, sse_generator
from utils.task_utils import update_task_status, TASK_STATUS_PROCESSING, get_task_result, set_task_result, TASK_STATUS_COMPLETED, \
    TASK_STATUS_FAILED
from tool.logger import logger
# 1. 创建应用
app = FastAPI(
    title="汇思智藏-查询API",
    description="此文档是汇思智藏查询流程的API接口说明"
)

# 2. 跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许的源
    allow_credentials=True,  # 允许携带cookie
    allow_methods=["*"],  # 允许的请求方法
    allow_headers=["*"],  # 允许的请求头
)

# 3. 静态页面路由
@app.get("/", include_in_schema=False)  # 根路径自动跳转到对话页（浏览器只输 IP 也能进）
async def root_redirect():
    return RedirectResponse(url="/chat.html")

@app.get("/chat.html")  # 对外访问地址
async def chat():
    current_dir_parent_path = Path(__file__).absolute().parent.parent
    html_path = current_dir_parent_path / "page" / "chat.html"
    # 如果不存在，抛出404异常
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"没有查询到页面，地址为：{html_path}")
    return FileResponse(html_path)


# 定义接口接收的数据结构
class QueryRequest(BaseModel):
    """查询请求数据结构"""
    query: str = Field(..., description="查询内容")  # ...必须填写
    session_id: str = Field(None, description="会话ID")
    is_stream: bool = Field(False, description="是否流式返回")
    # [检索开关面板] 可选检索参数（不传用默认值）
    search_top_k: int = Field(None, description="最终参考文档条数 1~10，默认3")
    rrf_k: int = Field(None, description="RRF 平滑参数 K，默认60")
    rerank_enable: bool = Field(None, description="是否启用 rerank 精排，默认True")
    web_search_enable: bool = Field(None, description="是否启用联网检索，默认True")
    image_b64: str = Field(None, description="用户上传图片的 data URL（base64），触发多模态问答")
    space_id: str = Field(None, description="知识库空间ID：指定后只在该空间包含的文档内检索")
    # [检索开关面板] 新增可调参数
    rrf_weight: float = Field(None, description="embedding 路相对 hyde 路的 RRF 融合权重，默认1.0（经典等权 RRF）")
    similarity_threshold: float = Field(None, description="向量相似度阈值(COSINE：相似度=1-距离)，>0 时过滤低相似度片段，默认0（不过滤）")

@app.post("/query")
async def query(background_tasks: BackgroundTasks, request: QueryRequest, raw_request: Request):
    """
    1 解析参数
    2 更新任务状态
    3 调用处理流程图
    4 返回结果
    :param background_tasks:
    :param request:
    :return:
    """
    user_query = request.query
    session_id = request.session_id if request.session_id else str(uuid.uuid4())

    # 处理是不是流式返回结果
    is_stream = request.is_stream
    # [检索开关面板] 收集可选检索参数（None 的字段不覆盖默认值）
    # [租户隔离] 先从请求里解析当前用户所属租户，默认 "default" 表示不隔离
    tenant_id = "default"
    try:
        from web.api.kb_service import get_current_user
        _u = get_current_user(raw_request)
        if _u:
            tenant_id = _u.get("tenant_id") or "default"
    except Exception as _te:
        logger.warning(f"[租户] 解析失败（默认不隔离）: {_te}")

    search_params = {
        "search_top_k": request.search_top_k,
        "rrf_k": request.rrf_k,
        "rerank_enable": request.rerank_enable,
        "web_search_enable": request.web_search_enable,
        "rrf_weight": request.rrf_weight,
        "similarity_threshold": request.similarity_threshold,
        "tenant_id": tenant_id,
    }

    # [多知识库空间] 指定空间时解析文档白名单（校验成员身份）
    space_titles = None
    if getattr(request, "space_id", None):
        try:
            from utils.spaces_utils import get_space_titles
            _u = get_current_user(raw_request)
            if _u:
                space_titles = get_space_titles(_u["user_id"], request.space_id)
                if space_titles is None:
                    space_titles = []   # 非成员 → 限定为空集（不出结果）
        except Exception as _spe:
            logger.warning(f"[空间过滤] 解析失败（走全库）: {_spe}")

    # [知识库级访问控制] 默认检索（未指定 space_id）时，排除用户未加入空间的文档
    # 已加入空间的文档 + 未分配空间的公共文档可见；其他空间文档隐藏；无空间时 denied 为空 = 全库可见（兼容旧行为）
    space_denied_titles = []
    if not getattr(request, "space_id", None):
        try:
            from utils.spaces_utils import get_space_denied_titles
            _u = get_current_user(raw_request)
            if _u:
                space_denied_titles = get_space_denied_titles(_u["user_id"])
        except Exception as _de:
            logger.warning(f"[访问控制] 计算排除列表失败（不过滤）: {_de}")

    if is_stream:
        # 创建一个字典 存储对一个session_id : queue 结果队列
        create_sse_queue(session_id)
    # 更新任务状态
    # 当前会话id作为key! 整体装填处于运行中！
    update_task_status(session_id, TASK_STATUS_PROCESSING,is_stream)

    print("开始处理流程... 是否流式:", is_stream, f"其他参数:{user_query}, session_id:{session_id}")

    if is_stream:
        # 如果是流式，则返回一个流式响应，过程不断地推送
        # 运行执行图对象方法
        background_tasks.add_task(run_query_graph, session_id, user_query, is_stream, search_params, request.image_b64, space_titles, space_denied_titles)
        # 返回结果
        print("开始处理结果....")
        return {
            "message":"结果正在处理中...",
            "session_id":session_id
        }
    else:
        # 同步运行
        run_query_graph(session_id, user_query, is_stream, search_params, request.image_b64, space_titles, space_denied_titles)
        answer = get_task_result(session_id,"answer","")
        return {
            "message":"处理完成！",
            "session_id":session_id,
            "answer":answer,
            "done_list":[]
        }

# 定义查询接口
def run_query_graph(session_id: str, user_query: str, is_stream: bool = True, search_params: dict = None, image_b64: str = None, space_titles: list = None, space_denied_titles: list = None):
    print(f"开始流程图处理...{session_id} {user_query} {is_stream}")

    # [安全加固] 提示词注入检测：命中高危模式直接拒答，不进入 RAG 链路
    from utils.security_utils import detect_injection, trace_llm
    t0 = time.time()
    hits = detect_injection(user_query or "")
    if hits:
        refusal = ("抱歉，您的输入包含疑似指令注入内容，为保护知识库安全，本次请求已被拦截。"
                   "请重新描述您的正常问题。")
        logger.warning(f"[注入拦截] session={session_id} 命中模式: {hits}")
        trace_llm(session_id, "injection_block", (time.time() - t0) * 1000, prompt_chars=len(user_query or ""))
        if is_stream:
            push_to_session(session_id, SSEEvent.DELTA, {"delta": refusal})
            push_to_session(session_id, SSEEvent.FINAL,
                            {"answer": refusal, "status": "completed", "image_urls": [], "sources": []})
        else:
            set_task_result(session_id, "answer", refusal)
        update_task_status(session_id, TASK_STATUS_COMPLETED, is_stream)
        return

    # [语义缓存] 仅默认检索参数时启用（自定义参数的答案不具备可复用性）
    custom_search = search_params and any(v is not None for v in search_params.values())
    if not custom_search:
        try:
            from utils.cache_utils import lookup_cache
            cached = lookup_cache(user_query)
            if cached:
                answer = f"⚡ 缓存命中（相似问题：{cached['similar_question']}，相似度 {cached['score']}）\n\n{cached['answer']}"
                logger.info(f"[语义缓存] 直接返回缓存答案，session={session_id}")
                if is_stream:
                    push_to_session(session_id, SSEEvent.DELTA, {"delta": answer})
                    push_to_session(session_id, SSEEvent.FINAL,
                                    {"answer": answer, "status": "completed",
                                     "image_urls": [], "sources": cached.get("sources") or []})
                else:
                    set_task_result(session_id, "answer", answer)
                    set_task_result(session_id, "sources", json.dumps(cached.get("sources") or [], ensure_ascii=False))
                update_task_status(session_id, TASK_STATUS_COMPLETED, is_stream)
                return
        except Exception as _ce:
            logger.warning(f"[语义缓存] 查询异常（走正常链路）: {_ce}")

    # [多轮追问] 图谱启动前取上一轮的最近10条对话注入 state.history（此刻本轮消息尚未写入）
    try:
        raw_history = get_last_messages(session_id, limit=10)
        chat_history = [
            {"role": m.get("role", "user"), "text": m.get("text", "")}
            for m in raw_history if m.get("role") in ("user", "assistant") and m.get("text")
        ]
    except Exception as _he:
        logger.warning(f"加载会话历史失败（不影响本轮问答）: {_he}")
        chat_history = []

    init_state = {
        "session_id": session_id,
        "message_id": "",
        "original_query": user_query,
        "embedding_chunks": [],
        "hyde_embedding_chunks": [],
        "hyde_doc": "",
        "web_search_docs": [],
        "rrf_chunks": [],
        "reranked_docs": [],
        "prompt": "",
        "answer": "",
        "item_names": [],
        "rewritten_query": "",
        "history": chat_history,
        "is_stream": is_stream,
        # [检索开关面板] 前端可调参数（None 时走节点内默认值）
        "search_top_k": (search_params or {}).get("search_top_k"),
        "rrf_k": (search_params or {}).get("rrf_k"),
        "rerank_enable": (search_params or {}).get("rerank_enable"),
        "web_search_enable": (search_params or {}).get("web_search_enable"),
        "rrf_weight": (search_params or {}).get("rrf_weight"),
        "similarity_threshold": (search_params or {}).get("similarity_threshold"),
        "tenant_id": (search_params or {}).get("tenant_id") or "default",
        "image_b64": image_b64,
        "space_titles": space_titles,
        "space_denied_titles": space_denied_titles or [],
    }

    try:
        workflow = KBQueryWorkflowV2()
        for chunk in workflow.run(init_state, stream=is_stream):
            logger.debug(chunk)
        update_task_status(session_id, TASK_STATUS_COMPLETED, is_stream)
        # [语义缓存] 成功完成后写入缓存（answer/sources 来自任务结果）
        if not custom_search:
            try:
                from utils.cache_utils import save_cache
                ans = get_task_result(session_id, "answer", "")
                src = get_task_result(session_id, "sources", "[]")
                save_cache(user_query, ans, json.loads(src) if src else [])
            except Exception as _se:
                logger.warning(f"[语义缓存] 写入异常: {_se}")
    except Exception as e:
        print(f"流程执行异常: {e}")
        update_task_status(session_id, TASK_STATUS_FAILED, is_stream)
        if is_stream:
            push_to_session(session_id, SSEEvent.ERROR, {"error": str(e)})



@app.get("/stream/{session_id}")
async def stream(session_id: str, request: Request):

    print("调用流式/stream...")
    """
    sse 实时返回结果
    """
    return StreamingResponse(
        sse_generator(session_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@app.delete("/history/{session_id}")
async def clear_chat_history(session_id: str):
    """
    清空指定会话的历史记录
    """
    count = clear_history(session_id)
    return {"message": "历史会话已清空", "deleted_count": count}

@app.get("/history/{session_id}")
async def history(session_id: str, limit: int = 50):
    """
    查询当前会话历史记录
    """
    try:
        records = get_recent_messages(session_id, limit=limit)
        items = []
        for r in records:
            items.append({
                "_id": str(r.get("_id")) if r.get("_id") is not None else "",
                "session_id": r.get("session_id", ""),
                "role": r.get("role", ""),
                "text": r.get("text", ""),
                "rewritten_query": r.get("rewritten_query", ""),
                "item_names": r.get("item_names", []),
                "ts": r.get("ts")
            })
        return {"session_id": session_id, "items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"history error: {e}")


# 证明服务器启动即可
@app.get("/health")
async def health():
    """
    检查服务是否正常
    """
    return {"ok": True}

if __name__ == "__main__":
    # [修改] 8001 已被电商项目容器占用，改用 8002；启动 2 秒后自动打开浏览器
    import threading, time, webbrowser
    def _open_browser():
        time.sleep(2)
        webbrowser.open("http://127.0.0.1:8002/chat.html")
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8002)