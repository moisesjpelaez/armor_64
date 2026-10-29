package armor64;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;

using haxe.macro.ExprTools;
using StringTools;

/**
 * Helper class to build sprintf-style string concatenation IR nodes.
 *
 * Flattens expressions like "Score: " + Std.string(score) + " pts"
 * into a single sprintf node: { type: "sprintf", value: "Score: %ld pts", args: [score] }
 */
class StringConcatBuilder {
    var members:Map<String, MemberIR>;
    var localVarTypes:Map<String, String>;
    var exprToIRFunc:Expr->IRNode;
    var getExprTypeFunc:Expr->String;

    public function new(
        members:Map<String, MemberIR>,
        localVarTypes:Map<String, String>,
        exprToIRFunc:Expr->IRNode,
        getExprTypeFunc:Expr->String
    ) {
        this.members = members;
        this.localVarTypes = localVarTypes;
        this.exprToIRFunc = exprToIRFunc;
        this.getExprTypeFunc = getExprTypeFunc;
    }

    /**
     * Build a flattened string concatenation node.
     * Collects all parts and emits a single sprintf node.
     */
    public function build(e1:Expr, e2:Expr):IRNode {
        var formatParts:Array<String> = [];
        var args:Array<IRNode> = [];

        collectParts(e1, formatParts, args);
        collectParts(e2, formatParts, args);

        var node:IRNode = {
            type: "sprintf",
            value: formatParts.join(""),
            args: args
        };
        node.maxLength = maxLength(formatParts, args);
        N64MacroBase.recordConcatLength(node.maxLength);
        return node;
    }

    // Most UTF-8 bytes the formatted string can have, null if unknown (a
    // string argument that isn't a literal)
    static function maxLength(formatParts:Array<String>, args:Array<IRNode>):Null<Int> {
        var length = 0;
        var arg = 0;
        for (part in formatParts) {
            switch (part) {
                case "%ld": length += 11;  // -2147483648
                case "%.2f": length += 43; // -3.4e38 with 2 decimals
                case "%d": length += 1;    // Bool: 0 or 1
                case "%s":
                    var a = args[arg];
                    var known = a != null ? a.maxLength != null ? a.maxLength : (a.type == "string" ? N64MacroBase.utf8Length(Std.string(a.value)) : null) : null;
                    if (known == null) return null;
                    length += known;
                default:
                    length += N64MacroBase.utf8Length(part);
                    continue;
            }
            arg++;
        }
        return length;
    }

    /**
     * Recursively collect parts of a string concatenation.
     */
    function collectParts(e:Expr, formatParts:Array<String>, args:Array<IRNode>):Void {
        if (e == null) return;

        switch (e.expr) {
            case EConst(CString(s)):
                // Literal string - add to the format (a literal % is %% there)
                formatParts.push(s.split("%").join("%%"));

            case EBinop(OpAdd, left, right):
                // Nested concatenation - recurse if either side is string
                var leftType = getExprTypeFunc(left);
                var rightType = getExprTypeFunc(right);
                if (leftType == "String" || rightType == "String") {
                    collectParts(left, formatParts, args);
                    collectParts(right, formatParts, args);
                } else {
                    // Not string concat, treat as expression
                    addExpr(e, formatParts, args);
                }

            case ECall(callExpr, callArgs):
                // Check for Std.string(value)
                switch (callExpr.expr) {
                    case EField(obj, method):
                        switch (obj.expr) {
                            case _ if (TypedIndex.isClassRef(obj, "Std")):
                                if (method == "string" && callArgs.length > 0) {
                                    var valueExpr = callArgs[0];
                                    // Std.string() just ensures string conversion - recurse into the expression
                                    // This handles Std.string("/" + totalScore) by flattening it
                                    collectParts(valueExpr, formatParts, args);
                                    return;
                                }
                            default:
                        }
                    default:
                }
                // Other call - treat as expression
                addExpr(e, formatParts, args);

            case EParenthesis(inner):
                collectParts(inner, formatParts, args);

            case EConst(CIdent(name)):
                // Variable reference
                var varType = "Dynamic";
                if (members.exists(name)) {
                    varType = members.get(name).haxeType;
                } else if (localVarTypes.exists(name)) {
                    varType = localVarTypes.get(name);
                }

                if (varType == "String") {
                    formatParts.push("%s");
                    args.push(exprToIRFunc(e));
                } else {
                    addTyped(e, varType, formatParts, args);
                }

            default:
                addExpr(e, formatParts, args);
        }
    }

    /**
     * Add a typed format specifier for an expression.
     * Note: On N64, int32_t is 'long int', so we use %ld for integers.
     */
    function addTyped(e:Expr, exprType:String, formatParts:Array<String>, args:Array<IRNode>):Void {
        var format = switch (exprType) {
            case "Int": "%ld";      // int32_t is long on N64
            case "Float": "%.2f";
            case "Bool": "%d";
            case "String": "%s";
            default: "%ld";
        };
        formatParts.push(format);
        args.push(exprToIRFunc(e));
    }

    /**
     * Add any expression to format (infer type).
     */
    function addExpr(e:Expr, formatParts:Array<String>, args:Array<IRNode>):Void {
        var exprType = getExprTypeFunc(e);
        addTyped(e, exprType, formatParts, args);
    }
}
#end
