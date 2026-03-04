"""Linked Export - Handles linked blend file objects for N64 export.

Creates temporary local copies of linked objects so Fast64 can convert
their materials. Cleans up after export.
"""

import bpy

import arm.log as log
import arm.linked_utils as linked_utils


TEMP_SCENE_NAME = "_armory_n64_export_temp"
TEMP_COLLECTION_NAME = "_armory_n64_linked"


class _LinkedExportState:
    """Encapsulates state for linked object export.

    Using a class avoids scattered module-level globals and makes state clearer.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        """Reset all state to initial values."""
        self.temp_scene_name = None
        self.temp_object_names = []
        self.temp_material_names = []
        self.temp_mesh_names = []
        self.original_mesh_refs = {}  # {mesh_name: original_mesh}


# Single module-level state instance
_state = _LinkedExportState()


def _collect_linked_instance_collections():
    """Find all instance collections containing linked objects."""
    collections = set()
    for scene in bpy.data.scenes:
        if scene.library:
            continue
        for obj in scene.collection.all_objects:
            if obj.instance_type == 'COLLECTION' and obj.instance_collection:
                coll = obj.instance_collection
                if any(cobj.library for cobj in coll.all_objects):
                    collections.add(coll)
    return list(collections)


def prepare_linked_for_export():
    """Create temp scene with localized copies of linked objects for export.

    Copies ALL object types (mesh, armature, empty, etc.) and rebuilds
    parent-child + modifier references so any hierarchy depth works.

    Returns:
        List of (local_object_name, original_mesh_name) tuples for export
    """
    linked_collections = _collect_linked_instance_collections()
    if not linked_collections:
        return []

    temp_scene = bpy.data.scenes.new(TEMP_SCENE_NAME)
    _state.temp_scene_name = temp_scene.name
    temp_collection = bpy.data.collections.new(TEMP_COLLECTION_NAME)
    temp_scene.collection.children.link(temp_collection)
    if not temp_scene.view_layers:
        temp_scene.view_layers.new("ViewLayer")

    mesh_objects = []
    processed_meshes = set()

    for coll in linked_collections:
        # --- Copy all objects, build name -> local mapping ---
        obj_map = {}
        for obj in coll.all_objects:
            local = obj.copy()

            if obj.type == 'MESH':
                mesh_name = linked_utils.asset_name(obj.data)
                local.name = f"_armory_temp_{mesh_name}"
                if obj.data.library:
                    local_data = obj.data.copy()
                    local_data.name = mesh_name
                    local.data = local_data
                    _state.temp_mesh_names.append(mesh_name)
                for slot in local.material_slots:
                    if slot.material and slot.material.library:
                        local_mat = slot.material.copy()
                        local_mat.name = linked_utils.asset_name(slot.material)
                        slot.material = local_mat
                        _state.temp_material_names.append(local_mat.name)
            elif obj.type == 'ARMATURE':
                local.name = f"_armory_temp_arm_{linked_utils.asset_name(obj.data)}"
                if obj.data.library:
                    local.data = obj.data.copy()
            else:
                local.name = f"_armory_temp_{obj.name}"

            _state.temp_object_names.append(local.name)
            temp_collection.objects.link(local)
            obj_map[obj.name] = local

        # --- Fix ALL parent + modifier references ---
        for obj in coll.all_objects:
            local = obj_map.get(obj.name)
            if not local:
                continue
            if obj.parent and obj.parent.name in obj_map:
                local.parent = obj_map[obj.parent.name]
                local.matrix_parent_inverse = obj.matrix_parent_inverse.copy()
            for mod in local.modifiers:
                if mod.type == 'ARMATURE' and mod.object and mod.object.name in obj_map:
                    mod.object = obj_map[mod.object.name]

        # --- Collect mesh objects for export ---
        for obj in coll.all_objects:
            if obj.type != 'MESH':
                continue
            mesh_name = linked_utils.asset_name(obj.data)
            if mesh_name in processed_meshes:
                continue
            processed_meshes.add(mesh_name)
            _state.original_mesh_refs[mesh_name] = obj.data
            mesh_objects.append((obj_map[obj.name].name, mesh_name))

    return mesh_objects


def get_temp_scene():
    """Get the temporary export scene (or None if not created)."""
    if not _state.temp_scene_name:
        return None
    return bpy.data.scenes.get(_state.temp_scene_name)


def get_original_mesh(mesh_name):
    """Get original mesh data block by qualified name."""
    return _state.original_mesh_refs.get(mesh_name)


def is_temp_scene(scene):
    """Check if a scene is the temporary export scene."""
    return _state.temp_scene_name and scene.name == _state.temp_scene_name


def _safe_remove(data_collection, name):
    """Safely remove a data block by name."""
    try:
        item = data_collection.get(name)
        if item:
            data_collection.remove(item, do_unlink=True)
    except Exception:
        pass


def cleanup_linked_export():
    """Remove temporary scene and all localized objects."""
    # Remove objects first (they reference meshes/materials)
    for name in _state.temp_object_names:
        _safe_remove(bpy.data.objects, name)

    # Remove collection
    _safe_remove(bpy.data.collections, TEMP_COLLECTION_NAME)

    # Remove materials and meshes
    for name in _state.temp_material_names:
        _safe_remove(bpy.data.materials, name)
    for name in _state.temp_mesh_names:
        _safe_remove(bpy.data.meshes, name)

    # Remove scene
    if _state.temp_scene_name:
        try:
            scene = bpy.data.scenes.get(_state.temp_scene_name)
            if scene:
                bpy.data.scenes.remove(scene)
        except Exception:
            pass

    _state.reset()
