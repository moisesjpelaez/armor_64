package armory.n64.mapping;

#if macro
import haxe.macro.Context;
import haxe.macro.Type;

/**
 * Engine Types
 *
 * Engine objects N64 provides implicitly: there is one controller, and
 * physics and transforms apply to the trait's own object. Members of these
 * types aren't stored and references to these classes are dropped; the
 * converters translate their API calls.
 */
class EngineTypes {
    static var providedTypes = [
        "iron.system.Gamepad", "iron.system.Keyboard", "iron.system.Mouse",
        "iron.object.Transform",
    ];

    public static function isProvided(path:Null<String>):Bool {
        if (path == null) return false;
        if (providedTypes.indexOf(path) >= 0) return true;
        // armory.trait.physics.oimo.* or armory.trait.physics.bullet.*
        return StringTools.startsWith(path, "armory.trait.physics.")
            && (StringTools.endsWith(path, ".RigidBody") || StringTools.endsWith(path, ".PhysicsWorld"));
    }

    /**
     * Whether a member of `cls` (or a parent class) holds an engine-provided
     * value, e.g. `var pad:Gamepad` or `var rb:RigidBody`.
     */
    public static function isProvidedMember(cls:ClassType, name:String):Bool {
        var c = cls;
        while (c != null) {
            for (f in c.fields.get().concat(c.statics.get())) {
                if (f.name == name) return isProvided(typePath(f.type));
            }
            c = c.superClass != null ? c.superClass.t.get() : null;
        }
        return false;
    }

    static function typePath(t:Type):Null<String> {
        return switch (Context.follow(t)) {
            case TInst(c, _): c.get().pack.concat([c.get().name]).join(".");
            case TAbstract(a, _): a.get().pack.concat([a.get().name]).join(".");
            default: null;
        };
    }
}
#end
