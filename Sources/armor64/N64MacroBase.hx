package armor64;

#if macro
import haxe.macro.Context;
import haxe.macro.Expr;
import haxe.macro.Type;
import haxe.Json;
import sys.io.File;
import sys.FileSystem;

import armor64.IRTypes;

/**
 * N64 Macro Base - Shared Utilities
 *
 * Contains common functionality used by both N64TraitMacro and N64AutoloadMacro:
 * - IR node serialization
 * - Binary/unary operator conversion
 * - Type conversion utilities
 * - JSON output helpers
 *
 * This reduces code duplication between the two macro files.
 */
class N64MacroBase {
    // Global audio configuration (set by AudioCallConverter when Aura.init() is found)
    // Audio facts for audio_config.h (n64_audio.json): the mixer channel
    // count from Aura.init(), and the mix channels the game uses
    public static var audioChannelSize:Null<Int> = null;
    static var mixChannels:Array<String> = [];

    // Extraction runs after typing so the extractors can use the TypedIndex.
    // Jobs keep the build order of trait and autoload classes: generated
    // callback names depend on it (static counters in the converters).
    static var extractedClasses:Map<String, Ref<ClassType>> = new Map();
    static var extractions:Array<Void->Void> = [];
    static var writers:Array<Void->Void> = [];
    static var afterTypingRegistered:Bool = false;

    /**
     * Queue extraction of a class (called from a build macro). Runs after
     * typing, once the TypedIndex is available.
     */
    public static function deferExtraction(cls:Ref<ClassType>, extract:Void->Void):Void {
        registerAfterTyping();
        var c = cls.get();
        extractedClasses.set(c.pack.concat([c.name]).join("."), cls);
        // `using` is only known in the class's own build context
        var usings = Context.getLocalUsing();
        extractions.push(function() {
            TypedIndex.currentUsings = [for (u in usings) u.get()];
            extract();
        });
    }

    // Exported names already used, by kind ("trait", "autoload") -> name -> full path
    static var exportedNames:Map<String, Map<String, String>> = new Map();

    /**
     * Report a trait or autoload whose name is already used by another class
     * in a different package: on N64 they are identified by name, so the
     * second one would silently replace the first. Returns false on a clash.
     */
    public static function claimExportedName(kind:String, name:String, cls:ClassType):Bool {
        var names = exportedNames.get(kind);
        if (names == null) exportedNames.set(kind, names = new Map());
        var path = cls.pack.concat([cls.name]).join(".");
        var other = names.get(name);
        if (other != null && other != path) {
            Context.reportError('N64: $kind class names must be unique: $path and $other are both named "$name"', cls.pos);
            return false;
        }
        names.set(name, path);
        return true;
    }

    /**
     * Whether a class (full path) is a trait or autoload being exported.
     */
    public static function isExtractedClass(path:String):Bool {
        return extractedClasses.exists(path);
    }

    /** A trait or autoload class being exported (full path), null otherwise. */
    public static function extractedClass(path:Null<String>):Null<ClassType> {
        var c = path != null ? extractedClasses.get(path) : null;
        return c != null ? c.get() : null;
    }

    /**
     * Register a function writing IR output, run after all extractions.
     */
    public static function addWriter(write:Void->Void):Void {
        registerAfterTyping();
        writers.push(write);
    }

    static function registerAfterTyping():Void {
        if (afterTypingRegistered) return;
        afterTypingRegistered = true;
        Context.onAfterTyping(function(_) {
            for (cls in extractedClasses) TypedIndex.addClass(cls.get());
            for (extract in extractions) extract();
            for (write in writers) write();
            writeSignalsJson();
            writeJsonFile("n64_audio.json", {ir_version: 1, channel_size: audioChannelSize, mix_channels: mixChannels});
            writeLimitFactsJson();
        });
    }

    /**
     * A name as the exporter writes it in C identifiers (Armory's arm.utils.safesrc):
     * special and non-ASCII characters, dots and dashes become "_", spaces are
     * removed, and a leading digit gets a "_" first. Scene ids are SCENE_ + this,
     * uppercased.
     */
    public static function safesrc(s:String):String {
        var special = "[]/\\;,><&*:$%=+@!#^()|?'\".-";
        var out = new StringBuf();
        for (i in 0...s.length) {
            var c = s.charCodeAt(i);
            if (c == " ".code) continue;
            out.addChar(c >= 128 || special.indexOf(String.fromCharCode(c)) >= 0 ? "_".code : c);
        }
        s = out.toString();
        return s.length > 0 && s.charCodeAt(0) >= "0".code && s.charCodeAt(0) <= "9".code ? "_" + s : s;
    }

    /**
     * C constant of an Aura mix channel ("music" -> AUDIO_MIX_MUSIC), recorded
     * so audio_config.h defines it. Characters C doesn't allow become "_".
     */
    public static function mixChannelConstant(name:Null<String>):String {
        if (name == null || name == "") name = "master";
        var id = ~/[^A-Z0-9_]/g.replace(name.toUpperCase(), "_");
        if (mixChannels.indexOf(id) < 0) mixChannels.push(id);
        return 'AUDIO_MIX_$id';
    }

    // ========================================================================
    // Facts for the engine limits (n64_limits.json, read by codegen/limits.py)
    // ========================================================================

    /** A string's size in the C code: UTF-8 bytes (an accented letter takes 2). */
    public static function utf8Length(s:String):Int {
        return haxe.io.Bytes.ofString(s).length;
    }

    // Longest literal Map key (UTF-8 bytes); null once a key isn't a literal
    static var mapKeyLength:Null<Int> = 0;
    // Literal keys set into maps, by map C type; a type is removed once a key
    // set into one of its maps isn't a literal
    static var mapKeys:Map<String, Map<String, Bool>> = new Map();
    static var dynamicMapTypes:Map<String, Bool> = new Map();

    /** A key used with a map (any operation); set: whether it's stored. */
    public static function recordMapKey(mapType:String, key:IRNode, set:Bool):Void {
        var literal = key != null && key.type == "string" ? Std.string(key.value) : null;
        if (literal == null) {
            mapKeyLength = null;
            if (set) dynamicMapTypes.set(mapType, true);
            return;
        }
        if (mapKeyLength != null && utf8Length(literal) > mapKeyLength) mapKeyLength = utf8Length(literal);
        if (set) {
            if (!mapKeys.exists(mapType)) mapKeys.set(mapType, new Map());
            mapKeys.get(mapType).set(literal, true);
        }
    }

    // Longest string (UTF-8 bytes) a concatenation builds / a label is set to at
    // runtime; null once one can't be bounded
    static var concatLength:Null<Int> = 0;
    static var labelTextLength:Null<Int> = 0;

    public static function recordConcatLength(length:Null<Int>):Void {
        concatLength = length == null || concatLength == null ? null : Std.int(Math.max(concatLength, length));
    }

    public static function recordLabelText(value:IRNode):Void {
        var length:Null<Int> = value == null ? null
            : value.maxLength != null ? value.maxLength
            : value.type == "string" ? utf8Length(Std.string(value.value)) : null;
        labelTextLength = length == null || labelTextLength == null ? null : Std.int(Math.max(labelTextLength, length));
    }

    static function writeLimitFactsJson():Void {
        // Distinct keys per map type, null when not countable
        var keys:Dynamic = {};
        var types = [for (t in mapKeys.keys()) t].concat([for (t in dynamicMapTypes.keys()) t]);
        for (t in types) {
            Reflect.setField(keys, t, dynamicMapTypes.exists(t) ? null : Lambda.count(mapKeys.get(t)));
        }
        writeJsonFile("n64_limits.json", {
            ir_version: 1,
            map_key_length: mapKeyLength,
            map_keys: keys,
            concat_length: concatLength,
            label_text_length: labelTextLength
        });
    }

    // Signals used by the game, by "<declaring class>.<name>". Argument types
    // are collected from every trait and autoload using a signal (typed
    // handlers first, then emits), then written to n64_signals.json
    public static var signals:Map<String, SignalDef> = new Map();

    /** A connect() call in class `site` (a trait or autoload, full path). */
    public static function signalConnectSite(key:String, site:String):Void {
        signals.get(key).connectSites.push(site);
    }

    public static function useSignal(owner:String, name:String, isStatic:Bool):String {
        var key = '$owner.$name';
        if (!signals.exists(key)) {
            var shortName = owner.substr(owner.lastIndexOf(".") + 1);
            signals.set(key, {
                owner: owner,
                name: name,
                isStatic: isStatic,
                cSymbol: isStatic ? 'g_${shortName.toLowerCase()}_$name' : null,
                handlerTypes: [],
                emitTypes: [],
                connectSites: []
            });
        }
        return key;
    }

    // Argument C types of a signal (null when unknown): the first known type
    // per position wins
    public static function recordSignalTypes(key:String, cTypes:Array<Null<String>>, fromHandler:Bool):Void {
        var def = signals.get(key);
        var known = fromHandler ? def.handlerTypes : def.emitTypes;
        for (i in 0...cTypes.length) {
            if (i >= known.length) known.push(cTypes[i]);
            else if (known[i] == null) known[i] = cTypes[i];
        }
    }

    static function writeSignalsJson():Void {
        var out:Dynamic = {};
        var keys = [for (k in signals.keys()) k];
        keys.sort(Reflect.compare);
        for (key in keys) {
            var def = signals.get(key);
            var count = Std.int(Math.max(def.handlerTypes.length, def.emitTypes.length));
            var argTypes = [for (i in 0...count) {
                var t = i < def.handlerTypes.length ? def.handlerTypes[i] : null;
                if (t == null && i < def.emitTypes.length) t = def.emitTypes[i];
                t != null ? t : "void*";
            }];
            // Sorted: they're found in Haxe's typing order, which can change between builds
            var sites = def.connectSites.copy();
            sites.sort(Reflect.compare);
            var entry:Dynamic = {owner: def.owner, name: def.name, is_static: def.isStatic, arg_types: argTypes, connect_sites: sites};
            if (def.cSymbol != null) entry.c_symbol = def.cSymbol;
            Reflect.setField(out, key, entry);
        }
        writeJsonFile("n64_signals.json", {ir_version: 1, signals: out});
    }

    // Position of the expression being converted, set by the extractors' exprToIR()
    public static var currentPos:Null<Position> = null;
    static var reportedUnsupported:Map<String, Bool> = new Map();

    /**
     * Skip node for valid Haxe that the N64 exporter can't translate (yet).
     * Reports it at the source line as a warning (an error with -D n64_strict)
     * instead of silently leaving it out of the generated C.
     */
    public static function unsupported(msg:String, ?pos:Position):IRNode {
        if (pos == null) pos = currentPos != null ? currentPos : Context.currentPos();

        // The same expression can be converted more than once (e.g. inlined methods)
        var infos = Context.getPosInfos(pos);
        var key = '${infos.file}:${infos.min}:$msg';
        if (!reportedUnsupported.exists(key)) {
            reportedUnsupported.set(key, true);
            if (Context.defined("n64_strict")) {
                Context.reportError('N64: $msg', pos);
            } else {
                Context.warning('N64: $msg', pos);
            }
        }
        return { type: "skip", warn: msg };
    }

    /**
     * `obj.method(args)` that no converter or binding translates: reported at
     * the source line, as there is no generic C for a method call.
     */
    /**
     * What an anonymous callback (tween, method argument, signal handler,
     * button) captures, as the exporter keeps it: [{name, ctype}], each copied
     * where the callback is registered (see TypedIndex.capturedLocals).
     */
    public static function callbackCaptures(fn:Expr):Array<Dynamic> {
        var locals = TypedIndex.capturedLocals(fn);
        if (locals == null) {
            unsupported("this callback's uses of the variables around it couldn't be found (no typed code)", fn.pos);
            return [];
        }
        return [for (v in locals) {name: v.name, ctype: captureCType(v.t)}];
    }

    static function captureCType(t:Type):String {
        return switch (Context.follow(t)) {
            // A callback passed on: the runtime's callback types take obj/data
            case TFun(args, _) if (args.length == 1 && TypedIndex.typeName(args[0].t) == "Float"): "ArmFloatObjCallback";
            case TFun(_, _): "ArmCallback";
            default: armor64.mapping.TypeMap.getCType(TypedIndex.typeName(t));
        };
    }

    public static function unsupportedMethodCall(obj:Expr, method:String):IRNode {
        if (TypedIndex.nodesAt(obj).length == 0) {
            return unsupported('could not resolve the type of this .$method() call on N64');
        }
        var owner = TypedIndex.callOwner(obj, method);
        if (owner == null) {
            return unsupported('.$method() is not supported on N64 (the class declaring it could not be found)');
        }
        var shortName = owner.substr(owner.lastIndexOf(".") + 1);
        return unsupported('$shortName.$method() is not supported on N64 yet');
    }

    // iron.math vectors: C struct type and constructor defaults
    static var vectorTypes:Map<String, {ctype:String, defaults:Array<String>}> = [
        "iron.math.Vec2" => {ctype: "ArmVec2", defaults: ["0", "0"]},
        "iron.math.Vec3" => {ctype: "ArmVec3", defaults: ["0", "0", "0"]},
        "iron.math.Vec4" => {ctype: "ArmVec4", defaults: ["0", "0", "0", "1.0f"]},
    ];

    /**
     * `new Vec4(x, y)` etc. as a C struct literal, missing arguments taking the
     * constructor's defaults. Null if `e` isn't an iron.math vector.
     */
    public static function vectorLiteral(e:Expr, args:Array<IRNode>):Null<IRNode> {
        var vector = vectorTypes.get(TypedIndex.newClass(e));
        if (vector == null) return null;
        var values = [for (i in 0...vector.defaults.length) i < args.length ? '{$i}' : vector.defaults[i]];
        return {
            type: "new_vec",
            c_code: '(${vector.ctype}){' + values.join(", ") + '}',
            args: args.slice(0, vector.defaults.length)
        };
    }

    /**
     * `a == b` / `a != b` on two Strings (not a null check): compares the text,
     * as Haxe does, instead of the pointers.
     */
    public static function stringEquality(op:Binop, e1:Expr, e2:Expr, exprToIR:Expr->IRNode):Null<IRNode> {
        if (op != OpEq && op != OpNotEq) return null;
        if (isNullLiteral(e1) || isNullLiteral(e2)) return null;
        if (TypedIndex.typeOf(e1) != "String" || TypedIndex.typeOf(e2) != "String") return null;
        return {
            type: "string_equals",
            value: op == OpEq ? "==" : "!=",
            children: [exprToIR(e1), exprToIR(e2)]
        };
    }

    public static function isNullLiteral(e:Expr):Bool {
        return switch (e.expr) {
            case EConst(CIdent("null")): true;
            default: false;
        };
    }

    /**
     * Whether a converted statement goes into the IR. Plain skips are dropped;
     * unsupported ones are kept so the generated C shows them as comments.
     */
    public static function keepStatement(node:IRNode):Bool {
        return node != null && (node.type != "skip" || node.warn != null);
    }

    /**
     * Short readable name of an expression kind, for unsupported() messages.
     */
    public static function describeExpr(e:Expr):String {
        return switch (e.expr) {
            case ESwitch(_, _, _): "switch";
            case ETry(_, _): "try/catch";
            case EThrow(_): "throw";
            case EFunction(_, _): "local function";
            case ECast(_, _): "cast";
            case EArrayDecl(_): "array literal";
            case EObjectDecl(_): "object literal";
            case EUntyped(_): "untyped";
            case EMeta(_, _): "metadata expression";
            case ECheckType(_, _): "type check";
            case EIs(_, _): "'is' check";
            case EFor(_, _): "for loop";
            case EParenthesis(_): "parenthesized expression";
            case ECall(_, _): "call result";
            case EArray(_, _): "array element";
            default: e.expr.getName();
        };
    }

    /**
     * Fallback for expressions the extractors don't handle.
     * Anonymous functions are callback arguments, handled by the code that
     * consumes the call's raw params, so they are skipped silently.
     */
    public static function unhandledExpr(e:Expr):IRNode {
        return switch (e.expr) {
            case EFunction(FAnonymous | FArrow, _): { type: "skip" };
            default: unsupported(describeExpr(e) + " is not supported on N64 yet");
        };
    }

    /**
     * Serialize an IRNode to a Dynamic object for JSON output.
     */
    public static function serializeIRNode(node:IRNode):Dynamic {
        if (node == null) return null;

        var obj:Dynamic = { type: node.type };

        if (node.value != null) obj.value = node.value;
        if (node.children != null && node.children.length > 0) {
            obj.children = [for (c in node.children) serializeIRNode(c)];
        }
        if (node.args != null && node.args.length > 0) {
            obj.args = [for (a in node.args) serializeIRNode(a)];
        }
        if (node.method != null) obj.method = node.method;
        if (node.object != null) obj.object = serializeIRNode(node.object);
        if (node.props != null) obj.props = node.props;
        if (node.c_code != null) obj.c_code = node.c_code;
        if (node.c_func != null) obj.c_func = node.c_func;
        if (node.cName != null) obj.cName = node.cName;
        if (node.trait != null) obj.trait = node.trait;
        // Declaring class (inherited members, method calls)
        if (node.owner != null) obj.owner = node.owner;
        // Callback parameter call fields
        if (node.name != null) obj.name = node.name;
        if (node.paramType != null) obj.paramType = node.paramType;
        // Callback wrapper fields (for inherited method callbacks)
        if (node.callback_name != null) obj.callback_name = node.callback_name;
        if (node.body != null && node.body.length > 0) {
            obj.body = [for (b in node.body) serializeIRNode(b)];
        }
        if (node.captures != null) obj.captures = node.captures;
        if (node.param_name != null) obj.param_name = node.param_name;
        if (node.param_type != null) obj.param_type = node.param_type;
        if (node.param_ctype != null) obj.param_ctype = node.param_ctype;
        if (node.warn != null) obj.warn = node.warn;

        return obj;
    }

    /**
     * The C operator for a Haxe binary operator that C has as it is (null for
     * the others: N64Extractor.operatorTemplate, for loops, map literals).
     */
    public static function binopToString(op:Binop):String {
        return switch (op) {
            case OpAdd: "+";
            case OpSub: "-";
            case OpMult: "*";
            case OpDiv: "/";
            case OpMod: "%";
            case OpEq: "==";
            case OpNotEq: "!=";
            case OpLt: "<";
            case OpLte: "<=";
            case OpGt: ">";
            case OpGte: ">=";
            case OpAnd: "&";      // Bitwise AND
            case OpOr: "|";       // Bitwise OR
            case OpBoolAnd: "&&"; // Logical AND
            case OpBoolOr: "||";  // Logical OR
            case OpXor: "^";      // Bitwise XOR
            case OpShl: "<<";     // Shift left
            case OpShr: ">>";     // Shift right
            case OpAssign: "=";
            case OpAssignOp(innerOp):
                var inner = binopToString(innerOp);
                inner != null ? inner + "=" : null;
            default: null;
        };
    }

    /**
     * Convert a Haxe unary operator to its C string representation.
     */
    public static function unopToString(op:Unop):String {
        return switch (op) {
            case OpNeg: "-";
            case OpNot: "!";
            case OpNegBits: "~";  // Bitwise NOT
            case OpIncrement: "++";
            case OpDecrement: "--";
            case OpSpread: "..."; // Should not appear in N64 code
        };
    }

    /**
     * Convert a ComplexType to its string name (including type parameters).
     * E.g., Array<BaseChannelHandle> -> "Array<BaseChannelHandle>"
     *       Map<String, Array<Int>> -> "Map<String, Array<Int>>"
     */
    public static function complexTypeToString(ct:ComplexType):String {
        return switch (ct) {
            case TPath(p):
                if (p.params == null || p.params.length == 0) {
                    p.name;
                } else {
                    // Build parameterized type string: Name<Param1, Param2>
                    var paramStrings = [for (tp in p.params) typeParamToString(tp)];
                    p.name + "<" + paramStrings.join(", ") + ">";
                }
            case TFunction(args, ret):
                // Handle function types like Void->Void, Float->Void
                var argTypes = [for (arg in args) complexTypeToString(arg)];
                var retType = complexTypeToString(ret);
                if (argTypes.length == 0) {
                    "Void->" + retType;
                } else {
                    argTypes.join("->") + "->" + retType;
                }
            default: "Dynamic";
        };
    }

    /**
     * Convert a TypeParam to string.
     */
    static function typeParamToString(tp:TypeParam):String {
        return switch (tp) {
            case TPType(ct): complexTypeToString(ct);
            case TPExpr(e): "Dynamic"; // Expression type params not supported
        };
    }

    /**
     * Write JSON data to a file in the build directory.
     * Creates the directory if it doesn't exist.
     */
    static function buildDir():String {
        var buildDir = Context.getDefines().get("arm_build_dir");
        return buildDir != null ? buildDir : "build";
    }

    /** The file N64 builds change before each compile (build_pipeline.py). */
    public static function compileStampPath():String {
        return FileSystem.absolutePath(buildDir() + "/n64_compile.stamp");
    }

    public static function writeJsonFile(filename:String, data:Dynamic):Void {
        var json = Json.stringify(data, null, "  ");

        var outPath = buildDir() + "/" + filename;
        try {
            var dir = haxe.io.Path.directory(outPath);
            if (dir != "" && !FileSystem.exists(dir)) {
                FileSystem.createDirectory(dir);
            }
            File.saveContent(outPath, json);
        } catch (e:Dynamic) {
            Context.error('Failed to write $filename: $e', Context.currentPos());
        }
    }

    /**
     * Convert a constant expression to its IR node representation.
     */
    public static function constToIR(c:Constant):IRNode {
        return switch (c) {
            case CInt(v): { type: "int", value: Std.parseInt(v) };
            case CFloat(f): { type: "float", value: Std.parseFloat(f) };
            case CString(s): { type: "string", value: s };
            case CIdent("true"): { type: "bool", value: true };
            case CIdent("false"): { type: "bool", value: false };
            case CIdent("null"): { type: "null" };
            case CIdent("this"): { type: "ident", value: "this" };
            case CIdent("object"): { type: "ident", value: "object" };
            case CIdent(name): { type: "ident", value: name };
            default: { type: "skip" };
        };
    }

}

#end
