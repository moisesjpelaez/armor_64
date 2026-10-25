package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.N64MacroBase;
import armor64.TypedIndex;
import armor64.converters.ICallConverter;

/**
 * Converts kha.graphics2.Graphics calls to N64 render2d functions.
 * Supports: g2.fillRect, g2.drawRect, g2.color
 */
class Graphics2CallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        // Only Kha's 2D graphics, from the receiver's type
        if (TypedIndex.receiverPath(obj) != "kha.graphics2.Graphics") return null;
        return convertGraphics2Call(method, args, rawParams, ctx);
    }

    function convertGraphics2Call(method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        switch (method) {
            case "fillRect":
                if (args.length >= 4) {
                    return {
                        type: "render2d_fill_rect",
                        props: {
                            x: args[0],
                            y: args[1],
                            width: args[2],
                            height: args[3]
                        }
                    };
                }
            case "begin", "end":
                // Kha batching calls, nothing to do with rdpq on N64
                return { type: "skip" };
            default:
                return N64MacroBase.unsupported('Graphics.$method() is not supported on N64 yet');
        }
        return null;
    }
}
#end
