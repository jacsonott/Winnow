"""Which identifiers a piece of JavaScript uses but doesn't define.

There is no build step here, so nothing else in the toolchain knows whether
`static/js/grid.js` actually imports the `toast` it calls. A missed import is
a ReferenceError on a code path that may only run when an analyst hits an
error branch six months from now — exactly the class of bug the browser tests
can't be relied on to cover, because they exercise a fraction of the surface.

So: parse each module, walk it with real lexical scoping, and collect every
identifier that resolves to nothing local. `test_static_syntax.py` asserts
that set is a subset of (this module's imports) ∪ (browser globals). That's a
mechanical proof for the whole file, not a sample of it.

Scoping implemented here, deliberately narrow to what this codebase uses:
function scope for `var`/params/function declarations, block scope for
`let`/`const`/`class`, catch params, and the function-expression name
binding. Identifiers in non-reference positions — `a.b`, `{a: 1}`, labels —
are not references and are skipped.
"""

from __future__ import annotations

import re

import esprima

# Rewrites applied before parsing: esprima (the Python port) is ES2017 and
# this codebase uses three newer syntaxes. Same table as test_static_syntax.
ES2017_REWRITES = [
    (re.compile(r"catch\s*\{"), "catch (e) {"),
    (re.compile(r"\?\?"), "||"),
    (re.compile(r"\?\."), "."),
]


def parse(src: str, module: bool = False):
    for pattern, replacement in ES2017_REWRITES:
        src = pattern.sub(replacement, src)
    return esprima.parseModule(src) if module else esprima.parseScript(src)


# Everything the app legitimately reaches for that no module defines. Kept
# explicit rather than "anything on window": a typo'd global is the exact
# thing this file exists to catch, and `window.foo` still resolves through
# `window`, which is listed.
BROWSER_GLOBALS = {
    "window", "document", "console", "navigator", "location", "history", "screen",
    "localStorage", "sessionStorage", "fetch", "Headers", "Request", "Response",
    "FormData", "Blob", "File", "FileReader", "URL", "URLSearchParams", "AbortController",
    "XMLHttpRequest", "WebSocket", "EventSource", "ClipboardItem", "DataTransfer",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame", "requestIdleCallback",
    "queueMicrotask", "structuredClone", "getComputedStyle", "matchMedia",
    "alert", "confirm", "prompt", "atob", "btoa", "CustomEvent", "Event", "Node",
    "HTMLElement", "Element", "Image", "Option", "MutationObserver", "ResizeObserver",
    "IntersectionObserver", "performance", "crypto", "TextEncoder", "TextDecoder",
    "CSS", "DOMParser", "XMLSerializer", "canvas", "OffscreenCanvas", "Worker",
    # ECMAScript built-ins
    "Object", "Array", "String", "Number", "Boolean", "Symbol", "BigInt", "Math",
    "JSON", "Date", "RegExp", "Error", "TypeError", "RangeError", "SyntaxError",
    "Map", "Set", "WeakMap", "WeakSet", "Promise", "Proxy", "Reflect", "Intl",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI", "globalThis", "undefined",
    "NaN", "Infinity", "arguments", "this", "eval",
}


class _Scope:
    __slots__ = ("names", "parent", "fn")

    def __init__(self, parent=None, fn=False):
        self.names: set[str] = set()
        self.parent = parent
        self.fn = fn  # function scope (var/params land here) vs block scope

    def declare(self, name, block_scoped=True):
        target = self
        if not block_scoped:  # var / function declaration: nearest function scope
            while not target.fn and target.parent is not None:
                target = target.parent
        target.names.add(name)

    def resolves(self, name):
        s = self
        while s is not None:
            if name in s.names:
                return True
            s = s.parent
        return False


def _pattern_names(node, out):
    """Names bound by a binding pattern — plain, destructured, defaulted, rest."""
    if node is None:
        return
    t = node.type
    if t == "Identifier":
        out.append(node.name)
    elif t == "ObjectPattern":
        for p in node.properties:
            _pattern_names(getattr(p, "value", None) or getattr(p, "argument", None), out)
    elif t == "ArrayPattern":
        for e in node.elements:
            _pattern_names(e, out)
    elif t == "AssignmentPattern":
        _pattern_names(node.left, out)
    elif t == "RestElement":
        _pattern_names(node.argument, out)


def _children(node):
    for key in dir(node):
        if key.startswith("_") or key in ("type", "range", "loc", "toDict"):
            continue
        try:
            value = getattr(node, key)
        except Exception:
            continue
        if isinstance(value, list):
            for item in value:
                if hasattr(item, "type"):
                    yield key, item
        elif hasattr(value, "type"):
            yield key, value


def free_identifiers(src: str, module: bool = False) -> set[str]:
    """Identifiers referenced by `src` that nothing in it declares."""
    tree = parse(src, module=module)
    free: set[str] = set()
    root = _Scope(fn=True)

    def hoist(body, scope):
        """Function and top-level declarations are visible before their line."""
        for stmt in body:
            # `export function f() {}` binds f in the module scope exactly as
            # the bare declaration would — the export wrapper is not a scope.
            if stmt.type in ("ExportNamedDeclaration", "ExportDefaultDeclaration"):
                if getattr(stmt, "declaration", None) is None:
                    continue
                stmt = stmt.declaration
            t = stmt.type
            if t == "FunctionDeclaration" and stmt.id:
                scope.declare(stmt.id.name, block_scoped=False)
            elif t == "ClassDeclaration" and stmt.id:
                scope.declare(stmt.id.name)
            elif t == "VariableDeclaration":
                for d in stmt.declarations:
                    names: list[str] = []
                    _pattern_names(d.id, names)
                    for n in names:
                        scope.declare(n, block_scoped=(stmt.kind != "var"))
            elif t == "ImportDeclaration":
                for spec in stmt.specifiers:
                    scope.declare(spec.local.name)

    def walk(node, scope, parent_key=None):
        t = node.type

        if t == "Identifier":
            if not scope.resolves(node.name):
                free.add(node.name)
            return

        if t in ("FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"):
            inner = _Scope(scope, fn=True)
            # A named function expression can refer to itself by name.
            if t == "FunctionExpression" and node.id:
                inner.declare(node.id.name)
            for p in node.params:
                names: list[str] = []
                _pattern_names(p, names)
                for n in names:
                    inner.declare(n, block_scoped=False)
            # Defaults in params are evaluated in the inner scope.
            for p in node.params:
                if p.type == "AssignmentPattern":
                    walk(p.right, inner)
            body = node.body
            if body.type == "BlockStatement":
                hoist(body.body, inner)
                for stmt in body.body:
                    walk(stmt, inner)
            else:
                walk(body, inner)  # concise arrow body
            return

        if t in ("BlockStatement", "Program"):
            inner = _Scope(scope) if t == "BlockStatement" else scope
            hoist(node.body, inner)
            for stmt in node.body:
                walk(stmt, inner)
            return

        if t == "CatchClause":
            inner = _Scope(scope)
            names = []
            _pattern_names(node.param, names)
            for n in names:
                inner.declare(n)
            walk(node.body, inner)
            return

        if t in ("ForStatement", "ForInStatement", "ForOfStatement"):
            inner = _Scope(scope)
            for key, child in _children(node):
                if key == "init" and child.type == "VariableDeclaration":
                    hoist([child], inner)
                elif key == "left" and child.type == "VariableDeclaration":
                    hoist([child], inner)
            for key, child in _children(node):
                walk(child, inner, key)
            return

        if t == "MemberExpression":
            walk(node.object, scope)
            if node.computed:
                walk(node.property, scope)
            return

        if t == "Property":
            if node.computed:
                walk(node.key, scope)
            walk(node.value, scope)
            return

        if t == "MethodDefinition":
            if node.computed:
                walk(node.key, scope)
            walk(node.value, scope)
            return

        if t in ("LabeledStatement", "BreakStatement", "ContinueStatement"):
            body = getattr(node, "body", None)
            if body is not None:
                walk(body, scope)
            return  # the label itself is not a reference

        if t == "VariableDeclaration":
            for d in node.declarations:
                if d.init is not None:
                    walk(d.init, scope)
                # Computed keys / defaults inside the binding pattern still count.
                if d.id.type in ("ObjectPattern", "ArrayPattern", "AssignmentPattern"):
                    for key, child in _children(d.id):
                        if key in ("right", "value") and child.type not in ("Identifier",):
                            walk(child, scope)
            return

        if t in ("ImportDeclaration", "ExportAllDeclaration"):
            return

        if t == "ExportNamedDeclaration" and node.declaration is None:
            return  # `export { a, b }` — re-exports of names already declared

        for key, child in _children(node):
            walk(child, scope, key)

    hoist(tree.body, root)
    for stmt in tree.body:
        walk(stmt, root)
    return free
