# Beginner Japanese Daily Podcast

One new sentence pattern per day, in a 6–10 line dialogue between Yuki (female voice) and Ken (male voice).
Each episode reuses earlier vocabulary and grammar.

## Commands
| | |
|---|---|
| `jp-podcast` (or `./today`) | generate today's episode (does nothing if it already exists) |
| `jp-podcast --force` | redo today's episode (same pattern, new dialogue) |
| `jp-podcast --force --redo-narration` | redo only the English teacher part (with `--from-json <file>` to keep the dialogue) |
| `jp-podcast --topic "at the bakery"` | override the topic |
| `./schedule.sh [HH:MM]` | run every morning (default 06:30); `./schedule.sh off` removes it |
| `./setup.sh` | one-time install (VOICEVOX on x86_64, Google TTS on ARM) |

## Listening (Pocket Casts)
Feed: **https://granolamatt.github.io/japanese-patterns-daily/feed.xml**. In Pocket Casts, paste it into Search to subscribe.
After each run, `publish.py` rebuilds `site/` (feed, the latest `keep_episodes`=60 MP3s, transcripts, roadmap) and force-pushes it
as a single commit to GitHub repo `granolamatt/japanese-patterns-daily`, which is served by GitHub Pages. The repo is public;
it holds only the lessons. `upload.sh` (Google Drive via rclone) also exists but only runs if an rclone remote `gdrive` is set up.

## Setting up on a new machine
```
git clone https://github.com/granolamatt/japanese-patterns-podcast.git && cd japanese-patterns-podcast
rsync -a oldbox:~/workarea/japanese_podcast/episodes/ episodes/   # MP3s aren't in git (scripts are)
./setup.sh                      # voices (VOICEVOX on x86_64, Google TTS on ARM)
ln -sf "$PWD/today" ~/.local/bin/jp-podcast
./schedule.sh 06:30 && sudo loginctl enable-linger "$USER"
```
Also needed: `ffmpeg`, `uv`, `python3`, a signed-in `claude` CLI, and `git push` access to the feed repo.
Turn the schedule off on the old machine first (`./schedule.sh off`) so only one machine publishes.
Any missing MP3 can be rebuilt from its script: `jp-podcast --date YYYY-MM-DD --force --from-json episodes/…/….json`.

**Secrets** go in `secrets.env` (git-ignored), e.g. `GOOGLE_TTS_API_KEY=…`. Never put keys in `config.env`, which is committed.
Commits to the public repos use the GitHub noreply address set in `config.env` (`git_author_email`).

## Schedule, reboots, outages
- Runs daily at 06:30 UTC (`./schedule.sh HH:MM` to change). A systemd user timer with linger, so it starts at boot without login;
  `Persistent=true` catches up a run missed while the machine was off.
- Needs the network only for writing the script (`claude`) and publishing (GitHub). Voices are local.
- A failed run retries every 20 min, up to 6 times. If publishing failed, the retry skips regeneration and just republishes.
- Still failed? Run `jp-podcast` by hand once the network is back. Check what happened: `journalctl --user -u jp-podcast -n 50`.

## Output: `episodes/YYYY-MM-DD/`
- `…_epNNN.mp3`: the audio
- `…_epNNN.html`: open in Chrome. Furigana, romaji, English, and a color-coded word-by-word structure breakdown
- `…_epNNN.md`: the same as plain text, with furigana written as 漢字(かんじ)
- `…_epNNN.json`: raw episode data

## Audio layout
0. **English teacher** (Kokoro voice) explains the pattern as a formula ("A wa B desu = A is B"),
   walks through each slot, and builds practical sentences piece by piece with the Japanese voices
1. The full dialogue at normal speed (just listen)
2. Each line: English meaning → **slow → normal → pause** (repeat out loud)
3. Pattern drill: hear the English, try saying it in Japanese, then slow → normal → pause
4. **Real Japanese corner**: one news/weather/announcement-style sentence using the pattern, natural speed → broken down → natural speed
5. Quick review: sentences from earlier episodes (English → you say it → Japanese)
6. Shadowing (only if the episode is short) and the full dialogue again (only if there's room). Episodes aim for 5–10 minutes (`min_minutes` / `max_minutes` in config.env)

Everything works by ear, so it's safe to use while driving.

`patterns.html` / `patterns.md` is a running cheat sheet of every pattern you know, with its English equivalent.

## Files
- `syllabus.json`: 800 patterns in learning order — all JLPT N5→N1 grammar plus news/broadcast and casual-speech patterns interleaved where they make sense. Each has a slot formula, romaji, and English equivalent ("A wa B desu = A is B"). `syllabus.v1-40.json` is the original short version.
- `patterns.html` / `patterns.md`: what you know so far + the full roadmap with progress.
- `topics.json`: topic rotation; the least-used topic is picked next.
- `progress.json`: episodes made, patterns covered, and every word seen with its use count. Words with few uses are fed back in for review.
- `config.env`: TTS backend, voice IDs (`voicevox_speaker_a=14`, `voicevox_speaker_b=11`), `slow_speed=0.72`, `english_voice=af_heart`, `english_speed=0.95`, `claude_model=`.
- `logs/`: one log per day. For the scheduled run: `journalctl --user -u jp-podcast -n 50`.

Dialogue is written by the `claude` CLI (`claude -p`, structured JSON output) and validated before any audio is made.
VOICEVOX voices: VOICEVOX:冥鳴ひまり, VOICEVOX:玄野武宏. Credit them if you ever share the audio publicly.
