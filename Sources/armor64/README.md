# The Haxe macros (`Sources/armor64/`)

They read the game's Haxe code during the Haxe build and write it as IR (JSON) for the [Python exporter](../../exporter/README.md), which turns it into C. All the analysis happens here, where the compiler's types are known; Python only translates.

## How the extraction works

1. `N64Macro.register("<package>")` (added by `armory_hooks.py`, only with `arm_target_n64`) puts a build macro on every class of the project's package. It hands traits (classes extending `iron.Trait`) to `N64TraitMacro` and `@:n64Autoload` classes to `N64AutoloadMacro`; it changes no code.
2. Nothing is extracted while building: after typing (`Context.onAfterTyping`, in `N64MacroBase`), `TypedIndex` maps every source position of the game's code to the compiler's typed nodes.
3. `N64Extractor`, shared by traits and autoloads, walks each function's syntax and asks `TypedIndex` what each expression is: its type, the class declaring a field or method, whether a field is a property and which accessor the compiler calls, which locals a closure captures. So aliases, imports, `using`, inheritance and inferred types work, and nothing is guessed from names.
4. The IR is written to `build_<project>/`: `n64_traits.json` (`N64TraitMacro`), `n64_autoloads.json` (`N64AutoloadMacro`), `n64_signals.json`, `n64_audio.json` and `n64_limits.json` (`N64MacroBase`: facts for the engine limits). Each has an `ir_version`; the exporter stops on another version.

## Modules

| Module | What it does |
|---|---|
| `N64Macro.hx` | Entry point (above) |
| `N64MacroBase.hx` | What traits and autoloads share outside the extractor: scheduling after typing, reporting (`unsupported`, `differs`), signals, audio, limits facts, the JSON files |
| `N64Extractor.hx` | Haxe → IR for both: statements, expressions, members, transforms, properties, calls (to the converters, then the bindings table) |
| `N64TraitMacro.hx` | Traits: members, methods, inheritance, lifecycle sites (`notifyOnX`/`removeX`), statics |
| `N64AutoloadMacro.hx` | Autoloads: statics, functions, initialization order (`@:n64Autoload(order=N)`) |
| `TypedIndex.hx` | Source positions → typed nodes; queries (`receiverPath`, `callOwner`, `isClassRef`, `accessorAt`, `capturedLocals`, `isInstanceOf`...) |
| `StringFormatter.hx` | Strings as Haxe builds them: `+`, interpolation, `Std.string`, `trace`; each part written by its type (`str_format` nodes) |
| `Bindings.hx` | The table of calls that map straight to C (below) |
| `IRTypes.hx` | The IR's node types and fields |
| `converters/` | Calls that need logic (below): arrays, maps, audio, autoloads, buttons, canvases, input, objects, physics, scenes, signals, `Std.string`, tweens, vectors |
| `mapping/` | Haxe → C types (`TypeMap`), engine types (`EngineTypes`), gamepad buttons (`ButtonMap`), shared constants |
| `util/ExprUtils.hx` | Small syntax helpers |

## Keeping Haxe's meaning

The generated code must do what the Haxe does on Krom. Where the N64 can't (on purpose: speed, fixed memory), the difference is reported, never silent. What game makers see of this is in [GUIDE.md](../../GUIDE.md).

- **Lifecycles**: each `notifyOnX(f)` call site gets an id, reserved before its body is converted (nested sites get their own); the IR lists the sites, and `removeX(f)` points at the sites registering `f`. Static initializers that register are found too.
- **Properties**: a field access becomes an accessor call where the compiler calls one (`TypedIndex.accessorAt`; compound assignments and `++` through `compoundReadAccessor`), only for the game's own classes. Fields without storage aren't in the data struct.
- **Strings**: `StringFormatter` writes each part by its compiler type (Ints, single-precision Floats printed as Haxe prints them, Bools, nulls).
- **Captures**: `TypedIndex.capturedLocals` gives a callback's captured locals and parameters (also loop variables of unrolled loops: `N64MacroBase.loops`). They're copied where the callback is registered; the cases where a copy differs from Haxe's shared variable are reported (`captureDifferences`).
- **Transforms**: game code reads and writes `loc`, `rot`, `scale` in Blender's axes and units; the extractor turns each access into the runtime's Blender-space accessors (`it_get_loc`, `it_set_loc_x`...) and vector calls on them into reads and writes.
- **Other trait instances**: a member typed as a trait is a reference to that instance's data; calls on it (`trait_method_call` with its `object`) and its fields go through the declaring class (`fieldRef`; `memberOwner` when a compiler temporary hides the typed node).
- **Statics**: a trait's statics are globals; autoload members are globals of their autoload.

## Unsupported code

Anything the extractor can't translate goes through `N64MacroBase.unsupported(msg, pos)`: a warning at its Haxe line (`N64: ...`), an error with `-D n64_strict`, and a `skip` IR node, written as an `/* N64 unsupported: ... */` comment in the C. `N64MacroBase.differs(msg, pos)` reports code that runs, but not exactly as on Krom: a warning once per line, a warning even with `n64_strict`. The list of what's unsupported is in [GUIDE.md](../../GUIDE.md).

## Adding a Haxe API

A call either maps straight to C, and is **one line in `Bindings.hx`**, or needs logic, and is a **converter**. Use the table unless the call needs one of:

- a decision from the arguments' values or types (`Std.string` writes its argument by its type, `Scene.setActive("Level")` resolves the scene ID at export time)
- something recorded for the exporter (input buttons used, map key lengths, contact handlers)
- generated code (callbacks, captures, signal handlers)

Calls nothing translates are reported at their Haxe line (`N64MacroBase.unsupportedMethodCall`), never copied to the C.

## The bindings table (`Bindings.hx`)

An entry is keyed by the class (or abstract) **declaring** the method, from the typed AST. So aliases, imports, full paths, inherited methods and `using` extensions all match, whatever the code calls the receiver:

```haxe
"Math.sin" => {c: "fm_sinf({0})"},
"iron.object.Animation.play" => {c: 'animation_play({self}, {0=""})', receiver: Value},
"iron.object.Transform.move" => {c: "it_move(&((ArmObject*){self})->transform, ({0}).x, ({0}).z, -({0}).y, {1=1.0f});",
                                 receiver: FieldOwner("transform"), flags: ["uses_transform"]},
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

## Converters (`converters/`)

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

## Tests

Add the call to a golden suite (`Tests/suites/*/Sources`) and accept the output with `python Tests/run_tests.py --update` once the generated C is right. The suites' compile stage builds the C with `-Werror`. `typed_resolution/.../ApiSurface.hx` calls every binding in the forms games write.
