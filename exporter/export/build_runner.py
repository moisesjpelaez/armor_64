"""
Build Runner - Handles Makefile generation and N64 build execution.

This module provides functions for generating the N64 Makefile and
running the build process via MSYS2.
"""

import os
import subprocess

import bpy

import arm.utils
import arm.log as log
import armor64.utils as n64_utils
from armor64.export import scene_exporter


def write_makefile(exporter):
    """Generate the N64 Makefile from template.

    Args:
        exporter: N64Exporter instance with all export state
    """
    # Import sibling modules here to avoid circular imports
    from armor64.export import audio_exporter, ui_exporter

    wrd = bpy.data.worlds['Arm']
    scene_files = []
    for scene in scene_exporter.exported_scenes():
        scene_files.append(f'src/scenes/{arm.utils.safesrc(scene.name).lower()}.c')
    autoload_files = [f'src/autoloads/{c_name}.c' for c_name in exporter.autoload_info.get('autoloads', [])]

    # Generate font targets and rules for each size variant
    font_targets, font_rules = ui_exporter.generate_font_makefile_entries(exporter)

    # Generate per-file sprite rules for UI images (RGBA32)
    ui_sprite_rules = ui_exporter.generate_ui_sprite_rules(exporter)

    # Generate audio targets and rules
    audio_targets, audio_rules = audio_exporter.generate_audio_makefile_entries(exporter.exported_audio)

    tiny3d_path = n64_utils.get_tiny3d_path()
    if not os.path.isdir(tiny3d_path):
        log.warn(f'tiny3d library not found at: {tiny3d_path}. Clone it into the project Libraries directory.')

    tiny3d_rel = _tiny3d_relpath()

    n64_utils.render_template(
        'Makefile.j2', 'Makefile',
        tiny3d_path=tiny3d_rel,
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


def _tiny3d_relpath():
    """Relative path from the Makefile directory to tiny3d.

    Uses a relative path to avoid spaces in the project root
    (e.g. "D:/Game Development/...") which break GNU Make's include
    directive and compiler flags.
    """
    tiny3d_abs = os.path.abspath(n64_utils.get_tiny3d_path())
    makefile_abs = os.path.abspath(
        os.path.join(arm.utils.get_fp(), arm.utils.build_dir(), 'n64')
    )
    return os.path.relpath(tiny3d_abs, makefile_abs).replace('\\', '/')


def run_make():
    """Run the N64 make process via MSYS2.

    Returns:
        bool: True if build succeeded, False otherwise
    """
    msys2_executable = n64_utils.get_msys2_bash_executable()
    if len(msys2_executable) > 0:
        n64_toolchain = n64_utils.get_n64_toolchain_path()
        mingw64 = n64_utils.get_mingw64_path()
        build_abs = os.path.join(
            arm.utils.get_fp(), arm.utils.build_dir(), 'n64'
        ).replace('\\', '/')
        t3d_dir = _tiny3d_relpath() + '/'
        try:
            proc = subprocess.run(
                [
                    msys2_executable,
                    '--login',
                    '-c',
                    (
                        f'export MSYSTEM=MINGW64; '
                        f'export N64_INST="{n64_toolchain}"; '
                        f'export PATH="{n64_toolchain}:{mingw64}:$PATH"; '
                        f'cd "{build_abs}" && make CURDIR=. T3D_DIR={t3d_dir}'
                    )
                ],
                stdout=None,
                stderr=None,
                text=True
            )
        except Exception as e:
            log.error(f'Error running make: {e}')
            return False
        if proc.returncode != 0:
            log.error(f'Make process failed with exit code {proc.returncode}.')
            return False
    else:
        log.error('MSYS2 Bash executable path is not set in Armory preferences.')
        return False
    log.info('Info: N64 make process completed successfully.')
    _write_memory_report()
    return True


def _write_memory_report():
    """Complete build/n64/limits.txt with the RAM the built game uses, and print it."""
    from armor64.codegen import limits
    n64_dir = os.path.join(arm.utils.get_fp(), arm.utils.build_dir(), 'n64')
    report_path = os.path.join(n64_dir, 'limits.txt')
    elf = os.path.join(n64_dir, 'build', f'{arm.utils.safestr(bpy.data.worlds["Arm"].arm_project_name)}.elf')
    try:
        memory = limits.memory_lines(elf, n64_utils.get_n64_toolchain_path())
        with open(report_path, 'a', encoding='utf-8') as f:
            f.write('\n' + '\n'.join(memory) + '\n')
        with open(report_path, 'r', encoding='utf-8') as f:
            print(f.read())
    except Exception as e:  # The report never fails the build
        log.warn(f'N64 limits report: {e}')


def run_emulator():
    """Launch the game ROM in the Ares emulator.

    Returns:
        bool: True if emulator launched, False if not configured
    """
    ares_emulator_executable = n64_utils.get_ares_emulator_executable()

    if not ares_emulator_executable:
        log.error('Ares emulator executable path is not set in Armory preferences.')
        return False

    wrd = bpy.data.worlds['Arm']
    rom_path = os.path.join(
        arm.utils.get_fp(), arm.utils.build_dir(), 'n64',
        f'{arm.utils.safestr(wrd.arm_project_name)}.z64'
    )

    subprocess.Popen(
        [ares_emulator_executable, rom_path],
        stdout=None,
        stderr=None,
        text=True
    )
    return True
