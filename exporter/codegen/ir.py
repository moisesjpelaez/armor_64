"""
Reading the macros' IR (n64_traits.json, n64_autoloads.json...): the files and
their node trees. Used by the trait, autoload, tween, signal and audio code
generation and the limits.
"""

import json
import os

# The IR version the macros write (N64MacroBase.writeJsonFile) and this reads
IR_VERSION = 1


def load_ir_json(filename: str, default_key: str, build_dir: str = None) -> dict:
    """An IR file of the Haxe build (`{default_key: {}}` when the build wrote none).
    A file that can't be read or has another IR version stops the export: the
    macros and the exporter would disagree."""
    if build_dir is None:
        import arm.utils
        build_dir = arm.utils.build_dir()
    for path in (os.path.join(build_dir, filename), os.path.join(build_dir, "build", filename),
                 os.path.join(build_dir, "debug", filename)):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise ValueError(f'{path} can\'t be read: {e}') from None
        if data.get("ir_version", 0) != IR_VERSION:
            raise ValueError(f'{path} has IR version {data.get("ir_version", 0)}, the exporter reads {IR_VERSION}: '
                             f'the macros (Sources/armor64) and the exporter are from different versions')
        return data
    return {"ir_version": IR_VERSION, default_key: {}}


def walk_ir(value):
    """Every IR node in a tree (any depth: children, args, bodies, props...)."""
    if isinstance(value, dict):
        if "type" in value:
            yield value
        for v in value.values():
            yield from walk_ir(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_ir(v)


def nodes_of_type(value, *types: str) -> list:
    """The nodes of the given types in an IR tree, in tree order."""
    return [node for node in walk_ir(value) if node.get("type") in types]
