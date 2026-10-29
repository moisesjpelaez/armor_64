"""
Trait code generation: data/traits.h and data/traits.c from n64_traits.json
(the macros' IR): each trait's data struct, handlers, methods and callbacks
(tweens, buttons, signals), and the inheritance rules the scene generator and
limits share. Used by traits_exporter.
"""

from typing import Dict, List

from arm import log
from armor64.codegen.trait_emitter import TraitEmitter
from armor64.codegen import tween_helper
from armor64.codegen.ir import load_ir_json, nodes_of_type, walk_ir
from armor64.codegen.signal_codegen import SignalContext, find_signal_bindings


# =============================================================================
# Inheritance rules
#
# Shared by the struct layout, instance allocation (scene generator) and call
# sites, so they can't disagree. `all_traits` maps trait name -> trait IR.
#
# A child's data struct embeds its parent's first (_parent), down to the base
# trait's, which holds the object. A virtual method (overriding an ancestor's,
# or overridden) has a function pointer, _vfn_<method>, in the struct of the
# topmost trait declaring it, set to the instance's most derived
# implementation when its data is initialized (vtable_initializers); calls go
# through it, super.method() calls the parent's function directly.
# =============================================================================

def _ancestors(all_traits: dict, name: str):
    """Parent, grandparent, ... of a trait (exported traits only)."""
    current = all_traits.get(name, {}).get("parent")
    while current and current in all_traits:
        yield current
        current = all_traits[current].get("parent")


def is_virtual_method(all_traits: dict, name: str, method: str) -> bool:
    """A method of trait `name` needs vtable dispatch if it overrides an
    ancestor's method or a descendant overrides it (in Haxe a method with an
    ancestor's name is always an override)."""
    if any(method in all_traits[a].get("methods", {}) for a in _ancestors(all_traits, name)):
        return True
    return any(method in ir.get("methods", {}) for other, ir in all_traits.items()
               if name in _ancestors(all_traits, other))


def vtable_owner(all_traits: dict, name: str, method: str) -> str:
    """Trait whose struct holds the vtable pointer of a method: the topmost
    ancestor (or `name` itself) declaring it. Overrides set that pointer."""
    owner = name
    for ancestor in _ancestors(all_traits, name):
        if method in all_traits[ancestor].get("methods", {}):
            owner = ancestor
    return owner


def declared_vtable_methods(all_traits: dict, name: str) -> list:
    """Virtual methods whose vtable pointer lives in this trait's struct."""
    return [m for m in all_traits.get(name, {}).get("methods", {})
            if is_virtual_method(all_traits, name, m) and vtable_owner(all_traits, name, m) == name]


def vtable_initializers(all_traits: dict, instance: str, level: str) -> list:
    """(method, C function) of each virtual method pointer in `level`'s struct
    (`instance` or one of its ancestors) for an instance of `instance`: the most
    derived implementation, set when the data is initialized, so it works
    from the start (on_add, other traits), as in Haxe."""
    chain = [instance, *_ancestors(all_traits, instance)]
    pointers = []
    for method in declared_vtable_methods(all_traits, level):
        owner = next(t for t in chain if method in all_traits[t].get("methods", {}))
        pointers.append((method, all_traits[owner]["methods"][method]["cName"]))
    return pointers


def visible_vtable_owners(all_traits: dict, name: str) -> dict:
    """Virtual methods callable from trait `name` (own and inherited) -> the
    trait whose struct holds their vtable pointer."""
    owners = {}
    for trait in [name, *_ancestors(all_traits, name)]:
        for method in all_traits.get(trait, {}).get("methods", {}):
            if method not in owners and is_virtual_method(all_traits, trait, method):
                owners[method] = vtable_owner(all_traits, trait, method)
    return owners


def chain_events(all_traits: dict, name: str) -> set:
    """Events of a trait and its ancestors: a trait's handlers call its
    parent's, so the runtime must call them when any ancestor has one."""
    events = set(all_traits.get(name, {}).get("events", {}))
    for ancestor in _ancestors(all_traits, name):
        events |= set(all_traits[ancestor].get("events", {}))
    return events


def needs_data_struct(all_traits: dict, name: str) -> bool:
    """Whether a trait has a data struct (and its instances get data)."""
    ir = all_traits.get(name, {})
    meta = ir.get("meta", {})
    return bool(ir.get("members") or meta.get("signals") or meta.get("connects_signals")
                or ir.get("parent") or meta.get("stores_self")
                or declared_vtable_methods(all_traits, name)
                or tween_helper.param_captures([ir.get("events", {}), ir.get("methods", {})]))


# =============================================================================
# The IR files
# =============================================================================

def load_signals_json(build_dir: str = None) -> dict:
    """Load n64_signals.json: the signals used by the game (see signal_codegen)."""
    return load_ir_json("n64_signals.json", "signals", build_dir)


def signals_context(traits: dict = None) -> SignalContext:
    """Signal definitions with the trait and autoload info to render them."""
    import arm.utils
    from armor64.codegen.autoload_generator import load_autoloads_json

    build_dir = arm.utils.build_dir()
    if traits is None:
        traits = load_traits_json(build_dir).get("traits", {})
    return SignalContext(load_signals_json(build_dir).get("signals", {}), traits,
                         load_autoloads_json(build_dir).get("autoloads", {}))


def load_traits_json(build_dir: str = None) -> dict:
    """Load the n64_traits.json file generated by the macro."""
    return load_ir_json("n64_traits.json", "traits", build_dir)


def get_trait_info(build_dir: str = None) -> dict:
    """
    Load traits JSON and return in expected format.

    This is the main entry point for other code to get trait info.
    Returns dict with:
        ir_version: Schema version
        traits: {TraitName: {module, c_name, members, events, meta}}
    """
    return load_traits_json(build_dir)


# =============================================================================
# Trait Code Generator
# =============================================================================

class TraitCodeGenerator:
    """The C of one trait: its data struct, lifecycle handlers (each calling
    its parent's first), methods (C functions taking obj and data), and the
    functions its callbacks become. Its code is its own: an inherited member or
    method is reached through the ancestor's struct or function."""

    def __init__(self, name: str, ir: Dict, type_overrides: Dict = None, all_traits: Dict = None,
                 signals: SignalContext = None):
        self.name = name
        self.c_name = ir["c_name"]
        self.members = ir.get("members", [])
        self.methods = ir.get("methods", {})  # Callable methods (non-lifecycle)
        self.events = ir.get("events", {})
        self.meta = ir.get("meta", {})
        self.type_overrides = type_overrides or {}

        # Inheritance support
        self.parent_name = ir.get("parent")  # Parent trait name (None if no parent)
        self.all_traits = all_traits or {}   # All traits for looking up parent info

        # Build member names list (this trait's own members only)
        member_names = [m.get("name") for m in self.members]

        # Build complete member map including inherited members for routing
        self.member_map = self._build_member_map()

        vtable_owners = visible_vtable_owners(self.all_traits, self.name)

        # Create emitter with inheritance info
        self.emitter = TraitEmitter(
            name,
            self.c_name,
            member_names,
            is_trait=True,
            parent_name=self.parent_name,
            all_traits=self.all_traits,
            methods=self.methods,
            vtable_owners=vtable_owners,
            member_map=self.member_map
        )
        self.signals = signals
        self.emitter.signals = signals
        self._tween_callbacks = []  # Collected tween callbacks from all events
        self._inherited_callbacks = []  # Collected callback wrappers for inherited method calls
        self._button_callbacks = []  # Collected button_register_callback nodes

    def _build_member_map(self) -> Dict[str, Dict]:
        """Build a map of all members including inherited ones.

        Returns dict of member_name -> {ctype, owner, depth} where:
        - ctype: C type of the member
        - owner: trait name that owns this member
        - depth: inheritance depth (0 = this class, 1 = parent, 2 = grandparent, etc.)
        """
        member_map = {}

        # Add this trait's own members at depth 0
        for m in self.members:
            mname = m.get("name", "")
            member_map[mname] = {
                "ctype": m["ctype"],
                "type": m.get("type", ""),
                "owner": self.name,
                "depth": 0
            }

        # Walk up inheritance chain
        depth = 1
        current_parent = self.parent_name
        while current_parent and current_parent in self.all_traits:
            parent_ir = self.all_traits[current_parent]
            parent_members = parent_ir.get("members", [])
            for m in parent_members:
                mname = m.get("name", "")
                # Don't override if child has same member (child shadows parent)
                if mname not in member_map:
                    member_map[mname] = {
                        "ctype": m["ctype"],
                        "type": m.get("type", ""),
                        "owner": current_parent,
                        "depth": depth
                    }
            # Move up to grandparent
            current_parent = parent_ir.get("parent")
            depth += 1

        return member_map

    def _get_member_ctype(self, member: Dict) -> str:
        """Get the C type for a member, applying overrides if present."""
        name = member.get("name", "unknown")
        ctype = member["ctype"]
        # Apply type override if present
        if self.name in self.type_overrides:
            if name in self.type_overrides[self.name]:
                ctype = self.type_overrides[self.name][name]
        return ctype

    def _get_object_access_path(self) -> str:
        """Get the path to access 'object' member from trait data struct.

        For traits with inheritance, 'object' is in the base struct, so we need
        to traverse the _parent chain. _parent is embedded by value, not pointer.
        For example:
        - No parent: data->object
        - One parent: data->_parent.object
        - Two parents: data->_parent._parent.object
        """
        path = "data->"
        current = self.name
        while current and current in self.all_traits:
            current_ir = self.all_traits[current]
            parent = current_ir.get("parent")
            if parent:
                path += "_parent."
                current = parent
            else:
                break
        return f"{path}object"

    def generate_data_struct(self) -> str:
        """Generate the data struct for trait members and signals.

        For inheritance (Option B - Composition):
        - If trait has a parent, embed parent struct at offset 0
        - This allows safe casting and proper memory layout

        Example:
            typedef struct {
                GameSceneData _parent;  // Embedded parent (if any)
                int32_t score;          // This trait's members
            } LevelData;
        """
        signals = self.meta.get("signals", [])

        # Virtual methods FIRST DECLARED in this trait (overrides use the
        # pointer in the declaring ancestor's struct)
        virtual_methods = [self.methods[m] for m in declared_vtable_methods(self.all_traits, self.name)]

        if not needs_data_struct(self.all_traits, self.name):
            return ""

        lines = [f"typedef struct {{"]

        # Embed parent struct at offset 0 (if any)
        if self.parent_name:
            # Get parent's c_name for proper C identifier
            parent_c_name = self.all_traits[self.parent_name]["c_name"]
            lines.append(f"    {parent_c_name}Data _parent;")
        else:
            # Base trait - add object pointer (like iron.Trait.object)
            lines.append(f"    void* object;")

        # Virtual method function pointers (for polymorphism)
        for vm in virtual_methods:
            vm_name = vm.get("name", "")
            vm_params = vm.get("params", [])
            vm_ret = vm.get("returnType", "void")
            # Build function pointer signature: returnType (*_vfn_methodname)(void* obj, void* data, params...)
            param_types = ["void*", "void*"]  # obj and data
            for p in vm_params:
                param_types.append(p["ctype"])
            params_str = ", ".join(param_types)
            lines.append(f"    {vm_ret} (*_vfn_{vm_name})({params_str});")

        # This trait's own members
        for m in self.members:
            ctype = self._get_member_ctype(m)
            # Another trait's data (any trait, this one too): the structs are
            # anonymous typedefs declared in any order
            if m.get("trait_ref"):
                ctype = "void*"
            name = m.get("name", "unknown")
            lines.append(f"    {ctype} {name};")

        # Parameters this trait's callbacks capture (tween_helper.capture_ref)
        for callback, param, ctype in tween_helper.param_captures([self.events, self.methods]):
            lines.append(f"    {ctype} _cap_{callback}_{param};")

        # Signal members - each signal is an ArmSignal struct
        for sig in signals:
            sig_name = sig.get("name", "")
            if sig_name:
                lines.append(f"    ArmSignal {sig_name};")

        lines.append(f"}} {self.c_name}Data;")

        return "\n".join(lines)

    def _find_tween_callbacks(self, nodes: list) -> list:
        """Recursively find all tween callbacks in IR nodes."""
        # Delegate to shared helper
        return tween_helper.find_tween_callbacks(nodes)

    def _collect_callbacks_from_all_bodies(self, cache_list: list, finder_fn) -> None:
        """Scan all events AND methods for callbacks using the given finder function.

        Args:
            cache_list: List to store found callbacks (modified in place)
            finder_fn: Function(nodes) -> list of callbacks
        """
        if cache_list:
            return  # Already collected

        # Scan event bodies
        for event_nodes in self.events.values():
            cache_list.extend(finder_fn(event_nodes))

        # Scan method bodies too
        for method_ir in self.methods.values():
            method_body = method_ir.get("body", [])
            cache_list.extend(finder_fn(method_body))

    def _generate_tween_callback(self, callback_info: dict) -> str:
        """Generate a static C callback function for a tween.

        For traits, callbacks can access the trait data via the 'data' pointer
        which is passed through the tween's obj/data parameters.
        """
        # Delegate to shared helper with is_trait=True, pass c_name for captures
        return tween_helper.generate_tween_callback(
            callback_info,
            self.emitter,
            c_name=self.c_name,
            is_trait=True
        )

    def _collect_tween_callbacks(self):
        """Scan all events AND methods for tween callbacks and store them."""
        self._collect_callbacks_from_all_bodies(
            self._tween_callbacks,
            self._find_tween_callbacks
        )

    def generate_tween_callbacks(self) -> str:
        """Generate all tween callback functions for this trait."""
        self._collect_tween_callbacks()

        if not self._tween_callbacks:
            return ""

        lines = []
        seen_callbacks = set()

        for cb in self._tween_callbacks:
            cb_name = cb.get("callback_name", "")
            if cb_name and cb_name not in seen_callbacks:
                seen_callbacks.add(cb_name)
                cb_code = self._generate_tween_callback(cb)
                if cb_code:
                    lines.append(cb_code)
                    lines.append("")

        return "\n".join(lines)

    def _find_inherited_callbacks(self, nodes: list) -> list:
        return nodes_of_type(nodes, "callback_wrapper")

    def _collect_inherited_callbacks(self):
        """Scan all events AND methods for inherited method callback wrappers."""
        self._collect_callbacks_from_all_bodies(
            self._inherited_callbacks,
            self._find_inherited_callbacks
        )

    def _generate_inherited_callback(self, callback_info: dict) -> str:
        """Generate a C callback function for an inherited method call.

        These callbacks are passed to parent methods which typically pass them
        to tweens. They need obj/data parameters to access trait members.
        The signature matches tween done callbacks: void cb(void* obj, void* data)
        """
        cb_name = callback_info.get("callback_name", "")
        param_name = callback_info.get("param_name")
        param_ctype = callback_info.get("param_ctype", "void")
        body_nodes = callback_info.get("body", [])
        captures = callback_info.get("captures", [])

        if not cb_name:
            return ""

        lines = []

        # All inherited method callbacks need obj/data for member access
        # If the callback also has a value parameter (like Float->Void), include it
        if param_name and param_ctype != "void":
            lines.append(f"static void {cb_name}({param_ctype} {param_name}, void* obj, void* data) {{")
        else:
            lines.append(f"static void {cb_name}(void* obj, void* data) {{")

        # The captured parameters, read where the call stored them
        param_captures = {cap["name"]: tween_helper.capture_ref(self.c_name, True, cb_name, cap["name"])
                          for cap in captures if cap.get("is_param", False)}

        # Use capture-aware emitter if we have param captures
        emitter = self.emitter
        if param_captures:
            emitter = tween_helper._CaptureEmitter(self.emitter, param_captures)

        lines.append("    (void)obj;")
        body = emitter.emit_statements(body_nodes, "    ")
        if body:
            lines.append(body)
        lines.append("}")
        return "\n".join(lines)

    def generate_inherited_callbacks(self) -> str:
        """Generate all inherited method callback functions for this trait.

        Generates forward declarations first to handle mutual references between
        callbacks (e.g., fadeIn_cb calls fadeOut which uses fadeOut_cb).
        """
        self._collect_inherited_callbacks()

        if not self._inherited_callbacks:
            return ""

        lines = []
        seen_callbacks = set()

        # Second pass: collect unique callbacks with their signatures for forward declarations
        unique_callbacks = []
        for cb in self._inherited_callbacks:
            cb_name = cb.get("callback_name", "")
            if cb_name and cb_name not in seen_callbacks:
                seen_callbacks.add(cb_name)
                unique_callbacks.append(cb)

        # Generate forward declarations with matching signatures
        if len(unique_callbacks) > 1:
            lines.append("// Forward declarations for inherited callbacks")
            for cb in unique_callbacks:
                cb_name = cb.get("callback_name", "")
                param_name = cb.get("param_name")
                param_ctype = cb.get("param_ctype", "void")
                if param_name and param_ctype != "void":
                    lines.append(f"static void {cb_name}({param_ctype} {param_name}, void* obj, void* data);")
                else:
                    lines.append(f"static void {cb_name}(void* obj, void* data);")
            lines.append("")

        # Reset seen for third pass
        seen_callbacks = set()

        # Third pass: generate full definitions
        for cb in self._inherited_callbacks:
            cb_name = cb.get("callback_name", "")
            if cb_name and cb_name not in seen_callbacks:
                seen_callbacks.add(cb_name)
                cb_code = self._generate_inherited_callback(cb)
                if cb_code:
                    lines.append(cb_code)
                    lines.append("")

        return "\n".join(lines)

    def _find_button_callbacks(self, nodes: list) -> list:
        return nodes_of_type(nodes, "button_register_callback")

    def _collect_button_callbacks(self):
        """Scan all events AND methods for button callback registrations."""
        self._collect_callbacks_from_all_bodies(
            self._button_callbacks,
            self._find_button_callbacks
        )

    def _generate_button_callback(self, callback_info: dict) -> str:
        """Generate a static C callback function for a button event.

        ButtonCallback signature: void (*)(int button_index, void* user_data)
        user_data is the trait data pointer.
        """
        props = callback_info.get("props", {})
        cb_name = props.get("callback_name", "")
        body_nodes = callback_info.get("body", [])

        if not cb_name:
            return ""

        lines = []
        lines.append(f"static void {cb_name}(int button_index, void* user_data) {{")
        lines.append(f"    (void)button_index;")
        lines.append(f"    {self.c_name}Data* data = ({self.c_name}Data*)user_data;")

        # Get object pointer from data struct, traversing inheritance chain
        obj_access = self._get_object_access_path()
        lines.append(f"    void* obj = (void*){obj_access};")
        lines.append(f"    (void)obj;")

        body = self.emitter.emit_statements(body_nodes, "    ")
        if body:
            lines.append(body)
        lines.append("}")
        return "\n".join(lines)

    def generate_button_callbacks(self) -> str:
        """Generate all button callback static functions for this trait."""
        self._collect_button_callbacks()

        if not self._button_callbacks:
            return ""

        lines = []
        seen_callbacks = set()

        for cb in self._button_callbacks:
            props = cb.get("props", {})
            cb_name = props.get("callback_name", "")
            if cb_name and cb_name not in seen_callbacks:
                seen_callbacks.add(cb_name)
                cb_code = self._generate_button_callback(cb)
                if cb_code:
                    lines.append(cb_code)
                    lines.append("")

        return "\n".join(lines)

    def generate_lifecycle_declarations(self) -> List[str]:
        """Generate declarations for lifecycle event handlers."""
        decls = []
        # Match the typedefs in types.h:
        # ArmTraitAddFn: (void *entity, void *data) - no dt, called when trait is added
        # ArmTraitReadyFn: (void *entity, void *data) - no dt
        # ArmTraitFixedUpdateFn: (void *entity, float dt, void *data)
        # ArmTraitUpdateFn: (void *entity, float dt, void *data)
        # ArmTraitLateUpdateFn: (void *entity, float dt, void *data)
        # ArmTraitRemoveFn: (void *entity, void *data) - no dt
        # ArmTraitRender2DFn: (void *entity, void *data) - no dt, for 2D overlay rendering
        decls.append(f"void {self.c_name}_on_add(void* obj, void* data);")
        decls.append(f"void {self.c_name}_on_ready(void* obj, void* data);")
        if self.handles("on_fixed_update"):
            decls.append(f"void {self.c_name}_on_fixed_update(void* obj, float _dt, void* data);")
            # The functions notifyOnFixedUpdate registers, when there are several
            for callback_name in self.meta.get("dynamic_fixed_updates", []):
                decls.append(f"void {self.c_name}_on_fixed_update_{callback_name}(void* obj, float _dt, void* data);")
        if self.handles("on_update"):
            decls.append(f"void {self.c_name}_on_update(void* obj, float _dt, void* data);")
            for callback_name in self.meta.get("dynamic_updates", []):
                decls.append(f"void {self.c_name}_on_update_{callback_name}(void* obj, float _dt, void* data);")
        if self.handles("on_late_update"):
            decls.append(f"void {self.c_name}_on_late_update(void* obj, float _dt, void* data);")
        if self.handles("on_remove"):
            decls.append(f"void {self.c_name}_on_remove(void* obj, void* data);")
        if self.handles("on_render2d"):
            decls.append(f"void {self.c_name}_on_render2d(void* obj, void* data);")
        return decls

    def contact_handler_signature(self, event_name: str) -> str:
        """A contact handler (PhysicsContactHandler): (obj, data, the other body's
        object), the last parameter named as the Haxe handler's RigidBody one."""
        handler = event_name[len("contact_"):]
        param = next((e.get("param_name") for e in self.meta.get("contact_events", [])
                      if e.get("event_name") == handler and e.get("param_name")), "_other")
        return f"void {self.c_name}_{event_name}(void* obj, void* data, ArmObject* {param})"

    def generate_contact_event_declarations(self) -> List[str]:
        """Generate declarations for contact event handlers."""
        return [f"{self.contact_handler_signature(name)};" for name in self.events.keys()
                if name.startswith("contact_")]

    def generate_method_declarations(self) -> List[str]:
        """Generate declarations for all callable methods (non-lifecycle).

        Methods are generated as C functions with signature:
        return_type cName(void* obj, void* data[, params...]);
        """
        decls = []
        for method_name, method_ir in self.methods.items():
            c_name = method_ir["cName"]
            return_type = method_ir.get("returnType", "void")
            params = method_ir.get("params", [])

            # Build parameter string
            param_strs = ["void* obj", "void* data"]
            for p in params:
                pname = p.get("name", "")
                ctype = p["ctype"]
                param_strs.append(f"{ctype} {pname}")

            decls.append(f"{return_type} {c_name}({', '.join(param_strs)});")

        return decls

    def generate_method_implementations(self) -> str:
        """Generate C implementations for all callable methods (non-lifecycle).

        Methods are standalone C functions that can be called from any event handler.
        """
        if not self.methods:
            return ""

        impl_lines = []

        for method_name, method_ir in self.methods.items():
            c_name = method_ir["cName"]
            return_type = method_ir.get("returnType", "void")
            params = method_ir.get("params", [])
            body = method_ir.get("body", [])

            # Build parameter string
            param_strs = ["void* obj", "void* data"]
            for p in params:
                pname = p.get("name", "")
                ctype = p["ctype"]
                param_strs.append(f"{ctype} {pname}")

            impl_lines.append(f"{return_type} {c_name}({', '.join(param_strs)}) {{")

            # Check if method uses render2D features (needs _g2_color local var)
            uses_render2d = self._method_uses_render2d(body)
            if uses_render2d:
                impl_lines.append("    color_t _g2_color = RGBA32(255, 255, 255, 255);")

            # Emit body statements
            if body:
                body_code = self.emitter.emit_statements(body, "    ")
                impl_lines.append(body_code)
            else:
                impl_lines.append("    // Empty method")

            impl_lines.append("}")
            impl_lines.append("")

        return "\n".join(impl_lines)

    def _method_uses_render2d(self, nodes: list) -> bool:
        """Whether a method sets or uses the 2D drawing color (needs _g2_color)."""
        return any(n.get("type") == "render2d_set_color" or
                   (n.get("type") == "c_template" and "_g2_color" in n.get("c_code", ""))
                   for n in walk_ir(nodes))

    def handles(self, event: str) -> bool:
        """Whether the trait or an ancestor handles a lifecycle event (it then has
        the handler, and the runtime calls it; otherwise the scene gets NULL)."""
        return event in chain_events(self.all_traits, self.name)

    def _parent_event_call(self, event: str, with_dt: bool) -> list:
        """Call the parent's handler first: callbacks a parent class registers
        (e.g. in notifyOnUpdate) run for its children too, as in Armory."""
        if not (self.parent_name and self.parent_name in self.all_traits):
            return []
        if event not in chain_events(self.all_traits, self.parent_name):
            return []
        parent_c_name = self.all_traits[self.parent_name]["c_name"]
        args = "obj, _dt, data" if with_dt else "obj, data"
        return [f"    {parent_c_name}_{event}({args});"]

    def _update_handler(self, event: str) -> List[str]:
        """on_fixed_update, on_update or on_late_update (dt before data): the
        parent's first; with several functions registered (notifyOnUpdate), a
        dispatcher calling each one still enabled, else the body, skipped once
        removed (removeUpdate)."""
        if not self.handles(event):
            return []
        kind = event[len("on_"):]  # fixed_update, update, late_update
        lines = [f"void {self.c_name}_{event}(void* obj, float _dt, void* data) {{"]
        lines += self._parent_event_call(event, True)
        dynamic = self.meta.get(f"dynamic_{kind}s", [])
        if dynamic:
            lines.append(f"    {self.c_name}Data* d = ({self.c_name}Data*)data;")
            for callback_name in dynamic:
                lines.append(f"    if (d->_{kind}_{callback_name}_enabled) "
                             f"{self.c_name}_{event}_{callback_name}(obj, _dt, data);")
            lines += ["}", ""]
            for callback_name in dynamic:
                event_nodes = self.events.get(f"{event}_{callback_name}", [])
                body = self.emitter.emit_statements(event_nodes, "    ") if event_nodes else "    // Empty"
                lines += [f"void {self.c_name}_{event}_{callback_name}(void* obj, float _dt, void* data) {{",
                          body, "}", ""]
            return lines
        if self.meta.get(f"has_remove_{kind}", False):
            lines.append(f"    if (!(({self.c_name}Data*)data)->_{kind}_enabled) return;")
        event_nodes = self.events.get(event, [])
        lines += [self.emitter.emit_statements(event_nodes, "    ") if event_nodes else "    // Empty", "}", ""]
        return lines

    def generate_signal_handlers(self) -> list:
        """(name, declaration, definition) of the handlers this trait connects
        or disconnects, anywhere in its events and methods."""
        handlers = []
        for node in find_signal_bindings([self.events, self.methods]):
            props = node.get("props", {})
            handler, key = props.get("handler", {}), props.get("signal", "")
            name = self.signals.handler_name(handler, key, self.c_name)
            handlers.append((
                name,
                self.signals.handler_declaration(handler, key, self.c_name),
                self.signals.handler_function(handler, key, self.c_name, self.name, self.emitter),
            ))
        return handlers

    def generate_all_event_implementations(self) -> str:
        """Generate C implementations for all event handlers."""
        impl_lines = [f"// ========== {self.name} =========="]

        # on_add - called when trait is added, BEFORE on_ready
        # Used for setting up autoload references like MainInstances.player = this
        event_nodes = self.events.get("on_add", [])
        impl_lines.append(f"void {self.c_name}_on_add(void* obj, void* data) {{")
        if self.parent_name and self.parent_name in self.all_traits:
            parent_ir = self.all_traits[self.parent_name]
            parent_c_name = parent_ir["c_name"]
            impl_lines.append(f"    {parent_c_name}_on_add(obj, data);")
        else:
            # Base trait - initialize object pointer in data struct
            if needs_data_struct(self.all_traits, self.name):
                impl_lines.append(f"    (({self.c_name}Data*)data)->object = obj;")
        body = self.emitter.emit_statements(event_nodes, "    ") if event_nodes else "    // Empty"
        impl_lines.append(body)
        impl_lines.append("}")

        # on_ready - no dt parameter
        event_nodes = self.events.get("on_ready", [])
        impl_lines.append(f"void {self.c_name}_on_ready(void* obj, void* data) {{")

        # Call parent's on_ready FIRST (if we have a parent)
        # Pass full data pointer (not &_parent) so virtual method calls work correctly
        # Since _parent is at offset 0, casting to parent type works
        if self.parent_name and self.parent_name in self.all_traits:
            parent_ir = self.all_traits[self.parent_name]
            parent_c_name = parent_ir["c_name"]
            impl_lines.append(f"    {parent_c_name}_on_ready(obj, data);")

        # Virtual method pointers and member tweens are set when the data is
        # initialized (utils.build_trait_initializer), before any handler

        body = self.emitter.emit_statements(event_nodes, "    ") if event_nodes else "    // Empty"
        impl_lines.append(body)
        impl_lines.append("}")
        impl_lines.append("")

        for event in ("on_fixed_update", "on_update", "on_late_update"):
            impl_lines += self._update_handler(event)

        # on_remove - no dt parameter
        if self.handles("on_remove"):
            event_nodes = self.events.get("on_remove", [])
            body = self.emitter.emit_statements(event_nodes, "    ") if event_nodes else "    // Empty"
            impl_lines.append(f"void {self.c_name}_on_remove(void* obj, void* data) {{")
            impl_lines += self._parent_event_call("on_remove", False)
            impl_lines.append(body)
            impl_lines.append("}")
            impl_lines.append("")

        # on_render2d - no dt parameter, for 2D overlay rendering
        if self.handles("on_render2d"):
            event_nodes = self.events.get("on_render2d", [])
            has_render2d_content = event_nodes and len(event_nodes) > 0
            body = self.emitter.emit_statements(event_nodes, "    ") if event_nodes else "    // Empty"
            impl_lines.append(f"void {self.c_name}_on_render2d(void* obj, void* data) {{")
            # The parent's first (it draws behind), then this trait's unless it was
            # removed (removeRender2D): removing it leaves the parent's, as in Armory
            impl_lines += self._parent_event_call("on_render2d", False)
            if self.meta.get("has_remove_render2d", False):
                impl_lines.append(f"    if (!(({self.c_name}Data*)data)->_render2d_enabled) return;")
            # Only add render2d infrastructure if there's actual content
            if has_render2d_content:
                impl_lines.append("    color_t _g2_color = RGBA32(255, 255, 255, 255);")
                impl_lines.append("    render2d_begin();")
                impl_lines.append(body)
                impl_lines.append("    render2d_end();")
            else:
                impl_lines.append(body)
            impl_lines.append("}")
            impl_lines.append("")

        # Contact events - PhysicsContactHandler signature: (obj, data, other)
        for event_name in self.events.keys():
            if event_name.startswith("contact_"):
                event_nodes = self.events.get(event_name, [])
                body = self.emitter.emit_statements(event_nodes, "    ") if event_nodes else "    // Empty"
                impl_lines.append(f"{self.contact_handler_signature(event_name)} {{")
                impl_lines.append(body)
                impl_lines.append("}")
                impl_lines.append("")


        return "\n".join(impl_lines)


# =============================================================================
# Traits File Generation
# =============================================================================

def prepare_traits_template_data(type_overrides: dict = None):
    """Prepare template data for traits.h and traits.c generation.

    Args:
        type_overrides: Optional dict of {trait_name: {member_name: ctype}} overrides

    Returns:
        tuple of (template_data dict, features dict) or (None, None) if no traits
    """
    import arm.utils

    build_dir = arm.utils.build_dir()

    # Load traits from Haxe macro JSON
    data = load_traits_json(build_dir)
    traits = data.get("traits", {})

    if not traits:
        return None, {'has_physics': False, 'has_ui': False}

    # Generate template substitution data and detect features
    return _prepare_traits_template_data(traits, type_overrides)


def _validate_ancestor_chain(traits: dict) -> None:
    """Validate that all ancestors in inheritance chains are present in traits.

    Walks up the inheritance chain for each trait and ensures all ancestors
    are included in the traits dict. Raises RuntimeError if any ancestor is
    missing, which would cause C compilation errors.

    Args:
        traits: Dict of trait_name -> trait_ir

    Raises:
        RuntimeError: If an ancestor trait is referenced but not in traits dict
    """
    for trait_name, trait_ir in traits.items():
        # Walk up the entire inheritance chain
        current = trait_ir.get("parent")
        chain = [trait_name]

        while current:
            if current not in traits:
                chain_str = " -> ".join(chain)
                raise RuntimeError(
                    f"N64 Trait Error: '{trait_name}' has ancestor '{current}' that is not "
                    f"included in the N64 build. Inheritance chain: {chain_str} -> {current}. "
                    f"Ensure '{current}' has at least one lifecycle event or member."
                )
            chain.append(current)
            current = traits[current].get("parent")


def _topological_sort_traits(traits: dict) -> List[str]:
    """Sort traits in topological order (parents before children).

    For inheritance to work correctly, parent data structs must be defined
    before child data structs that embed them.

    Args:
        traits: Dict of trait_name -> trait_ir

    Returns:
        List of trait names in topological order
    """
    # Build dependency graph
    # A trait depends on its parent (parent must come first)
    in_degree = {name: 0 for name in traits}
    children = {name: [] for name in traits}

    for name, ir in traits.items():
        parent = ir.get("parent")
        if parent and parent in traits:
            in_degree[name] += 1
            children[parent].append(name)

    # Kahn's algorithm for topological sort
    # Start with traits that have no parents (in_degree = 0)
    queue = [name for name, degree in in_degree.items() if degree == 0]
    result = []

    while queue:
        # Pop from queue (stable sort: use the one added first)
        current = queue.pop(0)
        result.append(current)

        # Reduce in_degree for children
        for child in children[current]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    # If we didn't process all traits, there's a cycle (shouldn't happen in valid code)
    if len(result) != len(traits):
        log.warn("Circular inheritance detected in traits - this shouldn't happen!")
        # Add remaining traits anyway
        for name in traits:
            if name not in result:
                result.append(name)

    return result


def _prepare_traits_template_data(traits: dict, type_overrides: dict = None) -> tuple:
    """Prepare data for traits.h.j2 and traits.c.j2 templates.

    Uses TraitCodeGenerator for each trait to avoid code duplication.

    Inheritance Processing (Option B - Composition):
    - Traits are sorted in topological order (parents before children)
    - Data structs are emitted with embedded parent structs
    - Emitter routes member access through inheritance chain

    Args:
        traits: Dict of trait_name -> trait_ir from JSON
        type_overrides: Optional dict of {trait_name: {member_name: ctype}} overrides

    Returns:
        tuple of (template_data dict, features dict)
    """
    import arm.utils
    from armor64.codegen.autoload_generator import load_autoloads_json

    # Validate all ancestors are present before proceeding
    _validate_ancestor_chain(traits)

    # Sort traits in topological order (parents before children)
    sorted_traits = _topological_sort_traits(traits)

    trait_data_structs = []
    trait_declarations = []
    event_handler_declarations = []
    trait_implementations = []
    tween_callbacks = []  # Tween callback functions (must come before implementations)

    signals = signals_context(traits)
    # Signal handler functions, once each (several traits may use the same one)
    handler_names = set()
    handler_declarations = []
    handler_definitions = []

    # Track features across all traits
    all_features = {'has_physics': False, 'has_ui': False, 'has_tween': False}

    # Process traits in topological order (parents first)
    for trait_name in sorted_traits:
        trait_ir = traits[trait_name]
        gen = TraitCodeGenerator(trait_name, trait_ir, type_overrides, all_traits=traits, signals=signals)

        # Header data: data struct + declarations
        struct = gen.generate_data_struct()
        if struct:
            trait_data_structs.append(struct)

        trait_declarations.extend(gen.generate_lifecycle_declarations())
        trait_declarations.extend(gen.generate_method_declarations())  # Callable methods
        event_handler_declarations.extend(gen.generate_contact_event_declarations())

        for name, declaration, definition in gen.generate_signal_handlers():
            if name not in handler_names:
                handler_names.add(name)
                handler_declarations.append(declaration)
                handler_definitions.append(definition)

        meta = trait_ir.get("meta", {})
        # Check if this trait uses UI
        if meta.get("uses_ui"):
            all_features['has_ui'] = True

        if meta.get("uses_tween"):
            all_features['has_tween'] = True
        if meta.get("uses_physics"):
            all_features['has_physics'] = True
        all_features.setdefault('autoloads', set()).update(meta.get("autoloads_used", []))

        # Generate tween callbacks (must come before implementations that reference them)
        tween_cb_code = gen.generate_tween_callbacks()
        if tween_cb_code:
            tween_callbacks.append(f"// Tween callbacks for {trait_name}")
            tween_callbacks.append(tween_cb_code)

        # Generate inherited method callbacks (must come before implementations)
        inherited_cb_code = gen.generate_inherited_callbacks()
        if inherited_cb_code:
            tween_callbacks.append(f"// Inherited method callbacks for {trait_name}")
            tween_callbacks.append(inherited_cb_code)

        # Generate button callbacks (must come before implementations)
        button_cb_code = gen.generate_button_callbacks()
        if button_cb_code:
            tween_callbacks.append(f"// Button callbacks for {trait_name}")
            tween_callbacks.append(button_cb_code)

        # Implementation data: methods first (they may be called by events), then events
        method_impls = gen.generate_method_implementations()
        if method_impls:
            trait_implementations.append(f"// Methods for {trait_name}")
            trait_implementations.append(method_impls)
        trait_implementations.append(gen.generate_all_event_implementations())

    # Generate autoload include directives
    autoloads_used = all_features.get('autoloads', set())
    autoload_includes = []
    for autoload_cname in sorted(autoloads_used):
        autoload_includes.append(f'#include "../autoloads/{autoload_cname}.h"')

    # Combine tween callbacks with implementations (callbacks must come first)
    all_implementations = []
    if tween_callbacks:
        all_implementations.append("\n".join(tween_callbacks))
    all_implementations.append("\n".join(trait_implementations))
    if handler_definitions:
        all_implementations.append("// Signal handlers")
        all_implementations.append("\n\n".join(handler_definitions))

    template_data = {
        "trait_data_structs": "\n\n".join(trait_data_structs),
        "trait_declarations": "\n".join(trait_declarations),
        "event_handler_declarations": "\n".join(event_handler_declarations),
        "trait_implementations": "\n".join(all_implementations),
        "global_signals": signals.static_definitions() + (
            "\n\n// Signal handlers (defined after the traits)\n" + "\n".join(handler_declarations)
            if handler_declarations else ""),
        "autoload_includes": "\n".join(autoload_includes),
        "tween_include": '#include "../system/tween.h"' if all_features['has_tween'] else "",
    }

    return template_data, all_features
