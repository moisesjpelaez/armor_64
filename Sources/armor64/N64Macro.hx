package armor64;

#if macro
import haxe.macro.Context;
import haxe.macro.Expr;

using StringTools;

/**
 * Entry point of the N64 macros: one build macro on every class of the
 * project's package, handing autoloads to N64AutoloadMacro and traits to
 * N64TraitMacro. Both extract after typing, through N64Extractor.
 */
class N64Macro {
    /**
     * Called via --macro (see armory_hooks.py) to add the build macro to the
     * project's package (Armory's Package setting, "arm" by default).
     */
    public static function register(projectPackage:String = "arm"):Void {
        haxe.macro.Compiler.addGlobalMetadata(
            projectPackage,
            "@:build(armor64.N64Macro.build())",
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

        // A compile server keeps typed modules and doesn't run their build
        // macro again: the build changes this file before each compile, so the
        // game's modules are typed (and extracted) every time
        Context.registerModuleDependency(cls.module, N64MacroBase.compileStampPath());

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
