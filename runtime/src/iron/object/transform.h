#pragma once

#include "../../types.h"

#ifdef __cplusplus
extern "C" {
#endif

// Transform manipulation
void it_translate(ArmTransform *t, float x, float y, float z);
void it_move(ArmTransform *t, float dx, float dy, float dz, float scale);
void it_rotate_axis(ArmTransform *t, float ax, float ay, float az, float angle);
void it_rotate_axis_global(ArmTransform *t, float ax, float ay, float az, float angle);

// Setters
void it_set_loc(ArmTransform *t, float x, float y, float z);
void it_set_rot(ArmTransform *t, float x, float y, float z, float w);
void it_set_rot_euler(ArmTransform *t, float x, float y, float z);
void it_set_scale(ArmTransform *t, float x, float y, float z);
void it_reset(ArmTransform *t);

// Direction vectors (returns in provided out vector)
void it_look(const ArmTransform *t, T3DVec3 *out);
void it_right(const ArmTransform *t, T3DVec3 *out);
void it_up(const ArmTransform *t, T3DVec3 *out);

// A transform keeps N64 space (exporter/utils.py): Blender's axes (x, y, z) as
// (x, z, -y), its scale x1/64 (models are x64). A parented object keeps its
// local position x64 and its scale without the factor (the parent's world
// matrix has it). The functions below read and write Blender's values, as
// Iron's transform.loc, scale and rot.

// N64 position per Blender unit, N64 scale per Blender scale
static inline float it_loc_k(const ArmTransform *t) { return t->parented ? 64.0f : 1.0f; }
static inline float it_scale_k(const ArmTransform *t) { return t->parented ? 1.0f : 0.015625f; }

static inline ArmVec4 it_get_loc(const ArmTransform *t) {
    float u = t->parented ? 0.015625f : 1.0f;
    return (ArmVec4){t->loc.x * u, -t->loc.z * u, t->loc.y * u, 1.0f};
}
static inline void it_set_loc_b(ArmTransform *t, float x, float y, float z) {
    float k = it_loc_k(t);
    t->loc.x = x * k; t->loc.y = z * k; t->loc.z = -y * k;
    t->dirty = TRANSFORM_DIRTY_FRAMES;
}
static inline void it_set_loc_x(ArmTransform *t, float v) { t->loc.x = v * it_loc_k(t); t->dirty = TRANSFORM_DIRTY_FRAMES; }
static inline void it_set_loc_y(ArmTransform *t, float v) { t->loc.z = -v * it_loc_k(t); t->dirty = TRANSFORM_DIRTY_FRAMES; }
static inline void it_set_loc_z(ArmTransform *t, float v) { t->loc.y = v * it_loc_k(t); t->dirty = TRANSFORM_DIRTY_FRAMES; }

static inline ArmVec4 it_get_scale(const ArmTransform *t) {
    float u = t->parented ? 1.0f : 64.0f;
    return (ArmVec4){t->scale.x * u, t->scale.z * u, t->scale.y * u, 1.0f};
}
static inline void it_set_scale_b(ArmTransform *t, float x, float y, float z) {
    float k = it_scale_k(t);
    t->scale.x = x * k; t->scale.y = z * k; t->scale.z = y * k;
    t->dirty = TRANSFORM_DIRTY_FRAMES;
}
static inline void it_set_scale_x(ArmTransform *t, float v) { t->scale.x = v * it_scale_k(t); t->dirty = TRANSFORM_DIRTY_FRAMES; }
static inline void it_set_scale_y(ArmTransform *t, float v) { t->scale.z = v * it_scale_k(t); t->dirty = TRANSFORM_DIRTY_FRAMES; }
static inline void it_set_scale_z(ArmTransform *t, float v) { t->scale.y = v * it_scale_k(t); t->dirty = TRANSFORM_DIRTY_FRAMES; }

// Rotation quaternion: Blender (x, y, z, w) is N64 (x, z, -y, w)
static inline ArmVec4 it_get_rot(const ArmTransform *t) {
    return (ArmVec4){t->rot.x, -t->rot.z, t->rot.y, t->rot.w};
}
static inline void it_set_rot_b(ArmTransform *t, float x, float y, float z, float w) {
    t->rot.x = x; t->rot.y = z; t->rot.z = -y; t->rot.w = w;
    t->dirty = TRANSFORM_DIRTY_FRAMES;
}
static inline void it_set_rot_x(ArmTransform *t, float v) { t->rot.x = v; t->dirty = TRANSFORM_DIRTY_FRAMES; }
static inline void it_set_rot_y(ArmTransform *t, float v) { t->rot.z = -v; t->dirty = TRANSFORM_DIRTY_FRAMES; }
static inline void it_set_rot_z(ArmTransform *t, float v) { t->rot.y = v; t->dirty = TRANSFORM_DIRTY_FRAMES; }
static inline void it_set_rot_w(ArmTransform *t, float v) { t->rot.w = v; t->dirty = TRANSFORM_DIRTY_FRAMES; }

// World position in Blender's axes and units (Iron's worldx/y/z): a root's
// location; a parented object's from its world matrix, as last built (only
// objects are parented, so the transform is its ArmObject's)
static inline ArmVec4 it_world(const ArmTransform *t) {
    if (!t->parented) return it_get_loc(t);
    const T3DMat4 *m = &((const ArmObject*)t)->world_mat;
    return (ArmVec4){m->m[3][0], -m->m[3][2], m->m[3][1], 1.0f};
}
static inline float it_world_x(const ArmTransform *t) { return it_world(t).x; }
static inline float it_world_y(const ArmTransform *t) { return it_world(t).y; }
static inline float it_world_z(const ArmTransform *t) { return it_world(t).z; }

#ifdef __cplusplus
}
#endif
