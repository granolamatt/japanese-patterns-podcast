#!/usr/bin/env python3
"""Publish episodes as a podcast feed on GitHub Pages (for Pocket Casts etc.).

Builds ./site (feed.xml, cover.png, audio/, transcripts/, index.html) from progress.json,
then force-pushes it as a single commit (no history, so the repo never grows past
`keep_episodes` episodes).

config.env:
  publish_repo=granolamatt/japanese-patterns-daily
  keep_episodes=60
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import math
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import podcast as P  # noqa: E402

SITE = P.ROOT / "site"
SHOW_TITLE = "Japanese Patterns Daily"
SHOW_DESC = ("One Japanese sentence pattern a day, explained in English as a formula "
             "(A wa B desu = A is B), built up piece by piece, then used in a short dialogue "
             "with slow / normal / repeat drills and a real-Japanese news corner. "
             "Works entirely by ear. Voices: VOICEVOX:冥鳴ひまり, VOICEVOX:玄野武宏, Kokoro.")
COMMIT_TRAILER = ("\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
                  "Claude-Session: https://claude.ai/code/session_01KJMeKZSBszfH9eZDJtSnKE\n")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout


# ---------------------------------------------------------------- cover art (pure python PNG)
def make_cover(path: Path, size: int = 1400) -> None:
    bg, ring, sun, bar = (27, 31, 59), (246, 241, 231), (232, 93, 63), (246, 241, 231)
    cx = cy = size / 2
    r_outer, r_inner = size * 0.36, size * 0.33
    bars = [0.25, 0.45, 0.7, 0.95, 0.7, 1.0, 0.6, 0.85, 0.5, 0.3, 0.2]
    bw, gap = size * 0.028, size * 0.018
    total_w = len(bars) * bw + (len(bars) - 1) * gap
    x0 = cx - total_w / 2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            d = math.hypot(x - cx, y - cy)
            c = bg
            if d <= r_inner:
                c = sun
                k = int((x - x0) // (bw + gap))
                if 0 <= k < len(bars) and (x - x0) - k * (bw + gap) < bw:
                    h = bars[k] * r_inner * 0.9
                    if abs(y - cy) <= h / 2:
                        c = bar
            elif d <= r_outer:
                c = ring
            row += bytes(c)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
    path.write_bytes(png)


# ---------------------------------------------------------------- feed
def duration_s(mp3: Path) -> int:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)])
    return int(float(out.strip()))


def episode_desc(ep: dict, data: dict) -> str:
    n = data.get("narration") or {}
    parts = []
    if n.get("pattern_jp"):
        parts.append(f"Pattern: {n['pattern_jp']} ({n.get('pattern_romaji', '')}) = {n.get('pattern_english', '')}")
    else:
        parts.append(f"Pattern: {ep['pattern']}")
    parts.append(f"Topic: {ep['topic']}")
    if n.get("tip"):
        parts.append(f"Tip: {n['tip']}")
    rw = n.get("real_world")
    if rw:
        parts.append(f"Real Japanese ({rw['style']}): {rw['sentence_jp']} = {rw['sentence_en']}")
    if data.get("new_vocab"):
        parts.append("New words: " + ", ".join(f"{v['jp']} ({v['en']})" for v in data["new_vocab"]))
    return "\n".join(parts)


def build_site(base: str, keep: int) -> list[dict]:
    progress = P.load_progress()
    eps = [ep for ep in progress["episodes"] if (P.ROOT / ep["mp3"]).exists()][-keep:]
    if SITE.exists():
        for child in SITE.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    (SITE / "audio").mkdir(parents=True, exist_ok=True)
    (SITE / "transcripts").mkdir(exist_ok=True)
    (SITE / ".nojekyll").write_text("")
    cover_src = P.ROOT / "cover.png"
    if not cover_src.exists():
        make_cover(cover_src)
    shutil.copy(cover_src, SITE / "cover.png")
    if P.PATTERNS_HTML.exists():
        shutil.copy(P.PATTERNS_HTML, SITE / "patterns.html")

    e = html.escape
    items, rows = [], []
    for ep in reversed(eps):
        mp3 = P.ROOT / ep["mp3"]
        data = P.load_json(P.ROOT / ep["data_file"], {})
        shutil.copy(mp3, SITE / "audio" / mp3.name)
        tr = mp3.with_suffix(".html")
        if tr.exists():
            # transcript's <audio> points at a sibling file; rewrite to the audio folder
            (SITE / "transcripts" / tr.name).write_text(
                tr.read_text().replace(f"src='{mp3.name}'", f"src='../audio/{mp3.name}'"))
        date = dt.datetime.fromisoformat(ep["date"]).replace(hour=6, tzinfo=dt.timezone.utc)
        url = f"{base}/audio/{mp3.name}"
        title = f"#{ep['number']} {ep['title']}"
        desc = episode_desc(ep, data)
        items.append(f"""    <item>
      <title>{e(title)}</title>
      <description>{e(desc)}</description>
      <itunes:summary>{e(desc)}</itunes:summary>
      <link>{base}/transcripts/{tr.name}</link>
      <enclosure url="{url}" length="{mp3.stat().st_size}" type="audio/mpeg"/>
      <guid isPermaLink="false">jpd-{ep['date']}-{ep['number']}</guid>
      <pubDate>{email.utils.format_datetime(date)}</pubDate>
      <itunes:duration>{duration_s(mp3)}</itunes:duration>
      <itunes:episode>{ep['number']}</itunes:episode>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
        rows.append(f"<li><a href='transcripts/{e(tr.name)}'>{e(title)}</a> · {ep['date']}</li>")

    now = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))
    (SITE / "feed.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{SHOW_TITLE}</title>
    <link>{base}/</link>
    <atom:link href="{base}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>{e(SHOW_DESC)}</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>{SHOW_TITLE}</itunes:author>
    <itunes:summary>{e(SHOW_DESC)}</itunes:summary>
    <itunes:image href="{base}/cover.png"/>
    <image><url>{base}/cover.png</url><title>{SHOW_TITLE}</title><link>{base}/</link></image>
    <itunes:category text="Education"><itunes:category text="Language Learning"/></itunes:category>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>
{chr(10).join(items)}
  </channel>
</rss>
""")
    (SITE / "index.html").write_text(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{SHOW_TITLE}</title><style>{P.CSS}</style></head><body>"
        f"<h1>{SHOW_TITLE}</h1><p>{e(SHOW_DESC)}</p>"
        f"<p><b>Podcast feed:</b> <code>{base}/feed.xml</code> — in Pocket Casts: Search → paste this URL.</p>"
        f"<p><a href='patterns.html'>Pattern roadmap</a></p><ul>{''.join(rows)}</ul></body></html>")
    return eps


def push(repo: str, eps: list[dict], cfg: dict) -> None:
    git = ["git", "-C", str(SITE)]
    if not (SITE / ".git").exists():
        run(git + ["init", "-q", "-b", "main"])
        run(git + ["remote", "add", "origin", f"git@github.com:{repo}.git"])
    # public repo: commit under the GitHub noreply identity, never the personal global one
    name, email = cfg.get("git_author_name"), cfg.get("git_author_email")
    if not (name and email):
        raise SystemExit("publish: set git_author_name / git_author_email in config.env "
                         "(use your GitHub noreply address) so no personal email gets published")
    run(git + ["config", "user.name", name])
    run(git + ["config", "user.email", email])
    # single orphan commit each time: old audio drops out of the repo entirely
    run(git + ["checkout", "-q", "--orphan", "publish-tmp"])
    run(git + ["add", "-A"])
    latest = eps[-1]["number"] if eps else 0
    run(git + ["commit", "-q", "-m", f"Publish feed through episode {latest}" + COMMIT_TRAILER])
    subprocess.run(git + ["branch", "-q", "-D", "main"], capture_output=True)
    run(git + ["branch", "-q", "-m", "main"])
    run(git + ["push", "-q", "-f", "origin", "main"])


def main() -> None:
    cfg = P.load_config()
    repo = cfg.get("publish_repo", "")
    if not repo:
        print("publish: no publish_repo in config.env; skipping")
        return
    owner, name = repo.split("/")
    base = cfg.get("feed_base_url") or f"https://{owner.lower()}.github.io/{name}"
    eps = build_site(base.rstrip("/"), int(cfg.get("keep_episodes", 60)))
    push(repo, eps, cfg)
    print(f"publish: {len(eps)} episodes → {base}/feed.xml")


if __name__ == "__main__":
    main()
