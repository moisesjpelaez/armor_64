"""
N64 Exporter - the export of an Armory project to N64: runs the export modules
in order, then cleans up the .blend. Used by build_pipeline.py (Play/Publish).

Export modules (export/):
- mesh_exporter: the meshes as glTF (tiny3d converts them)
- scene_exporter: the scenes' data and C
- traits_exporter: the C of the Haxe code (traits, autoloads) and engine headers
- ui_exporter: canvases, fonts and the UI's C
- audio_exporter: the sounds
- build_runner: the Makefile, make and the emulator
"""

import os
import bpy

import arm.utils
import arm.log as log

import armor64.codegen as codegen
import armor64.utils as n64_utils

from armor64.export import mesh_exporter
from armor64.export import scene_exporter
from armor64.export import traits_exporter
from armor64.export import ui_exporter
from armor64.export import audio_exporter
from armor64.export import build_runner
from armor64.export import linked_export


class N64Exporter:
    """N64 Exporter - Orchestrates export of Armory scenes to N64 C code."""

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

        self.max_mesh_triangles = 0      # Most triangles of a mesh collider (limits.py)

        # UI state
        self.ui_canvas_data = {}
        self.theme = None                # The Koui themes (KouiThemeParser)
        self.color_style_map = {}
        self.font_id_map = {}
        self.ui_images = set()           # Image names the canvases show (ui_exporter)

        # External blend state (for arm_external_blends_path support)
        self.external_scene_names = []   # Appended scene names (for cleanup)
        self.collection_original_names = {}  # {renamed: original} for Blender dedup renames
        self.libraries_before = None     # Library names before appending them (None: nothing appended)

        self.assets_exported = False
        self.for_emulator = False  # Play: the ROM runs in Ares (debug_config.h)
        self.materials_converted = False
        self.mesh_plan = {}  # glTF file -> {key, entry} (mesh_exporter.plan_meshes)
        self.data_before = None  # The .blend's data before the export (cleanup removes what it adds)

    # -------------------------------------------------------------------------
    # Main Export Pipeline
    # -------------------------------------------------------------------------

    def export(self):
        """Run the complete export pipeline."""
        if not self.assets_exported:
            self.export_assets()
        self.export_code()

    def export_assets(self):
        """The part that only needs the .blend, not Haxe's output (the build runs
        it while Haxe compiles). Leaves the scene prepared for export_code(), or
        cleaned up if it fails."""
        log.info('Starting N64 export...')
        self.data_before = n64_utils.data_snapshot()

        try:
            # External blends' scenes (appended, named as Armory)
            self._load_external_blends()

            # Local copies of linked objects (Fast64 converts their materials)
            self.linked_objects = linked_export.prepare_linked_for_export()
            if self.linked_objects:
                log.info(f'Prepared {len(self.linked_objects)} linked object(s) for export')

            # The F3D materials, if a mesh changed
            self._make_directories()
            if mesh_exporter.plan_meshes(self):
                self.materials_converted = True
                self._convert_materials_to_f3d()

            mesh_exporter.export_meshes(self)
        except Exception:
            self.cleanup()
            raise
        self.assets_exported = True

    def export_code(self):
        """The rest of the export, from Haxe's output (IR) and the prepared scene;
        cleans up the scene at the end."""
        # Load trait metadata from Haxe macro output
        self.trait_info = codegen.get_trait_info()
        if not self.trait_info.get('traits'):
            log.warn("No traits found in n64_traits.json. Make sure arm_target_n64 is defined during build.")

        try:
            # What the game uses: scenes (physics), canvases (UI), the Haxe code
            # (UI, physics, audio), sounds in Assets (audio)
            for scene in scene_exporter.exported_scenes():
                scene_exporter.build_scene_data(self, scene)
            ui_exporter.detect_ui_canvas(self)
            self._compute_limits()
            features = traits_exporter.write_traits(self)
            self.has_ui |= bool(features.get('has_ui'))
            self.has_physics |= bool(features.get('has_physics'))
            self.has_audio |= bool(traits_exporter.write_autoloads(self).get('has_audio'))
            audio_exporter.scan_and_copy_audio(self)

            # The runtime and the files that depend on the features
            n64_utils.copy_runtime(self.has_physics, self.has_ui, self.has_audio)
            traits_exporter.write_types(self)
            traits_exporter.write_limits(self)
            traits_exporter.write_engine(self)
            if self.has_audio:
                audio_exporter.write_audio_config(self)

            # Fonts before the Makefile (their rules) and the canvas (their ids)
            ui_exporter.write_fonts(self)
            build_runner.write_makefile(self)
            ui_exporter.write_canvas(self)

            scene_exporter.write_main(self)
            scene_exporter.write_models(self)
            scene_exporter.write_scenes(self)
        finally:
            self.cleanup()

        log.info('N64 export completed.')

    def _compute_limits(self):
        """The engine limits for this project (codegen/limits.py): self.limits."""
        from armor64.codegen import limits
        from armor64.codegen.trait_generator import load_signals_json, _load_ir_json
        from armor64.codegen.autoload_generator import load_autoloads_json
        traits = self.trait_info.get("traits", {})

        def trait_key(class_name):
            return n64_utils.trait_key(self.trait_info, class_name)
        self.limit_notes = {}
        self.limits = limits.compute(
            scenes=[limits.scene_counts(scene, trait_key, traits) for scene in self.scene_data.values()],
            mesh_triangles=self.max_mesh_triangles,
            ui_canvases=self.ui_canvas_data,
            traits=traits,
            autoloads=load_autoloads_json().get("autoloads", {}),
            signals=load_signals_json().get("signals", {}),
            facts=_load_ir_json("n64_limits.json", "map_keys"),
            notes=self.limit_notes,
        )

    def cleanup(self):
        """Undo export_assets()'s changes to the .blend (also after a failed build):
        the materials back to BSDF, the linked objects' copies and the external
        blends' scenes removed, then the data the export added and nothing uses
        anymore (the .blend's own unused data stays)."""
        if self.materials_converted:
            self._reset_materials_to_bsdf()
            self.materials_converted = False
        if self.linked_objects:
            linked_export.cleanup_linked_export()
            self.linked_objects = []
            log.info('Cleaned up linked object temp data')
        if self.libraries_before is not None:
            self._clear_external_blends()
            log.info('Cleaned up external blend data')
        if self.data_before is not None:
            n64_utils.remove_new_unused_data(self.data_before)
            self.data_before = None

    def publish(self):
        """Export and build the project."""
        self.export()
        return build_runner.run_make()

    def play(self) -> bool:
        """Export, build, and run in emulator."""
        self.for_emulator = True
        if not self.publish():
            return False
        build_runner.run_emulator()
        return True

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
        converted. Each appended scene is renamed to
        SceneName_[Subdirs_]BlendBasename to match Armory's naming convention.

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

        self.libraries_before = {lib.name for lib in bpy.data.libraries}
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
                rel_dir = os.path.relpath(root, abs_path)
                subdirs = [] if rel_dir == os.curdir else rel_dir.split(os.sep)
                blend_basename = '_'.join([*subdirs, filename.replace('.blend', '')])

                try:
                    # Track existing collections before append to detect renames
                    existing_collections = set(c.name for c in bpy.data.collections)

                    with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
                        data_to.scenes = list(data_from.scenes)

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
                        # Rename to match Armory convention: SceneName_[Subdirs_]BlendBasename
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

        # Remove the libraries the append brought in (the external blends and
        # the ones they link): left behind empty, they'd be saved in the file,
        # and Armory's library reload at the next build crashes on them
        for lib in list(bpy.data.libraries):
            if lib.name not in self.libraries_before:
                try:
                    bpy.data.libraries.remove(lib)
                except Exception as e:
                    log.error(f'Failed to remove library {lib.name}: {e}')

        self.external_scene_names = []
        self.libraries_before = None

    # -------------------------------------------------------------------------
    # Material Conversion
    # -------------------------------------------------------------------------

    def _convert_materials_to_f3d(self):
        """Convert the materials to F3D (Fast64), which the models' export reads:
        without it the models would come out without their materials."""
        if not hasattr(bpy.ops.scene, 'f3d_convert_to_bsdf'):
            raise RuntimeError('N64: the Fast64 add-on is needed to convert the materials (enable it)')

        # By name: each F3D material takes its source material's name
        cull_modes = {m.name: getattr(m, 'arm_cull_mode', 'clockwise')
                      for m in bpy.data.materials if not getattr(m, 'is_f3d', False)}
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
        self._apply_armory_culling(cull_modes)

    @staticmethod
    def _apply_armory_culling(cull_modes: dict):
        """Fast64 takes back-face culling from Blender's viewport setting (off by
        default), while Armory draws with the material's Cull Mode: set the
        converted materials' culling from the Cull Mode of their source."""
        for material in bpy.data.materials:
            mode = cull_modes.get(material.name)
            if mode is None or material.library or not getattr(material, 'is_f3d', False):
                continue
            if mode == 'counter_clockwise':
                log.warn(f'Material "{material.name}": Cull Mode "Back" (back faces only) isn\'t '
                         'supported on N64; it is drawn on both sides')
            # Fast64's update callbacks act on the context's material
            with bpy.context.temp_override(material=material):
                material.f3d_mat.rdp_settings.g_cull_back = mode == 'clockwise'

    def _reset_materials_to_bsdf(self):
        """The materials back to BSDF (Fast64). A failure is reported, not raised:
        the cleanup goes on."""
        try:
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
        except Exception as e:
            log.error(f'N64: converting the materials back to BSDF failed ({e}): check them before saving')
