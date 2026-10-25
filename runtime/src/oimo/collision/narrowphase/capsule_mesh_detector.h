#pragma once

// capsule_mesh_detector.h - Capsule vs Static Mesh collision detection (Optimized for N64)

#include "detector.h"
#include "detector_result.h"
#include "triangle_util.h"
#include "mesh_contact.h"
#include "../geometry/capsule_geometry.h"
#include "../geometry/static_mesh_geometry.h"
#include "../broadphase/bvh.h"
#include "../../common/transform.h"
#include "../../common/math_util.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct OimoCapsuleMeshDetector {
    OimoDetector base;
} OimoCapsuleMeshDetector;

static inline void oimo_capsule_mesh_detector_init(OimoCapsuleMeshDetector* detector, bool swapped) {
    oimo_detector_init(&detector->base,
        swapped ? OIMO_DETECTOR_MESH_CAPSULE : OIMO_DETECTOR_CAPSULE_MESH,
        swapped);
}

// Contact ids: a triangle's features each get their own, so the points of one
// triangle keep their warm start between frames
#define OIMO_CAPSULE_MESH_ID(tri_idx, feature) ((tri_idx) * 16 + (feature))
enum {
    OIMO_CAPSULE_MESH_EDGE = 0,      // + edge (0..2)
    OIMO_CAPSULE_MESH_ENDPOINT = 3,  // + endpoint (0..1)
    OIMO_CAPSULE_MESH_VERTEX = 5,    // + vertex (0..2)
    OIMO_CAPSULE_MESH_AXIS = 8,
};

// Add a contact between a capsule axis point and a mesh point within the radius
static inline void oimo_capsule_mesh_add(
    const OimoVec3* axis_point, const OimoVec3* mesh_point,
    float radius, float radius_sq, int id,
    OimoMeshContactStorage* storage
) {
    OimoVec3 diff = oimo_vec3_sub(*axis_point, *mesh_point);
    float dist_sq = oimo_vec3_dot(diff, diff);

    if (dist_sq < radius_sq && dist_sq > 1e-8f) {
        float inv_dist = oimo_fast_inv_sqrt(dist_sq);
        float depth = radius - dist_sq * inv_dist;

        if (depth > 0.0f) {
            OimoVec3 normal_dir = oimo_vec3_scale(diff, inv_dist);
            oimo_mesh_contact_storage_add_unique(storage, mesh_point, &normal_dir, depth, id);
        }
    }
}

// Capsule axis against a triangle edge
static inline void oimo_test_capsule_vs_edge(
    const OimoVec3* cap_p1, const OimoVec3* cap_p2,
    const OimoVec3* edge_a, const OimoVec3* edge_b,
    float radius, float radius_sq, int id,
    OimoMeshContactStorage* storage
) {
    OimoVec3 closest_cap, closest_edge;
    oimo_closest_points_segments(*cap_p1, *cap_p2, *edge_a, *edge_b, &closest_cap, &closest_edge);
    oimo_capsule_mesh_add(&closest_cap, &closest_edge, radius, radius_sq, id, storage);
}

// A capsule endpoint against the triangle face
static inline void oimo_test_point_vs_triangle(
    const OimoVec3* point, const OimoTriangle* tri,
    float radius, float radius_sq, int id,
    OimoMeshContactStorage* storage
) {
    OimoVec3 closest = oimo_closest_point_on_triangle(point, &tri->v0, &tri->v1, &tri->v2);
    oimo_capsule_mesh_add(point, &closest, radius, radius_sq, id, storage);
}

// A triangle vertex against the capsule axis
static inline void oimo_test_vertex_vs_capsule(
    const OimoVec3* vertex,
    const OimoVec3* cap_p1, const OimoVec3* cap_p2,
    float radius, float radius_sq, int id,
    OimoMeshContactStorage* storage
) {
    OimoVec3 closest_cap = oimo_closest_point_on_segment(*vertex, *cap_p1, *cap_p2);
    oimo_capsule_mesh_add(&closest_cap, vertex, radius, radius_sq, id, storage);
}

// The capsule axis passing through the triangle face (a capsule sunk into a
// large triangle, which the tests above can't see: no endpoint or edge is
// near). Pushes it out on the side its farther endpoint is on. Returns true
// when it does
static inline bool oimo_test_axis_through_triangle(
    const OimoVec3* cap_p1, const OimoVec3* cap_p2, const OimoTriangle* tri,
    float radius, int id, OimoMeshContactStorage* storage
) {
    float d1 = oimo_point_plane_distance(cap_p1, &tri->v0, &tri->normal);
    float d2 = oimo_point_plane_distance(cap_p2, &tri->v0, &tri->normal);
    if (d1 * d2 > 0.0f || d1 == d2) return false;

    // Where the axis crosses the plane, inside the triangle?
    OimoVec3 x = oimo_vec3_add(*cap_p1, oimo_vec3_scale(oimo_vec3_sub(*cap_p2, *cap_p1), d1 / (d1 - d2)));
    const OimoVec3* n = &tri->normal;
    if (oimo_vec3_dot(oimo_vec3_cross(oimo_vec3_sub(tri->v1, tri->v0), oimo_vec3_sub(x, tri->v0)), *n) < 0.0f) return false;
    if (oimo_vec3_dot(oimo_vec3_cross(oimo_vec3_sub(tri->v2, tri->v1), oimo_vec3_sub(x, tri->v1)), *n) < 0.0f) return false;
    if (oimo_vec3_dot(oimo_vec3_cross(oimo_vec3_sub(tri->v0, tri->v2), oimo_vec3_sub(x, tri->v2)), *n) < 0.0f) return false;

    // The endpoint that's in less deep sets the way out
    bool p1_out = fabsf(d1) >= fabsf(d2);
    float sunk = p1_out ? fabsf(d2) : fabsf(d1);
    const OimoVec3* inner = p1_out ? cap_p2 : cap_p1;
    OimoVec3 normal_dir = (p1_out ? d1 : d2) >= 0.0f ? *n : oimo_vec3_scale(*n, -1.0f);
    OimoVec3 closest = oimo_closest_point_on_triangle(inner, &tri->v0, &tri->v1, &tri->v2);
    oimo_mesh_contact_storage_add_unique(storage, &closest, &normal_dir, radius + sunk, id);
    return true;
}

// Detect capsule vs mesh collision
static inline void oimo_capsule_mesh_detector_detect(
    OimoCapsuleMeshDetector* detector,
    OimoDetectorResult* result,
    const OimoCapsuleGeometry* capsule,
    const OimoStaticMeshGeometry* mesh,
    const OimoTransform* tf_capsule,
    const OimoTransform* tf_mesh
) {
    result->incremental = false;

    OimoScalar radius = capsule->radius;
    OimoScalar half_height = capsule->halfHeight;
    float radius_sq = radius * radius;
    float max_extent = radius + half_height;

    // Get capsule axis in world space (local Y axis)
    OimoVec3 axis_world = oimo_vec3(
        tf_capsule->rotation.e01,
        tf_capsule->rotation.e11,
        tf_capsule->rotation.e21
    );

    // Capsule endpoints in world space
    OimoVec3 axis_offset = oimo_vec3_scale(axis_world, half_height);
    OimoVec3 cap_p1_world = oimo_vec3_add(tf_capsule->position, axis_offset);
    OimoVec3 cap_p2_world = oimo_vec3_sub(tf_capsule->position, axis_offset);

    // Transform capsule to mesh local space
    OimoVec3 capsule_to_mesh = oimo_vec3_sub(tf_capsule->position, tf_mesh->position);
    OimoMat3 mesh_rot_inv = oimo_mat3_transpose(&tf_mesh->rotation);
    OimoVec3 capsule_local = oimo_mat3_mul_vec3(&mesh_rot_inv, capsule_to_mesh);

    // Also transform endpoints to mesh local space
    OimoVec3 p1_to_mesh = oimo_vec3_sub(cap_p1_world, tf_mesh->position);
    OimoVec3 p2_to_mesh = oimo_vec3_sub(cap_p2_world, tf_mesh->position);
    OimoVec3 cap_p1_local = oimo_mat3_mul_vec3(&mesh_rot_inv, p1_to_mesh);
    OimoVec3 cap_p2_local = oimo_mat3_mul_vec3(&mesh_rot_inv, p2_to_mesh);

    // Triangles within the radius of the capsule's axis
    OimoStaticMeshQuery query;
    oimo_static_mesh_query_segment(&query, mesh, &cap_p1_local, &cap_p2_local, radius);

    // Initialize contact storage
    OimoMeshContactStorage storage;
    oimo_mesh_contact_storage_init(&storage);

    const OimoTriangle* tri;
    while ((tri = oimo_static_mesh_query_next(&query)) != NULL) {
        int tri_idx = query.index;

        // EARLY OUT: Quick plane distance check using capsule center
        float plane_dist = oimo_point_plane_distance(&capsule_local, &tri->v0, &tri->normal);
        if (plane_dist > max_extent || plane_dist < -max_extent) {
            continue;
        }

        // Axis through the face: that's the contact (the others would push
        // the sunk endpoint further in)
        if (oimo_test_axis_through_triangle(&cap_p1_local, &cap_p2_local, tri, radius,
                OIMO_CAPSULE_MESH_ID(tri_idx, OIMO_CAPSULE_MESH_AXIS), &storage)) {
            continue;
        }

        // Test capsule axis against all triangle edges
        oimo_test_capsule_vs_edge(&cap_p1_local, &cap_p2_local, &tri->v0, &tri->v1,
            radius, radius_sq, OIMO_CAPSULE_MESH_ID(tri_idx, OIMO_CAPSULE_MESH_EDGE + 0), &storage);
        oimo_test_capsule_vs_edge(&cap_p1_local, &cap_p2_local, &tri->v1, &tri->v2,
            radius, radius_sq, OIMO_CAPSULE_MESH_ID(tri_idx, OIMO_CAPSULE_MESH_EDGE + 1), &storage);
        oimo_test_capsule_vs_edge(&cap_p1_local, &cap_p2_local, &tri->v2, &tri->v0,
            radius, radius_sq, OIMO_CAPSULE_MESH_ID(tri_idx, OIMO_CAPSULE_MESH_EDGE + 2), &storage);

        // Test capsule endpoints against triangle face
        oimo_test_point_vs_triangle(&cap_p1_local, tri,
            radius, radius_sq, OIMO_CAPSULE_MESH_ID(tri_idx, OIMO_CAPSULE_MESH_ENDPOINT + 0), &storage);
        oimo_test_point_vs_triangle(&cap_p2_local, tri,
            radius, radius_sq, OIMO_CAPSULE_MESH_ID(tri_idx, OIMO_CAPSULE_MESH_ENDPOINT + 1), &storage);

        // Test triangle vertices against capsule axis
        oimo_test_vertex_vs_capsule(&tri->v0, &cap_p1_local, &cap_p2_local,
            radius, radius_sq, OIMO_CAPSULE_MESH_ID(tri_idx, OIMO_CAPSULE_MESH_VERTEX + 0), &storage);
        oimo_test_vertex_vs_capsule(&tri->v1, &cap_p1_local, &cap_p2_local,
            radius, radius_sq, OIMO_CAPSULE_MESH_ID(tri_idx, OIMO_CAPSULE_MESH_VERTEX + 1), &storage);
        oimo_test_vertex_vs_capsule(&tri->v2, &cap_p1_local, &cap_p2_local,
            radius, radius_sq, OIMO_CAPSULE_MESH_ID(tri_idx, OIMO_CAPSULE_MESH_VERTEX + 2), &storage);
    }

    // No collision
    if (!storage.has_contact) return;

    // Transform primary normal to world space
    OimoVec3 normal_world = oimo_mat3_mul_vec3(&tf_mesh->rotation, storage.primary_normal);
    oimo_detector_set_normal(&detector->base, result, &normal_world);

    // Add all stored contacts
    for (int i = 0; i < storage.count; i++) {
        OimoMeshContact* c = &storage.contacts[i];

        // Mesh contact point (in world space)
        OimoVec3 closest_world = oimo_mat3_mul_vec3(&tf_mesh->rotation, c->closest);
        OimoVec3 mesh_contact = oimo_vec3_add(closest_world, tf_mesh->position);

        // Capsule contact point: mesh contact offset by (depth - radius) along normal
        // This places the contact on the capsule surface
        OimoVec3 cap_contact = oimo_vec3_add(mesh_contact,
            oimo_vec3_scale(normal_world, c->depth));

        oimo_detector_add_point(&detector->base, result, &cap_contact, &mesh_contact,
            c->depth, c->id);
    }
}

#ifdef __cplusplus
}
#endif
