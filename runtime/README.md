# The C runtime (`runtime/`)

The engine the generated game code runs on, over [libdragon](https://github.com/n64brew/libdragon) and [tiny3d](https://github.com/HailToDodongo/tiny3d). Each export copies `src/` into `build_<project>/n64/src/` (only the files that changed) and renders the templates (`*.j2`) next to them. The [Python exporter](../exporter/README.md) writes the game's data and code; the [macros](../Sources/armor64/README.md) decide what that code calls here.

## Rules

- **Performance first.** The N64 has a 93.75 MHz CPU and 4 MB of RAM: work that can be done at export time is done there, unused features are compiled out, hot paths avoid function pointers and copies.
- **Nothing is allocated during gameplay.** Pools and containers are static arrays sized at export (`limits.h`, see [exporter/](../exporter/README.md#engine-limits-codegenlimitspy)). A full one prints `N64 limit reached: <name>` once (`ARM_LIMIT_REACHED`) and drops the request.
- **Iron's behavior.** The engine follows Iron/Krom's semantics (update order, transforms, removals); `oimo/` follows upstream OimoPhysics, so fixes can be compared with it.

## Folders

| Path | What it holds |
|---|---|
| `main.c`, `engine.c` | Boot, the main loop (below), shutdown |
| `types.h`, `utils.h` | Shared types (objects, transforms, scenes, cameras, lights); string buffers, `ARM_DIFFERS` |
| `renderer.c`, `render2d.h` | Drawing the scene with tiny3d (culling, lights, the debug HUD); 2D drawing for `notifyOnRender2D` |
| `iron/object/` | Objects (removal), transforms (`transform.h`), animations |
| `iron/system/` | Gamepad input |
| `system/` | Time, tweens, signals, strings (`str.c`), `arm_array.h`, `arm_map.h` |
| `data/` | Scenes (loading, switching, trait handlers), models; the generated traits (`traits.*.j2`) |
| `events/` | Physics contact handlers |
| `oimo/` | OimoPhysics ported to C (collision, dynamics, debug drawing) and Iron's `RigidBody` API (`physics.c`) |
| `ui/` | Koui canvases: labels, images, panels, buttons, fonts, anchors |
| `audio/` | Aura on libdragon's mixer: sounds, channels, mix channels |
| `autoloads/` | The generated autoloads (templates) |

## Templates

Rendered with Python's `str.format` (`{name}`; a literal brace is doubled), not Jinja, despite the extension:

| Template | Written from |
|---|---|
| `Makefile.j2` | The sources to build, the toolchain's paths |
| `src/engine.h.j2` | The feature flags: `ENGINE_ENABLE_PHYSICS`, `ENGINE_ENABLE_PHYSICS_DEBUG`, `ENGINE_ENABLE_UI`, `ENGINE_ENABLE_AUDIO` (0 when the game doesn't use the feature) |
| `src/limits.h.j2` | The engine limits, `ARM_LIMIT_REACHED` |
| `src/data/traits.h.j2`, `traits.c.j2` | The traits' code (`codegen/trait_generator.py`) |
| `src/autoloads/*.j2` | The autoloads' code |
| `src/audio/audio_config.h.j2` | Sounds and mix channels |

`debug_config.h` (`ARM_RDP_VALIDATOR`: 1 in Play builds) and the scene files are written by Python without a template.

## The frame (`main.c`)

As Iron's `App.update`:

1. Time, gamepad input, audio.
2. Fixed steps: the physics step, then the traits' `on_fixed_update`, as many times as the elapsed time needs.
3. The bodies' transforms synced from physics (with the remaining fraction of a step).
4. `on_update`, tweens, `on_late_update`.
5. Objects removed this frame are removed (they leave the physics world).
6. A scene switch, if one was asked for: tweens cleared, the old scene's removals, physics reset, the new scene loaded and its canvas set up.
7. Render: the scene, the canvas, `on_render2d`; then audio is fed again.

Trait handlers (`on_add`, `on_ready`, `on_update`...) are generated per trait; the scene calls them through its trait instances (`data/scenes.c`).

## Transforms

The runtime keeps tiny3d's space (y up): Blender's (x, y, z) is (x, z, −y). Positions are in Blender units; a root object's scale has the ×1/64 the models are exported with, a parented object's (`ArmTransform.parented`) local position has ×64 instead. Game code works in Blender's axes and units through the accessors in `iron/object/transform.h` (`it_get_loc`, `it_set_loc_x`, `it_get_scale`...), which convert on access. A changed transform is marked `dirty` and its matrix rebuilt.

## Strings, signals, tweens

- **Strings** built by the game (`str_add*` in `system/str.c`) go into rotating buffers (`STR_FMT_BUFFER_COUNT` of `STR_FMT_BUFFER_SIZE`, per C file); numbers print as Haxe prints them.
- **Signals** (`system/signal.c`): a fixed list of `(handler, ctx)` subscribers per signal; `emit` passes a payload struct.
- **Tweens** (`system/tween.c`): a pool updated after `on_update`; a scene switch cancels the scene's tweens, autoload tweens keep running.

## Debugging

- `ARM_DEBUG_HUD` (Armory's Debug Console option): frame times and physics counters on screen.
- `ARM_RDP_VALIDATOR` (Play builds): libdragon's RDP validator, which catches commands that only crash on real hardware.
- `ARM_LIMIT_REACHED`: a full pool, once per limit.
- `ARM_DIFFERS(cond, where, what)`: code that runs differently from Krom (e.g. a capturing callback registered again while registered), reported once with its Haxe line.
- `debugf` output (these reports, `trace()`) goes to Ares' log; every build has it (NDEBUG isn't defined).
