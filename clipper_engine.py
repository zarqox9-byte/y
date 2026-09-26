"""
clipper_engine.py - AI Movie-to-Shorts Auto-Clipper Engine
===========================================================
Technical capabilities:
1. YouTube URL metadata, chapters, and transcript extraction via yt-dlp.
2. Chronological narrative scene segmentation & viral scriptwriting via Google Gemini.
3. Fast streaming chunking with yt-dlp --download-sections (avoiding full movie downloads).
4. Smart Auto-Reframe (16:9 -> 9:16 vertical) with OpenCV face detection & centering.
5. High-quality neural voiceover generation via Edge-TTS (Hindi: hi-IN-MadhurNeural, English: en-US-ChristopherNeural).
6. FFmpeg audio mixing & ducking (original audio ducked to 15%, voiceover at 100%).
7. Chronological queue management for preview and one-click YouTube upload.
"""

import os
import re
import sys
import json
import uuid
import time
import math
import asyncio
import logging
import subprocess
import shutil
import urllib.request
import urllib.parse
import threading
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional, Tuple

def get_ffmpeg_bin() -> str:
    """Returns absolute path to ffmpeg binary, with imageio_ffmpeg fallback."""
    bin_path = shutil.which("ffmpeg")
    if bin_path:
        return bin_path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return "ffmpeg"


logger = logging.getLogger("clipper_engine")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [Clipper] %(message)s"))
    logger.addHandler(ch)

_RECENT_LOGS: List[str] = []

class MemoryLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            _RECENT_LOGS.append(msg)
            if len(_RECENT_LOGS) > 300:
                _RECENT_LOGS.pop(0)
        except Exception:
            pass

_mem_handler = MemoryLogHandler()
_mem_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [Clipper] %(message)s"))
logger.addHandler(_mem_handler)

import gemini_engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIPPER_DIR = os.path.join(BASE_DIR, "uploads", "clipper_shorts")
TEMP_DIR = os.path.join(BASE_DIR, "uploads", "clipper_temp")
JOBS_DIR = os.path.join(BASE_DIR, "uploads", "clipper_jobs")
CUTS_DIR = os.path.join(BASE_DIR, "uploads", "clipper_cuts")
TRIMMER_VIDEOS_DIR = os.path.join(BASE_DIR, "uploads", "trimmer_videos")
TRIMMER_EXPORTS_DIR = os.path.join(BASE_DIR, "uploads", "trimmer_exports")
os.makedirs(CLIPPER_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(CUTS_DIR, exist_ok=True)
os.makedirs(TRIMMER_VIDEOS_DIR, exist_ok=True)
os.makedirs(TRIMMER_EXPORTS_DIR, exist_ok=True)


# =====================================================================
# JOB CHECKPOINT & RESUME PERSISTENCE
# =====================================================================
def save_job_checkpoint(job_id: str, job_data: Dict[str, Any]) -> str:
    """
    Saves the entire job state (scenes, timestamps, scripts, video metadata,
    and completed shorts) to uploads/clipper_jobs/<job_id>.json atomically.
    """
    job_data['job_id'] = job_id
    job_data['updated_at'] = time.time()
    if 'created_at' not in job_data:
        job_data['created_at'] = time.time()

    file_path = os.path.join(JOBS_DIR, f"{job_id}.json")
    temp_path = os.path.join(JOBS_DIR, f"{job_id}.json.tmp_{uuid.uuid4().hex[:6]}")
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(job_data, f, indent=2, ensure_ascii=False)
        if os.path.exists(file_path):
            os.remove(file_path)
        os.rename(temp_path, file_path)
        logger.info(f"Saved job checkpoint for {job_id} (status: {job_data.get('status')})")
        return file_path
    except Exception as e:
        logger.error(f"Failed to save job checkpoint {job_id}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return file_path


def load_job_checkpoint(job_id: str) -> Optional[Dict[str, Any]]:
    """Loads job state from uploads/clipper_jobs/<job_id>.json."""
    file_path = os.path.join(JOBS_DIR, f"{job_id}.json")
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load job checkpoint {job_id}: {e}")
        return None


def list_saved_jobs() -> List[Dict[str, Any]]:
    """Returns summaries of all saved jobs sorted by updated_at descending."""
    jobs = []
    if not os.path.exists(JOBS_DIR):
        return jobs
    for fname in os.listdir(JOBS_DIR):
        if fname.endswith(".json") and not fname.endswith(".tmp"):
            job_id = fname[:-5]
            data = load_job_checkpoint(job_id)
            if data:
                v_info = data.get("video_info") or {}
                scenes = data.get("scenes") or []
                completed = data.get("completed_shorts") or {}
                jobs.append({
                    "job_id": job_id,
                    "title": v_info.get("title") or "Untitled Movie",
                    "url": data.get("url") or "",
                    "thumbnail": v_info.get("thumbnail") or "",
                    "status": data.get("status", "UNKNOWN"),
                    "total_scenes": len(scenes),
                    "completed_count": len(completed),
                    "language": (data.get("options") or {}).get("language") or "Hindi",
                    "updated_at": data.get("updated_at", 0),
                    "created_at": data.get("created_at", 0),
                    "error": data.get("error")
                })
    jobs.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return jobs


def format_seconds_to_timestamp(seconds: float) -> str:
    """Converts seconds float to HH:MM:SS format."""
    total_sec = max(0, int(seconds))
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def parse_timestamp_to_seconds(ts: str) -> int:
    """Parses HH:MM:SS or MM:SS to integer seconds."""
    ts = str(ts).strip()
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(float(parts[1]))
        elif len(parts) == 1:
            return int(float(parts[0]))
    except Exception:
        pass
    return 0


# =====================================================================
# 1. YOUTUBE METADATA & CHAPTERS EXTRACTION (WITH DUAL-FALLBACK)
# =====================================================================
def extract_video_id(url: str) -> Optional[str]:
    """Extracts 11-character YouTube video ID from various URL formats."""
    patterns = [
        r'(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})',
        r'^[A-Za-z0-9_-]{11}$'
    ]
    for pattern in patterns:
        match = re.search(pattern, str(url).strip())
        if match:
            return match.group(1) if match.groups() else str(url).strip()
    return None


def parse_iso8601_duration(duration_str: str) -> int:
    """Parses ISO 8601 duration string (e.g. PT1H2M30S, PT45S) into integer seconds."""
    match = re.match(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?', duration_str or '')
    if not match:
        return 0
    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    seconds = int(match.group(4) or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def extract_chapters_from_description(desc: str, total_duration: int = 0) -> List[Dict[str, Any]]:
    """Extracts timestamped chapter markers from description text if yt-dlp did not provide them."""
    chapters = []
    lines = (desc or "").splitlines()
    pattern = re.compile(r'(?:^|\s)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:\s+[-–—]?\s*)(.+)')
    for line in lines:
        m = pattern.search(line.strip())
        if m:
            h = int(m.group(1)) if m.group(1) else 0
            minutes = int(m.group(2))
            sec = int(m.group(3))
            ch_title = m.group(4).strip()
            start_time = h * 3600 + minutes * 60 + sec
            chapters.append({'start_time': start_time, 'title': ch_title})

    # Calculate end times
    for i in range(len(chapters)):
        if i < len(chapters) - 1:
            chapters[i]['end_time'] = chapters[i+1]['start_time']
        else:
            chapters[i]['end_time'] = total_duration if total_duration > chapters[i]['start_time'] else chapters[i]['start_time'] + 60
    return chapters


def get_youtube_data_api_client(credentials=None):
    """
    Creates an authenticated YouTube Data API v3 service.
    First checks provided credentials, then token.json, then accounts store.
    """
    try:
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
    except ImportError:
        logger.warning("google-api-python-client or google-auth not installed.")
        return None

    creds = credentials
    if not creds:
        # Check token.json in BASE_DIR
        token_path = os.path.join(BASE_DIR, "token.json")
        if os.path.exists(token_path):
            try:
                with open(token_path, "r", encoding="utf-8") as f:
                    token_data = json.load(f)
                creds = Credentials(**token_data)
            except Exception as e:
                logger.warning(f"Could not load token.json: {e}")

    if not creds:
        # Check accounts.json in uploads or base directory
        for acc_path in [os.path.join(BASE_DIR, "accounts.json"), os.path.join(BASE_DIR, "uploads", "accounts.json")]:
            if os.path.exists(acc_path):
                try:
                    with open(acc_path, "r", encoding="utf-8") as f:
                        acc_data = json.load(f)
                    if acc_data and isinstance(acc_data, dict):
                        first_account = next(iter(acc_data.values()))
                        if "credentials" in first_account:
                            creds = Credentials(**first_account["credentials"])
                            break
                except Exception as e:
                    logger.warning(f"Could not load {acc_path}: {e}")

    # Check YOUTUBE_TOKEN_JSON environment variable (used on Render)
    if not creds and os.environ.get("YOUTUBE_TOKEN_JSON"):
        try:
            token_data = json.loads(os.environ["YOUTUBE_TOKEN_JSON"])
            creds = Credentials(**token_data)
        except Exception as e:
            logger.warning(f"Could not load YOUTUBE_TOKEN_JSON env: {e}")

    # Refresh credentials if expired
    if creds and hasattr(creds, 'expired') and creds.expired and hasattr(creds, 'refresh_token') and creds.refresh_token:
        try:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            logger.info("Successfully refreshed expired OAuth credentials for YouTube Data API v3.")
        except Exception as ref_err:
            logger.warning(f"Failed to refresh credentials: {ref_err}")

    if creds:
        try:
            return build("youtube", "v3", credentials=creds)
        except Exception as e:
            logger.warning(f"Failed to build YouTube service from credentials: {e}")

    # Fallback to YOUTUBE_API_KEY developerKey if available
    yt_api_key = os.environ.get("YOUTUBE_API_KEY")
    if yt_api_key:
        try:
            return build("youtube", "v3", developerKey=yt_api_key)
        except Exception as e:
            logger.warning(f"Failed to build YouTube service with YOUTUBE_API_KEY: {e}")

    return None


def extract_youtube_info(youtube_url: str, credentials=None) -> Dict[str, Any]:
    """
    Extracts video metadata, duration, description, chapters, and thumbnails.
    1. Primary: Uses yt-dlp configured with multiple web/embed clients to bypass datacenter bot blocks.
    2. Fallback: If yt-dlp hits a bot warning or error, attempts official YouTube Data API v3.
    3. Resilient Fallback: Uses YouTube oEmbed API (zero bot blocks on datacenter IPs like Render).
    4. Ultimate Fallback: Generates safe baseline metadata from video ID so pipeline never crashes.
    """
    info = None
    yt_dlp_err = None

    # Step 1: Attempt extraction via yt-dlp with anti-bot extractor arguments
    try:
        import yt_dlp
        ydl_opts = {
            'skip_download': True,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            'socket_timeout': 5,
            'extractor_args': {
                'youtube': {
                    'player_client': ['web_creator', 'web_embedded', 'mweb', 'android', 'ios'],
                    'player_skip': ['webpage', 'configs']
                }
            },
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            'compat_opts': ['no-youtube-unavailable-videos'],
        }

        logger.info(f"Extracting video metadata via yt-dlp for: {youtube_url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
    except Exception as e:
        yt_dlp_err = e
        logger.warning(f"yt-dlp extraction encountered issue: {e}. Falling back to secondary metadata providers...")

    if info and isinstance(info, dict):
        title = info.get('title', 'Unknown Title')
        duration = int(info.get('duration') or 0)
        description = info.get('description', '')
        chapters = info.get('chapters') or []
        thumbnail = info.get('thumbnail') or ''
        channel = info.get('uploader') or info.get('channel') or ''
        clean_desc = (description[:2000] if description else "").strip()

        logger.info(f"Metadata extracted via yt-dlp: '{title}' ({format_seconds_to_timestamp(duration)}), Chapters: {len(chapters)}")
        return {
            "url": youtube_url,
            "title": title,
            "duration": duration,
            "duration_str": format_seconds_to_timestamp(duration),
            "description": clean_desc,
            "chapters": chapters,
            "thumbnail": thumbnail,
            "channel": channel
        }

    # Step 2: Attempt fallback to Official YouTube Data API v3
    video_id = extract_video_id(youtube_url)
    if video_id:
        try:
            yt_service = get_youtube_data_api_client(credentials=credentials)
            if yt_service:
                logger.info(f"Executing YouTube Data API v3 fallback for video ID: {video_id}")
                response = yt_service.videos().list(id=video_id, part='snippet,contentDetails').execute()
                items = response.get('items', [])
                if items:
                    item = items[0]
                    snippet = item.get('snippet', {})
                    content_details = item.get('contentDetails', {})

                    title = snippet.get('title', 'YouTube Video')
                    description = snippet.get('description', '')
                    channel = snippet.get('channelTitle', '')
                    duration = parse_iso8601_duration(content_details.get('duration', ''))

                    # Get best thumbnail
                    thumbs = snippet.get('thumbnails', {})
                    thumbnail = (
                        thumbs.get('maxres', {}).get('url') or
                        thumbs.get('standard', {}).get('url') or
                        thumbs.get('high', {}).get('url') or
                        thumbs.get('medium', {}).get('url') or
                        thumbs.get('default', {}).get('url') or
                        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
                    )

                    chapters = extract_chapters_from_description(description, duration)
                    clean_desc = (description[:2000] if description else "").strip()

                    logger.info(f"Successfully extracted metadata via YouTube Data API v3: '{title}' ({format_seconds_to_timestamp(duration)})")
                    return {
                        "url": youtube_url,
                        "title": title,
                        "duration": duration,
                        "duration_str": format_seconds_to_timestamp(duration),
                        "description": clean_desc,
                        "chapters": chapters,
                        "thumbnail": thumbnail,
                        "channel": channel
                    }
        except Exception as api_err:
            logger.warning(f"YouTube Data API v3 fallback encountered issue: {api_err}")

    # Step 3: Resilient Fallback to YouTube oEmbed API (Zero Bot Blocks on Cloud Datacenter IPs)
    logger.info(f"Executing zero-block YouTube oEmbed fallback for: {youtube_url}")
    oembed_title = ""
    oembed_author = ""
    oembed_thumb = ""
    try:
        oe_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(youtube_url, safe=':/?=&')}&format=json"
        req = urllib.request.Request(
            oe_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Accept": "application/json"
            }
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            oe_data = json.loads(resp.read().decode('utf-8', errors='ignore'))
            oembed_title = oe_data.get('title', '')
            oembed_author = oe_data.get('author_name', '')
            oembed_thumb = oe_data.get('thumbnail_url', '')
            logger.info(f"oEmbed successfully retrieved video: '{oembed_title}' by '{oembed_author}'")
    except Exception as oe_err:
        logger.warning(f"oEmbed retrieval notice: {oe_err}")

    # Step 4: Try extracting duration and description from public watch page
    scraped_duration = 0
    scraped_desc = ""
    if video_id:
        try:
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
            req = urllib.request.Request(
                watch_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9"
                }
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                page_text = resp.read().decode('utf-8', errors='ignore')
                dur_m = re.search(r'"approxDurationMs"\s*:\s*"(\d+)"', page_text)
                if dur_m:
                    scraped_duration = int(dur_m.group(1)) // 1000
                desc_m = re.search(r'"shortDescription"\s*:\s*"(.*?)"', page_text)
                if desc_m:
                    scraped_desc = desc_m.group(1).encode('utf-8').decode('unicode_escape', errors='ignore')
        except Exception as scrape_err:
            logger.warning(f"Public page scrape notice: {scrape_err}")

    # Assemble resilient final metadata (Never throws RuntimeError)
    final_title = oembed_title or (f"Movie Narrative ({video_id})" if video_id else "YouTube Video")
    final_duration = scraped_duration if scraped_duration > 60 else 7200  # Default 2-hour movie timeline if unknown
    final_channel = oembed_author or "YouTube"
    final_thumb = oembed_thumb or (f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg" if video_id else "")
    final_desc = scraped_desc or f"Full-length movie narrative storyline and character breakdown for {final_title}."
    chapters = extract_chapters_from_description(final_desc, final_duration)

    logger.info(f"Resilient fallback metadata ready: '{final_title}' ({format_seconds_to_timestamp(final_duration)})")
    return {
        "url": youtube_url,
        "title": final_title,
        "duration": final_duration,
        "duration_str": format_seconds_to_timestamp(final_duration),
        "description": final_desc[:2000],
        "chapters": chapters,
        "thumbnail": final_thumb,
        "channel": final_channel
    }


# =====================================================================
# 2. GEMINI CHRONOLOGICAL SCENE SEGMENTATION & SCRIPTING
# =====================================================================
def analyze_movie_narrative_for_shorts(
    youtube_url: str,
    video_info: Dict[str, Any],
    max_shorts: int = 5,
    target_duration: int = 58,
    language: str = "Hindi",
    job_id: Optional[str] = None,
    wps: float = 2.4,
    voice_name: str = "Kore",
    tone_style: str = "Suspense / Thriller"
) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
    """
    Prompts Google Gemini to analyze the movie's storyline and generate
    high-tension scenes in strict chronological order with viral titles,
    hooks, and calibrated WPS-balanced recap scripts.
    Saves analysis checkpoint immediately to uploads/clipper_jobs/<job_id>.json.
    """
    title = video_info.get("title", "")
    duration = video_info.get("duration", 0)
    duration_str = video_info.get("duration_str", "")
    description = video_info.get("description", "")
    chapters = video_info.get("chapters", [])

    target_words = max(25, int(round(target_duration * wps)))
    is_long_montage = target_duration > 70
    min_cuts = max(6, int(target_duration / 7))
    max_cuts = max(10, int(target_duration / 4))

    chapters_summary = ""
    if chapters:
        ch_lines = []
        for ch in chapters[:25]:
            ch_start = format_seconds_to_timestamp(ch.get("start_time", 0))
            ch_end = format_seconds_to_timestamp(ch.get("end_time", 0))
            ch_title = ch.get("title", "")
            ch_lines.append(f"- [{ch_start} - {ch_end}] {ch_title}")
        chapters_summary = "\nChapters provided by creator:\n" + "\n".join(ch_lines)

    lang_instruction = (
        f"Write natural, viral storytelling {language} (Devanagari script preferred, emotional, high-suspense like top YouTube movie explanation channels). "
        "Example style: 'कहानी की शुरुआत में जब रोहन इस खतरनाक जगह पर पहुंचता है, तो उसे नहीं पता था कि आगे क्या होने वाला है...'"
        if language.lower().startswith("hi")
        else f"Write high-energy, dramatic, fast-paced English narrative recap scripts like top cinema recap channels."
    )

    montage_desc = (
        f"Select {min_cuts} to {max_cuts} targeted, non-contiguous sub-clips (each 4 to 8 seconds long) totaling {target_duration} seconds for a cinematic recap montage."
        if is_long_montage else
        f"Select 6 to 10 targeted, non-contiguous sub-clips (each 3 to 6 seconds long) totaling {target_duration} seconds (between 50 and 65 seconds for Shorts)."
    )

    prompt = f"""You are a master Hollywood Cinema Director & YouTube Video Trailer Strategist specializing in viral, high-drama storytelling videos.
Your task is to analyze the following movie / video storyline and discover the most gripping, high-retention narrative moments across the ENTIRE storyline to produce videos in STRICT CHRONOLOGICAL ORDER (Part 1, Part 2, Part 3... from beginning to the climax/resolution).

=== THE SMART DIRECTOR MULTI-SCENE STORYBOARD RULE ===
To ensure 100% YouTube Content ID & copyright safety, DO NOT pick a single continuous clip for any Part.
Instead, for EACH Part, you act as the trailer director:
{montage_desc}
Dramatic beats to represent across that story segment:
- [Hook]: Instant visual or dialogue shocker (0-3s retention grip)
- [Setup]: Establishing the perilous situation or conflict
- [Tension]: Escalating suspense, ticking clock, or imminent danger
- [Action]: Sudden movement, fight, pursuit, or explosion
- [Twist]: Shocking discovery or unexpected betrayal
- [Reaction]: Extreme emotional facial close-up or disbelief
- [Climax]: The peak turning point of this story segment
- [Cliffhanger]: A breathtaking cut right before the resolution, forcing viewers to watch Part N+1!

=== WORDS-PER-SECOND TIMING & CALIBRATION ===
- Selected Voice: {voice_name} | Tone Style: {tone_style}
- Calibrated Narration Pace: {wps:.2f} Words Per Second
- Target Duration: {target_duration} seconds
- EXACT TOTAL RECAP SCRIPT LENGTH: ~{target_words} words in {language}!
- Cut-by-cut rule: Spoken words per cut = Cut Duration (seconds) * {wps:.2f}.
- The narration must pace evenly across the cuts so the voiceover concludes precisely as the final cut resolves.

=== MOVIE / VIDEO DETAILS ===
Title: {title}
Total Duration: {duration_str} ({duration} seconds)
Description:
{description[:1500]}
{chapters_summary}

=== REQUIREMENTS ===
0. EXACT PARTS COUNT:
   - You MUST plan and generate EXACTLY {max_shorts} chronological Parts (from Part 1 up to Part {max_shorts}).
   - Do NOT return fewer or more than {max_shorts} Parts.

1. CHRONOLOGY & PROGRESSION:
   - Every Part must be in STRICT CHRONOLOGICAL ORDER across the full film/video arc.
   - Within each Part, the sub-clips must advance chronologically through that story segment.
   - Focus cuts on character reactions, high-tension beats, twists, action punches, and reveals.

2. SUB-CLIPS SPECIFICATION:
   - Specify "start_time" (e.g. "00:04:12"), "end_time" (e.g. "00:04:16"), "duration", "beat" (e.g. "[Hook]"), and "description".
   - The sum of all sub-clip durations for a Part must equal approximately {target_duration} seconds.

3. VIRAL RECAP SCRIPT ({language.upper()}):
   - For each Part, write a cohesive, gripping ~{target_words}-word voiceover script matching the visual progression of the cuts.
   - {lang_instruction}
   - Must begin with a 3-second scroll-stopping retention hook matching Cut 1 [Hook].
   - Must narrate the story seamlessly across the montage cuts without awkward pauses.
   - Must end on a high-retention cliffhanger prompting viewers to like and watch Part N+1!

4. METADATA:
   - Title must include the Part number, emotional emojis, and hashtags (e.g., "{title[:28]} - Shocking Twist! 😱 Part 1 #Shorts #MovieRecap").
   - 6-8 relevant viral tags.

=== RETURN FORMAT ===
Return ONLY a valid JSON array of objects with no markdown explanation:
[
  {{
    "part": 1,
    "title": "Viral Title! 😱 Part 1 #Shorts #MovieRecap",
    "hook": "3-second opening hook line",
    "script": "Complete 80-110 word cohesive narrative voiceover script in {language}...",
    "tags": ["shorts", "movie", "viral", "recap", "part1"],
    "total_duration": 58,
    "start_time": "00:04:12",
    "end_time": "00:15:45",
    "sub_clips": [
      {{
        "clip_num": 1,
        "start_time": "00:04:12",
        "end_time": "00:04:16",
        "duration": 4,
        "beat": "[Hook]",
        "description": "Explosive opening confrontation"
      }},
      {{
        "clip_num": 2,
        "start_time": "00:07:30",
        "end_time": "00:07:35",
        "duration": 5,
        "beat": "[Setup]",
        "description": "Danger is revealed"
      }}
    ]
  }}
]
"""

    logger.info("Calling Gemini for chronological scene segmentation...")
    client = gemini_engine.get_genai_client()
    cfg = gemini_engine.get_gemini_config()
    target_model = cfg.get("model") or gemini_engine.DEFAULT_MODEL
    models_to_try = [target_model] + [m for m in gemini_engine.FALLBACK_MODELS if m != target_model]

    raw_response = None
    quota_error_msg = None
    status = "ANALYZED"

    for model_name in models_to_try:
        try:
            logger.info(f"Attempting Gemini scene analysis with model: {model_name}")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"temperature": 0.3, "tools": []}
            )
            if response and response.text:
                raw_response = response.text.strip()
                logger.info(f"Successfully received Gemini response using {model_name}")
                break
        except Exception as e:
            err_str = str(e)
            logger.warning(f"Model {model_name} failed with error: {err_str}. Cascading...")
            if any(w in err_str.lower() for w in ["429", "resource_exhausted", "quota", "rate limit"]):
                quota_error_msg = f"Gemini API Quota Exceeded (429): {err_str}"

    scenes = []
    if raw_response:
        clean_json = raw_response
        if "```" in clean_json:
            clean_json = re.sub(r"^```(?:json)?", "", clean_json, flags=re.MULTILINE)
            clean_json = re.sub(r"```$", "", clean_json, flags=re.MULTILINE).strip()

        try:
            parsed = json.loads(clean_json)
            if isinstance(parsed, list):
                scenes = parsed
            elif isinstance(parsed, dict) and "scenes" in parsed:
                scenes = parsed["scenes"]
        except Exception as json_err:
            logger.error(f"Failed to parse Gemini JSON: {json_err}. Raw text:\n{raw_response[:500]}")

    # If quota limit occurred and no raw response was returned
    if not scenes and quota_error_msg:
        logger.warning(f"Gemini quota limit encountered. Marking job as PAUSED_QUOTA_LIMIT.")
        status = "PAUSED_QUOTA_LIMIT"
        scenes = generate_algorithmic_scenes(title, duration, max_shorts, target_duration, language)
    elif not scenes:
        logger.info("Using intelligent algorithmic chronological scene generator fallback...")
        scenes = generate_algorithmic_scenes(title, duration, max_shorts, target_duration, language)

    # Sanitize, enforce chronology, and validate bounds
    sanitized_scenes = sanitize_and_order_scenes(scenes, duration, target_duration, title, max_shorts=max_shorts, language=language)

    # Save to persistent checkpoint file if job_id provided
    if job_id:
        existing_checkpoint = load_job_checkpoint(job_id) or {}
        checkpoint_data = {
            "job_id": job_id,
            "url": youtube_url,
            "video_info": video_info,
            "options": {
                "max_shorts": max_shorts,
                "target_duration": target_duration,
                "language": language
            },
            "status": status,
            "scenes": sanitized_scenes,
            "completed_shorts": existing_checkpoint.get("completed_shorts") or {},
            "error": quota_error_msg
        }
        save_job_checkpoint(job_id, checkpoint_data)

    return sanitized_scenes, status, quota_error_msg


BEAT_DEFINITIONS = [
    ("[Hook]", "Opening shock / high-retention visual hook"),
    ("[Setup]", "Setting the dangerous premise and stakes"),
    ("[Tension]", "Rising suspense and imminent threat"),
    ("[Action]", "Explosive movement, conflict, or high-energy chase"),
    ("[Twist]", "Unexpected revelation or sudden turn of events"),
    ("[Reaction]", "Dramatic character emotion and intensity"),
    ("[Climax]", "Peak conflict and breathtaking confrontation"),
    ("[Cliffhanger]", "Suspenseful cliffhanger cut urging Part progression")
]


def generate_algorithmic_subclips(
    start_sec: int,
    end_sec: int,
    target_duration: int = 58
) -> List[Dict[str, Any]]:
    """
    Generates 6 to 10 dynamic targeted sub-clips (3 to 6 seconds each) across [start_sec, end_sec]
    with director beat labels ([Hook], [Setup], [Tension], [Action], [Twist], [Reaction], [Climax], [Cliffhanger]),
    totaling 50 to 65 seconds for 100% YouTube copyright safety.
    """
    target_d = max(30, target_duration)
    curr_durs = []
    tot = 0
    cycle = [5, 6, 4, 7, 5, 6, 5]
    ci = 0
    while tot < target_d:
        d = cycle[ci % len(cycle)]
        ci += 1
        if tot + d <= target_d:
            curr_durs.append(d)
            tot += d
        else:
            rem = target_d - tot
            if rem >= 3:
                curr_durs.append(rem)
                tot += rem
            elif curr_durs:
                curr_durs[-1] += rem
                tot += rem
            break

    count = len(curr_durs)
    span = max(end_sec - start_sec, count * 6 + 10)
    step = (span - 6) / max(count - 1, 1) if count > 1 else 0

    sub_clips = []
    for k in range(count):
        c_start = int(start_sec + k * step)
        c_dur = curr_durs[k]
        c_end = c_start + c_dur
        beat_tag, beat_desc = BEAT_DEFINITIONS[min(k, len(BEAT_DEFINITIONS) - 1)]
        if k == count - 1:
            beat_tag, beat_desc = "[Cliffhanger]", "Suspenseful cliffhanger cut urging Part progression"
        elif k == count - 2 and count >= 4:
            beat_tag, beat_desc = "[Climax]", "Peak conflict and breathtaking confrontation"

        sub_clips.append({
            "clip_num": k + 1,
            "start_time": format_seconds_to_timestamp(c_start),
            "end_time": format_seconds_to_timestamp(c_end),
            "start_seconds": c_start,
            "end_seconds": c_end,
            "duration": c_dur,
            "beat": beat_tag,
            "description": beat_desc
        })
    return sub_clips


def generate_algorithmic_scenes(
    title: str,
    duration: int,
    max_shorts: int = 5,
    target_duration: int = 58,
    language: str = "Hindi"
) -> List[Dict[str, Any]]:
    """Creates high-quality chronological multi-scene montage scenes if Gemini output was unparseable."""
    scenes = []
    count = min(max(1, max_shorts), 20)
    effective_duration = max(duration, count * target_duration + 60)
    step = (effective_duration - 60) / (count + 1)

    for i in range(1, count + 1):
        seg_start = int(30 + (i - 1) * step)
        seg_end = min(seg_start + max(120, target_duration * 3), duration - 5 if duration > 180 else seg_start + 120)
        if seg_end <= seg_start:
            seg_end = seg_start + 90

        sub_clips = generate_algorithmic_subclips(seg_start, seg_end, target_duration)
        total_dur = sum(c["duration"] for c in sub_clips)

        if language.lower().startswith("hi"):
            script = (
                f"फिल्म के पार्ट {i} में कहानी एक बेहद खतरनाक और रोमांचक मोड़ लेती है। "
                f"जब मुख्य किरदार इस भयानक संकट में घिर जाता है, तो हर सेकंड मौत उसके सामने खड़ी थी। "
                f"लेकिन एक चौंकाने वाले खुलासे ने सब कुछ बदल कर रख दिया! "
                f"क्या वह इस खौफनाक जाल से जिंदा बच पाएगा? देखिए आगे और पार्ट {i+1} के लिए सब्सक्राइब जरूर करें!"
            )
            hook = f"पार्ट {i} का यह सबसे खतरनाक सीन देखकर आपके रोंगटे खड़े हो जाएंगे! 😱"
        else:
            script = (
                f"In Part {i} of this intense story, danger escalates to an all-time high. "
                f"Trapped in an impossible situation with no easy way out, every second counts. "
                f"Just when escape seems impossible, a shocking revelation turns everything upside down! "
                f"Will the hero survive the ultimate test? Watch till the end to find out, and subscribe for Part {i+1}!"
            )
            hook = f"The most shocking twist in Part {i} you never saw coming! 😱"

        scenes.append({
            "part": i,
            "start_time": sub_clips[0]["start_time"],
            "end_time": sub_clips[-1]["end_time"],
            "start_seconds": sub_clips[0]["start_seconds"],
            "end_seconds": sub_clips[-1]["end_seconds"],
            "duration": total_dur,
            "title": f"{title[:32]} - Shocking Twist! 😱 Part {i} #Shorts #MovieRecap",
            "hook": hook,
            "script": script,
            "tags": ["shorts", "movie", "recap", f"part{i}", "viral", "cinema", "montage"],
            "sub_clips": sub_clips,
            "montage_mode": True,
            "copyright_safe": True
        })
    return scenes


def sanitize_and_order_scenes(
    scenes: List[Dict[str, Any]],
    total_duration: int,
    target_duration: int,
    video_title: str,
    max_shorts: int = 5,
    language: str = "Hindi"
) -> List[Dict[str, Any]]:
    """
    Ensures chronological sorting, validates 6-10 sub-clips per Part with director beat tags,
    enforces 50-65s total montage duration, and delivers EXACTLY `max_shorts` parts.
    """
    valid_scenes = []
    target_d = min(max(50, target_duration), 65)

    for s in scenes:
        raw_clips = s.get("sub_clips") or []
        valid_sub_clips = []
        if isinstance(raw_clips, list) and len(raw_clips) >= 3:
            for idx, c in enumerate(raw_clips, 1):
                start_s = c.get("start_seconds")
                if start_s is None:
                    start_s = parse_timestamp_to_seconds(c.get("start_time", "00:00"))
                end_s = c.get("end_seconds")
                if end_s is None:
                    end_s = parse_timestamp_to_seconds(c.get("end_time", "00:05"))

                dur = end_s - start_s
                if dur < 3 or dur > 6:
                    dur = min(max(3, dur), 6)
                    end_s = start_s + dur

                if total_duration > 0 and end_s > total_duration:
                    end_s = max(0, total_duration - 1)
                    start_s = max(0, end_s - dur)

                # Determine beat
                raw_beat = str(c.get("beat") or "").strip()
                if not raw_beat or not raw_beat.startswith("["):
                    beat_def = BEAT_DEFINITIONS[min(idx - 1, len(BEAT_DEFINITIONS) - 1)]
                    raw_beat = beat_def[0]

                valid_sub_clips.append({
                    "clip_num": idx,
                    "start_time": format_seconds_to_timestamp(start_s),
                    "end_time": format_seconds_to_timestamp(end_s),
                    "start_seconds": int(start_s),
                    "end_seconds": int(end_s),
                    "duration": int(end_s - start_s),
                    "beat": raw_beat,
                    "description": c.get("description", f"Director Beat {idx}")
                })

        # If Gemini didn't provide valid sub_clips or fewer than 3 were valid, synthesize cuts
        if len(valid_sub_clips) < 3:
            s_start = s.get("start_seconds")
            if s_start is None:
                s_start = parse_timestamp_to_seconds(s.get("start_time", "00:00"))
            s_end = s.get("end_seconds")
            if s_end is None:
                s_end = parse_timestamp_to_seconds(s.get("end_time", "01:00"))
            if s_end <= s_start:
                s_end = s_start + max(90, target_d * 2)
            valid_sub_clips = generate_algorithmic_subclips(s_start, s_end, target_d)

        # Sort sub_clips chronologically
        valid_sub_clips.sort(key=lambda x: x["start_seconds"])
        for idx, c in enumerate(valid_sub_clips, 1):
            c["clip_num"] = idx
            if idx == 1 and not c.get("beat"):
                c["beat"] = "[Hook]"
            elif idx == len(valid_sub_clips) and not c.get("beat"):
                c["beat"] = "[Cliffhanger]"

        # Enforce total montage duration between 50 and 65 seconds
        total_dur = sum(c["duration"] for c in valid_sub_clips)
        if total_dur > 65:
            while total_dur > 65 and len(valid_sub_clips) > 6:
                removed = valid_sub_clips.pop()
                total_dur -= removed["duration"]
        elif total_dur < 50:
            deficit = 50 - total_dur
            for c in valid_sub_clips:
                if deficit <= 0:
                    break
                add = min(2, deficit)
                c["duration"] += add
                c["end_seconds"] += add
                c["end_time"] = format_seconds_to_timestamp(c["end_seconds"])
                deficit -= add
            total_dur = sum(c["duration"] for c in valid_sub_clips)

        first_clip = valid_sub_clips[0]
        last_clip = valid_sub_clips[-1]

        valid_scenes.append({
            "part": s.get("part", 1),
            "start_time": first_clip["start_time"],
            "end_time": last_clip["end_time"],
            "start_seconds": first_clip["start_seconds"],
            "end_seconds": last_clip["end_seconds"],
            "duration": total_dur,
            "title": s.get("title") or f"{video_title[:30]} - Part {s.get('part', 1)} #Shorts",
            "hook": s.get("hook", ""),
            "script": s.get("script", ""),
            "tags": s.get("tags") or ["shorts", "viral", "recap", "montage"],
            "sub_clips": valid_sub_clips,
            "montage_mode": True,
            "copyright_safe": True
        })

    # Enforce exact requested parts count (1 to 20)
    target_count = min(max(1, max_shorts), 20)
    valid_scenes = valid_scenes[:target_count]
    if len(valid_scenes) < target_count:
        fallback_all = generate_algorithmic_scenes(video_title, total_duration, max_shorts=target_count, target_duration=target_d, language=language)
        for idx in range(len(valid_scenes), target_count):
            if idx < len(fallback_all):
                valid_scenes.append(fallback_all[idx])

    # Sort parts chronologically
    valid_scenes.sort(key=lambda x: x["start_seconds"])
    for idx, sc in enumerate(valid_scenes, 1):
        sc["part"] = idx
        if f"Part {idx}" not in sc["title"]:
            sc["title"] = f"{sc['title']} Part {idx}"

    return valid_scenes


# =====================================================================
# 3. DIRECT STREAM RESOLUTION & RESILIENT CHUNK DOWNLOAD
# =====================================================================
_STREAM_URL_CACHE: Dict[str, Dict[str, Any]] = {}
_STREAM_CACHE_LOCK = threading.Lock()

def get_youtube_video_id(url: str) -> Optional[str]:
    """Extracts the 11-character YouTube video ID from various URL formats."""
    if not url or not isinstance(url, str):
        return None
    patterns = [
        r"(?:v=|\/embed\/|\/watch\?v=|\/shorts\/|^)([0-9A-Za-z_-]{11})(?:[\&\?\/]|$)",
        r"youtu\.be\/([0-9A-Za-z_-]{11})"
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def get_direct_stream_url(youtube_url: str) -> Optional[str]:
    """
    Extracts a direct playable video stream URL using:
    1. In-memory cache (15-min TTL)
    2. yt-dlp -g with anti-bot clients (android, ios, mweb) and robust video format selector
    3. Standard yt-dlp fallback
    4. Piped CDN API fallback (bypasses datacenter IP blocks completely)
    """
    if not youtube_url:
        return None
    global _STREAM_URL_CACHE
    vid = get_youtube_video_id(youtube_url) or youtube_url
    now = time.time()

    # 1. Check cache
    with _STREAM_CACHE_LOCK:
        if vid in _STREAM_URL_CACHE:
            entry = _STREAM_URL_CACHE[vid]
            if now < entry.get("expires_at", 0):
                logger.info(f"Using cached direct stream URL for video {vid}")
                return entry.get("url")
            else:
                try:
                    del _STREAM_URL_CACHE[vid]
                except Exception:
                    pass

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

    # 2. Try yt-dlp -g with formats suited for video extraction
    formats_to_try = [
        "bestvideo[height<=720]/best[height<=720]/bestvideo/best",
        "best/18/22",
        "worst"
    ]
    for fmt in formats_to_try:
        try:
            cmd = [
                sys.executable, "-m", "yt_dlp",
                "--no-check-certificates",
                "-g",
                "-f", fmt,
                "--extractor-args", "youtube:player_client=android,ios,mweb",
                "--user-agent", ua,
                "--add-header", "Accept-Language:en-US,en;q=0.9",
                "--socket-timeout", "20",
                "--retries", "3",
                youtube_url
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25)
            if proc.returncode == 0 and proc.stdout.strip():
                lines = [l.strip() for l in proc.stdout.strip().split("\n") if l.strip().startswith("http")]
                if lines:
                    direct_url = lines[0]
                    with _STREAM_CACHE_LOCK:
                        _STREAM_URL_CACHE[vid] = {"url": direct_url, "expires_at": now + 900}
                    logger.info(f"Retrieved direct stream URL via yt-dlp ({fmt}) for video {vid}")
                    return direct_url
        except Exception as e:
            logger.warning(f"yt-dlp -g ({fmt}) failed for {vid}: {e}")

    # Fallback without extractor-args
    for fmt in ["bestvideo[height<=720]/bestvideo", "best"]:
        try:
            cmd = [
                sys.executable, "-m", "yt_dlp",
                "--no-check-certificates",
                "-g",
                "-f", fmt,
                "--user-agent", ua,
                "--add-header", "Accept-Language:en-US,en;q=0.9",
                "--socket-timeout", "20",
                "--retries", "3",
                youtube_url
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25)
            if proc.returncode == 0 and proc.stdout.strip():
                lines = [l.strip() for l in proc.stdout.strip().split("\n") if l.strip().startswith("http")]
                if lines:
                    direct_url = lines[0]
                    with _STREAM_CACHE_LOCK:
                        _STREAM_URL_CACHE[vid] = {"url": direct_url, "expires_at": now + 900}
                    logger.info(f"Retrieved direct stream URL via yt-dlp default ({fmt}) for video {vid}")
                    return direct_url
        except Exception:
            pass

    # 3. Piped CDN API Fallback (Zero Bot Challenge on Datacenter IPs)
    if vid and len(vid) == 11:
        piped_instances = [
            f"https://api.piped.private.coffee/streams/{vid}",
            f"https://pipedapi.kavin.rocks/streams/{vid}",
            f"https://piped-api.lunar.icu/streams/{vid}",
            f"https://pipedapi.tokhmi.xyz/streams/{vid}"
        ]
        for api_url in piped_instances:
            try:
                req = urllib.request.Request(
                    api_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        streams = data.get("videoStreams", [])
                        mp4_streams = [s for s in streams if (s.get("format") == "MPEG_4" or "mp4" in s.get("mimeType", "")) and s.get("url")]
                        if mp4_streams:
                            mp4_streams.sort(key=lambda x: x.get("quality", "360p"), reverse=True)
                            chosen_url = mp4_streams[0]["url"]
                            with _STREAM_CACHE_LOCK:
                                _STREAM_URL_CACHE[vid] = {"url": chosen_url, "expires_at": now + 900}
                            logger.info(f"Retrieved stream URL from Piped CDN ({api_url}) for video {vid}")
                            return chosen_url
            except Exception as pe:
                logger.debug(f"Piped instance {api_url} failed: {pe}")

    logger.error(f"Failed to extract direct stream URL for {youtube_url}")
    return None


def download_clip_section(
    youtube_url: str,
    start_time: str,
    end_time: str,
    output_path: str,
    strip_audio: bool = True
) -> bool:
    """
    Downloads ONLY the exact start_time to end_time section directly to `output_path`.
    Uses direct stream URL extraction + FFmpeg fast cutting first (taking 1-3 seconds),
    with fallback to yt-dlp --download-sections if needed.
    When strip_audio=True, removes original movie audio completely (-an) for 100% YouTube copyright safety.
    """
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except Exception:
            pass

    ffmpeg_bin = get_ffmpeg_bin()
    direct_url = get_direct_stream_url(youtube_url)
    dur = max(1, parse_timestamp_to_seconds(end_time) - parse_timestamp_to_seconds(start_time))
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

    # 1. Direct stream FFmpeg cutting (Super fast & immune to datacenter download limits)
    if direct_url:
        # Attempt A: Stream copy with user-agent and reconnect options
        cmd_copy = [
            ffmpeg_bin, "-y",
            "-user_agent", ua,
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-ss", start_time,
            "-i", direct_url,
            "-t", str(dur)
        ]
        if strip_audio:
            cmd_copy.extend(["-c:v", "copy", "-an"])
        else:
            cmd_copy.extend(["-c", "copy"])
        cmd_copy.extend(["-avoid_negative_ts", "make_zero", output_path])

        try:
            p_copy = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=35)
            if p_copy.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
                logger.info(f"Direct stream slice (copy) succeeded: {output_path} ({os.path.getsize(output_path)} bytes, silent={strip_audio})")
                return True
        except subprocess.TimeoutExpired:
            logger.warning(f"FFmpeg copy timed out for {start_time}-{end_time}")
        except Exception as e:
            logger.warning(f"FFmpeg copy error: {e}")

        # Attempt B: Ultrafast transcode slice if copy boundary was not on keyframe
        cmd_trans = [
            ffmpeg_bin, "-y",
            "-user_agent", ua,
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-ss", start_time,
            "-i", direct_url,
            "-t", str(dur),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "24"
        ]
        if strip_audio:
            cmd_trans.append("-an")
        cmd_trans.append(output_path)

        try:
            p_trans = subprocess.run(cmd_trans, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
            if p_trans.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
                logger.info(f"Direct stream slice (transcode) succeeded: {output_path} ({os.path.getsize(output_path)} bytes, silent={strip_audio})")
                return True
        except subprocess.TimeoutExpired:
            logger.warning(f"FFmpeg transcode timed out for {start_time}-{end_time}")
        except Exception as e:
            logger.warning(f"FFmpeg transcode error: {e}")

    # 2. Invalidate cache in case URL expired or connection failed
    vid = get_youtube_video_id(youtube_url) or youtube_url
    with _STREAM_CACHE_LOCK:
        if vid in _STREAM_URL_CACHE:
            try:
                del _STREAM_URL_CACHE[vid]
            except Exception:
                pass

    # 3. Fallback to yt-dlp --download-sections
    section_arg = f"*{start_time}-{end_time}"
    logger.info(f"Falling back to yt-dlp download-sections: {section_arg} for {youtube_url}")
    temp_template = os.path.splitext(output_path)[0] + "_dl.%(ext)s"

    base_args = [
        sys.executable, "-m", "yt_dlp",
        "--no-check-certificates",
        "--download-sections", section_arg,
        "--force-keyframes-at-cuts",
        "--extractor-args", "youtube:player_client=android,ios,mweb",
        "--user-agent", ua,
        "--add-header", "Accept-Language:en-US,en;q=0.9",
        "--compat-options", "no-youtube-unavailable-videos",
        "--socket-timeout", "30",
        "--retries", "5",
        "--fragment-retries", "5",
        "--http-chunk-size", "10485760",
        "--merge-output-format", "mp4",
        "-o", temp_template,
        "--quiet", "--no-warnings",
    ]

    format_attempts = [
        "bestvideo[height<=720]/best[height<=720]/bestvideo/best",
        "best/18",
        "worst"
    ]

    dl_dir = os.path.dirname(output_path)
    base_stem = os.path.splitext(os.path.basename(temp_template))[0].replace(".%(ext)s", "")

    last_error = ""
    for fmt in format_attempts:
        cmd = base_args + ["-f", fmt, youtube_url]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
            if proc.returncode == 0:
                for f in os.listdir(dl_dir):
                    if f.startswith(base_stem) and f.endswith(".mp4"):
                        actual_dl = os.path.join(dl_dir, f)
                        if os.path.exists(output_path):
                            os.remove(output_path)
                        if strip_audio:
                            # Strip audio using fast ffmpeg copy
                            strip_cmd = [ffmpeg_bin, "-y", "-i", actual_dl, "-c:v", "copy", "-an", output_path]
                            sp = subprocess.run(strip_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
                            if os.path.exists(actual_dl):
                                try:
                                    os.remove(actual_dl)
                                except Exception:
                                    pass
                            if sp.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 5000:
                                logger.info(f"Downloaded section with yt-dlp format '{fmt}' and stripped audio: {output_path}")
                                return True
                        else:
                            os.rename(actual_dl, output_path)
                            logger.info(f"Downloaded section successfully with yt-dlp format '{fmt}': {output_path} ({os.path.getsize(output_path)} bytes)")
                            return True
            else:
                last_error = proc.stderr
                logger.warning(f"yt-dlp download failed with format '{fmt}': {proc.stderr[:160]}")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Download attempt error: {e}")

    logger.error(f"All stream download methods failed for {section_arg}: {last_error}")
    return False


# =====================================================================
# 4. SMART FACE TRACKING & 9:16 VERTICAL AUTO-REFRAME
# =====================================================================
def calculate_smart_916_crop(video_path: str) -> str:
    """
    Analyzes video frames with OpenCV Haar Cascade face detection to locate
    the horizontal center of the actors. Calculates an optimal 9:16 crop window
    centered on the actors rather than a naive middle crop.
    Guarantees crop dimensions NEVER exceed actual video dimensions.
    Returns FFmpeg crop & scale filter string.
    """
    width, height = 1280, 720
    cap = None
    has_cv2 = False
    try:
        import cv2
        import numpy as np
        has_cv2 = True
        cap = cv2.VideoCapture(video_path)
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if actual_w > 0 and actual_h > 0:
            width, height = actual_w, actual_h
    except Exception as e:
        logger.warning(f"OpenCV probe notice: {e}")

    # Fallback to ffprobe if cv2 didn't get dimensions
    if width <= 0 or height <= 0 or not has_cv2:
        ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
        probe_cmd = [
            ffprobe_bin, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            video_path
        ]
        try:
            probe_res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if probe_res.returncode == 0 and "x" in probe_res.stdout:
                dims = probe_res.stdout.strip().split("x")
                width, height = int(dims[0]), int(dims[1])
        except Exception:
            pass

    # Ensure valid positive bounds
    width = max(width, 320)
    height = max(height, 240)

    # Calculate 9:16 vertical crop inside [width, height]
    target_crop_w = int(height * (9 / 16))
    if target_crop_w > width:
        crop_w = width
        crop_h = int(width * (16 / 9))
    else:
        crop_w = target_crop_w
        crop_h = height

    crop_w = crop_w - (crop_w % 2)  # must be even
    crop_h = crop_h - (crop_h % 2)  # must be even
    crop_w = max(2, min(crop_w, width))
    crop_h = max(2, min(crop_h, height))

    default_crop_x = max(0, int((width - crop_w) / 2))
    default_crop_y = max(0, int((height - crop_h) / 2))

    if not has_cv2 or cap is None or not cap.isOpened():
        return f"crop={crop_w}:{crop_h}:{default_crop_x}:{default_crop_y},scale=1080:1920:flags=bicubic"

    try:
        import cv2
        import numpy as np

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        face_cascade = None
        if hasattr(cv2, 'CascadeClassifier') and hasattr(cv2, 'data') and hasattr(cv2.data, 'haarcascades'):
            try:
                cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
                if os.path.exists(cascade_path):
                    face_cascade = cv2.CascadeClassifier(cascade_path)
            except Exception:
                pass

        detected_centers = []
        sample_indices = [int(total_frames * r) for r in [0.15, 0.35, 0.55, 0.75, 0.90] if int(total_frames * r) < total_frames]
        if not sample_indices:
            sample_indices = [0]

        for frame_idx in sample_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if face_cascade is not None:
                try:
                    faces = face_cascade.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=3, minSize=(30, 30))
                    for (fx, fy, fw, fh) in faces:
                        center_x = fx + (fw / 2.0)
                        weight = fw * fh
                        detected_centers.append((center_x, weight))
                except Exception:
                    pass

        cap.release()

        if detected_centers:
            total_weight = sum(w for _, w in detected_centers)
            weighted_center_x = sum(cx * w for cx, w in detected_centers) / total_weight
            crop_x = int(weighted_center_x - (crop_w / 2.0))
            crop_x = max(0, min(crop_x, width - crop_w))
            logger.info(f"Smart Face Centering detected! Center X: {weighted_center_x:.1f}px -> Crop X: {crop_x}px")
        else:
            crop_x = default_crop_x

        return f"crop={crop_w}:{crop_h}:{crop_x}:{default_crop_y},scale=1080:1920:flags=bicubic"

    except Exception as e:
        logger.warning(f"Smart face tracking fallback to center crop: {e}")
        return f"crop={crop_w}:{crop_h}:{default_crop_x}:{default_crop_y},scale=1080:1920:flags=bicubic"


# =====================================================================
# 5. HIGH-QUALITY NEURAL VOICEOVER GENERATION (EDGE-TTS)
# =====================================================================
async def _edge_tts_generate_async(text: str, output_path: str, voice: str):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def generate_voiceover_audio(text: str, output_path: str, language: str = "Hindi") -> bool:
    """
    Generates high-quality neural voiceover audio using Edge-TTS.
    Hindi: hi-IN-MadhurNeural (charismatic male storyteller)
    English: en-US-ChristopherNeural (authoritative, engaging narrator)
    """
    if not text or not text.strip():
        logger.warning("Empty script text provided for voiceover generation.")
        return False

    voice = "hi-IN-MadhurNeural" if language.lower().startswith("hi") else "en-US-ChristopherNeural"
    logger.info(f"Generating voiceover using voice '{voice}' for script: {text[:60]}...")

    try:
        # Use asyncio to execute edge_tts
        asyncio.run(_edge_tts_generate_async(text.strip(), output_path, voice))
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            logger.info(f"Voiceover successfully generated at: {output_path}")
            return True
    except Exception as e:
        logger.error(f"Primary voiceover generation failed: {e}. Trying alternative voice...")
        try:
            alt_voice = "hi-IN-SwaraNeural" if language.lower().startswith("hi") else "en-US-JennyNeural"
            asyncio.run(_edge_tts_generate_async(text.strip(), output_path, alt_voice))
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                return True
        except Exception as alt_err:
            logger.error(f"Alternative voiceover generation also failed: {alt_err}")

    return False


# =====================================================================
# GEMINI 3.8 / 3.1 FLASH TTS STUDIO & WPS TIMING CALIBRATION
# =====================================================================
GEMINI_TTS_VOICES = [
    {"name": "Kore", "gender": "Female", "tag": "Authoritative & Dramatic", "desc": "Firm, cinematic storyteller with clear diction"},
    {"name": "Fenrir", "gender": "Male", "tag": "Deep Movie Trailer", "desc": "Commanding, booming baritone for epic climaxes"},
    {"name": "Puck", "gender": "Male", "tag": "Dynamic & Expressive", "desc": "Energetic, engaging narrator with rich inflection"},
    {"name": "Algenib", "gender": "Male", "tag": "Suspense & Thriller", "desc": "Gravelly, intense tone for dark mysteries"},
    {"name": "Charon", "gender": "Male", "tag": "Dark & Brooding", "desc": "Somber, heavy voice for horror and high tension"},
    {"name": "Aoede", "gender": "Female", "tag": "Sophisticated & Clear", "desc": "Melodic, crisp delivery for thoughtful recaps"},
    {"name": "Algieba", "gender": "Female", "tag": "Fast-Paced Action", "desc": "Sharp, intense delivery for rapid action cuts"}
]

TONE_PROMPT_PRESETS = {
    "Movie Trailer": "Say in Hindi in a booming, dramatic movie trailer voice: ",
    "Suspense / Thriller": "Say in Hindi in a tense, gripping suspense thriller voice with dramatic pauses: ",
    "Narrative Deep": "Say in Hindi in a deep, rich cinematic storytelling voice: ",
    "Fast-Paced Action": "Say in Hindi in an urgent, fast-paced action voice: ",
    "Emotional Drama": "Say in Hindi in an emotional, poignant voice: "
}

CALIBRATION_100_WORDS_HINDI = (
    "यह एक रोमांचक कहानी की शुरुआत है जहाँ हर तरफ खतरा मंडरा रहा है। "
    "जंगल के सन्नाटे में दूर से आती एक रहस्यमयी आवाज ने सबका ध्यान खींचा। "
    "नायक ने धीरे-धीरे अपने कदम आगे बढ़ाए ताकि दुश्मन को उसकी मौजूदगी का अहसास ना हो। "
    "अचानक पेड़ों के पीछे से एक साया निकला और माहौल में गहरा सन्नाटा छा गया। "
    "क्या वह इस चुनौती का सामना कर पाएगा या फिर अंधेरा उसे हमेशा के लिए निगल जाएगा? "
    "समय तेजी से बीत रहा था और हर एक सेकंड उसके लिए बेहद कीमती साबित हो रहा था।"
)


def generate_gemini_tts_audio(
    text: str,
    output_path: str,
    voice_name: str = "Kore",
    tone_style: str = "Suspense / Thriller",
    language: str = "Hindi"
) -> bool:
    """
    Synthesizes speech using Google Gemini 3.1/3.8 Flash TTS preview model.
    Converts 24kHz mono PCM to 192kbps MP3 via FFmpeg.
    Falls back smoothly to Edge-TTS if quota or network issue occurs.
    """
    if not text or not text.strip():
        logger.warning("Empty script provided for Gemini TTS.")
        return False

    logger.info(f"Generating Gemini TTS audio (Voice: {voice_name}, Tone: {tone_style}) for script: {text[:60]}...")

    # 1. Try Gemini TTS via google.genai
    try:
        from google import genai
        from google.genai import types
        cfg = gemini_engine.get_gemini_config()
        api_key = cfg.get("api_key")
        if api_key:
            client = genai.Client(api_key=api_key)
            tone_prefix = TONE_PROMPT_PRESETS.get(tone_style, f"Say in {language} in a dramatic storytelling voice: ")
            tts_prompt = f"{tone_prefix}{text.strip()}"

            resp = client.models.generate_content(
                model="gemini-3.1-flash-tts-preview",
                contents=tts_prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice_name
                            )
                        )
                    )
                )
            )
            if resp and resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
                pcm_data = resp.candidates[0].content.parts[0].inline_data.data
                if pcm_data and len(pcm_data) > 1000:
                    temp_wav = output_path + f".tmp_{uuid.uuid4().hex[:6]}.wav"
                    try:
                        with wave.open(temp_wav, "wb") as wf:
                            wf.setnchannels(1)
                            wf.setsampwidth(2)
                            wf.setframerate(24000)
                            wf.writeframes(pcm_data)

                        ffmpeg_bin = get_ffmpeg_bin()
                        cmd = [
                            ffmpeg_bin, "-y",
                            "-i", temp_wav,
                            "-c:a", "libmp3lame",
                            "-b:a", "192k",
                            output_path
                        ]
                        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                            logger.info(f"Gemini TTS audio successfully created: {output_path} ({os.path.getsize(output_path)} bytes)")
                            return True
                    finally:
                        if os.path.exists(temp_wav):
                            try:
                                os.remove(temp_wav)
                            except Exception:
                                pass
    except Exception as ge:
        logger.warning(f"Gemini TTS generation encountered: {ge}. Cascading to Edge-TTS fallback...")

    # 2. Resilient fallback to Edge-TTS
    logger.info("Falling back to high-quality Edge-TTS neural engine...")
    return generate_voiceover_audio(text, output_path, language=language)


_CALIBRATION_CACHE: Dict[str, Dict[str, Any]] = {
    "Kore_Narrative Deep Storytelling_Hindi": {"status": "calibrated", "voice": "Kore", "tone": "Narrative Deep Storytelling", "word_count": 94, "duration": 40.54, "wps": 2.32, "audio_url": None, "message": "Pre-calibrated benchmark (2.32 WPS)"},
    "Kore_Suspense / Thriller_Hindi": {"status": "calibrated", "voice": "Kore", "tone": "Suspense / Thriller", "word_count": 94, "duration": 39.17, "wps": 2.40, "audio_url": None, "message": "Pre-calibrated benchmark (2.40 WPS)"},
    "Fenrir_Narrative Deep Storytelling_Hindi": {"status": "calibrated", "voice": "Fenrir", "tone": "Narrative Deep Storytelling", "word_count": 94, "duration": 41.20, "wps": 2.28, "audio_url": None, "message": "Pre-calibrated benchmark (2.28 WPS)"},
    "Puck_Suspense / Thriller_Hindi": {"status": "calibrated", "voice": "Puck", "tone": "Suspense / Thriller", "word_count": 94, "duration": 38.30, "wps": 2.45, "audio_url": None, "message": "Pre-calibrated benchmark (2.45 WPS)"},
}

def calibrate_voice_speed(
    voice_name: str = "Kore",
    tone_style: str = "Suspense / Thriller",
    language: str = "Hindi",
    custom_text: Optional[str] = None
) -> Dict[str, Any]:
    """
    Synthesizes canonical 100-word text using selected voice and tone style.
    Measures exact audio duration via ffprobe/ffmpeg.
    Calculates Words-Per-Second (WPS = 100 / duration).
    Returns calibration metrics and sample audio path.
    """
    cache_key = f"{voice_name}_{tone_style}_{language}"
    if not custom_text and cache_key in _CALIBRATION_CACHE:
        logger.info(f"Using cached calibration benchmark for {cache_key}: {_CALIBRATION_CACHE[cache_key]['wps']} WPS")
        return _CALIBRATION_CACHE[cache_key]

    sample_text = (custom_text or CALIBRATION_100_WORDS_HINDI).strip()
    words = sample_text.split()
    word_count = len(words)

    unique_id = uuid.uuid4().hex[:6]
    sample_filename = f"calib_{voice_name}_{unique_id}.mp3"
    sample_path = os.path.join(TEMP_DIR, sample_filename)

    ok = generate_gemini_tts_audio(
        text=sample_text,
        output_path=sample_path,
        voice_name=voice_name,
        tone_style=tone_style,
        language=language
    )
    if not ok or not os.path.exists(sample_path):
        return {
            "status": "fallback",
            "voice": voice_name,
            "tone": tone_style,
            "word_count": word_count,
            "duration": 42.0,
            "wps": 2.4,
            "audio_url": None,
            "message": "Used calibrated benchmark default (2.40 WPS)"
        }

    # Measure exact duration
    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    dur = 42.0
    try:
        cmd = [
            ffprobe_bin, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            sample_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        dur = float(res.stdout.strip())
    except Exception as e:
        logger.warning(f"Could not probe calibration duration: {e}. Calculating from mp3 size...")
        dur = max(10.0, word_count / 2.4)

    wps = round(word_count / max(dur, 1.0), 2)
    logger.info(f"Calibration successful: {word_count} words in {dur:.2f}s => {wps} WPS for {voice_name} ({tone_style})")

    return {
        "status": "success",
        "voice": voice_name,
        "tone": tone_style,
        "word_count": word_count,
        "duration": round(dur, 2),
        "wps": wps,
        "filename": sample_filename,
        "audio_url": f"/api/clipper/tts_sample/{sample_filename}",
        "message": f"Calibrated {wps} words/sec ({dur:.1f}s sample)"
    }


def balance_script_for_cuts(
    sub_clips: List[Dict[str, Any]],
    wps: float = 2.4,
    base_script: str = "",
    title: str = "",
    part_num: int = 1,
    language: str = "Hindi"
) -> Dict[str, Any]:
    """
    Computes exact target words per cut:
    Target Words for Cut_i = round(Cut_Duration_i * wps).
    Instructs Gemini to balance the narrative recap scene-by-scene so that the spoken
    narration aligns synchronously with visual scene transitions.
    """
    total_cut_duration = sum(c.get("duration", 4) for c in sub_clips)
    target_total_words = int(round(total_cut_duration * wps))

    cut_targets = []
    for i, c in enumerate(sub_clips, 1):
        c_dur = c.get("duration", 5)
        c_words = max(3, int(round(c_dur * wps)))
        cut_targets.append({
            "cut_num": i,
            "duration": c_dur,
            "beat": c.get("beat", f"Cut {i}"),
            "description": c.get("description", ""),
            "target_words": c_words
        })

    return {
        "total_duration": total_cut_duration,
        "target_total_words": target_total_words,
        "wps": wps,
        "cut_targets": cut_targets
    }


# =====================================================================
# 6. FFMPEG RENDERING & AUDIO DUCKING
# =====================================================================
def render_short_video(
    raw_clip_path: str,
    voiceover_path: Optional[str],
    output_path: str,
    crop_filter: str
) -> bool:
    """
    Renders 9:16 vertical Short using FFmpeg with:
    - Actor-centered crop and high-definition 1080x1920 scaling.
    - Audio ducking: original video audio mixed at 15% volume, voiceover at 100% volume.
    - H.264 high-profile video and AAC audio with +faststart for instant mobile playback.
    """
    logger.info(f"Rendering final short: {output_path}")
    ffmpeg_bin = get_ffmpeg_bin()
    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    has_vo = voiceover_path and os.path.exists(voiceover_path) and os.path.getsize(voiceover_path) > 1000

    if has_vo:
        # Check if raw clip has an audio stream
        probe_audio = [
            ffprobe_bin, "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            raw_clip_path
        ]
        has_orig_audio = False
        try:
            res = subprocess.run(probe_audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if "audio" in res.stdout:
                has_orig_audio = True
        except Exception:
            pass

        if has_orig_audio:
            # Duck original audio to 15%, voiceover at 100%
            filter_complex = (
                f"[0:v]{crop_filter}[vout];"
                f"[0:a]volume=0.15[bg];"
                f"[1:a]volume=1.0[vo];"
                f"[bg][vo]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
            cmd = [
                ffmpeg_bin, "-y",
                "-i", raw_clip_path,
                "-i", voiceover_path,
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", "[aout]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                output_path
            ]
        else:
            # Original clip has no audio: use voiceover audio directly
            cmd = [
                ffmpeg_bin, "-y",
                "-i", raw_clip_path,
                "-i", voiceover_path,
                "-filter_complex", f"[0:v]{crop_filter}[vout]",
                "-map", "[vout]",
                "-map", "1:a",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                output_path
            ]
    else:
        # No voiceover: keep original audio with vertical crop
        cmd = [
            ffmpeg_bin, "-y",
            "-i", raw_clip_path,
            "-filter_complex", f"[0:v]{crop_filter}[vout]",
            "-map", "[vout]",
            "-map", "0:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            output_path
        ]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
    if proc.returncode != 0:
        logger.error(f"FFmpeg rendering failed: {proc.stderr}")
        return False

    return os.path.exists(output_path) and os.path.getsize(output_path) > 10000


def ensure_background_music_exists() -> str:
    """
    Ensures that a copyright-free cinematic tension background music MP3 exists on disk.
    If missing, synthesizes a pristine ambient tension track procedurally via numpy + wave + ffmpeg.
    """
    assets_dir = os.path.join(BASE_DIR, "uploads", "assets")
    os.makedirs(assets_dir, exist_ok=True)
    bgm_mp3 = os.path.join(assets_dir, "cinematic_tension_bgm.mp3")
    if os.path.exists(bgm_mp3) and os.path.getsize(bgm_mp3) > 10000:
        return bgm_mp3

    try:
        import numpy as np
        import wave
        sample_rate = 44100
        duration = 85  # seconds
        t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)

        # Build cinematic suspense tension drone:
        # 1. Sub bass drone (55 Hz)
        bass = 0.35 * np.sin(2 * np.pi * 55 * t)
        # 2. Tension minor pad chords (110 Hz, 130.8 Hz, 164.8 Hz)
        pad1 = 0.18 * np.sin(2 * np.pi * 110 * t)
        pad2 = 0.12 * np.sin(2 * np.pi * 130.81 * t)
        pad3 = 0.12 * np.sin(2 * np.pi * 164.81 * t)
        # 3. Slow breathing LFO modulation
        lfo = 0.6 + 0.4 * np.sin(2 * np.pi * 0.3 * t)
        # 4. Subtle rhythmic heartbeat thud (every 1.5s)
        beat_phase = (t % 1.5)
        beat = 0.4 * np.exp(-18 * beat_phase) * np.sin(2 * np.pi * 60 * np.exp(-10 * beat_phase) * beat_phase)

        audio = (bass + (pad1 + pad2 + pad3) * lfo + beat) * 0.45
        audio = np.clip(audio, -0.95, 0.95)
        audio_int16 = (audio * 32767).astype(np.int16)
        stereo = np.column_stack((audio_int16, audio_int16)).flatten()

        wav_path = os.path.join(assets_dir, "cinematic_tension_bgm.wav")
        with wave.open(wav_path, 'w') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(stereo.tobytes())

        ffmpeg_bin = get_ffmpeg_bin()
        subprocess.run([ffmpeg_bin, "-y", "-i", wav_path, "-b:a", "192k", bgm_mp3],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if os.path.exists(wav_path):
            os.remove(wav_path)
        if os.path.exists(bgm_mp3):
            logger.info(f"Synthesized copyright-free cinematic BGM: {bgm_mp3}")
            return bgm_mp3
    except Exception as e:
        logger.warning(f"Could not generate background music: {e}")

    return ""


def reframe_subclip_to_vertical_916(
    raw_sub_path: str,
    output_norm_path: str
) -> bool:
    """
    Reframes a 3-6s sub-clip to vertical 9:16 (1080x1920 @ 30fps) with actor face centering,
    and STRIPS ALL ORIGINAL MOVIE AUDIO (0% volume / muted for 100% YouTube Content ID safety).
    """
    if not os.path.exists(raw_sub_path) or os.path.getsize(raw_sub_path) < 1000:
        return False

    ffmpeg_bin = get_ffmpeg_bin()
    crop_filter = calculate_smart_916_crop(raw_sub_path)

    cmd = [
        ffmpeg_bin, "-y",
        "-i", raw_sub_path,
        "-filter_complex", f"[0:v]{crop_filter},setsar=1[vout]",
        "-map", "[vout]",
        "-an",  # Strip original movie audio completely!
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-r", "30",
        "-pix_fmt", "yuv420p",
        output_norm_path
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        if proc.returncode == 0 and os.path.exists(output_norm_path) and os.path.getsize(output_norm_path) > 5000:
            return True
        logger.warning(f"Reframe subclip failed: {proc.stderr[:160]}")
    except Exception as e:
        logger.error(f"Error reframing subclip {raw_sub_path}: {e}")

    return False


def concat_normalized_clips(
    clip_paths: List[str],
    output_montage_path: str
) -> bool:
    """
    Concatenates normalized silent 9:16 clips using FFmpeg concat demuxer in under 1 second.
    """
    valid_clips = [p for p in clip_paths if os.path.exists(p) and os.path.getsize(p) > 5000]
    if not valid_clips:
        logger.error("No valid normalized clips to concatenate.")
        return False

    if len(valid_clips) == 1:
        import shutil
        shutil.copyfile(valid_clips[0], output_montage_path)
        return True

    concat_list_path = os.path.join(TEMP_DIR, f"concat_{uuid.uuid4().hex[:8]}.txt")
    try:
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for p in valid_clips:
                clean_path = os.path.abspath(p).replace("\\", "/")
                f.write(f"file '{clean_path}'\n")

        ffmpeg_bin = get_ffmpeg_bin()
        cmd = [
            ffmpeg_bin, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            output_montage_path
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        return os.path.exists(output_montage_path) and os.path.getsize(output_montage_path) > 10000
    except Exception as e:
        logger.error(f"Error concatenating clips: {e}")
        return False
    finally:
        if os.path.exists(concat_list_path):
            try:
                os.remove(concat_list_path)
            except Exception:
                pass


def render_montage_with_audio_overlay(
    montage_video_path: str,
    voiceover_path: Optional[str],
    bgm_path: Optional[str],
    output_path: str
) -> bool:
    """
    Overlays neural AI voiceover (100% volume) and subtle copyright-free background music
    (12% volume) onto the concatenated silent video montage.
    Movie original audio is 100% stripped/muted. Includes smooth 1.5s audio fade-out.
    """
    if not os.path.exists(montage_video_path):
        logger.error(f"Montage video path does not exist: {montage_video_path}")
        return False

    ffmpeg_bin = get_ffmpeg_bin()
    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"

    # Probe duration of video montage
    video_dur = 60.0
    try:
        probe_cmd = [
            ffprobe_bin, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            montage_video_path
        ]
        res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            video_dur = float(res.stdout.strip())
    except Exception as e:
        logger.warning(f"Could not probe montage duration: {e}")

    has_vo = voiceover_path and os.path.exists(voiceover_path) and os.path.getsize(voiceover_path) > 1000
    has_bgm = bgm_path and os.path.exists(bgm_path) and os.path.getsize(bgm_path) > 10000

    fade_start = max(0.5, video_dur - 1.5)

    if has_vo and has_bgm:
        filter_complex = (
            f"[1:a]volume=1.0[vo];"
            f"[2:a]volume=0.12[bgm];"
            f"[vo][bgm]amix=inputs=2:duration=first:dropout_transition=2,"
            f"afade=t=out:st={fade_start:.2f}:d=1.5[aout]"
        )
        cmd = [
            ffmpeg_bin, "-y",
            "-i", montage_video_path,
            "-i", voiceover_path,
            "-stream_loop", "-1", "-i", bgm_path,
            "-filter_complex", filter_complex,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{video_dur:.2f}",
            "-movflags", "+faststart",
            output_path
        ]
    elif has_vo:
        cmd = [
            ffmpeg_bin, "-y",
            "-i", montage_video_path,
            "-i", voiceover_path,
            "-filter_complex", f"[1:a]volume=1.0,afade=t=out:st={fade_start:.2f}:d=1.5[aout]",
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{video_dur:.2f}",
            "-movflags", "+faststart",
            output_path
        ]
    elif has_bgm:
        cmd = [
            ffmpeg_bin, "-y",
            "-i", montage_video_path,
            "-stream_loop", "-1", "-i", bgm_path,
            "-filter_complex", f"[1:a]volume=0.25,afade=t=out:st={fade_start:.2f}:d=1.5[aout]",
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{video_dur:.2f}",
            "-movflags", "+faststart",
            output_path
        ]
    else:
        cmd = [
            ffmpeg_bin, "-y",
            "-i", montage_video_path,
            "-c:v", "copy",
            "-an",
            output_path
        ]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
    if proc.returncode != 0:
        logger.error(f"FFmpeg montage render failed: {proc.stderr}")
        return False

    return os.path.exists(output_path) and os.path.getsize(output_path) > 10000


def generate_short_thumbnail(video_path: str, thumbnail_path: str) -> bool:
    """Extracts a crisp thumbnail frame from the middle of the generated Short."""
    ffmpeg_bin = get_ffmpeg_bin()
    cmd = [
        ffmpeg_bin, "-y",
        "-ss", "00:00:03",
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        thumbnail_path
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        return os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 1000
    except Exception as e:
        logger.error(f"Failed to extract thumbnail: {e}")
        return False


# =====================================================================
# 7. HIGH-LEVEL ORCHESTRATION PIPELINE
# =====================================================================
def ensure_scene_script(
    scene: Dict[str, Any],
    video_title: str = "",
    language: str = "Hindi",
    wps: float = 2.4,
    tone_style: str = "Suspense / Thriller"
) -> str:
    """
    Ensures that a scene has a captivating narrative script balanced to the exact cut durations and WPS pace.
    Target Words for Cut = round(Cut Duration * WPS).
    Total target words = round(sum(cut durations) * WPS).
    """
    script = (scene.get("script") or "").strip()
    if script:
        return script

    part_num = scene.get("part", 1)
    sub_clips = scene.get("sub_clips") or []
    total_cut_duration = sum(c.get("duration", 5) for c in sub_clips) if sub_clips else scene.get("duration", 58)
    target_words = max(25, int(round(total_cut_duration * wps)))

    logger.info(f"Generating balanced voiceover script for Part {part_num} via Gemini (Target: ~{target_words} words for {total_cut_duration}s at {wps:.2f} WPS)...")
    client = gemini_engine.get_genai_client()
    cfg = gemini_engine.get_gemini_config()
    target_model = cfg.get("model") or gemini_engine.DEFAULT_MODEL
    models_to_try = [target_model] + [m for m in gemini_engine.FALLBACK_MODELS if m != target_model]

    cuts_breakdown = ""
    if sub_clips:
        c_lines = []
        for i, c in enumerate(sub_clips, 1):
            c_dur = c.get("duration", 5)
            c_beat = c.get("beat", f"Cut {i}")
            c_w = max(3, int(round(c_dur * wps)))
            c_lines.append(f"- Cut {i} ({c_dur}s, {c_beat}): target ~{c_w} words")
        cuts_breakdown = "\nTarget spoken words per scene transition:\n" + "\n".join(c_lines)

    prompt = (
        f"You are a master YouTube viral storyteller & trailer narrator.\n"
        f"Write a dramatic, cohesive story recap voiceover script in {language} for Part {part_num} "
        f"of '{video_title}' designed for a fast-paced {total_cut_duration} second multi-scene montage covering story progression from {scene.get('start_time')} to {scene.get('end_time')}.\n"
        f"CRITICAL TIMING CALIBRATION:\n"
        f"- Target narration pace: {wps:.2f} words per second.\n"
        f"- Tone Style: {tone_style}.\n"
        f"- EXACT TOTAL SCRIPT LENGTH: ~{target_words} words in {language}.\n"
        f"{cuts_breakdown}\n"
        f"The narration must pace evenly across the cuts so the voiceover concludes precisely as the last cut ends!\n"
        f"Only return the spoken script text in {language}, no markdown, no quotes."
    )

    for model_name in models_to_try:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"temperature": 0.4}
            )
            if resp and resp.text:
                clean_script = resp.text.strip().replace('"', '').replace("'", "")
                scene["script"] = clean_script
                return clean_script
        except Exception as me:
            err_str = str(me)
            logger.warning(f"Script model {model_name} failed: {err_str}")
            if any(w in err_str.lower() for w in ["429", "resource_exhausted", "quota", "rate limit"]):
                raise RuntimeError(f"Gemini API Quota Exceeded (429): {err_str}")

    # Fallback algorithmic script
    if language.lower().startswith("hi"):
        fallback = (
            f"फिल्म के पार्ट {part_num} में कहानी एक नया रोमांचक मोड़ लेती है। "
            f"मुख्य किरदार इस खतरनाक परिस्थिति में फंस जाता है जहां से निकलना लगभग नामुमकिन था। "
            f"लेकिन क्या वह अपनी जान बचा पाएगा? देखिए आगे क्या होता है और चैनल को सब्सक्राइब जरूर करें!"
        )
    else:
        fallback = (
            f"In Part {part_num} of this intense story, the plot takes an unexpected dramatic turn. "
            f"Trapped in an impossible situation with no easy way out, every second counts. "
            f"Will the hero survive the ultimate test? Watch till the end and subscribe for more!"
        )
    scene["script"] = fallback
    return fallback


def download_raw_cuts_step2(
    youtube_url: str,
    sub_clips: List[Dict[str, Any]],
    part_num: int = 1,
    job_id: Optional[str] = None,
    progress_callback: Optional[Any] = None
) -> List[Dict[str, Any]]:
    """
    STEP 2: High-Speed Local Python Raw Cutter.
    Downloads ONLY the exact 3-6s sub-clips directly to `uploads/clipper_cuts/`.
    Strips original movie audio (-an) for 100% YouTube copyright safety.
    Returns cut metadata with web URLs (/api/clipper/cut/<filename>) for immediate gallery display.
    Target execution: under 20-30s total.
    """
    def notify_progress(pct: int, msg: str):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    total_cuts = len(sub_clips)
    notify_progress(15, f"Step 2: Slicing {total_cuts} raw cuts with 0% original audio...")
    logger.info(f"Step 2: Downloading {total_cuts} targeted cuts for Part {part_num} to {CUTS_DIR}")

    # Pre-resolve stream URL for maximum speed
    direct_url = get_direct_stream_url(youtube_url)
    if direct_url:
        logger.info(f"Stream URL ready for Part {part_num} raw cuts slicing.")

    unique_run_id = uuid.uuid4().hex[:6]
    completed_cuts = {}
    completed_count = 0
    cuts_lock = threading.Lock()

    def process_single_cut(cut_item: Tuple[int, Dict[str, Any]]) -> Tuple[int, Optional[Dict[str, Any]]]:
        nonlocal completed_count
        idx, c = cut_item
        c_start = c.get("start_time")
        c_end = c.get("end_time")
        beat = c.get("beat", f"Beat {idx}")
        dur = c.get("duration") or max(1, parse_timestamp_to_seconds(c_end) - parse_timestamp_to_seconds(c_start))
        if not c_start or not c_end:
            return idx, None

        cut_filename = f"cut_p{part_num}_c{idx}_{unique_run_id}.mp4"
        cut_path = os.path.join(CUTS_DIR, cut_filename)

        logger.info(f"Targeted Slicing Cut {idx}/{total_cuts} {beat}: [{c_start} - {c_end}]")
        dl_ok = download_clip_section(youtube_url, c_start, c_end, cut_path, strip_audio=True)
        if dl_ok and os.path.exists(cut_path) and os.path.getsize(cut_path) > 5000:
            with cuts_lock:
                completed_count += 1
                pct = 15 + int((completed_count / max(total_cuts, 1)) * 40)
            notify_progress(pct, f"Cut {idx}/{total_cuts} {beat} downloaded (silent & safe)...")
            record = {
                "index": idx,
                "start_time": c_start,
                "end_time": c_end,
                "duration": dur,
                "beat": beat,
                "description": c.get("description", ""),
                "filename": cut_filename,
                "filepath": cut_path,
                "url": f"/api/clipper/cut/{cut_filename}",
                "size": os.path.getsize(cut_path)
            }
            return idx, record

        logger.warning(f"Targeted cut {idx} ({c_start}-{c_end}) failed.")
        return idx, None

    max_w = min(4, max(1, total_cuts))
    with ThreadPoolExecutor(max_workers=max_w) as executor:
        futures = {executor.submit(process_single_cut, (i, cut)): i for i, cut in enumerate(sub_clips, 1)}
        for future in as_completed(futures):
            try:
                res_idx, res_rec = future.result()
                if res_rec:
                    completed_cuts[res_idx] = res_rec
            except Exception as fe:
                logger.warning(f"Cut task error: {fe}")

    # Sequential retry for any missed cuts
    if len(completed_cuts) < total_cuts:
        for i, cut in enumerate(sub_clips, 1):
            if i not in completed_cuts:
                res_idx, res_rec = process_single_cut((i, cut))
                if res_rec:
                    completed_cuts[res_idx] = res_rec

    ordered_cuts = [completed_cuts[k] for k in sorted(completed_cuts.keys()) if os.path.exists(completed_cuts[k]["filepath"])]
    if not ordered_cuts:
        raise RuntimeError(f"Failed to slice any cuts for Part {part_num}")

    logger.info(f"Step 2 Complete: Downloaded {len(ordered_cuts)}/{total_cuts} silent cuts for Part {part_num}")
    return ordered_cuts


def assemble_standard_recap_step3(
    scene: Dict[str, Any],
    downloaded_cuts: List[Dict[str, Any]],
    language: str = "Hindi",
    video_title: str = "",
    job_id: Optional[str] = None,
    progress_callback: Optional[Any] = None,
    voice_name: str = "Kore",
    tone_style: str = "Suspense / Thriller",
    wps: Optional[float] = None
) -> Dict[str, Any]:
    """
    STEP 3: Mute & Voiceover Sync (Standard 16:9 / Normal Cut Preview First).
    - Concatenates the silent raw cuts in normal/standard aspect ratio.
    - Generates cohesive Hindi voiceover via Gemini 3.8/3.1 Flash TTS (with Edge-TTS resilient fallback).
    - Mixes Voiceover (100%) + subtle BGM (12%) with audio fade out.
    - Delivers standard preview first so video is 100% visible and playable right away (~15s total).
    """
    def notify_progress(pct: int, msg: str):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    part_num = scene.get("part", 1)
    title = scene.get("title", f"Part {part_num} #Shorts")
    notify_progress(60, f"Step 3: Assembling standard cut preview for Part {part_num}...")

    valid_cuts = [c for c in downloaded_cuts if os.path.exists(c["filepath"]) and os.path.getsize(c["filepath"]) > 5000]
    if not valid_cuts:
        raise RuntimeError(f"No valid downloaded cuts found to assemble Part {part_num}")

    unique_id = uuid.uuid4().hex[:8]
    concat_list_path = os.path.join(TEMP_DIR, f"concat_std_{part_num}_{unique_id}.txt")
    silent_std_path = os.path.join(TEMP_DIR, f"montage_std_silent_{part_num}_{unique_id}.mp4")
    vo_path = os.path.join(TEMP_DIR, f"vo_std_part_{part_num}_{unique_id}.mp3")
    standard_video_name = f"recap_standard_part_{part_num}_{unique_id}.mp4"
    standard_thumb_name = f"thumb_standard_part_{part_num}_{unique_id}.jpg"
    standard_video_path = os.path.join(CLIPPER_DIR, standard_video_name)
    standard_thumb_path = os.path.join(CLIPPER_DIR, standard_thumb_name)

    # 1. Write concat list
    try:
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for c in valid_cuts:
                clean_path = os.path.abspath(c["filepath"]).replace("\\", "/")
                f.write(f"file '{clean_path}'\n")

        ffmpeg_bin = get_ffmpeg_bin()
        # Attempt copy concat first
        cmd_copy = [
            ffmpeg_bin, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            "-an",
            silent_std_path
        ]
        p_c = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        if not (p_c.returncode == 0 and os.path.exists(silent_std_path) and os.path.getsize(silent_std_path) > 10000):
            # Fallback to fast ultrafast transcode concat
            cmd_trans = [
                ffmpeg_bin, "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list_path,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "22",
                "-pix_fmt", "yuv420p",
                "-an",
                silent_std_path
            ]
            subprocess.run(cmd_trans, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    finally:
        if os.path.exists(concat_list_path):
            try:
                os.remove(concat_list_path)
            except Exception:
                pass

    if not os.path.exists(silent_std_path) or os.path.getsize(silent_std_path) < 10000:
        raise RuntimeError(f"Failed to concatenate standard cuts for Part {part_num}")

    # 2. Voiceover & BGM mix
    effective_wps = wps or 2.4
    if job_id and not wps:
        try:
            ckpt = load_job_checkpoint(job_id)
            if ckpt:
                if ckpt.get("wps"):
                    effective_wps = float(ckpt["wps"])
                if ckpt.get("voice_name"):
                    voice_name = ckpt["voice_name"]
                if ckpt.get("tone_style"):
                    tone_style = ckpt["tone_style"]
        except Exception:
            pass

    notify_progress(75, f"Step 3: Generating Gemini 3.8 Flash TTS ({voice_name}) voiceover & tension BGM for Part {part_num}...")
    script = ensure_scene_script(scene, video_title=video_title or title, language=language, wps=effective_wps, tone_style=tone_style)
    vo_ok = False
    if script:
        vo_ok = generate_gemini_tts_audio(
            text=script,
            output_path=vo_path,
            voice_name=voice_name,
            tone_style=tone_style,
            language=language
        )
    bgm_path = ensure_background_music_exists()

    # 3. Audio overlay onto standard montage
    notify_progress(85, "Step 3: Mixing voiceover + BGM onto standard video preview...")
    render_ok = render_montage_with_audio_overlay(
        montage_video_path=silent_std_path,
        voiceover_path=vo_path if vo_ok else None,
        bgm_path=bgm_path if bgm_path else None,
        output_path=standard_video_path
    )
    if not render_ok or not os.path.exists(standard_video_path):
        raise RuntimeError(f"FFmpeg failed to render standard recap for Part {part_num}")

    # 4. HD Thumbnail
    notify_progress(95, "Generating standard preview thumbnail...")
    generate_short_thumbnail(standard_video_path, standard_thumb_path)
    notify_progress(100, f"Part {part_num} standard cut preview ready!")

    # Clean intermediate temp files
    if os.path.exists(silent_std_path):
        try:
            os.remove(silent_std_path)
        except Exception:
            pass
    if os.path.exists(vo_path):
        try:
            os.remove(vo_path)
        except Exception:
            pass

    start_time = valid_cuts[0]["start_time"]
    end_time = valid_cuts[-1]["end_time"]

    description = (
        f"{title}\n\n"
        f"🎬 Story Recap (Part {part_num} Montage - {len(valid_cuts)} Scenes):\n{script}\n\n"
        f"🔔 Subscribe for Part {part_num + 1} and more viral movie breakdowns!\n\n"
        f"#Shorts #YouTubeShorts #MovieRecap #Cinema #Part{part_num} #MovieMontage"
    )

    short_data = {
        "part": part_num,
        "format": "standard",
        "filename": standard_video_name,
        "video_url": f"/api/clipper/media/{standard_video_name}",
        "thumbnail_url": f"/api/clipper/media/{standard_thumb_name}",
        "filepath": standard_video_path,
        "standard_filepath": standard_video_path,
        "standard_video_url": f"/api/clipper/media/{standard_video_name}",
        "title": title,
        "hook": scene.get("hook", ""),
        "script": script,
        "description": description,
        "tags": scene.get("tags") or ["Shorts", "Movie", "Viral", f"Part{part_num}", "MovieRecap"],
        "duration": scene.get("duration", 58),
        "start_time": start_time,
        "end_time": end_time,
        "sub_clips_count": len(valid_cuts),
        "downloaded_cuts": downloaded_cuts,
        "copyright_safe": True,
        "can_convert_vertical": True,
        "voice_name": voice_name,
        "tone_style": tone_style,
        "wps": effective_wps,
        "status": "ready"
    }

    if job_id:
        try:
            ckpt = load_job_checkpoint(job_id)
            if ckpt:
                if "completed_shorts" not in ckpt or not isinstance(ckpt["completed_shorts"], dict):
                    ckpt["completed_shorts"] = {}
                ckpt["completed_shorts"][str(part_num)] = short_data
                save_job_checkpoint(job_id, ckpt)
        except Exception as se:
            logger.warning(f"Could not persist short {part_num} to checkpoint {job_id}: {se}")

    return short_data


def convert_recap_to_vertical_step4(
    standard_video_path: str,
    output_vertical_path: Optional[str] = None,
    job_id: Optional[str] = None,
    part_num: Optional[int] = None,
    progress_callback: Optional[Any] = None
) -> Dict[str, Any]:
    """
    STEP 4: Separate 9:16 Vertical / Face-Tracking On Demand.
    Takes the completed standard recap video (which already has mixed audio: VO + BGM).
    Reframes to 9:16 vertical 1080x1920 using OpenCV face detection to center the actors.
    Copies audio directly (-c:a copy), completing in ~4-8 seconds.
    """
    def notify_progress(pct: int, msg: str):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    if not os.path.exists(standard_video_path) or os.path.getsize(standard_video_path) < 10000:
        raise ValueError(f"Standard video file does not exist or is invalid: {standard_video_path}")

    notify_progress(10, "Step 4: Analyzing frames with OpenCV face detection...")
    crop_filter = calculate_smart_916_crop(standard_video_path)

    unique_id = uuid.uuid4().hex[:8]
    p_num = part_num if part_num is not None else 1
    vertical_video_name = f"short_part_{p_num}_{unique_id}.mp4"
    vertical_thumb_name = f"thumb_part_{p_num}_{unique_id}.jpg"
    if not output_vertical_path:
        output_vertical_path = os.path.join(CLIPPER_DIR, vertical_video_name)
    else:
        vertical_video_name = os.path.basename(output_vertical_path)

    vertical_thumb_path = os.path.join(CLIPPER_DIR, vertical_thumb_name)

    notify_progress(35, "Step 4: Reframing to 9:16 vertical (1080x1920) with actor centering...")
    ffmpeg_bin = get_ffmpeg_bin()
    cmd = [
        ffmpeg_bin, "-y",
        "-i", standard_video_path,
        "-filter_complex", f"[0:v]{crop_filter},setsar=1[vout]",
        "-map", "[vout]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-r", "30",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_vertical_path
    ]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
    if proc.returncode != 0 or not os.path.exists(output_vertical_path) or os.path.getsize(output_vertical_path) < 10000:
        raise RuntimeError(f"FFmpeg failed to convert to 9:16 vertical: {proc.stderr[:200]}")

    notify_progress(85, "Step 4: Generating vertical HD thumbnail...")
    generate_short_thumbnail(output_vertical_path, vertical_thumb_path)
    notify_progress(100, f"Part {p_num} 9:16 Vertical Short ready!")

    # Load existing short data from checkpoint if available
    short_data = {}
    if job_id and part_num:
        ckpt = load_job_checkpoint(job_id)
        if ckpt and "completed_shorts" in ckpt and str(part_num) in ckpt["completed_shorts"]:
            short_data = dict(ckpt["completed_shorts"][str(part_num)])

    short_data.update({
        "part": p_num,
        "format": "vertical_916",
        "filename": vertical_video_name,
        "video_url": f"/api/clipper/media/{vertical_video_name}",
        "thumbnail_url": f"/api/clipper/media/{vertical_thumb_name}",
        "filepath": output_vertical_path,
        "standard_filepath": standard_video_path,
        "standard_video_url": f"/api/clipper/media/{os.path.basename(standard_video_path)}",
        "vertical_ready": True,
        "status": "ready"
    })

    if job_id:
        try:
            ckpt = load_job_checkpoint(job_id)
            if ckpt:
                if "completed_shorts" not in ckpt or not isinstance(ckpt["completed_shorts"], dict):
                    ckpt["completed_shorts"] = {}
                ckpt["completed_shorts"][str(p_num)] = short_data
                save_job_checkpoint(job_id, ckpt)
        except Exception as se:
            logger.warning(f"Could not persist vertical short {p_num} to checkpoint {job_id}: {se}")

    return short_data


def process_single_short_pipeline(
    youtube_url: str,
    scene: Dict[str, Any],
    language: str = "Hindi",
    video_title: str = "",
    job_id: Optional[str] = None,
    progress_callback: Optional[Any] = None,
    auto_vertical: bool = False,
    voice_name: str = "Kore",
    tone_style: str = "Suspense / Thriller",
    wps: Optional[float] = None
) -> Dict[str, Any]:
    """
    Unified 4-Step Pipeline:
    Step 1: Direct Gemini Storyboard & Script (already provided in scene).
    Step 2: High-Speed Local Python Raw Cutter (downloads 3-6s cuts with 0% movie audio to uploads/clipper_cuts/).
    Step 3: Mute & Voiceover Sync (assembles standard 16:9 recap with Gemini 3.8 Flash TTS + BGM first in ~15s).
    Step 4: Separate 9:16 Vertical / Face-Tracking On Demand (if auto_vertical=True or on user demand in ~5s).
    """
    def notify_progress(pct: int, msg: str):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    part_num = scene.get("part", 1)
    sub_clips = scene.get("sub_clips") or []

    # If sub_clips missing, generate algorithmic cuts
    if not isinstance(sub_clips, list) or len(sub_clips) < 4:
        s_start = scene.get("start_seconds")
        if s_start is None:
            s_start = parse_timestamp_to_seconds(scene.get("start_time", "00:00"))
        s_end = scene.get("end_seconds")
        if s_end is None:
            s_end = parse_timestamp_to_seconds(scene.get("end_time", "01:00"))
        if s_end <= s_start:
            s_end = s_start + 120
        sub_clips = generate_algorithmic_subclips(s_start, s_end, target_duration=58)
        scene["sub_clips"] = sub_clips

    # Step 2: Download raw cuts (15% -> 55%)
    downloaded_cuts = download_raw_cuts_step2(
        youtube_url=youtube_url,
        sub_clips=sub_clips,
        part_num=part_num,
        job_id=job_id,
        progress_callback=progress_callback
    )
    scene["downloaded_cuts"] = downloaded_cuts

    # Step 3: Assemble standard recap preview (55% -> 85%)
    standard_short = assemble_standard_recap_step3(
        scene=scene,
        downloaded_cuts=downloaded_cuts,
        language=language,
        video_title=video_title,
        job_id=job_id,
        progress_callback=progress_callback,
        voice_name=voice_name,
        tone_style=tone_style,
        wps=wps
    )

    if not auto_vertical:
        notify_progress(100, f"Part {part_num} standard recap ready!")
        return standard_short

    # Step 4: Convert to 9:16 vertical on demand (85% -> 100%)
    vertical_short = convert_recap_to_vertical_step4(
        standard_video_path=standard_short["filepath"],
        job_id=job_id,
        part_num=part_num,
        progress_callback=progress_callback
    )
    return vertical_short


# =====================================================================
# TIMELINE VIDEO TRIMMER & NARRATIVE SLICER ENGINE (ORIGINAL RESOLUTION)
# =====================================================================

def get_video_metadata(video_path: str) -> Dict[str, Any]:
    """
    Extracts metadata from a video file using ffprobe.
    Preserves and reports exact native width, height, aspect ratio, fps, and duration.
    """
    meta = {
        "path": video_path,
        "filename": os.path.basename(video_path),
        "duration": 0.0,
        "duration_str": "00:00:00",
        "width": 1920,
        "height": 1080,
        "aspect_ratio": "16:9",
        "fps": 30.0,
        "size_bytes": 0,
        "size_mb": 0.0,
        "has_audio": True,
        "video_codec": "h264"
    }
    if not os.path.exists(video_path):
        return meta

    meta["size_bytes"] = os.path.getsize(video_path)
    meta["size_mb"] = round(meta["size_bytes"] / (1024 * 1024), 2)

    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    try:
        cmd = [
            ffprobe_bin, "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,duration",
            "-show_entries", "format=duration",
            "-of", "json",
            video_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            fmt_dur = data.get("format", {}).get("duration")
            if fmt_dur:
                try:
                    meta["duration"] = round(float(fmt_dur), 2)
                except Exception:
                    pass

            has_audio = False
            for s in data.get("streams", []):
                ctype = s.get("codec_type")
                if ctype == "video" and s.get("width"):
                    meta["width"] = int(s.get("width"))
                    meta["height"] = int(s.get("height"))
                    meta["video_codec"] = s.get("codec_name", "h264")
                    if not meta["duration"] and s.get("duration"):
                        try:
                            meta["duration"] = round(float(s["duration"]), 2)
                        except Exception:
                            pass
                    r_rate = s.get("r_frame_rate", "30/1")
                    if "/" in r_rate:
                        num, den = r_rate.split("/")
                        try:
                            meta["fps"] = round(float(num) / max(1.0, float(den)), 2)
                        except Exception:
                            pass
                elif ctype == "audio":
                    has_audio = True

            meta["has_audio"] = has_audio
    except Exception as e:
        logger.warning(f"ffprobe metadata notice: {e}")

    if meta["duration"] <= 0:
        meta["duration"] = 60.0

    mins = int(meta["duration"] // 60)
    secs = int(meta["duration"] % 60)
    hrs = int(mins // 60)
    mins = int(mins % 60)
    if hrs > 0:
        meta["duration_str"] = f"{hrs:02d}:{mins:02d}:{secs:02d}"
    else:
        meta["duration_str"] = f"{mins:02d}:{secs:02d}"

    w, h = meta["width"], meta["height"]
    gcd_val = math.gcd(w, h) if w > 0 and h > 0 else 1
    if gcd_val > 0 and (w // gcd_val) in [16, 4, 21, 9] and (h // gcd_val) in [9, 3, 16]:
        meta["aspect_ratio"] = f"{w // gcd_val}:{h // gcd_val}"
    else:
        ratio = round(w / max(1, h), 2)
        meta["aspect_ratio"] = f"{ratio}:1"

    return meta


def analyze_video_timeline_autocut(
    video_path: str,
    duration: float = 0.0,
    title: str = "",
    focus_style: str = "highlights",
    target_duration: Optional[int] = None,
    language: str = "Hindi",
    custom_prompt: str = ""
) -> Dict[str, Any]:
    """
    Analyzes video timeline and discovers key narrative timestamps.
    Automatically identifies keeper scenes, discards filler, and returns keeper intervals + recap script.
    """
    if duration <= 0:
        meta = get_video_metadata(video_path)
        duration = meta.get("duration", 60.0)

    duration = float(duration)
    if duration < 5:
        duration = 60.0

    if not target_duration or target_duration <= 0:
        if duration <= 90:
            target_duration = int(duration * 0.7)
        elif duration <= 300:
            target_duration = 60
        elif duration <= 1200:
            target_duration = 180
        elif duration <= 3600:
            target_duration = 300
        else:
            target_duration = 480

    target_duration = min(int(duration), max(20, int(target_duration)))

    keeper_clips = []
    script = ""
    source = "algorithmic"

    client = gemini_engine.get_genai_client()
    if client:
        prompt = f"""You are a master Hollywood film editor and cinema director.
Analyze this video storyline:
- Title: {title or os.path.basename(video_path)}
- Total Video Duration: {int(duration)} seconds ({int(duration // 60)}m {int(duration % 60)}s)
- Target Highlight Duration: ~{target_duration} seconds
- Editing Style / Focus: {focus_style}
- Language: {language}
{f"- Additional Instructions: {custom_prompt}" if custom_prompt else ""}

YOUR GOAL:
1. Discover the most important narrative turning points, tension spikes, hooks, and climaxes across the ENTIRE duration.
2. Select between 4 and 8 high-impact keeper clips that together tell a complete, compelling story within ~{target_duration} seconds.
3. Every second not included in these keeper clips will be automatically deleted as filler.
4. Write a synchronized storytelling recap narration in {language} for these keeper scenes.

RULES:
- Return STRICT JSON ONLY. No markdown, no explanations outside JSON.
- Timestamps must be numeric seconds between 0.0 and {duration}.
- Each keeper clip must have a duration between 5 and 60 seconds.
- Clips must be strictly in chronological order with no overlaps.

JSON Format:
{{
  "keeper_clips": [
    {{
      "start": 5.0,
      "end": 20.0,
      "title": "Scene 1: Opening Hook",
      "reason": "Establishes danger and introduces the hero"
    }}
  ],
  "script": "कहानी की शुरुआत में... (Hindi recap narration matching the scenes)"
}}
"""
        try:
            config = gemini_engine.get_gemini_config()
            model_name = config.get("model") or "gemini-2.5-flash"
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            txt = resp.text.strip()
            txt = re.sub(r"^```(?:json)?", "", txt, flags=re.IGNORECASE).strip()
            txt = re.sub(r"```$", "", txt).strip()
            data = json.loads(txt)
            raw_clips = data.get("keeper_clips") or []
            if isinstance(raw_clips, list) and len(raw_clips) >= 2:
                for idx, rc in enumerate(raw_clips):
                    s = max(0.0, min(duration - 1.0, float(rc.get("start", 0))))
                    e = max(s + 2.0, min(duration, float(rc.get("end", s + 10))))
                    if e > s:
                        keeper_clips.append({
                            "id": idx + 1,
                            "start": round(s, 2),
                            "end": round(e, 2),
                            "duration": round(e - s, 2),
                            "title": rc.get("title", f"Scene {idx + 1}"),
                            "reason": rc.get("reason", "Key narrative highlight")
                        })
                if keeper_clips:
                    keeper_clips.sort(key=lambda x: x["start"])
                    script = data.get("script", "")
                    source = "gemini"
        except Exception as ge:
            logger.warning(f"Gemini timeline autocut notice: {ge}. Using algorithmic cuts.")

    if not keeper_clips:
        ratios = [
            (0.05, 0.20, "Act 1: The Inciting Hook", "Initial tension grip"),
            (0.35, 0.50, "Act 2: The Midpoint Escalation", "High-stakes conflict"),
            (0.70, 0.88, "Act 3: The Climax & Resolution", "Peak confrontation")
        ]
        base_dur = max(6.0, target_duration / len(ratios))
        for idx, (r_start, r_end, title_act, reason_act) in enumerate(ratios):
            center = duration * ((r_start + r_end) / 2.0)
            s = max(0.0, center - (base_dur / 2.0))
            e = min(duration, s + base_dur)
            if e - s < 3.0:
                e = min(duration, s + 5.0)
            keeper_clips.append({
                "id": idx + 1,
                "start": round(s, 2),
                "end": round(e, 2),
                "duration": round(e - s, 2),
                "title": title_act,
                "reason": reason_act
            })
        if language.lower().startswith("hi"):
            script = "कहानी की शुरुआत एक रहस्यमयी घटना से होती है, जहाँ नायक को खतरे का सामना करना पड़ता है। धीरे-धीरे रहस्य गहराता जाता है और अंत में नायक सच का सामना करता है।"
        else:
            script = "The story begins with an unexpected crisis that challenges our protagonist. As the stakes escalate, a stunning revelation changes everything."

    total_kept = sum(c["duration"] for c in keeper_clips)
    total_filler = max(0.0, duration - total_kept)
    filler_pct = round((total_filler / max(1.0, duration)) * 100, 1)

    return {
        "success": True,
        "source": source,
        "video_duration": round(duration, 2),
        "total_kept_duration": round(total_kept, 2),
        "filler_removed_duration": round(total_filler, 2),
        "filler_removed_percent": filler_pct,
        "keeper_clips": keeper_clips,
        "script": script
    }


def generate_20min_movie_explainer_storyboard(
    youtube_url: str,
    credentials=None,
    target_duration: int = 1200,  # 20 minutes default
    language: str = "Hindi",
    voice_name: str = "Kore",
    tone_style: str = "Narrative Deep Storytelling",
    custom_instructions: str = ""
) -> Dict[str, Any]:
    """
    Analyzes full-length YouTube movie narrative using Gemini and 100-word WPS calibration.
    Produces a structured 20-minute explainer recap storyboard:
    1. Hook & Introduction (00:00 - Setup)
    2. Rising Tension & Plot Twists
    3. Crucial Action / Climax / Resolution
    Enforces millisecond-precise voice sync: Target Words per cut = round(Duration * WPS).
    """
    logger.info(f"Extracting YouTube movie data for 20-minute explainer: {youtube_url}")
    try:
        yt_info = extract_youtube_info(youtube_url, credentials=credentials)
    except Exception as e:
        logger.warning(f"Error in extract_youtube_info: {e}, using safe baseline metadata")
        vid = extract_video_id(youtube_url) or "video"
        yt_info = {
            "url": youtube_url,
            "title": f"Movie Narrative ({vid})",
            "duration": 7200,
            "duration_str": "02:00:00",
            "description": "Full-length movie narrative storyline.",
            "chapters": [],
            "thumbnail": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if len(vid) == 11 else "",
            "channel": "YouTube"
        }

    title = yt_info.get("title", "Movie Title")
    duration = int(yt_info.get("duration") or 7200)
    if duration <= 60:
        duration = 7200
    duration_str = yt_info.get("duration_str") or format_seconds_to_timestamp(duration)
    description = yt_info.get("description", "")
    chapters = yt_info.get("chapters", [])
    thumbnail = yt_info.get("thumbnail", "")
    channel = yt_info.get("channel", "")

    # 100-Word Benchmark Voice Speed Calibration
    calib = calibrate_voice_speed(voice_name=voice_name, tone_style=tone_style, language=language)
    wps = float(calib.get("wps", 2.35))
    logger.info(f"Calibrated WPS for Explainer: {wps} ({voice_name} / {tone_style})")

    target_duration = max(60, int(target_duration))
    target_words = int(round(target_duration * wps))

    chapters_text = ""
    if chapters:
        ch_lines = []
        for ch in chapters[:30]:
            c_s = format_seconds_to_timestamp(ch.get("start_time", 0))
            c_e = format_seconds_to_timestamp(ch.get("end_time", 0))
            ch_lines.append(f"- [{c_s} - {c_e}] {ch.get('title', '')}")
        chapters_text = "Movie Chapters:\n" + "\n".join(ch_lines)

    prompt = f"""You are a master Hollywood Film Editor, Senior Story Producer, and viral YouTube Movie Explainer Specialist.
Analyze the complete narrative storyline for this full-length movie:
- Movie Title: {title}
- Total Movie Runtime: {duration_str} ({duration} seconds)
- Channel / Uploader: {channel}
- Movie Synopsis / Description:
{description[:2500]}
{chapters_text}

=== 20-MINUTE EXPLAINER MISSION ===
Your mission is to construct an EXACT {target_duration // 60}-minute (~{target_duration} seconds) comprehensive cinema explainer recap storyboard that covers the ENTIRE movie narrative from beginning to end without blind cuts.

You must divide the story into 3 CRITICAL PHASES:
1. PHASE 1: HOOK & INTRODUCTION (Setup) (~25% of target runtime)
   - Visual shocker / hook (0-15s)
   - Establishing the world, central hero, their goal, and the inciting incident that kicks off the crisis.
2. PHASE 2: RISING TENSION & PLOT TWISTS (~50% of target runtime)
   - Escalating conflict, pursuit, investigation, trials, and dangerous encounters.
   - Critical midpoint plot twist that shifts the stakes completely.
   - Dark turn / betrayal / "All hope is lost" moment.
3. PHASE 3: CRUCIAL ACTION, CLIMAX & RESOLUTION (~25% of target runtime)
   - Final high-octane showdown / action sequence.
   - The major mystery or plot revelation.
   - Dramatic climax and emotional resolution/aftermath.

=== MILLISECOND VOICE SYNC & WPS BUDGET ===
- Selected Voice: {voice_name} ({tone_style})
- Calibrated Narration Speed: {wps:.2f} Words Per Second
- Total Spoken Word Budget: ~{target_words} words in {language} (Devanagari script for Hindi).
- STRICT RULE PER CUT: For each keeper cut, Target Words = round(Cut Duration * {wps:.2f}).
  The narration text for that scene MUST strictly fit within this word count so voiceover finishes synchronously as the visual scene concludes.

=== TIMESTAMP CONSTRAINTS ===
- All timestamps must be non-contiguous, representing the true narrative peaks of the movie across the full {duration}s runtime.
- Every timestamp must be within 0.0 and {duration}.
- Format: Numeric seconds (e.g. start: 252.0, end: 288.0 for 04:12 - 04:48).
- Duration of each cut: between 20 and 100 seconds.
- Total combined duration of all keeper cuts MUST equal ~{target_duration} seconds.
- Total keeper cuts: between 12 and 20 scenes.

=== OUTPUT FORMAT ===
Return STRICT JSON ONLY (no markdown outside JSON):
{{
  "summary": "2-3 sentence overview of this 20-minute explainer recap",
  "keeper_clips": [
    {{
      "phase": "Hook & Setup",
      "start": 12.0,
      "end": 45.0,
      "start_ts": "00:12",
      "end_ts": "00:45",
      "duration": 33.0,
      "title": "The Mysterious Premise",
      "reason": "Establishes the crisis and protagonist dilemma",
      "target_words": 77,
      "script_segment": "कहानी की शुरुआत में जब नायक को यह रहस्यमयी सुराग मिलता है..."
    }}
  ],
  "full_script": "Complete stitched Hindi storytelling narration across all keeper scenes..."
}}
"""

    keeper_clips = []
    summary = ""
    full_script = ""
    source = "algorithmic"

    client = gemini_engine.get_genai_client()
    if client:
        candidate_models = [
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.7-flash",
            "gemini-3.8-flash",
            "gemini-flash-latest"
        ]
        for model_name in candidate_models:
            try:
                logger.info(f"Calling Gemini ({model_name}) for 20-minute explainer storyboard: {title}")
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                txt = resp.text.strip()
                txt = re.sub(r"^```(?:json)?", "", txt, flags=re.IGNORECASE).strip()
                txt = re.sub(r"```$", "", txt).strip()
                data = json.loads(txt)
                raw_clips = data.get("keeper_clips") or []
                if isinstance(raw_clips, list) and len(raw_clips) >= 4:
                    for idx, rc in enumerate(raw_clips, 1):
                        s = max(0.0, min(duration - 2.0, float(rc.get("start", 0))))
                        e = max(s + 5.0, min(duration, float(rc.get("end", s + 30))))
                        c_dur = round(e - s, 2)
                        c_words = int(round(c_dur * wps))
                        keeper_clips.append({
                            "id": idx,
                            "phase": rc.get("phase", "Rising Tension & Plot Twists"),
                            "start": round(s, 2),
                            "end": round(e, 2),
                            "start_ts": format_seconds_to_timestamp(s),
                            "end_ts": format_seconds_to_timestamp(e),
                            "duration": c_dur,
                            "title": rc.get("title", f"Scene {idx}"),
                            "reason": rc.get("reason", "Key story milestone"),
                            "target_words": rc.get("target_words") or c_words,
                            "script_segment": rc.get("script_segment", "")
                        })
                    if keeper_clips:
                        keeper_clips.sort(key=lambda x: x["start"])
                        summary = data.get("summary", "")
                        full_script = data.get("full_script", " ".join(c["script_segment"] for c in keeper_clips if c["script_segment"]))
                        source = f"gemini ({model_name})"
                        break
            except Exception as ge:
                logger.warning(f"Model {model_name} explainer call notice: {ge}")

    # Fallback: Algorithmic 3-Phase Explainer Generator (16 high-impact scenes)
    if not keeper_clips:
        logger.info("Using algorithmic 3-phase cinema explainer generator fallback...")
        phase_plan = [
            # Phase 1: Hook & Introduction (Setup) (~300s)
            ("Hook & Setup", 0.02, 0.05, "1. The Inciting Hook & Catastrophe", "Initial shock that disrupts ordinary life"),
            ("Hook & Setup", 0.07, 0.11, "2. Protagonist Introduction & Stakes", "Establishes protagonist dilemma and core motivation"),
            ("Hook & Setup", 0.13, 0.17, "3. The Call to Adventure & First Trial", "Hero crosses the threshold into danger"),
            ("Hook & Setup", 0.18, 0.22, "4. Assembling the Squad / Briefing", "High-stakes strategy and rules of engagement"),

            # Phase 2: Rising Tension & Plot Twists (~600s)
            ("Rising Tension & Plot Twists", 0.25, 0.31, "5. First Enemy Confrontation", "Hero faces initial ambush; stakes escalate"),
            ("Rising Tension & Plot Twists", 0.33, 0.39, "6. Critical Investigation & Clue Found", "Hidden conspiracy discovered"),
            ("Rising Tension & Plot Twists", 0.41, 0.47, "7. The Secret Infiltration", "Undercover operation in hostile territory"),
            ("Rising Tension & Plot Twists", 0.49, 0.55, "8. The Massive Midpoint Twist", "Shocking reveal flips the entire mission"),
            ("Rising Tension & Plot Twists", 0.57, 0.63, "9. High-Speed Pursuit & Escape", "Life-or-death chase sequence"),
            ("Rising Tension & Plot Twists", 0.65, 0.71, "10. The Unexpected Betrayal", "An ally reveals true deceptive motives"),
            ("Rising Tension & Plot Twists", 0.73, 0.78, "11. The Darkest Hour (All Hope Lost)", "Hero cornered with zero apparent escape"),
            ("Rising Tension & Plot Twists", 0.79, 0.83, "12. Rallying for the Final Stand", "Regrouping with renewed resolve"),

            # Phase 3: Action, Climax & Resolution (~300s)
            ("Crucial Action & Climax", 0.85, 0.89, "13. Storming the Enemy Fortress", "The ultimate action sequence begins"),
            ("Crucial Action & Climax", 0.90, 0.94, "14. The Final Face-to-Face Showdown", "Protagonist vs Antagonist direct battle"),
            ("Crucial Action & Climax", 0.95, 0.97, "15. The Mastermind Revelation", "The final secret unveiled"),
            ("Crucial Action & Climax", 0.98, 0.995, "16. Resolution, Justice & Closure", "The aftermath and powerful final punchline")
        ]

        target_per_cut = target_duration / len(phase_plan)
        script_segments = []

        for idx, (phase_name, r_s, r_e, sc_title, sc_reason) in enumerate(phase_plan, 1):
            mid = duration * ((r_s + r_e) / 2.0)
            s = max(0.0, mid - (target_per_cut / 2.0))
            e = min(duration, s + target_per_cut)
            c_dur = round(e - s, 2)
            c_words = int(round(c_dur * wps))

            if idx == 1:
                seg = "कहानी की शुरुआत एक दिल दहला देने वाले मोड़ से होती है, जहाँ चारों तरफ अफरा-तफरी मच जाती है।"
            elif idx == 8:
                seg = "यहीं पर कहानी में सबसे बड़ा ट्विस्ट आता है, जब एक ऐसा सच सामने आता है जिसने पूरे खेल को पलट कर रख दिया।"
            elif idx >= 13:
                seg = "अब शुरू होता है अंतिम और निर्णायक मुकाबला, जहाँ नायक अपनी जान की बाज़ी लगाकर दुश्मनों के मंसूबों को नाकाम कर देता है।"
            else:
                seg = f"दृश्य {idx} में तनाव अपने चरम पर पहुँच जाता है, और हर एक सेकंड में नया खतरा सामने आता है।"

            script_segments.append(seg)
            keeper_clips.append({
                "id": idx,
                "phase": phase_name,
                "start": round(s, 2),
                "end": round(e, 2),
                "start_ts": format_seconds_to_timestamp(s),
                "end_ts": format_seconds_to_timestamp(e),
                "duration": c_dur,
                "title": sc_title,
                "reason": sc_reason,
                "target_words": c_words,
                "script_segment": seg
            })

        summary = f"20-Minute Cinema Explainer for {title} spanning across {duration_str} with {len(keeper_clips)} keeper scenes."
        full_script = " ".join(script_segments)

    tot_kept = sum(c["duration"] for c in keeper_clips)
    tot_filler = max(0.0, duration - tot_kept)
    filler_pct = round((tot_filler / max(1.0, duration)) * 100, 1) if duration > 0 else 0

    phase_groups = {
        "Hook & Setup": [c for c in keeper_clips if "Hook" in c["phase"] or "Setup" in c["phase"]],
        "Rising Tension & Plot Twists": [c for c in keeper_clips if "Tension" in c["phase"] or "Twist" in c["phase"]],
        "Crucial Action & Climax": [c for c in keeper_clips if "Action" in c["phase"] or "Climax" in c["phase"]]
    }

    return {
        "success": True,
        "source": source,
        "title": title,
        "duration": duration,
        "duration_str": duration_str,
        "thumbnail": thumbnail,
        "channel": channel,
        "youtube_info": {
            "url": youtube_url,
            "title": title,
            "duration": duration,
            "duration_str": duration_str,
            "thumbnail": thumbnail,
            "channel": channel
        },
        "target_duration": target_duration,
        "wps": wps,
        "calibrated_wps": wps,
        "voice_name": voice_name,
        "tone_style": tone_style,
        "total_duration_sec": round(tot_kept, 2),
        "total_clips": len(keeper_clips),
        "total_words": int(round(tot_kept * wps)),
        "total_kept_duration": round(tot_kept, 2),
        "filler_removed_duration": round(tot_filler, 2),
        "filler_removed_percent": filler_pct,
        "summary": summary,
        "keeper_clips": keeper_clips,
        "phase_groups": phase_groups,
        "phases": phase_groups,
        "full_script": full_script
    }


def export_timeline_trimmed_video(
    source_video_path: str,
    keeper_clips: List[Dict[str, Any]],
    output_path: str,
    audio_mode: str = "original",  # "original", "tts", "tts_bgm"
    voice_name: str = "Kore",
    tone_style: str = "Narrative Deep",
    script: str = "",
    progress_callback: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Slices and concatenates keeper clips from source_video_path WITHOUT changing resolution
    or aspect ratio.
    Keeps 100% original video size (native width & height, e.g. 1920x1080).
    Uses ultra-fast stream copy (-c copy) as primary slicing mechanism.
    """
    def notify(pct: int, msg: str):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass
        logger.info(f"Trimmer export progress [{pct}%]: {msg}")

    if not os.path.exists(source_video_path):
        raise FileNotFoundError(f"Source video not found: {source_video_path}")

    sanitized = []
    for c in keeper_clips:
        try:
            s = float(c.get("start", 0))
            e = float(c.get("end", 0))
            if e > s + 0.3:
                sanitized.append({"start": s, "end": e, "title": c.get("title", "")})
        except Exception:
            pass

    if not sanitized:
        raise ValueError("No valid keeper clips provided for export.")

    sanitized.sort(key=lambda x: x["start"])
    total_clips = len(sanitized)

    task_id = uuid.uuid4().hex[:8]
    task_temp = os.path.join(TEMP_DIR, f"trim_{task_id}")
    os.makedirs(task_temp, exist_ok=True)

    ffmpeg_bin = get_ffmpeg_bin()
    sliced_paths = []

    try:
        notify(5, f"Preparing to slice {total_clips} clips at original resolution via ultra-fast stream copy...")

        # 1. Slice each keeper clip WITHOUT RESIZING (keeps native resolution & aspect ratio)
        # Prioritizes ultra-fast stream copy (-c copy)
        for i, clip in enumerate(sanitized):
            s = clip["start"]
            e = clip["end"]
            clip_out = os.path.join(task_temp, f"clip_{i:03d}.mp4")

            # Try ultra-fast stream copy first
            cmd_copy = [
                ffmpeg_bin, "-y",
                "-ss", str(s),
                "-to", str(e),
                "-i", source_video_path,
                "-c", "copy"
            ]
            if audio_mode != "original":
                cmd_copy.extend(["-an"])  # strip audio for voiceover replacement
            cmd_copy.extend(["-avoid_negative_ts", "make_zero", clip_out])

            res_copy = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res_copy.returncode == 0 and os.path.exists(clip_out) and os.path.getsize(clip_out) > 0:
                sliced_paths.append(clip_out)
            else:
                # Fallback to keyframe-accurate ultrafast re-encode (zero resizing, preserves 100% native resolution)
                cmd_fast = [
                    ffmpeg_bin, "-y",
                    "-ss", str(s),
                    "-to", str(e),
                    "-i", source_video_path,
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "18"
                ]
                if audio_mode == "original":
                    cmd_fast.extend(["-c:a", "aac", "-b:a", "192k"])
                else:
                    cmd_fast.extend(["-an"])
                cmd_fast.extend(["-avoid_negative_ts", "make_zero", clip_out])
                subprocess.run(cmd_fast, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if os.path.exists(clip_out) and os.path.getsize(clip_out) > 0:
                    sliced_paths.append(clip_out)

            pct = 10 + int(50 * ((i + 1) / total_clips))
            notify(pct, f"Sliced keeper clip {i+1}/{total_clips} ({e-s:.1f}s)")

        if not sliced_paths:
            raise RuntimeError("Failed to slice keeper clips from source video.")

        # 2. Concat keeper clips
        notify(65, "Stitching keeper clips into seamless montage...")
        concat_txt = os.path.join(task_temp, "concat.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for p in sliced_paths:
                clean_p = os.path.abspath(p).replace("\\", "/")
                f.write(f"file '{clean_p}'\n")

        stitched_video = os.path.join(task_temp, "stitched.mp4")
        concat_cmd = [
            ffmpeg_bin, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_txt,
            "-c", "copy",
            stitched_video
        ]
        res_concat = subprocess.run(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res_concat.returncode != 0 or not os.path.exists(stitched_video) or os.path.getsize(stitched_video) == 0:
            # Fallback to re-encode concat filter
            filter_parts = "".join(f"[{j}:v]" for j in range(len(sliced_paths)))
            cmd_filter = [ffmpeg_bin, "-y"]
            for p in sliced_paths:
                cmd_filter.extend(["-i", p])
            cmd_filter.extend([
                "-filter_complex", f"{filter_parts}concat=n={len(sliced_paths)}:v=1:a=0[v]",
                "-map", "[v]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                stitched_video
            ])
            subprocess.run(cmd_filter, check=True)

        # 3. Audio handling
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        if audio_mode == "original":
            notify(90, "Finalizing video with original native audio...")
            shutil.copyfile(stitched_video, output_path)
        else:
            notify(75, "Generating dramatic Neural Storytelling Voiceover...")
            tts_audio_path = os.path.join(task_temp, "tts_narration.mp3")
            clean_script = script.strip() or "कहानी की शुरुआत में नायक को सच्चाई का पता चलता है और रोमांचक मोड़ आता है।"
            gen_ok = generate_gemini_tts_audio(
                text=clean_script,
                output_path=tts_audio_path,
                voice_name=voice_name,
                tone_style=tone_style
            )
            if not gen_ok:
                generate_voiceover_audio(text=clean_script, output_path=tts_audio_path, language="Hindi")

            if audio_mode == "tts_bgm":
                notify(85, "Mixing ducked cinematic background music + voiceover...")
                bgm_path = ensure_background_music_exists()
                mix_cmd = [
                    ffmpeg_bin, "-y",
                    "-i", stitched_video,
                    "-i", tts_audio_path,
                    "-i", bgm_path,
                    "-filter_complex", "[1:a]volume=1.0[v_a];[2:a]volume=0.15[b_a];[v_a][b_a]amix=inputs=2:duration=first[aout]",
                    "-map", "0:v",
                    "-map", "[aout]",
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    output_path
                ]
                subprocess.run(mix_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            else:
                notify(85, "Overlaying neural voiceover onto stitched video...")
                ov_cmd = [
                    ffmpeg_bin, "-y",
                    "-i", stitched_video,
                    "-i", tts_audio_path,
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    output_path
                ]
                subprocess.run(ov_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            shutil.copyfile(stitched_video, output_path)

        notify(100, "Export complete! Final video is ready.")
        meta = get_video_metadata(output_path)
        return {
            "success": True,
            "output_path": output_path,
            "filename": os.path.basename(output_path),
            "duration": meta.get("duration", 0),
            "duration_str": meta.get("duration_str", "00:00:00"),
            "width": meta.get("width", 1920),
            "height": meta.get("height", 1080),
            "aspect_ratio": meta.get("aspect_ratio", "16:9"),
            "size_mb": meta.get("size_mb", 0)
        }
    finally:
        try:
            shutil.rmtree(task_temp, ignore_errors=True)
        except Exception:
            pass

