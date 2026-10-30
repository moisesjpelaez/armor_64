# The Python exporter (`exporter/`)

The Python side of armor_64: it runs the N64 build from Blender, exports the scenes, meshes, UI and audio, turns the macros' IR into C, decides the engine's limits and builds the ROM. `blender.py` loads it as the `armor64` package (`from armor64.export import scene_exporter`).

The other parts: [Sources/armor64/](../Sources/armor64/README.md) (the Haxe macros that write the IR), [runtime/](../runtime/README.md) (the C runtime and templates), [Tests/](../Tests/README.md).

```
Blender + Armory ─► khamake (asset list) ─► Haxe + armor_64's macros ─► IR (build_<project>/n64_*.json)
        │                                                                  │
        └─ meshes, while Haxe compiles ─► export_code(): scenes, UI, audio, C from the IR, limits
                                                  │
                                                  └─► make (build_<project>/n64/) ─► ROM ─► Ares (Play)
```

## Armory integration (`../armory_hooks.py`)

armor_64 plugs into Armory through its library hooks (`arm/library_hooks.py` in Armory), without patching Armory:

| Hook | What it does |
|---|---|
| `runtimes()`, `targets()` | "Ares" in the Runtime list (Play), "Nintendo 64" in the exporter's Target list (Build, Publish); Armory exports them as Kha's `custom` target |
| `owns_build(request)` | Those builds are armor_64's |
| `compile(request, done)` | armor_64 builds them (`export/build_pipeline.py`) instead of Armory's khamake compile |
| `clean(request)`, `clean_project()` | Publish cleans only the N64 output (khamake's cache stays warm); Clean Project stops armor_64's compilation server |
| `write_main()` | The `arm_target_n64` and `arm_build_dir` defines, and the macro's registration |
| `preferences()`, `draw_preferences()` | The Nintendo 64 settings in Armory's preferences |

armor_64 uses Armory's `run_process` (the build's processes: Blender stays responsive, Stop ends them), `run_player` (Ares as Armory's player) and `stop_process`. `../blender.py` registers the package and the libdragon/tiny3d operators, which run in MSYS2 through the same `run_process`.

## The build (`export/build_pipeline.py`)

After Armory's export (scene data, `khafile.js`, `Main.hx`), Haxe only has to run the macros: its output isn't used, so no shader is compiled and no code is generated.

1. khamake `--nohaxe --noshaders --noproject`: only the Haxe build file and the asset list.
2. The asset list for Haxe (`build/n64haxe-resources/files.json`): khamake's assets, plus every shader as an empty placeholder (Kha builds `kha.Shaders` from it); Koui's `.ksn` themes copied where Koui's theme macro reads them.
3. Haxe on khamake's build file, retargeted to `n64haxe` with `--no-output`; meanwhile the meshes are exported (`N64Exporter.export_assets()`: they only need the .blend). With Armory's compilation server preference on, Haxe runs through armor_64's own server (its own port), which keeps typed modules between builds; `n64_compile.stamp` makes it type the game's modules again every build.
4. `N64Exporter.export_code()` (`exporter.py`): the rest of the export; then the .blend is cleaned up.
5. Play and Publish: `make` (`export/build_runner.py`), one job per core, in MSYS2's bash (not a login shell: `utils.msys2_command()` sets the toolchain's paths). Then Play runs Ares.

## Modules

| Module | What it does |
|---|---|
| `exporter.py` | `N64Exporter`: the export steps in order, the export state, the .blend cleaned up afterwards (only the data the export added) |
| `utils.py` | Blender → N64 coordinates, Blender data reads, trait data initializers, C literals, files written only when they change, settings and paths, `msys2_command()` |
| `export/build_pipeline.py` | The build above: khamake, Haxe (and the compilation server), the export, make, Ares |
| `export/build_runner.py` | The Makefile, make, the RAM report, Ares |
| `export/scene_exporter.py` | Scene data (objects, cameras, lights, physics, traits), the scene files and `main_config.h` |
| `export/mesh_exporter.py` | glTF export of the meshes through Fast64 (F3D materials), cached: unchanged meshes aren't exported again |
| `export/linked_export.py` | Linked objects: local copies Fast64 can convert, removed afterwards |
| `export/ui_exporter.py`, `export/koui_theme_parser.py` | Koui canvases → UI tables, fonts and sprites; `.ksn` themes read as Koui reads them |
| `export/audio_exporter.py` | The sounds the game uses, converted to `wav64` |
| `export/traits_exporter.py` | The C of the Haxe code (traits, autoloads, signals) and the configuration headers (`engine.h`, `limits.h`, `debug_config.h`) |
| `codegen/ir.py` | Reads the IR files (`n64_traits.json`, `n64_autoloads.json`, `n64_signals.json`, `n64_audio.json`, `n64_limits.json`); a different IR version stops the export |
| `codegen/trait_generator.py` | `data/traits.h`, `data/traits.c`: data structs, lifecycle dispatch, methods, callbacks; the inheritance rules at its top (`needs_data_struct`, `chain_events`, `vtable_owner`...), shared with the scene generator |
| `codegen/trait_emitter.py` | The C of each IR node; `autoload_emitter.py` adapts it to autoloads |
| `codegen/autoload_generator.py` | One C file per autoload |
| `codegen/signal_codegen.py` | Signals: payload structs, static signals, handler functions |
| `codegen/tween_helper.py` | Callbacks and their captures (tweens, method arguments, signal handlers, buttons, lifecycle functions) |
| `codegen/scene_generator.py` | The scene tables (objects, cameras, lights, trait instances and their data) |
| `codegen/limits.py` | The engine limits (below) and the `limits.txt` report |
| `codegen/audio_codegen.py` | `audio/audio_config.h` |
| `codegen/c_text.py` | C string literals (no Blender imports: the tests load it) |

## From the IR to C

The macros ([Sources/armor64/](../Sources/armor64/README.md)) do the analysis; the codegen translates each IR node mechanically. An IR node type the exporter doesn't know stops the export (the macros and the exporter are from different versions). The generated C doesn't need to be readable: what matters is its speed and size (the test suites' `metrics.txt`).

- **Traits**: each trait instance has a data struct, static and filled when its scene starts (`init_trait_data()`); methods are C functions `<trait>_<method>(obj, data, ...)`, next to the runtime handlers (`on_add`, `on_ready`, `on_update`...). A child's struct embeds its parent's first, so a child's data is also each ancestor's: members are reached through the declaring trait's struct; overridden methods through a `_vfn_<method>` pointer, set when the data is filled. A trait's static vars are globals (`<trait>_<name>`).
- **Lifecycles**: each `notifyOnX(f)` call site is a counter in the trait's data (`_lc_<n>`) and a function (`<trait>_lc_<n>`). The call adds one; `removeX(f)` takes one from the first site that registers `f`. A handler runs each site as many times as its counter, the parent's sites first. `on_add` runs the constructor (`<trait>_on_create`) then the add sites.
- **Callbacks**: a callback's captured locals are copied where it's registered, into its trait's data (`_cap_<callback>_<name>`) or an autoload global (`<autoload>_cap_<callback>_<name>`), and read there by its function (`tween_helper.py`).
- **Another trait instance**: a call passes that instance's data (the reference) and its object; a field is read through the declaring trait's struct.
- **Signals**: a signal is identified by its declaring class and name; its argument types come from `n64_signals.json`. Every handler is a `void (void* ctx, void* payload)` function: method handlers call the method (through its `_vfn_` pointer if overridden), anonymous ones hold their body.

## Engine limits (`codegen/limits.py`)

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

Each export writes `build_<project>/n64/limits.txt`: every limit with its default and how its value was decided. After a successful build, the RAM is appended from the built ELF (code, data, bss; the pools with the limits sizing them; the largest other symbols) and the whole report is printed to the console.

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

## Blender → N64 coordinates (`utils.py`)

The runtime keeps N64 space (tiny3d's, y up): Blender's (x, y, z) is (x, z, −y), quaternions (x, z, −y, w). Positions stay in Blender units. Root objects' scales are ×1/64 (`SCALE_FACTOR`), because models are exported 64× bigger for tiny3d's fixed-point precision; a parented object keeps its scale and has its local position ×64 (its parent's matrix has the 1/64). Game code still sees Blender's axes and units: the macros convert every transform access (see [runtime/](../runtime/README.md)).

## Debugging

- The IR: `build_<project>/n64_traits.json`, `n64_autoloads.json` and the other `n64_*.json`.
- The generated C: `build_<project>/n64/src/` (`data/traits.c`, `autoloads/`, `scenes/`).
- `build_<project>/n64/limits.txt`: each limit, how it was decided, the RAM of the built game.
- make's and the compilers' output: Blender's system console.
- Code the macros can't translate: an `N64: ...` warning at its Haxe line (an error with `n64_strict`), and a comment in the C.
