# utils/graph_utils.py
"""
知识图谱：从 Milvus 文档切片中用 LLM 抽取实体关系三元组，存 Mongo kb_graph。
- build_graph_for_doc(user_id, file_title)：拉取该文档切片 → 分批抽三元组 → 落库
- get_graph(user_id, file_title)：合并三元组为 nodes/edges，供 ECharts 渲染
"""
import json
import re

from tool.logger import logger

EXTRACT_PROMPT = """你是知识图谱构建专家。请从下面的设备手册文本片段中抽取实体关系三元组。

要求：
1. 实体是具体名词（设备/部件/参数/操作/故障等），关系用简短中文动词短语（如"位于""默认为""用于""导致""属于"）
2. 只输出 JSON 数组，不要任何其他文字，格式：
[{"head":"实体1","relation":"关系","tail":"实体2"}]
3. 最多提取 12 条，没有可提取的就输出 []

【文本片段】
{texts}"""


def _col():
    from utils.auth_utils import get_auth_tool
    return get_auth_tool().db["kb_graph"]


def extract_triples(texts: list, max_triples_per_batch: int = 12) -> list:
    """分批（每批5段）调用 LLM 抽三元组，解析失败跳过该批"""
    from utils.llm_utils import get_llm_client
    llm = get_llm_client()
    triples = []
    for i in range(0, len(texts), 5):
        batch = [t[:600] for t in texts[i:i + 5]]
        prompt = EXTRACT_PROMPT.replace("{texts}", "\n---\n".join(f"[{j+1}] {t}" for j, t in enumerate(batch)))
        try:
            resp = llm.invoke(prompt)
            m = re.search(r"\[.*\]", resp.content or "", re.S)
            if not m:
                continue
            arr = json.loads(m.group(0))
            for t in arr if isinstance(arr, list) else []:
                head = str(t.get("head", "")).strip()
                rel = str(t.get("relation", "")).strip()
                tail = str(t.get("tail", "")).strip()
                if head and rel and tail and head != tail:
                    triples.append({"head": head[:30], "relation": rel[:20], "tail": tail[:30]})
                if len(triples) >= 120:
                    break
        except Exception as e:
            logger.warning(f"[知识图谱] 批次 {i//5} 抽取失败（跳过）: {e}")
            continue
    return triples


def build_graph_for_doc(user_id: str, file_title: str, top_chunks: int = 20) -> dict:
    """对单个文档构建图谱并落库（同用户同文档覆盖更新）"""
    from utils.milvus_utils import get_milvus_client, escape_milvus_string
    from config.milvus_config import milvus_config
    import time as _time
    client = get_milvus_client()
    rows = client.query(
        collection_name=milvus_config.chunks_collection,
        filter=f'file_title == "{escape_milvus_string(file_title)}"',
        output_fields=["content"],
        limit=top_chunks,
    )
    texts = [(r.get("content") or "").strip() for r in rows if isinstance(r, dict)]
    texts = [t for t in texts if t]
    if not texts:
        return {"ok": False, "message": f"Milvus 中未找到文档《{file_title}》的切片"}
    triples = extract_triples(texts)
    _col().update_one(
        {"user_id": user_id, "file_title": file_title},
        {"$set": {"triples": triples, "chunk_count": len(texts),
                  "ts": _time.time(), "title": file_title}},
        upsert=True
    )
    return {"ok": True, "triples": len(triples), "chunks": len(texts)}


def get_graph(user_id: str, file_title: str = None, max_nodes: int = 40) -> dict:
    """读取图谱并合并为 ECharts 需要的 nodes/edges（按度数取前 max_nodes 个节点）"""
    cond = {"user_id": user_id}
    if file_title:
        cond["file_title"] = file_title
    docs = list(_col().find(cond, {"triples": 1, "file_title": 1, "ts": 1}).sort("ts", -1).limit(5))
    degree = {}
    edges = []
    seen_edge = set()
    for d in docs:
        for t in d.get("triples") or []:
            key = (t["head"], t["relation"], t["tail"])
            if key in seen_edge:
                continue
            seen_edge.add(key)
            edges.append({"source": t["head"], "target": t["tail"], "relation": t["relation"]})
            degree[t["head"]] = degree.get(t["head"], 0) + 1
            degree[t["tail"]] = degree.get(t["tail"], 0) + 1
    top_nodes = sorted(degree, key=degree.get, reverse=True)[:max_nodes]
    node_set = set(top_nodes)
    nodes = [{"name": n, "symbolSize": min(18 + degree[n] * 4, 60)} for n in top_nodes]
    edges = [e for e in edges if e["source"] in node_set and e["target"] in node_set]
    return {"nodes": nodes, "edges": edges, "built_from": [d.get("file_title") for d in docs]}
