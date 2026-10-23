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

    // Static extensions (`using`) of the class being extracted
    public static var currentUsings:Array<ClassType> = [];

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
     * Type of an expression as a short Haxe type name, in the format the
     * extractors and TypeMap use (e.g. "Vec4", "Map<String, Array<Int>>").
     * Null if the expression has no typed node or its type isn't known.
     */
    public static function typeOf(e:Expr):Null<String> {
        // Outermost node: the expression itself (see classOf)
        var list = nodesAt(e);
        return list.length > 0 ? typeName(list[0].t) : null;
    }

    /**
     * Short name of a type with its parameters. Typedef names are kept
     * (FastFloat stays FastFloat) and Null<T> is unwrapped to T.
     */
    public static function typeName(t:Type):Null<String> {
        return switch (t) {
            case TMono(ref): ref.get() != null ? typeName(ref.get()) : null;
            case TLazy(f): typeName(f());
            case TInst(c, params): withParams(c.get().name, params);
            case TEnum(en, params): withParams(en.get().name, params);
            case TType(td, params): withParams(td.get().name, params);
            case TAbstract(a, params):
                var ab = a.get();
                if (ab.pack.length == 0 && ab.name == "Null" && params.length == 1) typeName(params[0]);
                else withParams(ab.name, params);
            case TFun(args, ret):
                var names = [for (arg in args) typeName(arg.t)];
                var retName = typeName(ret);
                if (names.indexOf(null) >= 0 || retName == null) null;
                else (names.length == 0 ? "Void" : names.join("->")) + "->" + retName;
            case TDynamic(_), TAnonymous(_): "Dynamic";
        };
    }

    static function withParams(name:String, params:Array<Type>):Null<String> {
        if (params.length == 0) return name;
        var names = [for (p in params) typeName(p)];
        if (names.indexOf(null) >= 0) return null;
        return name + "<" + names.join(", ") + ">";
    }

    /**
     * Full path of the class (or abstract) declaring the method called by
     * `obj.method(...)`, e.g. "iron.object.Transform" for
     * `object.transform.rotate(...)`. Found from the receiver's typed node, so
     * it works for inlined calls too (they keep no typed call node).
     * Null if the receiver has no typed node or the method isn't found.
     */
    public static function callOwner(obj:Expr, method:String):Null<String> {
        var list = nodesAt(obj);
        if (list.length == 0) return null;

        var owner = switch (list[0].expr) {
            // Static call: ClassName.method()
            case TTypeExpr(TClassDecl(c)):
                switch (c.get().kind) {
                    case KAbstractImpl(a): abstractOwner(a.get(), method);
                    default: declaringClass(c.get(), method, true);
                }
            case TTypeExpr(TAbstract(a)): abstractOwner(a.get(), method);
            default: ownerFromType(list[0].t, method);
        };
        if (owner != null) return owner;

        // Static extension: `using Ext;` then value.method()
        for (u in currentUsings) {
            owner = declaringClass(u, method, true);
            if (owner != null) return owner;
        }
        return null;
    }

    /**
     * Full path of what a call receiver is: its class or abstract type
     * (e.g. "iron.system.Gamepad" for `gamepad` in `gamepad.started("a")`),
     * or the class itself for static calls (`Scene` in `Scene.setActive()`).
     * Null if the receiver has no typed node or isn't a class/abstract.
     */
    public static function receiverPath(obj:Expr):Null<String> {
        var list = nodesAt(obj);
        if (list.length == 0) return null;
        return switch (list[0].expr) {
            case TTypeExpr(TClassDecl(c)): classPath(c.get());
            case TTypeExpr(TAbstract(a)): fullPath(a.get().pack, a.get().name);
            default:
                switch (Context.follow(list[0].t)) {
                    case TInst(c, _): fullPath(c.get().pack, c.get().name);
                    case TAbstract(a, _): fullPath(a.get().pack, a.get().name);
                    default: null;
                }
        };
    }

    static function ownerFromType(t:Type, method:String):Null<String> {
        return switch (Context.follow(t)) {
            case TInst(c, _): declaringClass(c.get(), method, false);
            case TAbstract(a, _): abstractOwner(a.get(), method);
            default: null;
        };
    }

    // Abstract methods are statics of the implementation class
    static function abstractOwner(a:AbstractType, method:String):Null<String> {
        if (a.impl == null) return null;
        for (f in a.impl.get().statics.get()) {
            if (f.name == method) return fullPath(a.pack, a.name);
        }
        return null;
    }

    // Class declaring `name`, searching parent classes for instance fields
    static function declaringClass(c:ClassType, name:String, isStatic:Bool):Null<String> {
        while (c != null) {
            var fields = isStatic ? c.statics.get() : c.fields.get();
            for (f in fields) {
                if (f.name == name) return fullPath(c.pack, c.name);
            }
            if (isStatic || c.superClass == null) return null;
            c = c.superClass.t.get();
        }
        return null;
    }

    // Static access on an abstract is typed through its implementation class
    // (kha._Color.Color_Impl_ for kha.Color): name the abstract instead
    static function classPath(c:ClassType):String {
        return switch (c.kind) {
            case KAbstractImpl(a): fullPath(a.get().pack, a.get().name);
            default: fullPath(c.pack, c.name);
        };
    }

    static function fullPath(pack:Array<String>, name:String):String {
        return pack.concat([name]).join(".");
    }

    /**
     * Whether an expression refers to the class or abstract `path` as a type
     * (e.g. "iron.Scene" for `Scene`, `iron.Scene` or an import alias).
     * Without a typed node, falls back to comparing the class's short name.
     */
    public static function isClassRef(e:Expr, path:String):Bool {
        var list = nodesAt(e);
        if (list.length > 0) {
            return switch (list[0].expr) {
                case TTypeExpr(TClassDecl(c)): classPath(c.get()) == path;
                case TTypeExpr(TAbstract(a)): fullPath(a.get().pack, a.get().name) == path;
                default: false;
            };
        }
        var shortName = path.substr(path.lastIndexOf(".") + 1);
        return switch (e.expr) {
            case EConst(CIdent(name)): name == shortName;
            default: false;
        };
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
