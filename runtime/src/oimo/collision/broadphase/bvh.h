#pragma once

// bvh.h - Simple BVH for static mesh triangle queries (N64 optimized)

#include "../geometry/aabb.h"
#include "../../../limits.h"
#include "../../common/vec3.h"
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

#ifdef __cplusplus
extern "C" {
#endif


// Maximum triangles per leaf before splitting
#define OIMO_BVH_LEAF_THRESHOLD 4

// BVH Node - compact for N64
// If left_child == -1, it's a leaf node
typedef struct OimoBvhNode {
    OimoAabb aabb;           // Node bounding box
    int16_t left_child;      // Index of left child (-1 if leaf)
    int16_t right_child;     // Index of right child
    int16_t first_triangle;  // First index into sorted_indices array (for leaves)
    int16_t triangle_count;  // Number of triangles (for leaves)
} OimoBvhNode;

// BVH Tree for static mesh
typedef struct OimoBvhTree {
    OimoBvhNode* nodes;      // Node array (allocated)
    int16_t* sorted_indices; // Sorted triangle indices for leaf nodes
    int16_t node_count;      // Number of nodes used
    int16_t max_nodes;       // Maximum nodes allocated
    int16_t root;            // Root node index
    int16_t triangle_count;  // Total triangles
} OimoBvhTree;

// Triangle data for building BVH
typedef struct OimoBvhTriangle {
    OimoVec3 centroid;       // Triangle center (for partitioning)
    OimoAabb aabb;           // Triangle AABB
    int16_t original_index;  // Original triangle index in mesh
} OimoBvhTriangle;

// Initialize BVH tree (allocate memory)
static inline bool oimo_bvh_tree_init(OimoBvhTree* tree, int max_triangles) {
    // Estimate max nodes: worst case is 2*n-1 for n leaves
    tree->max_nodes = (int16_t)(max_triangles * 2);
    tree->nodes = (OimoBvhNode*)malloc(sizeof(OimoBvhNode) * tree->max_nodes);
    if (!tree->nodes) return false;
    tree->sorted_indices = (int16_t*)malloc(sizeof(int16_t) * max_triangles);
    if (!tree->sorted_indices) {
        free(tree->nodes);
        tree->nodes = NULL;
        return false;
    }
    tree->node_count = 0;
    tree->root = -1;
    tree->triangle_count = 0;
    return true;
}

// After the build: the node array shrunk to the nodes used, and the leaf
// order (sorted_indices) freed, once the caller has stored its data in that
// order (leaves then index the data directly)
static inline void oimo_bvh_tree_compact(OimoBvhTree* tree) {
    OimoBvhNode* nodes = (OimoBvhNode*)realloc(tree->nodes, sizeof(OimoBvhNode) * tree->node_count);
    if (nodes) {
        tree->nodes = nodes;
        tree->max_nodes = tree->node_count;
    }
    free(tree->sorted_indices);
    tree->sorted_indices = NULL;
}

// Free BVH tree
static inline void oimo_bvh_tree_free(OimoBvhTree* tree) {
    if (tree->nodes) {
        free(tree->nodes);
        tree->nodes = NULL;
    }
    if (tree->sorted_indices) {
        free(tree->sorted_indices);
        tree->sorted_indices = NULL;
    }
    tree->node_count = 0;
    tree->root = -1;
    tree->triangle_count = 0;
}

// Allocate a new node
static inline int16_t oimo_bvh_alloc_node(OimoBvhTree* tree) {
    if (tree->node_count >= tree->max_nodes) return -1;
    int16_t idx = tree->node_count++;
    OimoBvhNode* node = &tree->nodes[idx];
    node->left_child = -1;
    node->right_child = -1;
    node->first_triangle = 0;
    node->triangle_count = 0;
    oimo_aabb_init(&node->aabb);
    return idx;
}

// Compute AABB enclosing all triangles in range
static inline void oimo_bvh_compute_bounds(
    const OimoBvhTriangle* triangles,
    const int16_t* indices,
    int count,
    OimoAabb* out_aabb
) {
    oimo_aabb_init(out_aabb);
    for (int i = 0; i < count; i++) {
        const OimoBvhTriangle* tri = &triangles[indices[i]];
        oimo_aabb_merge(out_aabb, out_aabb, &tri->aabb);
    }
}

static inline float oimo_bvh_centroid(const OimoBvhTriangle* triangles, int16_t index, int axis) {
    const OimoVec3* c = &triangles[index].centroid;
    return axis == 0 ? c->x : axis == 1 ? c->y : c->z;
}

// Partition triangles at the median along an axis (quickselect): both halves
// get half the triangles, so the tree stays balanced (depth log2(n/leaf)) for
// any triangle distribution, as in OimoPhysics' StaticMeshBvh
static inline int oimo_bvh_partition(
    OimoBvhTriangle* triangles,
    int16_t* indices,
    int count,
    int axis
) {
    if (count <= 1) return 0;
    int mid = count / 2;
    int lo = 0, hi = count - 1;
    while (lo < hi) {
        float pivot = oimo_bvh_centroid(triangles, indices[(lo + hi) / 2], axis);
        int i = lo, j = hi;
        while (i <= j) {
            while (oimo_bvh_centroid(triangles, indices[i], axis) < pivot) i++;
            while (oimo_bvh_centroid(triangles, indices[j], axis) > pivot) j--;
            if (i <= j) {
                int16_t tmp = indices[i];
                indices[i] = indices[j];
                indices[j] = tmp;
                i++;
                j--;
            }
        }
        if (mid <= j) hi = j;
        else if (mid >= i) lo = i;
        else break;
    }
    return mid;
}

// Build BVH recursively - returns node index
// sorted_pos tracks current position in sorted_indices array
static inline int16_t oimo_bvh_build_recursive(
    OimoBvhTree* tree,
    OimoBvhTriangle* triangles,
    int16_t* indices,
    int count,
    int depth,
    int16_t* sorted_pos
) {
    if (count == 0) return -1;

    int16_t node_idx = oimo_bvh_alloc_node(tree);
    if (node_idx < 0) return -1;

    OimoBvhNode* node = &tree->nodes[node_idx];
    oimo_bvh_compute_bounds(triangles, indices, count, &node->aabb);

    // Make leaf if few enough triangles. Past the depth limit (can't happen
    // with median splits) the node keeps all its triangles: none is dropped
    if (depth >= OIMO_BVH_MAX_DEPTH && count > OIMO_BVH_LEAF_THRESHOLD) ARM_LIMIT_REACHED(OIMO_BVH_MAX_DEPTH);
    if (count <= OIMO_BVH_LEAF_THRESHOLD || depth >= OIMO_BVH_MAX_DEPTH) {
        node->left_child = -1;
        node->right_child = -1;
        node->first_triangle = *sorted_pos;
        node->triangle_count = (int16_t)count;
        // Copy triangle indices to sorted array
        for (int i = 0; i < count; i++) {
            tree->sorted_indices[(*sorted_pos)++] = triangles[indices[i]].original_index;
        }
        return node_idx;
    }

    // Find longest axis
    OimoVec3 size = oimo_vec3_sub(node->aabb.max, node->aabb.min);
    int axis = 0;
    if (size.y > size.x) axis = 1;
    if (size.z > (axis == 0 ? size.x : size.y)) axis = 2;

    // Partition
    int mid = oimo_bvh_partition(triangles, indices, count, axis);

    // Build children
    node->left_child = oimo_bvh_build_recursive(tree, triangles, indices, mid, depth + 1, sorted_pos);
    node->right_child = oimo_bvh_build_recursive(tree, triangles, indices + mid, count - mid, depth + 1, sorted_pos);

    return node_idx;
}

// Build BVH from triangle data
static inline bool oimo_bvh_tree_build(
    OimoBvhTree* tree,
    OimoBvhTriangle* triangles,
    int triangle_count
) {
    if (triangle_count == 0 || triangle_count > OIMO_BVH_MAX_TRIANGLES) return false;

    tree->triangle_count = (int16_t)triangle_count;

    // Create index array
    int16_t* indices = (int16_t*)malloc(sizeof(int16_t) * triangle_count);
    if (!indices) return false;

    for (int i = 0; i < triangle_count; i++) {
        indices[i] = (int16_t)i;
        triangles[i].original_index = (int16_t)i;
    }

    // Build tree
    int16_t sorted_pos = 0;
    tree->root = oimo_bvh_build_recursive(tree, triangles, indices, triangle_count, 0, &sorted_pos);

    free(indices);
    return tree->root >= 0;
}

// Squared distance between a query box and a node box (0 when they overlap)
static inline float oimo_bvh_box_distance_sq(const OimoVec3* qmin, const OimoVec3* qmax, const OimoAabb* b) {
    float dx = qmax->x < b->min.x ? b->min.x - qmax->x : (b->max.x < qmin->x ? qmin->x - b->max.x : 0.0f);
    float dy = qmax->y < b->min.y ? b->min.y - qmax->y : (b->max.y < qmin->y ? qmin->y - b->max.y : 0.0f);
    float dz = qmax->z < b->min.z ? b->min.z - qmax->z : (b->max.z < qmin->z ? qmin->z - b->max.z : 0.0f);
    return dx * dx + dy * dy + dz * dz;
}

// A query: the leaves whose bounds are within `radius` of a box. A box with
// radius 0 is an overlap query; a point (min == max) with a radius, a sphere;
// a segment's box with a radius, a capsule (as OimoPhysics' StaticMeshBvh).
// Walked with an explicit stack: no recursion, no result array
typedef struct OimoBvhQuery {
    const OimoBvhTree* tree;
    OimoVec3 min, max;
    float radius_sq;
    int16_t stack[OIMO_BVH_MAX_DEPTH + 2];  // A pending sibling per level, plus the root
    int sp;
    int pos, end;  // Leaf positions left in the current leaf
} OimoBvhQuery;

static inline void oimo_bvh_query_begin(OimoBvhQuery* q, const OimoBvhTree* tree,
                                        const OimoVec3* min, const OimoVec3* max, float radius) {
    q->tree = tree;
    q->min = *min;
    q->max = *max;
    q->radius_sq = radius * radius;
    q->sp = 0;
    q->pos = q->end = 0;
    if (tree->root >= 0) q->stack[q->sp++] = tree->root;
}

// Next leaf position (an index in the leaf order), -1 when done
static inline int oimo_bvh_query_next(OimoBvhQuery* q) {
    while (q->pos >= q->end) {
        if (q->sp == 0) return -1;
        const OimoBvhNode* node = &q->tree->nodes[q->stack[--q->sp]];
        if (oimo_bvh_box_distance_sq(&q->min, &q->max, &node->aabb) > q->radius_sq) continue;
        if (node->left_child < 0) {
            q->pos = node->first_triangle;
            q->end = node->first_triangle + node->triangle_count;
        } else {
            q->stack[q->sp++] = node->right_child;
            q->stack[q->sp++] = node->left_child;
        }
    }
    return q->pos++;
}

#ifdef __cplusplus
}
#endif
