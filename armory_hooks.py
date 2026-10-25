"""
N64 Exporter - Armory Hooks

Provides khafile.js injection for N64 builds via Armory's library hook system.
Called by write_data.py get_library_hooks() during khafile and Main.hx generation.

When an N64 build is active (state.is_n64), this hook injects:
  - arm_target_n64 define (enables N64 Haxe macros at compile-time)
  - arm_build_dir define (tells macros where to write IR output)
  - --macro parameter to register the N64 build macro (N64Macro)
"""

def write_main():
    import arm.make_state as state
    import arm.utils

    if not getattr(state, 'is_n64', False):
        return {}

    build_dir = arm.utils.build_dir()

    return {
        'defines': [
            'arm_target_n64',
            f'arm_build_dir=../{build_dir}',
        ],
        'parameters': [
            '--macro armor64.N64Macro.register()',
        ],
    }
