# processor/query_processor/nodes/node_rrf.py
from typing import List, Tuple, Dict

from processor.query_processor.base import NodeBase
from processor.query_processor.state import QueryGraphState
from tool.logger import logger
from utils.json_format_utils import serialize_json
from utils.task_utils import add_done_task


class NodeRrf(NodeBase):
    """
    节点功能：Reciprocal Rank Fusion
    将多路召回的结果（向量、HyDE）进行加权融合排序。
    """

    # 覆盖基类的 name 属性，标识节点名称
    name: str = "node_rrf"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        """
        节点逻辑
        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """

        # 1. 获取各搜索路的搜索结果
        embedding_chunks_list = state.get("embedding_chunks") or []
        hyde_embedding_chunks_list = state.get("hyde_embedding_chunks") or []

        # 2. 清洗数据
        # embedding_search_list = [
        #     {"chunk_id": "chunk_1", "content": "向量搜索结果#1"},
        #     {"chunk_id": "chunk_2", "content": "向量搜索结果#2"},
        #     {"chunk_id": "chunk_3", "content": "向量搜索结果#3"}
        # ]
        # embedding_search_list = []
        # for doc in embedding_chunks_list:
        #     if isinstance(doc, dict):
        #         embedding_search_list.append(doc.get("entity"))

        # [BUG修复] 追加 doc.get("entity") 非空过滤：
        # Milvus 返回结果缺少 entity 字段时 doc.get("entity") 为 None，
        # 若不过滤，下游 _rrf_merge 中 doc.get("chunk_id") 会对 None 调用 .get() 抛出 AttributeError
        # [BUG修复 2] hybrid_search 返回的是 pymilvus 的 Hit 对象（非 dict），
        # 原 isinstance(doc, dict) 过滤会把所有结果滤空，导致下游"无参考内容"。
        # 统一兼容：dict 直接取 entity；Hit 对象走 .entity 属性 / to_dict() 兜底
        def _extract_entity(doc):
            if isinstance(doc, dict):
                entity = doc.get("entity") or doc
            else:
                # 注意：Hit.entity 属性返回的还是 Hit 对象（自嵌套陷阱）！
                # 必须用 doc["entity"]（__getitem__）拿真正的字段 dict，to_dict 兜底
                try:
                    entity = doc["entity"]
                except Exception:
                    try:
                        entity = doc.to_dict().get("entity")
                    except Exception:
                        entity = None
            # 兜底：若仍是 {'chunk_id':..,'distance':..,'entity':{...}} 包装层则再剥一层
            if isinstance(entity, dict) and isinstance(entity.get("entity"), dict):
                entity = entity["entity"]
            return entity or None

        embedding_search_list = [e for e in (_extract_entity(doc) for doc in embedding_chunks_list) if e]
        hyde_embedding_search_list = [e for e in (_extract_entity(doc) for doc in hyde_embedding_chunks_list) if e]

        # rrf_inputs = [
        #     {"list":embedding_search_list, "weight":1.0},
        #     {"list":hyde_embedding_search_list, "weight":1.0}
        # ]

        # 3. 构建融合前的数据结构
        # [检索开关面板] embedding 路相对 hyde 路的融合权重（默认 1.0 = 经典 RRF，两路等权）
        rrf_weight = float(state.get("rrf_weight") or 1.0)
        rrf_inputs = [
            (embedding_search_list, rrf_weight),# embedding 路权重（可配）
            (hyde_embedding_search_list, 1.0)
        ]

        # 4. 根据RRF公式进行数据的融合
        # [检索开关面板] K 与截断数支持从 state 透传（默认 K=60, top3）
        rrf_k = int(state.get("rrf_k") or 60)
        top_k = int(state.get("search_top_k") or 3)
        rrf_merge_results = self._rrf_merge(rrf_inputs, k=rrf_k, max_results=top_k)

        # 5. 将分数清洗出去（下一个步骤重新排序，不参考这个步骤的分数）
        rrf_chunks = [doc for doc, _ in rrf_merge_results]

        # [BUG修复 4] 输出统一扁平化：Milvus Hit 的结果仍是 {'chunk_id','distance','entity':{...}} 包装层，
        # 下游 rerank/answer 都按顶层字段取 content/title，不剥开会全部取到 None（表现为"无参考内容"）
        flat_chunks = []
        for doc in rrf_chunks:
            if isinstance(doc, dict) and isinstance(doc.get("entity"), dict):
                d = dict(doc["entity"])
                if doc.get("distance") is not None:
                    d.setdefault("distance", doc["distance"])
                flat_chunks.append(d)
            else:
                flat_chunks.append(doc)
        rrf_chunks = flat_chunks

        state["rrf_chunks"] = rrf_chunks
        add_done_task(state.get("session_id"), self.name, state.get("is_stream"))
        return state

    def _rrf_merge(self, rrf_inputs, k: int = 60, max_results: int = None) -> List[Tuple[Dict, float]]:
        """
        RRF (Reciprocal Rank Fusion) 融合算法
        :param rrf_inputs: 输入数据列表，每个元素是一个元组，包含一个列表和一个权重
        :param k: RRF中的平滑参数
        :param max_results: 最大结果数量限制，如果是None，则获取所有结果
        :return: 融合后的结果列表 [({},得分),({},得分),...]
        """

        # 1. 遍历需要融合的数据，并组装数据
        chunk_data = {}  # 最后数据
        chunk_scores = {} # 综合得分
        for rrf_input, weight in rrf_inputs:
            # 对当前这一路计算
            for rank, doc in enumerate(rrf_input, start=1):
                chunk_id = doc.get("chunk_id")# 拿片段 名称
                # [BUG修复] 防御 chunk_id 为 None 的情况，使用 id(doc) 生成唯一标识
                # 避免多个缺失 chunk_id 的文档共用 None 作为字典 key，导致 RRF 分数错误累加和内容覆盖
                safe_id = chunk_id if chunk_id is not None else f"_no_id_{id(doc)}"
                # 计算每个文档的得分，��将得分组织在list中
                chunk_scores[safe_id] = chunk_scores.get(safe_id, 0.0) + weight / (k + rank)
                # 将所有文档组织在list中
                chunk_data.setdefault(# setdefault 如果这个键不存在就创建 吧综合得分写进去 如果存在就忽略 片段名 和内容
                    safe_id,
                    doc
                )# 只保留第一次遇到的那个文档实体，不会覆盖。"如果 chunk_id 这个键还不存在，就把它存进字典，值设为 doc；如果已经存在了，就什么也不做。"

        # 2. 将得分和文档组装在一起
        unsorted_results = [(chunk_data[chunk_id], score) for chunk_id, score in chunk_scores.items()]

        # 3. 按照得分降序排列
        sorted_results = sorted(unsorted_results, key=lambda x:x[1], reverse=True)
        # topN  取前几条
        return sorted_results[:max_results] if max_results else sorted_results

if __name__ == '__main__':

    # 模拟两路检索结果
    mock_state = {
        # 直接硬搜得到相似度高的片段
        "embedding_chunks": [
            {"entity": {"chunk_id": "chunk_1", "content": "向量搜索结果#1"}},
            {"entity": {"chunk_id": "chunk_2", "content": "向量搜索结果#2"}},
            {"entity": {"chunk_id": "chunk_3", "content": "向量搜索结果#3"}},
        ],
        # 通过假设性文档搜到的相似度高的片段   比如 chunk_1 两路排名里面都高
        "hyde_embedding_chunks": [
            {"entity": {"chunk_id": "chunk_1", "content": "HyDE搜索结果#1"}},
            {"entity": {"chunk_id": "chunk_4", "content": "HyDE搜索结果#2"}},
            {"entity": {"chunk_id": "chunk_2", "content": "HyDE搜索结果#3"}},
        ]
    }

    node_rrf = NodeRrf()# __call__  process
    result = node_rrf(mock_state)
    logger.info(serialize_json(result, indent=4))# 打印日志