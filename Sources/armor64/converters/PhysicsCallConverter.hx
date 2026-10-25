package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.N64MacroBase;
import armor64.TypedIndex;
import armor64.converters.ICallConverter;
import armor64.util.ExprUtils;

using StringTools;

/**
 * RigidBody contact subscriptions: notifyOnContact, removeContact (their
 * handlers are extracted as events). The other RigidBody calls are in
 * Bindings.hx.
 */
class PhysicsCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        // Only Armory's RigidBody, from the receiver's type
        var receiver = TypedIndex.receiverPath(obj);
        if (receiver == null || !isRigidBody(receiver)) return null;

        if (method == "notifyOnContact" || method == "removeContact") {
            ctx.getMeta().uses_physics = true;
            return convertContactSubscription(method, args, rawParams, ctx);
        }
        return null;
    }

    // armory.trait.physics.oimo.RigidBody or armory.trait.physics.bullet.RigidBody
    static function isRigidBody(path:String):Bool {
        return StringTools.startsWith(path, "armory.trait.physics.") && StringTools.endsWith(path, ".RigidBody");
    }

    function convertContactSubscription(method:String, args:Array<IRNode>, params:Array<Expr>, ctx:IExtractorContext):IRNode {
        // Extract handler function name from first argument
        if (params.length == 0) return N64MacroBase.unsupported('$method() needs a handler method');

        var handlerName = ExprUtils.extractIdentName(params[0]);

        if (handlerName == null) return N64MacroBase.unsupported('$method() handler must be a method name on N64');

        var isSubscribe = (method == "notifyOnContact");
        var meta = ctx.getMeta();
        var cName = ctx.getCName();

        // Build full C handler name
        var fullHandlerName = cName + "_contact_" + handlerName;

        meta.contact_events.push({
            event_name: handlerName,
            handler_name: fullHandlerName,
            subscribe: isSubscribe
        });

        // If subscribing, extract the handler method body
        if (isSubscribe) {
            var func = ctx.getMethod(handlerName);
            if (func != null) {
                var events = ctx.getEvents();
                var eventKey = "contact_" + handlerName;
                if (!events.exists(eventKey)) {
                    events.set(eventKey, []);
                    extractContactHandler(func, events.get(eventKey), ctx);
                }
            }
        }

        // Return skip - the actual subscription is done at init time by Python
        return { type: "skip" };
    }

    function extractContactHandler(func:Function, eventNodes:Array<IRNode>, ctx:IExtractorContext):Void {
        if (func.expr == null) return;

        switch (func.expr.expr) {
            case EBlock(exprs):
                for (expr in exprs) {
                    var node = ctx.exprToIR(expr);
                    if (N64MacroBase.keepStatement(node)) {
                        eventNodes.push(node);
                    }
                }
            default:
                var node = ctx.exprToIR(func.expr);
                if (N64MacroBase.keepStatement(node)) {
                    eventNodes.push(node);
                }
        }
    }
}

#end