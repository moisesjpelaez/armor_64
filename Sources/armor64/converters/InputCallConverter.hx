package armor64.converters;

#if macro
import haxe.macro.Expr;
import armor64.IRTypes;
import armor64.TypedIndex;
import armor64.mapping.ButtonMap;
import armor64.converters.ICallConverter;

/**
 * Converts input API calls to C N64 input functions.
 * Handles: gamepad.started("a"), keyboard.down("w"), mouse.released("left"), etc.
 * Maps buttons to N64_BTN_* constants.
 *
 * Input checks stay where the game wrote them, as in Armory: `if (gamepad.started("a"))`
 * in update() becomes `if (input_started(N64_BTN_A))` in the same place, so guards like
 * `if (!active) return;` block it as they do on other targets.
 */
class InputCallConverter implements ICallConverter {
	public function new() {}

	/**
	 * Whether an expression is a gamepad, from its type.
	 */
	public static function isGamepad(e:Expr):Bool {
		return TypedIndex.receiverPath(e) == "iron.system.Gamepad";
	}

	public function tryConvert(obj:Expr, method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
		// The receiver's type decides
		return switch (TypedIndex.receiverPath(obj)) {
			case "iron.system.Gamepad", "iron.system.Input":
				convert(method, args, rawParams, ctx);
			case "iron.system.Keyboard", "iron.system.Mouse":
				// Safely ignore PC inputs on N64
				{
					type: "input_call",
					c_code: "0",
					method: method
				};
			default:
				null;
		};
	}

	function convert(method:String, args:Array<IRNode>, rawParams:Array<Expr>, ctx:IExtractorContext):IRNode {
		var meta = ctx.getMeta();
		meta.uses_input = true;

		// Map button to N64_BTN_* constant
		var n64Button = "N64_BTN_COUNT"; // default button (ignored by input system)
		if (args.length > 0) {
			var btnArg = args[0];
			if (btnArg.type == "string") {
				var btnName = btnArg.value;
				n64Button = ButtonMap.toN64Const(btnName);
			}
		}

		// Emit ready-to-use C code
		// Note: down() returns float (intensity 0.0-1.0), started/released return bool
		var cCode = switch (method) {
			case "started": 'input_started($n64Button)';
			case "released": 'input_released($n64Button)';
			case "down": 'input_down($n64Button)';
			case "getStickX": 'input_stick_x()';
			case "getStickY": 'input_stick_y()';
			default: '0'; // unsupported input method - return neutral value
		};

		return {
			type: "input_call",
			c_code: cCode,
			method: method
		};
	}
}
#end
