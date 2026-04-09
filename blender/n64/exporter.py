"""
N64 Exporter - Orchestrates the N64 export pipeline.

This is the main entry point for exporting Armory projects to N64.
It coordinates the various export modules to generate C code, assets,
and build the final ROM.

Export Modules:
- mesh_exporter: GLTF mesh export
- scene_exporter: Scene data extraction and C generation
- traits_exporter: Trait and autoload C code generation
- ui_exporter: Canvas, fonts, and UI code generation
- physics_exporter: Physics engine file generation
- audio_exporter: Audio asset processing
- build_runner: Makefile generation and build execution
"""

import os
import bpy

import arm
import arm.utils
import arm.log as log

# Use direct module imports to avoid circular imports through arm.n64.__init__
import arm.n64.codegen as codegen
import arm.n64.utils as n64_utils

# Import export submodules directly (not through arm.n64.export.__init__)
from arm.n64.export import mesh_exporter
from arm.n64.export import scene_exporter
from arm.n64.export import traits_exporter
from arm.n64.export import ui_exporter
from arm.n64.export import physics_exporter
from arm.n64.export import audio_exporter
from arm.n64.export import build_runner
from arm.n64.export import linked_export

if arm.is_reload(__name__):
    arm.utils = arm.reload_module(arm.utils)
    log = arm.reload_module(log)
    codegen = arm.reload_module(codegen)
    n64_utils = arm.reload_module(n64_utils)
else:
    arm.enable_reload(__name__)


class N64Exporter:
    """N64 Exporter - Orchestrates export of Armory scenes to N64 C code."""

    # Font size conversion factor: Kha pixel height -> mkfont point size.
    # libdragon's mkfont uses point sizes while Kha uses pixel heights.
    # This factor accounts for the difference (empirically determined to
    # produce visually equivalent text sizes on N64).
    FONT_SIZE_SCALE = 0.82

    def __init__(self):
        # Export state
        self.scene_data = {}
        self.exported_meshes = {}
        self.exported_fonts = {}
        self.font_sizes = set()
        self.trait_info = {}
        self.exported_audio = {}
        self.autoload_info = {}
        self.linked_objects = []  # (local_obj_name, original_mesh_name) tuples

        # Feature flags (set during export)
        self.has_physics = False
        self.has_ui = False
        self.has_audio = False

        # Physics body counts (auto-calculated during scene export)
        self.physics_body_count = 0      # Dynamic bodies (box, sphere, capsule)
        self.mesh_collider_count = 0     # Static mesh colliders
        self.contact_body_count = 0      # Bodies with contact events (triggers)
        self.max_physics_bodies = 0      # Max across all scenes
        self.max_mesh_colliders = 0      # Max across all scenes
        self.max_contact_bodies = 0      # Max across all scenes
        self.max_mesh_triangles = 0      # Max triangles in any single mesh collider

        # UI state
        self.ui_canvas_data = {}
        self.theme_parser = None
        self.color_style_map = {}
        self.font_id_map = {}

        # External blend state (for arm_external_blends_path support)
        self.external_scene_names = []   # Appended scene names (for cleanup)
        self.external_blend_paths = []   # Source .blend file paths
        self.collection_original_names = {}  # {renamed: original} for Blender dedup renames

    # -------------------------------------------------------------------------
    # Class Methods (Entry Points)
    # -------------------------------------------------------------------------

    @classmethod
    def export_project(cls):
        """Export project without building."""
        exporter = cls()
        exporter.export()

    @classmethod
    def publish_project(cls):
        """Export and build project to ROM."""
        exporter = cls()
        exporter.publish()

    @classmethod
    def play_project(cls):
        """Export, build, and run in emulator."""
        exporter = cls()
        exporter.play()

    # -------------------------------------------------------------------------
    # Main Export Pipeline
    # -------------------------------------------------------------------------

    def export(self):
        """Run the complete export pipeline."""
        log.info('Starting N64 export...')

        # Load trait metadata from Haxe macro output
        self.trait_info = codegen.get_trait_info()
        if not self.trait_info.get('traits'):
            log.warn("No traits found in n64_traits.json. Make sure arm_target_n64 is defined during build.")

        # Load external blend scenes (append with suffix naming)
        self._load_external_blends()

        # Phase 0: Prepare linked objects (create temp local copies for Fast64)
        self.linked_objects = linked_export.prepare_linked_for_export()
        if self.linked_objects:
            log.info(f'Prepared {len(self.linked_objects)} linked object(s) for export')

        try:
            # Phase 1: Prepare materials and directories
            self._convert_materials_to_f3d()
            self._make_directories()

            # Phase 2: Export meshes to GLTF/T3D
            mesh_exporter.export_meshes(self)

            # Phase 3: Build scene data (sets has_physics flag)
            for scene in bpy.data.scenes:
                if scene.library:
                    continue
                # Skip temp scene used for linked object export
                if linked_export.is_temp_scene(scene):
                    continue
                # Reset per-scene counters before building scene data
                self.physics_body_count = 0
                self.mesh_collider_count = 0
                self.contact_body_count = 0
                scene_exporter.build_scene_data(self, scene)
                # Track max counts across all scenes
                self.max_physics_bodies = max(self.max_physics_bodies, self.physics_body_count)
                self.max_mesh_colliders = max(self.max_mesh_colliders, self.mesh_collider_count)
                self.max_contact_bodies = max(self.max_contact_bodies, self.contact_body_count)

            # Compute static flags after trait_info is loaded
            n64_utils.compute_static_flags(self.scene_data, self.trait_info)

            # Phase 4: Detect UI canvas (sets has_ui flag)
            ui_exporter.detect_ui_canvas(self)

            # Phase 5: Generate trait code (may set has_ui/has_physics from traits)
            features = traits_exporter.write_traits(self)
            if features.get('has_ui'):
                self.has_ui = True
            if features.get('has_physics'):
                self.has_physics = True

            # Phase 6: Generate autoload code (may set has_audio)
            autoload_features = traits_exporter.write_autoloads(self)
            if autoload_features.get('has_audio'):
                self.has_audio = True

            # Phase 7: Generate engine and system files
            traits_exporter.write_types(self)
            traits_exporter.write_engine(self)
            physics_exporter.write_physics(self)

            # Phase 8: Generate audio files (must be before makefile)
            audio_exporter.scan_and_copy_audio(self)
            if self.has_audio:
                audio_exporter.write_audio_config(self)

            # Phase 9: Generate UI files (must be before makefile)
            ui_exporter.write_fonts(self)

            # Phase 10: Generate Makefile (uses exported_fonts, exported_audio)
            build_runner.write_makefile(self)

            # Phase 11: Generate canvas after fonts
            ui_exporter.write_canvas(self)

            # Phase 12: Generate scene files
            scene_exporter.write_main(self)
            scene_exporter.write_models(self)
            self._write_renderer()
            scene_exporter.write_scenes(self)

            # Phase 13: Generate Iron runtime files
            self._write_iron()
            self._write_signal()
            self._write_time()
            self._write_tween()
            self._write_containers()

            # Phase 14: Cleanup materials
            self._reset_materials_to_bsdf()

        finally:
            # Phase 15: Cleanup linked object temp data (always runs)
            if self.linked_objects:
                linked_export.cleanup_linked_export()
                log.info('Cleaned up linked object temp data')

            # Cleanup external blend scenes
            if self.external_scene_names:
                self._clear_external_blends()
                log.info('Cleaned up external blend data')

        log.info('N64 export completed.')

    def publish(self):
        """Export and build the project."""
        self.export()
        return build_runner.run_make()

    def play(self):
        """Export, build, and run in emulator."""
        if not self.publish():
            return
        build_runner.run_emulator()

    # -------------------------------------------------------------------------
    # Directory Setup
    # -------------------------------------------------------------------------

    def _make_directories(self):
        """Create the N64 build directory structure."""
        build_dir = arm.utils.build_dir()
        dirs = [
            'n64',
            'n64/assets',
            'n64/src',
            'n64/src/data',
            'n64/src/events',
            'n64/src/iron',
            'n64/src/iron/object',
            'n64/src/iron/system',
            'n64/src/oimo',
            'n64/src/scenes',
            'n64/src/system',
            'n64/src/ui',
        ]
        for d in dirs:
            os.makedirs(os.path.join(build_dir, d), exist_ok=True)

    # -------------------------------------------------------------------------
    # External Blend Loading
    # -------------------------------------------------------------------------

    def _load_external_blends(self):
        """Append scenes from external blend files.

        Uses the same arm_external_blends_path World property as Armory's
        Krom/HTML5 pipeline. Scenes are appended (not linked) so their
        local objects can be exported via GLTF and have F3D materials
        converted. Each appended scene is renamed to SceneName_BlendBasename
        to match Armory's naming convention.

        Linked objects within external scenes (instance collections from
        other blend files) are preserved as library references and handled
        by the existing linked_export pipeline — they are NOT duplicated.
        """
        wrd = bpy.data.worlds['Arm']

        if not hasattr(wrd, 'arm_external_blends_path'):
            return

        external_path = getattr(wrd, 'arm_external_blends_path', '')
        if not external_path or not external_path.strip():
            return

        abs_path = bpy.path.abspath(external_path.strip())
        if not os.path.exists(abs_path):
            log.warn(f'External blends path not found: {abs_path}')
            return

        existing_scenes = set(s.name for s in bpy.data.scenes)

        for root, dirs, files in os.walk(abs_path):
            dirs.sort()  # Deterministic directory traversal
            for filename in sorted(files):
                if not filename.endswith('.blend'):
                    continue
                # Skip backup files
                if filename.endswith('.blend1') or filename.endswith('.blend2'):
                    continue

                blend_path = os.path.join(root, filename)
                blend_basename = filename.replace('.blend', '')

                try:
                    # Track existing collections before append to detect renames
                    existing_collections = set(c.name for c in bpy.data.collections)

                    with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
                        data_to.scenes = list(data_from.scenes)

                    self.external_blend_paths.append(blend_path)

                    # Detect Blender dedup renames (e.g. "Gems" → "Gems.001")
                    for coll in bpy.data.collections:
                        if coll.name not in existing_collections:
                            dot_idx = coll.name.rfind('.')
                            if dot_idx > 0:
                                suffix = coll.name[dot_idx + 1:]
                                base = coll.name[:dot_idx]
                                if suffix.isdigit() and len(suffix) >= 3 and base in existing_collections:
                                    self.collection_original_names[coll.name] = base

                    for scn in data_to.scenes:
                        if scn is None:
                            continue
                        # Rename to match Armory convention: SceneName_BlendBasename
                        suffixed_name = scn.name + '_' + blend_basename
                        scn.name = suffixed_name
                        self.external_scene_names.append(scn.name)
                        existing_scenes.add(scn.name)
                        log.info(f'Appended external scene: {scn.name} from {filename}')

                except Exception as e:
                    log.error(f'Failed to load external blend {blend_path}: {e}')

    def _clear_external_blends(self):
        """Remove appended external blend scenes and clean up orphaned data."""
        for scene_name in self.external_scene_names:
            scn = bpy.data.scenes.get(scene_name)
            if scn:
                try:
                    bpy.data.scenes.remove(scn, do_unlink=True)
                except Exception as e:
                    log.error(f'Failed to remove external scene {scene_name}: {e}')

        # Remove orphaned library references from appended data
        for lib in list(bpy.data.libraries):
            try:
                if lib.users == 0:
                    bpy.data.libraries.remove(lib)
            except Exception:
                pass

        try:
            bpy.ops.outliner.orphans_purge(
                do_local_ids=True,
                do_linked_ids=True,
                do_recursive=True
            )
        except Exception:
            pass

        self.external_scene_names = []
        self.external_blend_paths = []

    # -------------------------------------------------------------------------
    # Material Conversion
    # -------------------------------------------------------------------------

    def _convert_materials_to_f3d(self):
        """Convert materials to F3D format (requires Fast64 addon)."""
        try:
            if not hasattr(bpy.ops.scene, 'f3d_convert_to_bsdf'):
                log.warn('Fast64 addon not found - skipping F3D material conversion')
                return False

            bpy.ops.scene.f3d_convert_to_bsdf(
                direction='F3D',
                converter_type='All',
                backup=False,
                put_alpha_into_color=False,
                use_recommended=True,
                lights_for_colors=False,
                default_to_fog=False,
                set_rendermode_without_fog=False
            )
            return True
        except Exception as e:
            log.warn(f'F3D material conversion failed: {e}')
            return False

    def _reset_materials_to_bsdf(self):
        """Reset materials back to BSDF format (requires Fast64 addon)."""
        try:
            if not hasattr(bpy.ops.scene, 'f3d_convert_to_bsdf'):
                return False

            bpy.ops.scene.f3d_convert_to_bsdf(
                direction='BSDF',
                converter_type='All',
                backup=False,
                put_alpha_into_color=False,
                use_recommended=True,
                lights_for_colors=False,
                default_to_fog=False,
                set_rendermode_without_fog=False
            )
            bpy.ops.outliner.orphans_purge(
                do_local_ids=True,
                do_linked_ids=True,
                do_recursive=True
            )
            return True
        except Exception as e:
            log.warn(f'BSDF material reset failed: {e}')
            return False

    # -------------------------------------------------------------------------
    # Static File Copying (Renderer, Iron, System)
    # -------------------------------------------------------------------------

    def _write_renderer(self):
        """Copy renderer files."""
        n64_utils.copy_src('renderer.c', 'src')
        n64_utils.copy_src('renderer.h', 'src')
        n64_utils.copy_src('utils.h', 'src')
        n64_utils.copy_src('render2d.h', 'src')

    def _write_iron(self):
        """Copy Iron runtime files."""
        # Render trait_events.h template
        tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'events', 'trait_events.h.j2')
        out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'events', 'trait_events.h')

        with open(tmpl_path, 'r', encoding='utf-8') as f:
            tmpl_content = f.read()

        max_button_subscribers = n64_utils.get_config('max_button_subscribers', 16)
        output = tmpl_content.format(max_button_subscribers=max_button_subscribers)

        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(output)

        n64_utils.copy_src('events/trait_events.c', 'src')
        n64_utils.copy_src('iron/object/transform.h', 'src')
        n64_utils.copy_src('iron/object/transform.c', 'src')
        n64_utils.copy_src('iron/object/object.h', 'src')
        n64_utils.copy_src('iron/object/object.c', 'src')
        n64_utils.copy_src('iron/object/animation.h', 'src')
        n64_utils.copy_src('iron/object/animation.c', 'src')
        n64_utils.copy_src('iron/system/input.c', 'src')
        n64_utils.copy_src('iron/system/input.h', 'src')

    def _write_signal(self):
        """Copy signal system files."""
        n64_utils.copy_src('signal.c', 'src/system')
        n64_utils.copy_src('signal.h', 'src/system')

    def _write_time(self):
        """Copy time system files."""
        n64_utils.copy_src('time.c', 'src/system')
        n64_utils.copy_src('time.h', 'src/system')

    def _write_tween(self):
        """Copy tween system files."""
        n64_utils.copy_src('tween.c', 'src/system')
        n64_utils.copy_src('tween.h', 'src/system')

    def _write_containers(self):
        """Copy container utility headers (arrays, maps)."""
        n64_utils.copy_src('arm_array.h', 'src/system')
        n64_utils.copy_src('arm_map.h', 'src/system')
