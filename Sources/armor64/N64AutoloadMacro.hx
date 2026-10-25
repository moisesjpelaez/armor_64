package armor64;

#if macro
import haxe.macro.Expr;
import haxe.macro.Type;

import armor64.IRTypes;
import armor64.N64MacroBase;
import armor64.N64Extractor;
import armor64.TypedIndex;
import armor64.mapping.TypeMap;
import armor64.converters.VecCallConverter;
import armor64.converters.SignalCallConverter;
import armor64.converters.StdCallConverter;
import armor64.converters.AutoloadCallConverter;
import armor64.converters.AudioCallConverter;
import armor64.converters.TweenCallConverter;
import armor64.converters.MapCallConverter;
import armor64.converters.ArrayCallConverter;

using Lambda;

/**
 * Autoloads (classes marked @:n64Autoload) → n64_autoloads.json.
 *
 * Autoloads are singleton-like classes that:
 * - Initialize once at engine startup (before scenes)
 * - Persist across scene changes
 * - Are globally accessible from traits and other autoloads
 *
 * Their static members and functions become C globals prefixed with the
 * class's C name. The expression conversion is shared with traits
 * (N64Extractor).
 *
 * Usage:
 *   @:n64Autoload(order=0)
 *   class GameEvents {
 *       public static var sceneLoaded: Signal = new Signal();
 *       public static function init() { ... }
 *   }
 */
class N64AutoloadMacro {
    static var autoloadData:Map<String, AutoloadIR> = new Map();
    static var initialized:Bool = false;

    /** Called by N64Macro.build() for an autoload class. */
    public static function build(localClass:Ref<ClassType>, fields:Array<Field>, order:Int):Void {
        if (!initialized) {
            initialized = true;
            N64MacroBase.addWriter(writeAutoloadJson);
        }

        var cls = localClass.get();
        // Autoloads are identified by class name in the IR, and their C names
        // are the lowercase class name
        if (!N64MacroBase.claimExportedName("autoload", cls.name.toLowerCase(), cls)) return;

        // Extract autoload IR after typing, when the TypedIndex is available
        N64MacroBase.deferExtraction(localClass, function() {
            // Get the class again: the build-time snapshot has no typed fields yet
            var autoloadIR = new AutoloadExtractor(fields, order, localClass.get()).extract();
            if (autoloadIR != null) {
                autoloadData.set(cls.name, autoloadIR);
            }
        });
    }

    /** Initialization order from @:n64Autoload, null if the class isn't an autoload. */
    public static function getAutoloadOrder(meta:Array<MetadataEntry>):Null<Int> {
        for (m in meta) {
            if (m.name == ":n64Autoload" || m.name == "n64Autoload") {
                // Default order is 100 if not specified
                var order = 100;
                if (m.params != null) {
                    for (param in m.params) {
                        switch (param.expr) {
                            case EBinop(OpAssign, e1, e2):
                                // order=N syntax
                                switch (e1.expr) {
                                    case EConst(CIdent("order")):
                                        switch (e2.expr) {
                                            case EConst(CInt(v)): order = Std.parseInt(v);
                                            default:
                                        }
                                    default:
                                }
                            case EConst(CInt(v)):
                                // Just a number: @:n64Autoload(5)
                                order = Std.parseInt(v);
                            default:
                        }
                    }
                }
                return order;
            }
        }
        return null;
    }

    static function writeAutoloadJson():Void {
        if (!autoloadData.keys().hasNext()) return;

        var autoloads:Dynamic = {};

        for (name in autoloadData.keys()) {
            var ir = autoloadData.get(name);

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
                if (m.traitRef != null) memberObj.trait_ref = m.traitRef;
                membersArr.push(memberObj);
            }

            // Convert functions
            var functionsArr:Array<Dynamic> = [];
            for (funcName in ir.functions.keys()) {
                var f = ir.functions.get(funcName);
                var paramsArr:Array<Dynamic> = [];
                for (p in f.params) {
                    paramsArr.push({
                        name: p.name,
                        type: p.haxeType,
                        ctype: p.ctype,
                        optional: p.optional,
                        default_value: p.defaultValue != null ? N64MacroBase.serializeIRNode(p.defaultValue) : null
                    });
                }
                functionsArr.push({
                    name: f.name,
                    c_name: f.cName,
                    return_type: f.returnType,
                    params: paramsArr,
                    body: [for (n in f.body) N64MacroBase.serializeIRNode(n)],
                    is_public: f.isPublic
                });
            }

            Reflect.setField(autoloads, name, {
                module: ir.module,
                c_name: ir.cName,
                order: ir.order,
                members: membersArr,
                functions: functionsArr,
                has_init: ir.hasInit,
                meta: ir.meta
            });
        }

        var output:Dynamic = {
            ir_version: 1,
            autoloads: autoloads
        };

        N64MacroBase.writeJsonFile("n64_autoloads.json", output);
    }
}

// ============================================================================
// Autoload Extractor
// ============================================================================

class AutoloadExtractor extends N64Extractor {
    var order:Int;
    var functions:Map<String, AutoloadFunctionIR> = new Map();
    var hasInit:Bool = false;

    public function new(fields:Array<Field>, order:Int, cls:ClassType) {
        // Autoloads don't need: Physics, Transform, Input, Object, Scene,
        // Canvas, Graphics2, Button converters: their C uses the trait's
        // object or data, which autoloads don't have
        super(fields, cls, cls.name.toLowerCase(), [
            new VecCallConverter(),
            new SignalCallConverter(),
            new StdCallConverter(),
            new AutoloadCallConverter(),
            new AudioCallConverter(),
            new TweenCallConverter(),
            new MapCallConverter(),
            new ArrayCallConverter(),
        ]);
        this.order = order;
    }

    public function extract():AutoloadIR {
        // Pass 1: Collect all methods and detect singleton pattern
        var isSingleton = false;
        for (field in fields) {
            switch (field.kind) {
                case FFun(func):
                    methodMap.set(field.name, func);
                case FVar(t, _):
                    // Singleton: static field of the same type as the class
                    if (hasAccess(field.access, AStatic) && t != null && N64MacroBase.complexTypeToString(t) == className) {
                        isSingleton = true;
                    }
                default:
            }
        }

        // Pass 2: Extract static members and functions
        for (field in fields) {
            // For singletons, also process instance members/methods
            if (!hasAccess(field.access, AStatic) && !isSingleton) continue;
            if (field.name == "new") continue;

            switch (field.kind) {
                // Fields of the class's own type are the singleton instance
                case FVar(t, _) | FProp(_, _, t, _) if (t != null && N64MacroBase.complexTypeToString(t) == className):
                case FVar(t, e):
                    collectVar(field.name, t, e);
                case FProp(_, set, t, e):
                    // Property with a backing field (e.g., public var volume(default, set): Float)
                    collectVar(field.name, t, e, set);
                case FFun(func):
                    var funcIR = extractFunction(field.name, func, hasAccess(field.access, APublic));
                    functions.set(field.name, funcIR);
                    if (field.name == "init") hasInit = true;
                default:
            }
        }

        return {
            name: className,
            module: modulePath,
            cName: cName,
            order: order,
            members: members,
            functions: functions,
            hasInit: hasInit,
            meta: { order: order, uses_audio: meta.uses_audio, allocates_tweens: meta.allocates_tweens }
        };
    }

    function hasAccess(access:Array<Access>, target:Access):Bool {
        return access != null && access.exists(a -> Type.enumEq(a, target));
    }

    function extractFunction(name:String, func:Function, isPublic:Bool):AutoloadFunctionIR {
        beginScope();
        var params:Array<AutoloadParamIR> = [];
        for (arg in func.args) {
            var haxeType = arg.type != null ? N64MacroBase.complexTypeToString(arg.type) : "Dynamic";
            var ctype = TypeMap.getCType(haxeType);
            params.push({
                name: arg.name,
                haxeType: haxeType,
                ctype: ctype != null ? ctype : "void*",
                optional: arg.opt || arg.value != null,
                defaultValue: arg.value != null ? exprToIR(arg.value) : null
            });
            localVarTypes.set(arg.name, haxeType);
        }

        var returnHaxeType = func.ret != null ? N64MacroBase.complexTypeToString(func.ret) : "Void";
        var returnType = returnHaxeType == "Void" ? null : TypeMap.getCType(returnHaxeType);

        return {
            name: name,
            cName: '${cName}_$name',
            returnType: returnType != null ? returnType : "void",
            params: params,
            body: statements(func.expr),
            isPublic: isPublic
        };
    }

    // ========================================================================
    // AST → IR hooks (see N64Extractor)
    // ========================================================================

    // Locals, then this class's members (C globals) and functions
    override function resolveIdent(name:String, e:Expr):IRNode {
        if (localVarTypes.exists(name)) return localIdent(name);
        if (members.exists(name)) return { type: "member", value: name };
        if (methodMap.exists(name)) return { type: "method_ref", method: name, cName: '${cName}_$name' };
        return super.resolveIdent(name, e);
    }

    // inst.x on the singleton instance: this class's member
    override function convertFieldAccess(e:Expr, obj:Expr, field:String):IRNode {
        var ref = TypedIndex.fieldRef(e);
        if (ref != null && !ref.isStatic && ref.owner == cls.pack.concat([cls.name]).join(".") && members.exists(field)) {
            return { type: "member", value: field };
        }
        return super.convertFieldAccess(e, obj, field);
    }

    // This class's functions: their C globals, with default arguments filled in
    override function convertFunctionCall(callExpr:Expr, name:String, params:Array<Expr>, args:Array<IRNode>):IRNode {
        var f = methodMap.get(name);
        if (f != null && !localVarTypes.exists(name)) {
            return { type: "call", value: '${cName}_$name', args: withDefaults(f, args) };
        }
        return super.convertFunctionCall(callExpr, name, params, args);
    }

    override public function isAutoload():Bool {
        return true;
    }
}

#end
