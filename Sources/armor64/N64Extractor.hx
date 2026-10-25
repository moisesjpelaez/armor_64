package armor64;

#if macro
import haxe.macro.Context;
import haxe.macro.Expr;
import haxe.macro.Type;

import armor64.IRTypes;
import armor64.N64MacroBase;
import armor64.TypedIndex;
import armor64.mapping.TypeMap;
import armor64.mapping.EngineTypes;
import armor64.converters.ICallConverter;
import armor64.converters.AudioCallConverter;
import armor64.converters.AutoloadCallConverter;
import armor64.converters.InputCallConverter;

using StringTools;

/**
 * Haxe → IR conversion shared by traits and autoloads, run after typing.
 *
 * Subclasses only add what really differs, through the hooks below:
 * - TraitExtractor: lifecycles and events, inheritance, the data struct
 *   (members are data->x), setters, callbacks
 * - AutoloadExtractor: static members and functions stored as C globals
 *
 * Hooks: resolveIdent, convertAssign, convertSpecialMethodCall,
 * convertFunctionCall.
 */
class N64Extractor implements IExtractorContext {
    var className:String;
    var modulePath:String;
    var fields:Array<Field>;
    var cls:ClassType;              // Typed class (fields have their compiler types)
    public var cName(default, null):String;
    public var meta(default, null):TraitMeta;

    var members:Map<String, MemberIR> = new Map();
    var memberNames:Array<String> = [];               // Members in declaration order
    var memberTypes:Map<String, String> = new Map();  // Every var field's Haxe type (signals and unsupported types too)
    var localVarTypes:Map<String, String> = new Map();
    var pointerLocals:Map<String, Bool> = new Map();  // Locals holding a pointer (see convertVar)
    var methodMap:Map<String, Function> = new Map();
    var converters:Array<ICallConverter>;
    var inMemberDefault:Bool = false;  // Converting a member's default value

    // Static APIs without an N64 equivalent, checked before the converters
    // (the Tween converter would otherwise take iron.system.Tween's static calls)
    static var unsupportedStaticClasses = [
        "iron.system.Audio", "kha.audio1.Audio", "kha.audio2.Audio",
        "iron.system.Tween", "kha.netsync.Network", "koui.Koui", "kha.System",
    ];

    function new(fields:Array<Field>, cls:ClassType, cName:String, converters:Array<ICallConverter>) {
        this.className = cls.name;
        this.modulePath = cls.module;
        this.fields = fields;
        this.cls = cls;
        this.cName = cName;
        this.converters = converters;
        this.meta = {
            uses_input: false,
            uses_transform: false,
            mutates_transform: false,
            uses_time: false,
            uses_physics: false,
            uses_ui: false,
            uses_tween: false,
            uses_autoload: false,
            buttons_used: [],
            button_events: [],
            contact_events: [],
            signals: [],
            has_remove_update: false,
            has_remove_fixed_update: false,
            has_remove_late_update: false,
            has_remove_render2d: false,
            dynamic_updates: [],
            dynamic_fixed_updates: [],
            autoloads_used: []
        };

        // Project configuration written in code (e.g. in a constructor)
        for (field in fields) {
            switch (field.kind) {
                case FFun(f): AudioCallConverter.scanConfig(f.expr);
                default:
            }
        }
    }

    // ========================================================================
    // Classes
    // ========================================================================

    /** Whether a class extends iron.Trait (directly or indirectly). */
    public static function isTrait(c:ClassType):Bool {
        while (c.superClass != null) {
            c = c.superClass.t.get();
            if (c.module == "iron.Trait" && c.name == "Trait") return true;
        }
        return false;
    }

    /** C prefix of a trait: its module path, plus the class name if it differs from the module's. */
    public static function traitCName(c:ClassType):String {
        var moduleName = c.module.substr(c.module.lastIndexOf(".") + 1);
        var path = moduleName.toLowerCase() == c.name.toLowerCase() ? c.module : c.module + "_" + c.name;
        return path.replace(".", "_").toLowerCase();
    }

    // ========================================================================
    // Members and functions
    // ========================================================================

    /**
     * Record a var or property field. Signals and engine-provided values get
     * no member; `setter` is the property's write access ("set" for a setter).
     */
    function collectVar(name:String, t:ComplexType, e:Expr, ?setter:String):Void {
        var haxeType = t != null ? N64MacroBase.complexTypeToString(t) : "Dynamic";
        memberTypes.set(name, haxeType);

        // Per-instance signals are ArmSignal fields, static ones are declared
        // by the signal registry (n64_signals.json)
        if (haxeType == "Signal") {
            meta.signals.push({name: name});
            return;
        }
        var member = extractMember(name, haxeType, e);
        if (member == null) return;
        if (setter == "set") member.setter = "set_" + name;
        members.set(name, member);
        memberNames.push(name);
    }

    function extractMember(name:String, haxeType:String, e:Expr):MemberIR {
        // Engine-provided on N64 (gamepad, rigid body, ...): not stored
        if (EngineTypes.isProvidedMember(cls, name)) return null;

        if (TypeMap.isSupported(haxeType)) {
            return {haxeType: haxeType, ctype: TypeMap.getCType(haxeType), defaultValue: memberDefault(e)};
        }
        var trait = traitMemberClass(name);
        if (trait == null) return null;
        return {
            haxeType: haxeType,
            ctype: traitCName(trait) + "Data*",
            defaultValue: memberDefault(e),
            traitRef: trait.pack.concat([trait.name]).join(".")
        };
    }

    function memberDefault(e:Expr):IRNode {
        if (e == null) return null;
        return switch (e.expr) {
            // Containers are zero-initialized in C
            case ENew({name: "Map" | "Array"}, _): { type: "skip" };
            default:
                inMemberDefault = true;
                var ir = exprToIR(e);
                inMemberDefault = false;
                ir;
        };
    }

    /**
     * The trait a member is typed as, from its typed field (its C type is a
     * pointer to that trait's data: `var player:Player` →
     * "arm_player_playerData*"). Null if it isn't a trait.
     */
    function traitMemberClass(name:String):ClassType {
        var field = Lambda.find(cls.statics.get().concat(cls.fields.get()), f -> f.name == name);
        if (field == null) return null;
        return switch (Context.follow(field.type)) {
            case TInst(c, _) if (isTrait(c.get())): c.get();
            default: null;
        };
    }

    /** Start of a function body: locals of other functions are out of scope. */
    function beginScope():Void {
        localVarTypes = new Map();
        pointerLocals = new Map();
    }

    /** A local variable, marked when it holds a pointer. */
    function localIdent(name:String):IRNode {
        return pointerLocals.exists(name) ? { type: "ident", value: name, props: { pointer: true } } : { type: "ident", value: name };
    }

    /** Arguments of a call to `f`, missing ones taking their default values. */
    function withDefaults(f:Function, args:Array<IRNode>):Array<IRNode> {
        var all = args.copy();
        while (all.length < f.args.length && f.args[all.length].value != null) {
            all.push(exprToIR(f.args[all.length].value));
        }
        return all;
    }

    /** A function body (block or single expression) as IR statements. */
    function statements(e:Expr):Array<IRNode> {
        var out:Array<IRNode> = [];
        if (e == null) return out;
        var exprs = switch (e.expr) {
            case EBlock(list): list;
            default: [e];
        };
        for (expr in exprs) {
            var node = exprToIR(expr);
            if (N64MacroBase.keepStatement(node)) out.push(node);
        }
        return out;
    }

    // ========================================================================
    // Expressions
    // ========================================================================

    public function exprToIR(e:Expr):IRNode {
        if (e == null) return null;

        // Track the source position so unsupported code is reported at its line
        var prevPos = N64MacroBase.currentPos;
        N64MacroBase.currentPos = e.pos;
        var ir = convertExpr(e);
        N64MacroBase.currentPos = prevPos;
        return ir;
    }

    function convertExpr(e:Expr):IRNode {
        return switch (e.expr) {
            case EConst(CIdent(name)) if (name != "true" && name != "false" && name != "null"):
                resolveIdent(name, e);
            case EConst(c):
                N64MacroBase.constToIR(c);

            // handle = null: the runtime's "no sound" handle
            case EBinop(OpAssign, e1, e2) if (isSoundHandle(e1) && N64MacroBase.isNullLiteral(e2)):
                { type: "assign", children: [exprToIR(e1), SOUND_HANDLE_NULL] };
            case EBinop(OpAssign, e1, e2):
                var ui = convertUIAssign(e1, e2);
                ui != null ? ui : convertAssign(e1, e2);
            // Sound handles are compared by sound (also with null)
            case EBinop(op = OpEq | OpNotEq, e1, e2) if (isSoundHandle(e1) || isSoundHandle(e2)):
                { type: "sound_handle_equals", value: op == OpEq ? "==" : "!=", children: [handleValue(e1), handleValue(e2)] };
            case EBinop(OpAdd, e1, e2) if (getExprType(e1) == "String" || getExprType(e2) == "String"):
                new StringConcatBuilder(members, localVarTypes, exprToIR, getExprType).build(e1, e2);
            case EBinop(op, e1, e2):
                var equality = N64MacroBase.stringEquality(op, e1, e2, exprToIR);
                equality != null ? equality : { type: "binop", value: N64MacroBase.binopToString(op), children: [exprToIR(e1), exprToIR(e2)] };

            case EUnop(op, postFix, operand):
                { type: "unop", value: N64MacroBase.unopToString(op), children: [exprToIR(operand)], props: { postfix: postFix } };

            case EField(obj, field):
                convertFieldAccess(e, obj, field);

            case ECall(callExpr, params):
                convertCall(callExpr, params);

            // Binary operators are emitted in parentheses
            case EParenthesis(inner):
                exprToIR(inner);

            case ENew(tp, params):
                convertNew(e, tp, params);

            case EBlock(exprs):
                { type: "block", children: [for (expr in exprs) exprToIR(expr)] };

            case EIf(econd, eif, eelse):
                convertIf(econd, eif, eelse);

            case EVars(vars):
                var decls = [for (v in vars) convertVar(v)];
                decls.length == 1 ? decls[0] : { type: "block", children: decls };

            case EReturn(retExpr):
                { type: "return", children: retExpr != null ? [exprToIR(retExpr)] : null };

            case EWhile(econd, ebody, normalWhile):
                { type: normalWhile ? "while" : "do_while", children: [exprToIR(econd), exprToIR(ebody)] };

            case EFor(it, ebody):
                convertForLoop(it, ebody);

            case EBreak:
                { type: "break" };

            case EContinue:
                { type: "continue" };

            case ETernary(econd, eif, eelse):
                { type: "ternary", children: [exprToIR(econd), exprToIR(eif), exprToIR(eelse)] };

            // Aura.mixChannels["name"]: the mix channel's constant
            case EArray(_, _) if (AudioCallConverter.mixChannel(e) != null):
                { type: "c_literal", c_code: AudioCallConverter.mixChannel(e) };
            case EArray(e1, e2):
                convertArrayAccess(e1, e2);

            case EArrayDecl(values):
                values.length == 0 ? { type: "empty_array" } : N64MacroBase.unsupported("non-empty array literals are not supported on N64 yet");

            default:
                N64MacroBase.unhandledExpr(e);
        };
    }

    /** Hook: an identifier that isn't a literal. */
    function resolveIdent(name:String, e:Expr):IRNode {
        if (localVarTypes.exists(name)) return localIdent(name);
        return { type: "ident", value: name };
    }

    // ========================================================================
    // Sound handles (Aura channel handles → ArmSoundHandle)
    // ========================================================================

    static var SOUND_HANDLE_NULL:IRNode = { type: "c_literal", c_code: "ARM_SOUND_HANDLE_NULL" };

    function isSoundHandle(e:Expr):Bool {
        return TypeMap.getCType(getExprType(e)) == "ArmSoundHandle";
    }

    function isPointerLocal(e:Expr):Bool {
        return switch (e.expr) {
            case EConst(CIdent(name)): pointerLocals.exists(name);
            default: false;
        };
    }

    // A handle's value (null is the "no sound" handle)
    function handleValue(e:Expr):IRNode {
        if (N64MacroBase.isNullLiteral(e)) return SOUND_HANDLE_NULL;
        if (isPointerLocal(e)) return { type: "deref", children: [exprToIR(e)] };
        return exprToIR(e);
    }

    public function handlePointer(e:Expr):IRNode {
        if (isPointerLocal(e)) return exprToIR(e);
        return { type: "address_of", children: [exprToIR(e)] };
    }

    /** Hook: `this.name` (thisExpr is the `this`) as a member, null if it isn't one. */
    function thisField(thisExpr:Expr, name:String):IRNode {
        return members.exists(name) ? { type: "member", value: name } : null;
    }

    // ========================================================================
    // Koui UI (by type)
    // ========================================================================

    // Koui's element list (Array<koui.elements.Element>): N64's canvas element list
    function isKouiElements(e:Expr):Bool {
        var t = e != null ? TypedIndex.exprType(e) : null;
        if (t == null) return false;
        return switch (Context.follow(t)) {
            case TInst(_.get() => {name: "Array", pack: []}, [elem]):
                switch (Context.follow(elem)) {
                    case TInst(c, _): TypedIndex.classPath(c.get()) == "koui.elements.Element";
                    default: false;
                }
            default: false;
        };
    }

    function ctypeOf(e:Expr):String {
        return TypeMap.getCType(getExprType(e));
    }

    // Snake case of a Haxe field (focusUp -> focus_up), as the C UI structs name them
    static function snakeCase(name:String):String {
        return ~/([a-z0-9])([A-Z])/g.replace(name, "$1_$2").toLowerCase();
    }

    /** UI assignments with their own runtime calls, null for others. */
    function convertUIAssign(e1:Expr, e2:Expr):IRNode {
        // The element list itself: N64 uses the canvas's
        if (isKouiElements(e1)) return { type: "skip" };
        switch (e1.expr) {
            case EField(obj, "visible"):
                switch (obj.expr) {
                    // elements[i].visible = v
                    case EArray(list, index) if (isKouiElements(list)):
                        return { type: "canvas_element_set_visible", args: [exprToIR(index), exprToIR(e2)] };
                    default:
                }
                // A layout's visibility applies to its children
                if (ctypeOf(obj) == "UIGroup*") {
                    return { type: "ui_group_set_visible", args: [exprToIR(obj), exprToIR(e2)] };
                }
            case EField(obj, "text") if (ctypeOf(obj) == "UILabel*"):
                meta.uses_ui = true;
                var text = exprToIR(e2);
                N64MacroBase.recordLabelText(text);
                return { type: "label_set_text", args: [exprToIR(obj), text] };
            default:
        }
        return null;
    }

    /** Hook: `e1 = e2`. */
    function convertAssign(e1:Expr, e2:Expr):IRNode {
        return { type: "assign", children: [exprToIR(e1), exprToIR(e2)] };
    }

    /** `e1[e2]`: typed Map and Array containers use their generated functions. */
    function convertArrayAccess(e1:Expr, e2:Expr):IRNode {
        // elements[i]: the canvas element (a pointer)
        if (isKouiElements(e1)) return { type: "canvas_element", children: [exprToIR(e2)] };
        var objType = getExprType(e1);
        var ctype = TypeMap.getCType(objType);
        var container = TypeMap.isMapType(objType) ? TypeMap.mapReadNode(objType) : TypeMap.isArrayType(objType) ? "array_get" : null;
        // Other arrays (e.g. Koui's elements) are left to the emitter
        if (container == null || ctype == null || !ctype.startsWith("Arm")) {
            return { type: "array_access", children: [exprToIR(e1), exprToIR(e2)] };
        }
        // map[key][i]: the inner get returns a pointer
        switch (e1.expr) {
            case EArray(_, _):
                return { type: "array_get_nested", value: ctype, children: [exprToIR(e1), exprToIR(e2)] };
            default:
        }
        var key = exprToIR(e2);
        if (container != "array_get") N64MacroBase.recordMapKey(ctype, key, false);
        return { type: container, value: ctype, object: exprToIR(e1), children: [key] };
    }

    function convertIf(econd:Expr, eif:Expr, eelse:Expr):IRNode {
        return {
            type: "if",
            children: [exprToIR(econd)],
            props: {
                then: statements(eif),
                else_: eelse != null ? statements(eelse) : null
            }
        };
    }

    function convertVar(v:Var):IRNode {
        // Declared type, or the one the compiler inferred
        var haxeType = v.type != null ? N64MacroBase.complexTypeToString(v.type) : getExprType(v.expr);
        var ctype = TypeMap.getCType(haxeType);
        if (ctype == null) ctype = "float";
        localVarTypes.set(v.name, haxeType);
        pointerLocals.remove(v.name);
        var init = v.expr != null ? exprToIR(v.expr) : null;

        // A sound handle read from a handle array (channels[key][i]): a pointer
        // to it, so the audio state is updated in place
        if (ctype == "ArmSoundHandle" && init != null && init.type == "array_get_nested" && init.value == "ArmSoundHandleArray") {
            pointerLocals.set(v.name, true);
            ctype += "*";
            init = { type: "array_get_nested_ptr", value: init.value, children: init.children };
        }
        return {
            type: "var",
            value: v.name,
            props: { ctype: ctype, haxeType: haxeType },
            children: init != null ? [init] : null
        };
    }

    // for (i in a...b)
    function convertForLoop(it:Expr, body:Expr):IRNode {
        switch (it.expr) {
            case EBinop(OpIn, e1, e2):
                var varName = switch (e1.expr) {
                    case EConst(CIdent(s)): s;
                    default: "_i";
                };
                localVarTypes.set(varName, "Int");

                switch (e2.expr) {
                    case EBinop(OpInterval, start, end):
                        return {
                            type: "for_range",
                            value: varName,
                            children: [exprToIR(start), exprToIR(end), exprToIR(body)]
                        };
                    default:
                }
            default:
        }
        return N64MacroBase.unsupported("for loops are only supported over integer ranges (for (i in a...b)) on N64");
    }

    function convertNew(e:Expr, tp:TypePath, params:Array<Expr>):IRNode {
        var args = [for (p in params) exprToIR(p)];

        // Vectors: C struct literal
        var vector = N64MacroBase.vectorLiteral(e, args);
        if (vector != null) return vector;

        // Tweens come from the runtime's pool
        if (TypedIndex.newClass(e) == "armory.system.Tween") {
            meta.uses_tween = true;
            // Members' tweens are counted from the instances; others can't be
            if (!inMemberDefault) meta.allocates_tweens = true;
            return { type: "tween_alloc" };
        }

        return N64MacroBase.unsupported('new ${tp.name}() is not supported on N64 yet');
    }

    function convertFieldAccess(e:Expr, obj:Expr, field:String):IRNode {
        // handle.finished: whether the sound still plays (not a cached flag)
        if (field == "finished" && isSoundHandle(obj)) {
            return { type: "sound_handle_finished", children: [handlePointer(obj)] };
        }

        // this.member: routed through the class's storage, not the object
        switch (obj.expr) {
            case EConst(CIdent("this")):
                var member = thisField(obj, field);
                if (member != null) return member;
            default:
        }

        // iron.App static field access -> C global
        if (field == "pauseUpdates" && TypedIndex.isClassRef(obj, "iron.App")) {
            return { type: "c_literal", c_code: "app_pause_updates" };
        }

        // Field of an engine-provided class (like PhysicsWorld.active)
        if (EngineTypes.isProvided(TypedIndex.typeRefPath(obj))) {
            return { type: "skip" };
        }

        // AutoloadClass.traitMember.object -> object pointer from the trait's data
        if (field == "object") {
            switch (obj.expr) {
                case EField(innerObj, memberName):
                    var autoload = AutoloadCallConverter.getAutoloadCName(innerObj);
                    if (autoload != null) {
                        useAutoload(autoload);
                        return {
                            type: "autoload_trait_object",
                            value: field,
                            props: { autoload: autoload, member: memberName }
                        };
                    }
                default:
            }
        }

        // AutoloadClass.member
        var autoload = AutoloadCallConverter.getAutoloadCName(obj);
        if (autoload != null) {
            useAutoload(autoload);
            return {
                type: "autoload_field",
                value: field,
                props: { autoload: autoload, member: field }
            };
        }

        // kha.Window.get(0).width/height -> render2d_get_width()/height()
        if (field == "width" || field == "height") {
            switch (obj.expr) {
                case ECall({expr: EField(windowExpr, "get")}, _) if (TypedIndex.isClassRef(windowExpr, "kha.Window")):
                    return { type: "c_literal", c_code: field == "width" ? "render2d_get_width()" : "render2d_get_height()" };
                default:
            }
        }

        // Scene.active.raw.name -> scene_get_name(scene_get_current_id())
        if (field == "name") {
            switch (obj.expr) {
                case EField({expr: EField(sceneExpr, "active")}, "raw") if (TypedIndex.isClassRef(sceneExpr, "iron.Scene")):
                    return { type: "c_literal", c_code: "scene_get_name(scene_get_current_id())" };
                default:
            }
        }

        // Scene.active.getGroup(groupName).length -> scene_get_group_count(groupName)
        if (field == "length") {
            switch (obj.expr) {
                case ECall({expr: EField({expr: EField(sceneExpr, "active")}, "getGroup")}, params)
                    if (params.length > 0 && TypedIndex.isClassRef(sceneExpr, "iron.Scene")):
                    return { type: "scene_get_group_count", args: [exprToIR(params[0])] };
                default:
            }
        }

        // Color constants (Color.Black, Color.Orange, ...): the compiler folds
        // them to their ARGB value -> RGBA32 for N64
        var argb = TypedIndex.receiverPath(e) == "kha.Color" ? TypedIndex.intConstant(e) : null;
        if (argb != null) {
            var r = (argb >> 16) & 0xff, g = (argb >> 8) & 0xff, b = argb & 0xff, a = (argb >>> 24) & 0xff;
            return { type: "c_literal", c_code: 'RGBA32($r, $g, $b, $a)' };
        }

        // Assets.sounds.sound_name -> "rom:/sound_name.wav64"
        switch (obj.expr) {
            case EField(innerObj, "sounds") if (TypedIndex.isClassRef(innerObj, "kha.Assets")):
                var soundPath = 'rom:/${field}.wav64';
                return { type: "string", value: soundPath, c_code: '"$soundPath"' };
            default:
        }

        switch (obj.expr) {
            // Transform access
            case EField(innerObj, "transform"):
                meta.uses_transform = true;
                var ownerNode:IRNode = switch (innerObj.expr) {
                    // Self-reference - use "object"
                    case EConst(CIdent("this")), EConst(CIdent("object")): { type: "ident", value: "object" };
                    // External object (e.g., target.transform.loc)
                    default: exprToIR(innerObj);
                };
                return { type: "field_access", object: ownerNode, value: "transform." + field };
            case EConst(CIdent("transform")):
                meta.uses_transform = true;
                return { type: "field_access", object: { type: "ident", value: "object" }, value: "transform." + field };
            case _ if (TypedIndex.isClassRef(obj, "iron.system.Time")):
                if (field == "delta") {
                    meta.uses_time = true;
                    return { type: "ident", value: "time_delta" };
                } else if (field == "scale") {
                    meta.uses_time = true;
                    return { type: "ident", value: "time_scale" };
                } else if (field == "fixedStep") {
                    meta.uses_time = true;
                    return { type: "ident", value: "time_fixed_step" };
                }
            case _ if (InputCallConverter.isGamepad(obj)):
                // gamepad.leftStick, gamepad.rightStick
                meta.uses_input = true;
                if (field == "leftStick" || field == "rightStick") {
                    return { type: "c_literal", c_code: "(ArmVec2){input_stick_x(), input_stick_y()}" };
                }
            default:
        }

        // The canvas element list's length
        if (field == "length" && isKouiElements(obj)) {
            return { type: "c_literal", c_code: "UI_ELEMENT_COUNT" };
        }

        // Buttons are indices into the canvas's button pool
        if (getExprType(obj) == "Button") {
            return { type: "button_field", value: snakeCase(field), children: [exprToIR(obj)] };
        }

        // Array length: the C arrays' count
        if (field == "length" && TypeMap.isArrayType(getExprType(obj))) {
            return fieldAccess(obj, "count");
        }

        // Vec3 component access
        if (field == "x" || field == "y" || field == "z") {
            return { type: "field_access", object: exprToIR(obj), value: field };
        }

        // object.parent -> object_get_parent((ArmObject*)obj, scene_get(scene_get_current_id()))
        if (field == "parent") {
            switch (obj.expr) {
                case EConst(CIdent("object")), EConst(CIdent("this")):
                    return { type: "c_literal", c_code: "object_get_parent((ArmObject*)obj, scene_get(scene_get_current_id()))" };
                default:
                    return { type: "c_literal", c_code: 'object_get_parent((ArmObject*){obj}, scene_get(scene_get_current_id()))' };
            }
        }

        // Static field of a class nothing above translates
        var classPath = TypedIndex.typeRefPath(obj);
        if (classPath != null && !N64MacroBase.isExtractedClass(classPath)) {
            var shortName = classPath.substr(classPath.lastIndexOf(".") + 1);
            return N64MacroBase.unsupported('$shortName.$field is not supported on N64 yet');
        }

        return fieldAccess(obj, field);
    }

    /** obj.field in C, through a pointer (->) for pointer locals, pointer types and pointers the runtime returns. */
    function fieldAccess(obj:Expr, field:String):IRNode {
        var objIR = exprToIR(obj);
        var ctype = ctypeOf(obj);
        var deref = isPointerLocal(obj) || objIR.type == "map_get" || objIR.type == "canvas_element"
            || (ctype != null && ctype.endsWith("*") && ctype != "void*" && ctype != "const char*");
        return deref ? { type: "field_access", object: objIR, value: field, props: { deref: true } }
            : { type: "field_access", object: objIR, value: field };
    }

    function convertCall(callExpr:Expr, params:Array<Expr>):IRNode {
        var args = [for (p in params) exprToIR(p)];

        switch (callExpr.expr) {
            case EField(obj, method):
                var special = convertSpecialMethodCall(callExpr, obj, method, params, args);
                if (special != null) return special;

                // Time.time() -> time_get()
                if (method == "time" && TypedIndex.isClassRef(obj, "iron.system.Time")) {
                    meta.uses_time = true;
                    return { type: "call", value: "time_get", args: [] };
                }
                // Color.fromFloats(r, g, b, a) -> color built at runtime
                if (method == "fromFloats" && TypedIndex.isClassRef(obj, "kha.Color")) {
                    return { type: "color_from_floats", args: args };
                }

                var staticClass = TypedIndex.typeRefPath(obj);
                if (staticClass != null && Lambda.has(unsupportedStaticClasses, staticClass)) {
                    var shortName = staticClass.substr(staticClass.lastIndexOf(".") + 1);
                    return N64MacroBase.unsupported('$shortName.$method() is not supported on N64');
                }
                if (TypedIndex.isClassRef(obj, "koui.utils.SceneManager")) {
                    if (method == "clearScenes") return { type: "canvas_clear_scenes" };
                    return N64MacroBase.unsupported('SceneManager.$method() is not supported on N64 yet');
                }

                for (conv in converters) {
                    var result = conv.tryConvert(obj, method, args, params, this);
                    if (result != null) return result;
                }

                // A method of an extracted class, called on an instance
                var owner = N64MacroBase.extractedClass(TypedIndex.callOwner(obj, method));
                if (owner != null) {
                    // Autoloads are singletons: their functions are C globals
                    var autoload = owner.meta.has(":n64Autoload") || owner.meta.has("n64Autoload") ? owner.name.toLowerCase() : null;
                    if (autoload != null) {
                        useAutoload(autoload);
                        return {
                            type: "autoload_call",
                            value: method,
                            c_code: '${autoload}_$method',
                            args: args,
                            props: { c_name: autoload, method: method }
                        };
                    }
                    return N64MacroBase.unsupported('calling $method() on another trait instance is not supported on N64 yet');
                }

                // The methods the emitters translate
                return N64MacroBase.methodCall(obj, method, args, exprToIR);

            // trace() -> debugf() for N64 debug output
            case EConst(CIdent("trace")):
                return { type: "debug_call", args: args };

            case EConst(CIdent(name)):
                return convertFunctionCall(callExpr, name, params, args);

            default:
                return N64MacroBase.unsupported("only calls to named functions and methods are supported on N64 (not " + N64MacroBase.describeExpr(callExpr) + ")");
        }
    }

    /** Hook: `obj.method(...)` (callExpr is `obj.method`) handled before the shared rules (null if not). */
    function convertSpecialMethodCall(callExpr:Expr, obj:Expr, method:String, params:Array<Expr>, args:Array<IRNode>):IRNode {
        return null;
    }

    /** Hook: `name(...)`, a call without a receiver (callExpr is `name`). */
    function convertFunctionCall(callExpr:Expr, name:String, params:Array<Expr>, args:Array<IRNode>):IRNode {
        return { type: "call", value: name, args: args };
    }

    // ========================================================================
    // IExtractorContext
    // ========================================================================

    /**
     * Haxe type name of an expression (Int, Float, String, Vec2, Label, Signal, etc.),
     * from the compiler's typed AST, or "Dynamic" if unknown.
     */
    public function getExprType(e:Expr):String {
        var typed = e != null ? TypedIndex.typeOf(e) : null;
        return typed != null ? typed : "Dynamic";
    }

    public function inferExprType(e:Expr):String {
        // `this` and `object` stand for the trait's object on N64
        switch (e.expr) {
            case EConst(CIdent("this")), EConst(CIdent("object")): return "ArmObject";
            default:
        }
        var typed = TypedIndex.typeOf(e);
        return typed != null ? typed : "Dynamic";
    }

    // A string literal, or an identifier's name (e.g. removeUpdate(onUpdate))
    public function extractStringArg(e:Expr):String {
        return switch (e.expr) {
            case EConst(CString(s)): s;
            case EConst(CIdent(s)): s;
            default: null;
        };
    }

    public function getMeta():TraitMeta {
        return meta;
    }

    public function getMemberType(name:String):String {
        return memberTypes.get(name);
    }

    public function getCName():String {
        return cName;
    }

    public function getLocalVarType(name:String):String {
        return localVarTypes.get(name);
    }

    public function getMethod(name:String):Function {
        return methodMap.get(name);
    }

    public function getEvents():Map<String, Array<IRNode>> {
        return new Map();
    }

    public function isAutoload():Bool {
        return false;
    }

    public function classPath():String {
        return cls.pack.concat([cls.name]).join(".");
    }

    public function useAutoload(autoload:String):Void {
        meta.uses_autoload = true;
        if (autoload != cName && meta.autoloads_used.indexOf(autoload) < 0) meta.autoloads_used.push(autoload);
    }
}
#end
