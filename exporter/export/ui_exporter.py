"""
UI exporter: turns the koui_editor canvases of the exported scenes into the N64 UI.

Each canvas JSON (Bundled/koui_canvas) is flattened to fixed positions (labels,
images, buttons, panels, and groups for containers), with the colors, fonts and
borders read from the Koui themes (koui_theme_parser). Writes the ui/*_defs.h and
ui/*_data.h files of the runtime's ui/ module, copies the fonts and images, and
gives build_runner their Makefile rules. Used by exporter.py.
"""

import os
import json
import glob

import arm.utils
import armor64.utils as n64_utils
import arm.log as log
from armor64.export.koui_theme_parser import KouiThemeParser
from armor64.codegen.c_text import c_string


# =============================================================================
# Constants
# =============================================================================

# Anchor enum values (matches Koui)
ANCHOR_TOP_LEFT = 0
ANCHOR_TOP_CENTER = 1
ANCHOR_TOP_RIGHT = 2
ANCHOR_MIDDLE_LEFT = 3
ANCHOR_MIDDLE_CENTER = 4
ANCHOR_MIDDLE_RIGHT = 5
ANCHOR_BOTTOM_LEFT = 6
ANCHOR_BOTTOM_CENTER = 7
ANCHOR_BOTTOM_RIGHT = 8

# Default UI values
DEFAULT_FONT_SIZE = 15
DEFAULT_TEXT_COLOR = (221, 221, 221, 255)  # #dddddd

# Kha font size (pixel height) -> N64 font. Measured, not derived: chosen so text
# looks the same size as on Krom, and its baseline lands where Koui draws it.
MKFONT_SIZE_SCALE = 0.82      # Kha size -> mkfont's --size (points)
MKFONT_LINE_HEIGHT = 1.22     # mkfont size -> drawn line height (pixels)
MKFONT_BASELINE = 0.80        # line height -> baseline, from the line's top


def _mkfont_size(kha_size):
    return max(8, int(kha_size * MKFONT_SIZE_SCALE))


def detect_ui_canvas(exporter):
    """Detect and parse Koui canvas JSON files referenced by scenes.

    Only parses canvases that are actually attached to scenes via UI Canvas trait.
    Sets exporter.has_ui = True if any canvas with labels or images is found.
    Stores parsed data in exporter.ui_canvas_data for code generation.
    Also parses Koui theme files to extract font size and text color per label.

    Layouts (RowLayout, ColLayout) are flattened at export time - their children
    are extracted with pre-computed absolute positions for N64's fixed resolution.

    Args:
        exporter: N64Exporter instance to update with UI state
    """
    bundled_dir = os.path.join(arm.utils.get_fp(), 'Bundled', 'koui_canvas')
    if not os.path.exists(bundled_dir):
        return

    # Collect canvas names referenced by scenes
    referenced_canvases = set()
    for scene_name, data in exporter.scene_data.items():
        canvas_name = data.get('canvas')
        if canvas_name:
            referenced_canvases.add(canvas_name)

    if not referenced_canvases:
        return  # No scenes use UI Canvas trait

    # Parse Koui theme files for style information
    _parse_koui_themes(exporter)

    # Sorted: the generated UI files must not change between exports (make)
    for canvas_name in sorted(referenced_canvases):
        json_path = os.path.join(bundled_dir, f'{canvas_name}.json')
        if not os.path.exists(json_path):
            log.warn(f'UI Canvas "{canvas_name}" not found at {json_path}')
            continue

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                canvas_data = json.load(f)

            # N64 always uses 320x240 display resolution
            canvas_width = 320
            canvas_height = 240

            labels = []
            images = []
            buttons = []
            panels = []
            groups = []       # Groups with their child indices
            elements = []     # Unified elements array (maps Haxe index to image/group)

            ui_scenes = []  # Track per-Koui-scene element ranges

            for scene in canvas_data.get('scenes', []):
                scene_key = scene.get('key', '')
                scene_active = scene.get('active', True)
                scene_elements = scene.get('elements', [])

                # Track range start indices before processing this scene
                label_start = len(labels)
                image_start = len(images)
                button_start = len(buttons)
                panel_start = len(panels)

                # Build element lookup by key and parent-child relationships
                elem_by_key = {e['key']: e for e in scene_elements}
                children_by_parent = {}
                root_elements = []

                for elem in scene_elements:
                    parent_key = elem.get('parentKey')
                    if parent_key:
                        if parent_key not in children_by_parent:
                            children_by_parent[parent_key] = []
                        children_by_parent[parent_key].append(elem)
                    else:
                        root_elements.append(elem)

                # Process root elements recursively, flattening layouts
                # but tracking groups and unified elements array.
                # Use scene_key as the root parent_path so that element keys
                # are scene-scoped (e.g. "Paused/buttons/menu_button" vs
                # "Win/buttons/menu_button").
                for elem in root_elements:
                    _flatten_element(
                        exporter, elem, elem_by_key, children_by_parent,
                        canvas_width, canvas_height,
                        0, 0,  # parent_abs_x, parent_abs_y
                        labels, images, buttons, panels, groups, elements,
                        parent_path=scene_key
                    )

                # Record this Koui scene's element ranges
                ui_scenes.append({
                    'key': scene_key,
                    'active': scene_active,
                    'first_label': label_start,
                    'label_count': len(labels) - label_start,
                    'first_image': image_start,
                    'image_count': len(images) - image_start,
                    'first_button': button_start,
                    'button_count': len(buttons) - button_start,
                    'first_panel': panel_start,
                    'panel_count': len(panels) - panel_start,
                })

            # Resolve button focus graph indices after all buttons collected
            resolve_button_focus(buttons, ui_scenes)

            if labels or images or buttons or panels or groups:
                exporter.ui_canvas_data[canvas_name] = {
                    'width': canvas_width,
                    'height': canvas_height,
                    'labels': labels,
                    'images': images,
                    'buttons': buttons,
                    'panels': panels,
                    'groups': groups,
                    'elements': elements,
                    'ui_scenes': ui_scenes,
                }
                exporter.has_ui = True
                log.info(f'Found UI canvas: {canvas_name} with {len(labels)} label(s), {len(images)} image(s), {len(buttons)} button(s), {len(panels)} panel(s), {len(groups)} group(s), {len(elements)} element(s)')

                # Validate element counts fit in uint8_t indices used by C templates
                for type_name, count in [('labels', len(labels)), ('images', len(images)),
                                         ('buttons', len(buttons)), ('panels', len(panels)),
                                         ('groups', len(groups))]:
                    if count > 255:
                        log.warn(f'UI canvas "{canvas_name}" has {count} {type_name}, '
                                 f'exceeding uint8_t index limit (255)')

        except Exception as e:
            log.warn(f'Failed to parse Koui canvas {json_path}: {e}')


def _calc_anchor_position(pos_x, pos_y, width, height, anchor, container_width, container_height):
    """Calculate absolute position based on anchor within a container.

    Args:
        pos_x, pos_y: Element's relative position
        width, height: Element's dimensions
        anchor: Anchor enum value (0-8)
        container_width, container_height: Parent container dimensions

    Returns:
        (abs_x, abs_y): Absolute position within container
    """
    # Calculate X based on anchor
    if anchor in (ANCHOR_TOP_LEFT, ANCHOR_MIDDLE_LEFT, ANCHOR_BOTTOM_LEFT):
        abs_x = pos_x
    elif anchor in (ANCHOR_TOP_CENTER, ANCHOR_MIDDLE_CENTER, ANCHOR_BOTTOM_CENTER):
        abs_x = (container_width // 2) - (width // 2) + pos_x
    elif anchor in (ANCHOR_TOP_RIGHT, ANCHOR_MIDDLE_RIGHT, ANCHOR_BOTTOM_RIGHT):
        abs_x = container_width - width + pos_x
    else:
        abs_x = pos_x

    # Calculate Y based on anchor
    if anchor in (ANCHOR_TOP_LEFT, ANCHOR_TOP_CENTER, ANCHOR_TOP_RIGHT):
        abs_y = pos_y
    elif anchor in (ANCHOR_MIDDLE_LEFT, ANCHOR_MIDDLE_CENTER, ANCHOR_MIDDLE_RIGHT):
        abs_y = (container_height // 2) - (height // 2) + pos_y
    elif anchor in (ANCHOR_BOTTOM_LEFT, ANCHOR_BOTTOM_CENTER, ANCHOR_BOTTOM_RIGHT):
        abs_y = container_height - height + pos_y
    else:
        abs_y = pos_y

    return abs_x, abs_y


# =============================================================================
# Element Flatten Helpers
# =============================================================================

def _build_full_path(parent_path: str, key: str) -> str:
    """Build full element path like 'parent/child' for Koui-style access."""
    if parent_path:
        return f"{parent_path}/{key}"
    return key


def _apply_opacity(color_tuple, opacity):
    """Pre-multiply opacity (0.0\u20131.0) into the alpha channel of an RGBA tuple."""
    r, g, b, a = color_tuple
    return (r, g, b, int(a * max(0.0, min(1.0, opacity))))


def _calc_element_alignment(anchor, elem_width, container_width, final_x, json_align_h=0):
    """Compute alignment info for any element based on its anchor.

    For center/right anchored elements, undoes the pre-computed centering offset
    and returns alignment parameters so the N64 runtime can center based on
    actual dimensions (important for text whose glyph widths differ from Krom).

    Args:
        anchor: Element anchor enum value (0-8)
        elem_width: Element width from JSON
        container_width: Parent container width
        final_x: Pre-computed absolute X position
        json_align_h: Explicit alignmentHor from JSON properties (0=left default)

    Returns:
        (adjusted_x, align_h, align_width)
    """
    if anchor in (ANCHOR_TOP_CENTER, ANCHOR_MIDDLE_CENTER, ANCHOR_BOTTOM_CENTER):
        # Undo the centering offset applied by _calc_anchor_position:
        #   abs_x = (container_width // 2) - (elem_width // 2) + posX
        # Reverse to get the container left edge position.
        adjusted_x = final_x - (container_width // 2 - elem_width // 2)
        return adjusted_x, 1, container_width  # ALIGN_CENTER
    elif anchor in (ANCHOR_TOP_RIGHT, ANCHOR_MIDDLE_RIGHT, ANCHOR_BOTTOM_RIGHT):
        adjusted_x = final_x - (container_width - elem_width)
        return adjusted_x, 2, container_width  # ALIGN_RIGHT
    elif json_align_h != 0:
        # Explicit text alignment without center/right anchor
        return final_x, json_align_h, elem_width
    else:
        return final_x, 0, 0


def _snapshot_child_counts(labels, images, buttons, panels, groups=None):
    """Snapshot current list lengths before processing a child element."""
    return len(images), len(labels), len(buttons), len(panels), len(groups) if groups is not None else 0


def _append_child_indices(group_data, labels, images, buttons, panels, groups, snapshot):
    """Append indices for elements added since snapshot to the group's child lists.

    If the child created sub-groups, those primitives belong to the sub-group,
    so we only add the sub-group index to child_group_indices instead.
    """
    img_start, lbl_start, btn_start, pnl_start, grp_start = snapshot
    new_groups = len(groups) - grp_start if groups is not None else 0

    if new_groups > 0:
        # Child created sub-group(s); the first new one is the direct child group.
        # Primitives belong to the sub-group, not to us.
        group_data['child_group_indices'].append(grp_start)
    else:
        # Direct primitive child — add to our child lists
        for i in range(img_start, len(images)):
            group_data['child_image_indices'].append(i)
        for i in range(lbl_start, len(labels)):
            group_data['child_label_indices'].append(i)
        for i in range(btn_start, len(buttons)):
            group_data['child_button_indices'].append(i)
        for i in range(pnl_start, len(panels)):
            group_data['child_panel_indices'].append(i)


def _layout_cells(elem, count):
    """The cell (x, y, width, height) of each of an element's `count` children:
    one row or column per child in Row/ColLayout, rows x cols in GridLayout,
    the whole element otherwise (AnchorPane)."""
    width, height = elem['width'], elem['height']
    elem_type = elem.get('type')
    if elem_type == 'RowLayout':
        cell_height = height // count
        return [(0, cell_height * i, width, cell_height) for i in range(count)]
    if elem_type == 'ColLayout':
        cell_width = width // count
        return [(cell_width * i, 0, cell_width, height) for i in range(count)]
    if elem_type == 'GridLayout':
        props = elem.get('properties', {})
        rows, cols = props.get('rows', 1), props.get('cols', 1)
        cell_width = width // cols if cols > 0 else width
        cell_height = height // rows if rows > 0 else height
        return [(cell_width * (i % cols), cell_height * (i // cols), cell_width, cell_height)
                for i in range(count)]
    return [(0, 0, width, height)] * count


def _create_group(exporter, elem, children, final_x, final_y,
                  elem_by_key, children_by_parent,
                  labels, images, buttons, panels, groups, elements, is_root,
                  parent_path: str = ""):
    """Export a container as a group (so its visibility can be toggled) and its
    children inside their cells, tracking their indices."""
    full_path = _build_full_path(parent_path, elem.get('key'))
    group_data = {
        'key': full_path,
        'visible': elem.get('visible', True),
        'child_image_indices': [],
        'child_label_indices': [],
        'child_button_indices': [],
        'child_panel_indices': [],
        'child_group_indices': [],
    }

    # Reserve the group's slot first, so child groups get the indices after it
    if is_root:
        elements.append({'type': 'group', 'index': len(groups)})
    groups.append(group_data)

    for child, (cell_x, cell_y, cell_width, cell_height) in zip(children, _layout_cells(elem, len(children))):
        snap = _snapshot_child_counts(labels, images, buttons, panels, groups)
        _flatten_element(
            exporter, child, elem_by_key, children_by_parent,
            cell_width, cell_height,
            final_x + cell_x, final_y + cell_y,
            labels, images, buttons, panels, groups, elements,
            is_root=False,
            parent_path=full_path
        )
        _append_child_indices(group_data, labels, images, buttons, panels, groups, snap)


def _handle_label(exporter, elem, final_x, final_y, labels, container_width, parent_path: str = ""):
    """Handle Label element - extract text, font, and color info."""
    props = elem.get('properties', {})
    tid = elem.get('tID', '_label')
    anchor = elem.get('anchor', ANCHOR_TOP_LEFT)

    font_size = DEFAULT_FONT_SIZE
    text_color = DEFAULT_TEXT_COLOR

    # Shadow defaults (no shadow)
    shadow = False
    shadow_dx = 0
    shadow_dy = 0
    shadow_style_id = 0

    if exporter.theme_parser:
        font_size = exporter.theme_parser.get_font_size(tid, DEFAULT_FONT_SIZE)
        color_hex = exporter.theme_parser.get_text_color(tid, '#dddddd')
        text_color = KouiThemeParser.parse_hex_color(color_hex)

        shadow_hex = exporter.theme_parser.get_shadow_color(tid, '#00000000')
        shadow_color = KouiThemeParser.parse_hex_color(shadow_hex)
        shadow_dx = exporter.theme_parser.get_shadow_offset_x(tid, 0)
        shadow_dy = exporter.theme_parser.get_shadow_offset_y(tid, 0)
        # Shadow is active only when alpha > 0 and at least one offset is non-zero
        shadow = (shadow_color[3] > 0 and (shadow_dx != 0 or shadow_dy != 0))
        if shadow:
            shadow_style_id = _get_or_create_color_style(exporter, shadow_color)

    exporter.font_sizes.add(font_size)
    style_id = _get_or_create_color_style(exporter, text_color)

    # Build full path for key
    full_path = _build_full_path(parent_path, elem['key'])

    # Determine text alignment from anchor and/or explicit alignmentHor property.
    # For center/right anchored labels, use rdpq alignment with the container width
    # so libdragon centers text based on actual N64 font metrics (which may differ
    # from the Krom font metrics used to compute JSON positions).
    json_align_h = props.get('alignmentHor', 0)
    label_x, align_h, align_width = _calc_element_alignment(
        anchor, elem['width'], container_width, final_x, json_align_h
    )

    label_data = {
        'key': full_path,  # Use full path as key
        'text': props.get('text', ''),
        'pos_x': label_x,
        'pos_y': final_y,
        'width': elem['width'],
        'height': elem['height'],
        'anchor': ANCHOR_TOP_LEFT,
        'visible': elem.get('visible', True),
        'align_h': align_h,
        'align_width': align_width,
        'tID': tid,
        'font_size': font_size,
        'text_color': text_color,
        'style_id': style_id,
        'shadow': shadow,
        'shadow_dx': shadow_dx,
        'shadow_dy': shadow_dy,
        'shadow_style_id': shadow_style_id,
    }
    labels.append(label_data)


def _handle_button(exporter, elem, final_x, final_y, buttons, container_width, parent_path: str = ""):
    """Handle Button element — extract position, colors, and focus graph."""
    props = elem.get('properties', {})
    tid = elem.get('tID', '_button')
    anchor = elem.get('anchor', ANCHOR_TOP_LEFT)

    font_size = DEFAULT_FONT_SIZE
    text_color = DEFAULT_TEXT_COLOR

    # Defaults matching Koui theme values
    bg_default = KouiThemeParser.parse_hex_color('#262833')
    border_default = KouiThemeParser.parse_hex_color('#1f2028')
    bg_hover = KouiThemeParser.parse_hex_color('#2b2b33')
    border_hover = KouiThemeParser.parse_hex_color('#ef6413')
    bg_click = KouiThemeParser.parse_hex_color('#343746')
    border_click = KouiThemeParser.parse_hex_color('#ffffff')
    bg_disabled = bg_default
    border_disabled = border_default
    border_size = 2
    opacity_disabled = 0.6  # Koui default: _root!disabled -> opacity: 0.6

    if exporter.theme_parser:
        tp = exporter.theme_parser
        font_size = tp.get_font_size(tid, DEFAULT_FONT_SIZE)

        text_hex = tp.get_text_color(tid, '#dddddd')
        text_color = KouiThemeParser.parse_hex_color(text_hex)

        bg_default = KouiThemeParser.parse_hex_color(tp.get_bg_color(tid, 'default', '#262833'))
        border_default = KouiThemeParser.parse_hex_color(tp.get_border_color(tid, 'default', '#1f2028'))
        bg_hover = KouiThemeParser.parse_hex_color(tp.get_bg_color(tid, 'hover', '#2b2b33'))
        border_hover = KouiThemeParser.parse_hex_color(tp.get_border_color(tid, 'hover', '#ef6413'))
        bg_click = KouiThemeParser.parse_hex_color(tp.get_bg_color(tid, 'click', '#343746'))
        border_click = KouiThemeParser.parse_hex_color(tp.get_border_color(tid, 'click', '#ffffff'))
        bg_disabled = KouiThemeParser.parse_hex_color(tp.get_bg_color(tid, 'disabled', '#262833'))
        border_disabled = KouiThemeParser.parse_hex_color(tp.get_border_color(tid, 'disabled', '#1f2028'))
        border_size = tp.get_border_size(tid, 2)

        # Per-state opacity from theme (pre-multiplied into alpha at export time)
        op = tp.get_opacity(tid, 'default', 1.0)
        bg_default = _apply_opacity(bg_default, op)
        border_default = _apply_opacity(border_default, op)
        op = tp.get_opacity(tid, 'hover', 1.0)
        bg_hover = _apply_opacity(bg_hover, op)
        border_hover = _apply_opacity(border_hover, op)
        op = tp.get_opacity(tid, 'click', 1.0)
        bg_click = _apply_opacity(bg_click, op)
        border_click = _apply_opacity(border_click, op)
        opacity_disabled = tp.get_opacity(tid, 'disabled', 1.0)

    # Always apply disabled opacity (theme value or default 0.6)
    bg_disabled = _apply_opacity(bg_disabled, opacity_disabled)
    border_disabled = _apply_opacity(border_disabled, opacity_disabled)

    exporter.font_sizes.add(font_size)
    text_style_id = _get_or_create_color_style(exporter, text_color)

    # Build full path for key
    full_path = _build_full_path(parent_path, elem['key'])
    # Buttons are filled rectangles — use the pre-computed absolute position directly.
    # _calc_element_alignment undoes anchor-centering for text labels (so the runtime
    # can re-center via rdpq_text_print), but buttons must keep the real pixel position.
    button_x = final_x

    button_data = {
        'key': full_path,
        'text': props.get('text', ''),
        'pos_x': button_x,
        'pos_y': final_y,
        'width': elem['width'],
        'height': elem['height'],
        'visible': elem.get('visible', True),
        'font_size': font_size,
        'text_style_id': text_style_id,
        'hover_text_style_id': text_style_id,  # same color; no change on hover in default theme
        'bg_default': bg_default,
        'border_default': border_default,
        'bg_hover': bg_hover,
        'border_hover': border_hover,
        'bg_click': bg_click,
        'border_click': border_click,
        'bg_disabled': bg_disabled,
        'border_disabled': border_disabled,
        'border_size': border_size,
        # Focus graph: resolved to indices by resolve_button_focus(), -1 = no link
        'focus_up_key':    elem.get('focusUp'),
        'focus_down_key':  elem.get('focusDown'),
        'focus_left_key':  elem.get('focusLeft'),
        'focus_right_key': elem.get('focusRight'),
        'focus_up': -1, 'focus_down': -1, 'focus_left': -1, 'focus_right': -1,
        # Assigned by _assign_font_ids_to_labels after write_fonts
        'font_id': 0,
        'font_baseline_offset': 12,
    }
    buttons.append(button_data)


def _handle_panel(exporter, elem, final_x, final_y, panels, parent_path: str = ""):
    """Handle Panel element — extract position, colors, and border from theme."""
    tid = elem.get('tID', '_panel')
    anchor = elem.get('anchor', ANCHOR_TOP_LEFT)

    # Defaults matching Koui _panel theme
    bg_color = '#2b2e38ff'
    border_color = '#000000bb'
    border_size = 2

    if exporter.theme_parser:
        tp = exporter.theme_parser
        bg_hex = tp.get_bg_color(tid, 'default', '#2b2e38')
        border_hex = tp.get_border_color(tid, 'default', '#000000bb')
        bg_color = bg_hex if len(bg_hex) > 7 else bg_hex + 'ff'
        border_color = border_hex if len(border_hex) > 7 else border_hex + 'ff'
        border_size = tp.get_border_size(tid, 2)

    bg = KouiThemeParser.parse_hex_color(bg_color)
    border = KouiThemeParser.parse_hex_color(border_color)

    full_path = _build_full_path(parent_path, elem['key'])

    panel_data = {
        'key': full_path,
        'pos_x': final_x,
        'pos_y': final_y,
        'width': elem['width'],
        'height': elem['height'],
        'anchor': anchor,
        'visible': elem.get('visible', True),
        'bg_r': bg[0], 'bg_g': bg[1], 'bg_b': bg[2], 'bg_a': bg[3],
        'border_r': border[0], 'border_g': border[1], 'border_b': border[2], 'border_a': border[3],
        'border_size': border_size,
    }
    panels.append(panel_data)


def resolve_button_focus(buttons, ui_scenes):
    """Resolve focus graph string keys to integer indices after all buttons are collected.

    Focus keys in the Koui JSON are bare element names (e.g. "menu_button") that
    are always relative to the same scene.  We use the ui_scenes list to scope
    the lookup so that a button in the Win scene resolves "menu_button" to the
    Win scene's menu_button, not the Paused scene's.
    """
    # Build per-scene lookup tables keyed by bare element name.
    # Each entry maps  bare_name -> global button index  but only within
    # the range [first_button .. first_button+button_count).
    scene_bare_maps = []
    for sc in ui_scenes:
        bare_map = {}
        start = sc['first_button']
        count = sc['button_count']
        for i in range(start, start + count):
            # The key is scene-prefixed (e.g. "Paused/buttons/menu_button");
            # strip the scene prefix and parent path to get the bare name.
            bare = buttons[i]['key'].split('/')[-1]
            bare_map[bare] = i
        scene_bare_maps.append((start, start + count, bare_map))

    def _find_scene_for_button(btn_idx):
        """Return the bare_map for the scene that owns this button index."""
        for start, end, bare_map in scene_bare_maps:
            if start <= btn_idx < end:
                return bare_map
        return {}

    for i, btn in enumerate(buttons):
        scene_map = _find_scene_for_button(i)
        for direction in ('up', 'down', 'left', 'right'):
            target_key = btn.get(f'focus_{direction}_key')
            if target_key is None:
                btn[f'focus_{direction}'] = -1
            else:
                idx = scene_map.get(target_key, -1)
                btn[f'focus_{direction}'] = idx


def _handle_image(exporter, elem, final_x, final_y, images, elements, is_root,
                  container_width, parent_path: str = ""):
    """Handle ImagePanel element - track image for copying."""
    props = elem.get('properties', {})
    image_name = props.get('imageName', '')

    if not image_name:
        return

    exporter.ui_images.add(image_name)

    # Build full path for key
    full_path = _build_full_path(parent_path, elem['key'])

    anchor = elem.get('anchor', ANCHOR_TOP_LEFT)
    image_x, align_h, align_width = _calc_element_alignment(
        anchor, elem['width'], container_width, final_x
    )

    image_index = len(images)
    image_data = {
        'key': full_path,  # Use full path as key
        'image_name': image_name,
        'pos_x': image_x,
        'pos_y': final_y,
        'width': elem['width'],
        'height': elem['height'],
        'anchor': ANCHOR_TOP_LEFT,
        'visible': elem.get('visible', True),
        'scale': props.get('scale', False),
        'align_h': align_h,
        'align_width': align_width,
    }
    images.append(image_data)

    if is_root:
        elements.append({'type': 'image', 'index': image_index})


# =============================================================================
# Main Flatten Function
# =============================================================================

def _flatten_element(exporter, elem, elem_by_key, children_by_parent,
                     container_width, container_height,
                     parent_abs_x, parent_abs_y,
                     labels, images, buttons, panels, groups, elements,
                     is_root=True,
                     parent_path: str = ""):
    """Recursively flatten an element, computing absolute positions.

    Layout elements (RowLayout, ColLayout) are exported as groups so their
    visibility can be toggled. Their children are processed with adjusted positions.

    Groups (containers with children) are tracked for parent-child visibility.
    The elements array provides unified Haxe-compatible indexing.

    parent_path is used to build full keys like "parent/child" for Koui-style access.
    """
    elem_type = elem.get('type')
    elem_key = elem.get('key')
    anchor = elem.get('anchor', ANCHOR_TOP_LEFT)

    # Calculate final absolute position
    elem_abs_x, elem_abs_y = _calc_anchor_position(
        elem['posX'], elem['posY'],
        elem['width'], elem['height'],
        anchor,
        container_width, container_height
    )
    final_x = parent_abs_x + elem_abs_x
    final_y = parent_abs_y + elem_abs_y

    children = children_by_parent.get(elem_key, [])
    has_children = len(children) > 0

    # Dispatch to type-specific handlers
    if elem_type in ('AnchorPane', 'RowLayout', 'ColLayout', 'GridLayout'):
        if has_children:
            _create_group(exporter, elem, children, final_x, final_y,
                          elem_by_key, children_by_parent,
                          labels, images, buttons, panels, groups, elements, is_root,
                          parent_path=parent_path)
        return

    if elem_type == 'Label':
        _handle_label(exporter, elem, final_x, final_y, labels, container_width, parent_path=parent_path)
        return

    if elem_type == 'ImagePanel':
        _handle_image(exporter, elem, final_x, final_y, images, elements, is_root,
                      container_width, parent_path=parent_path)
        return

    if elem_type == 'Button':
        _handle_button(exporter, elem, final_x, final_y, buttons, container_width, parent_path=parent_path)
        return

    if elem_type == 'Panel':
        _handle_panel(exporter, elem, final_x, final_y, panels, parent_path=parent_path)
        return

    # Generic container with children - create a group
    if has_children and is_root:
        _create_group(exporter, elem, children, final_x, final_y,
                      elem_by_key, children_by_parent,
                      labels, images, buttons, panels, groups, elements, is_root=True,
                      parent_path=parent_path)
        return

    # Non-root elements with children - just process children
    if has_children:
        current_path = _build_full_path(parent_path, elem_key)
        for child in children:
            _flatten_element(
                exporter, child, elem_by_key, children_by_parent,
                elem['width'], elem['height'],
                final_x, final_y,
                labels, images, buttons, panels, groups, elements,
                is_root=False,
                parent_path=current_path
            )


def _parse_koui_themes(exporter):
    """Parse Koui base theme and project override files."""
    exporter.theme_parser = KouiThemeParser()

    # Base theme from Koui Subprojects
    base_theme_path = os.path.join(arm.utils.get_fp(), 'Subprojects', 'Koui', 'Assets', 'theme.ksn')
    if os.path.exists(base_theme_path):
        exporter.theme_parser.parse_file(base_theme_path)
        log.info(f'Parsed base Koui theme: {base_theme_path}')

    # Project override from Assets/koui_canvas
    override_path = os.path.join(arm.utils.get_fp(), 'Assets', 'koui_canvas', 'ui_override.ksn')
    if os.path.exists(override_path):
        exporter.theme_parser.parse_file(override_path)
        log.info(f'Parsed Koui theme override: {override_path}')

    # Resolve all inheritance chains
    exporter.theme_parser.resolve_all()


def _get_or_create_color_style(exporter, color: tuple) -> int:
    """Get or create a style_id for a given (r, g, b, a) color tuple."""
    if color in exporter.color_style_map:
        return exporter.color_style_map[color]

    style_id = len(exporter.color_style_map)
    exporter.color_style_map[color] = style_id
    return style_id


def write_canvas(exporter):
    """Generate all UI C source files from templates."""
    if not exporter.ui_canvas_data:
        return

    write_label_h(exporter)
    write_label_c(exporter)
    write_image_h(exporter)
    write_image_c(exporter)
    write_button_h(exporter)
    write_button_c(exporter)
    write_panel_h(exporter)
    write_panel_c(exporter)
    write_canvas_h(exporter)
    write_canvas_c(exporter)
    copy_canvas_images(exporter)


def copy_canvas_images(exporter):
    """Copy PNG images referenced by canvas ImagePanel elements to build/n64/assets.

    Images are searched recursively in the project Assets/ folder.
    The Makefile converts them to .sprite files.
    """
    if not exporter.ui_images:
        return

    n64_assets = os.path.join(arm.utils.build_dir(), 'n64', 'assets')
    os.makedirs(n64_assets, exist_ok=True)

    # Build index of all PNG files in Assets folder (recursive)
    assets_dir = os.path.join(arm.utils.get_fp(), 'Assets')
    image_index = {}  # basename (without ext) -> full path

    if os.path.exists(assets_dir):
        for root, dirs, files in os.walk(assets_dir):
            for f in files:
                if f.lower().endswith('.png'):
                    basename = os.path.splitext(f)[0]
                    image_index[basename] = os.path.join(root, f)
                    # Also index lowercase version for case-insensitive matching
                    image_index[basename.lower()] = os.path.join(root, f)

    copied_count = 0
    for image_name in sorted(exporter.ui_images):
        # Try exact match first, then lowercase
        png_path = image_index.get(image_name) or image_index.get(image_name.lower())

        if png_path and os.path.exists(png_path):
            # Create safe filename (lowercase, no spaces)
            safe_name = image_name.lower().replace(' ', '_')
            dst_path = os.path.join(n64_assets, f'{safe_name}.png')

            if n64_utils.copy_if_changed(png_path, dst_path):
                log.info(f'Copied canvas image: {safe_name}.png')
                copied_count += 1
        else:
            log.warn(f'Canvas image not found: {image_name}.png')

    if copied_count > 0:
        log.info(f'Copied {copied_count} canvas image(s) to build/n64/assets/')


def _key_defines(exporter, name):
    """The `UI_<NAME>_<key>` defines (each element's index in its canvas) of every
    canvas's `<name>s`, plus one without the scene (`Scene/path/key` -> `path/key`)
    when no other key took it. Returns the lines and the largest canvas's count."""
    lines, seen, count = [], set(), 0
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        items = canvas.get(f'{name}s', [])
        if not items:
            continue
        lines.append(f'// Canvas: {canvas_name} {name}s')
        for idx, item in enumerate(items):
            for key in (item['key'], item['key'].split('/', 1)[-1]):
                safe = arm.utils.safesrc(key).upper()
                if safe not in seen:
                    lines.append(f'#define UI_{name.upper()}_{safe} {idx}')
                    seen.add(safe)
        lines.append('')
        count = max(count, len(items))
    return lines, count


def _canvas_tables(exporter, name, ctype, row, comment=None):
    """Each canvas's `g_<canvas>_<name>_defs` table, one `row(item)` per element of
    its `<name>s` (followed by `// comment(item)` if given), and its `<CANVAS>_<NAME>_COUNT`."""
    lines = []
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        items = canvas.get(f'{name}s', [])
        if not items:
            continue
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        count = f'{safe_canvas.upper()}_{name.upper()}_COUNT'
        lines += [f'// Canvas: {canvas_name} {name}s',
                  f'#define {count} {len(items)}',
                  f'static const {ctype} g_{safe_canvas}_{name}_defs[{count}] = {{',
                  *(f'    {row(item)},' + (f'  // {comment(item)}' if comment else '') for item in items),
                  '};', '']
    return lines


def _scene_rows(exporter, name):
    """The rows of a `g_..._scenes` table: each scene's canvas's `<name>` table
    and count, by SceneId (scenes without a canvas, or without any, stay zeroed)."""
    rows = []
    for scene_name, data in exporter.scene_data.items():
        canvas_name = data.get('canvas')
        if not exporter.ui_canvas_data.get(canvas_name, {}).get(f'{name}s'):
            continue
        safe_scene = arm.utils.safesrc(scene_name).upper()
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        rows.append(f'    [SCENE_{safe_scene}] = {{ g_{safe_canvas}_{name}_defs, {safe_canvas.upper()}_{name.upper()}_COUNT }},')
    return rows


def _c_bool(value) -> str:
    return 'true' if value else 'false'


def write_label_h(exporter):
    """Generate label_defs.h (for label.h): the label keys, counts and sizes."""
    lines, count = _key_defines(exporter, 'label')
    lines += [f'#define UI_LABEL_COUNT      {count}',
              f'#define UI_MAX_LABELS       {exporter.limits["UI_MAX_LABELS"]}',
              f'#define UI_LABEL_TEXT_SIZE  {exporter.limits["UI_LABEL_TEXT_SIZE"]}']
    n64_utils.write_generated('ui/label_defs.h', '\n'.join(lines), 'ui_exporter.write_label_h', 'ui/label.h')


def write_label_c(exporter):
    """Generate label_data.h (for label.c): each canvas's labels, and g_label_scenes
    (the labels of each scene's canvas, by SceneId)."""
    def row(label):
        return (f'{{ {c_string(label["text"])}, {label["pos_x"]}, {label["pos_y"]}, {label["width"]}, {label["height"]}, '
                f'{label.get("baseline_offset", 12)}, {label["anchor"]}, {label.get("style_id", 0)}, '
                f'{label.get("font_id", 0)}, {_c_bool(label["visible"])}, {_c_bool(label.get("shadow", False))}, '
                f'{label.get("shadow_dx", 0)}, {label.get("shadow_dy", 0)}, {label.get("shadow_style_id", 0)}, '
                f'{label.get("align_h", 0)}, {label.get("align_width", 0)} }}')

    lines = _canvas_tables(exporter, 'label', 'UILabelDef', row)
    lines += ['static const LabelSceneDefs g_label_scenes[SCENE_COUNT] = {', *_scene_rows(exporter, 'label'), '};']
    n64_utils.write_generated('ui/label_data.h', '\n'.join(lines), 'ui_exporter.write_label_c', 'ui/label.c')


def write_image_h(exporter):
    """Generate image_defs.h (for image.h): the image keys, counts and sizes."""
    lines, count = _key_defines(exporter, 'image')
    lines += [f'#define UI_IMAGE_COUNT  {count}',
              f'#define UI_MAX_IMAGES   {exporter.limits["UI_MAX_IMAGES"]}']
    n64_utils.write_generated('ui/image_defs.h', '\n'.join(lines), 'ui_exporter.write_image_h', 'ui/image.h')


def write_image_c(exporter):
    """Generate image_data.h (for image.c): the sprite files (each loaded once),
    each canvas's images, and g_image_scenes (by SceneId)."""
    # One entry per sprite file, so an image shown twice is loaded once
    def sprite_name(image):
        return image['image_name'].lower().replace(' ', '_')
    sprites = sorted({sprite_name(image) for canvas in exporter.ui_canvas_data.values()
                      for image in canvas.get('images', [])})
    if len(sprites) > 255:
        raise ValueError(f'{len(sprites)} UI images: at most 255 are supported (sprite ids are 8-bit)')
    sprite_ids = {name: i for i, name in enumerate(sprites)}
    lines = [f'#define UI_SPRITE_COUNT {max(len(sprites), 1)}',
             'static const char *const UI_SPRITE_PATHS[UI_SPRITE_COUNT] = {']
    lines += [f'    "rom:/{name}.sprite",' for name in sprites] or ['    NULL,']
    lines += ['};', '']

    def row(image):
        return (f'{{ {sprite_ids[sprite_name(image)]}, {image["pos_x"]}, {image["pos_y"]}, {image["width"]}, {image["height"]}, '
                f'{image["anchor"]}, {_c_bool(image.get("scale", False))}, {_c_bool(image["visible"])}, '
                f'{image.get("align_h", 0)}, {image.get("align_width", 0)} }}')

    lines += _canvas_tables(exporter, 'image', 'UIImageDef', row, sprite_name)
    lines += ['static const ImageSceneDefs g_image_scenes[SCENE_COUNT] = {', *_scene_rows(exporter, 'image'), '};']
    n64_utils.write_generated('ui/image_data.h', '\n'.join(lines), 'ui_exporter.write_image_c', 'ui/image.c')


def write_button_h(exporter):
    """Generate button_defs.h (for button.h): the button keys, counts and sizes."""
    lines, count = _key_defines(exporter, 'button')
    lines += [f'#define UI_BUTTON_COUNT     {count}',
              f'#define UI_MAX_BUTTONS      {exporter.limits["UI_MAX_BUTTONS"]}',
              f'#define UI_BUTTON_TEXT_SIZE {exporter.limits["UI_BUTTON_TEXT_SIZE"]}']
    n64_utils.write_generated('ui/button_defs.h', '\n'.join(lines), 'ui_exporter.write_button_h', 'ui/button.h')


# The button colors (RGBA) of each state, in UIButtonDef's order, when the themes don't set them
_BUTTON_COLORS = (
    ('bg_default', (38, 40, 51, 255)), ('border_default', (31, 32, 40, 255)),
    ('bg_hover', (43, 43, 51, 255)), ('border_hover', (239, 100, 19, 255)),
    ('bg_click', (52, 55, 70, 255)), ('border_click', (255, 255, 255, 255)),
    ('bg_disabled', (38, 40, 51, 153)), ('border_disabled', (31, 32, 40, 153)),
)


def write_button_c(exporter):
    """Generate button_data.h (for button.c): each canvas's buttons, and g_button_scenes
    (the buttons of each scene's canvas, by SceneId)."""
    def row(btn):
        colors = ', '.join(str(c) for field, default in _BUTTON_COLORS for c in btn.get(field, default))
        return (f'{{ {btn["pos_x"]}, {btn["pos_y"]}, {btn["width"]}, {btn["height"]}, '
                f'{colors}, {btn.get("border_size", 2)}, '
                f'{c_string(btn.get("text", ""))}, {btn.get("font_id", 0)}, {btn.get("font_baseline_offset", 20)}, '
                f'{btn.get("text_style_id", 0)}, {btn.get("hover_text_style_id", 0)}, '
                f'{btn.get("focus_up", -1)}, {btn.get("focus_down", -1)}, {btn.get("focus_left", -1)}, '
                f'{btn.get("focus_right", -1)}, {_c_bool(btn.get("visible", True))} }}')

    lines = _canvas_tables(exporter, 'button', 'UIButtonDef', row)
    lines += ['static const ButtonSceneDefs g_button_scenes[SCENE_COUNT] = {', *_scene_rows(exporter, 'button'), '};']
    n64_utils.write_generated('ui/button_data.h', '\n'.join(lines), 'ui_exporter.write_button_c', 'ui/button.c')


def write_panel_h(exporter):
    """Generate panel_defs.h (for panel.h): the panel keys, counts and sizes."""
    lines, count = _key_defines(exporter, 'panel')
    lines += [f'#define UI_PANEL_COUNT  {count}',
              f'#define UI_MAX_PANELS   {exporter.limits["UI_MAX_PANELS"]}']
    n64_utils.write_generated('ui/panel_defs.h', '\n'.join(lines), 'ui_exporter.write_panel_h', 'ui/panel.h')


def write_panel_c(exporter):
    """Generate panel_data.h (for panel.c): each canvas's panels, and g_panel_scenes
    (the panels of each scene's canvas, by SceneId)."""
    def row(panel):
        return (f'{{ {panel["pos_x"]}, {panel["pos_y"]}, {panel["width"]}, {panel["height"]}, {panel["anchor"]}, '
                f'{panel["bg_r"]}, {panel["bg_g"]}, {panel["bg_b"]}, {panel["bg_a"]}, '
                f'{panel["border_r"]}, {panel["border_g"]}, {panel["border_b"]}, {panel["border_a"]}, '
                f'{panel["border_size"]}, {_c_bool(panel.get("visible", True))} }}')

    lines = _canvas_tables(exporter, 'panel', 'UIPanelDef', row)
    lines += ['static const PanelSceneDefs g_panel_scenes[SCENE_COUNT] = {', *_scene_rows(exporter, 'panel'), '};']
    n64_utils.write_generated('ui/panel_data.h', '\n'.join(lines), 'ui_exporter.write_panel_c', 'ui/panel.c')


def write_canvas_h(exporter):
    """Generate canvas_defs.h (for canvas.h): group and Koui scene keys, counts."""
    lines, group_count = _key_defines(exporter, 'group')

    # The Koui scenes of each canvas (no aliases: their keys have no scene part)
    ui_scene_count = 0
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        ui_scenes = canvas.get('ui_scenes', [])
        if ui_scenes:
            lines.append(f'// Canvas: {canvas_name} ui_scenes')
            lines += [f'#define UI_SCENE_{arm.utils.safesrc(scene["key"]).upper()} {idx}'
                      for idx, scene in enumerate(ui_scenes)]
            lines.append('')
            ui_scene_count = max(ui_scene_count, len(ui_scenes))

    element_count = max((len(canvas.get('elements', [])) for canvas in exporter.ui_canvas_data.values()), default=0)
    lines += [f'#define UI_GROUP_COUNT {group_count}',
              f'#define UI_ELEMENT_COUNT {element_count}',
              f'#define UI_SCENE_COUNT {ui_scene_count}',
              f'#define UI_MAX_GROUPS         {exporter.limits["UI_MAX_GROUPS"]}',
              f'#define UI_MAX_GROUP_CHILDREN {exporter.limits["UI_MAX_GROUP_CHILDREN"]}']
    n64_utils.write_generated('ui/canvas_defs.h', '\n'.join(lines), 'ui_exporter.write_canvas_h', 'ui/canvas.h')


def write_canvas_c(exporter):
    """Generate canvas_data.h (for canvas.c): each canvas's groups, elements and
    Koui scenes, the tables of each scene's (by SceneId), and the text colors."""
    max_children = exporter.limits['UI_MAX_GROUP_CHILDREN']

    def group_row(group):
        # Each child list padded to the arrays' size (UI_MAX_GROUP_CHILDREN, codegen/limits.py), then the counts
        kinds = ('image', 'label', 'button', 'panel', 'group')
        children = [group.get(f'child_{kind}_indices', []) for kind in kinds]
        padded = [list(c[:max_children]) + [0] * (max_children - min(len(c), max_children)) for c in children]
        arrays = ', '.join(f'{{ {", ".join(str(i) for i in p)} }}' for p in padded)
        counts = ', '.join(str(len(c)) for c in children)
        return f'{{ {arrays}, {counts}, {_c_bool(group.get("visible", True))} }}'

    def element_row(elem):
        return f'{{ {"UI_ELEM_GROUP" if elem.get("type") == "group" else "UI_ELEM_IMAGE"}, {elem["index"]} }}'

    def ui_scene_row(scene):
        return (f'{{ {scene["first_label"]}, {scene["label_count"]}, {scene["first_image"]}, {scene["image_count"]}, '
                f'{scene["first_button"]}, {scene["button_count"]}, {scene["first_panel"]}, {scene["panel_count"]}, '
                f'{_c_bool(scene.get("active", True))} }}')

    lines = (_canvas_tables(exporter, 'group', 'UIGroupDef', group_row)
             + _canvas_tables(exporter, 'element', 'UIElementDef', element_row)
             + _canvas_tables(exporter, 'ui_scene', 'UISceneRange', ui_scene_row, lambda scene: scene['key']))

    # The groups, elements and Koui scenes of each scene's canvas, by SceneId
    for name, table in (('group', 'GroupSceneDefs g_group_scenes'),
                        ('element', 'ElementSceneDefs g_element_scenes'),
                        ('ui_scene', 'KouiSceneDefs g_koui_scenes')):
        rows = _scene_rows(exporter, name)
        if rows:
            lines += [f'static const {table}[SCENE_COUNT] = {{', *rows, '};', '']

    # The text colors: font style ids, the same on every font size (canvas_init)
    colors = sorted(exporter.color_style_map.items(), key=lambda x: x[1])
    lines += [f'#define UI_FONT_STYLE_COUNT {len(colors)}',
              f'static const color_t g_font_style_colors[{max(len(colors), 1)}] = {{']
    lines += [f'    {{ .r = {r}, .g = {g}, .b = {b}, .a = {a} }},' for (r, g, b, a), _ in colors]
    lines.append('};')
    n64_utils.write_generated('ui/canvas_data.h', '\n'.join(lines), 'ui_exporter.write_canvas_c', 'ui/canvas.c')


def write_fonts(exporter):
    """Copy font files and generate fonts.c/fonts.h if UI is used.

    Creates separate .font64 files for each unique font size needed from the theme.
    """
    if not exporter.has_ui:
        return

    n64_assets = os.path.join(arm.utils.build_dir(), 'n64', 'assets')
    os.makedirs(n64_assets, exist_ok=True)

    # Ensure we have at least the default size
    if not exporter.font_sizes:
        exporter.font_sizes.add(15)

    # Search order for fonts
    font_search_paths = [
        os.path.join(arm.utils.get_fp(), 'Assets'),
        os.path.join(arm.utils.get_fp(), 'Subprojects', 'Koui', 'Assets'),
    ]

    base_font_name = None
    base_font_path = None

    for search_path in font_search_paths:
        if os.path.exists(search_path):
            fonts = glob.glob(os.path.join(search_path, '**', '*.ttf'), recursive=True)
            for font_path in fonts:
                font_basename = os.path.splitext(os.path.basename(font_path))[0]
                if base_font_name is None:
                    base_font_name = font_basename
                    base_font_path = font_path
                    break
        if base_font_name:
            break

    if not base_font_name or not base_font_path:
        log.warn('No TTF fonts found for UI. Labels may not render correctly.')
        base_font_name = 'default'
        base_font_path = None

    # Create font entries for each unique size
    font_id = 0
    for size in sorted(exporter.font_sizes):
        font_key = f'{base_font_name}_{size}'

        if base_font_path:
            dst = os.path.join(n64_assets, f'{font_key}.ttf')
            if n64_utils.copy_if_changed(base_font_path, dst):
                log.info(f'Copied font: {font_key}.ttf (size {size})')

        exporter.exported_fonts[font_key] = {
            'name': base_font_name,
            'size': size,
            'font_id': font_id
        }
        exporter.font_id_map[size] = font_id
        font_id += 1
        log.info(f'Font registered: {font_key} (size {size}, id {font_id - 1})')

    write_fonts_c(exporter)
    write_fonts_h(exporter)
    _assign_font_ids_to_labels(exporter)


def write_fonts_c(exporter):
    """Generate fonts_data.h (for fonts.c): the font files, by FontId."""
    lines = ['static const char *FONT_PATHS[FONT_COUNT] = {']
    sorted_fonts = sorted(exporter.exported_fonts.items(), key=lambda x: x[1]['font_id'])
    lines += [f'    "rom:/{font_key}.font64",' for font_key, _ in sorted_fonts]
    lines.append('};')
    n64_utils.write_generated('ui/fonts_data.h', '\n'.join(lines), 'ui_exporter.write_fonts_c', 'ui/fonts.c')


def write_fonts_h(exporter):
    """Generate fonts_defs.h (for fonts.h): FontId."""
    lines = ['typedef enum {']
    sorted_fonts = sorted(exporter.exported_fonts.items(), key=lambda x: x[1]['font_id'])
    for font_key, font_info in sorted_fonts:
        enum_name = font_key.upper().replace('-', '_').replace(' ', '_')
        lines.append(f'    FONT_{enum_name} = {font_info["font_id"]},')
    lines += [f'    FONT_COUNT = {len(exporter.exported_fonts)}', '} FontId;']
    n64_utils.write_generated('ui/fonts_defs.h', '\n'.join(lines), 'ui_exporter.write_fonts_h', 'ui/fonts.h')


def _assign_font_ids_to_labels(exporter):
    """Assign font_id and baseline_offset to each label and button based on theme font size."""
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        for label in canvas.get('labels', []):
            kha_size = label.get('font_size', 15)
            font_id = exporter.font_id_map.get(kha_size, 0)
            label['font_id'] = font_id
            mkfont_size = _mkfont_size(kha_size)
            rendered_height = mkfont_size * MKFONT_LINE_HEIGHT
            label['baseline_offset'] = int(rendered_height * MKFONT_BASELINE)
            log.debug(f"Label '{label.get('key', 'unnamed')}': kha {kha_size} -> mkfont {mkfont_size}, font_id {font_id}")

        for button in canvas.get('buttons', []):
            kha_size = button.get('font_size', 15)
            font_id = exporter.font_id_map.get(kha_size, 0)
            button['font_id'] = font_id
            rendered_height = _mkfont_size(kha_size) * MKFONT_LINE_HEIGHT
            baseline_in_font = int(rendered_height * MKFONT_BASELINE)
            btn_height = button.get('height', 40)
            top_of_text = (btn_height - rendered_height) / 2
            button['font_baseline_offset'] = max(0, round(top_of_text + baseline_in_font))
            log.debug(f"Button '{button.get('key', 'unnamed')}': kha {kha_size} -> font_id {font_id}, baseline {button['font_baseline_offset']}")


def generate_font_makefile_entries(exporter):
    """Generate Makefile entries for font conversion at different sizes.

    Returns:
        tuple: (font_targets_str, font_rules_str)
    """
    if not exporter.exported_fonts:
        return 'font_conv =', '# No fonts'

    targets = []
    rules = []

    for font_key, font_info in exporter.exported_fonts.items():
        font_name = font_info['name']
        kha_size = font_info['size']
        mkfont_size = _mkfont_size(kha_size)
        target = f'filesystem/{font_key}.font64'
        targets.append(target)

        rule = f'''{target}: assets/{font_key}.ttf
	@mkdir -p $(dir $@)
	@echo "    [FONT] $@ (kha size {kha_size} -> mkfont size {mkfont_size})"
	$(N64_MKFONT) $(MKFONT_FLAGS) --size {mkfont_size} --range 0x0020-0x00FF -o filesystem "$<"'''
        rules.append(rule)

    font_targets = 'font_conv = ' + ' \\\n             '.join(targets)
    font_rules = '\n\n'.join(rules)

    return font_targets, font_rules


def generate_ui_sprite_rules(exporter):
    """Generate per-file Makefile rules for UI sprites to use RGBA32 format.

    UI images need RGBA32 for smooth 8-bit alpha on logos and HUD elements.
    Model textures use mksprite's AUTO format detection (→ RGBA16 for RGBA PNGs).

    Returns:
        str: Makefile per-file variable assignments, or a comment if no UI images.
    """
    if not exporter.ui_images:
        return '# No UI sprites'

    rules = []
    for image_name in sorted(exporter.ui_images):
        safe_name = image_name.lower().replace(' ', '_')
        rules.append(
            f'filesystem/{safe_name}.sprite: MKSPRITE_FLAGS=--format RGBA32'
        )
    return '\n'.join(rules)
