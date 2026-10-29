"""
Signal code generation.

Renders the signal IR from the macros (SignalCallConverter.hx) and the signal
definitions in n64_signals.json, for traits and autoloads alike:

- A signal is identified by "<declaring class>.<name>". A static signal is one
  global ArmSignal (its `c_symbol`), a per-instance one an ArmSignal field in
  its declaring trait's data struct.
- A signal with arguments passes a pointer to its payload struct
  (`<signal>_payload_t`, fields arg0, arg1...), built on the stack by each emit:
  handlers run during signal_emit(), so it stays valid. Any argument type works,
  and emitted values are converted to the signal's argument types.
- Every handler is a `void handler(void* ctx, void* payload)` function: method
  handlers are thin adapters calling the method (through its vtable pointer if
  it's virtual), anonymous handlers contain their body. A handler unpacks only
  the parameters it declares.
"""

from typing import Dict, List, Optional

from armor64.codegen.ir import nodes_of_type


class SignalContext:
    """Signal definitions plus the class info needed to name things in C."""

    def __init__(self, defs: Dict, traits: Dict, autoloads: Dict):
        self.defs = defs or {}
        self.traits = traits or {}
        # Autoloads by full class path (module) and by class name
        self.autoload_c_names = {}
        for name, ir in (autoloads or {}).items():
            c_name = ir["c_name"]
            self.autoload_c_names[ir.get("module", name)] = c_name
            self.autoload_c_names[name] = c_name

    # ------------------------------------------------------------------ names

    def class_c_name(self, path: str) -> str:
        """C prefix of a trait or autoload class."""
        if path in self.traits:
            return self.traits[path]["c_name"]
        if path in self.autoload_c_names:
            return self.autoload_c_names[path]
        return self.autoload_c_names.get(path.rsplit('.', 1)[-1], path.replace('.', '_').lower())

    def signal_base(self, key: str) -> str:
        """C name of a signal: its global symbol, or <owner>_<name> for
        per-instance signals (used to name its payload and handlers)."""
        d = self.defs[key]
        if d.get("is_static"):
            return d["c_symbol"]
        return f"{self.class_c_name(d['owner'])}_{d['name']}"

    def payload_type(self, key: str) -> Optional[str]:
        return f"{self.signal_base(key)}_payload_t" if self.defs[key].get("arg_types") else None

    def handler_name(self, handler: Dict, key: str, connecting_c_name: str) -> str:
        if handler.get("kind") == "anonymous":
            return f"{connecting_c_name}_{handler['id']}"
        return f"{self.class_c_name(handler['owner'])}_{handler['name']}__{self.signal_base(key)}"

    # ------------------------------------------------------------------ files

    def header(self) -> str:
        """data/signals.h: payload structs and the static signals."""
        lines = [
            "// Auto-generated signal declarations (signal_codegen.py)",
            "#pragma once",
            "",
            '#include "../types.h"',
            "",
        ]
        for key in sorted(self.defs):
            payload = self.payload_type(key)
            if not payload:
                continue
            fields = [f"    {_payload_ctype(t)} arg{i};" for i, t in enumerate(self.defs[key]["arg_types"])]
            lines.append(f"// Arguments of {key}")
            lines.append("typedef struct {")
            lines.extend(fields)
            lines.append(f"}} {payload};")
            lines.append("")
        statics = [k for k in sorted(self.defs) if self.defs[k].get("is_static")]
        for key in statics:
            lines.append(f"extern ArmSignal {self.defs[key]['c_symbol']};")
        return "\n".join(lines) + "\n"

    def static_definitions(self) -> str:
        """Static signals, for traits.c."""
        statics = [k for k in sorted(self.defs) if self.defs[k].get("is_static")]
        return "\n".join(f"ArmSignal {self.defs[k]['c_symbol']} = {{0}};" for k in statics)

    # ------------------------------------------------------------------ calls

    def signal_ptr(self, key: str, instance_code: Optional[str]) -> str:
        d = self.defs[key]
        if d.get("is_static"):
            return f"&{d['c_symbol']}"
        owner_c = self.class_c_name(d["owner"])
        return f"&(({owner_c}Data*)({instance_code or 'data'}))->{d['name']}"

    def emit_code(self, key: str, ptr: str, args: List[str]) -> str:
        payload = self.payload_type(key)
        if not payload:
            return f"signal_emit({ptr}, NULL)"
        values = ", ".join(args) if args else "0"
        return f"{{ {payload} _p = {{{values}}}; signal_emit({ptr}, &_p); }}"

    # ------------------------------------------------------------------ handlers

    def handler_function(self, handler: Dict, key: str, connecting_c_name: str,
                         connecting_trait: Optional[str], emitter) -> str:
        """C function for a handler. `connecting_trait` is the trait connecting
        it (None in an autoload); `emitter` renders anonymous bodies."""
        name = self.handler_name(handler, key, connecting_c_name)
        kind = handler.get("kind")
        arg_types = self.defs[key].get("arg_types", [])
        params = handler.get("params", [])
        lines = [f"static void {name}(void* ctx, void* payload) {{"]

        # Context: the trait's data (and object) for methods and trait closures
        if kind == "method":
            lines.append("    void* data = ctx;")
            lines.append(f"    void* obj = (({self._root_c_name(handler['owner'])}Data*)data)->object;")
        elif kind == "anonymous" and connecting_trait:
            lines.append(f"    {connecting_c_name}Data* data = ({connecting_c_name}Data*)ctx;")
            lines.append(f"    void* obj = (({self._root_c_name(connecting_trait)}Data*)data)->object;")
            lines.append("    (void)data; (void)obj;")
        else:
            lines.append("    (void)ctx;")

        # Parameters the handler declares, from the payload
        if params and arg_types:
            payload = self.payload_type(key)
            lines.append(f"    {payload}* p = ({payload}*)payload;")
        else:
            lines.append("    (void)payload;")
        names = []
        for i, param in enumerate(params):
            ctype = param.get("ctype") or (arg_types[i] if i < len(arg_types) else "int32_t")
            value = f"p->arg{i}" if i < len(arg_types) else f"({ctype}){{0}}"
            lines.append(f"    {ctype} {param['name']} = {value};")
            names.append(param["name"])

        # Call or body
        if kind == "anonymous":
            body = emitter.emit_statements(handler.get("body", []), "    ")
            if body:
                lines.append(body)
        else:
            lines.append(f"    {self._method_call(handler, names)};")
        lines.append("}")
        return "\n".join(lines)

    def handler_declaration(self, handler: Dict, key: str, connecting_c_name: str) -> str:
        return f"static void {self.handler_name(handler, key, connecting_c_name)}(void* ctx, void* payload);"

    def _method_call(self, handler: Dict, args: List[str]) -> str:
        owner, name = handler["owner"], handler["name"]
        if handler.get("kind") == "static":
            return f"{self.class_c_name(owner)}_{name}({', '.join(args)})"
        from armor64.codegen.trait_generator import visible_vtable_owners
        call_args = ", ".join(["obj", "data"] + args)
        vtable_owner = visible_vtable_owners(self.traits, owner).get(name)
        if vtable_owner:
            return f"(({self.class_c_name(vtable_owner)}Data*)data)->_vfn_{name}({call_args})"
        c_name = self.traits.get(owner, {}).get("methods", {}).get(name, {}).get("cName")
        if not c_name:
            raise ValueError(f'Signal handler {owner}.{name}: the method has no N64 code (not in the Haxe build\'s IR)')
        return f"{c_name}({call_args})"

    def _root_c_name(self, trait: str) -> str:
        """The topmost ancestor's struct holds the object pointer."""
        root = trait
        while self.traits.get(root, {}).get("parent") in self.traits:
            root = self.traits[root]["parent"]
        return self.class_c_name(root)


def _payload_ctype(ctype: str) -> str:
    # Trait data pointers: signals.h is included before the trait structs
    return "void*" if ctype.endswith("Data*") else ctype


def find_signal_bindings(ir) -> List[Dict]:
    """All signal_connect/signal_disconnect nodes in an IR tree (any depth,
    including callbacks and handler bodies)."""
    return nodes_of_type(ir, "signal_connect", "signal_disconnect")
