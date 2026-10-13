# processor/query_processor/state.py

from typing import TypedDict, List

class QueryGraphState(TypedDict):
    """
    查询流程图状态
    包含整个查询流程中传递的所有数据。
    """

    session_id: str  # 会话ID
    message_id: str  # 消息ID

    original_query: str  # 用户原始问题

    # 检索过程中的中间数据
    embedding_chunks: list  # 普通向量检索回来的切片
    hyde_embedding_chunks: list  # 已向量化的假设性问题切片
    hyde_doc: str  # HyDE 节点生成的假设性答案文档，用于检索增强
    web_search_docs: list  # 网络搜索回来的文档

    # 排序过程中的数据
    rrf_chunks: list  # RRF 融合排序后的切片
    reranked_docs: list  # 重排序后的最终 Top-K 文档

    # 生成过程中的数据
    prompt: str  # 组装好的 Prompt
    answer: str  # 最终生成的答案

    # 辅助信息
    item_names: List[str]  # 提取出的商品名称
    rewritten_query: str  # 改写后的问题   理解用户的意图
    history: list  # 历史对话记录
    is_stream: bool  # 是否流式输出

    # [检索开关面板] 可调参数（不传时用默认值）
    search_top_k: int   # 最终参考文档条数（RRF 截断数，默认3）
    rrf_k: int          # RRF 平滑参数 K（默认60）
    rerank_enable: bool  # 是否启用 rerank 精排（默认True）
    web_search_enable: bool  # 是否启用联网检索（默认True）
    image_b64: str    # [图片问答] 用户上传图片的 data URL（base64），空则走纯文本链路
    space_titles: list  # [多知识库空间] 检索白名单（file_title 列表），空则全库检索
    space_denied_titles: list  # [知识库级访问控制] 默认检索时排除的文档（未加入空间的 file_title 并集），空则不排除
    # [检索开关面板] 新增可调参数
    rrf_weight: float       # embedding 路相对 hyde 路的 RRF 融合权重（默认1.0 = 经典等权 RRF）
    similarity_threshold: float  # 向量相似度阈值（COSINE：相似度 = 1 - 距离），>0 时过滤低相似度片段，默认0（不过滤）
    tenant_id: str          # [租户隔离] 当前用户所属租户；"default" 表示不隔离（兼容旧单租户数据）