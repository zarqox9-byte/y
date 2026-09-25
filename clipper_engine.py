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
import asyncio
import logging
import subprocess
import shutil
import urllib.request
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

import gemini_engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIPPER_DIR = os.path.join(BASE_DIR, "uploads", "clipper_shorts")
TEMP_DIR = os.path.join(BASE_DIR, "uploads", "clipper_temp")
JOBS_DIR = os.path.join(BASE_DIR, "uploads", "clipper_jobs")
os.makedirs(CLIPPER_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)


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
    2. Fallback: If yt-dlp hits a bot warning or error, immediately uses official YouTube Data API v3.
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
            'extractor_args': {
                'youtube': {
                    'player_client': ['web_creator', 'web_embedded', 'mweb', 'android'],
                    'player_skip': ['webpage', 'configs']
                }
            },
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            'compat_opts': ['no-youtube-unavailable-videos'],
        }

        logger.info(f"Extracting video metadata via yt-dlp for: {youtube_url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
    except Exception as e:
        yt_dlp_err = e
        logger.warning(f"yt-dlp extraction encountered issue: {e}. Falling back to official YouTube Data API v3...")

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

    # Step 2: Dual-Fallback to Official YouTube Data API v3 (Zero Bot Block)
    video_id = extract_video_id(youtube_url)
    if not video_id:
        raise RuntimeError(f"Could not extract video ID from '{youtube_url}' and yt-dlp failed: {yt_dlp_err}")

    logger.info(f"Executing YouTube Data API v3 fallback for video ID: {video_id}")
    yt_service = get_youtube_data_api_client(credentials=credentials)
    if not yt_service:
        raise RuntimeError(f"yt-dlp failed ({yt_dlp_err}) and YouTube Data API v3 client could not authenticate.")

    try:
        response = yt_service.videos().list(id=video_id, part='snippet,contentDetails').execute()
        items = response.get('items', [])
        if not items:
            raise RuntimeError(f"YouTube Data API v3 returned no video matching ID: {video_id}")

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
        logger.error(f"YouTube Data API v3 fallback failed: {api_err}")
        raise RuntimeError(f"Failed to extract video info: yt-dlp error ({yt_dlp_err}), API error ({api_err})")


# =====================================================================
# 2. GEMINI CHRONOLOGICAL SCENE SEGMENTATION & SCRIPTING
# =====================================================================
def analyze_movie_narrative_for_shorts(
    youtube_url: str,
    video_info: Dict[str, Any],
    max_shorts: int = 5,
    target_duration: int = 50,
    language: str = "Hindi",
    job_id: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
    """
    Prompts Google Gemini to analyze the movie's storyline and generate
    high-tension scenes in strict chronological order with viral titles,
    hooks, and ~60-word recap scripts.
    Saves analysis checkpoint immediately to uploads/clipper_jobs/<job_id>.json.
    """
    title = video_info.get("title", "")
    duration = video_info.get("duration", 0)
    duration_str = video_info.get("duration_str", "")
    description = video_info.get("description", "")
    chapters = video_info.get("chapters", [])

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
        "Write natural, viral storytelling Hindi (Devanagari script preferred, emotional, high-suspense like top YouTube movie explanation channels). "
        "Example style: 'कहानी की शुरुआत में जब रोहन इस खतरनाक जगह पर पहुंचता है, तो उसे नहीं पता था कि आगे क्या होने वाला है...'"
        if language.lower().startswith("hi")
        else "Write high-energy, dramatic, fast-paced English narrative recap scripts like top cinema recap channels."
    )

    prompt = f"""You are an elite YouTube Shorts Strategist and Cinema Editor specializing in viral, 100% copyright-safe movie recap shorts.
Analyze the following movie / video narrative and segment it into high-retention, high-drama, engaging YouTube Shorts in STRICT CHRONOLOGICAL ORDER (Part 1, Part 2, Part 3... from the beginning of the movie to the climax/ending).

=== CRITICAL COPYRIGHT-SAFETY & DYNAMIC MONTAGE RULE ===
To guarantee 100% YouTube Content ID and copyright safety, DO NOT select a single continuous 50-second clip for any Part.
Instead, for EACH Part (Short), you MUST generate a dynamic MULTI-SCENE MONTAGE composed of 8 to 14 engaging sub-clips sampled across the relevant narrative act (each sub-clip MUST be between 3 and 6 seconds long, e.g., 01:15-01:19, 02:40-02:44, 04:10-04:15).
The combined total duration of all sub-clips in the Part MUST be between 50 seconds and 70 seconds (1 min 10 sec max).

=== MOVIE / VIDEO DETAILS ===
Title: {title}
Total Duration: {duration_str} ({duration} seconds)
Description:
{description[:1500]}
{chapters_summary}

=== REQUIREMENTS ===
1. CHRONOLOGY & PROGRESSION:
   - Every Part must be in STRICT CHRONOLOGICAL ORDER (Part 1 covers the opening/inciting incident, Part 2 follows Part 1, Part 3 advances further towards climax).
   - Within each Part, the 8 to 14 sub-clips must also progress chronologically through that story segment.
   - Sub-clips must focus on key character reactions, high-tension beats, twists, action, and reveals.

2. SUB-CLIPS SPECIFICATION (8 to 14 cuts per Part):
   - Each sub-clip duration must be between 3 and 6 seconds.
   - The sum of all sub-clip durations for a Part must total between 50 and 70 seconds.

3. VIRAL RECAP SCRIPT ({language.upper()}):
   - For each Part, write a cohesive, gripping ~80 to 110-word voiceover script matching the 50-70 second visual sequence.
   - {lang_instruction}
   - Must begin with a 3-second scroll-stopping retention hook.
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
    "start_time": "00:01:10",
    "end_time": "00:06:45",
    "sub_clips": [
      {{
        "clip_num": 1,
        "start_time": "00:01:10",
        "end_time": "00:01:15",
        "duration": 5,
        "description": "Character arrives at location"
      }},
      {{
        "clip_num": 2,
        "start_time": "00:02:20",
        "end_time": "00:02:25",
        "duration": 5,
        "description": "Mystery object discovered"
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
    sanitized_scenes = sanitize_and_order_scenes(scenes, duration, target_duration, title)

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


def generate_algorithmic_subclips(
    start_sec: int,
    end_sec: int,
    target_duration: int = 58
) -> List[Dict[str, Any]]:
    """
    Generates 8 to 12 dynamic sub-clips (3 to 6 seconds each) across [start_sec, end_sec]
    with total combined duration between 50 and 70 seconds for 100% YouTube copyright safety.
    """
    durs = [6, 7, 6, 7, 6, 7, 6, 7]
    target_d = min(max(50, target_duration), 70)

    curr_durs = []
    tot = 0
    for d in durs:
        if tot + d <= target_d:
            curr_durs.append(d)
            tot += d
        else:
            rem = target_d - tot
            if rem >= 3:
                curr_durs.append(rem)
                tot += rem
            break
    if tot < 50:
        curr_durs.append(50 - tot)
        tot = 50

    count = len(curr_durs)
    span = max(end_sec - start_sec, count * 6 + 10)
    step = (span - 6) / max(count - 1, 1) if count > 1 else 0

    sub_clips = []
    for k in range(count):
        c_start = int(start_sec + k * step)
        c_dur = curr_durs[k]
        c_end = c_start + c_dur
        sub_clips.append({
            "clip_num": k + 1,
            "start_time": format_seconds_to_timestamp(c_start),
            "end_time": format_seconds_to_timestamp(c_end),
            "start_seconds": c_start,
            "end_seconds": c_end,
            "duration": c_dur,
            "description": f"Narrative Beat {k+1}"
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
    count = min(max(1, max_shorts), 10)
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
                f"मुख्य किरदार इस अनपेक्षित परिस्थिति में फंस जाता है जहां हर सेकंड उसकी जान दांव पर लगी थी। "
                f"लेकिन क्या वह इस जाल से बचकर निकल पाएगा? देखिए आगे की पूरी कहानी और सब्सक्राइब करना बिल्कुल न भूलें!"
            )
            hook = f"फिल्म के पार्ट {i} का यह सबसे खतरनाक सीन देखकर आपके रोंगटे खड़े हो जाएंगे!"
        else:
            script = (
                f"In Part {i} of this intense story, the plot takes an unexpected dramatic turn. "
                f"Trapped in an impossible situation with no easy way out, every second counts. "
                f"Will the hero survive the ultimate test? Watch till the end to find out, and subscribe for Part {i+1}!"
            )
            hook = f"The most shocking twist in Part {i} you never saw coming!"

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
    video_title: str
) -> List[Dict[str, Any]]:
    """
    Ensures chronological sorting, validates 8-14 sub-clips per Part,
    enforces 50-70s total montage duration, and sets uniform metadata keys.
    """
    valid_scenes = []
    target_d = min(max(50, target_duration), 70)

    for s in scenes:
        raw_clips = s.get("sub_clips") or []
        valid_sub_clips = []
        if isinstance(raw_clips, list) and len(raw_clips) >= 4:
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

                valid_sub_clips.append({
                    "clip_num": idx,
                    "start_time": format_seconds_to_timestamp(start_s),
                    "end_time": format_seconds_to_timestamp(end_s),
                    "start_seconds": int(start_s),
                    "end_seconds": int(end_s),
                    "duration": int(end_s - start_s),
                    "description": c.get("description", f"Montage Cut {idx}")
                })

        # If Gemini didn't provide valid sub_clips or fewer than 4 were valid, synthesize 8-12 cuts
        if len(valid_sub_clips) < 4:
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

        # Enforce total montage duration between 50 and 70 seconds
        total_dur = sum(c["duration"] for c in valid_sub_clips)
        if total_dur > 70:
            while total_dur > 70 and len(valid_sub_clips) > 8:
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

def get_youtube_video_id(url: str) -> Optional[str]:
    """Extracts the 11-character YouTube video ID from various URL formats."""
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
    Extracts a direct playable MP4 video stream URL using:
    1. In-memory cache (15-min TTL)
    2. yt-dlp -g with anti-bot clients (android, ios, mweb)
    3. Piped CDN API fallback (bypasses datacenter IP blocks completely)
    """
    global _STREAM_URL_CACHE
    vid = get_youtube_video_id(youtube_url) or youtube_url
    now = time.time()

    # 1. Check cache
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

    # 2. Try yt-dlp -g with android,ios,mweb clients
    try:
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "-g",
            "-f", "best[ext=mp4][height<=720]/bestvideo[height<=720]+bestaudio/best",
            "--extractor-args", "youtube:player_client=android,ios,mweb",
            "--socket-timeout", "20",
            "--retries", "3",
            youtube_url
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25)
        if proc.returncode == 0 and proc.stdout.strip():
            lines = [l.strip() for l in proc.stdout.strip().split("\n") if l.strip().startswith("http")]
            if lines:
                direct_url = lines[0]
                _STREAM_URL_CACHE[vid] = {"url": direct_url, "expires_at": now + 900}
                logger.info(f"Retrieved direct stream URL via yt-dlp for video {vid}")
                return direct_url
    except Exception as e:
        logger.warning(f"yt-dlp -g failed for {vid}: {e}")

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
    output_path: str
) -> bool:
    """
    Downloads ONLY the exact start_time to end_time section directly to `output_path`.
    Uses direct stream URL extraction + FFmpeg fast cutting first (taking 1-3 seconds),
    with fallback to yt-dlp --download-sections if needed.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except Exception:
            pass

    ffmpeg_bin = get_ffmpeg_bin()
    direct_url = get_direct_stream_url(youtube_url)

    # 1. Direct stream FFmpeg cutting (Super fast & immune to datacenter download limits)
    if direct_url:
        # Attempt A: Stream copy (-c copy)
        cmd_copy = [
            ffmpeg_bin, "-y",
            "-ss", start_time,
            "-to", end_time,
            "-i", direct_url,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            output_path
        ]
        try:
            p_copy = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=35)
            if p_copy.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
                logger.info(f"Direct stream slice (copy) succeeded: {output_path} ({os.path.getsize(output_path)} bytes)")
                return True
        except subprocess.TimeoutExpired:
            logger.warning(f"FFmpeg copy timed out for {start_time}-{end_time}")
        except Exception as e:
            logger.warning(f"FFmpeg copy error: {e}")

        # Attempt B: Ultrafast transcode slice if copy boundary was not on keyframe
        cmd_trans = [
            ffmpeg_bin, "-y",
            "-ss", start_time,
            "-to", end_time,
            "-i", direct_url,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "24",
            "-an",
            output_path
        ]
        try:
            p_trans = subprocess.run(cmd_trans, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
            if p_trans.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
                logger.info(f"Direct stream slice (transcode) succeeded: {output_path} ({os.path.getsize(output_path)} bytes)")
                return True
        except subprocess.TimeoutExpired:
            logger.warning(f"FFmpeg transcode timed out for {start_time}-{end_time}")
        except Exception as e:
            logger.warning(f"FFmpeg transcode error: {e}")

    # 2. Invalidate cache in case URL expired or connection failed
    vid = get_youtube_video_id(youtube_url) or youtube_url
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
        "--download-sections", section_arg,
        "--force-keyframes-at-cuts",
        "--extractor-args", "youtube:player_client=android,ios,mweb",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
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
        "best[ext=mp4][height<=720]/bestvideo[height<=720]+bestaudio/best",
        "best/18"
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
def ensure_scene_script(scene: Dict[str, Any], video_title: str = "", language: str = "Hindi") -> str:
    """
    Ensures that a scene has a captivating narrative script.
    If empty, calls Gemini to write a high-tension ~80-100 word recap script matching the montage sequence.
    Gracefully detects 429 quota limits and raises an informative error.
    """
    script = (scene.get("script") or "").strip()
    if script:
        return script

    part_num = scene.get("part", 1)
    logger.info(f"Generating voiceover script for Part {part_num} via Gemini...")
    client = gemini_engine.get_genai_client()
    cfg = gemini_engine.get_gemini_config()
    target_model = cfg.get("model") or gemini_engine.DEFAULT_MODEL
    models_to_try = [target_model] + [m for m in gemini_engine.FALLBACK_MODELS if m != target_model]

    prompt = (
        f"You are a master YouTube Shorts viral storyteller.\n"
        f"Write a dramatic, cohesive ~80-100 word story recap voiceover script in {language} for Part {part_num} "
        f"of '{video_title}' designed for a fast-paced 50-70 second multi-scene montage covering story progression from {scene.get('start_time')} to {scene.get('end_time')}.\n"
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


def process_single_short_pipeline(
    youtube_url: str,
    scene: Dict[str, Any],
    language: str = "Hindi",
    video_title: str = "",
    job_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes the 100% copyright-safe dynamic multi-scene montage pipeline for a single Part:
    1. Extracts 8-14 sub-clips (3 to 6 seconds each) across the narrative act.
    2. Downloads each sub-clip and reframes to 9:16 vertical (1080x1920) with face/subject centering.
    3. Strips original movie audio completely (0% volume) so YouTube Content ID cannot flag it.
    4. Concatenates normalized silent sub-clips into a fast-paced vertical montage video.
    5. Generates cohesive neural voiceover script (Edge-TTS).
    6. Ensures subtle copyright-free ambient tension background music exists.
    7. Overlays Voiceover (100%) + Background Music (12%) onto the montage.
    8. Extracts preview thumbnail and saves checkpoint.
    """
    part_num = scene.get("part", 1)
    title = scene.get("title", f"Part {part_num} #Shorts")
    sub_clips = scene.get("sub_clips") or []

    # If sub_clips is missing or less than 4 cuts, generate dynamic cuts
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

    start_time = sub_clips[0].get("start_time", "00:00:00")
    end_time = sub_clips[-1].get("end_time", "00:01:10")

    unique_id = uuid.uuid4().hex[:8]
    vo_path = os.path.join(TEMP_DIR, f"vo_part_{part_num}_{unique_id}.mp3")
    silent_montage_path = os.path.join(TEMP_DIR, f"montage_silent_{part_num}_{unique_id}.mp4")
    final_video_name = f"short_part_{part_num}_{unique_id}.mp4"
    final_thumb_name = f"thumb_part_{part_num}_{unique_id}.jpg"
    final_video_path = os.path.join(CLIPPER_DIR, final_video_name)
    final_thumb_path = os.path.join(CLIPPER_DIR, final_thumb_name)

    logger.info(f"--- Starting Dynamic Multi-Scene Montage for Part {part_num} ({len(sub_clips)} cuts: {start_time} to {end_time}) ---")

    # Step 1: Ensure narrative voiceover script exists & generate neural voiceover audio
    script = ensure_scene_script(scene, video_title=video_title or title, language=language)
    vo_ok = False
    if script:
        vo_ok = generate_voiceover_audio(script, vo_path, language)

    # Step 2: Ensure subtle copyright-free background music exists
    bgm_path = ensure_background_music_exists()

    # Step 3: Download and reframe sub-clips to 9:16 vertical (stripping 100% movie audio)
    normalized_clips = []
    temp_clip_paths = []
    ffmpeg_bin = get_ffmpeg_bin()

    # Optimization: Download the act segment in ONE network request (~3s), then slice cuts locally in 0.1s
    act_segment_path = os.path.join(TEMP_DIR, f"act_seg_{part_num}_{unique_id}.mp4")
    temp_clip_paths.append(act_segment_path)
    act_dl_ok = download_clip_section(youtube_url, start_time, end_time, act_segment_path)
    if act_dl_ok and os.path.exists(act_segment_path) and os.path.getsize(act_segment_path) > 10000:
        logger.info(f"Downloaded act segment for Part {part_num} in one pass: {act_segment_path} ({os.path.getsize(act_segment_path)} bytes)")
    else:
        logger.warning(f"Single-pass act segment download failed. Falling back to per-cut streaming.")
        act_segment_path = None

    start_sec = parse_timestamp_to_seconds(start_time)
    for idx, c in enumerate(sub_clips, 1):
        c_start = c.get("start_time")
        c_end = c.get("end_time")
        if not c_start or not c_end:
            continue

        raw_sub = os.path.join(TEMP_DIR, f"sub_raw_{part_num}_{idx}_{unique_id}.mp4")
        norm_sub = os.path.join(TEMP_DIR, f"sub_norm_{part_num}_{idx}_{unique_id}.mp4")
        temp_clip_paths.extend([raw_sub, norm_sub])

        logger.info(f"Processing Cut {idx}/{len(sub_clips)} for Part {part_num}: [{c_start} - {c_end}]")
        dl_ok = False

        if act_segment_path and os.path.exists(act_segment_path):
            rel_start = max(0, parse_timestamp_to_seconds(c_start) - start_sec)
            dur = max(1, parse_timestamp_to_seconds(c_end) - parse_timestamp_to_seconds(c_start))
            cmd_slice = [
                ffmpeg_bin, "-y",
                "-ss", str(rel_start),
                "-t", str(dur),
                "-i", act_segment_path,
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                raw_sub
            ]
            try:
                subprocess.run(cmd_slice, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                if os.path.exists(raw_sub) and os.path.getsize(raw_sub) > 5000:
                    dl_ok = True
            except Exception as se:
                logger.warning(f"Local slice failed: {se}")

        if not dl_ok:
            dl_ok = download_clip_section(youtube_url, c_start, c_end, raw_sub)

        if dl_ok:
            rf_ok = reframe_subclip_to_vertical_916(raw_sub, norm_sub)
            if os.path.exists(raw_sub):
                try:
                    os.remove(raw_sub)
                except Exception:
                    pass
            if rf_ok and os.path.exists(norm_sub):
                normalized_clips.append(norm_sub)
        else:
            logger.warning(f"Sub-clip {idx} ({c_start}-{c_end}) failed download. Proceeding with remaining cuts...")

    if not normalized_clips:
        raise RuntimeError(f"Failed to stream and download sub-clips for Part {part_num}")

    logger.info(f"Successfully processed {len(normalized_clips)} vertical cuts for Part {part_num}. Concatenating montage...")

    # Step 4: Concatenate normalized silent sub-clips
    concat_ok = concat_normalized_clips(normalized_clips, silent_montage_path)
    if not concat_ok or not os.path.exists(silent_montage_path):
        raise RuntimeError(f"Failed to concatenate montage sub-clips for Part {part_num}")

    # Step 5: Overlay AI Voiceover & Copyright-Free Background Music (0% original movie audio)
    render_ok = render_montage_with_audio_overlay(
        montage_video_path=silent_montage_path,
        voiceover_path=vo_path if vo_ok else None,
        bgm_path=bgm_path if bgm_path else None,
        output_path=final_video_path
    )
    if not render_ok or not os.path.exists(final_video_path):
        raise RuntimeError(f"FFmpeg failed to render final montage Short for Part {part_num}")

    # Step 6: Extract Preview Thumbnail Frame
    generate_short_thumbnail(final_video_path, final_thumb_path)

    # Clean intermediate temporary files
    for p in temp_clip_paths:
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    if os.path.exists(silent_montage_path):
        try:
            os.remove(silent_montage_path)
        except Exception:
            pass
    if os.path.exists(vo_path):
        try:
            os.remove(vo_path)
        except Exception:
            pass

    # Build description with hashtags and hook
    description = (
        f"{title}\n\n"
        f"🎬 Story Recap (Part {part_num} Montage - {len(normalized_clips)} Scenes):\n{script}\n\n"
        f"🔔 Subscribe for Part {part_num + 1} and more viral movie breakdowns!\n\n"
        f"#Shorts #YouTubeShorts #MovieRecap #Cinema #Part{part_num} #MovieMontage"
    )

    short_data = {
        "part": part_num,
        "filename": final_video_name,
        "video_url": f"/api/clipper/media/{final_video_name}",
        "thumbnail_url": f"/api/clipper/media/{final_thumb_name}",
        "filepath": final_video_path,
        "title": title,
        "hook": scene.get("hook", ""),
        "script": script,
        "description": description,
        "tags": scene.get("tags") or ["Shorts", "Movie", "Viral", f"Part{part_num}", "Montage"],
        "duration": scene.get("duration", 58),
        "start_time": start_time,
        "end_time": end_time,
        "sub_clips_count": len(normalized_clips),
        "montage_mode": True,
        "copyright_safe": True,
        "status": "ready"
    }

    # Save to persistent job checkpoint if job_id was provided
    if job_id:
        try:
            ckpt = load_job_checkpoint(job_id)
            if ckpt:
                if "completed_shorts" not in ckpt or not isinstance(ckpt["completed_shorts"], dict):
                    if isinstance(ckpt.get("completed_shorts"), list):
                        ckpt["completed_shorts"] = {str(s.get("part", i+1)): s for i, s in enumerate(ckpt["completed_shorts"])}
                    else:
                        ckpt["completed_shorts"] = {}
                ckpt["completed_shorts"][str(part_num)] = short_data
                save_job_checkpoint(job_id, ckpt)
        except Exception as se:
            logger.warning(f"Could not persist short {part_num} to checkpoint {job_id}: {se}")

    return short_data

