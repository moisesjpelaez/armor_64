// UI panels: declarations. Keys and counts: panel_defs.h (generated).
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "../data/scenes.h"

#ifdef __cplusplus
extern "C" {
#endif

// Panel keys (UI_PANEL_*), UI_PANEL_COUNT, UI_MAX_PANELS
#include "panel_defs.h"

// Runtime panel element — simple filled rectangle with border
typedef struct {
    int16_t pos_x, pos_y;
    int16_t width, height;
    uint8_t bg_r, bg_g, bg_b, bg_a;
    uint8_t border_r, border_g, border_b, border_a;
    int8_t  border_size;
    bool    visible;
    bool    initial_visible;
} UIPanel;

#if UI_PANEL_COUNT > 0

UIPanel* canvas_get_panel(int index);

// Internal - called by canvas orchestrator
void panel_clear(void);
void panel_scene_init(SceneId scene_id);
void panel_render(void);
uint32_t panel_state_hash(uint32_t h);  // Adds everything panel_render draws

#else

static inline UIPanel* canvas_get_panel(int i) { (void)i; return NULL; }
static inline void panel_clear(void) {}
static inline void panel_scene_init(SceneId s) { (void)s; }
static inline void panel_render(void) {}
static inline uint32_t panel_state_hash(uint32_t h) { return h; }

#endif // UI_PANEL_COUNT > 0

#ifdef __cplusplus
}
#endif
