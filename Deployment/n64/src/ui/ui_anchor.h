/* N64 Exporter - Shared UI anchor position helpers
 * Inline anchor calculation used by label, image, and panel modules.
 *
 * Anchor values match the Koui UIAnchor enum (0-8):
 *   0=TopLeft  1=TopCenter  2=TopRight
 *   3=MidLeft  4=MidCenter  5=MidRight
 *   6=BotLeft  7=BotCenter  8=BotRight
 */
#pragma once

#include <stdint.h>

static inline int16_t ui_anchor_x(int16_t pos_x, int16_t width, uint8_t anchor,
                                   int16_t canvas_w) {
    switch (anchor) {
        case 1: case 4: case 7: return (canvas_w / 2) - (width / 2) + pos_x;
        case 2: case 5: case 8: return canvas_w - width + pos_x;
        default:                return pos_x;
    }
}

static inline int16_t ui_anchor_y(int16_t pos_y, int16_t height, uint8_t anchor,
                                   int16_t canvas_h) {
    switch (anchor) {
        case 3: case 4: case 5: return (canvas_h / 2) - (height / 2) + pos_y;
        case 6: case 7: case 8: return canvas_h - height + pos_y;
        default:                return pos_y;
    }
}
