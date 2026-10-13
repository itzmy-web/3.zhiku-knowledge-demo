# processor/import_processor/nodes/node_office_to_md.py
"""
Office 文档（docx/pptx）转 Markdown 节点 —— 零依赖实现。

原理：docx/pptx 本质是 ZIP 压缩包，内部是 OOXML（XML）文件：
- docx: word/document.xml，正文在 <w:p> 段落的 <w:t> 文本片段里
- pptx: ppt/slides/slideN.xml 每页的 <a:t> 文本片段，备注在 notesSlideN.xml

只抽文本（含标题级别近似），输出 Markdown 到 file_dir，下游切分/向量化管线零改动。
"""
import json
import logging
import re
import zipfile
from pathlib import Path

from processor.import_processor.base import BaseNode, setup_logging
from processor.import_processor.exceptions import StateFieldError, FileProcessingError
from processor.import_processor.state import ImportGraphState


class NodeOfficeToMD(BaseNode):
    """docx/pptx → Markdown 文本抽取（零外部依赖）"""

    name = "node_office_to_md"

    # XML 命名空间无关的文本提取：抓所有 <w:t> / <a:t> 标签内容
    _W_T = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)
    _A_T = re.compile(r"<a:t(?:\s[^>]*)?>(.*?)</a:t>", re.S)
    _PARA = re.compile(r"</w:p>")
    _SHAPE = re.compile(r"</p:sp>|</p:graphicFrame>")
    _XML_ESC = {"&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"', "&apos;": "'"}

    def process(self, state: ImportGraphState):
        office_path_obj, output_dir_obj = self._step_1_validate(state)
        suffix = office_path_obj.suffix.lower()

        if suffix == ".docx":
            md_content = self._docx_to_md(office_path_obj)
        elif suffix == ".pptx":
            md_content = self._pptx_to_md(office_path_obj)
        else:
            raise FileProcessingError(f"不支持的 Office 类型：{suffix}")

        if not md_content.strip():
            raise FileProcessingError(f"{office_path_obj.name} 未解析出任何文本内容")

        # 落盘 Markdown，与 PDF 转换后的产物保持同构（md_content + md_path）
        md_path = output_dir_obj / f"{office_path_obj.stem}.md"
        md_path.write_text(md_content, encoding="utf-8")

        self.log_step("Office解析", f"{office_path_obj.name} → {len(md_content)} 字符")
        state["md_content"] = md_content
        state["md_path"] = str(md_path.absolute())
        return state

    # ---------- 步骤1：路径校验 ----------
    def _step_1_validate(self, state: ImportGraphState):
        office_path = state.get("office_path")
        if not office_path:
            raise StateFieldError(field_name="office_path", message="Office 文档路径不能为空", expected_type=str)
        file_dir = state.get("file_dir")
        if not file_dir:
            raise StateFieldError(field_name="file_dir", message="输出路径不能为空", expected_type=str)

        office_path_obj = Path(office_path)
        if not office_path_obj.exists():
            raise FileProcessingError(message=f"文件{office_path_obj.name}不存在")

        output_dir_obj = Path(file_dir)
        output_dir_obj.mkdir(parents=True, exist_ok=True)
        return office_path_obj, output_dir_obj

    # ---------- 工具：XML 转义还原 ----------
    def _unescape(self, text: str) -> str:
        for k, v in self._XML_ESC.items():
            text = text.replace(k, v)
        return text

    # ---------- docx → md ----------
    def _docx_to_md(self, path: Path) -> str:
        with zipfile.ZipFile(path, "r") as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")

        lines = []
        # 按 </w:p> 切段，每段内拼接所有 <w:t>
        for para in self._PARA.split(xml):
            runs = self._W_T.findall(para)
            if not runs:
                continue
            text = self._unescape("".join(runs)).strip()
            if not text:
                continue
            # 近似标题：Word 内置样式 Heading 出现在段落属性里（pStyle w:val="Heading1"）
            m = re.search(r'w:pStyle\s+w:val="(Heading|heading)\s*(\d)"', para)
            if m:
                level = min(int(m.group(2)), 4)
                lines.append("#" * level + " " + text)
            elif text.startswith(("第", "Chapter")) and len(text) < 40 and not text.endswith("。"):
                lines.append("## " + text)   # 无样式时的兜底标题猜测
            else:
                lines.append(text)
        return "\n\n".join(lines)

    # ---------- pptx → md ----------
    def _pptx_to_md(self, path: Path) -> str:
        lines = []
        with zipfile.ZipFile(path, "r") as z:
            slides = sorted(
                [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)],
                key=lambda n: int(re.search(r"slide(\d+)\.xml", n).group(1))
            )
            for i, name in enumerate(slides, start=1):
                xml = z.read(name).decode("utf-8", errors="ignore")
                texts = [self._unescape(t).strip() for t in self._A_T.findall(xml)]
                texts = [t for t in texts if t]
                if texts:
                    lines.append(f"## 第 {i} 页")
                    # 第一段通常是标题
                    lines.append("**" + texts[0] + "**" if len(texts) > 1 else texts[0])
                    lines.extend(texts[1:])
                # 演讲备注（可选）
                notes_name = name.replace("slides/slide", "notesSlides/notesSlide")
                if notes_name in z.namelist():
                    nxml = z.read(notes_name).decode("utf-8", errors="ignore")
                    ntexts = [self._unescape(t).strip() for t in self._A_T.findall(nxml)]
                    ntexts = [t for t in ntexts if t and not t.isdigit()]
                    if ntexts:
                        lines.append("> 备注：" + " ".join(ntexts))
                lines.append("")
        return "\n".join(lines)


if __name__ == "__main__":
    setup_logging()
    init_state = {
        "office_path": r"D:\test\示例文档.docx",
        "file_dir": r"D:\output",
    }
    node = NodeOfficeToMD()
    result = node(init_state)
    logging.getLogger().info(json.dumps({k: str(v)[:200] for k, v in result.items()},
                                        ensure_ascii=False, indent=2))
