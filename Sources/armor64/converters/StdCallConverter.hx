package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.TypedIndex;
import armor64.N64MacroBase;
import armor64.converters.ICallConverter;

/**
 * Std.string(), whose format depends on the value's type. Std.int() and
 * Std.parseFloat() are in Bindings.hx.
 */
class StdCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        switch (obj.expr) {
            case _ if (TypedIndex.isClassRef(obj, "Std")):
                return convert(method, args);
            default:
                return null;
        }
    }

    function convert(method:String, args:Array<IRNode>):IRNode {
        return switch (method) {
            case "string":
                // Std.string(value) - emit sprintf with format based on arg type
                if (args.length > 0) {
                    var argType = args[0].type;
                    var format = switch (argType) {
                        case "float": "%.2f";
                        case "int": "%d";
                        case "member": "%d";
                        default: "%d";
                    };
                    { type: "sprintf", value: format, args: args };
                } else {
                    { type: "string", value: "" };
                }
            default:
                null;
        };
    }
}

#end