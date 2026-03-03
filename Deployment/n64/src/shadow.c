/**
 * Planar Shadow Projection — N64 Implementation
 *
 * Uses the classic planar projection technique from the PS1/N64 era.
 *
 * For each receiver (receive_shadow=true):
 *   1. Compute receiver's surface plane from its world matrix
 *   2. Project receiver corners to screen space → set RDP scissor rectangle
 *   3. Build projection matrix: shadow_mat = proj * caster.world_mat
 *   4. Draw caster's geometry-only DPL with flat semi-transparent black
 *
 * KEY CONVENTIONS:
 *   - light->dir points TOWARD the light (tiny3d convention)
 *   - Shadow projection needs ray direction (FROM light), so we negate it
 *   - Projection matrix is normalized (divided by dot) so m[3][3]=1.0,
 *     which is required for correct RSP vertex transformation
 *   - Projection is degenerate so we disable backface culling
 *
 * The RDP is configured for:
 *   - Flat color combiner (shadow_color from scene)
 *   - Alpha blending (multiply blend)
 *   - No backface culling (degenerate projection)
 *   - ZMODE_STANDARD + Z compare ON + Z write ON
 *   - Z-write prevents overdraw: overlapping triangles at same Z are rejected
 *   - Per-receiver scissor rectangle prevents shadows from leaking into air
 */

#include <libdragon.h>
#include <math.h>
#include <string.h>
#include <t3d/t3d.h>
#include <t3d/t3dmath.h>
#include <t3d/t3dmodel.h>

#include "shadow.h"
#include "types.h"

// Ring buffer of fixed-point matrices to avoid RSP DMA race conditions.
// Must be large enough that the RSP finishes reading old entries before the
// CPU overwrites them. 64 slots is sufficient for ~20 shadow draws.
#define SHADOW_MAT_POOL_SIZE 64
static T3DMat4FP *shadow_mat_pool = NULL;
static uint8_t shadow_mat_idx = 0;

// Minimum receiver horizontal area (in Blender units²) to qualify as a
// shadow-receiving surface. This prevents tiny objects (gems, rocks, plants)
// from acting as receivers — their tiny shadow planes cause projected
// geometry to extend far into empty space, rendering as giant black shapes.
#define MIN_RECEIVER_AREA 16.0f  // 4×4 minimum (platforms are typically 10×10)

void shadow_projection_matrix(T3DMat4 *out, const T3DVec3 *plane_n, float plane_d, const T3DVec3 *light_dir)
{
    // light_dir must point FROM light TOWARD the scene (ray direction).
    //
    // Dot product of light ray direction and plane normal.
    // For a downward light (0,-1,0) and upward normal (0,1,0): dot = -1
    float dot = plane_n->v[0] * light_dir->v[0]
              + plane_n->v[1] * light_dir->v[1]
              + plane_n->v[2] * light_dir->v[2];

    // Skip if light is parallel to plane (would project to infinity)
    if (fabsf(dot) < 0.001f) {
        // Return identity — no shadow
        memset(out, 0, sizeof(*out));
        out->m[0][0] = 1.0f;
        out->m[1][1] = 1.0f;
        out->m[2][2] = 1.0f;
        out->m[3][3] = 1.0f;
        return;
    }

    // Normalize by 1/dot so the resulting matrix has m[3][3] = 1.0
    // This is REQUIRED for the N64 RSP which expects affine model matrices
    // (w-component must be preserved as 1.0 for correct perspective divide).
    float inv_dot = 1.0f / dot;

    // Plane equation coefficients: ax + by + cz + d = 0
    float a = plane_n->v[0];
    float b = plane_n->v[1];
    float c = plane_n->v[2];
    float d = -plane_d;  // d in plane equation = -dot(N, point_on_plane)

    float lx = light_dir->v[0];
    float ly = light_dir->v[1];
    float lz = light_dir->v[2];

    // Shadow projection matrix (column-major m[col][row]):
    //   M = (dot*I - L⊗P) / dot
    // where P = (a,b,c,d) is the plane, L = (lx,ly,lz,0) is light ray dir.
    // Dividing by dot normalizes so M[3][3] = 1.0.

    // Column 0
    out->m[0][0] = 1.0f - lx * a * inv_dot;
    out->m[0][1] =      - ly * a * inv_dot;
    out->m[0][2] =      - lz * a * inv_dot;
    out->m[0][3] = 0.0f;

    // Column 1
    out->m[1][0] =      - lx * b * inv_dot;
    out->m[1][1] = 1.0f - ly * b * inv_dot;
    out->m[1][2] =      - lz * b * inv_dot;
    out->m[1][3] = 0.0f;

    // Column 2
    out->m[2][0] =      - lx * c * inv_dot;
    out->m[2][1] =      - ly * c * inv_dot;
    out->m[2][2] = 1.0f - lz * c * inv_dot;
    out->m[2][3] = 0.0f;

    // Column 3 (translation — places shadow on the plane)
    out->m[3][0] =      - lx * d * inv_dot;
    out->m[3][1] =      - ly * d * inv_dot;
    out->m[3][2] =      - lz * d * inv_dot;
    out->m[3][3] = 1.0f;
}

void renderer_draw_shadows(T3DViewport *viewport, ArmScene *scene)
{
    if (!viewport || scene->light_count == 0) return;

    // Lazy-allocate the ring buffer of fixed-point matrices
    if (!shadow_mat_pool) {
        shadow_mat_pool = malloc_uncached(sizeof(T3DMat4FP) * SHADOW_MAT_POOL_SIZE);
        if (!shadow_mat_pool) return;
    }
    shadow_mat_idx = 0;

    // Get light ray direction: negate light->dir (tiny3d points TOWARD light)
    ArmLight *light = &scene->lights[0];
    T3DVec3 ray_dir;
    ray_dir.v[0] = -light->dir.v[0];
    ray_dir.v[1] = -light->dir.v[1];
    ray_dir.v[2] = -light->dir.v[2];

    // Normalize ray direction
    float len2 = ray_dir.v[0]*ray_dir.v[0] + ray_dir.v[1]*ray_dir.v[1] + ray_dir.v[2]*ray_dir.v[2];
    if (len2 < 0.001f) return;
    if (len2 < 0.99f || len2 > 1.01f) {
        float inv_len = 1.0f / sqrtf(len2);
        ray_dir.v[0] *= inv_len;
        ray_dir.v[1] *= inv_len;
        ray_dir.v[2] *= inv_len;
    }

    // Light must point somewhat downward for ground shadows to make sense
    if (ray_dir.v[1] >= -0.01f) return;

    // === Find valid receivers (large horizontal surfaces only) ===
    // Small objects (gems, rocks, plants) must NOT be receivers — their tiny
    // shadow planes cause projected geometry to extend into empty space,
    // producing giant black shapes that fill the screen.

    // === Configure RDP for shadow rendering ===
    t3d_tri_sync();
    rdpq_sync_pipe();
    rdpq_set_mode_standard();
    rdpq_mode_combiner(RDPQ_COMBINER_FLAT);
    rdpq_set_prim_color(RGBA32(
        scene->world.shadow_color[0],
        scene->world.shadow_color[1],
        scene->world.shadow_color[2],
        scene->world.shadow_color[3]
    ));
    rdpq_mode_blender(RDPQ_BLENDER_MULTIPLY);

    // --- Z-buffer: ZMODE_STANDARD + Z-write ON ---
    // Z-write ON eliminates overdraw darkening: all of a caster's projected
    // triangles lie on the same shadow plane. The first triangle writes its Z;
    // subsequent overlapping triangles (e.g., pine tree branch layers) are at
    // the same Z and FAIL the strict depth test, so each pixel is shaded
    // exactly once — perfectly consistent shadow color.
    //
    // Shadows are clipped to each receiver's screen-space bounding box via
    // rdpq_set_scissor(). This prevents shadow geometry from rendering past
    // platform edges into empty space ("casting on air"). No depth_offset is
    // needed — the scissor handles the containment problem, while Z_BIAS
    // provides the depth separation.
    rdpq_mode_zbuf(true, true);
    t3d_state_set_drawflags(T3D_FLAG_DEPTH);

    const float Z_BIAS = 0.005f;

    // Screen dimensions for scissor clamping
    const float scr_w = (float)viewport->size[0];
    const float scr_h = (float)viewport->size[1];

    t3d_matrix_push_pos(1);

    for (uint16_t r = 0; r < scene->object_count; r++) {
        ArmObject *receiver = &scene->objects[r];
        if (!receiver->receive_shadow || receiver->is_removed || !receiver->visible) continue;

        // --- Filter: only large horizontal surfaces can receive shadows ---
        float rx_extent = receiver->cached_world_aabb_max.v[0] - receiver->cached_world_aabb_min.v[0];
        float rz_extent = receiver->cached_world_aabb_max.v[2] - receiver->cached_world_aabb_min.v[2];
        float receiver_area = rx_extent * rz_extent;
        if (receiver_area < MIN_RECEIVER_AREA) continue;

        // Receiver's XZ bounds (for clipping shadow draws)
        float recv_xmin = receiver->cached_world_aabb_min.v[0];
        float recv_xmax = receiver->cached_world_aabb_max.v[0];
        float recv_zmin = receiver->cached_world_aabb_min.v[2];
        float recv_zmax = receiver->cached_world_aabb_max.v[2];
        float recv_top  = receiver->cached_world_aabb_max.v[1];

        // --- Scissor: clip shadows to receiver's screen-space bounds ---
        // Project the 4 corners of the receiver's top face to screen space.
        // The scissor rectangle prevents shadow geometry from rendering past
        // the platform edges into empty space (the "casting on air" problem).
        {
            T3DVec3 corners[4];
            corners[0] = (T3DVec3){{recv_xmin, recv_top, recv_zmin}};
            corners[1] = (T3DVec3){{recv_xmax, recv_top, recv_zmin}};
            corners[2] = (T3DVec3){{recv_xmin, recv_top, recv_zmax}};
            corners[3] = (T3DVec3){{recv_xmax, recv_top, recv_zmax}};

            float sx_min = scr_w, sy_min = scr_h;
            float sx_max = 0.0f, sy_max = 0.0f;

            for (int i = 0; i < 4; i++) {
                T3DVec3 scr;
                t3d_viewport_calc_viewspace_pos(viewport, &scr, &corners[i]);
                if (scr.v[0] < sx_min) sx_min = scr.v[0];
                if (scr.v[0] > sx_max) sx_max = scr.v[0];
                if (scr.v[1] < sy_min) sy_min = scr.v[1];
                if (scr.v[1] > sy_max) sy_max = scr.v[1];
            }

            // Clamp to screen bounds and add 1px margin for rounding
            if (sx_min < 0.0f) sx_min = 0.0f;
            if (sy_min < 0.0f) sy_min = 0.0f;
            if (sx_max > scr_w) sx_max = scr_w;
            if (sy_max > scr_h) sy_max = scr_h;

            // Skip this receiver if entirely off-screen
            if (sx_min >= sx_max || sy_min >= sy_max) continue;

            rdpq_set_scissor(
                (int32_t)sx_min, (int32_t)sy_min,
                (int32_t)(sx_max + 0.99f), (int32_t)(sy_max + 0.99f)
            );
        }

        // Extract and normalize plane normal from receiver's Y-axis
        T3DVec3 plane_normal;
        plane_normal.v[0] = receiver->world_mat.m[1][0];
        plane_normal.v[1] = receiver->world_mat.m[1][1];
        plane_normal.v[2] = receiver->world_mat.m[1][2];

        float nlen2 = plane_normal.v[0]*plane_normal.v[0]
                    + plane_normal.v[1]*plane_normal.v[1]
                    + plane_normal.v[2]*plane_normal.v[2];
        if (nlen2 < 0.000001f) continue;
        if (nlen2 < 0.99f || nlen2 > 1.01f) {
            float inv_len = 1.0f / sqrtf(nlen2);
            plane_normal.v[0] *= inv_len;
            plane_normal.v[1] *= inv_len;
            plane_normal.v[2] *= inv_len;
        }

        // Shadow plane sits at the receiver's top surface + small bias
        float plane_y = recv_top + Z_BIAS;

        // Compute plane point and distance
        T3DVec3 plane_point;
        plane_point.v[0] = (recv_xmin + recv_xmax) * 0.5f;
        plane_point.v[1] = plane_y;
        plane_point.v[2] = (recv_zmin + recv_zmax) * 0.5f;

        float plane_d = plane_normal.v[0] * plane_point.v[0]
                      + plane_normal.v[1] * plane_point.v[1]
                      + plane_normal.v[2] * plane_point.v[2];

        // Build projection matrix for this receiver
        T3DMat4 proj_mat;
        shadow_projection_matrix(&proj_mat, &plane_normal, plane_d, &ray_dir);

        // Draw each caster projected onto this receiver
        for (uint16_t c = 0; c < scene->object_count; c++) {
            if (c == r) continue;

            ArmObject *caster = &scene->objects[c];
            if (!caster->cast_shadow || caster->is_removed || !caster->visible || !caster->shadow_dpl) continue;

            // --- Filter: caster must be ABOVE receiver surface ---
            // Prevents ground from casting upward onto elevated surfaces
            float caster_bottom = caster->cached_world_aabb_min.v[1];
            if (caster_bottom < recv_top - 1.0f) continue;

            // --- Filter: caster must not be a large platform itself ---
            // Platforms casting onto other platforms creates huge shadows
            float cx_extent = caster->cached_world_aabb_max.v[0] - caster->cached_world_aabb_min.v[0];
            float cz_extent = caster->cached_world_aabb_max.v[2] - caster->cached_world_aabb_min.v[2];
            if (cx_extent * cz_extent >= MIN_RECEIVER_AREA) continue;

            // --- Filter: project caster center onto receiver plane ---
            // Skip if the projected shadow doesn't overlap the receiver's XZ bounds.
            // This prevents shadows from extending into empty space (which renders
            // as giant black shapes against the background's max-depth Z-buffer).
            float caster_cx = (caster->cached_world_aabb_min.v[0] + caster->cached_world_aabb_max.v[0]) * 0.5f;
            float caster_cy = (caster->cached_world_aabb_min.v[1] + caster->cached_world_aabb_max.v[1]) * 0.5f;
            float caster_cz = (caster->cached_world_aabb_min.v[2] + caster->cached_world_aabb_max.v[2]) * 0.5f;

            // Project caster center along light ray onto the receiver plane
            float t = (plane_y - caster_cy) / ray_dir.v[1];
            float shadow_cx = caster_cx + ray_dir.v[0] * t;
            float shadow_cz = caster_cz + ray_dir.v[2] * t;

            // Caster's XZ half-extent (use larger axis for margin)
            float caster_r = (cx_extent > cz_extent ? cx_extent : cz_extent) * 0.5f;
            float margin = caster_r + 2.0f;  // Extra margin for light-direction shear

            // AABB overlap test: projected shadow center ± margin vs receiver XZ
            if (shadow_cx + margin < recv_xmin || shadow_cx - margin > recv_xmax ||
                shadow_cz + margin < recv_zmin || shadow_cz - margin > recv_zmax) {
                continue;
            }

            // --- Safety: don't exceed ring buffer ---
            if (shadow_mat_idx >= SHADOW_MAT_POOL_SIZE) break;

            // Compose: shadow_world = projection * caster_world
            T3DMat4 shadow_world;
            t3d_mat4_mul(&shadow_world, &proj_mat, &caster->world_mat);

            // Convert to fixed-point for RSP
            T3DMat4FP *mat = &shadow_mat_pool[shadow_mat_idx];
            shadow_mat_idx++;
            t3d_mat4_to_fixed(mat, &shadow_world);

            // Set matrix and draw geometry-only (no material override)
            t3d_matrix_set(mat, true);
            rspq_block_run(caster->shadow_dpl);
        }

        // Stop if ring buffer is full
        if (shadow_mat_idx >= SHADOW_MAT_POOL_SIZE) break;
    }

    t3d_matrix_pop(1);

    // === Restore normal rendering state ===
    rdpq_sync_pipe();

    // Restore full-screen scissor (was narrowed per-receiver)
    rdpq_set_scissor(0, 0, (int32_t)scr_w, (int32_t)scr_h);

    t3d_state_set_drawflags(T3D_FLAG_DEPTH | T3D_FLAG_CULL_BACK);
}
