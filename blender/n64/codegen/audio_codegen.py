"""
Audio configuration (audio/audio_config.h) from n64_audio.json, which the
macros write: the mix channels the game uses (Aura.mixChannels["name"]) and
the mixer channel count (Aura.init({channelSize: N})).
"""

from typing import Dict

from arm.n64.codegen.trait_generator import _load_ir_json

# Aura's own mix channels, always defined (index 0 is the master)
DEFAULT_MIX_CHANNELS = ["MASTER", "MUSIC", "FX"]
# libdragon's mixer supports up to 32 channels
DEFAULT_MIXER_CHANNELS = 32


def load_audio_json(build_dir: str = None) -> Dict:
    return _load_ir_json("n64_audio.json", "mix_channels", build_dir)


def audio_config_data(audio: Dict) -> Dict:
    """Values for audio_config.h.j2."""
    names = list(DEFAULT_MIX_CHANNELS)
    for name in audio.get("mix_channels") or []:
        if name not in names:
            names.append(name)
    channel_size = audio.get("channel_size") or DEFAULT_MIXER_CHANNELS
    return {
        "audio_mixer_channels": channel_size,
        "audio_mix_channel_count": len(names),
        "mix_channel_defines": "\n".join(f"#define AUDIO_MIX_{name} {i}" for i, name in enumerate(names)),
    }
