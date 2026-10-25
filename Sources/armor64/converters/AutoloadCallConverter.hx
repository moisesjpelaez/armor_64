package armor64.converters;

#if macro
import haxe.macro.Expr;
import haxe.macro.Context;
import haxe.macro.Type;
import armor64.IRTypes;
import armor64.TypedIndex;
import armor64.converters.ICallConverter;

using haxe.macro.TypeTools;
using StringTools;

/**
 * Converts calls to autoload classes (static functions and variables).
 *
 * Handles patterns like:
 *   GameEvents.resetScore() -> gameevents_resetScore()
 *   GameEvents.score -> gameevents_score
 *   GameEvents.score = 10 -> gameevents_score = 10
 *
 * Autoload classes are detected by checking for @:n64Autoload metadata.
 */
class AutoloadCallConverter implements ICallConverter {
    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        // Check if this is a static call on an autoload class: ClassName.method()
        switch (obj.expr) {
            case EConst(CIdent(_)):
                // Check if the identifier refers to an autoload class
                var cName = getAutoloadCName(obj);
                if (cName != null) {
                    return convertAutoloadCall(cName, method, args, ctx);
                }
            default:
        }
        return null;
    }

    function convertAutoloadCall(cName:String, method:String, args:Array<IRNode>, ctx:IExtractorContext):IRNode {
        ctx.useAutoload(cName);
        // Generate: autoload_method(args...)
        return {
            type: "autoload_call",
            value: method,
            c_code: '${cName}_${method}',
            args: args,
            props: {
                c_name: cName,
                method: method
            }
        };
    }

    /**
     * C name of the autoload class an expression refers to (e.g. `GameEvents`
     * in `GameEvents.levelWon.emit()`), or null if it isn't an autoload.
     */
    public static function getAutoloadCName(e:Expr):String {
        var classType = TypedIndex.classOf(e);
        if (classType == null) return null;
        if (classType.meta.has(":n64Autoload") || classType.meta.has("n64Autoload")) {
            return classType.name.toLowerCase();
        }
        return null;
    }
}
#end
