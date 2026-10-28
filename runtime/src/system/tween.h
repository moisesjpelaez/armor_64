/**
 * Tween system for N64 - Fixed pool, maximum performance
 * No dynamic allocation, direct function pointer callbacks
 */

#ifndef ARM_TWEEN_H
#define ARM_TWEEN_H

#include <stdint.h>
#include <stdbool.h>
#include "../types.h"
#include "../limits.h"

// Tween type enum
typedef enum {
    TWEEN_NONE = 0,       // Slot is free
    TWEEN_ALLOCATED = 1,  // Slot is allocated but not yet configured
    TWEEN_FLOAT = 2,
    TWEEN_VEC4 = 3,
    TWEEN_DELAY = 4
} TweenType;

// Easing enum
typedef enum {
    EASE_LINEAR = 0,
    EASE_QUAD_IN,
    EASE_QUAD_OUT,
    EASE_QUAD_IN_OUT,
    EASE_CUBIC_IN,
    EASE_CUBIC_OUT,
    EASE_CUBIC_IN_OUT,
    EASE_SINE_IN,
    EASE_SINE_OUT,
    EASE_SINE_IN_OUT
} TweenEase;

// Callback signatures
typedef void (*TweenFloatCallback)(float value, void* obj, void* data);
typedef void (*TweenVec4Callback)(ArmVec4* value, void* obj, void* data);
typedef void (*TweenDoneCallback)(void* obj, void* data);

// Tween structure
typedef struct ArmTween {
    TweenType type;
    TweenEase ease;

    // Values
    float from_f;
    float to_f;
    ArmVec4 from_v;
    ArmVec4 to_v;
    ArmVec4 current_v;  // For vec4 callback

    // Timing
    float duration;
    float elapsed;

    // Callbacks
    TweenFloatCallback on_float;
    TweenVec4Callback on_vec4;
    TweenDoneCallback on_done;

    // Context
    void* object;
    void* data;

    // State
    bool active;        // Started and not finished or stopped (Armory's !isStopped())
    bool paused;
    bool persistent;    // Autoload tween: kept across scene changes (tween_clear_all)
} ArmTween;

// Pool management
ArmTween* tween_alloc(void);
ArmTween* tween_alloc_persistent(void);
void tween_free(ArmTween* tween);

// Configuration (call before start)
void tween_float(ArmTween* tween, float from, float to, float duration,
                 TweenFloatCallback on_update, TweenDoneCallback on_done,
                 TweenEase ease, void* object, void* data);

void tween_vec4(ArmTween* tween, ArmVec4* from, ArmVec4* to, float duration,
                TweenVec4Callback on_update, TweenDoneCallback on_done,
                TweenEase ease, void* object, void* data);

void tween_delay(ArmTween* tween, float duration,
                 TweenDoneCallback on_done, void* object, void* data);

// Control
void tween_start(ArmTween* tween);
void tween_pause(ArmTween* tween);
void tween_stop(ArmTween* tween);
bool tween_is_stopped(ArmTween* tween);

// System update - once per frame, with the trait updates (paused with them)
void tween_update_all(float dt);

// Scene switch: cancel the scene's tweens without firing callbacks (their trait
// data is freed), reporting the ones the game left running. Autoload tweens stay
void tween_clear_all(void);

#endif // ARM_TWEEN_H
