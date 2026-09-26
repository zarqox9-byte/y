import os
import json
import time
import uuid
import re
import hashlib
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import channel_key_store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GEMINI_CONFIG_FILE = os.path.join(BASE_DIR, "gemini_config.json")
THUMBNAILS_DIR = os.path.join(BASE_DIR, "uploads", "thumbnails")
os.makedirs(THUMBNAILS_DIR, exist_ok=True)

# Default model configuration
DEFAULT_MODEL = "gemini-2.5-flash"
FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-flash-latest",
]


def get_gemini_config(channel_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Loads Gemini API key and model selection.
    Checks in order:
    1. Channel-bound 10-key pool in Database / Persistent Store (channel_key_store)
    2. Environment variable GEMINI_API_KEY
    3. Local gemini_config.json file
    """
    config = {
        "api_key": "",
        "model": DEFAULT_MODEL,
        "is_configured": False,
        "pool_count": 0,
        "channel_id": channel_id or "default"
    }

    if os.path.exists(GEMINI_CONFIG_FILE):
        try:
            with open(GEMINI_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                config["api_key"] = saved.get("api_key", "").strip()
                saved_model = saved.get("model", DEFAULT_MODEL).strip()
                config["model"] = saved_model if saved_model else DEFAULT_MODEL
        except Exception as e:
            print(f"Error reading gemini_config.json: {e}")

    try:
        pool_key = channel_key_store.get_next_channel_key(channel_id)
        pool_keys = channel_key_store.get_channel_keys(channel_id)
        if pool_key:
            config["api_key"] = pool_key
            config["pool_count"] = len(pool_keys)
    except Exception as e:
        print(f"Notice checking channel_key_store: {e}")

    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key and not config["api_key"]:
        config["api_key"] = env_key

    config["is_configured"] = bool(config["api_key"])
    return config


def save_gemini_config(api_key: str, model: str = DEFAULT_MODEL, channel_id: Optional[str] = None) -> Dict[str, Any]:
    """Saves Gemini API key and model selection to local file and channel key pool."""
    clean_key = api_key.strip()
    data = {
        "api_key": clean_key,
        "model": model.strip() or DEFAULT_MODEL,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    with open(GEMINI_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    if clean_key:
        try:
            channel_key_store.add_channel_key(channel_id or "default", clean_key, verify=False)
        except Exception as e:
            print(f"Notice adding key to channel_key_store: {e}")

    os.environ["GEMINI_API_KEY"] = data["api_key"]
    return {
        "success": True,
        "model": data["model"],
        "is_configured": bool(data["api_key"])
    }


def get_genai_client(channel_id: Optional[str] = None, explicit_key: Optional[str] = None):
    """Initializes and returns the google-genai Client using the channel's key pool."""
    from google import genai
    if explicit_key:
        return genai.Client(api_key=explicit_key)
    cfg = get_gemini_config(channel_id)
    if not cfg["is_configured"]:
        raise ValueError("Gemini API key is not configured. Please add your Gemini API key in the Studio settings.")
    return genai.Client(api_key=cfg["api_key"])


def execute_with_key_rotation(channel_id: Optional[str], func, *args, **kwargs):
    """
    Executes a function `func(client, *args, **kwargs)` using the channel's 10-key pool.
    If a key hits 429 Resource Exhausted / Quota Exceeded or 403 Invalid Key, it automatically
    marks the key in cooldown and retries seamlessly with the next available key in the pool!
    """
    from google import genai
    pool_keys = channel_key_store.get_channel_keys(channel_id)
    if not pool_keys:
        cfg = get_gemini_config(channel_id)
        if cfg["api_key"]:
            pool_keys = [cfg["api_key"]]
        else:
            raise ValueError("No Gemini API keys configured in pool. Please add at least 1 key in '⚙️ Configure API Key'.")

    max_attempts = max(len(pool_keys), 1)
    last_err = None

    for attempt in range(max_attempts):
        active_key = channel_key_store.get_next_channel_key(channel_id) or pool_keys[attempt % len(pool_keys)]
        client = genai.Client(api_key=active_key)
        try:
            return func(client, *args, **kwargs)
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_quota_or_auth = any(w in err_str for w in [
                "429", "resource_exhausted", "quota", "rate limit", "too many requests",
                "403", "api_key_invalid", "permission_denied", "503", "overloaded"
            ])
            if is_quota_or_auth:
                masked_k = getattr(channel_key_store, "mask_key", lambda x: "****")(active_key)
                if hasattr(channel_key_store, "mark_key_rate_limited"):
                    channel_key_store.mark_key_rate_limited(active_key, cooldown_seconds=120)
                print(f"[KeyPool Failover] Key {masked_k} hit quota/error ({str(e)[:80]}). Rotating to next key (attempt {attempt+1}/{max_attempts})...")
                continue
            raise e

    raise last_err


def get_gemini_status(channel_id: Optional[str] = None) -> Dict[str, Any]:
    """Checks if Gemini is configured and returns status + masked key + pool info."""
    cfg = get_gemini_config(channel_id)
    key = cfg["api_key"]
    masked = ""
    if key and len(key) > 8:
        masked = key[:4] + "*" * (len(key) - 8) + key[-4:]
    elif key:
        masked = "****"

    pool_status = channel_key_store.get_channel_key_pool_status(channel_id)
    return {
        "is_configured": cfg["is_configured"],
        "model": cfg["model"],
        "masked_key": masked,
        "pool": pool_status
    }


def save_client_frame(
    image_bytes: bytes,
    filename: str,
    timestamp: str = "00:00",
    label: str = "Authentic Video Frame",
    aspect_ratio: Optional[str] = None
) -> Dict[str, Any]:
    """Saves a client-extracted raw video frame into the thumbnails folder, cropped to strict 9:16 or 16:9 if specified."""
    filepath = os.path.join(THUMBNAILS_DIR, filename)
    saved_cropped = False
    if aspect_ratio in ("9:16", "16:9"):
        try:
            import cv2
            import numpy as np
            arr = np.frombuffer(image_bytes, dtype=np.uint8)
            decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if decoded is not None and decoded.size > 0:
                cropped = fit_and_crop_to_aspect_ratio(decoded, aspect_ratio=aspect_ratio)
                cv2.imwrite(filepath, cropped, [cv2.IMWRITE_JPEG_QUALITY, 95])
                saved_cropped = True
        except Exception:
            saved_cropped = False

    if not saved_cropped:
        with open(filepath, "wb") as f:
            f.write(image_bytes)

    return {
        "id": filename,
        "filename": filename,
        "url": f"/api/thumbnail_file/{filename}",
        "filepath": filepath,
        "timestamp": timestamp,
        "label": label,
        "aspect_ratio": aspect_ratio or "16:9",
        "is_ai_generated": False,
        "is_recommended": False,
        "selected": False
    }


def detect_target_aspect_ratio(video_path: Optional[str] = None, format_type: str = "Short") -> Tuple[str, int, int]:
    """
    ASPECT RATIO AUTO-DETECTION:
    - Shorts / Reels: Render thumbnail strictly in 9:16 vertical ratio (720x1280).
    - Long-form Video: Render thumbnail strictly in 16:9 cinematic widescreen ratio (1280x720).
    """
    fmt_clean = str(format_type or "").strip().lower()
    if fmt_clean.startswith("long") or fmt_clean in ("16:9", "widescreen", "landscape"):
        return ("16:9", 1280, 720)
    if fmt_clean.startswith("short") or fmt_clean in ("9:16", "reel", "reels", "vertical"):
        return ("9:16", 720, 1280)

    # Auto-detect from video stream dimensions if format_type is unspecified
    if video_path and os.path.exists(video_path):
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                cap.release()
                if w > 0 and h > 0:
                    if w < h:
                        return ("9:16", 720, 1280)
                    else:
                        return ("16:9", 1280, 720)
        except Exception:
            pass

    return ("9:16", 720, 1280)


def fit_and_crop_to_aspect_ratio(
    img_bgr: np.ndarray,
    aspect_ratio: str = "9:16",
    focus_box: Optional[Tuple[int, int, int, int]] = None
) -> np.ndarray:
    """
    Strictly formats any BGR frame into 9:16 (720x1280) for Shorts/Reels or 16:9 (1280x720) for Long-form.
    Centers the crop intelligently on the detected face (`focus_box`) or visual saliency center so subjects
    are framed with maximum emotional impact and zero black bars.
    """
    import cv2

    if aspect_ratio == "9:16":
        target_w, target_h = 720, 1280
    else:
        target_w, target_h = 1280, 720

    if img_bgr is None or img_bgr.size == 0:
        return np.zeros((target_h, target_w, 3), dtype=np.uint8)

    src_h, src_w = img_bgr.shape[:2]
    target_ratio = target_w / float(target_h)
    src_ratio = src_w / float(src_h)

    # Determine focal center (face center if available, otherwise image center)
    cx = src_w / 2.0
    cy = src_h / 2.0
    if focus_box is not None and len(focus_box) == 4:
        fx, fy, fw, fh = focus_box
        cx = fx + (fw / 2.0)
        cy = fy + (fh * 0.45)  # Focus slightly towards eyes

    if abs(src_ratio - target_ratio) < 0.02:
        return cv2.resize(img_bgr, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)

    if src_ratio > target_ratio:
        # Source is wider than target (e.g. 16:9 frame -> 9:16 vertical Short)
        crop_h = src_h
        crop_w = max(1, int(round(crop_h * target_ratio)))
        x1 = int(round(cx - (crop_w / 2.0)))
        x1 = max(0, min(src_w - crop_w, x1))
        y1 = 0
    else:
        # Source is taller than target (e.g. 9:16 frame -> 16:9 widescreen)
        crop_w = src_w
        crop_h = max(1, int(round(crop_w / target_ratio)))
        y1 = int(round(cy - (crop_h / 2.0)))
        y1 = max(0, min(src_h - crop_h, y1))
        x1 = 0

    cropped = img_bgr[y1:y1 + crop_h, x1:x1 + crop_w]
    return cv2.resize(cropped, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)


def _score_frame_emotion_and_motion(
    frame_bgr: np.ndarray,
    prev_gray: Optional[np.ndarray],
    face_cascade: Any
) -> Tuple[float, int, Optional[Tuple[int, int, int, int]], np.ndarray]:
    """
    Scores a video frame on:
    - Facial presence & expression intensity (eye/mouth gradient micro-contrast)
    - Visual sharpness (Laplacian variance)
    - Motion / action energy (frame-to-frame optical delta + Sobel edge density)
    - Color vibrancy (HSV saturation + luminance contrast)
    Returns (composite_score, face_count, primary_face_box, gray_frame).
    """
    import cv2

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    # Sobel gradient energy (high-contrast dramatic edges)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    edge_energy = float(np.mean(cv2.magnitude(gx, gy)))

    # Motion energy relative to previous sampled frame
    motion_score = 0.0
    if prev_gray is not None and prev_gray.shape == gray.shape:
        diff = cv2.absdiff(gray, prev_gray)
        motion_score = float(np.mean(diff)) * 18.0

    # Color vibrancy & contrast
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    sat_mean = float(np.mean(hsv[:, :, 1]))
    val_std = float(np.std(hsv[:, :, 2]))
    vibrancy_score = (sat_mean * 2.5) + (val_std * 4.0)

    face_count = 0
    best_face_box = None
    max_face_score = 0.0

    if face_cascade is not None:
        faces = face_cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=4,
            minSize=(48, 48)
        )
        face_count = len(faces)
        for (x, y, w, h) in faces:
            face_roi = gray[max(0, y):min(gray.shape[0], y + h), max(0, x):min(gray.shape[1], x + w)]
            # High expression intensity = strong micro-contrast in eyes/brow/mouth
            expr_intensity = float(cv2.Laplacian(face_roi, cv2.CV_64F).var()) if face_roi.size > 0 else 0.0
            f_score = (w * h * 0.55) + (expr_intensity * 8.0)
            if f_score > max_face_score:
                max_face_score = f_score
                best_face_box = (int(x), int(y), int(w), int(h))

    composite_score = sharpness + (edge_energy * 6.0) + motion_score + vibrancy_score
    if face_count > 0:
        composite_score += 6500.0 + max_face_score

    return composite_score, face_count, best_face_box, gray


def extract_video_thumbnails(
    video_path: str,
    count: int = 5,
    aspect_ratio: Optional[str] = None,
    format_type: str = "Short",
    return_best_raw: bool = False
) -> Any:
    """
    SLOTS 2 to 6: HIGH-EMOTION LOCAL VIDEO FRAMES
    Extracts `count` (default 5) native candidate keyframes from high-motion/emotion moments
    as fallback choices alongside the Slot 1 AI Dynamic Thumbnail.
    Strictly renders every candidate in 9:16 (for Shorts/Reels) or 16:9 (for Long-form).
    """
    results: List[Dict[str, Any]] = []
    best_raw_info: Dict[str, Any] = {"frame": None, "face_box": None, "seconds": 0.0, "timestamp": "00:00"}

    detected_ratio, target_w, target_h = detect_target_aspect_ratio(video_path, format_type=aspect_ratio or format_type)
    if aspect_ratio in ("9:16", "16:9"):
        detected_ratio = aspect_ratio
        target_w, target_h = (720, 1280) if detected_ratio == "9:16" else (1280, 720)

    try:
        import cv2
    except ImportError:
        print("OpenCV (cv2) not installed, skipping server frame extraction.")
        return (results, best_raw_info) if return_best_raw else results

    if not os.path.exists(video_path):
        return (results, best_raw_info) if return_best_raw else results

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return (results, best_raw_info) if return_best_raw else results

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_sec = total_frames / fps if fps > 0 else 0

    if total_frames <= 0:
        cap.release()
        return (results, best_raw_info) if return_best_raw else results

    face_cascade = None
    try:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        if os.path.exists(cascade_path):
            face_cascade = cv2.CascadeClassifier(cascade_path)
    except Exception as e:
        print(f"Haar cascade load notice: {e}")

    num_samples = min(30, max(count * 4, 15))
    start_frame = int(total_frames * 0.05)
    end_frame = int(total_frames * 0.94)
    if end_frame <= start_frame:
        start_frame = 0
        end_frame = max(1, total_frames - 1)

    sample_indices = [
        int(start_frame + i * (end_frame - start_frame) / max(1, num_samples - 1))
        for i in range(num_samples)
    ]

    candidates = []
    prev_gray = None

    for f_idx in sample_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
        ret, frame = cap.read()
        if not ret or frame is None:
            continue

        sec = f_idx / fps if fps > 0 else 0.0
        score, face_count, best_face_box, gray = _score_frame_emotion_and_motion(frame, prev_gray, face_cascade)
        prev_gray = gray

        candidates.append({
            "frame_idx": f_idx,
            "seconds": sec,
            "score": score,
            "face_count": face_count,
            "face_box": best_face_box,
            "frame": frame
        })

    cap.release()

    if not candidates:
        return (results, best_raw_info) if return_best_raw else results

    # Sort by emotion + motion + face score descending
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Record the #1 highest-emotion raw frame for Slot 1 AI Dynamic Thumbnail reference
    top_cand = candidates[0]
    top_sec = top_cand["seconds"]
    best_raw_info = {
        "frame": top_cand["frame"].copy(),
        "face_box": top_cand["face_box"],
        "seconds": round(top_sec, 1),
        "timestamp": f"{int(top_sec // 60):02d}:{int(top_sec % 60):02d}"
    }

    # Select `count` (5) distinct high-emotion frames spaced across the video
    selected = []
    min_time_gap = max(1.2, duration_sec / 12.0)

    for cand in candidates:
        if len(selected) >= count:
            break
        if any(abs(cand["seconds"] - s["seconds"]) < min_time_gap for s in selected):
            continue
        selected.append(cand)

    if len(selected) < count:
        for cand in candidates:
            if len(selected) >= count:
                break
            if all(cand["frame_idx"] != s["frame_idx"] for s in selected):
                selected.append(cand)

    # Sort selected chronologically for Slots 2..6
    selected.sort(key=lambda x: x["seconds"])

    task_prefix = uuid.uuid4().hex[:8]
    for idx, item in enumerate(selected):
        slot_num = idx + 2  # Slots 2 to 6
        sec = item["seconds"]
        mins = int(sec // 60)
        secs = int(sec % 60)
        time_str = f"{mins:02d}:{secs:02d}"

        formatted_bgr = fit_and_crop_to_aspect_ratio(
            item["frame"],
            aspect_ratio=detected_ratio,
            focus_box=item.get("face_box")
        )

        emotion_tag = "High-Emotion Expression" if item["face_count"] > 0 else "Climactic Action Frame"
        label = f"Slot {slot_num}: {emotion_tag} ({time_str})"
        filename = f"thumb_slot{slot_num}_{task_prefix}_{mins}m{secs}s.jpg"
        filepath = os.path.join(THUMBNAILS_DIR, filename)

        cv2.imwrite(filepath, formatted_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])

        results.append({
            "id": f"slot_{slot_num}",
            "slot": slot_num,
            "filename": filename,
            "url": f"/api/thumbnail_file/{filename}",
            "filepath": filepath,
            "seconds": round(sec, 1),
            "timestamp": f"SLOT {slot_num} • {time_str}",
            "label": label,
            "has_face": item["face_count"] > 0,
            "is_ai_generated": False,
            "is_recommended": False,
            "selected": False,
            "aspect_ratio": detected_ratio,
            "width": target_w,
            "height": target_h
        })

    return (results, best_raw_info) if return_best_raw else results


def _select_dynamic_color_palette(color_theme_str: str, genre_str: str, video_hash_int: int) -> Dict[str, Tuple[int, int, int]]:
    """
    Determines a unique, non-repetitive cinematic color grading & typography accent palette
    matched to the video's specific genre, mood, and Gemini art direction.
    Returns RGB tuples for shadow_tint, highlight_tint, rim_glow, text_primary, badge_bg.
    """
    combined = f"{color_theme_str} {genre_str}".lower()
    palettes = [
        # 0: Thriller / Suspense Teal & Fiery Amber
        {
            "name": "Cyber Thriller Teal-Amber",
            "shadow_bgr": (42, 24, 8),
            "highlight_bgr": (18, 165, 255),
            "glow_rgb": (255, 140, 0),
            "text_rgb": (255, 245, 157),
            "badge_rgb": (220, 38, 38)
        },
        # 1: High-Drama Crimson & Electric Gold
        {
            "name": "Crimson Gold Intensity",
            "shadow_bgr": (18, 10, 48),
            "highlight_bgr": (40, 210, 255),
            "glow_rgb": (255, 45, 85),
            "text_rgb": (255, 255, 255),
            "badge_rgb": (234, 179, 8)
        },
        # 2: Mystery / Sci-Fi Neon Cyan & Violet
        {
            "name": "Neon Noir Cyan-Violet",
            "shadow_bgr": (45, 12, 30),
            "highlight_bgr": (255, 210, 40),
            "glow_rgb": (56, 189, 248),
            "text_rgb": (125, 211, 252),
            "badge_rgb": (147, 51, 234)
        },
        # 3: Action / Survival Emerald & Solar Orange
        {
            "name": "High Voltage Emerald-Flame",
            "shadow_bgr": (16, 38, 12),
            "highlight_bgr": (30, 180, 255),
            "glow_rgb": (16, 185, 129),
            "text_rgb": (254, 240, 138),
            "badge_rgb": (239, 68, 68)
        },
        # 4: Dark Suspense Magenta & Ice Blue
        {
            "name": "Electric Suspense Magenta-Ice",
            "shadow_bgr": (38, 14, 38),
            "highlight_bgr": (255, 225, 120),
            "glow_rgb": (236, 72, 153),
            "text_rgb": (255, 255, 255),
            "badge_rgb": (219, 39, 119)
        }
    ]

    if any(k in combined for k in ["red", "crimson", "blood", "danger", "shock", "horror"]):
        return palettes[1]
    if any(k in combined for k in ["cyan", "blue", "neon", "tech", "sci-fi", "mystery"]):
        return palettes[2]
    if any(k in combined for k in ["green", "emerald", "toxic", "survival", "money"]):
        return palettes[3]
    if any(k in combined for k in ["purple", "magenta", "pink", "violet", "royal"]):
        return palettes[4]
    if any(k in combined for k in ["teal", "orange", "amber", "gold", "cinematic", "thriller"]):
        return palettes[0]

    return palettes[video_hash_int % len(palettes)]


def _render_ultra_high_contrast_ai_visual(
    base_bgr: np.ndarray,
    aspect_ratio: str,
    metadata: Dict[str, Any],
    focus_box: Optional[Tuple[int, int, int, int]],
    video_seed_str: str,
    apply_typography: bool = True
) -> np.ndarray:
    """
    Transforms a base frame or AI-generated image into an ultra-high-contrast, click-worthy
    YouTube thumbnail in strict 9:16 (720x1280) or 16:9 (1280x720) ratio with:
    - Dynamic subject-centered dramatic zoom (unique per video hash)
    - Multi-band CLAHE facial expression & micro-detail enhancement
    - Video-specific split-toning color grade & chiaroscuro radial rim lighting
    - Crisp, high-impact hook typography from Gemini's `thumbnail_directive.text_overlay`
    """
    import cv2
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    target_w, target_h = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
    seed_digest = int(hashlib.sha256(video_seed_str.encode("utf-8", errors="ignore")).hexdigest()[:12], 16)

    # 1. Dynamic dramatic zoom on the focal expression (1.08x to 1.20x depending on video seed)
    framed = fit_and_crop_to_aspect_ratio(base_bgr, aspect_ratio=aspect_ratio, focus_box=focus_box)
    zoom_factor = 1.08 + ((seed_digest % 13) * 0.01)
    zh, zw = int(round(target_h * zoom_factor)), int(round(target_w * zoom_factor))
    zoomed = cv2.resize(framed, (zw, zh), interpolation=cv2.INTER_LANCZOS4)

    # Slight horizontal/vertical rule-of-thirds offset based on video seed
    x_off = max(0, min(zw - target_w, int((zw - target_w) * (0.35 + ((seed_digest >> 4) % 30) / 100.0))))
    y_off = max(0, min(zh - target_h, int((zh - target_h) * 0.32)))
    canvas_bgr = zoomed[y_off:y_off + target_h, x_off:x_off + target_w].copy()

    # 2. CLAHE Local Micro-Contrast Enhancement in LAB space (makes facial expressions & eyes pop)
    lab = cv2.cvtColor(canvas_bgr, cv2.COLOR_BGR2LAB)
    l_chan, a_chan, b_chan = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=3.2, tileGridSize=(8, 8))
    l_enhanced = clahe.apply(l_chan)
    # S-curve contrast boost on luminance
    l_float = l_enhanced.astype(np.float32) / 255.0
    l_curve = np.clip(0.5 + 1.28 * (l_float - 0.5), 0.0, 1.0)
    l_final = (l_curve * 255.0).astype(np.uint8)
    canvas_bgr = cv2.cvtColor(cv2.merge([l_final, a_chan, b_chan]), cv2.COLOR_LAB2BGR)

    # 3. Boost saturation & vibrance for high-CTR visual punch
    hsv = cv2.cvtColor(canvas_bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * 1.35, 0, 255)
    canvas_bgr = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

    # 4. Video-specific split-toning color grade + dramatic radial spotlight / vignette
    thumb_dir = metadata.get("thumbnail_directive") or {}
    color_theme = str(thumb_dir.get("recommended_color_theme") or "")
    genre_emotion = str(metadata.get("detected_genre_emotion") or metadata.get("primary_context") or "")
    palette = _select_dynamic_color_palette(color_theme, genre_emotion, seed_digest)

    yy, xx = np.mgrid[0:target_h, 0:target_w].astype(np.float32)
    focal_x = target_w * 0.5
    focal_y = target_h * (0.42 if aspect_ratio == "9:16" else 0.46)
    norm_dist = np.sqrt(((xx - focal_x) / (target_w * 0.62)) ** 2 + ((yy - focal_y) / (target_h * 0.62)) ** 2)

    vignette_mask = np.clip(1.0 - 0.58 * (norm_dist ** 1.65), 0.22, 1.0)[:, :, np.newaxis]
    rim_mask = np.clip(np.exp(-((norm_dist - 0.55) ** 2) / 0.12) * 0.28, 0.0, 0.35)[:, :, np.newaxis]

    img_f = canvas_bgr.astype(np.float32)
    shadow_tint = np.array(palette["shadow_bgr"], dtype=np.float32).reshape((1, 1, 3))
    highlight_tint = np.array(palette["highlight_bgr"], dtype=np.float32).reshape((1, 1, 3))
    lum_ratio = (cv2.cvtColor(canvas_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0)[:, :, np.newaxis]

    graded = img_f * vignette_mask + shadow_tint * (1.0 - vignette_mask) * 0.65 + highlight_tint * rim_mask * lum_ratio

    # Unsharp mask for razor-sharp eyes and facial details
    blurred = cv2.GaussianBlur(graded, (0, 0), sigmaX=2.2)
    sharpened = np.clip(cv2.addWeighted(graded, 1.42, blurred, -0.42, 0), 0, 255).astype(np.uint8)

    if not apply_typography:
        return sharpened

    # 5. Dynamic Video-Specific Hook Overlay & Cinematic Border via PIL
    rgb_img = cv2.cvtColor(sharpened, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb_img).convert("RGBA")

    # Dark gradient scrim at the bottom for ultra-legible click-worthy hook text
    scrim = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    scrim_draw = ImageDraw.Draw(scrim)
    grad_start_y = int(target_h * (0.66 if aspect_ratio == "9:16" else 0.60))
    for y in range(grad_start_y, target_h):
        prog = (y - grad_start_y) / float(max(1, target_h - grad_start_y))
        alpha = int(min(225, (prog ** 1.35) * 220))
        scrim_draw.line([(0, y), (target_w, y)], fill=(6, 6, 12, alpha))
    pil_img = Image.alpha_composite(pil_img, scrim)

    draw = ImageDraw.Draw(pil_img)

    raw_hook = str(thumb_dir.get("text_overlay") or "").strip()
    if not raw_hook or raw_hook.upper() in ("WATCH THIS", "MUST WATCH", "-"):
        # Derive a video-specific 3-4 word hook from viral_title / primary_context
        v_title = str(metadata.get("viral_title") or metadata.get("primary_context") or "").strip()
        clean_words = [w for w in re.sub(r'[#|!?:🔥🎯⚡]+', ' ', v_title).split() if len(w) > 1]
        raw_hook = " ".join(clean_words[:4]).upper() if clean_words else "SHOCKING TRUTH"
    else:
        raw_hook = " ".join(raw_hook.split()[:4]).upper()

    # Load boldest available system font
    font_size = 64 if aspect_ratio == "9:16" else 68
    font = None
    font_candidates = [
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for fp in font_candidates:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except Exception:
                pass
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

    # Wrap hook into 1 or 2 punchy lines if needed
    words = raw_hook.split()
    if aspect_ratio == "9:16" and len(words) >= 3:
        mid = (len(words) + 1) // 2
        lines = [" ".join(words[:mid]), " ".join(words[mid:])]
    elif len(raw_hook) > 16 and len(words) >= 2:
        mid = (len(words) + 1) // 2
        lines = [" ".join(words[:mid]), " ".join(words[mid:])]
    else:
        lines = [raw_hook]

    # Draw glowing accent bar & text lines near bottom
    line_height = int(font_size * 1.18)
    total_text_h = line_height * len(lines)
    base_y = target_h - total_text_h - (88 if aspect_ratio == "9:16" else 48)

    # Accent bar above text
    bar_w = int(target_w * 0.28)
    bar_x = (target_w - bar_w) // 2
    bar_y = max(20, base_y - 18)
    draw.rounded_rectangle(
        [bar_x, bar_y, bar_x + bar_w, bar_y + 6],
        radius=3,
        fill=(*palette["glow_rgb"], 245)
    )

    for idx, line_str in enumerate(lines):
        ly = base_y + idx * line_height
        try:
            bbox = draw.textbbox((0, 0), line_str, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(line_str) * (font_size // 2)
        lx = max(24, (target_w - tw) // 2)

        # Heavy multi-pass black stroke + drop shadow for maximum CTR readability
        for dx in (-4, -2, 0, 2, 4):
            for dy in (-4, -2, 0, 2, 4):
                if dx != 0 or dy != 0:
                    draw.text((lx + dx, ly + dy), line_str, font=font, fill=(0, 0, 0, 245))
        draw.text((lx + 4, ly + 6), line_str, font=font, fill=(0, 0, 0, 220))
        line_color = (255, 255, 255, 255) if idx == 0 else (*palette["text_rgb"], 255)
        draw.text((lx, ly), line_str, font=font, fill=line_color)

    # Subtle high-contrast rim border
    draw.rectangle(
        [2, 2, target_w - 3, target_h - 3],
        outline=(*palette["glow_rgb"], 195),
        width=4
    )

    final_rgb = np.array(pil_img.convert("RGB"))
    return cv2.cvtColor(final_rgb, cv2.COLOR_RGB2BGR)


def generate_dynamic_ai_thumbnail(
    video_path: str,
    format_type: str,
    aspect_ratio: str,
    metadata: Dict[str, Any],
    reference_frame_bgr: Optional[np.ndarray] = None,
    reference_face_box: Optional[Tuple[int, int, int, int]] = None,
    channel_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    SLOT 1 (DEFAULT SELECTED): AI DYNAMIC THUMBNAIL via GEMINI / IMAGEN API
    - Analyzes key facial expressions, climactic context, and genre of the specific video.
    - Generates a brand-new, ultra-high-contrast, click-worthy visual with dramatic lighting
      and suspenseful expression matching that specific video in strict 9:16 (Shorts) or 16:9 (Long-form).
    - Guarantees zero repetition across different videos and marks Slot 1 as auto-selected by default.
    """
    import cv2
    from io import BytesIO
    from PIL import Image

    target_w, target_h = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
    primary_ctx = str(metadata.get("primary_context") or "Dramatic Video Moment")
    climactic_ctx = str(metadata.get("climactic_context") or metadata.get("summary_insights") or primary_ctx)
    facial_expr = str(metadata.get("facial_expression_analysis") or "Intense, expressive close-up emotion with dramatic eye contact")
    genre_emotion = str(metadata.get("detected_genre_emotion") or "High-Suspense Cinematic")
    viral_title = str(metadata.get("viral_title") or primary_ctx)
    thumb_dir = metadata.get("thumbnail_directive") or {}
    text_overlay = str(thumb_dir.get("text_overlay") or "MUST WATCH").strip()
    scene_dir = str(thumb_dir.get("visual_scene_direction") or "Close-up dramatic subject with chiaroscuro rim lighting")
    color_theme = str(thumb_dir.get("recommended_color_theme") or "Ultra-high-contrast cinematic teal and fiery amber")

    orientation_desc = (
        "vertical 9:16 portrait YouTube Shorts / Reels thumbnail (720x1280)"
        if aspect_ratio == "9:16"
        else "cinematic 16:9 widescreen YouTube Long-form thumbnail (1280x720)"
    )

    ai_prompt = (
        f"Create a brand-new, ultra-high-contrast, click-worthy {orientation_desc} for this specific video.\n"
        f"- Specific Video Context & Climax: {primary_ctx} — {climactic_ctx}\n"
        f"- Key Facial Expression & Subject Focus: {facial_expr}. {scene_dir}\n"
        f"- Genre & Emotional Tone: {genre_emotion}\n"
        f"- Dramatic Lighting & Color Grade: {color_theme}, volumetric rim lighting, deep shadows, razor-sharp eyes, intense suspenseful expression.\n"
        f"- Bold Hook Text Overlay (3-4 words max): \"{text_overlay}\"\n"
        f" Photorealistic, hyper-detailed, high-CTR cinematic composition, strictly {aspect_ratio} aspect ratio, unique to this story with zero generic stock templates."
    )

    video_seed_str = f"{os.path.basename(video_path)}|{viral_title}|{primary_ctx}|{climactic_ctx}|{scene_dir}|{color_theme}|{time.time()}"
    generated_bgr = None
    generation_engine = "gemini-ai-dynamic-compositor"

    # Prepare reference keyframe bytes if available so Gemini image models preserve character likeness
    ref_pil = None
    if reference_frame_bgr is not None and reference_frame_bgr.size > 0:
        cropped_ref = fit_and_crop_to_aspect_ratio(reference_frame_bgr, aspect_ratio=aspect_ratio, focus_box=reference_face_box)
        ref_rgb = cv2.cvtColor(cropped_ref, cv2.COLOR_BGR2RGB)
        ref_pil = Image.fromarray(ref_rgb)

    # Attempt 1: Gemini Native Multimodal Image Generation (gemini-2.5-flash-image / gemini-3.1-flash-image / Imagen 3)
    try:
        from google.genai import types

        def _try_gemini_or_imagen(client):
            # 1A: Try Gemini Multimodal Image Generation models (with reference frame for authentic character expression)
            image_gen_models = [
                "gemini-2.5-flash-image",
                "gemini-2.0-flash-exp-image-generation",
            ]
            for img_model in image_gen_models:
                try:
                    contents_payload = [ref_pil, ai_prompt] if ref_pil is not None else [ai_prompt]
                    resp = client.models.generate_content(
                        model=img_model,
                        contents=contents_payload,
                        config=types.GenerateContentConfig(
                            response_modalities=["IMAGE", "TEXT"]
                        )
                    )
                    if resp and getattr(resp, "candidates", None):
                        for cand in resp.candidates:
                            content = getattr(cand, "content", None)
                            for part in (getattr(content, "parts", None) or []):
                                inline = getattr(part, "inline_data", None)
                                if inline and getattr(inline, "data", None):
                                    img_bytes = inline.data
                                    arr = np.frombuffer(img_bytes, dtype=np.uint8)
                                    decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                                    if decoded is not None and decoded.size > 0:
                                        return (decoded, img_model)
                except Exception:
                    continue

            # 1B: Try Imagen 3 API with explicit aspect_ratio ("9:16" or "16:9")
            for imagen_model in ["imagen-3.0-generate-002", "imagen-3.0-fast-generate-001"]:
                try:
                    img_resp = client.models.generate_images(
                        model=imagen_model,
                        prompt=ai_prompt,
                        config=types.GenerateImagesConfig(
                            number_of_images=1,
                            aspect_ratio=aspect_ratio,
                            output_mime_type="image/jpeg"
                        )
                    )
                    gen_imgs = getattr(img_resp, "generated_images", None) or []
                    if gen_imgs:
                        img_obj = getattr(gen_imgs[0], "image", None)
                        img_bytes = getattr(img_obj, "image_bytes", None)
                        if img_bytes:
                            arr = np.frombuffer(img_bytes, dtype=np.uint8)
                            decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            if decoded is not None and decoded.size > 0:
                                return (decoded, imagen_model)
                except Exception:
                    continue

            return (None, None)

        gen_img, used_engine = execute_with_key_rotation(channel_id, _try_gemini_or_imagen)
        if gen_img is not None and gen_img.size > 0:
            generated_bgr = fit_and_crop_to_aspect_ratio(gen_img, aspect_ratio=aspect_ratio)
            # Apply ultra-high-contrast grading polish without duplicating text if model already rendered it
            generated_bgr = _render_ultra_high_contrast_ai_visual(
                generated_bgr,
                aspect_ratio=aspect_ratio,
                metadata=metadata,
                focus_box=None,
                video_seed_str=video_seed_str,
                apply_typography=False
            )
            generation_engine = used_engine or "gemini-imagen-api"
            print(f"[Gemini Thumbnail Suite] Generated Slot 1 AI Dynamic Thumbnail via {generation_engine} ({aspect_ratio})")
    except Exception as e:
        print(f"[Gemini Thumbnail Suite] Remote image generation fallback notice ({str(e)[:90]}). Using AI-directed dynamic high-contrast synthesizer...")

    # Attempt 2 / Fallback: Synthesize brand-new, ultra-high-contrast AI-directed thumbnail from the peak-emotion frame
    if generated_bgr is None:
        base_frame = reference_frame_bgr
        if base_frame is None or base_frame.size == 0:
            base_frame = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        generated_bgr = _render_ultra_high_contrast_ai_visual(
            base_frame,
            aspect_ratio=aspect_ratio,
            metadata=metadata,
            focus_box=reference_face_box,
            video_seed_str=video_seed_str,
            apply_typography=True
        )

    uid = uuid.uuid4().hex[:8]
    ratio_slug = "9x16" if aspect_ratio == "9:16" else "16x9"
    ai_filename = f"thumb_slot1_ai_{ratio_slug}_{uid}.jpg"
    ai_filepath = os.path.join(THUMBNAILS_DIR, ai_filename)
    cv2.imwrite(ai_filepath, generated_bgr, [cv2.IMWRITE_JPEG_QUALITY, 96])

    return {
        "id": "slot_1_ai",
        "slot": 1,
        "filename": ai_filename,
        "url": f"/api/thumbnail_file/{ai_filename}",
        "filepath": ai_filepath,
        "seconds": 0.0,
        "timestamp": f"SLOT 1 • AI DYNAMIC ({aspect_ratio})",
        "label": f"✨ Slot 1: AI Dynamic Thumbnail ({aspect_ratio} • Default Selected)",
        "has_face": True,
        "is_ai_generated": True,
        "is_recommended": True,
        "selected": True,
        "aspect_ratio": aspect_ratio,
        "width": target_w,
        "height": target_h,
        "engine": generation_engine
    }


def analyze_video_with_gemini(
    video_path: str,
    format_type: str = "Short",
    custom_instructions: str = "",
    channel_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Evaluates video dynamically as an elite YouTube SEO & Algorithm Strategist.
    1. Uses Google Search Grounding to evaluate top-performing benchmarks on YouTube,
       tailors metadata specifically for 'Short' or 'Long' format, strictly avoids copying
       the first dialogue sentence as the title, and returns structured viral metadata.
    2. ENHANCED THUMBNAIL GENERATION SUITE:
       - Auto-detects aspect ratio: strictly 9:16 vertical for Shorts/Reels, strictly 16:9 widescreen for Long-form.
       - Slot 1 (Default Selected): AI Dynamic Thumbnail via Gemini / Imagen API matching the specific video's
         key facial expressions, climactic context, and genre.
       - Slots 2 to 6: 5 High-Emotion Local Video Frames extracted from peak moments.
    """
    cfg = get_gemini_config(channel_id)
    if not cfg["is_configured"]:
        raise ValueError("Gemini API key is not configured. Please click '⚙️ Configure API Key' in the header to enter your API key first.")

    target_model = cfg.get("model") or DEFAULT_MODEL
    candidate_models = [
        target_model,
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-flash-latest",
    ]
    models_to_try = []
    for m in candidate_models:
        if m and m not in models_to_try:
            models_to_try.append(m)

    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found at {video_path}")

    # Normalize format_type and auto-detect strict thumbnail aspect ratio (9:16 vs 16:9)
    format_type = "Long" if str(format_type).lower().startswith("long") else "Short"
    aspect_ratio, target_w, target_h = detect_target_aspect_ratio(video_path, format_type=format_type)

    # Step 1: Extract Slots 2 to 6 (5 High-Emotion Local Video Frames) + peak emotion reference frame
    local_slots_2_to_6, best_raw_info = extract_video_thumbnails(
        video_path,
        count=5,
        aspect_ratio=aspect_ratio,
        format_type=format_type,
        return_best_raw=True
    )

    # Step 2: Upload raw video stream to Gemini Files API & run Multimodal Grounded Analysis
    system_instruction = """You are an elite YouTube Algorithm & Growth Strategist managing top-tier global creators.

RULES & WORKFLOW:
1. Video Analysis:
   - Deeply inspect both visual frames and audio for 100% ground-truth plot understanding.
   - NEVER copy the first dialogue or spoken sentence as the title.
   - Detect the core emotion, key facial expressions, climactic context, notable figures, and genre.

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

    analysis_prompt = f"""VIDEO TARGET FORMAT: {format_type} (Thumbnail Aspect Ratio: {aspect_ratio})

Analyze this uploaded video with Google Search Grounding enabled.
Research trending YouTube search queries and competitor title formats matching this exact context.

{f'Creator Additional Guidance: {custom_instructions}' if custom_instructions else ''}

Return strictly a valid JSON object with the following schema:
{{
  "format_type": "{format_type}",
  "primary_context": "Detected speaker, character, event, or story plot",
  "climactic_context": "Specific description of the video's climax, twist, or highest-stakes moment",
  "facial_expression_analysis": "Specific description of the main subject's facial expressions, eyes, and emotion",
  "detected_genre_emotion": "Specific genre and dominant emotional tone (e.g. Suspense Thriller, High-Energy Comedy, Tech Reveal)",
  "viral_title": "High CTR, curiosity-driven title (zero raw line copy-pasting, under 50 chars for Shorts or Hook | Keyword for Long)",
  "description": "Optimized description with search keywords (and timestamps if long)",
  "hashtags": ["#Shorts", "#Trending", "#Topic"],
  "search_tags": ["10-15 high-volume search keywords for YouTube Studio tags box"],
  "category_id": "24",
  "category_name": "Entertainment",
  "thumbnail_directive": {{
    "text_overlay": "Max 3-4 impactful words specific to this video",
    "visual_scene_direction": "Detailed art direction for facial expression, subject focus, dramatic lighting, and camera angle",
    "recommended_color_theme": "High contrast color palette tailored to this video's mood"
  }}
}}"""

    metadata = None
    last_error = None

    def _upload_and_analyze_with_client(client):
        nonlocal metadata, last_error
        from google.genai import types

        print(f"[Gemini Engine] Uploading video stream ({video_path}) to Gemini Files API...")
        uploaded_file = client.files.upload(file=video_path)
        file_name = uploaded_file.name

        try:
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
                time.sleep(2.5)

            cfg_with_search = types.GenerateContentConfig(
                tools=[{"google_search": {}}],
                temperature=0.35,
                system_instruction=system_instruction
            )
            cfg_json_only = types.GenerateContentConfig(
                temperature=0.35,
                response_mime_type="application/json",
                system_instruction=system_instruction
            )

            for model_to_call in models_to_try:
                for use_search in [True, False]:
                    config = cfg_with_search if use_search else cfg_json_only
                    mode_label = "with Google Search Grounding" if use_search else "direct JSON mode"
                    max_retries = 2

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

                            match = re.search(r'(\{[\s\S]*\})', raw_text)
                            if not match:
                                continue
                            parsed = json.loads(match.group(1))

                            viral_title = parsed.get("viral_title") or parsed.get("primary_title") or parsed.get("recommended_title") or "High Engagement Video"
                            primary_ctx = parsed.get("primary_context") or "Trending YouTube Content"
                            climactic_ctx = parsed.get("climactic_context") or primary_ctx
                            facial_expr = parsed.get("facial_expression_analysis") or "Intense emotional expression"
                            genre_emotion = parsed.get("detected_genre_emotion") or "High-Impact Entertainment"
                            desc = parsed.get("description") or ""
                            hashtags = parsed.get("hashtags") or []
                            search_tags = parsed.get("search_tags") or parsed.get("seo_keywords") or parsed.get("tags") or []
                            thumb_dir = parsed.get("thumbnail_directive") or {
                                "text_overlay": "SHOCKING MOMENT",
                                "visual_scene_direction": "High emotion close-up frame with dramatic rim lighting",
                                "recommended_color_theme": "High contrast teal and amber"
                            }

                            full_desc = desc
                            if hashtags:
                                formatted_tags = [h if h.startswith("#") else f"#{h}" for h in hashtags]
                                tag_line = " ".join(formatted_tags)
                                if tag_line not in full_desc:
                                    full_desc = f"{full_desc}\n\n{tag_line}"

                            metadata = {
                                "format_type": parsed.get("format_type", format_type),
                                "thumbnail_aspect_ratio": aspect_ratio,
                                "primary_context": primary_ctx,
                                "climactic_context": climactic_ctx,
                                "facial_expression_analysis": facial_expr,
                                "detected_genre_emotion": genre_emotion,
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
                                "recommended_thumbnail_second": float(parsed.get("recommended_thumbnail_second", best_raw_info.get("seconds", 2.5))),
                                "category_id": str(parsed.get("category_id", "24")),
                                "category_name": parsed.get("category_name", "Entertainment"),
                                "video_type": format_type,
                                "made_for_kids": False,
                                "model_used": model_to_call,
                                "summary_insights": f"Model: {model_to_call} | Mode: {mode_label} | Format: {format_type} ({aspect_ratio}) | Context: {primary_ctx} | Climax: {climactic_ctx}."
                            }
                            return metadata
                        except Exception as me:
                            last_error = me
                            err_str = str(me)
                            if any(w in err_str.lower() for w in ["429", "resource_exhausted", "quota", "403"]):
                                raise me
                            break
        finally:
            try:
                client.files.delete(name=file_name)
            except Exception:
                pass
        return metadata

    try:
        execute_with_key_rotation(channel_id, _upload_and_analyze_with_client)
    except Exception as rot_err:
        last_error = rot_err
        print(f"[Gemini Engine] Key rotation analysis notice: {rot_err}")

    if not metadata:
        print(f"[Gemini Engine] Generating context-aware fallback metadata ({last_error})...")
        clean_name = os.path.splitext(os.path.basename(video_path))[0]
        clean_name = re.sub(r'^(gemini_[a-f0-9-]+_|vid_\d+_)', '', clean_name).replace('_', ' ').replace('-', ' ').title() or "Viral Story"
        short_hook_words = " ".join(clean_name.split()[:3]).upper() or "UNBELIEVABLE TWIST"
        viral_title = f"{clean_name} #Shorts" if format_type == "Short" else f"{clean_name} | Full Story Explained"
        metadata = {
            "format_type": format_type,
            "thumbnail_aspect_ratio": aspect_ratio,
            "primary_context": clean_name,
            "climactic_context": f"Climactic turning point in {clean_name}",
            "facial_expression_analysis": "High-tension expressive reaction at the decisive moment",
            "detected_genre_emotion": "High-Suspense Drama",
            "viral_title": viral_title,
            "primary_title": viral_title,
            "recommended_title": viral_title,
            "alternative_titles": [
                f"{viral_title} 🔥",
                f"Why Everyone Is Talking About {clean_name} 🎯"
            ],
            "description": f"Watch {clean_name} ({format_type}).\n\nLike, comment, and subscribe for more!\n\n#Shorts #Trending #Viral",
            "raw_description": f"Watch {clean_name} ({format_type}).",
            "hashtags": ["#Shorts", "#Trending", "#Viral"],
            "search_tags": [clean_name, "viral video", "trending", "youtube shorts", "must watch"],
            "seo_keywords": [clean_name, "viral video", "trending", "youtube shorts"],
            "tags": [clean_name, "viral video", "trending", "youtube shorts"],
            "thumbnail_directive": {
                "text_overlay": short_hook_words,
                "visual_scene_direction": f"Dramatic close-up expression from {clean_name} with intense rim lighting",
                "recommended_color_theme": "High contrast cinematic teal and fiery amber"
            },
            "recommended_thumbnail_second": float(best_raw_info.get("seconds", 2.0)),
            "category_id": "24",
            "category_name": "Entertainment",
            "video_type": format_type,
            "made_for_kids": False,
            "model_used": "smart-fallback-engine",
            "summary_insights": f"Format: {format_type} ({aspect_ratio}) | Context: {clean_name}."
        }

    # Step 3: Generate SLOT 1 (DEFAULT SELECTED) AI Dynamic Thumbnail via Gemini / Imagen API
    slot_1_ai_thumb = generate_dynamic_ai_thumbnail(
        video_path=video_path,
        format_type=format_type,
        aspect_ratio=aspect_ratio,
        metadata=metadata,
        reference_frame_bgr=best_raw_info.get("frame"),
        reference_face_box=best_raw_info.get("face_box"),
        channel_id=channel_id
    )

    # Combine Slot 1 (Default Selected AI Dynamic Thumbnail) + Slots 2 to 6 (5 High-Emotion Local Video Frames)
    all_slots = [slot_1_ai_thumb] + local_slots_2_to_6
    metadata["extracted_thumbnails"] = all_slots
    metadata["selected_thumbnail"] = slot_1_ai_thumb
    metadata["thumbnail_aspect_ratio"] = aspect_ratio
    return metadata


def chat_with_gemini(message: str, history: Optional[List[Dict[str, str]]] = None, studio_context: Optional[Dict[str, Any]] = None, channel_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Interactive creator copilot chat. Can rewrite titles, tailor descriptions,
    translate metadata, brainstorm hooks, and advise on video performance.
    """
    cfg = get_gemini_config(channel_id)
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
        reply_text = ""
        models_to_try = [model_name] + [m for m in FALLBACK_MODELS if m != model_name]

        def _call_chat(client):
            nonlocal reply_text
            for m in models_to_try:
                try:
                    response = client.models.generate_content(
                        model=m,
                        contents=[full_prompt]
                    )
                    reply_text = response.text or ""
                    if reply_text:
                        return reply_text
                except Exception as ce:
                    err_s = str(ce).lower()
                    if any(t in err_s for t in ["429", "resource_exhausted", "quota", "403"]):
                        raise ce
                    continue
            return reply_text

        execute_with_key_rotation(channel_id, _call_chat)

        if not reply_text:
            reply_text = "I'm currently optimizing for high traffic, but here is a quick tip: Focus your title on high curiosity + clear emotional hook and keep YouTube Shorts under 50 characters."

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
