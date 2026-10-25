package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.TypedIndex;
import armor64.N64MacroBase;
import armor64.converters.ICallConverter;

/**
 * Converts StringTools.* calls to C equivalents.
 * Handles: StringTools.startsWith(), StringTools.endsWith(), StringTools.contains()
 *
 * Haxe usage:
 *   StringTools.startsWith(str, prefix)  -> strncmp(str, prefix, strlen(prefix)) == 0
 *   StringTools.endsWith(str, suffix)    -> str_ends_with(str, suffix)
 *   StringTools.contains(str, sub)       -> strstr(str, sub) != NULL
 */
class StringCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        switch (obj.expr) {
            case _ if (TypedIndex.isClassRef(obj, "StringTools")):
                return convert(method, args);
            default:
                return null;
        }
    }

    function convert(method:String, args:Array<IRNode>):IRNode {
        return switch (method) {
            case "startsWith":
                { type: "string_call", value: "starts_with", args: args };
            case "endsWith":
                { type: "string_call", value: "ends_with", args: args };
            case "contains":
                { type: "string_call", value: "contains", args: args };
            default:
                N64MacroBase.unsupported('StringTools.$method() is not supported on N64 yet');
        };
    }
}

#end
