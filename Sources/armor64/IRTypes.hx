package armor64;

/**
 * IR Types for N64 Trait/Autoload Macros
 *
 * These types define the intermediate representation (IR) format
 * that the macros emit and Python consumes.
 *
 * IR Node Types Reference:
 * ========================
 *
 * LITERALS:
 *   int, float, string, bool, null   - Literals
 *   empty_array                      - []
 *   skip                             - Left out (warn: reported); an expression with a skip operand is one (N64Extractor.droppedOperand)
 *   c_literal                        - Raw C code passthrough
 *
 * VARIABLES:
 *   member                           - Trait data member: data->name; owner: the declaring trait;
 *                                      object: another instance's data (its member); a static var is a global
 *   ident                            - Identifier: local var, dt, object, this
 *   field_access                     - Field access: object.field, vec.x, this.field
 *   autoload_field                   - Access to another autoload's member
 *   autoload_trait_object            - Get object pointer from autoload trait member
 *   autoload_trait_assign            - AutoloadClass.member = this (the trait's data)
 *   method_ref                       - A method as a value (cName of its C function)
 *   array_access                     - a[i] on a C array (typed containers are below)
 *
 * OPERATORS:
 *   assign                           - Assignment: target = value
 *   binop                            - Binary operator: a + b
 *   unop                             - Unary operator: !a, -a, ++a
 *   string_equals                    - String == / != (strcmp)
 *
 * CONTROL FLOW:
 *   if                               - Conditional: if/else
 *   block                            - Code block { ... }
 *   var                              - Local variable declaration
 *   return                           - Return statement
 *   while, do_while                  - Loop constructs
 *   for_range                        - For loop with range
 *   break, continue                  - Loop control
 *   ternary                          - Ternary operator a ? b : c
 *
 * CALLS:
 *   c_template                       - A call bound to C (Bindings.hx): c_code = the template,
 *                                      args = its {0}..., object = its {self} (if any),
 *                                      props.in_place: {self} is written to
 *   call                             - A C function call (value = its name)
 *   super_call                       - super.method(): cName of the declaring ancestor's C function;
 *                                      super(): the parent's constructor (on_create)
 *   trait_method_call                - A method of the trait or an ancestor (cName; vtable when overridden);
 *                                      object: another instance (its data and object); props.is_static: no instance
 *   callback_wrapper                 - An anonymous function passed to a method (body, captures)
 *   callback_param_call              - Calling a function parameter (name)
 *   scene_call, input_call           - Scene.setActive(), input calls
 *   vec_call                         - Vector clone() / set() (on transform vectors: c_template, N64Extractor)
 *   tween_float, tween_vec4, tween_delay, tween_start, tween_stop, tween_pause - Tweens (props: on_update, on_done callbacks)
 *   color_from_floats                - Color.fromFloats(); render2d_set_color: g2.color = c
 *   scene_get_group_count            - Scene.active.getGroup(name).length
 *   signal_connect, signal_disconnect, signal_emit - Signals (see SignalCallConverter)
 *   canvas_get_label, label_set_text - UI calls (label_set_text.args: [label, text])
 *   canvas_element_set_visible       - elements[i].visible = v on Koui's element list (Array<Element>)
 *   ui_group_set_visible, button_field - Layout visibility, field of a Button (pool index)
 *   canvas_get_button, canvas_get_group, canvas_set_scene, canvas_clear_scenes - Koui canvas
 *   button_register_callback         - onPressed/onFocus... (props: event, callback_name, captures; body)
 *   button_grab_focus                - button.grabFocus()
 *   autoload_call                    - Call to autoload function
 *   debug_call                       - trace() -> debugf()
 *   lifecycle_register               - notifyOnX(f): value = site id (meta.lifecycle_sites), props.captures
 *   lifecycle_remove                 - removeX(f): value = kind, props.method
 *   str_format                       - String built from parts (StringFormatter; props.kinds)
 *
 * AUDIO:
 *   sound_handle_equals              - handle == / != (null: ARM_SOUND_HANDLE_NULL)
 *   sound_handle_finished            - handle.finished: !arm_audio_is_playing(ptr)
 *   address_of, deref                - A handle as a pointer / a pointer local's value
 *   audio_load, audio_mix_volume, audio_mix_volume_get - Aura calls
 *   audio_handle_play, _pause, _stop, _volume          - Sound handle methods
 *
 * CONSTRUCTORS:
 *   new_vec, tween_alloc             - Vector literal, tween from the pool
 *
 * CONTAINERS (object = the map or array, value = its C type):
 *   map_set, map_get, map_value      - map_get gives a pointer (container values),
 *   map_exists, map_remove, map_clear  map_value a copy
 *   array_push, array_pop, array_get, array_clear
 *   array_set                        - c[k] = v (also map_set): children = [key, value]
 *   container_element                - Pointer to c[k], written in place (c[k] += v, c[k]++),
 *                                      inside a deref: props.map, props.pointer
 *   array_get_nested                 - map[key][i]: children = [inner get, index]
 *   array_get_nested_ptr             - The same, as a pointer (a sound handle local)
 *   (props.pointer: object is already a pointer, as map[key] in map[key][i])
 */

typedef IRNode = {
    ?maxLength: Null<Int>,  // Strings: most UTF-8 bytes the value can have (null: unknown), not written to the IR
    type: String,
    ?value: Dynamic,
    ?children: Array<IRNode>,
    ?args: Array<IRNode>,
    ?method: String,
    ?object: IRNode,
    ?props: Dynamic,
    ?c_code: String,
    ?c_func: String,   // Direct C function name for 1:1 translation
    ?cName: String,    // C function name for trait_method_call
    ?trait: String,    // Trait name for trait_method_call
    ?warn: String,     // Warning message to emit as C comment (for skip nodes)
    // Inherited member fields
    ?owner: String,       // Parent trait that owns this member
    // Callback parameter call fields
    ?name: String,        // Name of callback parameter
    ?paramType: String,   // Type of callback parameter (e.g., "Void->Void")
    // Callback wrapper fields (for inherited method callbacks)
    ?callback_name: String,  // Generated C function name for callback
    ?body: Array<IRNode>,    // Callback body IR nodes
    ?captures: Array<Dynamic>,  // Captured variables
    ?param_name: String,     // Parameter name in callback
    ?param_type: String,     // Haxe type of callback parameter
    ?param_ctype: String     // C type of callback parameter
}

typedef MemberIR = {
    haxeType: String,
    ctype: String,
    defaultValue: IRNode,
    ?traitRef: String, // Typed as a trait (full path): ctype is a pointer to its data
    ?isStatic: Bool    // A trait's static var: one C global, <trait C name>_<name>
}

typedef ContactEventMeta = {
    event_name: String,      // Handler function name, e.g., "onContact"
    handler_name: String,    // Full C handler name, e.g., "arm_player_player_contact_onContact"
    subscribe: Bool,         // true for notifyOnContact, false for removeContact
    param_name: String       // The handler's RigidBody parameter (the other body's object in C)
}

typedef SignalMeta = {
    name: String             // Signal member name, e.g., "onDeath"
}

/**
 * A signal used by the game, identified by "<declaring class>.<name>" (see
 * N64MacroBase.signals): written to n64_signals.json for the C generator.
 */
typedef SignalDef = {
    owner: String,           // Declaring class, full path
    name: String,            // Field name
    isStatic: Bool,          // Static (one global ArmSignal) or per-instance (in the owner's data)
    ?cSymbol: String,        // Global C symbol of a static signal, e.g., "g_gameevents_levelWon"
    handlerTypes: Array<Null<String>>, // Argument C types from typed handlers (preferred)
    emitTypes: Array<Null<String>>,    // Argument C types from emit() calls
    connectSites: Array<String>        // Class of each connect() call (one subscriber per instance at most)
}

/**
 * Method parameter definition for callable methods.
 */
typedef MethodParamIR = {
    name: String,            // Parameter name
    haxeType: String,        // Haxe type (Int, Float, SceneId, etc.)
    ctype: String            // C type (int32_t, float, SceneId, etc.)
}

/**
 * Method definition for callable trait methods.
 * All non-lifecycle methods are generated as callable C functions.
 */
typedef MethodIR = {
    name: String,            // Method name, e.g., "init", "loadScene"
    cName: String,           // Full C function name, e.g., "arm_gamescene_init"
    params: Array<MethodParamIR>,  // Parameters (not including obj/data which are implicit)
    returnType: String,      // C return type ("void", "int32_t", etc.)
    body: Array<IRNode>,     // Method body as IR nodes
    ?isVirtual: Bool         // True if method is public (can be overridden by children)
}

typedef TraitMeta = {
    uses_input: Bool,
    uses_transform: Bool,
    mutates_transform: Bool,   // True if trait modifies transform (translate, rotate, etc.)
    uses_time: Bool,
    uses_physics: Bool,
    uses_ui: Bool,             // True if trait uses UI labels (canvas.getElementAs, label.text)
    uses_tween: Bool,          // True if trait uses Tween (tween.float, tween.delay, etc.)
    uses_autoload: Bool,       // True if trait accesses autoload fields/methods
    contact_events: Array<ContactEventMeta>, // physics contact subscriptions
    signals: Array<SignalMeta>, // per-instance signal declarations
    ?connects_signals: Bool,   // Connects handlers with its data as context: needs a data struct
    ?uses_audio: Bool,         // Calls the audio API
    ?allocates_tweens: Bool,   // new Tween() outside member defaults: not countable (see limits.py)
    ?autoloads_used: Array<String>, // C names of the autoloads it refers to (their headers are included)
    ?stores_self: Bool,            // True if trait stores `this` elsewhere (e.g. MainInstances.player = this): needs a data struct
    // Lifecycle registrations: {id, kind, order (source position), method (or null), late_init}
    ?lifecycle_sites: Array<Dynamic>
}

/**
 * Super call metadata - tracks super() and super.method() calls for inheritance.
 */
typedef SuperCallMeta = {
    type: String,            // "super_new" for super() or "super_method" for super.method()
    method: String,          // Method name (empty for super_new)
    args: Array<IRNode>      // Arguments to pass
}

typedef TraitIR = {
    name: String,
    module: String,
    cName: String,
    needsData: Bool,
    ?parent: String,         // Parent trait name (null if none or base Trait class)
    members: Map<String, MemberIR>,
    methods: Map<String, MethodIR>,  // All callable methods (non-lifecycle)
    events: Map<String, Array<IRNode>>,  // Lifecycle events (on_ready, on_update, etc.)
    meta: TraitMeta
}

// ============================================================================
// Autoload IR Types
// ============================================================================

typedef AutoloadFunctionIR = {
    name: String,            // Haxe function name, e.g., "doSomething"
    cName: String,           // C function name, e.g., "myautoload_doSomething"
    returnType: String,      // C return type, e.g., "float" or "void"
    params: Array<AutoloadParamIR>,  // Function parameters
    body: Array<IRNode>,     // Function body as IR nodes
    isPublic: Bool           // Whether function is public (exported) or private (internal)
}

typedef AutoloadParamIR = {
    name: String,            // Parameter name
    haxeType: String,        // Haxe type
    ctype: String,           // C type
    ?optional: Bool,         // Whether parameter is optional (?param or has default)
    ?defaultValue: IRNode    // Default value expression if optional
}

typedef AutoloadMeta = {
    order: Int,              // Initialization order (lower = earlier)
    ?uses_audio: Bool,       // Calls the audio API
    ?allocates_tweens: Bool  // new Tween() outside member defaults
}

typedef AutoloadIR = {
    name: String,            // Class name, e.g., "GameEvents"
    module: String,          // Full module path, e.g., "arm.autoload.GameEvents"
    cName: String,           // C-safe prefix, e.g., "gameevents"
    order: Int,              // Initialization order
    members: Map<String, MemberIR>,       // Static members (non-Signal)
    functions: Map<String, AutoloadFunctionIR>,  // Static functions
    hasInit: Bool,           // Whether class has init() function
    meta: AutoloadMeta
}
