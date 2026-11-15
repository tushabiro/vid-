#!/usr/bin/env python3
"""Daily science-based lifting Short.

topic -> script -> AI voice -> captions -> vertical video -> (optional) YouTube upload
Everything runs on free tools. Output goes in the output/ folder.
"""
import asyncio
import datetime
import json
import os
import pathlib
import random
import subprocess
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
VOICE = os.getenv("VOICE") or "en-US-AndrewNeural"  # Hindi example: hi-IN-MadhurNeural
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
W, H = 1080, 1920


# ---------- topic and script ----------
def pick_topic():
    topics = json.loads((ROOT / "topics.json").read_text(encoding="utf-8"))
    forced = os.getenv("TOPIC_INDEX", "").strip()
    i = int(forced) if forced else datetime.date.today().toordinal()
    return topics[i % len(topics)]


def fallback_script(t):
    return " ".join([t["hook"]] + t["points"] + ["Follow for more science based lifting."])


def llm_script(t):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    prompt = (
        "Write a spoken script for a 40 to 55 second vertical video about: " + t["title"] + "\n"
        "Use ONLY these facts. Do not invent studies, numbers or claims:\n- "
        + "\n- ".join([t["hook"]] + t["points"])
        + "\nRules: open with a strong one-sentence hook, short punchy sentences, "
        "friendly and clear, 100 to 140 words, no emojis, no hashtags, no stage directions, "
        "no medical advice. End with a short call to follow. Return only the script text."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    try:
        r = requests.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text if 40 <= len(text.split()) <= 220 else None
    except Exception as e:  # fall back to the plain script
        print("LLM script failed, using fallback:", e)
        return None


# ---------- voice and captions ----------
async def synth(text, mp3_path):
    import edge_tts

    try:
        comm = edge_tts.Communicate(text, VOICE, boundary="WordBoundary")
    except TypeError:
        comm = edge_tts.Communicate(text, VOICE)
    words = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                end = (chunk["offset"] + chunk["duration"]) / 1e7
                words.append((start, end, chunk["text"]))
    return words


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def ts(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def build_srt(words, text, duration, srt_path, per_cue=3):
    if not words:  # no timings from the voice engine: spread words evenly
        toks = text.split()
        step = duration / max(len(toks), 1)
        words = [(i * step, (i + 1) * step, w) for i, w in enumerate(toks)]
    cues = []
    for i in range(0, len(words), per_cue):
        group = words[i:i + per_cue]
        cues.append([group[0][0], group[-1][1], " ".join(w[2] for w in group)])
    for k in range(len(cues) - 1):  # no gaps between captions
        cues[k][1] = cues[k + 1][0]
    cues[-1][1] = min(cues[-1][1] + 0.3, duration)
    lines = []
    for n, (a, b, txt) in enumerate(cues, 1):
        lines += [str(n), f"{ts(a)} --> {ts(b)}", txt.upper(), ""]
    pathlib.Path(srt_path).write_text("\n".join(lines), encoding="utf-8")


# ---------- background footage (free Pexels API) ----------
def get_background(query):
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={"query": query, "orientation": "portrait", "per_page": 20},
            timeout=30,
        )
        r.raise_for_status()
        vids = r.json().get("videos", [])
        random.shuffle(vids)
        for v in vids:
            files = [
                f for f in v.get("video_files", [])
                if f.get("file_type") == "video/mp4" and f.get("height") and f.get("width")
                and f["height"] >= 1280 and f["width"] < f["height"]
            ]
            if not files:
                continue
            best = min(files, key=lambda f: f["height"])
            path = OUT / "bg.mp4"
            with requests.get(best["link"], stream=True, timeout=120) as dl:
                dl.raise_for_status()
                with open(path, "wb") as fh:
                    for part in dl.iter_content(1 << 20):
                        fh.write(part)
            return path
    except Exception as e:
        print("Pexels failed, using plain background:", e)
    return None


# ---------- render ----------
def render(audio_name, srt_name, bg_name, duration, out_name):
    style = ("FontName=DejaVu Sans,Fontsize=12,Bold=1,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=70")
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
          f"eq=brightness=-0.08,subtitles={srt_name}:force_style='{style}'")
    if bg_name:
        inputs = ["-stream_loop", "-1", "-i", bg_name]
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=0x0f172a:s={W}x{H}:r=30"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-i", audio_name,
           "-t", f"{duration + 0.5:.2f}", "-vf", vf, "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_name]
    subprocess.run(cmd, check=True, cwd=OUT)  # run inside output/ so file names stay simple


# ---------- main ----------
def main():
    OUT.mkdir(exist_ok=True)
    topic = pick_topic()
    print("Topic:", topic["title"])

    script = llm_script(topic) or fallback_script(topic)
    (OUT / "script.txt").write_text(script, encoding="utf-8")

    words = asyncio.run(synth(script, OUT / "voice.mp3"))
    duration = probe_duration(OUT / "voice.mp3")
    build_srt(words, script, duration, OUT / "captions.srt")

    bg = get_background(topic.get("bg_query", "gym weightlifting"))
    render("voice.mp3", "captions.srt", bg.name if bg else None, duration, "video.mp4")

    description = (
        topic["title"] + "\n\n"
        "Sources:\n" + "\n".join(topic["sources"]) + "\n\n"
        "Educational content only, not medical advice. "
        "Voice generated with AI. Stock footage from Pexels.\n\n#Shorts #"
        + " #".join(t.replace(" ", "") for t in topic["tags"])
    )
    meta = {"title": topic["title"], "description": description, "tags": topic["tags"]}
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("Video ready:", OUT / "video.mp4")

    if os.getenv("UPLOAD_TO_YOUTUBE", "").lower() == "true":
        sys.path.insert(0, str(pathlib.Path(__file__).parent))
        from youtube_upload import upload
        upload(OUT / "video.mp4", topic["title"] + " #Shorts", description, topic["tags"])
    else:
        print("Upload skipped (UPLOAD_TO_YOUTUBE is not 'true'). Review the video artifact first.")


if __name__ == "__main__":
    main()

File 2: name it .github/workflows/daily.yml

yaml
name: daily-lifting-short

on:
  schedule:
    - cron: "30 3 * * *"   # every day at 03:30 UTC (9:00 AM India time)
  workflow_dispatch:        # lets you press "Run workflow" by hand

jobs:
  make-video:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install ffmpeg and fonts
        run: sudo apt-get update && sudo apt-get install -y ffmpeg fonts-dejavu-core

      - name: Install Python packages
        run: pip install -r requirements.txt

      - name: Make the video
        run: python src/pipeline.py
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          YT_CLIENT_ID: ${{ secrets.YT_CLIENT_ID }}
          YT_CLIENT_SECRET: ${{ secrets.YT_CLIENT_SECRET }}
          YT_REFRESH_TOKEN: ${{ secrets.YT_REFRESH_TOKEN }}
          UPLOAD_TO_YOUTUBE: ${{ vars.UPLOAD_TO_YOUTUBE }}
          VOICE: ${{ vars.VOICE }}

      - name: Keep the video so you can download and review it
        uses: actions/upload-artifact@v4
        with:
          name: video
          path: output/
          retention-days: 7
