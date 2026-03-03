"""
Scene Exporter - Handles scene data extraction and C code generation for N64.

This module provides functions for building scene data from Blender scenes
and generating scene C files.
"""

import os
import math

import bpy

import arm.utils
import arm.log as log
import arm.linked_utils as linked_utils
import arm.n64.codegen as codegen
import arm.n64.utils as n64_utils
from arm.n64.export import linked_export


def _collect_all_objects(scene):
    """Collect all objects including those inside instance collections.

    Each instance collection empty is processed separately to capture
    all instances at their respective transforms.

    Returns:
        List of (blender_obj, instance_matrix, instancer_parent) tuples.
        - instance_matrix: Empty's matrix_world for instance collection members,
          None for direct scene objects.
        - instancer_parent: The instancer Empty's parent object (e.g. Cube) for
          instance collection members, None for direct scene objects.
          Used to propagate parent/child hierarchy to the N64 scene.
    """
    objects = []

    for obj in scene.collection.all_objects:
        if obj.instance_type == 'COLLECTION' and obj.instance_collection is not None:
            # This is an instanced collection - process objects inside it
            # Each instance empty gets its own copy of the collection objects
            coll = obj.instance_collection
            instancer_parent = obj.parent  # e.g. Cube
            for cobj in coll.all_objects:
                objects.append((cobj, obj.matrix_world, instancer_parent))
        else:
            objects.append((obj, None, None))

    return objects


def _topological_sort_objects(objects):
    """Sort objects so every parent precedes all its children (DFS order).

    This ensures that when iterating in index order at runtime, parent
    world transforms are always computed before their children's.

    Args:
        objects: list of (blender_obj, instance_matrix, instancer_parent) tuples

    Returns:
        Sorted list in the same format, with parent/child ordering preserved.
    """
    # Stable base order: sort by name so indices are deterministic across builds.
    # Blender's scene.collection.all_objects has no guaranteed order.
    objects = sorted(objects, key=lambda t: t[0].name)

    # Build lookup: blender object name -> index in input list
    name_to_idx = {}
    for i, (obj, _, _) in enumerate(objects):
        # Use object name as key (unique within a scene)
        name_to_idx[obj.name] = i

    visited = set()
    sorted_result = []

    def visit(idx):
        if idx in visited:
            return
        visited.add(idx)
        obj, inst_mat, iparent = objects[idx]
        if inst_mat:
            # Instance collection object: visit instancer's parent first
            # (skip intra-collection obj.parent — names collide across instances)
            if iparent and iparent.name in name_to_idx:
                visit(name_to_idx[iparent.name])
        else:
            # Direct scene object: visit obj.parent first
            if obj.parent and obj.parent.name in name_to_idx:
                visit(name_to_idx[obj.parent.name])
        sorted_result.append((obj, inst_mat, iparent))

    for i in range(len(objects)):
        visit(i)

    return sorted_result


def build_scene_data(exporter, scene):
    """Build scene data dictionary from a Blender scene.

    Args:
        exporter: N64Exporter instance to update with scene data
        scene: Blender scene object to extract data from
    """
    scene_name = arm.utils.safesrc(scene.name).lower()
    scene_traits = _extract_traits(scene)

    # Extract canvas name directly from Blender trait list (UI Canvas is not a runtime trait)
    canvas_name = None
    if hasattr(scene, 'arm_traitlist'):
        for trait in scene.arm_traitlist:
            if trait.enabled_prop and trait.type_prop == 'UI Canvas':
                canvas_name = getattr(trait, 'canvas_name_prop', '')
                if canvas_name:
                    break

    # Get gravity from scene (Blender's scene.gravity is the actual gravity vector)
    gravity = [0.0, -9.81, 0.0]  # Default gravity
    if hasattr(scene, 'gravity'):
        gravity = [scene.gravity[0], scene.gravity[1], scene.gravity[2]]

    # Get physics debug draw mode from Armory settings
    debug_draw_mode = n64_utils.get_physics_debug_mode()

    # Safe access to Fast64 ambient color with fallback
    try:
        ambient_color = list(scene.fast64.renderSettings.ambientColor)
    except (AttributeError, KeyError):
        ambient_color = [0.2, 0.2, 0.2]  # Default ambient

    # Collect groups from Blender collections
    # Each collection used in this scene becomes a group with object count
    groups = {}
    for collection in bpy.data.collections:
        if collection.name.startswith(('RigidBodyWorld', 'Trait|')):
            continue
        # Check if collection is used in this scene
        collection_used = (
            scene.user_of_id(collection) or
            collection in scene.collection.children_recursive or
            any(obj.name in scene.objects for obj in collection.objects)
        )
        if collection_used:
            # Count mesh objects, including those inside linked/instanced collections
            mesh_count = 0
            for obj in collection.all_objects:
                if obj.type == 'MESH':
                    mesh_count += 1
                elif obj.instance_type == 'COLLECTION' and obj.instance_collection is not None:
                    mesh_count += sum(1 for cobj in obj.instance_collection.all_objects if cobj.type == 'MESH')
            groups[collection.name] = {
                'original_name': collection.name,
                'count': mesh_count
            }

    exporter.scene_data[scene_name] = {
        "canvas": canvas_name,  # UI canvas for this scene (or None)
        "world": {
            "clear_color": n64_utils.get_clear_color(scene),
            "ambient_color": ambient_color,
            "gravity": gravity,
            "physics_debug_mode": debug_draw_mode
        },
        "cameras": [],
        "lights": [],
        "objects": [],
        "traits": scene_traits,
        "groups": groups
    }

    for obj, instance_matrix, _ in _collect_all_objects(scene):
        if obj.type == 'LIGHT':
            _process_light(exporter, scene_name, obj, instance_matrix)

    # Collect mesh + empty objects together for topological sort (parent/child ordering).
    # Empties go into the same objects[] array as meshes — they're ArmObjects with
    # dpl=NULL and model_mat=NULL.  This enables full nested parenting:
    #   empty→mesh, mesh→empty, empty→empty, camera→any.
    scene_objects = [
        (obj, inst_mat, iparent) for obj, inst_mat, iparent in _collect_all_objects(scene)
        if obj.type == 'MESH' or obj.type == 'EMPTY'
    ]
    sorted_scene_objects = _topological_sort_objects(scene_objects)

    # Filter exportable:  meshes must be in exported_meshes, empties always pass.
    # Instance collection empties (instance_type == 'COLLECTION') are instancer
    # holders — they should NOT become scene entities (they're consumed above to
    # build instance_matrix).  Only plain empties (instance_type != 'COLLECTION')
    # are actual scene objects.
    exportable_objects = []
    for obj, inst_mat, iparent in sorted_scene_objects:
        if obj.type == 'MESH':
            mesh = obj.data
            if mesh in exporter.exported_meshes:
                exportable_objects.append((obj, inst_mat, iparent))
            else:
                log.warn(f'Object "{obj.name}": mesh not exported, skipping')
        elif obj.type == 'EMPTY':
            # Skip instance collection empties — they are consumed as instancers,
            # not exported as scene entities.  Also skip empties that come through
            # instance collections (inst_mat is not None) to avoid duplicates.
            if obj.instance_type != 'COLLECTION' and inst_mat is None:
                exportable_objects.append((obj, inst_mat, iparent))

    # Build name→index map for parent resolution (indices match objects[] array)
    object_name_to_index = {}
    for i, (obj, _, _) in enumerate(exportable_objects):
        object_name_to_index[obj.name] = i

    for obj, instance_matrix, instancer_parent in exportable_objects:
        if obj.type == 'MESH':
            _process_mesh_object(exporter, scene_name, obj, instance_matrix, object_name_to_index, instancer_parent)
        elif obj.type == 'EMPTY':
            _process_empty_object(exporter, scene_name, obj, object_name_to_index)

    # Now process cameras (after objects are indexed, so camera parent resolution works)
    for obj, instance_matrix, _ in _collect_all_objects(scene):
        if obj.type == 'CAMERA':
            _process_camera(exporter, scene_name, obj, instance_matrix, object_name_to_index)


def _process_camera(exporter, scene_name, obj, instance_matrix=None, object_name_to_index=None):
    """Process a camera object and add to scene data.

    Args:
        object_name_to_index: dict mapping object names to indices in the
            unified objects[] array.  Used for camera parent resolution.
    """
    # Compute world matrix considering instance transform
    world_matrix = instance_matrix @ obj.matrix_local if instance_matrix else obj.matrix_world
    world_pos = list(world_matrix.to_translation())

    cam_dir = world_matrix.to_3x3().col[2]
    cam_target = [
        world_pos[0] - cam_dir[0],
        world_pos[1] - cam_dir[1],
        world_pos[2] - cam_dir[2]
    ]
    # Compute vertical FOV — tiny3d's t3d_mat4_perspective expects vertical FOV.
    # Blender's sensor_fit determines which dimension the focal length maps to.
    # For AUTO (default), Blender picks the larger render dimension (horizontal
    # for landscape).  We always need the vertical FOV for tiny3d.
    cam_data_bl = obj.data
    sensor_fit = cam_data_bl.sensor_fit
    sensor_w = cam_data_bl.sensor_width
    sensor_h = cam_data_bl.sensor_height
    lens = cam_data_bl.lens

    if sensor_fit == 'VERTICAL':
        # Blender fits FOV to vertical sensor dimension
        cam_fov = math.degrees(2 * math.atan((sensor_h * 0.5) / lens))
    elif sensor_fit == 'HORIZONTAL':
        # Blender fits FOV to horizontal sensor dimension — convert to vertical
        # via aspect ratio (render_resolution_y / render_resolution_x)
        import bpy as _bpy
        res_x = _bpy.context.scene.render.resolution_x
        res_y = _bpy.context.scene.render.resolution_y
        hfov = 2 * math.atan((sensor_w * 0.5) / lens)
        cam_fov = math.degrees(2 * math.atan(math.tan(hfov * 0.5) * res_y / res_x))
    else:
        # AUTO: Blender picks the larger render dimension as the fit axis.
        # For landscape (width >= height), it fits horizontally, so convert to vertical.
        # For portrait (height > width), it already is vertical.
        import bpy as _bpy
        res_x = _bpy.context.scene.render.resolution_x
        res_y = _bpy.context.scene.render.resolution_y
        if res_x >= res_y:
            # Landscape: sensor_width maps to horizontal — derive vertical FOV
            hfov = 2 * math.atan((sensor_w * 0.5) / lens)
            cam_fov = math.degrees(2 * math.atan(math.tan(hfov * 0.5) * res_y / res_x))
        else:
            # Portrait: sensor_height maps to vertical — already vertical FOV
            cam_fov = math.degrees(2 * math.atan((sensor_h * 0.5) / lens))

    # Camera parent detection — camera can be child of any object (mesh or empty)
    parent_index = -1
    local_pos = None
    local_target = None
    if object_name_to_index and obj.parent and obj.parent.name in object_name_to_index:
        parent_index = object_name_to_index[obj.parent.name]
        # Compute camera pos/target relative to parent
        parent_world = obj.parent.matrix_world
        parent_inv = parent_world.inverted()
        from mathutils import Vector
        local_pos_vec = parent_inv @ Vector(world_pos)
        local_target_vec = parent_inv @ Vector(cam_target)
        local_pos = list(local_pos_vec)
        local_target = list(local_target_vec)

    cam_data = {
        "name": arm.utils.safesrc(linked_utils.asset_name(obj)),
        "pos": world_pos,
        "target": cam_target,
        "fov": cam_fov,
        "near": obj.data.clip_start,
        "far": obj.data.clip_end,
        "traits": _extract_traits(obj),
        "parent_index": parent_index,
    }
    if local_pos is not None:
        cam_data["local_pos"] = local_pos
        cam_data["local_target"] = local_target

    exporter.scene_data[scene_name]["cameras"].append(cam_data)


def _process_light(exporter, scene_name, obj, instance_matrix=None):
    """Process a light object and add to scene data."""
    # Compute world matrix considering instance transform
    world_matrix = instance_matrix @ obj.matrix_local if instance_matrix else obj.matrix_world
    light_dir = world_matrix.to_3x3().col[2]

    exporter.scene_data[scene_name]["lights"].append({
        "name": arm.utils.safesrc(linked_utils.asset_name(obj)),
        "pos": list(world_matrix.to_translation()),
        "color": list(obj.data.color),
        "energy": obj.data.energy,
        "dir": list(light_dir),
        "traits": _extract_traits(obj)
    })


def _process_empty_object(exporter, scene_name, obj, object_name_to_index=None):
    """Process an empty object and add to scene data as an ArmObject (no mesh).

    Empties are lightweight transform nodes in the unified objects[] array.
    They support full parent/child hierarchy, traits, and can serve as
    parents for cameras, mesh objects, or other empties.
    """
    world_matrix = obj.matrix_world

    # Parent resolution — empties can parent to any object type in objects[]
    parent_index = -1
    has_parent = False
    if object_name_to_index and obj.parent and obj.parent.name in object_name_to_index:
        parent_index = object_name_to_index[obj.parent.name]
        has_parent = True

    # World-space decomposition
    world_pos = list(world_matrix.to_translation())
    world_quat = world_matrix.to_quaternion()
    world_scale = world_matrix.to_scale()

    # Local transform for parented empties
    if has_parent:
        local_matrix = obj.matrix_local
        local_pos = list(local_matrix.to_translation())
        local_quat = local_matrix.to_quaternion()
        local_scale = local_matrix.to_scale()
    else:
        local_pos = world_pos
        local_quat = world_quat
        local_scale = world_scale

    obj_data = {
        "name": arm.utils.safesrc(obj.name),
        "is_empty": True,
        "pos": world_pos,
        "rot": [world_quat.x, world_quat.y, world_quat.z, world_quat.w],
        "scale": list(world_scale),
        "visible": not obj.hide_render,
        "bounds_min": [0, 0, 0],
        "bounds_max": [0, 0, 0],
        "traits": _extract_traits(obj),
        "is_static": True,  # Computed after trait_info is loaded
        "parent_index": parent_index,
        "local_pos": local_pos,
        "local_rot": [local_quat.x, local_quat.y, local_quat.z, local_quat.w],
        "local_scale": list(local_scale),
    }

    exporter.scene_data[scene_name]["objects"].append(obj_data)


def _process_mesh_object(exporter, scene_name, obj, instance_matrix=None, object_name_to_index=None, instancer_parent=None):
    """Process a mesh object and add to scene data.

    Args:
        instancer_parent: For instance collection objects, the mesh parent
            of the instancer Empty (e.g. Cube). Used to propagate hierarchy
            so instance collection members follow the parent at runtime.
    """
    mesh = obj.data
    if mesh not in exporter.exported_meshes:
        log.warn(f'Object "{obj.name}": mesh not exported, skipping')
        return
    mesh_name = exporter.exported_meshes[mesh]

    # Determine parent index (−1 = root / no parent)
    parent_index = -1
    has_parent = False
    parent_source = None  # 'direct' or 'instancer'

    if instance_matrix:
        # Instance collection object: parent is the instancer Empty's parent.
        # Intra-collection parents (obj.parent within the linked collection)
        # are skipped because names collide across multiple instances of the
        # same collection.  Flattening all members under the instancer's
        # parent is correct for the typical use-case (decoration objects that
        # rotate/move with a mesh parent like Cube).
        if (object_name_to_index and instancer_parent is not None
                and instancer_parent.name in object_name_to_index):
            parent_index = object_name_to_index[instancer_parent.name]
            has_parent = True
            parent_source = 'instancer'
    else:
        # Direct scene object: use Blender's obj.parent
        if object_name_to_index and obj.parent and obj.parent.name in object_name_to_index:
            parent_index = object_name_to_index[obj.parent.name]
            has_parent = True
            parent_source = 'direct'

    if has_parent:
        # Validation: block parented physics bodies
        parent_name = instancer_parent.name if parent_source == 'instancer' else obj.parent.name
        if obj.rigid_body is not None:
            wrd = bpy.data.worlds['Arm']
            if wrd.arm_physics != 'Disabled':
                log.warn(f'Object "{obj.name}": physics bodies on parented objects are not supported on N64. '
                         f'Parent "{parent_name}" will be ignored for physics sync.')

    # Compute world matrix considering instance transform
    world_matrix = instance_matrix @ obj.matrix_local if instance_matrix else obj.matrix_world

    # World-space decomposition (always needed for AABB computation and obj_data["pos"])
    world_pos = list(world_matrix.to_translation())
    world_quat = world_matrix.to_quaternion()
    world_scale = world_matrix.to_scale()

    # For parented objects, also compute local-space transform relative to parent.
    # For root objects, local == world.
    if has_parent and parent_source == 'instancer':
        # Instance collection member parented to instancer's parent.
        # Compute local transform relative to the instancer's parent world matrix.
        parent_world = instancer_parent.matrix_world
        local_matrix = parent_world.inverted() @ world_matrix
        local_pos = list(local_matrix.to_translation())
        local_quat = local_matrix.to_quaternion()
        local_scale = local_matrix.to_scale()
    elif has_parent and parent_source == 'direct':
        # Direct scene child: use Blender's local matrix (relative to parent)
        local_matrix = obj.matrix_local
        local_pos = list(local_matrix.to_translation())
        local_quat = local_matrix.to_quaternion()
        local_scale = local_matrix.to_scale()
    else:
        local_pos = world_pos
        local_quat = world_quat
        local_scale = world_scale

    # Export rotation as quaternion (XYZW order for T3D)

    # Compute AABB from mesh's bounding box (local space)
    bb = obj.bound_box
    local_min = [min(v[i] for v in bb) for i in range(3)]
    local_max = [max(v[i] for v in bb) for i in range(3)]
    half_extents = [
        (local_max[0] - local_min[0]) * 0.5,
        (local_max[1] - local_min[1]) * 0.5,
        (local_max[2] - local_min[2]) * 0.5
    ]

    # Pre-scale bounds by Blender object scale so they match position coordinate space.
    # Positions are exported in Blender world units (no SCALE_FACTOR).
    # SCALE_FACTOR is only applied to obj.transform.scale for the SRT rendering matrix.
    # Since bounds are used for frustum culling (which operates in the same space as
    # positions), they must be in Blender units, not in SCALE_FACTOR-compressed space.
    # Always use world scale for bounds (even for parented objects).
    bounds_min = [local_min[i] * world_scale[i] for i in range(3)]
    bounds_max = [local_max[i] * world_scale[i] for i in range(3)]
    # Handle negative scale (swap min/max per axis)
    for i in range(3):
        if bounds_min[i] > bounds_max[i]:
            bounds_min[i], bounds_max[i] = bounds_max[i], bounds_min[i]

    # Extract rigid body data
    rigid_body_data = _extract_rigid_body(exporter, obj, half_extents)

    obj_data = {
        "name": arm.utils.safesrc(linked_utils.asset_name(obj)),
        "mesh": f'MODEL_{mesh_name.upper()}',
        "pos": world_pos,
        "rot": [world_quat.x, world_quat.y, world_quat.z, world_quat.w],
        "scale": list(world_scale),
        "visible": not obj.hide_render,
        "bounds_min": bounds_min,
        "bounds_max": bounds_max,
        "traits": _extract_traits(obj),
        "is_static": True,  # Computed after trait_info is loaded
        "parent_index": parent_index,
        "local_pos": local_pos,
        "local_rot": [local_quat.x, local_quat.y, local_quat.z, local_quat.w],
        "local_scale": list(local_scale),
    }

    # Shadow casting/receiving — read from first material slot (Armory material props)
    cast_shadow = False
    receive_shadow = False
    if obj.material_slots:
        mat = obj.material_slots[0].material
        if mat:
            cast_shadow = getattr(mat, 'arm_cast_shadow', False)
            receive_shadow = getattr(mat, 'arm_receive_shadow', False)

    obj_data["cast_shadow"] = cast_shadow
    obj_data["receive_shadow"] = receive_shadow

    if rigid_body_data is not None:
        obj_data["rigid_body"] = rigid_body_data

    exporter.scene_data[scene_name]["objects"].append(obj_data)



def _extract_rigid_body(exporter, obj, half_extents):
    """Extract rigid body data from an object if present.

    Args:
        exporter: N64Exporter instance to update with physics flag
        obj: Blender object with potential rigid body
        half_extents: Pre-computed half extents from bounding box

    Returns:
        dict with rigid body data or None
    """
    wrd = bpy.data.worlds['Arm']
    if obj.rigid_body is None or wrd.arm_physics == 'Disabled':
        return None

    rb = obj.rigid_body
    shape = rb.collision_shape

    # N64 supports BOX, SPHERE, CAPSULE, and MESH (static only)
    rb_mesh_data = None
    if shape == 'SPHERE':
        rb_shape = "sphere"
        max_scale = max(obj.scale)
        rb_radius = max(half_extents) * max_scale
        rb_half_extents = None
        rb_half_height = None
    elif shape == 'CAPSULE':
        rb_shape = "capsule"
        rb_radius = max(half_extents[0], half_extents[1]) * max(obj.scale[0], obj.scale[1])
        total_height = half_extents[2] * 2.0 * obj.scale[2]
        rb_half_height = max(0.0, (total_height - 2.0 * rb_radius) / 2.0)
        rb_half_extents = None
    elif shape == 'MESH' and rb.type == 'PASSIVE':
        rb_shape = "mesh"
        rb_radius = None
        rb_half_height = None
        rb_half_extents = None
        rb_mesh_data = n64_utils.extract_collision_mesh(obj)
        if rb_mesh_data is None:
            log.warn(f'Object "{obj.name}": failed to extract mesh collision data, using BOX')
            rb_shape = "box"
            rb_half_extents = [
                half_extents[0] * obj.scale[0],
                half_extents[2] * obj.scale[2],
                half_extents[1] * obj.scale[1]
            ]
    else:
        rb_shape = "box"
        rb_radius = None
        rb_half_height = None
        rb_half_extents = [
            half_extents[0] * obj.scale[0],
            half_extents[2] * obj.scale[2],
            half_extents[1] * obj.scale[1]
        ]
        if shape not in ('BOX', 'SPHERE', 'CAPSULE'):
            if shape == 'MESH' and rb.type != 'PASSIVE':
                log.warn(f'Object "{obj.name}": MESH collision shape only supported for static (passive) bodies, using BOX')
            else:
                log.warn(f'Object "{obj.name}": collision shape "{shape}" not supported on N64, using BOX')

    # Mass (0 = static)
    is_static = rb.type == 'PASSIVE'
    rb_mass = 0.0 if is_static else rb.mass

    # Collision groups/masks
    col_group = 0
    for i, b in enumerate(rb.collision_collections):
        if b:
            col_group |= (1 << i)

    col_mask = 0
    if hasattr(obj, 'arm_rb_collision_filter_mask'):
        for i, b in enumerate(obj.arm_rb_collision_filter_mask):
            if b:
                col_mask |= (1 << i)
    else:
        col_mask = 1

    rigid_body_data = {
        "shape": rb_shape,
        "mass": rb_mass,
        "friction": rb.friction,
        "restitution": rb.restitution,
        "linear_damping": rb.linear_damping,
        "angular_damping": rb.angular_damping,
        "collision_group": col_group,
        "collision_mask": col_mask,
        "is_trigger": getattr(obj, 'arm_rb_trigger', False),
        "rb_type": rb.type,
        "is_animated": rb.kinematic,
        "is_dynamic": rb.enabled,
        "use_deactivation": rb.use_deactivation
    }

    if rb_shape == "sphere":
        rigid_body_data["radius"] = rb_radius
    elif rb_shape == "capsule":
        rigid_body_data["radius"] = rb_radius
        rigid_body_data["half_height"] = rb_half_height
    elif rb_shape == "mesh":
        rigid_body_data["mesh_data"] = rb_mesh_data
        exporter.mesh_collider_count += 1  # Track mesh collider count
    else:
        rigid_body_data["half_extents"] = rb_half_extents

    # Track dynamic body count (non-mesh shapes)
    if rb_shape != "mesh":
        exporter.physics_body_count += 1

    # Track contact body count (triggers or objects that may have contact events)
    if rigid_body_data.get("is_trigger", False):
        exporter.contact_body_count += 1

    exporter.has_physics = True
    return rigid_body_data


def _extract_traits(obj):
    """Extract traits from an object's trait list.

    Args:
        obj: Blender object (can be object, scene, light, camera)

    Returns:
        list of trait dictionaries
    """
    import json

    traits = []
    if not hasattr(obj, 'arm_traitlist'):
        return traits

    for trait in obj.arm_traitlist:
        if not trait.enabled_prop:
            continue
        if trait.type_prop != 'Haxe Script' or not trait.class_name_prop:
            continue

        props = {}
        type_overrides = {}
        if hasattr(trait, 'arm_traitpropslist'):
            for prop in trait.arm_traitpropslist:
                val = prop.get_value()
                override = getattr(prop, 'override_type_prop', 'auto')
                try:
                    val = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
                props[prop.name] = val
                if override and override != 'auto':
                    type_overrides[prop.name] = override

        traits.append({
            'class_name': trait.class_name_prop,
            'props': props,
            'type_overrides': type_overrides
        })

    return traits


def write_scenes(exporter):
    """Write all scene C files.

    Args:
        exporter: N64Exporter instance with scene_data and trait_info
    """
    write_scenes_c(exporter)
    write_scenes_h(exporter)

    # Apply coordinate conversion (Blender Z-up → N64 Y-up)
    codegen.convert_scene_data(exporter.scene_data)

    # Write converted scene data to C files
    for scene in bpy.data.scenes:
        if scene.library:
            continue
        if linked_export.is_temp_scene(scene):
            continue
        write_scene_c(exporter, scene)


def write_scene_c(exporter, scene):
    """Generate individual scene C file.

    Args:
        exporter: N64Exporter instance with scene_data and trait_info
        scene: Blender scene object
    """
    scene_name = arm.utils.safesrc(scene.name).lower()
    scene_data = exporter.scene_data[scene_name]

    clear_color = scene_data['world']['clear_color']
    ambient_color = scene_data['world']['ambient_color']

    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'scenes', 'scene.c.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'scenes', f'{scene_name}.c')
    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    scene_traits = scene_data.get('traits', [])

    output = tmpl_content.format(
        scene_name=scene_name,
        cr=n64_utils.to_uint8(clear_color[0]),
        cg=n64_utils.to_uint8(clear_color[1]),
        cb=n64_utils.to_uint8(clear_color[2]),
        ar=n64_utils.to_uint8(ambient_color[0]),
        ag=n64_utils.to_uint8(ambient_color[1]),
        ab=n64_utils.to_uint8(ambient_color[2]),
        shadow_color_block=codegen.generate_shadow_color_block(scene_data['lights'], ambient_color),
        camera_count=len(scene_data['cameras']),
        cameras_block=codegen.generate_camera_block(scene_data['cameras'], exporter.trait_info, scene_name),
        light_count=len(scene_data['lights']),
        lights_block=codegen.generate_light_block(scene_data['lights'], exporter.trait_info, scene_name),
        object_count=len(scene_data['objects']),
        objects_block=codegen.generate_object_block(scene_data['objects'], exporter.trait_info, scene_name),
        physics_block=codegen.generate_physics_block(scene_data['objects'], scene_data['world']),
        contact_subs_block=codegen.generate_contact_subscriptions_block(scene_data['objects'], exporter.trait_info),
        scene_trait_count=len(scene_traits),
        scene_traits_block=codegen.generate_scene_traits_block(scene_traits, exporter.trait_info, scene_name)
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_scenes_c(exporter):
    """Generate scenes.c master file.

    Args:
        exporter: N64Exporter instance
    """
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'data', 'scenes.c.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'data', 'scenes.c')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    init_lines = []
    init_switch_cases_lines = []
    name_entry_lines = []
    group_data_lines = []
    group_lookup_lines = []
    scene_count = 0

    for scene in bpy.data.scenes:
        if scene.library:
            continue
        if linked_export.is_temp_scene(scene):
            continue
        scene_name = arm.utils.safesrc(scene.name).lower()
        original_name = scene.name
        init_lines.append(f'    scene_{scene_name}_init(&g_scenes[SCENE_{scene_name.upper()}]);')
        init_switch_cases_lines.append(f'        case SCENE_{scene_name.upper()}:\n'
                                       f'            scene_{scene_name}_init(&g_scenes[SCENE_{scene_name.upper()}]);\n'
                                       f'            break;')
        name_entry_lines.append(f'    {{"{original_name}", SCENE_{scene_name.upper()}}},')

        # Generate group data for this scene
        scene_data = exporter.scene_data.get(scene_name, {})
        groups = scene_data.get('groups', {})
        if groups:
            entries = []
            for norm_name, group_info in groups.items():
                orig_name = group_info['original_name']
                count = group_info['count']
                entries.append(f'    {{"{orig_name}", {count}}}')
            group_data_lines.append(f'static const SceneGroupEntry g_scene_{scene_name}_groups[] = {{\n' +
                                    ',\n'.join(entries) + '\n};')
            group_data_lines.append(f'static const uint16_t g_scene_{scene_name}_group_count = {len(groups)};')

            # Add lookup case
            group_lookup_lines.append(f'    if (g_current_scene == SCENE_{scene_name.upper()}) {{')
            group_lookup_lines.append(f'        for (uint16_t i = 0; i < g_scene_{scene_name}_group_count; i++) {{')
            group_lookup_lines.append(f'            if (strcmp(group_name, g_scene_{scene_name}_groups[i].name) == 0) {{')
            group_lookup_lines.append(f'                return g_scene_{scene_name}_groups[i].count;')
            group_lookup_lines.append(f'            }}')
            group_lookup_lines.append(f'        }}')
            group_lookup_lines.append(f'    }}')

        scene_count += 1

    scene_inits = '\n'.join(init_lines)
    scene_init_switch_cases = '\n'.join(init_switch_cases_lines)
    scene_name_entries = '\n'.join(name_entry_lines)
    scene_group_data = '\n'.join(group_data_lines) if group_data_lines else '// No groups defined'
    scene_group_lookup = '\n'.join(group_lookup_lines) if group_lookup_lines else '    // No groups defined'

    output = tmpl_content.format(
        scene_inits=scene_inits,
        scene_init_switch_cases=scene_init_switch_cases,
        scene_name_entries=scene_name_entries,
        scene_count=scene_count,
        scene_group_data=scene_group_data,
        scene_group_lookup=scene_group_lookup
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_scenes_h(exporter):
    """Generate scenes.h header file.

    Args:
        exporter: N64Exporter instance
    """
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'data', 'scenes.h.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'data', 'scenes.h')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    enum_lines = []
    declaration_lines = []
    scene_count = 0
    for scene in bpy.data.scenes:
        if scene.library:
            continue
        if linked_export.is_temp_scene(scene):
            continue
        scene_name = arm.utils.safesrc(scene.name).lower()
        enum_lines.append(f'    SCENE_{scene_name.upper()} = {scene_count},')
        declaration_lines.append(f'void scene_{scene_name.lower()}_init(ArmScene *scene);')
        scene_count += 1
    scene_enum_entries = '\n'.join(enum_lines)
    scene_declarations = '\n'.join(declaration_lines)

    output = tmpl_content.format(
        scene_enum_entries=scene_enum_entries,
        scene_declarations=scene_declarations,
        scene_count=scene_count
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_main(exporter):
    """Generate main.c from template.

    Args:
        exporter: N64Exporter instance with autoload_info
    """
    wrd = bpy.data.worlds['Arm']
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'main.c.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'main.c')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    # Get physics fixed timestep from Armory settings (default 0.02 = 50Hz)
    fixed_timestep = getattr(wrd, 'arm_physics_fixed_step', 0.02)

    # Get physics debug mode
    physics_debug_mode = n64_utils.get_physics_debug_mode()

    # Autoload include and init
    has_autoloads = exporter.autoload_info.get('has_autoloads', False)
    autoloads_include = '#include "autoloads/autoloads.h"' if has_autoloads else ''
    autoloads_init = '    autoloads_init();\n' if has_autoloads else ''

    output = tmpl_content.format(
        initial_scene_id=f'SCENE_{arm.utils.safesrc(wrd.arm_exporterlist[wrd.arm_exporterlist_index].arm_project_scene.name).upper()}',
        fixed_timestep=fixed_timestep,
        physics_debug_mode=physics_debug_mode,
        autoloads_include=autoloads_include,
        autoloads_init=autoloads_init
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_models(exporter):
    """Write models.c and models.h files.

    Args:
        exporter: N64Exporter instance with exported_meshes
    """
    write_models_c(exporter)
    write_models_h(exporter)


def write_models_c(exporter):
    """Generate models.c from template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'data', 'models.c.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'data', 'models.c')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    lines = []
    for model_name in exporter.exported_meshes.values():
        lines.append(f'    "rom:/{model_name}.t3dm"')
    mesh_paths = ',\n'.join(lines)

    output = tmpl_content.format(
        mesh_paths=mesh_paths,
        model_count=len(exporter.exported_meshes)
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_models_h(exporter):
    """Generate models.h from template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'data', 'models.h.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'data', 'models.h')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    lines = []
    for i, model_name in enumerate(exporter.exported_meshes.values()):
        lines.append(f'    MODEL_{model_name.upper()} = {i},')
    model_enum_entries = '\n'.join(lines)

    output = tmpl_content.format(
        model_enum_entries=model_enum_entries,
        model_count=len(exporter.exported_meshes)
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)
