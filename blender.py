"""
armor_64's Blender side, loaded by Armory's library system
(handlers.py load_py_libraries()). register():
  - the `armor64` Python package (exporter/)
  - the operators of the Nintendo 64 preferences: Install Dependencies (MSYS2
    packages), Build libdragon, Build Tiny3D
unregister() removes both. The builds themselves ("Ares" runtime, "Nintendo
64" target) go through Armory's library hooks: see armory_hooks.py.
"""

import importlib.util
import os
import sys

import bpy

_LIBRARY_DIR = os.path.dirname(os.path.abspath(__file__))
_EXPORTER_DIR = os.path.join(_LIBRARY_DIR, 'exporter')


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
# Operators: MSYS2 packages, libdragon and tiny3d
# =============================================================================

def _run_msys2(operator, task: str, script: str):
    """Run `script` in MSYS2 as Armory's build process: in the background (the
    output in the console), Stop ends it; the result is logged when it ends."""
    import arm.library_hooks as library_hooks
    import arm.log as log
    import arm.make_state as state
    import armor64.utils as n64_utils

    if state.proc_build is not None:
        operator.report({'ERROR'}, 'A build is running: wait for it to end, or Stop it')
        return {'CANCELLED'}
    cmd = n64_utils.msys2_command(script)
    if cmd is None:
        operator.report({'ERROR'}, 'Set the MSYS2 Bash Executable in the Nintendo 64 settings')
        return {'CANCELLED'}

    def done(code: int):
        if code == 0:
            log.info(f'N64: {task} done')
        else:
            log.error(f'N64: {task} failed (exit code {code}), see the console')

    log.info(f'N64: {task}, see the console')
    library_hooks.run_process(cmd, done)
    return {'FINISHED'}


def _library_path(operator, name: str, path: str):
    """The library's MSYS2 path, None if it isn't there (reported)."""
    import armor64.utils as n64_utils
    if not os.path.isdir(path):
        operator.report({'ERROR'}, f"{name} not found. Clone it into the project's Libraries directory: "
                                   f"{n64_utils.get_n64_libraries_dir()}")
        return None
    return n64_utils.win_path_to_posix(path)


class N64_OT_InstallLibdragonDependencies(bpy.types.Operator):
    """Install libdragon dependencies via pacman"""
    bl_idname = "n64_exporter.install_libdragon_dependencies"
    bl_label = "Install Dependencies"
    bl_description = "Install libdragon dependencies (base-devel, mingw-w64-x86_64-gcc, mingw-w64-x86_64-make, git)"

    def execute(self, context):
        return _run_msys2(self, 'installing the libdragon dependencies',
                          'pacman -S --needed --noconfirm base-devel mingw-w64-x86_64-gcc mingw-w64-x86_64-make git')


class N64_OT_BuildLibdragon(bpy.types.Operator):
    """Build libdragon for Nintendo 64"""
    bl_idname = "n64_exporter.build_libdragon"
    bl_label = "Build libdragon"
    bl_description = "Build libdragon, its tools and examples (what changed, or everything with Rebuild From Scratch)"

    def execute(self, context):
        import armor64.utils as n64_utils
        path = _library_path(self, 'libdragon', n64_utils.get_libdragon_path())
        if path is None:
            return {'CANCELLED'}
        # make's own commands instead of build.sh: CURDIR=. works around spaces
        # in the project's path (n64.mk uses $(CURDIR) in -ffile-prefix-map)
        make = f'make CURDIR=. -j{n64_utils.jobs()}'
        clean = f'{make} clobber && ' if n64_utils.get_rebuild_libraries() else ''
        return _run_msys2(self, 'building libdragon', (
            f'cd "{path}" && {make} install-mk && {clean}'
            f'{make} libdragon tools && {make} install tools-install && {make} examples'))


class N64_OT_BuildTiny3d(bpy.types.Operator):
    """Build Tiny3D for Nintendo 64"""
    bl_idname = "n64_exporter.build_tiny3d"
    bl_label = "Build Tiny3D"
    bl_description = "Build Tiny3D, its glTF importer and examples (what changed, or everything with Rebuild From Scratch)"

    def execute(self, context):
        import armor64.utils as n64_utils
        path = _library_path(self, 'tiny3d', n64_utils.get_tiny3d_path())
        if path is None:
            return {'CANCELLED'}
        make = f'make CURDIR=. -j{n64_utils.jobs()}'
        t3d_inst = f'T3D_INST="{n64_utils.get_n64_toolchain_path()}/include"'
        rebuild = n64_utils.get_rebuild_libraries()

        def build(make_cmd: str) -> str:
            # Grouped: a failed `cd` before it stops the chain
            return f'({make_cmd} clean; {make_cmd})' if rebuild else make_cmd

        return _run_msys2(self, 'building tiny3d', (
            f'cd "{path}" && {build(make)} && make CURDIR=. install && '
            f'(cd tools/gltf_importer && {build(make)} && make CURDIR=. install) && '
            f'for d in examples/*/; do (cd "$d" && {build(f"{make} {t3d_inst}")}) || exit 1; done'))


_N64_OPERATOR_CLASSES = [
    N64_OT_InstallLibdragonDependencies,
    N64_OT_BuildLibdragon,
    N64_OT_BuildTiny3d,
]


def _register_operators():
    for cls in _N64_OPERATOR_CLASSES:
        bpy.utils.register_class(cls)


def _unregister_operators():
    for cls in reversed(_N64_OPERATOR_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:  # Not registered (a failed register)
            pass


# =============================================================================
# Public API: register() / unregister()
# =============================================================================

def register():
    """Called by Armory's load_py_libraries()."""
    # The package first: the operators import it
    _register_package()
    _register_operators()
    print('N64 Exporter library registered')


def unregister():
    """Called by Armory's unload_py_libraries()."""
    _unregister_operators()
    _unregister_package()
    print('N64 Exporter library unregistered')
