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

## Armory Integration

armor_64 plugs into Armory through Armory's library hooks (`arm/library_hooks.py` in Armory), without patching Armory's code. Its `armory_hooks.py` adds:

| Hook | What it does |
|---|---|
| `runtimes()`, `targets()` | "Ares" in the Runtime list (Play) and "Nintendo 64" in the exporter's Target list (Build, Publish); Armory exports these as Kha's `custom` target |
| `owns_build(request)` | Those builds are armor_64's |
| `compile(request, done)` | armor_64 builds them (`export/build_pipeline.py`) instead of Armory's khamake compile |
| `clean(request)` | Publish cleans only the N64 output (khamake's cache stays warm) |
| `write_main()` | For those builds: the `arm_target_n64` and `arm_build_dir` defines and the `N64Macro` Haxe macro |

`blender.py` only registers the `arm.n64` Python namespace, the N64 preferences and the libdragon/tiny3d operators.

### Build pipeline (`export/build_pipeline.py`)

After Armory's export (scene data, `khafile.js`, `Main.hx`), Haxe runs only to execute armor_64's macros, which write the IR: its output isn't used. So no shader is compiled and no code is generated:

1. khamake `--nohaxe --noshaders --noproject`: only the Haxe build file and the asset list (a fraction of a second when warm)
2. The asset list for Haxe (`build/n64haxe-resources/files.json`): khamake's assets, plus every shader as an empty placeholder (Kha builds `kha.Shaders` from it). Shader names come from the project's and libraries' `khafile.js` `addShaders()` and from `Sources/Shaders`. Koui's themes (`.ksn`) are copied next to it (`build/n64haxe/`), where Koui's theme macro reads them.
3. Haxe directly on khamake's build file, retargeted to `n64haxe` with `--no-output`
4. The N64 export, make, and Ares for Play

## Key Design Principles

0. **Extraction runs after typing**: the build macros only capture each class's untyped fields; extraction runs in `onAfterTyping` (`N64MacroBase.deferExtraction`) so extractors can ask `TypedIndex` what an expression really is (e.g. which class an identifier refers to) instead of guessing from names. Resolve calls from their receiver's typed node: inlined calls (e.g. `Vec4.mult`) have no typed call node of their own. `getExprType()` returns the typed type (`TypedIndex.typeOf`) in the same short-name format `TypeMap` uses. Nothing is matched by variable or class names: engine-provided values (gamepad, rigid body, transform) are recognized by type in `mapping/EngineTypes.hx`, and calls whose receiver has no typed node are reported as unsupported.

1. **Clean IR Separation**: Haxe macros perform semantic analysis and emit JSON IR. Python does mechanical 1:1 translation to C without re-analyzing semantics.

2. **Bindings table, then converters**: a Haxe call that maps straight to C is one entry in `Bindings.hx` (`"Class.method" => C template`). Calls needing logic (callbacks, export-time lookups, generated code) have a converter per API domain (`converters/`). See [Adding Support for New Haxe APIs](#adding-support-for-new-haxe-apis).

3. **Template-Based Codegen**: Jinja2 templates separate C code structure from generated data.

## Module Responsibilities

### Python Side (`exporter/`, loaded by `blender.py` as `arm.n64`)

| Module | Purpose |
|--------|---------|
| `exporter.py` | Main entry point, orchestrates pipeline |
| `utils.py` | Shared utilities, configuration, coordinate conversion |
| `codegen/trait_emitter.py` | IR → C code translation (`emit_c_template` renders the bindings) |
| `codegen/trait_generator.py` | Prepares template data, handles inheritance |
| `codegen/autoload_generator.py` | Autoload-specific code generation |
| `codegen/tween_helper.py` | Tween animation support |
| `export/scene_exporter.py` | Scene data extraction |
| `export/mesh_exporter.py` | GLTF mesh export |
| `export/audio_exporter.py` | Audio asset processing |
| `export/ui_exporter.py` | Canvas/font generation |
| `export/physics_exporter.py` | Physics engine files |
| `export/build_runner.py` | Makefile generation, build execution |

### Haxe Side (`Sources/armor64/`)

| Module | Purpose |
|--------|---------|
| `N64Macro.hx` | Entry point (`register()`): one build macro on the `arm` package, hands classes to the two below |
| `N64Extractor.hx` | Haxe → IR conversion shared by traits and autoloads; subclasses override a few hooks |
| `N64TraitMacro.hx` | Traits: lifecycles and events, inheritance, data struct → `n64_traits.json` |
| `N64AutoloadMacro.hx` | Autoloads: static members and functions as C globals → `n64_autoloads.json` |
| `IRTypes.hx` | IR node type definitions and documentation |
| `TypedIndex.hx` | Maps source positions to the compiler's typed AST |
| `Bindings.hx` | Haxe calls that map straight to C: one template per `Class.method` |
| `converters/*.hx` | Calls needing logic, one converter per API domain |
| `mapping/TypeMap.hx` | Haxe → C type mapping |
| `mapping/Constants.hx` | Shared constants (scale factors, limits) |

### C Runtime and Templates (`runtime/src/`)

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
- **Calls**: `c_template` (a binding), `call`, `trait_method_call`, `autoload_call`, `signal_*`, `audio_*`, `tween_*`, etc.
- **Containers**: `map_*`, `array_*`, `container_element`

## Adding Support for New Haxe APIs

A call either maps straight to C, and is **one line in `Bindings.hx`**, or needs logic, and is a **converter**. Use the table unless the call needs one of:

- a decision from the arguments' values or types (`Std.string` writes its argument by its type, `Scene.setActive("Level")` resolves the scene ID at export time)
- something recorded for the exporter (input buttons used, map key lengths, contact handlers)
- generated code (callbacks, captures, signal handlers)

Calls nothing translates are reported at their Haxe line (`N64MacroBase.unsupportedMethodCall`), never copied to the C.

### The bindings table (`Sources/armor64/Bindings.hx`)

An entry is keyed by the class (or abstract) **declaring** the method, from the typed AST. So aliases, imports, full paths, inherited methods and `using` extensions all match, whatever the code calls the receiver:

```haxe
"Math.sin" => {c: "fm_sinf({0})"},
"iron.object.Animation.play" => {c: 'animation_play({self}, {0=""})', receiver: Value},
"iron.object.Transform.move" => {c: "it_move(&((ArmObject*){self})->transform, ({0}).x, ({0}).z, -({0}).y, {1=1.0f});",
                                 receiver: FieldOwner("transform"), flags: ["uses_transform", "mutates_transform"]},
"kha.graphics2.Graphics.begin" => {noop: true},
```

Then add the C function to the runtime, if it's new. Nothing else is needed: no IR node type, no Python code.

**Placeholders in `c`:**

| Placeholder | Is |
|---|---|
| `{0}`, `{1}`, ... | The call's arguments |
| `{1=1.0f}` | Argument 1, or `1.0f` when the call leaves it out |
| `{self}` | The receiver, see `receiver` below |

Any other `{name}` is an error. A C brace around a bare name needs a space: `{ x }`. Braces next to other text, such as `(ArmVec4){{self}.x, ...}`, are fine.

**`receiver`, what `{self}` is:**

| Receiver | `{self}` | Example |
|---|---|---|
| `None` (default) | Nothing: a static call | `Math.sin(x)`, `StringTools.endsWith(s, x)` (or `s.endsWith(x)`) |
| `Value` | The receiver | `v.length()`, `anim.play("run")`, `rb.applyForce(f)` (a RigidBody is its object's pointer) |
| `InPlace` | The receiver, which is written to. It's used as written and never copied. | `v.normalize()` |
| `FieldOwner("transform")` | The object owning the field: `x.transform.m()` gives `x`, `transform.m()` gives the trait's object (traits only) | `object.transform.translate(...)` |

**Other fields:**
- `flags`: the `TraitMeta` flags the call sets, such as `uses_physics`.
- `noop: true`: the call does nothing on N64, and its arguments aren't evaluated.

**Rules, checked when a call uses the entry** (a broken entry fails at the Haxe line, not in C):
- Every argument the call passes must appear in the template. An argument the C can't take, such as `applyForce`'s `loc`, is reported instead of dropped.
- A template ending with `;` or starting with `{` (a block) is a statement. Otherwise it's an expression; it may still end with `}`, as a compound literal does. A value computed with statements is a statement expression: `({ T3DVec3 _d; it_look(..., &_d); (ArmVec4){...}; })`.
- An argument or receiver used more than once, and that isn't plain C (a name, member path or literal), is evaluated once into a temporary (`__auto_type _t0 = ...`). A receiver used once that isn't plain C is parenthesized. So `({0}).x, ({0}).z, -({0}).y` is safe with any argument.
- No logic in the table. An entry that would need a condition or a lookup is a converter.

### Converters (`Sources/armor64/converters/`)

Converters run **before** the table, so they also handle special cases of methods the table maps (e.g. `Vec4.set()` on a transform's `loc`, or a mix channel's `setVolume()`). Return `null` for anything you don't handle, so the table and the other converters get it.

Decide whether a call is yours from the typed AST, not from variable or class names:

- `TypedIndex.receiverPath(obj)`: what the receiver is, e.g. `"iron.system.Gamepad"` for `pad` in `pad.started("a")`
- `TypedIndex.isClassRef(obj, "iron.Scene")`: static calls (`Scene.x()`, `iron.Scene.x()`, aliases)
- `TypedIndex.callOwner(obj, method)`: the class declaring the method

They return null/false for expressions without a typed node; don't add name-based fallbacks.

```haxe
class MyCallConverter implements ICallConverter {
    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>,
                               rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        if (TypedIndex.receiverPath(obj) != "my.api.MyClass") return null;
        if (method != "load") return null;

        // The logic: here, an asset resolved at export time
        var asset = ctx.extractStringArg(rawParams[0]);
        if (asset == null) return N64MacroBase.unsupported("MyClass.load() needs a constant name on N64");
        return {type: "c_template", c_code: 'my_load(ASSET_${asset.toUpperCase()})', args: []};
    }
}
```

A converter can return a `c_template` node, rendered like a table entry (placeholders, temporaries) with no Python needed; the entry checks above are the converter's job. Use its own IR node type and an `emit_<type>` method in `trait_emitter.py` only when the C needs data the template can't carry, such as callbacks or handler names.

Register it in the converter list passed to `super()` in `N64TraitMacro.hx`. Also add it in `N64AutoloadMacro.hx` if its C doesn't use the trait's `obj` or `data`.

### Tests

Add the call to a golden suite (`Tests/suites/*/Sources`) and accept the output with `python Tests/run_tests.py --update` once the generated C is right. The suites' compile stage builds the C with `-Werror`. `typed_resolution/.../ApiSurface.hx` calls every binding in the forms games write.

## Configuration

### Engine limits (`codegen/limits.py`)

The runtime allocates nothing during gameplay: pools and containers are static
arrays, so each one has a size decided at export. `limits.py` decides all of
them, and the exporter writes them into the C as plain numbers: `limits.h` (from
`limits.h.j2`) for the engine, the UI headers for the UI pools.

A full pool or container reports it once with `debugf` (`ARM_LIMIT_REACHED` in
`limits.h`) and drops the request.

`DEFAULTS` holds each limit's value when nothing more precise is known.
`compute()` replaces defaults with values worked out from the project:

- scene counts (bodies, colliders, contact and button handlers, objects, animation clips): exact, for the largest scene, plus the copies spawnable templates may add (`SPAWN_MAX_INSTANCES` each)
- code facts the macros record, times the instances in the scene: tweens (member tweens of each trait instance, plus autoload tweens, which survive scene changes), signal subscribers (one per `connect()` site and instance of its class or subclasses; one for an autoload), map keys (literal ones: length and distinct keys)
- UI pools: exact, for the largest canvas; label text also fits the text the code sets at runtime
- derived bounds: physics pairs and contacts (the body pairs that can overlap / collide; a budget: used only when below the default, since the worst case is far above what overlaps at once), string concatenations and label text (the longest value the code can build)

Values that can't be counted keep the default as a floor: a `new Tween()` inside a function, a map key built at runtime, a string argument that isn't a literal. Concatenated strings and label/button text report truncation with `ARM_LIMIT_REACHED` too.

Each export writes `build/n64/limits.txt`: every limit with its default and how its value was decided. After a successful build, the RAM is appended from the built ELF (code, data, bss; the pools with the limits sizing them; the largest other symbols) and the whole report is printed to the console.

| Limit | Default | What it limits | Where it's defined |
|---|---|---|---|
| `MAX_TWEENS` | 16 | Tweens allocated at once (tween pool) | `limits.h` |
| `ARM_SIGNAL_MAX_SUBS` | 16 | Subscribers of one signal | `limits.h` |
| `ANIM_MAX_CLIPS` | 8 | Animation clips of one model | `limits.h` |
| `MAX_PENDING_REMOVALS` | 32 | Objects removed during one frame | `limits.h` |
| `SPAWN_MAX_INSTANCES` | 4 | Copies alive at once of one spawnable template (Python only; spawning isn't supported yet) | Python only |
| `MAX_PHYSICS_BODIES` | 128 | Rigid bodies (physics body pool) | `limits.h` |
| `MAX_MESH_COLLIDERS` | 8 | Mesh colliders | `limits.h` |
| `MAX_CONTACT_BODIES` | 16 | Bodies with contact handlers | `limits.h` |
| `MAX_CONTACT_SUBSCRIBERS` | 4 | Contact handlers of one body | `limits.h` |
| `OIMO_MAX_PROXIES` | 128 | Broadphase proxies (one per shape) | `limits.h` |
| `OIMO_MAX_PROXY_PAIRS` | 256 | Broadphase overlapping pairs (budget: the bound is used when lower) | `limits.h` |
| `OIMO_MAX_CONTACTS` | 64 | Contacts between shapes (2.6 KB each; without scene data) | `limits.h` |
| `OIMO_CONTACTS_PER_DYNAMIC_BODY` | 16 | Contact budget per dynamic body (Python only: OIMO_MAX_CONTACTS is at most the bound) | Python only |
| `OIMO_MAX_ISLAND_BODIES` | 32 | Bodies in one simulation island | `limits.h` |
| `OIMO_MAX_ISLAND_SOLVERS` | 64 | Contact solvers in one simulation island (at most the contacts) | `limits.h` |
| `OIMO_MAX_RIGID_BODIES` | 32 | Bodies visited while building one island | `limits.h` |
| `OIMO_MESH_MAX_CONTACTS` | 4 | Contacts kept per mesh collision (the shallowest is replaced) | `limits.h` |
| `OIMO_BVH_MAX_TRIANGLES` | 4 | Triangles of the largest mesh collider (its BVH) | `limits.h` |
| `OIMO_BVH_MAX_DEPTH` | 4 | Depth of the largest mesh collider's BVH | `limits.h` |
| `ARM_INT_ARRAY_CAPACITY` | 32 | Items of an Array<Int> | `limits.h` |
| `ARM_FLOAT_ARRAY_CAPACITY` | 32 | Items of an Array<Float> | `limits.h` |
| `ARM_BOOL_ARRAY_CAPACITY` | 32 | Items of an Array<Bool> | `limits.h` |
| `ARM_STRING_ARRAY_CAPACITY` | 16 | Items of an Array<String> | `limits.h` |
| `ARM_MAP_CAPACITY` | 16 | Keys of a Map<String, *> | `limits.h` |
| `ARM_MAP_MAX_KEY_LEN` | 32 | Characters of a Map key, including the terminator | `limits.h` |
| `ARM_SOUND_HANDLE_ARRAY_CAPACITY` | 8 | Items of an Array<BaseChannelHandle> | `limits.h` |
| `ARM_SOUND_CHANNEL_MAP_CAPACITY` | 16 | Keys of a Map<String, Array<BaseChannelHandle>> | `limits.h` |
| `STR_FMT_BUFFER_SIZE` | 64 | Characters of a string built by concatenation | `limits.h` |
| `STR_FMT_BUFFER_COUNT` | 4 | Concatenated strings alive at once (rotating buffers) | `limits.h` |
| `UI_MAX_LABELS` | 1 | Labels | UI headers |
| `UI_LABEL_TEXT_SIZE` | 32 | Characters of a label | UI headers |
| `UI_MAX_IMAGES` | 1 | Images | UI headers |
| `UI_MAX_BUTTONS` | 1 | Buttons | UI headers |
| `UI_BUTTON_TEXT_SIZE` | 32 | Characters of a button | UI headers |
| `UI_MAX_PANELS` | 1 | Panels | UI headers |
| `UI_MAX_GROUPS` | 1 | Layout groups | UI headers |
| `UI_MAX_GROUP_CHILDREN` | 8 | Children of one layout group | UI headers |

### Haxe Metadata

- `@:n64Autoload(order=N)` - Set autoload initialization order (the order of their static initializers at boot; their `init()` is called by the game, as in Armory)

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

C code is generated in `build/n64/src/data/traits.c` and related files.

### Unsupported Code

Haxe the macros can't translate is reported as a compile warning at its source line (`N64: ...`) and kept as an `/* N64 unsupported: ... */` comment in the generated C. Converters report it with `N64MacroBase.unsupported(msg)`; return a plain `{ type: "skip" }` (or use a `noop` binding) only for code that is intentionally a no-op on N64. `-D n64_strict` turns the warnings into errors.

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
