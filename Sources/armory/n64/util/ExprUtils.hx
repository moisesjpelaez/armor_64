package armory.n64.util;

#if macro
import haxe.macro.Expr;
import armory.n64.converters.ICallConverter;

/**
 * Expression Utilities
 *
 * Common helper functions for working with Haxe macro expressions.
 * Reduces code duplication across converters and extractors.
 */
class ExprUtils {
    /**
     * Extract identifier name from an expression.
     * Handles both direct identifiers (x) and field access (this.x, obj.x).
     * Returns null if not an identifier pattern.
     */
    public static function extractIdentName(e:Expr):String {
        if (e == null) return null;
        switch (e.expr) {
            case EConst(CIdent(name)): return name;
            case EField(_, fieldName): return fieldName;
            default: return null;
        }
    }

    /**
     * Extract string literal from an expression.
     * Handles both forms: CString(s) and CString(s, quotation).
     */
    public static function extractString(e:Expr):String {
        if (e == null) return null;
        switch (e.expr) {
            case EConst(CString(s, _)): return s;
            default: return null;
        }
    }

    /**
     * Get expression type safely, catching any exceptions.
     * Returns null on failure instead of throwing.
     */
    public static function getExprTypeSafe(e:Expr, ctx:IExtractorContext):String {
        try {
            return ctx.getExprType(e);
        } catch (_:Dynamic) {
            return null;
        }
    }
}
#end
