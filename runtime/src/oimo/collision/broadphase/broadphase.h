#pragma once

#include "broadphase_type.h"
#include "../../../limits.h"
#include "proxy.h"
#include "proxy_pair.h"
#include "../geometry/aabb.h"
#include "../../common/vec3.h"
#include "../../common/math_util.h"


typedef void (*OimoBroadPhaseProxyCallback)(OimoProxy* proxy, void* userData);

typedef struct OimoBroadPhase {
    int _type;
    int _numProxies;
    OimoProxy* _proxyList;
    OimoProxy* _proxyListLast;
    OimoProxyPair* _proxyPairList;
    int _incremental;
    int _testCount;
    OimoProxyPair* _proxyPairPool;
    int _idCount;
    OimoProxy _proxyStorage[OIMO_MAX_PROXIES];
    OimoProxyPair _pairStorage[OIMO_MAX_PROXY_PAIRS];
    int _proxyStorageUsed;
    int _pairStorageUsed;
    // Destroyed proxies, reused before new storage (upstream lets the GC free them)
    OimoProxy* _freeProxies[OIMO_MAX_PROXIES];
    int _freeProxyCount;
    // N64 pair collection (see oimo_broadphase_collect_pairs_sorted): static
    // proxies sorted by AABB min x (the unusually wide ones apart, always
    // tested), the moving ones sorted by AABB min x, and the pairs found
    // before they're ordered
    OimoProxy* _statics[OIMO_MAX_PROXIES];
    int _numStatics;
    OimoProxy* _wideStatics[OIMO_MAX_PROXIES];
    int _numWideStatics;
    float _maxStaticWidth;      // Widest (x) of the sorted statics
    int _staticsDirty;
    OimoProxy* _movers[OIMO_MAX_PROXIES];
    int _numMovers;
    float _maxMoverWidth;       // Widest (x) of the moving proxies
    int _listChanged;           // Proxies added or removed: renumber and regroup
    struct { int16_t pos1, pos2; OimoProxy* p1; OimoProxy* p2; } _found[OIMO_MAX_PROXY_PAIRS];
} OimoBroadPhase;

static inline void oimo_broadphase_list_push(OimoBroadPhase* bp, OimoProxy* p) {
    if (bp->_proxyListLast != NULL) {
        bp->_proxyListLast->_next = p;
        p->_prev = bp->_proxyListLast;
        p->_next = NULL;
        bp->_proxyListLast = p;
    } else {
        bp->_proxyList = p;
        bp->_proxyListLast = p;
        p->_prev = NULL;
        p->_next = NULL;
    }
}

static inline void oimo_broadphase_list_remove(OimoBroadPhase* bp, OimoProxy* p) {
    OimoProxy* prev = p->_prev;
    OimoProxy* next = p->_next;

    if (prev != NULL) {
        prev->_next = next;
    } else {
        bp->_proxyList = next;
    }

    if (next != NULL) {
        next->_prev = prev;
    } else {
        bp->_proxyListLast = prev;
    }

    p->_prev = NULL;
    p->_next = NULL;
}

static inline OimoProxyPair* oimo_broadphase_pick_pair(OimoBroadPhase* bp) {
    OimoProxyPair* pp;

    if (bp->_proxyPairPool != NULL) {
        // Reuse from pool
        pp = bp->_proxyPairPool;
        bp->_proxyPairPool = pp->_next;
    } else {
        // Allocate from storage
        if (bp->_pairStorageUsed < OIMO_MAX_PROXY_PAIRS) {
            pp = &bp->_pairStorage[bp->_pairStorageUsed++];
            oimo_proxy_pair_init(pp);
        } else {
            ARM_LIMIT_REACHED(OIMO_MAX_PROXY_PAIRS);
            return NULL;
        }
    }

    return pp;
}

static inline void oimo_broadphase_pick_and_push_pair(OimoBroadPhase* bp, OimoProxy* p1, OimoProxy* p2) {
    OimoProxyPair* pp = oimo_broadphase_pick_pair(bp);
    if (pp == NULL) return;

    // Add to front of list: M.singleList_addFirst(_proxyPairList, _next, pp)
    pp->_next = bp->_proxyPairList;
    bp->_proxyPairList = pp;

    pp->_p1 = p1;
    pp->_p2 = p2;
}

static inline int oimo_broadphase_is_overlapping(const OimoProxy* p1, const OimoProxy* p2);
static inline void oimo_broadphase_add_proxy(OimoBroadPhase* bp, OimoProxy* p);
static inline void oimo_broadphase_remove_proxy(OimoBroadPhase* bp, OimoProxy* p);
static inline void oimo_broadphase_pool_pairs(OimoBroadPhase* bp) {
    OimoProxyPair* p = bp->_proxyPairList;

    if (p != NULL) {
        // Clear pair references and find end of list
        OimoProxyPair* last = p;
        while (1) {
            p->_p1 = NULL;
            p->_p2 = NULL;
            if (p->_next == NULL) {
                last = p;
                break;
            }
            p = p->_next;
        }

        // Move entire list to pool
        last->_next = bp->_proxyPairPool;
        bp->_proxyPairPool = bp->_proxyPairList;
        bp->_proxyPairList = NULL;
    }
}

// Static proxies sorted by AABB min x; the ones over 4x the median width are
// kept apart (always tested), so a search range is the others' widest
static inline void oimo_broadphase_sort_statics(OimoBroadPhase* bp) {
    int n = bp->_numStatics;
    OimoProxy** a = bp->_statics;
    for (int i = 1; i < n; i++) {  // Insertion sort: re-sorted only when statics change
        OimoProxy* p = a[i];
        int j = i - 1;
        while (j >= 0 && a[j]->_aabbMin.x > p->_aabbMin.x) { a[j + 1] = a[j]; j--; }
        a[j + 1] = p;
    }
    float widths[OIMO_MAX_PROXIES];
    for (int i = 0; i < n; i++) widths[i] = a[i]->_aabbMax.x - a[i]->_aabbMin.x;
    for (int i = 1; i < n; i++) {  // Median width
        float w = widths[i];
        int j = i - 1;
        while (j >= 0 && widths[j] > w) { widths[j + 1] = widths[j]; j--; }
        widths[j + 1] = w;
    }
    float wide = n > 0 ? widths[n / 2] * 4.0f : 0.0f;
    int kept = 0;
    bp->_numWideStatics = 0;
    bp->_maxStaticWidth = 0.0f;
    for (int i = 0; i < n; i++) {
        float w = a[i]->_aabbMax.x - a[i]->_aabbMin.x;
        if (w > wide && wide > 0.0f) {
            bp->_wideStatics[bp->_numWideStatics++] = a[i];
        } else {
            a[kept++] = a[i];
            if (w > bp->_maxStaticWidth) bp->_maxStaticWidth = w;
        }
    }
    bp->_numStatics = kept;
}

static inline void oimo_broadphase_found(OimoBroadPhase* bp, int* count, OimoProxy* a, OimoProxy* b) {
    bp->_testCount++;
    if (!oimo_broadphase_is_overlapping(a, b)) return;
    if (*count >= OIMO_MAX_PROXY_PAIRS) {
        ARM_LIMIT_REACHED(OIMO_MAX_PROXY_PAIRS);
        return;
    }
    // The earlier proxy in the list first, as testing every two proxies in order
    if (b->_listPos < a->_listPos) { OimoProxy* t = a; a = b; b = t; }
    bp->_found[*count].pos1 = a->_listPos;
    bp->_found[*count].pos2 = b->_listPos;
    bp->_found[*count].p1 = a;
    bp->_found[*count].p2 = b;
    (*count)++;
}

// Moving proxies sorted by AABB min x, re-sorted every step: they move a little
// between steps, so the insertion sort is about one pass
static inline void oimo_broadphase_sort_movers(OimoBroadPhase* bp) {
    int n = bp->_numMovers;
    OimoProxy** a = bp->_movers;
    float widest = 0.0f;
    for (int i = 0; i < n; i++) {
        OimoProxy* p = a[i];
        int j = i - 1;
        while (j >= 0 && a[j]->_aabbMin.x > p->_aabbMin.x) { a[j + 1] = a[j]; j--; }
        a[j + 1] = p;
        float w = p->_aabbMax.x - p->_aabbMin.x;
        if (w > widest) widest = w;
    }
    bp->_maxMoverWidth = widest;
}

// The first of n proxies sorted by min x whose x range can reach x: min x at
// least x - widest (padded for float rounding: more tests, never fewer pairs)
static inline int oimo_broadphase_search(OimoProxy** a, int n, float x, float widest) {
    float lo = x - widest * 1.0001f - oimo_abs(x) * 1e-6f;
    int first = 0, last = n;
    while (first < last) {
        int mid = (first + last) / 2;
        if (a[mid]->_aabbMin.x < lo) first = mid + 1; else last = mid;
    }
    return first;
}

// Pairs with a dynamic body (the contact manager drops the others,
// ContactManager.shouldCollide). N64: each dynamic proxy is tested only against
// the proxies whose x range can reach it: a binary search in the moving proxies
// and in the static ones (both sorted by min x), plus the wide statics. The pairs
// are then pushed in the order testing every two proxies in list order gives
// (upstream's brute force), so contacts and the solver see the same order.
static inline void oimo_broadphase_collect_pairs_sorted(OimoBroadPhase* bp) {
    oimo_broadphase_pool_pairs(bp);
    bp->_testCount = 0;

    // Regroup when proxies were added or removed or a body changed between
    // static and moving: list positions, statics and moving proxies
    int regroup = bp->_listChanged;
    for (int i = 0; i < bp->_numStatics && !regroup; i++) regroup = !bp->_statics[i]->_isStatic;
    for (int i = 0; i < bp->_numWideStatics && !regroup; i++) regroup = !bp->_wideStatics[i]->_isStatic;
    for (int i = 0; i < bp->_numMovers && !regroup; i++) regroup = bp->_movers[i]->_isStatic;
    if (regroup) {
        bp->_listChanged = 0;
        bp->_numStatics = 0;
        bp->_numWideStatics = 0;
        bp->_numMovers = 0;
        int16_t pos = 0;
        for (OimoProxy* p = bp->_proxyList; p != NULL; p = p->_next, pos++) {
            p->_listPos = pos;
            if (p->_isStatic) bp->_statics[bp->_numStatics++] = p;
            else bp->_movers[bp->_numMovers++] = p;
        }
        bp->_staticsDirty = 1;
    }

    // Re-sort the statics when one moved
    for (int i = 0; i < bp->_numStatics; i++) {
        if (bp->_statics[i]->_staticChanged) { bp->_statics[i]->_staticChanged = 0; bp->_staticsDirty = 1; }
    }
    for (int i = 0; i < bp->_numWideStatics; i++) {
        if (bp->_wideStatics[i]->_staticChanged) { bp->_wideStatics[i]->_staticChanged = 0; bp->_staticsDirty = 1; }
    }
    if (bp->_staticsDirty) {
        bp->_staticsDirty = 0;
        for (int i = 0; i < bp->_numWideStatics; i++) bp->_statics[bp->_numStatics++] = bp->_wideStatics[i];
        bp->_numWideStatics = 0;
        oimo_broadphase_sort_statics(bp);
    }
    oimo_broadphase_sort_movers(bp);

    int count = 0;
    int numMovers = bp->_numMovers;
    OimoProxy** movers = bp->_movers;
    for (int i = 0; i < numMovers; i++) {
        OimoProxy* m = movers[i];
        if (!m->_isDynamic) continue;
        // Moving proxies whose x range can reach m's (a dynamic pair only once:
        // two that overlap in x find each other)
        for (int k = oimo_broadphase_search(movers, numMovers, m->_aabbMin.x, bp->_maxMoverWidth);
             k < numMovers && movers[k]->_aabbMin.x <= m->_aabbMax.x; k++) {
            OimoProxy* n = movers[k];
            if (k == i || (n->_isDynamic && k < i)) continue;
            oimo_broadphase_found(bp, &count, m, n);
        }
        // Static ones likewise, plus the wide ones
        for (int k = oimo_broadphase_search(bp->_statics, bp->_numStatics, m->_aabbMin.x, bp->_maxStaticWidth);
             k < bp->_numStatics && bp->_statics[k]->_aabbMin.x <= m->_aabbMax.x; k++) {
            oimo_broadphase_found(bp, &count, m, bp->_statics[k]);
        }
        for (int k = 0; k < bp->_numWideStatics; k++) {
            oimo_broadphase_found(bp, &count, m, bp->_wideStatics[k]);
        }
    }

    // Push in (first position, second position) order
    for (int i = 1; i < count; i++) {
        __typeof__(bp->_found[0]) f = bp->_found[i];
        int j = i - 1;
        while (j >= 0 && (bp->_found[j].pos1 > f.pos1 || (bp->_found[j].pos1 == f.pos1 && bp->_found[j].pos2 > f.pos2))) {
            bp->_found[j + 1] = bp->_found[j];
            j--;
        }
        bp->_found[j + 1] = f;
    }
    for (int i = 0; i < count; i++) {
        oimo_broadphase_pick_and_push_pair(bp, bp->_found[i].p1, bp->_found[i].p2);
    }
}

static inline void oimo_broadphase_collect_pairs(OimoBroadPhase* bp) {
    oimo_broadphase_collect_pairs_sorted(bp);
}

static inline void oimo_broadphase_init(OimoBroadPhase* bp, int type) {
    bp->_type = type;
    bp->_numProxies = 0;
    bp->_proxyList = NULL;
    bp->_proxyListLast = NULL;
    bp->_proxyPairList = NULL;
    bp->_incremental = 0;
    bp->_testCount = 0;
    bp->_proxyPairPool = NULL;
    bp->_idCount = 0;
    bp->_proxyStorageUsed = 0;
    bp->_pairStorageUsed = 0;
    bp->_freeProxyCount = 0;
    bp->_numStatics = 0;
    bp->_numWideStatics = 0;
    bp->_maxStaticWidth = 0.0f;
    bp->_staticsDirty = 1;
    bp->_numMovers = 0;
    bp->_maxMoverWidth = 0.0f;
    bp->_listChanged = 1;
}

static inline OimoProxy* oimo_broadphase_create_proxy(OimoBroadPhase* bp, void* userData, const OimoAabb* aabb) {
    OimoProxy* p;
    if (bp->_freeProxyCount > 0) {
        p = bp->_freeProxies[--bp->_freeProxyCount];
    } else if (bp->_proxyStorageUsed < OIMO_MAX_PROXIES) {
        p = &bp->_proxyStorage[bp->_proxyStorageUsed++];
    } else {
        ARM_LIMIT_REACHED(OIMO_MAX_PROXIES);
        return NULL;
    }
    oimo_proxy_init(p, userData, bp->_idCount++);

    // Set AABB
    p->_aabbMin = aabb->min;
    p->_aabbMax = aabb->max;

    // Add to broadphase
    oimo_broadphase_add_proxy(bp, p);

    return p;
}

static inline void oimo_broadphase_destroy_proxy(OimoBroadPhase* bp, OimoProxy* p) {
    oimo_broadphase_remove_proxy(bp, p);
    bp->_freeProxies[bp->_freeProxyCount++] = p;
}

static inline void oimo_broadphase_add_proxy(OimoBroadPhase* bp, OimoProxy* p) {
    bp->_numProxies++;
    bp->_listChanged = 1;
    oimo_broadphase_list_push(bp, p);
}

static inline void oimo_broadphase_remove_proxy(OimoBroadPhase* bp, OimoProxy* p) {
    bp->_numProxies--;
    bp->_listChanged = 1;
    oimo_broadphase_list_remove(bp, p);
}

static inline int oimo_broadphase_is_overlapping(const OimoProxy* p1, const OimoProxy* p2) {
    return (p1->_aabbMin.x <= p2->_aabbMax.x && p1->_aabbMax.x >= p2->_aabbMin.x) &&
           (p1->_aabbMin.y <= p2->_aabbMax.y && p1->_aabbMax.y >= p2->_aabbMin.y) &&
           (p1->_aabbMin.z <= p2->_aabbMax.z && p1->_aabbMax.z >= p2->_aabbMin.z);
}

static inline OimoProxyPair* oimo_broadphase_get_proxy_pair_list(const OimoBroadPhase* bp) {
    return bp->_proxyPairList;
}

static inline int oimo_broadphase_is_incremental(const OimoBroadPhase* bp) {
    return bp->_incremental;
}

static inline int oimo_broadphase_get_test_count(const OimoBroadPhase* bp) {
    return bp->_testCount;
}

// AABB-segment intersection using SAT (6 axes)
static inline int oimo_aabb_segment_test(
    const OimoVec3* aabbMin,
    const OimoVec3* aabbMax,
    const OimoVec3* begin,
    const OimoVec3* end
) {
    OimoScalar x1 = begin->x, y1 = begin->y, z1 = begin->z;
    OimoScalar x2 = end->x, y2 = end->y, z2 = end->z;

    // Segment bounding box
    OimoScalar sminx = oimo_min(x1, x2);
    OimoScalar sminy = oimo_min(y1, y2);
    OimoScalar sminz = oimo_min(z1, z2);
    OimoScalar smaxx = oimo_max(x1, x2);
    OimoScalar smaxy = oimo_max(y1, y2);
    OimoScalar smaxz = oimo_max(z1, z2);

    OimoScalar pminx = aabbMin->x, pminy = aabbMin->y, pminz = aabbMin->z;
    OimoScalar pmaxx = aabbMax->x, pmaxy = aabbMax->y, pmaxz = aabbMax->z;

    // Test axes (1,0,0), (0,1,0), (0,0,1)
    if (pminx > smaxx || pmaxx < sminx ||
        pminy > smaxy || pmaxy < sminy ||
        pminz > smaxz || pmaxz < sminz) {
        return 0;
    }

    // Segment direction
    OimoScalar dx = x2 - x1;
    OimoScalar dy = y2 - y1;
    OimoScalar dz = z2 - z1;
    OimoScalar adx = oimo_abs(dx);
    OimoScalar ady = oimo_abs(dy);
    OimoScalar adz = oimo_abs(dz);

    // AABB half-extents and center
    OimoScalar pextx = (pmaxx - pminx) * 0.5f;
    OimoScalar pexty = (pmaxy - pminy) * 0.5f;
    OimoScalar pextz = (pmaxz - pminz) * 0.5f;
    OimoScalar pcntx = (pmaxx + pminx) * 0.5f;
    OimoScalar pcnty = (pmaxy + pminy) * 0.5f;
    OimoScalar pcntz = (pmaxz + pminz) * 0.5f;

    // Vector from AABB center to segment start
    OimoScalar cpx = x1 - pcntx;
    OimoScalar cpy = y1 - pcnty;
    OimoScalar cpz = z1 - pcntz;

    // Test cross product axes:
    // axis4: d × (1,0,0) = (0, dz, -dy)
    // axis5: d × (0,1,0) = (-dz, 0, dx)
    // axis6: d × (0,0,1) = (dy, -dx, 0)
    if (oimo_abs(cpy * dz - cpz * dy) - (pexty * adz + pextz * ady) > 0 ||
        oimo_abs(cpz * dx - cpx * dz) - (pextz * adx + pextx * adz) > 0 ||
        oimo_abs(cpx * dy - cpy * dx) - (pextx * ady + pexty * adx) > 0) {
        return 0;
    }

    return 1;
}

