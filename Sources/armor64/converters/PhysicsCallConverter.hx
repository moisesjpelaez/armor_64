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
 * RigidBody contact subscriptions: notifyOnContact, removeContact, made where
 * the code calls them, on the receiver's body, as in oimo_module. The handler
 * method is extracted as an event; its RigidBody parameter is the other body's
 * object. The other RigidBody calls are in Bindings.hx.
 */
class PhysicsCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        // Only Armory's RigidBody, from the receiver's type
        var receiver = TypedIndex.receiverPath(obj);
        if (receiver == null || !isRigidBody(receiver)) return null;

        if (method == "notifyOnContact" || method == "removeContact") {
            ctx.getMeta().uses_physics = true;
            return convertContactSubscription(obj, method, rawParams, ctx);
        }
        return null;
    }

    // armory.trait.physics.oimo.RigidBody or armory.trait.physics.bullet.RigidBody
    static function isRigidBody(path:String):Bool {
        return StringTools.startsWith(path, "armory.trait.physics.") && StringTools.endsWith(path, ".RigidBody");
    }

    function convertContactSubscription(receiver:Expr, method:String, params:Array<Expr>, ctx:IExtractorContext):IRNode {
        if (ctx.isAutoload()) return N64MacroBase.unsupported('$method() is only supported in traits on N64');
        if (params.length == 0) return N64MacroBase.unsupported('$method() needs a handler method');

        var handlerName = ExprUtils.extractIdentName(params[0]);
        var func = handlerName != null ? ctx.getMethod(handlerName) : null;
        if (func == null) return N64MacroBase.unsupported('$method() handler must be a method of this trait on N64');

        var isSubscribe = (method == "notifyOnContact");
        var fullHandlerName = ctx.getCName() + "_contact_" + handlerName;
        // The handler's RigidBody parameter (the other body), named as in Haxe
        var paramName = func.args.length > 0 ? func.args[0].name : "_other";

        // Counted by limits.py (bodies with handlers, handlers per body)
        ctx.getMeta().contact_events.push({
            event_name: handlerName,
            handler_name: fullHandlerName,
            subscribe: isSubscribe,
            param_name: paramName
        });

        // The handler, extracted once (removeContact names it too)
        var events = ctx.getEvents();
        var eventKey = "contact_" + handlerName;
        if (!events.exists(eventKey)) {
            events.set(eventKey, []);
            extractContactHandler(func, events.get(eventKey), ctx);
        }

        // On the receiver's body, with this trait's object and data, where the code calls it
        var cFunction = isSubscribe ? "physics_contact_subscribe" : "physics_contact_unsubscribe";
        return {
            type: "c_template",
            c_code: '$cFunction({self}->rigid_body, $fullHandlerName, obj, data)',
            args: [],
            object: ctx.exprToIR(receiver)
        };
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