"""
Koui theme parser: reads .ksn theme files as Koui does at compile time
(Subprojects/Koui/Sources/koui/theme/parser/ThemeParser.hx), so the N64 UI gets
the same styles as Krom. Used by ui_exporter.

Grammar (tabs for indentation, `//` comments on their own line):
    @rules:                       property types: Asset, Bool, Color, Float, Int,
        name: Type                String; `?group:` is an optional group
    @globals:                     values other properties reference
        NAME: value
    selector > parent:            a selector's default state, extending parent
    selector!state:               a state (hover, click, disabled...)
        group:
            property: value       or `$selector.path`, `$@globals.NAME`

Resolution, as Koui: references first (against what each selector itself
defines), then extensions, then states. A selector's state takes its own values,
then its own default state's, then its parent's same state, and every selector
gets the states of the selectors it extends. Errors stop the export, as they
stop Koui's build.
"""

import re


_INDENT = re.compile(r'^(\t*)(.*)$')
_EMPTY = re.compile(r'^[ \t]*(//.*)?$')
_LINE = re.compile(r'^([\w\-]+)(![\w\-]+)?( *> *([\w\-]+))? *: *(.*)$')
_RULE = re.compile(r'^(\?)?([\w\-]+) *: *(.*)$')
_RULES = re.compile(r'^@rules( )*:( )*$')
_GLOBALS = re.compile(r'^@globals( )*:( )*$')
_COLOR = re.compile(r'^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})?$', re.I)
_INT = re.compile(r'^\s*([+-]?)(0x[0-9a-f]+|\d+)', re.I)
_FLOAT = re.compile(r'^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?')

_TYPES = ('Asset', 'Bool', 'Color', 'Float', 'Int', 'String')
_GLOBALS_SELECTOR = '@globals!default'


class ThemeError(ValueError):
    pass


class _Value:
    """A property value: its text, its type (from @rules), and what it parses to."""
    def __init__(self, path, type_, text, where):
        self.path = path
        self.where = where        # 'file:line', for errors found when resolving
        self.type = type_
        self.text = text
        self.value = None
        self.reference = text[1:] if text.startswith('$') else None
        self.inherited = False

    def copy(self, inherited):
        value = _Value.__new__(_Value)
        value.__dict__.update(self.__dict__)
        value.inherited = inherited
        return value


class _Group:
    def __init__(self, optional=False):
        self.optional = optional
        self.map = {}


class _Selector:
    def __init__(self, pure_name, state, extending, where):
        self.where = where
        self.pure_name = pure_name
        self.state = state
        self.name = pure_name + state
        self.map = {}
        # `extending` is only set on default states; states follow their default's
        self.extending = extending + '!default' if extending else None
        self.resolved = extending is None and state == '!default'
        self.has_all_states = extending is None


class KouiThemeParser:
    """Parse theme files with parse_file() (Koui's main theme first), then resolve()."""

    def __init__(self):
        self.rules = _Group()
        self.selectors = {}       # 'name!state' -> _Selector
        self.states = {}          # pure name -> ['!default', '!hover', ...]
        self._references = []
        self._styles = {}

    # -------------------------------------------------------------------------
    # Parsing (ThemeParser.parseFile / parseLine)
    # -------------------------------------------------------------------------

    def parse_file(self, path: str):
        first_file = not self.selectors and not self.rules.map
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()

        self._found_rules = not first_file
        self._in_rules = False
        self._in_globals = False
        self._indents = [0]
        self._stack = []          # Open groups/selectors, innermost last
        self._path = []           # Names of the current path, innermost last
        self._last = None         # 'selector', 'group' or 'definition'

        for number, line in enumerate(lines, 1):
            if _EMPTY.match(line):
                continue
            self._where = f'{path}:{number}'
            try:
                self._parse_line(line)
            except ThemeError as e:
                raise ThemeError(f'{path}:{number}: {e}\n    {line}') from None

    def _parse_line(self, line):
        if not self._found_rules:
            if not _RULES.match(line):
                raise ThemeError('the main theme file must start with @rules')
            self._found_rules = self._in_rules = True
            self._stack = [self.rules]
            return

        m = _INDENT.match(line)
        indent, line = len(m.group(1)), m.group(2)
        if line.lstrip() != line:
            raise ThemeError('tabs must be used for indentation')
        last_indent = self._indents[-1]
        self._indent(indent)

        if indent == 0:
            self._in_rules = self._in_globals = False

        if self._in_rules:
            m = _RULE.match(line)
            if not m:
                raise ThemeError('syntax error')
            self._parse_rule(m, indent, last_indent)
        elif _GLOBALS.match(line):
            self._in_globals = True
            self._open_selector('@globals', None, None, '')
        elif m := _LINE.match(line):
            self._parse_property(m, indent, last_indent)
        else:
            raise ThemeError('syntax error')

    def _indent(self, indent):
        last = self._indents[-1]
        if indent > last + 1 or (indent > last and self._last == 'definition'):
            raise ThemeError('wrong indentation')
        if indent > last:
            self._indents.append(indent)
        else:
            while indent < self._indents[-1]:
                self._indents.pop()

    def _unindent(self, indent, last_indent):
        while indent <= last_indent:
            if indent != last_indent:
                self._stack.pop()
            self._path.pop()
            indent += 1

    def _parse_rule(self, m, indent, last_indent):
        optional, name, type_ = m.group(1), m.group(2), m.group(3)
        if not type_:
            self._open_group(name, indent, last_indent).optional |= optional == '?'
            return
        self._unindent(indent, last_indent)
        self._path.append(name)
        if type_ not in _TYPES:
            raise ThemeError(f'type "{type_}" of rule "{".".join(self._path)}" is not supported')
        self._stack[-1].map[name] = type_
        self._last = 'definition'

    def _parse_property(self, m, indent, last_indent):
        name, state, extending, text = m.group(1), m.group(2), m.group(4), m.group(5)
        if indent == 0:
            self._open_selector(name, state, extending, text)
            return
        if extending is not None:
            raise ThemeError('only selectors can extend')
        if not text:
            if self._in_globals:
                raise ThemeError('groups are not allowed in @globals')
            group = self._open_group(name, indent, last_indent)
            rule = self._rule(self._path[1:])
            if not isinstance(rule, _Group):
                raise ThemeError(f'"{".".join(self._path)}" is not a group in @rules')
            group.optional = rule.optional
            return
        self._unindent(indent, last_indent)
        self._path.append(name)
        self._stack[-1].map[name] = self._new_value(text)
        self._last = 'definition'

    def _open_selector(self, name, state, extending, text):
        self._path = [name]
        if name in ('_root', '@globals'):
            if extending is not None:
                raise ThemeError(f'{name} can\'t extend another selector')
        elif state is None and extending is None:
            raise ThemeError('a default state selector must extend _root or one of its extensions')
        if name == extending:
            raise ThemeError('a selector can\'t extend itself')
        if extending is not None and state not in (None, '!default'):
            raise ThemeError('an extending selector can only be a default state')
        if text:
            raise ThemeError('a selector can\'t have a value')

        state = state or '!default'
        self.states.setdefault(name, []).append(state)
        # Opened again (a later file overriding it): add to it, keep what it extends
        selector = self.selectors.get(name + state)
        if selector is None:
            selector = self.selectors[name + state] = _Selector(name, state, extending, self._where)
        self._stack = [selector]
        self._last = 'selector'

    def _open_group(self, name, indent, last_indent):
        self._unindent(indent, last_indent)
        self._path.append(name)
        group = self._stack[-1].map.get(name)
        if group is None:
            group = self._stack[-1].map[name] = _Group()
        elif not isinstance(group, _Group):
            raise ThemeError(f'"{name}" is a value, not a group')
        self._stack.append(group)
        self._last = 'group'
        return group

    def _rule(self, path):
        node = self.rules
        for name in path:
            node = node.map.get(name) if isinstance(node, _Group) else None
            if node is None:
                raise ThemeError(f'"{".".join(path)}" is not in @rules')
        return node

    def _new_value(self, text):
        path = '.'.join(self._path)
        if self._in_globals:
            return _Value(path, '', text, self._where)
        type_ = self._rule(self._path[1:])
        if isinstance(type_, _Group):
            raise ThemeError(f'"{path}" is a group in @rules, not a value')
        value = _Value(path, type_, text, self._where)
        if value.reference:
            self._references.append(value)
        else:
            value.value = _parse_value(type_, text)
        return value

    # -------------------------------------------------------------------------
    # Resolution (ThemeParser.resolveTheme)
    # -------------------------------------------------------------------------

    def resolve(self):
        for value in self._references:
            try:
                self._resolve_reference(value, set())
            except ThemeError as e:
                raise ThemeError(f'{value.where}: {value.path}: {e}') from None
        for selector in list(self.selectors.values()):
            try:
                self._create_required_states(selector)
            except ThemeError as e:
                raise ThemeError(f'{selector.where}: {e}') from None
        for selector in self.selectors.values():
            try:
                self._resolve_extensions(selector)
                self._resolve_states(selector)
            except ThemeError as e:
                raise ThemeError(f'{selector.where}: {selector.name}: {e}') from None
        self.selectors.pop(_GLOBALS_SELECTOR, None)
        self.states.pop('@globals', None)
        self._styles = {name: _plain(selector.map) for name, selector in self.selectors.items()}

    def _resolve_reference(self, value, visiting):
        if value.reference is None:
            return
        if id(value) in visiting:
            raise ThemeError(f'circular reference ${value.reference}')
        visiting.add(id(value))
        path = value.reference.split('.')
        selector = self.selectors.get(path[0]) or self.selectors.get(path[0] + '!default')
        if selector is None:
            raise ThemeError(f'referenced selector "{path[0]}" does not exist')
        node = selector
        for name in path[1:]:
            node = node.map.get(name) if isinstance(node, (_Group, _Selector)) else None
            if node is None:
                raise ThemeError(f'"${value.reference}" does not exist')
        if not isinstance(node, _Value):
            raise ThemeError(f'"${value.reference}" is a group, not a value')
        self._resolve_reference(node, visiting)
        value.text = node.text
        value.reference = None
        value.value = _parse_value(value.type, value.text)

    def _create_required_states(self, selector):
        """Give a selector the states of the selectors it extends."""
        if selector.extending is None or selector.state != '!default' or selector.has_all_states:
            return
        extending = self.selectors.get(selector.extending)
        if extending is None:
            raise ThemeError(f'{selector.name} extends {selector.extending}, which does not exist')
        if not extending.has_all_states:
            self._create_required_states(extending)
        for state in self.states[extending.pure_name]:
            own = self.selectors.get(selector.pure_name + state)
            if own is None:
                self.states[selector.pure_name].append(state)
                own = self.selectors[selector.pure_name + state] = _Selector(selector.pure_name, state, None, selector.where)
            own.has_all_states = True

    def _resolve_extensions(self, selector):
        if selector.resolved:
            return
        if selector.extending is None:
            self._resolve_states(selector)
            return
        extending = self.selectors.get(selector.extending)
        if extending is None:
            raise ThemeError(f'extends {selector.extending}, which does not exist')
        selector.map = self._merge_resolved(selector, extending)
        selector.resolved = True

    def _resolve_states(self, selector):
        if selector.resolved or selector.state == '!default':
            return
        default = self.selectors.get(selector.pure_name + '!default')
        if default is None:
            raise ThemeError('missing a default state')
        self._resolve_extensions(default)
        base = default.map
        # The parent's same state, under this selector's own default values
        if default.extending is not None:
            same_state = self.selectors.get(default.extending.split('!')[0] + selector.state)
            if same_state is not None:
                base = self._merge_resolved(default, same_state)
        selector.map = _merge(base, selector.map)
        selector.resolved = True

    def _merge_resolved(self, selector, extending):
        self._resolve_extensions(extending)
        self._resolve_states(extending)
        return _merge(extending.map, selector.map)

    # -------------------------------------------------------------------------
    # Styles
    # -------------------------------------------------------------------------

    def style(self, tid: str, state: str = 'default') -> dict:
        """The resolved style of a theme ID (an element's tID) in a state, as nested
        dicts: colors are (r, g, b, a), the other values bool/int/float/str. A state
        the selector doesn't have is its default."""
        style = self._styles.get(f'{tid}!{state}') or self._styles.get(f'{tid}!default')
        if style is None:
            raise ThemeError(f'the Koui themes have no style "{tid}"')
        return style


def _merge(base: dict, add: dict) -> dict:
    """`add` over a copy of `base` (Koui's mergeMaps): values `add` only inherited
    don't replace `base`'s, and `add` can only set what `base` has (or optional groups)."""
    result = _clone(base)
    for key, node in add.items():
        if isinstance(node, _Value):
            if node.inherited:
                continue
            if key not in result:
                raise ThemeError(f'property "{key}" is not in what it extends')
            result[key] = node.copy(inherited=False)
        elif isinstance(node, _Group):
            if key not in result:
                if not node.optional:
                    raise ThemeError(f'group "{key}" is not in what it extends')
                result[key] = node
                continue
            merged = _Group(result[key].optional)
            merged.map = _merge(result[key].map, node.map)
            result[key] = merged
    return result


def _clone(nodes: dict) -> dict:
    """A copy with every value marked inherited."""
    result = {}
    for key, node in nodes.items():
        if isinstance(node, _Value):
            result[key] = node.copy(inherited=True)
        else:
            group = _Group(node.optional)
            group.map = _clone(node.map)
            result[key] = group
    return result


def _plain(nodes: dict) -> dict:
    return {key: node.value if isinstance(node, _Value) else _plain(node.map) for key, node in nodes.items()}


def _parse_value(type_, text):
    if type_ == 'Bool':
        if text.lower() not in ('true', 'false'):
            raise ThemeError(f'invalid Bool "{text}"')
        return text.lower() == 'true'
    if type_ == 'Color':
        m = _COLOR.match(text)
        if not m:
            raise ThemeError(f'invalid Color "{text}" (#rrggbb or #rrggbbaa)')
        return tuple(int(c, 16) for c in m.groups(default='ff'))
    if type_ == 'Float':
        m = _FLOAT.match(text)
        if not m:
            raise ThemeError(f'invalid Float "{text}"')
        return float(m.group(0))
    if type_ == 'Int':
        m = _INT.match(text)
        if not m:
            raise ThemeError(f'invalid Int "{text}"')
        digits = m.group(2)
        number = int(digits, 16) if digits[:2].lower() == '0x' else int(digits)
        return -number if m.group(1) == '-' else number
    # String, Asset
    if len(text) < 2 or not text.startswith('"') or not text.endswith('"'):
        raise ThemeError(f'strings must be in double quotes: {text}')
    return text[1:-1].encode('latin-1', 'backslashreplace').decode('unicode_escape')
