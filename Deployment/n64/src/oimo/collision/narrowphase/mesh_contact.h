#pragma once

// mesh_contact.h - Shared contact storage for mesh collision detectors

#include "../../common/vec3.h"
#include "../../../limits.h"

#ifdef __cplusplus
extern "C" {
#endif


// Temporary contact storage for single-pass mesh collision
typedef struct OimoMeshContact {
    OimoVec3 closest;       // Closest point on triangle (mesh local space)
    OimoVec3 normal_dir;    // Contact normal direction
    float depth;            // Penetration depth
    int id;                 // Contact id (persists between frames for warm starting)
} OimoMeshContact;

// Storage for multiple contacts during detection
typedef struct OimoMeshContactStorage {
    OimoMeshContact contacts[OIMO_MESH_MAX_CONTACTS];
    int count;
    float max_depth;
    OimoVec3 primary_normal;
    bool has_contact;
} OimoMeshContactStorage;

// Initialize contact storage
static inline void oimo_mesh_contact_storage_init(OimoMeshContactStorage* storage) {
    storage->count = 0;
    storage->max_depth = 0.0f;
    storage->primary_normal = oimo_vec3(0, 1, 0);
    storage->has_contact = false;
}

// Track the deepest contact's normal (the manifold's normal)
static inline void oimo_mesh_contact_storage_track(OimoMeshContactStorage* storage, const OimoVec3* normal_dir, float depth) {
    if (depth > storage->max_depth) {
        storage->max_depth = depth;
        storage->primary_normal = *normal_dir;
        storage->has_contact = true;
    }
}

static inline void oimo_mesh_contact_set(OimoMeshContact* c, const OimoVec3* closest, const OimoVec3* normal_dir,
                                         float depth, int id) {
    c->closest = *closest;
    c->normal_dir = *normal_dir;
    c->depth = depth;
    c->id = id;
}

// Add a contact, replacing shallowest if full
static inline void oimo_mesh_contact_storage_add(
    OimoMeshContactStorage* storage,
    const OimoVec3* closest,
    const OimoVec3* normal_dir,
    float depth,
    int id
) {
    oimo_mesh_contact_storage_track(storage, normal_dir, depth);

    if (storage->count < OIMO_MESH_MAX_CONTACTS) {
        oimo_mesh_contact_set(&storage->contacts[storage->count++], closest, normal_dir, depth, id);
    } else {
        // Replace shallowest contact if this one is deeper
        int shallowest = 0;
        for (int i = 1; i < OIMO_MESH_MAX_CONTACTS; i++) {
            if (storage->contacts[i].depth < storage->contacts[shallowest].depth) {
                shallowest = i;
            }
        }
        if (depth > storage->contacts[shallowest].depth) {
            oimo_mesh_contact_set(&storage->contacts[shallowest], closest, normal_dir, depth, id);
        }
    }
}

// Points closer than this (squared, 1 cm) are the same contact
#define OIMO_MESH_CONTACT_MERGE_SQ 1e-4f

// Add a contact unless one is already at that point (then the deeper one is
// kept): several features of a triangle, or neighbouring triangles, often
// find the same point (a vertex is on two edges)
static inline void oimo_mesh_contact_storage_add_unique(
    OimoMeshContactStorage* storage,
    const OimoVec3* closest,
    const OimoVec3* normal_dir,
    float depth,
    int id
) {
    for (int i = 0; i < storage->count; i++) {
        OimoMeshContact* c = &storage->contacts[i];
        OimoVec3 d = oimo_vec3_sub(c->closest, *closest);
        if (oimo_vec3_dot(d, d) < OIMO_MESH_CONTACT_MERGE_SQ) {
            if (depth > c->depth) {
                oimo_mesh_contact_storage_track(storage, normal_dir, depth);
                oimo_mesh_contact_set(c, closest, normal_dir, depth, id);
            }
            return;
        }
    }
    oimo_mesh_contact_storage_add(storage, closest, normal_dir, depth, id);
}

#ifdef __cplusplus
}
#endif
