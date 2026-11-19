// -*- coding: utf-8 -*-
/**
 * 前端 TypeScript 7 Compiler API + TypeChecker 符号级依赖与事件链提取器
 * 输出 JSON 至 stdout，供 docs/map/fact_extractor.py 消费。
 */
import path from "path";
import fs from "fs";
import { fileURLToPath, pathToFileURL } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const ROOT = path.resolve(__dirname, "../..");
const FRONTEND_DIR = path.join(ROOT, "frontend");
const SRC_DIR = path.join(FRONTEND_DIR, "src");

const syncApiMod = await import(
  pathToFileURL(path.join(FRONTEND_DIR, "node_modules/typescript/dist/api/sync/api.js")).href
);
const ast = await import(
  pathToFileURL(path.join(FRONTEND_DIR, "node_modules/typescript/dist/ast/index.js")).href
);

function toRel(absPath) {
  return path.relative(ROOT, absPath).replace(/\\/g, "/");
}

function normalizeUrl(raw) {
  let p = raw.replace(/\$\{[^}]+\}/g, "{}").replace(/\{[^}]+\}/g, "{}");
  p = p.replace(/\/+/g, "/").trim();
  if (p.length > 1 && p.endsWith("/")) {
    p = p.slice(0, -1);
  }
  return p;
}

function getSourceFileOfNode(node) {
  let cur = node;
  while (cur && !cur.fileName) {
    cur = cur.parent;
  }
  return cur || null;
}

function extractUrlText(expr, sf) {
  if (!expr) return { ok: false, reason: "empty" };
  if (ast.isStringLiteral(expr) || ast.isNoSubstitutionTemplateLiteral(expr)) {
    return { ok: true, raw: expr.text, isDynamicHead: false };
  }
  if (ast.isTemplateExpression(expr)) {
    let out = expr.head.text;
    const isDynamicHead = out === "" && expr.templateSpans.length > 0;
    for (const span of expr.templateSpans) {
      out += "{}" + span.literal.text;
    }
    return { ok: true, raw: out, isDynamicHead };
  }
  return { ok: false, reason: "non_literal:" + expr.getText(sf) };
}

function collectSrcFiles(dir) {
  const out = [];
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) {
      out.push(...collectSrcFiles(full));
    } else if (/\.(ts|tsx)$/.test(ent.name) && !ent.name.endsWith(".d.ts")) {
      out.push(path.resolve(full));
    }
  }
  return out.sort();
}

// 读取 tsconfig 路径别名
const rawTsconfig = fs
  .readFileSync(path.join(FRONTEND_DIR, "tsconfig.json"), "utf-8")
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/^\s*\/\/.*$/gm, "");
const tsconfig = JSON.parse(rawTsconfig);
const tsPaths = (tsconfig.compilerOptions && tsconfig.compilerOptions.paths) || {};

function resolveModuleSpecifier(specText, fromFileAbs) {
  const candidates = [];
  if (specText.startsWith(".")) {
    candidates.push(path.resolve(path.dirname(fromFileAbs), specText));
  } else {
    for (const [aliasPat, targets] of Object.entries(tsPaths)) {
      const prefix = aliasPat.replace(/\*$/, "");
      if (specText.startsWith(prefix)) {
        const rest = specText.slice(prefix.length);
        for (const t of targets) {
          candidates.push(path.resolve(FRONTEND_DIR, t.replace(/\*$/, "") + rest));
        }
      }
    }
  }
  for (const base of candidates) {
    for (const suffix of ["", ".tsx", ".ts", ".jsx", ".js", "/index.tsx", "/index.ts"]) {
      const full = base + suffix;
      if (fs.existsSync(full) && fs.statSync(full).isFile()) {
        const norm = path.resolve(full);
        return {
          target: toRel(norm),
          isInternal: norm.startsWith(SRC_DIR),
          isAsset: /\.(css|scss|png|svg|jpg|jpeg|webp)$/i.test(norm),
        };
      }
    }
  }
  return {
    target: specText,
    isInternal: false,
    isAsset: /\.(css|scss|png|svg|jpg|jpeg|webp)$/i.test(specText),
  };
}

const srcFilesAbs = collectSrcFiles(SRC_DIR);
const api = new syncApiMod.API({ cwd: FRONTEND_DIR });
const snap = api.updateSnapshot({ openProject: path.join(FRONTEND_DIR, "tsconfig.json") });
const proj = snap.getProject(path.join(FRONTEND_DIR, "tsconfig.json"));
const checker = proj.checker;

function resolveInternalSymbol(node) {
  if (!node) return null;
  let sym = checker.getSymbolAtLocation(node);
  if (!sym) return null;
  if (sym.flags & syncApiMod.SymbolFlags.Alias) {
    try {
      const aliased = checker.getAliasedSymbol(sym);
      if (aliased) sym = aliased;
    } catch (_) {}
  }
  const declHandle = sym.valueDeclaration || (sym.declarations && sym.declarations[0]);
  if (!declHandle || typeof declHandle.resolve !== "function") return null;
  const declNode = declHandle.resolve();
  const declSf = getSourceFileOfNode(declNode);
  if (!declSf || !declSf.fileName) return null;
  const absTarget = path.resolve(declSf.fileName);
  if (!absTarget.startsWith(SRC_DIR)) return null;
  const targetRel = toRel(absTarget);
  if (targetRel.startsWith("frontend/src/types/")) return null;
  return {
    symbolName: sym.name,
    targetFile: targetRel,
    declLine: declSf.getLineAndCharacterOfPosition(declNode.getStart(declSf)).line + 1,
  };
}

// 1. 解析 api/index.ts 导出方法 -> HTTP 端点
const apiMethods = {};
let axiosBaseUrl = "";
const apiIndexSf = proj.program.getSourceFile(path.join(SRC_DIR, "api/index.ts").replace(/\\/g, "/"));
if (apiIndexSf) {
  function walkApi(node) {
    if (
      ast.isCallExpression(node) &&
      ast.isPropertyAccessExpression(node.expression) &&
      node.expression.expression.getText(apiIndexSf) === "axios" &&
      node.expression.name.text === "create" &&
      node.arguments.length > 0 &&
      ast.isObjectLiteralExpression(node.arguments[0])
    ) {
      for (const prop of node.arguments[0].properties) {
        if (
          ast.isPropertyAssignment(prop) &&
          prop.name.getText(apiIndexSf) === "baseURL" &&
          ast.isStringLiteral(prop.initializer)
        ) {
          axiosBaseUrl = prop.initializer.text;
        }
      }
    }
    if (
      ast.isVariableStatement(node) &&
      node.modifiers &&
      node.modifiers.some((m) => m.kind === ast.SyntaxKind.ExportKeyword)
    ) {
      for (const decl of node.declarationList.declarations) {
        if (ast.isIdentifier(decl.name) && decl.initializer && ast.isObjectLiteralExpression(decl.initializer)) {
          const objName = decl.name.text;
          for (const prop of decl.initializer.properties) {
            if (ast.isPropertyAssignment(prop) || ast.isMethodDeclaration(prop)) {
              const methName = ast.isIdentifier(prop.name) ? prop.name.text : prop.name.getText(apiIndexSf);
              const fullKey = `${objName}.${methName}`;
              const line = apiIndexSf.getLineAndCharacterOfPosition(prop.getStart(apiIndexSf)).line + 1;
              function scanProp(bnode) {
                if (ast.isCallExpression(bnode)) {
                  const callee = bnode.expression;
                  if (
                    ast.isPropertyAccessExpression(callee) &&
                    callee.expression.getText(apiIndexSf) === "api" &&
                    ["get", "post", "put", "delete", "patch"].includes(callee.name.text)
                  ) {
                    const httpM = callee.name.text.toUpperCase();
                    const urlInfo = extractUrlText(bnode.arguments[0], apiIndexSf);
                    if (urlInfo.ok) {
                      const rawFull = urlInfo.raw.startsWith("/")
                        ? axiosBaseUrl + urlInfo.raw
                        : axiosBaseUrl + "/" + urlInfo.raw;
                      apiMethods[fullKey] = {
                        httpMethod: httpM,
                        rawUrl: rawFull,
                        normUrl: normalizeUrl(rawFull),
                        line,
                        isDynamicHead: urlInfo.isDynamicHead,
                      };
                    }
                  } else if (ast.isIdentifier(callee) && callee.text === "fetch") {
                    const urlInfo = extractUrlText(bnode.arguments[0], apiIndexSf);
                    let httpM = "GET";
                    if (bnode.arguments[1] && ast.isObjectLiteralExpression(bnode.arguments[1])) {
                      for (const p2 of bnode.arguments[1].properties) {
                        if (
                          ast.isPropertyAssignment(p2) &&
                          p2.name.getText(apiIndexSf) === "method" &&
                          ast.isStringLiteral(p2.initializer)
                        ) {
                          httpM = p2.initializer.text.toUpperCase();
                        }
                      }
                    }
                    if (urlInfo.ok) {
                      apiMethods[fullKey] = {
                        httpMethod: httpM,
                        rawUrl: urlInfo.raw,
                        normUrl: normalizeUrl(urlInfo.raw),
                        line,
                        isDynamicHead: urlInfo.isDynamicHead,
                      };
                    }
                  }
                }
                bnode.forEachChild(scanProp);
              }
              scanProp(prop);
            }
          }
        }
      }
    }
    node.forEachChild(walkApi);
  }
  walkApi(apiIndexSf);
}

// 2. 扫描模块 import / re-export / Route / JSX组件渲染 / React事件 / 符号调用
const moduleImports = {};
const reExports = {};
const routes = [];
const jsxRenders = [];
const symbolCalls = [];
const directNetCalls = [];
const reactEvents = [];
const localFnCalls = {};

for (const absFile of srcFilesAbs) {
  const sf = proj.program.getSourceFile(absFile.replace(/\\/g, "/"));
  if (!sf) continue;
  const rel = toRel(absFile);
  moduleImports[rel] = [];
  reExports[rel] = [];

  for (const stmt of sf.statements) {
    if (ast.isImportDeclaration(stmt)) {
      const clause = stmt.importClause;
      const specText = ast.isStringLiteral(stmt.moduleSpecifier) ? stmt.moduleSpecifier.text : "";
      if (!specText) continue;
      if (clause && clause.isTypeOnly) continue;

      const resolved = resolveModuleSpecifier(specText, absFile);
      if (resolved.target.startsWith("frontend/src/types/") || resolved.isAsset) continue;

      const specs = [];
      if (clause) {
        if (clause.name) specs.push(clause.name.text);
        if (clause.namedBindings) {
          if (ast.isNamespaceImport(clause.namedBindings)) {
            specs.push("* as " + clause.namedBindings.name.text);
          } else if (ast.isNamedImports(clause.namedBindings)) {
            for (const el of clause.namedBindings.elements) {
              if (el.isTypeOnly) continue;
              const localName = el.name.text;
              const origName = el.propertyName ? el.propertyName.text : localName;
              specs.push(origName === localName ? localName : `${origName} as ${localName}`);
            }
          }
        }
      } else {
        specs.push("(side-effect)");
      }
      if (specs.length > 0) {
        moduleImports[rel].push({ target: resolved.target, isInternal: resolved.isInternal, specifiers: specs });
      }
    } else if (ast.isExportDeclaration(stmt) && stmt.moduleSpecifier && !stmt.isTypeOnly) {
      const specText = ast.isStringLiteral(stmt.moduleSpecifier) ? stmt.moduleSpecifier.text : "";
      const resolved = resolveModuleSpecifier(specText, absFile);
      if (resolved.isInternal && !resolved.target.startsWith("frontend/src/types/")) {
        reExports[rel].push({ target: resolved.target, text: stmt.getText(sf).trim() });
      }
    }
  }

  if (rel.startsWith("frontend/src/types/") || rel === "frontend/src/api/index.ts") continue;

  localFnCalls[rel] = {};

  function getEnclosingFnName(node) {
    if (ast.isFunctionDeclaration(node) && node.name) {
      return node.name.text;
    }
    if ((ast.isArrowFunction(node) || ast.isFunctionExpression(node)) && node.parent) {
      if (ast.isVariableDeclaration(node.parent) && ast.isIdentifier(node.parent.name)) {
        return node.parent.name.text;
      }
      if (ast.isPropertyAssignment(node.parent)) {
        return node.parent.name.getText(sf);
      }
      if (ast.isCallExpression(node.parent) && ast.isIdentifier(node.parent.expression)) {
        return node.parent.expression.text;
      }
      if (ast.isJsxExpression(node.parent) && ast.isJsxAttribute(node.parent.parent)) {
        const attrName = node.parent.parent.name.getText(sf);
        const jsxEl = node.parent.parent.parent?.parent;
        const tag = jsxEl && jsxEl.tagName ? jsxEl.tagName.getText(sf) : "JSX";
        const ln = sf.getLineAndCharacterOfPosition(node.parent.parent.getStart(sf)).line + 1;
        return `<${tag}>.${attrName}@L${ln}`;
      }
    }
    return null;
  }

  function walk(node, scopeStack) {
    const newName = getEnclosingFnName(node);
    const nextStack = newName ? [...scopeStack, newName] : scopeStack;
    const callerLabel = nextStack.length > 0 ? nextStack.join(" -> ") : "(module)";
    const topComponent = nextStack[0] || "(module)";
    const immediateFn = nextStack[nextStack.length - 1] || "(module)";

    // <Route> 提取
    if (rel === "frontend/src/App.tsx" && (ast.isJsxSelfClosingElement(node) || ast.isJsxOpeningElement(node))) {
      if (node.tagName.getText(sf) === "Route") {
        let routePath = null;
        let elemTagNode = null;
        for (const attr of node.attributes.properties) {
          if (ast.isJsxAttribute(attr) && attr.name) {
            const an = attr.name.getText(sf);
            if (an === "path" && attr.initializer && ast.isStringLiteral(attr.initializer)) {
              routePath = attr.initializer.text;
            } else if (an === "element" && attr.initializer && ast.isJsxExpression(attr.initializer)) {
              const ex = attr.initializer.expression;
              if (ex && ast.isJsxSelfClosingElement(ex)) elemTagNode = ex.tagName;
              else if (ex && ast.isJsxElement(ex)) elemTagNode = ex.openingElement.tagName;
            }
          }
        }
        if (routePath && elemTagNode) {
          const resolved = resolveInternalSymbol(elemTagNode);
          routes.push({
            path: routePath,
            component: elemTagNode.getText(sf),
            targetFile: resolved ? resolved.targetFile : null,
            targetSymbol: resolved ? resolved.symbolName : null,
          });
        }
      }
    }

    // JSX 组件渲染 & React 事件绑定
    if (ast.isJsxSelfClosingElement(node) || ast.isJsxOpeningElement(node)) {
      const tagText = node.tagName.getText(sf);
      if (/^[A-Z]/.test(tagText)) {
        const resolved = resolveInternalSymbol(node.tagName);
        if (resolved && resolved.targetFile !== rel) {
          const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
          jsxRenders.push({
            callerFile: rel,
            callerChain: callerLabel,
            line,
            tagName: tagText,
            targetFile: resolved.targetFile,
            targetSymbol: resolved.symbolName,
          });
        }
      }

      for (const attr of node.attributes.properties) {
        if (ast.isJsxAttribute(attr) && attr.name) {
          const propName = attr.name.getText(sf);
          if (/^on[A-Z]/.test(propName) && attr.initializer && ast.isJsxExpression(attr.initializer) && attr.initializer.expression) {
            const expr = attr.initializer.expression;
            const line = sf.getLineAndCharacterOfPosition(attr.getStart(sf)).line + 1;
            let handlerKind = "expr";
            let handlerName = expr.getText(sf).slice(0, 60);
            if (ast.isIdentifier(expr)) {
              handlerKind = "named_fn";
              handlerName = expr.text;
            } else if (ast.isArrowFunction(expr) || ast.isFunctionExpression(expr)) {
              handlerKind = "inline_arrow";
              const innerCalls = [];
              function scanInline(inNode) {
                if (ast.isCallExpression(inNode)) innerCalls.push(inNode.expression.getText(sf));
                inNode.forEachChild(scanInline);
              }
              scanInline(expr);
              handlerName = innerCalls.join(", ") || "(inline)";
            }
            reactEvents.push({
              callerFile: rel,
              callerComponent: topComponent,
              line,
              jsxTag: tagText,
              eventProp: propName,
              handlerKind,
              handlerName,
            });
          }
        }
      }
    }

    // CallExpression
    if (ast.isCallExpression(node)) {
      const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
      const expr = node.expression;
      const resolved = resolveInternalSymbol(expr);

      if (resolved) {
        if (resolved.targetFile === "frontend/src/api/index.ts") {
          let apiKey = resolved.symbolName;
          if (ast.isPropertyAccessExpression(expr)) {
            const baseResolved = resolveInternalSymbol(expr.expression);
            if (baseResolved) apiKey = `${baseResolved.symbolName}.${resolved.symbolName}`;
          }
          const ep = apiMethods[apiKey];
          const rec = {
            callerFile: rel,
            callerChain: callerLabel,
            callerComponent: topComponent,
            callerFunction: immediateFn,
            line,
            kind: "api_call",
            targetFile: resolved.targetFile,
            targetSymbol: apiKey,
            httpMethod: ep ? ep.httpMethod : null,
            normUrl: ep ? ep.normUrl : null,
          };
          symbolCalls.push(rec);
          (localFnCalls[rel][immediateFn] ||= []).push(rec);
        } else if (resolved.targetFile !== rel) {
          const rec = {
            callerFile: rel,
            callerChain: callerLabel,
            callerComponent: topComponent,
            callerFunction: immediateFn,
            line,
            kind: "cross_file_call",
            targetFile: resolved.targetFile,
            targetSymbol: resolved.symbolName,
          };
          symbolCalls.push(rec);
          (localFnCalls[rel][immediateFn] ||= []).push(rec);
        } else {
          (localFnCalls[rel][immediateFn] ||= []).push({
            kind: "local_call",
            targetSymbol: resolved.symbolName,
            line,
          });
        }
      }

      // 分散 fetch / window.open 直连检查
      if (ast.isIdentifier(expr) && expr.text === "fetch") {
        const urlInfo = extractUrlText(node.arguments[0], sf);
        let httpM = "GET";
        if (node.arguments[1] && ast.isObjectLiteralExpression(node.arguments[1])) {
          for (const p2 of node.arguments[1].properties) {
            if (
              ast.isPropertyAssignment(p2) &&
              p2.name.getText(sf) === "method" &&
              ast.isStringLiteral(p2.initializer)
            ) {
              httpM = p2.initializer.text.toUpperCase();
            }
          }
        }
        directNetCalls.push({
          callerFile: rel,
          callerChain: callerLabel,
          line,
          kind: "direct_fetch",
          httpMethod: httpM,
          rawUrl: urlInfo.ok ? urlInfo.raw : null,
          normUrl: urlInfo.ok ? normalizeUrl(urlInfo.raw) : null,
          blindReason: urlInfo.ok ? (urlInfo.isDynamicHead ? "dynamic_head" : null) : urlInfo.reason,
        });
      } else if (
        ast.isPropertyAccessExpression(expr) &&
        expr.expression.getText(sf) === "window" &&
        expr.name.text === "open"
      ) {
        const urlInfo = extractUrlText(node.arguments[0], sf);
        if (urlInfo.ok && urlInfo.raw.includes("/api/")) {
          directNetCalls.push({
            callerFile: rel,
            callerChain: callerLabel,
            line,
            kind: "window_open",
            httpMethod: "GET",
            rawUrl: urlInfo.raw,
            normUrl: normalizeUrl(urlInfo.raw),
            blindReason: null,
          });
        }
      }
    }

    node.forEachChild((child) => walk(child, nextStack));
  }

  walk(sf, []);
}

api.close();

console.log(
  JSON.stringify(
    {
      tsVersion: "7.0.2",
      axiosBaseUrl,
      srcFiles: srcFilesAbs.map(toRel),
      moduleImports,
      reExports,
      routes,
      apiMethods,
      jsxRenders,
      symbolCalls,
      directNetCalls,
      reactEvents,
      localFnCalls,
    },
    null,
    2
  )
);
