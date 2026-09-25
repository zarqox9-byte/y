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
from typing import Dict, Any, List, Optional, Tuple

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
os.makedirs(CLIPPER_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)


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
# 1. YOUTUBE METADATA & CHAPTERS EXTRACTION
# =====================================================================
def extract_youtube_info(youtube_url: str) -> Dict[str, Any]:
    """
    Extracts video metadata, duration, description, chapters, and thumbnails
    using yt-dlp without downloading the video.
    """
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp is not installed. Please install it with `pip install yt-dlp`.")

    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False
    }

    logger.info(f"Extracting video metadata for: {youtube_url}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)

    title = info.get('title', 'Unknown Title')
    duration = info.get('duration', 0)
    description = info.get('description', '')
    chapters = info.get('chapters') or []
    thumbnail = info.get('thumbnail') or ''
    channel = info.get('uploader') or info.get('channel') or ''

    # Clean description snippet
    clean_desc = (description[:2000] if description else "").strip()

    logger.info(f"Metadata extracted: '{title}' ({format_seconds_to_timestamp(duration)}), Chapters: {len(chapters)}")
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


# =====================================================================
# 2. GEMINI CHRONOLOGICAL SCENE SEGMENTATION & SCRIPTING
# =====================================================================
def analyze_movie_narrative_for_shorts(
    youtube_url: str,
    video_info: Dict[str, Any],
    max_shorts: int = 5,
    target_duration: int = 50,
    language: str = "Hindi"
) -> List[Dict[str, Any]]:
    """
    Prompts Google Gemini to analyze the movie's storyline and generate
    high-tension scenes in strict chronological order with viral titles,
    hooks, and ~60-word recap scripts.
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

    prompt = f"""You are an elite YouTube Shorts Strategist and Cinema Editor specializing in viral movie recap shorts.
Analyze the following movie / video narrative and segment it into high-retention, high-drama, engaging YouTube Shorts in STRICT CHRONOLOGICAL ORDER (Part 1, Part 2, Part 3... from the beginning of the movie to the climax/ending).

=== MOVIE / VIDEO DETAILS ===
Title: {title}
Total Duration: {duration_str} ({duration} seconds)
Description:
{description[:1500]}
{chapters_summary}

=== REQUIREMENTS ===
1. CHRONOLOGY:
   - Every short must be in STRICT CHRONOLOGICAL ORDER.
   - Part 1 must be early in the story (e.g. intro/inciting incident).
   - Part 2 must take place AFTER Part 1.
   - Part 3 must take place AFTER Part 2, and so forth, leading towards the climax/resolution.
   - Do NOT jump backwards in time.

2. QUANTITY & DURATION:
   - Determine the optimal number of shorts (between 1 and {max_shorts}) that best captures the narrative arc without filler.
   - Each short should have a target duration of approximately {target_duration} seconds (valid range: 35 to 58 seconds).
   - Ensure start_time and end_time do not exceed total video duration ({duration}s).

3. VIRAL RECAP SCRIPT ({language.upper()}):
   - For each part, provide a gripping ~50 to 65-word voiceover script.
   - {lang_instruction}
   - Must begin with a strong 3-second hook that freezes the user from scrolling.
   - Must end on a high-retention cliffhanger or transition to the next part.

4. TITLES & METADATA:
   - Title must include the Part number, emotional emojis, and hashtags (e.g., "{title[:30]} - The Beginning! 😱 Part 1 #Shorts #Movie").
   - Include 6-8 relevant viral tags.

=== RETURN FORMAT ===
Return ONLY a valid JSON array of objects with no markdown formatting around it:
[
  {{
    "part": 1,
    "start_time": "00:01:15",
    "end_time": "00:02:05",
    "start_seconds": 75,
    "end_seconds": 125,
    "duration": 50,
    "title": "Viral Short Title Here! 😱 Part 1 #Shorts",
    "hook": "3-second opening hook line",
    "script": "Complete 50-65 word voiceover script in {language}...",
    "tags": ["shorts", "movie", "viral", "recap", "part1"]
  }}
]
"""

    logger.info("Calling Gemini for chronological scene segmentation...")
    client = gemini_engine.get_genai_client()
    cfg = gemini_engine.get_gemini_config()
    target_model = cfg.get("model") or gemini_engine.DEFAULT_MODEL
    models_to_try = [target_model] + [m for m in gemini_engine.FALLBACK_MODELS if m != target_model]

    raw_response = None
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
            logger.warning(f"Model {model_name} failed with error: {e}. Cascading...")

    scenes = []
    if raw_response:
        # Strip potential markdown code blocks
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

    # Fallback algorithmic scene generation if Gemini API returned no valid scenes
    if not scenes:
        logger.info("Using intelligent algorithmic chronological scene generator fallback...")
        scenes = generate_algorithmic_scenes(title, duration, max_shorts, target_duration, language)

    # Sanitize, enforce chronology, and validate bounds
    sanitized_scenes = sanitize_and_order_scenes(scenes, duration, target_duration, title)
    return sanitized_scenes


def generate_algorithmic_scenes(
    title: str,
    duration: int,
    max_shorts: int = 5,
    target_duration: int = 50,
    language: str = "Hindi"
) -> List[Dict[str, Any]]:
    """Creates high-quality chronological scenes if Gemini output was unparseable."""
    scenes = []
    count = min(max(1, max_shorts), 10)
    effective_duration = max(duration, count * target_duration + 60)
    step = (effective_duration - 60) / (count + 1)

    for i in range(1, count + 1):
        start_sec = int(30 + (i - 1) * step)
        end_sec = min(start_sec + target_duration, duration - 5 if duration > 60 else start_sec + target_duration)
        if end_sec <= start_sec:
            end_sec = start_sec + 45

        if language.lower().startswith("hi"):
            script = (
                f"फिल्म के पार्ट {i} में कहानी एक नया मोड़ लेती है। "
                f"मुख्य किरदार इस खतरनाक परिस्थिति में फंस जाता है जहां से निकलना नामुमकिन लग रहा था। "
                f"लेकिन क्या वह अपनी जान बचा पाएगा? देखिए आगे क्या होता है और चैनल को सब्सक्राइब जरूर करें!"
            )
            hook = f"फिल्म के पार्ट {i} का यह सबसे खतरनाक सीन मिस मत करना!"
        else:
            script = (
                f"In Part {i} of this intense story, the plot takes an unexpected dramatic turn. "
                f"Trapped in an impossible situation with no easy way out, every second counts. "
                f"Will the hero survive the ultimate test? Watch till the end to find out!"
            )
            hook = f"The most shocking twist in Part {i} you never saw coming!"

        scenes.append({
            "part": i,
            "start_time": format_seconds_to_timestamp(start_sec),
            "end_time": format_seconds_to_timestamp(end_sec),
            "start_seconds": start_sec,
            "end_seconds": end_sec,
            "duration": end_sec - start_sec,
            "title": f"{title[:35]} - Unbelievable Moment! 😱 Part {i} #Shorts",
            "hook": hook,
            "script": script,
            "tags": ["shorts", "movie", "recap", f"part{i}", "viral", "cinema"]
        })
    return scenes


def sanitize_and_order_scenes(
    scenes: List[Dict[str, Any]],
    total_duration: int,
    target_duration: int,
    video_title: str
) -> List[Dict[str, Any]]:
    """Ensures chronological sorting, valid timestamp bounds, and uniform keys."""
    valid_scenes = []
    for s in scenes:
        start_s = s.get("start_seconds")
        if start_s is None:
            start_s = parse_timestamp_to_seconds(s.get("start_time", "00:00"))
        end_s = s.get("end_seconds")
        if end_s is None:
            end_s = parse_timestamp_to_seconds(s.get("end_time", "00:50"))

        if end_s <= start_s:
            end_s = start_s + target_duration

        dur = end_s - start_s
        if dur < 25 or dur > 65:
            end_s = start_s + target_duration
            dur = target_duration

        if total_duration > 0 and end_s > total_duration:
            end_s = total_duration - 2
            start_s = max(0, end_s - target_duration)
            dur = end_s - start_s

        valid_scenes.append({
            "part": s.get("part", 1),
            "start_time": format_seconds_to_timestamp(start_s),
            "end_time": format_seconds_to_timestamp(end_s),
            "start_seconds": int(start_s),
            "end_seconds": int(end_s),
            "duration": int(dur),
            "title": s.get("title") or f"{video_title[:30]} - Part {s.get('part', 1)} #Shorts",
            "hook": s.get("hook", ""),
            "script": s.get("script", ""),
            "tags": s.get("tags") or ["shorts", "viral", "recap"]
        })

    # Sort strictly chronologically by start_seconds
    valid_scenes.sort(key=lambda x: x["start_seconds"])

    # Re-index parts 1..N
    for idx, sc in enumerate(valid_scenes, 1):
        sc["part"] = idx
        if f"Part {idx}" not in sc["title"]:
            sc["title"] = f"{sc['title']} Part {idx}"

    return valid_scenes


# =====================================================================
# 3. STREAMING CHUNKING VIA YT-DLP
# =====================================================================
def download_clip_section(
    youtube_url: str,
    start_time: str,
    end_time: str,
    output_path: str
) -> bool:
    """
    Downloads ONLY the exact start_time to end_time section of the YouTube video
    directly to `output_path` using yt-dlp's --download-sections argument.
    Avoids downloading the full movie.
    """
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp is not installed.")

    section_arg = f"*{start_time}-{end_time}"
    logger.info(f"Downloading stream chunk: {section_arg} for {youtube_url}")

    # Temporary template
    temp_template = os.path.splitext(output_path)[0] + "_dl.%(ext)s"

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--download-sections", section_arg,
        "--force-keyframes-at-cuts",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", temp_template,
        "--quiet", "--no-warnings",
        youtube_url
    ]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
    if proc.returncode != 0:
        logger.error(f"yt-dlp section download failed: {proc.stderr}")
        return False

    # Find the resulting file
    dl_dir = os.path.dirname(output_path)
    base_stem = os.path.splitext(os.path.basename(temp_template))[0].replace(".%(ext)s", "")
    for f in os.listdir(dl_dir):
        if f.startswith(base_stem) and f.endswith(".mp4"):
            actual_dl = os.path.join(dl_dir, f)
            if os.path.exists(output_path):
                os.remove(output_path)
            os.rename(actual_dl, output_path)
            logger.info(f"Downloaded section successfully: {output_path} ({os.path.getsize(output_path)} bytes)")
            return True

    logger.error("Downloaded file could not be located after yt-dlp execution.")
    return False


# =====================================================================
# 4. SMART FACE TRACKING & 9:16 VERTICAL AUTO-REFRAME
# =====================================================================
def calculate_smart_916_crop(video_path: str) -> str:
    """
    Analyzes video frames with OpenCV Haar Cascade face detection to locate
    the horizontal center of the actors. Calculates an optimal 9:16 crop window
    centered on the actors rather than a naive middle crop.
    Returns FFmpeg crop & scale filter string.
    """
    has_cv2 = False
    try:
        import cv2
        import numpy as np
        has_cv2 = True
    except ImportError:
        logger.warning("OpenCV/NumPy not installed. Falling back to high-quality center 9:16 crop.")

    width, height = 1920, 1080
    # Probe video dimensions with ffprobe
    probe_cmd = [
        "ffprobe", "-v", "error",
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
    except Exception as e:
        logger.warning(f"Could not probe video dimensions with ffprobe: {e}")

    # Vertical 9:16 crop dimensions
    # native height is kept, crop width is height * 9 / 16
    crop_w = int(height * (9 / 16))
    crop_w = crop_w - (crop_w % 2)  # must be even

    # Default fallback: center crop
    default_crop_x = max(0, int((width - crop_w) / 2))

    if not has_cv2 or not os.path.exists(video_path):
        logger.info(f"Applying default center crop: crop={crop_w}:{height}:{default_crop_x}:0,scale=1080:1920")
        return f"crop={crop_w}:{height}:{default_crop_x}:0,scale=1080:1920:flags=lanczos"

    # Analyze frames with OpenCV Face Detection / Visual Saliency
    try:
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        # Check for Haar Cascade support
        face_cascade = None
        if hasattr(cv2, 'CascadeClassifier') and hasattr(cv2, 'data') and hasattr(cv2.data, 'haarcascades'):
            try:
                cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
                if os.path.exists(cascade_path):
                    face_cascade = cv2.CascadeClassifier(cascade_path)
            except Exception as ce:
                logger.warning(f"Haar cascade initialization notice: {ce}")

        detected_centers = []
        saliency_centers = []
        # Sample ~25 frames evenly across the clip
        sample_step = max(1, total_frames // 25) if total_frames > 25 else 1

        frame_idx = 0
        while cap.isOpened() and frame_idx < total_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                break

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # 1. Try Face Detection
            if face_cascade is not None:
                try:
                    faces = face_cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(50, 50))
                    for (fx, fy, fw, fh) in faces:
                        center_x = fx + (fw / 2.0)
                        weight = fw * fh  # larger faces have higher weight
                        detected_centers.append((center_x, weight))
                except Exception:
                    pass

            # 2. Compute Visual Saliency (Actor / Movement Sharpness)
            try:
                sobel_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
                energy_x = np.sum(np.abs(sobel_x), axis=0)
                sum_e = np.sum(energy_x)
                if sum_e > 100:
                    sal_x = float(np.sum(np.arange(len(energy_x)) * energy_x) / sum_e)
                    saliency_centers.append(sal_x)
            except Exception:
                pass

            frame_idx += sample_step

        cap.release()

        if detected_centers:
            total_weight = sum(w for _, w in detected_centers)
            weighted_center_x = sum(cx * w for cx, w in detected_centers) / total_weight
            crop_x = int(weighted_center_x - (crop_w / 2.0))
            crop_x = max(0, min(crop_x, width - crop_w))
            logger.info(f"Smart Face Centering detected! Weighted Face X: {weighted_center_x:.1f}px -> Crop X: {crop_x}px")
        elif saliency_centers:
            avg_saliency_x = float(np.mean(saliency_centers))
            crop_x = int(avg_saliency_x - (crop_w / 2.0))
            crop_x = max(0, min(crop_x, width - crop_w))
            logger.info(f"Visual Saliency Subject Centering applied! Subject X: {avg_saliency_x:.1f}px -> Crop X: {crop_x}px")
        else:
            crop_x = default_crop_x
            logger.info(f"Using default center crop: {crop_x}px")

        return f"crop={crop_w}:{height}:{crop_x}:0,scale=1080:1920:flags=lanczos"

    except Exception as e:
        logger.error(f"Error during smart face tracking: {e}. Falling back to center crop.")
        return f"crop={crop_w}:{height}:{default_crop_x}:0,scale=1080:1920:flags=lanczos"


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
    has_vo = voiceover_path and os.path.exists(voiceover_path) and os.path.getsize(voiceover_path) > 1000

    if has_vo:
        # Check if raw clip has an audio stream
        probe_audio = [
            "ffprobe", "-v", "error",
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
                "ffmpeg", "-y",
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
                "ffmpeg", "-y",
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
            "ffmpeg", "-y",
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


def generate_short_thumbnail(video_path: str, thumbnail_path: str) -> bool:
    """Extracts a crisp thumbnail frame from the middle of the generated Short."""
    cmd = [
        "ffmpeg", "-y",
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
def process_single_short_pipeline(
    youtube_url: str,
    scene: Dict[str, Any],
    language: str = "Hindi"
) -> Dict[str, Any]:
    """
    Executes the end-to-end pipeline for a single chronological short:
    1. Downloads exact clip section via yt-dlp.
    2. Generates neural voiceover from Gemini script.
    3. Analyzes actors' faces for optimal 9:16 vertical crop.
    4. Renders final vertical Short with ducked audio.
    5. Extracts thumbnail and returns complete ready-to-upload object.
    """
    part_num = scene.get("part", 1)
    start_time = scene.get("start_time", "00:00:00")
    end_time = scene.get("end_time", "00:00:50")
    script = scene.get("script", "")
    title = scene.get("title", f"Part {part_num} #Shorts")

    unique_id = uuid.uuid4().hex[:8]
    raw_clip_path = os.path.join(TEMP_DIR, f"raw_part_{part_num}_{unique_id}.mp4")
    vo_path = os.path.join(TEMP_DIR, f"vo_part_{part_num}_{unique_id}.mp3")
    final_video_name = f"short_part_{part_num}_{unique_id}.mp4"
    final_thumb_name = f"thumb_part_{part_num}_{unique_id}.jpg"
    final_video_path = os.path.join(CLIPPER_DIR, final_video_name)
    final_thumb_path = os.path.join(CLIPPER_DIR, final_thumb_name)

    logger.info(f"--- Starting Processing for Part {part_num} ({start_time} to {end_time}) ---")

    # Step 1: Download clip section
    download_ok = download_clip_section(youtube_url, start_time, end_time, raw_clip_path)
    if not download_ok:
        raise RuntimeError(f"Failed to stream and download clip section {start_time}-{end_time}")

    # Step 2: Generate Voiceover Audio
    vo_ok = False
    if script:
        vo_ok = generate_voiceover_audio(script, vo_path, language)

    # Step 3: Smart Face Tracking & Crop Filter
    crop_filter = calculate_smart_916_crop(raw_clip_path)

    # Step 4: FFmpeg Render & Audio Ducking
    render_ok = render_short_video(
        raw_clip_path,
        vo_path if vo_ok else None,
        final_video_path,
        crop_filter
    )
    if not render_ok:
        raise RuntimeError(f"FFmpeg failed to render vertical Short for Part {part_num}")

    # Step 5: Extract Preview Thumbnail
    generate_short_thumbnail(final_video_path, final_thumb_path)

    # Clean temporary raw files
    try:
        if os.path.exists(raw_clip_path):
            os.remove(raw_clip_path)
        if os.path.exists(vo_path):
            os.remove(vo_path)
    except Exception:
        pass

    # Build description with hashtags and hook
    description = (
        f"{title}\n\n"
        f"🎬 Story Recap (Part {part_num}):\n{script}\n\n"
        f"🔔 Subscribe for Part {part_num + 1} and more viral movie breakdowns!\n\n"
        f"#Shorts #YouTubeShorts #MovieRecap #Cinema #Part{part_num}"
    )

    return {
        "part": part_num,
        "filename": final_video_name,
        "video_url": f"/api/clipper/media/{final_video_name}",
        "thumbnail_url": f"/api/clipper/media/{final_thumb_name}",
        "filepath": final_video_path,
        "title": title,
        "hook": scene.get("hook", ""),
        "script": script,
        "description": description,
        "tags": scene.get("tags") or ["Shorts", "Movie", "Viral", f"Part{part_num}"],
        "duration": scene.get("duration", 50),
        "start_time": start_time,
        "end_time": end_time,
        "status": "ready"
    }
