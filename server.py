#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Script Studio - Web & Mobile Server
Listens on 0.0.0.0:8741 for laptop and mobile access over LAN/Wi-Fi.
"""

import os
import sys
import json
import time
import uuid
import shutil
import tempfile
import threading
import mimetypes
import webbrowser
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Force UTF-8 on Windows terminal so emojis & Urdu/Chinese don't trigger charmap errors
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from core import (
    APP_NAME, VERSION, DEFAULT_PORT, LANGUAGES, LANG_NAMES, LANG_CODES,
    get_local_ip, generate_qr_base64, load_config, save_config,
    extract_url, detect_platform, valid_url, fetch_video_info,
    download_media, download_audio_for_transcription, test_groq_key,
    transcribe_audio_groq, translate_text, segments_to_srt, segments_to_text
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

jobs = {}
jobs_lock = threading.Lock()


def update_job(job_id, **kw):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kw)


# ------------------------------------------------------------- Workers ---

def worker_transcribe(job_id, url, key, target_lang):
    tmpdir = tempfile.mkdtemp(prefix="vss_audio_")
    try:
        plat = detect_platform(url)
        update_job(job_id, step=f"Checking link ({plat}). Downloading audio track...", progress=0.1)

        def audio_hook(frac):
            update_job(job_id, progress=0.1 + frac * 0.3, step=f"Audio downloading: {int(frac * 100)}%")

        audio_path, title = download_audio_for_transcription(url, tmpdir, audio_hook)
        kb = os.path.getsize(audio_path) // 1024

        update_job(job_id, title=title, platform=plat, step=f"Audio ready ({kb} KB). Transcribing with Groq AI...", progress=0.45)

        def ai_hook(msg):
            update_job(job_id, step=msg, progress=0.6)

        text, segments, detected_lang = transcribe_audio_groq(audio_path, key, ai_hook)

        update_job(
            job_id,
            text=text,
            segments=segments,
            detected_lang=detected_lang,
            step=f"Transcript complete! Translating to {target_lang}...",
            progress=0.8,
        )

        translated = translate_text(text, target_lang, api_key=key, source_lang=detected_lang or "auto")

        update_job(
            job_id,
            translated=translated,
            target_name=target_lang,
            step="Script extraction and translation completed successfully!",
            progress=1.0,
            state="done",
        )
    except Exception as e:
        update_job(job_id, state="error", error=str(e), step=f"Error: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def worker_download(job_id, url, quality):
    job_dir = os.path.join(DOWNLOADS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        plat = detect_platform(url)
        update_job(job_id, step=f"Connecting to {plat} for {quality.upper()} stream...", progress=0.05)

        def dl_hook(frac, msg):
            update_job(job_id, progress=min(0.95, frac), step=msg)

        path, title = download_media(url, quality, job_dir, dl_hook)
        size_mb = os.path.getsize(path) / (1024 * 1024)

        update_job(
            job_id,
            file_path=path,
            file_name=os.path.basename(path),
            file_size_mb=round(size_mb, 2),
            title=title,
            step=f"Video ready ({size_mb:.1f} MB) in {quality.upper()} quality!",
            progress=1.0,
            state="done",
        )
    except Exception as e:
        update_job(job_id, state="error", error=str(e), step=f"Error: {e}")


# ----------------------------------------------------------- HTTP Handler ---

class StudioHandler(BaseHTTPRequestHandler):
    server_version = f"VideoScriptStudio/{VERSION}"

    def log_message(self, format, *args):
        # Silent clean logging
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n:
                return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            pass
        return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # -- GET Routes --
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_file(os.path.join(STATIC_DIR, "index.html"), "text/html")

        if path.startswith("/static/"):
            rel = path[len("/static/"):].lstrip("/\\")
            if ".." in rel:
                return self._send(403, "Forbidden", "text/plain")
            fpath = os.path.join(STATIC_DIR, rel)
            ctype, _ = mimetypes.guess_type(fpath)
            return self._serve_file(fpath, ctype or "application/octet-stream")

        if path == "/api/config":
            cfg = load_config()
            return self._json({
                "app_name": APP_NAME,
                "version": VERSION,
                "groq_key": cfg.get("groq_key", ""),
                "languages": LANG_NAMES,
            })

        if path == "/api/network":
            local_ip = get_local_ip()
            port = self.server.server_port
            mobile_url = f"http://{local_ip}:{port}/"
            qr_b64 = generate_qr_base64(mobile_url)
            return self._json({
                "local_ip": local_ip,
                "port": port,
                "mobile_url": mobile_url,
                "qr_image": qr_b64,
            })

        if path == "/api/info":
            url = (query.get("url") or [""])[0]
            clean = extract_url(url)
            if not valid_url(clean):
                return self._json({"ok": False, "error": "Invalid URL provided."}, 400)
            try:
                info = fetch_video_info(clean)
                return self._json({"ok": True, "info": info})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)

        if path == "/api/job_status":
            job_id = (query.get("id") or [""])[0]
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                return self._json({"error": "Job not found"}, 404)

            res = {k: job[k] for k in (
                "id", "type", "state", "step", "progress", "error", "title",
                "platform", "detected_lang", "target_name", "file_name", "file_size_mb"
            ) if k in job}

            if job.get("state") == "done":
                if job.get("type") == "transcribe":
                    res["text"] = job.get("text", "")
                    res["translated"] = job.get("translated", "")
                    res["segments_text"] = segments_to_text(job.get("segments") or [])
                    res["word_count"] = len(job.get("text", "").split())
                    res["translated_word_count"] = len(job.get("translated", "").split())
                    res["segment_count"] = len(job.get("segments") or [])
            return self._json(res)

        if path == "/api/download_file":
            job_id = (query.get("job_id") or [""])[0]
            kind = (query.get("kind") or ["video"])[0]
            with jobs_lock:
                job = jobs.get(job_id)
            if not job or job.get("state") != "done":
                return self._send(404, "File not ready or job not found", "text/plain")

            # Video/Audio download
            if job.get("type") == "download":
                fpath = job.get("file_path")
                if not fpath or not os.path.isfile(fpath):
                    return self._send(404, "File missing on server", "text/plain")
                fname = os.path.basename(fpath)
                return self._serve_download(fpath, fname)

            # Transcribe exports
            if kind == "srt":
                body = segments_to_srt(job.get("segments") or [])
                fname = f"{job.get('title','transcript')[:40]}.srt"
            elif kind == "translated":
                body = job.get("translated", "")
                fname = f"{job.get('title','translation')[:40]}_translated.txt"
            else:
                body = (
                    f"=========================================\n"
                    f"  {APP_NAME} v{VERSION} - Transcript\n"
                    f"  Platform: {job.get('platform','')}\n"
                    f"  Title: {job.get('title','')}\n"
                    f"  Language: {job.get('detected_lang','')}\n"
                    f"=========================================\n\n"
                    f"--- ORIGINAL SCRIPT ---\n\n{job.get('text','')}\n\n"
                )
                if job.get("translated"):
                    body += f"--- TRANSLATION ({job.get('target_name','English')}) ---\n\n{job.get('translated','')}\n"
                fname = f"{job.get('title','transcript')[:40]}.txt"

            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.end_headers()
            self.wfile.write(data)
            return

        return self._send(404, "Not found", "text/plain")

    # -- POST Routes --
    def do_POST(self):
        path = urlparse(self.path).path
        payload = self._body_json()

        if path == "/api/test_key":
            key = (payload.get("key") or "").strip()
            if not key:
                return self._json({"ok": False, "msg": "Key is empty."})
            ok = test_groq_key(key)
            if ok:
                cfg = load_config()
                cfg["groq_key"] = key
                save_config(cfg)
            return self._json({
                "ok": ok,
                "msg": "Key verified & saved successfully!" if ok else "Invalid Groq API key or network issue."
            })

        if path == "/api/start_transcribe":
            url = (payload.get("url") or "").strip()
            key = (payload.get("key") or "").strip()
            target_lang = payload.get("target_lang") or "English"

            clean = extract_url(url)
            if not valid_url(clean):
                return self._json({"ok": False, "msg": "Invalid video link."})
            if not key:
                cfg = load_config()
                key = cfg.get("groq_key", "")
                if not key:
                    return self._json({"ok": False, "msg": "Groq API Key is required (FREE from console.groq.com)."})

            # Save key locally
            cfg = load_config()
            cfg["groq_key"] = key
            save_config(cfg)

            job_id = uuid.uuid4().hex[:12]
            with jobs_lock:
                jobs[job_id] = {
                    "id": job_id,
                    "type": "transcribe",
                    "state": "running",
                    "step": "Starting audio extraction...",
                    "progress": 0.05,
                    "target_name": target_lang,
                }
            threading.Thread(target=worker_transcribe, args=(job_id, clean, key, target_lang), daemon=True).start()
            return self._json({"ok": True, "job_id": job_id})

        if path == "/api/start_download":
            url = (payload.get("url") or "").strip()
            quality = (payload.get("quality") or "1080p").lower()
            clean = extract_url(url)
            if not valid_url(clean):
                return self._json({"ok": False, "msg": "Invalid video link."})

            job_id = uuid.uuid4().hex[:12]
            with jobs_lock:
                jobs[job_id] = {
                    "id": job_id,
                    "type": "download",
                    "state": "running",
                    "step": f"Preparing {quality.upper()} download...",
                    "progress": 0.05,
                    "quality": quality,
                }
            threading.Thread(target=worker_download, args=(job_id, clean, quality), daemon=True).start()
            return self._json({"ok": True, "job_id": job_id})

        if path == "/api/translate_now":
            text = (payload.get("text") or "").strip()
            target_lang = payload.get("target_lang") or "English"
            key = (payload.get("key") or "").strip() or load_config().get("groq_key")
            if not text:
                return self._json({"ok": False, "msg": "Text is empty."})
            try:
                res = translate_text(text, target_lang, api_key=key)
                return self._json({"ok": True, "translated": res})
            except Exception as e:
                return self._json({"ok": False, "msg": str(e)}, 500)

        return self._send(404, "Not found", "text/plain")

    def _serve_file(self, fpath, ctype):
        if not os.path.isfile(fpath):
            return self._send(404, "File not found", "text/plain")
        with open(fpath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8" if "text" in ctype or "javascript" in ctype else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_download(self, fpath, fname):
        ctype, _ = mimetypes.guess_type(fpath)
        size = os.path.getsize(fpath)
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.end_headers()
        with open(fpath, "rb") as f:
            shutil.copyfileobj(f, self.wfile)


def run_server(port=None, auto_open=True):
    if port is None:
        port = int(os.environ.get("PORT", DEFAULT_PORT))
    httpd = None
    chosen_port = port
    for p in range(port, port + 20):
        try:
            # Bind to 0.0.0.0 so phone on same Wi-Fi can connect!
            httpd = ThreadingHTTPServer(("0.0.0.0", p), StudioHandler)
            chosen_port = p
            break
        except OSError:
            continue

    if httpd is None:
        print("[!] No free port available.", flush=True)
        return

    local_ip = get_local_ip()
    local_url = f"http://localhost:{chosen_port}/"
    mobile_url = f"http://{local_ip}:{chosen_port}/"

    print("=" * 60, flush=True)
    print(f"🚀 {APP_NAME} v{VERSION} Running!", flush=True)
    print(f"💻 Laptop / Desktop URL : {local_url}", flush=True)
    print(f"📱 Mobile (Same Wi-Fi)  : {mobile_url}", flush=True)
    print("=" * 60, flush=True)

    if auto_open:
        threading.Timer(1.2, lambda: webbrowser.open(local_url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.", flush=True)


if __name__ == "__main__":
    auto_open = "--silent" not in sys.argv
    run_server(auto_open=auto_open)
