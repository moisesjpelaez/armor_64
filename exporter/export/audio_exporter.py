"""
Audio Exporter - Handles audio file scanning, conversion, and configuration.

This module provides functions for exporting audio assets to N64 format,
including WAV/MP3 scanning, audioconv64 integration, and mix channel configuration.
"""

import os
import shutil
import json

import arm.utils
import arm.log as log
import armor64.utils as n64_utils


def scan_and_copy_audio(exporter):
    """Scan for audio files and copy them to the build assets folder.

    Searches for .wav and .mp3 files in the project's Assets folder
    (and subdirectories like audio/, music/, sfx/).
    Sets exporter.has_audio = True if any audio files are found.

    Args:
        exporter: N64Exporter instance to update with audio state
    """
    n64_assets = os.path.join(arm.utils.build_dir(), 'n64', 'assets')
    os.makedirs(n64_assets, exist_ok=True)

    # Search for audio files in Assets folder and subdirectories
    assets_dir = os.path.join(arm.utils.get_fp(), 'Assets')
    audio_extensions = ('.wav', '.mp3', '.aiff')
    audio_files = []

    if os.path.exists(assets_dir):
        for root, dirs, files in os.walk(assets_dir):
            for f in files:
                if f.lower().endswith(audio_extensions):
                    audio_files.append(os.path.join(root, f))

    if not audio_files:
        log.info('No audio files found in Assets folder.')
        return

    exporter.has_audio = True
    log.info(f'Found {len(audio_files)} audio file(s)')

    # Copy audio files to build/n64/assets
    for src_path in audio_files:
        filename = os.path.basename(src_path)
        # Create a safe filename (lowercase, replace spaces)
        safe_name = filename.lower().replace(' ', '_')
        dst_path = os.path.join(n64_assets, safe_name)

        # Determine if this is likely music (in music/ folder or long duration)
        # For now, simple heuristic: files in 'music' subfolder are music
        rel_path = os.path.relpath(src_path, assets_dir).lower()
        is_music = 'music' in rel_path

        if not os.path.exists(dst_path):
            n64_utils.copy_if_changed(src_path, dst_path)
            log.info(f'Copied audio: {safe_name} (music={is_music})')

        # Store audio info (without extension, as audioconv64 changes it)
        audio_name = os.path.splitext(safe_name)[0]
        exporter.exported_audio[audio_name] = {
            'source_path': src_path,
            'dest_name': safe_name,
            'is_music': is_music,
            'loop': is_music  # Music loops by default
        }

    # Copy audio module source files
    n64_utils.copy_dir('audio', 'src')


def write_audio_config(exporter):
    """Generate audio_config.h: mix channels and mixer channel count from the
    macros' n64_audio.json (see codegen/audio_codegen.py)."""
    from armor64.codegen import audio_codegen

    tmpl_path = os.path.join(n64_utils.get_n64_runtime_path(), 'src', 'audio', 'audio_config.h.j2')
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'audio', 'audio_config.h')

    with open(tmpl_path, 'r', encoding='utf-8') as f:
        tmpl_content = f.read()

    data = audio_codegen.audio_config_data(audio_codegen.load_audio_json())
    n64_utils.write_if_changed(out_path, tmpl_content.format(**data))


def generate_audio_makefile_entries(exported_audio):
    """Generate Makefile entries for audio conversion.

    Uses audioconv64 to convert .wav/.mp3 files to .wav64 format.
    All audio uses VADPCM compression (--wav-compress 1) for stability with t3d.
    Opus (level 3) has known RSP conflicts with t3d rendering on real hardware.

    Args:
        exported_audio: Dict of {audio_name: {dest_name, is_music, loop, ...}}

    Returns:
        tuple: (audio_targets_str, audio_rules_str)
    """
    if not exported_audio:
        return 'audio_conv =', '# No audio'

    targets = []
    rules = []

    for audio_name, audio_info in exported_audio.items():
        dest_name = audio_info['dest_name']
        is_music = audio_info['is_music']

        # Target: filesystem/audio_name.wav64
        target = f'filesystem/{audio_name}.wav64'
        targets.append(target)

        # Use VADPCM (level 1) for all audio - stable with t3d
        # Opus (level 3) causes RSP conflicts with t3d rendering
        if is_music:
            compress_flag = '--wav-compress 1 --wav-loop true'
        else:
            compress_flag = '--wav-compress 1'

        rule = f'''{target}: assets/{dest_name}
	@mkdir -p $(dir $@)
	@echo "    [AUDIO] $@"
	@$(N64_AUDIOCONV) {compress_flag} -o filesystem "$<"'''
        rules.append(rule)

    audio_targets = 'audio_conv = ' + ' \\\n             '.join(targets)
    audio_rules = '\n\n'.join(rules)

    return audio_targets, audio_rules
