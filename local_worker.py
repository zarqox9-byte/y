#!/usr/bin/env python3
"""
LOCAL WORKER ENGINE - HYBRID ARCHITECTURE FOR YOUTUBE STUDIO PRO
================================================================
This local worker runs on your local PC to handle the heavy compute and video streaming:
- Direct video stream extraction using unblocked residential/local ISP connection
- Multi-cut narrative sub-clip slicing via FFmpeg
- OpenCV face tracking and vertical 9:16 reframing
- 100% original movie audio muting (YouTube Content ID safe)
- Neural Edge-TTS voiceover synthesis (Hindi/English)
- Ambient cinematic BGM overlay and fast concatenation
- Automatic checkpoint updates and sync to local/remote web studio
"""

import argparse
import glob
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# Ensure safe console output on Windows
if hasattr(sys.stdout, "reconfigure"):
  sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
  sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [LocalWorker] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("LocalWorker")

# Import core clipper engine
import clipper_engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "uploads", "clipper_jobs")
SHORTS_DIR = os.path.join(BASE_DIR, "uploads", "clipper_shorts")
DEFAULT_REMOTE_URL = "https://youtube-studio-pro.onrender.com"

os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(SHORTS_DIR, exist_ok=True)


class LocalWorkerEngine:

  def __init__(self, remote_url: Optional[str] = None):
    self.remote_url = (remote_url or DEFAULT_REMOTE_URL).rstrip("/")
    logger.info(
        "Local Worker Engine initialized. Ready for local batch rendering."
    )

  def process_job_all_parts(
      self,
      job_id: str,
      force_rerun: bool = False,
      sync_to_remote: bool = False,
  ) -> Dict[str, Any]:
    """Processes all uncompleted parts of a job locally on this PC."""
    ckpt_path = os.path.join(JOBS_DIR, f"{job_id}.json")
    if not os.path.exists(ckpt_path):
      raise FileNotFoundError(f"Job checkpoint not found at: {ckpt_path}")

    with open(ckpt_path, "r", encoding="utf-8") as f:
      ckpt = json.load(f)

    youtube_url = ckpt.get("youtube_url") or ckpt.get("url") or (ckpt.get("video_info") or {}).get("webpage_url") or ""
    movie_title = ckpt.get("movie_title") or (ckpt.get("video_info") or {}).get("title") or "Movie Recap"
    language = ckpt.get("language") or (ckpt.get("options") or {}).get("language") or "Hindi"
    scenes = ckpt.get("scenes", [])
    total_parts = len(scenes)

    raw_completed = ckpt.get("completed_shorts") or {}
    if isinstance(raw_completed, list):
      completed_shorts = {
          str(s.get("part", i + 1)): s for i, s in enumerate(raw_completed)
      }
    elif isinstance(raw_completed, dict):
      completed_shorts = dict(raw_completed)
    else:
      completed_shorts = {}

    logger.info(f"==================================================")
    logger.info(f"PROCESSING JOB LOCALLY: {job_id}")
    logger.info(f"Movie Title: {movie_title}")
    logger.info(f"Total Parts: {total_parts}")
    logger.info(
        f"Already Completed Parts: {list(completed_shorts.keys()) or 'None'}"
    )
    logger.info(f"==================================================")

    ckpt["status"] = "PROCESSING"
    clipper_engine.save_job_checkpoint(job_id, ckpt)

    for idx, scene in enumerate(scenes, 1):
      part_num = scene.get("part", idx)
      part_key = str(part_num)

      # Check if already completed and video file exists
      if not force_rerun and part_key in completed_shorts:
        existing_short = completed_shorts[part_key]
        filename = existing_short.get("filename", "")
        filepath = os.path.join(SHORTS_DIR, filename)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 1000000:
          logger.info(
              f"✓ Part {part_num} already complete ({os.path.getsize(filepath) / (1024*1024):.2f} MB). Skipping."
          )
          continue

      logger.info(f"\n>>> Starting Local Rendering for Part {part_num}/{total_parts}...")
      t_start = time.time()

      def progress_cb(pct: int, msg: str):
        print(f"  [Part {part_num} | {pct:03d}%] {msg}")

      try:
        short_data = clipper_engine.process_single_short_pipeline(
            youtube_url=youtube_url,
            scene=scene,
            language=language,
            video_title=movie_title,
            job_id=job_id,
            progress_callback=progress_cb,
        )

        completed_shorts[part_key] = short_data
        ckpt["completed_shorts"] = completed_shorts
        ckpt["status"] = (
            "COMPLETED" if len(completed_shorts) >= total_parts else "PROCESSING"
        )
        clipper_engine.save_job_checkpoint(job_id, ckpt)

        elapsed = time.time() - t_start
        filename = short_data.get("filename")
        vpath = os.path.join(SHORTS_DIR, filename)
        fsize_mb = (
            os.path.getsize(vpath) / (1024 * 1024) if os.path.exists(vpath) else 0
        )
        logger.info(
            f"🎉 Part {part_num} SUCCESS in {elapsed:.1f}s | File: {filename} ({fsize_mb:.2f} MB)"
        )

        if sync_to_remote:
          self.sync_short_to_remote(job_id, short_data)

      except Exception as pe:
        logger.error(f"❌ Error processing Part {part_num}: {pe}", exc_info=True)
        ckpt["error"] = str(pe)
        clipper_engine.save_job_checkpoint(job_id, ckpt)
        if "429" in str(pe) or "quota" in str(pe).lower():
          ckpt["status"] = "PAUSED_QUOTA_LIMIT"
          clipper_engine.save_job_checkpoint(job_id, ckpt)
          logger.warning(
              f"Job paused due to API quota. Resume after updating key or waiting."
          )
          break

    # Final summary
    with open(ckpt_path, "r", encoding="utf-8") as f:
      final_ckpt = json.load(f)

    done_count = len(final_ckpt.get("completed_shorts", {}))
    logger.info(f"\n==================================================")
    logger.info(f"BATCH COMPLETE FOR JOB: {job_id}")
    logger.info(f"Finished {done_count}/{total_parts} parts locally.")
    logger.info(f"Status: {final_ckpt.get('status')}")
    logger.info(f"==================================================")
    return final_ckpt

  def run_new_url_job(
      self,
      youtube_url: str,
      max_shorts: int = 5,
      language: str = "Hindi",
      target_duration: int = 58,
      sync_to_remote: bool = False,
  ) -> str:
    """Analyzes a movie URL and processes all parts locally from scratch."""
    logger.info(f"Analyzing movie narrative: {youtube_url} ({max_shorts} parts)...")
    video_info = clipper_engine.extract_youtube_info(youtube_url)
    job_id = f"job_local_{int(time.time())}"

    scenes, summary, returned_job_id = (
        clipper_engine.analyze_movie_narrative_for_shorts(
            youtube_url=youtube_url,
            video_info=video_info,
            max_shorts=max_shorts,
            target_duration=target_duration,
            language=language,
            job_id=job_id,
        )
    )

    logger.info(
        f"Analysis complete. Planned exactly {len(scenes)} parts. Starting batch processing..."
    )
    self.process_job_all_parts(
        job_id, force_rerun=False, sync_to_remote=sync_to_remote
    )
    return job_id

  def sync_short_to_remote(
      self, job_id: str, short_data: Dict[str, Any]
  ) -> bool:
    """Uploads a locally rendered MP4 and thumbnail to the remote Render server."""
    filename = short_data.get("filename")
    thumb_name = short_data.get("thumbnail_url", "").replace(
        "/api/clipper/media/", ""
    )
    video_path = os.path.join(SHORTS_DIR, filename)
    thumb_path = os.path.join(SHORTS_DIR, thumb_name)

    if not os.path.exists(video_path):
      logger.warning(f"Cannot sync: Video file {video_path} does not exist.")
      return False

    sync_url = f"{self.remote_url}/api/clipper/sync_rendered_short"
    logger.info(
        f"Syncing Part {short_data.get('part')} ({filename}) to {self.remote_url}..."
    )

    try:
      import mimetypes
      boundary = f"----WebKitFormBoundary{time.time():.0f}"
      body = bytearray()

      def add_field(name: str, value: str):
        nonlocal body
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                "utf-8"
            )
        )
        body.extend(f"{value}\r\n".encode("utf-8"))

      def add_file(field_name: str, file_path: str, mime: str):
        nonlocal body
        fname = os.path.basename(file_path)
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{field_name}";'
            f' filename="{fname}"\r\n'.encode("utf-8")
        )
        body.extend(f"Content-Type: {mime}\r\n\r\n".encode("utf-8"))
        with open(file_path, "rb") as f:
          body.extend(f.read())
        body.extend(b"\r\n")

      add_field("job_id", job_id)
      add_field("part", str(short_data.get("part", 1)))
      add_field("short_json", json.dumps(short_data))
      add_file("video", video_path, "video/mp4")
      if os.path.exists(thumb_path):
        add_file("thumbnail", thumb_path, "image/jpeg")
      body.extend(f"--{boundary}--\r\n".encode("utf-8"))

      req = urllib.request.Request(
          sync_url,
          data=bytes(body),
          headers={
              "Content-Type": f"multipart/form-data; boundary={boundary}",
              "User-Agent": "LocalWorker/1.0",
          },
          method="POST",
      )
      with urllib.request.urlopen(req, timeout=120) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if res.get("success"):
          logger.info(f"✓ Part {short_data.get('part')} synced to remote successfully!")
          return True
        else:
          logger.warning(f"Remote sync response: {res}")
    except Exception as e:
      logger.warning(f"Remote sync notice: {e}")

    return False

  def run_watch_daemon(self, poll_interval: int = 5):
    """Watches local uploads/clipper_jobs/ for pending jobs and processes them continuously."""
    logger.info(
        f"Local Worker Daemon started in watch mode (checking every"
        f" {poll_interval}s)..."
    )
    logger.info(f"Monitoring folder: {JOBS_DIR}")

    while True:
      try:
        job_files = glob.glob(os.path.join(JOBS_DIR, "*.json"))
        for jf in job_files:
          job_id = os.path.splitext(os.path.basename(jf))[0]
          try:
            with open(jf, "r", encoding="utf-8") as f:
              data = json.load(f)
            scenes = data.get("scenes", [])
            completed = data.get("completed_shorts", {})
            status = data.get("status", "")

            # If job has uncompleted scenes and is marked for processing or analyzed
            if scenes and len(completed) < len(scenes) and status != "ERROR":
              logger.info(
                  f"Found pending job in queue: {job_id} ({len(completed)}/{len(scenes)} parts done)"
              )
              self.process_job_all_parts(job_id)
          except Exception as je:
            logger.debug(f"Error reading job {jf}: {je}")
      except Exception as e:
        logger.error(f"Daemon watch loop error: {e}")

      time.sleep(poll_interval)


def main():
  parser = argparse.ArgumentParser(
      description="Local Worker Engine for YouTube Studio Pro"
  )
  parser.add_argument(
      "--job", type=str, help="Process all parts of a specific job_id"
  )
  parser.add_argument("--url", type=str, help="Analyze and generate from a YouTube URL")
  parser.add_argument(
      "--parts", type=int, default=5, help="Number of parts (default: 5)"
  )
  parser.add_argument(
      "--lang", type=str, default="Hindi", help="Voiceover language (default: Hindi)"
  )
  parser.add_argument(
      "--duration", type=int, default=58, help="Target duration in seconds (default: 58)"
  )
  parser.add_argument(
      "--watch", action="store_true", help="Run in continuous daemon watch mode"
  )
  parser.add_argument(
      "--sync",
      action="store_true",
      help="Sync completed MP4s and checkpoints to remote web app",
  )
  parser.add_argument(
      "--remote",
      type=str,
      default=DEFAULT_REMOTE_URL,
      help="Remote server URL",
  )

  args = parser.parse_args()
  engine = LocalWorkerEngine(remote_url=args.remote)

  if args.job:
    engine.process_job_all_parts(args.job, sync_to_remote=args.sync)
  elif args.url:
    engine.run_new_url_job(
        args.url,
        max_shorts=args.parts,
        language=args.lang,
        target_duration=args.duration,
        sync_to_remote=args.sync,
    )
  elif args.watch:
    engine.run_watch_daemon()
  else:
    # If no argument passed, scan for the most recent local job and process it
    job_files = sorted(
        glob.glob(os.path.join(JOBS_DIR, "*.json")),
        key=os.path.getmtime,
        reverse=True,
    )
    if job_files:
      latest_job = os.path.splitext(os.path.basename(job_files[0]))[0]
      logger.info(f"No arguments provided. Processing latest job: {latest_job}")
      engine.process_job_all_parts(latest_job, sync_to_remote=args.sync)
    else:
      parser.print_help()


if __name__ == "__main__":
  main()
