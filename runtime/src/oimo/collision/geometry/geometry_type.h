#pragma once

typedef enum OimoGeometryType {
    OIMO_GEOMETRY_SPHERE  = 0,
    OIMO_GEOMETRY_BOX     = 1,
    OIMO_GEOMETRY_CAPSULE = 2,
    OIMO_GEOMETRY_STATIC_MESH = 3,

    // Count of supported types
    OIMO_GEOMETRY_TYPE_COUNT = 4
} OimoGeometryType;

