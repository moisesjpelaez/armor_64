package armory.n64.converters;

#if macro
import haxe.macro.Expr;
import armory.n64.IRTypes;
import armory.n64.converters.ICallConverter;

/**
 * Button Call Converter
 *
 * Handles ButtonExt static-extension method calls on Button objects:
 * - button.onHover(fn)    -> button_register_callback (on_focus on N64, no mouse hover)
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
        // N64 has no mouse, so onHover and onFocus both map to on_focus (gamepad focus ≈ hover).
        // If both are registered on the same button, the second overwrites the first since
        // the C struct has a single on_focus slot. This is intentional — user code typically
        // registers both with the same body (e.g. buttonSelected) for Krom+N64 compatibility.
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
            case "onHover":   eventType = "on_focus";
            case "onFocus":   eventType = "on_focus";
            case "onPressed": eventType = "on_pressed";
            case "onReleased": eventType = "on_released";
            default:
                return null;
        }

        if (rawParams.length < 1) {
            return { type: "skip", warn: "Button." + method + "() missing callback argument" };
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
                                if (node != null && node.type != "skip") {
                                    bodyNodes.push(node);
                                }
                            }
                        default:
                            var node = ctx.exprToIR(func.expr);
                            if (node != null && node.type != "skip") {
                                bodyNodes.push(node);
                            }
                    }
                }
            default:
                return { type: "skip", warn: "Button." + method + "() expected anonymous function" };
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
