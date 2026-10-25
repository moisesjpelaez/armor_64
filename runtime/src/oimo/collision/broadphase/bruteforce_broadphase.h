#pragma once

#include "broadphase.h"

static inline void oimo_bruteforce_broadphase_init(OimoBroadPhase* bp) {
    oimo_broadphase_init(bp, OIMO_BROADPHASE_BRUTE_FORCE);
    bp->_incremental = 0;  // Brute force is not incremental
}

static inline int oimo_bruteforce_overlap(const OimoProxy* p1, const OimoProxy* p2) {
    // M.aabb_overlap(p1._aabbMin, p1._aabbMax, p2._aabbMin, p2._aabbMax)
    return (p1->_aabbMin.x <= p2->_aabbMax.x && p1->_aabbMax.x >= p2->_aabbMin.x) &&
           (p1->_aabbMin.y <= p2->_aabbMax.y && p1->_aabbMax.y >= p2->_aabbMin.y) &&
           (p1->_aabbMin.z <= p2->_aabbMax.z && p1->_aabbMax.z >= p2->_aabbMin.z);
}

static inline OimoProxy* oimo_bruteforce_create_proxy(OimoBroadPhase* bp, void* userData, const OimoAabb* aabb) {
    // Allocate proxy from storage
    if (bp->_proxyStorageUsed >= OIMO_MAX_PROXIES) {
        return NULL;  // Out of proxies
    }

    OimoProxy* proxy = &bp->_proxyStorage[bp->_proxyStorageUsed++];
    oimo_proxy_init(proxy, userData, bp->_idCount++);

    oimo_broadphase_add_proxy(bp, proxy);
    oimo_proxy_set_aabb(proxy, aabb);

    return proxy;
}

static inline void oimo_bruteforce_destroy_proxy(OimoBroadPhase* bp, OimoProxy* proxy) {
    oimo_broadphase_remove_proxy(bp, proxy);
    proxy->userData = NULL;
}

static inline void oimo_bruteforce_move_proxy(OimoBroadPhase* bp, OimoProxy* proxy, const OimoAabb* aabb, const OimoVec3* displacement) {
    (void)bp;           // Unused in brute force
    (void)displacement; // Unused in brute force (no predictive expansion)
    oimo_proxy_set_aabb(proxy, aabb);
}

static inline void oimo_bruteforce_ray_cast(
    OimoBroadPhase* bp,
    const OimoVec3* begin,
    const OimoVec3* end,
    OimoBroadPhaseProxyCallback callback,
    void* callbackUserData
) {
    OimoProxy* p = bp->_proxyList;
    while (p != NULL) {
        if (oimo_aabb_segment_test(&p->_aabbMin, &p->_aabbMax, begin, end)) {
            callback(p, callbackUserData);
        }
        p = p->_next;
    }
}

static inline void oimo_bruteforce_aabb_test(
    OimoBroadPhase* bp,
    const OimoAabb* aabb,
    OimoBroadPhaseProxyCallback callback,
    void* callbackUserData
) {
    OimoProxy* p = bp->_proxyList;
    while (p != NULL) {
        // M.aabb_overlap(aabb._min, aabb._max, p._aabbMin, p._aabbMax)
        if ((aabb->min.x <= p->_aabbMax.x && aabb->max.x >= p->_aabbMin.x) &&
            (aabb->min.y <= p->_aabbMax.y && aabb->max.y >= p->_aabbMin.y) &&
            (aabb->min.z <= p->_aabbMax.z && aabb->max.z >= p->_aabbMin.z)) {
            callback(p, callbackUserData);
        }
        p = p->_next;
    }
}

