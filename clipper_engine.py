"""
clipper_engine.py - Lightweight 1-Minute Episodic Shorts Engine
===============================================================
Clean, memory-safe backend engine for YouTube Studio Pro:
1. Extracts ground-truth plot, character identities, chapters, and subtitles from a YouTube Official URL
   (using youtube-transcript-api, timedtext JSON3 captions, YouTube Data API v3, and oEmbed — zero YouTube video downloads).
2. Uses Google Gemini (with multi-key channel rotation, Multimodal YouTube URL analysis, and Google Search Grounding)
   to generate an authentic 60-second Hindi suspense script (140-150 words, character-only names) and
   10 to 12 fast, dynamic visual scene cuts (each 4-6s, totaling ~60s) for Part 1, Part 2, Part 3...
3. Slices and exports the combined 1-minute video clip (muted audio `-an`, optimized for CapCut) from a local video file.
"""

import os
import re
import json
import uuid
import time
import math
import logging
import subprocess
import shutil
import urllib.request
import urllib.parse
from typing import Dict, Any, List, Optional


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
    ch.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [EpisodicShorts] %(message)s"))
    logger.addHandler(ch)

import gemini_engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "uploads", "clipper_temp")
TRIMMER_VIDEOS_DIR = os.path.join(BASE_DIR, "uploads", "trimmer_videos")
TRIMMER_EXPORTS_DIR = os.path.join(BASE_DIR, "uploads", "trimmer_exports")
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(TRIMMER_VIDEOS_DIR, exist_ok=True)
os.makedirs(TRIMMER_EXPORTS_DIR, exist_ok=True)


def format_seconds_to_timestamp(seconds: float) -> str:
    """Converts seconds float to HH:MM:SS or MM:SS format."""
    total_sec = max(0, int(round(float(seconds or 0))))
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def parse_timestamp_to_seconds(ts: Any) -> float:
    """Parses HH:MM:SS, MM:SS, or numeric seconds into float seconds."""
    if isinstance(ts, (int, float)):
        return max(0.0, float(ts))
    ts_str = str(ts or "").strip()
    if not ts_str:
        return 0.0
    parts = ts_str.split(":")
    try:
        if len(parts) == 3:
            return float(int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2]))
        elif len(parts) == 2:
            return float(int(parts[0]) * 60 + float(parts[1]))
        elif len(parts) == 1:
            return max(0.0, float(parts[0]))
    except Exception:
        pass
    return 0.0


# =====================================================================
# 1. YOUTUBE GROUND-TRUTH METADATA & SUBTITLE EXTRACTION (NO VIDEO DL)
# =====================================================================
def extract_video_id(url: str) -> Optional[str]:
    """Extracts 11-character YouTube video ID from various URL formats."""
    patterns = [
        r'(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})',
        r'^[A-Za-z0-9_-]{11}$'
    ]
    for pattern in patterns:
        match = re.search(pattern, str(url or "").strip())
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
    """Extracts timestamped chapter markers from description text."""
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

    for i in range(len(chapters)):
        if i < len(chapters) - 1:
            chapters[i]['end_time'] = chapters[i + 1]['start_time']
        else:
            chapters[i]['end_time'] = total_duration if total_duration > chapters[i]['start_time'] else chapters[i]['start_time'] + 60
    return chapters


def get_youtube_data_api_client(credentials=None):
    """Creates an authenticated YouTube Data API v3 service for official metadata lookup."""
    try:
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None

    creds = credentials
    if not creds:
        token_path = os.path.join(BASE_DIR, "token.json")
        if os.path.exists(token_path):
            try:
                with open(token_path, "r", encoding="utf-8") as f:
                    token_data = json.load(f)
                creds = Credentials(**token_data)
            except Exception:
                pass

    if not creds:
        for acc_path in [
            os.path.join(BASE_DIR, "user_accounts.json"),
            os.path.join(BASE_DIR, "uploads", "user_accounts.json"),
            os.path.join(BASE_DIR, "accounts.json"),
            os.path.join(BASE_DIR, "uploads", "accounts.json")
        ]:
            if os.path.exists(acc_path):
                try:
                    with open(acc_path, "r", encoding="utf-8") as f:
                        acc_data = json.load(f)
                    if acc_data and isinstance(acc_data, dict):
                        for acc_entry in acc_data.values():
                            if isinstance(acc_entry, dict) and isinstance(acc_entry.get("credentials"), dict):
                                creds = Credentials(**acc_entry["credentials"])
                                break
                        if creds:
                            break
                except Exception:
                    pass

    if not creds and os.environ.get("YOUTUBE_TOKEN_JSON"):
        try:
            token_data = json.loads(os.environ["YOUTUBE_TOKEN_JSON"])
            creds = Credentials(**token_data)
        except Exception:
            pass

    if creds and hasattr(creds, 'expired') and creds.expired and getattr(creds, 'refresh_token', None):
        try:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        except Exception:
            pass

    if creds:
        try:
            return build("youtube", "v3", credentials=creds)
        except Exception:
            pass

    yt_api_key = os.environ.get("YOUTUBE_API_KEY")
    if yt_api_key:
        try:
            return build("youtube", "v3", developerKey=yt_api_key)
        except Exception:
            pass

    return None


def extract_youtube_info(youtube_url: str, credentials=None) -> Dict[str, Any]:
    """
    Extracts ground-truth video metadata, duration, description, and chapters from YouTube URL.
    Never downloads video streams. Uses YouTube Data API v3, oEmbed, and public watch page metadata.
    """
    video_id = extract_video_id(youtube_url)

    # 1. Official YouTube Data API v3 (Fastest & 100% reliable on cloud servers)
    if video_id:
        try:
            yt_service = get_youtube_data_api_client(credentials=credentials)
            if yt_service:
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
                    thumbs = snippet.get('thumbnails', {})
                    thumbnail = (
                        thumbs.get('maxres', {}).get('url') or
                        thumbs.get('standard', {}).get('url') or
                        thumbs.get('high', {}).get('url') or
                        thumbs.get('medium', {}).get('url') or
                        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
                    )
                    chapters = extract_chapters_from_description(description, duration)
                    return {
                        "url": youtube_url,
                        "video_id": video_id,
                        "title": title,
                        "duration": duration if duration > 0 else 7200,
                        "duration_str": format_seconds_to_timestamp(duration if duration > 0 else 7200),
                        "description": (description[:4500] if description else "").strip(),
                        "chapters": chapters,
                        "thumbnail": thumbnail,
                        "channel": channel
                    }
        except Exception as api_err:
            logger.warning(f"YouTube Data API v3 notice: {api_err}")

    # 2. Resilient oEmbed + Watch Page JSON scrape
    oembed_title = ""
    oembed_author = ""
    oembed_thumb = ""
    try:
        oe_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(youtube_url, safe=':/?=&')}&format=json"
        req = urllib.request.Request(
            oe_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            oe_data = json.loads(resp.read().decode('utf-8', errors='ignore'))
            oembed_title = oe_data.get('title', '')
            oembed_author = oe_data.get('author_name', '')
            oembed_thumb = oe_data.get('thumbnail_url', '')
    except Exception:
        pass

    scraped_duration = 0
    scraped_desc = ""
    if video_id:
        try:
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
            req = urllib.request.Request(
                watch_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                page_text = resp.read().decode('utf-8', errors='ignore')
                dur_m = re.search(r'"approxDurationMs"\s*:\s*"(\d+)"', page_text)
                if dur_m:
                    scraped_duration = int(dur_m.group(1)) // 1000
                desc_m = re.search(r'"shortDescription"\s*:\s*"(.*?)"', page_text)
                if desc_m:
                    scraped_desc = desc_m.group(1).encode('utf-8').decode('unicode_escape', errors='ignore')
                if not oembed_title:
                    title_m = re.search(r'"title"\s*:\s*"(.*?)"', page_text)
                    if title_m:
                        oembed_title = title_m.group(1).encode('utf-8').decode('unicode_escape', errors='ignore')
        except Exception:
            pass

    final_title = oembed_title or (f"Movie Storyline ({video_id})" if video_id else "Movie Storyline")
    final_duration = scraped_duration if scraped_duration > 60 else 7200
    final_channel = oembed_author or "YouTube"
    final_thumb = oembed_thumb or (f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg" if video_id else "")
    final_desc = scraped_desc or ""
    chapters = extract_chapters_from_description(final_desc, final_duration)

    return {
        "url": youtube_url,
        "video_id": video_id or "",
        "title": final_title,
        "duration": final_duration,
        "duration_str": format_seconds_to_timestamp(final_duration),
        "description": final_desc[:4500],
        "chapters": chapters,
        "thumbnail": final_thumb,
        "channel": final_channel
    }


# =====================================================================
# 2. STRICT CHARACTER-ONLY NAMING POLICY
# =====================================================================
STRICT_CHARACTER_ONLY_NAMING_RULE = """=== STRICT CHARACTER-ONLY NAMING RULE (ZERO REAL ACTOR / CELEBRITY NAMES) ===
1. NEVER mention real-life actors, actresses, directors, producers, or celebrity names anywhere in the script or scene descriptions (e.g., strictly BAN real names like "Akshay Kumar", "Salman Khan", "Shah Rukh Khan", "Aamir Khan", "Ajay Devgn", "Allu Arjun", "Prabhas", "Rajinikanth", "Vijay", "Hrithik Roshan", "Ranbir Kapoor", "Sunny Deol", "Deepika Padukone", "Alia Bhatt", or their Hindi/Devanagari forms like "अक्षय कुमार", "सलमान खान", "शाहरुख खान", etc.).
2. ALWAYS refer to people strictly by their IN-MOVIE FICTIONAL CHARACTER NAMES (e.g., "बहत्तर सिंह", "इंदु", "तात्या", "कबीर", "विक्रम", "राजू") whenever fictional names exist in the story.
3. If fictional character names are unknown for a scene, refer to them PURELY by their in-universe archetype role in Hindi ("नायक", "अन्वेषक", "वह रहस्यमयी इंसान", "वह साया", "मुसाफ़िर", "अधिकारी")."""

_BANNED_MALE_ACTORS = [
    ("Akshay Kumar", "अक्षय कुमार"),
    ("Salman Khan", "सलमान खान"),
    ("Shah Rukh Khan", "शाहरुख खान"),
    ("Shahrukh Khan", "शाहरुख़ खान"),
    ("Aamir Khan", "आमिर खान"),
    ("Ajay Devgn", "अजय देवगन"),
    ("Ajay Devgan", "अजय देवगन"),
    ("Allu Arjun", "अल्लू अर्जुन"),
    ("Prabhas", "प्रभास"),
    ("Rajinikanth", "रजनीकांत"),
    ("Thalapathy Vijay", "थलापति विजय"),
    ("Hrithik Roshan", "ऋतिक रोशन"),
    ("Ranbir Kapoor", "रणबीर कपूर"),
    ("Ranveer Singh", "रणवीर सिंह"),
    ("Sunny Deol", "सनी देओल"),
    ("Bobby Deol", "बॉबी देओल"),
    ("Kartik Aaryan", "कार्तिक आर्यन"),
    ("Varun Dhawan", "वरुण धवन"),
    ("Tiger Shroff", "टाइगर श्रॉफ"),
    ("John Abraham", "जॉन अब्राहम"),
    ("Saif Ali Khan", "सैफ अली खान"),
    ("Nawazuddin Siddiqui", "नवाजुद्दीन सिद्दीकी"),
    ("Manoj Bajpayee", "मनोज बाजपेयी"),
    ("Pankaj Tripathi", "पंकज त्रिपाठी"),
    ("Vicky Kaushal", "विक्की कौशल"),
    ("Ayushmann Khurrana", "आयुष्मान खुराना"),
    ("Rajkummar Rao", "राजकुमार राव"),
    ("Shahid Kapoor", "शाहिद कपूर"),
    ("Sanjay Dutt", "संजय दत्त"),
    ("Amitabh Bachchan", "अमिताभ बच्चन"),
    ("Anil Kapoor", "अनिल कपूर"),
    ("Jackie Shroff", "जैकी श्रॉफ"),
    ("Suniel Shetty", "सुनील शेट्टी"),
    ("Govinda", "गोविंदा"),
    ("Mithun Chakraborty", "मिथुन चक्रवर्ती"),
    ("Ram Charan", "राम चरण"),
    ("Jr NTR", "जूनियर एनटीआर"),
    ("Mahesh Babu", "महेश बाबू"),
    ("Dhanush", "धनुष"),
    ("Suriya", "सूर्या"),
    ("Kamal Haasan", "कमल हासन"),
    ("Yash", "यश"),
    ("Rishab Shetty", "ऋषभ शेट्टी"),
]

_BANNED_FEMALE_ACTORS = [
    ("Deepika Padukone", "दीपिका पादुकोण"),
    ("Alia Bhatt", "आलिया भट्ट"),
    ("Katrina Kaif", "कैटरीना कैफ"),
    ("Kareena Kapoor", "करीना कपूर"),
    ("Priyanka Chopra", "प्रियंका चोपड़ा"),
    ("Anushka Sharma", "अनुष्का शर्मा"),
    ("Kriti Sanon", "कृति सेनन"),
    ("Kiara Advani", "कियारा आडवाणी"),
    ("Shraddha Kapoor", "श्रद्धा कपूर"),
    ("Rashmika Mandanna", "रश्मिका मंदाना"),
    ("Samantha Ruth Prabhu", "सामंथा रुथ प्रभु"),
    ("Nayanthara", "नयनतारा"),
    ("Tamannaah Bhatia", "तमन्ना भाटिया"),
    ("Taapsee Pannu", "तापसी पन्नू"),
    ("Vidya Balan", "विद्या बालन"),
    ("Madhuri Dixit", "माधुरी दीक्षित"),
    ("Kajol", "काजोल"),
    ("Rani Mukerji", "रानी मुखर्जी"),
    ("Aishwarya Rai", "ऐश्वर्या राय"),
]


def sanitize_actor_names_to_character_roles(text: str, language: str = "Hindi") -> str:
    """Scrubs real-life actor/celebrity/director names and replaces them with in-story character roles."""
    if not text or not isinstance(text, str):
        return ""
    out = text
    is_hi = language.lower().startswith("hi")
    male_role = "नायक" if is_hi else "the protagonist"
    female_role = "नायिका" if is_hi else "the heroine"

    for en_name, hi_name in _BANNED_MALE_ACTORS:
        if en_name and len(en_name) > 4:
            out = re.sub(rf"\b{re.escape(en_name)}\b", male_role, out, flags=re.IGNORECASE)
        if hi_name:
            out = out.replace(hi_name, male_role)

    for en_name, hi_name in _BANNED_FEMALE_ACTORS:
        if en_name:
            out = re.sub(rf"\b{re.escape(en_name)}\b", female_role, out, flags=re.IGNORECASE)
        if hi_name:
            out = out.replace(hi_name, female_role)

    banned_directors = [
        ("Rohit Shetty", "रोहित शेट्टी"),
        ("S. S. Rajamouli", "राजामौली"),
        ("Karan Johar", "करण जौहर"),
        ("Sanjay Leela Bhansali", "संजय लीला भंसाली"),
        ("Prashanth Neel", "प्रशांत नील"),
        ("Lokesh Kanagaraj", "लोकेश कनगराज"),
        ("Sandeep Reddy Vanga", "संदीप रेड्डी वांगा"),
        ("Christopher Nolan", "क्रिस्टोफर नोलन"),
    ]
    for en_d, hi_d in banned_directors:
        out = re.sub(rf"\b{re.escape(en_d)}\b", male_role, out, flags=re.IGNORECASE)
        if hi_d:
            out = out.replace(hi_d, male_role)

    out = re.sub(r"\b(actor|actress|superstar|megastar|directed by|director)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?", male_role, out, flags=re.IGNORECASE)
    out = re.sub(r"\bdirected by\b", "", out, flags=re.IGNORECASE)
    if is_hi:
        out = out.replace("डायरेक्टर", "किरदार").replace("अभिनेता", "नायक").replace("अभिनेत्री", "नायिका")
        out = out.replace("इस फिल्म में", "इस कहानी में").replace("फिल्म", "कहानी")
    return out.strip()


sanitize_character_only_narration = sanitize_actor_names_to_character_roles


# =====================================================================
# 3. GROUND-TRUTH TRANSCRIPT & SUBTITLE EXTRACTION
# =====================================================================
def extract_real_movie_transcript_and_story(
    youtube_url: str = "",
    video_id: Optional[str] = None,
    yt_info: Optional[Dict[str, Any]] = None,
    start_offset_sec: float = 0.0,
    window_end_sec: Optional[float] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Extracts timestamped subtitles/captions from YouTube via `youtube-transcript-api`
    or YouTube `captionTracks` JSON3 timedtext.
    """
    vid = video_id or extract_video_id(youtube_url) or ""
    info = yt_info or {}
    duration = int(info.get("duration") or 7200)
    title = str(info.get("title") or "").strip()
    description = str(info.get("description") or "").strip()
    chapters = info.get("chapters") or []

    transcript_entries: List[Dict[str, Any]] = []
    transcript_source = "metadata_synopsis"

    # Tier 1: youtube-transcript-api
    if vid and len(vid) == 11:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            fetched = None
            if hasattr(YouTubeTranscriptApi, "get_transcript"):
                try:
                    fetched = YouTubeTranscriptApi.get_transcript(
                        vid, languages=["hi", "hi-IN", "en", "en-IN", "en-US"]
                    )
                except Exception:
                    if hasattr(YouTubeTranscriptApi, "list_transcripts"):
                        t_list = YouTubeTranscriptApi.list_transcripts(vid)
                        for tr in t_list:
                            fetched = tr.fetch()
                            if fetched:
                                break
            elif hasattr(YouTubeTranscriptApi, "fetch"):
                fetched = YouTubeTranscriptApi().fetch(vid, languages=["hi", "en"])

            if fetched:
                for item in fetched:
                    if isinstance(item, dict):
                        txt = str(item.get("text", "")).strip()
                        st = float(item.get("start", 0.0))
                        du = float(item.get("duration", 3.0))
                    else:
                        txt = str(getattr(item, "text", "")).strip()
                        st = float(getattr(item, "start", 0.0))
                        du = float(getattr(item, "duration", 3.0))
                    if txt and txt not in ["[Music]", "[Applause]", "[संगीत]"]:
                        transcript_entries.append({"start": st, "duration": du, "text": txt})
                if transcript_entries:
                    transcript_source = "youtube_transcript_api"
        except Exception as yta_err:
            logger.info(f"youtube-transcript-api notice for {vid}: {yta_err}")

    # Tier 2: YouTube watch page captionTracks JSON3 timedtext
    if not transcript_entries and vid and len(vid) == 11:
        try:
            watch_url = f"https://www.youtube.com/watch?v={vid}"
            req = urllib.request.Request(
                watch_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
                    "Accept-Language": "hi-IN,hi;q=0.9,en-US;q=0.8,en;q=0.7"
                }
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            m_cap = re.search(r'"captionTracks"\s*:\s*(\[.*?\])', html)
            if m_cap:
                tracks = json.loads(m_cap.group(1))
                chosen_url = ""
                for pref_lang in ["hi", "en"]:
                    for tr in tracks:
                        if str(tr.get("languageCode", "")).startswith(pref_lang) and tr.get("baseUrl"):
                            chosen_url = tr["baseUrl"]
                            break
                    if chosen_url:
                        break
                if not chosen_url and tracks and tracks[0].get("baseUrl"):
                    chosen_url = tracks[0]["baseUrl"]

                if chosen_url:
                    json3_url = chosen_url + ("&fmt=json3" if "?" in chosen_url else "?fmt=json3")
                    c_req = urllib.request.Request(json3_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(c_req, timeout=8) as c_resp:
                        c_data = json.loads(c_resp.read().decode("utf-8", errors="ignore"))
                    for ev in c_data.get("events", []):
                        segs = ev.get("segs") or []
                        line_txt = "".join(str(s.get("utf8", "")) for s in segs).replace("\n", " ").strip()
                        if line_txt and line_txt not in ["[Music]", "[Applause]"]:
                            st_sec = float(ev.get("tStartMs", 0)) / 1000.0
                            du_sec = float(ev.get("dDurationMs", 3000)) / 1000.0
                            transcript_entries.append({"start": st_sec, "duration": du_sec, "text": line_txt})
                    if transcript_entries:
                        transcript_source = "youtube_caption_tracks_json3"
        except Exception as cap_err:
            logger.info(f"Watch page captionTracks notice: {cap_err}")

    # Build full movie digest & window-specific digest for the requested Part
    bucket_summaries: List[str] = []
    window_lines: List[str] = []
    w_start = max(0.0, float(start_offset_sec or 0.0))
    w_end = float(window_end_sec) if window_end_sec and window_end_sec > w_start else float(duration)

    if transcript_entries:
        for e in transcript_entries:
            if w_start <= e["start"] <= w_end:
                window_lines.append(f"[{format_seconds_to_timestamp(e['start'])}] {e['text']}")

        max_ts = max(float(duration), max((e["start"] + e["duration"]) for e in transcript_entries))
        num_buckets = 16
        bucket_span = max(30.0, max_ts / num_buckets)
        for b_idx in range(num_buckets):
            b_start = b_idx * bucket_span
            b_end = (b_idx + 1) * bucket_span
            lines_in_bucket = [e["text"] for e in transcript_entries if b_start <= e["start"] < b_end]
            if lines_in_bucket:
                joined_dialogue = " ".join(lines_in_bucket)[:900]
                bucket_summaries.append(
                    f"[{format_seconds_to_timestamp(b_start)} - {format_seconds_to_timestamp(b_end)}] {joined_dialogue}"
                )

    return {
        "video_id": vid,
        "title": title,
        "duration": duration,
        "description": description,
        "chapters": chapters,
        "transcript_source": transcript_source,
        "transcript_entries_count": len(transcript_entries),
        "transcript_digest": "\n".join(bucket_summaries),
        "window_transcript": "\n".join(window_lines[:180]),
        "has_real_transcript": len(transcript_entries) > 0
    }


# =====================================================================
# 4. 1-MINUTE EPISODIC SHORTS GENERATOR (PART 1, PART 2, PART 3...)
# =====================================================================
def parse_storyboard_json_payload(resp_text: str) -> Dict[str, Any]:
    """Robustly parses JSON object from Gemini response."""
    txt = (resp_text or "").strip()
    if not txt:
        return {}
    m_json = re.search(r"\{[\s\S]*\}", txt)
    if m_json:
        txt = m_json.group(0)
    txt_clean = re.sub(r",\s*([\]}])", r"\1", txt)
    try:
        data = json.loads(txt_clean)
        if isinstance(data, dict):
            if "keeper_clips" in data and "scenes" not in data:
                data["scenes"] = data["keeper_clips"]
            elif "scenes" in data and "keeper_clips" not in data:
                data["keeper_clips"] = data["scenes"]
            return data
    except Exception:
        pass
    return {}


def _normalize_10_to_12_cuts_for_60s(
    raw_clips: List[Dict[str, Any]],
    window_start: float,
    window_end: float,
    script_text: str,
    language: str = "Hindi"
) -> List[Dict[str, Any]]:
    """
    Ensures strictly 10 to 12 fast, dynamic scene cuts (each 4.0s to 6.0s, totaling ~60.0s)
    arranged in ascending chronological order within [window_start, window_end].
    """
    beat_labels = [
        "[Hook]",
        "[Setup]",
        "[Mystery]",
        "[Escalation]",
        "[Discovery]",
        "[Tension]",
        "[Action]",
        "[Shock]",
        "[Confrontation]",
        "[Twist]",
        "[Climax Beat]",
        "[Cliffhanger]"
    ]

    w_start = max(0.0, float(window_start))
    w_end = max(w_start + 65.0, float(window_end))
    w_span = w_end - w_start

    valid_clips: List[Dict[str, Any]] = []
    for rc in (raw_clips or []):
        if not isinstance(rc, dict):
            continue
        s_val = rc.get("start", rc.get("start_seconds", rc.get("start_time")))
        e_val = rc.get("end", rc.get("end_seconds", rc.get("end_time")))
        s = parse_timestamp_to_seconds(s_val) if s_val is not None else 0.0
        e = parse_timestamp_to_seconds(e_val) if e_val is not None else 0.0
        dur = float(rc.get("duration") or (e - s) or 5.0)
        dur = max(4.0, min(6.0, dur))
        valid_clips.append({
            "start": s,
            "duration": round(dur, 2),
            "beat": str(rc.get("beat") or "").strip(),
            "title": sanitize_actor_names_to_character_roles(str(rc.get("title") or rc.get("description") or "").strip(), language),
            "narration": sanitize_actor_names_to_character_roles(str(rc.get("narration") or rc.get("script_segment") or "").strip(), language)
        })

    # Target between 10 and 12 cuts (default 12 cuts * 5.0s = 60.0s)
    if len(valid_clips) < 10:
        target_count = 12
    elif len(valid_clips) > 12:
        valid_clips = valid_clips[:12]
        target_count = 12
    else:
        target_count = len(valid_clips)

    # Split full script evenly across cuts if individual cut narration is missing
    words = (script_text or "").split()
    words_per_cut = max(1, int(math.ceil(len(words) / float(target_count)))) if words else 12

    normalized: List[Dict[str, Any]] = []
    slot_step = w_span / float(target_count)
    cursor = w_start

    # Scale durations so the sum of the 10-12 cuts is ~60.0s while every cut stays in [4.0s, 6.0s]
    base_durs = [
        valid_clips[i]["duration"] if i < len(valid_clips) else 5.0
        for i in range(target_count)
    ]
    raw_sum = sum(base_durs) or 60.0
    scaled_durs = [round(max(4.0, min(6.0, d * (60.0 / raw_sum))), 2) for d in base_durs]
    # Fine-tune final cut so total is close to 60.0s
    diff = round(60.0 - sum(scaled_durs), 2)
    if abs(diff) <= 1.5:
        scaled_durs[-1] = round(max(4.0, min(6.0, scaled_durs[-1] + diff)), 2)

    for idx in range(target_count):
        vc = valid_clips[idx] if idx < len(valid_clips) else {}
        cut_dur = scaled_durs[idx]

        slot_default_s = w_start + idx * slot_step + min(1.5, slot_step * 0.1)
        proposed_s = float(vc.get("start", slot_default_s))
        if proposed_s < w_start or proposed_s > w_end - cut_dur:
            proposed_s = slot_default_s

        remaining_cuts_dur = sum(scaled_durs[idx + 1:]) + (target_count - 1 - idx) * 0.4
        max_s = max(cursor, w_end - cut_dur - remaining_cuts_dur)
        cut_s = round(max(cursor, min(max_s, proposed_s)), 2)
        cut_e = round(cut_s + cut_dur, 2)
        cursor = round(cut_e + 0.4, 2)

        beat_tag = vc.get("beat") or beat_labels[idx % len(beat_labels)]
        cut_title = vc.get("title") or f"Cut {idx + 1}: {beat_tag.strip('[]')}"
        cut_narr = vc.get("narration") or ""
        if not cut_narr and words:
            w_slice = words[idx * words_per_cut:(idx + 1) * words_per_cut]
            cut_narr = " ".join(w_slice)

        normalized.append({
            "id": idx + 1,
            "clip_num": idx + 1,
            "beat": beat_tag,
            "start": cut_s,
            "end": cut_e,
            "start_ts": format_seconds_to_timestamp(cut_s),
            "end_ts": format_seconds_to_timestamp(cut_e),
            "duration": round(cut_e - cut_s, 2),
            "title": cut_title,
            "reason": f"Part beat {idx + 1} ({beat_tag})",
            "narration": cut_narr
        })

    return normalized


def _enforce_140_to_150_word_script(script: str, part_number: int = 1, title: str = "", language: str = "Hindi") -> str:
    """
    Ensures the Hindi suspense script is clean, uses character-only names, and falls within
    the 140-150 word target budget for a 60-second YouTube Short (~2.4 words/sec).
    """
    clean = sanitize_actor_names_to_character_roles(script or "", language)
    clean = re.sub(r"\s+", " ", clean).strip()
    words = clean.split()

    if len(words) > 152:
        trimmed = " ".join(words[:146])
        # Try ending cleanly at the last sentence boundary if possible
        last_punct = max(trimmed.rfind("।"), trimmed.rfind("?"), trimmed.rfind("!"), trimmed.rfind("."))
        if last_punct > int(len(trimmed) * 0.88):
            trimmed = trimmed[:last_punct + 1]
        else:
            trimmed = trimmed.rstrip(" ,;-") + "... आगे क्या होगा? जानने के लिए अगला पार्ट ज़रूर देखें!"
        words = trimmed.split()
        if len(words) > 152:
            trimmed = " ".join(words[:148]) + "!"
        return trimmed

    if 0 < len(words) < 138:
        cliffhanger_tail = (
            f" लेकिन असली रहस्य तो अब खुलने वाला था, क्योंकि नायक के सामने एक ऐसा खौफनाक सच आने वाला है "
            f"जो इस पूरी कहानी को हमेशा के लिए बदल कर रख देगा। आखिर उस बंद दरवाज़े के पीछे कौन सा राज़ छुपा है, "
            f"और क्या नायक इस जानलेवा जाल से बाहर निकल पाएगा? इसके आगे की कहानी जानने के लिए पार्ट {part_number + 1} अभी देखें!"
        )
        tail_words = cliffhanger_tail.split()
        needed = max(0, 144 - len(words))
        clean = (clean + " " + " ".join(tail_words[:needed])).strip()
        if not clean.endswith(("।", "!", "?")):
            clean += "!"

    return clean


def generate_episodic_60s_short_part(
    youtube_url: str,
    part_number: int = 1,
    start_offset_sec: float = 0.0,
    previous_summary: str = "",
    video_duration: Optional[float] = None,
    local_video_title: str = "",
    language: str = "Hindi",
    custom_instructions: str = "",
    credentials=None,
    channel_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Core 1-Minute Episodic Shorts Generator:
    - Extracts ground-truth plot, characters, and subtitles from `youtube_url` (never downloads YouTube video).
    - Generates an authentic 60-second Hindi suspense script (140-150 words) using character-only names.
    - Identifies 10 to 12 fast, dynamic visual scene cuts (each 4-6s, totaling ~60s) aligned with Part `part_number`.
    - Returns `next_start_sec` and `part_summary` so clicking "Generate Next Part (Part N+1)" continues chronologically.
    """
    youtube_url_clean = (youtube_url or "").strip()
    part_num = max(1, int(part_number or 1))

    yt_info = extract_youtube_info(youtube_url_clean, credentials=credentials) if youtube_url_clean else {
        "url": "",
        "video_id": "",
        "title": local_video_title or "Movie Storyline",
        "duration": int(video_duration or 7200),
        "duration_str": format_seconds_to_timestamp(video_duration or 7200),
        "description": custom_instructions or "",
        "chapters": [],
        "thumbnail": "",
        "channel": "Cinema"
    }

    title = yt_info.get("title") or local_video_title or "Movie Storyline"
    yt_duration = float(yt_info.get("duration") or 7200.0)
    # If local file duration is provided by browser, bound all cut timestamps to the local video duration
    effective_duration = float(video_duration) if (video_duration and float(video_duration) > 65.0) else max(120.0, yt_duration)

    # Determine chronological window [window_start, window_end] for this Part
    # Each 60s Short summarizes a chronological act/window of the movie (~180s to 600s of movie runtime per Part)
    window_span = max(90.0, min(600.0, effective_duration * 0.12))
    if start_offset_sec and float(start_offset_sec) > 0:
        window_start = float(start_offset_sec)
    else:
        window_start = (part_num - 1) * window_span

    # Ensure at least 70s of footage remains in the window so 10-12 cuts (60s total) fit cleanly
    if window_start > effective_duration - 70.0:
        window_start = max(0.0, effective_duration - max(90.0, window_span))
    window_end = min(effective_duration, max(window_start + 75.0, window_start + window_span))

    story_data = extract_real_movie_transcript_and_story(
        youtube_url=youtube_url_clean,
        video_id=yt_info.get("video_id"),
        yt_info=yt_info,
        start_offset_sec=window_start,
        window_end_sec=window_end
    )
    transcript_source = story_data.get("transcript_source", "metadata_synopsis")
    window_transcript = story_data.get("window_transcript", "")
    full_digest = story_data.get("transcript_digest", "")
    description = yt_info.get("description", "")
    chapters = yt_info.get("chapters") or []

    chapters_text = ""
    if chapters:
        ch_lines = [
            f"- [{format_seconds_to_timestamp(ch.get('start_time', 0))} - {format_seconds_to_timestamp(ch.get('end_time', 0))}] {ch.get('title', '')}"
            for ch in chapters[:25]
        ]
        chapters_text = "Official Video Chapters:\n" + "\n".join(ch_lines)

    continuity_block = ""
    if part_num > 1:
        continuity_block = f"""
=== EPISODIC CONTINUITY (THIS IS PART {part_num}) ===
- Previous Part ({part_num - 1}) ended at timestamp {format_seconds_to_timestamp(window_start)} ({window_start:.1f}s).
- Summary of Previous Part: {previous_summary or f'Part {part_num - 1} introduced the opening suspense conflict.'}
- CRITICAL: Continue the story chronologically from {format_seconds_to_timestamp(window_start)} to {format_seconds_to_timestamp(window_end)} without repeating Part {part_num - 1}!
"""

    prompt = f"""You are a master Hindi Movie Shorts Storyteller & Trailer Editor (optimizing 60-second episodic YouTube Shorts for CapCut).
Analyze the official movie/video storyline and generate **PART {part_num} (60-Second Episodic Short)**:

=== MOVIE / VIDEO GROUND-TRUTH METADATA ===
- Official Title: {title}
- YouTube URL: {youtube_url_clean}
- Total Movie Runtime: {format_seconds_to_timestamp(effective_duration)} ({int(effective_duration)} seconds)
- Current Episodic Window for PART {part_num}: [{format_seconds_to_timestamp(window_start)} ({window_start:.1f}s) to {format_seconds_to_timestamp(window_end)} ({window_end:.1f}s)]
- Official Synopsis / Description:
{description[:3500]}
{chapters_text}
{continuity_block}

=== EXTRACTED SUBTITLES / DIALOGUE FOR THIS WINDOW ({transcript_source}) ===
{window_transcript[:8000] if window_transcript else full_digest[:8000]}

{STRICT_CHARACTER_ONLY_NAMING_RULE}

=== STRICT 60-SECOND EPISODIC SHORTS SPECIFICATIONS ===
1. **AUTHENTIC 60-SECOND HINDI SUSPENSE SCRIPT (140 TO 150 WORDS)**:
   - Write a gripping, high-retention 60-second Hindi suspense story script in Devanagari (`"script"`) for **Part {part_num}**.
   - Word count MUST be **strictly between 140 and 150 words** (calibrated for 60 seconds of voiceover at 2.4 words/sec).
   - Use ONLY in-movie fictional character names (e.g., बहत्तर सिंह, इंदु, कबीर, विक्रम) or Hindi archetype roles ("नायक", "अन्वेषक", "वह रहस्यमयी इंसान") — NEVER mention real-life actors or celebrities!
   - Start with an immediate 3-second suspense hook and end with a cliffhanger hook leading into Part {part_num + 1}.

2. **10 TO 12 FAST, DYNAMIC VISUAL SCENE CUTS (EACH 4 TO 6 SECONDS, TOTALING ~60 SECONDS)**:
   - Select **10 to 12** chronological visual cuts inside the timestamp window `[{window_start:.1f}, {window_end:.1f}]`.
   - Every single cut MUST have a duration between **4.0 and 6.0 seconds** (`4.0 <= duration <= 6.0`).
   - The sum of all 10-12 cuts MUST equal **~60.0 seconds**.
   - Align each cut chronologically with the narrative beats of Part {part_num}.

Return STRICT JSON ONLY with this exact schema:
{{
  "part_number": {part_num},
  "part_title": "{title[:30]} - खौफनाक सच! 😱 Part {part_num} #Shorts",
  "part_summary": "1-2 sentence summary of what happened in Part {part_num} so Part {part_num + 1} can continue seamlessly",
  "script": "Full 140 to 150 word Hindi suspense storytelling script in Devanagari for Part {part_num}...",
  "keeper_clips": [
    {{
      "id": 1,
      "beat": "[Hook]",
      "start": {round(window_start + 2.0, 1)},
      "end": {round(window_start + 7.0, 1)},
      "duration": 5.0,
      "title": "Cut 1 description",
      "narration": "12-14 word Hindi line matching Cut 1..."
    }}
  ]
}}
"""

    parsed_payload: Dict[str, Any] = {}
    source_used = f"grounded_story ({transcript_source})"

    try:
        import channel_key_store
        from google.genai import types

        candidate_models = [
            "gemini-3-flash-preview",
            "gemini-3.8-flash",
            "gemini-3.1-flash-lite-preview",
            "gemini-2.5-flash"
        ]

        def _try_parse(resp_text: str, mode_label: str, model_name: str, masked_k: str) -> bool:
            nonlocal parsed_payload, source_used
            data = parse_storyboard_json_payload(resp_text)
            if not isinstance(data, dict):
                return False
            clips = data.get("keeper_clips") or data.get("scenes") or data.get("sub_clips") or []
            script_cand = str(data.get("script") or data.get("full_script") or "").strip()
            if not script_cand and isinstance(clips, list):
                script_cand = " ".join(str(c.get("narration") or "") for c in clips if isinstance(c, dict)).strip()
            if script_cand and len(script_cand.split()) >= 40:
                parsed_payload = {
                    "part_title": str(data.get("part_title") or data.get("title") or f"{title[:30]} - Part {part_num} #Shorts").strip(),
                    "part_summary": str(data.get("part_summary") or data.get("summary") or "").strip(),
                    "script": script_cand,
                    "keeper_clips": clips if isinstance(clips, list) else []
                }
                source_used = f"gemini ({model_name} | {mode_label}) [{masked_k}]"
                return True
            return False

        def _do_gemini_part(client, api_key):
            masked_k = channel_key_store.mask_key(api_key)
            is_yt = bool(youtube_url_clean and ("youtube.com" in youtube_url_clean or "youtu.be" in youtube_url_clean))

            for model_name in candidate_models:
                # Pass 1: Direct YouTube URL Multimodal analysis
                if is_yt and model_name in ["gemini-3-flash-preview", "gemini-3.8-flash"]:
                    try:
                        mm_contents = types.Content(
                            parts=[
                                types.Part(file_data=types.FileData(file_uri=youtube_url_clean)),
                                types.Part(text=prompt)
                            ]
                        )
                        resp = client.models.generate_content(
                            model=model_name,
                            contents=mm_contents,
                            config=types.GenerateContentConfig(temperature=0.3, response_mime_type="application/json")
                        )
                        if resp and resp.text and _try_parse(resp.text, "youtube_multimodal", model_name, masked_k):
                            return True
                    except Exception as mm_err:
                        if any(k in str(mm_err).lower() for k in ["429", "resource_exhausted", "quota", "rate limit"]):
                            raise mm_err
                        logger.info(f"Multimodal pass notice on {model_name}: {mm_err}")

                # Pass 2: Google Search Grounded story & character extraction
                try:
                    resp = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            tools=[types.Tool(google_search=types.GoogleSearch())],
                            temperature=0.3
                        )
                    )
                    if resp and resp.text and _try_parse(resp.text, "google_search_grounded", model_name, masked_k):
                        return True
                except Exception as gs_err:
                    if any(k in str(gs_err).lower() for k in ["429", "resource_exhausted", "quota", "rate limit"]):
                        raise gs_err
                    logger.info(f"Google Search pass notice on {model_name}: {gs_err}")

                # Pass 3: Direct JSON generation from extracted transcript & metadata
                try:
                    resp = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(temperature=0.3, response_mime_type="application/json")
                    )
                    if resp and resp.text and _try_parse(resp.text, "transcript_json", model_name, masked_k):
                        return True
                except Exception as j_err:
                    if any(k in str(j_err).lower() for k in ["429", "resource_exhausted", "quota", "rate limit"]):
                        raise j_err
                    logger.warning(f"JSON pass notice on {model_name}: {j_err}")
            return False

        channel_key_store.execute_with_channel_key_rotation(channel_id, f"Episodic Short Part {part_num}", _do_gemini_part)
    except Exception as e:
        logger.warning(f"Gemini episodic generation notice: {e}")

    # Fallback synthesis from real extracted subtitles/synopsis if Gemini API was unreachable
    raw_script = parsed_payload.get("script", "")
    raw_clips = parsed_payload.get("keeper_clips", [])

    if not raw_script:
        clean_title = sanitize_actor_names_to_character_roles(title, language)
        dialogue_lines = [
            re.sub(r"^\[.*?\]\s*", "", ln).strip()
            for ln in (window_transcript or full_digest or description).splitlines()
            if len(re.sub(r"^\[.*?\]\s*", "", ln).strip()) > 15
        ]
        joined_real = " ".join(dialogue_lines[:10])
        joined_real = sanitize_actor_names_to_character_roles(joined_real, language)
        raw_script = (
            f"क्या आपने कभी सोचा है कि जब एक इंसान के सामने अचानक ऐसा खौफनाक राज़ खुल जाए तो वह क्या करेगा? "
            f"{clean_title} के पार्ट {part_num} में कहानी ठीक वहीं से तेज़ होती है जहाँ नायक एक अनजान खतरे के बीच फंस चुका है। "
            f"{joined_real[:380]} "
            f"हर गुज़रते पल के साथ नायक के चारों तरफ साज़िश का घेरा और गहरा होता जा रहा है, और उसे समझ आ जाता है कि "
            f"सामने दिखने वाला सच केवल एक धोखा है। लेकिन तभी एक ऐसा चौंकाने वाला मोड़ आता है जिसकी किसी ने कल्पना भी नहीं की थी! "
            f"आखिर आगे नायक इस जाल को कैसे तोड़ेगा? जानने के लिए पार्ट {part_num + 1} ज़रूर देखें!"
        )

    final_script = _enforce_140_to_150_word_script(raw_script, part_number=part_num, title=title, language=language)
    keeper_clips = _normalize_10_to_12_cuts_for_60s(
        raw_clips=raw_clips,
        window_start=window_start,
        window_end=window_end,
        script_text=final_script,
        language=language
    )

    total_cuts_duration = round(sum(c["duration"] for c in keeper_clips), 2)
    word_count = len(final_script.split())
    next_start_sec = round(min(effective_duration, keeper_clips[-1]["end"] if keeper_clips else window_end), 2)
    part_summary = sanitize_actor_names_to_character_roles(
        parsed_payload.get("part_summary") or f"Part {part_num} covered {format_seconds_to_timestamp(window_start)} to {format_seconds_to_timestamp(next_start_sec)}: {final_script[:180]}...",
        language
    )
    part_title = sanitize_actor_names_to_character_roles(
        parsed_payload.get("part_title") or f"{title[:32]} - Part {part_num} 😱 #Shorts",
        language
    )

    return {
        "success": True,
        "part_number": part_num,
        "next_part_number": part_num + 1,
        "part_title": part_title,
        "title": title,
        "youtube_url": youtube_url_clean,
        "thumbnail": yt_info.get("thumbnail", ""),
        "channel": yt_info.get("channel", ""),
        "source": source_used,
        "transcript_source": transcript_source,
        "has_real_transcript": story_data.get("has_real_transcript", False),
        "movie_duration": round(effective_duration, 2),
        "movie_duration_str": format_seconds_to_timestamp(effective_duration),
        "window_start_sec": round(window_start, 2),
        "window_end_sec": round(window_end, 2),
        "window_range_str": f"{format_seconds_to_timestamp(window_start)} - {format_seconds_to_timestamp(window_end)}",
        "next_start_sec": next_start_sec,
        "next_start_ts": format_seconds_to_timestamp(next_start_sec),
        "part_summary": part_summary,
        "script": final_script,
        "full_script": final_script,
        "word_count": word_count,
        "total_words": word_count,
        "target_words_range": "140-150",
        "keeper_clips": keeper_clips,
        "total_clips": len(keeper_clips),
        "total_duration_sec": total_cuts_duration,
        "total_kept_duration": total_cuts_duration,
        "muted_for_capcut": True
    }


def generate_cinema_explainer_storyboard(
    youtube_url: Optional[str] = None,
    credentials=None,
    target_duration: Any = 60,
    language: str = "Hindi",
    custom_instructions: str = "",
    channel_id: Optional[str] = None,
    local_video_path: Optional[str] = None,
    video_duration: Optional[float] = None,
    part_number: int = 1,
    start_offset_sec: float = 0.0,
    previous_summary: str = "",
    **kwargs
) -> Dict[str, Any]:
    """Compatibility wrapper routing directly to the lightweight 60-second episodic shorts generator."""
    local_dur = video_duration
    local_title = ""
    if local_video_path and os.path.exists(str(local_video_path)):
        meta = get_video_metadata(str(local_video_path))
        if not local_dur:
            local_dur = meta.get("duration")
        local_title = os.path.splitext(os.path.basename(str(local_video_path)))[0]

    return generate_episodic_60s_short_part(
        youtube_url=youtube_url or "",
        part_number=int(part_number or 1),
        start_offset_sec=float(start_offset_sec or 0.0),
        previous_summary=str(previous_summary or ""),
        video_duration=local_dur,
        local_video_title=local_title,
        language=language,
        custom_instructions=custom_instructions,
        credentials=credentials,
        channel_id=channel_id
    )


# =====================================================================
# 5. LOCAL VIDEO METADATA & FAST MUTED 60S SLICING FOR CAPCUT
# =====================================================================
def get_video_metadata(video_path: str) -> Dict[str, Any]:
    """Extracts resolution, aspect ratio, and duration from a local video file using ffprobe."""
    meta = {
        "path": video_path,
        "filename": os.path.basename(video_path) if video_path else "",
        "duration": 0.0,
        "duration_str": "00:00",
        "width": 1920,
        "height": 1080,
        "aspect_ratio": "16:9",
        "fps": 30.0,
        "size_bytes": 0,
        "size_mb": 0.0
    }
    if not video_path or not os.path.exists(video_path):
        return meta

    meta["size_bytes"] = os.path.getsize(video_path)
    meta["size_mb"] = round(meta["size_bytes"] / (1024 * 1024), 2)

    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    try:
        cmd = [
            ffprobe_bin, "-v", "error",
            "-show_entries", "stream=codec_type,width,height,r_frame_rate,duration",
            "-show_entries", "format=duration",
            "-of", "json",
            video_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            fmt_dur = data.get("format", {}).get("duration")
            if fmt_dur:
                meta["duration"] = round(float(fmt_dur), 2)
            for s in data.get("streams", []):
                if s.get("codec_type") == "video" and s.get("width"):
                    meta["width"] = int(s.get("width"))
                    meta["height"] = int(s.get("height"))
                    if not meta["duration"] and s.get("duration"):
                        meta["duration"] = round(float(s["duration"]), 2)
    except Exception as e:
        logger.warning(f"ffprobe metadata notice: {e}")

    if meta["duration"] <= 0:
        meta["duration"] = 60.0
    meta["duration_str"] = format_seconds_to_timestamp(meta["duration"])
    return meta


def export_timeline_trimmed_video(
    source_video_path: str,
    keeper_clips: List[Dict[str, Any]],
    output_path: str,
    progress_callback: Optional[Any] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Slices the 10-12 keeper cuts (each 4-6s, totaling ~60s) from `source_video_path`
    with 100% muted audio (`-an`) and stitches them into a single CapCut-ready `.mp4`.
    """
    def notify(pct: int, msg: str):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    if not os.path.exists(source_video_path):
        raise FileNotFoundError(f"Source video not found: {source_video_path}")

    sanitized = []
    for c in (keeper_clips or []):
        try:
            s = float(c.get("start", 0))
            e = float(c.get("end", s + float(c.get("duration", 5.0))))
            if e > s + 0.3:
                sanitized.append({
                    "start": s,
                    "end": e,
                    "duration": round(e - s, 3)
                })
        except Exception:
            pass

    if not sanitized:
        raise ValueError("No valid keeper clips provided for 1-minute export.")

    sanitized.sort(key=lambda x: x["start"])
    task_id = uuid.uuid4().hex[:8]
    task_temp = os.path.join(TEMP_DIR, f"short60s_{task_id}")
    os.makedirs(task_temp, exist_ok=True)
    ffmpeg_bin = get_ffmpeg_bin()
    sliced_paths = []

    try:
        total_clips = len(sanitized)
        notify(15, f"Slicing {total_clips} dynamic cuts (muted for CapCut)...")

        for i, clip in enumerate(sanitized):
            s = float(clip["start"])
            d_cut = float(clip["duration"])
            clip_out = os.path.join(task_temp, f"cut_{i:02d}.mp4")

            cmd_copy = [
                ffmpeg_bin, "-y",
                "-ss", f"{s:.3f}",
                "-t", f"{d_cut:.3f}",
                "-i", source_video_path,
                "-c:v", "copy",
                "-an",
                "-avoid_negative_ts", "make_zero",
                clip_out
            ]
            res_copy = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res_copy.returncode == 0 and os.path.exists(clip_out) and os.path.getsize(clip_out) > 1000:
                sliced_paths.append(clip_out)
            else:
                cmd_fast = [
                    ffmpeg_bin, "-y",
                    "-ss", f"{s:.3f}",
                    "-t", f"{d_cut:.3f}",
                    "-i", source_video_path,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                    "-an",
                    "-avoid_negative_ts", "make_zero",
                    clip_out
                ]
                subprocess.run(cmd_fast, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if os.path.exists(clip_out) and os.path.getsize(clip_out) > 1000:
                    sliced_paths.append(clip_out)

            pct = 15 + int(65 * ((i + 1) / float(total_clips)))
            notify(pct, f"Sliced cut {i + 1}/{total_clips} ({d_cut:.1f}s)")

        if not sliced_paths:
            raise RuntimeError("Failed to slice cuts from source video.")

        notify(85, "Stitching 1-minute CapCut video...")
        concat_txt = os.path.join(task_temp, "concat.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for p in sliced_paths:
                clean_p = os.path.abspath(p).replace("\\", "/")
                f.write(f"file '{clean_p}'\n")

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        concat_cmd = [
            ffmpeg_bin, "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_txt,
            "-c:v", "copy",
            "-an",
            "-movflags", "+faststart",
            output_path
        ]
        res_concat = subprocess.run(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res_concat.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            filter_parts = "".join(f"[{j}:v]" for j in range(len(sliced_paths)))
            cmd_filter = [ffmpeg_bin, "-y"]
            for p in sliced_paths:
                cmd_filter.extend(["-i", p])
            cmd_filter.extend([
                "-filter_complex", f"{filter_parts}concat=n={len(sliced_paths)}:v=1:a=0[v]",
                "-map", "[v]",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                "-an",
                "-movflags", "+faststart",
                output_path
            ])
            subprocess.run(cmd_filter, check=True)

        notify(100, "1-Minute Sliced Video (.mp4) ready for CapCut!")
        meta = get_video_metadata(output_path)
        return {
            "success": True,
            "output_path": output_path,
            "filename": os.path.basename(output_path),
            "duration": meta.get("duration", 60.0),
            "duration_str": meta.get("duration_str", "01:00"),
            "width": meta.get("width", 1920),
            "height": meta.get("height", 1080),
            "aspect_ratio": meta.get("aspect_ratio", "16:9"),
            "size_mb": meta.get("size_mb", 0.0),
            "muted": True
        }
    finally:
        try:
            shutil.rmtree(task_temp, ignore_errors=True)
        except Exception:
            pass
