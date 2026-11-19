# -*- coding: utf-8 -*-
"""
拓扑动力学校验单元测试：验证下游顺承跳步与跨分支窜流拦截
"""
import pytest
from backend.app.schemas.rule import TopologySOPConfig
from backend.app.engine.topology import UniversalTopologyValidator

def test_topology_reachability_and_parallel_branch_detection():
    # 构建包含分支结构的 Mermaid 流程图:
    # Start -> N1
    # N1 -> N2_BranchA (分支A) -> N3_A1 -> N3_A2 -> End
    # N1 -> N2_BranchB (分支B) -> N3_B1 -> End
    mermaid_def = """
    graph TD
      Start --> N1
      N1 --> N2_BranchA
      N1 --> N2_BranchB
      N2_BranchA --> N3_A1
      N3_A1 --> N3_A2
      N3_A2 --> End
      N2_BranchB --> N3_B1
      N3_B1 --> End
    """

    config = TopologySOPConfig(
        enabled=True,
        mermaid_graph=mermaid_def,
        entry_nodes=["Start"],
        terminal_nodes=["End"],
        allow_self_loop=True,
        allow_slot_shortcut=True,
    )

    validator = UniversalTopologyValidator(config)

    # 1. 正常单步推进测试
    ok, msg, _ = validator.validate_trajectory(["Start", "N1", "N2_BranchA", "N3_A1", "N3_A2", "End"])
    assert ok, f"正常推进应当通过: {msg}"

    # 2. FAQ 自旋就地停留测试
    ok, msg, _ = validator.validate_trajectory(["Start", "N1", "N1", "N2_BranchA", "End"])
    assert ok, f"自旋应当放行: {msg}"

    # 3. 合法的下游顺承跳步测试：在同一支线 BranchA 内，跳过了 N3_A1，直接由 N2_BranchA 到 N3_A2
    ok, msg, _ = validator.validate_trajectory(["Start", "N1", "N2_BranchA", "N3_A2", "End"])
    assert ok, f"沿有向主干跳过槽位应当放行: {msg}"

    # 4. 非法的跨并行分支窜流测试：从 N2_BranchA 突然窜入互斥的 N3_B1！
    ok, msg, _ = validator.validate_trajectory(["Start", "N1", "N2_BranchA", "N3_B1", "End"])
    assert not ok, "窜入互斥并行分支必须被拦截！"
    assert "跨分支窜流" in msg or "互不可达" in msg, f"报错信息应当提示跨分支窜流: {msg}"

    # 5. 非法的逆向倒流测试：从 N3_A2 逆行跳回 N1
    ok, msg, _ = validator.validate_trajectory(["Start", "N1", "N2_BranchA", "N3_A2", "N1"])
    assert not ok, "逆向倒流必须被拦截！"
