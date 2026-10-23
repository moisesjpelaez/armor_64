package armory.n64;

#if macro
import haxe.macro.Context;
import haxe.macro.Expr;
import haxe.macro.Type;

using haxe.macro.TypedExprTools;

/**
 * Typed Index
 *
 * Maps source positions of user code to the compiler's typed AST, so the
 * extractors (which read the untyped syntax) can ask what an expression
 * really is instead of guessing from names.
 *
 * Built after typing (see N64MacroBase.afterTyping). Inlined calls keep no
 * typed node of their own, so resolve calls from their receiver instead.
 */
class TypedIndex {
    // "file:min:max" -> typed nodes at that exact position, outermost first
    static var nodes:Map<String, Array<TypedExpr>> = new Map();

    static function key(pos:Position):String {
        var infos = Context.getPosInfos(pos);
        return infos.file + ":" + infos.min + ":" + infos.max;
    }

    public static function addClass(c:ClassType):Void {
        if (c.constructor != null) addExpr(c.constructor.get().expr());
        for (f in c.fields.get()) addExpr(f.expr());
        for (f in c.statics.get()) addExpr(f.expr());
    }

    static function addExpr(t:TypedExpr):Void {
        if (t == null) return;
        var k = key(t.pos);
        var list = nodes.get(k);
        if (list == null) nodes.set(k, list = []);
        list.push(t);
        t.iter(addExpr);
    }

    /**
     * Typed nodes at the exact source position of an untyped expression.
     */
    public static function nodesAt(e:Expr):Array<TypedExpr> {
        var list = nodes.get(key(e.pos));
        return list != null ? list : [];
    }

    /**
     * The class an expression refers to as a type, e.g. `GameEvents` in
     * `GameEvents.levelWon.emit()`. Null if it isn't a class reference.
     */
    public static function classOf(e:Expr):Null<ClassType> {
        // Only the outermost node is the expression itself: a bare static field
        // `tween` is typed as `Music.tween`, with a synthetic `Music` type
        // expression at the same position
        var list = nodesAt(e);
        if (list.length == 0) return null;
        return switch (list[0].expr) {
            case TTypeExpr(TClassDecl(c)): c.get();
            default: null;
        };
    }
}
#end
