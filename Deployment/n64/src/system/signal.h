/**
 * Signal System Header
 * ====================
 *
 * Per-instance signals for event-driven communication between traits.
 * Similar to Godot's signal system.
 *
 * Usage in Haxe traits:
 *   var onDeath:Signal;                    // Declare signal
 *
 *   onDeath.connect(myCallback);           // Subscribe
 *   onDeath.disconnect(myCallback);        // Unsubscribe
 *   onDeath.emit(damage, attacker);        // Emit with any number of args
 */

#pragma once

#include "../types.h"

// Functions are declared in types.h since ArmSignal is defined there.
// Payload structs and static signals of the game (generated)
#include "../data/signals.h"
