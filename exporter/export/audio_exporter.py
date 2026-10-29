"""
Audio Exporter - Handles audio file scanning, conversion, and configuration.

This module provides functions for exporting audio assets to N64 format,
including WAV/MP3 scanning, audioconv64 integration, and mix channel configuration.
"""

import os

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

        if n64_utils.copy_if_changed(src_path, dst_path):
            log.info(f'Copied audio: {safe_name}')

        # Store audio info (without extension, as audioconv64 changes it)
        audio_name = os.path.splitext(safe_name)[0]
        exporter.exported_audio[audio_name] = {
            'source_path': src_path,
            'dest_name': safe_name,
        }


def write_audio_config(exporter):
    """Generate audio_config.h: mix channels and mixer channel count from the
    macros' n64_audio.json (see codegen/audio_codegen.py)."""
    from armor64.codegen import audio_codegen

    n64_utils.render_template('src/audio/audio_config.h.j2', 'src/audio/audio_config.h',
                              **audio_codegen.audio_config_data(audio_codegen.load_audio_json()))


def generate_audio_makefile_entries(exported_audio):
    """The Makefile's audio targets and rules: each sound to .wav64 with
    audioconv64. Returns (audio_targets, audio_rules).

    VADPCM (--wav-compress 1) for every sound: Opus (3) conflicts with tiny3d's
    RSP use on hardware. Every sound is converted with loop data (--wav-loop:
    the loop start aligned, the decoder's state and first samples there), so
    whichever sound the game plays looped loops without a click; the runtime sets
    looping at each play (wav64_set_loop), so the others play once.
    """
    if not exported_audio:
        return 'audio_conv =', '# No audio'

    targets = []
    rules = []
    for audio_name, audio_info in exported_audio.items():
        target = f'filesystem/{audio_name}.wav64'
        targets.append(target)
        rules.append(f'''{target}: assets/{audio_info['dest_name']}
	@mkdir -p $(dir $@)
	@echo "    [AUDIO] $@"
	@$(N64_AUDIOCONV) --wav-compress 1 --wav-loop true -o filesystem "$<"''')

    audio_targets = 'audio_conv = ' + ' \\\n             '.join(targets)
    audio_rules = '\n\n'.join(rules)
    return audio_targets, audio_rules
