"""
Tween Helper - Shared tween callback utilities for N64 code generation.

This module provides common functions for finding and generating tween
callback functions, used by both trait_generator.py and autoload_generator.py.
"""

from typing import List, Dict, Tuple

from armor64.codegen.ir import nodes_of_type


def capture_ref(c_name: str, is_trait: bool, callback_name: str, param: str) -> str:
    """Where a callback keeps a parameter of its function it captures (a Haxe
    closure keeps its own copy): a field of the trait's data, so each instance
    has its own, or a global in an autoload (one instance); per callback, so
    two callbacks capturing a parameter with the same name don't share it."""
    if is_trait:
        return f"(({c_name}Data*)data)->_cap_{callback_name}_{param}"
    return f"{c_name}_cap_{callback_name}_{param}"


def callback_captures(ir) -> List[Tuple[str, List[Dict]]]:
    """(name, captures) of each callback in an IR tree: tween callbacks,
    callbacks passed to methods, anonymous signal handlers, button handlers,
    lifecycle functions.
    The name keys where its captures are kept (capture_ref); a capture is a
    local or parameter of the function around it ({name, ctype}, the macros'
    N64MacroBase.callbackCaptures)."""
    found = [(cb["callback_name"], cb.get("captures", []))
             for cb in find_tween_callbacks(ir) + nodes_of_type(ir, "callback_wrapper")]
    for node in nodes_of_type(ir, "signal_connect"):
        handler = node["props"]["handler"]
        if handler.get("kind") == "anonymous":
            found.append((handler["id"], handler.get("captures", [])))
    for node in nodes_of_type(ir, "button_register_callback"):
        found.append((node["props"]["callback_name"], node["props"].get("captures", [])))
    for node in nodes_of_type(ir, "lifecycle_register"):
        found.append((f"lc_{node['value']}", (node.get("props") or {}).get("captures", [])))
    return found


def param_captures(ir) -> List[Tuple[str, str, str]]:
    """(callback, captured name, C type) of each capture of the callbacks in an
    IR tree, once each."""
    found = {}
    for callback, captures in callback_captures(ir):
        for capture in captures:
            found.setdefault((callback, capture["name"]), capture["ctype"])
    return [(callback, param, ctype) for (callback, param), ctype in found.items()]


def capture_globals(captures: List[Tuple[str, str, str]], c_name: str) -> List[str]:
    """An autoload's capture globals (traits keep them in their data)."""
    return [f"static {ctype} {capture_ref(c_name, False, callback, param)};" for callback, param, ctype in captures]


def find_tween_callbacks(nodes: List[dict]) -> List[dict]:
    """The callbacks (props on_update, on_done) of the tween nodes in an IR tree
    that need a generated function: anonymous ones, not method references."""
    callbacks = []
    for node in nodes_of_type(nodes, "tween_float", "tween_vec4", "tween_delay"):
        props = node.get("props") or {}
        for key in ("on_update", "on_done"):
            callback = props.get(key)
            if isinstance(callback, dict) and callback.get("callback_name") and not callback.get("is_method_ref"):
                callbacks.append(callback)
    return callbacks


def generate_tween_callback(
    callback_info: dict,
    emitter,
    c_name: str = "",
    is_trait: bool = True
) -> str:
    """Generate a static C callback function for a tween.

    For traits, callbacks can access the trait data via the 'data' pointer.
    For autoloads, callbacks use global state with optional captured params.

    Args:
        callback_info: Dict with callback_name, callback_type, body, param_name, captures
        emitter: The code emitter to use for generating C code from IR
        c_name: The C name prefix (for autoload capture globals)
        is_trait: Whether this is a trait callback (affects data pointer handling)

    Returns:
        C code string for the callback function
    """
    if not callback_info:
        return ""

    # Method references are existing trait/autoload functions passed directly.
    if callback_info.get("is_method_ref"):
        return ""

    cb_name = callback_info.get("callback_name", "")
    cb_type = callback_info.get("callback_type", "")
    body_nodes = callback_info.get("body", [])
    param_name = callback_info.get("param_name") or "v"  # Handle null from JSON
    captures = callback_info.get("captures", [])

    if not cb_name or not body_nodes:
        return ""

    lines = []

    # The captured parameters, read where the call stored them
    param_captures = {c["name"]: capture_ref(c_name, is_trait, cb_name, c["name"]) for c in captures}

    # Generate function signature based on callback type
    if cb_type == "float":
        lines.append(f"static void {cb_name}_float(float {param_name}, void* obj, void* data) {{")
        if is_trait:
            lines.append("    (void)obj;")
        else:
            lines.append("    (void)obj; (void)data;")
    elif cb_type == "vec4":
        lines.append(f"static void {cb_name}_vec4(ArmVec4* {param_name}, void* obj, void* data) {{")
        if is_trait:
            lines.append("    (void)obj;")
        else:
            lines.append("    (void)obj; (void)data;")
    elif cb_type == "done":
        lines.append(f"static void {cb_name}_done(void* obj, void* data) {{")
        if is_trait:
            lines.append("    (void)obj;")
        else:
            lines.append("    (void)obj; (void)data;")
    else:
        return ""

    # For both autoloads and traits with param captures, create a wrapper emitter
    if param_captures:
        emitter = _CaptureEmitter(emitter, param_captures)

    body = emitter.emit_statements(body_nodes, "    ")
    if body:
        lines.append(body)
    lines.append("}")
    return "\n".join(lines)


class _CaptureEmitter:
    """An emitter reading a callback's captured parameters where they're kept
    (capture_ref) instead of as the function's locals."""

    def __init__(self, base_emitter, param_captures: dict):
        self.base = base_emitter
        self.param_captures = param_captures

    def emit(self, node):
        if node is None:
            return ""

        # First, recursively substitute captures in any nested nodes
        substituted_node = self._substitute_captures_in_node(node)

        # Then emit using the base emitter
        return self.base.emit(substituted_node)

    def emit_statements(self, nodes, indent: str = "    ") -> str:
        return self.base.emit_statements([self._substitute_captures_in_node(n) for n in nodes], indent)

    def _substitute_captures_in_node(self, node):
        """Recursively substitute captured params throughout the node tree."""
        if node is None:
            return None
        if not isinstance(node, dict):
            return node

        node_type = node.get("type", "")

        # For ident nodes, substitute if it's a captured param
        if node_type == "ident":
            name = node.get("value", "")
            if name in self.param_captures:
                return {"type": "ident", "value": self.param_captures[name]}
            return node

        # For callback_param_call nodes, substitute the callback name if captured
        if node_type == "callback_param_call":
            name = node.get("name", "")
            if name in self.param_captures:
                result = dict(node)
                result["name"] = self.param_captures[name]
                return result
            return node

        # For all other nodes, recursively substitute in children, args, object, etc.
        result = dict(node)  # Shallow copy

        # Handle children array
        if "children" in result and result["children"]:
            result["children"] = [self._substitute_captures_in_node(c) for c in result["children"]]

        # Handle args array
        if "args" in result and result["args"]:
            result["args"] = [self._substitute_captures_in_node(a) for a in result["args"]]

        # Handle object
        if "object" in result and result["object"]:
            result["object"] = self._substitute_captures_in_node(result["object"])

        # Handle props dict - recursively process ALL props, not just then/else_
        if "props" in result and result["props"]:
            props = dict(result["props"])
            for key, value in props.items():
                props[key] = self._substitute_captures_in_value(value)
            result["props"] = props

        return result

    def _substitute_captures_in_value(self, value):
        """Recursively substitute captures in any value (list, dict, or node)."""
        if value is None:
            return None
        if isinstance(value, list):
            return [self._substitute_captures_in_value(v) for v in value]
        if isinstance(value, dict):
            # Could be a node or just a dict of values
            if "type" in value:
                # It's a node
                return self._substitute_captures_in_node(value)
            else:
                # It's a plain dict - recurse into values
                return {k: self._substitute_captures_in_value(v) for k, v in value.items()}
        # Primitive value (string, int, etc.) - return as-is
        return value
