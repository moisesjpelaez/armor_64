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
exported again (assets/.mesh_cache.json, see _mesh_key).
"""

import hashlib
import json
import os
import re
import sys

import bpy
import numpy as np

import arm.utils
import arm.log as log
import arm.linked_utils as linked_utils
import arm.n64.utils as n64_utils
from arm.n64.export import linked_export


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


def _collect_all_objects(scene):
    """Collect all objects including those inside instance collections.

    Each instance collection empty is processed separately to capture
    all instances. For mesh export, duplicates are filtered later by mesh data.

    Returns:
        List of (object, instance_matrix) tuples. instance_matrix is None
        for direct scene objects, or the parent empty's world matrix for
        objects inside instanced collections.
    """
    objects = []

    for obj in scene.collection.all_objects:
        if obj.instance_type == 'COLLECTION' and obj.instance_collection:
            coll = obj.instance_collection
            for cobj in coll.all_objects:
                objects.append((cobj, obj.matrix_world))
        else:
            objects.append((obj, None))

    return objects


def _export_mesh_to_gltf(obj, output_path, cache, armature=None) -> bool:
    """Export a mesh to GLTF_SEPARATE format.

    Replicates the same settings as a manual File > Export > glTF 2.0.
    If armature is provided, includes skin/animation data.
    Returns False when the cache had it (the files from last time are kept).
    """
    is_skinned = armature is not None
    view_layer = bpy.context.view_layer
    export_objects = {obj} | ({armature} if armature else set())

    # --- Prepare actions (clean own + stash foreign) ---
    restore_actions = None
    if armature:
        restore_actions = _prepare_export_actions(armature)

    # --- Ensure UV layer named "UVMap" for Fast64 ---
    uv_orig_name = _ensure_uvmap_layer(obj.data)

    # --- Hide every other object so GLTF exporter only sees ours ---
    # use_selection=True still follows parent/child chains and can pull in
    # unintended objects from the same scene. Hiding everything else avoids
    # any cross-contamination between models sharing the same temp scene.
    hidden_objects = []
    for o in view_layer.objects:
        if o not in export_objects and not o.hide_viewport:
            o.hide_viewport = True
            hidden_objects.append(o)

    # Save and reset transforms to origin
    orig = (obj.location.copy(), obj.rotation_euler.copy(), obj.scale.copy())
    arm_orig = None
    arm_parent_orig = None
    if armature:
        arm_orig = (armature.location.copy(), armature.rotation_euler.copy(),
                    armature.scale.copy())
        # Temporarily unparent armature so the GLTF exporter doesn't
        # traverse up to parent objects (e.g. collider mesh) which causes
        # duplicated skeleton nodes and crashes gltf_to_t3d.
        if armature.parent:
            arm_parent_orig = (armature.parent, armature.matrix_parent_inverse.copy())
            armature.parent = None
    for o in export_objects:
        o.location = (0, 0, 0)
        o.rotation_euler = (0, 0, 0)
        o.scale = (1, 1, 1)

    bpy.ops.object.select_all(action='DESELECT')
    view_layer.update()

    obj.select_set(True)
    if armature:
        armature.select_set(True)
        view_layer.objects.active = armature

    # Skinned meshes are always exported (their animations aren't in the key)
    assets_dir = os.path.dirname(output_path)
    name = os.path.basename(output_path)
    _settle_scene(cache)
    key = None if armature else _mesh_key(obj)
    entry = cache['old'].get(name)
    exported = not (key is not None and entry and entry['key'] == key
                    and all(os.path.isfile(os.path.join(assets_dir, f)) for f in entry['files']))
    if exported:
        files = _gltf_export(output_path, is_skinned)
        if key is not None:
            cache['new'][name] = {'key': key, 'files': files}
    else:
        cache['new'][name] = entry

    # Restore transforms and parent
    obj.location, obj.rotation_euler, obj.scale = orig
    if armature and arm_orig:
        armature.location, armature.rotation_euler, armature.scale = arm_orig
    if arm_parent_orig:
        armature.parent, armature.matrix_parent_inverse = arm_parent_orig

    # Restore hidden objects
    for o in hidden_objects:
        o.hide_viewport = False

    view_layer.update()

    # Restore UV layer name
    if uv_orig_name is not None and obj.data and obj.data.uv_layers:
        for layer in obj.data.uv_layers:
            if layer.name == "UVMap":
                layer.name = uv_orig_name
                break

    # Restore stashed/cleaned actions
    if restore_actions:
        restore_actions()
    return exported


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


def _mesh_key(obj):
    """Hash of everything the glTF export of `obj` reads (as prepared for it:
    at the origin, alone and selected), or None if something can't be hashed
    (then it's exported). The same key means the same glTF files."""
    from io_scene_gltf2.blender.com import extras

    def generate_extras(element):  # As written in the glTF (_stable_gltf)
        values = extras.generate_extras(element)
        if values:
            values.pop(DROPPED_EXTRA, None)
        return values

    try:
        h = hashlib.sha1()
        h.update(_KEY_SALT.encode())
        scene = bpy.context.scene
        materials = [slot.material for slot in obj.material_slots]
        info = {
            'object': [obj.name, [list(row) for row in obj.matrix_world], generate_extras(obj)],
            'mesh': [obj.data.name, generate_extras(obj.data)],
            # Not written (_stable_gltf), but Fast64's settings there change the output
            'scene': [_without_empty(generate_extras(scene)), _without_empty(generate_extras(scene.world))],
            'materials': [None if m is None else [m.name, generate_extras(m), _rna_values(m), _node_images(m)]
                          for m in materials],
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


def _rna_values(struct):
    """The settable non-pointer RNA values of `struct` (blend mode, culling...)."""
    values = {}
    for prop in struct.bl_rna.properties:
        if prop.is_readonly or prop.type in ('POINTER', 'COLLECTION'):
            continue
        value = getattr(struct, prop.identifier)
        values[prop.identifier] = list(value) if hasattr(value, '__len__') and not isinstance(value, str) else value
    return values


def _node_images(material):
    if not material.node_tree:
        return []
    return [node.image for node in material.node_tree.nodes if getattr(node, 'image', None) is not None]


def _key_value(value):
    """JSON for the values the key holds that json can't write."""
    if isinstance(value, bpy.types.ID):
        result = [type(value).__name__, value.name, value.library.filepath if value.library else '']
        if isinstance(value, bpy.types.Image):
            path = bpy.path.abspath(value.filepath, library=value.library)
            stat = os.stat(path) if os.path.isfile(path) else None
            result += [value.filepath, value.source, list(value.size),
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
    exporter, Fast64 (its glTF extension writes the materials) and this file."""
    with open(__file__, 'rb') as f:
        source = hashlib.sha1(f.read()).hexdigest()
    return '|'.join([bpy.app.version_string, _addon_version('io_scene_gltf2'), _addon_version('fast64'), source])


def _settle_fast64(cache):
    """Fast64's glTF export changes what it reads: the first time, it stores
    every F3D property of the materials it writes and adds its settings to the
    scene; each time, it sets up the nodes and render modes of all F3D materials
    for the current scene. A mesh's glTF would then depend on which meshes were
    exported before it (the cache skips some): make the first changes to all of
    them now, and the others when the scene changes (_settle_scene)."""
    cache['scene'] = None
    cache['modify_nodes'] = None
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
    cache['modify_nodes'] = modify_f3d_nodes_for_export


def _settle_scene(cache):
    """The F3D materials as Fast64's export leaves them in the current scene."""
    scene = bpy.context.scene
    if cache['modify_nodes'] is not None and cache['scene'] != scene:
        cache['modify_nodes'](False)
        cache['modify_nodes'](True)
        cache['scene'] = scene


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
    """The same glTF for the same mesh on every export: material names without
    Blender's .001 suffixes (the local copies of linked materials get them in
    whatever order they're made), unless two would then share a name; no
    DROPPED_EXTRA, and no scene extras (they hold every Blender scene's settings,
    which Fast64 adds to while exporting; tiny3d only reads material extras)."""
    with open(path, encoding='utf-8') as f:
        gltf = json.load(f)
    for scene in gltf.get('scenes', []):
        scene.pop('extras', None)
    for elements in gltf.values():
        for element in elements if isinstance(elements, list) else []:
            extras = element.get('extras') if isinstance(element, dict) else None
            if isinstance(extras, dict) and extras.pop(DROPPED_EXTRA, None) is not None and not extras:
                del element['extras']
    materials = gltf.get('materials', [])
    bare = [re.sub(r'\.\d{3}$', '', m.get('name', '')) for m in materials]
    if len(set(bare)) == len(bare):
        for material, name in zip(materials, bare):
            material['name'] = name
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(gltf, f, indent=2, sort_keys=True)


def export_meshes(exporter):
    """Export all meshes from all scenes to GLTF format.

    Exports linked objects from temp scene first (with F3D materials),
    then local objects from user scenes.

    Updates exporter.exported_meshes with {mesh: mesh_name} mapping.
    Updates exporter.skinned_meshes with {mesh_name: {"armature_clips": [...]}} for skinned models.
    """
    global _KEY_SALT
    assets_dir = os.path.join(arm.utils.build_dir(), 'n64', 'assets')
    exporter.exported_meshes = {}
    exporter.skinned_meshes = {}  # mesh_name -> {"anim_clips": [...]}

    # Cache entries by glTF file name: the ones of this export replace the old ones
    _KEY_SALT = _key_salt()
    cache = {'old': _load_cache(assets_dir), 'new': {}}
    _settle_fast64(cache)
    _export_linked_meshes(exporter, assets_dir, cache)
    _export_scene_meshes(exporter, assets_dir, cache)
    n64_utils.write_if_changed(os.path.join(assets_dir, CACHE_FILE),
                               json.dumps(cache['new'], indent=1, sort_keys=True))


def _export_linked_meshes(exporter, assets_dir, cache):
    """Export meshes from temp scene (localized linked objects)."""
    if not exporter.linked_objects:
        return

    temp_scene = linked_export.get_temp_scene()
    if not temp_scene:
        return

    n64_utils.deselect_from_all_viewlayers()
    main_scene = bpy.context.scene
    main_view_layer = bpy.context.view_layer

    # Switch to temp scene
    bpy.context.window.scene = temp_scene
    bpy.context.window.view_layer = temp_scene.view_layers[0]

    for local_obj_name, original_mesh_name in exporter.linked_objects:
        bpy.ops.object.select_all(action='DESELECT')
        bpy.context.view_layer.update()

        local_obj = bpy.data.objects.get(local_obj_name)
        if not local_obj or local_obj.type != 'MESH':
            if not local_obj:
                log.warn(f'Linked object not found: {local_obj_name}')
            continue

        mesh_name = arm.utils.safesrc(original_mesh_name)
        if mesh_name in exporter.exported_meshes.values():
            continue

        # Check for armature (skinned mesh)
        armature = _find_armature(local_obj)
        output_path = os.path.join(assets_dir, f'{mesh_name}.gltf')
        exported = _export_mesh_to_gltf(local_obj, output_path, cache, armature=armature)

        if armature:
            anim_clips = _get_animation_names(armature)
            exporter.skinned_meshes[mesh_name] = {"anim_clips": anim_clips}
            log.info(f'Exported linked skinned mesh: {mesh_name} (clips: {anim_clips})')
        else:
            log.info(f'{"Exported" if exported else "Unchanged"} linked mesh: {mesh_name}')

        # Store by original mesh for scene_exporter lookup
        original_mesh = linked_export.get_original_mesh(original_mesh_name)
        if original_mesh:
            exporter.exported_meshes[original_mesh] = mesh_name
        else:
            log.warn(f'Could not find original mesh for: {original_mesh_name}')

    # Restore original scene
    bpy.context.window.scene = main_scene
    bpy.context.window.view_layer = main_view_layer


def _export_scene_meshes(exporter, assets_dir, cache):
    """Export meshes from local objects in user scenes."""
    for scene in bpy.data.scenes:
        if scene.library or linked_export.is_temp_scene(scene):
            continue

        n64_utils.deselect_from_all_viewlayers()
        main_scene = bpy.context.scene
        main_view_layer = bpy.context.view_layer

        for obj, _ in _collect_all_objects(scene):
            if obj.type != 'MESH' or obj.library:
                continue

            mesh = obj.data
            if mesh in exporter.exported_meshes:
                continue

            bpy.ops.object.select_all(action='DESELECT')
            bpy.context.view_layer.update()

            mesh_name = arm.utils.safesrc(linked_utils.asset_name(mesh))

            bpy.context.window.scene = scene
            bpy.context.window.view_layer = scene.view_layers[0]

            # Check for armature (skinned mesh)
            armature = _find_armature(obj)
            output_path = os.path.join(assets_dir, f'{mesh_name}.gltf')
            exported = _export_mesh_to_gltf(obj, output_path, cache, armature=armature)

            if armature:
                anim_clips = _get_animation_names(armature)
                exporter.skinned_meshes[mesh_name] = {"anim_clips": anim_clips}
                log.info(f'Exported skinned mesh: {mesh_name} (clips: {anim_clips})')
            else:
                log.info(f'{"Exported" if exported else "Unchanged"} mesh: {mesh_name}')

            exporter.exported_meshes[mesh] = mesh_name

        bpy.context.window.scene = main_scene
        bpy.context.window.view_layer = main_view_layer
