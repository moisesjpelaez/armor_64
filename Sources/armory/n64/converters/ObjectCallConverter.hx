package armory.n64.converters;

#if macro
import haxe.macro.Expr;
import armory.n64.IRTypes;
import armory.n64.converters.ICallConverter;

/**
 * Converts object lifecycle method calls.
 * Handles: object.remove(), object.getTrait(), object.parent, this.remove(), etc.
 */
class ObjectCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        switch (obj.expr) {
            case EConst(CIdent("object")), EConst(CIdent("this")):
                return convert(method, args);
            default:
                return null;
        }
    }

    function convert(method:String, args:Array<IRNode>):IRNode {
        return switch (method) {
            case "remove":
                // object.remove() -> object_remove((ArmObject*)obj)
                { type: "object_call", c_code: "object_remove((ArmObject*)obj)" };
            case "getTrait", "addTrait", "removeTrait":
                // Trait system not supported on N64
                { type: "skip", warn: method + "() not supported on N64" };
            case "getChildren", "getChild":
                // Children iteration not yet supported (hierarchy is flat array with parent_index)
                { type: "skip", warn: method + "() not yet supported on N64 - use parent_index-based iteration" };
            case "getAnimation":
                // object.getAnimation() -> object->animation
                { type: "object_call", c_code: '((ArmObject*)obj)->animation' };
            default:
                // Unknown object method
                { type: "skip", warn: "object." + method + "() not supported on N64" };
        };
    }
}

#end