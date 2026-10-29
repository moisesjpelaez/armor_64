"""Mesh Exporter - Handles GLTF mesh export for N64.

This module exports Blender meshes to GLTF format for subsequent
conversion to N64 model format (T3D).

All meshes are exported as GLTF_SEPARATE (.gltf + .bin) for consistency
and debuggability. For skinned meshes, additional export options are set:
  - export_apply=False  (preserve skin deformation data)
  - export_skins=True   (include joint/weight data)
  - export_animations=True (include animation clips)
  - Both mesh AND armature objects are selected for export

A mesh whose export inputs didn't change since the last export isn't
exported again (assets/.mesh_cache.json, see _mesh_key); when none changed,
the materials aren't even converted to F3D (plan_meshes).
"""

import hashlib
import json
import os
import sys

import bpy
import numpy as np

import arm.utils
import arm.log as log
import arm.linked_utils as linked_utils
import armor64.utils as n64_utils
from armor64.export import linked_export, scene_exporter


# ---------------------------------------------------------------------------
# Skinned mesh helpers
# ---------------------------------------------------------------------------

def _find_armature(obj):
    """Find armature for a mesh via modifier or parent."""
    if obj.type != 'MESH':
        return None
    for mod in obj.modifiers:
        if mod.type == 'ARMATURE' and mod.object:
            return mod.object
    if obj.parent and obj.parent.type == 'ARMATURE':
        return obj.parent
    return None


def _ensure_uvmap_layer(mesh_data):
    """Ensure mesh has a UV layer named 'UVMap' for Fast64 compatibility.

    Fast64's GLTF hook requires a UV layer literally named "UVMap".
    If the mesh has UV layers but none with that name, temporarily rename
    the first one. Returns the original name to restore, or None.
    """
    if not mesh_data or not mesh_data.uv_layers:
        return None
    for layer in mesh_data.uv_layers:
        if layer.name == "UVMap":
            return None  # already exists
    # Rename first UV layer
    original_name = mesh_data.uv_layers[0].name
    mesh_data.uv_layers[0].name = "UVMap"
    return original_name


def _is_bone_fcurve(fc, bone_names):
    """Check if an fcurve targets a valid bone."""
    dp = fc.data_path
    if not dp.startswith('pose.bones['):
        return False
    try:
        name = dp.split('"')[1] if '"' in dp else dp.split("'")[1]
        return name in bone_names
    except (IndexError, KeyError):
        return False


def _prepare_export_actions(armature_obj):
    """Prepare all actions for a clean GLTF ACTIONS-mode export.

    Two things happen:
    1. The armature's own actions get cleaned copies (non-bone fcurves
       stripped) so gltf_to_t3d won't crash on object-level channels.
    2. Bone fcurves in OTHER actions that target bones NOT on this
       armature get temporarily mangled so the GLTF exporter skips them
       (prevents cross-armature action pollution and warnings).

    Returns a restore() callable that undoes all modifications.
    """
    if not armature_obj or not armature_obj.data:
        return lambda: None
    bone_names = set(armature_obj.data.bones.keys())
    if not bone_names:
        return lambda: None

    stashed_paths = []      # (fcurve, original_data_path)
    replaced_refs = []      # (holder, attr, original_action)
    clean_action_names = [] # temp copy names to remove on restore

    # --- Clean the armature's own actions (create copies without bad fcurves) ---
    own_action_ids = set()
    anim_data = armature_obj.animation_data
    if anim_data:
        refs = {}
        if anim_data.action:
            own_action_ids.add(id(anim_data.action))
            refs.setdefault(id(anim_data.action),
                            (anim_data.action, []))[1].append((anim_data, 'action'))
        for track in (anim_data.nla_tracks or []):
            for strip in track.strips:
                if strip.action:
                    own_action_ids.add(id(strip.action))
                    refs.setdefault(id(strip.action),
                                    (strip.action, []))[1].append((strip, 'action'))

        for _, (action, holders) in refs.items():
            bad = [fc for fc in action.fcurves if not _is_bone_fcurve(fc, bone_names)]
            if not bad:
                continue
            clean = action.copy()
            clean.name = f"_armory_clean_{action.name}"
            clean_action_names.append(clean.name)
            for fc in list(clean.fcurves):
                if not _is_bone_fcurve(fc, bone_names):
                    clean.fcurves.remove(fc)
            for holder, attr in holders:
                replaced_refs.append((holder, attr, action))
                setattr(holder, attr, clean)
            log.info(f'Cleaned {len(bad)} non-bone fcurve(s) from "{action.name}"')

    # --- Stash foreign bone paths in all other actions ---
    # GLTF ACTIONS mode iterates bpy.data.actions and tries every action
    # that has pose.bones channels.  Mangle paths for bones not on this
    # armature so the exporter won't attempt (and warn about) them.
    for action in bpy.data.actions:
        if id(action) in own_action_ids or action.name.startswith('_armory_clean_'):
            continue
        for fc in action.fcurves:
            if (fc.data_path.startswith('pose.bones[')
                    and not _is_bone_fcurve(fc, bone_names)):
                stashed_paths.append((fc, fc.data_path))
                fc.data_path = f"_x_{fc.data_path}"

    if stashed_paths:
        log.info(f'Stashed {len(stashed_paths)} foreign bone fcurve(s) during export')

    def restore():
        for fc, path in stashed_paths:
            fc.data_path = path
        for holder, attr, orig in replaced_refs:
            setattr(holder, attr, orig)
        for name in clean_action_names:
            a = bpy.data.actions.get(name)
            if a:
                bpy.data.actions.remove(a)

    return restore


def _get_animation_names(armature_obj):
    """Extract animation clip names from an armature's actions/NLA tracks.

    Returns list of action names that will appear in the exported GLTF.
    Blender's GLTF exporter uses NLA strips when present, else active action.
    """
    names = []
    if not armature_obj or not armature_obj.animation_data:
        return names

    anim_data = armature_obj.animation_data

    # NLA tracks: each strip's action becomes a separate GLTF animation
    if anim_data.nla_tracks:
        for track in anim_data.nla_tracks:
            for strip in track.strips:
                if strip.action and strip.action.name not in names:
                    names.append(strip.action.name)

    # Active action (if not already collected via NLA)
    if anim_data.action and anim_data.action.name not in names:
        names.append(anim_data.action.name)

    # Fallback: scan all actions for bone channels matching this armature
    if not names:
        bone_names = set(armature_obj.data.bones.keys()) if armature_obj.data else set()
        for action in bpy.data.actions:
            has_bone = any(
                fc.data_path.startswith('pose.bones[')
                for fc in action.fcurves
            )
            if has_bone and action.name not in names:
                names.append(action.name)

    return names


def _export_mesh_to_gltf(obj, output_path, armature=None) -> list:
    """Export a mesh to GLTF_SEPARATE format; returns the files written.

    Replicates the same settings as a manual File > Export > glTF 2.0.
    If armature is provided, includes skin/animation data.
    """
    is_skinned = armature is not None
    view_layer = bpy.context.view_layer
    export_objects = {obj} | ({armature} if armature else set())

    # Everything changed here is undone in `finally`, also when the export fails
    restore_actions = None
    uv_orig_name = None
    hidden_objects = []
    transforms = {}  # object -> (location, rotation, scale)
    arm_parent_orig = None
    try:
        # --- Prepare actions (clean own + stash foreign) ---
        if armature:
            restore_actions = _prepare_export_actions(armature)

        # --- Ensure UV layer named "UVMap" for Fast64 ---
        uv_orig_name = _ensure_uvmap_layer(obj.data)

        # --- Hide every other object so GLTF exporter only sees ours ---
        # use_selection=True still follows parent/child chains and can pull in
        # unintended objects from the same scene. Hiding everything else avoids
        # any cross-contamination between models sharing the same temp scene.
        for o in view_layer.objects:
            if o not in export_objects and not o.hide_viewport:
                o.hide_viewport = True
                hidden_objects.append(o)

        # Temporarily unparent armature so the GLTF exporter doesn't
        # traverse up to parent objects (e.g. collider mesh) which causes
        # duplicated skeleton nodes and crashes gltf_to_t3d.
        if armature and armature.parent:
            arm_parent_orig = (armature.parent, armature.matrix_parent_inverse.copy())
            armature.parent = None

        # Exported at the origin
        for o in export_objects:
            transforms[o] = (o.location.copy(), o.rotation_euler.copy(), o.scale.copy())
            o.location = (0, 0, 0)
            o.rotation_euler = (0, 0, 0)
            o.scale = (1, 1, 1)

        bpy.ops.object.select_all(action='DESELECT')
        view_layer.update()

        obj.select_set(True)
        if armature:
            armature.select_set(True)
            view_layer.objects.active = armature

        return _gltf_export(output_path, is_skinned)
    finally:
        for o, (location, rotation, scale) in transforms.items():
            o.location, o.rotation_euler, o.scale = location, rotation, scale
        if arm_parent_orig:
            armature.parent, armature.matrix_parent_inverse = arm_parent_orig
        for o in hidden_objects:
            o.hide_viewport = False
        view_layer.update()
        if uv_orig_name is not None and obj.data and obj.data.uv_layers:
            for layer in obj.data.uv_layers:
                if layer.name == "UVMap":
                    layer.name = uv_orig_name
                    break
        if restore_actions:
            restore_actions()


def _gltf_export(output_path, is_skinned):
    """Export the selection to output_path; returns the files written."""
    # Exported to a staging folder, then only the files whose content changed
    # replace those in assets/ (make converts a model again only when it changed)
    staging = os.path.join(os.path.dirname(output_path), '.export')
    os.makedirs(staging, exist_ok=True)
    for old in os.listdir(staging):
        os.remove(os.path.join(staging, old))

    # --- Same settings as manual export ---
    bpy.ops.export_scene.gltf(
        filepath=os.path.join(staging, os.path.basename(output_path)),
        export_format='GLTF_SEPARATE',
        use_selection=True,
        export_yup=True,
        export_apply=not is_skinned,
        export_extras=True,              # F3D material custom properties
        export_skins=is_skinned,
        export_animations=is_skinned,
        export_morph=False,
        export_def_bones=is_skinned,     # only deform bones (skip IK helpers)
        export_animation_mode='ACTIONS',
    )
    files = sorted(os.listdir(staging))
    for file in files:
        staged = os.path.join(staging, file)
        if file.endswith('.gltf'):
            _stable_gltf(staged)
        n64_utils.copy_if_changed(staged, os.path.join(os.path.dirname(output_path), file))
        os.remove(staged)
    return files


def _mesh_key(obj, conversion_scene):
    """Hash of everything the glTF export of `obj` depends on, read before the
    F3D conversion and the export's preparation (both are functions of it), or
    None if something can't be hashed (then it's exported). The same key means
    the same glTF files."""
    from io_scene_gltf2.blender.com import extras

    def generate_extras(element):  # As written in the glTF (_stable_gltf)
        values = extras.generate_extras(element)
        if values:
            values.pop(DROPPED_EXTRA, None)
        return values

    def scene_info(scene):  # Not written (_stable_gltf), but Fast64's settings there change the output
        return [scene.name, _without_empty(generate_extras(scene)),
                _without_empty(generate_extras(scene.world)) if scene.world else None]

    def material_info(material):
        return [material.name, generate_extras(material), _rna_values(material, {DROPPED_EXTRA}),
                _tree_info(material.node_tree)]

    try:
        if obj.constraints:
            raise ValueError('constraints')
        h = hashlib.sha1()
        h.update(_KEY_SALT.encode())
        parent = obj.parent
        info = {
            # Exported at the origin: only the parent's transform is left
            'object': [obj.name, obj.hide_viewport, generate_extras(obj), None if parent is None else [
                obj.parent_type, obj.parent_bone, parent.name, parent.matrix_world, obj.matrix_parent_inverse]],
            'mesh': [obj.data.name, generate_extras(obj.data)],
            'scene': [scene_info(bpy.context.scene), scene_info(conversion_scene)],
            'materials': [None if slot.material is None else material_info(slot.material)
                          for slot in obj.material_slots],
        }
        h.update(json.dumps(info, sort_keys=True, default=_key_value).encode())
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        try:
            _hash_mesh(h, mesh)
        finally:
            evaluated.to_mesh_clear()
        return h.hexdigest()
    except Exception as e:
        log.info(f'Mesh cache: no key for {obj.name} ({e}), exporting it')
        return None


# Attribute data types: (foreach_get property, values per item, numpy type)
_ATTRIBUTE_ARRAYS = {
    'FLOAT': ('value', 1, np.float32), 'INT': ('value', 1, np.int32), 'INT8': ('value', 1, np.int32),
    'BOOLEAN': ('value', 1, bool), 'FLOAT2': ('vector', 2, np.float32), 'FLOAT_VECTOR': ('vector', 3, np.float32),
    'INT16_2D': ('value', 2, np.int32), 'INT32_2D': ('value', 2, np.int32), 'FLOAT_COLOR': ('color', 4, np.float32),
    'BYTE_COLOR': ('color', 4, np.float32), 'QUATERNION': ('value', 4, np.float32), 'FLOAT4X4': ('value', 16, np.float32),
}


def _hash_mesh(h, mesh):
    """The exported mesh: topology, normals and every attribute (positions,
    UVs, colors, material indices...), with the active layers' names."""
    def add(collection, prop, size, dtype):
        values = np.empty(len(collection) * size, dtype=dtype)
        collection.foreach_get(prop, values)
        h.update(values.tobytes())

    add(mesh.loops, 'vertex_index', 1, np.int32)
    add(mesh.polygons, 'loop_start', 1, np.int32)
    add(mesh.corner_normals, 'vector', 3, np.float32)
    for attribute in sorted(mesh.attributes, key=lambda a: a.name):
        prop, size, dtype = _ATTRIBUTE_ARRAYS[attribute.data_type]  # Unknown type: no key
        h.update(f'{attribute.name}/{attribute.domain}/{attribute.data_type}'.encode())
        add(attribute.data, prop, size, dtype)
    uv = mesh.uv_layers
    h.update(json.dumps([[l.name for l in uv], uv.active.name if uv.active else None,
                         [l.name for l in uv if l.active_render], mesh.color_attributes.active_color_name,
                         mesh.color_attributes.render_color_index]).encode())


def _without_empty(value):
    """`value` without empty groups: a property group is created empty the first
    time some code reads it, and empty means all defaults, like missing."""
    if isinstance(value, dict):
        pruned = {k: _without_empty(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v != {}}
    return value


def _rna_values(struct, skip=()):
    """The settable non-pointer RNA values of `struct` (blend mode, culling...)."""
    values = {}
    for prop in struct.bl_rna.properties:
        if prop.is_readonly or prop.type in ('POINTER', 'COLLECTION') or prop.identifier in skip:
            continue
        values[prop.identifier] = _plain(getattr(struct, prop.identifier))
    return values


def _plain(value):
    """Arrays (colors, vectors...) as lists."""
    return list(value) if hasattr(value, '__len__') and not isinstance(value, str) else value


# Node settings that only change how the node editor shows it
_NODE_UI = {'location', 'location_absolute', 'width', 'height', 'select', 'hide', 'show_options',
            'show_preview', 'show_texture', 'color', 'use_custom_color', 'label', 'warning_propagation'}


def _tree_info(tree):
    """A node tree as the F3D conversion and the export read it: each node's
    settings, input values, images, textures settings and groups, and the links."""
    if tree is None:
        return None
    nodes = []
    for node in sorted(tree.nodes, key=lambda n: n.name):
        pointers = {}
        for prop in node.bl_rna.properties:
            if prop.type != 'POINTER' or prop.identifier == 'rna_type':
                continue
            value = getattr(node, prop.identifier)
            if isinstance(value, bpy.types.NodeTree):
                pointers[prop.identifier] = [value.name, _tree_info(value)]
            elif isinstance(value, bpy.types.Node):
                pointers[prop.identifier] = value.name
            elif value is not None:  # An ID (image...) or settings (texture mapping, image user...)
                pointers[prop.identifier] = value if isinstance(value, bpy.types.ID) else _rna_values(value)
        inputs = [[socket.identifier, _plain(getattr(socket, 'default_value', None))] for socket in node.inputs]
        nodes.append([node.bl_idname, node.name, _rna_values(node, _NODE_UI), pointers, inputs])
    links = sorted([link.from_node.name, link.from_socket.identifier, link.to_node.name,
                    link.to_socket.identifier, link.is_muted] for link in tree.links)
    return [nodes, links]


def _key_value(value):
    """JSON for the values the key holds that json can't write."""
    if isinstance(value, bpy.types.ID):
        result = [type(value).__name__, value.name, value.library.filepath if value.library else '']
        if isinstance(value, bpy.types.Image):
            path = bpy.path.abspath(value.filepath, library=value.library)
            stat = os.stat(path) if os.path.isfile(path) else None
            result += [value.filepath, value.source, list(value.size), value.colorspace_settings.name, value.alpha_mode,
                       value.packed_file.size if value.packed_file else None,
                       [stat.st_size, stat.st_mtime_ns] if stat else None]
        return result
    if hasattr(value, 'to_dict'):
        return value.to_dict()
    if hasattr(value, 'to_list'):
        return value.to_list()
    if hasattr(value, '__len__'):  # mathutils Vector/Color/Euler...
        return list(value)
    raise TypeError(f'{type(value).__name__} in the key')


def _addon_version(module_name):
    module = sys.modules.get(module_name)
    return str(getattr(module, 'bl_info', {}).get('version'))


def _key_salt():
    """Changes to what produces the glTF from the same data: Blender, its glTF
    exporter, Fast64 (converts the materials, its glTF extension writes them),
    this file and exporter.py (prepares the material conversion)."""
    sources = hashlib.sha1()
    for path in (__file__, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'exporter.py')):
        with open(path, 'rb') as f:
            sources.update(f.read())
    return '|'.join([bpy.app.version_string, _addon_version('io_scene_gltf2'), _addon_version('fast64'),
                     sources.hexdigest()])


def _settle_fast64(state):
    """Fast64's glTF export changes what it reads: the first time, it stores
    every F3D property of the materials it writes and adds its settings to the
    scene; each time, it sets up the nodes and render modes of all F3D materials
    for the current scene. A mesh's glTF would then depend on which meshes were
    exported before it (the cache skips some): make the first changes to all of
    them now, and the others when the scene changes (_settle_scene)."""
    state['scene'] = None
    state['modify_nodes'] = None
    try:
        from fast64.fast64_internal.utility import fix_invalid_props
        from fast64.fast64_internal.f3d.glTF.f3d_gltf import is_mat_f3d, modify_f3d_nodes_for_export
    except ImportError:  # No Fast64: the materials stay as they are
        return
    for scene in bpy.data.scenes:
        if not scene.library:
            scene.fast64.settings.glTF.f3d  # Reading it creates it
    for material in bpy.data.materials:
        if is_mat_f3d(material) and not material.library:
            fix_invalid_props(material.f3d_mat)
    state['modify_nodes'] = modify_f3d_nodes_for_export


def _settle_scene(state):
    """The F3D materials as Fast64's export leaves them in the current scene."""
    scene = bpy.context.scene
    if state['modify_nodes'] is not None and state['scene'] != scene:
        state['modify_nodes'](False)
        state['modify_nodes'](True)
        state['scene'] = scene


_KEY_SALT = ''
CACHE_FILE = '.mesh_cache.json'


def _load_cache(assets_dir):
    try:
        with open(os.path.join(assets_dir, CACHE_FILE), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# Armory's own export cache flag: it depends on Armory's last export, not on the mesh
DROPPED_EXTRA = 'arm_cached'


def _stable_gltf(path):
    """The same glTF for the same mesh on every export: no DROPPED_EXTRA, and no
    scene extras (they hold every Blender scene's settings, which Fast64 adds to
    while exporting; tiny3d only reads material extras)."""
    with open(path, encoding='utf-8') as f:
        gltf = json.load(f)
    for scene in gltf.get('scenes', []):
        scene.pop('extras', None)
    for elements in gltf.values():
        for element in elements if isinstance(elements, list) else []:
            extras = element.get('extras') if isinstance(element, dict) else None
            if isinstance(extras, dict) and extras.pop(DROPPED_EXTRA, None) is not None and not extras:
                del element['extras']
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(gltf, f, indent=2, sort_keys=True)


def _assets_dir():
    return os.path.join(arm.utils.build_dir(), 'n64', 'assets')


def plan_meshes(exporter) -> bool:
    """Before the F3D conversion: which meshes changed since the last export
    (exporter.mesh_plan, by glTF file); returns whether any has to be exported."""
    global _KEY_SALT
    _KEY_SALT = _key_salt()
    assets_dir = _assets_dir()
    old = _load_cache(assets_dir)
    conversion_scene = bpy.context.scene
    exporter.mesh_plan = {}
    for obj, mesh_name, _, armature, _ in _mesh_jobs(exporter):
        file = f'{mesh_name}.gltf'
        # tiny3d drops geometry without a material (fine for hidden collision shapes)
        if not obj.hide_render and not any(slot.material for slot in obj.material_slots):
            log.warn(f'N64: mesh "{mesh_name}" has no material, so it is invisible on N64: assign one')
        # Skinned meshes are always exported (their animations aren't in the key)
        key = None if armature else _mesh_key(obj, conversion_scene)
        entry = old.get(file)
        cached = (key is not None and entry is not None and entry['key'] == key
                  and all(os.path.isfile(os.path.join(assets_dir, f)) for f in entry['files']))
        exporter.mesh_plan[file] = {'key': key, 'entry': entry if cached else None}
    return any(plan['entry'] is None for plan in exporter.mesh_plan.values())


def export_meshes(exporter):
    """Export the meshes plan_meshes found changed (after the F3D conversion,
    if any), keep the others' files.

    Exports linked objects from temp scene first (with F3D materials),
    then local objects from user scenes.

    Updates exporter.exported_meshes with {mesh: mesh_name} mapping.
    Updates exporter.skinned_meshes with {mesh_name: {"armature_clips": [...]}} for skinned models.
    """
    assets_dir = _assets_dir()
    exporter.exported_meshes = {}
    exporter.skinned_meshes = {}  # mesh_name -> {"anim_clips": [...]}

    # Cache entries by glTF file name: the ones of this export replace the old ones
    cache = {}
    # When some mesh is exported: Fast64 set up in every scene in turn, also for
    # the unchanged meshes, as for an export of all (a material keeps some of a
    # scene's setup in the next one)
    settle = None
    if any(plan['entry'] is None for plan in exporter.mesh_plan.values()):
        settle = {}
        _settle_fast64(settle)
    for obj, mesh_name, mesh, armature, linked in _mesh_jobs(exporter):
        file = f'{mesh_name}.gltf'
        plan = exporter.mesh_plan.get(file, {'key': None, 'entry': None})
        kind = 'linked mesh' if linked else 'mesh'
        if settle is not None:
            _settle_scene(settle)
        if plan['entry'] is not None:
            cache[file] = plan['entry']
            log.info(f'Unchanged {kind}: {mesh_name}')
        else:
            files = _export_mesh_to_gltf(obj, os.path.join(assets_dir, file), armature=armature)
            if plan['key'] is not None:
                cache[file] = {'key': plan['key'], 'files': files}
            log.info(f'Exported {kind}: {mesh_name}')

        if armature:
            anim_clips = _get_animation_names(armature)
            exporter.skinned_meshes[mesh_name] = {"anim_clips": anim_clips}
            log.info(f'  skinned, clips: {anim_clips}')
        if mesh is not None:
            exporter.exported_meshes[mesh] = mesh_name
    n64_utils.write_if_changed(os.path.join(assets_dir, CACHE_FILE), json.dumps(cache, indent=1, sort_keys=True))


def _mesh_jobs(exporter):
    """(object, glTF name, mesh for exporter.exported_meshes, armature, linked) of each
    mesh to export, in export order, with the window on the object's scene:
    first the linked objects' local copies (temp scene), then the user scenes'
    objects. Objects are looked up each time (the F3D conversion replaces them)."""
    window = bpy.context.window
    main_scene, main_view_layer = window.scene, window.view_layer

    def use_scene(scene):
        # Nothing selected elsewhere: the glTF export of the selection could take it
        if window.scene != scene:
            n64_utils.deselect_from_all_viewlayers()
            window.scene = scene
            window.view_layer = scene.view_layers[0]

    names = {}  # Model name -> the Blender mesh it comes from
    meshes = set()
    try:
        temp_scene = linked_export.get_temp_scene() if exporter.linked_objects else None
        for local_obj_name, original_mesh_name in exporter.linked_objects if temp_scene else []:
            local_obj = bpy.data.objects.get(local_obj_name)
            if not local_obj or local_obj.type != 'MESH':
                if not local_obj:
                    log.warn(f'Linked object not found: {local_obj_name}')
                continue
            mesh_name = arm.utils.safesrc(original_mesh_name)
            if mesh_name in names:
                continue
            names[mesh_name] = original_mesh_name
            # Stored by original mesh for scene_exporter lookup
            original_mesh = linked_export.get_original_mesh(original_mesh_name)
            if original_mesh is None:
                log.warn(f'Could not find original mesh for: {original_mesh_name}')
            else:
                meshes.add(original_mesh)
            use_scene(temp_scene)
            yield local_obj, mesh_name, original_mesh, _find_armature(local_obj), True

        for scene in bpy.data.scenes:
            if scene.library or linked_export.is_temp_scene(scene):
                continue
            for obj, _, _ in scene_exporter.collect_scene_objects(scene):
                if obj.type != 'MESH' or obj.library or obj.data in meshes:
                    continue
                meshes.add(obj.data)
                mesh_name = arm.utils.safesrc(linked_utils.asset_name(obj.data))
                if mesh_name in names:
                    # Both would write the same model file: one would replace the other
                    raise ValueError(f'N64: the meshes "{names[mesh_name]}" and "{obj.data.name}" both become '
                                     f'the model "{mesh_name}" (names in C keep only letters, digits and _): '
                                     'rename one of them')
                names[mesh_name] = obj.data.name
                use_scene(scene)
                yield obj, mesh_name, obj.data, _find_armature(obj), False
    finally:
        window.scene = main_scene
        window.view_layer = main_view_layer
