package armor64.converters;

#if macro
import haxe.macro.Expr;
import haxe.ds.StringMap;
import armor64.IRTypes;
import armor64.N64MacroBase;
import armor64.mapping.Constants;
import armor64.converters.ICallConverter;

using StringTools;

/**
 * Vec2/Vec3/Vec4 clone() and set(), which differ on transform vectors (loc,
 * scale: T3DVec3, scale stored with SCALE_FACTOR). The other vector methods
 * are in Bindings.hx.
 */
class VecCallConverter implements ICallConverter {
    static var vecMethods = ["clone", "set"];

    public function new() {}

    public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
        // Check if this is a known Vec method
        if (!Lambda.has(vecMethods, method)) return null;

        // Check if the object type is a Vec type
        var objType = ctx.getExprType(obj);
        if (objType == null || !objType.startsWith("Vec")) return null;

        var objIR = ctx.exprToIR(obj);
        return convert(method, objIR, args, objType);
    }

    function convert(method:String, objIR:IRNode, args:Array<IRNode>, vecType:String):IRNode {
        // Determine C type based on Haxe type
        var cType = switch (vecType) {
            case "Vec4": "ArmVec4";
            case "Vec3": "ArmVec3";
            default: "ArmVec2";
        };
        var is3D = (vecType == "Vec3" || vecType == "Vec4");

        // Template uses {v} for vector, {0} for first arg
        var c_code = switch (method) {
            case "clone":
                // Clone creates a copy - type depends on target
                // Special case: transform.scale is stored with SCALE_FACTOR, so multiply by inverse to get Blender values
                var isScaleClone = (objIR.type == "field_access" && objIR.value == "transform.scale");
                if (isScaleClone) {
                    var inv = Constants.SCALE_FACTOR_INV_C;
                    if (cType == "ArmVec4") '($cType){{v}.x*$inv, {v}.y*$inv, {v}.z*$inv, 1.0f}';
                    else if (cType == "ArmVec3") '($cType){{v}.x*$inv, {v}.y*$inv, {v}.z*$inv}';
                    else '($cType){{v}.x*$inv, {v}.y*$inv}';
                } else {
                    if (cType == "ArmVec4") "(" + cType + "){{v}.x, {v}.y, {v}.z, 1.0f}";
                    else if (cType == "ArmVec3") "(" + cType + "){{v}.x, {v}.y, {v}.z}";
                    else "(" + cType + "){{v}.x, {v}.y}";
                }
            case "set":
                // set(x, y, z) or set(x, y, z, w) - modifies in-place
                // Check if this is a transform vector (loc, scale) which uses T3DVec3
                var isTransformVec = (objIR.type == "field_access" && objIR.value != null &&
                    (StringTools.endsWith(objIR.value, ".loc") || StringTools.endsWith(objIR.value, ".scale")));
                if (isTransformVec) {
                    // Transform vectors are T3DVec3 (3 components, stored in .v array)
                    "{ {vraw}.v[0] = {0}; {vraw}.v[1] = {1}; {vraw}.v[2] = {2}; }";
                } else if (cType == "ArmVec4") {
                    "{ {vraw}.x = {0}; {vraw}.y = {1}; {vraw}.z = {2}; {vraw}.w = {3}; }";
                } else if (cType == "ArmVec3") {
                    "{ {vraw}.x = {0}; {vraw}.y = {1}; {vraw}.z = {2}; }";
                } else {
                    "{ {vraw}.x = {0}; {vraw}.y = {1}; }";
                }
            default:
                null;
        };

        if (c_code == null) return N64MacroBase.unsupported('$vecType.$method() is not supported on N64 yet');

        return {
            type: "vec_call",
            c_code: c_code,
            object: objIR,
            args: args,
            props: {
                vecType: vecType,
                cType: cType,
                is3D: is3D
            }
        };
    }
}

#end