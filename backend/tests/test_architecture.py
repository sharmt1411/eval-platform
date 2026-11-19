# -*- coding: utf-8 -*-
"""
架构守护测试 (Architecture Fitness Functions):
防止 Fat Router、越级绕过服务层直连引擎、包级聚合导入回归、隐蔽延迟导入及循环依赖。
"""
import ast
import pathlib
import sys
from typing import List, Set

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
MAP_DIR = PROJECT_ROOT / "docs" / "map"
if str(MAP_DIR) not in sys.path:
    sys.path.insert(0, str(MAP_DIR))

from fact_extractor import BackendAnalyzer  # noqa: E402


def _iter_backend_source_files() -> List[pathlib.Path]:
    files = [PROJECT_ROOT / "backend" / "cli.py"]
    app_dir = PROJECT_ROOT / "backend" / "app"
    files.extend(
        p for p in sorted(app_dir.rglob("*.py")) if p.name != "__init__.py"
    )
    return [f for f in files if f.exists()]


def _short_mod_edges(be: BackendAnalyzer, full_mod: str) -> Set[str]:
    return {
        t.replace("backend.app.", "")
        for t in be.mod_edges.get(full_mod, set())
    }


def test_no_backend_module_cycles():
    """后端模块之间不允许存在循环依赖"""
    be = BackendAnalyzer()
    be.build()
    cycles = be.find_cycles()
    assert cycles == [], f"检测到后端模块循环依赖: {cycles}"


def test_direct_submodule_imports_only():
    """跨层调用必须直导具体子模块，禁止走 adapters/engine/services 包入口聚合导入"""
    forbidden_barrels = {
        "backend.app.adapters",
        "backend.app.engine",
        "backend.app.services",
    }
    violations: List[str] = []
    for fpath in _iter_backend_source_files():
        tree = ast.parse(fpath.read_text(encoding="utf-8"), filename=str(fpath))
        rel = fpath.relative_to(PROJECT_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in forbidden_barrels:
                violations.append(f"{rel}:{node.lineno} -> from {node.module} import ...")
    assert not violations, f"发现禁止的包级聚合导入（请改为直导具体子模块）:\n" + "\n".join(violations)


def test_no_function_local_engine_or_service_imports_in_routers():
    """路由文件函数体内部禁止隐藏延迟导入 engine/services/adapters"""
    api_dir = PROJECT_ROOT / "backend" / "app" / "api"
    violations: List[str] = []
    for fpath in sorted(api_dir.glob("*.py")):
        if fpath.name == "__init__.py":
            continue
        tree = ast.parse(fpath.read_text(encoding="utf-8"), filename=str(fpath))
        rel = fpath.relative_to(PROJECT_ROOT).as_posix()
        for fn_node in ast.walk(tree):
            if not isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(fn_node):
                if isinstance(sub, ast.ImportFrom) and sub.module:
                    if sub.module.startswith(("backend.app.engine", "backend.app.services", "backend.app.adapters")):
                        violations.append(f"{rel}:{sub.lineno} ({fn_node.name}) -> from {sub.module}")
                elif isinstance(sub, ast.Import):
                    for alias in sub.names:
                        if alias.name.startswith(("backend.app.engine", "backend.app.services", "backend.app.adapters")):
                            violations.append(f"{rel}:{sub.lineno} ({fn_node.name}) -> import {alias.name}")
    assert not violations, "路由函数内部禁止延迟导入引擎或服务:\n" + "\n".join(violations)


def test_router_to_service_encapsulation_boundaries():
    """
    核心分层守护断言：
    1. api.flywheel 禁止越过 FlywheelPipelineService 直连其封装的底层引擎；
    2. api.simulation 禁止越过服务层直连对弈内部子引擎或飞轮修复/切题引擎；
    3. services/ 保持无 FastAPI/SQLAlchemy 耦合；engine/ 保持无 FastAPI 耦合。
    """
    be = BackendAnalyzer()
    be.build()

    flywheel_deps = _short_mod_edges(be, "backend.app.api.flywheel")
    forbidden_flywheel_deps = {
        "adapters.factory",
        "engine.judge",
        "engine.remediator",
        "engine.slicer",
        "engine.topology",
        "engine.faq_retriever",
        "engine.simulator",
        "engine.benchmark_runner",
    }
    flywheel_skips = sorted(flywheel_deps & forbidden_flywheel_deps)
    assert not flywheel_skips, f"api.flywheel 不得绕过服务层或 deps 直连底层模块: {flywheel_skips}"

    sim_deps = _short_mod_edges(be, "backend.app.api.simulation")
    forbidden_sim_deps = {
        "adapters.factory",
        "engine.remediator",
        "engine.slicer",
        "engine.simulator",
        "engine.topology",
        "engine.faq_retriever",
        "engine.benchmark_runner",
        "engine.benchmark_sampler",
    }
    sim_skips = sorted(sim_deps & forbidden_sim_deps)
    assert not sim_skips, f"api.simulation 不得绕过服务层或 deps 直连底层模块: {sim_skips}"

    for mod in be.mod_to_path:
        if mod.startswith("backend.app.services."):
            raw_imports = set(be.file_imports.get(mod, {}).values())
            assert not any(e == "fastapi" or e.startswith(("fastapi.", "sqlalchemy")) for e in raw_imports), (
                f"{mod} 不得耦合 Web/ORM 框架: {raw_imports}"
            )
        if mod.startswith("backend.app.engine."):
            raw_imports = set(be.file_imports.get(mod, {}).values())
            assert not any(e == "fastapi" or e.startswith("fastapi.") for e in raw_imports), (
                f"{mod} 不得耦合 FastAPI 框架: {raw_imports}"
            )


def test_no_new_external_network_bypass_outside_adapters():
    """除已登记的 faq_retriever 远程向量可选支路外，禁止在 adapters/ 外直连外部网络"""
    be = BackendAnalyzer()
    be.build()
    allowed = {"backend/app/engine/faq_retriever.py"}
    unexpected = [
        f"{h['file']}:{h['line']} ({h['detail']})"
        for h in be.external_network_hits
        if h["file"] not in allowed
    ]
    assert not unexpected, "检测到未经 adapters/ 底座直连外部网络:\n" + "\n".join(unexpected)
