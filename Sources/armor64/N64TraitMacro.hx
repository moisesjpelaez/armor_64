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

            var membersArr = N64MacroBase.serializeMembers(ir.members);

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

    // Lifecycle registrations (registerLifecycle), in the order found; each is
    // a counter in the trait's data, see trait_generator.py
    var lifecycleSites:Array<Dynamic> = [];
    // notifyOnInit calls that run before the trait is ready (scanStaticInits)
    var staticInits:Map<String, Bool> = new Map();
    var siteIds:Map<String, Int> = new Map();
    var siteRegistrations:Map<String, IRNode> = new Map();

    // Iron's Trait.notifyOnX / removeX -> the kind of lifecycle function
    static var REGISTER_KINDS = ["notifyOnInit" => "init", "notifyOnAdd" => "add", "notifyOnRemove" => "remove",
        "notifyOnUpdate" => "update", "notifyOnFixedUpdate" => "fixed_update", "notifyOnLateUpdate" => "late_update",
        "notifyOnRender2D" => "render2d"];
    static var REMOVE_KINDS = ["removeUpdate" => "update", "removeFixedUpdate" => "fixed_update",
        "removeLateUpdate" => "late_update", "removeRender2D" => "render2d"];
    // The runtime handler calling the functions of a kind
    static var LIFECYCLE_EVENTS = ["init" => "on_ready", "add" => "on_add", "remove" => "on_remove", "update" => "on_update",
        "fixed_update" => "on_fixed_update", "late_update" => "on_late_update", "render2d" => "on_render2d"];

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
                    collectVar(field.name, t, e, field.pos, isStatic(field));
                case FProp(get, set, t, e):
                    // Property with a backing field (e.g., @:isVar var paused(default, set): Bool)
                    if (N64Extractor.hasStorage(field, get, set)) collectVar(field.name, t, e, field.pos, isStatic(field));
                case FFun(func):
                    methodMap.set(field.name, func);
                default:
            }
        }

        // The constructor runs first when the trait is added (on_add, then
        // the add functions); every notifyOn* call in it, in methods and in
        // closures is a registration (registerLifecycle)
        var ctor = methodMap.get("new");
        if (ctor != null && ctor.expr != null) {
            scanStaticInits(ctor.expr, true);
            beginScope();
            events.set("on_create", statements(ctor.expr));
        }

        // Pass 4: Extract ALL non-lifecycle methods as callable C functions
        extractMethods();

        // The runtime calls the trait's handler of each kind registered
        for (site in lifecycleSites) {
            var event = LIFECYCLE_EVENTS.get(site.kind);
            if (!events.exists(event)) events.set(event, []);
            // An init registered once the trait is ready runs before its next update
            if (site.kind == "init" && site.late_init && !events.exists("on_update")) events.set("on_update", []);
        }
        meta.lifecycle_sites = lifecycleSites;

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
            // The constructor is on_create
            if (methodName == "new") continue;

            // Skip internal/inherited Haxe methods
            if (methodName.charAt(0) == '_' && methodName != "new") continue;

            var method = methodMap.get(methodName);
            if (method == null || method.expr == null) continue;

            // Extract parameter info and add to localVarTypes for capture detection
            beginScope();
            var params:Array<MethodParamIR> = [];
            if (method.args != null) {
                for (arg in method.args) {
                    var haxeType = paramTypeName(methodName, params.length, arg.type);
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
            var retHaxeType = returnTypeName(methodName, method.ret);
            if (retHaxeType != "Void") returnType = TypeMap.getCType(retHaxeType);

            var bodyNodes = statements(method.expr);

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

    static function isStatic(field:Field):Bool {
        return field.access != null && field.access.indexOf(AStatic) >= 0;
    }

    function getFuncName(e:Expr):String {
        return switch (e.expr) {
            case EConst(CIdent(s)): s;
            case EField(_, field): field;
            default: null;
        };
    }

    /**
     * Records which notifyOnInit calls run before the trait is ready: in the
     * constructor, directly or in the add and init functions it registers
     * (and canvas.notifyOnReady, inlined). on_ready runs those; an init
     * registered anywhere else may come later (late_init).
     */
    function scanStaticInits(e:Expr, inPath:Bool):Void {
        if (e == null) return;
        switch (e.expr) {
            case ECall(callExpr, params):
                var name = getFuncName(callExpr);
                if (inPath && name == "notifyOnInit") staticInits.set(posKey(callExpr.pos), true);
                scanStaticInits(callExpr, inPath);
                var runsNow = name == "notifyOnInit" || name == "notifyOnAdd" || name == "notifyOnReady";
                for (p in params) switch (p.expr) {
                    case EFunction(_, f): scanStaticInits(f.expr, inPath && runsNow);
                    default: scanStaticInits(p, inPath);
                }
            case EFunction(_, f):
                scanStaticInits(f.expr, false);
            default:
                e.iter(sub -> scanStaticInits(sub, inPath));
        }
    }

    static function posKey(pos:Position):String {
        var infos = Context.getPosInfos(pos);
        return infos.file + ":" + infos.min;
    }

    /**
     * notifyOnX(f) on this trait: one more registration of its site (a
     * counter in the trait's data). f is an anonymous function (its body
     * becomes the site's function, with what it captures) or a method of
     * this trait or an ancestor (called).
     */
    function registerLifecycle(registerName:String, callExpr:Expr, fn:Expr):IRNode {
        var kind = REGISTER_KINDS.get(registerName);
        // A call converted again (an expression can be converted more than once) is the same site
        var key = posKey(callExpr.pos);
        if (siteRegistrations.exists(key)) return siteRegistrations.get(key);
        var methodCall:IRNode = null, method:String = null, func:Function = null;
        switch (fn.expr) {
            case EFunction(_, f):
                func = f;
            case EConst(CIdent(name)), EField({expr: EConst(CIdent("this"))}, name):
                methodCall = traitMethodCall(fn, name, []);
                if (methodCall == null) return N64MacroBase.unsupported('$registerName() of a function that isn\'t a method of this trait is not supported on N64 yet', fn.pos);
                // A 2D drawing method's Graphics parameter (unused on N64)
                if (kind == "render2d") methodCall.args = [{ type: "c_literal", c_code: "NULL" }];
                method = name;
            default:
                return N64MacroBase.unsupported('$registerName() of a function stored in a variable is not supported on N64 yet (pass a method or an anonymous function)', fn.pos);
        }
        // The site first: registrations inside its function get the next ids
        var id = lifecycleSites.length;
        siteIds.set(key, id);
        lifecycleSites.push({
            id: id,
            kind: kind,
            order: Context.getPosInfos(callExpr.pos).min,
            method: method,
            late_init: kind == "init" && !staticInits.exists(posKey(callExpr.pos))
        });
        var captures:Array<Dynamic> = func != null ? N64MacroBase.callbackCaptures(fn) : [];
        var node:IRNode = { type: "lifecycle_register", value: id, props: { captures: captures, where: N64MacroBase.whereOf(callExpr.pos) } };
        siteRegistrations.set(key, node);
        events.set('lc_$id', func != null ? statements(func.expr) : [methodCall]);
        return node;
    }

    /** removeX(f): one registration less, of the first site with method f (in the trait's chain). */
    function removeLifecycle(removeName:String, fn:Expr):IRNode {
        var name = switch (fn.expr) {
            case EConst(CIdent(name)), EField({expr: EConst(CIdent("this"))}, name): name;
            default: null;
        };
        if (name == null || traitMethodCall(fn, name, []) == null) {
            return N64MacroBase.unsupported('$removeName() of a function that isn\'t a method of this trait is not supported on N64 yet', fn.pos);
        }
        return { type: "lifecycle_remove", value: REMOVE_KINDS.get(removeName), props: { method: name } };
    }

    /** notifyOnX / removeX of iron.Trait (from the typed call), else null. */
    function lifecycleCall(callExpr:Expr, name:String, params:Array<Expr>):IRNode {
        if (!REGISTER_KINDS.exists(name) && !REMOVE_KINDS.exists(name) && name != "notifyOnRender") return null;
        if (methodMap.exists(name)) return null;  // The trait's own method of that name
        if (name == "notifyOnRender") return N64MacroBase.unsupported("notifyOnRender() (3D drawing) is not supported on N64 yet");
        if (params.length != 1) return null;
        return REGISTER_KINDS.exists(name) ? registerLifecycle(name, callExpr, params[0]) : removeLifecycle(name, params[0]);
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
        if (leftOutMembers.exists(name)) return leftOutMember(name);
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
                cName: N64Extractor.methodCNameOf(inherited.cName, name),
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
            cName = N64Extractor.methodCNameOf(inherited.cName, name);
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
        return N64Extractor.methodCNameOf(cName, name);
    }

    override function convertAssign(e1:Expr, e2:Expr):IRNode {

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
                cName: N64Extractor.methodCNameOf(N64Extractor.traitCName(owner), method),
                args: args
            };
        }

        // this.notifyOnUpdate(f): a registration; on another trait it's not supported
        if (REGISTER_KINDS.exists(method) || REMOVE_KINDS.exists(method)) {
            if (isThis(obj)) {
                var lifecycle = lifecycleCall(callExpr, method, params);
                if (lifecycle != null) return lifecycle;
            } else if (TypedIndex.callOwner(obj, method) == "iron.Trait") {
                return N64MacroBase.unsupported('$method() on another trait is not supported on N64 yet (call it in that trait)');
            }
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

        var lifecycle = lifecycleCall(callExpr, funcName, params);
        if (lifecycle != null) return lifecycle;

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

    // g2.color = v on a 2D graphics (kha.graphics2.Graphics, from the typed AST)
    function isGraphics2ColorAssign(expr:Expr):Bool {
        return switch (expr.expr) {
            case EField(obj, "color"): TypedIndex.isInstanceOf(obj, "kha.graphics2.Graphics");
            default: false;
        };
    }

    // Any other property of a 2D graphics (imageScaleQuality...): nothing on N64
    function isGraphics2PropertyAssign(expr:Expr):Bool {
        return switch (expr.expr) {
            case EField(obj, _): TypedIndex.isInstanceOf(obj, "kha.graphics2.Graphics");
            default: false;
        };
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
                    var callback = extractInheritedCallback(param, func, methodName, i);
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
    function extractInheritedCallback(fn:Expr, func:Function, methodName:String, argIndex:Int):IRNode {
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

        var captures = N64MacroBase.callbackCaptures(fn);

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
}

#end
