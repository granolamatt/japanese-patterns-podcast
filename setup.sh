#!/usr/bin/env bash
# One-time setup: picks the TTS backend from the CPU type and installs it.
#   x86_64 -> VOICEVOX engine (free, local)   -> ./vendor/voicevox_engine
#   ARM    -> Google Cloud Text-to-Speech      (prints API-key instructions)
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$PWD
ARCH=$(uname -m)
VV_VERSION=${VV_VERSION:-0.26.2}
mkdir -p vendor

# English teacher voice (Kokoro, local neural TTS) — used on every architecture
if [[ ! -x vendor/kokoro-venv/bin/python ]] || ! vendor/kokoro-venv/bin/python -c "import kokoro" 2>/dev/null; then
  echo "Installing English voice (Kokoro)..."
  uv venv -q --allow-existing --python 3.11 vendor/kokoro-venv
  # CPU-only torch: small download, and plenty fast for short clips
  VIRTUAL_ENV=vendor/kokoro-venv uv pip install -q torch --index-url https://download.pytorch.org/whl/cpu
  VIRTUAL_ENV=vendor/kokoro-venv uv pip install -q kokoro soundfile pip \
    en_core_web_sm@https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
fi

# pre-download the English voice model (one time)
vendor/kokoro-venv/bin/python -c "from kokoro import KPipeline; KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')" >/dev/null 2>&1 || true

# set tts=... in config.env without touching the other settings
set_tts() {
  touch config.env
  if grep -q '^tts=' config.env; then sed -i "s/^tts=.*/tts=$1/" config.env; else echo "tts=$1" >> config.env; fi
}

echo "CPU architecture: $ARCH"
case "$ARCH" in
  x86_64)
    set_tts voicevox
    ;;
  aarch64|arm64|armv7l|armv8l)
    set_tts google
    cat <<'EOF'

ARM CPU detected -> using Google Cloud Text-to-Speech.
Get an API key:
  1. Go to https://console.cloud.google.com/ and create (or pick) a project.
  2. Enable billing on the project (the free tier covers ~1M WaveNet chars/month).
  3. APIs & Services -> Library -> search "Cloud Text-to-Speech API" -> Enable.
  4. APIs & Services -> Credentials -> Create credentials -> API key.
     Click the key -> "API restrictions" -> restrict it to Cloud Text-to-Speech API.
  5. Save it (secrets.env is git-ignored, never commit keys):
       echo 'GOOGLE_TTS_API_KEY=YOUR_KEY' >> secrets.env && chmod 600 secrets.env
EOF
    exit 0
    ;;
  *) echo "Unsupported architecture $ARCH"; exit 1 ;;
esac

# --- VOICEVOX (x86_64) -------------------------------------------------------
if [[ -x vendor/voicevox_engine/run ]]; then
  echo "VOICEVOX engine already installed."; exit 0
fi

# 7-Zip extractor (official static build, no root needed)
if [[ ! -x vendor/7zz ]]; then
  echo "Downloading 7-Zip..."
  SZ_URL=$(curl -fsL https://api.github.com/repos/ip7z/7zip/releases/latest \
    | grep -oE 'https://[^"]+linux-x64\.tar\.xz' | head -1)
  curl -fL --retry 3 -o vendor/7z.tar.xz "$SZ_URL"
  tar -xJf vendor/7z.tar.xz -C vendor 7zz
  rm vendor/7z.tar.xz
fi

ARCHIVE=vendor/voicevox_engine-linux-cpu-x64-$VV_VERSION.7z.001
URL=https://github.com/VOICEVOX/voicevox_engine/releases/download/$VV_VERSION/voicevox_engine-linux-cpu-x64-$VV_VERSION.7z.001
echo "Downloading VOICEVOX engine $VV_VERSION (~1.9 GB, resumable)..."
curl -fL --retry 5 -C - -o "$ARCHIVE" "$URL"

echo "Extracting..."
rm -rf vendor/extract && mkdir vendor/extract
vendor/7zz x -y -ovendor/extract "$ARCHIVE" >/dev/null
# archive contains a single top-level folder (e.g. linux-cpu-x64/)
inner=$(find vendor/extract -maxdepth 2 -name run -type f -printf '%h\n' | head -1)
mv "$inner" vendor/voicevox_engine
rm -rf vendor/extract "$ARCHIVE"
chmod +x vendor/voicevox_engine/run
echo "VOICEVOX installed at $ROOT/vendor/voicevox_engine"
