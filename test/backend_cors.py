import json
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
from tool.logger import logger

# 导入你的识别节点
from processor.import_processor.nodes.node_item_name_recognition import NodeItemNameRecognition

app = FastAPI(title="知识库商品识别服务", version="1.0")

# ---------- 添加 CORS（允许所有来源，仅供开发测试） ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],                # 生产环境请替换为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 请求模型 ----------
class ChunkItem(BaseModel):
    title: str
    content: str

class RecognizeRequest(BaseModel):
    file_title: str
    chunks: List[ChunkItem]   # 前端应传入符合此结构的切片列表

class RecognizeResponse(BaseModel):
    item_name: str
    status: str = "success"
    message: Optional[str] = None

# ---------- 核心 API ----------
@app.post("/recognize", response_model=RecognizeResponse, summary="识别商品名称")
async def recognize_item_name(req: RecognizeRequest):
    """
    接收文件标题和切片列表，返回识别出的商品名称。
    """
    try:
        # 将请求数据转换为节点所需的 state 字典
        state = {
            "file_title": req.file_title,
            "chunks": [chunk.dict() for chunk in req.chunks]  # Pydantic 转 dict
        }

        # 调用节点（注意：节点类是可调用对象，执行 process）
        node = NodeItemNameRecognition()
        result_state = node(state)   # 返回更新后的 state

        item_name = result_state.get("item_name", "")
        if not item_name:
            # 如果识别结果为空，可以返回警告，但状态依然成功
            return RecognizeResponse(
                item_name=item_name,
                status="warning",
                message="未能识别出商品名称，请检查输入"
            )

        return RecognizeResponse(item_name=item_name)

    except Exception as e:
        logger.error(f"识别失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"服务器处理错误: {str(e)}")

# ---------- 健康检查 ----------
@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    logger.info("商品识别服务启动中...")
    uvicorn.run(
        app=app,
        host="127.0.0.1",
        port=8001,      # 与你现有的端口一致
        reload=True     # 开发时自动重启，方便调试
    )