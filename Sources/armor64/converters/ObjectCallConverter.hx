package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.N64MacroBase;
import armor64.converters.ICallConverter;
import armor64.util.ExprUtils;

/**
 * object.getTrait() and the other trait/children calls N64 doesn't have (or
 * has built in). remove() and getAnimation() are in Bindings.hx.
 */
class ObjectCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        switch (obj.expr) {
            case EConst(CIdent("object")), EConst(CIdent("this")):
                return convert(method, args, rawParams);
            default:
                return null;
        }
    }

    // Engine traits with a built-in N64 equivalent: physics always applies to the
    // trait's own object and there is a single global UI canvas
    static var builtinTraits = ["RigidBody", "KouiCanvas"];

    function convert(method:String, args:Array<IRNode>, rawParams:Array<Expr>):IRNode {
        return switch (method) {
            case "getTrait" if (rawParams.length > 0 && Lambda.has(builtinTraits, ExprUtils.extractIdentName(rawParams[0]))):
                { type: "skip" };
            case "getTrait", "addTrait", "removeTrait":
                // Trait system not supported on N64
                N64MacroBase.unsupported(method + "() is not supported on N64");
            case "getChildren", "getChild":
                // Children iteration not yet supported (hierarchy is flat array with parent_index)
                N64MacroBase.unsupported(method + "() is not supported on N64 yet");
            default:
                null;
        };
    }
}

#end