package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.N64MacroBase;
import armor64.TypedIndex;
import armor64.mapping.TypeMap;
import armor64.converters.ICallConverter;

/**
 * Converts armory.system.Signal calls: connect, disconnect and emit, on static
 * and per-instance signals, with method or anonymous handlers.
 *
 * Signals are identified by their declaring class and name (from the typed
 * AST) and registered in N64MacroBase.signals, which collects their argument
 * types from typed handlers and emits across all traits and autoloads. The C
 * generator (signal_codegen.py) renders the nodes:
 *   signal_connect / signal_disconnect: props {signal, handler}, object = instance
 *   signal_emit: props {signal}, args, object = instance
 * where `object` is the instance whose signal it is, absent for static signals
 * and the trait's own ones.
 */
class SignalCallConverter implements ICallConverter {
    // Per class (one converter per extractor): anonymous handler names stay the
    // same whatever order the classes are compiled in
    var anonCounter:Int = 0;

    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        // Only armory.system.Signal, from the typed AST
        if (TypedIndex.receiverPath(obj) != "armory.system.Signal") return null;

        var ref = TypedIndex.fieldRef(obj);
        if (ref == null) {
            return N64MacroBase.unsupported('Signal.$method() is only supported on Signal fields on N64');
        }
        var key = N64MacroBase.useSignal(ref.owner, ref.name, ref.isStatic);
        var instance = ref.isStatic ? null : instanceOf(obj, ctx);

        return switch (method) {
            case "connect", "disconnect": connection(method, key, ref.name, instance, rawParams, ctx);
            case "emit": emit(key, instance, args, rawParams, ctx);
            default: N64MacroBase.unsupported('Signal.$method() is not supported on N64 yet');
        };
    }

    // The instance whose per-instance signal this is: null for the trait's own
    function instanceOf(signalExpr:Expr, ctx:IExtractorContext):Null<IRNode> {
        return switch (signalExpr.expr) {
            case EField(receiver, _) if (!isThis(receiver)): ctx.exprToIR(receiver);
            default: null;
        };
    }

    function connection(method:String, key:String, signalName:String, instance:Null<IRNode>, params:Array<Expr>, ctx:IExtractorContext):IRNode {
        if (params.length == 0) return N64MacroBase.unsupported('Signal.$method() needs a handler');

        var handler = handlerOf(params[0], key, signalName, method == "connect", ctx);
        if (handler == null) {
            return N64MacroBase.unsupported(method == "connect"
                ? "Signal.connect() handler must be a method or an anonymous function on N64"
                : "Signal.disconnect() handler must be a method on N64");
        }
        // Handlers of a trait are connected with its data as context
        if (!ctx.isAutoload() && handler.kind != "static") ctx.getMeta().connects_signals = true;
        if (method == "connect") N64MacroBase.signalConnectSite(key, ctx.classPath());

        var node:IRNode = {
            type: 'signal_$method',
            props: {signal: key, handler: handler}
        };
        if (instance != null) node.object = instance;
        return node;
    }

    function emit(key:String, instance:Null<IRNode>, args:Array<IRNode>, params:Array<Expr>, ctx:IExtractorContext):IRNode {
        N64MacroBase.recordSignalTypes(key, [for (p in params) cType(argType(p, ctx))], false);
        var node:IRNode = {
            type: "signal_emit",
            props: {signal: key},
            args: args
        };
        if (instance != null) node.object = instance;
        return node;
    }

    /**
     * Handler of connect()/disconnect(): a method (of this trait, a parent,
     * another instance or a static function) or an anonymous function.
     */
    function handlerOf(e:Expr, key:String, signalName:String, allowAnonymous:Bool, ctx:IExtractorContext):Dynamic {
        var args = TypedIndex.functionArgs(e);
        var params = args != null ? [for (a in args) {name: a.name, ctype: cType(a.type)}] : null;

        switch (e.expr) {
            case EFunction(_, func):
                if (!allowAnonymous) return null;
                if (params == null) {
                    params = [for (a in func.args) {name: a.name, ctype: a.type != null ? cType(haxe.macro.ComplexTypeTools.toString(a.type)) : null}];
                }
                N64MacroBase.recordSignalTypes(key, [for (p in params) p.ctype], true);
                return {
                    kind: "anonymous",
                    id: '${signalName}_cb_${anonCounter++}',
                    params: params,
                    body: bodyOf(func, ctx)
                };
            default:
                var ref = TypedIndex.fieldRef(e);
                if (ref == null || params == null) return null;
                N64MacroBase.recordSignalTypes(key, [for (p in params) p.ctype], true);
                var handler:Dynamic = {
                    kind: ref.isStatic ? "static" : "method",
                    owner: ref.owner,
                    name: ref.name,
                    params: params
                };
                // Another instance's method: other.onHit
                if (!ref.isStatic) {
                    switch (e.expr) {
                        case EField(receiver, _) if (!isThis(receiver)): handler.instance = ctx.exprToIR(receiver);
                        default:
                    }
                }
                return handler;
        }
    }

    function bodyOf(func:Function, ctx:IExtractorContext):Array<IRNode> {
        var nodes:Array<IRNode> = [];
        if (func.expr == null) return nodes;
        var statements = switch (func.expr.expr) {
            case EBlock(exprs): exprs;
            default: [func.expr];
        };
        for (statement in statements) {
            var node = ctx.exprToIR(statement);
            if (N64MacroBase.keepStatement(node)) nodes.push(node);
        }
        return nodes;
    }

    // Type of an emitted argument: emit() takes `...args:Any`, so look past
    // the implicit conversion to Any
    static function argType(arg:Expr, ctx:IExtractorContext):String {
        var t = TypedIndex.valueType(arg);
        return t != null ? t : ctx.inferExprType(arg);
    }

    // C type of a signal argument, null when unknown
    static function cType(haxeType:Null<String>):Null<String> {
        if (haxeType == null || haxeType == "Dynamic" || haxeType == "Any") return null;
        return TypeMap.getCType(haxeType);
    }

    static function isThis(e:Expr):Bool {
        return switch (e.expr) {
            case EConst(CIdent("this")): true;
            default: false;
        };
    }
}

#end
