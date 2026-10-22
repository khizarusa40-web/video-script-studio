#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Script Studio - Core Engine
Handles:
- Video & Audio downloading in multi-quality (4K, 2K, 1080p, 720p, 480p, Audio MP3)
- Subtitle & Transcript extraction via Groq Whisper Large v3 (100% accurate)
- Multi-language translation (Chinese, English, Urdu, Hindi, etc.)
- Multi-platform detection (YouTube, TikTok, Instagram, Facebook, RedNote / Xiaohongshu, etc.)
- Local Network IP & QR code generation for Mobile access
"""

import os
import sys
import re
import json
import time
import socket
import tempfile
import shutil
import base64
import io
from pathlib import Path
import requests

import yt_dlp

try:
    import imageio_ffmpeg
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_EXE = None

try:
    import qrcode
    import qrcode.image.svg
    HAVE_QR = True
except Exception:
    HAVE_QR = False

APP_NAME = "Video Script Studio"
VERSION = "2.0"
DEFAULT_PORT = int(os.environ.get("PORT", 8741))

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"

ACCEPTED_AUDIO_EXTS = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg"}
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB Groq limit

LANGUAGES = [
    ("English", "en"),
    ("Urdu", "ur"),
    ("Hindi", "hi"),
    ("Chinese (Simplified)", "zh-CN"),
    ("Chinese (Traditional)", "zh-TW"),
    ("Arabic", "ar"),
    ("Spanish", "es"),
    ("French", "fr"),
    ("German", "de"),
    ("Russian", "ru"),
    ("Portuguese", "pt"),
    ("Turkish", "tr"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Vietnamese", "vi"),
    ("Indonesian", "id"),
    ("Bengali", "bn"),
    ("Punjabi", "pa"),
    ("Italian", "it"),
    ("Dutch", "nl"),
]
LANG_NAMES = [name for name, _ in LANGUAGES]
LANG_CODES = {name: code for name, code in LANGUAGES}


# ---------------------------------------------------------------- Network ---

def get_local_ip():
    """Find the local LAN / Wi-Fi IP of this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def generate_qr_base64(url):
    """Generate QR code as base64 PNG or SVG data URL."""
    if not HAVE_QR:
        return ""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=7,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        try:
            img = qr.make_image(fill_color="#8b5cf6", back_color="#0f0f13")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except Exception:
            img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
            buf = io.BytesIO()
            img.save(buf)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/svg+xml;base64,{b64}"
    except Exception:
        return ""


# ---------------------------------------------------------------- Config ---

def config_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA", str(Path.home()))
    else:
        base = str(Path.home() / ".config")
    d = os.path.join(base, "VideoScriptStudio")
    os.makedirs(d, exist_ok=True)
    return d


def load_config():
    p = os.path.join(config_dir(), "config.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # Check old tool config if present
        old_p = os.path.join(os.environ.get("APPDATA", ""), "NRTranscriptStudio", "config.json")
        if os.path.exists(old_p):
            try:
                with open(old_p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}


def save_config(cfg):
    p = os.path.join(config_dir(), "config.json")
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ------------------------------------------------------------- URL & Platform ---

def extract_url(text):
    """
    Extract first valid URL from user input.
    Essential for RedNote / TikTok / Douyin where copied share text includes Chinese/emojis.
    """
    if not text:
        return ""
    text = text.strip()
    match = re.search(r"https?://[^\s<>\"'{}|\\^`\[\]]+", text)
    if match:
        return match.group(0).rstrip(".,;!?:)]")
    return text


def detect_platform(url):
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    if "tiktok.com" in u:
        return "TikTok"
    if "instagram.com" in u:
        return "Instagram"
    if "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "Facebook"
    if "xhslink.com" in u or "xiaohongshu.com" in u or "rednote" in u:
        return "RedNote (小红书)"
    if "douyin.com" in u:
        return "Douyin (抖音)"
    if "bilibili.com" in u or "b23.tv" in u:
        return "Bilibili"
    if "twitter.com" in u or "x.com" in u:
        return "X (Twitter)"
    if "threads.net" in u:
        return "Threads"
    if "pinterest.com" in u:
        return "Pinterest"
    return "Universal Video"


def valid_url(url):
    clean = extract_url(url)
    return bool(re.match(r"^https?://\S+\.\S+", clean))


# ----------------------------------------------------------- Video Info & Download ---

def get_base_ydl_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }
    if FFMPEG_EXE and os.path.exists(FFMPEG_EXE):
        opts["ffmpeg_location"] = FFMPEG_EXE
    return opts


def fetch_video_info(url):
    """
    Fetch metadata: title, thumbnail, duration, author, platform,
    and available quality options (4K, 2K, 1080p, 720p, 480p, audio).
    """
    clean = extract_url(url)
    opts = get_base_ydl_opts()
    opts["noplaylist"] = True

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(clean, download=False)
    except Exception as e:
        msg = str(e)
        if "sign in" in msg.lower() or "bot" in msg.lower():
            raise RuntimeError("Platform ne temporary bot-check lagaya hai. Thodi der baad dobara try karein.")
        if "private" in msg.lower():
            raise RuntimeError("Ye video private ya login-protected hai. Public video ka link dein.")
        raise RuntimeError(f"Video info fetch nahi ho saki: {msg}")

    # Detect max height available
    formats = info.get("formats") or []
    max_height = 0
    has_audio = False
    for f in formats:
        h = f.get("height") or 0
        if isinstance(h, int) and h > max_height:
            max_height = h
        if f.get("vcodec") == "none" and f.get("acodec") != "none":
            has_audio = True

    # Determine which qualities to show
    qualities = []
    if max_height >= 2160:
        qualities.append({"id": "4k", "label": "4K (2160p)", "height": 2160, "badge": "Ultra HD"})
    if max_height >= 1440:
        qualities.append({"id": "2k", "label": "2K (1440p)", "height": 1440, "badge": "Quad HD"})
    if max_height >= 1080:
        qualities.append({"id": "1080p", "label": "1080p", "height": 1080, "badge": "Full HD"})
    if max_height >= 720:
        qualities.append({"id": "720p", "label": "720p", "height": 720, "badge": "HD"})
    if max_height >= 480 or not qualities:
        qualities.append({"id": "480p", "label": "480p", "height": 480, "badge": "SD"})

    # Always provide Audio option
    qualities.append({"id": "audio", "label": "Audio Only (MP3)", "height": 0, "badge": "MP3"})

    duration_sec = info.get("duration") or 0
    mins = int(duration_sec // 60)
    secs = int(duration_sec % 60)
    dur_str = f"{mins}:{secs:02d}" if duration_sec else "Live / Unknown"

    return {
        "title": info.get("title") or "Unknown Video",
        "thumbnail": info.get("thumbnail") or "",
        "duration": dur_str,
        "duration_seconds": duration_sec,
        "author": info.get("uploader") or info.get("channel") or info.get("extractor") or "",
        "platform": detect_platform(clean),
        "url": clean,
        "max_resolution": f"{max_height}p" if max_height else "Standard",
        "qualities": qualities,
    }


def download_media(url, quality, output_dir, progress_cb=None):
    """
    Download video or audio in selected quality.
    Returns path of downloaded file and title.
    """
    clean = extract_url(url)
    os.makedirs(output_dir, exist_ok=True)
    downloaded = {}

    def hook(d):
        if d.get("status") == "downloading" and progress_cb:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            pct = (done / total) if total else 0
            speed_mb = (speed / (1024 * 1024)) if speed else 0
            progress_cb(min(0.99, pct), f"Downloading: {int(pct*100)}% ({speed_mb:.1f} MB/s, ETA {eta}s)")
        elif d.get("status") == "finished":
            downloaded["path"] = d.get("filename")
            if progress_cb:
                progress_cb(0.99, "Processing / Finalizing file...")

    opts = get_base_ydl_opts()
    opts["noplaylist"] = True
    opts["progress_hooks"] = [hook]

    if quality == "audio":
        opts["format"] = "bestaudio/best"
        opts["outtmpl"] = os.path.join(output_dir, "%(title).80s.%(ext)s")
        if FFMPEG_EXE:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
    else:
        h_map = {"4k": 2160, "2k": 1440, "1080p": 1080, "720p": 720, "480p": 480}
        h = h_map.get(quality, 1080)
        # Format selector: best video up to height + best audio, or fallback to single best
        opts["format"] = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}]+bestaudio/"
            f"best[height<={h}][ext=mp4]/"
            f"best[height<={h}]/best"
        )
        opts["outtmpl"] = os.path.join(output_dir, f"%(title).80s [{quality}].%(ext)s")
        if FFMPEG_EXE:
            opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(clean, download=True)

    path = downloaded.get("path")
    if not path or not os.path.exists(path):
        # find matching file
        cands = [os.path.join(output_dir, f) for f in os.listdir(output_dir)]
        cands = [c for c in cands if os.path.isfile(c) and not c.endswith(".part")]
        if not cands:
            raise RuntimeError("File download nahi ho saki.")
        path = max(cands, key=os.path.getmtime)

    title = (info or {}).get("title") or os.path.basename(path)
    return path, title


# ----------------------------------------------------------- Audio Extraction & Groq Whisper ---

def download_audio_for_transcription(url, tmpdir, progress_cb=None):
    """Download audio-only lightweight file for Groq Whisper transcription."""
    clean = extract_url(url)
    downloaded = {}

    def hook(d):
        if d.get("status") == "downloading" and progress_cb:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                progress_cb(min(0.99, done / total))
        if d.get("status") == "finished":
            downloaded["path"] = d.get("filename")

    opts = get_base_ydl_opts()
    opts.update({
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": os.path.join(tmpdir, "%(title).60s.%(ext)s"),
        "progress_hooks": [hook],
        "writesubtitles": False,
        "writeautomaticsub": False,
        "writethumbnail": False,
        "writeinfojson": False,
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(clean, download=True)

    path = downloaded.get("path")
    if not path or not os.path.exists(path):
        cands = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
        cands = [c for c in cands if os.path.isfile(c)]
        if not cands:
            raise RuntimeError("Audio download nahi ho saka.")
        path = max(cands, key=os.path.getsize)

    size = os.path.getsize(path)
    if size > MAX_AUDIO_BYTES:
        raise RuntimeError("Audio 25 MB se bara hai (Groq API limit). Chhoti video ka link dein.")
    if size < 1024:
        raise RuntimeError("Audio file khaali mili.")

    title = (info or {}).get("title") or ""
    return path, title


def test_groq_key(api_key):
    try:
        r = requests.get(
            GROQ_MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


def transcribe_audio_groq(path, api_key, progress_cb=None):
    """
    Transcribe audio using Groq Whisper Large v3.
    Returns (text, segments, detected_language).
    """
    if progress_cb:
        progress_cb("Groq Whisper AI ko bheja ja raha hai (High Accuracy)...")

    fname = os.path.basename(path)
    with open(path, "rb") as f:
        files = {"file": (fname, f, "application/octet-stream")}
        data = {
            "model": "whisper-large-v3",
            "response_format": "verbose_json",
            "temperature": "0",
        }
        r = requests.post(
            GROQ_TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
            timeout=600,
        )

    if r.status_code == 401:
        raise RuntimeError("Groq API key ghalat hai. Key dobara check karein.")
    if r.status_code == 413:
        raise RuntimeError("Audio file bohat bari hai (limit 25 MB).")
    if r.status_code == 429:
        raise RuntimeError("Groq free rate limit hit ho gayi. Thodi der baad try karein.")
    if r.status_code != 200:
        raise RuntimeError(f"Groq API error ({r.status_code}): {r.text[:200]}")

    j = r.json()
    text = (j.get("text") or "").strip()
    segments = j.get("segments") or []
    lang = j.get("language") or ""

    if not text:
        raise RuntimeError("Is audio mein koi awaz/bol nahi mila (ya awaz bohat dheemi hai).")

    return text, segments, lang


# ---------------------------------------------------------------- Translation ---

def split_chunks(text, limit=3500):
    parts = re.split(r"(?<=[.!?\n。！？])\s*", text)
    chunks, cur = [], ""
    for p in parts:
        if not p:
            continue
        if len(cur) + len(p) + 1 <= limit:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                chunks.append(cur)
            while len(p) > limit:
                chunks.append(p[:limit])
                p = p[limit:]
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or [text]


def translate_google(text, target_lang):
    chunks = split_chunks(text, 3500)
    out = []
    for c in chunks:
        params = {"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t", "q": c}
        r = requests.get(
            TRANSLATE_URL,
            params=params,
            timeout=45,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        j = r.json()
        out.append("".join(seg[0] for seg in j[0] if seg and seg[0]))
    return "".join(out).strip()


def translate_groq_llm(text, target_name, api_key):
    """
    Use Groq's high-speed LLaMA 3.3 for professional contextual translation
    especially great for Chinese idioms and video transcripts!
    """
    chunks = split_chunks(text, 4000)
    results = []
    for c in chunks:
        prompt = (
            f"You are a professional video transcript translator. "
            f"Translate the following transcript accurately and naturally into {target_name}. "
            f"Maintain speaker meaning, natural spoken flow, and do not add any conversational commentary, explanations, or notes. "
            f"Return ONLY the direct translation:\n\n{c}"
        )
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        r = requests.post(
            GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"].strip()
            results.append(content)
        else:
            # Fallback to Google if LLaMA model is rate limited
            return translate_google(text, LANG_CODES.get(target_name, "en"))
    return "\n\n".join(results)


def translate_text(text, target_name, api_key=None, source_lang="auto"):
    """
    Smart translation:
    1. If Groq API Key available -> uses Groq LLaMA 3.3 for best contextual quality.
    2. Fallback to Google Translate.
    """
    code = LANG_CODES.get(target_name, "en")
    if api_key:
        try:
            return translate_groq_llm(text, target_name, api_key)
        except Exception:
            pass
    try:
        return translate_google(text, code)
    except Exception as e:
        raise RuntimeError(f"Translation failed: {e}")


# ------------------------------------------------------------- Timestamps & SRT ---

def fmt_ts(sec):
    sec = max(0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments):
    lines = []
    for i, seg in enumerate(segments, 1):
        try:
            start = fmt_ts(seg.get("start", 0))
            end = fmt_ts(seg.get("end", 0))
            txt = (seg.get("text") or "").strip()
            if txt:
                lines.append(f"{i}\n{start} --> {end}\n{txt}\n")
        except Exception:
            continue
    return "\n".join(lines)


def segments_to_text(segments):
    lines = []
    for seg in segments:
        try:
            start = fmt_ts(seg.get("start", 0))[:8]
            txt = (seg.get("text") or "").strip()
            if txt:
                lines.append(f"[{start}] {txt}")
        except Exception:
            continue
    return "\n".join(lines)
