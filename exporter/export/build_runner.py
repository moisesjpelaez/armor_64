"""
The Makefile, make and Ares: the end of an N64 build (build_pipeline.py).

make and Ares run as Armory's build process and player (library_hooks), so
Blender stays responsive and Armory's Stop ends them.
"""

import os
from typing import Callable

import bpy

import arm.library_hooks as library_hooks
import arm.log as log
import arm.utils
import armor64.utils as n64_utils
from armor64.export import scene_exporter


def write_makefile(exporter):
    """build/n64/Makefile, from the export's results (exporter: N64Exporter)."""
    # Here: they import this module
    from armor64.export import audio_exporter, ui_exporter

    wrd = bpy.data.worlds['Arm']
    scene_files = [f'src/scenes/{arm.utils.safesrc(scene.name).lower()}.c'
                   for scene in scene_exporter.exported_scenes()]
    autoload_files = [f'src/autoloads/{c_name}.c' for c_name in exporter.autoload_info.get('autoloads', [])]

    font_targets, font_rules = ui_exporter.generate_font_makefile_entries(exporter)
    ui_sprite_rules = ui_exporter.generate_ui_sprite_rules(exporter)
    audio_targets, audio_rules = audio_exporter.generate_audio_makefile_entries(exporter.exported_audio)

    if not os.path.isdir(n64_utils.get_tiny3d_path()):
        log.warn(f'tiny3d library not found at: {n64_utils.get_tiny3d_path()}. Clone it into the project Libraries directory.')

    n64_utils.render_template(
        'Makefile.j2', 'Makefile',
        tiny3d_path=_tiny3d_relpath(),
        game_title=arm.utils.safestr(wrd.arm_project_name),
        enable_physics=1 if exporter.has_physics else 0,
        enable_physics_debug=1 if n64_utils.get_physics_debug_mode() > 0 else 0,
        enable_ui=1 if exporter.has_ui else 0,
        enable_audio=1 if exporter.has_audio else 0,
        scene_files=_make_list(scene_files),
        autoload_files=_make_list(autoload_files),
        font_targets=font_targets,
        font_rules=font_rules,
        ui_sprite_rules=ui_sprite_rules,
        audio_targets=audio_targets,
        audio_rules=audio_rules
    )


def _make_list(paths):
    """Paths as a Makefile list, one per line."""
    return ' \\\n    '.join(paths)


def _n64_dir() -> str:
    return os.path.join(arm.utils.get_fp(), arm.utils.build_dir(), 'n64')


def _tiny3d_relpath():
    """tiny3d's path from the Makefile's folder: relative, as GNU Make's include
    and the compiler flags break on spaces (e.g. "D:/Game Development/...")."""
    tiny3d_abs = os.path.abspath(n64_utils.get_tiny3d_path())
    return os.path.relpath(tiny3d_abs, os.path.abspath(_n64_dir())).replace('\\', '/')


def run_make(done: Callable[[bool], None]):
    """Build the ROM with make (one job per core) in the background, then
    done(success) on Blender's main thread."""
    build_abs = _n64_dir().replace('\\', '/')
    cmd = n64_utils.msys2_command(
        f'cd "{build_abs}" && make CURDIR=. T3D_DIR={_tiny3d_relpath()}/ -j{n64_utils.jobs()}')
    if cmd is None:
        done(False)
        return

    def finished(code: int):
        if code != 0:
            log.error(f'N64: make failed (exit code {code}, see the errors above)')
            done(False)
            return
        log.info('N64: ROM built')
        _write_memory_report()
        done(True)

    library_hooks.run_process(cmd, finished)


def _write_memory_report():
    """Complete build/n64/limits.txt with the RAM the built game uses, and print it."""
    from armor64.codegen import limits
    report_path = os.path.join(_n64_dir(), 'limits.txt')
    elf = os.path.join(_n64_dir(), 'build', f'{arm.utils.safestr(bpy.data.worlds["Arm"].arm_project_name)}.elf')
    try:
        memory = limits.memory_lines(elf, n64_utils.get_n64_toolchain_path())
        with open(report_path, 'a', encoding='utf-8') as f:
            f.write('\n' + '\n'.join(memory) + '\n')
        with open(report_path, 'r', encoding='utf-8') as f:
            print(f.read())
    except Exception as e:  # The report never fails the build
        log.warn(f'N64 limits report: {e}')


def run_emulator() -> bool:
    """The ROM in Ares, as Armory's player (Stop closes it); False if Ares
    isn't set up (reported)."""
    ares = n64_utils.get_ares_emulator_executable()
    if not ares or not os.path.isfile(ares):
        log.error("N64: set the Ares Emulator Executable in Armory's preferences (Nintendo 64 settings)")
        return False
    rom = os.path.join(_n64_dir(), f'{arm.utils.safestr(bpy.data.worlds["Arm"].arm_project_name)}.z64')
    library_hooks.run_player([ares, rom])
    return True
