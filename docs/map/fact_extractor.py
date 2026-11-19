# -*- coding: utf-8 -*-
"""
系统地图代码事实提取器与图-码对齐校验器 (docs/map/fact_extractor.py)
仅使用 Python 标准库；前端若存在 Node + typescript 则自动调用 ts_probe.mjs (TypeScript 7 TypeChecker)。

核心产物（落盘保存供 AI 与人随时查阅）：
- docs/map/facts/facts.json : 结构化机器事实
- docs/map/facts/FACTS.md   : 给 AI / 开发者看的端到端事实清单（防漏线、防猜线）
"""
import ast
import datetime
import json
import pathlib
import re
import shutil
import subprocess
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set, Tuple

MAP_DIR = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = MAP_DIR.parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
BACKEND_APP = BACKEND_DIR / "app"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
FRONTEND_SRC = FRONTEND_DIR / "src"
TS_PROBE_JS = MAP_DIR / "ts_probe.mjs"
FACTS_DIR = MAP_DIR / "facts"
FACTS_JSON = FACTS_DIR / "facts.json"
FACTS_MD = FACTS_DIR / "FACTS.md"

NOISE_MODULES = {"backend.app.schemas", "backend.app.config"}

BUILTIN_CONTAINER_METHODS = {
    "strip", "lstrip", "rstrip", "split", "rsplit", "replace", "join",
    "lower", "upper", "title", "format", "startswith", "endswith",
    "isdigit", "isalpha", "isalnum", "encode", "decode", "find", "count",
    "get", "pop", "setdefault", "update", "keys", "values", "items",
    "append", "extend", "insert", "remove", "clear", "copy", "sort", "reverse",
    "index", "add", "discard", "union", "intersection",
}

STDLIB_OR_COMMON_VARS = {
    "self", "cls", "super", "os", "sys", "re", "json", "time",
    "math", "uuid", "logging", "logger", "asyncio",
}

NETWORK_MODULES = (
    "requests", "httpx", "aiohttp", "urllib.request", "urllib3",
    "openai", "anthropic", "google.genai", "genai", "websockets", "websocket",
)

# 前端页面路径归属到 L1/L2 业务能力 ID（对应 AGENTS.md 5.4）
ROUTE_TO_CAPABILITY = {
    "/models": ("model_hub", "模型中心"),
    "/rules": ("rule_center", "规则中心"),
    "/benchmarks": ("benchmark", "常规评测"),
    "/benchmarks/:taskId": ("benchmark", "常规评测"),
    "/simulation": ("simulation", "模拟对弈"),
    "/flywheel": ("flywheel", "数据飞轮"),
    "/datasets": ("dataset_center", "数据集中心"),
    "/prompts": ("prompt_center", "提示词中心"),
}


def path_to_module(path: pathlib.Path) -> str:
    rel = path.relative_to(PROJECT_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def unwrap_type_name(ann: Optional[ast.expr]) -> Optional[str]:
    if ann is None:
        return None
    if isinstance(ann, ast.Name):
        return ann.id
    if isinstance(ann, ast.Attribute):
        return ann.attr
    if isinstance(ann, ast.Subscript):
        base = unwrap_type_name(ann.value)
        if base in ("Optional", "List", "list", "Sequence", "Iterable"):
            return unwrap_type_name(ann.slice)
        if base == "Union" and isinstance(ann.slice, ast.Tuple):
            for elt in ann.slice.elts:
                if not (isinstance(elt, ast.Constant) and elt.value is None):
                    res = unwrap_type_name(elt)
                    if res:
                        return res
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        for side in (ann.left, ann.right):
            if not (isinstance(side, ast.Constant) and side.value is None):
                res = unwrap_type_name(side)
                if res:
                    return res
    return None


def resolve_import_module(current_mod: str, module: Optional[str], level: int) -> str:
    if level == 0:
        return module or ""
    parts = current_mod.split(".")
    base = parts[:-level] if level <= len(parts) else []
    return ".".join(base + ([module] if module else []))


def normalize_url(path: str) -> str:
    p = re.sub(r"\$\{[^}]+\}", "{}", path)
    p = re.sub(r"\{[^}]+\}", "{}", p)
    p = re.sub(r"/+", "/", p).strip()
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


class BackendAnalyzer:
    """后端 Python AST 调用图、盲区、孤岛、外网直连与切片构建器"""

    def __init__(self):
        self.py_files = sorted(list(BACKEND_APP.rglob("*.py")) + [BACKEND_DIR / "cli.py"])
        self.trees: Dict[pathlib.Path, ast.Module] = {}
        self.path_to_mod: Dict[pathlib.Path, str] = {}
        self.mod_to_path: Dict[str, pathlib.Path] = {}

        self.class_defs: Dict[str, Tuple[str, ast.ClassDef]] = {}
        self.class_name_to_qnames: Dict[str, List[str]] = defaultdict(list)
        self.func_defs: Dict[str, Tuple[str, ast.AST]] = {}
        self.func_name_to_qnames: Dict[str, List[str]] = defaultdict(list)
        self.class_bases: Dict[str, List[str]] = defaultdict(list)
        self.project_methods: Set[str] = set()
        self.func_return_types: Dict[str, str] = {}

        self.file_imports: Dict[str, Dict[str, str]] = defaultdict(dict)
        self.self_attr_types: Dict[str, Dict[str, str]] = defaultdict(dict)

        self.orm_models: Set[str] = set()
        self.route_handlers: Dict[Tuple[str, str], str] = {}
        self.all_routes: List[Dict] = []

        self.mod_edges: Dict[str, Set[str]] = defaultdict(set)
        self.fn_callees: Dict[str, Set[str]] = defaultdict(set)
        self.fn_orm_uses: Dict[str, Set[str]] = defaultdict(set)
        self.fn_data_io: Dict[str, Set[str]] = defaultdict(set)
        self.fn_net_calls: Dict[str, Set[str]] = defaultdict(set)

        self.blind_spots: List[Dict] = []
        self.defensive_getattr_count: int = 0
        self.ref_counts: Dict[str, int] = defaultdict(int)
        self.entrypoint_symbols: Set[str] = set()
        self.external_network_hits: List[Dict] = []

    def build(self):
        for f in self.py_files:
            mod = path_to_module(f)
            self.path_to_mod[f] = mod
            self.mod_to_path[mod] = f
            self.trees[f] = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))

        # Pass 1: 导入、类、函数、基类、返回值注解、入口标记
        for f, tree in self.trees.items():
            mod = self.path_to_mod[f]
            rel = f.relative_to(PROJECT_ROOT).as_posix()

            for sub in ast.walk(tree):
                if isinstance(sub, ast.Import):
                    for a in sub.names:
                        self.file_imports[mod][a.asname or a.name.split(".")[0]] = a.name
                elif isinstance(sub, ast.ImportFrom):
                    tmod = resolve_import_module(mod, sub.module, sub.level)
                    for a in sub.names:
                        self.file_imports[mod][a.asname or a.name] = f"{tmod}.{a.name}" if tmod else a.name

            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    cq = f"{mod}:{node.name}"
                    self.class_defs[cq] = (rel, node)
                    self.class_name_to_qnames[node.name].append(cq)
                    for b in node.bases:
                        bn = unwrap_type_name(b)
                        if bn:
                            self.class_bases[node.name].append(bn)
                    if mod == "backend.app.models.base" and any(
                        unwrap_type_name(b) == "Base" for b in node.bases
                    ):
                        self.orm_models.add(node.name)
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if not item.name.startswith("__"):
                                self.project_methods.add(item.name)
                            mq = f"{mod}:{node.name}.{item.name}"
                            self.func_defs[mq] = (rel, item)
                            self.func_name_to_qnames[f"{node.name}.{item.name}"].append(mq)
                            ret = unwrap_type_name(item.returns)
                            if ret:
                                self.func_return_types[f"{node.name}.{item.name}"] = ret
                                self.func_return_types[item.name] = ret
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fq = f"{mod}:{node.name}"
                    self.func_defs[fq] = (rel, node)
                    self.func_name_to_qnames[node.name].append(fq)
                    ret = unwrap_type_name(node.returns)
                    if ret:
                        self.func_return_types[node.name] = ret
                    for dec in node.decorator_list:
                        dec_str = ast.unparse(dec)
                        if any(k in dec_str for k in ("router.", "app.", "cli.", "command", "on_event")):
                            self.entrypoint_symbols.add(fq)

            for target in self.file_imports[mod].values():
                tmod = self._resolve_to_project_module(target)
                if tmod and tmod != mod:
                    self.mod_edges[mod].add(tmod)

        # Pass 2: 推导 self.xxx 属性类型（3 种来源）
        for cq, (_, cls_node) in self.class_defs.items():
            self._infer_self_attrs(cq, cls_node)

        # Pass 3: 解析 FastAPI 路由前缀与处理器
        self._extract_routes()

        # Pass 4: 全库扫描调用边、盲区、入度、外网直连、ORM/文件底座
        for f, tree in self.trees.items():
            mod = self.path_to_mod[f]
            rel = f.relative_to(PROJECT_ROOT).as_posix()
            self._scan_file(mod, rel, tree)

    def _resolve_to_project_module(self, target: str) -> Optional[str]:
        if target in self.mod_to_path:
            return target
        if "." in target:
            parent = target.rsplit(".", 1)[0]
            if parent in self.mod_to_path:
                return parent
        return None

    def _infer_expr_type(self, expr: ast.expr, local_vars: Dict[str, str]) -> Optional[str]:
        if isinstance(expr, ast.Name):
            return local_vars.get(expr.id)
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Name):
                fn = expr.func.id
                if fn in self.class_name_to_qnames:
                    return fn
                if fn in self.func_return_types:
                    return self.func_return_types[fn]
            elif isinstance(expr.func, ast.Attribute):
                attr = expr.func.attr
                if isinstance(expr.func.value, ast.Name):
                    if expr.func.value.id == "re" and attr == "compile":
                        return "re.Pattern"
                    key = f"{expr.func.value.id}.{attr}"
                    if key in self.func_return_types:
                        return self.func_return_types[key]
                if attr in self.func_return_types:
                    return self.func_return_types[attr]
        if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
            for v in expr.values:
                t = self._infer_expr_type(v, local_vars)
                if t:
                    return t
        if isinstance(expr, ast.IfExp):
            return self._infer_expr_type(expr.body, local_vars) or self._infer_expr_type(expr.orelse, local_vars)
        return None

    def _infer_self_attrs(self, cq: str, cls_node: ast.ClassDef):
        attrs = self.self_attr_types[cq]
        for item in cls_node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                t = unwrap_type_name(item.annotation)
                if t:
                    attrs[item.target.id] = t
        for item in cls_node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local_vars: Dict[str, str] = {}
            for arg in item.args.args + item.args.kwonlyargs:
                t = unwrap_type_name(arg.annotation)
                if t:
                    local_vars[arg.arg] = t
            for sub in ast.walk(item):
                if isinstance(sub, ast.AnnAssign):
                    if isinstance(sub.target, ast.Attribute) and isinstance(sub.target.value, ast.Name) and sub.target.value.id == "self":
                        t = unwrap_type_name(sub.annotation)
                        if t:
                            attrs[sub.target.attr] = t
                    elif isinstance(sub.target, ast.Name):
                        t = unwrap_type_name(sub.annotation)
                        if t:
                            local_vars[sub.target.id] = t
                elif isinstance(sub, ast.Assign):
                    t = self._infer_expr_type(sub.value, local_vars)
                    if t:
                        for tgt in sub.targets:
                            if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name) and tgt.value.id == "self":
                                attrs[tgt.attr] = t
                            elif isinstance(tgt, ast.Name):
                                local_vars[tgt.id] = t

    def _extract_routes(self):
        cfg_src = (BACKEND_APP / "config.py").read_text(encoding="utf-8")
        m_pref = re.search(r'API_V1_PREFIX\s*:\s*str\s*=\s*["\']([^"\']+)["\']', cfg_src)
        api_v1_prefix = m_pref.group(1) if m_pref else "/api/v1"

        main_src = (BACKEND_APP / "main.py").read_text(encoding="utf-8")
        api_init_src = (BACKEND_APP / "api" / "__init__.py").read_text(encoding="utf-8")
        top_prefix = api_v1_prefix if "API_V1_PREFIX" in main_src else ""

        for m in re.finditer(r'@app\.(get|post|put|delete|patch)\(\s*["\']([^"\']*)["\']', main_src):
            raw_p = m.group(2)
            norm = normalize_url(raw_p)
            meth = m.group(1).upper()
            self.all_routes.append({"file": "main.py", "method": meth, "raw": raw_p, "norm": norm, "handler": "backend.app.main"})

        alias_to_mod = {
            m.group(2): m.group(1)
            for m in re.finditer(r'from\s+backend\.app\.api\.(\w+)\s+import\s+router\s+as\s+(\w+)', api_init_src)
        }
        sub_prefixes = {}
        for m in re.finditer(r'api_router\.include_router\(\s*(\w+)(?:\s*,\s*prefix\s*=\s*["\']([^"\']+)["\'])?', api_init_src):
            sub_prefixes[alias_to_mod.get(m.group(1), m.group(1))] = m.group(2) or ""

        for api_file in sorted((BACKEND_APP / "api").glob("*.py")):
            if api_file.name == "__init__.py":
                continue
            mod = self.path_to_mod[api_file]
            tree = self.trees[api_file]
            src = api_file.read_text(encoding="utf-8")
            m_rp = re.search(r'APIRouter\(\s*prefix\s*=\s*["\']([^"\']+)["\']', src)
            file_prefix = m_rp.group(1) if m_rp else ""
            full_prefix = top_prefix + sub_prefixes.get(api_file.stem, "") + file_prefix

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                            if isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router":
                                meth = dec.func.attr.upper()
                                if meth in ("GET", "POST", "PUT", "DELETE", "PATCH") and dec.args:
                                    if isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                                        raw_p = full_prefix + dec.args[0].value
                                        norm = normalize_url(raw_p)
                                        handler_q = f"{mod}:{node.name}"
                                        self.route_handlers[(meth, norm)] = handler_q
                                        self.all_routes.append({
                                            "file": api_file.name,
                                            "method": meth,
                                            "raw": raw_p,
                                            "norm": norm,
                                            "handler": handler_q,
                                        })

    def _resolve_method_qname(self, cls_name: str, method_name: str) -> Optional[str]:
        cands = self.func_name_to_qnames.get(f"{cls_name}.{method_name}", [])
        return cands[0] if cands else None

    def _resolve_func_qname(self, mod: str, name: str) -> Optional[str]:
        local = f"{mod}:{name}"
        if local in self.func_defs:
            return local
        imp = self.file_imports[mod].get(name)
        if imp and "." in imp:
            tmod, sym = imp.rsplit(".", 1)
            cand = f"{tmod}:{sym}"
            if cand in self.func_defs:
                return cand
        cands = self.func_name_to_qnames.get(name, [])
        return cands[0] if len(cands) == 1 else None

    def _scan_file(self, mod: str, rel: str, tree: ast.Module):
        imported = self.file_imports[mod]
        local_classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        is_adapter_dir = rel.startswith("backend/app/adapters/")

        # 外网底座绕过扫描（含函数内延迟 import）
        imported_net_aliases: Dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for alias in n.names:
                    local = alias.asname or alias.name.split(".")[0]
                    target = alias.name
                    if any(target == nm or target.startswith(nm + ".") for nm in NETWORK_MODULES):
                        imported_net_aliases[local] = target
                        if not is_adapter_dir:
                            self.external_network_hits.append({
                                "file": rel, "line": n.lineno, "kind": "import", "detail": f"import {target}",
                            })
            elif isinstance(n, ast.ImportFrom):
                tmod = resolve_import_module(mod, n.module, n.level)
                for alias in n.names:
                    local = alias.asname or alias.name
                    target = f"{tmod}.{alias.name}" if tmod else alias.name
                    if any(target == nm or target.startswith(nm + ".") for nm in NETWORK_MODULES):
                        imported_net_aliases[local] = target
                        if not is_adapter_dir:
                            self.external_network_hits.append({
                                "file": rel, "line": n.lineno, "kind": "import", "detail": f"from {tmod} import {alias.name}",
                            })

        # 全范围引用统计（跨文件 + 同文件）
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                for q in self.class_name_to_qnames.get(n.id, []):
                    _, cnode = self.class_defs[q]
                    if not (q.startswith(mod + ":") and n.lineno == cnode.lineno):
                        self.ref_counts[q] += 1
                for q in self.func_name_to_qnames.get(n.id, []):
                    _, fnode = self.func_defs[q]
                    if not (q.startswith(mod + ":") and n.lineno == fnode.lineno):
                        self.ref_counts[q] += 1
            elif isinstance(n, ast.Attribute):
                for q in self.class_name_to_qnames.get(n.attr, []):
                    self.ref_counts[q] += 1
                for q in self.func_name_to_qnames.get(n.attr, []):
                    self.ref_counts[q] += 1

        base_vars = dict(imported)
        for c in local_classes:
            base_vars[c] = c

        def analyze_fn(fn_qname: Optional[str], fn_node: ast.AST, cur_cls_qname: Optional[str]):
            cur_vars = dict(base_vars)
            if isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in fn_node.args.args + fn_node.args.kwonlyargs:
                    t = unwrap_type_name(arg.annotation)
                    if t:
                        cur_vars[arg.arg] = t

            for sub in ast.walk(fn_node):
                if isinstance(sub, ast.Import):
                    for a in sub.names:
                        cur_vars[a.asname or a.name.split(".")[0]] = a.name
                elif isinstance(sub, ast.ImportFrom):
                    tmod = resolve_import_module(mod, sub.module, sub.level)
                    for a in sub.names:
                        cur_vars[a.asname or a.name] = f"{tmod}.{a.name}" if tmod else a.name
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    t = unwrap_type_name(sub.annotation)
                    if t:
                        cur_vars[sub.target.id] = t
                elif isinstance(sub, ast.Assign):
                    t = self._infer_expr_type(sub.value, cur_vars)
                    if t:
                        for tgt in sub.targets:
                            if isinstance(tgt, ast.Name):
                                cur_vars[tgt.id] = t
                elif isinstance(sub, ast.With):
                    for item in sub.items:
                        if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                            t = self._infer_expr_type(item.context_expr, cur_vars)
                            if t:
                                cur_vars[item.optional_vars.id] = t

                if fn_qname:
                    if isinstance(sub, ast.Name) and sub.id in self.orm_models:
                        self.fn_orm_uses[fn_qname].add(sub.id)
                    if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                        if sub.value.id == "settings" and sub.attr in ("DATA_DIR", "RULES_DIR", "DATASETS_DIR", "RUNS_DIR"):
                            self.fn_data_io[fn_qname].add(sub.attr)

                if isinstance(sub, ast.Call):
                    func = sub.func
                    if not is_adapter_dir and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        if func.value.id in imported_net_aliases:
                            call_txt = f"{imported_net_aliases[func.value.id]}.{func.attr}"
                            self.external_network_hits.append({
                                "file": rel, "line": sub.lineno, "kind": "call", "detail": f"{ast.unparse(func)}(...)",
                            })
                            if fn_qname:
                                self.fn_net_calls[fn_qname].add(call_txt)

                    # 盲区检测
                    if isinstance(func, ast.Name) and func.id in ("import_module", "eval", "exec", "__import__"):
                        self.blind_spots.append({"file": rel, "line": sub.lineno, "kind": "动态加载/执行", "detail": ast.unparse(sub)[:100]})
                    elif isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "getattr":
                        self.blind_spots.append({"file": rel, "line": sub.lineno, "kind": "反射动态调用", "detail": ast.unparse(sub)[:100]})
                    elif isinstance(func, ast.Name) and func.id == "getattr":
                        self.defensive_getattr_count += 1
                    elif isinstance(func, ast.Subscript):
                        self.blind_spots.append({"file": rel, "line": sub.lineno, "kind": "下标直接调用", "detail": ast.unparse(sub)[:100]})
                    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Subscript):
                        if func.attr not in BUILTIN_CONTAINER_METHODS:
                            self.blind_spots.append({"file": rel, "line": sub.lineno, "kind": "下标对象方法调用", "detail": ast.unparse(sub)[:100]})

                    # 函数级调用边 + 未注解变量盲区
                    if fn_qname:
                        if isinstance(func, ast.Name):
                            if func.id in self.class_name_to_qnames:
                                init_q = self._resolve_method_qname(func.id, "__init__")
                                self.fn_callees[fn_qname].add(init_q or self.class_name_to_qnames[func.id][0])
                            else:
                                fq = self._resolve_func_qname(mod, func.id)
                                if fq:
                                    self.fn_callees[fn_qname].add(fq)
                        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                            var, method = func.value.id, func.attr
                            if var == "self" and cur_cls_qname:
                                mq = self._resolve_method_qname(cur_cls_qname.split(":")[1], method)
                                if mq:
                                    self.fn_callees[fn_qname].add(mq)
                            elif var in self.class_name_to_qnames:
                                mq = self._resolve_method_qname(var, method)
                                if mq:
                                    self.fn_callees[fn_qname].add(mq)
                            elif var in cur_vars:
                                t = cur_vars[var]
                                t_cls = t.rsplit(".", 1)[-1] if "." in t else t
                                mq = self._resolve_method_qname(t_cls, method)
                                if mq:
                                    self.fn_callees[fn_qname].add(mq)
                                else:
                                    cand = f"{t}:{method}"
                                    if cand in self.func_defs:
                                        self.fn_callees[fn_qname].add(cand)
                            elif var not in STDLIB_OR_COMMON_VARS and var not in local_classes and method in self.project_methods and method not in BUILTIN_CONTAINER_METHODS:
                                self.blind_spots.append({"file": rel, "line": sub.lineno, "kind": "未注解变量调用", "detail": f"{var}.{method}()"})
                        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute):
                            if isinstance(func.value.value, ast.Name) and func.value.value.id == "self" and cur_cls_qname:
                                attr, method = func.value.attr, func.attr
                                t = self.self_attr_types.get(cur_cls_qname, {}).get(attr)
                                if t:
                                    if t != "re.Pattern":
                                        mq = self._resolve_method_qname(t, method)
                                        if mq:
                                            self.fn_callees[fn_qname].add(mq)
                                elif method in self.project_methods and method not in BUILTIN_CONTAINER_METHODS:
                                    self.blind_spots.append({"file": rel, "line": sub.lineno, "kind": "未推导self属性调用", "detail": f"self.{attr}.{method}()"})

                        if "add_task" in ast.unparse(func):
                            for arg in sub.args:
                                if isinstance(arg, ast.Name):
                                    fq = self._resolve_func_qname(mod, arg.id)
                                    if fq:
                                        self.fn_callees[fn_qname].add(fq)

        for n in tree.body:
            if isinstance(n, ast.ClassDef):
                cq = f"{mod}:{n.name}"
                for item in n.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        analyze_fn(f"{mod}:{n.name}.{item.name}", item, cq)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                analyze_fn(f"{mod}:{n.name}", n, None)

    def find_cycles(self) -> List[List[str]]:
        visited: Set[str] = set()
        stack: List[str] = []
        on_stack: Set[str] = set()
        cycles: List[List[str]] = []

        def dfs(u: str):
            visited.add(u)
            stack.append(u)
            on_stack.add(u)
            for v in sorted(self.mod_edges.get(u, ())):
                if v in NOISE_MODULES or any(v.startswith(nm + ".") for nm in NOISE_MODULES):
                    continue
                if v not in visited:
                    dfs(v)
                elif v in on_stack:
                    cycles.append(stack[stack.index(v):] + [v])
            stack.pop()
            on_stack.remove(u)

        for m in sorted(self.mod_to_path.keys()):
            if m not in visited:
                dfs(m)
        return cycles

    def find_islands(self) -> List[Dict]:
        referenced_base_names = {q.split(":")[1] for q, cnt in self.ref_counts.items() if cnt > 0}
        islands: List[Dict] = []
        for qname, (rel, _) in sorted(self.class_defs.items()):
            if any(rel.startswith(p) for p in ("backend/app/schemas/", "backend/app/models/")):
                continue
            cls_name = qname.split(":")[1]
            bases = self.class_bases.get(cls_name, [])
            if self.ref_counts.get(qname, 0) == 0 and not any(b in referenced_base_names for b in bases):
                islands.append({"file": rel, "symbol": f"class {cls_name}"})
        for qname, (rel, _) in sorted(self.func_defs.items()):
            if "." in qname.split(":", 1)[1]:
                continue
            if qname in self.entrypoint_symbols or rel == "backend/cli.py":
                continue
            fn_name = qname.split(":")[1]
            if fn_name in ("main", "init_db", "health_check") or (fn_name.startswith("__") and fn_name.endswith("__")):
                continue
            if self.ref_counts.get(qname, 0) == 0:
                islands.append({"file": rel, "symbol": f"def {fn_name}()"})
        return islands

    def trace_slice(self, entry_fns: List[str], stop_modules: Optional[Set[str]] = None) -> Dict:
        return trace_slice_from_graph(
            entry_fns=entry_fns,
            fn_callees=self.fn_callees,
            fn_orm_uses=self.fn_orm_uses,
            fn_data_io=self.fn_data_io,
            fn_net_calls=self.fn_net_calls,
            stop_modules=stop_modules,
        )


def trace_slice_from_graph(
    entry_fns: List[str],
    fn_callees: Dict[str, Any],
    fn_orm_uses: Dict[str, Any],
    fn_data_io: Dict[str, Any],
    fn_net_calls: Dict[str, Any],
    stop_modules: Optional[Set[str]] = None,
) -> Dict:
    """
    沿函数调用图从入口函数做 BFS 纵向切片。
    若提供 stop_modules（跨能力下钻委派边界模块），命中时记录该边界模块本身并停止继续穿透其内部调用。
    """
    visited: Set[str] = set()
    q = deque(entry_fns)
    touched_modules: Set[str] = set()
    touched_orms: Set[str] = set()
    touched_data_dirs: Set[str] = set()
    uses_adapter_layer = False
    direct_ext_net: Set[str] = set()
    stop_set = stop_modules or set()

    while q:
        cur = q.popleft()
        if cur in visited:
            continue
        visited.add(cur)
        mod = cur.split(":")[0]
        touched_modules.add(mod)
        if mod in stop_set:
            # 在跨能力边界节点处止步，不吸收对方内部实现细节
            continue
        if mod.startswith("backend.app.adapters"):
            uses_adapter_layer = True
        touched_orms |= set(fn_orm_uses.get(cur, ()))
        touched_data_dirs |= set(fn_data_io.get(cur, ()))
        for net in fn_net_calls.get(cur, ()):
            if not mod.startswith("backend.app.adapters"):
                direct_ext_net.add(f"{mod.replace('backend.app.', '')}:{net}")
        for nxt in fn_callees.get(cur, ()):
            if nxt not in visited:
                q.append(nxt)

    return {
        "visited_fns": sorted(visited),
        "modules": sorted(touched_modules),
        "orms": sorted(touched_orms),
        "data_dirs": sorted(touched_data_dirs),
        "uses_adapter": uses_adapter_layer,
        "direct_ext_net": sorted(direct_ext_net),
    }


def run_frontend_probe() -> Dict:
    """优先调用 Node + TS 7 Compiler API；缺失时降级提示（不中断主流程）"""
    node_exe = shutil.which("node")
    ts_pkg = FRONTEND_DIR / "node_modules" / "typescript" / "package.json"
    if node_exe and ts_pkg.exists() and TS_PROBE_JS.exists():
        proc = subprocess.run(
            [node_exe, str(TS_PROBE_JS)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        data = json.loads(proc.stdout)
        data["engine"] = "typescript-7-compiler-api"
        return data
    return {
        "engine": "unavailable",
        "tsVersion": None,
        "axiosBaseUrl": "/api/v1",
        "srcFiles": [],
        "moduleImports": {},
        "reExports": {},
        "routes": [],
        "apiMethods": {},
        "jsxRenders": [],
        "symbolCalls": [],
        "directNetCalls": [],
        "reactEvents": [],
        "localFnCalls": {},
    }


def extract_and_save_facts() -> Dict:
    be = BackendAnalyzer()
    be.build()
    fe = run_frontend_probe()

    def short_be(s: str) -> str:
        return s.replace("backend.app.", "").replace("backend.", "")

    def short_fe(p: str) -> str:
        return p.replace("frontend/src/", "")

    # 1. 前端内部依赖邻接表 + 孤立文件检查
    internal_adj: Dict[str, List[str]] = defaultdict(list)
    for src, imps in sorted(fe.get("moduleImports", {}).items()):
        targets = sorted({short_fe(x["target"]) for x in imps if x["isInternal"]})
        if targets:
            internal_adj[short_fe(src)] = targets
    for src, reexps in sorted(fe.get("reExports", {}).items()):
        if reexps:
            targets = sorted({short_fe(x["target"]) for x in reexps})
            internal_adj[short_fe(src)].extend(targets)

    reachable_fe: Set[str] = set()
    dq = deque(["main.tsx"])
    while dq:
        cur = dq.popleft()
        if cur in reachable_fe:
            continue
        reachable_fe.add(cur)
        for nxt in internal_adj.get(cur, []):
            if nxt not in reachable_fe:
                dq.append(nxt)

    all_non_type_fe = [
        short_fe(f) for f in fe.get("srcFiles", []) if not f.startswith("frontend/src/types/")
    ]
    fe_orphans = sorted(set(all_non_type_fe) - reachable_fe) if all_non_type_fe else []

    # 2. 前后端路由对齐 + 页面事件链路串联
    local_fn_calls = fe.get("localFnCalls", {})

    def resolve_fn_to_apis(file_rel: str, entry_fn: str) -> List[Dict]:
        visited: Set[str] = set()
        q = deque([entry_fn])
        apis: List[Dict] = []
        fmap = local_fn_calls.get(file_rel, {})
        while q:
            cur = q.popleft()
            if cur in visited:
                continue
            visited.add(cur)
            for item in fmap.get(cur, []):
                if item.get("kind") == "api_call":
                    apis.append(item)
                elif item.get("kind") == "local_call":
                    tgt = item.get("targetSymbol")
                    if tgt and tgt not in visited:
                        q.append(tgt)
        return apis

    used_api_symbols: Set[str] = set()
    matched_be_keys: Set[Tuple[str, str]] = set()
    broken_fe_calls: List[Dict] = []
    route_chains: List[Dict] = []

    for c in fe.get("symbolCalls", []):
        if c["kind"] == "api_call":
            used_api_symbols.add(c["targetSymbol"])
            key = (c.get("httpMethod"), c.get("normUrl"))
            if key in be.route_handlers:
                matched_be_keys.add(key)
            else:
                broken_fe_calls.append({
                    "file": c["callerFile"], "line": c["line"], "symbol": c["targetSymbol"],
                    "method": c.get("httpMethod"), "url": c.get("normUrl"),
                })

    for dn in fe.get("directNetCalls", []):
        if dn.get("normUrl"):
            key = (dn.get("httpMethod"), dn.get("normUrl"))
            if key in be.route_handlers:
                matched_be_keys.add(key)
            else:
                broken_fe_calls.append({
                    "file": dn["callerFile"], "line": dn["line"], "symbol": dn["kind"],
                    "method": dn.get("httpMethod"), "url": dn.get("normUrl"),
                })
        else:
            broken_fe_calls.append({
                "file": dn["callerFile"], "line": dn["line"], "symbol": dn["kind"],
                "detail": dn.get("blindReason"),
            })

    # 按能力聚合切片 & 按路由收集事件链
    cap_handlers: Dict[str, Set[str]] = defaultdict(set)
    cap_pages: Dict[str, Set[str]] = defaultdict(set)
    cap_components: Dict[str, Set[str]] = defaultdict(set)
    cap_names: Dict[str, str] = {}

    for r in fe.get("routes", []):
        page_rel = r.get("targetFile")
        if not page_rel:
            continue
        page_short = short_fe(page_rel)
        cap_id, cap_name = ROUTE_TO_CAPABILITY.get(r["path"], (page_short, r["component"]))
        cap_names[cap_id] = cap_name
        cap_pages[cap_id].add(page_rel)

        # 组件递归收集
        sub_files: Set[str] = set()
        cq = deque([page_short])
        while cq:
            cur = cq.popleft()
            if cur in sub_files or cur == "api/index.ts":
                continue
            sub_files.add(cur)
            if cur != page_short:
                cap_components[cap_id].add(f"frontend/src/{cur}")
            for nxt in internal_adj.get(cur, []):
                if nxt not in sub_files and nxt != "api/index.ts":
                    cq.append(nxt)

        # 该页面及关联组件的后端入口函数
        page_handlers: Set[str] = set()
        for c in fe.get("symbolCalls", []):
            if c["kind"] == "api_call" and short_fe(c["callerFile"]) in sub_files:
                h = be.route_handlers.get((c.get("httpMethod"), c.get("normUrl")))
                if h:
                    page_handlers.add(h)
                    cap_handlers[cap_id].add(h)

        # 事件与生命周期完整链路
        chains: List[Dict] = []
        seen_chain = set()
        for c in fe.get("symbolCalls", []):
            if c["callerFile"] == page_rel and c["kind"] == "api_call" and "useEffect" in c["callerChain"]:
                h = be.route_handlers.get((c.get("httpMethod"), c.get("normUrl")))
                sig = ("lifecycle", c["callerChain"], c["targetSymbol"], h)
                if sig not in seen_chain:
                    seen_chain.add(sig)
                    chains.append({
                        "trigger": f"[生命周期] {c['callerChain']}",
                        "fe_fn": c["callerFunction"],
                        "api_fn": c["targetSymbol"],
                        "http": f"{c.get('httpMethod')} {c.get('normUrl')}",
                        "be_handler": short_be(h) if h else "MISSING",
                    })

        for ev in fe.get("reactEvents", []):
            if ev["callerFile"] != page_rel:
                continue
            apis = []
            if ev["handlerKind"] == "named_fn":
                apis = resolve_fn_to_apis(page_rel, ev["handlerName"])
                fe_fn_disp = ev["handlerName"]
            elif ev["handlerKind"] == "inline_arrow":
                apis = resolve_fn_to_apis(page_rel, f"<{ev['jsxTag']}>.{ev['eventProp']}@L{ev['line']}")
                fe_fn_disp = f"() => {ev['handlerName']}"
            else:
                continue
            for a in apis:
                h = be.route_handlers.get((a.get("httpMethod"), a.get("normUrl")))
                sig = ("event", ev["jsxTag"], ev["eventProp"], fe_fn_disp, a["targetSymbol"], h)
                if sig not in seen_chain:
                    seen_chain.add(sig)
                    chains.append({
                        "trigger": f"[L{ev['line']}] <{ev['jsxTag']} {ev['eventProp']}>",
                        "fe_fn": fe_fn_disp,
                        "api_fn": a["targetSymbol"],
                        "http": f"{a.get('httpMethod')} {a.get('normUrl')}",
                        "be_handler": short_be(h) if h else "MISSING",
                    })

        route_chains.append({
            "route": r["path"],
            "cap_id": cap_id,
            "component": r["component"],
            "page_file": page_rel,
            "handlers": sorted(short_be(h) for h in page_handlers),
            "chains": chains,
        })

    # 能力级纵向切片汇总
    capability_slices: Dict[str, Dict] = {}
    for cap_id in sorted(cap_names.keys()):
        handlers = sorted(cap_handlers[cap_id])
        sl = be.trace_slice(handlers)
        capability_slices[cap_id] = {
            "name": cap_names[cap_id],
            "pages": sorted(cap_pages[cap_id]),
            "components": sorted(cap_components[cap_id]),
            "be_handlers": [short_be(h) for h in handlers],
            "api_modules": sorted(m.replace("backend.app.", "") for m in sl["modules"] if m.startswith("backend.app.api")),
            "services": sorted(m.replace("backend.app.", "") for m in sl["modules"] if m.startswith("backend.app.services")),
            "engines": sorted(m.replace("backend.app.", "") for m in sl["modules"] if m.startswith("backend.app.engine")),
            "uses_adapter": sl["uses_adapter"],
            "orms": sl["orms"],
            "data_dirs": sl["data_dirs"],
            "direct_ext_net": sl["direct_ext_net"],
        }

    unused_api_exports = [
        {"symbol": k, "method": v["httpMethod"], "url": v["normUrl"]}
        for k, v in sorted(fe.get("apiMethods", {}).items())
        if k not in used_api_symbols
    ]
    backend_only_routes = [
        r for r in be.all_routes if (r["method"], r["norm"]) not in matched_be_keys
    ]

    # 模块依赖邻接表
    be_module_graph: Dict[str, List[str]] = {}
    for caller in sorted(be.mod_to_path.keys()):
        sc = short_be(caller)
        if sc.startswith(("schemas", "config")):
            continue
        targets = sorted(
            short_be(t) for t in be.mod_edges.get(caller, ())
            if not any(t == nm or t.startswith(nm + ".") for nm in NOISE_MODULES)
        )
        if targets:
            be_module_graph[sc] = targets

    # 共享底座与共享引擎自动识别（统计被 >=2 个业务切片及 >=2 个上游模块复用的引擎，并审计其是否复用 adapters）
    direct_callers: Dict[str, Set[str]] = defaultdict(set)
    for caller, targets in be_module_graph.items():
        if caller in ("engine", "cli"):
            continue
        for tgt in targets:
            direct_callers[tgt].add(caller)

    shared_bases: List[Dict[str, Any]] = [
        {
            "id": "adapters",
            "path": "backend/app/adapters",
            "kind": "adapter_base",
            "capabilities": sorted(cid for cid, sl in capability_slices.items() if sl["uses_adapter"]),
            "uses_adapters": True,
            "direct_ext_net": [],
            "status": "统一模型接入底座",
        }
    ]
    bypass_by_mod: Dict[str, List[str]] = defaultdict(list)
    for item in be.external_network_hits:
        fpath = item["file"]
        if fpath.startswith("backend/app/") and fpath.endswith(".py"):
            mshort = fpath[len("backend/app/"):-3].replace("/", ".")
            bypass_by_mod[mshort].append(item["detail"])

    all_engines = sorted({e for sl in capability_slices.values() for e in sl["engines"]})
    for eng in all_engines:
        caps_using = sorted(cid for cid, sl in capability_slices.items() if eng in sl["engines"])
        callers = sorted(direct_callers.get(eng, ()))
        if len(caps_using) >= 2 and len(callers) >= 2:
            # 沿模块依赖图向下遍历引擎自身触达的底座与外网直连
            sub_q = deque([eng])
            sub_seen: Set[str] = set()
            eng_uses_adapter = False
            eng_ext_bypasses: List[str] = []
            while sub_q:
                cur_m = sub_q.popleft()
                if cur_m in sub_seen:
                    continue
                sub_seen.add(cur_m)
                if cur_m == "adapters" or cur_m.startswith("adapters."):
                    eng_uses_adapter = True
                if cur_m in bypass_by_mod:
                    eng_ext_bypasses.extend(f"{cur_m}:{d}" for d in bypass_by_mod[cur_m])
                for nxt_m in be_module_graph.get(cur_m, []):
                    if nxt_m not in sub_seen:
                        sub_q.append(nxt_m)

            if eng_ext_bypasses:
                status_text = "⚠️ 子链存在绕过 adapters 直连外网"
            elif eng_uses_adapter:
                status_text = "✅ 复用 adapters 底座"
            else:
                status_text = "ℹ️ 本地算法/ORM（无需外部模型底座）"

            shared_bases.append({
                "id": eng,
                "path": f"backend/app/{eng.replace('.', '/')}.py",
                "kind": "shared_engine",
                "capabilities": caps_using,
                "direct_callers": callers,
                "uses_adapters": eng_uses_adapter,
                "direct_ext_net": sorted(set(eng_ext_bypasses)),
                "status": status_text,
            })

    # self.xxx 推导摘要（排除 schemas/models）
    self_attrs_summary = {}
    for qname, attrs in sorted(be.self_attr_types.items()):
        if qname.startswith(("backend.app.schemas.", "backend.app.models.")):
            continue
        proj_attrs = {k: v for k, v in attrs.items() if v in be.class_name_to_qnames}
        if proj_attrs:
            self_attrs_summary[short_be(qname)] = proj_attrs

    facts = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "frontend_engine": fe.get("engine"),
        "health": {
            "cycles": [[short_be(x) for x in c] for c in be.find_cycles()],
            "be_islands": be.find_islands(),
            "fe_orphans": fe_orphans,
            "external_network_bypasses": be.external_network_hits,
            "be_blind_spots": be.blind_spots,
            "defensive_getattr_ignored": be.defensive_getattr_count,
            "broken_fe_calls": broken_fe_calls,
            "unused_api_exports": unused_api_exports,
            "backend_only_routes": backend_only_routes,
            "be_route_count": len(be.all_routes),
            "fe_api_export_count": len(fe.get("apiMethods", {})),
            "fe_used_api_count": len(used_api_symbols),
        },
        "backend_module_graph": be_module_graph,
        "orm_models": sorted(be.orm_models),
        "self_attr_types": self_attrs_summary,
        "frontend_routes": fe.get("routes", []),
        "frontend_internal_graph": dict(sorted(internal_adj.items())),
        "jsx_renders": fe.get("jsxRenders", []),
        "capability_slices": capability_slices,
        "shared_bases": shared_bases,
        "route_chains": route_chains,
        "backend_call_graph": {
            "fn_callees": {k: sorted(v) for k, v in be.fn_callees.items() if v},
            "fn_orm_uses": {k: sorted(v) for k, v in be.fn_orm_uses.items() if v},
            "fn_data_io": {k: sorted(v) for k, v in be.fn_data_io.items() if v},
            "fn_net_calls": {k: sorted(v) for k, v in be.fn_net_calls.items() if v},
        },
    }

    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    FACTS_JSON.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    FACTS_MD.write_text(render_facts_markdown(facts), encoding="utf-8")
    return facts


def render_facts_markdown(facts: Dict) -> str:
    h = facts["health"]
    lines: List[str] = [
        "# 代码事实真相清单 (FACTS.md)",
        "",
        f"> 自动生成时间：`{facts['generated_at']}` | 前端解析引擎：`{facts['frontend_engine']}`  ",
        "> **用途**：画 L1/L2/L3 系统地图与修改代码前的**唯一真实依赖底稿**（由 `python docs/map/build.py --facts` 生成）。",
        "",
        "---",
        "",
        "## 0. 架构健康与红线体检摘要",
        "",
        f"- **后端循环依赖**：`{len(h['cycles'])}` 条",
        f"- **后端零入度孤岛**：`{len(h['be_islands'])}` 个",
        f"- **前端未挂载孤立文件**（从 `main.tsx` 不可达）：`{len(h['fe_orphans'])}` 个" + (f" (`{', '.join(h['fe_orphans'])}`)" if h["fe_orphans"] else ""),
        f"- **绕过 `adapters/` 直连外部网络**：`{len(h['external_network_bypasses'])}` 处命中",
        f"- **前后端接口对齐**：后端路由 `{h['be_route_count']}` 条，前端 `api/index.ts` 导出 `{h['fe_api_export_count']}` 个（页面调用 `{h['fe_used_api_count']}` 个），前端断头路 `{len(h['broken_fe_calls'])}` 处",
        f"- **后端静态盲区**（已忽略 `{h['defensive_getattr_ignored']}` 处防御性 `getattr`）：`{len(h['be_blind_spots'])}` 处",
        "",
    ]

    if h["external_network_bypasses"]:
        lines.append("### ⚠️ 绕过 adapters 底座直连外部系统（L1/L2 必须画连线）")
        lines.append("")
        for item in h["external_network_bypasses"]:
            lines.append(f"- `{item['file']}:{item['line']}` → `{item['detail']}`")
        lines.append("")

    if facts.get("shared_bases"):
        lines.append("### 🏛️ 共用底座与共享引擎复用审计（自动识别跨切片共享模块）")
        lines.append("")
        lines.append("| 模块路径 | 类型 | 共用业务切片 | 底座复用检测 |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for sb in facts["shared_bases"]:
            kind_lbl = "基础底座" if sb["kind"] == "adapter_base" else "共享引擎"
            caps_lbl = ", ".join(sb.get("capabilities", [])) or "无"
            lines.append(f"| `{sb['path']}` | {kind_lbl} | {caps_lbl} | {sb['status']} |")
        lines.append("")

    if h["be_blind_spots"]:
        lines.append("### 后端未注解/动态调用盲区明细")
        lines.append("")
        for b in h["be_blind_spots"]:
            lines.append(f"- `{b['file']}:{b['line']}` [{b['kind']}] `{b['detail']}`")
        lines.append("")

    # 1. 六大业务能力纵向切片总表（L1/L2 底稿）
    lines.extend([
        "---",
        "",
        "## 1. 业务能力纵向切片总表（L1 / L2 画图唯一底稿）",
        "",
        "| 能力 ID | 名称 | 前端页面 & 组件 | 编排服务 | 触达核心引擎 | SQLite ORM 表 | 数据目录 | 外部系统 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ])
    for cap_id, sl in facts["capability_slices"].items():
        pages = ", ".join(p.replace("frontend/src/pages/", "") for p in sl["pages"])
        comps = ", ".join(c.replace("frontend/src/components/", "") for c in sl["components"])
        fe_str = pages + (f" (+{comps})" if comps else "")
        svc_str = ", ".join(s.replace("services.", "") for s in sl["services"]) or "无"
        eng_str = ", ".join(e.replace("engine.", "") for e in sl["engines"]) or "无"
        orm_str = ", ".join(sl["orms"]) or "无"
        dir_str = ", ".join(sl["data_dirs"]) or "无"
        ext_parts = []
        if sl["uses_adapter"]:
            ext_parts.append("经 adapters")
        if sl["direct_ext_net"]:
            ext_parts.append("⚠️直连:" + ", ".join(sl["direct_ext_net"]))
        ext_str = "; ".join(ext_parts) or "无"
        lines.append(f"| `{cap_id}` | {sl['name']} | {fe_str} | {svc_str} | {eng_str} | {orm_str} | {dir_str} | {ext_str} |")

    # 2. 后端模块真实依赖图
    lines.extend([
        "",
        "---",
        "",
        "## 2. 后端模块真实依赖邻接表（已剥离 `schemas` / `config`）",
        "",
        "```text",
    ])
    for caller, targets in facts["backend_module_graph"].items():
        lines.append(f"{caller:30s} --> {', '.join(targets)}")
    lines.append("```")

    # 3. 前端内部模块与 JSX 组件渲染关系
    lines.extend([
        "",
        "---",
        "",
        "## 3. 前端路由、模块引用与 JSX 组件渲染（已过滤 `import type` 与 `types/`）",
        "",
        "```text",
    ])
    seen_jsx = set()
    for jr in facts["jsx_renders"]:
        sig = (jr["callerFile"], jr["callerChain"], jr["targetSymbol"], jr["targetFile"])
        if sig in seen_jsx:
            continue
        seen_jsx.add(sig)
        cf = jr["callerFile"].replace("frontend/src/", "")
        tf = jr["targetFile"].replace("frontend/src/", "")
        lines.append(f"{cf:26s} [{jr['callerChain']}] --> <{jr['targetSymbol']} /> ({tf})")
    lines.append("```")

    # 4. 端到端功能链路（页面事件/生命周期 -> 前端函数 -> API -> 后端路由函数）
    lines.extend([
        "",
        "---",
        "",
        "## 4. 端到端功能链路（UI 事件 / 生命周期 → 前端函数 → API → 后端路由函数）",
        "",
    ])
    for rc in facts["route_chains"]:
        pf = rc["page_file"].replace("frontend/src/", "")
        lines.append(f"### `{rc['route']}` — {rc['component']} (`{pf}`)")
        lines.append("")
        if not rc["chains"]:
            lines.append("- （无直接 API 调用事件）")
        else:
            for ch in rc["chains"]:
                lines.append(f"- {ch['trigger']} → `{ch['fe_fn']}` → `{ch['api_fn']}()` → `{ch['http']}` → `{ch['be_handler']}`")
        lines.append("")

    # 5. self.xxx 推导与冗余接口列表
    lines.extend([
        "---",
        "",
        "## 5. `self.xxx` 属性类型推导与未调用接口明细",
        "",
        "### `self.xxx` 已推导类属性",
        "",
    ])
    for cls_q, attrs in facts["self_attr_types"].items():
        lines.append(f"- `{cls_q}`: `{attrs}`")

    lines.extend(["", "### 未被前端调用的后端路由 / `api/index.ts` 方法", ""])
    for u in h["unused_api_exports"]:
        lines.append(f"- 前端未调导出：`{u['symbol']}` (`{u['method']} {u['url']}`)")
    for r in h["backend_only_routes"]:
        lines.append(f"- 仅后端存在：`[{r['method']}] {r['raw']}` (`{r['file']}`)")
    lines.append("")
    return "\n".join(lines)


def verify_maps_against_facts(diagrams: List[Dict], facts: Dict, strict: bool = False) -> Tuple[List[str], List[str]]:
    """
    对照代码事实校验系统地图，返回 (errors, warnings)：
    - 若代码存在循环依赖或前端断头路调用，记为 error；
    - 若 L1/L2 地图存在，校验外部直连防漏与 L2 纵向切片防漂移（默认 warning，--strict-facts 升为 error）。
    """
    errors: List[str] = []
    warnings: List[str] = []
    h = facts["health"]

    if h["cycles"]:
        errors.append(f"后端存在 {len(h['cycles'])} 条循环依赖: {h['cycles'][0]}")
    if h["broken_fe_calls"]:
        for bc in h["broken_fe_calls"]:
            errors.append(f"前端存在未对齐后端路由的调用: {bc['file']}:{bc['line']} ({bc.get('symbol')})")

    diag_by_id = {d["rel_name"]: d for d in diagrams if d.get("category") != "changes"}
    if not diag_by_id:
        return errors, warnings

    # 检查 L1 外部网络防漏（非 adapters 直连外网点必须在 L1 有所体现）
    l1 = diag_by_id.get("L1-overview.mmd")
    if l1 and h["external_network_bypasses"]:
        l1_raw = l1.get("raw_text", "")
        bypass_files = sorted({item["file"] for item in h["external_network_bypasses"]})
        for bf in bypass_files:
            short_name = pathlib.Path(bf).stem
            if bf not in l1_raw and short_name not in l1_raw:
                msg = f"[L1-overview.mmd] 代码存在绕过 adapters 直连外网的文件 `{bf}`，但 L1 未见该节点/标注。"
                (errors if strict else warnings).append(msg)

    # 检查各 L2-<cap>.mmd 与能力切片的漂移（双向：漏画 + 多画/过期；支持跨能力 %% link 边界止步）
    orm_models = set(facts.get("orm_models", []))
    l2_path_re = re.compile(r"backend/app/(services|engine)/([a-zA-Z0-9_]+)\.py")
    call_graph = facts.get("backend_call_graph", {})

    for cap_id, sl in facts["capability_slices"].items():
        l2_id = f"L2-{cap_id}.mmd"
        l2 = diag_by_id.get(l2_id)
        if not l2:
            continue
        l2_raw = l2.get("raw_text", "")

        # 识别本 L2 中通过 `%% link: node -> L2-<other>.mmd` 显式下钻给其他能力或共用引擎底座的边界模块
        stop_modules: Set[str] = set()
        node_map = {n["id"]: n for n in l2.get("nodes", [])}
        for nid, target_file in l2.get("links", {}).items():
            if not (target_file.startswith("L2-") and target_file.endswith(".mmd")):
                continue
            target_cap = target_file[3:-4]
            if target_cap == cap_id or target_file not in diag_by_id:
                continue
            nobj = node_map.get(nid)
            if not nobj or len(nobj.get("lines", [])) < 2:
                continue
            rel_path = nobj["lines"][1].split(":", 1)[0].strip()
            if rel_path.startswith("backend/app/") and rel_path.endswith(".py"):
                mod = "backend.app." + rel_path[len("backend/app/"):-3].replace("/", ".")
                stop_modules.add(mod)

        if stop_modules and call_graph:
            bounded_sl = trace_slice_from_graph(
                entry_fns=[f"backend.app.{h}" for h in sl.get("be_handlers", [])],
                fn_callees=call_graph.get("fn_callees", {}),
                fn_orm_uses=call_graph.get("fn_orm_uses", {}),
                fn_data_io=call_graph.get("fn_data_io", {}),
                fn_net_calls=call_graph.get("fn_net_calls", {}),
                stop_modules=stop_modules,
            )
            eff_services = sorted(m.replace("backend.app.", "") for m in bounded_sl["modules"] if m.startswith("backend.app.services"))
            eff_engines = sorted(m.replace("backend.app.", "") for m in bounded_sl["modules"] if m.startswith("backend.app.engine"))
            eff_orms = bounded_sl["orms"]
        else:
            eff_services = sl["services"]
            eff_engines = sl["engines"]
            eff_orms = sl["orms"]

        expected_svcs = set(eff_services)
        expected_engs = set(eff_engines)
        expected_orms = set(eff_orms)

        # 正向检查：代码有但 L2 没画
        for svc in eff_services:
            svc_path = f"backend/app/{svc.replace('.', '/')}.py"
            if svc_path not in l2_raw:
                msg = f"[{l2_id}] 漏画：代码切片触达编排服务 `{svc_path}`，但 L2 图中未体现。"
                (errors if strict else warnings).append(msg)
        for eng in eff_engines:
            eng_path = f"backend/app/{eng.replace('.', '/')}.py"
            if eng_path not in l2_raw:
                msg = f"[{l2_id}] 漏画：代码切片触达核心引擎 `{eng_path}`，但 L2 图中未体现。"
                (errors if strict else warnings).append(msg)
        for orm in eff_orms:
            if orm not in l2_raw:
                msg = f"[{l2_id}] 漏画：代码切片读写 SQLite 表 `{orm}`，但 L2 图中未见标注。"
                (errors if strict else warnings).append(msg)

        # 反向检查：L2 画了但代码切片未触达（多画/过期模块或表）
        seen_paths = set()
        for m in l2_path_re.finditer(l2_raw):
            layer, mod_stem = m.group(1), m.group(2)
            if mod_stem == "__init__":
                continue
            mod_key = f"{layer}.{mod_stem}"
            full_path = f"backend/app/{layer}/{mod_stem}.py"
            if full_path in seen_paths:
                continue
            seen_paths.add(full_path)
            if mod_key not in expected_svcs and mod_key not in expected_engs:
                msg = f"[{l2_id}] 疑似多画/过期：图中标注了 `{full_path}`，但代码事实显示该切片未触达。"
                (errors if strict else warnings).append(msg)

        for orm in sorted(orm_models):
            if re.search(rf"\b{re.escape(orm)}\b", l2_raw) and orm not in expected_orms:
                msg = f"[{l2_id}] 疑似多画/过期：图中标注了数据表 `{orm}`，但代码事实显示该切片未读写该表。"
                (errors if strict else warnings).append(msg)

    # 检查 L2-cli.mmd 与 cli/catalog.py 真实路由及客户端分层覆盖对齐
    cli_l2 = diag_by_id.get("L2-cli.mmd")
    cli_catalog = PROJECT_ROOT / "cli" / "catalog.py"
    if cli_l2 and cli_catalog.exists():
        cli_l2_raw = cli_l2.get("raw_text", "")
        cat_tree = ast.parse(cli_catalog.read_text(encoding="utf-8"), filename=str(cli_catalog))
        expected_api_paths: Set[str] = set()
        for node in ast.walk(cat_tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CommandSpec"):
                continue
            ep_val: Optional[str] = None
            for kw in node.keywords:
                if kw.arg == "endpoint" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    ep_val = kw.value.value
                    break
            if ep_val is None and len(node.args) >= 6:
                arg5 = node.args[5]
                if isinstance(arg5, ast.Constant) and isinstance(arg5.value, str):
                    ep_val = arg5.value
            if not ep_val:
                continue
            if ep_val == "/health":
                expected_api_paths.add("backend/app/main.py")
            elif ep_val.startswith("/api/v1/"):
                parts = ep_val.split("/")
                if len(parts) >= 4 and parts[3]:
                    expected_api_paths.add(f"backend/app/api/{parts[3]}.py")

        for req_path in sorted(expected_api_paths | {"cli/main.py", "cli/policy.py", "cli/commands", "cli/client.py"}):
            if req_path not in cli_l2_raw:
                msg = f"[L2-cli.mmd] 漏画：CLI 触达或依赖 `{req_path}`，但 L2 图中未体现。"
                (errors if strict else warnings).append(msg)

        for m in re.finditer(r"backend/app/api/([a-zA-Z0-9_]+)\.py", cli_l2_raw):
            api_path = f"backend/app/api/{m.group(1)}.py"
            if api_path not in expected_api_paths:
                msg = f"[L2-cli.mmd] 疑似多画/过期：图中标注了 `{api_path}`，但 cli/catalog.py 未调用该路由模块。"
                (errors if strict else warnings).append(msg)

    return errors, warnings


if __name__ == "__main__":
    data = extract_and_save_facts()
    print(f"已写入 {FACTS_JSON.relative_to(PROJECT_ROOT).as_posix()} 与 {FACTS_MD.relative_to(PROJECT_ROOT).as_posix()}")
    print(f"能力切片: {len(data['capability_slices'])} 个 | 循环依赖: {len(data['health']['cycles'])} | 断头路: {len(data['health']['broken_fe_calls'])}")
