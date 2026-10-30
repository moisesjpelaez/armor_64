// UI canvas: groups, elements and Koui scenes. Keys and counts: canvas_defs.h (generated).
#pragma once

// Sub-module headers provide all element types and their management APIs:
#include "label.h"   // UILabel, UIAnchor, CANVAS_WIDTH/HEIGHT
#include "image.h"   // UIImage
#include "button.h"  // UIButton, button input/render
#include "panel.h"   // UIPanel, simple filled rectangle

#ifdef __cplusplus
extern "C" {
#endif

// Group keys (UI_GROUP_*), Koui scene keys (UI_SCENE_*), UI_GROUP_COUNT,
// UI_ELEMENT_COUNT (top-level images + groups), UI_SCENE_COUNT, UI_MAX_GROUPS,
// UI_MAX_GROUP_CHILDREN
#include "canvas_defs.h"

#if UI_LABEL_COUNT > 0 || UI_IMAGE_COUNT > 0 || UI_GROUP_COUNT > 0 || UI_BUTTON_COUNT > 0 || UI_PANEL_COUNT > 0

#if UI_GROUP_COUNT > 0
// Group element — container with shared visibility for child images/labels/buttons
typedef struct {
    uint8_t child_image_indices[UI_MAX_GROUP_CHILDREN];
    uint8_t child_label_indices[UI_MAX_GROUP_CHILDREN];
    uint8_t child_button_indices[UI_MAX_GROUP_CHILDREN];
    uint8_t child_panel_indices[UI_MAX_GROUP_CHILDREN];
    uint8_t child_group_indices[UI_MAX_GROUP_CHILDREN];
    uint8_t num_child_images;
    uint8_t num_child_labels;
    uint8_t num_child_buttons;
    uint8_t num_child_panels;
    uint8_t num_child_groups;
    bool visible;
} UIGroup;
#endif // UI_GROUP_COUNT > 0

// Element type for the unified elements[] array
typedef enum {
    UI_ELEM_IMAGE = 0,
    UI_ELEM_GROUP = 1,
} UIElementType;

// Unified element reference (maps Haxe elements[i] → image or group index)
typedef struct {
    UIElementType type;
    uint8_t index;
} UIElement;

// Canvas lifecycle
void canvas_init(void);                     // Call once at startup
void canvas_scene_init(SceneId scene_id);   // Call on scene change
void canvas_clear(void);                    // Reset all pools
void canvas_update(float dt);               // Call every frame (delegates to button_update)
void canvas_render(void);                   // Call after 3D render, before display_show

// UI scene switching (Koui SceneManager)
#if UI_SCENE_COUNT > 0
void canvas_set_ui_scene(int scene_idx);    // Switch active Koui UI scene
#else
static inline void canvas_set_ui_scene(int s) { (void)s; }
#endif

#if UI_GROUP_COUNT > 0
UIGroup* canvas_get_group(int index);
void ui_group_set_visible(UIGroup *group, bool visible);
#endif

UIElement* canvas_get_element(int index);
void canvas_element_set_visible(int index, bool visible);
bool canvas_element_visible(int index);

#else
// Stubs when no UI elements exist
static inline void canvas_init(void) {}
static inline void canvas_scene_init(SceneId s) { (void)s; }
static inline void canvas_clear(void) {}
static inline void canvas_update(float dt) { (void)dt; }
static inline void canvas_render(void) {}
static inline void canvas_element_set_visible(int i, bool v) { (void)i; (void)v; }
static inline bool canvas_element_visible(int i) { (void)i; return false; }
static inline void canvas_set_ui_scene(int s) { (void)s; }

#endif // UI_LABEL_COUNT > 0 || UI_IMAGE_COUNT > 0 || UI_GROUP_COUNT > 0 || UI_BUTTON_COUNT > 0 || UI_PANEL_COUNT > 0

#ifdef __cplusplus
}
#endif
