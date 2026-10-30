package armor64;

#if macro
import haxe.macro.Context;
import haxe.macro.Expr;
import haxe.macro.Type;

import armor64.IRTypes;
import armor64.N64MacroBase;
import armor64.N64Extractor;
import armor64.TypedIndex;
import armor64.mapping.Constants;
import armor64.mapping.TypeMap;
import armor64.mapping.EngineTypes;
import armor64.converters.SceneCallConverter;
import armor64.converters.CanvasCallConverter;
import armor64.converters.VecCallConverter;
import armor64.converters.PhysicsCallConverter;
import armor64.converters.InputCallConverter;
import armor64.converters.SignalCallConverter;
import armor64.converters.StdCallConverter;
import armor64.converters.ObjectCallConverter;
import armor64.converters.AutoloadCallConverter;
import armor64.converters.AudioCallConverter;
import armor64.converters.TweenCallConverter;
import armor64.converters.MapCallConverter;
import armor64.converters.ArrayCallConverter;
import armor64.converters.ButtonCallConverter;

using haxe.macro.ExprTools;
using StringTools;

/**
 * Traits (classes extending iron.Trait) → n64_traits.json.
 *
 * Lifecycle callbacks become events (on_ready, on_update, ...), other methods
 * callable C functions, members fields of the trait's data struct. The
 * expression conversion is shared with autoloads (N64Extractor).
 */
class N64TraitMacro {
    static var traitData:Map<String, TraitIR> = new Map();
    static var initialized:Bool = false;

    /** Called by N64Macro.build() for a trait class. */
    public static function build(localClass:Ref<ClassType>, fields:Array<Field>):Void {
        if (!initialized) {
            initialized = true;
            N64MacroBase.addWriter(writeTraitJson);
        }

        // Extract trait IR after typing, when the TypedIndex is available
        N64MacroBase.deferExtraction(localClass, function() {
            // Get the class again: the build-time snapshot has no typed fields yet
            var extractor = new TraitExtractor(fields, localClass.get());
            var traitIR = extractor.extract();

            // Traits are identified by their full class path in the IR
            if (traitIR != null) {
                traitData.set(extractor.traitKey, traitIR);
            }
        });
    }

    static function writeTraitJson():Void {
        if (traitData.keys().hasNext() == false) return;

        var traits:Dynamic = {};

        for (name in traitData.keys()) {
            var ir = traitData.get(name);

            // Skip empty traits (no events and doesn't need data struct)
            // But include if trait has contact_events in meta
            // Also include if trait has a parent (needed for inheritance chain)
            var hasEvents = Lambda.count(ir.events) > 0;
            var hasContactEvents = ir.meta.contact_events.length > 0;
            var hasParent = ir.parent != null;
            if (!hasEvents && !hasContactEvents && !ir.needsData && !hasParent) continue;

            // Convert members
            var membersArr:Array<Dynamic> = [];
            for (memberName in ir.members.keys()) {
                var m = ir.members.get(memberName);
                var memberObj:Dynamic = {
                    name: memberName,
                    type: m.haxeType,
                    ctype: m.ctype,
                    default_value: N64MacroBase.serializeIRNode(m.defaultValue)
                };
                if (m.setter != null) {
                    memberObj.setter = m.setter;
                }
                if (m.traitRef != null) {
                    memberObj.trait_ref = m.traitRef;
                }
                membersArr.push(memberObj);
            }

            // Convert events
            var eventsObj:Dynamic = {};
            for (eventName in ir.events.keys()) {
                var eventNodes = ir.events.get(eventName);
                Reflect.setField(eventsObj, eventName, [for (n in eventNodes) N64MacroBase.serializeIRNode(n)]);
            }

            // Convert methods (non-lifecycle callable functions)
            var methodsObj:Dynamic = {};
            for (methodName in ir.methods.keys()) {
                var method = ir.methods.get(methodName);
                var paramsArr = [for (p in method.params) {
                    name: p.name,
                    haxeType: p.haxeType,
                    ctype: p.ctype
                }];
                var methodObj:Dynamic = {
                    name: method.name,
                    cName: method.cName,
                    params: paramsArr,
                    returnType: method.returnType,
                    body: [for (n in method.body) N64MacroBase.serializeIRNode(n)]
                };
                Reflect.setField(methodsObj, methodName, methodObj);
            }

            // Build trait JSON object
            var traitObj:Dynamic = {
                module: ir.module,
                c_name: ir.cName,
                members: membersArr,
                methods: methodsObj,  // All callable methods (non-lifecycle)
                events: eventsObj,
                meta: ir.meta
            };

            // Add parent if this trait inherits from another user trait
            if (ir.parent != null) {
                Reflect.setField(traitObj, "parent", ir.parent);
            }

            Reflect.setField(traits, name, traitObj);
        }

        var output:Dynamic = {
            ir_version: 1,
            traits: traits
        };

        N64MacroBase.writeJsonFile("n64_traits.json", output);
    }
}

// ============================================================================
// Trait Extractor
// ============================================================================

class TraitExtractor extends N64Extractor {
    var methodIRMap:Map<String, MethodIR> = new Map();   // Extracted methods as IR for C generation
    var events:Map<String, Array<IRNode>> = new Map();
    var currentSetterTarget:String;  // Inside a setter body, the member it sets (written directly, not through the setter)

    // Lifecycle method names that are handled specially (not as callable methods)
    static var lifecycleMethodNames:Array<String> = ["new", "__onInit", "__onUpdate", "__onFixedUpdate", "__onLateUpdate", "__onRemove", "__onRender2D"];

    var parentName:String;  // Parent user trait (full path), null if none
    public var traitKey(default, null):String;  // Full class path, identifies the trait in the IR

    public function new(fields:Array<Field>, cls:ClassType) {
        super(fields, cls, N64Extractor.traitCName(cls), [
            new VecCallConverter(),
            new PhysicsCallConverter(),
            new InputCallConverter(),
            new SignalCallConverter(),
            new StdCallConverter(),
            new ObjectCallConverter(),
            new SceneCallConverter(),
            new CanvasCallConverter(),
            new AutoloadCallConverter(),
            new AudioCallConverter(),
            new TweenCallConverter(),
            new MapCallConverter(),
            new ArrayCallConverter(),
            new ButtonCallConverter(),
        ]);
        this.traitKey = cls.pack.concat([cls.name]).join(".");

        // Only user traits are parents (not iron.Trait or armory.* classes)
        if (cls.superClass != null) {
            var superClass = cls.superClass.t.get();
            if (!superClass.module.startsWith("iron.") && !superClass.module.startsWith("armory.")) {
                parentName = superClass.pack.concat([superClass.name]).join(".");
            }
        }
    }

    public function extract():TraitIR {
        // Pass 1: Extract members and methods
        for (field in fields) {
            switch (field.kind) {
                case FVar(t, e):
                    collectVar(field.name, t, e);
                case FProp(_, set, t, e):
                    // Property with a backing field (e.g., @:isVar var paused(default, set): Bool)
                    collectVar(field.name, t, e, set);
                case FFun(func):
                    methodMap.set(field.name, func);
                default:
            }
        }

        // Pass 2: Find lifecycle registrations and extract events
        // This finds ALL registered lifecycles (for code extraction)
        var lifecycles = findLifecycles();
        // This finds only STATICALLY registered lifecycles (for initial enabled state)
        var staticLifecycles = findStaticLifecycles();

        // Pass 3: Convert lifecycle functions to events
        // on_add runs BEFORE on_ready - used for setting up autoload references
        if (lifecycles.add != null) {
            extractEvents("on_add", lifecycles.add);
        }
        if (lifecycles.init != null) {
            extractEvents("on_ready", lifecycles.init);
        }

        // Handle multiple update functions - each gets its own event
        // Collect keys first to avoid iterator issues
        var updateCallbackNames = [for (k in lifecycles.updates.keys()) k];
        if (updateCallbackNames.length > 0) {
            // We have named update functions - generate separate events for each
            for (callbackName in updateCallbackNames) {
                var body = lifecycles.updates.get(callbackName);
                if (body != null) {
                    // Use format: on_update_functionName (e.g., on_update_update, on_update_winUpdate)
                    extractEvents("on_update_" + callbackName, body);
                    meta.dynamic_updates.push(callbackName);
                }
            }
            // Also register canonical on_update to mark lifecycle presence for export-time flags
            if (!events.exists("on_update")) {
                events.set("on_update", []);
            }
        } else if (lifecycles.update != null) {
            // Fallback for inline/anonymous update functions
            extractEvents("on_update", lifecycles.update);
        }

        // Handle multiple fixed update functions - each gets its own event
        var fixedUpdateCallbackNames = [for (k in lifecycles.fixed_updates.keys()) k];
        if (fixedUpdateCallbackNames.length > 0) {
            // We have named fixed update functions - generate separate events for each
            for (callbackName in fixedUpdateCallbackNames) {
                var body = lifecycles.fixed_updates.get(callbackName);
                if (body != null) {
                    // Use format: on_fixed_update_functionName
                    extractEvents("on_fixed_update_" + callbackName, body);
                    meta.dynamic_fixed_updates.push(callbackName);
                }
            }
            // Also register canonical on_fixed_update to mark lifecycle presence for export-time flags
            if (!events.exists("on_fixed_update")) {
                events.set("on_fixed_update", []);
            }
        } else if (lifecycles.fixed_update != null) {
            // Fallback for inline/anonymous fixed update functions
            extractEvents("on_fixed_update", lifecycles.fixed_update);
        }
        if (lifecycles.late_update != null) {
            extractEvents("on_late_update", lifecycles.late_update);
        }
        if (lifecycles.remove != null) {
            extractEvents("on_remove", lifecycles.remove);
        }
        if (lifecycles.render2d != null) {
            extractEvents("on_render2d", lifecycles.render2d);
        }

        // Auto-add per-callback _update_<name>_enabled members if trait has multiple update functions
        // Each update function gets its own enabled flag for independent enable/disable
        if (meta.dynamic_updates.length > 0) {
            for (callbackName in meta.dynamic_updates) {
                var flagName = "_update_" + callbackName + "_enabled";
                var isStaticallyRegistered = staticLifecycles.static_updates.exists(callbackName);
                members.set(flagName, {
                    haxeType: "Bool",
                    ctype: "bool",
                    defaultValue: { type: "bool", value: isStaticallyRegistered }
                });
                memberNames.push(flagName);
            }
        } else if (meta.has_remove_update) {
            // Fallback: single _update_enabled for anonymous/inline update functions
            var hasStaticUpdate = staticLifecycles.update != null;
            members.set("_update_enabled", {
                haxeType: "Bool",
                ctype: "bool",
                defaultValue: { type: "bool", value: hasStaticUpdate }
            });
            memberNames.push("_update_enabled");
        }

        // Auto-add _late_update_enabled member if trait uses removeLateUpdate()
        // Initial value: true if trait has STATIC late_update function, false if only dynamic registration
        if (meta.has_remove_late_update) {
            var hasStaticLateUpdate = staticLifecycles.late_update != null;
            members.set("_late_update_enabled", {
                haxeType: "Bool",
                ctype: "bool",
                defaultValue: { type: "bool", value: hasStaticLateUpdate }
            });
            memberNames.push("_late_update_enabled");
        }

        // Auto-add per-callback _fixed_update_<name>_enabled members if trait has multiple fixed update functions
        // Each fixed update function gets its own enabled flag for independent enable/disable
        if (meta.dynamic_fixed_updates.length > 0) {
            for (callbackName in meta.dynamic_fixed_updates) {
                var flagName = "_fixed_update_" + callbackName + "_enabled";
                var isStaticallyRegistered = staticLifecycles.static_fixed_updates.exists(callbackName);
                members.set(flagName, {
                    haxeType: "Bool",
                    ctype: "bool",
                    defaultValue: { type: "bool", value: isStaticallyRegistered }
                });
                memberNames.push(flagName);
            }
        } else if (meta.has_remove_fixed_update) {
            // Fallback: single _fixed_update_enabled for anonymous/inline fixed update functions
            var hasStaticFixedUpdate = staticLifecycles.fixed_update != null;
            members.set("_fixed_update_enabled", {
                haxeType: "Bool",
                ctype: "bool",
                defaultValue: { type: "bool", value: hasStaticFixedUpdate }
            });
            memberNames.push("_fixed_update_enabled");
        }

        // Auto-add _render2d_enabled member if trait uses removeRender2D() or notifyOnRender2D() at runtime
        // Initial value: true if trait has STATIC render2d function, false if only dynamic registration
        if (meta.has_remove_render2d) {
            var hasStaticRender2d = staticLifecycles.render2d != null;
            members.set("_render2d_enabled", {
                haxeType: "Bool",
                ctype: "bool",
                defaultValue: { type: "bool", value: hasStaticRender2d }
            });
            memberNames.push("_render2d_enabled");
        }

        // Pass 4: Extract ALL non-lifecycle methods as callable C functions
        extractMethods();

        return {
            name: traitKey,
            module: modulePath,
            cName: cName,
            needsData: memberNames.length > 0,
            parent: parentName,  // Parent trait name for inheritance (null if none)
            members: members,
            methods: methodIRMap,  // All callable methods (non-lifecycle)
            events: events,
            meta: meta
        };
    }

    /**
     * Extract ALL non-lifecycle methods as callable C functions.
     * Each method becomes a MethodIR with its parameters and body converted to IR.
     */
    function extractMethods():Void {
        for (methodName in methodMap.keys()) {
            // Skip lifecycle methods - they're handled as events
            if (Lambda.has(lifecycleMethodNames, methodName)) continue;

            // Skip internal/inherited Haxe methods
            if (methodName.charAt(0) == '_' && methodName != "new") continue;

            var method = methodMap.get(methodName);
            if (method == null || method.expr == null) continue;

            // Extract parameter info and add to localVarTypes for capture detection
            beginScope();
            var params:Array<MethodParamIR> = [];
            if (method.args != null) {
                for (arg in method.args) {
                    var haxeType = arg.type != null ? N64MacroBase.complexTypeToString(arg.type) : "Dynamic";
                    params.push({
                        name: arg.name,
                        haxeType: haxeType,
                        ctype: TypeMap.getCType(haxeType)
                    });
                    // Track parameter as local variable for capture detection
                    localVarTypes.set(arg.name, haxeType);
                }
            }

            // Determine return type
            var returnType = "void";
            if (method.ret != null) {
                var retHaxeType = N64MacroBase.complexTypeToString(method.ret);
                if (retHaxeType != "Void") returnType = TypeMap.getCType(retHaxeType);
            }

            // Convert method body to IR
            // If this is a setter method (set_X), mark it so assignments to X inside
            // the body do direct writes instead of recursively calling the setter
            var prevSetterTarget = currentSetterTarget;
            if (methodName.length > 4 && methodName.substr(0, 4) == "set_") {
                var memberName = methodName.substr(4);
                if (members.exists(memberName) && members.get(memberName).setter != null) {
                    currentSetterTarget = memberName;
                }
            }
            var bodyNodes = statements(method.expr);
            currentSetterTarget = prevSetterTarget;

            methodIRMap.set(methodName, {
                name: methodName,
                cName: methodCName(methodName),
                params: params,
                returnType: returnType,
                body: bodyNodes
            });

            // Clear method-specific local variables (parameters) after processing
            if (method.args != null) {
                for (arg in method.args) {
                    localVarTypes.remove(arg.name);
                }
            }
        }
    }

    function findLifecycles():{init:Expr, add:Expr, update:Expr, fixed_update:Expr, late_update:Expr, remove:Expr, render2d:Expr, updates:Map<String, Expr>, fixed_updates:Map<String, Expr>} {
        var result = {init: null, add: null, update: null, fixed_update: null, late_update: null, remove: null, render2d: null, updates: new Map<String, Expr>(), fixed_updates: new Map<String, Expr>()};

        // Scan ALL methods for lifecycle registrations (for code extraction)
        for (methodName in methodMap.keys()) {
            var method = methodMap.get(methodName);
            if (method != null && method.expr != null) {
                scanForLifecycles(method.expr, result, true);
            }
        }

        return result;
    }

    // Find only STATICALLY registered lifecycles (called from constructor/init path)
    // Used to determine initial enabled state for dynamic toggle flags
    function findStaticLifecycles():{init:Expr, add:Expr, update:Expr, fixed_update:Expr, late_update:Expr, remove:Expr, render2d:Expr, static_updates:Map<String, Bool>, static_fixed_updates:Map<String, Bool>} {
        var result = {init: null, add: null, update: null, fixed_update: null, late_update: null, remove: null, render2d: null, static_updates: new Map<String, Bool>(), static_fixed_updates: new Map<String, Bool>()};

        // Only scan constructor - lifecycle registrations elsewhere are "dynamic"
        var ctor = methodMap.get("new");
        if (ctor != null && ctor.expr != null) {
            scanForStaticLifecycles(ctor.expr, result, true);
        }

        return result;
    }

    // Scan for lifecycle registrations (finds all, for code extraction)
    function scanForLifecycles(e:Expr, result:{init:Expr, add:Expr, update:Expr, fixed_update:Expr, late_update:Expr, remove:Expr, render2d:Expr, updates:Map<String, Expr>, fixed_updates:Map<String, Expr>}, inInitPath:Bool):Void {
        if (e == null) return;

        switch (e.expr) {
            case ECall(callExpr, params):
                var funcName = getFuncName(callExpr);
                if (params.length > 0) {
                    var body = resolveCallback(params[0]);
                    switch (funcName) {
                        case "notifyOnInit": result.init = body;
                        case "notifyOnAdd": result.add = body;
                        case "notifyOnUpdate":
                            // Track update callbacks by their function name
                            var callbackName = getCallbackName(params[0]);
                            if (callbackName != null && body != null) {
                                result.updates.set(callbackName, body);
                            }
                            // Also keep backward compatibility with single update field
                            result.update = body;
                        case "notifyOnFixedUpdate":
                            // Track fixed update callbacks by their function name
                            var callbackName = getCallbackName(params[0]);
                            if (callbackName != null && body != null) {
                                result.fixed_updates.set(callbackName, body);
                            }
                            // Also keep backward compatibility with single fixed_update field
                            result.fixed_update = body;
                        case "notifyOnLateUpdate": result.late_update = body;
                        case "notifyOnRemove": result.remove = body;
                        case "notifyOnRender2D": result.render2d = body;
                        case "notifyOnRender":
                            N64MacroBase.unsupported("notifyOnRender() (3D drawing) is not supported on N64 yet", e.pos);
                        default:
                    }
                }
                // Scan parameters AND the callExpr (for chained calls like tween.float(...).start())
                scanForLifecycles(callExpr, result, inInitPath);
                for (p in params) scanForLifecycles(p, result, inInitPath);
            case EBlock(exprs):
                for (expr in exprs) scanForLifecycles(expr, result, inInitPath);
            case EFunction(_, f):
                // Preserve inInitPath - if we're scanning a function passed to notifyOnInit,
                // the body is still part of the init path. The path only breaks when
                // scanning params of non-init callbacks (handled in ECall case above).
                if (f.expr != null) scanForLifecycles(f.expr, result, inInitPath);
            default:
                e.iter(function(sub) scanForLifecycles(sub, result, inInitPath));
        }
    }

    // Scan for STATIC lifecycle registrations only (for initial enabled state)
    function scanForStaticLifecycles(e:Expr, result:{init:Expr, add:Expr, update:Expr, fixed_update:Expr, late_update:Expr, remove:Expr, render2d:Expr, static_updates:Map<String, Bool>, static_fixed_updates:Map<String, Bool>}, inInitPath:Bool):Void {
        if (e == null) return;

        switch (e.expr) {
            case ECall(callExpr, params):
                var funcName = getFuncName(callExpr);
                if (params.length > 0) {
                    var body = resolveCallback(params[0]);
                    // Only record lifecycle registrations if we're in the init path
                    if (inInitPath) {
                        switch (funcName) {
                            case "notifyOnInit": result.init = body;
                            case "notifyOnAdd": result.add = body;
                            case "notifyOnUpdate":
                                // Track static update callbacks by name
                                var callbackName = getCallbackName(params[0]);
                                if (callbackName != null) {
                                    result.static_updates.set(callbackName, true);
                                }
                                result.update = body;
                            case "notifyOnFixedUpdate":
                                // Track static fixed update callbacks by name
                                var callbackName = getCallbackName(params[0]);
                                if (callbackName != null) {
                                    result.static_fixed_updates.set(callbackName, true);
                                }
                                result.fixed_update = body;
                            case "notifyOnLateUpdate": result.late_update = body;
                            case "notifyOnRemove": result.remove = body;
                            case "notifyOnRender2D": result.render2d = body;
                            default:
                        }
                    }
                    // Determine if we should continue in init path
                    var isInitCallback = funcName == "notifyOnInit" || funcName == "notifyOnAdd";
                    for (p in params) scanForStaticLifecycles(p, result, inInitPath && isInitCallback);
                }
                // Scan the callExpr for chained calls - stay in init path
                scanForStaticLifecycles(callExpr, result, inInitPath);
            case EBlock(exprs):
                for (expr in exprs) scanForStaticLifecycles(expr, result, inInitPath);
            case EFunction(_, f):
                // Preserve inInitPath through function bodies
                if (f.expr != null) scanForStaticLifecycles(f.expr, result, inInitPath);
            default:
                e.iter(function(sub) scanForStaticLifecycles(sub, result, inInitPath));
        }
    }

    function getFuncName(e:Expr):String {
        return switch (e.expr) {
            case EConst(CIdent(s)): s;
            case EField(_, field): field;
            default: null;
        };
    }

    function resolveCallback(e:Expr):Expr {
        return switch (e.expr) {
            case EFunction(_, f): f.expr;
            case EField(_, methodName):
                var method = methodMap.get(methodName);
                method != null ? method.expr : null;
            case EConst(CIdent(methodName)):
                var method = methodMap.get(methodName);
                method != null ? method.expr : null;
            default: null;
        };
    }

    // Get the name of the callback function (for dynamic update tracking)
    function getCallbackName(e:Expr):String {
        return switch (e.expr) {
            case EFunction(_, _): null; // Inline function - no name
            case EField(_, methodName): methodName;
            case EConst(CIdent(methodName)): methodName;
            default: null;
        };
    }

    // ========================================================================
    // Lifecycle bodies
    // ========================================================================

    // A lifecycle body's statements, in order, as the event's IR. Input checks
    // stay inline, where the game wrote them (as in Armory)
    function extractEvents(baseEventName:String, body:Expr):Void {
        beginScope();
        var eventNodes:Array<IRNode> = [];

        switch (body.expr) {
            case EBlock(exprs):
                for (expr in exprs) {
                    processStatement(expr, eventNodes);
                }
            default:
                processStatement(body, eventNodes);
        }

        if (eventNodes.length > 0) {
            if (!events.exists(baseEventName)) events.set(baseEventName, []);
            for (stmt in eventNodes) {
                events.get(baseEventName).push(stmt);
            }
        }
    }

    function processStatement(e:Expr, eventNodes:Array<IRNode>):Void {
        if (e == null) return;

        switch (e.expr) {
            case EIf(econd, eif, eelse):
                eventNodes.push(convertIf(econd, eif, eelse));

            default:
                var node = exprToIR(e);
                if (N64MacroBase.keepStatement(node)) {
                    eventNodes.push(node);
                }
        }
    }

    // Check if expression is 'super' keyword
    function isSuperExpr(e:Expr):Bool {
        return switch (e.expr) {
            case EConst(CIdent("super")): true;
            default: false;
        };
    }

    override public function getEvents():Map<String, Array<IRNode>> {
        return events;
    }

    // ========================================================================
    // AST → IR hooks (see N64Extractor)
    // ========================================================================

    override function resolveIdent(name:String, e:Expr):IRNode {
        if (name == "this" || name == "object") {
            meta.uses_transform = true;
            return { type: "ident", value: "object" };
        }
        // Locals (and parameters) shadow members
        if (localVarTypes.exists(name)) {
            return localIdent(name);
        }
        if (memberNames.indexOf(name) >= 0) {
            return { type: "member", value: name };
        }
        // Check if this is a method reference (function pointer)
        // e.g., fadeOut(initTransitionDone) -> fadeOut(arm_gamescene_inittransitiondone)
        if (methodMap.exists(name)) {
            return {
                type: "method_ref",
                method: name,
                cName: methodCName(name),
                trait: traitKey
            };
        }
        // Engine-provided values and classes (gamepad, RigidBody, ...) and
        // inherited Trait fields (name) have no data on N64
        if (EngineTypes.isProvidedMember(cls, name) || EngineTypes.isProvided(TypedIndex.typeRefPath(e))) {
            return { type: "skip" };
        }
        if (TypedIndex.fieldOwner(e) == "iron.Trait") {
            return N64MacroBase.unsupported('Trait.$name is not supported on N64 yet');
        }
        // A field an ancestor trait declares
        var inherited = inheritedField(e);
        if (inherited != null) {
            if (!inherited.isMethod) return { type: "member", value: name, owner: inherited.owner };
            return {
                type: "method_ref",
                method: name,
                cName: inherited.cName + "_" + name.toLowerCase(),
                trait: inherited.owner
            };
        }
        return { type: "ident", value: name };
    }

    /**
     * The ancestor trait declaring the instance field `e` reads (from the
     * typed AST): its path and C name, and whether the field is a method.
     * Null for this trait's own fields and anything else.
     */
    function inheritedField(e:Expr):{owner:String, cName:String, isMethod:Bool} {
        var ref = TypedIndex.fieldRef(e);
        if (ref == null || ref.isStatic || ref.owner == traitKey) return null;
        var owner = N64MacroBase.extractedClass(ref.owner);
        if (owner == null || !N64Extractor.isTrait(owner)) return null;
        var field = Lambda.find(owner.fields.get(), f -> f.name == ref.name);
        if (field == null) return null;
        var isMethod = switch (field.kind) {
            case FMethod(_): true;
            default: false;
        };
        return { owner: ref.owner, cName: N64Extractor.traitCName(owner), isMethod: isMethod };
    }

    // this.x: own members, then the ones an ancestor declares (looked up from
    // the `this`: the access itself may be typed at a wider position)
    override function thisField(thisExpr:Expr, name:String):IRNode {
        var member = super.thisField(thisExpr, name);
        if (member != null) return member;
        var inherited = inheritedField(thisExpr);
        return inherited != null && !inherited.isMethod ? { type: "member", value: name, owner: inherited.owner } : null;
    }

    /**
     * A call to a method of this trait or an ancestor (callExpr is `name` or
     * `this.name`): the C function of the class declaring it. Python calls it
     * through the vtable when a descendant overrides it.
     */
    function traitMethodCall(callExpr:Expr, name:String, params:Array<Expr>):IRNode {
        var cName:String = null, owner:String = null;
        var own = methodMap.get(name);
        if (own != null && own.expr != null) {
            cName = methodCName(name);
            owner = traitKey;
        } else {
            var inherited = inheritedField(callExpr);
            if (inherited == null || !inherited.isMethod) return null;
            cName = inherited.cName + "_" + name.toLowerCase();
            owner = inherited.owner;
        }
        return {
            type: "trait_method_call",
            cName: cName,
            method: name,
            trait: owner,
            // Function arguments become callback wrappers
            args: extractCallbackArgs(params, name)
        };
    }

    // C function of one of this trait's methods: <trait C name>_<method>
    function methodCName(name:String):String {
        return cName + "_" + name.toLowerCase();
    }

    override function convertAssign(e1:Expr, e2:Expr):IRNode {
        // Check if assigning to transform.loc/rot/scale (mutating transform)
        checkTransformMutation(e1);

        // transform.scale = value -> it_set_scale()
        if (isTransformScaleAssign(e1)) {
            // Swizzle: Blender (x, y, z) → N64 (x, z, y) and multiply by SCALE_FACTOR
            var sf = Constants.SCALE_FACTOR_C;
            return {
                type: "c_template",
                c_code: 'it_set_scale(&((ArmObject*)obj)->transform, ({0}).x * $sf, ({0}).z * $sf, ({0}).y * $sf);',
                args: [exprToIR(e2)]
            };
        }
        // g2.color = value -> render2d_set_color
        if (isGraphics2ColorAssign(e1)) {
            return { type: "render2d_set_color", args: [exprToIR(e2)] };
        }
        // Other g2 property assignments (imageScaleQuality, etc.)
        if (isGraphics2PropertyAssign(e1)) {
            return { type: "skip" };
        }
        // AutoloadClass.member = this where member is a trait type
        // e.g., MainInstances.player = this -> maininstances_player = data
        if (isAutoloadTraitAssign(e1, e2)) {
            var autoloadInfo = getAutoloadFieldInfo(e1);
            useAutoload(autoloadInfo.c_name);
            // The stored pointer is this trait's data, which must exist
            meta.stores_self = true;
            return {
                type: "autoload_trait_assign",
                props: {
                    autoload: autoloadInfo.c_name,
                    member: autoloadInfo.field
                }
            };
        }

        var targetIR = exprToIR(e1);
        // A member with a setter: call it, except inside that setter's own
        // body (to avoid infinite recursion)
        if (targetIR != null && targetIR.type == "member" && targetIR.value != currentSetterTarget) {
            var member = members.get(targetIR.value);
            if (member != null && member.setter != null) {
                return {
                    type: "trait_method_call",
                    cName: cName + "_" + member.setter,
                    method: member.setter,
                    trait: traitKey,
                    args: [exprToIR(e2)]
                };
            }
        }
        // `this` stored in a trait-typed member: the trait's data
        if (targetIR != null && targetIR.type == "member" && isThis(e2)) {
            var member = members.get(targetIR.value);
            if (member != null && member.ctype.endsWith("Data*")) {
                meta.stores_self = true;
                return { type: "assign", children: [targetIR, { type: "c_literal", c_code: "data" }] };
            }
        }
        return { type: "assign", children: [targetIR, exprToIR(e2)] };
    }

    function isThis(e:Expr):Bool {
        return switch (e.expr) {
            case EConst(CIdent("this")): true;
            default: false;
        };
    }

    override function convertSpecialMethodCall(callExpr:Expr, obj:Expr, method:String, params:Array<Expr>, args:Array<IRNode>):IRNode {
        // super.method(): the implementation of the ancestor declaring it
        if (isSuperExpr(obj)) {
            // No parent - skip (shouldn't happen in valid Haxe code)
            if (parentName == null) return { type: "skip" };
            var ref = TypedIndex.fieldRef(callExpr);
            var owner = ref != null ? N64MacroBase.extractedClass(ref.owner) : null;
            if (owner == null) return N64MacroBase.unsupported("super." + method + "() of a class that is not exported is not supported on N64");
            return {
                type: "super_call",
                value: parentName,    // Parent trait name
                method: method,
                cName: N64Extractor.traitCName(owner) + "_" + method.toLowerCase(),
                args: args
            };
        }

        // this.method(): same as method()
        if (isThis(obj)) {
            var call = traitMethodCall(callExpr, method, params);
            if (call != null) return call;
        }
        return null;
    }

    override function convertFunctionCall(callExpr:Expr, funcName:String, params:Array<Expr>, args:Array<IRNode>):IRNode {
        // super() - call parent constructor (on_ready)
        if (funcName == "super") {
            // No parent - skip (shouldn't happen in valid Haxe code)
            if (parentName == null) return { type: "skip" };
            return {
                type: "super_call",
                value: parentName,    // Parent trait name
                method: "new",        // Constructor - maps to on_ready
                args: args
            };
        }

        // Skip lifecycle registration calls - they're handled by scanForLifecycles
        // Note: notifyOnRender2D is now handled separately, not skipped
        if (funcName == "notifyOnInit" ||
            funcName == "notifyOnFixedUpdate" || funcName == "notifyOnLateUpdate" ||
            funcName == "notifyOnRemove" || funcName == "notifyOnAdd") {
            return { type: "skip" };
        }
        if (funcName == "notifyOnRender") {
            return N64MacroBase.unsupported("notifyOnRender() (3D drawing) is not supported on N64 yet");
        }

        // removeFixedUpdate(callback) -> set _fixed_update_enabled = false
        if (funcName == "removeFixedUpdate") {
            meta.has_remove_fixed_update = true;
            var callbackName:String = null;
            if (params.length > 0) {
                callbackName = extractStringArg(params[0]);
            }
            return { type: "remove_fixed_update", value: callbackName };
        }

        // removeUpdate(callback) -> set _update_enabled = false
        // On N64, we only have one update per trait, so we just disable it
        // The callback parameter is for Haxe compatibility
        if (funcName == "removeUpdate") {
            meta.has_remove_update = true;
            // Extract callback name for documentation/debugging (optional)
            var callbackName:String = null;
            if (params.length > 0) {
                callbackName = extractStringArg(params[0]);
            }
            return { type: "remove_update", value: callbackName };
        }

        // removeLateUpdate(callback) -> set _late_update_enabled = false
        if (funcName == "removeLateUpdate") {
            meta.has_remove_late_update = true;
            var callbackName:String = null;
            if (params.length > 0) {
                callbackName = extractStringArg(params[0]);
            }
            return { type: "remove_late_update", value: callbackName };
        }

        // removeRender2D(callback) -> set _render2d_enabled = false
        if (funcName == "removeRender2D") {
            meta.has_remove_render2d = true;
            var callbackName:String = null;
            if (params.length > 0) {
                callbackName = extractStringArg(params[0]);
            }
            return { type: "remove_render2d", value: callbackName };
        }

        // notifyOnUpdate(callback) outside constructor -> re-enable updates
        // In constructor it's handled by scanForLifecycles, but runtime calls enable updates
        if (funcName == "notifyOnUpdate") {
            // Always enable the toggle mechanism when runtime notifyOnUpdate is used
            meta.has_remove_update = true;
            var callbackName:String = null;
            if (params.length > 0) {
                callbackName = extractStringArg(params[0]);
            }
            return { type: "notify_update", value: callbackName };
        }

        // notifyOnRender2D(callback) outside init -> re-enable render2D
        if (funcName == "notifyOnRender2D") {
            // Always enable the toggle mechanism when runtime notifyOnRender2D is used
            meta.has_remove_render2d = true;
            var callbackName:String = null;
            if (params.length > 0) {
                callbackName = extractStringArg(params[0]);
            }
            return { type: "notify_render2d", value: callbackName };
        }

        // A method of this trait or an ancestor
        var call = traitMethodCall(callExpr, funcName, params);
        if (call != null) return call;

        // Check if this is a call to a function-type parameter (callback invocation)
        // e.g., finishedCallback() where finishedCallback: Void->Void
        var localType = localVarTypes.get(funcName);
        if (localType != null && (localType.indexOf("->") >= 0 || localType == "Void->Void")) {
            // This is a callback parameter being invoked
            return {
                type: "callback_param_call",
                name: funcName,
                paramType: localType,
                args: args
            };
        }

        return super.convertFunctionCall(callExpr, funcName, params, args);
    }

    // Check if expression is an assignment to transform properties (loc, rot, scale, dirty)
    function checkTransformMutation(expr:Expr):Void {
        switch (expr.expr) {
            case EField(obj, field):
                switch (obj.expr) {
                    // transform.loc = ..., transform.rot = ..., transform.scale = ..., transform.dirty = ...
                    case EField(_, "transform"), EConst(CIdent("transform")):
                        if (field == "loc" || field == "rot" || field == "scale" || field == "dirty") {
                            meta.mutates_transform = true;
                        }
                    // transform.loc.x = ... (nested field access)
                    case EField(innerObj, innerField):
                        if (innerField == "loc" || innerField == "rot" || innerField == "scale") {
                            switch (innerObj.expr) {
                                case EField(_, "transform"), EConst(CIdent("transform")):
                                    meta.mutates_transform = true;
                                default:
                            }
                        }
                    default:
                }
            default:
        }
    }

    // Check if expression is assignment to transform.scale (needs special handling)
    function isTransformScaleAssign(expr:Expr):Bool {
        switch (expr.expr) {
            case EField(obj, "scale"):
                switch (obj.expr) {
                    case EField(_, "transform"), EConst(CIdent("transform")):
                        return true;
                    default:
                }
            default:
        }
        return false;
    }

    // Check if expression is assignment to g2.color (Graphics2 color property)
    function isGraphics2ColorAssign(expr:Expr):Bool {
        switch (expr.expr) {
            case EField(obj, "color"):
                switch (obj.expr) {
                    case EConst(CIdent(name)):
                        // Check for common graphics2 parameter names or type
                        if (name == "g" || name == "g2") return true;
                        var objType = getExprType(obj);
                        if (objType == "kha.graphics2.Graphics" || objType == "Graphics") return true;
                    default:
                }
            default:
        }
        return false;
    }

    // Check if expression is assignment to any g2 property (for skipping unsupported ones)
    function isGraphics2PropertyAssign(expr:Expr):Bool {
        switch (expr.expr) {
            case EField(obj, _):
                switch (obj.expr) {
                    case EConst(CIdent(name)):
                        if (name == "g" || name == "g2") return true;
                        var objType = getExprType(obj);
                        if (objType == "kha.graphics2.Graphics" || objType == "Graphics") return true;
                    default:
                }
            default:
        }
        return false;
    }

    // Check if this is an assignment to an autoload trait member with `this` on the right side
    // e.g., MainInstances.player = this
    function isAutoloadTraitAssign(lhs:Expr, rhs:Expr):Bool {
        // Check if rhs is `this`
        switch (rhs.expr) {
            case EConst(CIdent("this")):
                // Check if lhs is AutoloadClass.field
                switch (lhs.expr) {
                    case EField(obj, _):
                        switch (obj.expr) {
                            case EConst(CIdent(_)):
                                return AutoloadCallConverter.getAutoloadCName(obj) != null;
                            default:
                        }
                    default:
                }
            default:
        }
        return false;
    }

    // Get autoload field info from expression (for autoload trait assignments)
    function getAutoloadFieldInfo(expr:Expr):{c_name:String, field:String} {
        switch (expr.expr) {
            case EField(obj, field):
                switch (obj.expr) {
                    case EConst(CIdent(_)):
                        var cName = AutoloadCallConverter.getAutoloadCName(obj);
                        if (cName != null) {
                            return { c_name: cName, field: field };
                        }
                    default:
                }
            default:
        }
        return { c_name: "", field: "" };
    }

    /**
     * Counter for generating unique callback names for inherited method callbacks
     */
    var inheritedCallbackCounter:Int = 0;

    /**
     * Extract callback arguments from raw params for inherited method calls.
     * Function arguments are converted to callback wrapper structures.
     * Non-function arguments are converted via exprToIR as usual.
     */
    function extractCallbackArgs(params:Array<Expr>, methodName:String):Array<IRNode> {
        var result:Array<IRNode> = [];

        for (i in 0...params.length) {
            var param = params[i];
            switch (param.expr) {
                case EFunction(_, func):
                    // Anonymous function - extract as callback wrapper
                    var callback = extractInheritedCallback(func, methodName, i);
                    if (callback != null) {
                        result.push(callback);
                    } else {
                        result.push(N64MacroBase.unsupported('callback passed to $methodName() could not be converted', param.pos));
                    }
                default:
                    // Non-function arg - convert normally
                    result.push(exprToIR(param));
            }
        }

        return result;
    }

    /**
     * Extract an anonymous function as a callback for an inherited method.
     * Returns a callback_wrapper IR node that will be emitted as a C function.
     */
    function extractInheritedCallback(func:Function, methodName:String, argIndex:Int):IRNode {
        if (func == null || func.expr == null) return null;

        // Generate unique callback name
        var callbackName = '${cName}_${methodName}_cb_${inheritedCallbackCounter++}';

        // Get parameter name if any (for callbacks like Float->Void)
        var paramName:String = null;
        var paramType:String = "Void";
        if (func.args != null && func.args.length > 0) {
            paramName = func.args[0].name;
            paramType = func.args[0].type != null ? N64MacroBase.complexTypeToString(func.args[0].type) : "Dynamic";
            // Track the callback parameter as a local variable during body extraction
            localVarTypes.set(paramName, paramType);
        }

        // Convert function body to IR
        var bodyNodes = statements(func.expr);

        // Clean up local variable tracking
        if (paramName != null) {
            localVarTypes.remove(paramName);
        }

        // Analyze captures - variables from outer scope that need to be passed via data
        var captures = analyzeCallbackCaptures(bodyNodes, paramName);

        // Return callback wrapper with all required fields
        // Type as IRNode so serializeIRNode can access all fields
        var cbWrapper:IRNode = {
            type: "callback_wrapper",
            callback_name: callbackName,
            param_name: paramName,
            param_type: paramType,
            param_ctype: TypeMap.getCType(paramType),
            body: bodyNodes,
            captures: captures
        };
        return cbWrapper;
    }

    /**
     * Analyze IR nodes to find captured variables (members and params from outer scope).
     */
    function analyzeCallbackCaptures(nodes:Array<IRNode>, excludeParam:String):Array<Dynamic> {
        var captures:Map<String, Dynamic> = new Map();
        for (node in nodes) {
            findCallbackCaptures(node, excludeParam, captures);
        }
        return Lambda.array(captures);
    }

    function findCallbackCaptures(node:IRNode, excludeParam:String, captures:Map<String, Dynamic>):Void {
        if (node == null) return;

        switch (node.type) {
            case "ident":
                var name = Std.string(node.value);
                if (name != excludeParam && name != "null" && name != "true" && name != "false" && name != "object" && name != "time_delta") {
                    // First check if it's a class member
                    var memberType = memberTypes.get(name);
                    if (memberType != null && !captures.exists(name)) {
                        captures.set(name, {
                            name: name,
                            type: memberType,
                            ctype: TypeMap.getCType(memberType),
                            is_member: true
                        });
                    } else if (!captures.exists(name)) {
                        // Check if it's a local variable (including method parameters)
                        var localType = localVarTypes.get(name);
                        if (localType != null) {
                            captures.set(name, {
                                name: name,
                                type: localType,
                                ctype: TypeMap.getCType(localType),
                                is_member: false,
                                is_param: true
                            });
                        }
                    }
                }
            case "member" if (node.owner == null):
                var name = Std.string(node.value);
                if (!captures.exists(name)) {
                    var memberType = memberTypes.get(name);
                    captures.set(name, {
                        name: name,
                        type: memberType != null ? memberType : "Dynamic",
                        ctype: TypeMap.getCType(memberType != null ? memberType : "Dynamic"),
                        is_member: true
                    });
                }
            default:
                // Recurse into children and args
                if (node.children != null) {
                    for (child in node.children) {
                        findCallbackCaptures(child, excludeParam, captures);
                    }
                }
                if (node.args != null) {
                    for (arg in node.args) {
                        findCallbackCaptures(arg, excludeParam, captures);
                    }
                }
        }
    }
}

#end
