package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.mapping.TypeMap;
import armor64.converters.ICallConverter;

/**
 * Converts Haxe Array operations to C arm_array functions. The array itself
 * is the node's `object` (any expression: member, local, autoload field...).
 *
 * Handles:
 *   array.push(item)    -> arraytype_push(&array, item)
 *   array.pop()         -> arraytype_pop(&array)
 *   array.resize(0)     -> arraytype_clear(&array)
 * (array.length and array[i] are converted with fields and array access)
 */
class ArrayCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        var objType = ctx.getExprType(obj);
        if (!TypeMap.isArrayType(objType)) {
            return null;
        }

        var type = switch (method) {
            case "push": "array_push";
            case "pop": "array_pop";
            case "resize" if (args.length == 1 && args[0].type == "int" && args[0].value == 0): "array_clear";
            default: return null;
        };
        return {
            type: type,
            value: TypeMap.getCType(objType),
            object: ctx.exprToIR(obj),
            children: type == "array_push" ? args : null
        };
    }
}

#end
