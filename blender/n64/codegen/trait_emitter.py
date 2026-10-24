"""
Trait Emitter - Core IR→C translation engine for traits.

This module provides the TraitEmitter class which performs pure 1:1 translation
from IR nodes to C code. No semantic analysis - just translation.

Inheritance Support (Option B - Composition):
- Child data structs embed parent at offset 0 as _parent field
- Member access routes through inheritance chain: data->member or data->_parent.member
- super() calls emit as parent lifecycle function calls

Virtual Method Support:
- Public methods can be overridden in child classes
- Virtual methods are called through function pointers (_vfn_methodname)
- Child classes set their own function pointers in on_ready
"""

import logging
import re
from typing import Dict, List, Optional, Set

log = logging.getLogger(__name__)


class TraitEmitter:
    """Emits C code from IR nodes. No semantic analysis, just translation.

    Inheritance Support:
    - Tracks parent_name and all_traits for member routing
    - emit_member() routes to correct depth in inheritance chain
    - emit_super_call() emits parent function calls

    Virtual Method Support:
    - Tracks methods dict to identify virtual methods
    - emit_trait_method_call() calls through vtable for virtual methods
    """

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

    def _get_member_haxe_type(self, member_name: str) -> str:
        """Get the Haxe type of a member from member_map."""
        return self._get_member_info(member_name, "type")

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        """Convert camelCase to snake_case (e.g. focusUp -> focus_up)."""
        return re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name).lower()

    def emit(self, node: Optional[Dict]) -> str:
        """Emit C code for an IR node."""
        if node is None:
            return ""

        node_type = node.get("type", "")
        method_name = f"emit_{node_type}"
        method = getattr(self, method_name, None)

        if method:
            return method(node)

        # Unknown node type - warn and skip
        if node_type and node_type != "skip":
            log.warn(f"N64 Trait Emitter: Unknown IR node type '{node_type}' - skipping")
        return ""

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
        val = node.get("value", 0.0)
        # Ensure float format (e.g., 0.0f not 0f)
        val_str = str(val)
        if "." not in val_str:
            val_str = f"{val}.0"
        return f"{val_str}f"

    def emit_string(self, node: Dict) -> str:
        val = node.get("value", "")
        return f'"{val}"' if val else "NULL"

    def emit_sprintf(self, node: Dict) -> str:
        """Pre-built sprintf from macro: { type: "sprintf", value: "format", args: [...] }
        The macro has already built the format string and collected typed args.
        We just translate to C."""
        format_str = node.get("value", "")
        args = node.get("args", [])

        if not args:
            # No args, just a literal string
            return f'"{format_str}"'

        # Emit each argument
        arg_codes = [self.emit(arg) for arg in args]
        args_str = ", ".join(arg_codes)

        return f'_str_concat("{format_str}", {args_str})'

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
    # Lifecycle Control (removeUpdate, notifyOnUpdate at runtime)
    # =========================================================================

    def emit_remove_update(self, node: Dict) -> str:
        """removeUpdate(callback) -> disable specific on_update callback via _update_<name>_enabled flag."""
        callback_name = node.get("value")
        if callback_name:
            # Multiple update functions - use specific flag
            return f"(({self.data_type}*)data)->_update_{callback_name}_enabled = false;"
        else:
            # Single/anonymous update - use generic flag
            return f"(({self.data_type}*)data)->_update_enabled = false;"

    def emit_remove_fixed_update(self, node: Dict) -> str:
        """removeFixedUpdate(callback) -> disable specific on_fixed_update callback via _fixed_update_<name>_enabled flag."""
        callback_name = node.get("value")
        if callback_name:
            # Multiple fixed update functions - use specific flag
            return f"(({self.data_type}*)data)->_fixed_update_{callback_name}_enabled = false;"
        else:
            # Single/anonymous fixed update - use generic flag
            return f"(({self.data_type}*)data)->_fixed_update_enabled = false;"

    def emit_remove_late_update(self, node: Dict) -> str:
        """removeLateUpdate() -> disable on_late_update callback via _late_update_enabled flag."""
        return f"(({self.data_type}*)data)->_late_update_enabled = false;"

    def emit_remove_render2d(self, node: Dict) -> str:
        """removeRender2D() -> disable on_render2d callback via _render2d_enabled flag."""
        return f"(({self.data_type}*)data)->_render2d_enabled = false;"

    def emit_notify_update(self, node: Dict) -> str:
        """notifyOnUpdate(callback) at runtime -> re-enable specific on_update callback."""
        callback_name = node.get("value")
        if callback_name:
            # Multiple update functions - use specific flag
            return f"(({self.data_type}*)data)->_update_{callback_name}_enabled = true;"
        else:
            # Single/anonymous update - use generic flag
            return f"(({self.data_type}*)data)->_update_enabled = true;"

    def emit_notify_render2d(self, node: Dict) -> str:
        """notifyOnRender2D() at runtime -> re-enable on_render2d callback."""
        return f"(({self.data_type}*)data)->_render2d_enabled = true;"

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

    def emit_render2d_fill_rect(self, node: Dict) -> str:
        """g2.fillRect(x, y, w, h) -> render2d_fill_rect(x, y, x+w, y+h, _g2_color)."""
        props = node.get("props", {})
        x = self.emit(props.get("x", {})) or "0"
        y = self.emit(props.get("y", {})) or "0"
        width = self.emit(props.get("width", {})) or "0"
        height = self.emit(props.get("height", {})) or "0"
        # N64 fill_rectangle takes x0, y0, x1, y1 (corners, not width/height)
        return f"render2d_fill_rect({x}, {y}, ({x}) + ({width}), ({y}) + ({height}), _g2_color);"

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
            owner_c_name = self.all_traits.get(owner, {}).get("c_name", owner.replace(".", "_").lower())
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

            # Handle elements.length for UI elements array
            if obj_type == "member" and field in ("length", "count"):
                if obj_value == "elements":
                    # Koui elements.length -> N64 UI element count (unified array)
                    return "UI_ELEMENT_COUNT"

            # Emit object and access field
            obj = self.emit(obj_node)
            if obj:
                # Transform field access: object->transform.loc
                if "transform." in field:
                    subfield = field.replace("transform.", "")
                    return f"{obj}->transform.{subfield}"

                # Object pointer access (ArmObject*, etc.)
                if "ArmObject*" in obj or "ArmCamera*" in obj or "ArmLight*" in obj:
                    return f"{obj}->{field}"

                # Handle array_access objects (e.g., elements[i].visible)
                # canvas_get_image() returns UIImage* so use -> for field access
                if obj_type == "array_access":
                    return f"{obj}->{field}"

                # Handle map_get results - they return pointers so use ->
                if obj_type == "map_get":
                    return f"{obj}->{field}"

                # Handle array_get results - they return values so use .
                # (but if the element is a struct with pointer, this varies)

                # Handle member types that are pointers (UIGroup*, UILabel*, etc.)
                # These need -> access since they're pointer types stored in data struct
                if obj_type == "member":
                    member_name = obj_value
                    member_ctype = self._get_member_ctype(member_name)
                    if member_ctype and member_ctype.endswith("*"):
                        return f"{obj}->{field}"
                    # Button members (uint8_t index) — field access goes through canvas_get_button()
                    # Haxe uses camelCase (focusUp), C struct uses snake_case (focus_up)
                    member_haxe_type = self._get_member_haxe_type(member_name)
                    if member_haxe_type == "Button":
                        c_field = self._camel_to_snake(field)
                        return f"canvas_get_button({obj})->{c_field}"

                # Through a pointer (Haxe marks it)
                if node.get("props", {}).get("deref"):
                    return f"{obj}->{field}"

                return f"({obj}).{field}"

        return field

    def emit_array_access(self, node: Dict) -> str:
        """Array access: array[index]

        IR format:
            type: "array_access"
            children: [array_node, index_node]

        For N64 UI elements (like Koui's elements array), this maps to
        canvas_get_image() since logo screens typically use images.
        Returns a pointer so field access like .visible works directly.
        """
        children = node.get("children", [])
        if len(children) < 2:
            return ""

        array_node = children[0]
        index_node = children[1]

        array_expr = self.emit(array_node)
        index_expr = self.emit(index_node)

        if not index_expr:
            return ""

        # Check for UI elements array (Koui pattern)
        # elements[i] -> canvas_get_image(i) for image-based UIs like splash screens
        array_type = array_node.get("type", "")
        array_value = array_node.get("value", "")

        if array_type == "member" and array_value == "elements":
            # Map to canvas_get_image() - returns UIImage* with .visible field
            return f"canvas_get_image({index_expr})"

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

        # Check for special case: elements[i].visible = value
        # This maps to canvas_element_set_visible(i, value) for proper group handling
        target_node = children[0]
        if target_node.get("type") == "field_access" and target_node.get("value") == "visible":
            obj_node = target_node.get("object", {})
            if obj_node.get("type") == "array_access":
                array_children = obj_node.get("children", [])
                if len(array_children) >= 2:
                    array_node = array_children[0]
                    index_node = array_children[1]
                    # The `elements` member (Koui elements array)
                    if (array_node.get("type") == "member" and
                        array_node.get("value") == "elements"):
                        # This is elements[i].visible = value
                        index_expr = self.emit(index_node)
                        value_expr = self.emit(children[1])
                        if index_expr and value_expr:
                            return f"canvas_element_set_visible({index_expr}, {value_expr});"
                        return ""

            # Check for UIGroup member visible assignment: groupMember.visible = value
            # This needs ui_group_set_visible() to propagate visibility to children
            if obj_node.get("type") == "member":
                member_name = obj_node.get("value")
                member_info = self.member_map.get(member_name, {})
                member_ctype = member_info.get("ctype", "")
                if member_ctype == "UIGroup*":
                    group_expr = self.emit(obj_node)
                    value_expr = self.emit(children[1])
                    if group_expr and value_expr:
                        return f"ui_group_set_visible({group_expr}, {value_expr});"
                    return ""

        target = self.emit(children[0])
        value = self.emit(children[1])

        # Only emit if both target and value are non-empty
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

        then_nodes = props.get("then", [])
        if not then_nodes:
            return ""

        # Emit body with single indent level (caller adds outer indent via emit_statements)
        then_code = self.emit_statements(then_nodes, "    ")
        result = f"if ({cond}) {{\n{then_code}\n}}"

        # Check for else branch
        else_nodes = props.get("else_")
        if else_nodes:
            else_code = self.emit_statements(else_nodes, "    ")
            result += f" else {{\n{else_code}\n}}"

        return result

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
        """For loop with integer range: for (int i = start; i < end; i++) { body }

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

        return f"for (int {var_name} = {start}; {var_name} < {end}; {var_name}++) {body_block}"

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
        """
        ctype = node.get("value", "")
        container = self.emit(node.get("object"))
        args = [self.emit(c) for c in (node.get("children") or [])[:nargs]]
        if not ctype or not container or len(args) < nargs or not all(args):
            return ""
        return f"{ctype}_{func}({', '.join([f'&{container}'] + args)})"

    def emit_map_set(self, node: Dict) -> str:
        return self._container_call(node, "set", 2)

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
        if func_name:
            # IR uses 'args' for call arguments
            args = node.get("args", []) or node.get("children", [])
            arg_strs = [self.emit(a) for a in args if self.emit(a)]
            return f"{func_name}({', '.join(arg_strs)})"
        return ""

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

        # Emit with standard obj, data parameters plus any extra args
        # For callbacks like Float->Void, the float value comes first
        arg_strs = [self.emit(a) for a in args if self.emit(a)]
        # Always append obj, data for our callback convention
        arg_strs.extend(["obj", "data"])
        return f"if ({name}) {{ {name}({', '.join(arg_strs)}); }}"

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
        parent_c_name = parent_ir.get("c_name", parent_name.lower())

        # super(): the parent's constructor, i.e. its on_ready
        if method == "new":
            return f"{parent_c_name}_on_ready(obj, data)"
        # super.method(): the C function of the ancestor declaring it (Haxe gives its name)
        arg_strs = ["obj", "data"] + [a for a in (self.emit(a) for a in args) if a]
        return f"{node.get('cName', '')}({', '.join(arg_strs)})"

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

        # Collect capture assignments that need to happen before the call
        capture_assignments = []

        # Emit argument values - skip nodes become NULL for optional callback params
        arg_strs = []
        for a in args:
            if a and a.get("type") == "skip":
                # Skip node = omitted optional parameter, pass NULL
                arg_strs.append("NULL")
            elif a and a.get("type") == "callback_wrapper":
                # Callback wrapper - use the generated function name
                arg_strs.append(a.get("callback_name", ""))
                # Generate capture assignments for any captured params
                captures = a.get("captures", [])
                for cap in captures:
                    if cap.get("is_param", False):
                        cap_name = cap.get("name", "")
                        if cap_name:
                            global_name = f"{self.c_name}_capture_{cap_name}"
                            capture_assignments.append(f"{global_name} = {cap_name}")
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

        # If we have capture assignments, emit them before the call
        if capture_assignments:
            return "; ".join(capture_assignments) + "; " + method_call
        return method_call

    def _vtable_call(self, method: str) -> str:
        """`((OwnerData*)data)->_vfn_method` for virtual methods, through the struct
        of the trait holding the pointer; empty for non-virtual methods."""
        owner = self.vtable_owners.get(method)
        if not owner:
            return ""
        owner_c_name = self.all_traits.get(owner, {}).get("c_name", owner.lower())
        return f"(({owner_c_name}Data*)data)->_vfn_{method}"

    def emit_method_call(self, node: Dict) -> str:
        """Method call on object: object.method(args).

        IR format:
            type: "method_call"
            method: "play"
            object: { type: "ident", value: "handle" }
            args: [...]

        For audio handles, maps to arm_audio_* functions.
        For tween methods (start, pause, stop) on tween_float/vec4/delay objects,
        we emit both the setup call and the tween control call.
        """
        method = node.get("method", "")
        obj_node = node.get("object")
        args = node.get("args", [])

        if not method or not obj_node:
            return ""

        obj_type = obj_node.get("type", "")

        # Handle tween method chaining: tween.float(...).start()
        # obj_node is tween_float/vec4/delay, method is start/pause/stop
        if obj_type in ("tween_float", "tween_vec4", "tween_delay"):
            # Get the actual tween variable from the nested tween node
            inner_tween = obj_node.get("object")
            tween_var = self.emit(inner_tween) if inner_tween else "NULL"

            # Collect capture assignments from callbacks
            capture_assignments = []
            props = obj_node.get("props", {})
            for cb_key in ("on_update", "on_done"):
                cb = props.get(cb_key)
                if cb:
                    captures = cb.get("captures", [])
                    for cap in captures:
                        if cap.get("is_param", False):
                            pname = cap.get("name", "")
                            # Emit assignment: capture_global = param
                            capture_assignments.append(f"{self.c_name}_capture_{pname} = {pname}")

            # Emit the setup call (tween_float, etc.)
            setup_call = self.emit(obj_node)

            # Emit the control call (tween_start, etc.)
            if method == "start":
                control_call = f"tween_start({tween_var})"
            elif method == "pause":
                control_call = f"tween_pause({tween_var})"
            elif method == "stop":
                control_call = f"tween_stop({tween_var})"
            else:
                control_call = ""

            # Return capture assignments, setup, and control as a sequence
            parts = capture_assignments + [setup_call]
            if control_call:
                parts.append(control_call)
            return ";\n    ".join(parts)

        obj = self.emit(obj_node)
        if not obj:
            return ""

        # Routed by the class declaring the method (see emitterMethods in N64MacroBase.hx)
        owner = node.get("owner", "")

        # getAnimation() call on ArmObject -> o->animation
        if owner == "iron.object.Object" and method == "getAnimation":
            return f"((ArmObject*){obj})->animation"

        # Animation methods: play(name), pause(), resume()
        if owner == "iron.object.Animation":
            if method == "play":
                anim_name = self.emit(args[0]) if args else '""'
                return f"animation_play({obj}, {anim_name})"
            elif method == "pause":
                return f"animation_pause({obj})"
            elif method == "resume":
                return f"animation_resume({obj})"
            elif method == "setSpeed":
                spd = self.emit(args[0]) if args else "1.0f"
                return f"animation_set_speed({obj}, {spd})"

        # Audio handle methods
        if owner != "aura.channels.BaseChannelHandle":
            pass
        elif method == "play":
            return f"arm_audio_start(&{obj})"
        elif method == "stop":
            return f"arm_audio_stop(&{obj})"
        elif method == "pause":
            return f"arm_audio_stop(&{obj})"  # No pause in libdragon
        elif method == "setVolume":
            vol = "1.0f"
            if args:
                vol = self.emit(args[0])
            return f"arm_audio_set_volume(&{obj}, {vol})"

        # Fallback: unknown method, emit as C function call
        arg_strs = [self.emit(a) for a in args]
        return f"{obj}.{method}({', '.join(arg_strs)})"

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
            return "NULL"

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
            return "NULL"

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
            return "0"

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

        return f"{reg_fn}({button_expr}, {cb_name}, data);"

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

    def emit_label_set_text(self, node: Dict) -> str:
        """label.text = value -> ui_label_set_text(label, value)"""
        props = node.get("props", {})
        label_name = props.get("label", "")
        args = node.get("args", [])

        if not label_name or not args:
            return ""

        # Check if this is a member variable (stored in data struct) or local variable
        if label_name in self.member_names:
            label_expr = f"(({self.data_type}*)data)->{label_name}"
        else:
            label_expr = label_name

        value = self.emit(args[0])
        return f"ui_label_set_text({label_expr}, {value});"

    def emit_transform_call(self, node: Dict) -> str:
        """Transform calls - substitute placeholders in macro-provided c_code."""
        c_code = node.get("c_code", "")
        if not c_code:
            return ""

        args = node.get("args", [])
        arg_strs = [self.emit(a) for a in args]

        # Substitute {0}, {1}, {2}, etc. with emitted args
        # Provide default "1.0f" for missing optional args (e.g., move scale)
        for i in range(10):
            placeholder = "{" + str(i) + "}"
            if placeholder in c_code:
                val = arg_strs[i] if i < len(arg_strs) else "1.0f"
                c_code = c_code.replace(placeholder, val)

        # Substitute {obj} with the transform owner (or "obj" for self)
        obj_node = node.get("object")
        obj_str = self.emit(obj_node) if obj_node else "obj"
        c_code = c_code.replace("{obj}", obj_str)

        return c_code

    def emit_math_call(self, node: Dict) -> str:
        """Math.sin() -> sinf(), etc. - use c_code if available."""
        c_code = node.get("c_code", "")
        if c_code:
            return self._substitute_placeholders(c_code, node.get("args", []))
        # Fallback for simple math calls
        c_func = node.get("value", "")
        args = node.get("args", [])
        arg_strs = [self.emit(a) for a in args if self.emit(a)]
        return f"{c_func}({', '.join(arg_strs)})"

    def emit_input_call(self, node: Dict) -> str:
        """Input calls - macro provides C code."""
        return node.get("c_code", "0")

    def emit_physics_call(self, node: Dict) -> str:
        """Physics calls - substitute placeholders in macro-provided c_code."""
        c_code = node.get("c_code", "")
        if not c_code:
            return ""

        obj_node = node.get("object")
        obj = self.emit(obj_node) if obj_node else "((ArmObject*)obj)"

        return self._substitute_placeholders(c_code, node.get("args", []), obj=obj)

    # Signals: rendered by signal_codegen.SignalContext (set by the generator)

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
        return f"{func}({ptr}, {name}, {ctx})"

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

    def emit_cast_call(self, node: Dict) -> str:
        """Std.int() -> (int32_t)value"""
        cast = node.get("value", "(int32_t)")
        args = node.get("args", [])
        if args:
            return f"{cast}({self.emit(args[0])})"
        return ""

    def emit_string_call(self, node: Dict) -> str:
        """StringTools calls -> C string functions.

        starts_with: strncmp(str, prefix, strlen(prefix)) == 0
        ends_with:   str_ends_with(str, suffix) (custom helper)
        contains:    strstr(str, sub) != NULL
        """
        func = node.get("value", "")
        args = node.get("args", [])
        if len(args) < 2:
            return ""
        s = self.emit(args[0])
        sub = self.emit(args[1])
        if not s or not sub:
            return ""
        if func == "starts_with":
            return f"(strncmp({s}, {sub}, strlen({sub})) == 0)"
        elif func == "ends_with":
            return f"str_ends_with({s}, {sub})"
        elif func == "contains":
            return f"(strstr({s}, {sub}) != NULL)"
        return ""

    def emit_debug_call(self, node: Dict) -> str:
        """trace() -> debugf()"""
        args = node.get("args", [])
        if not args:
            return 'debugf("")'
        arg_strs = [self.emit(a) for a in args if self.emit(a)]
        if len(arg_strs) == 1:
            return f'debugf("%s\\n", {arg_strs[0]})'
        return f'debugf("{", ".join(["%s"] * len(arg_strs))}\\n", {", ".join(arg_strs)})'

    def emit_object_call(self, node: Dict) -> str:
        """Object calls - return macro-provided c_code."""
        return node.get("c_code", "")

    def emit_vec_call(self, node: Dict) -> str:
        """Vec calls - substitute placeholders in macro-provided c_code.

        Special handling for camera traits: when setting transform.loc on 'object',
        also update the camera's look-at target to maintain fixed look direction.
        """
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

        result = self._substitute_placeholders(c_code, node.get("args", []), vec=vec, vec_raw=obj)

        # Special handling for camera traits: when updating camera transform.loc,
        # also update the look-at target to follow the player
        if self._is_camera_transform_loc_set(obj_node):
            # Check if this trait has a 'target' member (camera follow pattern)
            if self._trait_has_member("target"):
                # Get the target member and update camera's look-at point
                # This keeps the look direction constant as the camera moves
                result += f" ((ArmCamera*)obj)->target.v[0] = it_world_x(&((ArmObject*)(({self.c_name}Data*)data)->target)->transform); ((ArmCamera*)obj)->target.v[1] = it_world_y(&((ArmObject*)(({self.c_name}Data*)data)->target)->transform); ((ArmCamera*)obj)->target.v[2] = it_world_z(&((ArmObject*)(({self.c_name}Data*)data)->target)->transform);"

        return result

    def _is_camera_transform_loc_set(self, obj_node: Dict) -> bool:
        """Check if we're setting object.transform.loc (camera position)."""
        if not obj_node:
            return False
        if obj_node.get("type") != "field_access":
            return False
        if obj_node.get("value") != "transform.loc":
            return False
        inner = obj_node.get("object", {})
        return inner.get("type") == "ident" and inner.get("value") == "object"

    def _trait_has_member(self, member_name: str) -> bool:
        """Check if current trait has a member with given name."""
        # Check trait member names
        return member_name in self.member_names

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
        loop = props.get("loop", "false")

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

        If callbacks have captured parameters, we emit assignments to the
        capture globals before the tween call.
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

        # Generate capture assignments for param captures
        capture_lines = []
        for callback_info in [on_update, on_done]:
            if callback_info:
                captures = callback_info.get("captures", [])
                for cap in captures:
                    if cap.get("is_param", False):
                        cap_name = cap.get("name", "")
                        # Assign param to capture global
                        capture_lines.append(f"{self.c_name}_capture_{cap_name} = {cap_name}")

        # Tween returns itself for chaining, so emit setup
        # tween_float(tween, from, to, duration, on_update, on_done, ease, obj, data)
        # For traits, pass obj/data so callbacks can access trait members
        # For autoloads, pass NULL (callbacks access globals directly)
        obj_param = "obj" if self.is_trait else "NULL"
        data_param = "data" if self.is_trait else "NULL"

        tween_call = f"tween_float({tween_var}, {from_val}, {to_val}, {duration}, {update_str}, {done_str}, {ease}, {obj_param}, {data_param})"

        # If we have capture assignments, combine them with semicolons
        if capture_lines:
            # Return multiple statements - need to handle this carefully
            # We'll join with "; " so the emit_statements can handle it
            return "; ".join(capture_lines) + "; " + tween_call

        return tween_call

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

        # Generate capture assignments for param captures
        capture_lines = []
        for callback_info in [on_update, on_done]:
            if callback_info:
                captures = callback_info.get("captures", [])
                for cap in captures:
                    if cap.get("is_param", False):
                        cap_name = cap.get("name", "")
                        capture_lines.append(f"{self.c_name}_capture_{cap_name} = {cap_name}")

        obj_param = "obj" if self.is_trait else "NULL"
        data_param = "data" if self.is_trait else "NULL"
        tween_call = f"tween_vec4({tween_var}, &{from_val}, &{to_val}, {duration}, {update_str}, {done_str}, {ease}, {obj_param}, {data_param})"

        if capture_lines:
            return "; ".join(capture_lines) + "; " + tween_call
        return tween_call

    def emit_tween_delay(self, node: Dict) -> str:
        """Emit tween_delay() call."""
        obj = node.get("object")
        args = node.get("args", [])
        props = node.get("props", {})

        tween_var = self.emit(obj) if obj else "NULL"
        duration = self.emit(args[0]) if args else "1.0f"

        on_done = props.get("on_done")
        done_str = self._tween_callback_name(on_done, "_done") or "NULL"

        # Generate capture assignments for param captures
        capture_lines = []
        if on_done:
            captures = on_done.get("captures", [])
            for cap in captures:
                if cap.get("is_param", False):
                    cap_name = cap.get("name", "")
                    capture_lines.append(f"{self.c_name}_capture_{cap_name} = {cap_name}")

        obj_param = "obj" if self.is_trait else "NULL"
        data_param = "data" if self.is_trait else "NULL"
        tween_call = f"tween_delay({tween_var}, {duration}, {done_str}, {obj_param}, {data_param})"

        if capture_lines:
            return "; ".join(capture_lines) + "; " + tween_call
        return tween_call

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
