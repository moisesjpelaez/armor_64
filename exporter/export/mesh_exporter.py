"""Mesh Exporter - Handles GLTF mesh export for N64.

This module exports Blender meshes to GLTF format for subsequent
conversion to N64 model format (T3D).

All meshes are exported as GLTF_SEPARATE (.gltf + .bin) for consistency
and debuggability. For skinned meshes, additional export options are set:
  - export_apply=False  (preserve skin deformation data)
  - export_skins=True   (include joint/weight data)
  - export_animations=True (include animation clips)
  - Both mesh AND armature objects are selected for export
"""

import os
import bpy

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


def _export_mesh_to_gltf(obj, output_path, armature=None):
    """Export a mesh to GLTF_SEPARATE format.

    Replicates the same settings as a manual File > Export > glTF 2.0.
    If armature is provided, includes skin/animation data.
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

    # --- Same settings as manual export ---
    bpy.ops.export_scene.gltf(
        filepath=output_path,
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


def export_meshes(exporter):
    """Export all meshes from all scenes to GLTF format.

    Exports linked objects from temp scene first (with F3D materials),
    then local objects from user scenes.

    Updates exporter.exported_meshes with {mesh: mesh_name} mapping.
    Updates exporter.skinned_meshes with {mesh_name: {"armature_clips": [...]}} for skinned models.
    """
    assets_dir = os.path.join(arm.utils.build_dir(), 'n64', 'assets')
    exporter.exported_meshes = {}
    exporter.skinned_meshes = {}  # mesh_name -> {"anim_clips": [...]}

    _export_linked_meshes(exporter, assets_dir)
    _export_scene_meshes(exporter, assets_dir)


def _export_linked_meshes(exporter, assets_dir):
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
        _export_mesh_to_gltf(local_obj, output_path, armature=armature)

        if armature:
            anim_clips = _get_animation_names(armature)
            exporter.skinned_meshes[mesh_name] = {"anim_clips": anim_clips}
            log.info(f'Exported linked skinned mesh: {mesh_name} (clips: {anim_clips})')
        else:
            log.info(f'Exported linked mesh: {mesh_name}')

        # Store by original mesh for scene_exporter lookup
        original_mesh = linked_export.get_original_mesh(original_mesh_name)
        if original_mesh:
            exporter.exported_meshes[original_mesh] = mesh_name
        else:
            log.warn(f'Could not find original mesh for: {original_mesh_name}')

    # Restore original scene
    bpy.context.window.scene = main_scene
    bpy.context.window.view_layer = main_view_layer


def _export_scene_meshes(exporter, assets_dir):
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
            _export_mesh_to_gltf(obj, output_path, armature=armature)

            if armature:
                anim_clips = _get_animation_names(armature)
                exporter.skinned_meshes[mesh_name] = {"anim_clips": anim_clips}
                log.info(f'Exported skinned mesh: {mesh_name} (clips: {anim_clips})')
            else:
                log.info(f'Exported mesh: {mesh_name}')

            exporter.exported_meshes[mesh] = mesh_name

        bpy.context.window.scene = main_scene
        bpy.context.window.view_layer = main_view_layer
