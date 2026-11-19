# -*- coding: utf-8 -*-
"""
通用 Mermaid 有向图拓扑连通性校验器 (Universal Topology Validator)
核心算法设计准则:
1. Mermaid 流程图是有且仅有的全局业务状态转移图 (Directed Graph);
2. 业务合规本质: 会话轨迹在图上构成从 entry_nodes 出发、沿有向连线直达 terminal_nodes 的单向连通路径;
3. 三大合法动力学行为:
   - 节点推进 (Advance): (u, v) 在直接有向边集合 valid_edges 中;
   - 节点自旋 (Self-Loop): u == v，遇客户反问/质疑/FAQ问答，就地解答后拉回主线;
   - 同阶段顺承跳过 (Shortcut): 属于 stage_groups 内部的下游推进 (如客户提前交代多槽位);
4. 刚性一票否决: 
   - 跨阶段非法悬空跃迁 (图上无直接有向边且非同阶段顺承);
   - 漏掉核心必经模块 (根据 fatal_if_skipped_after 规则检查);
   - 未抵达法定出口节点。
"""
import re
from typing import Dict, Any, Optional, List, Tuple, Set
from backend.app.schemas.rule import TopologySOPConfig


class UniversalTopologyValidator:
    """基于通用 Mermaid 流程图的有向图拓扑连通性校验器"""

    def __init__(self, config: TopologySOPConfig):
        self.config = config
        self.raw_text = config.mermaid_graph
        self.node_pattern_str = config.node_id_pattern or r"[A-Za-z0-9_]+"
        self.node_regex = re.compile(self.node_pattern_str)
        self.valid_edges: Set[Tuple[str, str]] = set()
        self.adj: Dict[str, Set[str]] = {}
        self.all_nodes: Set[str] = set()
        self.entry_nodes: Set[str] = set(config.entry_nodes)
        self.terminal_nodes: Set[str] = set(config.terminal_nodes)
        self.stage_groups = config.stage_groups or []
        self._parse_graph()

    def clean_node_id(self, raw_str: str) -> str:
        """从字符串中提取纯净的节点ID，如 'N4_2 [提前收集...]' -> 'N4_2'"""
        if not raw_str:
            return ""
        m = self.node_regex.search(str(raw_str))
        return m.group(0) if m else str(raw_str).strip()

    def _parse_graph(self):
        """解析 Mermaid TD / LR 流程图中的所有有向边与节点"""
        if not self.raw_text:
            return

        edge_pattern = re.compile(
            rf"({self.node_pattern_str})\s*(?:--[^-]*-->|-->)\s*({self.node_pattern_str})"
        )

        for line in self.raw_text.split("\n"):
            line = line.strip()
            if (
                not line
                or line.startswith("%%")
                or line.startswith("subgraph")
                or line.startswith("end")
            ):
                continue

            # 清洗行内的节点标签文本（如 ("1.1 是本人？") 或 ["1.1 是本人？"]），防止其中的数字/文字被误提取为 node_id
            clean_line = re.sub(r'\("[^"]*"\)|\([^\)]*\)|\["[^"]*"\]|\[[^\]]*\]', '', line)

            parts = [
                self.clean_node_id(p) for p in self.node_regex.findall(clean_line)
            ]
            if "-->" in clean_line and len(parts) >= 2:
                for i in range(len(parts) - 1):
                    u, v = parts[i], parts[i + 1]
                    if u and v and u != v:
                        self.valid_edges.add((u, v))
                        self.all_nodes.add(u)
                        self.all_nodes.add(v)
                        self.adj.setdefault(u, set()).add(v)
            else:
                for match in edge_pattern.finditer(clean_line):
                    u, v = match.group(1), match.group(2)
                    if u and v and u != v:
                        self.valid_edges.add((u, v))
                        self.all_nodes.add(u)
                        self.all_nodes.add(v)
                        self.adj.setdefault(u, set()).add(v)

        # 默认入口与出口容错推导
        if not self.entry_nodes and self.all_nodes:
            in_degrees = {n: 0 for n in self.all_nodes}
            for u, v in self.valid_edges:
                in_degrees[v] = in_degrees.get(v, 0) + 1
            entries = [n for n, deg in in_degrees.items() if deg == 0]
            self.entry_nodes = set(entries) if entries else {next(iter(self.all_nodes))}

        if not self.terminal_nodes and self.all_nodes:
            out_degrees = {n: len(self.adj.get(n, set())) for n in self.all_nodes}
            terminals = [n for n, deg in out_degrees.items() if deg == 0]
            self.terminal_nodes = set(terminals) if terminals else self.all_nodes

    def has_directed_path(self, u: str, v: str) -> bool:
        """
        基于 BFS 检查在有向图上节点 u 是否可以沿有向边到达节点 v (Downstream Reachability)。
        用于验证大模型跳过槽位收集节点时，目标节点是否属于合法的下游流转支线，
        严格防止：
        1. 跨入完全没有连通关系的互斥并行分支 (Parallel Branch Crossing)
        2. 逆向倒流 (Backward Leap)
        """
        if u == v:
            return True
        if (u, v) in self.valid_edges:
            return True

        visited = {u}
        queue = [u]
        while queue:
            curr = queue.pop(0)
            for nxt in self.adj.get(curr, set()):
                if nxt == v:
                    return True
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        return False

    def find_path_nodes(self, u: str, v: str) -> Optional[List[str]]:
        """基于 BFS 寻找从节点 u 到节点 v 的一条最短有向路径，返回途经的完整节点序列 [u, ..., v]"""
        if u == v:
            return [u]
        visited = {u}
        queue: List[List[str]] = [[u]]
        while queue:
            path = queue.pop(0)
            curr = path[-1]
            for nxt in self.adj.get(curr, set()):
                if nxt == v:
                    return path + [nxt]
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(path + [nxt])
        return None

    def is_same_stage_group_advance(self, u: str, v: str) -> bool:
        """兼容性方法：检查 u 和 v 是否下游可达或同属顺承组"""
        return self.has_directed_path(u, v)

    def validate_trajectory(
        self,
        trajectory: List[str],
        collected_slots: Optional[Set[str]] = None,
        is_customer_hangup: bool = False,
    ) -> Tuple[bool, str, List[str]]:
        """
        Mermaid 客观图连通性校验 (纯连通性元数据生成)
        原则:
            - 代码层仅核验图论上的节点有向连通性 (节点是否在图中、相邻转移是否有向可达);
            - 业务跳步是否合理、槽位收集是否充分，代码层不做判定，完全交由质检模型结合上下文裁决。
        参数:
            trajectory: 坐席依次推进的节点序列 [N1_1, N2_1, ...]
            collected_slots: 客户在各轮次中已透露的槽位节点集合 (保留用于扩展或元数据展示)
            is_customer_hangup: 客户是否主动要求挂机或中断通话 (用于合法早退豁免)
        返回: (是否连通, 连通性诊断描述, 规范化节点序列)
        """
        if not self.config.enabled:
            return True, "拓扑校验已全局关闭，默认放行", [self.clean_node_id(n) for n in trajectory]

        if not trajectory:
            return False, "会话流转轨迹为空，未提取出任何有效 SOP 节点", []

        clean_nodes = [self.clean_node_id(n) for n in trajectory if n]
        clean_nodes = [n for n in clean_nodes if n]

        if not clean_nodes:
            return False, "无法从轨迹文本中提取合法节点 ID", []

        # 1. 检查入口节点合规性
        start_node = clean_nodes[0]
        if self.entry_nodes and start_node not in self.entry_nodes:
            return (
                False,
                f"【入口非法】会话起始节点为 '{start_node}'，不在法定开场入口集合 {list(self.entry_nodes)} 中",
                clean_nodes,
            )

        # 2. 逐跳检查状态转移连通性
        for i in range(len(clean_nodes) - 1):
            u, v = clean_nodes[i], clean_nodes[i + 1]

            # 动力学行为 1: 节点自旋停留 (大模型判定当前为 FAQ 质疑/安抚解答，就地停留)
            if u == v:
                if self.config.allow_self_loop:
                    continue
                return (
                    False,
                    f"【禁止节点自旋】在节点 '{u}' 发生重复停留，当前规则已关闭自旋放行",
                    clean_nodes,
                )

            # 动力学行为 2: 直接有向边推进 (直接存在于图中)
            if (u, v) in self.valid_edges:
                continue

            # 动力学行为 3: 必须在真实拓扑连线上才可以跳步 (严禁跨入无连接关系的支线)
            has_path = self.has_directed_path(u, v)
            if not has_path:
                return (
                    False,
                    f"【拓扑断路/跨分支窜流】从节点 '{u}' 无法到达节点 '{v}' (在流程图上无任何有向连通路径，属于窜入互斥分支或逆向违规)",
                    clean_nodes,
                )

            # 存在有向路径连接关系：核验是否允许下游槽位跳步
            if self.config.allow_slot_shortcut:
                continue

            # 违规: 拓扑断路悬空跃迁
            return (
                False,
                f"【拓扑断路】节点转移 '{u}' ➔ '{v}' 在图中不存在直接连通边 (当前规则未开启槽位顺承跳步)",
                clean_nodes,
            )

        # 3. 检查终结节点是否合法结束 (若开启了客户挂机早退豁免或未刚性要求终结节点，允许合法收尾)
        end_node = clean_nodes[-1]
        require_term = getattr(self.config, "require_terminal_node", False)
        allow_hangup_exit = getattr(self.config, "allow_customer_hangup_early_exit", True)

        if require_term and self.terminal_nodes and end_node not in self.terminal_nodes:
            if is_customer_hangup and allow_hangup_exit:
                return True, f"状态机连通合规 (客户主动提出挂机，在节点 '{end_node}' 合法收尾退出)", clean_nodes
            return (
                False,
                f"【会话未完成】最终节点为 '{end_node}'，未能推进至法定出口 {list(self.terminal_nodes)}",
                clean_nodes,
            )

        return True, "状态机轨迹完全连通合规", clean_nodes
