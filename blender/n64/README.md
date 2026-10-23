# N64 Exporter Architecture

This document provides an architectural overview of the Armory3D N64 exporter, which transpiles Armory/Iron Haxe traits to C code targeting the Nintendo 64 via libdragon and Tiny3D.

## Pipeline Overview

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Blender Export │ ──► │  Haxe IR Macros  │ ──► │  Python Codegen │ ──► │  libdragon Build│
│  (Python)       │     │  (Compile-time)  │     │  (Templates)    │     │  (make/gcc)     │
└─────────────────┘     └──────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │                        │                       │
        ▼                       ▼                        ▼                       ▼
   Scene data            n64_traits.json            C source files           .z64 ROM
   GLTF meshes           n64_autoloads.json         Header files
   Audio assets                                     Makefile
```

## Key Design Principles

0. **Extraction runs after typing**: the build macros only capture each class's untyped fields; extraction runs in `onAfterTyping` (`N64MacroBase.deferExtraction`) so extractors can ask `TypedIndex` what an expression really is (e.g. which class an identifier refers to) instead of guessing from names. Resolve calls from their receiver's typed node: inlined calls (e.g. `Vec4.mult`) have no typed call node of their own. `getExprType()` returns the typed type (`TypedIndex.typeOf`) in the same short-name format `TypeMap` uses. Nothing is matched by variable or class names: engine-provided values (gamepad, rigid body, transform) are recognized by type in `mapping/EngineTypes.hx`, and calls whose receiver has no typed node are reported as unsupported.

1. **Clean IR Separation**: Haxe macros perform semantic analysis and emit JSON IR. Python does mechanical 1:1 translation to C without re-analyzing semantics.

2. **Converter Plugin Pattern**: Each Haxe API domain (physics, audio, input, etc.) has a dedicated converter that can be extended independently.

3. **Template-Based Codegen**: Jinja2 templates separate C code structure from generated data.

## Module Responsibilities

### Python Side (`armory/blender/arm/n64/`)

| Module | Purpose |
|--------|---------|
| `exporter.py` | Main entry point, orchestrates pipeline |
| `utils.py` | Shared utilities, configuration, coordinate conversion |
| `codegen/trait_emitter.py` | IR → C code translation |
| `codegen/trait_generator.py` | Prepares template data, handles inheritance |
| `codegen/autoload_generator.py` | Autoload-specific code generation |
| `codegen/tween_helper.py` | Tween animation support |
| `export/scene_exporter.py` | Scene data extraction |
| `export/mesh_exporter.py` | GLTF mesh export |
| `export/audio_exporter.py` | Audio asset processing |
| `export/ui_exporter.py` | Canvas/font generation |
| `export/physics_exporter.py` | Physics engine files |
| `export/build_runner.py` | Makefile generation, build execution |

### Haxe Side (`armory/Sources/armory/n64/`)

| Module | Purpose |
|--------|---------|
| `N64TraitMacro.hx` | Extracts trait code to IR at compile-time |
| `N64AutoloadMacro.hx` | Extracts autoload code to IR |
| `IRTypes.hx` | IR node type definitions and documentation |
| `TypedIndex.hx` | Maps source positions to the compiler's typed AST |
| `converters/*.hx` | Domain-specific call converters |
| `mapping/TypeMap.hx` | Haxe → C type mapping |
| `mapping/Constants.hx` | Shared constants (scale factors, limits) |

### C Templates (`armorcore/Deployment/n64/src/`)

Templates use Python `.format()` placeholders (`{variable}`):

| Directory | Contents |
|-----------|----------|
| `data/` | traits.c/h, autoloads.c/h, signals.c/h |
| `scenes/` | scene.c template |
| `ui/` | canvas.c/h, fonts.c/h |
| `autoloads/` | Per-autoload C files |

## Trait Inheritance

Traits can extend other traits to any depth. In C, each trait's data struct embeds its parent's struct first (`_parent`), so a pointer to a child's data is also a valid pointer to every ancestor's data:

- Members, signals and vtable pointers are accessed through the struct of the trait declaring them (`((OwnerData*)data)->field`), which costs nothing at runtime.
- Virtual methods (overriding or overridden) are called through a `_vfn_<method>` pointer stored in the struct of the topmost trait declaring them; each trait sets it in `on_ready` after its parent, so the most derived override wins. `super.method()` calls the parent's implementation directly.
- Every lifecycle handler calls its parent's first, so callbacks a parent registers (`notifyOnUpdate`, `notifyOnRemove`, ...) run for its children too.

The rules deciding whether a trait has a data struct, which lifecycle events the runtime calls and where vtable pointers live are shared functions at the top of `codegen/trait_generator.py` (`needs_data_struct`, `chain_events`, `vtable_owner`, ...). The struct layout, the call sites and the scene generator all use them; don't duplicate them.

Trait and autoload class names must be unique across packages: they identify classes in the IR, and a clash is reported as a compile error.

## Signals

`armory.system.Signal` fields, static (e.g. in autoloads) or per-instance (in traits), work the same way, with any argument types (numbers, bools, strings, vectors, objects) and any number of arguments:

- `SignalCallConverter.hx` turns `connect`, `disconnect` and `emit` into `signal_connect`, `signal_disconnect` and `signal_emit` nodes. A signal is identified by its declaring class and name, and `N64MacroBase.signals` collects its argument types from typed handlers (first) and emits across the whole game, written to `n64_signals.json`.
- `codegen/signal_codegen.py` renders everything: `data/signals.h` (a payload struct per signal with arguments, the static signals), emits (`{ <signal>_payload_t _p = {...}; signal_emit(&sig, &_p); }`), connections, and one handler function per handler. Method handlers call the method (through its vtable pointer if it's virtual) and anonymous handlers contain their body; each unpacks only the parameters it declares.
- Handlers of a trait are connected with its data as context. Connecting and disconnecting is up to the game code: a trait must disconnect its handlers before it's removed (e.g. in `notifyOnRemove`), since its data is freed with its scene.
- `system/signal.c`: disconnect matches handler and context; handlers can connect and disconnect (also themselves) during an emit.

Every signal has `ARM_SIGNAL_MAX_SUBS` subscriber slots (16 by default).

## IR Node Types

The IR (Intermediate Representation) is a JSON format that bridges Haxe semantics to C code. See `IRTypes.hx` for complete documentation.

**Key Categories:**
- **Literals**: `int`, `float`, `string`, `bool`, `null`, `c_literal`
- **Variables**: `member`, `ident`, `field_access`, `autoload_field`
- **Operators**: `assign`, `binop`, `unop`
- **Control Flow**: `if`, `while`, `for_range`, `return`, `break`, `continue`
- **Calls**: `call`, `method_call`, `transform_call`, `physics_call`, `audio_*`, etc.

## Adding Support for New Haxe APIs

### Step 1: Create a Converter (Haxe)

Create `Sources/armory/n64/converters/MyCallConverter.hx`. Decide whether a call is yours from the typed AST, not from variable or class names, so aliases, full paths and differently named variables all work:

- `TypedIndex.receiverPath(obj)`: what the receiver is, e.g. `"iron.system.Gamepad"` for `pad` in `pad.started("a")`
- `TypedIndex.isClassRef(obj, "iron.Scene")`: static calls (`Scene.x()`, `iron.Scene.x()`, aliases)
- `TypedIndex.callOwner(obj, method)`: the class declaring the method

They return null/false for expressions without a typed node; don't add name-based fallbacks, `N64MacroBase.methodCall()` reports such calls.

```haxe
class MyCallConverter implements ICallConverter {
    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>,
                               rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        // Return null if not handling this call
        if (TypedIndex.receiverPath(obj) != "my.api.MyClass") return null;

        // Return IR node with c_code template
        return {
            type: "my_call",
            c_code: "my_c_function({0}, {1})",
            args: args
        };
    }
}
```

### Step 2: Register the Converter (Haxe)

In `N64TraitMacro.hx`, add to the converter list in `TraitExtractor`:

```haxe
callConverters = [
    // ... existing converters ...
    new MyCallConverter()
];
```

Calls no converter handles become a generic `method_call`. When the typed AST knows the method's class, `N64MacroBase.methodCall()` reports it as unsupported unless the Python emitter translates it (`emitterMethods`); add an entry there if you handle a new method in `emit_method_call`.

### Step 3: Add Emitter (Python)

In `trait_emitter.py`, add an `emit_my_call` method:

```python
def emit_my_call(self, node: Dict) -> str:
    """Handle my_call IR nodes."""
    c_code = node.get("c_code", "")
    return self._substitute_placeholders(c_code, node.get("args", []))
```

## Configuration

### Python Configuration (`utils.py`)

```python
N64_CONFIG = {
    'max_physics_bodies': 32,      # See also: Constants.hx
    'max_button_subscribers': 16,
    'max_contact_subscribers': 4,
    'max_contact_bodies': 16,
}
```

### Haxe Metadata

- `@:n64Autoload(order=N)` - Set autoload initialization order

## Coordinate System

Armory uses Blender's Z-up coordinate system. The exporter converts to N64's Y-up system:

- Blender X → N64 X
- Blender Y → N64 -Z
- Blender Z → N64 Y

Position scale factor: `1/64` (64 Blender units = 1 N64 unit)

## Debugging

### IR Inspection

After export, check `build_<project>/n64_traits.json` and `n64_autoloads.json` to see the generated IR.

### Generated Code

C code is generated in `armorcore/Deployment/n64/src/data/traits.c` and related files.

### Unsupported Code

Haxe the macros can't translate is reported as a compile warning at its source line (`N64: ...`) and kept as an `/* N64 unsupported: ... */` comment in the generated C. Converters report it with `N64MacroBase.unsupported(msg)`; return a plain `{ type: "skip" }` only for code that is intentionally a no-op on N64. `-D n64_strict` turns the warnings into errors.

### Tests

See `Tests/README.md`: golden tests that run the macros and the Python codegen without Blender.

### Build Errors

Build output appears in Blender's system console. Common issues:
- Missing includes: Check template files
- Type errors: Check TypeMap.hx mappings
- Link errors: Check that C functions exist in runtime

## Version Compatibility

The IR format includes a version number. If the Haxe macro IR version doesn't match what Python expects, export will warn. Update both sides when changing IR structure.

Current IR version: Check `ir_version` in `n64_traits.json` header.
