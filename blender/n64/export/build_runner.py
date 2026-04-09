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
import arm.n64.utils as n64_utils
from arm.n64.export import linked_export


def write_makefile(exporter):
    """Generate the N64 Makefile from template.

    Args:
        exporter: N64Exporter instance with all export state
    """
    # Import sibling modules here to avoid circular imports
    from arm.n64.export import audio_exporter, ui_exporter

    wrd = bpy.data.worlds['Arm']
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'Makefile.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'Makefile')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    scene_lines = []
    for scene in bpy.data.scenes:
        if scene.library:
            continue
        if linked_export.is_temp_scene(scene):
            continue
        scene_name = arm.utils.safesrc(scene.name).lower()
        scene_lines.append(f'    src/scenes/{scene_name}.c')
    scene_files = '\\\n'.join(scene_lines)

    # Physics source files (only if physics is used)
    if exporter.has_physics:
        physics_debug_mode = n64_utils.get_physics_debug_mode()
        if physics_debug_mode > 0:
            physics_sources = '''src +=\\
    src/events/physics_events.c \\
    src/oimo/physics.c \\
    src/oimo/debug/physics_debug.c \\
    src/oimo/collision/geometry/geometry.c'''
        else:
            physics_sources = '''src +=\\
    src/oimo/physics.c \\
    src/events/physics_events.c \\
    src/oimo/collision/geometry/geometry.c'''
    else:
        physics_sources = '# No physics'

    # UI source files (only if UI elements are used)
    if exporter.has_ui:
        ui_sources = '''src +=\\
    src/ui/fonts.c \\
    src/ui/label.c \\
    src/ui/image.c \\
    src/ui/button.c \\
    src/ui/panel.c \\
    src/ui/canvas.c'''
    else:
        ui_sources = '# No UI'

    # Autoload source files (only if autoloads exist)
    if exporter.autoload_info.get('has_autoloads', False):
        autoload_lines = ['src +=\\']
        for c_name in exporter.autoload_info.get('autoloads', []):
            autoload_lines.append(f'    src/autoloads/{c_name}.c \\')
        # Remove trailing backslash from last line
        autoload_lines[-1] = autoload_lines[-1].rstrip(' \\')
        autoload_sources = '\n'.join(autoload_lines)
    else:
        autoload_sources = '# No autoloads'

    # Generate font targets and rules for each size variant
    font_targets, font_rules = ui_exporter.generate_font_makefile_entries(exporter)

    # Generate per-file sprite rules for UI images (RGBA32)
    ui_sprite_rules = ui_exporter.generate_ui_sprite_rules(exporter)

    # Generate audio targets and rules
    audio_targets, audio_rules = audio_exporter.generate_audio_makefile_entries(exporter.exported_audio)

    # Audio source files (only if audio is used)
    if exporter.has_audio:
        audio_sources = '''src +=\\
    src/audio/audio.c'''
    else:
        audio_sources = '# No audio'

    tiny3d_path = arm.utils.get_tiny3d_path()
    if not os.path.isdir(tiny3d_path):
        log.warn(f'tiny3d library not found at: {tiny3d_path}. Clone it into the project Libraries directory.')

    tiny3d_rel = _tiny3d_relpath()

    output = tmpl_content.format(
        tiny3d_path=tiny3d_rel,
        game_title=arm.utils.safestr(wrd.arm_project_name),
        scene_files=scene_files,
        physics_sources=physics_sources,
        canvas_sources=ui_sources,
        autoload_sources=autoload_sources,
        audio_sources=audio_sources,
        font_targets=font_targets,
        font_rules=font_rules,
        ui_sprite_rules=ui_sprite_rules,
        audio_targets=audio_targets,
        audio_rules=audio_rules
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def _tiny3d_relpath():
    """Relative path from the Makefile directory to tiny3d.

    Uses a relative path to avoid spaces in the project root
    (e.g. "D:/Game Development/...") which break GNU Make's include
    directive and compiler flags.
    """
    tiny3d_abs = os.path.abspath(arm.utils.get_tiny3d_path())
    makefile_abs = os.path.abspath(
        os.path.join(arm.utils.get_fp(), arm.utils.build_dir(), 'n64')
    )
    return os.path.relpath(tiny3d_abs, makefile_abs).replace('\\', '/')


def run_make():
    """Run the N64 make process via MSYS2.

    Returns:
        bool: True if build succeeded, False otherwise
    """
    msys2_executable = arm.utils.get_msys2_bash_executable()
    if len(msys2_executable) > 0:
        n64_toolchain = arm.utils.get_n64_toolchain_path()
        mingw64 = arm.utils.get_mingw64_path()
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
    return True


def run_emulator():
    """Launch the game ROM in the Ares emulator.

    Returns:
        bool: True if emulator launched, False if not configured
    """
    ares_emulator_executable = arm.utils.get_ares_emulator_executable()

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
