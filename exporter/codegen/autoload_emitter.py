"""
Autoload Emitter - Extends TraitEmitter for autoload classes.

Autoloads use global variables prefixed with c_name instead of a data pointer.
"""

from typing import Dict, List

from arm.n64.codegen.trait_emitter import TraitEmitter


class AutoloadEmitter(TraitEmitter):
    """Emits C code from IR nodes for autoload classes.

    Unlike traits which use a data pointer (data->member), autoloads use
    global variables prefixed with c_name (music_volume, music_play(), etc.):
    Haxe resolves members and functions, this only prints members.
    """

    def __init__(self, autoload_name: str, c_name: str, member_names: List[str]):
        super().__init__(autoload_name, c_name, member_names, is_trait=False)

    def emit_member(self, node: Dict) -> str:
        """Autoload member access: c_name_member_name (global variable)"""
        name = node.get("value", "")
        return f"{self.c_name}_{name}"
