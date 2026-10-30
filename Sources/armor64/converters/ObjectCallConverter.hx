package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.N64MacroBase;
import armor64.TypedIndex;
import armor64.converters.ICallConverter;

/**
 * object.getTrait() and the other trait/children calls N64 doesn't have (or
 * has built in). remove() and getAnimation() are in Bindings.hx.
 */
class ObjectCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        switch (obj.expr) {
            case EConst(CIdent("object")), EConst(CIdent("this")):
                return convert(obj, method, rawParams, ctx);
            default:
                return TypedIndex.receiverPath(obj) == "iron.object.Object" ? convert(obj, method, rawParams, ctx) : null;
        }
    }

    function convert(obj:Expr, method:String, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        // The trait class asked for (getTrait(RigidBody)), from the typed AST
        var traitPath = rawParams.length > 0 ? TypedIndex.typeRefPath(rawParams[0]) : null;
        return switch (method) {
            // A RigidBody is its object's pointer: the object if it has a body, else null
            case "getTrait" if (traitPath != null && StringTools.startsWith(traitPath, "armory.trait.physics.") && StringTools.endsWith(traitPath, ".RigidBody")):
                { type: "c_template", c_code: "arm_rigid_body_of({self})", args: [], object: ctx.exprToIR(obj) };
            // There is a single global UI canvas
            case "getTrait" if (traitPath == "armory.trait.internal.KouiCanvas"):
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