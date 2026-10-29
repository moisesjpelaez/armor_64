"""
Traits Exporter - writes the C of the project's Haxe code and the engine's
configuration headers: traits.h/c and signals.h, the autoloads, types.h's
companions (debug_config.h, limits.h) and engine.h. Used by exporter.py.
"""

import bpy

import armor64.utils as n64_utils
import armor64.codegen as codegen


def write_types(exporter):
    """Generate debug_config.h. Only engine.c includes it, so switching between
    Play and Publish rebuilds one file. The RDP validator catches RDP crashes that
    only happen on hardware while running in the emulator, at some CPU cost: on
    for Play (Ares), off for ROMs meant for hardware."""
    validator = 1 if exporter.for_emulator else 0
    n64_utils.write_generated('debug_config.h', f'#define ARM_RDP_VALIDATOR {validator}',
                              'traits_exporter.write_types', 'engine.c')


def write_limits(exporter):
    """Generate limits.h: the engine limits computed for this project
    (codegen/limits.py), and limits.txt, their report (completed with the RAM once
    the game is built, build_runner)."""
    from armor64.codegen import limits

    n64_utils.render_template('src/limits.h.j2', 'src/limits.h', **limits.header_data(exporter.limits))
    n64_utils.write_if_changed(n64_utils.build_path('limits.txt'), '\n'.join(
        ['N64 engine limits (exporter/codegen/limits.py)', '']
        + limits.report_lines(exporter.limits, exporter.limit_notes)) + '\n')


def write_traits(exporter):
    """Generate traits.h, traits.c and signals.h (payload structs and static
    signals, included through system/signal.h). Returns the features the traits
    use ('has_ui', 'has_physics')."""
    template_data, features = codegen.prepare_traits_template_data(_collect_type_overrides(exporter))
    signals = codegen.signals_context()
    n64_utils.write_if_changed(n64_utils.build_path('src/data/signals.h'), signals.header())

    if template_data is None:
        # No traits: still defines the static signals
        n64_utils.write_if_changed(n64_utils.build_path('src/data/traits.h'),
                                   '// Auto-generated empty traits header\n#ifndef _TRAITS_H_\n#define _TRAITS_H_\n#endif\n')
        n64_utils.write_if_changed(n64_utils.build_path('src/data/traits.c'),
                                   '// Auto-generated traits implementation (no traits)\n#include "traits.h"\n\n'
                                   + signals.static_definitions() + '\n')
    else:
        n64_utils.render_template('src/data/traits.h.j2', 'src/data/traits.h', **template_data)
        n64_utils.render_template('src/data/traits.c.j2', 'src/data/traits.c', **template_data)
    return features or {}


def _collect_type_overrides(exporter) -> dict:
    """The member types trait instances set in Blender (a property's type
    override), by trait (full class path, as the IR) and member."""
    overrides = {}
    for scene_data in exporter.scene_data.values():
        entities = scene_data.get('cameras', []) + scene_data.get('lights', []) + scene_data.get('objects', [])
        traits = scene_data.get('traits', []) + [t for e in entities for t in e.get('traits', [])]
        for trait in traits:
            if trait.get('type_overrides'):
                # Scenes store package-relative names
                class_name = n64_utils.trait_key(exporter.trait_info, trait.get('class_name', ''))
                overrides.setdefault(class_name, {}).update(trait['type_overrides'])
    return overrides


def write_autoloads(exporter):
    """Generate the autoloads' C (@:n64Autoload classes: C modules) and
    autoloads.h, which includes them all. Sets exporter.autoload_info and
    returns the features they use ('has_audio')."""
    autoload_data, master_data, features = codegen.prepare_autoload_template_data()
    if not autoload_data:
        exporter.autoload_info = {'autoloads': [], 'has_autoloads': False}
        return {'has_audio': False}

    for c_name, data in autoload_data:
        n64_utils.render_template('src/autoloads/autoload.h.j2', f'src/autoloads/{c_name}.h', **data)
        n64_utils.render_template('src/autoloads/autoload.c.j2', f'src/autoloads/{c_name}.c', **data)
    n64_utils.render_template('src/autoloads/autoloads.h.j2', 'src/autoloads/autoloads.h', **master_data)

    exporter.autoload_info = {'autoloads': [c_name for c_name, _ in autoload_data], 'has_autoloads': True}
    return features or {}


def write_engine(exporter):
    """Generate engine.h: the features the game uses and the debug HUD (Armory's
    debug console setting: FPS, physics and draw counters)."""
    wrd = bpy.data.worlds.get('Arm')
    debug_hud = wrd is not None and wrd.arm_debug_console
    n64_utils.render_template(
        'src/engine.h.j2', 'src/engine.h',
        enable_physics=1 if exporter.has_physics else 0,
        enable_physics_debug=1 if n64_utils.get_physics_debug_mode() > 0 else 0,
        enable_ui=1 if exporter.has_ui else 0,
        enable_audio=1 if exporter.has_audio else 0,
        debug_hud_define='#define ARM_DEBUG_HUD' if debug_hud else '')
