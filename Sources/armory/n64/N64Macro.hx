package armory.n64;

#if macro
import haxe.macro.Context;
import haxe.macro.Expr;

using StringTools;

/**
 * Entry point of the N64 macros: one build macro on every class of the user's
 * `arm` package, handing autoloads to N64AutoloadMacro and traits to
 * N64TraitMacro. Both extract after typing, through N64Extractor.
 */
class N64Macro {
    /**
     * Called via --macro (see armory_hooks.py) to add the build macro to the
     * user's arm package.
     */
    public static function register():Void {
        haxe.macro.Compiler.addGlobalMetadata(
            "arm",  // user package
            "@:build(armory.n64.N64Macro.build())",
            true,   // recursive
            true,   // toTypes
            false   // toFields
        );
    }

    macro public static function build():Array<Field> {
        if (!Context.defined("arm_target_n64")) return null;

        var localClass = Context.getLocalClass();
        if (localClass == null) return null;
        var cls = localClass.get();

        // Skip internal/engine classes
        if (cls.module.startsWith("iron.") || cls.module.startsWith("armory.")) return null;

        var order = N64AutoloadMacro.getAutoloadOrder(cls.meta.get());
        if (order != null) {
            N64AutoloadMacro.build(localClass, Context.getBuildFields(), order);
        } else if (N64Extractor.isTrait(cls)) {
            N64TraitMacro.build(localClass, Context.getBuildFields());
        }
        return null;
    }
}
#end
