package armor64;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.converters.ICallConverter;

/**
 * How a binding's {self} is found from the call's receiver.
 */
enum BindingReceiver {
    None;     // A static call: no {self}
    Value;    // The receiver itself, e.g. the vector in `v.length()`
}

/**
 * A Haxe method that maps straight to C.
 *
 * c: the C template. Placeholders:
 *   {0}, {1}, ...   the call's arguments
 *   {1=1.0f}        argument 1, or 1.0f when the call leaves it out
 *   {self}          the receiver (see BindingReceiver)
 * Any other {name} is an error: write C braces with a space (`{ x }`).
 * An argument used more than once is evaluated once, into a temporary,
 * unless it's a plain name or literal (emit_c_template in trait_emitter.py).
 * A template ending with `;` or `}` is a statement, otherwise an expression.
 */
typedef Binding = {
    c:String,
    ?receiver:BindingReceiver,  // Default None
    ?flags:Array<String>,       // TraitMeta flags the call sets (uses_physics, ...)
}

/**
 * Haxe API -> C, for the calls that map straight to a C expression or
 * statement: one entry per method, keyed "Class.method" by the class (or
 * abstract) declaring it, from the typed AST. So aliases, imports, full
 * paths and inherited methods all match.
 *
 * Calls needing logic (callbacks, export-time lookups, generated code) stay
 * converters (converters/).
 */
class Bindings {
    static var calls:Map<String, Binding> = [
        // Math: libdragon's fm_* where it has one (fm_sinf: ~50 ticks vs ~800 for sinf)
        "Math.sin" => {c: "fm_sinf({0})"},
        "Math.cos" => {c: "fm_cosf({0})"},
        "Math.tan" => {c: "tanf({0})"},
        "Math.asin" => {c: "asinf({0})"},
        "Math.acos" => {c: "acosf({0})"},
        "Math.atan" => {c: "atanf({0})"},
        "Math.atan2" => {c: "fm_atan2f({0}, {1})"},
        "Math.sqrt" => {c: "sqrtf({0})"},
        "Math.pow" => {c: "powf({0}, {1})"},
        "Math.abs" => {c: "fabsf({0})"},
        "Math.floor" => {c: "fm_floorf({0})"},
        "Math.ceil" => {c: "ceilf({0})"},
        "Math.round" => {c: "roundf({0})"},
        "Math.min" => {c: "fminf({0}, {1})"},
        "Math.max" => {c: "fmaxf({0}, {1})"},
        "Math.exp" => {c: "fm_exp({0})"},  // ~3% error
        "Math.log" => {c: "logf({0})"},
    ];

    static var placeholder = ~/\{([A-Za-z0-9_]+)(=[^{}]*)?\}/;

    /**
     * The c_template node for `obj.method(args)` if a binding maps it (null
     * if none does). `owner` is the class declaring the method.
     */
    public static function convert(owner:String, method:String, obj:Expr, args:Array<IRNode>,
                                   ctx:IExtractorContext):Null<IRNode> {
        if (owner == null) return null;
        var b = calls.get('$owner.$method');
        if (b == null) return null;

        var shortName = owner.substr(owner.lastIndexOf(".") + 1);
        var receiver = b.receiver != null ? b.receiver : None;
        var problem = check(b, receiver, args.length);
        if (problem != null) return N64MacroBase.unsupported('$shortName.$method(): $problem');

        if (b.flags != null) {
            var meta = ctx.getMeta();
            for (f in b.flags) Reflect.setField(meta, f, true);
        }
        var node:IRNode = {type: "c_template", c_code: b.c, args: args};
        if (receiver == Value) node.object = ctx.exprToIR(obj);
        return node;
    }

    // What's wrong with using the binding for a call with `argCount` arguments (null: nothing)
    static function check(b:Binding, receiver:BindingReceiver, argCount:Int):Null<String> {
        var rest = b.c;
        while (placeholder.match(rest)) {
            var name = placeholder.matched(1);
            var hasDefault = placeholder.matched(2) != null;
            rest = placeholder.matchedRight();
            var index = Std.parseInt(name);
            if (index != null) {
                if (index >= argCount && !hasDefault) return 'needs at least ${index + 1} argument(s) on N64';
            } else if (name == "self") {
                if (receiver == None) return 'binding error: {self} in a static binding';
            } else {
                return 'binding error: unknown placeholder {$name} (write C braces with a space)';
            }
        }
        return null;
    }
}
#end
