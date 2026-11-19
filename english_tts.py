"""Batch English TTS with Kokoro. Runs inside vendor/kokoro-venv.

usage: english_tts.py jobs.json
jobs.json = [{"text": "...", "out": "/path/x.wav", "voice": "af_heart", "speed": 1.0}, ...]
"""
import json
import os
import sys
import warnings

from huggingface_hub import try_to_load_from_cache  # noqa: E402

# Once the model is cached, never touch the network (survives outages).
# On a fresh machine it isn't cached yet, so allow the one-time download.
if isinstance(try_to_load_from_cache("hexgrad/Kokoro-82M", "config.json"), str):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

warnings.filterwarnings("ignore")

import numpy as np
import soundfile as sf
import torch
from kokoro import KPipeline

jobs = json.load(open(sys.argv[1]))
device = "cuda" if torch.cuda.is_available() else "cpu"
pipe = KPipeline(lang_code="a", device=device, repo_id="hexgrad/Kokoro-82M")
for job in jobs:
    chunks = [np.asarray(audio) for _, _, audio in
              pipe(job["text"], voice=job["voice"], speed=job["speed"]) if audio is not None]
    audio = np.concatenate(chunks) if chunks else np.zeros(2400, dtype=np.float32)
    sf.write(job["out"], audio, 24000, subtype="PCM_16")
print(f"kokoro: {len(jobs)} clips on {device}")
