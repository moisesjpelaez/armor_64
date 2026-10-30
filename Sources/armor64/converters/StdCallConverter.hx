package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.TypedIndex;
import armor64.StringFormatter;
import armor64.converters.ICallConverter;

/**
 * Std.string(): the value's text as Haxe writes it (StringFormatter).
 * Std.int() and Std.parseFloat() are in Bindings.hx.
 */
class StdCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        if (method != "string" || rawParams.length != 1 || !TypedIndex.isClassRef(obj, "Std")) return null;
        return StringFormatter.format(rawParams[0], ctx.exprToIR);
    }
}

#end
