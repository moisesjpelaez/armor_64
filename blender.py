"""
N64 Exporter - Standalone Library Integration for Armory3D

This module is loaded by Armory's library system (handlers.py load_py_libraries).
On register():
  - Registers the arm.n64 Python namespace (pointing to this library's modules)
  - Injects N64 preferences into Armory addon settings
  - Registers N64 build operators (Install Dependencies, Build libdragon, Build Tiny3D)
The builds themselves ("Ares" runtime, "Nintendo 64" target) go through
Armory's library hooks: see armory_hooks.py.

On unregister(), all of it is cleanly reversed.
"""

import importlib
import os
import subprocess
import sys


import bpy
from bpy.props import StringProperty, BoolProperty, IntProperty

# ---------------------------------------------------------------------------
# Library path (determined at import time — Armory adds our dir to sys.path)
# ---------------------------------------------------------------------------
_LIBRARY_DIR = os.path.dirname(os.path.abspath(__file__))
_EXPORTER_DIR = os.path.join(_LIBRARY_DIR, 'exporter')

# ---------------------------------------------------------------------------
# Saved originals for clean unregister
# ---------------------------------------------------------------------------
_originals = {}
_registered_classes = []


# =============================================================================
# arm.n64 Namespace Registration
# =============================================================================

def _register_n64_namespace():
    """Register the library's exporter package into the arm.n64 namespace
    (each module is loaded by its path: nothing is added to sys.path)."""
    import arm

    n64_pkg_dir = _EXPORTER_DIR
    codegen_dir = os.path.join(n64_pkg_dir, 'codegen')
    export_dir = os.path.join(n64_pkg_dir, 'export')

    def _make_stub_package(dotted_name, file_path):
        """Create a stub package module in sys.modules without executing it."""
        spec = importlib.util.spec_from_file_location(
            dotted_name, file_path,
            submodule_search_locations=[os.path.dirname(file_path)]
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[dotted_name] = mod
        parts = dotted_name.rsplit('.', 1)
        if len(parts) == 2 and parts[0] in sys.modules:
            setattr(sys.modules[parts[0]], parts[1], mod)
        return mod, spec

    def _load_module(dotted_name, file_path):
        """Load and execute a module, registering it in sys.modules."""
        spec = importlib.util.spec_from_file_location(dotted_name, file_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[dotted_name] = mod
        parts = dotted_name.rsplit('.', 1)
        if len(parts) == 2 and parts[0] in sys.modules:
            setattr(sys.modules[parts[0]], parts[1], mod)
        spec.loader.exec_module(mod)
        return mod

    # Step 1: Create stub packages (registered in sys.modules but NOT executed)
    # This allows submodule imports like "from arm.n64.codegen import ..." to resolve
    n64_mod, n64_spec = _make_stub_package('arm.n64', os.path.join(n64_pkg_dir, '__init__.py'))
    codegen_mod, codegen_spec = _make_stub_package('arm.n64.codegen', os.path.join(codegen_dir, '__init__.py'))
    export_mod, export_spec = _make_stub_package('arm.n64.export', os.path.join(export_dir, '__init__.py'))

    # Step 2: Load leaf modules (these can import from arm.n64.* since stubs exist)
    _load_module('arm.n64.utils', os.path.join(n64_pkg_dir, 'utils.py'))

    # Codegen leaves (order: no-deps first, then modules that depend on them)
    for fname in ['limits', 'signal_codegen', 'trait_emitter', 'tween_helper', 'autoload_emitter', 'trait_generator', 'autoload_generator', 'audio_codegen', 'scene_generator']:
        fpath = os.path.join(codegen_dir, f'{fname}.py')
        if os.path.isfile(fpath):
            _load_module(f'arm.n64.codegen.{fname}', fpath)

    # Export leaves (order: no-deps first)
    for fname in ['koui_theme_parser', 'linked_export', 'mesh_exporter', 'scene_exporter',
                  'traits_exporter', 'ui_exporter', 'physics_exporter', 'audio_exporter', 'build_runner', 'build_pipeline']:
        fpath = os.path.join(export_dir, f'{fname}.py')
        if os.path.isfile(fpath):
            _load_module(f'arm.n64.export.{fname}', fpath)

    # Step 3: Execute package __init__.py files (all submodules now available)
    codegen_spec.loader.exec_module(codegen_mod)
    export_spec.loader.exec_module(export_mod)

    # Step 4: Load exporter (depends on codegen + export)
    _load_module('arm.n64.exporter', os.path.join(n64_pkg_dir, 'exporter.py'))

    # Step 5: Execute arm.n64 __init__ (utils, codegen, exporter all available)
    n64_spec.loader.exec_module(n64_mod)


def _unregister_n64_namespace():
    """Remove arm.n64 namespace from sys.modules."""
    import arm

    keys_to_remove = [k for k in sys.modules if k == 'arm.n64' or k.startswith('arm.n64.')]
    for k in keys_to_remove:
        del sys.modules[k]

    if hasattr(arm, 'n64'):
        delattr(arm, 'n64')


# =============================================================================
# N64 Addon Preferences (injected into ArmoryAddonPreferences)
# =============================================================================

def _get_prefs_class():
    """Get the ArmoryAddonPreferences class."""
    addon_prefs = bpy.context.preferences.addons.get("armory")
    if addon_prefs:
        return addon_prefs.preferences.__class__
    return None


def _path_update_factory(prop_name):
    """Create a path update callback for a given property name."""
    def update(self, context):
        if getattr(self, 'skip_update', False):
            return
        self.skip_update = True
        setattr(self, prop_name,
                bpy.path.reduce_dirs([bpy.path.abspath(getattr(self, prop_name))])[0])
        self.skip_update = False
    return update


def _register_preferences():
    """Inject N64 preference properties into ArmoryAddonPreferences.

    Uses unregister/re-register to properly register properties with
    Blender's RNA system. This is safe for AddonPreferences (singleton).
    Saved values are preserved across the re-registration.
    """
    prefs_cls = _get_prefs_class()
    if prefs_cls is None:
        return

    # Save current values before re-registration
    saved_values = {}
    try:
        prefs = bpy.context.preferences.addons["armory"].preferences
        for prop_name in ['n64_toolchain_path', 'msys2_bash_executable', 'mingw64_path',
                          'ares_emulator_executable', 'open_n64_rom_directory',
                          'n64_compilation_server_port']:
            if hasattr(prefs, prop_name):
                saved_values[prop_name] = getattr(prefs, prop_name)
    except Exception:
        pass

    bpy.utils.unregister_class(prefs_cls)

    prefs_cls.__annotations__['n64_toolchain_path'] = StringProperty(
        name="N64 Toolchain Path",
        description="Path to the N64 Toolchain installation directory",
        default="", subtype="FILE_PATH",
        update=_path_update_factory('n64_toolchain_path')
    )
    prefs_cls.__annotations__['msys2_bash_executable'] = StringProperty(
        name="MSYS2 Bash Executable",
        description="Path to the MSYS2 Bash executable",
        default="", subtype="FILE_PATH",
        update=_path_update_factory('msys2_bash_executable')
    )
    prefs_cls.__annotations__['mingw64_path'] = StringProperty(
        name="MinGW64 Path",
        description="Path to the MinGW64 directory",
        default="", subtype="FILE_PATH",
        update=_path_update_factory('mingw64_path')
    )
    prefs_cls.__annotations__['ares_emulator_executable'] = StringProperty(
        name="Ares Emulator Executable",
        description="Path to the Ares Emulator executable",
        default="", subtype="FILE_PATH",
        update=_path_update_factory('ares_emulator_executable')
    )
    prefs_cls.__annotations__['open_n64_rom_directory'] = BoolProperty(
        name="Open Nintendo 64 ROM Directory",
        description="Open the Nintendo 64 ROM directory after successfully build",
        default=False
    )
    prefs_cls.__annotations__['n64_compilation_server_port'] = IntProperty(
        name="N64 Compilation Server Port",
        description="Port of the Haxe compilation server of Nintendo 64 builds (used when "
                    "Armory's compilation server is on; different from Armory's port)",
        default=6464, min=1024, max=65535
    )

    bpy.utils.register_class(prefs_cls)

    # Restore saved values
    try:
        prefs = bpy.context.preferences.addons["armory"].preferences
        for prop_name, value in saved_values.items():
            setattr(prefs, prop_name, value)
    except Exception:
        pass


def _unregister_preferences():
    """Remove N64 preference properties from ArmoryAddonPreferences."""
    prefs_cls = _get_prefs_class()
    if prefs_cls is None:
        return

    bpy.utils.unregister_class(prefs_cls)

    for prop_name in ['n64_toolchain_path', 'msys2_bash_executable', 'mingw64_path',
                      'ares_emulator_executable', 'open_n64_rom_directory',
                      'n64_compilation_server_port']:
        if prop_name in prefs_cls.__annotations__:
            del prefs_cls.__annotations__[prop_name]

    bpy.utils.register_class(prefs_cls)


# =============================================================================
# Preferences UI Patch (draw Nintendo 64 Settings section)
# =============================================================================

def _register_preferences_ui():
    """Patch ArmoryAddonPreferences.draw to append N64 settings."""
    prefs_cls = _get_prefs_class()
    if prefs_cls is None:
        return

    _originals['prefs_draw'] = prefs_cls.draw

    def _patched_draw(self, context):
        _originals['prefs_draw'](self, context)

        layout = self.layout
        box = layout.box().column()
        box.label(text="Nintendo 64 Settings")
        box.prop(self, "n64_toolchain_path")
        box.prop(self, "msys2_bash_executable")
        box.prop(self, "mingw64_path")
        box.prop(self, "ares_emulator_executable")
        box.prop(self, "open_n64_rom_directory")
        if getattr(self, "compilation_server", False):
            box.prop(self, "n64_compilation_server_port")
        box.label(text="Libraries:")
        row = box.row(align=True)
        row.operator("n64_exporter.install_libdragon_dependencies", icon="IMPORT")
        row.operator("n64_exporter.build_libdragon", icon="MOD_BUILD")
        row.operator("n64_exporter.build_tiny3d", icon="MOD_BUILD")

    prefs_cls.draw = _patched_draw


def _unregister_preferences_ui():
    """Restore original preferences draw method."""
    prefs_cls = _get_prefs_class()
    if prefs_cls is None:
        return

    if 'prefs_draw' in _originals:
        prefs_cls.draw = _originals['prefs_draw']


# =============================================================================
# N64 Build Operators
# =============================================================================

class N64_OT_InstallLibdragonDependencies(bpy.types.Operator):
    """Install libdragon dependencies via pacman"""
    bl_idname = "n64_exporter.install_libdragon_dependencies"
    bl_label = "Install Dependencies"
    bl_description = "Install libdragon dependencies (base-devel, mingw-w64-x86_64-gcc, mingw-w64-x86_64-make, git)"

    def execute(self, context):
        import arm.n64.utils as n64_utils
        import arm.utils

        sdk_path = arm.utils.get_sdk_path()
        if sdk_path == "":
            self.report({"ERROR"}, "Configure Armory SDK path first")
            return {"CANCELLED"}

        msys2_exe = n64_utils.get_msys2_bash_executable()
        if not msys2_exe or not os.path.exists(msys2_exe):
            self.report({'ERROR'}, 'MSYS2 Bash executable not configured in preferences')
            return {'CANCELLED'}

        env = os.environ.copy()
        env['MSYSTEM'] = 'MINGW64'

        result = subprocess.run(
            [
                rf'{msys2_exe}',
                '--login',
                '-c',
                'pacman -S --needed --noconfirm base-devel mingw-w64-x86_64-gcc mingw-w64-x86_64-make git'
            ],
            stdout=None, stderr=None, text=True, env=env
        )

        if result.returncode == 0:
            self.report({'INFO'}, 'libdragon dependencies installed successfully.')
        else:
            self.report({'WARNING'}, 'libdragon dependencies installation completed with errors. Check console for details.')

        return {'FINISHED'}


class N64_OT_BuildLibdragon(bpy.types.Operator):
    """Build libdragon for Nintendo 64"""
    bl_idname = "n64_exporter.build_libdragon"
    bl_label = "Build libdragon"
    bl_description = "Build libdragon for Nintendo 64 development"

    def execute(self, context):
        import arm.n64.utils as n64_utils
        import arm.utils

        sdk_path = arm.utils.get_sdk_path()
        if sdk_path == "":
            self.report({"ERROR"}, "Configure Armory SDK path first")
            return {"CANCELLED"}

        msys2_exe = n64_utils.get_msys2_bash_executable()
        if not msys2_exe:
            self.report({"ERROR"}, "Configure MSYS2 Bash executable path first")
            return {"CANCELLED"}

        libdragon_path = n64_utils.get_libdragon_path()
        if not os.path.isdir(libdragon_path):
            self.report({"ERROR"}, f"libdragon not found. Clone it into the project's Libraries directory: {n64_utils.get_n64_libraries_dir()}")
            return {"CANCELLED"}

        addon_prefs = arm.utils.get_arm_preferences()
        libdragon_path_posix = n64_utils.win_path_to_posix(libdragon_path)
        n64_toolchain_path = n64_utils.win_path_to_posix(addon_prefs.n64_toolchain_path)
        mingw64_path = n64_utils.win_path_to_posix(addon_prefs.mingw64_path)

        print('Building libdragon for Nintendo 64, check console for details.')

        env = os.environ.copy()
        env['MSYSTEM'] = 'MINGW64'
        env['N64_INST'] = n64_toolchain_path
        env['PATH'] = f"{n64_toolchain_path}:{mingw64_path}:{env.get('PATH', '')}"

        # Run make commands directly (instead of build.sh) so we can pass
        # CURDIR=. to work around spaces in the project path.  n64.mk uses
        # $(CURDIR) in -ffile-prefix-map which breaks when CWD has spaces.
        build_cmd = (
            f'cd "{libdragon_path_posix}" && '
            f'make CURDIR=. -j4 install-mk && '
            f'make CURDIR=. -j4 clobber && '
            f'make CURDIR=. -j4 libdragon tools && '
            f'make CURDIR=. -j4 install tools-install && '
            f'make CURDIR=. -j4 examples'
        )
        result = subprocess.run(
            [rf'{msys2_exe}', '--login', '-c', build_cmd],
            stdout=None, stderr=None, text=True, env=env
        )

        if result.returncode != 0:
            self.report({"ERROR"}, "Failed building libdragon, check console for details.")
        else:
            self.report({'INFO'}, 'libdragon build completed.')

        return {"FINISHED"}


class N64_OT_BuildTiny3d(bpy.types.Operator):
    """Build Tiny3D for Nintendo 64"""
    bl_idname = "n64_exporter.build_tiny3d"
    bl_label = "Build Tiny3D"
    bl_description = "Build Tiny3D rendering library for Nintendo 64 development"

    def execute(self, context):
        import arm.n64.utils as n64_utils
        import arm.utils

        sdk_path = arm.utils.get_sdk_path()
        if sdk_path == "":
            self.report({"ERROR"}, "Configure Armory SDK path first")
            return {"CANCELLED"}

        msys2_exe = n64_utils.get_msys2_bash_executable()
        if not msys2_exe:
            self.report({"ERROR"}, "Configure MSYS2 Bash executable path first")
            return {"CANCELLED"}

        tiny3d_path = n64_utils.get_tiny3d_path()
        if not os.path.isdir(tiny3d_path):
            self.report({"ERROR"}, f"tiny3d not found. Clone it into the project's Libraries directory: {n64_utils.get_n64_libraries_dir()}")
            return {"CANCELLED"}

        addon_prefs = arm.utils.get_arm_preferences()
        tiny3d_path_posix = n64_utils.win_path_to_posix(tiny3d_path)
        n64_toolchain_path = n64_utils.win_path_to_posix(addon_prefs.n64_toolchain_path)
        mingw64_path = n64_utils.win_path_to_posix(addon_prefs.mingw64_path)

        print('Building Tiny3D for Nintendo 64, check console for details.')

        env = os.environ.copy()
        env['MSYSTEM'] = 'MINGW64'
        env['N64_INST'] = n64_toolchain_path
        env['PATH'] = f"{n64_toolchain_path}:{mingw64_path}:{env.get('PATH', '')}"

        # Run make commands directly (instead of build.sh) so we can pass
        # CURDIR=. to work around spaces in the project path.  n64.mk uses
        # $(CURDIR) in -ffile-prefix-map which breaks when CWD has spaces.
        build_cmd = (
            f'cd "{tiny3d_path_posix}" && '
            f'make CURDIR=. clean; '
            f'make CURDIR=. -j4 && '
            f'make CURDIR=. install && '
            f'(cd tools/gltf_importer && make CURDIR=. clean); '
            f'(cd tools/gltf_importer && make CURDIR=. -j4) && '
            f'for d in examples/*/; do (cd "$d" && '
            f'make T3D_INST="{n64_toolchain_path}/include" CURDIR=. clean; '
            f'make T3D_INST="{n64_toolchain_path}/include" CURDIR=. -j4) || exit 1; done'
        )
        result = subprocess.run(
            [rf'{msys2_exe}', '--login', '-c', build_cmd],
            stdout=None, stderr=None, text=True, env=env
        )

        if result.returncode != 0:
            self.report({"ERROR"}, "Failed building Tiny3D, check console for details.")
        else:
            self.report({'INFO'}, 'Tiny3D build completed.')

        return {"FINISHED"}


_N64_OPERATOR_CLASSES = [
    N64_OT_InstallLibdragonDependencies,
    N64_OT_BuildLibdragon,
    N64_OT_BuildTiny3d,
]


def _register_operators():
    for cls in _N64_OPERATOR_CLASSES:
        bpy.utils.register_class(cls)
        _registered_classes.append(cls)


def _unregister_operators():
    for cls in reversed(_N64_OPERATOR_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    _registered_classes.clear()


# =============================================================================
# Public API: register() / unregister()
# =============================================================================

def register():
    """Register the N64 exporter library. Called by Armory's load_py_libraries()."""
    # Order matters: namespace first
    _register_n64_namespace()
    _register_operators()
    _register_preferences()
    _register_preferences_ui()

    print('N64 Exporter library registered')


def unregister():
    """Unregister the N64 exporter library. Called by Armory's unload_py_libraries()."""
    _unregister_preferences_ui()
    _unregister_preferences()
    _unregister_operators()
    _unregister_n64_namespace()

    _originals.clear()
    print('N64 Exporter library unregistered')
