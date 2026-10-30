package armor64;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.converters.ICallConverter;

/**
 * What a binding's {self} is, from the call's receiver.
 */
enum BindingReceiver {
    None;                       // A static call: no {self}
    Value;                      // The receiver, e.g. the vector in `v.length()`
    InPlace;                    // The receiver, written to (`v.normalize()`): used as written, never copied
    FieldOwner(field:String);   // The object owning the receiver field: `x.transform.m()` -> x,
                                // `transform.m()` -> the trait's object (traits only)
}

/**
 * A Haxe method that maps straight to C.
 *
 * c: the C template. Placeholders:
 *   {0}, {1}, ...   the call's arguments
 *   {1=1.0f}        argument 1, or 1.0f when the call leaves it out
 *   {self}          the receiver (see BindingReceiver)
 * Any other {name} is an error (a C brace around a bare name needs a space:
 * `{ x }`). Every argument a call passes must appear in the template, so one
 * the C can't take (e.g. applyForce's `loc`) is reported, not dropped.
 * An argument (or receiver) used more than once is evaluated once, into a
 * temporary, unless it's a plain name or literal (emit_c_template in
 * trait_emitter.py). A template ending with `;` or starting with `{` (a
 * block) is a statement, otherwise an expression.
 *
 * noop: the call does nothing on N64 (its arguments aren't evaluated).
 */
typedef Binding = {
    ?c:String,
    ?receiver:BindingReceiver,  // Default None
    ?flags:Array<String>,       // TraitMeta flags the call sets (uses_physics, ...)
    ?noop:Bool,
}

/**
 * Haxe API -> C, for the calls that map straight to a C expression or
 * statement: one entry per method, keyed "Class.method" by the class (or
 * abstract) declaring it, from the typed AST. So aliases, imports, full
 * paths, inherited methods and `using` extensions all match.
 *
 * Calls needing logic (callbacks, export-time lookups, generated code) are
 * converters (converters/), which run first.
 */
class Bindings {
    static inline var TRANSFORM = "&((ArmObject*){self})->transform";

    static var calls:Map<String, Binding> = build();

    static function build():Map<String, Binding> {
        var map:Map<String, Binding> = [
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
            // Ints in Haxe; round goes up from .5 (-1.5 -> -1), computed exactly
            "Math.floor" => {c: "((int32_t)fm_floorf({0}))"},
            "Math.ceil" => {c: "((int32_t)fm_ceilf({0}))"},
            "Math.round" => {c: "((int32_t)fm_floorf({0}) + (({0}) - fm_floorf({0}) >= 0.5f))"},
            "Math.min" => {c: "fminf({0}, {1})"},
            "Math.max" => {c: "fmaxf({0}, {1})"},
            "Math.exp" => {c: "expf({0})"},  // Exact, as Haxe's (fm_expf is up to 3% off, unbounded)
            "Math.log" => {c: "logf({0})"},

            // Std (Std.string is a converter: its format depends on the value's type)
            "Std.int" => {c: "(int32_t)({0})"},
            "Std.parseFloat" => {c: "strtof({0}, NULL)"},

            // StringTools, as statics or `using StringTools` extensions
            "StringTools.startsWith" => {c: "(strncmp({0}, {1}, strlen({1})) == 0)"},
            "StringTools.endsWith" => {c: "str_ends_with({0}, {1})"},
            "StringTools.contains" => {c: "(strstr({0}, {1}) != NULL)"},

            // Transform, on `x.transform` (the C works on the owning object).
            // Blender (x, y, z) -> N64 (x, z, -y)
            "iron.object.Transform.translate" => transform('it_translate($TRANSFORM, {0}, {2}, -({1}));'),
            "iron.object.Transform.rotate" => transform('it_rotate_axis_global($TRANSFORM, ({0}).x, ({0}).z, -({0}).y, {1});'),
            "iron.object.Transform.move" => transform('it_move($TRANSFORM, ({0}).x, ({0}).z, -({0}).y, {1=1.0f});'),
            "iron.object.Transform.setRotation" => transform('it_set_rot_euler($TRANSFORM, {0}, {2}, -({1}));'),
            "iron.object.Transform.reset" => transform('it_reset($TRANSFORM);'),
            // The matrix is rebuilt from the dirty flag, into every framebuffer's slot
            "iron.object.Transform.buildMatrix" => transform("((ArmObject*){self})->transform.dirty = TRANSFORM_DIRTY_FRAMES;"),
            // Directions: N64 (x, y, z) -> Blender (x, -z, y)
            "iron.object.Transform.look" => transform('({ T3DVec3 _d; it_look($TRANSFORM, &_d); (ArmVec4){_d.v[0], -_d.v[2], _d.v[1], 1.0f}; })'),
            "iron.object.Transform.right" => transform('({ T3DVec3 _d; it_right($TRANSFORM, &_d); (ArmVec4){_d.v[0], -_d.v[2], _d.v[1], 1.0f}; })'),
            "iron.object.Transform.up" => transform('({ T3DVec3 _d; it_up($TRANSFORM, &_d); (ArmVec4){_d.v[0], -_d.v[2], _d.v[1], 1.0f}; })'),
            "iron.object.Transform.worldx" => transform('it_world_x($TRANSFORM)'),
            "iron.object.Transform.worldy" => transform('it_world_y($TRANSFORM)'),
            "iron.object.Transform.worldz" => transform('it_world_z($TRANSFORM)'),

            // RigidBody: the receiver is its object's pointer (body = ->rigid_body). Blender -> Oimo (x, z, -y)
            "armory.trait.physics.oimo.RigidBody.applyForce" => physics("{ OimoVec3 _f = (OimoVec3){({0}).x, ({0}).z, -({0}).y}; physics_apply_force({self}->rigid_body, &_f); }"),
            "armory.trait.physics.oimo.RigidBody.applyImpulse" => physics("{ OimoVec3 _i = (OimoVec3){({0}).x, ({0}).z, -({0}).y}; physics_apply_impulse({self}->rigid_body, &_i); }"),
            "armory.trait.physics.oimo.RigidBody.setLinearVelocity" => physics("{ OimoVec3 _v = (OimoVec3){{0}, {2}, -({1})}; physics_set_linear_velocity({self}->rigid_body, &_v); }"),
            "armory.trait.physics.oimo.RigidBody.setAngularVelocity" => physics("{ OimoVec3 _v = (OimoVec3){{0}, {2}, -({1})}; physics_set_angular_velocity({self}->rigid_body, &_v); }"),
            // Oimo (x, y, z) -> Blender (x, -z, y)
            "armory.trait.physics.oimo.RigidBody.getLinearVelocity" => physics("({ OimoVec3 _gv = physics_get_linear_velocity({self}->rigid_body); (ArmVec4){_gv.x, -_gv.z, _gv.y, 1.0f}; })"),
            "armory.trait.physics.oimo.RigidBody.getAngularVelocity" => physics("({ OimoVec3 _gv = physics_get_angular_velocity({self}->rigid_body); (ArmVec4){_gv.x, -_gv.z, _gv.y, 1.0f}; })"),

            // Object (getTrait and the like are a converter)
            "iron.object.Object.remove" => {c: "object_remove({self})", receiver: Value},
            "iron.object.Object.getAnimation" => {c: "((ArmObject*){self})->animation", receiver: Value},

            // Animation
            "iron.object.Animation.play" => {c: 'animation_play({self}, {0=""})', receiver: Value},
            "iron.object.Animation.pause" => {c: "animation_pause({self})", receiver: Value},
            "iron.object.Animation.resume" => {c: "animation_resume({self})", receiver: Value},

            // Kha 2D drawing: corners, not width/height; begin/end batch nothing on N64
            "kha.graphics2.Graphics.fillRect" => {c: "render2d_fill_rect({0}, {1}, ({0}) + ({2}), ({1}) + ({3}), _g2_color);", receiver: Value},
            "kha.graphics2.Graphics.begin" => {noop: true},
            "kha.graphics2.Graphics.end" => {noop: true},
        ];

        // Vectors (clone and set are a converter: transform vectors differ)
        for (v in [{name: "Vec4", c: "ArmVec4", xyz: true}, {name: "Vec3", c: "ArmVec3", xyz: true}, {name: "Vec2", c: "ArmVec2", xyz: false}]) {
            var axes = v.xyz ? ["x", "y", "z"] : ["x", "y"];
            inline function each(f:String->String, sep:String) return [for (a in axes) f(a)].join(sep);
            var k = 'iron.math.${v.name}.';
            map.set(k + "length", {c: 'sqrtf(${each(a -> '{self}.$a*{self}.$a', " + ")})', receiver: Value});
            map.set(k + "mult", {c: '(${v.c}){${each(a -> '{self}.$a*({0})', ", ")}}', receiver: Value});
            map.set(k + "add", {c: '(${v.c}){${each(a -> '{self}.$a+({0}).$a', ", ")}}', receiver: Value});
            map.set(k + "sub", {c: '(${v.c}){${each(a -> '{self}.$a-({0}).$a', ", ")}}', receiver: Value});
            map.set(k + "dot", {c: '(${each(a -> '{self}.$a*({0}).$a', " + ")})', receiver: Value});
            map.set(k + "normalize", {c: '{ float _l=sqrtf(${each(a -> '{self}.$a*{self}.$a', "+")}); if(_l>0.0f){ ${each(a -> '{self}.$a/=_l;', " ")} } }', receiver: InPlace});
        }

        // Bullet's RigidBody has the same API
        for (key in [for (k in map.keys()) k]) {
            if (StringTools.startsWith(key, "armory.trait.physics.oimo.RigidBody.")) {
                map.set(StringTools.replace(key, ".oimo.", ".bullet."), map.get(key));
            }
        }
        return map;
    }

    static function transform(c:String):Binding {
        return {c: c, receiver: FieldOwner("transform"), flags: ["uses_transform"]};
    }

    static function physics(c:String):Binding {
        return {c: c, receiver: Value, flags: ["uses_physics"]};
    }

    static var placeholder = ~/\{([A-Za-z0-9_]+)(=[^{}]*)?\}/;

    /**
     * The c_template node for `obj.method(args)` if a binding maps it (null
     * if none does). `owner` is the class declaring the method.
     */
    public static function convert(owner:String, method:String, obj:Expr, args:Array<IRNode>, ctx:IExtractorContext):Null<IRNode> {
        if (owner == null) return null;
        var b = calls.get('$owner.$method');
        if (b == null) return null;
        if (b.noop) return {type: "skip"};

        var shortName = owner.substr(owner.lastIndexOf(".") + 1);
        var receiver = b.receiver != null ? b.receiver : None;

        // `value.method(args)` through `using`: the value is the first argument
        if (receiver == None && TypedIndex.typeRefPath(obj) == null) args = [ctx.exprToIR(obj)].concat(args);

        var problem = check(b, receiver, args.length);
        if (problem != null) return N64MacroBase.unsupported('$shortName.$method(): $problem');

        var node:IRNode = {type: "c_template", c_code: b.c, args: args};
        switch (receiver) {
            case None:
            case Value:
                node.object = ctx.exprToIR(obj);
            case InPlace:
                node.object = ctx.exprToIR(obj);
                node.props = {in_place: true};
            case FieldOwner(field):
                if (ctx.isAutoload()) return N64MacroBase.unsupported('$shortName.$method() is only supported in traits on N64');
                switch (obj.expr) {
                    case EField(fieldOwner, f) if (f == field):
                        node.object = ctx.exprToIR(fieldOwner);
                    case EConst(CIdent(f)) if (f == field):
                        node.object = {type: "c_literal", c_code: "obj"};
                    default:
                        return N64MacroBase.unsupported('$shortName.$method() is only supported as `object.$field.$method()` (or `x.$field.$method()`) on N64');
                }
        }

        if (b.flags != null) {
            var meta = ctx.getMeta();
            for (f in b.flags) Reflect.setField(meta, f, true);
        }
        return node;
    }

    // What's wrong with using the binding for a call with `argCount` arguments (null: nothing)
    static function check(b:Binding, receiver:BindingReceiver, argCount:Int):Null<String> {
        var used = [for (i in 0...argCount) false];
        var rest = b.c;
        while (placeholder.match(rest)) {
            var name = placeholder.matched(1);
            var hasDefault = placeholder.matched(2) != null;
            rest = placeholder.matchedRight();
            var index = Std.parseInt(name);
            if (index != null) {
                if (index < argCount) used[index] = true;
                else if (!hasDefault) return 'needs at least ${index + 1} argument(s) on N64';
            } else if (name == "self") {
                if (receiver == None) return 'binding error: {self} in a static binding';
            } else {
                return 'binding error: unknown placeholder {$name}';
            }
        }
        var unused = used.indexOf(false);
        if (unused >= 0) return 'argument ${unused + 1} is not supported on N64';
        return null;
    }
}
#end
