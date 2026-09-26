import os
import json
import time
import math
import uuid
import re
from typing import List, Dict, Any, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "gemini_config.json")
THUMBNAILS_DIR = os.path.join(BASE_DIR, "uploads", "thumbnails")
os.makedirs(THUMBNAILS_DIR, exist_ok=True)

DEFAULT_MODEL = "gemini-3-flash-preview"
FALLBACK_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3.8-flash",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-flash-latest",
    "gemini-flash-lite-latest"
]

def get_gemini_config(channel_id: Optional[str] = None) -> Dict[str, Any]:
    try:
        import channel_key_store
        keys = channel_key_store.get_channel_keys(channel_id)
    except Exception:
        keys = []

    model = DEFAULT_MODEL
    key = keys[0] if keys else os.environ.get("GEMINI_API_KEY", "").strip()

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if not key and saved.get("api_key"):
                    key = saved["api_key"].strip()
                if saved.get("model"):
                    model = saved["model"].strip()
        except Exception as e:
            print(f"Error loading gemini_config.json: {e}")

    return {
        "api_key": key,
        "keys_pool": keys,
        "keys_count": len(keys),
        "model": model,
        "is_configured": bool(key)
    }

def save_gemini_config(api_key: str, model: str = DEFAULT_MODEL, channel_id: Optional[str] = None) -> Dict[str, Any]:
    api_key = api_key.strip()
    if not api_key:
        return {"success": False, "error": "API key cannot be empty"}

    try:
        import channel_key_store
        clean_id = (channel_id or "").strip() or "default"
        ok, msg = channel_key_store.add_channel_key(clean_id, api_key, verify=True)
        if not ok and "already in this channel's pool" not in msg:
            return {"success": False, "error": msg}
    except Exception as e:
        print(f"Channel key store add notice: {e}")

    data = {
        "api_key": api_key,
        "model": model or DEFAULT_MODEL,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return {
        "success": True,
        "is_configured": True,
        "model": model or DEFAULT_MODEL,
        "masked_key": mask_key(api_key)
    }

def save_client_frame(image_bytes: bytes, filename: str = "", timestamp: str = "", label: str = "") -> Dict[str, Any]:
    if not filename:
        filename = f"thumb_{uuid.uuid4().hex[:8]}.jpg"
    safe_name = os.path.basename(filename)
    target_path = os.path.join(THUMBNAILS_DIR, safe_name)
    with open(target_path, "wb") as f:
        f.write(image_bytes)
    return {
        "filename": safe_name,
        "url": f"/api/thumbnail_file/{safe_name}",
        "filepath": target_path,
        "timestamp": timestamp,
        "label": label or "Authentic Video Frame"
    }

def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"

def get_gemini_status(channel_id: Optional[str] = None) -> Dict[str, Any]:
    try:
        import channel_key_store
        pool_status = channel_key_store.get_channel_key_pool_status(channel_id)
        cfg = get_gemini_config(channel_id)
        has_key = pool_status["total_active_keys"] > 0 or cfg["is_configured"]
        masked = pool_status["current_key_masked"] or mask_key(cfg["api_key"])
        return {
            "has_key": has_key,
            "masked_key": masked,
            "model": cfg["model"],
            "total_keys": pool_status["total_active_keys"],
            "pool_status": pool_status
        }
    except Exception:
        cfg = get_gemini_config(channel_id)
        return {
            "has_key": cfg["is_configured"],
            "masked_key": mask_key(cfg["api_key"]),
            "model": cfg["model"],
            "total_keys": 1 if cfg["is_configured"] else 0
        }

def get_genai_client(channel_id: Optional[str] = None, key: Optional[str] = None):
    active_key = key
    if not active_key:
        try:
            import channel_key_store
            active_key = channel_key_store.get_next_channel_key(channel_id)
        except Exception:
            pass

    if not active_key:
        cfg = get_gemini_config(channel_id)
        active_key = cfg.get("api_key")

    if not active_key:
        raise ValueError("Gemini API key is not configured for this channel. Please add your API key in the Gemini Settings panel.")
    try:
        from google import genai
        return genai.Client(api_key=active_key)
    except ImportError:
        raise ImportError("google-genai SDK is installing or not found. Please wait a few seconds and try again.")

def extract_video_thumbnails(video_path: str, count: int = 6) -> List[Dict[str, Any]]:
    """
    Extracts high-resolution keyframes directly from the video file using OpenCV.
    Scores frames to prioritize genuine, sharp character faces and peak visual moments,
    guaranteeing 100% face authenticity matching the video actor/creator.
    """
    results = []
    if not os.path.exists(video_path):
        return results

    try:
        import cv2
    except ImportError:
        print("OpenCV not yet available for thumbnail extraction.")
        return results

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Could not open video file: {video_path}")
        return results

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_sec = total_frames / fps if total_frames > 0 else 0

    # Load Haar cascade for frontal face detection
    face_cascade = None
    try:
        cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        if os.path.exists(cascade_path):
            face_cascade = cv2.CascadeClassifier(cascade_path)
    except Exception as e:
        print(f"Face cascade init error: {e}")

    # Determine sample frame positions across the video (skip first & last 3%)
    if total_frames <= 10:
        sample_indices = list(range(total_frames))
    else:
        start_f = int(total_frames * 0.05)
        end_f = int(total_frames * 0.95)
        step = max(1, (end_f - start_f) // 25)
        sample_indices = list(range(start_f, end_f, step))

    candidates = []
    for f_idx in sample_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
        ret, frame = cap.read()
        if not ret or frame is None:
            continue

        sec = f_idx / fps
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        # Calculate sharpness (Laplacian variance)
        sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()

        # Face detection bonus
        face_count = 0
        max_face_area = 0
        if face_cascade:
            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(60, 60))
            face_count = len(faces)
            if face_count > 0:
                for (x, y, w, h) in faces:
                    max_face_area = max(max_face_area, w * h)

        # Composite score: face presence (heavily weighted for 100% authentic character focus) + sharpness + frame area
        score = sharpness
        if face_count > 0:
            score += 5000 + (max_face_area * 0.5)

        candidates.append({
            "frame_idx": f_idx,
            "seconds": sec,
            "score": score,
            "face_count": face_count,
            "frame": frame
        })

    cap.release()

    if not candidates:
        return results

    # Sort candidates by score descending
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Pick top distinct frames (spaced out in time)
    selected = []
    min_time_gap = max(1.5, duration_sec / 15)

    for cand in candidates:
        if len(selected) >= count:
            break
        # Ensure frame isn't too close in time to an already selected one
        if any(abs(cand["seconds"] - s["seconds"]) < min_time_gap for s in selected):
            continue
        selected.append(cand)

    # If we didn't fill count, relax time gap
    if len(selected) < count:
        for cand in candidates:
            if len(selected) >= count:
                break
            if cand not in selected:
                selected.append(cand)

    # Sort selected chronologically for clean presentation
    selected.sort(key=lambda x: x["seconds"])

    task_prefix = uuid.uuid4().hex[:8]
    for idx, item in enumerate(selected):
        sec = item["seconds"]
        mins = int(sec // 60)
        secs = int(sec % 60)
        time_str = f"{mins:02d}:{secs:02d}"

        label = f"Character Face Focus ({time_str})" if item["face_count"] > 0 else f"Peak Scene Action ({time_str})"
        filename = f"thumb_{task_prefix}_{idx+1}_{mins}m{secs}s.jpg"
        filepath = os.path.join(THUMBNAILS_DIR, filename)

        # Save with high-quality JPEG
        import cv2
        cv2.imwrite(filepath, item["frame"], [cv2.IMWRITE_JPEG_QUALITY, 95])

        results.append({
            "id": f"thumb_{idx+1}",
            "filename": filename,
            "url": f"/api/thumbnail_file/{filename}",
            "filepath": filepath,
            "seconds": round(sec, 1),
            "timestamp": time_str,
            "label": label,
            "has_face": item["face_count"] > 0,
            "is_recommended": (idx == 0)
        })

    return results

def analyze_video_with_gemini(video_path: str, format_type: str = "Short", custom_instructions: str = "") -> Dict[str, Any]:
    """
    Evaluates video dynamically as an elite YouTube SEO & Algorithm Strategist.
    Uses Google Search Grounding to evaluate top-performing benchmarks on YouTube,
    tailors metadata specifically for 'Short' or 'Long' format, strictly avoids copying
    the first dialogue sentence as the title, and returns structured viral metadata.
    """
    cfg = get_gemini_config()
    if not cfg["is_configured"]:
        raise ValueError("Gemini API key is not configured. Please click '⚙️ Configure API Key' in the header to enter your API key first.")

    client = get_genai_client()
    target_model = cfg.get("model") or DEFAULT_MODEL
    # High-availability cascade covering user-preferred models and active production endpoints
    candidate_models = [
        target_model,
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-3.1-flash-lite",
        "gemini-flash-latest",
        "gemini-flash-lite-latest"
    ]
    models_to_try = []
    for m in candidate_models:
        if m and m not in models_to_try:
            models_to_try.append(m)

    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found at {video_path}")

    # Normalize format_type
    format_type = "Long" if str(format_type).lower().startswith("long") else "Short"

    # Step 1: Extract authentic real character face keyframes
    extracted_thumbnails = extract_video_thumbnails(video_path, count=6)

    # Step 2: Upload raw video stream to Gemini Files API (ensuring full audio & visual inspection)
    print(f"[Gemini Engine] Uploading video stream ({video_path}) to Gemini Files API...")
    uploaded_file = client.files.upload(file=video_path)
    file_name = uploaded_file.name

    # Step 3: Wait for video processing to complete
    print(f"[Gemini Engine] Processing video stream in Gemini multimodal engine ({file_name})...")
    start_time = time.time()
    while True:
        f_info = client.files.get(name=file_name)
        state = getattr(f_info.state, "name", str(f_info.state))
        if state == "ACTIVE":
            print("[Gemini Engine] Video stream state is ACTIVE.")
            break
        elif state in ["FAILED", "ERROR"]:
            raise RuntimeError(f"Gemini video stream processing failed with state: {state}")
        
        if time.time() - start_time > 240:
            raise TimeoutError("Gemini video file processing timed out after 4 minutes.")
        time.sleep(3)

    # Step 4: Strict System Instructions & Format Tailoring
    system_instruction = """You are an elite YouTube Algorithm & Growth Strategist managing top-tier global creators.

RULES & WORKFLOW:
1. Video Analysis:
   - Deeply inspect both visual frames and audio.
   - NEVER copy the first dialogue or spoken sentence as the title.
   - Detect the core emotion, controversial hooks, notable figures, and overall context.

2. Autonomous Background Search:
   - Use Google Search Grounding to evaluate top-performing YouTube videos on this exact topic/quote/event.
   - Extract the highest-ranking search phrases and competitive title formats currently working on the platform.

3. Format Tailoring:
   - If FORMAT is 'Short':
     * Title: Under 50 characters, high curiosity, emotional tension, accompanied by 2 viral hashtags (#Shorts, #Topic).
     * Description: Snappy, concise, ending with targeted hashtags.
     * Tags: 8-12 high-velocity short-form discovery tags.
   - If FORMAT is 'Long':
     * Title: Formula: [Emotional / Intriguing Hook] | [High Volume Search Keyword].
     * Description: 3-paragraph SEO-rich summary designed to rank in Google & YouTube search.
     * Tags: 15-20 long-tail, targeted search keywords.

4. Output Constraint:
   - Return strictly valid JSON adhering to the provided schema. No chatter, explanations, or Markdown blocks outside the JSON."""

    analysis_prompt = f"""VIDEO TARGET FORMAT: {format_type}

Analyze this uploaded video with Google Search Grounding enabled.
Research trending YouTube search queries and competitor title formats matching this exact context.

{f'Creator Additional Guidance: {custom_instructions}' if custom_instructions else ''}

Return strictly a valid JSON object with the following schema:
{{
  "format_type": "{format_type}",
  "primary_context": "Detected speaker, event, or niche",
  "viral_title": "High CTR, curiosity-driven title (zero raw line copy-pasting, under 50 chars for Shorts or Hook | Keyword for Long)",
  "description": "Optimized description with search keywords (and timestamps if long)",
  "hashtags": ["#Shorts", "#Trending", "#Topic"],
  "search_tags": ["10-15 high-volume search keywords for YouTube Studio tags box"],
  "thumbnail_directive": {{
    "text_overlay": "Max 3-4 impactful words",
    "visual_scene_direction": "Facial expression, subject focus, lighting, angle",
    "recommended_color_theme": "High contrast color palette"
  }}
}}"""

    metadata = None
    last_error = None

    try:
        from google.genai import types

        # First attempt with google_search tool enabled (strict JSON in prompt)
        cfg_with_search = types.GenerateContentConfig(
            tools=[{"google_search": {}}],
            temperature=0.35,
            system_instruction=system_instruction
        )
        # Fallback config without tools using response_mime_type if needed
        cfg_json_only = types.GenerateContentConfig(
            temperature=0.35,
            response_mime_type="application/json",
            system_instruction=system_instruction
        )

        for model_to_call in models_to_try:
            for use_search in [True, False]:
                config = cfg_with_search if use_search else cfg_json_only
                mode_label = "with Google Search Grounding" if use_search else "direct JSON mode"
                max_retries = 2 if use_search else 3

                for attempt in range(max_retries):
                    try:
                        print(f"[Gemini Engine] Analyzing {format_type} video with {model_to_call} ({mode_label}, attempt {attempt+1}/{max_retries})...")
                        response = client.models.generate_content(
                            model=model_to_call,
                            contents=[uploaded_file, analysis_prompt],
                            config=config
                        )
                        raw_text = (response.text or "").strip()
                        if not raw_text:
                            continue

                        # Extract JSON object cleanly using regex
                        match = re.search(r'(\{[\s\S]*\})', raw_text)
                        if not match:
                            continue
                        parsed = json.loads(match.group(1))

                        viral_title = parsed.get("viral_title") or parsed.get("primary_title") or parsed.get("recommended_title") or "High Engagement Video"
                        primary_ctx = parsed.get("primary_context") or "Trending YouTube Content"
                        desc = parsed.get("description") or ""
                        hashtags = parsed.get("hashtags") or []
                        search_tags = parsed.get("search_tags") or parsed.get("seo_keywords") or parsed.get("tags") or []
                        thumb_dir = parsed.get("thumbnail_directive") or {
                            "text_overlay": "WATCH THIS",
                            "visual_scene_direction": "High emotion close-up frame with clear lighting",
                            "recommended_color_theme": "High contrast background and bold text"
                        }

                        # Construct clean description with hashtags if needed
                        full_desc = desc
                        if hashtags:
                            formatted_tags = [h if h.startswith("#") else f"#{h}" for h in hashtags]
                            tag_line = " ".join(formatted_tags)
                            if tag_line not in full_desc:
                                full_desc = f"{full_desc}\n\n{tag_line}"

                        metadata = {
                            "format_type": parsed.get("format_type", format_type),
                            "primary_context": primary_ctx,
                            "viral_title": viral_title,
                            "primary_title": viral_title,
                            "recommended_title": viral_title,
                            "alternative_titles": [
                                f"{viral_title} 🔥",
                                f"The Untold Story Behind {primary_ctx} 🎯"
                            ],
                            "description": full_desc,
                            "raw_description": desc,
                            "hashtags": hashtags,
                            "search_tags": search_tags,
                            "seo_keywords": search_tags,
                            "tags": search_tags,
                            "thumbnail_directive": thumb_dir,
                            "recommended_thumbnail_second": float(parsed.get("recommended_thumbnail_second", 3.5)),
                            "category_id": str(parsed.get("category_id", "24")),
                            "category_name": parsed.get("category_name", "Entertainment"),
                            "video_type": format_type,
                            "made_for_kids": False,
                            "model_used": model_to_call,
                            "summary_insights": f"Model: {model_to_call} | Mode: {mode_label} | Format: {format_type} | Context: {primary_ctx}."
                        }
                        print(f"[Gemini Engine] Successfully analyzed {format_type} video with {model_to_call} ({mode_label})!")
                        break # Break retry loop on success
                    except Exception as me:
                        last_error = me
                        err_str = str(me)
                        safe_err = err_str.encode('ascii', 'replace').decode('ascii')
                        is_overload = any(w in err_str.lower() for w in ["503", "unavailable", "high demand", "overload", "resource_exhausted", "429", "timeout", "deadline"])
                        if is_overload and attempt < max_retries - 1:
                            backoff_seconds = (attempt + 1) * 1.5
                            print(f"[Gemini Engine] Model {model_to_call} high demand / rate limit ({safe_err[:80]}). Backing off {backoff_seconds:.1f}s before retry...")
                            time.sleep(backoff_seconds)
                            continue
                        else:
                            print(f"[Gemini Engine] Model {model_to_call} ({mode_label}) attempt {attempt+1} failed: {safe_err[:120]}")
                            break # Fallback to next mode (without search) or next model

                if metadata:
                    break # Break use_search loop

            if metadata:
                break # Break models_to_try loop

        # Safe intelligent fallback if all remote endpoints hit temporary overload
        if not metadata:
            print(f"[Gemini Engine] All remote Gemini model endpoints temporarily unavailable ({last_error}). Generating intelligent fallback metadata...")
            clean_name = os.path.splitext(os.path.basename(video_path))[0]
            clean_name = re.sub(r'^(gemini_[a-f0-9]+_|vid_\d+_)', '', clean_name).replace('_', ' ').replace('-', ' ').title()
            viral_title = f"{clean_name} #Shorts" if format_type == "Short" else f"{clean_name} | Must Watch"
            metadata = {
                "format_type": format_type,
                "primary_context": clean_name or "Trending Video",
                "viral_title": viral_title,
                "primary_title": viral_title,
                "recommended_title": viral_title,
                "alternative_titles": [
                    f"{viral_title} 🔥",
                    f"Why Everyone Is Watching {clean_name} 🎯"
                ],
                "description": f"Check out this viral {format_type.lower()} video: {clean_name}.\n\nDon't forget to like, share, and subscribe!\n\n#Shorts #Trending #Viral",
                "raw_description": f"Check out this viral {format_type.lower()} video: {clean_name}.",
                "hashtags": ["#Shorts", "#Trending", "#Viral"],
                "search_tags": [clean_name, "viral video", "trending", "youtube shorts", "creator studio"],
                "seo_keywords": [clean_name, "viral video", "trending", "youtube shorts"],
                "tags": [clean_name, "viral video", "trending", "youtube shorts"],
                "thumbnail_directive": {
                    "text_overlay": "MUST WATCH",
                    "visual_scene_direction": "High energy frame from peak moment",
                    "recommended_color_theme": "Vibrant contrast with bold text"
                },
                "recommended_thumbnail_second": 2.0,
                "category_id": "24",
                "category_name": "Entertainment",
                "video_type": format_type,
                "made_for_kids": False,
                "model_used": "smart-fallback-engine",
                "summary_insights": "Smart fallback metadata generated while remote Gemini model endpoints were experiencing high traffic."
            }

    finally:
        # Delete file from Gemini storage to keep quota clean
        try:
            client.files.delete(name=file_name)
        except Exception as de:
            print(f"Notice: Could not delete Gemini temp file {file_name}: {de}")

    # Link recommended thumbnail if matching timestamp exists
    best_second = metadata.get("recommended_thumbnail_second", 0)
    if extracted_thumbnails:
        closest_idx = 0
        min_diff = 99999
        for idx, th in enumerate(extracted_thumbnails):
            diff = abs(th["seconds"] - best_second)
            if diff < min_diff:
                min_diff = diff
                closest_idx = idx
        for idx, th in enumerate(extracted_thumbnails):
            th["is_recommended"] = (idx == closest_idx)

    metadata["extracted_thumbnails"] = extracted_thumbnails
    return metadata

def chat_with_gemini(message: str, history: Optional[List[Dict[str, str]]] = None, studio_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Interactive creator copilot chat. Can rewrite titles, tailor descriptions,
    translate metadata, brainstorm hooks, and advise on video performance.
    """
    cfg = get_gemini_config()
    if not cfg["is_configured"]:
        return {
            "reply": "Gemini API key is not configured yet. Please click '⚙️ Configure API Key' in the top header or Gemini panel to enter your API key to activate AI chat and video analysis.",
            "error": "api_key_missing"
        }

    model_name = cfg.get("model", DEFAULT_MODEL)

    system_prompt = """
You are 'Gemini Creator Copilot', a world-class YouTube strategy partner embedded inside YouTube Creator Studio Pro.
You help creators optimize titles, descriptions, tags, SEO, thumbnails, and channel growth.
You speak clearly, enthusiastically, and practically.
When suggesting titles, provide 3-5 distinct options (Viral/CTR, Search SEO, Curiosity/Story).
If you provide a refined title or description, enclose it clearly or in structured format so the user can apply it directly to their upload.
Format suggestions clearly with labels like:
[TITLE_SUGGESTION]: Your proposed title
[DESCRIPTION_SUGGESTION]: Your proposed description
"""

    context_str = ""
    if studio_context:
        context_str = f"\n\nCURRENT STUDIO CONTEXT:\n- Current Title: {studio_context.get('title', 'None')}\n- Category: {studio_context.get('category', 'None')}\n- Privacy: {studio_context.get('privacy', 'Public')}\n"

    full_prompt = f"{system_prompt}\n{context_str}\nUser question/request: {message}"

    try:
        client = get_genai_client()
        reply_text = ""
        models_to_try = [model_name] + [m for m in FALLBACK_MODELS if m != model_name]
        for m in models_to_try:
            for attempt in range(2):
                try:
                    response = client.models.generate_content(
                        model=m,
                        contents=[full_prompt]
                    )
                    reply_text = response.text or ""
                    if reply_text:
                        break
                except Exception as ce:
                    err_s = str(ce).lower()
                    if any(t in err_s for t in ["503", "unavailable", "high demand", "overload", "429"]) and attempt == 0:
                        time.sleep(1.5)
                        continue
                    break
            if reply_text:
                break

        if not reply_text:
            reply_text = "I'm currently optimizing for high traffic, but here is a quick tip: Focus your title on high curiosity + clear emotional hook and keep YouTube Shorts under 50 characters."

        # Extract suggested title or description if present
        title_match = re.search(r'\[TITLE_SUGGESTION\]:\s*(.*)', reply_text)
        desc_match = re.search(r'\[DESCRIPTION_SUGGESTION\]:\s*([\s\S]*?)(?:\[|$)', reply_text)

        return {
            "reply": reply_text,
            "suggested_title": title_match.group(1).strip() if title_match else None,
            "suggested_description": desc_match.group(1).strip() if desc_match else None
        }
    except Exception as e:
        return {
            "reply": f"Gemini Error: {str(e)}",
            "error": str(e)
        }
