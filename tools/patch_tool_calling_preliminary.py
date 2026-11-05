from __future__ import annotations

import copy
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from lxml import etree


SOURCE = Path(r"D:\研究生\毕业论文\小论文\论文写作\初稿.docx")
BACKUP = SOURCE.with_name("初稿_修改前备份_20261023.docx")
OUTPUT = SOURCE.with_name("初稿_工具调用补充.docx")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {"w": W_NS}


PARAGRAPHS = [
    "工具调用（tool calling，也称函数调用）是大语言模型与外部程序交互的一种结构化机制。模型在上下文中接收可用工具及其接口说明，根据当前任务判断是否调用工具、选择工具并生成参数；真正的函数执行由模型之外的宿主程序完成，执行结果再作为新的上下文返回模型。由此，模型可以将自然语言推理与可验证的程序操作交替进行，而不必在文本中模拟外部操作[45-46]。",
    "一个工具接口通常由工具名称、功能描述和参数模式组成，参数模式明确字段名称、数据类型、必填项与取值约束。一次完整调用包括工具选择、结构化参数生成、宿主程序解析与校验、函数执行、结果回传以及后续决策。结构化模式只能约束调用的语法和接口边界，不能保证参数在具体业务状态下必然合法，因此宿主程序仍需检查目标对象是否存在、前置条件是否满足以及调用后状态是否符合领域约束。",
    "在本文框架中，工具调用主要用于执行已经定位的结构修复。Repair Agent只能从预先注册的类型化编辑函数中选择操作，并使用候选图中的真实元素ID生成参数；宿主程序在候选图副本上按顺序执行调用，并将错误信息或更新后的图状态返回。该机制与Generate Agent一次性输出声明式Graph Patch不同：Graph Patch用于表达一个完整的局部流程增量，类型化工具调用则用于对确定位置实施细粒度、可追踪的新增、更新或删除。所有调用完成后仍需重新进行确定性结构校验，工具返回成功不等同于流程整体正确。",
]

REFERENCES = [
    "[45] SCHICK T, DWIVEDI-YU J, DESSÌ R, et al. Toolformer: Language Models Can Teach Themselves to Use Tools[C]//Advances in Neural Information Processing Systems. 2023, 36: 68539-68551.",
    "[46] YAO S, ZHAO J, YU D, et al. ReAct: Synergizing Reasoning and Acting in Language Models[C]//The Eleventh International Conference on Learning Representations. 2023.",
]


def paragraph_text(paragraph: etree._Element) -> str:
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))


def paragraph_from_template(template: etree._Element, text: str) -> etree._Element:
    paragraph = etree.Element(f"{{{W_NS}}}p", nsmap=template.nsmap)
    p_pr = template.find(f"{{{W_NS}}}pPr")
    if p_pr is not None:
        paragraph.append(copy.deepcopy(p_pr))

    run = etree.SubElement(paragraph, f"{{{W_NS}}}r")
    template_run = template.find(f"{{{W_NS}}}r")
    if template_run is not None:
        r_pr = template_run.find(f"{{{W_NS}}}rPr")
        if r_pr is not None:
            run.append(copy.deepcopy(r_pr))

    text_node = etree.SubElement(run, f"{{{W_NS}}}t")
    if text.startswith(" ") or text.endswith(" "):
        text_node.set(f"{{{XML_NS}}}space", "preserve")
    text_node.text = text
    return paragraph


def find_paragraph(root: etree._Element, exact_text: str) -> etree._Element:
    for paragraph in root.xpath("//w:body/w:p", namespaces=NS):
        if paragraph_text(paragraph) == exact_text:
            return paragraph
    raise RuntimeError(f"未找到目标段落：{exact_text}")


def patch_document_xml(xml_bytes: bytes) -> tuple[bytes, bool]:
    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(xml_bytes, parser)
    all_text = "\n".join(paragraph_text(p) for p in root.xpath("//w:body/w:p", namespaces=NS))

    changed = False
    heading = find_paragraph(root, "3.1.3 工具调用")
    body_template = find_paragraph(root, "本文采用统一的结构化提示词架构组织各类 Agent 的提示词。现有研究和主流模型厂商尚未形成普遍接受的固定 Prompt 组织标准，但相关指南普遍强调角色与目标、上下文、行为约束、输出格式和示例等要素[34-38]。统一提示框架的目的不是让不同 Agent 使用相同内容，而是使其职责、输入、知识边界和输出接口能够被明确区分。")

    if PARAGRAPHS[0] not in all_text:
        cursor = heading
        for text in PARAGRAPHS:
            new_paragraph = paragraph_from_template(body_template, text)
            cursor.addnext(new_paragraph)
            cursor = new_paragraph
        changed = True

    reference_template = find_paragraph(root, "[44] MUNOZ-GAMA J. Conformance Checking and Diagnosis in Process Mining: Comparing Observed and Modeled Processes[M]. Cham: Springer, 2016. DOI:10.1007/978-3-319-49451-7.")
    if REFERENCES[0] not in all_text:
        cursor = reference_template
        for text in REFERENCES:
            new_paragraph = paragraph_from_template(reference_template, text)
            cursor.addnext(new_paragraph)
            cursor = new_paragraph
        changed = True

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True), changed


def main() -> None:
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    if not BACKUP.exists():
        shutil.copy2(SOURCE, BACKUP)

    with zipfile.ZipFile(SOURCE, "r") as source_zip:
        document_xml = source_zip.read("word/document.xml")
        patched_xml, changed = patch_document_xml(document_xml)
        if not changed:
            print("目标内容已存在，未重复写入。")
            return

        fd, temp_name = tempfile.mkstemp(suffix=".docx", dir=str(SOURCE.parent))
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with zipfile.ZipFile(temp_path, "w") as target_zip:
                for item in source_zip.infolist():
                    data = patched_xml if item.filename == "word/document.xml" else source_zip.read(item.filename)
                    target_zip.writestr(item, data)
            try:
                os.replace(temp_path, SOURCE)
                output_path = SOURCE
            except PermissionError:
                if OUTPUT.exists():
                    OUTPUT.unlink()
                os.replace(temp_path, OUTPUT)
                output_path = OUTPUT
        finally:
            if temp_path.exists():
                temp_path.unlink()

    print(f"已更新：{output_path}")
    print(f"备份：{BACKUP}")


if __name__ == "__main__":
    main()
