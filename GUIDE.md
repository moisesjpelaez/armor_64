# Game code on N64

What your Haxe traits can do when Armor 64 builds them for the Nintendo 64: what works as on Krom, where the N64 works differently on purpose, and what isn't supported yet. Armor 64 reads your code with the Haxe compiler's types, so aliases, imports, `using`, inheritance and inferred types all work.

Anything it can't build is reported as an `N64: ...` warning at its line when you build, and left out of the game (an error with `n64_strict`, see [README.md](README.md)). Nothing is dropped silently.

## Traits

- **Lifecycles** work as in Iron: every `notifyOnAdd`, `notifyOnInit`, `notifyOnUpdate`, `notifyOnFixedUpdate`, `notifyOnLateUpdate`, `notifyOnRender2D` and `notifyOnRemove` call registers its function once more (registering the same function twice runs it twice), and `removeUpdate(f)` and the others take one registration back. A function registered from a method or a callback starts then; an init registered once the trait is ready runs before its next update.
- **The constructor** runs when the trait is added, before its add functions; `super()` runs the parent's.
- **Inheritance**: traits extend traits to any depth; overridden methods (and accessors) run the child's version, `super.method()` the parent's.
- **Properties**: getters and setters run as in Haxe (`x = v`, `x += v`, `x++`, reads), also on another trait and on autoloads; a property with only accessors has no storage.
- **Other traits**: a member typed as a trait (e.g. `var player: Player`, set from an autoload) gives access to its fields, properties and methods.
- **Statics**: a trait's static vars are shared by its instances; its static functions can be called from anywhere.
- **Autoloads**: classes with `@:n64Autoload` (optionally `@:n64Autoload(order=N)` for the order of their static initializers at boot) are singletons whose statics and functions every trait can use; call their `init()` yourself, as in Armory.

## Values

- **Numbers**: `Int` is 32-bit; `Float` is single precision (the N64's), so results can differ from Krom's doubles in the last digits. `Int / Int`, `%`, `>>>`, `Math.floor`/`ceil`/`round` give Haxe's results.
- **Strings**: concatenation, interpolation (`'Score: $score'`), `Std.string`, `==`, `StringTools.startsWith`/`endsWith`/`contains`. Numbers print as Haxe prints them (`1.5`, `2`, `0.1`), `Bool` as `true`/`false`, `null` as `null`.
- **Containers**: `Array` of `Int`, `Float`, `Bool` or `String` (`push`, `pop`, `[i]`, `length`, `resize(0)`) and `Map<String, ...>` (`set`, `get`, `exists`, `remove`, `clear`, `[key]`). They have fixed capacities (see `limits.txt`).
- **Vectors**: `Vec2`, `Vec3`, `Vec4`, with `length`, `mult`, `add`, `sub`, `dot`, `normalize`, `set`, `clone`.
- **Math**: `Math.sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `sqrt`, `pow`, `abs`, `min`, `max`, `exp`, `log`, `floor`, `ceil`, `round`; `Std.int`, `Std.parseFloat`.
- **Callbacks**: anonymous functions and methods passed to tweens, signals, buttons, lifecycles and your own methods; they can use the locals and parameters of the function around them.

## Engine APIs

- **Scenes**: `Scene.setActive(name)`, `Scene.active.raw.name`, `Scene.active.getGroup(name).length`.
- **Objects**: `object.remove()`, `object.getAnimation()` (`play`, `pause`, `resume`), `object.parent`, `getTrait(RigidBody)`.
- **Transforms**: `loc`, `scale`, `rot` (read and write, in Blender's axes and units), `translate`, `rotate`, `move`, `setRotation`, `reset`, `buildMatrix`, `look`, `right`, `up`, `worldx`, `worldy`, `worldz`.
- **Time**: `Time.delta`, `Time.scale`, `Time.fixedStep`, `Time.time()`.
- **Input**: `Input.getGamepad()`: `started`, `down`, `released` with button names (`a`, `b`, `z`, `start`, `l`, `r`, `dup`, `cup`... and the Xbox/PlayStation names, mapped to the N64's), `leftStick` and `rightStick` (both the N64's one stick). The keyboard and the mouse are never pressed (reported once).
- **Physics** (`RigidBody`, Oimo): `applyForce`, `applyImpulse`, `setLinearVelocity`, `setAngularVelocity`, `getLinearVelocity`, `getAngularVelocity`, `notifyOnContact`, `removeContact`.
- **Tweens** (`armory.system.Tween`): `float`, `vec4`, `delay`, `start`, `pause`, `stop`, with the eases Linear, Quad, Cubic and Sine (In, Out, InOut).
- **Signals** (`armory.system.Signal`): `connect`, `disconnect`, `emit`, static or per instance, with any arguments.
- **UI** (Koui canvases): `getElementAs` (with or without a scene), `setScene`, `notifyOnReady` (ready at once on N64), `label.text`, `visible` on elements and layouts, buttons' `onPressed`, `onReleased`, `onFocus`, `grabFocus`.
- **Audio** (Aura): `Aura.createUncompBufferChannel`, sound handles' `play`, `pause`, `stop`, `setVolume`, `getVolume`, `finished`, `Aura.mixChannels[name].setVolume`. `Aura.init` and `Aura.loadAssets` work as loading done (everything is in the ROM). `Assets.loadEverything(done)` calls `done` at once.
- **2D drawing** (`notifyOnRender2D`): `fillRect`, `color`.

## Where the N64 works differently

These are on purpose (they keep the game fast and its memory fixed), and some are reported when they can matter:

- **Vectors are values.** `var p = transform.loc; p.x = 1;` doesn't move the object (it does on Krom); `v.add(w);` as a statement doesn't change `v`. Write the result back: `transform.loc = p`, `v = v.add(w)`. `transform.loc.add(...)` and other calls that change a transform's vector in place are reported.
- **A callback's captured values are copies**, taken when the callback is registered: changing the variable afterwards, or in the callback, isn't seen by the other side, and a callback registered in a loop sees the last values. Each case is reported at build time; the game also reports, in Ares' log, a capturing function registered again while still registered.
- **An anonymous signal handler connects once**: connecting the same handler again (e.g. from a function called twice) doesn't add a second one, as it would on Krom (reported in Ares' log).
- **Lifecycle order**: functions of one kind run in a trait's source order (its parent's first), where Iron runs them in the order they were registered across all traits.
- **Built strings live in a few rotating buffers**: a string built by concatenation and stored in a member is overwritten after several more concatenations in the same file. Labels copy their text, so `label.text = 'Score: $score'` is fine.
- **`Null<Int>`, `Null<Float>`, `Null<Bool>`** can't be null: `??` on them is reported.
- **Traces** are in every build except Publish with No Traces.

## Not supported yet

Reported at their line when you build:

- `switch`, `try`/`catch`, `throw`, `cast`; `for` loops over anything but integer ranges; non-empty array literals; object literals.
- Plain (non-trait) game classes as objects (`new MyClass()`), and properties on them.
- Locals and members of types the N64 doesn't have (they're left out with every use of them).
- `getTrait` (except `RigidBody` and `KouiCanvas`), `addTrait`, `removeTrait`, `getChildren`, `getChild`; spawning objects.
- Passing a function to a method of another trait instance; `notifyOnX`/`removeX` on another trait; a static `Tween` in a trait.
- `notifyOnRender` (3D drawing); eases other than Linear, Quad, Cubic and Sine (linear instead); an ease chosen at runtime (linear).
- Keyboard and mouse input; gamepad buttons the N64 doesn't have (e.g. `touchpad`).
- `Std.random`, `Math.random`, `String.split` and the rest of the standard library not listed above.
