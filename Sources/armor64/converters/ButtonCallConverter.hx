package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.N64MacroBase;
import armor64.converters.ICallConverter;

/**
 * Button Call Converter
 *
 * Handles ButtonExt static-extension method calls on Button objects:
 * - button.onHover(fn)    -> nothing (no mouse on N64, as on Krom with a gamepad)
 * - button.onFocus(fn)    -> button_register_callback (on_focus)
 * - button.onPressed(fn)  -> button_register_callback (on_pressed)
 * - button.onReleased(fn) -> button_register_callback (on_released)
 *
 * Each callback body is extracted from the anonymous function argument,
 * converted to IR, and wrapped in a button_register_callback node.
 * The trait_generator produces static C callback functions from these nodes.
 */
class ButtonCallConverter implements ICallConverter {

    var _callbackCounter:Int = 0;

    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        var objType = ctx.getExprType(obj);
        if (objType != "Button") {
            return null;
        }

        return convertButtonCall(obj, method, args, rawParams, ctx);
    }

    function convertButtonCall(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        // Map ButtonExt methods to N64 callback events
        // grabFocus() is a simple call, not a callback registration
        if (method == "grabFocus") {
            var objIR = ctx.exprToIR(obj);
            return {
                type: "button_grab_focus",
                object: objIR
            };
        }

        var eventType:String = null;
        switch (method) {
            // Hovering needs a mouse: never, as on Krom with a gamepad
            case "onHover":   return { type: "skip" };
            case "onFocus":   eventType = "on_focus";
            case "onPressed": eventType = "on_pressed";
            case "onReleased": eventType = "on_released";
            default:
                return null;
        }

        if (rawParams.length < 1) {
            return N64MacroBase.unsupported("Button." + method + "() needs a callback argument");
        }

        // Extract callback body from the anonymous function argument
        var bodyNodes:Array<IRNode> = [];
        switch (rawParams[0].expr) {
            case EFunction(_, func):
                if (func.expr != null) {
                    switch (func.expr.expr) {
                        case EBlock(exprs):
                            for (e in exprs) {
                                var node = ctx.exprToIR(e);
                                if (N64MacroBase.keepStatement(node)) {
                                    bodyNodes.push(node);
                                }
                            }
                        default:
                            var node = ctx.exprToIR(func.expr);
                            if (N64MacroBase.keepStatement(node)) {
                                bodyNodes.push(node);
                            }
                    }
                }
            default:
                return N64MacroBase.unsupported("Button." + method + "() callback must be an anonymous function on N64");
        }

        // Generate unique callback name
        var buttonVar = extractButtonVarName(obj);
        var callbackName = ctx.getCName() + "_btn_" + buttonVar + "_" + eventType + "_" + _callbackCounter;
        _callbackCounter++;

        // Emit object IR for the button variable reference
        var objIR = ctx.exprToIR(obj);

        return {
            type: "button_register_callback",
            props: {
                event: eventType,
                callback_name: callbackName
            },
            object: objIR,
            body: bodyNodes
        };
    }

    function extractButtonVarName(e:Expr):String {
        switch (e.expr) {
            case EConst(CIdent(name)):
                return name.toLowerCase();
            case EField(_, field):
                return field.toLowerCase();
            default:
                return "unknown";
        }
    }
}
#end
