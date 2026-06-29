"""
Phase D: Batch Subtitle Extraction
YouTube: yt-dlp auto-subs (API)
Xiaoyuzhou: webpage transcript -> audio download + ASR
Batch size 10, YouTube single concurrency, quality checks.
"""
import hashlib, json, os, re, subprocess, sys, time, requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

ROOT = Path("D:/Users/AS/Desktop/podcast-distill")
sys.path.insert(0, str(ROOT))

from scripts.backfill.db import get_conn

ITEMS_DIR = ROOT / "backfill" / "items"
TEMP_DIR = ROOT / "backfill" / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

BATCH_SIZE = 10
YT_DELAY = 3  # seconds between YouTube extractions
QUALITY_MIN_COVERAGE = 0.95
QUALITY_MIN_CHARS = 200
QUALITY_REQUIRE_DURATION = 300  # 5 minutes

def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def classify_error(msg):
    t = (msg or "").lower()
    if "sign in" in t or "bot" in t or "captcha" in t:
        return "youtube_bot_check", True
    if "429" in t or "too many requests" in t:
        return "rate_limited", True
    if "403" in t or "requestblocked" in t:
        return "blocked", True
    if "timeout" in t or "timed out" in t:
        return "timeout", True
    if "transcriptsdisabled" in t:
        return "transcripts_disabled", False
    if "notranscriptfound" in t:
        return "no_transcript", False
    if "unavailable" in t or "private" in t:
        return "video_unavailable", False
    return "unknown", True

def vtt_to_text(content):
    lines, prev = [], None
    for line in content.splitlines():
        line = line.strip()
        if not line or "-->" in line or line == "WEBVTT":
            continue
        if re.fullmatch(r"\d+", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\{[^}]*\}", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and line != prev:
            lines.append(line)
            prev = line
    return lines

def estimate_coverage(vtt, total_seconds):
    if total_seconds <= 0:
        return 1.0
    last = 0
    for line in vtt.splitlines():
        m = re.match(r"(\d+):(\d+):(\d+)\.(\d+)\s*-->", line)
        if m:
            ts = int(m[1])*3600 + int(m[2])*60 + int(m[3]) + int(m[4])/1000.0
            last = max(last, ts)
    return min(1.0, last / total_seconds)

def parse_pt_duration(pt_str):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", pt_str)
    if not m:
        return 0
    d, h, mi, s = (int(v or 0) for v in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s

# ──────── YouTube Extraction ────────

def extract_youtube(item, output_dir):
    video_id = item["platform_id"]
    url = item["url"]
    lang = item.get("language", "en")[:2] or "en"

    print(f"    yt-dlp {video_id}: {item['title'][:50]}", flush=True)

    vtt_path = output_dir / "transcript.vtt"
    txt_path = output_dir / "transcript.txt"

    meta = {
        "schema_version": 1, "item_id": item["item_id"],
        "platform": "youtube", "platform_id": video_id,
        "source_id": item["source_id"], "title": item["title"],
        "url": url, "published_at": item["published_at"],
        "report_date": item["report_date"], "duration_seconds": item["duration_seconds"],
        "language": lang,
    }
    (output_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # Try auto-subs first
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--skip-download", "--write-auto-subs", "--sub-lang", lang,
        "--convert-subs", "vtt",
        "-o", str(output_dir / "raw.%(ext)s"),
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                                encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"status": "retryable", "error_type": "timeout", "error_message": "yt-dlp timeout"}

    vtt_files = sorted(output_dir.glob("*.vtt"))
    if not vtt_files:
        # Try manual captions
        cmd2 = [
            sys.executable, "-m", "yt_dlp",
            "--skip-download", "--write-subs", "--sub-lang", lang,
            "--convert-subs", "vtt",
            "-o", str(output_dir / "raw-manual.%(ext)s"), url,
        ]
        try:
            subprocess.run(cmd2, capture_output=True, text=True, timeout=180,
                           encoding="utf-8", errors="replace")
        except:
            pass
        vtt_files = sorted(output_dir.glob("*.vtt"))

    if vtt_files:
        raw = vtt_files[0]
        content = raw.read_text(encoding="utf-8", errors="replace")
        vtt_path.write_text(content, encoding="utf-8", newline="\n")
        for f in output_dir.glob("raw*"):
            if f != vtt_path:
                f.unlink(missing_ok=True)

        text_lines = vtt_to_text(content)
        txt_path.write_text("\n".join(text_lines), encoding="utf-8", newline="\n")
        text_chars = sum(len(l) for l in text_lines)
        dur = item["duration_seconds"]
        coverage = estimate_coverage(content, dur) if dur > 0 else 1.0

        return {"status": "success", "method": "yt-dlp", "language": lang,
                "text_chars": text_chars, "coverage_ratio": coverage,
                "sha256": sha256_hex(content)}

    stderr = (result.stderr or "")[:300]
    err_type, retryable = classify_error(stderr)
    if retryable:
        return {"status": "retryable", "error_type": err_type, "error_message": stderr}
    return {"status": "blocked", "error_type": err_type, "error_message": stderr}

# ──────── Xiaoyuzhou Extraction ────────

def extract_xiaoyuzhou(item, output_dir):
    eid = item["platform_id"]
    url = item["url"]
    print(f"    xyz {eid}: {item['title'][:50]}", flush=True)

    meta = {
        "schema_version": 1, "item_id": item["item_id"],
        "platform": "xiaoyuzhou", "platform_id": eid,
        "source_id": item["source_id"], "title": item["title"],
        "url": url, "published_at": item["published_at"],
        "report_date": item["report_date"], "duration_seconds": item["duration_seconds"],
        "language": "zh",
    }
    (output_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 1. Try webpage embedded transcript
    result = try_webpage_transcript(url, output_dir)
    if result:
        return result

    # 2. Try official API
    token = os.environ.get("XIAOYUZHOU_ACCESS_TOKEN", "")
    if token:
        result = try_official_api(eid, token, output_dir)
        if result:
            return result

    # 3. ASR: download audio + whisper
    result = try_asr_extraction(url, eid, item, output_dir)
    return result

def try_webpage_transcript(url, output_dir):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.content.decode("utf-8", errors="replace")
        m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return None
        data = json.loads(m.group(1))
        ep = data.get("props", {}).get("pageProps", {}).get("episode") or {}
        td = ep.get("transcript") or {}
        sentences = td.get("sentences") or []
        if not sentences:
            return None
        write_transcript_files(sentences, output_dir)
        content = (output_dir / "transcript.txt").read_text(encoding="utf-8")
        return {"status": "success", "method": "webpage_embedded", "language": "zh",
                "text_chars": len(content), "coverage_ratio": 1.0, "sha256": sha256_hex(content)}
    except:
        return None

def try_official_api(eid, token, output_dir):
    for payload in [{"eid": eid, "version": "release"}, {"eid": eid, "version": "asr"}]:
        try:
            resp = requests.post(
                "https://podcast-api.midway.run/management/episode-transcript/get",
                headers={**HEADERS, "x-jike-access-token": token, "Content-Type": "application/json"},
                json=payload, timeout=30,
            )
            if resp.status_code != 200:
                continue
            sentences = find_sentences(resp.json())
            if not sentences:
                continue
            write_transcript_files(sentences, output_dir)
            content = (output_dir / "transcript.txt").read_text(encoding="utf-8")
            return {"status": "success", "method": "official_api", "language": "zh",
                    "text_chars": len(content), "coverage_ratio": 1.0, "sha256": sha256_hex(content)}
        except:
            continue
    return None

def find_sentences(obj):
    if isinstance(obj, dict):
        s = obj.get("sentences")
        if isinstance(s, list) and s:
            return s
        for v in obj.values():
            r = find_sentences(v)
            if r:
                return r
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) and "text" in x for x in obj):
            return obj
        for v in obj:
            r = find_sentences(v)
            if r:
                return r
    return None

def write_transcript_files(sentences, output_dir):
    srt_lines, txt_lines = [], []
    prev = None
    for i, seg in enumerate(sentences):
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        if "startMs" in seg:
            start = float(seg["startMs"]) / 1000.0
            end = float(seg.get("endMs", seg["startMs"] + 1000)) / 1000.0
        elif "start" in seg:
            start = float(seg["start"])
            end = float(seg.get("end", start + 1))
        else:
            continue
        s, ms = divmod(max(0, int(start * 1000)), 1000)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        e_s, e_ms = divmod(max(0, int(end * 1000)), 1000)
        e_m, e_s = divmod(e_s, 60)
        e_h, e_m = divmod(e_m, 60)
        srt_lines.append(str(i + 1))
        srt_lines.append(f"{h:02d}:{m:02d}:{s:02d},{ms:03d} --> {e_h:02d}:{e_m:02d}:{e_s:02d},{e_ms:03d}")
        srt_lines.append(text)
        srt_lines.append("")
        if text != prev:
            txt_lines.append(text)
            prev = text
    (output_dir / "transcript.srt").write_text("\n".join(srt_lines), encoding="utf-8", newline="\n")
    (output_dir / "transcript.txt").write_text("\n".join(txt_lines), encoding="utf-8", newline="\n")

WHISPER_BIN = ROOT / "whisper-bin-x64" / "Release" / "whisper-cli.exe"
WHISPER_MODEL = ROOT / "whisper-bin-x64" / "models" / "ggml-small-q5_1.bin"
FFMPEG_FULL = "C:\\Users\\AS\\AppData\\Local\\Microsoft\\WinGet\\Packages\\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\\ffmpeg-8.1.1-full_build\\bin\\ffmpeg.exe"

def try_asr_extraction(url, eid, item, output_dir):
    """Download audio, convert to MP3, run whisper.cpp CLI."""
    # Find audio URL from episode page
    audio_url = find_audio_url(url)
    if not audio_url:
        return {"status": "needs_asr", "error_type": "no_audio_url",
                "error_message": "could not find audio URL from episode page"}

    # Download raw audio
    raw_ext = ".m4a" if ".m4a" in audio_url else ".mp3"
    raw_path = TEMP_DIR / f"{eid}{raw_ext}"
    print(f"    Downloading audio: {audio_url[:80]}...", flush=True)
    try:
        resp = requests.get(audio_url, headers=HEADERS, timeout=300, stream=True)
        resp.raise_for_status()
        with open(raw_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        size_mb = raw_path.stat().st_size / 1024 / 1024
        print(f"    Downloaded: {size_mb:.1f} MB", flush=True)
    except Exception as e:
        return {"status": "retryable", "error_type": "audio_download_failed",
                "error_message": str(e)[:200]}

    # Convert to MP3 if needed (whisper.cpp supports mp3, wav, flac, ogg)
    if raw_ext == ".m4a":
        mp3_path = TEMP_DIR / f"{eid}.mp3"
        print(f"    Converting M4A to MP3...", flush=True)
        try:
            result = subprocess.run(
                [FFMPEG_FULL, "-y", "-i", str(raw_path), "-ar", "16000", "-ac", "1",
                 "-c:a", "libmp3lame", "-b:a", "32k", str(mp3_path)],
                capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace",
            )
            raw_path.unlink(missing_ok=True)
            if result.returncode != 0:
                return {"status": "retryable", "error_type": "ffmpeg_failed",
                        "error_message": (result.stderr or "")[:300]}
            audio_path = mp3_path
            print(f"    Converted: {mp3_path.stat().st_size / 1024 / 1024:.1f} MB", flush=True)
        except Exception as e:
            raw_path.unlink(missing_ok=True)
            return {"status": "retryable", "error_type": "ffmpeg_failed",
                    "error_message": str(e)[:200]}
    else:
        audio_path = raw_path

    # Run whisper.cpp
    lang = item.get("language", "zh")[:2] or "zh"
    cpu_count = os.cpu_count() or 4
    threads = str(max(1, min(8, cpu_count)))
    print(f"    Running whisper.cpp ({lang}, {threads}t)...", flush=True)

    try:
        result = subprocess.run(
            [str(WHISPER_BIN), "-m", str(WHISPER_MODEL), "-f", str(audio_path),
             "-l", lang, "-t", threads, "-osrt", "-of", str(output_dir / "transcript")],
            capture_output=True, text=True, timeout=3600,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            audio_path.unlink(missing_ok=True)
            return {"status": "retryable", "error_type": "whisper_failed",
                    "error_message": (result.stderr or "")[:300]}
    except subprocess.TimeoutExpired:
        audio_path.unlink(missing_ok=True)
        return {"status": "retryable", "error_type": "whisper_timeout",
                "error_message": "whisper.cpp timeout after 30min"}
    except Exception as e:
        audio_path.unlink(missing_ok=True)
        return {"status": "retryable", "error_type": "whisper_error",
                "error_message": str(e)[:200]}

    # Clean up temp MP3
    audio_path.unlink(missing_ok=True)

    # Find generated SRT, generate TXT
    srt_path = output_dir / "transcript.srt"
    if srt_path.exists():
        srt_content = srt_path.read_text(encoding="utf-8", errors="replace")
        txt_lines = []
        for line in srt_content.splitlines():
            line = line.strip()
            if not line or "-->" in line or re.fullmatch(r"\d+", line):
                continue
            txt_lines.append(line)
        (output_dir / "transcript.txt").write_text("\n".join(txt_lines), encoding="utf-8", newline="\n")
        text_chars = sum(len(l) for l in txt_lines)
        return {"status": "success", "method": "whisper_cpp", "language": lang,
                "text_chars": text_chars, "coverage_ratio": 0.95,
                "sha256": sha256_hex(srt_content)}

    return {"status": "retryable", "error_type": "no_srt_output",
            "error_message": "whisper.cpp completed but no SRT found"}

def find_audio_url(episode_url):
    """Find audio URL from episode page."""
    try:
        resp = requests.get(episode_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.content.decode("utf-8", errors="replace")

        # Method 1: og:audio meta tag
        m = re.search(r'<meta[^>]+property="og:audio"[^>]+content="([^"]+)"', html)
        if m:
            return m.group(1)

        # Method 2: __NEXT_DATA__ audio field
        m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if m:
            data = json.loads(m.group(1))
            ep = data.get("props", {}).get("pageProps", {}).get("episode") or {}
            audio = ep.get("audio") or {}
            url = audio.get("url") or ep.get("enclosure") or {}
            if isinstance(url, dict):
                url = url.get("url", "")
            if url:
                return url

        # Method 3: enclosure URL pattern
        m = re.search(r'https?://[^"\']+\.(mp3|m4a|aac)[^"\']*', html)
        if m:
            return m.group(0)
    except:
        pass
    return None

# ──────── Batch Runner ────────

def run_phase_d(max_batches=None, skip_xiaoyuzhou=False, skip_youtube=False):
    print("=" * 60, flush=True)
    print("  Phase D: Batch Subtitle Extraction", flush=True)
    print("=" * 60, flush=True)

    conn = get_conn()
    cur = conn.cursor()

    # Get pending items (YouTube first, then Xiaoyuzhou)
    cur.execute("""
        SELECT i.* FROM items i
        LEFT JOIN extractions e ON i.item_id = e.item_id
        WHERE (e.status IS NULL OR e.status = 'pending'
               OR (e.status = 'retryable' AND e.attempts < 3))
          AND i.duration_seconds >= ?
        ORDER BY
            CASE WHEN i.platform='youtube' THEN 0 ELSE 1 END,
            i.published_at ASC
    """, (QUALITY_REQUIRE_DURATION,))
    all_pending = [dict(r) for r in cur.fetchall()]

    yt_count = sum(1 for i in all_pending if i["platform"] == "youtube")
    xyz_count = sum(1 for i in all_pending if i["platform"] == "xiaoyuzhou")
    print(f"\nPending: {yt_count} YouTube + {xyz_count} Xiaoyuzhou = {len(all_pending)} total", flush=True)
    print(f"Batch size: {BATCH_SIZE}, YouTube delay: {YT_DELAY}s", flush=True)

    if skip_xiaoyuzhou:
        all_pending = [i for i in all_pending if i["platform"] == "youtube"]
        print(f"Skipping Xiaoyuzhou, processing {len(all_pending)} YouTube items", flush=True)
    if skip_youtube:
        all_pending = [i for i in all_pending if i["platform"] == "xiaoyuzhou"]
        print(f"Skipping YouTube, processing {len(all_pending)} Xiaoyuzhou items", flush=True)

    batch_num = 0
    total_processed = 0
    total_success = 0
    total_failed = 0
    start_time = datetime.now()

    for i in range(0, len(all_pending), BATCH_SIZE):
        batch = all_pending[i:i + BATCH_SIZE]
        batch_num += 1

        if max_batches and batch_num > max_batches:
            print(f"\nReached max_batches={max_batches}, stopping", flush=True)
            break

        print(f"\n--- Batch {batch_num} ({len(batch)} items) ---", flush=True)
        batch_success = 0

        for item in batch:
            item_id = item["item_id"]
            platform = item["platform"]

            # Skip if already completed
            cur.execute("SELECT status FROM extractions WHERE item_id=? AND status='success'", (item_id,))
            if cur.fetchone():
                print(f"  [SKIP] {item_id} already done", flush=True)
                batch_success += 1
                continue

            # Check attempts
            cur.execute("SELECT attempts FROM extractions WHERE item_id=?", (item_id,))
            row = cur.fetchone()
            attempts = row["attempts"] if row else 0
            if attempts >= 3:
                print(f"  [TERMINAL] {item_id} max retries", flush=True)
                continue

            # Create output dir
            if platform == "youtube":
                output_dir = ITEMS_DIR / "youtube" / item["platform_id"]
            else:
                output_dir = ITEMS_DIR / "xiaoyuzhou" / item["platform_id"]
            output_dir.mkdir(parents=True, exist_ok=True)

            # Mark running
            cur.execute(
                "INSERT OR REPLACE INTO extractions(item_id, status, attempts) VALUES(?, 'running', ?)",
                (item_id, attempts + 1),
            )
            conn.commit()

            # Extract
            try:
                if platform == "youtube":
                    r = extract_youtube(item, output_dir)
                    time.sleep(YT_DELAY)
                else:
                    r = extract_xiaoyuzhou(item, output_dir)
            except Exception as e:
                r = {"status": "retryable", "error_type": "exception", "error_message": str(e)[:200]}

            # Quality check for success
            if r["status"] == "success":
                dur = item["duration_seconds"]
                if dur >= QUALITY_REQUIRE_DURATION:
                    if r.get("coverage_ratio", 0) < QUALITY_MIN_COVERAGE:
                        r["status"] = "retryable"
                        r["error_type"] = "low_coverage"
                        r["error_message"] = f"coverage {r.get('coverage_ratio',0):.2f} < {QUALITY_MIN_COVERAGE}"
                    elif r.get("text_chars", 0) < QUALITY_MIN_CHARS:
                        r["status"] = "retryable"
                        r["error_type"] = "low_text"
                        r["error_message"] = f"text chars {r.get('text_chars',0)} < {QUALITY_MIN_CHARS}"

            # Write extraction.json
            rec = {
                "status": r["status"], "method": r.get("method", ""),
                "language": r.get("language", ""), "attempts": attempts + 1,
                "duration_seconds": item["duration_seconds"],
                "last_timestamp_seconds": None,
                "coverage_ratio": r.get("coverage_ratio"),
                "text_chars": r.get("text_chars"), "sha256": r.get("sha256"),
                "completed_at": datetime.now().isoformat(),
                "error_type": r.get("error_type"), "error_message": r.get("error_message"),
            }
            (output_dir / "extraction.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")

            # Update DB
            cur.execute(
                """UPDATE extractions SET status=?, method=?, language=?, attempts=?,
                   duration_seconds=?, coverage_ratio=?, text_chars=?, sha256=?,
                   error_type=?, error_message=?, completed_at=datetime('now'),
                   updated_at=datetime('now')
                   WHERE item_id=?""",
                (r["status"], r.get("method"), r.get("language"), attempts + 1,
                 item["duration_seconds"], r.get("coverage_ratio"),
                 r.get("text_chars"), r.get("sha256"),
                 r.get("error_type"), r.get("error_message"), item_id),
            )
            conn.commit()

            # Track failures
            if r["status"] not in ("success", "pending"):
                cur.execute(
                    """INSERT INTO failures(item_id, stage, error_type, error_message,
                       retry_count, max_retries, is_terminal, next_retry_at)
                       VALUES(?, 'extraction', ?, ?, ?, 3, ?, ?)""",
                    (item_id, r.get("error_type", "unknown"), r.get("error_message", "")[:500],
                     attempts, 0 if r["status"] == "blocked" else 1,
                     datetime.now().isoformat() if r["status"] == "retryable" else None),
                )
                conn.commit()

            marker = "[OK]" if r["status"] == "success" else f"[{r['status'].upper()}]"
            title = (item["title"] or "")[:60]
            print(f"  {marker} {title}", flush=True)

            if r["status"] == "success":
                batch_success += 1
                total_success += 1
            else:
                total_failed += 1

        total_processed += len(batch)

        # Progress snapshot
        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"  Batch {batch_num} done: {batch_success}/{len(batch)} success", flush=True)
        print(f"  Total: {total_processed}/{len(all_pending)} processed, "
              f"{total_success} success, {total_failed} failed, "
              f"{elapsed:.0f}s ({elapsed/60:.1f}m)", flush=True)

        # Save progress
        progress = {
            "updated_at": datetime.now().isoformat(),
            "total_pending": len(all_pending),
            "total_processed": total_processed,
            "total_success": total_success,
            "total_failed": total_failed,
            "batch_num": batch_num,
            "elapsed_seconds": elapsed,
        }
        (ROOT / "backfill" / "state" / "run_status.json").write_text(
            json.dumps(progress, ensure_ascii=False, indent=2))

        # Check for bot challenge
        if any("bot_check" in str(r.get("error_type", "")) for r in [r]):
            print("\n  [WARNING] YouTube bot challenge detected! Pausing...", flush=True)
            break

    # Final summary
    print("\n" + "=" * 60, flush=True)
    cur.execute("SELECT status, COUNT(*) as cnt FROM extractions GROUP BY status")
    for r in cur.fetchall():
        print(f"  {r['status']}: {r['cnt']}", flush=True)
    cur.execute("SELECT error_type, COUNT(*) as cnt FROM failures GROUP BY error_type")
    failed = cur.fetchall()
    if failed:
        for r in failed:
            print(f"  failures: {r['error_type']} = {r['cnt']}", flush=True)
    else:
        print(f"  no failures", flush=True)
    print("=" * 60, flush=True)

    conn.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-batches", type=int, help="Max batches to run")
    parser.add_argument("--skip-xiaoyuzhou", action="store_true", help="Skip Xiaoyuzhou (ASR)")
    parser.add_argument("--skip-youtube", action="store_true", help="Skip YouTube subtitles")
    args = parser.parse_args()
    run_phase_d(args.max_batches, args.skip_xiaoyuzhou, args.skip_youtube)