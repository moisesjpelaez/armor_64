#pragma once

// static_mesh_geometry.h - Static triangle mesh collision geometry with BVH

#include "geometry.h"
#include "../broadphase/bvh.h"
#include "../../common/vec3.h"
#include "../../common/mat3.h"
#include "../../common/transform.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>

#ifdef __cplusplus
extern "C" {
#endif

// Triangle structure
typedef struct OimoTriangle {
    OimoVec3 v0, v1, v2;     // Vertices
    OimoVec3 normal;         // Face normal
} OimoTriangle;

// Static mesh geometry
typedef struct OimoStaticMeshGeometry {
    OimoGeometry base;

    // Mesh data, in the BVH's leaf order (leaves index it directly)
    OimoTriangle* triangles;
    int16_t triangle_count;

    // BVH for spatial queries (build data freed once built)
    OimoBvhTree bvh;

    // Overall bounds
    OimoAabb bounds;
} OimoStaticMeshGeometry;

// Compute triangle normal from vertices
static inline OimoVec3 oimo_compute_triangle_normal(const OimoVec3* v0, const OimoVec3* v1, const OimoVec3* v2) {
    OimoVec3 edge1 = oimo_vec3_sub(*v1, *v0);
    OimoVec3 edge2 = oimo_vec3_sub(*v2, *v0);
    OimoVec3 normal = oimo_vec3_cross(edge1, edge2);
    return oimo_vec3_normalize(normal);
}

// Compute triangle centroid
static inline OimoVec3 oimo_compute_triangle_centroid(const OimoVec3* v0, const OimoVec3* v1, const OimoVec3* v2) {
    return oimo_vec3(
        (v0->x + v1->x + v2->x) / 3.0f,
        (v0->y + v1->y + v2->y) / 3.0f,
        (v0->z + v1->z + v2->z) / 3.0f
    );
}

// Compute triangle AABB
static inline void oimo_compute_triangle_aabb(const OimoVec3* v0, const OimoVec3* v1, const OimoVec3* v2, OimoAabb* aabb) {
    aabb->min.x = fminf(v0->x, fminf(v1->x, v2->x));
    aabb->min.y = fminf(v0->y, fminf(v1->y, v2->y));
    aabb->min.z = fminf(v0->z, fminf(v1->z, v2->z));
    aabb->max.x = fmaxf(v0->x, fmaxf(v1->x, v2->x));
    aabb->max.y = fmaxf(v0->y, fmaxf(v1->y, v2->y));
    aabb->max.z = fmaxf(v0->z, fmaxf(v1->z, v2->z));
}

// Initialize static mesh geometry from vertex and index arrays
// vertices: array of Vec3 positions
// indices: array of triangle indices (3 per triangle)
// vertex_count: number of vertices
// index_count: number of indices (must be multiple of 3)
static inline bool oimo_static_mesh_geometry_init(
    OimoStaticMeshGeometry* geom,
    const OimoVec3* vertices,
    const uint16_t* indices,
    int vertex_count,
    int index_count
) {
    oimo_geometry_init(&geom->base, OIMO_GEOMETRY_STATIC_MESH);

    geom->triangle_count = (int16_t)(index_count / 3);
    if (geom->triangle_count == 0 || geom->triangle_count > OIMO_BVH_MAX_TRIANGLES) {
        return false;
    }

    // Build data: triangles, their centroids and bounds (freed once built)
    OimoTriangle* triangles = (OimoTriangle*)malloc(sizeof(OimoTriangle) * geom->triangle_count);
    OimoBvhTriangle* bvh_triangles = (OimoBvhTriangle*)malloc(sizeof(OimoBvhTriangle) * geom->triangle_count);
    geom->triangles = (OimoTriangle*)malloc(sizeof(OimoTriangle) * geom->triangle_count);
    if (!triangles || !bvh_triangles || !geom->triangles || !oimo_bvh_tree_init(&geom->bvh, geom->triangle_count)) {
        free(triangles);
        free(bvh_triangles);
        free(geom->triangles);
        geom->triangles = NULL;
        return false;
    }

    // Build triangles and compute bounds
    oimo_aabb_init(&geom->bounds);

    for (int i = 0; i < geom->triangle_count; i++) {
        int idx = i * 3;
        const OimoVec3* v0 = &vertices[indices[idx]];
        const OimoVec3* v1 = &vertices[indices[idx + 1]];
        const OimoVec3* v2 = &vertices[indices[idx + 2]];

        triangles[i].v0 = *v0;
        triangles[i].v1 = *v1;
        triangles[i].v2 = *v2;
        triangles[i].normal = oimo_compute_triangle_normal(v0, v1, v2);

        bvh_triangles[i].centroid = oimo_compute_triangle_centroid(v0, v1, v2);
        oimo_compute_triangle_aabb(v0, v1, v2, &bvh_triangles[i].aabb);
        bvh_triangles[i].original_index = (int16_t)i;

        oimo_aabb_merge(&geom->bounds, &geom->bounds, &bvh_triangles[i].aabb);
    }

    bool built = oimo_bvh_tree_build(&geom->bvh, bvh_triangles, geom->triangle_count);
    if (built) {
        // Triangles in leaf order, then the build data goes
        for (int i = 0; i < geom->triangle_count; i++) {
            geom->triangles[i] = triangles[geom->bvh.sorted_indices[i]];
        }
        oimo_bvh_tree_compact(&geom->bvh);
    }
    free(triangles);
    free(bvh_triangles);
    if (!built) {
        oimo_bvh_tree_free(&geom->bvh);
        free(geom->triangles);
        geom->triangles = NULL;
        return false;
    }

    // Compute approximate volume (bounding box volume)
    OimoVec3 size = oimo_vec3_sub(geom->bounds.max, geom->bounds.min);
    geom->base.volume = size.x * size.y * size.z;

    // Compute approximate inertia (as if it were a box)
    float mass = 1.0f;  // Normalized
    geom->base.inertia_coeff = oimo_mat3_diagonal(
        mass / 12.0f * (size.y * size.y + size.z * size.z),
        mass / 12.0f * (size.z * size.z + size.x * size.x),
        mass / 12.0f * (size.x * size.x + size.y * size.y)
    );

    return true;
}

// Free static mesh geometry
static inline void oimo_static_mesh_geometry_free(OimoStaticMeshGeometry* geom) {
    oimo_bvh_tree_free(&geom->bvh);
    if (geom->triangles) {
        free(geom->triangles);
        geom->triangles = NULL;
    }
    geom->triangle_count = 0;
}

// A triangle query in mesh local space: the BVH leaves in range, then each
// triangle's own bounds tested too
typedef struct OimoStaticMeshQuery {
    OimoBvhQuery bvh;
    const OimoStaticMeshGeometry* mesh;
    int index;  // Index of the triangle next() returned
} OimoStaticMeshQuery;

// Triangles whose bounds overlap a box
static inline void oimo_static_mesh_query_aabb(OimoStaticMeshQuery* q, const OimoStaticMeshGeometry* geom,
                                               const OimoAabb* aabb) {
    q->mesh = geom;
    oimo_bvh_query_begin(&q->bvh, &geom->bvh, &aabb->min, &aabb->max, 0.0f);
}

// Triangles whose bounds are within `radius` of a point (a sphere)
static inline void oimo_static_mesh_query_sphere(OimoStaticMeshQuery* q, const OimoStaticMeshGeometry* geom,
                                                 const OimoVec3* center, float radius) {
    q->mesh = geom;
    oimo_bvh_query_begin(&q->bvh, &geom->bvh, center, center, radius);
}

// Triangles whose bounds are within `radius` of a segment's box (a capsule):
// tighter than a cube around the capsule, especially for one standing on a floor
static inline void oimo_static_mesh_query_segment(OimoStaticMeshQuery* q, const OimoStaticMeshGeometry* geom,
                                                  const OimoVec3* p0, const OimoVec3* p1, float radius) {
    OimoVec3 min = oimo_vec3(fminf(p0->x, p1->x), fminf(p0->y, p1->y), fminf(p0->z, p1->z));
    OimoVec3 max = oimo_vec3(fmaxf(p0->x, p1->x), fmaxf(p0->y, p1->y), fmaxf(p0->z, p1->z));
    q->mesh = geom;
    oimo_bvh_query_begin(&q->bvh, &geom->bvh, &min, &max, radius);
}

// Next triangle in range (its index in q->index), NULL when done
static inline const OimoTriangle* oimo_static_mesh_query_next(OimoStaticMeshQuery* q) {
    int i;
    while ((i = oimo_bvh_query_next(&q->bvh)) >= 0) {
        const OimoTriangle* tri = &q->mesh->triangles[i];
        OimoAabb bounds;
        oimo_compute_triangle_aabb(&tri->v0, &tri->v1, &tri->v2, &bounds);
        if (oimo_bvh_box_distance_sq(&q->bvh.min, &q->bvh.max, &bounds) <= q->bvh.radius_sq) {
            q->index = i;
            return tri;
        }
    }
    return NULL;
}

// Compute AABB in world space
static inline void oimo_static_mesh_compute_aabb(
    const OimoStaticMeshGeometry* geom,
    OimoAabb* aabb,
    const OimoTransform* tf
) {
    // Transform all 8 corners of local bounds and find enclosing AABB
    OimoVec3 corners[8];
    corners[0] = oimo_vec3(geom->bounds.min.x, geom->bounds.min.y, geom->bounds.min.z);
    corners[1] = oimo_vec3(geom->bounds.max.x, geom->bounds.min.y, geom->bounds.min.z);
    corners[2] = oimo_vec3(geom->bounds.min.x, geom->bounds.max.y, geom->bounds.min.z);
    corners[3] = oimo_vec3(geom->bounds.max.x, geom->bounds.max.y, geom->bounds.min.z);
    corners[4] = oimo_vec3(geom->bounds.min.x, geom->bounds.min.y, geom->bounds.max.z);
    corners[5] = oimo_vec3(geom->bounds.max.x, geom->bounds.min.y, geom->bounds.max.z);
    corners[6] = oimo_vec3(geom->bounds.min.x, geom->bounds.max.y, geom->bounds.max.z);
    corners[7] = oimo_vec3(geom->bounds.max.x, geom->bounds.max.y, geom->bounds.max.z);

    aabb->min = oimo_vec3(1e30f, 1e30f, 1e30f);
    aabb->max = oimo_vec3(-1e30f, -1e30f, -1e30f);

    for (int i = 0; i < 8; i++) {
        OimoVec3 world = oimo_mat3_mul_vec3(&tf->rotation, corners[i]);
        world = oimo_vec3_add(world, tf->position);

        if (world.x < aabb->min.x) aabb->min.x = world.x;
        if (world.y < aabb->min.y) aabb->min.y = world.y;
        if (world.z < aabb->min.z) aabb->min.z = world.z;
        if (world.x > aabb->max.x) aabb->max.x = world.x;
        if (world.y > aabb->max.y) aabb->max.y = world.y;
        if (world.z > aabb->max.z) aabb->max.z = world.z;
    }
}

// Ray cast against static mesh (uses BVH for acceleration)
static inline bool oimo_static_mesh_ray_cast(
    const OimoStaticMeshGeometry* geom,
    const OimoVec3* begin,
    const OimoVec3* end,
    OimoRayCastHit* hit
) {
    OimoVec3 ray_dir = oimo_vec3_sub(*end, *begin);
    float ray_length_sq = oimo_vec3_dot(ray_dir, ray_dir);
    if (ray_length_sq < 1e-12f) return false;

    float ray_length = sqrtf(ray_length_sq);
    ray_dir = oimo_vec3_scale(ray_dir, 1.0f / ray_length);

    // Build ray AABB
    OimoAabb ray_aabb;
    ray_aabb.min.x = fminf(begin->x, end->x);
    ray_aabb.min.y = fminf(begin->y, end->y);
    ray_aabb.min.z = fminf(begin->z, end->z);
    ray_aabb.max.x = fmaxf(begin->x, end->x);
    ray_aabb.max.y = fmaxf(begin->y, end->y);
    ray_aabb.max.z = fmaxf(begin->z, end->z);

    // Query overlapping triangles
    OimoStaticMeshQuery query;
    oimo_static_mesh_query_aabb(&query, geom, &ray_aabb);

    float closest_t = 1e30f;
    int closest_tri = -1;

    // Möller–Trumbore ray-triangle intersection
    const OimoTriangle* tri;
    while ((tri = oimo_static_mesh_query_next(&query)) != NULL) {
        OimoVec3 edge1 = oimo_vec3_sub(tri->v1, tri->v0);
        OimoVec3 edge2 = oimo_vec3_sub(tri->v2, tri->v0);
        OimoVec3 h = oimo_vec3_cross(ray_dir, edge2);
        float a = oimo_vec3_dot(edge1, h);

        if (a > -1e-6f && a < 1e-6f) continue;  // Parallel

        float f = 1.0f / a;
        OimoVec3 s = oimo_vec3_sub(*begin, tri->v0);
        float u = f * oimo_vec3_dot(s, h);

        if (u < 0.0f || u > 1.0f) continue;

        OimoVec3 q = oimo_vec3_cross(s, edge1);
        float v = f * oimo_vec3_dot(ray_dir, q);

        if (v < 0.0f || u + v > 1.0f) continue;

        float t = f * oimo_vec3_dot(edge2, q);

        if (t > 1e-6f && t <= ray_length && t < closest_t) {
            closest_t = t;
            closest_tri = query.index;
        }
    }

    if (closest_tri >= 0) {
        OimoVec3 scaled_dir = oimo_vec3_scale(ray_dir, closest_t);
        hit->position = oimo_vec3_add(*begin, scaled_dir);
        hit->normal = geom->triangles[closest_tri].normal;
        hit->fraction = closest_t / ray_length;
        return true;
    }

    return false;
}

#ifdef __cplusplus
}
#endif
