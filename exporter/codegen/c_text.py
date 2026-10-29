"""
C literals for text the exporter writes into the generated C (trait values,
Haxe strings, scene and UI names). No Blender imports: the golden tests load it.
"""

_ESCAPES = {'"': '\\"', '\\': '\\\\', '\n': '\\n', '\r': '\\r', '\t': '\\t'}


def c_string(value) -> str:
    """A C string literal: quotes, backslashes and control characters escaped;
    other characters (UTF-8 text) stay as they are."""
    out = []
    for ch in str(value):
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append(f'\\{ord(ch):03o}')
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'
