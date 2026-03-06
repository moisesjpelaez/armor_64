# N64 Exporter for Armory 3D
Export games for the Nintendo 64 with [Armory 3D](https://github.com/armory3d/armory) using [fast64](https://github.com/Fast-64/fast64), [libdragon](https://github.com/DragonMinded/libdragon) and [tiny3d](https://github.com/HailToDodongo/tiny3d).

> Note: this project is experimental. Working with Armory 2026.2 and Blender 4.5.

Supported features:
- Scenes:
  - cameras
  - directional lights
  - transitions (using `Scene.setActive`)
  - parent/child hierarchy (wip)
- Objects with Traits:
  - `Transform.translate`
  - `Transform.rotate`
  - `Transform.scale`
- Asset brower/Linked blend files
- Gamepad input (with hardcoded mapping)
- Physics (a stripped down [OimoPhysics](https://github.com/saharan/OimoPhysics) engine):
  - `RigidBody.applyForce`
  - `RigidBody.notifyOnContact`
  - shapes and contacts debugging
  - sphere, box, capsule and static mesh colliders
- UI ([Koui Editor](https://github.com/moisesjpelaez/koui_editor)):
  - labels (font family, text color and font size)
  - image panels
- Audio ([Aura](https://github.com/MoritzBrueckner/aura))
- Render2D (`kha.graphics2.Graphics`):
  - fillRect
  - color
- Animation actions (wip)

## Pre-requisites
- Windows 11
- MSYS2: https://www.msys2.org/
- Ares emulator: https://ares-emu.net/
- libdragon's toolchain: https://github.com/DragonMinded/libdragon/releases/tag/toolchain-continuous-prerelease (follow [Windows users with MSYS2](https://github.com/DragonMinded/libdragon/wiki/Installing-libdragon#windows-users-with-msys2) steps 1 and 2 to install it)
- Fast64: this specific branch https://github.com/moisesjpelaez/fast64/tree/f3d-to-bsdf

## Installation
- Locate the `.blend` file you are working with
- Create `Libraries` folder alongside your `.blend` file
- Clone these into `Libraries` folder:
  ```
  git clone https://github.com/DragonMinded/libdragon.git -b preview
  git clone https://github.com/HailToDodongo/tiny3d.git
  git clone https://github.com/moisesjpelaez/n64_exporter.git
  git clone https://github.com/moisesjpelaez/koui_editor.git
  git clone https://github.com/armory3d/oimo_module
  git clone https://github.com/armory3d/OimoPhysics
  ```
- Create `Subprojects` folder alongside your `.blend` file
- Clone these into `Subprojects` folder:
  ```
  git clone https://github.com/MoritzBrueckner/aura.git
  git clone https://gitlab.com/moises.jpelaez/Koui.git -b runtime_theme_parser
  ```

## Setup
- Go to `Edit > Preferences > Add-ons > Armory > N64 Settings` and set the paths for:
  - N64 toolchain
  - MSYS2 Bash Executable
  - MinGW 64
  - Ares Emulator
- Click `Install Dependencies` and wait for it to finish
- Click `Build libdragon` and wait for it to finish
- Click `Build Tiny3D` and wait for it to finish
- Set `Render > Armory Project > Modules > Physics Engine` to Oimo

## License
This project is licensed under the terms of the zlib License. See the [LICENSE](LICENSE.md) file for details.
