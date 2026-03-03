/**
 * Planar Shadow Projection
 * ========================
 *
 * Per-receiver planar shadow projection for N64.
 *
 * Each object with receive_shadow=true defines a shadow receiving plane.
 * The plane is derived from the receiver's world-space AABB top face
 * and the receiver's orientation (supports inclined surfaces).
 *
 * Each object with cast_shadow=true has its display list drawn a second
 * time, projected onto each receiver's plane along the directional
 * light direction, with a flat semi-transparent shadow color.
 *
 * Shadow color is auto-derived from light energy + ambient at export time
 * (WYSIWYG from Blender — no manual tuning required).
 */

#pragma once

#include <t3d/t3dmath.h>
#include "types.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Compute a planar projection matrix.
 *
 * Projects geometry onto the plane defined by (normal, point_on_plane)
 * along the given light direction.
 *
 * The resulting matrix M satisfies: for any vertex V,
 *   M * V = V projected onto the plane along light_dir.
 *
 * @param out       Output 4x4 float matrix (column-major, T3DMat4 layout)
 * @param plane_n   Plane normal (unit length, pointing away from surface)
 * @param plane_d   Plane distance: dot(plane_n, point_on_plane)
 * @param light_dir Light direction (pointing FROM light TO scene, normalized)
 */
void shadow_projection_matrix(T3DMat4 *out, const T3DVec3 *plane_n, float plane_d, const T3DVec3 *light_dir);

/**
 * Draw shadows for all casters onto all receivers.
 *
 * Called after renderer_draw_scene() while the RDP is still attached.
 * Iterates receivers, computes projection plane, then draws each visible
 * caster's display list with the shadow projection matrix.
 *
 * @param viewport  Current viewport (for frustum data)
 * @param scene     Scene with objects, lights, and shadow color
 */
void renderer_draw_shadows(T3DViewport *viewport, ArmScene *scene);

#ifdef __cplusplus
}
#endif
