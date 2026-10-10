"""Editable architecture comparison for the audited finance-agent progression."""
from pathlib import Path
import argparse
import subprocess
import xml.etree.ElementTree as ET

root_path = Path(__file__).resolve().parents[1]
output = root_path / "docs/research"
output.mkdir(parents=True, exist_ok=True)
file = ET.Element("mxfile", host="drawio", version="31.1.8")
diagram = ET.SubElement(file, "diagram", name="原型到第二版金融Agent")
model = ET.SubElement(diagram, "mxGraphModel", dx="1680", dy="1080", grid="1", gridSize="10", page="0", background="#ffffff")
graph = ET.SubElement(model, "root")
ET.SubElement(graph, "mxCell", id="0")
ET.SubElement(graph, "mxCell", id="1", parent="0")
next_id = 2


def box(text, x, y, w, h, *, parent="1", fill="#ffffff", stroke="#94a3b8", lane=False):
    global next_id
    identity, next_id = str(next_id), next_id + 1
    style = f"rounded=1;whiteSpace=wrap;html=1;fontFamily=Microsoft YaHei;fontSize=17;fillColor={fill};strokeColor={stroke};strokeWidth=1.5;"
    if lane:
        style += "swimlane;startSize=40;container=1;pointerEvents=0;fontStyle=1;align=left;spacingLeft=15;"
    cell = ET.SubElement(graph, "mxCell", id=identity, value=text.replace("\n", "<br>"), style=style, vertex="1", parent=parent)
    ET.SubElement(cell, "mxGeometry", x=str(x), y=str(y), width=str(w), height=str(h), **{"as": "geometry"})
    return identity


def edge(source, target, *, parent, text="", ports=(1, .5, 0, .5), color="#507DB4", points=(), dashed=False):
    global next_id
    identity, next_id = str(next_id), next_id + 1
    ex, ey, ix, iy = ports
    style = (f"edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;fontFamily=Microsoft YaHei;fontSize=14;"
             f"strokeColor={color};strokeWidth=2;endArrow=block;labelBackgroundColor=#ffffff;exitX={ex};exitY={ey};exitDx=0;exitDy=0;entryX={ix};entryY={iy};entryDx=0;entryDy=0;")
    if dashed:
        style += "dashed=1;"
    cell = ET.SubElement(graph, "mxCell", id=identity, value=text, style=style, edge="1", parent=parent, source=source, target=target)
    geometry = ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
    if points:
        array = ET.SubElement(geometry, "Array", **{"as": "points"})
        for x, y in points:
            ET.SubElement(array, "mxPoint", x=str(x), y=str(y))


box("原始 my_agent：LangGraph 工作流 + RAG + Oracle 记忆 + MCP + OpenSandbox\n金融第一版沿用该基础；原始原型没有同 50 题可比得分", 40, 20, 1540, 80, fill="#f1f5f9", stroke="#94a3b8")
old = box("金融第一版：35/50 · 通用图工作流 + 金融 Profile", 40, 140, 1540, 350, fill="#f4f8ff", stroke="#507DB4", lane=True)
planner = box("Planner\n单独生成任务计划", 40, 70, 280, 90, parent=old, fill="#dae8fc", stroke="#507DB4")
executor = box("Executor\n决定下一步代码 / 完成", 620, 70, 280, 90, parent=old, fill="#dae8fc", stroke="#507DB4")
finalizer = box("Finalizer\n再次生成答案 + FinQA DSL\n严格解析 / 程序一致性", 1200, 70, 280, 110, parent=old, fill="#fff2cc", stroke="#d6b656")
terminal = box("Python / Docker / SDK\n读取、检索、计算\n结果返回当前上下文", 620, 230, 280, 90, parent=old, fill="#ffffff", stroke="#507DB4")
edge(planner, executor, parent=old, text="计划")
edge(executor, finalizer, parent=old, text="done → 输出转写")
edge(executor, terminal, parent=old, text="执行代码", ports=(.3, 1, .3, 0))
edge(terminal, executor, parent=old, text="观察", ports=(.7, 0, .7, 1))

new = box("金融第二版：42/50 · 单一动作循环 + 宿主回执校验", 40, 550, 1540, 350, fill="#f4fbf7", stroke="#31846D", lane=True)
skill = box("Skill 按需加载\n本 50 题：financial-ratios\n方法约束，不授予工具权限", 40, 70, 280, 100, parent=new, fill="#d5e8d4", stroke="#31846D")
loop = box("单一动作循环\nskill / tool / finish\n当前文档范围 + 模型决策", 620, 70, 280, 100, parent=new, fill="#d5e8d4", stroke="#31846D")
finish = box("finish → 确定性校验\n观察过的证据 + 计算回执\n数值一致后交付", 1200, 70, 280, 110, parent=new, fill="#d5e8d4", stroke="#31846D")
archive = box("有界上下文 + 原始归档\n长观察保留完整记录\ncontext_read 按需回读", 40, 230, 280, 90, parent=new, fill="#ffffff", stroke="#31846D")
tools = box("结构化财务工具\n读表 / 正文 / 检索 / 计算\n宿主保存证据与计算 ID", 620, 230, 280, 90, parent=new, fill="#ffffff", stroke="#31846D")
edge(skill, loop, parent=new, text="固定方法", color="#31846D")
edge(loop, finish, parent=new, text="引用已有回执", color="#31846D")
edge(loop, tools, parent=new, text="类型化参数", ports=(.3, 1, .3, 0), color="#31846D")
edge(tools, loop, parent=new, text="可信结构化结果", ports=(.7, 0, .7, 1), color="#31846D")
edge(archive, loop, parent=new, text="回读", ports=(1, .5, 0, .8), color="#31846D", points=((470, 275), (470, 150)), dashed=True)

box("扩展能力：持久会话记忆、Docker 终端、MCP 连接器、失败→候选 Skill→验证→晋升\n这些能力已有其他验收记录；本 50 题没有使用跨题记忆、终端、MCP 或学习生成的 Skill，不能据此归因增分", 40, 950, 1540, 90, fill="#f1f5f9", stroke="#94a3b8")
path = output / "progression-architecture.drawio"
ET.indent(file)
ET.ElementTree(file).write(path, encoding="utf-8", xml_declaration=True)
print(path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", action="store_true", help="Also try native PNG/SVG export; requires a working Electron runtime")
    args = parser.parse_args()
    if not args.export:
        raise SystemExit(0)
    binary = Path("C:/Users/William/AppData/Local/Programs/draw.io/draw.io.exe")
    if not binary.exists():
        raise SystemExit("Editable XML generated; draw.io desktop is unavailable for local export")
    for extension, flags in (("png", ["--width", "2000"]), ("svg", ["-e"])):
        result = subprocess.run([str(binary), "-x", "-f", extension, *flags, "-b", "20", "-o",
                                 str(output / f"progression-architecture.{extension}"), str(path), "--disable-gpu"],
                                capture_output=True, text=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(f"draw.io export failed: {result.stderr[:300]}")
        print(result.stdout.strip())
