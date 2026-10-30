# Armor 64: Nintendo 64 games with Armory 3D

Armor 64 builds [Armory 3D](https://github.com/armory3d/armory) games as Nintendo 64 ROMs. Your scenes, meshes, UI, sounds and Haxe traits become C for [libdragon](https://github.com/n64brew/libdragon) and [tiny3d](https://github.com/HailToDodongo/tiny3d), and you play the result in the [Ares](https://ares-emu.net/) emulator or on real hardware.

> This library is experimental and in development, co-authored by Claude Opus 4.5/5.5. It runs [Armor Ball](https://github.com/moisesjpelaez/arm_ball) on Ares and on an Analogue 3D.

- **Making a game:** this page, then [GUIDE.md](GUIDE.md) for the game code you can write.
- **Changing Armor 64:** [exporter/](exporter/README.md) (Python), [Sources/armor64/](Sources/armor64/README.md) (Haxe macros), [runtime/](runtime/README.md) (C), [Tests/](Tests/README.md).

## What you can use

| Area | On N64 |
|---|---|
| Scenes | Cameras, directional lights, parent/child hierarchies, scene changes (`Scene.setActive`), linked and appended blends (asset browser) |
| Objects | Meshes with Fast64 (F3D) materials, Armory's culling mode, animation actions (`play`, `pause`, `resume`), `object.remove()` |
| Traits | Lifecycles (`notifyOnInit`, `notifyOnUpdate`, ...), constructors, inheritance and overrides, properties with getters and setters, references to other traits, autoloads (`@:n64Autoload`) |
| Haxe | Numbers, strings (concatenation, interpolation, `Std.string`, `trace`), `Map` and `Array`, `if`/`while`/`for` over ranges, math (`Math.*`), vectors (`Vec2`, `Vec3`, `Vec4`), signals (`armory.system.Signal`), tweens (`armory.system.Tween`), callbacks |
| Transforms | `loc`, `scale`, `rot`, `translate`, `rotate`, `move`, `setRotation`, `look`/`right`/`up`, `worldx`/`worldy`/`worldz`, in Blender's axes and units as on Krom |
| Input | The gamepad (buttons by name, the stick) |
| Physics | [OimoPhysics](https://github.com/saharan/OimoPhysics) (ported to C): sphere, box, capsule and static mesh colliders, forces, impulses, velocities, contacts (`notifyOnContact`), debug drawing |
| UI | [Koui Editor](https://github.com/moisesjpelaez/koui_editor) canvases: labels (font, size, color), images, panels, buttons (pressed, released, focus), layouts, scenes |
| Audio | [Aura](https://github.com/MoritzBrueckner/aura): sounds, mix channels and their volume, sound handles |
| 2D drawing | `kha.graphics2.Graphics`: `fillRect`, `color` |

What works differently from Krom, and what isn't supported yet, is in [GUIDE.md](GUIDE.md). Code the N64 can't run is reported when you build (below), never left out silently.

## Requirements

- Windows 11
- [MSYS2](https://www.msys2.org/)
- libdragon's toolchain: the [toolchain release](https://github.com/n64brew/libdragon/releases/tag/toolchain-continuous-prerelease) (follow [Windows users with MSYS2](https://github.com/n64brew/libdragon/wiki/Installing-libdragon#windows-users-with-msys2), steps 1 and 2)
- The [Ares](https://ares-emu.net/) emulator
- Blender with Armory
- Fast64, this branch: https://github.com/moisesjpelaez/fast64/tree/f3d-to-bsdf

## Installation

Next to your `.blend` file, create a `Libraries` folder and clone into it:
```
git clone https://github.com/n64brew/libdragon.git -b preview
git clone https://github.com/HailToDodongo/tiny3d.git
git clone https://github.com/moisesjpelaez/armor_64.git
git clone https://github.com/moisesjpelaez/koui_editor.git
git clone https://github.com/armory3d/oimo_module
git clone https://github.com/armory3d/OimoPhysics
```
Next to your `.blend` file, create a `Subprojects` folder and clone into it:
```
git clone https://github.com/MoritzBrueckner/aura.git
git clone https://gitlab.com/moises.jpelaez/Koui.git -b koui_editor
```

## Setup

1. In Armory's preferences (`Edit > Preferences > Add-ons > Armory`), check **Show Advanced**, open the **Build Preferences** tab and find the **Nintendo 64 Settings** box: set the N64 Toolchain Path, the MSYS2 Bash Executable, the MinGW64 Path and the Ares Emulator Executable.
2. In the same box, click **Install Dependencies**, then **Build libdragon**, then **Build Tiny3D**, each once the previous one is done. They run in the background: the console shows their output, Armory's log says when each is done, and Stop ends it. Later clicks only rebuild what changed; check **Rebuild From Scratch** to build everything again.
3. Set `Render > Armory Project > Modules > Physics Engine` to Oimo.

## Building

| | How | What you get |
|---|---|---|
| **Play** | Runtime **Ares**, then Play | The ROM, run in Ares. Stop closes Ares |
| **Build** | An exporter entry with the **Nintendo 64** target, then Build | The C source of the game (`build_<project>/n64/`), no ROM |
| **Publish** | The same entry, then Publish | A clean build of the ROM: `build_<project>/n64/<project>.z64` |

- **Play builds check the RDP** (libdragon's RDP validator): it catches graphics commands that crash only on real hardware, at a CPU cost, so expect a lower frame rate and possible VI warnings in Ares' log. Publish builds don't have it: test those on hardware.
- **Traces** (`trace()`) print to Ares' log. With **No Traces** checked (the Armory Exporter panel), Publish builds leave them out, as on Krom.
- Every build runs in the background: Blender stays responsive and Stop ends the build.
- Turn on Armory's **compilation server** preference for faster Haxe builds: Armor 64 runs its own server (its port is in the Nintendo 64 settings).

## When something goes wrong

- **`N64: ...` warnings** in the Haxe build point at code the N64 can't run yet, at its line: it's left out of the game. To make them errors, define `n64_strict`: a text block with `project.addDefine('n64_strict');` set as **Append Khafile** (`Render > Armory Project > Modules`).
- **`build_<project>/n64/limits.txt`**: every fixed-size pool of the engine (tweens, bodies, labels, string buffers...), how its size was decided for your game, and the RAM the ROM uses. A pool that fills up while the game runs prints `N64 limit reached: <name>` in Ares' log once.
- **The console** (`Window > Toggle System Console`) has the full output of make and the compilers.

## License

This project is licensed under the terms of the zlib License. See [LICENSE.md](LICENSE.md).
