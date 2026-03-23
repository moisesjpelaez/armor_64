"""
UI Exporter - Handles canvas, fonts, and UI-related code generation for N64.

This module provides functions for detecting UI canvases, parsing themes,
and generating canvas and font C code files.
"""

import os
import json
import glob
import shutil

import arm.utils
import arm.log as log
from arm.n64.export.koui_theme_parser import KouiThemeParser


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

    for canvas_name in referenced_canvases:
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


def _snapshot_child_counts(labels, images, buttons, panels):
    """Snapshot current list lengths before processing a child element."""
    return len(images), len(labels), len(buttons), len(panels)


def _append_child_indices(group_data, labels, images, buttons, panels, snapshot):
    """Append indices for elements added since snapshot to the group's child lists."""
    img_start, lbl_start, btn_start, pnl_start = snapshot
    for i in range(img_start, len(images)):
        group_data['child_image_indices'].append(i)
    for i in range(lbl_start, len(labels)):
        group_data['child_label_indices'].append(i)
    for i in range(btn_start, len(buttons)):
        group_data['child_button_indices'].append(i)
    for i in range(pnl_start, len(panels)):
        group_data['child_panel_indices'].append(i)


def _create_group_with_children(exporter, elem, children, final_x, final_y,
                                 elem_by_key, children_by_parent,
                                 labels, images, buttons, panels, groups, elements,
                                 parent_path: str = ""):
    """Create a group element and process its children, tracking indices."""
    group_index = len(groups)
    elem_key = elem.get('key')
    full_path = _build_full_path(parent_path, elem_key)

    group_data = {
        'key': full_path,  # Use full path as key
        'visible': elem.get('visible', True),
        'child_image_indices': [],
        'child_label_indices': [],
        'child_button_indices': [],
        'child_panel_indices': [],
    }

    # Add to elements array as a group
    elements.append({'type': 'group', 'index': group_index})

    container_width = elem['width']
    container_height = elem['height']

    # Process children - track their indices for the group
    for child in children:
        snap = _snapshot_child_counts(labels, images, buttons, panels)
        _flatten_element(
            exporter, child, elem_by_key, children_by_parent,
            container_width, container_height,
            final_x, final_y,
            labels, images, buttons, panels, groups, elements,
            is_root=False,
            parent_path=full_path
        )
        _append_child_indices(group_data, labels, images, buttons, panels, snap)

    groups.append(group_data)


def _handle_row_col_layout(exporter, elem, elem_type, children, final_x, final_y,
                            elem_by_key, children_by_parent,
                            labels, images, buttons, panels, groups, elements, is_root,
                            parent_path: str = ""):
    """Handle RowLayout and ColLayout - process children in cells."""
    if not children:
        return

    elem_key = elem.get('key')
    full_path = _build_full_path(parent_path, elem_key)

    # Create a group for this layout so visibility can be controlled
    group_index = len(groups)
    group_data = {
        'key': full_path,
        'visible': elem.get('visible', True),
        'child_image_indices': [],
        'child_label_indices': [],
        'child_button_indices': [],
        'child_panel_indices': [],
    }

    if is_root:
        elements.append({'type': 'group', 'index': group_index})

    layout_width = elem['width']
    layout_height = elem['height']
    num_children = len(children)

    if elem_type == 'RowLayout':
        cell_width = layout_width
        cell_height = layout_height // num_children
    else:  # ColLayout
        cell_width = layout_width // num_children
        cell_height = layout_height

    for idx, child in enumerate(children):
        if elem_type == 'RowLayout':
            cell_x, cell_y = 0, cell_height * idx
        else:
            cell_x, cell_y = cell_width * idx, 0

        snap = _snapshot_child_counts(labels, images, buttons, panels)
        _flatten_element(
            exporter, child, elem_by_key, children_by_parent,
            cell_width, cell_height,
            final_x + cell_x, final_y + cell_y,
            labels, images, buttons, panels, groups, elements,
            is_root=False,
            parent_path=full_path
        )
        _append_child_indices(group_data, labels, images, buttons, panels, snap)

    groups.append(group_data)


def _handle_grid_layout(exporter, elem, children, final_x, final_y,
                         elem_by_key, children_by_parent,
                         labels, images, buttons, panels, groups, elements, is_root,
                         parent_path: str = ""):
    """Handle GridLayout - place children in grid cells."""
    if not children:
        return

    elem_key = elem.get('key')
    full_path = _build_full_path(parent_path, elem_key)

    # Create a group for this layout
    group_index = len(groups)
    group_data = {
        'key': full_path,
        'visible': elem.get('visible', True),
        'child_image_indices': [],
        'child_label_indices': [],
        'child_button_indices': [],
        'child_panel_indices': [],
    }

    if is_root:
        elements.append({'type': 'group', 'index': group_index})

    layout_width = elem['width']
    layout_height = elem['height']
    props = elem.get('properties', {})

    num_rows = props.get('rows', 1)
    num_cols = props.get('cols', 1)

    cell_width = layout_width // num_cols if num_cols > 0 else layout_width
    cell_height = layout_height // num_rows if num_rows > 0 else layout_height

    for idx, child in enumerate(children):
        row = idx // num_cols
        col = idx % num_cols
        cell_x = cell_width * col
        cell_y = cell_height * row

        snap = _snapshot_child_counts(labels, images, buttons, panels)
        _flatten_element(
            exporter, child, elem_by_key, children_by_parent,
            cell_width, cell_height,
            final_x + cell_x, final_y + cell_y,
            labels, images, buttons, panels, groups, elements,
            is_root=False,
            parent_path=full_path
        )
        _append_child_indices(group_data, labels, images, buttons, panels, snap)

    groups.append(group_data)


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

    if not hasattr(exporter, 'ui_images'):
        exporter.ui_images = set()
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
    if elem_type in ('RowLayout', 'ColLayout'):
        _handle_row_col_layout(exporter, elem, elem_type, children, final_x, final_y,
                                elem_by_key, children_by_parent,
                                labels, images, buttons, panels, groups, elements, is_root,
                                parent_path=parent_path)
        return

    if elem_type == 'GridLayout':
        _handle_grid_layout(exporter, elem, children, final_x, final_y,
                             elem_by_key, children_by_parent,
                             labels, images, buttons, panels, groups, elements, is_root,
                             parent_path=parent_path)
        return

    if elem_type == 'AnchorPane':
        if has_children:
            _create_group_with_children(exporter, elem, children, final_x, final_y,
                                         elem_by_key, children_by_parent,
                                         labels, images, buttons, panels, groups, elements,
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
        _create_group_with_children(exporter, elem, children, final_x, final_y,
                                     elem_by_key, children_by_parent,
                                     labels, images, buttons, panels, groups, elements,
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

    # Copy shared header used by label, image, and panel
    from arm.n64 import utils as n64_utils
    n64_utils.copy_src('ui_anchor.h', 'src/ui')

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
    if not hasattr(exporter, 'ui_images') or not exporter.ui_images:
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
    for image_name in exporter.ui_images:
        # Try exact match first, then lowercase
        png_path = image_index.get(image_name) or image_index.get(image_name.lower())

        if png_path and os.path.exists(png_path):
            # Create safe filename (lowercase, no spaces)
            safe_name = image_name.lower().replace(' ', '_')
            dst_path = os.path.join(n64_assets, f'{safe_name}.png')

            if not os.path.exists(dst_path):
                shutil.copy(png_path, dst_path)
                log.info(f'Copied canvas image: {safe_name}.png')
                copied_count += 1
        else:
            log.warn(f'Canvas image not found: {image_name}.png')

    if copied_count > 0:
        log.info(f'Copied {copied_count} canvas image(s) to build/n64/assets/')


def write_label_h(exporter):
    """Generate label.h from label.h.j2 template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'label.h.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'label.h')
    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl = f.read()

    label_defines_lines = []
    total_label_count = 0
    seen = {}
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        labels = canvas.get('labels', [])
        if not labels:
            continue
        label_defines_lines.append(f'// Canvas: {canvas_name}')
        for idx, label in enumerate(labels):
            safe = arm.utils.safesrc(label['key']).upper()
            if safe not in seen:
                label_defines_lines.append(f'#define UI_LABEL_{safe} {idx}')
                seen[safe] = idx
            # Also emit a bare-path alias (without scene prefix) when unambiguous.
            parts = label['key'].split('/', 1)
            if len(parts) == 2:
                bare_safe = arm.utils.safesrc(parts[1]).upper()
                if bare_safe != safe and bare_safe not in seen:
                    label_defines_lines.append(f'#define UI_LABEL_{bare_safe} {idx}')
                    seen[bare_safe] = idx
        label_defines_lines.append('')
        total_label_count = max(total_label_count, len(labels))

    max_label_text_size = 32
    for canvas in exporter.ui_canvas_data.values():
        for label in canvas.get('labels', []):
            max_label_text_size = max(max_label_text_size, len(label.get('text', '')) + 1)
    max_label_text_size = 1 << (max_label_text_size - 1).bit_length()

    output = tmpl.format(
        canvas_width=320,
        canvas_height=240,
        label_defines='\n'.join(label_defines_lines) if label_defines_lines else '// No labels',
        label_count=total_label_count,
        max_labels=max(1, total_label_count + 4),
        max_label_text_size=max_label_text_size,
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_label_c(exporter):
    """Generate label.c from label.c.j2 template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'label.c.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'label.c')
    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl = f.read()

    canvas_label_arrays = []
    label_scene_init_cases = []

    for canvas_name, canvas in exporter.ui_canvas_data.items():
        labels = canvas.get('labels', [])
        if not labels:
            continue
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        count_def = f'{safe_canvas.upper()}_LABEL_COUNT'
        canvas_label_arrays.append(f'// Canvas: {canvas_name}')
        canvas_label_arrays.append(f'#define {count_def} {len(labels)}')
        canvas_label_arrays.append(f'static const UILabelDef g_{safe_canvas}_label_defs[{count_def}] = {{')
        for label in labels:
            text_esc = label['text'].replace('\\', '\\\\').replace('"', '\\"')
            visible = 'true' if label['visible'] else 'false'
            shadow = 'true' if label.get('shadow', False) else 'false'
            canvas_label_arrays.append(
                f'    {{ "{text_esc}", {label["pos_x"]}, {label["pos_y"]}, {label["width"]}, {label["height"]}, '
                f'{label.get("baseline_offset", 12)}, {label["anchor"]}, {label.get("style_id", 0)}, '
                f'{label.get("font_id", 0)}, {visible}, {shadow}, {label.get("shadow_dx", 0)}, '
                f'{label.get("shadow_dy", 0)}, {label.get("shadow_style_id", 0)}, '
                f'{label.get("align_h", 0)}, {label.get("align_width", 0)} }},'
            )
        canvas_label_arrays.append('};')
        canvas_label_arrays.append('')

    for scene_name, data in exporter.scene_data.items():
        canvas_name = data.get('canvas')
        if not canvas_name or canvas_name not in exporter.ui_canvas_data:
            continue
        if not exporter.ui_canvas_data[canvas_name].get('labels'):
            continue
        safe_scene = arm.utils.safesrc(scene_name).upper()
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        label_scene_init_cases.append(f'        case SCENE_{safe_scene}:')
        label_scene_init_cases.append(f'            load_labels(g_{safe_canvas}_label_defs, {safe_canvas.upper()}_LABEL_COUNT);')
        label_scene_init_cases.append('            break;')

    output = tmpl.format(
        canvas_label_arrays='\n'.join(canvas_label_arrays) if canvas_label_arrays else '// No labels defined',
        label_scene_init_cases='\n'.join(label_scene_init_cases),
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_image_h(exporter):
    """Generate image.h from image.h.j2 template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'image.h.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'image.h')
    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl = f.read()

    image_defines_lines = []
    total_image_count = 0
    seen = {}
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        images = canvas.get('images', [])
        if not images:
            continue
        image_defines_lines.append(f'// Canvas: {canvas_name}')
        for idx, image in enumerate(images):
            safe = arm.utils.safesrc(image['key']).upper()
            if safe not in seen:
                image_defines_lines.append(f'#define UI_IMAGE_{safe} {idx}')
                seen[safe] = idx
            # Also emit a bare-path alias (without scene prefix) when unambiguous.
            parts = image['key'].split('/', 1)
            if len(parts) == 2:
                bare_safe = arm.utils.safesrc(parts[1]).upper()
                if bare_safe != safe and bare_safe not in seen:
                    image_defines_lines.append(f'#define UI_IMAGE_{bare_safe} {idx}')
                    seen[bare_safe] = idx
        image_defines_lines.append('')
        total_image_count = max(total_image_count, len(images))

    output = tmpl.format(
        image_defines='\n'.join(image_defines_lines) if image_defines_lines else '// No images',
        image_count=total_image_count,
        max_images=max(1, total_image_count + 4),
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_image_c(exporter):
    """Generate image.c from image.c.j2 template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'image.c.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'image.c')
    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl = f.read()

    canvas_image_arrays = []
    image_scene_init_cases = []

    for canvas_name, canvas in exporter.ui_canvas_data.items():
        images = canvas.get('images', [])
        if not images:
            continue
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        count_def = f'{safe_canvas.upper()}_IMAGE_COUNT'
        canvas_image_arrays.append(f'// Canvas: {canvas_name} images')
        canvas_image_arrays.append(f'#define {count_def} {len(images)}')
        canvas_image_arrays.append(f'static UIImageDef g_{safe_canvas}_image_defs[{count_def}] = {{')
        for image in images:
            safe_name = image['image_name'].lower().replace(' ', '_')
            visible = 'true' if image['visible'] else 'false'
            scale = 'true' if image.get('scale', False) else 'false'
            canvas_image_arrays.append(
                f'    {{ "{safe_name}", {image["pos_x"]}, {image["pos_y"]}, {image["width"]}, {image["height"]}, '
                f'{image["anchor"]}, {scale}, {visible}, NULL, {image.get("align_h", 0)}, {image.get("align_width", 0)} }},'
            )
        canvas_image_arrays.append('};')
        canvas_image_arrays.append('')

    for scene_name, data in exporter.scene_data.items():
        canvas_name = data.get('canvas')
        if not canvas_name or canvas_name not in exporter.ui_canvas_data:
            continue
        if not exporter.ui_canvas_data[canvas_name].get('images'):
            continue
        safe_scene = arm.utils.safesrc(scene_name).upper()
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        image_scene_init_cases.append(f'        case SCENE_{safe_scene}:')
        image_scene_init_cases.append(f'            load_images(g_{safe_canvas}_image_defs, {safe_canvas.upper()}_IMAGE_COUNT);')
        image_scene_init_cases.append('            break;')

    output = tmpl.format(
        canvas_image_arrays='\n'.join(canvas_image_arrays) if canvas_image_arrays else '// No images defined',
        image_scene_init_cases='\n'.join(image_scene_init_cases),
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_button_h(exporter):
    """Generate button.h from button.h.j2 template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'button.h.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'button.h')
    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl = f.read()

    button_defines_lines = []
    total_button_count = 0
    seen = {}
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        buttons = canvas.get('buttons', [])
        if not buttons:
            continue
        button_defines_lines.append(f'// Canvas: {canvas_name}')
        for idx, btn in enumerate(buttons):
            safe = arm.utils.safesrc(btn['key']).upper()
            if safe not in seen:
                button_defines_lines.append(f'#define UI_BUTTON_{safe} {idx}')
                seen[safe] = idx
            # Also emit a bare-path alias (without scene prefix) when unambiguous.
            # Key format is "Scene/path/key"; strip the first segment to get "path/key".
            parts = btn['key'].split('/', 1)
            if len(parts) == 2:
                bare_safe = arm.utils.safesrc(parts[1]).upper()
                if bare_safe != safe and bare_safe not in seen:
                    button_defines_lines.append(f'#define UI_BUTTON_{bare_safe} {idx}')
                    seen[bare_safe] = idx
        button_defines_lines.append('')
        total_button_count = max(total_button_count, len(buttons))

    output = tmpl.format(
        button_defines='\n'.join(button_defines_lines) if button_defines_lines else '// No buttons',
        button_count=total_button_count,
        max_buttons=max(1, total_button_count + 2),
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_button_c(exporter):
    """Generate button.c from button.c.j2 template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'button.c.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'button.c')
    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl = f.read()

    canvas_button_arrays = []
    button_scene_init_cases = []

    for canvas_name, canvas in exporter.ui_canvas_data.items():
        buttons = canvas.get('buttons', [])
        if not buttons:
            continue
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        count_def = f'{safe_canvas.upper()}_BUTTON_COUNT'
        canvas_button_arrays.append(f'// Canvas: {canvas_name} buttons')
        canvas_button_arrays.append(f'#define {count_def} {len(buttons)}')
        canvas_button_arrays.append(f'static const UIButtonDef g_{safe_canvas}_button_defs[{count_def}] = {{')
        for btn in buttons:
            bg = btn.get('bg_default', (38, 40, 51, 255))
            bd = btn.get('border_default', (31, 32, 40, 255))
            hbg = btn.get('bg_hover', (43, 43, 51, 255))
            hbd = btn.get('border_hover', (239, 100, 19, 255))
            cbg = btn.get('bg_click', (52, 55, 70, 255))
            cbd = btn.get('border_click', (255, 255, 255, 255))
            dbg = btn.get('bg_disabled', (38, 40, 51, 153))
            dbd = btn.get('border_disabled', (31, 32, 40, 153))

            text_esc = btn.get('text', '').replace('\\', '\\\\').replace('"', '\\"')
            visible = 'true' if btn.get('visible', True) else 'false'
            focus_up    = btn.get('focus_up', -1)
            focus_down  = btn.get('focus_down', -1)
            focus_left  = btn.get('focus_left', -1)
            focus_right = btn.get('focus_right', -1)
            canvas_button_arrays.append(
                f'    {{ {btn["pos_x"]}, {btn["pos_y"]}, {btn["width"]}, {btn["height"]}, '
                f'{bg[0]}, {bg[1]}, {bg[2]}, {bg[3]}, '
                f'{bd[0]}, {bd[1]}, {bd[2]}, {bd[3]}, '
                f'{hbg[0]}, {hbg[1]}, {hbg[2]}, {hbg[3]}, '
                f'{hbd[0]}, {hbd[1]}, {hbd[2]}, {hbd[3]}, '
                f'{cbg[0]}, {cbg[1]}, {cbg[2]}, {cbg[3]}, '
                f'{cbd[0]}, {cbd[1]}, {cbd[2]}, {cbd[3]}, '
                f'{dbg[0]}, {dbg[1]}, {dbg[2]}, {dbg[3]}, '
                f'{dbd[0]}, {dbd[1]}, {dbd[2]}, {dbd[3]}, '
                f'{btn.get("border_size", 2)}, '
                f'"{text_esc}", {btn.get("font_id", 0)}, {btn.get("font_baseline_offset", 20)}, '
                f'{btn.get("text_style_id", 0)}, {btn.get("hover_text_style_id", 0)}, '
                f'{focus_up}, {focus_down}, {focus_left}, {focus_right}, {visible} }},'
            )
        canvas_button_arrays.append('};')
        canvas_button_arrays.append('')

    for scene_name, data in exporter.scene_data.items():
        canvas_name = data.get('canvas')
        if not canvas_name or canvas_name not in exporter.ui_canvas_data:
            continue
        if not exporter.ui_canvas_data[canvas_name].get('buttons'):
            continue
        safe_scene = arm.utils.safesrc(scene_name).upper()
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        button_scene_init_cases.append(f'        case SCENE_{safe_scene}:')
        button_scene_init_cases.append(f'            load_buttons(g_{safe_canvas}_button_defs, {safe_canvas.upper()}_BUTTON_COUNT);')
        button_scene_init_cases.append('            break;')

    output = tmpl.format(
        canvas_button_arrays='\n'.join(canvas_button_arrays) if canvas_button_arrays else '// No buttons defined',
        button_scene_init_cases='\n'.join(button_scene_init_cases),
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_panel_h(exporter):
    """Generate panel.h from panel.h.j2 template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'panel.h.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'panel.h')
    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl = f.read()

    panel_defines_lines = []
    total_panel_count = 0
    seen = {}
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        panels = canvas.get('panels', [])
        if not panels:
            continue
        panel_defines_lines.append(f'// Canvas: {canvas_name}')
        for idx, panel in enumerate(panels):
            safe = arm.utils.safesrc(panel['key']).upper()
            if safe not in seen:
                panel_defines_lines.append(f'#define UI_PANEL_{safe} {idx}')
                seen[safe] = idx
            # Also emit a bare-path alias (without scene prefix) when unambiguous.
            parts = panel['key'].split('/', 1)
            if len(parts) == 2:
                bare_safe = arm.utils.safesrc(parts[1]).upper()
                if bare_safe != safe and bare_safe not in seen:
                    panel_defines_lines.append(f'#define UI_PANEL_{bare_safe} {idx}')
                    seen[bare_safe] = idx
        panel_defines_lines.append('')
        total_panel_count = max(total_panel_count, len(panels))

    output = tmpl.format(
        panel_defines='\n'.join(panel_defines_lines) if panel_defines_lines else '// No panels',
        panel_count=total_panel_count,
        max_panels=max(1, total_panel_count + 2),
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_panel_c(exporter):
    """Generate panel.c from panel.c.j2 template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'panel.c.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'panel.c')
    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl = f.read()

    canvas_panel_arrays = []
    panel_scene_init_cases = []

    for canvas_name, canvas in exporter.ui_canvas_data.items():
        panels = canvas.get('panels', [])
        if not panels:
            continue
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        count_def = f'{safe_canvas.upper()}_PANEL_COUNT'
        canvas_panel_arrays.append(f'// Canvas: {canvas_name} panels')
        canvas_panel_arrays.append(f'#define {count_def} {len(panels)}')
        canvas_panel_arrays.append(f'static const UIPanelDef g_{safe_canvas}_panel_defs[{count_def}] = {{')
        for panel in panels:
            visible = 'true' if panel.get('visible', True) else 'false'
            canvas_panel_arrays.append(
                f'    {{ {panel["pos_x"]}, {panel["pos_y"]}, {panel["width"]}, {panel["height"]}, '
                f'{panel["anchor"]}, '
                f'{panel["bg_r"]}, {panel["bg_g"]}, {panel["bg_b"]}, {panel["bg_a"]}, '
                f'{panel["border_r"]}, {panel["border_g"]}, {panel["border_b"]}, {panel["border_a"]}, '
                f'{panel["border_size"]}, {visible} }},'
            )
        canvas_panel_arrays.append('};')
        canvas_panel_arrays.append('')

    for scene_name, data in exporter.scene_data.items():
        canvas_name = data.get('canvas')
        if not canvas_name or canvas_name not in exporter.ui_canvas_data:
            continue
        if not exporter.ui_canvas_data[canvas_name].get('panels'):
            continue
        safe_scene = arm.utils.safesrc(scene_name).upper()
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        panel_scene_init_cases.append(f'        case SCENE_{safe_scene}:')
        panel_scene_init_cases.append(f'            load_panels(g_{safe_canvas}_panel_defs, {safe_canvas.upper()}_PANEL_COUNT);')
        panel_scene_init_cases.append('            break;')

    output = tmpl.format(
        canvas_panel_arrays='\n'.join(canvas_panel_arrays) if canvas_panel_arrays else '// No panels defined',
        panel_scene_init_cases='\n'.join(panel_scene_init_cases),
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_canvas_h(exporter):
    """Generate canvas.h from template (group/element content only)."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'canvas.h.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'canvas.h')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    # Build group defines
    group_defines_lines = []
    total_group_count = 0
    seen_group_keys = {}

    for canvas_name, canvas in exporter.ui_canvas_data.items():
        groups = canvas.get('groups', [])
        if groups:
            group_defines_lines.append(f'// Canvas: {canvas_name} groups')
            for group_idx, group in enumerate(groups):
                safe_key = arm.utils.safesrc(group['key']).upper()
                define_name = f'UI_GROUP_{safe_key}'
                if safe_key not in seen_group_keys:
                    group_defines_lines.append(f'#define {define_name} {group_idx}')
                    seen_group_keys[safe_key] = group_idx
                # Also emit a bare-path alias (without scene prefix) when unambiguous.
                parts = group['key'].split('/', 1)
                if len(parts) == 2:
                    bare_safe = arm.utils.safesrc(parts[1]).upper()
                    bare_define = f'UI_GROUP_{bare_safe}'
                    if bare_safe != safe_key and bare_safe not in seen_group_keys:
                        group_defines_lines.append(f'#define {bare_define} {group_idx}')
                        seen_group_keys[bare_safe] = group_idx
            group_defines_lines.append('')
        total_group_count = max(total_group_count, len(groups))

    total_element_count = max(
        (len(canvas.get('elements', [])) for canvas in exporter.ui_canvas_data.values()),
        default=0
    )

    max_groups = max(1, total_group_count + 2)
    max_group_children = 8
    for canvas in exporter.ui_canvas_data.values():
        for group in canvas.get('groups', []):
            child_count = max(
                len(group.get('child_image_indices', [])),
                len(group.get('child_label_indices', [])),
                len(group.get('child_button_indices', [])),
                len(group.get('child_panel_indices', []))
            )
            max_group_children = max(max_group_children, child_count)

    # Build UI scene defines (Koui scenes within a canvas)
    ui_scene_defines_lines = []
    max_ui_scene_count = 0
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        ui_scenes = canvas.get('ui_scenes', [])
        if ui_scenes:
            ui_scene_defines_lines.append(f'// Canvas: {canvas_name} UI scenes')
            for scene_idx, scene in enumerate(ui_scenes):
                safe_key = arm.utils.safesrc(scene['key']).upper()
                ui_scene_defines_lines.append(f'#define UI_SCENE_{safe_key} {scene_idx}')
            ui_scene_defines_lines.append('')
            max_ui_scene_count = max(max_ui_scene_count, len(ui_scenes))

    output = tmpl_content.format(
        group_defines='\n'.join(group_defines_lines) if group_defines_lines else '// No groups',
        group_count=total_group_count,
        element_count=total_element_count,
        max_groups=max_groups,
        max_group_children=max_group_children,
        ui_scene_defines='\n'.join(ui_scene_defines_lines) if ui_scene_defines_lines else '// No UI scenes',
        ui_scene_count=max_ui_scene_count,
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_canvas_c(exporter):
    """Generate canvas.c from template (group/element arrays + font registration)."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'canvas.c.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'canvas.c')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    # Build per-canvas group definition arrays
    canvas_group_arrays = []
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        groups = canvas.get('groups', [])
        if not groups:
            continue
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        canvas_group_arrays.append(f'// Canvas: {canvas_name} groups')
        canvas_group_arrays.append(f'#define {safe_canvas.upper()}_GROUP_COUNT {len(groups)}')
        canvas_group_arrays.append(f'static const UIGroupDef g_{safe_canvas}_group_defs[{safe_canvas.upper()}_GROUP_COUNT] = {{')
        for group in groups:
            img_indices = group.get('child_image_indices', [])
            lbl_indices = group.get('child_label_indices', [])
            btn_indices = group.get('child_button_indices', [])
            pnl_indices = group.get('child_panel_indices', [])
            max_ch = max(8, len(img_indices), len(lbl_indices), len(btn_indices), len(pnl_indices))
            img_padded = list(img_indices[:max_ch]) + [0] * (max_ch - min(len(img_indices), max_ch))
            lbl_padded = list(lbl_indices[:max_ch]) + [0] * (max_ch - min(len(lbl_indices), max_ch))
            btn_padded = list(btn_indices[:max_ch]) + [0] * (max_ch - min(len(btn_indices), max_ch))
            pnl_padded = list(pnl_indices[:max_ch]) + [0] * (max_ch - min(len(pnl_indices), max_ch))
            img_str = ', '.join(str(i) for i in img_padded)
            lbl_str = ', '.join(str(i) for i in lbl_padded)
            btn_str = ', '.join(str(i) for i in btn_padded)
            pnl_str = ', '.join(str(i) for i in pnl_padded)
            visible = 'true' if group.get('visible', True) else 'false'
            canvas_group_arrays.append(f'    {{ {{ {img_str} }}, {{ {lbl_str} }}, {{ {btn_str} }}, {{ {pnl_str} }}, {len(img_indices)}, {len(lbl_indices)}, {len(btn_indices)}, {len(pnl_indices)}, {visible} }},')
        canvas_group_arrays.append('};')
        canvas_group_arrays.append('')

    # Build per-canvas element definition arrays
    canvas_element_arrays = []
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        elements = canvas.get('elements', [])
        if not elements:
            continue
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        canvas_element_arrays.append(f'// Canvas: {canvas_name} elements')
        canvas_element_arrays.append(f'#define {safe_canvas.upper()}_ELEMENT_COUNT {len(elements)}')
        canvas_element_arrays.append(f'static const UIElementDef g_{safe_canvas}_element_defs[{safe_canvas.upper()}_ELEMENT_COUNT] = {{')
        for elem in elements:
            t = 'UI_ELEM_GROUP' if elem.get('type') == 'group' else 'UI_ELEM_IMAGE'
            canvas_element_arrays.append(f'    {{ {t}, {elem["index"]} }},')
        canvas_element_arrays.append('};')
        canvas_element_arrays.append('')

    # Build group/element scene init switch cases
    scene_switch_cases = []
    for scene_name, data in exporter.scene_data.items():
        canvas_name = data.get('canvas')
        if not canvas_name or canvas_name not in exporter.ui_canvas_data:
            continue
        canvas = exporter.ui_canvas_data[canvas_name]
        group_count = len(canvas.get('groups', []))
        element_count = len(canvas.get('elements', []))
        ui_scene_count = len(canvas.get('ui_scenes', []))
        if group_count == 0 and element_count == 0 and ui_scene_count == 0:
            continue
        safe_scene = arm.utils.safesrc(scene_name).upper()
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        scene_switch_cases.append(f'        case SCENE_{safe_scene}:')
        if group_count > 0:
            scene_switch_cases.append(f'            load_groups(g_{safe_canvas}_group_defs, {safe_canvas.upper()}_GROUP_COUNT);')
        if element_count > 0:
            scene_switch_cases.append(f'            load_elements(g_{safe_canvas}_element_defs, {safe_canvas.upper()}_ELEMENT_COUNT);')
        if ui_scene_count > 0:
            scene_switch_cases.append(f'            load_ui_scenes(g_{safe_canvas}_ui_scene_defs, {safe_canvas.upper()}_UI_SCENE_COUNT);')
        scene_switch_cases.append('            break;')

    # Build per-canvas UI scene range arrays
    canvas_ui_scene_arrays = []
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        ui_scenes = canvas.get('ui_scenes', [])
        if not ui_scenes:
            continue
        safe_canvas = arm.utils.safesrc(canvas_name).lower()
        canvas_ui_scene_arrays.append(f'// Canvas: {canvas_name} UI scenes')
        canvas_ui_scene_arrays.append(f'#define {safe_canvas.upper()}_UI_SCENE_COUNT {len(ui_scenes)}')
        canvas_ui_scene_arrays.append(f'static const UISceneRange g_{safe_canvas}_ui_scene_defs[{safe_canvas.upper()}_UI_SCENE_COUNT] = {{')
        for scene in ui_scenes:
            active = 'true' if scene.get('active', True) else 'false'
            canvas_ui_scene_arrays.append(
                f'    {{ {scene["first_label"]}, {scene["label_count"]}, '
                f'{scene["first_image"]}, {scene["image_count"]}, '
                f'{scene["first_button"]}, {scene["button_count"]}, '
                f'{scene["first_panel"]}, {scene["panel_count"]}, '
                f'{active} }},  // {scene["key"]}'
            )
        canvas_ui_scene_arrays.append('};')
        canvas_ui_scene_arrays.append('')
        scene_switch_cases.append('            break;')

    # Build font style registration code
    style_registration_lines = []
    if exporter.exported_fonts:
        for font_key, font_info in sorted(exporter.exported_fonts.items(), key=lambda x: x[1]['font_id']):
            font_id = font_info['font_id']
            if font_id == 0:
                if exporter.color_style_map:
                    style_registration_lines.append(f'    // Font 0 styles (default font)')
                    style_registration_lines.append(f'    {{')
                    style_registration_lines.append(f'        rdpq_font_t *font_0 = fonts_get(0);')
                    style_registration_lines.append(f'        if (font_0) {{')
                    for color, style_id in sorted(exporter.color_style_map.items(), key=lambda x: x[1]):
                        r, g, b, a = color
                        style_registration_lines.append(
                            f'            rdpq_font_style(font_0, {style_id}, &(rdpq_fontstyle_t){{ .color = RGBA32({r}, {g}, {b}, {a}) }});'
                        )
                    style_registration_lines.append(f'        }}')
                    style_registration_lines.append(f'    }}')
                continue
            style_registration_lines.append(f'    // Font {font_id}: {font_key}')
            style_registration_lines.append(f'    {{')
            style_registration_lines.append(f'        rdpq_font_t *font_{font_id} = fonts_get({font_id});')
            if exporter.color_style_map:
                style_registration_lines.append(f'        if (font_{font_id}) {{')
                for color, style_id in sorted(exporter.color_style_map.items(), key=lambda x: x[1]):
                    r, g, b, a = color
                    style_registration_lines.append(
                        f'            rdpq_font_style(font_{font_id}, {style_id}, &(rdpq_fontstyle_t){{ .color = RGBA32({r}, {g}, {b}, {a}) }});'
                    )
                style_registration_lines.append(f'        }}')
            style_registration_lines.append(f'    }}')

    output = tmpl_content.format(
        canvas_group_arrays='\n'.join(canvas_group_arrays) if canvas_group_arrays else '// No groups defined',
        canvas_element_arrays='\n'.join(canvas_element_arrays) if canvas_element_arrays else '// No elements defined',
        canvas_ui_scene_arrays='\n'.join(canvas_ui_scene_arrays) if canvas_ui_scene_arrays else '// No UI scenes defined',
        group_element_scene_init_cases='\n'.join(scene_switch_cases),
        font_style_registration='\n'.join(style_registration_lines) if style_registration_lines else '    // No custom styles defined'
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


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
            if not os.path.exists(dst):
                shutil.copy(base_font_path, dst)
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
    """Generate fonts.c from template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'fonts.c.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'fonts.c')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    lines = []
    sorted_fonts = sorted(exporter.exported_fonts.items(), key=lambda x: x[1]['font_id'])
    for font_key, font_info in sorted_fonts:
        lines.append(f'    "rom:/{font_key}.font64"')
    font_paths = ',\n'.join(lines)

    output = tmpl_content.format(
        font_paths=font_paths,
        font_count=len(exporter.exported_fonts)
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def write_fonts_h(exporter):
    """Generate fonts.h from template."""
    tmpl_path = os.path.join(arm.utils.get_n64_deployment_path(), 'src', 'ui', 'fonts.h.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'ui', 'fonts.h')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    lines = []
    sorted_fonts = sorted(exporter.exported_fonts.items(), key=lambda x: x[1]['font_id'])
    for font_key, font_info in sorted_fonts:
        enum_name = font_key.upper().replace('-', '_').replace(' ', '_')
        lines.append(f'    FONT_{enum_name} = {font_info["font_id"]},')
    font_enum_entries = '\n'.join(lines)

    output = tmpl_content.format(
        font_enum_entries=font_enum_entries,
        font_count=len(exporter.exported_fonts)
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output)


def _assign_font_ids_to_labels(exporter):
    """Assign font_id and baseline_offset to each label and button based on theme font size."""
    for canvas_name, canvas in exporter.ui_canvas_data.items():
        for label in canvas.get('labels', []):
            kha_size = label.get('font_size', 15)
            font_id = exporter.font_id_map.get(kha_size, 0)
            label['font_id'] = font_id
            mkfont_size = max(8, int(kha_size * exporter.FONT_SIZE_SCALE))
            rendered_height = mkfont_size * 1.22
            label['baseline_offset'] = int(rendered_height * 0.80)
            log.debug(f"Label '{label.get('key', 'unnamed')}': kha {kha_size} -> mkfont {mkfont_size}, font_id {font_id}")

        for button in canvas.get('buttons', []):
            kha_size = button.get('font_size', 15)
            font_id = exporter.font_id_map.get(kha_size, 0)
            button['font_id'] = font_id
            mkfont_size = max(8, int(kha_size * exporter.FONT_SIZE_SCALE))
            rendered_height = mkfont_size * 1.22
            baseline_in_font = int(rendered_height * 0.80)
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
        mkfont_size = max(8, int(kha_size * exporter.FONT_SIZE_SCALE))
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
