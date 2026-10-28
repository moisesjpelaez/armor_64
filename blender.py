"""
N64 Exporter - Standalone Library Integration for Armory3D

This module is loaded by Armory's library system (handlers.py load_py_libraries).
On register():
  - Registers the armor64 Python package (exporter/)
  - Registers N64 build operators (Install Dependencies, Build libdragon, Build Tiny3D)
The builds themselves ("Ares" runtime, "Nintendo 64" target) go through
Armory's library hooks: see armory_hooks.py.

On unregister(), all of it is cleanly reversed.
"""

import importlib.util
import os
import subprocess
import sys


import bpy

# ---------------------------------------------------------------------------
# Library path (determined at import time — Armory adds our dir to sys.path)
# ---------------------------------------------------------------------------
_LIBRARY_DIR = os.path.dirname(os.path.abspath(__file__))
_EXPORTER_DIR = os.path.join(_LIBRARY_DIR, 'exporter')

_registered_classes = []


# =============================================================================
# armor64 Package Registration
# =============================================================================

PACKAGE = 'armor64'


def _register_package():
    """Register the library's Python code (exporter/) as the `armor64` package;
    its modules are imported when used."""
    spec = importlib.util.spec_from_file_location(
        PACKAGE, os.path.join(_EXPORTER_DIR, '__init__.py'),
        submodule_search_locations=[_EXPORTER_DIR])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)


def _unregister_package():
    """Forget the package and its modules (loaded again on the next register)."""
    for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + '.')]:
        del sys.modules[name]


# =============================================================================
# N64 Build Operators
# =============================================================================

class N64_OT_InstallLibdragonDependencies(bpy.types.Operator):
    """Install libdragon dependencies via pacman"""
    bl_idname = "n64_exporter.install_libdragon_dependencies"
    bl_label = "Install Dependencies"
    bl_description = "Install libdragon dependencies (base-devel, mingw-w64-x86_64-gcc, mingw-w64-x86_64-make, git)"

    def execute(self, context):
        import armor64.utils as n64_utils
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
        import armor64.utils as n64_utils
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
        import armor64.utils as n64_utils
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
            f'(cd tools/gltf_importer && make CURDIR=. -j4 && make CURDIR=. install) && '
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
    # Order matters: the package first
    _register_package()
    _register_operators()

    print('N64 Exporter library registered')


def unregister():
    """Unregister the N64 exporter library. Called by Armory's unload_py_libraries()."""
    _unregister_operators()
    _unregister_package()

    print('N64 Exporter library unregistered')
