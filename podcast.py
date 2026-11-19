#!/usr/bin/env python3
"""Daily beginner Japanese podcast generator.

Pipeline: pick next pattern + topic  ->  Claude writes dialogue (JSON)  ->
validate  ->  TTS each line (slow + normal)  ->  ffmpeg joins into one MP3  ->
write transcript (.md / .html)  ->  update progress.json.

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EPISODES_DIR = ROOT / "episodes"
PROGRESS_FILE = ROOT / "progress.json"
SYLLABUS_FILE = ROOT / "syllabus.json"
TOPICS_FILE = ROOT / "topics.json"
CONFIG_FILE = ROOT / "config.env"

RATE = 24000  # all audio is 24 kHz mono 16-bit

SPEAKERS = {
    "A": {"name": "Yuki", "jp": "ゆき"},
    "B": {"name": "Ken", "jp": "けん"},
}

DEFAULTS = {
    "tts": "voicevox",
    "voicevox_url": "http://127.0.0.1:50021",
    "voicevox_speaker_a": "14",   # 冥鳴ひまり (calm female)
    "voicevox_speaker_b": "11",   # 玄野武宏 (male)
    "google_voice_a": "ja-JP-Neural2-B",
    "google_voice_b": "ja-JP-Neural2-C",
    "slow_speed": "0.72",
    "english_voice": "af_heart",  # Kokoro voice for the English teacher
    "english_speed": "0.95",
    "claude_model": "",           # empty = CLI default
}
KOKORO_PY = ROOT / "vendor" / "kokoro-venv" / "bin" / "python"
PATTERNS_MD = ROOT / "patterns.md"
PATTERNS_HTML = ROOT / "patterns.html"

ROLES = ["noun", "pronoun", "particle", "verb", "i-adjective", "na-adjective",
         "adverb", "copula", "question-word", "conjunction", "number",
         "expression", "auxiliary", "prefix-suffix", "punct"]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- config / io
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for f in (CONFIG_FILE, ROOT / "secrets.env"):  # secrets.env is git-ignored
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip().lower()] = v.strip()
    for k in list(cfg):
        env = os.environ.get("JPOD_" + k.upper())
        if env is not None:
            cfg[k] = env
    cfg["google_tts_api_key"] = (os.environ.get("GOOGLE_TTS_API_KEY")
                                 or cfg.get("google_tts_api_key", ""))
    return cfg


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------- progress
def load_progress() -> dict:
    return load_json(PROGRESS_FILE, {"episodes": [], "vocab": {}})


def rebuild_vocab(progress: dict) -> None:
    """Recompute the vocab table from every saved episode JSON."""
    vocab: dict[str, dict] = {}
    for ep in progress["episodes"]:
        data = load_json(ROOT / ep["data_file"], None)
        if not data:
            continue
        all_words = [w for ln in data["lines"] for w in ln["words"]]
        all_words += [w for ex in data["examples"] for w in ex["words"]]
        for w in all_words:
            if w["role"] in ("punct", "particle"):
                continue
            key = w["jp"]
            v = vocab.setdefault(key, {"kana": w["kana"], "en": w["en"],
                                       "role": w["role"],
                                       "first_episode": ep["number"], "uses": 0})
            v["uses"] += 1
            v["last_episode"] = ep["number"]
    progress["vocab"] = vocab


def pick_pattern(progress: dict, syllabus: list) -> dict | None:
    done = {ep["pattern_id"] for ep in progress["episodes"]}
    for p in syllabus:
        if p["id"] not in done:
            return p
    return None  # syllabus finished -> review episodes


def pick_topic(progress: dict, topics: list) -> str:
    counts = {t: 0 for t in topics}
    last_seen = {t: -1 for t in topics}
    for i, ep in enumerate(progress["episodes"]):
        if ep["topic"] in counts:
            counts[ep["topic"]] += 1
            last_seen[ep["topic"]] = i
    return min(topics, key=lambda t: (counts[t], last_seen[t], topics.index(t)))


# ---------------------------------------------------------------- kana / romaji
KANJI_RE = re.compile(r"[㐀-鿿々]")
KANA_OK_RE = re.compile(r"^[぀-ヿー・ 　。、！？!?,.…「」〜~a-zA-Z0-9０-９]+$")


def kata_to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


_ROMA = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "ゐ": "i", "ゑ": "e", "を": "o", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ゔ": "vu", "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
    "ゃ": "ya", "ゅ": "yu", "ょ": "yo", "ゎ": "wa",
}
_YOON = {"ゃ": "a", "ゅ": "u", "ょ": "o"}
_SMALL_V = {"ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o"}
_PUNCT = {"。": ".", "、": ",", "？": "?", "！": "!", "・": " ", "ー": "-",
          "「": '"', "」": '"', "〜": "~", "…": "...", "　": " "}


def kana_to_romaji(kana: str) -> str:
    s = kata_to_hira(kana)
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if c == "っ":
            # double the next consonant
            j = i + 1
            if j < len(s):
                r = kana_to_romaji(s[j:j + 2] if s[j + 1:j + 2] in _YOON else s[j])
                out.append("t" if r.startswith("ch") else r[:1] if r[:1] not in "aeiou" else "")
            i += 1
            continue
        if c == "ー":
            prev = "".join(out)
            out.append(next((ch for ch in reversed(prev) if ch in "aeiou"), ""))
            i += 1
            continue
        if c in _ROMA:
            r = _ROMA[c]
            if nxt in _YOON and c not in _YOON:
                base = r[:-1]
                if base in ("sh", "ch", "j"):
                    r = base + _YOON[nxt]
                else:
                    r = base + "y" + _YOON[nxt]
                i += 1
            elif nxt in _SMALL_V and c in ("ふ", "て", "で", "う", "ゔ", "し", "ち", "じ"):
                base = {"ふ": "f", "て": "t", "で": "d", "う": "w", "ゔ": "v",
                        "し": "sh", "ち": "ch", "じ": "j"}[c]
                r = base + _SMALL_V[nxt]
                i += 1
            if c == "ん" and i + 1 < len(s) and s[i + 1] in "あいうえおやゆよ":
                r = "n'"
            out.append(r)
        else:
            out.append(_PUNCT.get(c, c))
        i += 1
    return "".join(out)


PARTICLE_ROMAJI = {"は": "wa", "へ": "e", "を": "o"}


def word_romaji(w: dict) -> str:
    if w["role"] == "particle" and w["kana"] in PARTICLE_ROMAJI:
        return PARTICLE_ROMAJI[w["kana"]]
    r = kana_to_romaji(w["kana"])
    # common spelling: ~wa endings in greetings (こんにちは / こんばんは)
    if w["role"] == "expression" and w["kana"].endswith(("にちは", "ばんは")):
        r = r[:-2] + "wa"
    return r


def line_romaji(words: list[dict]) -> str:
    names = {s["jp"] for s in SPEAKERS.values()}
    parts: list[str] = []
    cap_next = True
    for w in words:
        r = word_romaji(w)
        if w["role"] == "punct" and parts:
            parts[-1] += r.strip()
            cap_next = w["jp"] in "。？！?!"
            continue
        if r and (cap_next or w["kana"] in names or w["en"].lower().endswith("(name)")):
            r = r[:1].upper() + r[1:]
        cap_next = False
        parts.append(r)
    return " ".join(p for p in parts if p)


def split_ruby(jp: str, kana: str) -> tuple[str, str, str, str]:
    """Return (prefix, base, reading, suffix): strip shared leading/trailing kana
    so furigana only sits over the kanji part (食べます -> 食[た]べます)."""
    hj, hk = kata_to_hira(jp), kata_to_hira(kana)
    a = 0
    while a < len(jp) and a < len(kana) and not KANJI_RE.match(jp[a]) and hj[a] == hk[a]:
        a += 1
    b = 0
    while (b < len(jp) - a and b < len(kana) - a and not KANJI_RE.match(jp[-1 - b])
           and hj[-1 - b] == hk[-1 - b]):
        b += 1
    return jp[:a], jp[a:len(jp) - b], kana[a:len(kana) - b], jp[len(jp) - b:]


def furigana_html(w: dict) -> str:
    jp = w["jp"]
    if not KANJI_RE.search(jp):
        return html.escape(jp)
    pre, base, read, suf = split_ruby(jp, w["kana"])
    return (html.escape(pre) + f"<ruby>{html.escape(base)}<rt>{html.escape(read)}</rt></ruby>"
            + html.escape(suf))


def furigana_text(w: dict) -> str:
    jp = w["jp"]
    if not KANJI_RE.search(jp):
        return jp
    pre, base, read, suf = split_ruby(jp, w["kana"])
    return f"{pre}{base}({read}){suf}"


def line_jp(words: list[dict]) -> str:
    return "".join(w["jp"] for w in words)


# ---------------------------------------------------------------- the writer (Claude)
WORD_SCHEMA = {
    "type": "object",
    "properties": {
        "jp": {"type": "string", "description": "word as written (kanji/kana)"},
        "kana": {"type": "string", "description": "reading in kana only; katakana words stay katakana; punctuation = same as jp"},
        "en": {"type": "string", "description": "short English gloss; for particles say the function, e.g. 'topic marker'"},
        "role": {"type": "string", "enum": ROLES},
    },
    "required": ["jp", "kana", "en", "role"],
}
SENTENCE = {"type": "array", "items": WORD_SCHEMA, "minItems": 1}
EPISODE_SCHEMA = {
    "type": "object",
    "properties": {
        "title_en": {"type": "string"},
        "title_jp": {"type": "string", "description": "short Japanese title, beginner level"},
        "lines": {
            "type": "array", "minItems": 6, "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string", "enum": ["A", "B"]},
                    "words": SENTENCE,
                    "en": {"type": "string", "description": "natural English translation"},
                    "literal": {"type": "string", "description": "word-order-literal English, e.g. 'I TOPIC student am'"},
                },
                "required": ["speaker", "words", "en", "literal"],
            },
        },
        "explanation": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "3-5 sentence plain-English explanation of the new pattern focused on sentence structure"},
                "structure_notes": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5},
                "connects_to": {"type": "string", "description": "one or two sentences linking this pattern to earlier ones"},
            },
            "required": ["summary", "structure_notes", "connects_to"],
        },
        "examples": {
            "type": "array", "minItems": 3, "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "words": SENTENCE,
                    "en": {"type": "string"},
                    "swap": {"type": "string", "description": "which slot was swapped, e.g. 'noun: コーヒー → おちゃ'"},
                },
                "required": ["words", "en", "swap"],
            },
        },
        "new_vocab": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"jp": {"type": "string"}, "kana": {"type": "string"}, "en": {"type": "string"}},
                      "required": ["jp", "kana", "en"]},
        },
    },
    "required": ["title_en", "title_jp", "lines", "explanation", "examples", "new_vocab"],
}


LEARNER = {
    "N5": "a beginner (JLPT N5 level)",
    "N4": "an advanced beginner (working through JLPT N4)",
    "N3": "an intermediate learner (working through JLPT N3)",
    "N2": "an upper-intermediate learner (working through JLPT N2)",
    "N1": "an advanced learner (working through JLPT N1)",
}
SETTING = {
    None: "They are friends.",
    "news": ("Today's pattern is news/broadcast Japanese, so make the dialogue a short news segment: "
             "Yuki is the studio anchor, Ken is a reporter, in formal broadcast style (です/ます, news phrasing)."),
    "casual": ("Today's pattern is casual spoken Japanese: Yuki and Ken are close friends chatting in natural "
               "casual speech (plain forms, contractions, sentence-final particles)."),
}


def level_of(pattern: dict | None) -> tuple[str, str | None]:
    lvl = (pattern or {}).get("level") or "N5"
    base, _, reg = lvl.partition("+")
    return (base if base in LEARNER else "N5"), (reg or None)


def build_prompt(pattern: dict | None, covered: list[dict], topic: str,
                 progress: dict, number: int) -> str:
    vocab = progress.get("vocab", {})
    known = "\n".join(f"- {jp} ({v['kana']}) = {v['en']}" for jp, v in vocab.items()) or "(none yet — this is episode 1)"
    review = sorted(vocab.items(), key=lambda kv: (kv[1]["uses"], kv[1].get("last_episode", 0)))[:10]
    review_txt = ", ".join(jp for jp, _ in review) or "(none)"
    covered_txt = "\n".join(f"- {p['pattern']}: {p['name']}  e.g. {p['formula']}" for p in covered) or "(none — this is the first pattern)"
    plain_ok = any(p["id"] in ("dictionary-form", "plain-speech") for p in covered + ([pattern] if pattern else []))

    level, register = level_of(pattern)
    if pattern:
        focus = (f"NEW PATTERN for this episode: {pattern['pattern']} — {pattern['name']}\n"
                 f"Structure: {pattern['formula']}"
                 + (f"   ({pattern['romaji']} = {pattern['english']})" if pattern.get("english") else "")
                 + (f"\nReference example: {pattern['example_jp']} = {pattern['example_en']}" if pattern.get("example_jp") else "")
                 + "\nUse the new pattern in at least 3 dialogue lines, and in every example sentence.")
    else:
        focus = ("The syllabus is complete: this is a REVIEW episode. Pick 2-3 earlier patterns that "
                 "combine naturally and treat 'combining them' as the pattern to explain.")

    return f"""You are writing episode {number} of a daily podcast for an English-speaking adult who is
{LEARNER[level]}. Their goal is to understand Japanese SENTENCE STRUCTURE (word order,
particles, how adjectives and verbs attach) well enough to follow real content later.

Speakers: A = {SPEAKERS['A']['name']} ({SPEAKERS['A']['jp']}), B = {SPEAKERS['B']['name']} ({SPEAKERS['B']['jp']}). {SETTING[register]}
Topic: {topic}

{focus}

GRAMMAR ALREADY LEARNED (you may use ONLY these plus the new pattern):
{covered_txt}

Always allowed set phrases (role "expression"): こんにちは, おはようございます, ありがとうございます,
すみません, おねがいします, はい, いいえ, ええ, そうですか, そうですね, じゃあ, どうぞ, いただきます.
{'' if plain_ok else 'Use polite です/ます style only — no plain/casual forms.'}

KNOWN VOCABULARY (reuse these heavily — the learner builds up gradually):
{known}

Words that need review — use at least 4 of them if they fit: {review_txt}

Rules:
- 6 to 10 short lines, alternating A and B where natural. Keep each line to one or two short sentences.
- Introduce at most 6 new words, {'all extremely common everyday words' if level in ('N5','N4') else 'common words at this level (everyday life and news)'}. List them in new_vocab.
- Kanji: {'use kanji only for very common words (私, 今日, 行きます, 好き, 何, 日本, 学生 ...); otherwise kana.' if level in ('N5','N4') else 'write naturally, as a native writer would.'}
- Tokenize every sentence into words for a learner: particles (は, が, を, に, で, の, と, も, か, ね, よ, から ...)
  are separate tokens with role "particle"; です/でした/じゃないです → role "copula"; polite verb endings stay
  attached to the verb (食べます is one token); punctuation (。、？！) is its own token with role "punct".
- Concatenating the jp of all tokens must give the exact sentence. kana must be the kana reading of that token
  (no kanji, no romaji). For the particle は write kana "は" (it is read "wa"; the software handles that).
- "literal" is a word-for-word English gloss in Japanese order, with particles in CAPS
  (e.g. "I TOPIC coffee OBJECT drink"). This is how the learner sees the structure.
- The explanation must be structure-first: which slot each piece fills, word order, what the particle marks.
  Write for someone with no linguistics background.
- Examples are swap-in drills: keep the pattern, swap one slot each time (noun, adjective, place...), using
  known words where possible.
"""


NARRATION_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern_romaji": {"type": "string", "description": "the pattern with slot letters in romaji, e.g. 'A wa B desu'"},
        "pattern_jp": {"type": "string", "description": "the pattern with slot letters in Japanese, e.g. 'AはBです'"},
        "pattern_english": {"type": "string", "description": "the English equivalent with the same slot letters, e.g. 'A is B'"},
        "slots": {"type": "array", "description": "what can go in each slot",
                  "items": {"type": "object",
                            "properties": {"slot": {"type": "string"}, "fits": {"type": "string"}},
                            "required": ["slot", "fits"]}},
        "segments": {
            "type": "array", "minItems": 12,
            "description": "the spoken teacher script, in order",
            "items": {"$ref": "#/$defs/segment"},
        },
        "real_world": {
            "type": "object",
            "description": "the 'real Japanese' corner that plays near the end of the episode",
            "properties": {
                "style": {"type": "string", "description": "e.g. 'weather report', 'news headline', 'train announcement'"},
                "sentence_jp": {"type": "string"},
                "sentence_en": {"type": "string"},
                "segments": {"type": "array", "minItems": 6, "items": {"$ref": "#/$defs/segment"}},
            },
            "required": ["style", "sentence_jp", "sentence_en", "segments"],
        },
        "builder": {
            "type": "array", "minItems": 2, "maxItems": 3,
            "description": "practical sentences built up step by step",
            "items": {"type": "object",
                      "properties": {"steps": {"type": "array", "minItems": 2, "items": {
                          "type": "object",
                          "properties": {"jp": {"type": "string"}, "romaji": {"type": "string"}, "en": {"type": "string"}},
                          "required": ["jp", "romaji", "en"]}}},
                      "required": ["steps"]},
        },
        "tip": {"type": "string", "description": "one common beginner mistake or memory trick"},
    },
    "required": ["pattern_romaji", "pattern_jp", "pattern_english", "slots", "segments", "builder", "tip",
                 "real_world"],
    "$defs": {"segment": {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": ["en", "jp"]},
                       "text": {"type": "string"},
                       "speaker": {"type": "string", "enum": ["A", "B"]},
                       "speed": {"type": "string", "enum": ["teaching", "natural"],
                                 "description": "jp only: 'teaching' = a bit slow and clear, 'natural' = real-life speed"}},
        "required": ["kind", "text", "speaker", "speed"]}},
}

JP_CHAR_RE = re.compile(r"[぀-ヿ㐀-鿿]")


def build_narration_prompt(data: dict, pattern: dict | None, covered: list[dict]) -> str:
    covered_txt = "\n".join(f"- {p['pattern']}: {p['name']}" for p in covered) or "(none — first episode)"
    dialogue = "\n".join(f"{SPEAKERS[l['speaker']]['name']}: {line_jp(l['words'])}  ({l['en']})"
                         for l in data["lines"])
    pat = (f"{pattern['pattern']} — {pattern['name']}; structure {pattern['formula']}"
           + (f"; say it as \"{pattern['romaji']} = {pattern['english']}\"" if pattern and pattern.get("english") else "")
           + f" (learner level: {level_of(pattern)[0]}{', ' + level_of(pattern)[1] + ' register' if level_of(pattern)[1] else ''})"
           if pattern else "REVIEW episode: " + data["explanation"]["summary"])
    return f"""You are the friendly English-speaking teacher on a beginner Japanese podcast. The learner is an adult
English speaker who wants to understand HOW JAPANESE SENTENCES ARE BUILT, so they can learn reusable
patterns and then listen to real Japanese. Write the spoken teaching segment that plays BEFORE today's dialogue.

Today's pattern: {pat}
Patterns learned before today:
{covered_txt}

Today's dialogue (for reference; reuse its sentences as examples):
{dialogue}

IMPORTANT: the learner listens while DRIVING. Everything must work by ear alone: never ask them to look at,
read, write or picture text; never spell anything; say the English meaning of every Japanese sentence.
They also listen to a real Japanese news podcast and can't follow it yet, so show how each pattern
lives inside real broadcast/everyday Japanese.

The script is a list of segments. "en" segments are read by an English voice; "jp" segments are read by a
Japanese voice (speaker A = Yuki, female; B = Ken, male). Rules:
- NEVER write romaji anywhere in en segments: the English voice would pronounce it with English sounds and
  teach the wrong pronunciation. Whenever an English sentence mentions a Japanese word, particle, ending or
  short phrase, write it in Japanese script inside curly braces; it is spoken by the Japanese voice in the
  middle of the English sentence. Example: "The little word {{は}} marks the topic, and {{です}} comes last."
  Keep each {{...}} short (a word or short phrase). Whole sentences go in their own jp segment.
  Outside braces, en text must contain no Japanese script. English names (Yuki, Ken, Tokyo) are fine.
  Particles can stand alone in braces: {{は}} is pronounced "wa", {{へ}} "e", {{を}} "o" by the Japanese voice.
  Never describe a Japanese sound with English spelling ("sounds like wah", "dess") — just use {{...}}.
- jp segments contain only Japanese (kana/kanji, may use slot letters like A and B, e.g. "AはBです").
- Keep en segments short (1-3 sentences) so English and Japanese alternate naturally. For every jp
  segment, the next en segment usually says what it means.

Teach in this order (about 2-3 minutes spoken, 300-450 English words total):
1. Name the pattern and give its English equivalent formula FIRST, very plainly:
   e.g. "Today's pattern is {{AはBです}}. It means: A is B." 
2. Walk through the slots: what goes in A, what goes in B, what each little word (particle/ending) does,
   and how the Japanese word order differs from English (e.g. the verb or desu comes LAST).
3. Build 2-3 practical, everyday sentences step by step: start with one piece, add the particle,
   add the next piece, finish the sentence — say each stage in a jp segment and explain it in English.
   These must match the "builder" field.
4. Show one swap: same skeleton, different words, to prove the pattern is reusable.
5. One common mistake or memory trick.
6. {"Briefly connect to earlier patterns (how today's piece snaps onto what they already know)." if covered else "Encourage them: this is the foundation everything else builds on."}
7. End with a line like "Now let's hear Yuki and Ken use it."

SEPARATELY, write "real_world": the "real Japanese" corner, played after the drills (about 1 minute):
- One sentence in the style of something real: a weather report, a short news line, a station or store
  announcement, a TV/radio presenter. It must use today's pattern and mostly words the learner knows;
  at most 2 words they don't know, which you explain. Keep it short (under ~20 syllables if possible).
- Segments: introduce the style in English ("On the radio's weather report, you might hear...");
  play the sentence with speed "natural"; break it into 2-4 chunks, each chunk as a "teaching" jp segment
  followed by English explaining what the chunk is and which slot of the pattern it fills; then play the
  whole sentence "natural" again; end with one English line on what to listen for in real broadcasts.

"builder" restates the step-by-step sentences from part 3 (jp, romaji, en for each step) for the transcript.
Use speed "teaching" for jp segments in the main script; it is ignored for en segments.
"slots" lists each slot letter and what kinds of words fit (e.g. A: a person, thing or topic).
"""


INLINE_JP_RE = re.compile(r"\{([^{}]+)\}")
ROMAJI_FLAGS = {"wa", "ga", "wo", "desu", "masu", "mashita", "masen", "deshita", "ka", "ne", "yo", "kara",
                "dewa", "janai", "nai", "imasu", "arimasu", "kudasai", "watashi", "kore", "sore", "ano",
                "kono", "sono", "nan", "nani", "doko", "dare", "de", "mo", "ni", "te", "ta", "da", "deshou",
                "darou", "shimasu", "imashita", "nakereba", "naranai", "mitai", "rashii", "you", "sou", "wah", "dess", "deh", "kah", "noh"}
ENGLISH_WORDS = {"no", "to", "a", "e", "o", "so", "made", "are", "you", "ta", "te", "name", "sake", "ken", "yuki"}


def inline_html(text: str) -> str:
    return INLINE_JP_RE.sub(r"<b>\1</b>", html.escape(text))


def split_mixed(text: str) -> list[tuple[str, str]]:
    """'The word {は} marks it' -> [('en','The word'), ('jp','は'), ('en','marks it')]"""
    out, pos = [], 0
    for m in INLINE_JP_RE.finditer(text):
        if text[pos:m.start()].strip():
            out.append(("en", text[pos:m.start()].strip()))
        out.append(("jp", m.group(1).strip()))
        pos = m.end()
    if text[pos:].strip():
        out.append(("en", text[pos:].strip()))
    # drop English fragments that are only punctuation
    return [(k, t) for k, t in out if k == "jp" or re.search(r"[A-Za-z0-9]", t)]


def romaji_in(text: str, extra: set[str]) -> list[str]:
    import unicodedata
    plain = "".join(c for c in unicodedata.normalize("NFKD", INLINE_JP_RE.sub(" ", text))
                    if not unicodedata.combining(c)).lower()
    words = set(re.findall(r"[a-z]+(?:-[a-z]+)?", plain))
    flags = (ROMAJI_FLAGS | extra) - ENGLISH_WORDS
    return sorted(w for w in words if w in flags or w.split("-")[-1] in {"te", "masu", "tai", "nai"} and "-" in w)


def validate_narration(n: dict, data: dict | None = None) -> list[str]:
    errs = []
    extra: set[str] = set()
    for x in (data or {}).get("lines", []) + (data or {}).get("examples", []):
        for w in x["words"]:
            r = re.sub(r"[^a-z]", "", word_romaji(w).lower())
            if len(r) >= 3 and w["role"] not in ("punct",):
                extra.add(r)
    segs = n.get("segments", []) + n.get("real_world", {}).get("segments", [])
    for i, s in enumerate(segs, 1):
        if s["kind"] == "en":
            outside = INLINE_JP_RE.sub("", s["text"])
            if JP_CHAR_RE.search(outside):
                errs.append(f"segment {i}: Japanese script outside {{braces}} in an en segment: {s['text']!r}")
            for m in INLINE_JP_RE.finditer(s["text"]):
                if not JP_CHAR_RE.search(m.group(1)):
                    errs.append(f"segment {i}: {{{m.group(1)}}} must be Japanese script, not romaji")
            bad = romaji_in(s["text"], extra)
            if bad:
                errs.append(f"segment {i}: romaji {bad} in English text — write it in Japanese inside braces: {s['text']!r}")
        if s["kind"] == "jp" and not JP_CHAR_RE.search(s["text"]):
            errs.append(f"segment {i} is 'jp' but has no Japanese: {s['text']!r}")
        if not s["text"].strip():
            errs.append(f"segment {i} is empty")
    if sum(s["kind"] == "en" for s in n.get("segments", [])) < 6:
        errs.append("too few English segments")
    return errs


def write_narration(data, pattern, covered, cfg) -> dict:
    prompt = build_narration_prompt(data, pattern, covered)
    last_err = ""
    for attempt in range(1, 4):
        log(f"Writing English teacher script (attempt {attempt})...")
        p = prompt + (f"\n\nYOUR PREVIOUS ATTEMPT HAD THESE PROBLEMS — fix them:\n{last_err}" if last_err else "")
        try:
            n = call_claude(p, cfg, NARRATION_SCHEMA)
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            last_err = str(e); log(f"  failed: {last_err[:300]}"); continue
        errs = validate_narration(n, data)
        if not errs:
            return n
        last_err = "\n".join(f"- {e}" for e in errs)
        log("  validation problems:\n" + last_err)
    raise SystemExit(f"Could not get a valid teacher script. Last error:\n{last_err}")


def call_claude(prompt: str, cfg: dict, schema: dict | None = None) -> dict:
    cmd = ["claude", "-p", "--output-format", "json",
           "--json-schema", json.dumps(schema or EPISODE_SCHEMA, ensure_ascii=False),
           "--tools", ""]
    if cfg.get("claude_model"):
        cmd += ["--model", cfg["claude_model"]]
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                         timeout=900, cwd=tempfile.gettempdir())
    if res.returncode != 0:
        raise RuntimeError(f"claude CLI failed ({res.returncode}): {res.stderr.strip() or res.stdout.strip()[:500]}")
    out = json.loads(res.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude returned an error: {out.get('result')}")
    data = out.get("structured_output")
    if data is None:
        txt = out.get("result", "").strip()
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
        data = json.loads(txt)
    return data


def validate_episode(data: dict) -> list[str]:
    errs: list[str] = []
    lines = data.get("lines", [])
    if not 6 <= len(lines) <= 10:
        errs.append(f"dialogue has {len(lines)} lines; need 6-10")
    if {ln.get("speaker") for ln in lines} != {"A", "B"}:
        errs.append("both speakers A and B must talk")
    sentences = [("line", i + 1, ln["words"]) for i, ln in enumerate(lines)]
    sentences += [("example", i + 1, ex["words"]) for i, ex in enumerate(data.get("examples", []))]
    for kind, n, words in sentences:
        if not words:
            errs.append(f"{kind} {n} has no words")
        for w in words:
            if w["role"] not in ROLES:
                errs.append(f"{kind} {n}: bad role {w['role']!r}")
            if not w["jp"].strip():
                errs.append(f"{kind} {n}: empty token")
            if not KANA_OK_RE.match(w["kana"]) or KANJI_RE.search(w["kana"]):
                errs.append(f"{kind} {n}: kana for {w['jp']!r} is not pure kana: {w['kana']!r}")
        if words and not any(w["role"] != "punct" for w in words):
            errs.append(f"{kind} {n}: only punctuation")
    if not 3 <= len(data.get("examples", [])) <= 5:
        errs.append("need 3-5 examples")
    return errs


def write_episode(pattern, covered, topic, progress, number, cfg) -> dict:
    prompt = build_prompt(pattern, covered, topic, progress, number)
    last_err = ""
    for attempt in range(1, 4):
        log(f"Writing dialogue with Claude (attempt {attempt})...")
        p = prompt + (f"\n\nYOUR PREVIOUS ATTEMPT HAD THESE PROBLEMS — fix them:\n{last_err}" if last_err else "")
        try:
            data = call_claude(p, cfg)
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            last_err = str(e)
            log(f"  failed: {last_err[:300]}")
            continue
        errs = validate_episode(data)
        if not errs:
            return data
        last_err = "\n".join(f"- {e}" for e in errs)
        log("  validation problems:\n" + last_err)
    raise SystemExit(f"Could not get a valid episode after 3 attempts. Last error:\n{last_err}")


# ---------------------------------------------------------------- TTS backends
class VoicevoxTTS:
    def __init__(self, cfg: dict):
        self.url = cfg["voicevox_url"].rstrip("/")
        self.voices = {"A": int(cfg["voicevox_speaker_a"]), "B": int(cfg["voicevox_speaker_b"])}
        self.proc: subprocess.Popen | None = None

    def _alive(self) -> bool:
        try:
            urllib.request.urlopen(self.url + "/version", timeout=2).read()
            return True
        except Exception:
            return False

    def start(self) -> None:
        if self._alive():
            return
        run = ROOT / "vendor" / "voicevox_engine" / "run"
        if not run.exists():
            raise SystemExit("VOICEVOX engine not installed. Run ./setup.sh first.")
        port = urllib.parse.urlparse(self.url).port or 50021
        log("Starting VOICEVOX engine...")
        logf = open(ROOT / "vendor" / "voicevox.log", "ab")
        self.proc = subprocess.Popen([str(run), "--host", "127.0.0.1", "--port", str(port)],
                                     stdout=logf, stderr=subprocess.STDOUT, cwd=run.parent)
        for _ in range(180):
            if self._alive():
                return
            if self.proc.poll() is not None:
                raise SystemExit("VOICEVOX engine exited; see vendor/voicevox.log")
            time.sleep(1)
        raise SystemExit("VOICEVOX engine did not start within 3 minutes")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def synth(self, text: str, speaker: str, speed: float) -> bytes:
        sid = self.voices[speaker]
        q = urllib.parse.urlencode({"text": text, "speaker": sid})
        req = urllib.request.Request(f"{self.url}/audio_query?{q}", method="POST")
        query = json.loads(urllib.request.urlopen(req, timeout=60).read())
        query.update(speedScale=speed, prePhonemeLength=0.1, postPhonemeLength=0.1,
                     outputSamplingRate=RATE, outputStereo=False)
        req = urllib.request.Request(f"{self.url}/synthesis?speaker={sid}",
                                     data=json.dumps(query).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=120).read()


class GoogleTTS:
    def __init__(self, cfg: dict):
        self.key = cfg["google_tts_api_key"]
        if not self.key:
            raise SystemExit("Set GOOGLE_TTS_API_KEY in secrets.env (see ./setup.sh output).")
        self.voices = {"A": cfg["google_voice_a"], "B": cfg["google_voice_b"]}

    def start(self): pass
    def stop(self): pass

    def synth(self, text: str, speaker: str, speed: float) -> bytes:
        body = {"input": {"text": text},
                "voice": {"languageCode": "ja-JP", "name": self.voices[speaker]},
                "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": RATE,
                                "speakingRate": speed}}
        req = urllib.request.Request(
            "https://texttospeech.googleapis.com/v1/text:synthesize?key=" + urllib.parse.quote(self.key),
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return base64.b64decode(resp["audioContent"])


# ---------------------------------------------------------------- audio assembly
def write_wav(path: Path, samples: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(samples)


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(RATE * seconds)


def chime(freqs=(880.0, 1318.5)) -> bytes:
    out = bytearray()
    for f in freqs:
        n = int(RATE * 0.22)
        for i in range(n):
            env = math.exp(-5 * i / n)
            out += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * f * i / RATE)))
    return bytes(out) + silence(0.5)


def wav_duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


class EnglishTTS:
    """English teacher voice: Kokoro, run as one batch in its own venv."""

    def __init__(self, cfg: dict, work: Path):
        if not KOKORO_PY.exists():
            raise SystemExit("English voice not installed. Run ./setup.sh first.")
        self.voice, self.speed, self.work = cfg["english_voice"], float(cfg["english_speed"]), work
        self.files: dict[str, Path] = {}

    def batch(self, texts: list[str]) -> None:
        jobs = []
        for t in dict.fromkeys(texts):
            out = self.work / f"en_{hashlib.sha1(t.encode()).hexdigest()[:12]}.wav"
            self.files[t] = out
            jobs.append({"text": t, "out": str(out), "voice": self.voice, "speed": self.speed})
        jf = self.work / "en_jobs.json"
        jf.write_text(json.dumps(jobs))
        log(f"Synthesizing {len(jobs)} English clips with Kokoro...")
        r = subprocess.run([str(KOKORO_PY), str(ROOT / "english_tts.py"), str(jf)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("English TTS failed:\n" + r.stderr[-2000:])
        log("  " + r.stdout.strip().splitlines()[-1])


def normalize_peak(path: Path, target: float = 0.7) -> None:
    """Scale a 16-bit mono WAV so its peak sits at `target` (keeps voices level)."""
    import array
    with wave.open(str(path)) as w:
        params, frames = w.getparams(), w.readframes(w.getnframes())
    a = array.array("h", frames)
    peak = max((abs(x) for x in a), default=0)
    if peak:
        g = target * 32767 / peak
        a = array.array("h", (max(-32768, min(32767, int(x * g))) for x in a))
    with wave.open(str(path), "wb") as w:
        w.setparams(params); w.writeframes(a.tobytes())


class Assembler:
    def __init__(self, tts, work: Path, slow: float, en: EnglishTTS | None = None):
        self.tts, self.work, self.slow, self.en = tts, work, slow, en
        self.clips: list[Path] = []
        self._n = 0

    def _new(self, tag: str) -> Path:
        self._n += 1
        return self.work / f"{self._n:04d}_{tag}.wav"

    def raw(self, samples: bytes, tag: str) -> None:
        p = self._new(tag); write_wav(p, samples); self.clips.append(p)

    def pause(self, sec: float) -> None:
        self.raw(silence(sec), "sil")

    def chime(self, double=False) -> None:
        self.raw(chime() + (chime((1318.5, 880.0)) if double else b""), "chime")

    def say(self, text: str, speaker: str, speed: float) -> float:
        key = hashlib.sha1(f"{text}|{speaker}|{speed}".encode()).hexdigest()[:12]
        cache = self.work / f"tts_{key}.wav"
        if not cache.exists():
            cache.write_bytes(self.tts.synth(text, speaker, speed))
        return self._add_file(cache, "say")

    def _add_file(self, src: Path, tag: str) -> float:
        # normalize format through ffmpeg so every clip is identical PCM
        p = self._new(tag)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src),
                        "-ar", str(RATE), "-ac", "1", "-sample_fmt", "s16", str(p)], check=True)
        normalize_peak(p)
        self.clips.append(p)
        return wav_duration(p)

    def say_en(self, text: str, gap: float = 0.5) -> None:
        """English voice; any {日本語} inside is spoken by the Japanese voice in place."""
        if not self.en:
            return
        parts = split_mixed(text)
        for j, (kind, t) in enumerate(parts):
            if kind == "en":
                self._add_file(self.en.files[t], "en")
            else:
                self.say(t, "A", 0.85)
            if j < len(parts) - 1:
                self.pause(0.12)
        self.pause(gap)

    def teach(self, segments: list[dict]) -> None:
        for s in segments:
            if s["kind"] == "en":
                self.say_en(s["text"], 0.45)
            else:
                self.say(s["text"], s.get("speaker") or "A",
                         1.0 if s.get("speed") == "natural" else 0.85)
                self.pause(0.8)

    @property
    def total(self) -> float:
        return sum(wav_duration(c) for c in self.clips)

    def drill(self, text: str, speaker: str) -> None:
        """slow -> short gap -> normal -> repeat-after-me pause"""
        self.say(text, speaker, self.slow)
        self.pause(1.0)
        d = self.say(text, speaker, 1.0)
        self.pause(max(2.5, d * 1.4 + 1.0))

    def export(self, mp3: Path, title: str, album: str, track: int) -> None:
        listing = self.work / "concat.txt"
        listing.write_text("".join(f"file '{c.name}'\n" for c in self.clips))
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-af", "acompressor=threshold=-20dB:ratio=3:attack=5:release=200,loudnorm=I=-14:TP=-1.5:LRA=7",
                        "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-q:a", "4",
                        "-metadata", f"title={title}", "-metadata", f"album={album}",
                        "-metadata", f"track={track}", "-metadata", "genre=Podcast",
                        str(mp3)], check=True, cwd=self.work)


CUES = {
    "listen": "Part one. Listen to the whole conversation. Don't worry about catching every word.",
    "repeat": "Part two. Line by line. You'll hear what it means, then the Japanese slowly, then at normal speed. "
              "Repeat out loud in the pause.",
    "drill": "Part three. Pattern practice. I'll say a sentence in English. Try to say it in Japanese "
             "using today's pattern, then listen and repeat.",
    "real": "Real Japanese corner. Let's hear today's pattern the way it shows up in the real world.",
    "review": "Quick review from earlier episodes. Hear the English, say it in Japanese, then listen.",
    "shadow": "Bonus round. Shadowing. Say each line along with the speaker, at full speed.",
    "again": "Last part. Listen to the whole conversation one more time. Notice the pattern each time it appears.",
    "outro": "That's it for today. Great work. See you tomorrow.",
}


def pick_review(progress: dict, number: int, date: str, k: int = 4) -> list[dict]:
    """A few sentences from earlier episodes: mostly recent, one or two older."""
    import random
    rng = random.Random(date)
    earlier = [ep for ep in progress["episodes"] if ep["number"] < number]
    picks: list[dict] = []
    for i, ep in enumerate(reversed(earlier)):
        d = load_json(ROOT / ep["data_file"], None)
        if not d:
            continue
        pool = [{"jp": line_jp(x["words"]), "en": x["en"]} for x in d["lines"] + d["examples"]
                if len([w for w in x["words"] if w["role"] not in ("punct", "expression")]) >= 3]
        rng.shuffle(pool)
        picks += pool[: (2 if i == 0 else 1)]
        if len(picks) >= k:
            break
    return picks[:k]


def build_audio(data: dict, tts, cfg: dict, mp3: Path, number: int, keep_work: bool,
                review: list[dict] | None = None) -> None:
    work = Path(tempfile.mkdtemp(prefix="jpod_"))
    lines, nar = data["lines"], data.get("narration")
    real = (nar or {}).get("real_world")
    review = review or []
    max_s, min_s = float(cfg.get("max_minutes", 10)) * 60, float(cfg.get("min_minutes", 5)) * 60
    en = EnglishTTS(cfg, work)
    en.batch([t for s in (nar["segments"] if nar else []) if s["kind"] == "en"
              for k, t in split_mixed(s["text"]) if k == "en"]
             + [t for s in (real["segments"] if real else []) if s["kind"] == "en"
                for k, t in split_mixed(s["text"]) if k == "en"]
             + list(CUES.values())
             + [f"Episode {number}."]
             + [f"{SPEAKERS[ln['speaker']]['name']}: {ln['en']}" for ln in lines]
             + [ex["en"] for ex in data["examples"]] + [r["en"] for r in review])
    a = Assembler(tts, work, float(cfg["slow_speed"]), en)
    try:
        # Intro + English teacher segment
        a.chime(double=True)
        a.say_en(f"Episode {number}.", 0.3)
        a.say(data["title_jp"], "A", 1.0)
        a.pause(1.0)
        if nar:
            a.teach(nar["segments"])
            a.pause(0.8)
        # Part 1: listen to the whole dialogue
        a.chime(); a.say_en(CUES["listen"], 0.8)
        for ln in lines:
            a.say(line_jp(ln["words"]), ln["speaker"], 1.0)
            a.pause(0.7)
        # Part 2: meaning -> slow -> normal -> repeat
        a.pause(1.0); a.chime(double=True); a.say_en(CUES["repeat"], 0.8)
        for ln in lines:
            log(f"  TTS: {line_jp(ln['words'])}")
            a.say_en(f"{SPEAKERS[ln['speaker']]['name']}: {ln['en']}", 0.6)
            a.drill(line_jp(ln["words"]), ln["speaker"])
        # Part 3: English prompt -> try it -> slow -> normal -> repeat
        a.chime(double=True); a.say_en(CUES["drill"], 0.8)
        for i, ex in enumerate(data["examples"]):
            a.say_en(ex["en"], 0)
            a.pause(3.5)
            a.drill(line_jp(ex["words"]), "AB"[i % 2])
        # Real Japanese corner (news / announcement style)
        if real:
            a.chime(double=True); a.say_en(CUES["real"], 0.6)
            a.teach(real["segments"])
        # Review of earlier episodes
        if review:
            a.chime(); a.say_en(CUES["review"], 0.8)
            for i, r in enumerate(review):
                a.say_en(r["en"], 0); a.pause(3.5)
                d = a.say(r["jp"], "AB"[i % 2], 1.0)
                a.pause(max(2.5, d * 1.4 + 1.0))
        # Fit the 5-10 minute window: final listen costs ~dialogue length
        log(f"  running length {a.total / 60:.1f} min")
        if a.total < min_s:
            a.chime(); a.say_en(CUES["shadow"], 0.8)
            for ln in lines:
                d = a.say(line_jp(ln["words"]), ln["speaker"], 1.0)
                a.pause(max(2.0, d + 0.8))
        if a.total < max_s - 45:
            a.chime(); a.say_en(CUES["again"], 0.8)
            for ln in lines:
                a.say(line_jp(ln["words"]), ln["speaker"], 1.0)
                a.pause(0.7)
        a.pause(0.5); a.say_en(CUES["outro"], 0.3); a.say("またあした！", "A", 1.0)
        a.pause(0.5); a.chime(double=True)
        log("Joining clips with ffmpeg...")
        a.export(mp3, f"#{number} {data['title_jp']} — {data['title_en']}",
                 "Beginner Japanese Daily", number)
    finally:
        if keep_work:
            log(f"Work files kept in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- transcripts
ROLE_CLASS = {"particle": "p", "verb": "v", "i-adjective": "a", "na-adjective": "a",
              "copula": "c", "auxiliary": "c", "question-word": "q", "noun": "n",
              "pronoun": "n", "number": "n"}


def md_sentence(words):
    return "".join(furigana_text(w) for w in words)


def md_breakdown(words):
    rows = [w for w in words if w["role"] != "punct"]
    return ("| " + " | ".join(furigana_text(w) for w in rows) + " |\n"
            + "|" + "---|" * len(rows) + "\n"
            + "| " + " | ".join(word_romaji(w) for w in rows) + " |\n"
            + "| " + " | ".join(f"*{w['en']}*" for w in rows) + " |\n")


def render_markdown(data, meta) -> str:
    pat = meta["pattern"]
    out = [f"# Episode {meta['number']}: {data['title_jp']} — {data['title_en']}",
           f"*{meta['date']} · Topic: {meta['topic']} · Audio: `{meta['mp3']}`*", ""]
    out += ["## Today's pattern", ""]
    nar = data.get("narration")
    if nar:
        out += [f"### {nar['pattern_jp']}  ·  *{nar['pattern_romaji']}*  =  **{nar['pattern_english']}**", ""]
        out += [f"- **{s['slot']}**: {s['fits']}" for s in nar["slots"]] + [""]
    if pat:
        out += [f"**{pat['pattern']}** — {pat['name']}", "", f"Structure: `{pat['formula']}`", ""]
    if nar:
        out += ["### Building practical sentences", ""]
        for b in nar["builder"]:
            out += [f"{i}. {s['jp']}  *{s['romaji']}*  → {s['en']}" for i, s in enumerate(b["steps"], 1)] + [""]
        out += [f"> **Tip:** {nar['tip']}", ""]
        rw = nar.get("real_world")
        if rw:
            out += [f"### Real Japanese ({rw['style']})", "", f"**{rw['sentence_jp']}**  ", rw["sentence_en"], ""]
    ex = data["explanation"]
    out += [ex["summary"], ""] + [f"- {n}" for n in ex["structure_notes"]] + ["", f"*{ex['connects_to']}*", ""]
    out += ["### Swap-in examples", ""]
    for i, e in enumerate(data["examples"], 1):
        out += [f"{i}. {md_sentence(e['words'])}  ",
                f"   *{line_romaji(e['words'])}*  ", f"   {e['en']}  (swap → {e['swap']})", ""]
    out += ["## Dialogue", ""]
    for i, ln in enumerate(data["lines"], 1):
        sp = SPEAKERS[ln["speaker"]]
        out += [f"**{i}. {sp['name']}:** {md_sentence(ln['words'])}  ",
                f"*{line_romaji(ln['words'])}*  ", f"{ln['en']}  ", f"Literal: `{ln['literal']}`", "",
                md_breakdown(ln["words"])]
    if data["new_vocab"]:
        out += ["## New words", "", "| Word | Reading | Romaji | Meaning |", "|---|---|---|---|"]
        out += [f"| {v['jp']} | {v['kana']} | {kana_to_romaji(v['kana'])} | {v['en']} |" for v in data["new_vocab"]]
        out.append("")
    out += ["## Audio guide", "",
            "0. English teacher explains the pattern and builds sentences with it.",
            "1. The whole dialogue at normal speed — just listen.",
            "2. Each line: English meaning → slow → normal → pause: **repeat it out loud**.",
            "3. Pattern drill: hear the English, try saying it in Japanese, then slow → normal → repeat.",
            "4. The whole dialogue again at normal speed.", ""]
    if nar:
        out += ["## Teacher script", ""]
        out += [(INLINE_JP_RE.sub(r"**\1**", s["text"]) if s["kind"] == "en" else f"> 🔊 {s['text']}") + "  "
                for s in nar["segments"]]
        out.append("")
    return "\n".join(out)


def update_cheat_sheet(progress: dict) -> None:
    """patterns.md / patterns.html: every pattern learned so far, with its English equivalent."""
    rows = []
    for ep in progress["episodes"]:
        d = load_json(ROOT / ep["data_file"], None)
        if not d:
            continue
        n = d.get("narration") or {}
        first = d["examples"][0] if d["examples"] else None
        rows.append({
            "ep": ep["number"], "date": ep["date"],
            "jp": n.get("pattern_jp", ep["pattern"]), "romaji": n.get("pattern_romaji", ""),
            "en": n.get("pattern_english", ""),
            "slots": "; ".join(f"{s['slot']} = {s['fits']}" for s in n.get("slots", [])),
            "ex_jp": md_sentence(first["words"]) if first else "", "ex_en": first["en"] if first else "",
            "ex_ro": line_romaji(first["words"]) if first else "",
            "tip": n.get("tip", ""), "html": Path(ep["data_file"]).with_suffix(".html").as_posix(),
        })
    md = ["# Japanese sentence patterns I know", "",
          "| # | Pattern | Romaji | English | Example |", "|---|---|---|---|---|"]
    md += [f"| {r['ep']} | {r['jp']} | {r['romaji']} | **{r['en']}** | {r['ex_jp']} — {r['ex_en']} |" for r in rows]
    syllabus = load_json(SYLLABUS_FILE, [])
    done = {ep["pattern_id"] for ep in progress["episodes"]}
    md += ["", f"# Roadmap: {len(done)} of {len(syllabus)} patterns", ""]
    cur = None
    for i, sp in enumerate(syllabus, 1):
        lvl = sp.get("level", "")
        if lvl.split("+")[0] != cur:
            cur = lvl.split("+")[0]
            md += ["", f"## {cur}", ""]
        mark = "x" if sp["id"] in done else " "
        tag = f" *({lvl.split('+')[1]})*" if "+" in lvl else ""
        md.append(f"- [{mark}] {i}. {sp['pattern']}{tag} · {sp.get('romaji', '')} = {sp.get('english', sp['name'])}")
    PATTERNS_MD.write_text("\n".join(md) + "\n")
    e = html.escape
    cards = "".join(
        f"<div class='card'><div class='sp'>#{r['ep']} · {r['date']} · <a href='{e(r['html'])}'>episode</a></div>"
        f"<div class='natural'>{e(r['jp'])} <span class='meta'>{e(r['romaji'])}</span></div>"
        f"<div class='formula'>= {e(r['en'])}</div><p class='meta'>{e(r['slots'])}</p>"
        f"<div>{e(r['ex_jp'])}<br><span class='lit'>{e(r['ex_ro'])}</span><br>{e(r['ex_en'])}</div>"
        + (f"<p class='meta'>💡 {e(r['tip'])}</p>" if r["tip"] else "") + "</div>"
        for r in rows)
    PATTERNS_HTML.write_text(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>My Japanese Patterns</title><style>{CSS}</style></head><body>"
        f"<h1>Sentence patterns I know ({len(rows)})</h1>{cards}"
        f"<h1>Roadmap: {len(done)} of {len(syllabus)}</h1>{roadmap_html(syllabus, done)}</body></html>")


def roadmap_html(syllabus: list, done: set) -> str:
    e, out, cur = html.escape, [], None
    for i, sp in enumerate(syllabus, 1):
        base, _, reg = sp.get("level", "").partition("+")
        if base != cur:
            if cur:
                out.append("</table></details>")
            cur = base
            n = sum(1 for x in syllabus if x.get("level", "").startswith(base))
            k = sum(1 for x in syllabus if x.get("level", "").startswith(base) and x["id"] in done)
            out.append(f"<details{' open' if base == 'N5' else ''}><summary><b>{base}</b> — {k}/{n}</summary><table>")
        out.append(f"<tr style='opacity:{1 if sp['id'] in done else .65}'><td>{'✅' if sp['id'] in done else i}</td>"
                   f"<td>{e(sp['pattern'])}{' <i>(' + e(reg) + ')</i>' if reg else ''}</td>"
                   f"<td class='lit'>{e(sp.get('romaji', ''))}</td><td>{e(sp.get('english', sp['name']))}</td></tr>")
    return "".join(out) + "</table></details>"


def html_sentence(words, chips=True):
    if not chips:
        return "".join(furigana_html(w) for w in words)
    parts = []
    for w in words:
        if w["role"] == "punct":
            parts.append(f'<span class="punct">{html.escape(w["jp"])}</span>')
            continue
        cls = ROLE_CLASS.get(w["role"], "o")
        parts.append(f'<span class="w {cls}" title="{html.escape(w["role"])}">'
                     f'<span class="jp">{furigana_html(w)}</span>'
                     f'<span class="ro">{html.escape(word_romaji(w))}</span>'
                     f'<span class="gl">{html.escape(w["en"])}</span></span>')
    return "".join(parts)


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1f;--mut:#6b6b70;--card:#fff;--line:#e4e1da;
--p:#c2410c;--v:#1d4ed8;--a:#047857;--c:#7c3aed;--q:#be185d;--n:#1d1d1f}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececee;--mut:#9a9aa2;--card:#1d1d20;
--line:#2e2e33;--p:#fb923c;--v:#60a5fa;--a:#34d399;--c:#a78bfa;--q:#f472b6;--n:#ececee}}
body{background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,"Noto Sans JP",sans-serif;
max-width:860px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:1.5rem;margin-bottom:.2em}h2{margin-top:2em;border-bottom:1px solid var(--line);padding-bottom:.3em}
.meta{color:var(--mut)}audio{width:100%;margin:12px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0}
.sp{font-weight:600;color:var(--mut);font-size:.9rem}
.natural{font-size:1.45rem;margin:.2em 0}.natural rt{font-size:.55em;color:var(--mut)}
.en{margin:.2em 0}.lit{font-family:ui-monospace,monospace;font-size:.85rem;color:var(--mut)}
.chips{display:flex;flex-wrap:wrap;gap:6px 4px;margin-top:10px;align-items:flex-end}
.w{display:inline-flex;flex-direction:column;align-items:center;padding:2px 6px;border-bottom:3px solid var(--n);border-radius:4px}
.w .jp{font-size:1.15rem}.w rt{font-size:.6em;color:var(--mut)}
.w .ro{font-size:.75rem;color:var(--mut)}.w .gl{font-size:.72rem;color:var(--mut);max-width:9em;text-align:center}
.w.p{border-color:var(--p)}.w.p .jp{color:var(--p);font-weight:700}
.w.v{border-color:var(--v)}.w.a{border-color:var(--a)}.w.c{border-color:var(--c)}.w.q{border-color:var(--q)}
.w.o{border-color:var(--line)}.punct{font-size:1.15rem;align-self:center}
.legend span{margin-right:12px;font-size:.85rem}.legend b{display:inline-block;width:14px;height:4px;vertical-align:middle;margin-right:4px}
.formula{font-size:1.2rem;font-weight:600;padding:8px 12px;border-left:4px solid var(--p);background:var(--card)}
table{border-collapse:collapse}td,th{border:1px solid var(--line);padding:4px 10px;text-align:left}
details summary{cursor:pointer;color:var(--mut)}
"""


def render_html(data, meta) -> str:
    pat, ex = meta["pattern"], data["explanation"]
    e = html.escape
    h = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
         f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>Ep {meta['number']} {e(data['title_jp'])}</title><style>{CSS}</style></head><body>",
         f"<h1>Episode {meta['number']}: {e(data['title_jp'])}</h1>",
         f"<div class='meta'>{e(data['title_en'])} · {meta['date']} · Topic: {e(meta['topic'])}</div>",
         f"<audio controls src='{e(meta['mp3'])}'></audio>",
         "<h2>Today's pattern</h2>"]
    nar = data.get("narration")
    if nar:
        h += [f"<div class='natural'>{e(nar['pattern_jp'])} <span class='meta'>· {e(nar['pattern_romaji'])}</span></div>",
              f"<div class='formula'>= {e(nar['pattern_english'])}</div><ul>"]
        h += [f"<li><b>{e(s['slot'])}</b>: {e(s['fits'])}</li>" for s in nar["slots"]] + ["</ul>"]
        h.append("<h3>Building practical sentences</h3>")
        for b in nar["builder"]:
            h.append("<div class='card'>" + "".join(
                f"<div><span class='sp'>step {i}</span> <span class='natural' style='font-size:1.15rem'>{e(s['jp'])}</span> "
                f"<span class='lit'>{e(s['romaji'])}</span> → {e(s['en'])}</div>"
                for i, s in enumerate(b["steps"], 1)) + "</div>")
        h.append(f"<p>💡 <b>Tip:</b> {e(nar['tip'])}</p>")
        rw = nar.get("real_world")
        if rw:
            h.append(f"<h3>Real Japanese ({e(rw['style'])})</h3><div class='card'>"
                     f"<div class='natural'>{e(rw['sentence_jp'])}</div><div class='en'>{e(rw['sentence_en'])}</div></div>")
    if pat:
        h += [f"<p><b>{e(pat['pattern'])}</b> — {e(pat['name'])}</p>", f"<div class='formula'>{e(pat['formula'])}</div>"]
    h += [f"<p>{e(ex['summary'])}</p><ul>"] + [f"<li>{e(n)}</li>" for n in ex["structure_notes"]]
    h += [f"</ul><p class='meta'><i>{e(ex['connects_to'])}</i></p>", "<h3>Swap-in examples</h3>"]
    for x in data["examples"]:
        h.append(f"<div class='card'><div class='natural'>{html_sentence(x['words'], False)}</div>"
                 f"<div class='en'>{e(x['en'])} <span class='meta'>(swap → {e(x['swap'])})</span></div>"
                 f"<div class='chips'>{html_sentence(x['words'])}</div></div>")
    h += ["<h2>Dialogue</h2>",
          "<div class='legend'><span><b style='background:var(--p)'></b>particle</span>"
          "<span><b style='background:var(--v)'></b>verb</span><span><b style='background:var(--a)'></b>adjective</span>"
          "<span><b style='background:var(--c)'></b>です / copula</span><span><b style='background:var(--q)'></b>question word</span>"
          "<span><b style='background:var(--n)'></b>noun</span></div>"]
    for ln in data["lines"]:
        sp = SPEAKERS[ln["speaker"]]
        h.append(f"<div class='card'><div class='sp'>{e(sp['name'])} ({e(sp['jp'])})</div>"
                 f"<div class='natural'>{html_sentence(ln['words'], False)}</div>"
                 f"<div class='lit'>{e(line_romaji(ln['words']))}</div>"
                 f"<div class='en'>{e(ln['en'])}</div><div class='lit'>literal: {e(ln['literal'])}</div>"
                 f"<details open><summary>structure</summary><div class='chips'>{html_sentence(ln['words'])}</div></details></div>")
    if data["new_vocab"]:
        h += ["<h2>New words</h2><table><tr><th>Word</th><th>Reading</th><th>Romaji</th><th>Meaning</th></tr>"]
        h += [f"<tr><td>{e(v['jp'])}</td><td>{e(v['kana'])}</td><td>{e(kana_to_romaji(v['kana']))}</td><td>{e(v['en'])}</td></tr>"
              for v in data["new_vocab"]]
        h.append("</table>")
    if nar:
        h.append("<h2>Teacher script</h2><details><summary>show</summary>")
        h += [f"<p>{inline_html(s['text'])}</p>" if s["kind"] == "en" else f"<p class='natural' style='font-size:1.1rem'>🔊 {e(s['text'])}</p>"
              for s in nar["segments"]]
        h.append("</details>")
    h.append("</body></html>")
    return "\n".join(h)


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Generate today's beginner Japanese podcast episode.")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="episode date (YYYY-MM-DD)")
    ap.add_argument("--topic", help="override today's topic")
    ap.add_argument("--force", action="store_true", help="regenerate even if this date already exists")
    ap.add_argument("--text-only", action="store_true", help="skip audio (for testing)")
    ap.add_argument("--from-json", type=Path, help="reuse an existing episode JSON instead of calling Claude")
    ap.add_argument("--keep-work", action="store_true", help="keep intermediate WAV clips")
    ap.add_argument("--redo-narration", action="store_true", help="rewrite the English teacher segment")
    args = ap.parse_args()

    cfg = load_config()
    syllabus = load_json(SYLLABUS_FILE, [])
    topics = load_json(TOPICS_FILE, [])
    progress = load_progress()
    by_id = {p["id"]: p for p in syllabus}

    existing = next((ep for ep in progress["episodes"] if ep["date"] == args.date), None)
    if existing and not args.force:
        log(f"Episode for {args.date} already exists: {existing['mp3']}  (use --force to redo)")
        return
    if existing:  # regenerate same slot, same pattern
        progress["episodes"].remove(existing)
        rebuild_vocab(progress)

    number = existing["number"] if existing else len(progress["episodes"]) + 1
    pattern = by_id.get(existing["pattern_id"]) if existing else pick_pattern(progress, syllabus)
    done_ids = [ep["pattern_id"] for ep in progress["episodes"]]
    covered = [p for p in syllabus if p["id"] in done_ids]
    topic = args.topic or (existing["topic"] if existing else pick_topic(progress, topics))
    log(f"Episode {number} · {args.date} · pattern: {pattern['pattern'] if pattern else 'REVIEW'} · topic: {topic}")

    if args.from_json:
        data = load_json(args.from_json, None)
    else:
        data = write_episode(pattern, covered, topic, progress, number, cfg)
    if args.redo_narration or "real_world" not in data.get("narration", {}):
        data["narration"] = write_narration(data, pattern, covered, cfg)

    out_dir = EPISODES_DIR / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.date}_ep{number:03d}"
    mp3 = out_dir / f"{stem}.mp3"
    data_file = out_dir / f"{stem}.json"
    save_json(data_file, data)

    if not args.text_only:
        tts = GoogleTTS(cfg) if cfg["tts"] == "google" else VoicevoxTTS(cfg)
        tts.start()
        try:
            log("Synthesizing audio...")
            build_audio(data, tts, cfg, mp3, number, args.keep_work,
                        review=pick_review(progress, number, args.date))
        finally:
            tts.stop()

    meta = {"number": number, "date": args.date, "topic": topic, "pattern": pattern, "mp3": mp3.name}
    (out_dir / f"{stem}.md").write_text(render_markdown(data, meta))
    (out_dir / f"{stem}.html").write_text(render_html(data, meta))

    if args.text_only:
        log(f"Text-only run: {out_dir} (progress not updated)")
        return
    progress["episodes"].append({
        "number": number, "date": args.date, "topic": topic,
        "pattern_id": pattern["id"] if pattern else "review",
        "pattern": pattern["pattern"] if pattern else "review",
        "title": f"{data['title_jp']} — {data['title_en']}",
        "mp3": str(mp3.relative_to(ROOT)), "data_file": str(data_file.relative_to(ROOT)),
    })
    progress["episodes"].sort(key=lambda ep: ep["number"])
    rebuild_vocab(progress)
    progress["patterns_covered"] = [ep["pattern"] for ep in progress["episodes"]]
    save_json(PROGRESS_FILE, progress)
    update_cheat_sheet(progress)
    log(f"Done! {mp3}  ({wav_like_duration(mp3)})")
    log(f"Transcript: {out_dir / (stem + '.html')}")


def wav_like_duration(mp3: Path) -> str:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(mp3)], capture_output=True, text=True)
        s = float(r.stdout.strip())
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    except Exception:
        return "?"


if __name__ == "__main__":
    main()
