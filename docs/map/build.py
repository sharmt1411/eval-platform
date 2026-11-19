# -*- coding: utf-8 -*-
"""
系统地图构建与校验脚本 (docs/map/build.py)
仅使用 Python 标准库，读写文件显式指定 encoding="utf-8"。
"""
import argparse
import colorsys
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

MAP_DIR = pathlib.Path(__file__).resolve().parent
if str(MAP_DIR) not in sys.path:
    sys.path.insert(0, str(MAP_DIR))

from fact_extractor import FACTS_JSON, FACTS_MD, extract_and_save_facts, verify_maps_against_facts  # noqa: E402
PROJECT_ROOT = MAP_DIR.parent.parent
MERMAID_JS = PROJECT_ROOT / "frontend" / "node_modules" / "mermaid" / "dist" / "mermaid.min.js"
INDEX_HTML = MAP_DIR / "index.html"
CHANGES_DIR = MAP_DIR / "changes"

NODE_ID_RE = re.compile(r"^[a-z0-9_]+$")
TITLE_RE = re.compile(r"^%%\s*title:\s*(.+?)\s*$")
LINK_RE = re.compile(r"^%%\s*link:\s*([a-z0-9_]+)\s*->\s*(\S+)\s*$")
CLASSDEF_RE = re.compile(r"^\s*classDef\s+(added|changed|removed)\b", re.IGNORECASE)

# 匹配 flowchart / graph 中的节点定义（支持 [], [()], ([]), {{}}, (()), {} 以及 :::class）
FLOW_NODE_RE = re.compile(
    r'(?:^|[\s;|&>])([a-zA-Z0-9_]+)\s*'
    r'(?:\[\("([^"]*)"\)\]|'
    r'\(\["([^"]*)"\]\)|'
    r'\{\{"([^"]*)"\}\}|'
    r'\(\("([^"]*)"\)\)|'
    r'\["([^"]*)"\]|'
    r'\("([^"]*)"\)|'
    r'\{"([^"]*)"\})'
    r'(?:\s*:::\s*([a-zA-Z0-9_-]+))?'
)

NODE_BRACKET_STRIP_RE = re.compile(
    r'\[\("[^"]*"\)\]|'
    r'\(\["[^"]*"\]\)|'
    r'\{\{"[^"]*"\}\}|'
    r'\(\("[^"]*"\)\)|'
    r'\["[^"]*"\]|'
    r'\("[^"]*"\)|'
    r'\{"[^"]*"\}'
)

FLOW_EDGE_RE = re.compile(
    r'(?:<-->|<-\.->|--+>|-\.+->|==+>|---+|-\.-+)\s*\|\s*"?([^"|]*?)"?\s*\||'
    r'--\s*"([^"]*)"\s*--+>|'
    r'-\.\s*"([^"]*)"\s*\.+->|'
    r'(?:<-->|<-\.->|--+>|-\.+->|==+>|---+|-\.-+)'
)

SEQ_PARTICIPANT_RE = re.compile(
    r'^\s*(?:participant|actor)\s+([a-zA-Z0-9_]+)(?:\s+as\s+"?([^"\r\n]+)"?)?\s*$'
)
SEQ_MSG_RE = re.compile(
    r'^\s*[a-zA-Z0-9_]+\s*(?:->>|-->>|->|-->|-x|--x|-\)|--\))\s*[a-zA-Z0-9_]+\s*:'
)

INJECTED_CLASSDEFS = """
    classDef added fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px,color:#1b5e20;
    classDef changed fill:#fffde7,stroke:#f9a825,stroke-width:2px,color:#f57f17;
    classDef removed fill:#ffebee,stroke:#c62828,stroke-width:2px,stroke-dasharray: 5 5,color:#b71c1c;
""".strip("\n")


def split_label_lines(label: str) -> List[str]:
    text = label.replace("\\n", "\n")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    return [line.strip() for line in text.split("\n") if line.strip()]


def extract_rel_path(path_spec: str) -> str:
    """从 `path` 或 `path:symbol` / `path:line` 提取文件或目录相对路径。"""
    spec = path_spec.strip()
    if ":" in spec:
        return spec.split(":", 1)[0].strip()
    return spec


def detect_diagram_type(body_lines: List[str]) -> str:
    for line in body_lines:
        s = line.strip()
        if not s or s.startswith("%%"):
            continue
        low = s.lower()
        if low.startswith("sequencediagram"):
            return "sequence"
        if low.startswith("erdiagram"):
            return "er"
        if low.startswith("statediagram"):
            return "state"
        if low.startswith("flowchart") or low.startswith("graph"):
            return "flowchart"
    return "unknown"


def parse_flowchart_nodes(body_lines: List[str]) -> List[Dict]:
    nodes: Dict[str, Dict] = {}
    for line in body_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("%%") or stripped.startswith("subgraph") or stripped == "end":
            continue
        if stripped.startswith(("classDef ", "linkStyle ", "style ", "class ")):
            continue
        for m in FLOW_NODE_RE.finditer(line):
            nid = m.group(1)
            label = next((g for g in m.groups()[1:8] if g is not None), "")
            cls = m.group(8) or ""
            lines = split_label_lines(label)
            if nid not in nodes:
                nodes[nid] = {
                    "id": nid,
                    "label": label,
                    "lines": lines,
                    "class": cls,
                }
            elif cls and not nodes[nid]["class"]:
                nodes[nid]["class"] = cls
    return list(nodes.values())


def calc_label_width(text: str) -> int:
    """计算连线标签宽度：汉字算 2，其余字符算 1。"""
    return sum(2 if "\u4e00" <= ch <= "\u9fff" else 1 for ch in text)


def parse_flowchart_edges(body_lines: List[str]) -> List[str]:
    edges: List[str] = []
    for line in body_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("%%") or stripped.startswith("subgraph") or stripped == "end":
            continue
        if stripped.lower().startswith(("flowchart ", "graph ", "direction ")):
            continue
        if stripped.startswith(("classDef ", "linkStyle ", "style ", "class ")):
            continue
        cleaned = NODE_BRACKET_STRIP_RE.sub("", line)
        for m in FLOW_EDGE_RE.finditer(cleaned):
            lbl = next((g for g in m.groups() if g is not None), "").strip()
            edges.append(lbl)
    return edges


def parse_sequence_diagram(body_lines: List[str]) -> Tuple[List[Dict], int]:
    participants: List[Dict] = []
    seen = set()
    msg_count = 0
    for line in body_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("%%"):
            continue
        pm = SEQ_PARTICIPANT_RE.match(stripped)
        if pm:
            pid = pm.group(1)
            raw_label = pm.group(2) or pid
            lines = split_label_lines(raw_label)
            if pid not in seen:
                seen.add(pid)
                participants.append({
                    "id": pid,
                    "label": raw_label,
                    "lines": lines,
                    "name": lines[0] if lines else pid,
                })
            continue
        if SEQ_MSG_RE.match(stripped):
            msg_count += 1
    return participants, msg_count


def parse_er_entities(body_lines: List[str]) -> List[str]:
    entities = set()
    for line in body_lines:
        s = line.strip()
        if not s or s.startswith("%%") or s.lower().startswith("erdiagram"):
            continue
        m_block = re.match(r"^([a-zA-Z0-9_]+)\s*\{", s)
        if m_block:
            entities.add(m_block.group(1))
            continue
        m_rel = re.match(r"^([a-zA-Z0-9_]+)\s+[|o}{]+--[|o}{]+\s+([a-zA-Z0-9_]+)\s*:", s)
        if m_rel:
            entities.add(m_rel.group(1))
            entities.add(m_rel.group(2))
    return sorted(entities)


def parse_state_nodes(body_lines: List[str]) -> List[str]:
    states = set()
    for line in body_lines:
        s = line.strip()
        if not s or s.startswith("%%") or s.lower().startswith("statediagram"):
            continue
        if s.startswith(("note ", "end note", "}", "direction ")):
            continue
        m_trans = re.match(r"^(\[\*\]|[a-zA-Z0-9_]+)\s*-->\s*(\[\*\]|[a-zA-Z0-9_]+)", s)
        if m_trans:
            for st in (m_trans.group(1), m_trans.group(2)):
                if st != "[*]":
                    states.add(st)
            continue
        m_desc = re.match(r"^([a-zA-Z0-9_]+)\s*:\s*", s)
        if m_desc:
            states.add(m_desc.group(1))
            continue
        m_state = re.match(r'^state\s+(?:"[^"]+"\s+as\s+)?([a-zA-Z0-9_]+)', s)
        if m_state:
            states.add(m_state.group(1))
    return sorted(states)


def collect_mmd_files() -> List[pathlib.Path]:
    files: List[pathlib.Path] = []
    if MAP_DIR.exists():
        files.extend(sorted(p for p in MAP_DIR.glob("*.mmd") if p.is_file()))
    if CHANGES_DIR.exists():
        files.extend(sorted(p for p in CHANGES_DIR.glob("*.mmd") if p.is_file()))
    return files


def classify_diagram(rel_name: str) -> str:
    if rel_name.startswith("changes/"):
        return "changes"
    if rel_name.startswith("L1-"):
        return "L1"
    if rel_name.startswith("L2-"):
        return "L2"
    if rel_name.startswith("L3-"):
        return "L3"
    if rel_name.startswith("L4-"):
        return "L4"
    return "L4"


def prepare_mermaid_source(raw_text: str, diagram_type: str) -> str:
    if diagram_type == "flowchart":
        return raw_text.rstrip() + "\n" + INJECTED_CLASSDEFS + "\n"
    return raw_text


def validate_and_load(allow_empty: bool = False) -> Tuple[List[Dict], List[str]]:
    errors: List[str] = []
    files = collect_mmd_files()
    if not files:
        if allow_empty:
            print("提示: 当前尚无 .mmd 图文件，仅生成基于 facts.json 的自动代码全景与健康报告。")
            return [], []
        errors.append("未找到任何 .mmd 图文件（docs/map/*.mmd 或 docs/map/changes/*.mmd）。")
        return [], errors

    diagrams: List[Dict] = []
    l1_l2_nodes: Dict[str, Dict] = {}
    all_flowchart_nodes: Dict[str, Dict] = {}
    has_l2 = any(p.name.startswith("L2-") for p in files if p.parent == MAP_DIR)

    # 第一遍：解析与单图静态校验
    for fpath in files:
        rel_name = fpath.relative_to(MAP_DIR).as_posix()
        raw_text = fpath.read_text(encoding="utf-8")
        lines = raw_text.splitlines()
        if not lines:
            errors.append(f"[{rel_name}] 文件为空。")
            continue

        tm = TITLE_RE.match(lines[0])
        if not tm:
            errors.append(f"[{rel_name}] 第一行必须为 `%% title: 中文标题`。")
            title = fpath.stem
        else:
            title = tm.group(1)

        for idx, line in enumerate(lines, start=1):
            if CLASSDEF_RE.match(line):
                errors.append(f"[{rel_name}:{idx}] 禁止手写 classDef added/changed/removed，样式由 build.py 统一注入。")

        links: Dict[str, str] = {}
        for idx, line in enumerate(lines, start=1):
            if line.strip().startswith("%% link:"):
                lm = LINK_RE.match(line.strip())
                if not lm:
                    errors.append(f"[{rel_name}:{idx}] `%% link:` 格式错误，应为 `%% link: node_id -> target.mmd`。")
                else:
                    links[lm.group(1)] = lm.group(2)

        dtype = detect_diagram_type(lines)
        category = classify_diagram(rel_name)
        is_change = (category == "changes")
        nodes: List[Dict] = []
        participants: List[Dict] = []
        edges: List[str] = []

        if dtype == "flowchart":
            nodes = parse_flowchart_nodes(lines)
            edges = parse_flowchart_edges(lines)
            if len(nodes) > 12:
                errors.append(f"[{rel_name}] 节点数 {len(nodes)} 超过上限 12。")
            if len(edges) > 15:
                errors.append(f"[{rel_name}] 流程图连线数 {len(edges)} 超过上限 15。")
            if category != "L4":
                for lbl in edges:
                    w = calc_label_width(lbl)
                    if w > 28:
                        errors.append(f"[{rel_name}] 连线标签 `{lbl}` 宽度 {w} 超过上限 28。")

            for n in nodes:
                nid = n["id"]
                if not NODE_ID_RE.match(nid):
                    errors.append(f"[{rel_name}] 节点 ID `{nid}` 不合法，只允许英文小写、数字和下划线。")
                nlines = n["lines"]
                if not is_change and len(nlines) >= 2:
                    rel_path = extract_rel_path(nlines[1])
                    if not (PROJECT_ROOT / rel_path).exists():
                        errors.append(f"[{rel_name}] 节点 `{nid}` 标注的路径不存在: `{rel_path}`。")
                if is_change and n["class"] in ("added", "changed", "removed"):
                    if len(nlines) < 3:
                        errors.append(f"[{rel_name}] 变更节点 `{nid}` 缺少第 3 行变更说明。")
                    elif len(nlines[2]) > 12:
                        errors.append(f"[{rel_name}] 变更节点 `{nid}` 第 3 行说明 `{nlines[2]}` 超过 12 个字。")
                if not is_change:
                    sig = tuple(nlines[:2])
                    if nid in all_flowchart_nodes:
                        prev = all_flowchart_nodes[nid]
                        if prev["sig"] != sig:
                            errors.append(
                                f"[{rel_name}] 节点 `{nid}` 的名称或代码位置 {list(sig)} 与 [{prev['source']}] 中的 {list(prev['sig'])} 不一致。"
                            )
                    else:
                        all_flowchart_nodes[nid] = {
                            "id": nid,
                            "sig": sig,
                            "name": nlines[0] if nlines else nid,
                            "source": rel_name,
                        }
                    if rel_name.startswith(("L1-", "L2-")):
                        l1_l2_nodes[nid] = {
                            "id": nid,
                            "name": nlines[0] if nlines else nid,
                            "sig": sig,
                            "source": rel_name,
                        }

        elif dtype == "sequence":
            participants, msg_count = parse_sequence_diagram(lines)
            if len(participants) > 8:
                errors.append(f"[{rel_name}] 时序图参与者数量 {len(participants)} 超过上限 8。")
            if msg_count > 20:
                errors.append(f"[{rel_name}] 时序图消息数量 {msg_count} 超过上限 20。")
            for p in participants:
                if not NODE_ID_RE.match(p["id"]):
                    errors.append(f"[{rel_name}] 参与者 ID `{p['id']}` 不合法，只允许英文小写、数字和下划线。")

        elif dtype == "er":
            entities = parse_er_entities(lines)
            if len(entities) > 12:
                errors.append(f"[{rel_name}] ER 图实体数量 {len(entities)} 超过上限 12。")
            for e in entities:
                if not NODE_ID_RE.match(e):
                    errors.append(f"[{rel_name}] ER 实体 ID `{e}` 不合法，只允许英文小写、数字和下划线。")

        elif dtype == "state":
            states = parse_state_nodes(lines)
            if len(states) > 12:
                errors.append(f"[{rel_name}] 状态图节点数量 {len(states)} 超过上限 12。")
            for st in states:
                if not NODE_ID_RE.match(st):
                    errors.append(f"[{rel_name}] 状态 ID `{st}` 不合法，只允许英文小写、数字和下划线。")
        else:
            errors.append(f"[{rel_name}] 未识别的 Mermaid 图类型。")

        diagrams.append({
            "rel_name": rel_name,
            "category": category,
            "title": title,
            "dtype": dtype,
            "raw_text": raw_text,
            "render_text": prepare_mermaid_source(raw_text, dtype),
            "nodes": nodes,
            "edges": edges,
            "participants": participants,
            "links": links,
        })

    existing_rel_names = {d["rel_name"] for d in diagrams}

    # 第二遍：跨图引用校验（下钻目标 & L3 参与者一致性）
    for d in diagrams:
        rel_name = d["rel_name"]
        is_change = (d["category"] == "changes")
        node_ids = {n["id"] for n in d["nodes"]}
        for src_node, target_file in d["links"].items():
            if d["dtype"] == "flowchart" and src_node not in node_ids:
                errors.append(f"[{rel_name}] `%% link:` 指定的节点 `{src_node}` 在本图中不存在。")
            target_path = MAP_DIR / target_file
            if not target_path.exists() or target_file not in existing_rel_names:
                errors.append(f"[{rel_name}] `%% link:` 下钻目标文件不存在: `{target_file}`。")

        if rel_name == "L1-overview.mmd":
            auto_links: Dict[str, str] = {}
            for n in d["nodes"]:
                nid = n["id"]
                if nid in d["links"]:
                    auto_links[nid] = d["links"][nid]
                    continue
                # 内部模块节点（含路径行）默认对应 L2-<id>.mmd
                if len(n["lines"]) >= 2:
                    candidate = f"L2-{nid}.mmd"
                    if (MAP_DIR / candidate).exists():
                        auto_links[nid] = candidate
                    elif has_l2:
                        errors.append(f"[{rel_name}] L1 模块节点 `{nid}` 缺少下钻文件 `{candidate}`。")
            d["effective_links"] = auto_links
        else:
            d["effective_links"] = dict(d["links"])

        if d["dtype"] == "sequence" and not is_change:
            for p in d["participants"]:
                pid = p["id"]
                if pid not in l1_l2_nodes:
                    errors.append(f"[{rel_name}] L3 参与者 `{pid}` 在 L1/L2 图中不存在。")
                else:
                    expected_name = l1_l2_nodes[pid]["name"]
                    if p["name"] != expected_name:
                        errors.append(
                            f"[{rel_name}] L3 参与者 `{pid}` 名称 `{p['name']}` 与 {l1_l2_nodes[pid]['source']} 中的 `{expected_name}` 不一致。"
                        )
                    if len(p["lines"]) >= 2 and tuple(p["lines"][:2]) != l1_l2_nodes[pid]["sig"]:
                        errors.append(
                            f"[{rel_name}] L3 参与者 `{pid}` 位置 `{p['lines'][1]}` 与 {l1_l2_nodes[pid]['source']} 不一致。"
                        )

    if rel_exists := (not has_l2 and any(d["rel_name"] == "L1-overview.mmd" for d in diagrams)):
        _ = rel_exists
        print("提示: 当前尚无 L2-*.mmd 文件（处于 L1 确认阶段），跳过 L1 -> L2 下钻完整性强制校验。")

    return diagrams, errors


def _safe_group_palette(count: int) -> List[Tuple[str, str, str, str, str, str]]:
    """
    为任意数量的模块分组自动生成高对比度配色（跳过警告橙红区间 10°~42°）。
    返回: [(fill, stroke, text_color, cluster_fill, cluster_stroke, edge_color), ...]
    """
    if count <= 0:
        return []
    safe_span = 328.0  # [42°, 370°] -> 跳过 10°~42°
    palette = []
    for i in range(count):
        # 使用黄金角近似错开相邻分组色相，兼顾小集合均匀分布
        raw_deg = (42.0 + ((i * (safe_span / max(count, 1)) + 155.0) % safe_span)) % 360.0
        h = raw_deg / 360.0

        def _hex(s: float, l: float) -> str:
            r, g, b = colorsys.hls_to_rgb(h, l, s)
            return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"

        palette.append((
            _hex(0.75, 0.95),  # node fill
            _hex(0.62, 0.46),  # node stroke
            _hex(0.68, 0.22),  # node text
            _hex(0.55, 0.98),  # subgraph fill
            _hex(0.45, 0.76),  # subgraph stroke
            _hex(0.68, 0.40),  # outgoing edge stroke
        ))
    return palette


def build_auto_module_overview_diagram(facts: Dict) -> Dict:
    """根据 facts.json 自动生成只读的后端底层模块依赖全景图（上下方向、自动分层与动态着色）。"""
    graph = facts.get("backend_module_graph", {})
    bypasses = facts.get("health", {}).get("external_network_bypasses", [])

    # 收集所有真实模块节点（包含仅有入度的被调模块）
    all_mods = set(graph.keys())
    for targets in graph.values():
        all_mods.update(targets)

    # 识别绕过 adapters 直连外网的模块短名（如 engine.faq_retriever）
    bypass_mods: Dict[str, List[str]] = {}
    for item in bypasses:
        fpath = item.get("file", "")
        if fpath.startswith("backend/app/") and fpath.endswith(".py"):
            mname = fpath[len("backend/app/"):-3].replace("/", ".")
            bypass_mods.setdefault(mname, []).append(item.get("detail", ""))

    def node_id(mod_name: str) -> str:
        return "mod_" + re.sub(r"[^a-zA-Z0-9_]", "_", mod_name)

    # 1. 自动发现模块所属包分组（不写死模块名，新增任意包自动兼容；包 __init__.py 归入自身包而非入口层）
    known_pkgs = {m.split(".", 1)[0] for m in all_mods if "." in m}

    def pkg_of(mod_name: str) -> str:
        if "." in mod_name:
            return mod_name.split(".", 1)[0]
        return mod_name if mod_name in known_pkgs else "__entry__"

    pkg_mods: Dict[str, List[str]] = {}
    for m in sorted(all_mods):
        pkg_mods.setdefault(pkg_of(m), []).append(m)

    # 2. 基于依赖拓扑深度自动计算各分组的上下层次顺序（入度为0在最上，被依赖层逐级向下）
    indeg: Dict[str, int] = {m: 0 for m in all_mods}
    for caller, targets in graph.items():
        for tgt in targets:
            if tgt != caller:
                indeg[tgt] = indeg.get(tgt, 0) + 1

    depth: Dict[str, int] = {m: (0 if indeg.get(m, 0) == 0 else 0) for m in all_mods}
    for _ in range(len(all_mods)):
        changed = False
        for caller, targets in graph.items():
            for tgt in targets:
                if tgt == caller:
                    continue
                if depth[caller] + 1 > depth.get(tgt, 0):
                    depth[tgt] = depth[caller] + 1
                    changed = True
        if not changed:
            break

    def pkg_sort_key(pkg: str) -> Tuple[float, str]:
        ms = pkg_mods[pkg]
        avg_d = sum(depth.get(m, 0) for m in ms) / max(len(ms), 1)
        return (avg_d, pkg)

    ordered_pkgs = sorted(pkg_mods.keys(), key=pkg_sort_key)

    # 仅作友好标题提示；未登记的扩展包自动使用 "{pkg} 模块 ({pkg})"
    title_hints = {
        "__entry__": "入口层 (CLI / FastAPI)",
        "api": "API 路由层 (api)",
        "services": "编排服务层 (services)",
        "engine": "核心引擎层 (engine)",
        "adapters": "统一模型接入底座 (adapters)",
        "models": "持久化模型层 (models)",
    }

    palette = _safe_group_palette(len(ordered_pkgs))
    pkg_style: Dict[str, Tuple[str, str, str, str, str, str]] = {
        pkg: palette[idx] for idx, pkg in enumerate(ordered_pkgs)
    }

    lines = ["flowchart TD"]
    for pkg in ordered_pkgs:
        sg_id = "sg_" + re.sub(r"[^a-zA-Z0-9_]", "_", pkg)
        sg_title = title_hints.get(pkg, f"{pkg} 模块 ({pkg})")
        cls_name = "grp_" + re.sub(r"[^a-zA-Z0-9_]", "_", pkg)
        lines.append(f'  subgraph {sg_id}["{sg_title}"]')
        for m in pkg_mods[pkg]:
            nid = node_id(m)
            if m in bypass_mods:
                lines.append(f'    {nid}["{m}<br/>⚠️ 含外网直连"]:::warnNode')
            else:
                lines.append(f'    {nid}["{m}"]:::{cls_name}')
        lines.append("  end")

    # 外部系统节点
    lines.append('  ext_llm(["外部模型服务<br/>(经 adapters 底座)"])')
    if bypass_mods:
        lines.append('  ext_direct(["⚠️ 外部直连服务<br/>(绕过 adapters 底座)"]):::warnNode')

    edge_idx = 0
    edge_styles: List[Tuple[int, str, str]] = []

    # 绘制全部模块依赖边（含 api/__init__.py 路由装配与 models 导出链路，彻底贯通上下游）
    for caller in sorted(graph.keys()):
        caller_pkg = pkg_of(caller)
        edge_color = pkg_style[caller_pkg][5]
        for tgt in sorted(graph[caller]):
            lines.append(f"  {node_id(caller)} --> {node_id(tgt)}")
            edge_styles.append((edge_idx, edge_color, "stroke-width:1.8px"))
            edge_idx += 1

    # 底座 adapters 出海连线（仅真正发出外部模型请求的适配器）
    if "adapters.base" in all_mods:
        adap_color = pkg_style.get("adapters", ("", "", "", "", "", "#475569"))[5]
        for concrete in ("adapters.openai_adapter", "adapters.harmony_adapter", "adapters.vertex_adapter"):
            if concrete in all_mods:
                lines.append(f"  {node_id(concrete)} --> ext_llm")
                edge_styles.append((edge_idx, adap_color, "stroke-width:1.8px"))
                edge_idx += 1

    # 直连外部系统的警告连线
    for bm in sorted(bypass_mods.keys()):
        lines.append(f'  {node_id(bm)} -.->|"绕过adapters直连"| ext_direct')
        edge_styles.append((edge_idx, "#ea580c", "stroke-width:2.5px,stroke-dasharray:5 5"))
        edge_idx += 1

    # 动态注入各分组 classDef、subgraph style 与连线分色
    for pkg in ordered_pkgs:
        fill, stroke, text_c, sg_fill, sg_stroke, _ = pkg_style[pkg]
        cls_name = "grp_" + re.sub(r"[^a-zA-Z0-9_]", "_", pkg)
        sg_id = "sg_" + re.sub(r"[^a-zA-Z0-9_]", "_", pkg)
        lines.append(f"  classDef {cls_name} fill:{fill},stroke:{stroke},stroke-width:1.8px,color:{text_c};")
        lines.append(f"  style {sg_id} fill:{sg_fill},stroke:{sg_stroke},stroke-width:1.5px;")

    lines.append("  classDef warnNode fill:#fff7ed,stroke:#ea580c,stroke-width:2.2px,color:#9a3412;")
    for ei, ec, extra in edge_styles:
        lines.append(f"  linkStyle {ei} stroke:{ec},{extra};")

    return {
        "rel_name": "__auto_module_overview__.mmd",
        "category": "auto",
        "title": "底层模块依赖全景 (只读自动生成)",
        "dtype": "flowchart",
        "render_text": "\n".join(lines) + "\n",
        "effective_links": {},
    }


def build_index_html(
    diagrams: List[Dict],
    facts: Optional[Dict] = None,
    fact_warnings: Optional[List[str]] = None,
) -> str:
    all_diagrams = list(diagrams)
    if facts is not None:
        all_diagrams.append(build_auto_module_overview_diagram(facts))

    payload = [
        {
            "id": d["rel_name"],
            "category": d["category"],
            "title": d["title"],
            "dtype": d["dtype"],
            "source": d["render_text"],
            "links": d.get("effective_links", {}),
            "nodeLines": {n["id"]: n.get("lines", []) for n in d.get("nodes", [])},
        }
        for d in all_diagrams
    ]
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    shared_bases = (facts or {}).get("shared_bases", [])
    shared_base_paths = [sb.get("path") for sb in shared_bases if sb.get("path")] or ["backend/app/adapters"]

    h = (facts or {}).get("health", {})
    health_report = {
        "factsAvailable": facts is not None,
        "generatedAt": (facts or {}).get("generated_at", ""),
        "driftWarnings": list(fact_warnings or []),
        "externalBypasses": h.get("external_network_bypasses", []),
        "sharedBases": shared_bases,
        "sharedBasePaths": shared_base_paths,
        "cycles": h.get("cycles", []),
        "brokenFeCalls": h.get("broken_fe_calls", []),
        "feOrphans": h.get("fe_orphans", []),
        "beBlindSpotsCount": len(h.get("be_blind_spots", [])),
        "beBlindSpots": h.get("be_blind_spots", [])[:20],
    }
    health_json = json.dumps(health_report, ensure_ascii=False).replace("</", "<\\/")
    rel_mermaid_js = os.path.relpath(MERMAID_JS, MAP_DIR).replace("\\", "/")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Eval Platform · 系统地图</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
      color: #1e293b;
      background: #f8fafc;
      height: 100vh;
      display: flex;
      overflow: hidden;
    }}
    .sidebar {{
      width: 300px;
      min-width: 260px;
      background: #0f172a;
      color: #e2e8f0;
      display: flex;
      flex-direction: column;
      border-right: 1px solid #1e293b;
    }}
    .sidebar-header {{
      padding: 16px 18px;
      border-bottom: 1px solid #1e293b;
    }}
    .sidebar-header h1 {{
      margin: 0;
      font-size: 16px;
      font-weight: 600;
      color: #f8fafc;
    }}
    .sidebar-header p {{
      margin: 4px 0 0;
      font-size: 12px;
      color: #94a3b8;
    }}
    .sidebar-nav {{
      flex: 1;
      overflow-y: auto;
      padding: 12px 10px;
    }}
    .nav-group {{
      margin-bottom: 14px;
    }}
    .nav-group-title {{
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: #64748b;
      padding: 4px 10px;
    }}
    .nav-item {{
      display: block;
      width: 100%;
      text-align: left;
      background: transparent;
      border: none;
      color: #cbd5e1;
      padding: 8px 10px;
      margin: 2px 0;
      border-radius: 6px;
      cursor: pointer;
      transition: background 0.15s;
    }}
    .nav-item:hover {{ background: #1e293b; }}
    .nav-item.active {{
      background: #2563eb;
      color: #ffffff;
    }}
    .nav-item .item-title {{
      display: block;
      font-size: 13px;
      font-weight: 500;
    }}
    .nav-item .item-file {{
      display: block;
      font-size: 11px;
      opacity: 0.72;
      margin-top: 2px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .main {{
      flex: 1;
      display: flex;
      flex-direction: column;
      min-width: 0;
    }}
    .topbar {{
      background: #ffffff;
      border-bottom: 1px solid #e2e8f0;
      padding: 10px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .topbar-left {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .btn {{
      background: #f1f5f9;
      border: 1px solid #cbd5e1;
      color: #1e293b;
      border-radius: 6px;
      padding: 5px 10px;
      font-size: 12px;
      cursor: pointer;
    }}
    .btn:hover:not(:disabled) {{ background: #e2e8f0; }}
    .btn:disabled {{ opacity: 0.45; cursor: not-allowed; }}
    .breadcrumb {{
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
      flex-wrap: wrap;
    }}
    .crumb-link {{
      color: #2563eb;
      cursor: pointer;
      text-decoration: none;
    }}
    .crumb-link:hover {{ text-decoration: underline; }}
    .crumb-current {{
      font-weight: 600;
      color: #0f172a;
    }}
    .legend {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 12px;
      color: #475569;
    }}
    .legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }}
    .swatch {{
      width: 14px;
      height: 14px;
      border-radius: 3px;
      display: inline-block;
    }}
    .swatch-added {{ background: #e8f5e9; border: 2px solid #2e7d32; }}
    .swatch-changed {{ background: #fffde7; border: 2px solid #f9a825; }}
    .swatch-removed {{ background: #ffebee; border: 2px dashed #c62828; }}
    .swatch-cross {{ background: #f5f3ff; border: 2px dashed #7c3aed; }}
    .swatch-base {{ background: #fefce8; border: 2px solid #d97706; box-shadow: inset 0 0 0 1px #f59e0b; }}
    .canvas-wrap {{
      flex: 1;
      overflow: auto;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .canvas-meta {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }}
    .canvas-meta h2 {{
      margin: 0;
      font-size: 18px;
      color: #0f172a;
    }}
    .canvas-meta .hint {{
      font-size: 12px;
      color: #64748b;
    }}
    .diagram-stage {{
      flex: 1;
      position: relative;
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 10px;
      padding: 20px;
      overflow: hidden;
      cursor: grab;
      user-select: none;
      touch-action: none;
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
    }}
    .diagram-stage.dragging {{
      cursor: grabbing;
    }}
    .diagram-inner {{
      transform-origin: 0 0;
      display: inline-block;
      will-change: transform;
    }}
    .error-box {{
      background: #fef2f2;
      border: 1px solid #fecaca;
      color: #991b1b;
      border-radius: 8px;
      padding: 16px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      white-space: pre-wrap;
      font-size: 13px;
    }}
    .check-panel {{
      border-top: 1px solid #1e293b;
      padding: 10px 14px;
      background: #0b1220;
      font-size: 11px;
      color: #94a3b8;
    }}
    .check-panel pre {{
      margin: 6px 0 0;
      max-height: 110px;
      overflow-y: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11px;
      white-space: pre-wrap;
      color: #cbd5e1;
    }}
    .node.drillable {{ cursor: pointer !important; }}
    .node.drillable:hover rect,
    .node.drillable:hover polygon,
    .node.drillable:hover path {{
      stroke: #2563eb !important;
      stroke-width: 2.5px !important;
    }}
    .diagram-inner svg .node,
    .diagram-inner svg .cluster,
    .diagram-inner svg .flowchart-link,
    .diagram-inner svg .edgeLabel {{
      transition: opacity 0.16s ease;
    }}
    .diagram-inner svg.has-highlight .node.dimmed,
    .diagram-inner svg.has-highlight .cluster.dimmed,
    .diagram-inner svg.has-highlight .flowchart-link.dimmed,
    .diagram-inner svg.has-highlight .edgeLabel.dimmed {{
      opacity: 0.12 !important;
    }}
    .diagram-inner svg.has-highlight .flowchart-link.hl-in {{
      stroke: #2563eb !important;
      stroke-width: 3.2px !important;
      opacity: 1 !important;
    }}
    .diagram-inner svg.has-highlight .flowchart-link.hl-out {{
      stroke: #059669 !important;
      stroke-width: 3.2px !important;
      opacity: 1 !important;
    }}
    .diagram-inner svg.has-highlight .node.hl-active rect,
    .diagram-inner svg.has-highlight .node.hl-active polygon,
    .diagram-inner svg.has-highlight .node.hl-active path,
    .diagram-inner svg.has-highlight .node.hl-active circle {{
      stroke-width: 2.8px !important;
      filter: drop-shadow(0 2px 6px rgba(15, 23, 42, 0.18));
    }}
    .diagram-inner svg.has-highlight .node.hl-pinned rect,
    .diagram-inner svg.has-highlight .node.hl-pinned polygon,
    .diagram-inner svg.has-highlight .node.hl-pinned path,
    .diagram-inner svg.has-highlight .node.hl-pinned circle {{
      stroke: #1d4ed8 !important;
      stroke-width: 3.2px !important;
    }}
    .health-badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 11px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      border: 1px solid transparent;
      transition: filter 0.15s;
    }}
    .health-badge:hover {{ filter: brightness(0.96); }}
    .health-ok {{ background: #dcfce7; color: #166534; border-color: #86efac; }}
    .health-warn {{ background: #fef3c7; color: #92400e; border-color: #fcd34d; }}
    .health-err {{ background: #fee2e2; color: #991b1b; border-color: #fca5a5; }}
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.5);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 999;
      padding: 20px;
    }}
    .modal-backdrop.open {{ display: flex; }}
    .modal-card {{
      background: #ffffff;
      border-radius: 12px;
      max-width: 760px;
      width: 100%;
      max-height: 82vh;
      display: flex;
      flex-direction: column;
      box-shadow: 0 20px 50px rgba(15, 23, 42, 0.25);
      overflow: hidden;
    }}
    .modal-header {{
      padding: 14px 18px;
      border-bottom: 1px solid #e2e8f0;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .modal-header h3 {{ margin: 0; font-size: 15px; color: #0f172a; }}
    .modal-body {{
      padding: 16px 18px;
      overflow-y: auto;
      font-size: 13px;
      line-height: 1.6;
      color: #334155;
    }}
    .health-section {{ margin-bottom: 14px; }}
    .health-section h4 {{ margin: 0 0 6px; font-size: 13px; color: #0f172a; }}
    .health-list {{
      margin: 0;
      padding-left: 18px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
    }}
    .health-empty {{ color: #64748b; font-size: 12px; }}
  </style>
</head>
<body>
  <aside class="sidebar">
    <div class="sidebar-header">
      <h1>系统地图 · System Map</h1>
      <p>Eval &amp; Data Flywheel Platform</p>
    </div>
    <nav class="sidebar-nav" id="sidebar-nav"></nav>
    <div class="check-panel">
      <div>语法自检状态：<strong id="check-summary">检查中...</strong></div>
      <pre id="mermaid-check-result" data-ready="false">检查中...</pre>
    </div>
  </aside>

  <main class="main">
    <header class="topbar">
      <div class="topbar-left">
        <button class="btn" id="btn-back" disabled>⬅ 返回上一级</button>
        <div class="breadcrumb" id="breadcrumb"></div>
      </div>
      <div class="topbar-left">
        <button class="health-badge health-ok" id="health-badge" title="点击查看图码对齐与外网直连详情">检查中...</button>
        <div class="legend">
          <span class="legend-item"><span class="swatch swatch-added"></span>新增</span>
          <span class="legend-item"><span class="swatch swatch-changed"></span>修改</span>
          <span class="legend-item"><span class="swatch swatch-removed"></span>删除</span>
          <span class="legend-item"><span class="swatch swatch-cross"></span>跨模块连接</span>
          <span class="legend-item"><span class="swatch swatch-base"></span>共用底座/共享引擎</span>
        </div>
        <button class="btn" id="btn-zoom-fit" title="自适应画布大小">适应</button>
        <button class="btn" id="btn-zoom-out">缩小</button>
        <button class="btn" id="btn-zoom-reset" title="重置为 100% 缩放">100%</button>
        <button class="btn" id="btn-zoom-in">放大</button>
      </div>
    </header>

    <section class="canvas-wrap">
      <div class="canvas-meta">
        <h2 id="diagram-title">加载中...</h2>
        <span class="hint" id="diagram-hint"></span>
      </div>
      <div class="diagram-stage" id="diagram-stage">
        <div class="diagram-inner" id="diagram-output"></div>
      </div>
    </section>
  </main>

  <div class="modal-backdrop" id="health-modal">
    <div class="modal-card">
      <div class="modal-header">
        <h3 id="health-modal-title">代码事实对齐与健康检查</h3>
        <button class="btn" id="btn-close-health">关闭</button>
      </div>
      <div class="modal-body" id="health-modal-body"></div>
    </div>
  </div>

  <script src="{html.escape(rel_mermaid_js)}"></script>
  <script>
    const DIAGRAMS = {data_json};
    const HEALTH_REPORT = {health_json};
    const CATEGORY_LABELS = {{
      L1: "L1 全景图",
      L2: "L2 模块结构图",
      L3: "L3 业务场景时序图",
      L4: "L4 细节图",
      auto: "自动代码全景 (只读)",
      changes: "变更历史"
    }};
    const CATEGORY_ORDER = ["L1", "L2", "L3", "L4", "auto", "changes"];
    const diagramById = new Map(DIAGRAMS.map(d => [d.id, d]));

    let historyStack = [];
    let currentId = (diagramById.has("L1-overview.mmd") ? "L1-overview.mmd" : (DIAGRAMS[0]?.id || ""));
    let zoomScale = 1;
    let panX = 0;
    let panY = 0;
    const MIN_ZOOM = 0.1;
    const MAX_ZOOM = 4.0;

    function initHealthBadge() {{
      const badge = document.getElementById("health-badge");
      const modal = document.getElementById("health-modal");
      const body = document.getElementById("health-modal-body");
      const btnClose = document.getElementById("btn-close-health");

      if (!HEALTH_REPORT.factsAvailable) {{
        badge.className = "health-badge health-warn";
        badge.textContent = "ℹ️ 未加载代码事实";
      }} else {{
        const errCnt = (HEALTH_REPORT.cycles?.length || 0) + (HEALTH_REPORT.brokenFeCalls?.length || 0);
        const warnCnt = (HEALTH_REPORT.driftWarnings?.length || 0) + (HEALTH_REPORT.externalBypasses?.length || 0);
        if (errCnt > 0) {{
          badge.className = "health-badge health-err";
          badge.textContent = `❌ ${{errCnt}} 项异常 / ${{warnCnt}} 项提示`;
        }} else if (warnCnt > 0) {{
          badge.className = "health-badge health-warn";
          badge.textContent = `⚠️ ${{warnCnt}} 项对齐 / 外网提示`;
        }} else {{
          badge.className = "health-badge health-ok";
          badge.textContent = "✅ 图码已对齐";
        }}
      }}

      function renderListSection(title, items, fmt) {{
        let htmlStr = `<div class="health-section"><h4>${{escapeHtml(title)}} (${{items.length}})</h4>`;
        if (!items.length) {{
          htmlStr += `<div class="health-empty">无异常</div>`;
        }} else {{
          htmlStr += `<ul class="health-list">`;
          for (const it of items) {{
            htmlStr += `<li>${{escapeHtml(fmt ? fmt(it) : String(it))}}</li>`;
          }}
          htmlStr += `</ul>`;
        }}
        htmlStr += `</div>`;
        return htmlStr;
      }}

      let detailHtml = `<div class="health-empty" style="margin-bottom:12px;">事实快照时间：${{escapeHtml(HEALTH_REPORT.generatedAt || "无")}}（来源：docs/map/facts/facts.json）</div>`;
      detailHtml += renderListSection("图-码漂移提示（漏画 / 多画过期模块）", HEALTH_REPORT.driftWarnings || []);
      detailHtml += renderListSection("绕过 adapters/ 直连外部网络", HEALTH_REPORT.externalBypasses || [], x => `${{x.file}}:${{x.line}} → ${{x.detail}}`);
      detailHtml += renderListSection("共用底座与共享引擎复用审计", HEALTH_REPORT.sharedBases || [], b => `${{b.path}} [${{(b.capabilities || []).join(", ")}}] → ${{b.status}}`);
      detailHtml += renderListSection("后端循环依赖", HEALTH_REPORT.cycles || [], c => c.join(" -> "));
      detailHtml += renderListSection("前端断头路调用", HEALTH_REPORT.brokenFeCalls || [], b => `${{b.file}}:${{b.line}} (${{b.symbol || ""}} ${{b.url || ""}})`);
      detailHtml += renderListSection("前端未挂载孤立文件", HEALTH_REPORT.feOrphans || []);
      detailHtml += renderListSection("后端未注解调用盲区（前20条）", HEALTH_REPORT.beBlindSpots || [], b => `${{b.file}}:${{b.line}} [${{b.kind}}] ${{b.detail}}`);
      body.innerHTML = detailHtml;

      badge.onclick = () => modal.classList.add("open");
      btnClose.onclick = () => modal.classList.remove("open");
      modal.onclick = (e) => {{ if (e.target === modal) modal.classList.remove("open"); }};
    }}
    initHealthBadge();

    mermaid.initialize({{
      startOnLoad: false,
      suppressErrorRendering: true,
      securityLevel: "loose",
      theme: "base",
      themeVariables: {{
        background: "#ffffff",
        fontFamily: '"PingFang SC", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, sans-serif',
        fontSize: "14px",
        primaryColor: "#f8fafc",
        primaryTextColor: "#0f172a",
        primaryBorderColor: "#64748b",
        lineColor: "#475569",
        secondaryColor: "#f1f5f9",
        tertiaryColor: "#f8fafc",
        clusterBkg: "#f8fafc",
        clusterBorder: "#cbd5e1",
        edgeLabelBackground: "#ffffff"
      }},
      flowchart: {{
        useMaxWidth: false,
        htmlLabels: true,
        curve: "basis",
        nodeSpacing: 52,
        rankSpacing: 95,
        padding: 18
      }}
    }});

    function renderSidebar() {{
      const nav = document.getElementById("sidebar-nav");
      nav.innerHTML = "";
      for (const cat of CATEGORY_ORDER) {{
        const items = DIAGRAMS.filter(d => d.category === cat);
        if (!items.length) continue;
        const group = document.createElement("div");
        group.className = "nav-group";
        const header = document.createElement("div");
        header.className = "nav-group-title";
        header.textContent = CATEGORY_LABELS[cat] || cat;
        group.appendChild(header);

        for (const item of items) {{
          const btn = document.createElement("button");
          btn.className = "nav-item" + (item.id === currentId ? " active" : "");
          btn.innerHTML = `<span class="item-title">${{escapeHtml(item.title)}}</span><span class="item-file">${{escapeHtml(item.id)}}</span>`;
          btn.onclick = () => navigateTo(item.id, false);
          group.appendChild(btn);
        }}
        nav.appendChild(group);
      }}
    }}

    function escapeHtml(s) {{
      return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }}

    function renderBreadcrumb() {{
      const bc = document.getElementById("breadcrumb");
      const btnBack = document.getElementById("btn-back");
      btnBack.disabled = historyStack.length === 0;
      const trail = [...historyStack, currentId].filter(Boolean);
      bc.innerHTML = "";
      trail.forEach((id, idx) => {{
        const d = diagramById.get(id);
        const label = d ? d.title : id;
        if (idx > 0) {{
          const sep = document.createElement("span");
          sep.textContent = "/";
          sep.style.color = "#94a3b8";
          bc.appendChild(sep);
        }}
        if (idx < trail.length - 1) {{
          const a = document.createElement("a");
          a.className = "crumb-link";
          a.textContent = label;
          a.onclick = () => {{
            historyStack = historyStack.slice(0, idx);
            currentId = id;
            renderCurrent();
          }};
          bc.appendChild(a);
        }} else {{
          const span = document.createElement("span");
          span.className = "crumb-current";
          span.textContent = label;
          bc.appendChild(span);
        }}
      }});
    }}

    function navigateTo(targetId, pushHistory) {{
      if (!diagramById.has(targetId)) return;
      if (pushHistory && currentId && currentId !== targetId) {{
        historyStack.push(currentId);
      }} else if (!pushHistory) {{
        historyStack = [];
      }}
      currentId = targetId;
      renderCurrent();
    }}

    function applyZoom() {{
      const el = document.getElementById("diagram-output");
      el.style.transform = `translate(${{panX}}px, ${{panY}}px) scale(${{zoomScale}})`;
      const resetBtn = document.getElementById("btn-zoom-reset");
      if (resetBtn) {{
        resetBtn.textContent = `${{Math.round(zoomScale * 100)}}%`;
      }}
    }}

    function zoomAtPoint(nextScale, clientX, clientY) {{
      const stage = document.getElementById("diagram-stage");
      const clamped = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, nextScale));
      if (!stage) {{
        zoomScale = clamped;
        applyZoom();
        return;
      }}
      const rect = stage.getBoundingClientRect();
      const style = window.getComputedStyle(stage);
      const padLeft = parseFloat(style.paddingLeft) || 0;
      const padTop = parseFloat(style.paddingTop) || 0;
      const mx = clientX - rect.left - padLeft;
      const my = clientY - rect.top - padTop;
      const ratio = clamped / zoomScale;
      panX = mx - (mx - panX) * ratio;
      panY = my - (my - panY) * ratio;
      zoomScale = clamped;
      applyZoom();
    }}

    function zoomAtCenter(factor) {{
      const stage = document.getElementById("diagram-stage");
      if (!stage) return;
      const rect = stage.getBoundingClientRect();
      zoomAtPoint(zoomScale * factor, rect.left + rect.width / 2, rect.top + rect.height / 2);
    }}

    function fitToStage(allowDownscaleOnly = true) {{
      const stage = document.getElementById("diagram-stage");
      const outEl = document.getElementById("diagram-output");
      if (!stage || !outEl) return;
      const svg = outEl.querySelector("svg");
      if (!svg) {{
        zoomScale = 1;
        panX = 0;
        panY = 0;
        applyZoom();
        return;
      }}
      // 临时重置变换以测量自然尺寸
      outEl.style.transform = "translate(0px, 0px) scale(1)";
      const rect = svg.getBoundingClientRect();
      const natW = rect.width || svg.viewBox?.baseVal?.width || 600;
      const natH = rect.height || svg.viewBox?.baseVal?.height || 400;
      const style = window.getComputedStyle(stage);
      const availW = Math.max(100, stage.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0));
      const availH = Math.max(100, stage.clientHeight - (parseFloat(style.paddingTop) || 0) - (parseFloat(style.paddingBottom) || 0));
      let nextScale = Math.min(availW / natW, availH / natH);
      if (allowDownscaleOnly) {{
        nextScale = Math.min(1, nextScale);
      }}
      zoomScale = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, nextScale));
      panX = Math.max(0, (availW - natW * zoomScale) / 2);
      panY = Math.max(0, (availH - natH * zoomScale) / 2);
      applyZoom();
    }}

    function resetView100() {{
      const stage = document.getElementById("diagram-stage");
      const outEl = document.getElementById("diagram-output");
      zoomScale = 1;
      if (stage && outEl) {{
        const svg = outEl.querySelector("svg");
        if (svg) {{
          outEl.style.transform = "translate(0px, 0px) scale(1)";
          const rect = svg.getBoundingClientRect();
          const style = window.getComputedStyle(stage);
          const availW = Math.max(100, stage.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0));
          panX = Math.max(0, (availW - rect.width) / 2);
          panY = 0;
          applyZoom();
          return;
        }}
      }}
      panX = 0;
      panY = 0;
      applyZoom();
    }}

    let activeClearHighlight = null;

    function extractNodeIdFromDom(domId, renderId) {{
      if (!domId) return "";
      let s = domId;
      if (renderId && s.startsWith(renderId + "-")) {{
        s = s.slice(renderId.length + 1);
      }}
      const m = s.match(/^flowchart-(.+)-\\d+$/);
      if (m) return m[1];
      return s;
    }}

    function extractClusterIdFromDom(domId, renderId) {{
      if (!domId) return "";
      if (renderId && domId.startsWith(renderId + "-")) {{
        return domId.slice(renderId.length + 1);
      }}
      return domId;
    }}

    function resolveEdgeEndpoints(dataId, knownIds) {{
      if (!dataId || !dataId.startsWith("L_")) return null;
      const body = dataId.slice(2).replace(/_\\d+$/, "");
      let bestSrc = null;
      let bestDst = null;
      for (const cand of knownIds) {{
        if (body.startsWith(cand + "_")) {{
          const rest = body.slice(cand.length + 1);
          if (knownIds.has(rest) && (!bestSrc || cand.length > bestSrc.length)) {{
            bestSrc = cand;
            bestDst = rest;
          }}
        }}
      }}
      return bestSrc ? {{ src: bestSrc, dst: bestDst }} : null;
    }}

    function safePaletteAt(idx, total) {{
      const safeSpan = 328;
      const hue = (42 + ((idx * (safeSpan / Math.max(total, 1)) + 155) % safeSpan)) % 360;
      return {{
        fill: `hsl(${{hue.toFixed(1)}}, 75%, 95%)`,
        stroke: `hsl(${{hue.toFixed(1)}}, 62%, 46%)`,
        text: `hsl(${{hue.toFixed(1)}}, 68%, 22%)`,
        edge: `hsl(${{hue.toFixed(1)}}, 66%, 40%)`,
        clusterFill: `hsl(${{hue.toFixed(1)}}, 55%, 98%)`,
        clusterStroke: `hsl(${{hue.toFixed(1)}}, 45%, 76%)`
      }};
    }}

    function getNodeLabelLines(nodeEl, nid, diagram) {{
      const preParsed = diagram?.nodeLines?.[nid];
      if (Array.isArray(preParsed) && preParsed.length) {{
        return preParsed;
      }}
      const lbl = nodeEl.querySelector(".nodeLabel");
      if (lbl) {{
        const htmlStr = lbl.innerHTML
          .replace(/<br\\s*\\/?>/gi, "\\n")
          .replace(/<\\/p>\\s*<p[^>]*>/gi, "\\n")
          .replace(/<[^>]+>/g, "");
        return htmlStr.split(/\\r?\\n/).map(s => s.trim()).filter(Boolean);
      }}
      return (nodeEl.textContent || "").split(/\\r?\\n/).map(s => s.trim()).filter(Boolean);
    }}

    function autoStyleFlowchartSvg(svgEl, diagram, renderId) {{
      if (!svgEl || diagram.dtype !== "flowchart") return;

      // 让所有箭头自动继承连线 stroke 颜色（包括普通分色与悬停蓝/绿高亮）
      for (const mp of svgEl.querySelectorAll("marker path, marker polygon, marker circle")) {{
        mp.style.fill = "context-stroke";
        mp.style.stroke = "context-stroke";
      }}

      if (diagram.category === "auto" || diagram.category === "changes") return;

      const nodes = Array.from(svgEl.querySelectorAll("g.node"));
      const clusters = Array.from(svgEl.querySelectorAll("g.cluster"));
      const knownIds = new Set();
      const nodeMeta = [];
      const linkMap = diagram.links || {{}};
      const sharedBasePaths = new Set(HEALTH_REPORT.sharedBasePaths || ["backend/app/adapters"]);
      const crossNodeIds = new Set();
      const sharedBaseIds = new Set();

      for (const n of nodes) {{
        const nid = extractNodeIdFromDom(n.id, renderId);
        if (!nid) continue;
        knownIds.add(nid);

        const skip = n.classList.contains("added") || n.classList.contains("changed") || n.classList.contains("removed") || n.classList.contains("warnNode");

        // 优先使用预解析行，按目录前缀自动分色，不写死任何模块名
        const rawLines = getNodeLabelLines(n, nid, diagram);
        let groupKey = "external";
        let cleanPath = "";
        const pathLine = rawLines.find(l => /^[a-zA-Z0-9_.-]+(?:\\/[a-zA-Z0-9_.-]+)+(?::.*)?$/.test(l));
        if (pathLine) {{
          cleanPath = pathLine.split(":")[0];
          const parts = cleanPath.split("/").filter(Boolean);
          if (parts.length >= 3 && (parts[0] === "backend" || parts[0] === "frontend")) {{
            groupKey = "dir:" + parts.slice(0, 3).join("/");
          }} else if (parts.length >= 1) {{
            groupKey = "dir:" + parts[0];
          }}
        }} else if (n.querySelector("path") && !n.querySelector("rect.basic")) {{
          groupKey = "storage_or_ext";
        }}

        const isSharedBase = Boolean(cleanPath && sharedBasePaths.has(cleanPath));
        const isCrossModule = (!isSharedBase && diagram.category !== "L1" && Boolean(linkMap[nid]));
        if (isSharedBase) sharedBaseIds.add(nid);
        if (isCrossModule) crossNodeIds.add(nid);

        nodeMeta.push({{ el: n, nid, groupKey, skip, isSharedBase, isCrossModule }});
      }}
      for (const c of clusters) {{
        const cid = extractClusterIdFromDom(c.id, renderId);
        if (cid) knownIds.add(cid);
      }}

      const uniqueKeys = Array.from(new Set(nodeMeta.filter(x => !x.skip && !x.isSharedBase && !x.isCrossModule).map(x => x.groupKey))).sort();
      const paletteByKey = new Map();
      uniqueKeys.forEach((k, i) => paletteByKey.set(k, safePaletteAt(i, uniqueKeys.length)));

      const edgeColorBySrc = new Map();
      for (const item of nodeMeta) {{
        if (item.skip) continue;
        const shapes = item.el.querySelectorAll(".label-container, rect.basic, polygon.basic, path.basic, circle.basic, :scope > rect, :scope > polygon, :scope > path");

        if (item.isSharedBase) {{
          // 共用底座 / 共享引擎使用专属琥珀金双强调边框标注
          edgeColorBySrc.set(item.nid, "#d97706");
          for (const sh of shapes) {{
            sh.style.fill = "#fefce8";
            sh.style.stroke = "#d97706";
            sh.style.strokeWidth = "2.5px";
            sh.style.filter = "drop-shadow(0 0 0 2px rgba(245, 158, 11, 0.25))";
          }}
          for (const lbl of item.el.querySelectorAll(".nodeLabel, text, tspan")) {{
            lbl.style.color = "#78350f";
            lbl.style.fill = "#78350f";
            lbl.style.fontWeight = "600";
          }}
          continue;
        }}

        if (item.isCrossModule) {{
          // 跨模块连接节点使用专属紫色虚线粗框标注
          edgeColorBySrc.set(item.nid, "#7c3aed");
          for (const sh of shapes) {{
            sh.style.fill = "#f5f3ff";
            sh.style.stroke = "#7c3aed";
            sh.style.strokeWidth = "2.2px";
            sh.style.strokeDasharray = "6 3";
          }}
          for (const lbl of item.el.querySelectorAll(".nodeLabel, text, tspan")) {{
            lbl.style.color = "#4c1d95";
            lbl.style.fill = "#4c1d95";
          }}
          continue;
        }}

        const pal = paletteByKey.get(item.groupKey);
        if (!pal) continue;
        edgeColorBySrc.set(item.nid, pal.edge);

        for (const sh of shapes) {{
          sh.style.fill = pal.fill;
          sh.style.stroke = pal.stroke;
          sh.style.strokeWidth = "1.8px";
        }}
        for (const lbl of item.el.querySelectorAll(".nodeLabel, text, tspan")) {{
          lbl.style.color = pal.text;
          lbl.style.fill = pal.text;
        }}
      }}

      for (const link of svgEl.querySelectorAll(".flowchart-link")) {{
        const styleAttr = link.getAttribute("style") || "";
        if (/\\bstroke\\s*:/.test(styleAttr)) continue;
        const ep = resolveEdgeEndpoints(link.getAttribute("data-id") || "", knownIds);
        if (ep && (crossNodeIds.has(ep.src) || crossNodeIds.has(ep.dst))) {{
          link.style.stroke = "#7c3aed";
          link.style.strokeWidth = "2.2px";
          continue;
        }}
        if (ep && sharedBaseIds.has(ep.dst)) {{
          link.style.stroke = "#d97706";
          link.style.strokeWidth = "2.0px";
          continue;
        }}
        const c = ep ? edgeColorBySrc.get(ep.src) : null;
        link.style.stroke = c || "#475569";
        link.style.strokeWidth = "1.9px";
      }}
    }}

    function bindInteractiveHighlight(svgEl, diagram, renderId, baseHintText) {{
      activeClearHighlight = null;
      if (!svgEl || diagram.dtype !== "flowchart") return;

      const nodes = Array.from(svgEl.querySelectorAll("g.node"));
      const clusters = Array.from(svgEl.querySelectorAll("g.cluster"));
      const links = Array.from(svgEl.querySelectorAll(".flowchart-link"));
      const edgeLabels = Array.from(svgEl.querySelectorAll(".edgeLabel"));

      const nodeById = new Map();
      const clusterById = new Map();
      const knownIds = new Set();

      for (const n of nodes) {{
        const nid = extractNodeIdFromDom(n.id, renderId);
        if (nid) {{
          nodeById.set(nid, n);
          knownIds.add(nid);
        }}
      }}
      for (const c of clusters) {{
        const cid = extractClusterIdFromDom(c.id, renderId);
        if (cid) {{
          clusterById.set(cid, c);
          knownIds.add(cid);
        }}
      }}

      // 将子节点归属到包含它的 cluster，方便边连到 subgraph 时同步高亮内部节点
      const clusterChildren = new Map();
      for (const [cid, cEl] of clusterById.entries()) {{
        const box = cEl.getBBox();
        const inside = [];
        for (const [nid, nEl] of nodeById.entries()) {{
          const m = nEl.getAttribute("transform")?.match(/translate\\(\\s*([-\\d.]+)\\s*,\\s*([-\\d.]+)\\s*\\)/);
          if (m) {{
            const nx = parseFloat(m[1]), ny = parseFloat(m[2]);
            if (nx >= box.x && nx <= box.x + box.width && ny >= box.y && ny <= box.y + box.height) {{
              inside.push(nid);
            }}
          }}
        }}
        clusterChildren.set(cid, inside);
      }}

      const labelByDataId = new Map();
      for (const el of edgeLabels) {{
        const did = el.querySelector("[data-id]")?.getAttribute("data-id");
        if (did) labelByDataId.set(did, el);
      }}

      const edgeInfos = [];
      for (const link of links) {{
        const did = link.getAttribute("data-id") || "";
        const ep = resolveEdgeEndpoints(did, knownIds);
        if (!ep) continue;
        edgeInfos.push({{
          el: link,
          labelEl: labelByDataId.get(did) || null,
          src: ep.src,
          dst: ep.dst
        }});
      }}

      let pinnedId = null;
      let pinnedMode = "direct";
      const hintEl = document.getElementById("diagram-hint");

      function nodeMatchesEndpoint(nid, endpointId) {{
        return endpointId === nid || Boolean(clusterChildren.get(endpointId)?.includes(nid));
      }}

      function collectReachable(targetId, mode) {{
        const inEdges = new Set();
        const outEdges = new Set();
        const activeNodes = new Set([targetId]);
        const activeClusters = new Set();

        if (mode === "direct") {{
          for (const e of edgeInfos) {{
            if (nodeMatchesEndpoint(targetId, e.dst)) {{
              inEdges.add(e);
              activeNodes.add(e.src);
              if (clusterById.has(e.src)) activeClusters.add(e.src);
            }}
            if (nodeMatchesEndpoint(targetId, e.src)) {{
              outEdges.add(e);
              activeNodes.add(e.dst);
              if (clusterById.has(e.dst)) activeClusters.add(e.dst);
            }}
          }}
          return {{ inEdges, outEdges, activeNodes, activeClusters }};
        }}

        // 全链路模式：分别沿上游（入边反向）与下游（出边正向）做单向 BFS，不横串无关兄弟分支
        const upQueue = [targetId];
        const upVisited = new Set([targetId]);
        while (upQueue.length) {{
          const cur = upQueue.shift();
          for (const e of edgeInfos) {{
            if (nodeMatchesEndpoint(cur, e.dst)) {{
              inEdges.add(e);
              if (clusterById.has(e.src)) {{
                activeClusters.add(e.src);
                for (const cid of (clusterChildren.get(e.src) || [])) {{
                  activeNodes.add(cid);
                  if (!upVisited.has(cid)) {{
                    upVisited.add(cid);
                    upQueue.push(cid);
                  }}
                }}
              }} else {{
                activeNodes.add(e.src);
                if (!upVisited.has(e.src)) {{
                  upVisited.add(e.src);
                  upQueue.push(e.src);
                }}
              }}
            }}
          }}
        }}

        const downQueue = [targetId];
        const downVisited = new Set([targetId]);
        while (downQueue.length) {{
          const cur = downQueue.shift();
          for (const e of edgeInfos) {{
            if (nodeMatchesEndpoint(cur, e.src)) {{
              outEdges.add(e);
              if (clusterById.has(e.dst)) {{
                activeClusters.add(e.dst);
                for (const cid of (clusterChildren.get(e.dst) || [])) {{
                  activeNodes.add(cid);
                  if (!downVisited.has(cid)) {{
                    downVisited.add(cid);
                    downQueue.push(cid);
                  }}
                }}
              }} else {{
                activeNodes.add(e.dst);
                if (!downVisited.has(e.dst)) {{
                  downVisited.add(e.dst);
                  downQueue.push(e.dst);
                }}
              }}
            }}
          }}
        }}

        return {{ inEdges, outEdges, activeNodes, activeClusters }};
      }}

      function applyHighlight(targetId, mode = "direct") {{
        if (!targetId) {{
          svgEl.classList.remove("has-highlight");
          for (const el of [...nodes, ...clusters, ...links, ...edgeLabels]) {{
            el.classList.remove("dimmed", "hl-in", "hl-out", "hl-active", "hl-pinned");
          }}
          if (hintEl) hintEl.textContent = baseHintText;
          return;
        }}

        svgEl.classList.add("has-highlight");
        for (const n of nodes) n.classList.add("dimmed");
        for (const c of clusters) c.classList.add("dimmed");
        for (const l of links) l.classList.add("dimmed");
        for (const el of edgeLabels) el.classList.add("dimmed");
        for (const el of [...nodes, ...links]) el.classList.remove("hl-in", "hl-out", "hl-active", "hl-pinned");

        const {{ inEdges, outEdges, activeNodes, activeClusters }} = collectReachable(targetId, mode);

        for (const e of inEdges) {{
          e.el.classList.remove("dimmed");
          e.el.classList.add("hl-in");
          e.labelEl?.classList.remove("dimmed");
        }}
        for (const e of outEdges) {{
          e.el.classList.remove("dimmed");
          e.el.classList.add("hl-out");
          e.labelEl?.classList.remove("dimmed");
        }}

        for (const nid of activeNodes) {{
          const nEl = nodeById.get(nid);
          if (nEl) {{
            nEl.classList.remove("dimmed");
            nEl.classList.add("hl-active");
          }}
        }}
        if (pinnedId && nodeById.has(pinnedId)) {{
          nodeById.get(pinnedId).classList.add("hl-pinned");
        }}
        for (const cid of activeClusters) {{
          clusterById.get(cid)?.classList.remove("dimmed");
          for (const childId of (clusterChildren.get(cid) || [])) {{
            nodeById.get(childId)?.classList.remove("dimmed");
          }}
        }}

        if (hintEl) {{
          if (!pinnedId) {{
            hintEl.textContent = `聚焦节点 [${{targetId}}] 直接上下游（蓝入/绿出）· 单击锁一级 / 双击锁全链路`;
          }} else if (pinnedMode === "transitive") {{
            hintEl.textContent = `已锁定 [${{pinnedId}}] 全链路上下游（蓝=全部上游 / 绿=全部下游）· 单击切回一级 / 点空白或 Esc 取消`;
          }} else {{
            hintEl.textContent = `已锁定 [${{pinnedId}}] 直接上下游（蓝入/绿出）· 双击切全链路 / 点空白或 Esc 取消`;
          }}
        }}
      }}

      activeClearHighlight = () => {{
        pinnedId = null;
        pinnedMode = "direct";
        applyHighlight(null);
      }};

      for (const [nid, nEl] of nodeById.entries()) {{
        nEl.addEventListener("mouseenter", () => {{
          if (!pinnedId) applyHighlight(nid, "direct");
        }});
        nEl.addEventListener("mouseleave", () => {{
          if (!pinnedId) applyHighlight(null);
        }});
        nEl.addEventListener("click", (e) => {{
          // 下钻节点普通点击保持下钻；Shift+点击或非下钻节点点击用于锁定直接上下游
          if (nEl.classList.contains("drillable") && !e.shiftKey) return;
          e.stopPropagation();
          if (pinnedId === nid && pinnedMode === "direct") {{
            pinnedId = null;
            applyHighlight(null);
          }} else {{
            pinnedId = nid;
            pinnedMode = "direct";
            applyHighlight(pinnedId, "direct");
          }}
        }});
        nEl.addEventListener("dblclick", (e) => {{
          // 双击选中该节点的全链路所有上下游（下钻节点需配合 Shift+双击，防止冲突）
          if (nEl.classList.contains("drillable") && !e.shiftKey) return;
          e.stopPropagation();
          e.preventDefault();
          pinnedId = nid;
          pinnedMode = "transitive";
          applyHighlight(pinnedId, "transitive");
        }});
      }}

      svgEl.addEventListener("click", (e) => {{
        if (!e.target.closest || !e.target.closest("g.node")) {{
          if (pinnedId) {{
            pinnedId = null;
            pinnedMode = "direct";
            applyHighlight(null);
          }}
        }}
      }});
    }}

    function bindDrilldown(diagram) {{
      const container = document.getElementById("diagram-output");
      const links = diagram.links || {{}};
      const linkEntries = Object.entries(links);
      if (!linkEntries.length) return;
      const nodes = container.querySelectorAll(".node");
      for (const nodeEl of nodes) {{
        const domId = nodeEl.id || "";
        for (const [nodeId, targetFile] of linkEntries) {{
          const re = new RegExp(`(?:^|-)${{nodeId}}(?:-|$)`);
          if ((domId === nodeId || re.test(domId)) && diagramById.has(targetFile)) {{
            nodeEl.classList.add("drillable");
            nodeEl.title = `点击下钻至 ${{targetFile}}（Shift+单击锁一级 / Shift+双击锁全链路）`;
            nodeEl.addEventListener("click", (e) => {{
              if (e.shiftKey) return;
              e.stopPropagation();
              navigateTo(targetFile, true);
            }});
          }}
        }}
      }}
    }}

    async function renderCurrent() {{
      renderSidebar();
      renderBreadcrumb();
      const d = diagramById.get(currentId);
      const titleEl = document.getElementById("diagram-title");
      const hintEl = document.getElementById("diagram-hint");
      const outEl = document.getElementById("diagram-output");
      if (!d) {{
        titleEl.textContent = "未找到图";
        outEl.innerHTML = `<div class="error-box">未找到任何可用的系统地图文件。</div>`;
        return;
      }}
      titleEl.textContent = `${{d.title}} (${{d.id}})`;
      const linkCount = Object.keys(d.links || {{}}).length;
      const baseHintText = (linkCount > 0 ? `点击高亮节点下钻（共 ${{linkCount}} 个） · ` : "") + "滚轮缩放 · 拖拽平移 · 单击锁一级 / 双击锁全链路";
      hintEl.textContent = baseHintText;

      try {{
        const renderId = "active-" + Math.random().toString(36).slice(2, 9);
        const {{ svg }} = await mermaid.render(renderId, d.source);
        outEl.innerHTML = svg;
        const svgEl = outEl.querySelector("svg");
        if (svgEl) {{
          const vb = svgEl.viewBox?.baseVal;
          if (vb && vb.width > 0 && vb.height > 0) {{
            svgEl.style.width = vb.width + "px";
            svgEl.style.height = vb.height + "px";
            svgEl.style.maxWidth = "none";
          }}
          autoStyleFlowchartSvg(svgEl, d, renderId);
          bindInteractiveHighlight(svgEl, d, renderId, baseHintText);
        }}
        bindDrilldown(d);
        fitToStage(true);
      }} catch (err) {{
        const msg = err?.message || String(err);
        outEl.innerHTML = `<div class="error-box">渲染失败: ${{escapeHtml(d.id)}}\\n\\n${{escapeHtml(msg)}}\\n\\n--- 源码 ---\\n${{escapeHtml(d.source)}}</div>`;
        zoomScale = 1;
        panX = 0;
        panY = 0;
        applyZoom();
      }}
    }}

    function initPanZoom() {{
      const stage = document.getElementById("diagram-stage");
      if (!stage) return;

      let isPointerDown = false;
      let isDragging = false;
      let startClientX = 0;
      let startClientY = 0;
      let startPanX = 0;
      let startPanY = 0;
      let suppressNextClick = false;

      stage.addEventListener("wheel", (e) => {{
        e.preventDefault();
        const factor = Math.min(1.25, Math.max(0.8, Math.exp(-e.deltaY * 0.0015)));
        zoomAtPoint(zoomScale * factor, e.clientX, e.clientY);
      }}, {{ passive: false }});

      stage.addEventListener("pointerdown", (e) => {{
        if (e.button !== 0 && e.button !== 1) return;
        if (e.button === 1) e.preventDefault();
        isPointerDown = true;
        isDragging = false;
        startClientX = e.clientX;
        startClientY = e.clientY;
        startPanX = panX;
        startPanY = panY;
      }});

      window.addEventListener("pointermove", (e) => {{
        if (!isPointerDown) return;
        const dx = e.clientX - startClientX;
        const dy = e.clientY - startClientY;
        if (!isDragging && Math.hypot(dx, dy) > 4) {{
          isDragging = true;
          stage.classList.add("dragging");
        }}
        if (isDragging) {{
          panX = startPanX + dx;
          panY = startPanY + dy;
          applyZoom();
        }}
      }});

      function endDrag() {{
        if (!isPointerDown) return;
        if (isDragging) {{
          suppressNextClick = true;
          setTimeout(() => {{ suppressNextClick = false; }}, 0);
        }}
        isPointerDown = false;
        isDragging = false;
        stage.classList.remove("dragging");
      }}

      window.addEventListener("pointerup", endDrag);
      window.addEventListener("pointercancel", endDrag);

      stage.addEventListener("click", (e) => {{
        if (suppressNextClick) {{
          e.stopPropagation();
          e.preventDefault();
          suppressNextClick = false;
        }}
      }}, true);

      stage.addEventListener("dblclick", (e) => {{
        if (e.target.closest && e.target.closest("g.node")) return;
        fitToStage(true);
      }});

      stage.addEventListener("click", (e) => {{
        if (!e.target.closest || !e.target.closest("g.node")) {{
          if (activeClearHighlight) activeClearHighlight();
        }}
      }});

      window.addEventListener("keydown", (e) => {{
        if (e.key === "Escape" && activeClearHighlight) {{
          activeClearHighlight();
        }}
      }});
    }}
    initPanZoom();

    async function runAllSyntaxChecks() {{
      const resultEl = document.getElementById("mermaid-check-result");
      const summaryEl = document.getElementById("check-summary");
      const lines = [];
      let passCount = 0;
      let failCount = 0;

      for (let i = 0; i < DIAGRAMS.length; i++) {{
        const d = DIAGRAMS[i];
        try {{
          await mermaid.parse(d.source);
          const {{ svg }} = await mermaid.render(`check-${{i}}`, d.source);
          if (!svg || !svg.includes("<svg")) {{
            throw new Error("未生成有效 SVG");
          }}
          passCount++;
          lines.push(`PASS: ${{d.id}}`);
        }} catch (err) {{
          failCount++;
          const msg = (err?.message || String(err)).replace(/\\r?\\n/g, " ");
          lines.push(`FAIL: ${{d.id}} | ${{msg}}`);
        }}
      }}
      summaryEl.textContent = failCount === 0 ? `全部通过 (${{passCount}}/${{DIAGRAMS.length}})` : `存在错误 (${{failCount}} 失败)`;
      summaryEl.style.color = failCount === 0 ? "#4ade80" : "#f87171";
      resultEl.textContent = lines.join("\\n");
      resultEl.setAttribute("data-ready", "true");
    }}

    document.getElementById("btn-back").onclick = () => {{
      if (historyStack.length > 0) {{
        currentId = historyStack.pop();
        renderCurrent();
      }}
    }};
    document.getElementById("btn-zoom-fit").onclick = () => fitToStage(true);
    document.getElementById("btn-zoom-in").onclick = () => zoomAtCenter(1.2);
    document.getElementById("btn-zoom-out").onclick = () => zoomAtCenter(1 / 1.2);
    document.getElementById("btn-zoom-reset").onclick = () => resetView100();

    (async () => {{
      await runAllSyntaxChecks();
      await renderCurrent();
    }})();
  </script>
</body>
</html>
"""


def find_browser_candidates() -> List[str]:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        found = shutil.which(name)
        if found and found not in candidates:
            candidates.append(found)
    return [c for c in candidates if pathlib.Path(c).exists()]


def run_render_verification() -> bool:
    candidates = find_browser_candidates()
    if not candidates:
        print("未做渲染验证（未找到可用的 Chrome 或 Edge 浏览器）。")
        return True

    uri = INDEX_HTML.resolve().as_uri()
    last_err = ""
    for exe in candidates:
        try:
            proc = subprocess.run(
                [
                    exe,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--allow-file-access-from-files",
                    "--virtual-time-budget=5000",
                    "--dump-dom",
                    uri,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except OSError as exc:
            last_err = f"{exe} 启动失败: {exc}"
            continue
        except subprocess.TimeoutExpired:
            last_err = f"{exe} 无头渲染超时。"
            continue

        if proc.returncode != 0 and not proc.stdout:
            last_err = f"{exe} 返回非零状态 {proc.returncode}: {proc.stderr.strip()}"
            continue

        m = re.search(
            r'<pre id="mermaid-check-result" data-ready="([^"]*)">(.*?)</pre>',
            proc.stdout,
            flags=re.DOTALL,
        )
        if not m:
            last_err = f"{exe} 未读取到页面自检清单元素 #mermaid-check-result。"
            continue

        ready, raw_body = m.group(1), html.unescape(m.group(2)).strip()
        if ready != "true":
            last_err = f"{exe} 页面自检尚未完成 (data-ready={ready}): {raw_body}"
            continue

        print(f"无头渲染校验结果 ({pathlib.Path(exe).name}):")
        fail_lines = []
        for line in raw_body.splitlines():
            print(f"  {line}")
            if line.startswith("FAIL:"):
                fail_lines.append(line)

        if fail_lines:
            print("❌ 无头浏览器 Mermaid 渲染校验失败！", file=sys.stderr)
            return False
        return True

    print(f"未做渲染验证（所有候选浏览器启动或检查失败: {last_err}）。")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="系统地图代码事实提取、静态校验与 HTML 构建工具")
    parser.add_argument(
        "--facts",
        action="store_true",
        help="仅执行前后端代码解析并生成 docs/map/facts/FACTS.md 与 facts.json（供 AI 画图/改代码前阅读）",
    )
    parser.add_argument(
        "--skip-facts",
        action="store_true",
        help="跳过重新解析代码，仅校验并构建 .mmd 地图",
    )
    parser.add_argument(
        "--strict-facts",
        action="store_true",
        help="将图-码漂移告警视作错误并阻断构建",
    )
    args = parser.parse_args()

    facts = None
    if not args.skip_facts:
        facts = extract_and_save_facts()
        h = facts["health"]
        print(
            f"📌 已更新代码事实清单: {FACTS_MD.relative_to(PROJECT_ROOT).as_posix()} & {FACTS_JSON.relative_to(PROJECT_ROOT).as_posix()}\n"
            f"   能力切片={len(facts['capability_slices'])}, 循环依赖={len(h['cycles'])}, "
            f"前端断头路={len(h['broken_fe_calls'])}, 外网直连={len(h['external_network_bypasses'])}, "
            f"后端盲区={len(h['be_blind_spots'])}"
        )
    elif FACTS_JSON.exists():
        facts = json.loads(FACTS_JSON.read_text(encoding="utf-8"))
        print(f"ℹ️ 已跳过重新解析，加载已有事实快照: {FACTS_JSON.relative_to(PROJECT_ROOT).as_posix()}")

    if args.facts:
        if facts and (facts["health"]["cycles"] or facts["health"]["broken_fe_calls"]):
            print("⚠️ 检测到架构健康问题，请查看 FACTS.md 第 0 节详情。", file=sys.stderr)
            return 1
        return 0

    if not MERMAID_JS.exists():
        print(f"❌ 缺少 Mermaid 本地库文件: {MERMAID_JS}", file=sys.stderr)
        return 1

    diagrams, errors = validate_and_load(allow_empty=(facts is not None))
    fact_warnings: List[str] = []
    if facts is not None:
        fact_errors, fact_warnings = verify_maps_against_facts(diagrams, facts, strict=args.strict_facts)
        errors.extend(fact_errors)
        for w in fact_warnings:
            print(f"⚠️ 图-码对齐提示: {w}")

    if errors:
        print("❌ 系统地图静态校验未通过:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    html_content = build_index_html(diagrams, facts=facts, fact_warnings=fact_warnings)
    INDEX_HTML.write_text(html_content, encoding="utf-8")
    total_views = len(diagrams) + (1 if facts is not None else 0)
    print(f"✅ 已生成 {INDEX_HTML.relative_to(PROJECT_ROOT).as_posix()}（共 {total_views} 张视图，静态校验全部通过）。")

    if not run_render_verification():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
