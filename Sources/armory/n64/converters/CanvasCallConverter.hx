package armory.n64.converters;

#if macro
import haxe.macro.Expr;
import armory.n64.IRTypes;
import armory.n64.converters.ICallConverter;
import armory.n64.util.ExprUtils;

/**
 * Canvas Call Converter
 *
 * Handles KouiCanvas method calls:
 * - getElementAs(Label, "key") -> canvas_get_label IR
 * - notifyOnReady(callback) -> flatten callback inline (N64 loads synchronously)
 */
class CanvasCallConverter implements ICallConverter {

    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        // Check if object is KouiCanvas type
        var objType = ctx.getExprType(obj);
        if (objType != "KouiCanvas") {
            return null;
        }

        return convertCanvasCall(method, args, rawParams, ctx);
    }

    function resolveElementIR(elementType:String, elemKey:String, ctx:IExtractorContext):IRNode {
        if (elemKey == null) return null;

        ctx.getMeta().uses_ui = true;

        if (elementType == "Button") {
            return { type: "canvas_get_button", props: { key: elemKey } };
        }
        if (elementType == "Label") {
            return { type: "canvas_get_label", props: { key: elemKey } };
        }
        if (elementType == "AnchorPane" || elementType == "RowLayout" ||
            elementType == "ColLayout" || elementType == "GridLayout") {
            return { type: "canvas_get_group", props: { key: elemKey } };
        }
        return null;
    }

    function convertCanvasCall(method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        if (method == "getElementAs" && rawParams.length >= 2) {
            // canvas.getElementAs(Label, "score_label")
            // canvas.getElementAs(AnchorPane, "level_container")
            var elementType = ExprUtils.extractIdentName(rawParams[0]);
            var elemKey = ExprUtils.extractString(rawParams[1]);
            var result = resolveElementIR(elementType, elemKey, ctx);
            if (result != null) return result;
        }
        else if (method == "getElementFromSceneAs" && rawParams.length >= 3) {
            // canvas.getElementFromSceneAs(Button, "Paused", "buttons/menu_button")
            // Prefix with scene name so keys are scene-scoped (matching ui_exporter):
            //   "Paused" + "buttons/menu_button" -> "Paused/buttons/menu_button"
            var elementType = ExprUtils.extractIdentName(rawParams[0]);
            var sceneName = ExprUtils.extractString(rawParams[1]);
            var elemPath = ExprUtils.extractString(rawParams[2]);
            var fullKey = sceneName != null ? sceneName + "/" + elemPath : elemPath;
            var result = resolveElementIR(elementType, fullKey, ctx);
            if (result != null) return result;
        }
        else if (method == "setScene" && rawParams.length >= 1) {
            // canvas.setScene("Info") -> canvas_set_scene("Info")
            var sceneName = ExprUtils.extractString(rawParams[0]);
            if (sceneName != null) {
                ctx.getMeta().uses_ui = true;
                return {
                    type: "canvas_set_scene",
                    props: { scene: sceneName }
                };
            }
        }
        else if (method == "notifyOnReady" && rawParams.length >= 1) {
            // canvas.notifyOnReady(function() { ... }) or canvas.notifyOnReady(methodName)
            // On N64, canvas is always ready (sync loading), so flatten callback inline
            switch (rawParams[0].expr) {
                case EFunction(_, func):
                    // Inline anonymous function
                    if (func.expr != null) {
                        return ctx.exprToIR(func.expr);
                    }
                case EConst(CIdent(methodName)):
                    // Method reference: canvas.notifyOnReady(onCanvasReady)
                    var methodFunc = ctx.getMethod(methodName);
                    if (methodFunc != null && methodFunc.expr != null) {
                        return ctx.exprToIR(methodFunc.expr);
                    }
                default:
            }
        }
        // Unsupported canvas method
        return { type: "skip", warn: "KouiCanvas." + method + "() not yet supported on N64" };
    }
}
#end
