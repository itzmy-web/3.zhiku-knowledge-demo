# processor/query_processor/nodes/node_search_embedding.py
from config.milvus_config import milvus_config
from processor.query_processor.base import NodeBase, T
from processor.query_processor.state import QueryGraphState
from tool.logger import logger
from utils.embedding_utils import generate_embeddings
from utils.json_format_utils import serialize_json
from utils.milvus_utils import create_hybrid_search_requests, get_milvus_client, hybrid_search, escape_milvus_string
from utils.task_utils import add_done_task


class NodeSearchEmbedding(NodeBase):
    """
   节点功能：基于已确认主体名+改写后的用户问题，执行Milvus向量数据库混合检索
   """

    # 覆盖基类的 name 属性，标识节点名称
    name: str = "node_search_embedding"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        """
        核心节点函数：基于已确认商品名+改写后的用户问题，执行Milvus向量数据库混合检索
        流程：用户问题向量化 → 构造带商品名过滤的混合搜索请求 → 执行稠密+稀疏混合检索 → 返回检索结果
        :param state: Dict - 会话状态字典，包含上游传递的核心信息，关键字段：
                      {
                          "rewritten_query": str,   # step4改写后的完整用户问题（含商品名）
                          "item_names": list[str],  # step7已确认的标准化商品名列表
                      }

        :return: Dict - 检索结果字典，仅包含embedding_chunks字段，供下游节点使用：
                 {
                     "embedding_chunks": List[Dict]  # Milvus检索结果列表，无结果则为空列表
                                                     # 每个元素为一条匹配的向量数据，含业务字段
                 }
        """

        try:

            # 1、用户问题和已确认商品名
            query = state.get("rewritten_query")
            item_names = state.get("item_names")

            # 2、生成向量 (Dense + Sparse)
            embeddings = generate_embeddings([query])
            dense_vec = embeddings.get("dense")[0]
            sparse_vec = embeddings.get("sparse")[0]

            # 3. 获取Milvus的集合
            collection_name = milvus_config.chunks_collection

            # 4、处理 item_names：对每个值做安全转义后构造 Milvus 过滤表达式
            #    转义函数可防止商品名中的特殊字符（引号、反斜杠等）导致 Milvus 解析报错
            expr = None
            # [多知识库空间] 空间白名单优先：只在空间包含的文档内检索（file_title in [...]）
            space_titles = state.get("space_titles") or []
            if space_titles:
                escaped_titles = [escape_milvus_string(str(t)) for t in space_titles]
                quoted_titles = ", ".join(f'"{t}"' for t in escaped_titles)
                expr = f"file_title in [{quoted_titles}]"
                logger.info(f"[空间过滤] 检索范围限定 {len(space_titles)} 个文档")
            elif item_names:
                # 逐个转义：先将 item_name 中的 \ → \\, " → \", ' → \', 再包裹双引号
                escaped_names = [escape_milvus_string(str(v)) for v in item_names]
                # Milvus 表达式要求字符串值用双引号包裹，如: item_name in ["A","B"]
                quoted = ", ".join(f'"{v}"' for v in escaped_names)
                expr = f"item_name in [{quoted}]"
                logger.info(f"过滤条件: {expr}")
            else:
                logger.info("未指定商品名过滤，将全库检索")

            # [租户隔离] 指定「非默认租户」时，检索限定在该租户内；默认租户 "default" 不下过滤，兼容旧单租户数据
            tenant_id = state.get("tenant_id")
            if tenant_id and tenant_id != "default":
                cond = f'tenant_id == "{escape_milvus_string(str(tenant_id))}"'
                expr = f"({expr}) and {cond}" if expr else cond
                logger.info(f"[租户隔离] 限定租户 {tenant_id}")

            # [知识库级访问控制] 默认检索时排除用户未加入空间的文档（file_title not in [...]）
            # space_denied_titles 为空 → 不限制（兼容无空间/单空间场景）；与显式 space_id 白名单互斥（默认路径才设置）
            space_denied = state.get("space_denied_titles") or []
            if space_denied:
                escaped_denied = [escape_milvus_string(str(t)) for t in space_denied]
                quoted_denied = ", ".join(f'"{t}"' for t in escaped_denied)
                cond = f"file_title not in [{quoted_denied}]"
                expr = f"({expr}) and {cond}" if expr else cond
                logger.info(f"[访问控制] 排除 {len(space_denied)} 个无权限文档")

            # 5、构造Milvus混合搜索请求对象
            reqs = create_hybrid_search_requests(
                dense_vector=dense_vec,
                sparse_vector=sparse_vec,
                expr=expr,# 表达式： BrotherHAK-180烫金机","BrotherHAK180烫金机
                limit=10  # 底层检索返回数量（后续会再过滤为5，预留更多结果做重排序）
            )

            # 6、执行混合向量检索
            logger.info("开始执行 Milvus 混合检索...")
            client = get_milvus_client()
            res = hybrid_search(# 检索 混合检索
                client=client,
                collection_name=collection_name,  # 检索的目标集合名（文本片段向量集合）
                reqs=reqs,  # 构造好的混合搜索请求对象（稠密+稀疏）
                ranker_weights=(0.8, 0.2),  # 稠/稀疏向量评分权重配比，各占50%（可按业务调优）
                output_fields=["chunk_id", "content", "item_name", "file_title"]  # 指定返回的业务字段（含源文件标题，用于引用溯源）
            )
            # 7、构造并返回结果：若检索结果非空，取res[0]，否则返回空列表
            return {"embedding_chunks": res[0] if res else []}

        except Exception as e:
            logger.exception(f"向量搜索失败: {e}")
            # 返回标准格式空列表，避免下游 get() 时拿到 None（TypedDict 类型一致性）
            return {"embedding_chunks": []}
        finally:
            # [BUG修复] add_done_task 移入 finally 块，确保异常/正常路径下都能标记任务完成
            # 避免前端进度条因异常而永久卡在"切片搜索"运行中
            add_done_task(state.get("session_id"), self.name, state.get("is_stream"))


if __name__ == "__main__":
    init_state = {
        "rewritten_query": "关于brother HAK180烫金机，如何调节转印温度？",
        "item_names": ["BrotherHAK180烫金", "BrotherHAK-180烫金机","BrotherHAK180烫金扫描仪"]
    }
    node_search_embedding = NodeSearchEmbedding()
    result = node_search_embedding(init_state)
    logger.info(serialize_json(result, indent=4))# 打印
