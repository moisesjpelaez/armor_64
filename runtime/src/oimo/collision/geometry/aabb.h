#pragma once

#include "../../common/setting.h"
#include "../../common/vec3.h"

typedef struct OimoAabb {
    OimoVec3 min;  // Minimum corner
    OimoVec3 max;  // Maximum corner
} OimoAabb;

// Create empty AABB (both corners at zero)
static inline OimoAabb oimo_aabb_zero(void) {
    OimoAabb aabb;
    aabb.min = oimo_vec3_zero();
    aabb.max = oimo_vec3_zero();
    return aabb;
}

// Create AABB from min/max corners
static inline OimoAabb oimo_aabb(const OimoVec3* min, const OimoVec3* max) {
    OimoAabb aabb;
    aabb.min = oimo_vec3_copy(min);
    aabb.max = oimo_vec3_copy(max);
    return aabb;
}

// Copy AABB
static inline OimoAabb oimo_aabb_copy(const OimoAabb* aabb) {
    OimoAabb result;
    result.min = oimo_vec3_copy(&aabb->min);
    result.max = oimo_vec3_copy(&aabb->max);
    return result;
}

// Get center of AABB: (min + max) / 2
static inline OimoVec3 oimo_aabb_get_center(const OimoAabb* aabb) {
    OimoVec3 center = oimo_vec3_add(aabb->min, aabb->max);
    return oimo_vec3_scale(center, 0.5f);
}

// Get half-extents of AABB: (max - min) / 2
static inline OimoVec3 oimo_aabb_get_extents(const OimoAabb* aabb) {
    OimoVec3 extents = oimo_vec3_sub(aabb->max, aabb->min);
    return oimo_vec3_scale(extents, 0.5f);
}

// Set min corner
static inline void oimo_aabb_set_min(OimoAabb* aabb, const OimoVec3* min) {
    oimo_vec3_copy_to(&aabb->min, min);
}

// Set max corner
static inline void oimo_aabb_set_max(OimoAabb* aabb, const OimoVec3* max) {
    oimo_vec3_copy_to(&aabb->max, max);
}

// Set both corners
static inline void oimo_aabb_set(OimoAabb* aabb, const OimoVec3* min, const OimoVec3* max) {
    oimo_vec3_copy_to(&aabb->min, min);
    oimo_vec3_copy_to(&aabb->max, max);
}

// Check if two AABBs overlap
static inline int oimo_aabb_overlap(const OimoAabb* a, const OimoAabb* b) {
    // AABBs overlap if they overlap on all three axes
    return (a->min.x <= b->max.x && a->max.x >= b->min.x) &&
           (a->min.y <= b->max.y && a->max.y >= b->min.y) &&
           (a->min.z <= b->max.z && a->max.z >= b->min.z);
}

// Combine two AABBs into one that contains both
static inline OimoAabb oimo_aabb_combine(const OimoAabb* a, const OimoAabb* b) {
    OimoAabb result;
    result.min.x = oimo_min(a->min.x, b->min.x);
    result.min.y = oimo_min(a->min.y, b->min.y);
    result.min.z = oimo_min(a->min.z, b->min.z);
    result.max.x = oimo_max(a->max.x, b->max.x);
    result.max.y = oimo_max(a->max.y, b->max.y);
    result.max.z = oimo_max(a->max.z, b->max.z);
    return result;
}

// Get intersection of two AABBs (result may be invalid if they don't overlap)
static inline OimoAabb oimo_aabb_intersection(const OimoAabb* a, const OimoAabb* b) {
    OimoAabb result;
    result.min.x = oimo_max(a->min.x, b->min.x);
    result.min.y = oimo_max(a->min.y, b->min.y);
    result.min.z = oimo_max(a->min.z, b->min.z);
    result.max.x = oimo_min(a->max.x, b->max.x);
    result.max.y = oimo_min(a->max.y, b->max.y);
    result.max.z = oimo_min(a->max.z, b->max.z);
    return result;
}

// Initialize AABB to "empty" state (inverted bounds for expansion)
static inline void oimo_aabb_init(OimoAabb* aabb) {
    aabb->min.x = 1e30f;
    aabb->min.y = 1e30f;
    aabb->min.z = 1e30f;
    aabb->max.x = -1e30f;
    aabb->max.y = -1e30f;
    aabb->max.z = -1e30f;
}

// Merge AABB b into result (result = union of result and b)
static inline void oimo_aabb_merge(OimoAabb* result, const OimoAabb* a, const OimoAabb* b) {
    result->min.x = oimo_min(a->min.x, b->min.x);
    result->min.y = oimo_min(a->min.y, b->min.y);
    result->min.z = oimo_min(a->min.z, b->min.z);
    result->max.x = oimo_max(a->max.x, b->max.x);
    result->max.y = oimo_max(a->max.y, b->max.y);
    result->max.z = oimo_max(a->max.z, b->max.z);
}

