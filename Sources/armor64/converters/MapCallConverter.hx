package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.mapping.TypeMap;
import armor64.converters.ICallConverter;

/**
 * Converts Haxe Map operations to C arm_map functions. The map itself is the
 * node's `object` (any expression: member, local, autoload field...).
 *
 * Handles:
 *   map.set(key, value) -> map_type_set(&map, key, value)
 *   map.get(key)        -> map_type_value(&map, key), or map_type_get (a
 *                          pointer) when the values are containers
 *   map.exists(key)     -> map_type_exists(&map, key)
 *   map.remove(key)     -> map_type_remove(&map, key)
 *   map.clear()         -> map_type_clear(&map)
 * (map[key] is converted with array access)
 */
class MapCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        var objType = ctx.getExprType(obj);
        if (!TypeMap.isMapType(objType)) {
            return null;
        }

        if (method != "clear" && args.length > 0) N64MacroBase.recordMapKey(TypeMap.getCType(objType), args[0], method == "set");
        return switch (method) {
            case "set", "get", "exists", "remove", "clear":
                {
                    type: method == "get" ? TypeMap.mapReadNode(objType) : "map_" + method,
                    value: TypeMap.getCType(objType),
                    object: ctx.exprToIR(obj),
                    children: args
                };
            default:
                null;
        };
    }
}

#end
