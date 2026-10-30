"""
Trait emitter: the C of each IR node the macros write, for traits (and
autoloads, through AutoloadEmitter). Used by trait_generator, autoload_generator,
tween_helper and signal_codegen.

A trait's code reaches its data through `data` (members of an ancestor through
the ancestor's struct, embedded first) and its object through `obj`; virtual
methods are called through their pointer (trait_generator's inheritance rules).
A node that gives no C stops the export, unless the macros left something out
there themselves (a skip node, which they reported).
"""

import re
from typing import Dict, List, Optional

from armor64.codegen.c_text import c_string
from armor64.codegen.ir import walk_ir
from armor64.codegen.tween_helper import capture_ref

# Nodes that can rightly give no C: the macros' skip (they reported it) and an
# empty block
_MAY_BE_EMPTY = ("skip", "block")

# A binding template's placeholder: {0}, {self}, {1=1.0f} (Bindings.hx)
_PLACEHOLDER = re.compile(r'\{([A-Za-z0-9_]+)(?:=([^{}]*))?\}')
# C that's cheap and side-effect free to repeat: a name or member path (maybe
# through a cast, like ((PlayerData*)data)->speed) or a literal
_PLAIN_C = re.compile(r'''^(?:
    (?:\(\([A-Za-z_]\w*\s*\*\)\s*[A-Za-z_]\w*\)|[A-Za-z_]\w*)(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)*
  | -?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?[fFuUlL]*
  | "(?:[^"\\]|\\.)*"
)$''', re.X)


class TraitEmitter:
    """The C of IR nodes (emit), and of statement lists (emit_statements), for
    one trait: emit_<node type> for each node type the macros write."""

    def __init__(self, trait_name: str, c_name: str, member_names: List[str],
                 is_trait: bool = True, parent_name: str = None, all_traits: Dict = None,
                 methods: Dict = None, vtable_owners: Dict[str, str] = None,
                 member_map: Dict = None):
        self.trait_name = trait_name
        self.c_name = c_name
        self.member_names = member_names  # This trait's own members only
        self.data_type = f"{c_name}Data"
        self.is_trait = is_trait  # True for traits (use obj/data), False for autoloads (use NULL)

        # Inheritance support
        self.parent_name = parent_name
        self.all_traits = all_traits or {}

        # Virtual method support
        self.methods = methods or {}
        # Signal rendering (signal_codegen.SignalContext), set by the generator
        self.signals = None
        # Temporaries of binding templates (emit_c_template): _t0, _t1...
        self._temp_count = 0

        # Virtual methods callable from this trait (own and inherited) -> trait
        # whose struct holds their vtable pointer
        self.vtable_owners = vtable_owners or {}

        # Member map with ctype info for pointer detection
        self.member_map = member_map or {}


    # =========================================================================
    # Inheritance Chain Utilities
    # =========================================================================

    def _get_member_access(self, member_name: str) -> str:
        """C expression of one of this trait's members."""
        return f"(({self.data_type}*)data)->{member_name}"

    def _get_member_info(self, member_name: str, key: str) -> str:
        """Get a field from member_map for a given member.

        Args:
            member_name: The member variable name.
            key: The member_map dict key to retrieve ('ctype' or 'type').
        """
        member_info = self.member_map.get(member_name, {})
        return member_info.get(key, "")

    def _get_member_ctype(self, member_name: str) -> str:
        """Get the C type of a member from member_map."""
        return self._get_member_info(member_name, "ctype")

    def emit(self, node: Optional[Dict]) -> str:
        """The C of an IR node. A node that gives none stops the export (the
        macros and the exporter disagree), unless the macros left something out
        there themselves (a skip node, which they reported)."""
        if node is None:
            return ""
        node_type = node.get("type", "")
        method = getattr(self, f"emit_{node_type}", None)
        if method is None:
            raise ValueError(f'{self.trait_name}: the exporter doesn\'t know the IR node "{node_type}" '
                             f'(the macros and the exporter are from different versions)')
        code = method(node)
        if not code and node_type not in _MAY_BE_EMPTY and not any(
                n.get("type") == "skip" for n in walk_ir(node)):
            raise ValueError(f'{self.trait_name}: the IR node "{node_type}" gave no C '
                             f'(the macros and the exporter disagree)')
        return code

    def emit_list(self, nodes: List[Dict]) -> List[str]:
        """Emit list of nodes, filtering out empty results."""
        return [c for c in (self.emit(n) for n in nodes) if c]

    def _emit_statement_lines(self, nodes: List[Dict]) -> List[str]:
        """Emit nodes as statements, adding semicolons where needed.

        Statements the Haxe macros couldn't translate (skip nodes with a warning)
        are kept as comments so the generated C shows what was left out.
        """
        result = []
        for node in nodes:
            line = self.emit(node)
            if not line:
                if node and node.get("type") == "skip" and node.get("warn"):
                    result.append(f"/* N64 unsupported: {node['warn']} */")
                continue
            # Add the semicolon: an assignment's value may end with } (a
            # compound literal), other statements ending with } or { are blocks
            ends = line.rstrip()
            if not ends.endswith(';') and (node.get("type") == "assign" or not ends.endswith(('}', '{'))):
                line = line + ';'
            result.append(line)
        return result

    def emit_statements(self, nodes: List[Dict], indent: str = "    ") -> str:
        """Emit list of nodes as statements, adding semicolons where needed.

        Handles multi-line statements by indenting each line properly.
        """
        result = []
        for line in self._emit_statement_lines(nodes):
            # Handle multi-line statements by indenting each line
            for subline in line.split('\n'):
                result.append(f"{indent}{subline}")
        return "\n".join(result)

    def _substitute_placeholders(self, c_code: str, args: List[Dict] = None,
                                  obj: str = None, vec: str = None,
                                  vec_raw: str = None, **extra) -> str:
        """Substitute placeholders in c_code template.

        Handles standard placeholders:
            {0}, {1}, ... - argument substitution (emitted)
            {obj} - object reference
            {v} - vector with parens
            {vraw} - vector without parens

        Extra keyword args are substituted as {key} -> value.
        """
        if args:
            arg_strs = [self.emit(a) for a in args]
            for i, arg in enumerate(arg_strs):
                c_code = c_code.replace("{" + str(i) + "}", arg)

        if obj is not None:
            c_code = c_code.replace("{obj}", obj)

        if vec is not None:
            c_code = c_code.replace("{v}", vec)

        if vec_raw is not None:
            c_code = c_code.replace("{vraw}", vec_raw)

        for key, value in extra.items():
            c_code = c_code.replace("{" + key + "}", value)

        return c_code

    # =========================================================================
    # Literals
    # =========================================================================

    def emit_int(self, node: Dict) -> str:
        return str(node.get("value", 0))

    def emit_float(self, node: Dict) -> str:
        # A C float literal: 2 -> 2.0f, 0.00001 -> 1e-05f (an exponent needs no dot)
        text = repr(float(node.get("value", 0.0)))
        if text in ("inf", "-inf", "nan"):
            return {"inf": "INFINITY", "-inf": "(-INFINITY)", "nan": "NAN"}[text]
        if "." not in text and "e" not in text:
            text += ".0"
        return f"{text}f"

    def emit_string(self, node: Dict) -> str:
        # "" stays "" (Haxe's null is a null node)
        return c_string(node.get("value", ""))

    def emit_str_format(self, node: Dict) -> str:
        """A String built from parts (StringFormatter): each appended to a
        rotating buffer by its kind (text, int, float, bool, str; utils.h)."""
        parts = []
        for kind, arg in zip(node["props"]["kinds"], node["args"]):
            if kind == "text":
                text = arg.get("value", "")
                parts.append(f'_n = str_add(_s, _n, {c_string(text)}, {len(text.encode("utf-8"))});')
            else:
                parts.append(f'_n = str_add_{kind}(_s, _n, {self.emit(arg)});')
        return "({ char* _s = _str_get_buffer(); int _n = 0; " + " ".join(parts) + " _s[_n] = '\\0'; _s; })"

    def emit_bool(self, node: Dict) -> str:
        return "true" if node.get("value", False) else "false"

    def emit_null(self, node: Dict) -> str:
        return "NULL"

    def emit_empty_array(self, node: Dict) -> str:
        """Empty array literal: [] -> {0}"""
        return "{0}"

    def emit_skip(self, node: Dict) -> str:
        """Skip node - represents code that should not be emitted for N64.

        Always empty so a skip inside an expression can't break the C syntax.
        A skip used as a statement with a 'warn' field is emitted as a comment
        by _emit_statement_lines().
        """
        return ""

    # =========================================================================
    # Lifecycle registrations (notifyOnX / removeX, see trait_generator.py)
    # =========================================================================

    def emit_lifecycle_register(self, node: Dict) -> str:
        """notifyOnX(f): one more registration of its site, after storing what
        an anonymous function captures."""
        site = node["value"]
        call = f"(({self.data_type}*)data)->_lc_{site}++"
        captures = (node.get("props") or {}).get("captures", [])
        return self._with_captures([{"callback_name": f"lc_{site}", "captures": captures}], call)

    def emit_lifecycle_remove(self, node: Dict) -> str:
        """removeX(f): one registration less, of the first site in the trait's
        chain (ancestors first) registering method f that is registered."""
        from armor64.codegen.trait_generator import chain_sites
        kind, method = node["value"], node["props"]["method"]
        refs = [f"(({c_name}Data*)data)->_lc_{site['id']}"
                for c_name, site in chain_sites(self.all_traits, self.trait_name)
                if site["kind"] == kind and site.get("method") == method]
        if not refs:
            return ""
        return "{ " + " else ".join(f"if ({r}) {r}--;" for r in refs) + " }"

    def emit_scene_get_group_count(self, node: Dict) -> str:
        """Scene.active.getGroup(groupName).length -> scene_get_group_count(groupName)."""
        args = node.get("args", [])
        if args:
            group_arg = self.emit(args[0])
            return f"scene_get_group_count({group_arg})"
        return "0"

    def emit_render2d_set_color(self, node: Dict) -> str:
        """g2.color = value -> assign color (already in RGBA32 format)."""
        args = node.get("args", [])
        if not args:
            return "_g2_color = RGBA32(0, 0, 0, 255);"
        color_val = self.emit(args[0])
        return f"_g2_color = {color_val};"

    def emit_color_from_floats(self, node: Dict) -> str:
        """Color.fromFloats(r, g, b, a) -> RGBA32 directly."""
        args = node.get("args", [])
        if len(args) >= 4:
            r = self.emit(args[0])
            g = self.emit(args[1])
            b = self.emit(args[2])
            a = self.emit(args[3])
            return f"RGBA32((uint8_t)(({r}) * 255.0f), (uint8_t)(({g}) * 255.0f), (uint8_t)(({b}) * 255.0f), (uint8_t)(({a}) * 255.0f))"
        return "RGBA32(0, 0, 0, 255)"

    # =========================================================================
    # Variables
    # =========================================================================

    def emit_member(self, node: Dict) -> str:
        """Trait member access with inheritance routing.

        Routes to correct depth in inheritance chain:
        - This trait's members: data->member
        - Parent's members: data->_parent.member
        - Grandparent's: data->_parent._parent.member
        """
        name = node.get("value", "")
        owner = node.get("owner")
        if owner:
            # Declared by an ancestor: its struct is at offset 0 of ours
            owner_c_name = self.all_traits[owner]["c_name"]
            return f"(({owner_c_name}Data*)data)->{name}"
        return self._get_member_access(name)

    def emit_ident(self, node: Dict) -> str:
        """Identifier: local var, dt, object, etc."""
        name = node.get("value", "")
        if name == "object":
            return "((ArmObject*)obj)"
        return name

    def emit_method_ref(self, node: Dict) -> str:
        """Method reference: function pointer to a local trait method.

        IR format:
            type: "method_ref"
            method: "initTransitionDone"
            cName: "arm_gamescene_inittransitiondone"
            trait: "GameScene"

        Emits just the C function name, which can be used as a function pointer.
        """
        return node.get("cName", "")

    def emit_field_access(self, node: Dict) -> str:
        """Field access: object.field, vec.x, this.field, etc."""
        obj_node = node.get("object")
        field = node.get("value", "")

        if obj_node:
            obj_type = obj_node.get("type", "")
            obj_value = obj_node.get("value", "")

            # Emit object and access field
            obj = self.emit(obj_node)
            if obj:
                # Transform field access: object->transform.loc
                if "transform." in field:
                    subfield = field.replace("transform.", "")
                    return f"{obj}->transform.{subfield}"

                # Through a pointer (Haxe decides from the object's type)
                if node.get("props", {}).get("deref"):
                    return f"{obj}->{field}"

                return f"({obj}).{field}"

        return field

    def emit_array_access(self, node: Dict) -> str:
        """C array access: array[index] (children: the array, the index).
        Canvas elements are canvas_element nodes."""
        children = node.get("children", [])
        if len(children) < 2:
            return ""

        array_node = children[0]
        index_node = children[1]

        array_expr = self.emit(array_node)
        index_expr = self.emit(index_node)

        if not index_expr:
            return ""

        if not array_expr:
            return ""

        # Standard C array access
        return f"{array_expr}[{index_expr}]"

    def emit_c_literal(self, node: Dict) -> str:
        """Literal C code from macro - pure 1:1."""
        return node.get("c_code", "")

    # =========================================================================
    # Operators
    # =========================================================================

    def emit_assign(self, node: Dict) -> str:
        """Assignment: target = value;

        Returns empty string if either target or value is empty/skip,
        effectively filtering out skip-based assignments.
        """
        children = node.get("children", [])
        if len(children) < 2:
            return ""

        # Check if target or value is a skip node - don't emit assignment
        if children[0].get("type") == "skip":
            return ""
        if children[1].get("type") == "skip":
            return ""

        target = self.emit(children[0])
        value = self.emit(children[1])
        if target and value:
            return f"{target} = {value}"
        return ""

    def emit_binop(self, node: Dict) -> str:
        """Binary operator: left op right"""
        op = node.get("value", "+")
        children = node.get("children", [])
        if len(children) >= 2:
            left = self.emit(children[0])
            right = self.emit(children[1])
            if left and right:
                # Compound assignments are statements, need semicolon
                if op in ("+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="):
                    return f"{left} {op} {right};"
                return f"({left} {op} {right})"
        return ""

    def emit_string_equals(self, node: Dict) -> str:
        """String == / != (typed in the macro): compares the text."""
        children = node.get("children", [])
        if len(children) < 2:
            return ""
        equals = f"arm_string_equals({self.emit(children[0])}, {self.emit(children[1])})"
        return equals if node.get("value") == "==" else f"!{equals}"

    def emit_unop(self, node: Dict) -> str:
        """Unary operator: ++x or x++"""
        op = node.get("value", "!")
        children = node.get("children", [])
        props = node.get("props", {})
        postfix = props.get("postfix", False)

        if children:
            operand = self.emit(children[0])
            if operand:
                if postfix:
                    return f"({operand}{op})"
                return f"({op}{operand})"
        return ""

    # =========================================================================
    # Control Flow
    # =========================================================================

    def emit_if(self, node: Dict) -> str:
        """If statement.

        Unified IR format from both trait and autoload macros:
          children[0] = condition
          props.then = array of then body statement nodes
          props.else_ = array of else body statement nodes (optional, can be null)
        """
        children = node.get("children", [])
        props = node.get("props", {})

        if len(children) < 1:
            return ""

        cond = self.emit(children[0])
        if not cond:
            return ""

        # Bodies with one indent level (the caller adds the outer one); an empty
        # then is kept, for its else and its condition's side effects
        then_code = self.emit_statements(props.get("then") or [], "    ")
        result = f"if ({cond}) {{\n{then_code}\n}}" if then_code else f"if ({cond}) {{ }}"
        else_code = self.emit_statements(props.get("else_") or [], "    ")
        if else_code:
            result += f" else {{\n{else_code}\n}}"

        return result

    def emit_ternary(self, node: Dict) -> str:
        """cond ? a : b (children: the condition, the two values)."""
        cond, a, b = (self.emit(c) for c in node.get("children", []))
        return f"({cond} ? {a} : {b})" if cond and a and b else ""

    def emit_block(self, node: Dict) -> str:
        """Block of statements - emit as statements with semicolons."""
        children = node.get("children", [])
        # Use emit_statements logic but without extra indent (caller handles indent)
        return "\n".join(self._emit_statement_lines(children))

    def emit_var(self, node: Dict) -> str:
        """Local variable declaration: ctype name = value;"""
        name = node.get("value", "")
        props = node.get("props", {})
        children = node.get("children") or []
        ctype = props.get("ctype", "float")
        init = children[0] if children else None

        val = self.emit(init) if init else ""
        # Arrays start empty
        if ctype.endswith("Array") and val in ("", "[]", "NULL", "{0}"):
            return f"{ctype} {name} = {{0}};"
        if val:
            return f"{ctype} {name} = {val};"
        return f"{ctype} {name};"

    def emit_return(self, node: Dict) -> str:
        """Return statement: return value; or just return;"""
        children = node.get("children", [])
        if children and len(children) > 0:
            val = self.emit(children[0])
            if val:
                return f"return {val};"
        return "return;"

    def _emit_body(self, node: Optional[Dict]) -> str:
        """A loop body: a block, or a single statement (with its semicolon)."""
        if not node:
            return ""
        if node.get("type") == "block":
            return self.emit(node)
        return "\n".join(self._emit_statement_lines([node]))

    def emit_for_range(self, node: Dict) -> str:
        """for (i in start...end): the end evaluated once, as Haxe does
        (for (int i = start, i_end = end; i < i_end; i++)), inline when it's a
        literal.

        IR format:
            value: loop variable name
            children[0]: start expression
            children[1]: end expression
            children[2]: body expression
        """
        var_name = node.get("value", "i")
        children = node.get("children", [])

        if len(children) < 3:
            return ""

        start = self.emit(children[0])
        end = self.emit(children[1])
        body = self._emit_body(children[2])

        if not start or not end:
            return ""

        # Emit body with proper indentation
        body_code = body if body else ""
        # If body doesn't already have braces, wrap in block
        if body_code and not body_code.strip().startswith('{'):
            # Each line needs indentation
            body_lines = body_code.split('\n')
            indented_body = '\n'.join(f"    {line}" for line in body_lines)
            body_block = f"{{\n{indented_body}\n}}"
        else:
            body_block = body_code if body_code else "{ }"

        if re.fullmatch(r'-?\d+', end):
            return f"for (int {var_name} = {start}; {var_name} < {end}; {var_name}++) {body_block}"
        return (f"for (int {var_name} = {start}, {var_name}_end = {end}; {var_name} < {var_name}_end; "
                f"{var_name}++) {body_block}")

    def emit_while(self, node: Dict) -> str:
        """While loop: while (cond) { body }

        IR format:
            children[0]: condition expression
            children[1]: body expression
        """
        children = node.get("children", [])

        if len(children) < 2:
            return ""

        cond = self.emit(children[0])
        body = self._emit_body(children[1])

        if not cond:
            return ""

        body_code = body if body else ""
        if body_code and not body_code.strip().startswith('{'):
            body_lines = body_code.split('\n')
            indented_body = '\n'.join(f"    {line}" for line in body_lines)
            body_block = f"{{\n{indented_body}\n}}"
        else:
            body_block = body_code if body_code else "{ }"

        return f"while ({cond}) {body_block}"

    def emit_do_while(self, node: Dict) -> str:
        """Do-while loop: do { body } while (cond);

        IR format:
            children[0]: condition expression
            children[1]: body expression
        """
        children = node.get("children", [])

        if len(children) < 2:
            return ""

        cond = self.emit(children[0])
        body = self._emit_body(children[1])

        if not cond:
            return ""

        body_code = body if body else ""
        if body_code and not body_code.strip().startswith('{'):
            body_lines = body_code.split('\n')
            indented_body = '\n'.join(f"    {line}" for line in body_lines)
            body_block = f"{{\n{indented_body}\n}}"
        else:
            body_block = body_code if body_code else "{ }"

        return f"do {body_block} while ({cond})"

    def emit_break(self, node: Dict) -> str:
        """Break statement."""
        return "break"

    def emit_continue(self, node: Dict) -> str:
        """Continue statement."""
        return "continue"

    # =========================================================================
    # Map Operations (arm_map.h)
    # =========================================================================

    def _container_call(self, node: Dict, func: str, nargs: int) -> str:
        """<Type>_<func>(&container, args...) of arm_map.h / arm_array.h.

        IR format:
            value: the container's C type (e.g. "ArmIntArray", "ArmSoundChannelMap")
            object: the map or array (member, local, autoload field...)
            children: the arguments (key, value, index...)
            props.pointer: object is already a pointer (map[key] in map[key][i])
        """
        ctype = node.get("value", "")
        container = self.emit(node.get("object"))
        args = [self.emit(c) for c in (node.get("children") or [])[:nargs]]
        if not ctype or not container or len(args) < nargs or not all(args):
            return ""
        ref = container if (node.get("props") or {}).get("pointer") else f"&{container}"
        return f"{ctype}_{func}({', '.join([ref] + args)})"

    def emit_map_set(self, node: Dict) -> str:
        return self._container_call(node, "set", 2)

    def emit_array_set(self, node: Dict) -> str:
        return self._container_call(node, "set", 2)

    def emit_container_element(self, node: Dict) -> str:
        """Pointer to c[k], written in place (c[k] += v, c[k]++): a map's get
        gives one, an array's get_ptr."""
        func = "get" if (node.get("props") or {}).get("map") else "get_ptr"
        return self._container_call(node, func, 1)

    def emit_map_get(self, node: Dict) -> str:
        return self._container_call(node, "get", 1)

    def emit_map_value(self, node: Dict) -> str:
        return self._container_call(node, "value", 1)

    def emit_map_exists(self, node: Dict) -> str:
        return self._container_call(node, "exists", 1)

    def emit_map_remove(self, node: Dict) -> str:
        return self._container_call(node, "remove", 1)

    def emit_map_clear(self, node: Dict) -> str:
        return self._container_call(node, "clear", 0)

    def emit_array_push(self, node: Dict) -> str:
        return self._container_call(node, "push", 1)

    def emit_array_pop(self, node: Dict) -> str:
        return self._container_call(node, "pop", 0)

    def emit_array_get(self, node: Dict) -> str:
        return self._container_call(node, "get", 1)

    def emit_array_clear(self, node: Dict) -> str:
        return self._container_call(node, "clear", 0)

    # Gets on a nested expression (map[key][i]): the inner get already returns a pointer

    def emit_array_get_nested(self, node: Dict) -> str:
        """Array get on nested expression (e.g., map[key][i])

        IR format:
            value: array type name
            children[0]: array expression (e.g., map_get result)
            children[1]: index expression

        Since map_get returns a pointer, we pass it directly (no &)
        Returns a copy of the element.
        """
        array_type = node.get("value", "")
        children = node.get("children", [])

        if len(children) < 2:
            return ""

        array_ptr = self.emit(children[0])  # This is already a pointer from map_get
        index = self.emit(children[1])

        if not array_type or not array_ptr:
            return ""

        func_name = f"{array_type}_get"
        # map_get returns pointer, so no & needed
        return f"{func_name}({array_ptr}, {index})"

    def emit_array_get_nested_ptr(self, node: Dict) -> str:
        """Array get_ptr on nested expression - returns pointer instead of copy.

        Used for sound handles where we need to modify the handle in-place.
        """
        array_type = node.get("value", "")
        children = node.get("children", [])

        if len(children) < 2:
            return "NULL"

        array_ptr = self.emit(children[0])  # This is already a pointer from map_get
        index = self.emit(children[1])

        if not array_type or not array_ptr:
            return "NULL"

        func_name = f"{array_type}_get_ptr"
        return f"{func_name}({array_ptr}, {index})"

    # =========================================================================
    # Function Calls
    # =========================================================================

    def emit_call(self, node: Dict) -> str:
        """Function call - handles generic calls from macro.

        Trait macro uses 'method' field, autoload macro uses 'value' field.
        Support both for compatibility.
        """
        func_name = node.get("method", "") or node.get("value", "")
        if not func_name:
            return ""
        args = self._args(node.get("args", []) or node.get("children", []))
        return f"{func_name}({', '.join(args)})" if args is not None else ""

    def _args(self, nodes: List[Dict]) -> Optional[List[str]]:
        """A call's arguments, each translated once and kept in its place; None
        when one was left out by the macros (skip: the call can't be made)."""
        args = [self.emit(a) for a in nodes]
        return None if any(not a for a in args) else args

    def emit_callback_param_call(self, node: Dict) -> str:
        """Emit call to a function-type parameter (callback invocation).

        IR format:
            type: "callback_param_call"
            name: "finishedCallback"
            paramType: "Void->Void"
            args: [...]

        In C, the callback is passed as a function pointer parameter.
        We guard with NULL check since callbacks are often optional:
            if (finishedCallback) { finishedCallback(obj, data); }

        All callbacks in our N64 system take (void* obj, void* data) as the
        standard parameters for accessing trait members via closure-like mechanism.
        Additional arguments (like Float value for Float->Void) are prepended.
        """
        name = node.get("name", "")
        args = node.get("args", [])

        if not name:
            return ""

        # The callback's own arguments first (Float->Void: the float), then obj, data
        arg_strs = self._args(args)
        if arg_strs is None:
            return ""
        return f"if ({name}) {{ {name}({', '.join(arg_strs + ['obj', 'data'])}); }}"

    def emit_super_call(self, node: Dict) -> str:
        """Emit super() or super.method() calls for inheritance.

        IR format:
            type: "super_call"
            value: "ParentTraitName"
            method: "new" (for super()) or method name (for super.method())
            args: [...]

        Maps to parent lifecycle functions.

        We pass the FULL data pointer (not &_parent) so that virtual method calls
        within the parent method dispatch correctly to the child's override.
        Since _parent is at offset 0, casting LevelData* to GameSceneData* works.
        """
        parent_name = node.get("value", "")
        method = node.get("method", "")
        args = node.get("args", [])

        if not parent_name or parent_name not in self.all_traits:
            return "// super call - no parent found"

        parent_ir = self.all_traits[parent_name]
        parent_c_name = parent_ir["c_name"]

        # super(): the parent's constructor
        if method == "new":
            return f"{parent_c_name}_on_create(obj, data)"
        # super.method(): the C function of the ancestor declaring it (Haxe gives its name)
        arg_strs = self._args(args)
        if arg_strs is None:
            return ""
        return f"{node['cName']}({', '.join(['obj', 'data'] + arg_strs)})"

    def emit_trait_method_call(self, node: Dict) -> str:
        """Emit call to this trait's own method (generated as C function).

        IR format:
            type: "trait_method_call"
            cName: "arm_gamescene_init"
            method: "init"
            trait: "GameScene"
            args: [...]

        For virtual methods (public, can be overridden), call through vtable:
            (({TraitName}Data*)data)->_vfn_methodname(obj, data, args...)

        For non-virtual methods, call directly:
            cName(obj, data, args...)
        """
        c_name = node.get("cName", "")
        method_name = node.get("method", "")
        args = node.get("args", [])

        if not c_name:
            return "// trait_method_call - missing cName"

        # Arguments: an omitted optional one (skip) is NULL, a callback its
        # generated function (its captured parameters stored before the call)
        callbacks = []
        arg_strs = []
        for a in args:
            if a and a.get("type") == "skip":
                arg_strs.append("NULL")
            elif a and a.get("type") == "callback_wrapper":
                arg_strs.append(a.get("callback_name", ""))
                callbacks.append(a)
            else:
                emitted = self.emit(a)
                if emitted:
                    arg_strs.append(emitted)
        args_part = ", ".join(arg_strs) if arg_strs else ""

        # Build the method call
        vtable_call = self._vtable_call(method_name)
        if vtable_call:
            # Call through vtable for polymorphic dispatch
            if args_part:
                method_call = f"{vtable_call}(obj, data, {args_part})"
            else:
                method_call = f"{vtable_call}(obj, data)"
        else:
            # Non-virtual: direct call
            if args_part:
                method_call = f"{c_name}(obj, data, {args_part})"
            else:
                method_call = f"{c_name}(obj, data)"

        return self._with_captures(callbacks, method_call)

    def _with_captures(self, callbacks: List[Optional[Dict]], call: str) -> str:
        """A call after storing the parameters its callbacks capture (capture_ref):
        a comma expression, so it works as a statement, a value or in a chain."""
        stores = [f"{capture_ref(self.c_name, self.is_trait, cb['callback_name'], cap['name'])} = {cap['name']}"
                  for cb in callbacks if cb for cap in cb.get("captures", [])]
        return f"({', '.join(stores + [call])})" if stores else call

    def _vtable_call(self, method: str) -> str:
        """`((OwnerData*)data)->_vfn_method` for virtual methods, through the struct
        of the trait holding the pointer; empty for non-virtual methods."""
        owner = self.vtable_owners.get(method)
        if not owner:
            return ""
        owner_c_name = self.all_traits[owner]["c_name"]
        return f"(({owner_c_name}Data*)data)->_vfn_{method}"

    def emit_scene_call(self, node: Dict) -> str:
        """Scene.setActive() -> scene_switch_to()"""
        if "c_code" in node:
            return node["c_code"] + ";"
        args = node.get("args", [])
        if args:
            # Runtime lookup by name string
            scene_expr = self.emit(args[0])
            return f"scene_switch_to(scene_get_id_by_name({scene_expr}));"
        return ""

    def emit_canvas_get_label(self, node: Dict) -> str:
        """canvas.getElementAs(Label, "key") -> canvas_get_label(UI_LABEL_{KEY})

        Uses the label key to build the define name. Canvas-independent since
        each scene loads its own canvas into the same label pool with matching indices.
        """
        import arm.utils
        props = node.get("props", {})
        label_key = props.get("key", "")
        if not label_key:
            raise ValueError(f'{self.trait_name}: a canvas label without a key')

        safe_key = arm.utils.safesrc(label_key).upper()
        return f"canvas_get_label(UI_LABEL_{safe_key})"

    def emit_canvas_get_group(self, node: Dict) -> str:
        """canvas.getElementAs(AnchorPane/RowLayout/ColLayout, "key") -> canvas_get_group(UI_GROUP_{KEY})

        Used for layout containers that can have visibility toggled.
        """
        import arm.utils
        props = node.get("props", {})
        group_key = props.get("key", "")
        if not group_key:
            raise ValueError(f'{self.trait_name}: a canvas group without a key')

        safe_key = arm.utils.safesrc(group_key).upper()
        return f"canvas_get_group(UI_GROUP_{safe_key})"

    def emit_canvas_get_button(self, node: Dict) -> str:
        """canvas.getElementFromSceneAs(Button, ..., "key") -> UI_BUTTON_{KEY}

        Returns a uint8_t button index matching the UI_BUTTON_* defines
        generated by the UI exporter.
        """
        import arm.utils
        props = node.get("props", {})
        button_key = props.get("key", "")
        if not button_key:
            raise ValueError(f'{self.trait_name}: a canvas button without a key')

        safe_key = arm.utils.safesrc(button_key).upper()
        return f"UI_BUTTON_{safe_key}"

    def emit_button_register_callback(self, node: Dict) -> str:
        """Button callback registration -> button_on_*(index, callback, data)

        IR format:
            type: "button_register_callback"
            props: { event: "on_pressed", callback_name: "..." }
            object: { type: "member", value: "playButton" }
            body: [ ... callback body nodes ... ]

        The static callback function is generated separately by trait_generator.
        Here we emit just the registration call.
        """
        props = node.get("props", {})
        event = props.get("event", "")
        cb_name = props.get("callback_name", "")
        obj_node = node.get("object")

        if not event or not cb_name or not obj_node:
            return ""

        button_expr = self.emit(obj_node)
        if not button_expr:
            return ""

        # Map event type to C registration function
        reg_fn = {
            "on_pressed": "button_on_pressed",
            "on_released": "button_on_released",
            "on_focus": "button_on_focus",
        }.get(event, "")

        if not reg_fn:
            return f"/* WARNING: Unknown button event type '{event}' */"

        call = f"{reg_fn}({button_expr}, {cb_name}, data)"
        return self._with_captures([{"callback_name": cb_name, "captures": props.get("captures", [])}], call) + ";"

    def emit_button_grab_focus(self, node: Dict) -> str:
        """button.grabFocus() -> button_set_focus(UI_BUTTON_XXX)"""
        obj_node = node.get("object")
        if not obj_node:
            return ""
        button_expr = self.emit(obj_node)
        if not button_expr:
            return ""
        return f"button_set_focus({button_expr});"

    def emit_canvas_set_scene(self, node: Dict) -> str:
        """canvas.setScene("SceneName") -> canvas_set_ui_scene(UI_SCENE_XXX)

        Maps the Koui scene name to the generated UI_SCENE_* define.
        """
        import arm.utils
        props = node.get("props", {})
        scene_name = props.get("scene", "")
        safe_name = arm.utils.safesrc(scene_name).upper()
        return f'canvas_set_ui_scene(UI_SCENE_{safe_name})'

    def emit_canvas_clear_scenes(self, node: Dict) -> str:
        """SceneManager.clearScenes() -> canvas_clear()"""
        return "canvas_clear();"

    def _ui_call(self, node: Dict, func: str) -> str:
        """func(args...) for the UI nodes Haxe builds from types."""
        args = [self.emit(a) for a in node.get("args", [])]
        return f"{func}({', '.join(args)})" if args and all(args) else ""

    def emit_label_set_text(self, node: Dict) -> str:
        return self._ui_call(node, "ui_label_set_text")

    def emit_canvas_element_set_visible(self, node: Dict) -> str:
        return self._ui_call(node, "canvas_element_set_visible")

    def emit_ui_group_set_visible(self, node: Dict) -> str:
        return self._ui_call(node, "ui_group_set_visible")

    def emit_canvas_element(self, node: Dict) -> str:
        """elements[i]: Koui's element list is the canvas's (logo screens use images)."""
        return f"canvas_get_image({self.emit(node.get('children', [None])[0])})"

    def emit_button_field(self, node: Dict) -> str:
        """button.field: buttons are indices into the canvas's button pool."""
        return f"canvas_get_button({self.emit(node.get('children', [None])[0])})->{node.get('value', '')}"

    def emit_c_template(self, node: Dict) -> str:
        """A call bound to C in Bindings.hx: its template, with the arguments
        ({0}...), the receiver ({self}) and defaults ({1=1.0f}) filled in.

        An argument or receiver used more than once that isn't plain C (a
        name, member path or literal) is evaluated once, into a temporary; a
        receiver used once is parenthesized. A receiver written to (props
        in_place: `v.normalize()`) is used as written.
        """
        template = node.get("c_code", "")
        values = {str(i): self.emit(a) for i, a in enumerate(node.get("args") or [])}
        obj = node.get("object")
        in_place = (node.get("props") or {}).get("in_place", False)
        if obj:
            values["self"] = self.emit(obj)

        uses: Dict[str, int] = {}
        for m in _PLACEHOLDER.finditer(template):
            uses[m.group(1)] = uses.get(m.group(1), 0) + 1
        temps = []
        for name, count in uses.items():
            value = values.get(name)
            if not value or _PLAIN_C.match(value) or (name == "self" and in_place):
                continue
            if count > 1:
                temp = f"_t{self._temp_count}"
                self._temp_count += 1
                temps.append(f"__auto_type {temp} = ({value});")
                values[name] = temp
            elif name == "self":
                values[name] = f"({value})"

        missing = []

        def fill(m):
            value = values.get(m.group(1))
            if value:
                return value
            if m.group(2) is not None:
                return m.group(2)
            missing.append(m.group(1))
            return ""

        # One pass: arguments containing braces aren't read as placeholders
        code = _PLACEHOLDER.sub(fill, template)
        if missing:
            return ""
        if not temps:
            return code
        decls = " ".join(temps)
        # A statement ends with ; or is a block; an expression may still end
        # with } (a compound literal)
        if code.rstrip().endswith(";") or code.lstrip().startswith("{"):
            return f"{{ {decls} {code} }}"
        return f"({{ {decls} {code}; }})"

    def emit_input_call(self, node: Dict) -> str:
        """Input calls - macro provides C code."""
        return node.get("c_code", "0")

    def _signal_instance(self, node: Dict) -> Optional[str]:
        """C expression of the instance owning a per-instance signal (None: own data)."""
        obj = node.get("object")
        return self.emit(obj) if obj else None

    def emit_signal_emit(self, node: Dict) -> str:
        key = node.get("props", {}).get("signal", "")
        ptr = self.signals.signal_ptr(key, self._signal_instance(node))
        return self.signals.emit_code(key, ptr, [self.emit(a) for a in node.get("args", [])])

    def emit_signal_connect(self, node: Dict) -> str:
        return self._signal_connection(node, "signal_connect")

    def emit_signal_disconnect(self, node: Dict) -> str:
        return self._signal_connection(node, "signal_disconnect")

    def _signal_connection(self, node: Dict, func: str) -> str:
        props = node.get("props", {})
        key = props.get("signal", "")
        handler = props.get("handler", {})
        ptr = self.signals.signal_ptr(key, self._signal_instance(node))
        name = self.signals.handler_name(handler, key, self.c_name)
        # Context passed to the handler: the instance owning the method, or this
        # trait's data (none for static functions and in autoloads)
        if handler.get("kind") == "static" or not self.is_trait and not handler.get("instance"):
            ctx = "NULL"
        elif handler.get("instance"):
            ctx = self.emit(handler["instance"])
        else:
            ctx = "data"
        call = f"{func}({ptr}, {name}, {ctx})"
        if func == "signal_connect" and handler.get("kind") == "anonymous":
            return self._with_captures([{"callback_name": handler["id"], "captures": handler.get("captures", [])}], call)
        return call

    def emit_autoload_call(self, node: Dict) -> str:
        """Autoload function call - ClassName.method() -> classname_method(args)."""
        props = node.get("props", {})
        c_name = props.get("c_name", "")
        method = props.get("method", "")

        if not c_name or not method:
            return ""

        args = node.get("args", [])
        arg_strs = [self.emit(a) for a in args]

        return f"{c_name}_{method}({', '.join(arg_strs)})"

    def emit_autoload_field(self, node: Dict) -> str:
        """Autoload field access - ClassName.field -> classname_field."""
        props = node.get("props", {})
        # Support both old (c_name/field) and new (autoload/member) naming
        c_name = props.get("c_name") or props.get("autoload", "")
        field = props.get("field") or props.get("member", "")

        if not c_name or not field:
            return ""

        return f"{c_name}_{field}"

    def emit_autoload_trait_object(self, node: Dict) -> str:
        """Get object pointer from autoload trait member - MainInstances.player.object -> maininstances_player->object."""
        props = node.get("props", {})
        autoload = props.get("autoload", "")
        member = props.get("member", "")

        if not autoload or not member:
            return ""

        # Returns the object pointer from the trait data struct
        # autoload_member is a TraitData*, so we dereference to get .object
        return f"{autoload}_{member}->object"

    def emit_autoload_trait_assign(self, node: Dict) -> str:
        """Assign current trait data to autoload member - MainInstances.player = this -> maininstances_player = data;"""
        props = node.get("props", {})
        autoload = props.get("autoload", "")
        member = props.get("member", "")

        if not autoload or not member:
            return ""

        # Assign the current trait's data pointer to the autoload member
        # The cast is to the appropriate trait data type (c_name + "Data")
        data_struct = f"{self.c_name}Data"
        return f"{autoload}_{member} = ({data_struct}*)data;"

    def emit_debug_call(self, node: Dict) -> str:
        """trace() -> debugf()"""
        args = node.get("args", [])
        if not args:
            return 'debugf("")'
        arg_strs = self._args(args)
        if arg_strs is None:
            return ""
        if len(arg_strs) == 1:
            return f'debugf("%s\\n", {arg_strs[0]})'
        return f'debugf("{", ".join(["%s"] * len(arg_strs))}\\n", {", ".join(arg_strs)})'

    def emit_vec_call(self, node: Dict) -> str:
        """Vec calls - substitute placeholders in macro-provided c_code."""
        c_code = node.get("c_code", "")
        if not c_code:
            return ""

        obj_node = node.get("object")
        obj = self.emit(obj_node) if obj_node else ""
        if not obj:
            return ""

        # Determine if we need parentheses around obj
        is_compound = obj.startswith("(Arm")
        vec = f"({obj})" if is_compound else obj

        return self._substitute_placeholders(c_code, node.get("args", []), vec=vec, vec_raw=obj)

    # =========================================================================
    # Constructors
    # =========================================================================

    def emit_new_vec(self, node: Dict) -> str:
        """Vec constructor with c_code template from macro."""
        c_code = node.get("c_code", "")
        if not c_code:
            return ""
        return self._substitute_placeholders(c_code, node.get("args", []))

    # =========================================================================
    # Audio Calls - emit audio nodes from AudioCallConverter
    # =========================================================================

    def emit_audio_load(self, node: Dict) -> str:
        """Audio load call - loads sound without playing it."""
        props = node.get("props", {})
        args = node.get("args", [])

        # Get sound path - either from args or c_code
        if args:
            sound_path = self.emit(args[0])
        else:
            c_code = node.get("c_code", "")
            if c_code:
                return c_code
            sound_path = '"rom:/sound.wav64"'

        mix_channel = props.get("mix_channel", "AUDIO_MIX_MASTER")
        # A literal loop is in props, an expression is the second argument
        loop = props.get("loop") or (self.emit(args[1]) if len(args) > 1 else "false")

        return f"arm_audio_load({sound_path}, {mix_channel}, {loop})"

    def emit_audio_mix_volume(self, node: Dict) -> str:
        """Mix channel volume set: args[0] is the volume."""
        channel = node.get("props", {}).get("channel", "AUDIO_MIX_MASTER")
        args = node.get("args", [])
        volume = self.emit(args[0]) if args else "1.0f"
        return f"arm_audio_set_mix_volume({channel}, {volume})"

    def emit_audio_mix_volume_get(self, node: Dict) -> str:
        """Mix channel volume get."""
        return node.get("c_code", "")

    # Sound handles: Haxe gives the handle as a pointer (children[0]) to the
    # functions updating it, and as a value to comparisons

    def _handle_call(self, node: Dict, func: str, *extra: str) -> str:
        children = node.get("children", [])
        if not children:
            return node.get("c_code", "")
        return f"{func}({', '.join([self.emit(children[0]), *extra])})"

    def emit_audio_handle_play(self, node: Dict) -> str:
        return self._handle_call(node, "arm_audio_start")

    def emit_audio_handle_stop(self, node: Dict) -> str:
        return self._handle_call(node, "arm_audio_stop")

    def emit_audio_handle_pause(self, node: Dict) -> str:
        # libdragon has no pause
        return self._handle_call(node, "arm_audio_stop")

    def emit_audio_handle_volume(self, node: Dict) -> str:
        args = node.get("args", [])
        return self._handle_call(node, "arm_audio_set_volume", self.emit(args[0]) if args else "1.0f")

    def emit_sound_handle_finished(self, node: Dict) -> str:
        # The channel's state, not the cached flag
        return f"!{self._handle_call(node, 'arm_audio_is_playing')}"

    def emit_sound_handle_equals(self, node: Dict) -> str:
        children = node.get("children", [])
        if len(children) < 2:
            return ""
        equals = f"arm_sound_handle_equals({self.emit(children[0])}, {self.emit(children[1])})"
        return equals if node.get("value") == "==" else f"!{equals}"

    def emit_address_of(self, node: Dict) -> str:
        return f"&{self.emit(node.get('children', [None])[0])}"

    def emit_deref(self, node: Dict) -> str:
        return f"(*{self.emit(node.get('children', [None])[0])})"

    # =========================================================================
    # Tween emit methods
    # =========================================================================

    def emit_tween_alloc(self, node: Dict) -> str:
        """Tween allocation from fixed pool."""
        return "tween_alloc()"

    def _tween_callback_name(self, callback_info: Optional[Dict], suffix: str) -> Optional[str]:
        """Resolve the C symbol for a tween callback prop.

        A method reference (callback generated by the macro with
        is_method_ref) is an existing trait method and is passed directly.
        Anonymous functions get their generated wrapper suffix (_done/_float/
        _vec4).
        """
        if not callback_info:
            return None
        name = callback_info.get("callback_name")
        if not name:
            return None
        if callback_info.get("is_method_ref"):
            return name
        return f"{name}{suffix}"

    def emit_tween_float(self, node: Dict) -> str:
        """Emit tween_float() call.

        The actual callback functions are generated separately at the autoload level.
        Here we just emit the setup and start calls.

        Parameters the callbacks capture are stored first (_with_captures).
        """
        obj = node.get("object")
        args = node.get("args", [])
        props = node.get("props", {})

        tween_var = self.emit(obj) if obj else "NULL"
        from_val = self.emit(args[0]) if args else "0.0f"
        to_val = self.emit(args[1]) if len(args) > 1 else "0.0f"
        duration = self.emit(args[2]) if len(args) > 2 else "1.0f"

        ease = props.get("ease", "EASE_LINEAR")
        on_update = props.get("on_update")
        on_done = props.get("on_done")

        # Get callback names (method refs are passed directly, no wrapper suffix)
        update_str = self._tween_callback_name(on_update, "_float") or "NULL"
        done_str = self._tween_callback_name(on_done, "_done") or "NULL"

        # Tween returns itself for chaining, so emit setup
        # tween_float(tween, from, to, duration, on_update, on_done, ease, obj, data)
        # For traits, pass obj/data so callbacks can access trait members
        # For autoloads, pass NULL (callbacks access globals directly)
        obj_param = "obj" if self.is_trait else "NULL"
        data_param = "data" if self.is_trait else "NULL"

        tween_call = f"tween_float({tween_var}, {from_val}, {to_val}, {duration}, {update_str}, {done_str}, {ease}, {obj_param}, {data_param})"
        return self._with_captures([on_update, on_done], tween_call)

    def emit_tween_vec4(self, node: Dict) -> str:
        """Emit tween_vec4() call."""
        obj = node.get("object")
        args = node.get("args", [])
        props = node.get("props", {})

        tween_var = self.emit(obj) if obj else "NULL"
        from_val = self.emit(args[0]) if args else "NULL"
        to_val = self.emit(args[1]) if len(args) > 1 else "NULL"
        duration = self.emit(args[2]) if len(args) > 2 else "1.0f"

        ease = props.get("ease", "EASE_LINEAR")
        on_update = props.get("on_update")
        on_done = props.get("on_done")

        # Method refs are passed directly, no wrapper suffix
        update_str = self._tween_callback_name(on_update, "_vec4") or "NULL"
        done_str = self._tween_callback_name(on_done, "_done") or "NULL"

        obj_param = "obj" if self.is_trait else "NULL"
        data_param = "data" if self.is_trait else "NULL"
        tween_call = f"tween_vec4({tween_var}, &{from_val}, &{to_val}, {duration}, {update_str}, {done_str}, {ease}, {obj_param}, {data_param})"
        return self._with_captures([on_update, on_done], tween_call)

    def emit_tween_delay(self, node: Dict) -> str:
        """Emit tween_delay() call."""
        obj = node.get("object")
        args = node.get("args", [])
        props = node.get("props", {})

        tween_var = self.emit(obj) if obj else "NULL"
        duration = self.emit(args[0]) if args else "1.0f"

        on_done = props.get("on_done")
        done_str = self._tween_callback_name(on_done, "_done") or "NULL"

        obj_param = "obj" if self.is_trait else "NULL"
        data_param = "data" if self.is_trait else "NULL"
        tween_call = f"tween_delay({tween_var}, {duration}, {done_str}, {obj_param}, {data_param})"
        return self._with_captures([on_done], tween_call)

    def emit_tween_start(self, node: Dict) -> str:
        """Emit tween_start() call."""
        obj = node.get("object")
        tween_var = self.emit(obj) if obj else "NULL"
        return f"tween_start({tween_var})"

    def emit_tween_pause(self, node: Dict) -> str:
        """Emit tween_pause() call."""
        obj = node.get("object")
        tween_var = self.emit(obj) if obj else "NULL"
        return f"tween_pause({tween_var})"

    def emit_tween_stop(self, node: Dict) -> str:
        """Emit tween_stop() call."""
        obj = node.get("object")
        tween_var = self.emit(obj) if obj else "NULL"
        return f"tween_stop({tween_var})"
