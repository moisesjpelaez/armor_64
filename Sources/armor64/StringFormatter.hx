package armor64;

#if macro
import haxe.macro.Context;
import haxe.macro.Expr;
import haxe.macro.MacroStringTools;
import haxe.macro.Type;
import armor64.IRTypes;

/**
 * Strings built as Haxe builds them: `+` concatenation, string interpolation,
 * Std.string() and trace(). Each part is written by its compiler type, as
 * Haxe on Krom writes it (runtime system/str.c): Ints, Floats (the fewest
 * digits that read back), Bools ("true"/"false"), Strings (null is "null").
 *
 * The result is a str_format node (parts appended to a rotating buffer), a
 * plain string node when every part is constant.
 */
class StringFormatter {
    // Most UTF-8 bytes of a part's text (str.c)
    static final INT_LENGTH = 11;    // -2147483648
    static final FLOAT_LENGTH = 22;  // -100000000000000000000
    static final BOOL_LENGTH = 5;    // false

    var exprToIR:Expr->IRNode;
    var kinds:Array<String> = [];  // text, int, float, bool, str
    var args:Array<IRNode> = [];

    public function new(exprToIR:Expr->IRNode) {
        this.exprToIR = exprToIR;
    }

    public function addText(text:String):Void {
        if (text.length == 0) return;
        if (kinds.length > 0 && kinds[kinds.length - 1] == "text") {
            var last = args[args.length - 1];
            args[args.length - 1] = { type: "string", value: last.value + text };
            return;
        }
        kinds.push("text");
        args.push({ type: "string", value: text });
    }

    /** A value already converted, of kind int, float, bool or str. */
    public function addValue(ir:IRNode, kind:String):Void {
        if (kind == "str" && ir.type == "string") {
            addText(ir.value);
            return;
        }
        kinds.push(kind);
        args.push(ir);
    }

    /** `e` as text; a String concatenation, interpolation or Std.string() is flattened. */
    public function add(e:Expr):Void {
        switch (e.expr) {
            case EConst(CString(s, SingleQuotes)) if (s.indexOf("$") >= 0):
                add(MacroStringTools.formatString(s, e.pos));
            case EConst(CString(s)):
                addText(s);
            case EConst(CInt(v)) if (~/^[0-9]+$/.match(v)):
                addText(Std.string(Std.parseInt(v)));
            case EConst(CIdent(name = "true" | "false" | "null")):
                addText(name);
            case EParenthesis(inner):
                add(inner);
            case EBinop(OpAdd, left, right) if (kindOf(e) == "str"):
                add(left);
                add(right);
            case ECall({expr: EField(obj, "string")}, [value]) if (TypedIndex.isClassRef(obj, "Std")):
                add(value);
            default:
                var kind = kindOf(e);
                if (kind == null) {
                    var t = valueType(e);
                    var type = t != null ? TypedIndex.typeName(t) : null;
                    N64MacroBase.unsupported('a ${type != null ? type : "value of unknown type"} as a String is not supported on N64 yet', e.pos);
                    return;
                }
                addValue(exprToIR(e), kind);
        }
    }

    public function build():IRNode {
        if (kinds.length == 0) return { type: "string", value: "" };
        if (kinds.length == 1 && kinds[0] == "text") return args[0];
        // A String alone needs no copy
        if (kinds.length == 1 && kinds[0] == "str") {
            var node:IRNode = { type: "c_template", c_code: "str_or_null({0})", args: [args[0]] };
            node.maxLength = args[0].maxLength;
            return node;
        }
        var node:IRNode = { type: "str_format", args: args, props: { kinds: kinds } };
        node.maxLength = maxLength();
        N64MacroBase.recordConcatLength(node.maxLength);
        return node;
    }

    // Most UTF-8 bytes of the text, null if unknown (a String that isn't constant)
    function maxLength():Null<Int> {
        var length = 0;
        for (i in 0...kinds.length) {
            var part:Null<Int> = switch (kinds[i]) {
                case "text": N64MacroBase.utf8Length(args[i].value);
                case "int": INT_LENGTH;
                case "float": FLOAT_LENGTH;
                case "bool": BOOL_LENGTH;
                default: args[i].maxLength;
            };
            if (part == null) return null;
            length += part;
        }
        return length;
    }

    /** How a value of `e`'s compiler type is written, null if it can't be. */
    public static function kindOf(e:Expr):Null<String> {
        var t = valueType(e);
        if (t == null) return null;
        return switch (Context.followWithAbstracts(t)) {
            case TAbstract(_.get() => {pack: [], name: "Int"}, _): "int";
            case TAbstract(_.get() => {pack: [], name: "Float"}, _): "float";
            case TAbstract(_.get() => {pack: [], name: "Bool"}, _): "bool";
            case TInst(_.get() => {pack: [], name: "String"}, _): "str";
            // The N64 build types for C++: FastFloat is cpp.Float32
            case TAbstract(_.get() => {pack: ["cpp"], name: "Float32" | "Float64"}, _): "float";
            case TAbstract(_.get() => {pack: ["cpp"], name: "Int8" | "Int16" | "Int32" | "UInt8" | "UInt16"}, _): "int";
            default: null;
        };
    }

    // In a concatenation the compiler wraps a value that isn't a basic type in
    // Std.string() (at the value's position): the value's own type
    static function valueType(e:Expr):Null<Type> {
        switch (e.expr) {
            case ECall(_):
            default:
                var nodes = TypedIndex.nodesAt(e);
                if (nodes.length > 0) switch (nodes[0].expr) {
                    case TCall({expr: TField(_, FStatic(c, f))}, [arg]) if (c.get().name == "Std" && f.get().name == "string"):
                        return arg.t;
                    default:
                }
        }
        return TypedIndex.exprType(e);
    }

    /** `e` (a String expression) as one node. */
    public static function format(e:Expr, exprToIR:Expr->IRNode):IRNode {
        var f = new StringFormatter(exprToIR);
        f.add(e);
        return f.build();
    }
}
#end
