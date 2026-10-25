"""
Physics Exporter - Handles physics engine file generation for N64.

This module provides functions for copying and configuring the Oimo physics
engine files for N64 export.
"""

import os

import arm.utils
import arm.n64.utils as n64_utils


def write_physics(exporter):
    """Copy physics engine files if physics is enabled.

    Copies the Oimo physics library and generates configuration templates.
    Uses dynamically calculated body counts from scene export with buffers.

    Args:
        exporter: N64Exporter instance (checks exporter.has_physics)
    """
    if not exporter.has_physics:
        return

    # Copy oimo library first (header-only physics engine)
    # Must be done BEFORE rendering templates, since copy_dir clears the target
    n64_utils.copy_dir('oimo', 'src')

    # Its limits are in limits.h (codegen/limits.py)
    limits = exporter.limits
    print(f"  Physics limits: bodies={limits['MAX_PHYSICS_BODIES']} (from {exporter.max_physics_bodies}), "
          f"mesh_colliders={limits['MAX_MESH_COLLIDERS']} (from {exporter.max_mesh_colliders}), "
          f"contact_bodies={limits['MAX_CONTACT_BODIES']} (from {exporter.max_contact_bodies}), "
          f"bvh_triangles={limits['OIMO_BVH_MAX_TRIANGLES']} (from {exporter.max_mesh_triangles}), "
          f"bvh_depth={limits['OIMO_BVH_MAX_DEPTH']}")

    n64_utils.copy_src('physics_events.h', 'src/events')
    # Copy physics_events.c
    n64_utils.copy_src('physics_events.c', 'src/events')

    # Copy physics debug drawing files only if debug is enabled
    if n64_utils.get_physics_debug_mode() > 0:
        n64_utils.copy_src('physics_debug.h', 'src/oimo/debug')
        n64_utils.copy_src('physics_debug.c', 'src/oimo/debug')
