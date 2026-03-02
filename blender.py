"""
N64 Exporter - Standalone Library Integration for Armory3D

This module is loaded by Armory's library system (handlers.py load_py_libraries).
On register(), it monkey-patches the Armory engine to add full N64 support:
  - Registers the arm.n64 Python namespace (pointing to this library's modules)
  - Adds 'Ares' runtime and 'n64' export target
  - Injects N64 preferences into Armory addon settings
  - Patches the build pipeline (make.py, props_ui.py, utils.py)
  - Registers N64 build operators (Install Dependencies, Build libdragon, Build Tiny3D)

On unregister(), all patches are cleanly reversed.
"""

import importlib
import os
import shutil
import subprocess
import sys
import types


def _win_path_to_posix(path):
    """Convert a Windows path to POSIX for MSYS2 (e.g. D:\\foo → /d/foo)."""
    path = os.path.abspath(path).replace('\\', '/')
    # Convert drive letter: D:/... → /d/...
    if len(path) >= 2 and path[1] == ':':
        path = '/' + path[0].lower() + path[2:]
    return path

import bpy
from bpy.props import StringProperty, BoolProperty, EnumProperty

# ---------------------------------------------------------------------------
# Library path (determined at import time — Armory adds our dir to sys.path)
# ---------------------------------------------------------------------------
_LIBRARY_DIR = os.path.dirname(os.path.abspath(__file__))
_BLENDER_DIR = os.path.join(_LIBRARY_DIR, 'blender')
_DEPLOYMENT_DIR = os.path.join(_LIBRARY_DIR, 'Deployment', 'n64')

# ---------------------------------------------------------------------------
# Saved originals for clean unregister
# ---------------------------------------------------------------------------
_originals = {}
_registered_classes = []


# =============================================================================
# arm.n64 Namespace Registration
# =============================================================================

def _register_n64_namespace():
    """Register the library's n64 package into the arm.n64 namespace."""
    import arm

    n64_pkg_dir = os.path.join(_BLENDER_DIR, 'n64')
    codegen_dir = os.path.join(n64_pkg_dir, 'codegen')
    export_dir = os.path.join(n64_pkg_dir, 'export')

    # Ensure the blender dir is on sys.path so relative imports work
    if _BLENDER_DIR not in sys.path:
        sys.path.insert(0, _BLENDER_DIR)

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
    for fname in ['trait_emitter', 'tween_helper', 'autoload_emitter', 'trait_generator', 'autoload_generator', 'scene_generator']:
        fpath = os.path.join(codegen_dir, f'{fname}.py')
        if os.path.isfile(fpath):
            _load_module(f'arm.n64.codegen.{fname}', fpath)

    # Export leaves (order: no-deps first)
    for fname in ['koui_theme_parser', 'linked_export', 'mesh_exporter', 'scene_exporter',
                  'traits_exporter', 'ui_exporter', 'physics_exporter', 'audio_exporter', 'build_runner']:
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

    if _BLENDER_DIR in sys.path:
        sys.path.remove(_BLENDER_DIR)


# =============================================================================
# make_state.is_n64 Flag
# =============================================================================

def _register_state_flag():
    """Add is_n64 flag to arm.make_state."""
    import arm.make_state as state
    if not hasattr(state, 'is_n64'):
        state.is_n64 = False


def _unregister_state_flag():
    """Remove is_n64 flag from arm.make_state."""
    import arm.make_state as state
    if hasattr(state, 'is_n64'):
        delattr(state, 'is_n64')


# =============================================================================
# Runtime Enum Extension (add 'Ares')
# =============================================================================

def _register_runtime_enum():
    """Extend arm_runtime EnumProperty to include 'Ares'."""
    from arm import assets

    # Save current value if it exists
    saved_value = None
    if 'Arm' in bpy.data.worlds:
        try:
            saved_value = bpy.data.worlds['Arm'].arm_runtime
        except Exception:
            pass

    bpy.types.World.arm_runtime = EnumProperty(
        items=[('Krom', 'Krom', 'Krom'),
               ('Browser', 'Browser', 'Browser'),
               ('Ares', 'Ares', 'Ares')],
        name="Runtime",
        description="Runtime to use when launching the game",
        default='Krom',
        update=assets.invalidate_shader_cache
    )

    # Restore previous value
    if saved_value and 'Arm' in bpy.data.worlds:
        try:
            bpy.data.worlds['Arm'].arm_runtime = saved_value
        except Exception:
            pass


def _unregister_runtime_enum():
    """Restore arm_runtime to original 2-item list."""
    from arm import assets

    saved_value = None
    if 'Arm' in bpy.data.worlds:
        try:
            val = bpy.data.worlds['Arm'].arm_runtime
            if val != 'Ares':
                saved_value = val
        except Exception:
            pass

    bpy.types.World.arm_runtime = EnumProperty(
        items=[('Krom', 'Krom', 'Krom'),
               ('Browser', 'Browser', 'Browser')],
        name="Runtime",
        description="Runtime to use when launching the game",
        default='Krom',
        update=assets.invalidate_shader_cache
    )

    if saved_value and 'Arm' in bpy.data.worlds:
        try:
            bpy.data.worlds['Arm'].arm_runtime = saved_value
        except Exception:
            pass


# =============================================================================
# Export Target Enum Extension (add 'n64')
# =============================================================================

def _update_gapi_n64(self, context):
    import arm.utils
    from arm import assets
    n64_build_dir = arm.utils.get_fp_build() + '/n64-build'
    if os.path.isdir(n64_build_dir):
        shutil.rmtree(n64_build_dir, onerror=lambda f, p, e: (os.chmod(p, 0o777), f(p)))
    bpy.data.worlds['Arm'].arm_recompile = True
    assets.invalidate_compiled_data(self, context)


def _register_export_target():
    """Add 'n64' to the project target enum, and add arm_gapi_n64 property.

    Uses direct property assignment on the registered RNA type to avoid
    unregister/re-register, which would destroy existing CollectionProperty instances.
    """
    import arm.props_exporter as props_exporter

    ArmExporterListItem = props_exporter.ArmExporterListItem

    # Read current enum items from the live RNA property
    current_prop = ArmExporterListItem.bl_rna.properties.get('arm_project_target')
    original_items = [(item.identifier, item.name, item.description) for item in current_prop.enum_items]

    # Save original items for clean unregister
    _originals['target_items'] = list(original_items)

    # Save current values from existing export list items
    saved_targets = {}
    if 'Arm' in bpy.data.worlds:
        wrd = bpy.data.worlds['Arm']
        for i, item in enumerate(wrd.arm_exporterlist):
            try:
                saved_targets[i] = item.arm_project_target
            except Exception:
                pass

    # Insert n64 before 'custom' if not already present
    new_items = []
    n64_found = False
    for item in original_items:
        if item[0] == 'n64':
            n64_found = True
        if item[0] == 'custom' and not n64_found:
            new_items.append(('n64', 'Nintendo 64', 'n64'))
        new_items.append(item)

    # Assign directly to the class — no unregister/re-register needed
    ArmExporterListItem.arm_project_target = EnumProperty(
        items=new_items,
        name="Target",
        default='html5',
        description='Build platform'
    )

    # Add arm_gapi_n64
    ArmExporterListItem.arm_gapi_n64 = EnumProperty(
        items=[('tiny3d', 'Tiny3D', 'tiny3d')],
        name="Graphics API",
        default='tiny3d',
        description='Based on currently selected target',
        update=_update_gapi_n64
    )

    # Restore saved target values
    if 'Arm' in bpy.data.worlds:
        wrd = bpy.data.worlds['Arm']
        for i, target in saved_targets.items():
            try:
                wrd.arm_exporterlist[i].arm_project_target = target
            except Exception:
                pass


def _unregister_export_target():
    """Remove 'n64' from project target enum and remove arm_gapi_n64."""
    import arm.props_exporter as props_exporter

    ArmExporterListItem = props_exporter.ArmExporterListItem

    # Save current values, filter out 'n64' since it won't exist after restore
    saved_targets = {}
    if 'Arm' in bpy.data.worlds:
        wrd = bpy.data.worlds['Arm']
        for i, item in enumerate(wrd.arm_exporterlist):
            try:
                val = item.arm_project_target
                if val != 'n64':
                    saved_targets[i] = val
            except Exception:
                pass

    # Restore original enum items
    if 'target_items' in _originals:
        ArmExporterListItem.arm_project_target = EnumProperty(
            items=_originals['target_items'],
            name="Target",
            default='html5',
            description='Build platform'
        )

    # Remove arm_gapi_n64
    try:
        del ArmExporterListItem.arm_gapi_n64
    except Exception:
        pass

    # Restore saved target values
    if 'Arm' in bpy.data.worlds:
        wrd = bpy.data.worlds['Arm']
        for i, target in saved_targets.items():
            try:
                wrd.arm_exporterlist[i].arm_project_target = target
            except Exception:
                pass


# =============================================================================
# Patch arm.utils (get_gapi, target_to_gapi, + N64 helpers)
# =============================================================================

def _register_utils_patches():
    """Patch arm.utils with N64-aware functions and add N64 helper functions."""
    import arm.utils

    # --- Patch get_gapi() ---
    _originals['get_gapi'] = arm.utils.get_gapi

    def _patched_get_gapi():
        import arm.make_state as state
        wrd = bpy.data.worlds['Arm']
        if state.is_export:
            item = wrd.arm_exporterlist[wrd.arm_exporterlist_index]
            if item.arm_project_target == 'n64':
                return 'direct3d11' if arm.utils.get_os() == 'win' else 'opengl'
        if hasattr(wrd, 'arm_runtime') and wrd.arm_runtime == 'Ares':
            return 'direct3d11' if arm.utils.get_os() == 'win' else 'opengl'
        return _originals['get_gapi']()

    arm.utils.get_gapi = _patched_get_gapi

    # --- Patch target_to_gapi() ---
    _originals['target_to_gapi'] = arm.utils.target_to_gapi

    def _patched_target_to_gapi(arm_project_target):
        if arm_project_target == 'n64':
            return 'arm_gapi_n64'
        return _originals['target_to_gapi'](arm_project_target)

    arm.utils.target_to_gapi = _patched_target_to_gapi

    # --- Add N64 helper functions ---
    def get_n64_toolchain_path():
        if os.getenv('N64_INST') is not None:
            return os.getenv('N64_INST')
        addon_prefs = arm.utils.get_arm_preferences()
        return '' if not hasattr(addon_prefs, 'n64_toolchain_path') else _win_path_to_posix(addon_prefs.n64_toolchain_path)

    def get_msys2_bash_executable():
        addon_prefs = arm.utils.get_arm_preferences()
        return '' if not hasattr(addon_prefs, 'msys2_bash_executable') else addon_prefs.msys2_bash_executable

    def get_mingw64_path():
        addon_prefs = arm.utils.get_arm_preferences()
        return '' if not hasattr(addon_prefs, 'mingw64_path') else _win_path_to_posix(addon_prefs.mingw64_path)

    def get_ares_emulator_executable():
        addon_prefs = arm.utils.get_arm_preferences()
        return '' if not hasattr(addon_prefs, 'ares_emulator_executable') else addon_prefs.ares_emulator_executable

    def get_open_n64_rom_directory():
        addon_prefs = arm.utils.get_arm_preferences()
        return False if not hasattr(addon_prefs, 'open_n64_rom_directory') else addon_prefs.open_n64_rom_directory

    def get_n64_deployment_path():
        return _DEPLOYMENT_DIR

    def get_n64_libraries_dir():
        """Return the project's Libraries/ directory path."""
        return os.path.join(arm.utils.get_fp(), 'Libraries')

    def get_libdragon_path():
        """Return the path to the libdragon library (sibling in project Libraries/)."""
        return os.path.join(arm.utils.get_fp(), 'Libraries', 'libdragon')

    def get_tiny3d_path():
        """Return the path to the tiny3d library (sibling in project Libraries/)."""
        return os.path.join(arm.utils.get_fp(), 'Libraries', 'tiny3d')

    arm.utils.get_n64_toolchain_path = get_n64_toolchain_path
    arm.utils.get_msys2_bash_executable = get_msys2_bash_executable
    arm.utils.get_mingw64_path = get_mingw64_path
    arm.utils.get_ares_emulator_executable = get_ares_emulator_executable
    arm.utils.get_open_n64_rom_directory = get_open_n64_rom_directory
    arm.utils.get_n64_deployment_path = get_n64_deployment_path
    arm.utils.get_n64_libraries_dir = get_n64_libraries_dir
    arm.utils.get_libdragon_path = get_libdragon_path
    arm.utils.get_tiny3d_path = get_tiny3d_path


def _unregister_utils_patches():
    """Restore original arm.utils functions and remove N64 helpers."""
    import arm.utils

    if 'get_gapi' in _originals:
        arm.utils.get_gapi = _originals['get_gapi']
    if 'target_to_gapi' in _originals:
        arm.utils.target_to_gapi = _originals['target_to_gapi']

    for attr in ['get_n64_toolchain_path', 'get_msys2_bash_executable', 'get_mingw64_path',
                 'get_ares_emulator_executable', 'get_open_n64_rom_directory', 'get_n64_deployment_path',
                 'get_n64_libraries_dir', 'get_libdragon_path', 'get_tiny3d_path']:
        if hasattr(arm.utils, attr):
            delattr(arm.utils, attr)


# =============================================================================
# Patch arm.make (runtime_to_target, build_success)
# =============================================================================

def _register_make_patches():
    """Patch arm.make to support N64 build pipeline."""
    import arm.make as make

    # --- Patch runtime_to_target() ---
    _originals['runtime_to_target'] = make.runtime_to_target

    def _patched_runtime_to_target():
        wrd = bpy.data.worlds['Arm']
        if wrd.arm_runtime == 'Ares':
            return 'custom'
        return _originals['runtime_to_target']()

    make.runtime_to_target = _patched_runtime_to_target

    # --- Patch build_success() ---
    _originals['build_success'] = make.build_success

    def _patched_build_success():
        import arm.make_state as state
        import arm.log as log
        from arm.n64.exporter import N64Exporter

        if state.is_n64:
            log.clear()
            state.is_n64 = False
            if state.is_play:
                N64Exporter.play_project()
            elif state.is_publish:
                N64Exporter.publish_project()
                if arm.utils.get_open_n64_rom_directory():
                    arm.utils.open_folder(os.path.abspath(arm.utils.build_dir() + '/n64'))
            else:
                N64Exporter.export_project()
                if arm.utils.get_open_n64_rom_directory():
                    arm.utils.open_folder(os.path.abspath(arm.utils.build_dir() + '/n64'))
            return
        _originals['build_success']()

    make.build_success = _patched_build_success


def _unregister_make_patches():
    """Restore original arm.make functions."""
    import arm.make as make

    if 'runtime_to_target' in _originals:
        make.runtime_to_target = _originals['runtime_to_target']
    if 'build_success' in _originals:
        make.build_success = _originals['build_success']


# =============================================================================
# Patch Play / Export / Publish Operators
# =============================================================================

def _register_operator_patches():
    """Patch Play/Export/Publish operators to support N64."""
    import arm.props_ui as props_ui

    # --- Patch ArmoryPlayButton.execute ---
    _originals['play_execute'] = props_ui.ArmoryPlayButton.execute

    def _patched_play_execute(self, context):
        import arm.make_state as state
        wrd = bpy.data.worlds['Arm']
        if wrd.arm_runtime == 'Ares':
            state.is_n64 = True
            if not wrd.arm_cache_build:
                bpy.ops.arm.clean_project()
        return _originals['play_execute'](self, context)

    props_ui.ArmoryPlayButton.execute = _patched_play_execute

    # --- Patch ArmoryBuildProjectButton.execute ---
    _originals['build_execute'] = props_ui.ArmoryBuildProjectButton.execute

    def _patched_build_execute(self, context):
        import arm.make_state as state
        import arm.make as make
        from arm import assets
        wrd = bpy.data.worlds['Arm']
        item = wrd.arm_exporterlist[wrd.arm_exporterlist_index]
        if item.arm_project_target == 'n64':
            state.is_n64 = True
            if not wrd.arm_cache_build:
                bpy.ops.arm.clean_project()

            # We need to override the target to 'custom' for Haxe macro execution.
            # Save the original target, set to custom, call the build pipeline, then restore.
            if item.arm_project_scene is None:
                item.arm_project_scene = context.scene
            rplist_index = wrd.arm_rplist_index
            for i in range(0, len(wrd.arm_rplist)):
                if wrd.arm_rplist[i].name == item.arm_project_rp:
                    wrd.arm_rplist_index = i
                    break

            assets.invalidate_shader_cache(None, None)
            assets.invalidate_enabled = False
            if wrd.arm_clear_on_compile:
                os.system("cls")
            make.build('custom', is_export=True)
            make.compile()
            wrd.arm_rplist_index = rplist_index
            assets.invalidate_enabled = True
            return {'FINISHED'}

        return _originals['build_execute'](self, context)

    props_ui.ArmoryBuildProjectButton.execute = _patched_build_execute

    # --- Patch ArmoryPublishProjectButton.execute ---
    _originals['publish_execute'] = props_ui.ArmoryPublishProjectButton.execute

    def _patched_publish_execute(self, context):
        import arm.make_state as state
        import arm.make as make
        from arm import assets
        wrd = bpy.data.worlds['Arm']
        item = wrd.arm_exporterlist[wrd.arm_exporterlist_index]
        if item.arm_project_target == 'n64':
            state.is_n64 = True
            if not wrd.arm_cache_build:
                bpy.ops.arm.clean_project()

            if item.arm_project_scene is None:
                item.arm_project_scene = context.scene
            rplist_index = wrd.arm_rplist_index
            for i in range(0, len(wrd.arm_rplist)):
                if wrd.arm_rplist[i].name == item.arm_project_rp:
                    wrd.arm_rplist_index = i
                    break

            make.clean()
            assets.invalidate_enabled = False
            if wrd.arm_clear_on_compile:
                os.system("cls")
            make.build('custom', is_publish=True, is_export=True)
            make.compile()
            wrd.arm_rplist_index = rplist_index
            assets.invalidate_enabled = True
            return {'FINISHED'}

        return _originals['publish_execute'](self, context)

    props_ui.ArmoryPublishProjectButton.execute = _patched_publish_execute


def _unregister_operator_patches():
    """Restore original operator execute methods."""
    import arm.props_ui as props_ui

    if 'play_execute' in _originals:
        props_ui.ArmoryPlayButton.execute = _originals['play_execute']
    if 'build_execute' in _originals:
        props_ui.ArmoryBuildProjectButton.execute = _originals['build_execute']
    if 'publish_execute' in _originals:
        props_ui.ArmoryPublishProjectButton.execute = _originals['publish_execute']


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
                          'ares_emulator_executable', 'open_n64_rom_directory']:
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
                      'ares_emulator_executable', 'open_n64_rom_directory']:
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
        import arm.utils

        sdk_path = arm.utils.get_sdk_path()
        if sdk_path == "":
            self.report({"ERROR"}, "Configure Armory SDK path first")
            return {"CANCELLED"}

        msys2_exe = arm.utils.get_msys2_bash_executable()
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
        import arm.utils

        sdk_path = arm.utils.get_sdk_path()
        if sdk_path == "":
            self.report({"ERROR"}, "Configure Armory SDK path first")
            return {"CANCELLED"}

        msys2_exe = arm.utils.get_msys2_bash_executable()
        if not msys2_exe:
            self.report({"ERROR"}, "Configure MSYS2 Bash executable path first")
            return {"CANCELLED"}

        libdragon_path = arm.utils.get_libdragon_path()
        if not os.path.isdir(libdragon_path):
            self.report({"ERROR"}, f"libdragon not found. Clone it into the project's Libraries directory: {arm.utils.get_n64_libraries_dir()}")
            return {"CANCELLED"}

        addon_prefs = arm.utils.get_arm_preferences()
        libdragon_path_posix = _win_path_to_posix(libdragon_path)
        n64_toolchain_path = _win_path_to_posix(addon_prefs.n64_toolchain_path)
        mingw64_path = _win_path_to_posix(addon_prefs.mingw64_path)

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
        import arm.utils

        sdk_path = arm.utils.get_sdk_path()
        if sdk_path == "":
            self.report({"ERROR"}, "Configure Armory SDK path first")
            return {"CANCELLED"}

        msys2_exe = arm.utils.get_msys2_bash_executable()
        if not msys2_exe:
            self.report({"ERROR"}, "Configure MSYS2 Bash executable path first")
            return {"CANCELLED"}

        tiny3d_path = arm.utils.get_tiny3d_path()
        if not os.path.isdir(tiny3d_path):
            self.report({"ERROR"}, f"tiny3d not found. Clone it into the project's Libraries directory: {arm.utils.get_n64_libraries_dir()}")
            return {"CANCELLED"}

        addon_prefs = arm.utils.get_arm_preferences()
        tiny3d_path_posix = _win_path_to_posix(tiny3d_path)
        n64_toolchain_path = _win_path_to_posix(addon_prefs.n64_toolchain_path)
        mingw64_path = _win_path_to_posix(addon_prefs.mingw64_path)

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
            f'make CURDIR=. clean 2>/dev/null; '
            f'make CURDIR=. -j4 && '
            f'make CURDIR=. install && '
            f'(cd tools/gltf_importer && make CURDIR=. clean 2>/dev/null); '
            f'(cd tools/gltf_importer && make CURDIR=. -j4) && '
            f'for d in examples/*/; do (cd "$d" && make T3D_INST="{n64_toolchain_path}/include" CURDIR=. -j4) || exit 1; done'
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
    # Order matters: namespace first, then state flag, then patches
    _register_n64_namespace()
    _register_state_flag()
    _register_runtime_enum()
    _register_export_target()
    _register_utils_patches()
    _register_make_patches()
    _register_operator_patches()
    _register_operators()
    _register_preferences()
    _register_preferences_ui()

    print('N64 Exporter library registered')


def unregister():
    """Unregister the N64 exporter library. Called by Armory's unload_py_libraries()."""
    _unregister_preferences_ui()
    _unregister_preferences()
    _unregister_operators()
    _unregister_operator_patches()
    _unregister_make_patches()
    _unregister_utils_patches()
    _unregister_export_target()
    _unregister_runtime_enum()
    _unregister_state_flag()
    _unregister_n64_namespace()

    _originals.clear()
    print('N64 Exporter library unregistered')
