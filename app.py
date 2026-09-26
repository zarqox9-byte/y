import os
import sys
import json
import uuid
import time
import shutil
import threading
import tempfile
from typing import Dict, Any, List, Optional
from datetime import datetime
import socket
import ssl
import http.client
import httplib2
from googleapiclient.errors import HttpError
from flask import Flask, request, redirect, session, url_for, jsonify, render_template_string, send_from_directory
from werkzeug.utils import secure_filename
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
import google.auth.transport.requests
from werkzeug.middleware.proxy_fix import ProxyFix

import gemini_engine
import clipper_engine
import channel_key_store

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "youtube_studio_pro_permanent_production_secret_2026")
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=86400 * 30
)

# Enable ProxyFix for reverse proxies (Render, Cloudflare, etc.)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# Allow HTTP and relaxed scope matching for local testing
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube',
    'https://www.googleapis.com/auth/youtube.force-ssl',
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid'
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRETS_FILE = os.path.join(BASE_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")
ACCOUNTS_STORE_FILE = os.path.join(BASE_DIR, "user_accounts.json")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(gemini_engine.THUMBNAILS_DIR, exist_ok=True)

# Cloud deployment environment variable fallback
if not os.path.exists(CLIENT_SECRETS_FILE) and os.environ.get("GOOGLE_CLIENT_SECRET_JSON"):
    try:
        with open(CLIENT_SECRETS_FILE, "w") as f:
            f.write(os.environ["GOOGLE_CLIENT_SECRET_JSON"])
    except Exception as e:
        print(f"Notice: Failed to write CLIENT_SECRETS_FILE from env: {e}")

if not os.path.exists(TOKEN_FILE) and os.environ.get("YOUTUBE_TOKEN_JSON"):
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(os.environ["YOUTUBE_TOKEN_JSON"])
    except Exception as e:
        print(f"Notice: Failed to write TOKEN_FILE from env: {e}")

# In-memory tracking of background upload tasks
upload_tasks = {}

def load_accounts_store() -> dict:
    if os.path.exists(ACCOUNTS_STORE_FILE):
        try:
            with open(ACCOUNTS_STORE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading accounts store: {e}")
    return {}

def save_accounts_store(data: dict):
    try:
        with open(ACCOUNTS_STORE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving accounts store: {e}")

def save_user_account(email: str, creds_dict: dict, channels: list, active_channel_id: str = None) -> str:
    accounts = load_accounts_store()
    account_key = email.lower().strip() if email else (active_channel_id or "default")
    accounts[account_key] = {
        "email": email,
        "credentials": creds_dict,
        "channels": channels,
        "active_channel_id": active_channel_id or (channels[0]['id'] if channels else None),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    save_accounts_store(accounts)
    return account_key

def get_active_channel_id_or_default(explicit_channel_id: str = None) -> str:
    """Returns active channel ID from request, session, or accounts store, resolving real channel IDs over 'default'."""
    ch_id = (explicit_channel_id or "").strip()
    if ch_id and ch_id.lower() != "default":
        return ch_id

    try:
        if request:
            req_ch = request.args.get('channel_id')
            if not req_ch and request.is_json:
                data = request.get_json(silent=True) or {}
                req_ch = data.get('channel_id')
            if not req_ch:
                req_ch = request.headers.get('X-Channel-Id') or request.form.get('channel_id')
            if req_ch and str(req_ch).strip().lower() != "default":
                return str(req_ch).strip()
    except Exception:
        pass

    try:
        sess_ch = session.get('active_channel_id')
        if sess_ch and str(sess_ch).strip().lower() != "default":
            return str(sess_ch).strip()
    except Exception:
        pass

    try:
        acc_key = session.get('active_account_key') if session else None
        store = load_accounts_store()
        if acc_key and acc_key in store and store[acc_key].get('active_channel_id'):
            cand = str(store[acc_key]['active_channel_id']).strip()
            if cand and cand.lower() != "default":
                return cand
        if store:
            for acc in store.values():
                if acc.get('active_channel_id') and str(acc['active_channel_id']).strip().lower() != "default":
                    return str(acc['active_channel_id']).strip()
                elif acc.get('channels') and len(acc['channels']) > 0:
                    cid = str(acc['channels'][0].get('id') or '').strip()
                    if cid and cid.lower() != "default":
                        return cid
    except Exception:
        pass

    try:
        import channel_key_store
        db_cid = channel_key_store.get_first_authenticated_channel_id()
        if db_cid and db_cid.lower() != "default":
            return db_cid
    except Exception:
        pass

    return ch_id if ch_id else "default"

def get_oauth_redirect_uri():
    # Force HTTPS when behind reverse proxy like Render or if request is secure
    if request.headers.get('X-Forwarded-Proto') == 'https' or request.is_secure:
        scheme = 'https'
    else:
        scheme = request.scheme
    return url_for('oauth2callback', _external=True, _scheme=scheme)

def get_stored_credentials():
    creds = None
    account_key = session.get('active_account_key')

    # 1. Check session credentials first (isolated per browser session)
    if 'credentials' in session:
        try:
            creds = Credentials(**session['credentials'])
        except Exception:
            creds = None

    # 2. Check accounts store by active account key
    if not creds and account_key:
        accounts = load_accounts_store()
        if account_key in accounts:
            try:
                creds_data = accounts[account_key]['credentials']
                creds = Credentials(**creds_data)
                session['credentials'] = creds_data
                session['user_email'] = accounts[account_key].get('email', '')
                if not session.get('active_channel_id'):
                    session['active_channel_id'] = accounts[account_key].get('active_channel_id')
            except Exception:
                creds = None

    # 3. Check any account from persistent accounts store
    if not creds:
        accounts = load_accounts_store()
        if accounts:
            first_key = next(iter(accounts))
            try:
                creds_data = accounts[first_key]['credentials']
                creds = Credentials(**creds_data)
                session['active_account_key'] = first_key
                session['credentials'] = creds_data
                session['user_email'] = accounts[first_key].get('email', '')
                if not session.get('active_channel_id'):
                    session['active_channel_id'] = accounts[first_key].get('active_channel_id')
            except Exception:
                creds = None

    # 4. Fallback to initial TOKEN_FILE if available
    if not creds and os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'r') as f:
                creds_data = json.load(f)
                creds = Credentials(**creds_data)
                session['credentials'] = creds_data
        except Exception as e:
            print(f"Error loading token.json: {e}")
            creds = None

    # Auto token refresh handling
    if creds and creds.expired and creds.refresh_token:
        try:
            req = google.auth.transport.requests.Request()
            creds.refresh(req)
            updated_dict = {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': creds.scopes
            }
            session['credentials'] = updated_dict
            act_k = session.get('active_account_key')
            if act_k:
                accounts = load_accounts_store()
                if act_k in accounts:
                    accounts[act_k]['credentials'] = updated_dict
                    save_accounts_store(accounts)
            try:
                with open(TOKEN_FILE, 'w') as f:
                    json.dump(updated_dict, f)
            except Exception:
                pass
        except Exception as e:
            print(f"Token refresh failed: {e}")
            return None

    return creds

HTML_MAIN = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>YouTube Creator Studio Pro + Gemini AI</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/@ffmpeg/ffmpeg@0.11.6/dist/ffmpeg.min.js"></script>
    <style>
        :root {
            --bg-base: #0f0f0f;
            --bg-surface: #1f1f1f;
            --bg-elevated: #282828;
            --bg-input: #121212;
            --border-color: #383838;
            --accent-red: #ff0000;
            --accent-red-hover: #cc0000;
            --accent-blue: #3ea6ff;
            --accent-ai: #a855f7;
            --accent-ai-hover: #9333ea;
            --accent-ai-glow: rgba(168, 85, 247, 0.35);
            --text-primary: #f1f1f1;
            --text-secondary: #aaaaaa;
            --text-muted: #717171;
            --success-color: #2ba640;
            --warning-color: #ffba08;
            --card-radius: 12px;
        }

        * { box-sizing: border-box; }
        body {
            font-family: 'Roboto', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: var(--bg-base);
            color: var(--text-primary);
            margin: 0;
            padding: 0;
            min-width: 0;
            width: 100%;
            overflow-x: hidden;
            -webkit-tap-highlight-color: transparent;
        }

        /* Top Navbar */
        .top-navbar {
            height: 64px;
            background: var(--bg-surface);
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 28px;
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .brand {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 20px;
            font-weight: 700;
            letter-spacing: -0.3px;
        }
        .brand-logo {
            width: 38px;
            height: 26px;
            background: var(--accent-red);
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .brand-logo svg { fill: white; width: 18px; height: 18px; }
        .brand span { color: white; }
        .brand .badge {
            background: #333;
            color: var(--accent-red);
            font-size: 11px;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 600;
        }
        .brand .ai-badge {
            background: linear-gradient(135deg, #7928ca, #ff0080);
            color: white;
            font-size: 11px;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 700;
            letter-spacing: 0.5px;
            box-shadow: 0 0 10px rgba(121, 40, 202, 0.4);
        }

        .user-nav {
            display: flex;
            align-items: center;
            gap: 14px;
        }

        .gemini-status-pill {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(168, 85, 247, 0.12);
            border: 1px solid rgba(168, 85, 247, 0.35);
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.2s;
            color: #d8b4fe;
            font-weight: 500;
        }
        .gemini-status-pill:hover {
            background: rgba(168, 85, 247, 0.25);
            border-color: var(--accent-ai);
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #a855f7;
            box-shadow: 0 0 8px #a855f7;
        }
        .status-dot.active { background: #2ba640; box-shadow: 0 0 8px #2ba640; }
        .status-dot.warning { background: #ffba08; box-shadow: 0 0 8px #ffba08; }

        .account-nav-wrap {
            position: relative;
        }
        .account-pill {
            display: flex;
            align-items: center;
            gap: 10px;
            background: var(--bg-elevated);
            padding: 5px 14px 5px 6px;
            border-radius: 20px;
            border: 1px solid var(--border-color);
            font-size: 13px;
            cursor: pointer;
            transition: all 0.2s;
            user-select: none;
        }
        .account-pill:hover {
            border-color: #555;
            background: #252525;
        }
        .account-pill img {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            object-fit: cover;
        }
        .account-dropdown {
            position: absolute;
            top: calc(100% + 8px);
            right: 0;
            width: 320px;
            max-width: 90vw;
            background: #1c1c20;
            border: 1px solid #333338;
            border-radius: 14px;
            box-shadow: 0 16px 40px rgba(0,0,0,0.85);
            display: none;
            flex-direction: column;
            z-index: 1500;
            overflow: hidden;
            pointer-events: none;
            animation: fadeInDrop 0.18s ease-out;
        }
        .account-dropdown.show {
            display: flex;
            pointer-events: auto;
        }
        @keyframes fadeInDrop {
            from { opacity: 0; transform: translateY(-6px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .dropdown-email-header {
            padding: 14px 16px;
            background: #141417;
            border-bottom: 1px solid #29292e;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .dropdown-email-avatar {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            object-fit: cover;
            border: 2px solid var(--accent-red);
        }
        .dropdown-email-info {
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .dropdown-email-name {
            font-weight: 700;
            font-size: 14px;
            color: #fff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .dropdown-email-addr {
            font-size: 12px;
            color: #9ca3af;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .dropdown-section-title {
            padding: 10px 16px 6px 16px;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            color: #71717a;
            font-weight: 700;
        }
        .dropdown-channels-list {
            max-height: 200px;
            overflow-y: auto;
            padding: 4px 8px;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .dropdown-channel-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 10px;
            border-radius: 8px;
            cursor: pointer;
            transition: background 0.15s;
            text-decoration: none;
            color: #e5e7eb;
        }
        .dropdown-channel-item:hover {
            background: #27272a;
        }
        .dropdown-channel-item.active-channel {
            background: rgba(255, 0, 0, 0.15);
            border: 1px solid rgba(255, 0, 0, 0.4);
        }
        .dropdown-channel-item img {
            width: 32px;
            height: 32px;
            border-radius: 50%;
            object-fit: cover;
        }
        .channel-item-details {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .channel-item-title {
            font-size: 13px;
            font-weight: 600;
            color: #fff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .channel-item-subs {
            font-size: 11px;
            color: #9ca3af;
        }
        .active-check-badge {
            color: #ff3333;
            font-size: 15px;
            font-weight: bold;
        }
        .dropdown-actions-menu {
            border-top: 1px solid #29292e;
            padding: 6px 8px;
            display: flex;
            flex-direction: column;
            gap: 2px;
            background: #151518;
        }
        .dropdown-action-btn {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px 12px;
            border-radius: 8px;
            font-size: 13px;
            color: #d1d5db;
            text-decoration: none;
            cursor: pointer;
            transition: background 0.15s, color 0.15s;
        }
        .dropdown-action-btn:hover {
            background: #27272a;
            color: #fff;
        }
        .dropdown-action-btn.btn-switch-act {
            color: #60a5fa;
            font-weight: 600;
        }
        .dropdown-action-btn.btn-switch-act:hover {
            background: rgba(59, 130, 246, 0.15);
            color: #93c5fd;
        }
        .dropdown-action-btn.btn-logout-act {
            color: #f87171;
        }
        .dropdown-action-btn.btn-logout-act:hover {
            background: rgba(239, 68, 68, 0.15);
            color: #fca5a5;
        }
        .btn-sidebar-switch {
            width: 100%;
            margin-top: 14px;
            background: #232328;
            border: 1px solid #3a3a42;
            color: #e2e8f0;
            padding: 9px 12px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: all 0.2s;
        }
        .btn-sidebar-switch:hover {
            background: #2c2c34;
            border-color: #60a5fa;
            color: #fff;
        }
        .btn-logout {
            background: transparent;
            color: var(--text-secondary);
            border: 1px solid var(--border-color);
            padding: 6px 14px;
            border-radius: 18px;
            font-size: 13px;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.2s;
        }
        .btn-logout:hover {
            color: white;
            border-color: #666;
            background: #2a2a2a;
        }

        /* Layout Grid */
        .main-container {
            max-width: 1400px;
            margin: 24px auto;
            padding: 0 24px;
            display: grid;
            grid-template-columns: 320px 1fr;
            gap: 24px;
        }

        /* Sidebar Channel Overview */
        .sidebar {
            display: flex;
            flex-direction: column;
            gap: 20px;
        }
        .card {
            background: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: var(--card-radius);
            padding: 22px;
        }
        .channel-profile {
            text-align: center;
        }
        .channel-avatar {
            width: 90px;
            height: 90px;
            border-radius: 50%;
            border: 3px solid var(--accent-red);
            margin-bottom: 12px;
            object-fit: cover;
        }
        .channel-name {
            font-size: 18px;
            font-weight: 700;
            margin: 0 0 4px 0;
        }
        .channel-handle {
            color: var(--text-secondary);
            font-size: 13px;
            margin-bottom: 14px;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-top: 14px;
        }
        .stat-box {
            background: var(--bg-elevated);
            padding: 12px;
            border-radius: 8px;
            text-align: center;
        }
        .stat-value {
            font-size: 18px;
            font-weight: 700;
            color: white;
        }
        .stat-label {
            font-size: 11px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 4px;
        }

        .quota-card h4 {
            margin: 0 0 10px 0;
            font-size: 14px;
            display: flex;
            justify-content: space-between;
        }
        .quota-bar-bg {
            height: 8px;
            background: #333;
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 6px;
        }
        .quota-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #2ba640, #ffba08);
            width: 16%;
        }
        .quota-info {
            font-size: 12px;
            color: var(--text-secondary);
            display: flex;
            justify-content: space-between;
        }

        /* Workspace Header & Mode Tabs */
        .workspace {
            display: flex;
            flex-direction: column;
            gap: 24px;
        }
        /* Global Touch & Interaction Guarantee: All navigation tabs, inputs, and buttons */
        .mode-tab,
        button,
        input,
        select,
        textarea,
        .form-control,
        .btn-upload,
        .btn-populate,
        #trimmer-tab,
        #copilot-tab,
        #tabTrimmerMode,
        #tabGeminiMode,
        #tabClipperMode,
        #tabManualMode {
            pointer-events: auto !important;
            touch-action: manipulation;
            position: relative;
            z-index: 9999;
            -webkit-tap-highlight-color: transparent;
        }
        .mode-nav-tabs {
            position: relative;
            z-index: 9999;
            pointer-events: auto !important;
            display: flex;
            gap: 10px;
            background: var(--bg-surface);
            padding: 8px;
            border-radius: var(--card-radius);
            border: 1px solid var(--border-color);
        }
        .mode-tab {
            cursor: pointer !important;
            user-select: none;
            -webkit-user-select: none;
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            padding: 12px 18px;
            border-radius: 8px;
            border: none;
            font-size: 14px;
            font-weight: 600;
            color: var(--text-secondary);
            background: transparent;
            transition: all 0.2s;
        }
        .mode-tab * {
            pointer-events: none;
        }
        .mode-tab:hover {
            color: white;
            background: rgba(255, 255, 255, 0.04);
        }
        .mode-tab.active-ai {
            background: linear-gradient(135deg, rgba(168, 85, 247, 0.2), rgba(121, 40, 202, 0.25));
            color: #e9d5ff;
            border: 1px solid var(--accent-ai);
            box-shadow: 0 4px 15px var(--accent-ai-glow);
        }
        .mode-tab.active-manual {
            background: #2a2a2a;
            color: white;
            border: 1px solid #444;
        }
        .mode-tab .tab-badge {
            font-size: 10px;
            padding: 2px 7px;
            border-radius: 10px;
            font-weight: 700;
        }
        .badge-ai {
            background: linear-gradient(135deg, #7928ca, #ff0080);
            color: white;
        }
        .badge-manual {
            background: #444;
            color: #ddd;
        }

        .trimmer-explainer-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 12px;
            align-items: end;
            margin-bottom: 14px;
        }

        .voice-carousel-scroll {
            display: flex;
            overflow-x: auto;
            gap: 14px;
            padding: 6px 4px 14px 4px;
            scroll-snap-type: x mandatory;
            -webkit-overflow-scrolling: touch;
            scrollbar-width: thin;
            scrollbar-color: #7c3aed rgba(255,255,255,0.05);
        }
        .voice-carousel-card {
            flex: 0 0 235px;
            scroll-snap-align: start;
            background: rgba(15, 23, 42, 0.92);
            border: 1.5px solid rgba(124, 58, 237, 0.35);
            border-radius: 12px;
            padding: 14px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            gap: 10px;
            transition: all 0.2s ease;
            position: relative;
        }
        .voice-carousel-card.selected-voice {
            border-color: #10b981;
            background: linear-gradient(145deg, rgba(16, 185, 129, 0.16), rgba(15, 23, 42, 0.95));
            box-shadow: 0 0 18px rgba(16, 185, 129, 0.35);
        }
        .seq-step-pill {
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 11.5px;
            font-weight: 700;
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.12);
            color: #94a3b8;
            white-space: nowrap;
            transition: all 0.2s ease;
        }
        .seq-step-pill.active-step {
            background: rgba(168, 85, 247, 0.25);
            border-color: #a855f7;
            color: #f3e8ff;
            box-shadow: 0 0 10px rgba(168, 85, 247, 0.35);
        }
        .seq-step-pill.done-step {
            background: rgba(16, 185, 129, 0.2);
            border-color: #10b981;
            color: #6ee7b7;
        }

        /* Gemini AI Studio Box */
        .ai-banner {
            background: linear-gradient(135deg, rgba(168, 85, 247, 0.12), rgba(30, 20, 50, 0.5));
            border: 1px solid rgba(168, 85, 247, 0.35);
            border-radius: var(--card-radius);
            padding: 20px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .ai-banner-left {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .ai-banner-title {
            font-size: 18px;
            font-weight: 700;
            color: #f3e8ff;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .ai-banner-desc {
            font-size: 13px;
            color: #c084fc;
        }

        /* Video Format Selector */
        .format-selector-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 14px;
            margin-bottom: 16px;
        }
        .format-card {
            background: #171221;
            border: 2px solid rgba(168, 85, 247, 0.25);
            border-radius: 10px;
            padding: 16px 18px;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            flex-direction: column;
            gap: 6px;
            position: relative;
        }
        .format-card:hover {
            border-color: rgba(168, 85, 247, 0.6);
            background: #1e172e;
        }
        .format-card.active {
            border-color: var(--accent-ai);
            background: linear-gradient(135deg, rgba(168, 85, 247, 0.18), rgba(121, 40, 202, 0.25));
            box-shadow: 0 4px 20px rgba(168, 85, 247, 0.25);
        }
        .format-card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .format-icon {
            font-size: 24px;
        }
        .format-badge-pill {
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            padding: 3px 8px;
            border-radius: 12px;
            background: rgba(168, 85, 247, 0.2);
            color: #d8b4fe;
            border: 1px solid rgba(168, 85, 247, 0.4);
        }
        .format-card-title {
            font-size: 15px;
            font-weight: 700;
            color: #fff;
        }
        .format-card-desc {
            font-size: 12px;
            color: var(--text-secondary);
            line-height: 1.4;
        }

        /* AI Dropzone */
        .ai-dropzone {
            border: 2px dashed rgba(168, 85, 247, 0.5);
            border-radius: var(--card-radius);
            background: rgba(168, 85, 247, 0.04);
            padding: 36px 20px;
            text-align: center;
            cursor: pointer;
            transition: all 0.2s;
            position: relative;
        }
        .ai-dropzone:hover, .ai-dropzone.dragover {
            border-color: var(--accent-ai);
            background: rgba(168, 85, 247, 0.08);
            box-shadow: 0 0 25px rgba(168, 85, 247, 0.2);
        }
        .ai-dropzone input[type="file"] {
            position: absolute;
            top: 0; left: 0; width: 100%; height: 100%;
            opacity: 0;
            cursor: pointer;
        }
        .ai-icon {
            width: 52px;
            height: 52px;
            margin: 0 auto 12px;
            fill: #c084fc;
            filter: drop-shadow(0 0 8px rgba(168, 85, 247, 0.5));
        }

        .btn-ai-analyze {
            background: linear-gradient(135deg, #7928ca, #ff0080);
            color: white;
            border: none;
            border-radius: 8px;
            padding: 16px 24px;
            font-size: 16px;
            font-weight: 700;
            width: 100%;
            cursor: pointer;
            transition: transform 0.15s, box-shadow 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            box-shadow: 0 6px 20px rgba(121, 40, 202, 0.4);
            margin-top: 16px;
        }
        .btn-ai-analyze:hover {
            transform: translateY(-1px);
            box-shadow: 0 8px 25px rgba(121, 40, 202, 0.6);
        }
        .btn-ai-analyze:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }

        /* Stepper progress */
        .ai-steps-container {
            display: none;
            background: #171221;
            border: 1px solid rgba(168, 85, 247, 0.4);
            border-radius: var(--card-radius);
            padding: 22px;
            margin: 20px 0;
        }
        .ai-steps-list {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .ai-step-item {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 14px;
            color: var(--text-muted);
            transition: all 0.3s;
        }
        .ai-step-item.active {
            color: #e9d5ff;
            font-weight: 600;
        }
        .ai-step-item.completed {
            color: #2ba640;
        }
        .step-circle {
            width: 26px;
            height: 26px;
            border-radius: 50%;
            border: 2px solid var(--border-color);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: bold;
        }
        .ai-step-item.active .step-circle {
            border-color: var(--accent-ai);
            background: rgba(168, 85, 247, 0.2);
            color: #d8b4fe;
            box-shadow: 0 0 10px var(--accent-ai-glow);
        }
        .ai-step-item.completed .step-circle {
            border-color: #2ba640;
            background: rgba(43, 166, 64, 0.2);
            color: #2ba640;
        }

        /* Gemini Results Section */
        .gemini-results-box {
            display: none;
            margin-top: 24px;
            animation: fadeIn 0.4s ease;
        }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

        .result-group {
            margin-bottom: 22px;
        }
        .result-group-title {
            font-size: 14px;
            font-weight: 700;
            color: #d8b4fe;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        /* Title Selection Cards */
        .title-cards-grid {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .title-card {
            background: var(--bg-elevated);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 14px 16px;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 14px;
        }
        .title-card:hover {
            border-color: var(--accent-ai);
            background: #2f273b;
        }
        .title-card.selected {
            border-color: #a855f7;
            background: rgba(168, 85, 247, 0.15);
            box-shadow: 0 0 12px rgba(168, 85, 247, 0.25);
        }
        .title-card-text {
            font-size: 15px;
            font-weight: 500;
            color: white;
            line-height: 1.4;
        }
        .title-card-tag {
            font-size: 11px;
            background: #3c2a52;
            color: #d8b4fe;
            padding: 3px 8px;
            border-radius: 4px;
            white-space: nowrap;
            font-weight: 600;
        }
        .title-card.selected .title-card-tag {
            background: #7928ca;
            color: white;
        }

        /* 100% Authentic Character Face Thumbnail Gallery */
        .thumbnail-gallery-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 14px;
        }
        .thumb-candidate-card {
            position: relative;
            background: #141414;
            border: 2px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
            cursor: pointer;
            transition: all 0.2s;
            aspect-ratio: 16/9;
        }
        .thumb-candidate-card img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }
        .thumb-candidate-card:hover {
            border-color: var(--accent-ai);
            transform: scale(1.02);
            z-index: 2;
        }
        .thumb-candidate-card.selected {
            border-color: #2ba640;
            box-shadow: 0 0 16px rgba(43, 166, 64, 0.4);
        }
        .thumb-candidate-card.gemini-best {
            border-color: #ffba08;
            box-shadow: 0 0 16px rgba(255, 186, 8, 0.3);
        }
        .thumb-badge {
            position: absolute;
            bottom: 6px;
            left: 6px;
            background: rgba(0, 0, 0, 0.85);
            color: white;
            font-size: 10px;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: 600;
        }
        .thumb-highlight-badge {
            position: absolute;
            top: 6px;
            right: 6px;
            background: #2ba640;
            color: white;
            font-size: 10px;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: 700;
            display: none;
        }
        .thumb-candidate-card.selected .thumb-highlight-badge {
            display: block;
        }
        .thumb-ai-rec-badge {
            position: absolute;
            top: 6px;
            left: 6px;
            background: linear-gradient(135deg, #ffba08, #ff8c00);
            color: #000;
            font-size: 10px;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: 800;
            letter-spacing: 0.3px;
        }

        /* AI Action Buttons */
        .ai-actions-row {
            display: grid;
            grid-template-columns: 1fr 1.2fr;
            gap: 14px;
            margin-top: 24px;
        }
        .btn-populate {
            background: #282828;
            color: white;
            border: 1px solid #444;
            border-radius: 8px;
            padding: 16px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }
        .btn-populate:hover {
            background: #333;
            border-color: #666;
        }
        .btn-auto-publish {
            background: linear-gradient(135deg, #ff0000, #cc0000);
            color: white;
            border: none;
            border-radius: 8px;
            padding: 16px;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            box-shadow: 0 4px 14px rgba(255, 0, 0, 0.4);
        }
        .btn-auto-publish:hover {
            background: #ff1a1a;
            transform: translateY(-1px);
        }

        /* Standard Manual Studio Controls */
        .form-group {
            margin-bottom: 20px;
        }
        .form-label {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
            font-size: 13px;
            font-weight: 500;
            color: var(--text-secondary);
        }
        .char-counter {
            font-size: 11px;
            color: var(--text-muted);
        }
        .char-counter.limit-near { color: #f39c12; }
        .char-counter.limit-hit { color: var(--accent-red); font-weight: bold; }

        input[type="text"], textarea, select {
            width: 100%;
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 12px 14px;
            color: white;
            font-size: 14px;
            font-family: inherit;
            transition: border-color 0.2s;
        }
        input[type="text"]:focus, textarea:focus, select:focus {
            outline: none;
            border-color: var(--accent-blue);
        }
        textarea {
            height: 140px;
            resize: vertical;
            line-height: 1.5;
        }

        /* File Drop Zones */
        .file-dropzone {
            border: 2px dashed var(--border-color);
            border-radius: 8px;
            background: #151515;
            padding: 24px 16px;
            text-align: center;
            cursor: pointer;
            transition: all 0.2s;
            position: relative;
        }
        .file-dropzone:hover, .file-dropzone.dragover {
            border-color: var(--accent-red);
            background: #1c1c1c;
        }
        .file-dropzone input[type="file"] {
            position: absolute;
            top: 0; left: 0; width: 100%; height: 100%;
            opacity: 0;
            cursor: pointer;
        }
        .dropzone-icon {
            width: 44px;
            height: 44px;
            margin: 0 auto 10px;
            fill: var(--accent-red);
        }
        .dropzone-title {
            font-size: 15px;
            font-weight: 500;
            margin-bottom: 4px;
        }
        .dropzone-subtitle {
            font-size: 12px;
            color: var(--text-muted);
        }
        .selected-file-info {
            display: none;
            margin-top: 10px;
            font-size: 13px;
            color: var(--accent-blue);
            font-weight: 500;
        }

        /* Thumbnail Preview */
        .thumbnail-preview-box {
            width: 100%;
            height: 170px;
            border-radius: 6px;
            background: #121212;
            border: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            margin-top: 10px;
            position: relative;
        }
        .thumbnail-preview-box img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: none;
        }
        .thumbnail-placeholder {
            color: var(--text-muted);
            font-size: 13px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 6px;
        }

        /* Tag Chips */
        .tags-wrapper {
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 8px 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
            min-height: 46px;
        }
        .tag-chip {
            background: var(--bg-elevated);
            border: 1px solid #444;
            padding: 4px 10px;
            border-radius: 14px;
            font-size: 12px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .tag-chip .remove-tag {
            cursor: pointer;
            color: var(--text-muted);
            font-weight: bold;
        }
        .tag-chip .remove-tag:hover { color: var(--accent-red); }
        .tag-input-field {
            border: none !important;
            background: transparent !important;
            color: white !important;
            padding: 4px !important;
            font-size: 13px !important;
            flex: 1;
            min-width: 120px;
        }

        /* Audience switch */
        .toggle-box {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 14px 16px;
        }
        .toggle-label-title { font-size: 14px; font-weight: 500; }
        .toggle-label-desc { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

        .switch {
            position: relative;
            display: inline-block;
            width: 50px;
            height: 26px;
        }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background-color: #333;
            transition: .3s;
            border-radius: 26px;
        }
        .slider:before {
            position: absolute;
            content: "";
            height: 20px;
            width: 20px;
            left: 3px; bottom: 3px;
            background-color: white;
            transition: .3s;
            border-radius: 50%;
        }
        input:checked + .slider { background-color: var(--accent-red); }
        input:checked + .slider:before { transform: translateX(24px); }

        /* Action Buttons */
        .btn-upload {
            background: var(--accent-red);
            color: white;
            border: none;
            border-radius: 6px;
            padding: 16px;
            font-size: 16px;
            font-weight: 700;
            width: 100%;
            cursor: pointer;
            transition: background 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            box-shadow: 0 4px 14px rgba(255, 0, 0, 0.35);
        }
        .btn-upload:hover { background: var(--accent-red-hover); }
        .btn-upload:disabled { background: #444; cursor: not-allowed; box-shadow: none; }

        /* Progress Card */
        .progress-card {
            display: none;
            background: #181818;
            border: 1px solid var(--accent-red);
            border-radius: var(--card-radius);
            padding: 24px;
            margin-top: 20px;
        }
        .progress-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .progress-title { font-size: 16px; font-weight: 600; }
        .progress-percent { font-size: 16px; font-weight: 700; color: var(--accent-red); }
        .progress-bar-container {
            width: 100%;
            height: 12px;
            background: #2a2a2a;
            border-radius: 6px;
            overflow: hidden;
            margin-bottom: 10px;
        }
        .progress-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #ff0000, #ff6b6b);
            width: 0%;
            transition: width 0.3s ease;
        }
        .progress-status-text { font-size: 13px; color: var(--text-secondary); }

        /* Success Card */
        .success-card {
            display: none;
            background: #122315;
            border: 1px solid #2ba640;
            border-radius: var(--card-radius);
            padding: 24px;
            margin-top: 20px;
        }
        .success-actions {
            display: flex;
            gap: 12px;
            margin-top: 14px;
        }
        .btn-link {
            padding: 10px 18px;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 600;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .btn-link-primary { background: #2ba640; color: white; }
        .btn-link-secondary { background: #282828; color: white; border: 1px solid #444; }

        /* Recent Uploads */
        .recent-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
            gap: 16px;
            margin-top: 14px;
        }
        .video-item-card {
            background: var(--bg-elevated);
            border-radius: 8px;
            border: 1px solid var(--border-color);
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }
        .video-thumb-wrap {
            position: relative;
            width: 100%;
            aspect-ratio: 16/9;
            background: #000;
        }
        .video-thumb-wrap img { width: 100%; height: 100%; object-fit: cover; }
        .video-privacy-badge {
            position: absolute;
            bottom: 6px; right: 6px;
            background: rgba(0,0,0,0.8);
            font-size: 10px;
            padding: 2px 6px;
            border-radius: 4px;
            text-transform: uppercase;
        }
        .video-details { padding: 10px; display: flex; flex-direction: column; gap: 4px; flex: 1; }
        .video-item-title {
            font-size: 13px; font-weight: 500; line-height: 1.4;
            max-height: 2.8em; overflow: hidden; text-overflow: ellipsis;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
        }
        .video-meta { font-size: 11px; color: var(--text-muted); margin-top: auto; }

        /* Floating Gemini Chat Drawer */
        .chat-fab {
            position: fixed;
            bottom: 28px;
            right: 28px;
            background: linear-gradient(135deg, #7928ca, #ff0080);
            color: white;
            border: none;
            border-radius: 28px;
            padding: 12px 22px;
            font-size: 14px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 10px;
            cursor: pointer;
            box-shadow: 0 8px 25px rgba(121, 40, 202, 0.5);
            z-index: 1000;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .chat-fab:hover {
            transform: scale(1.04);
            box-shadow: 0 10px 30px rgba(121, 40, 202, 0.7);
        }

        .chat-drawer {
            position: fixed;
            top: 0;
            right: -440px;
            width: 420px;
            max-width: 100vw;
            height: 100vh;
            background: #161220;
            border-left: 1px solid rgba(168, 85, 247, 0.35);
            box-shadow: -10px 0 40px rgba(0,0,0,0.8);
            z-index: -100;
            display: none;
            flex-direction: column;
            transition: right 0.3s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.2s;
            pointer-events: none;
            opacity: 0;
            visibility: hidden;
        }
        .chat-drawer.open {
            display: flex !important;
            right: 0 !important;
            pointer-events: auto !important;
            opacity: 1 !important;
            visibility: visible !important;
            z-index: 1001 !important;
        }
        .chat-drawer:not(.open) {
            display: none !important;
            pointer-events: none !important;
            opacity: 0 !important;
            visibility: hidden !important;
            z-index: -100 !important;
        }
        .chat-header {
            padding: 18px 20px;
            border-bottom: 1px solid rgba(168, 85, 247, 0.2);
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #1c162b;
        }
        .chat-header-title {
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 16px;
            font-weight: 700;
            color: #f3e8ff;
        }
        .btn-close-chat {
            background: transparent;
            border: none;
            color: #aaa;
            font-size: 20px;
            cursor: pointer;
        }
        .btn-close-chat:hover { color: white; }

        .chat-body {
            flex: 1;
            overflow-y: auto;
            padding: 18px;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }
        .chat-msg {
            max-width: 85%;
            padding: 12px 14px;
            border-radius: 12px;
            font-size: 13px;
            line-height: 1.5;
        }
        .chat-msg-user {
            align-self: flex-end;
            background: #7928ca;
            color: white;
            border-bottom-right-radius: 2px;
        }
        .chat-msg-ai {
            align-self: flex-start;
            background: #251d38;
            color: #e9d5ff;
            border: 1px solid rgba(168, 85, 247, 0.3);
            border-bottom-left-radius: 2px;
        }
        .chat-apply-btn {
            display: inline-block;
            margin-top: 8px;
            background: rgba(168, 85, 247, 0.3);
            border: 1px solid var(--accent-ai);
            color: white;
            padding: 5px 10px;
            border-radius: 6px;
            font-size: 11px;
            cursor: pointer;
            font-weight: 600;
        }
        .chat-apply-btn:hover { background: var(--accent-ai); }

        .chat-suggestions {
            padding: 10px 18px;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            border-top: 1px solid rgba(168, 85, 247, 0.15);
            background: #14101e;
        }
        .chat-pill {
            background: #251d38;
            border: 1px solid rgba(168, 85, 247, 0.3);
            color: #d8b4fe;
            padding: 5px 10px;
            border-radius: 14px;
            font-size: 11px;
            cursor: pointer;
        }
        .chat-pill:hover { background: #382955; color: white; }

        .chat-footer {
            padding: 14px 18px;
            border-top: 1px solid rgba(168, 85, 247, 0.2);
            background: #1c162b;
            display: flex;
            gap: 8px;
        }
        .chat-input {
            flex: 1;
            background: #110e19;
            border: 1px solid rgba(168, 85, 247, 0.3);
            border-radius: 20px;
            padding: 10px 16px;
            color: white;
            font-size: 13px;
        }
        .chat-input:focus { outline: none; border-color: var(--accent-ai); }
        .chat-send-btn {
            background: var(--accent-ai);
            color: white;
            border: none;
            width: 38px;
            height: 38px;
            border-radius: 50%;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        /* Modal Overlay */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 0; height: 0;
            background: rgba(0,0,0,0.75);
            backdrop-filter: blur(4px);
            z-index: -99999;
            align-items: center;
            justify-content: center;
            pointer-events: none;
            opacity: 0;
            visibility: hidden;
        }
        .modal-overlay.active, .modal-overlay.open,
        .modal-overlay[style*="display: flex"],
        .modal-overlay[style*="display: block"],
        #clipperJobsModalOverlay.active,
        #clipperJobsModalOverlay.open,
        #clipperJobsModalOverlay[style*="display: flex"],
        #clipperJobsModalOverlay[style*="display: block"] {
            display: flex !important;
            width: 100vw !important;
            height: 100vh !important;
            pointer-events: auto !important;
            opacity: 1 !important;
            visibility: visible !important;
            z-index: 999999 !important;
        }
        .modal-overlay:not(.active):not(.open):not([style*="display: flex"]):not([style*="display: block"]),
        #geminiModalOverlay:not(.active):not(.open):not([style*="display: flex"]):not([style*="display: block"]),
        #clipperJobsModalOverlay:not(.active):not(.open):not([style*="display: flex"]):not([style*="display: block"]) {
            display: none !important;
            width: 0 !important;
            height: 0 !important;
            pointer-events: none !important;
            opacity: 0 !important;
            visibility: hidden !important;
            z-index: -99999 !important;
        }
        .modal-card {
            background: #1b1629;
            border: 1px solid rgba(168, 85, 247, 0.4);
            border-radius: var(--card-radius);
            max-width: 500px;
            width: 90%;
            padding: 28px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.8);
            pointer-events: auto;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }
        .modal-header h3 { margin: 0; color: #f3e8ff; font-size: 18px; display: flex; align-items: center; gap: 8px; }

        .spinner {
            border: 3px solid rgba(255,255,255,0.1);
            border-radius: 50%;
            border-top: 3px solid var(--accent-ai);
            width: 20px;
            height: 20px;
            animation: spin 1s linear infinite;
        }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }

        /* AI Movie-to-Shorts Auto-Clipper Styles */
        .clipper-card {
            background: #171321;
            border: 1px solid rgba(255, 0, 85, 0.3);
            border-radius: var(--card-radius);
            padding: 24px;
            margin-bottom: 24px;
        }
        .scene-item-card {
            background: #15111e;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 12px;
            overflow: hidden;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        .scene-item-card:hover {
            border-color: rgba(255, 0, 85, 0.4);
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
        }
        .scene-header {
            background: rgba(255, 255, 255, 0.03);
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            padding: 12px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .part-pill {
            background: linear-gradient(135deg, #ff0055, #9333ea);
            color: white;
            font-size: 11px;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .timestamp-pill {
            background: #231c30;
            color: #d8b4fe;
            font-size: 12px;
            font-weight: 600;
            padding: 4px 10px;
            border-radius: 6px;
            border: 1px solid rgba(168, 85, 247, 0.3);
        }
        .status-badge {
            font-size: 11px;
            font-weight: 600;
            padding: 4px 10px;
            border-radius: 6px;
        }
        .status-planned {
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-muted);
            border: 1px solid #444;
        }
        .status-ready {
            background: rgba(43, 166, 64, 0.2);
            color: #4ade80;
            border: 1px solid #2ba640;
        }
        .status-uploaded {
            background: rgba(59, 130, 246, 0.2);
            color: #60a5fa;
            border: 1px solid #3b82f6;
        }
        .scene-body {
            padding: 18px;
            display: grid;
            grid-template-columns: 200px 1fr;
            gap: 20px;
        }
        @media (max-width: 960px) {
            .main-container {
                grid-template-columns: 1fr;
                padding: 0 12px;
                margin: 12px auto;
                gap: 16px;
                width: 100%;
                max-width: 100%;
            }
            .sidebar {
                order: 2;
                width: 100%;
            }
            .workspace {
                order: 1;
                min-width: 0;
                width: 100%;
            }
            .mode-nav-tabs {
                position: relative;
                z-index: 9999;
                flex-wrap: wrap;
                gap: 8px;
            }
            .mode-tab {
                min-width: 130px;
                padding: 12px 14px;
                font-size: 13px;
                pointer-events: auto !important;
                z-index: 9999;
                touch-action: manipulation;
            }
            .top-navbar {
                padding: 0 14px;
            }
            .chat-drawer {
                width: 100vw;
                right: -100vw;
            }
            .chat-drawer.open {
                right: 0 !important;
            }
            .trimmer-explainer-grid {
                grid-template-columns: 1fr;
                gap: 12px;
            }
        }
        @media (max-width: 800px) {
            .scene-body { grid-template-columns: 1fr; }
        }
        .scene-video-box {
            width: 100%;
            aspect-ratio: 9/16;
            max-height: 350px;
            background: #0d0a14;
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid rgba(255, 255, 255, 0.06);
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
        }
        .scene-video-box video {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        .placeholder-916 {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 8px;
            color: var(--text-muted);
            text-align: center;
            padding: 16px;
        }
        .scene-meta-box {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .field-label {
            display: block;
            font-size: 12px;
            font-weight: 600;
            color: var(--text-secondary);
            margin-bottom: 4px;
        }
        .scene-actions-row {
            display: flex;
            gap: 10px;
            margin-top: auto;
            padding-top: 10px;
            flex-wrap: wrap;
        }
    </style>
</head>
<body>

    <!-- Top Navbar -->
    <header class="top-navbar">
        <div class="brand">
            <div class="brand-logo">
                <svg viewBox="0 0 24 24"><path d="M10 15l5.19-3L10 9v6m11.56-7.83c.13.47.22 1.1.28 1.9.07.8.1 1.49.1 2.09L22 12c0 2.19-.16 3.8-.44 4.83-.25.9-.83 1.48-1.73 1.73-.47.13-1.33.22-2.65.28-1.3.07-2.49.1-3.59.1L12 19c-4.19 0-6.8-.16-7.83-.44-.9-.25-1.48-.83-1.73-1.73-.13-.47-.22-1.1-.28-1.9-.07-.8-.1-1.49-.1-2.09L2 12c0-2.19.16-3.8.44-4.83.25-.9.83-1.48 1.73-1.73.47-.13 1.33-.22 2.65-.28 1.3-.07 2.49-.1 3.59-.1L12 5c4.19 0 6.8.16 7.83.44.9.25 1.48.83 1.73 1.73z"/></svg>
            </div>
            <span>Studio Pro</span>
            <span class="badge">V3</span>
            <span class="ai-badge">GEMINI 3.8 FLASH</span>
        </div>
        <div class="user-nav">
            <!-- Gemini API Status Pill -->
            <div class="gemini-status-pill" id="geminiNavPill" title="Configure Gemini API Key">
                <span class="status-dot" id="geminiDot"></span>
                <span id="geminiStatusLabel">Gemini AI</span>
            </div>

            <!-- YouTube Channel Pill with Dropdown Trigger -->
            <div class="account-nav-wrap">
                <div class="account-pill" id="userPill" title="Click to Switch Channel or Google Account">
                    <img id="userAvatar" src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='36' height='36' viewBox='0 0 36 36'><circle cx='18' cy='18' r='18' fill='%23383838'/><circle cx='18' cy='14' r='7' fill='%23aaaaaa'/><path d='M6 31 C 6 22, 30 22, 30 31' fill='%23aaaaaa'/></svg>" alt="Channel Avatar" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\'http://www.w3.org/2000/svg\' width=\'36\' height=\'36\' viewBox=\'0 0 36 36\'><circle cx=\'18\' cy=\'18\' r=\'18\' fill=\'%23383838\'/><circle cx=\'18\' cy=\'14\' r=\'7\' fill=\'%23aaaaaa\'/><path d=\'M6 31 C 6 22, 30 22, 30 31\' fill=\'%23aaaaaa\'/></svg>'">
                    <span id="userName">YouTube Creator</span>
                    <span style="font-size: 10px; color: var(--text-muted); margin-left: 2px;">▼</span>
                </div>

                <!-- Account / Multi-Channel Switcher Dropdown -->
                <div class="account-dropdown" id="accountDropdown">
                    <div class="dropdown-email-header">
                        <img id="dropAvatar" class="dropdown-email-avatar" src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='40' height='40' viewBox='0 0 40 40'><circle cx='20' cy='20' r='20' fill='%23383838'/><circle cx='20' cy='15' r='8' fill='%23aaaaaa'/><path d='M7 35 C 7 25, 33 25, 33 35' fill='%23aaaaaa'/></svg>" alt="Channel Avatar">
                        <div class="dropdown-email-info">
                            <span id="dropChannelName" class="dropdown-email-name">Channel</span>
                            <span id="dropUserEmail" class="dropdown-email-addr">account@gmail.com</span>
                        </div>
                    </div>
                    
                    <div class="dropdown-section-title">Your Channels</div>
                    <div class="dropdown-channels-list" id="dropdownChannelsList">
                        <!-- Populated dynamically via JS -->
                    </div>

                    <div class="dropdown-actions-menu">
                        <a href="/switch_account" class="dropdown-action-btn btn-switch-act">
                            <span>➕</span>
                            <span>Add / Switch Google Account</span>
                        </a>
                        <a href="/logout" class="dropdown-action-btn btn-logout-act">
                            <span>🚪</span>
                            <span>Sign Out / Disconnect</span>
                        </a>
                    </div>
                </div>
            </div>
            <a href="/logout" class="btn-logout">Disconnect</a>
        </div>
    </header>

    <!-- Main Container -->
    <main class="main-container">
        
        <!-- Left Sidebar: Channel Overview & Quota -->
        <aside class="sidebar">
            <div class="card channel-profile">
                <img id="channelAvatarLarge" class="channel-avatar" src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='90' height='90' viewBox='0 0 90 90'><circle cx='45' cy='45' r='45' fill='%23282828'/><circle cx='45' cy='34' r='18' fill='%23aaaaaa'/><path d='M15 76 C 15 54, 75 54, 75 76' fill='%23aaaaaa'/></svg>" alt="Channel Profile Picture" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\'http://www.w3.org/2000/svg\' width=\'90\' height=\'90\' viewBox=\'0 0 90 90\'><circle cx=\'45\' cy=\'45\' r=\'45\' fill=\'%23282828\'/><circle cx=\'45\' cy=\'34\' r=\'18\' fill=\'%23aaaaaa\'/><path d=\'M15 76 C 15 54, 75 54, 75 76\' fill=\'%23aaaaaa\'/></svg>'">
                <h3 id="channelTitle" class="channel-name">Channel</h3>
                <div id="channelHandle" class="channel-handle">@channel</div>
                
                <div class="stats-grid">
                    <div class="stat-box">
                        <div id="statSubscribers" class="stat-value">0</div>
                        <div class="stat-label">Subscribers</div>
                    </div>
                    <div class="stat-box">
                        <div id="statViews" class="stat-value">0</div>
                        <div class="stat-label">Total Views</div>
                    </div>
                    <div class="stat-box">
                        <div id="statVideos" class="stat-value">0</div>
                        <div class="stat-label">Videos</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value" style="color: #2ba640;">Active</div>
                        <div class="stat-label">API Status</div>
                    </div>
                </div>

                <button id="btnSwitchChannelSidebar" class="btn-sidebar-switch" title="Switch Channel or Google Account">
                    <span>🔄</span>
                    <span>Switch Channel / Account</span>
                </button>
            </div>

            <!-- Quota Tracking -->
            <div class="card quota-card">
                <h4>
                    <span>YouTube API Quota</span>
                    <span style="color: #2ba640;" id="quotaPercent">~16%</span>
                </h4>
                <div class="quota-bar-bg">
                    <div class="quota-bar-fill" id="quotaBar"></div>
                </div>
                <div class="quota-info">
                    <span>Est. 1,600 / 10,000 units</span>
                    <span>Resets 12:00 AM PT</span>
                </div>
                <p style="font-size: 11px; color: var(--text-muted); margin: 10px 0 0 0;">
                    Video upload costs ~1,600 quota units. Direct chunked resumable protocol is active.
                </p>
            </div>
        </aside>

        <!-- Right Main Workspace -->
        <section class="workspace">
            
            <!-- Mode Switcher Tabs (Original Two Core Pillars) -->
            <div class="mode-nav-tabs">
                <button type="button" class="mode-tab active-ai" id="tabGeminiMode" onclick="switchWorkspaceTab('gemini')">
                    <span>✨</span>
                    <span>Gemini AI Studio Copilot</span>
                    <span class="tab-badge badge-ai">PRO MULTIMODAL</span>
                </button>
                <button type="button" class="mode-tab" id="tabManualMode" onclick="switchWorkspaceTab('manual')">
                    <span>🛠️</span>
                    <span>Standard Manual Studio</span>
                    <span class="tab-badge badge-manual">CLASSIC</span>
                </button>
            </div>

            <!-- ============================================== -->
            <!-- 1. GEMINI AI STUDIO COPILOT PANEL              -->
            <!-- ============================================== -->
            <div class="card" id="geminiStudioSection" style="display: block;">
                <div class="ai-banner">
                    <div class="ai-banner-left">
                        <div class="ai-banner-title">
                            <span>✨ Gemini Multimodal Video Analysis &amp; Dynamic AI Thumbnail Suite</span>
                        </div>
                        <div class="ai-banner-desc">
                            Upload any Short or Long-form video. Gemini deeply analyzes spoken dialogue, ground-truth plot, and expressions — generating a custom Slot 1 AI Dynamic Thumbnail (9:16 for Shorts, 16:9 for Long-form) plus 5 High-Emotion Local Video Frames (Slots 2–6).
                        </div>
                    </div>
                    <button class="btn-populate" id="btnOpenKeyModal" style="padding: 8px 14px; font-size: 13px;">
                        ⚙️ Configure API Key
                    </button>
                </div>

                <!-- Video Format Selector (Shorts vs Long Form) -->
                <div class="form-group" style="margin-bottom: 16px;">
                    <div class="form-label" style="margin-bottom: 8px;">
                        <span style="font-size: 14px; font-weight: 700; color: #f3e8ff;">🎯 Video Target Format &amp; Aspect Ratio Engine:</span>
                        <span style="font-size: 11px; color: #c084fc;">Auto-configures algorithm rules &amp; 9:16 vs 16:9 thumbnail rendering</span>
                    </div>
                    <div class="format-selector-grid">
                        <div class="format-card active" id="formatCardShort" onclick="selectVideoFormat('Short')">
                            <div class="format-card-header">
                                <span class="format-icon">📱</span>
                                <span class="format-badge-pill">9:16 Vertical Ratio</span>
                            </div>
                            <div class="format-card-title">YouTube Shorts / Reels</div>
                            <div class="format-card-desc">Strict 9:16 Thumbnail &bull; Curiosity hook &lt; 50 chars &bull; 2 viral hashtags &bull; 8-12 search tags</div>
                        </div>
                        <div class="format-card" id="formatCardLong" onclick="selectVideoFormat('Long')">
                            <div class="format-card-header">
                                <span class="format-icon">🎬</span>
                                <span class="format-badge-pill" style="background: rgba(56, 189, 248, 0.2); color: #38bdf8; border-color: rgba(56, 189, 248, 0.4);">16:9 Cinematic Ratio</span>
                            </div>
                            <div class="format-card-title">Long Form Video</div>
                            <div class="format-card-desc">Strict 16:9 Thumbnail &bull; [Hook] | [High Volume Keyword] &bull; 3-paragraph summary &bull; 15-20 search tags</div>
                        </div>
                    </div>
                </div>

                <!-- Video Dropzone for Gemini -->
                <div class="form-group">
                    <div class="ai-dropzone" id="aiVideoDropzone">
                        <input type="file" id="aiVideoFileInput" accept="video/mp4,video/x-matroska,video/quicktime,video/webm">
                        <svg class="ai-icon" viewBox="0 0 24 24"><path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z"/></svg>
                        <div style="font-size: 18px; font-weight: 700; color: #f3e8ff; margin-bottom: 6px;">
                            Drag &amp; Drop Video to Ingest with Gemini AI
                        </div>
                        <div style="font-size: 13px; color: #c084fc;">
                            Supports MP4, MKV, WebM, MOV &bull; Seconds to Hours &bull; Shorts (9:16) &amp; Long-Form (16:9)
                        </div>
                        <div class="selected-file-info" id="aiVideoFileInfo" style="color: #e9d5ff; font-weight: 600;"></div>
                    </div>
                </div>

                <!-- Optional Creator Instructions -->
                <div class="form-group" style="margin-top: 14px;">
                    <div class="form-label">
                        <span>Creative Direction / Specific Instructions (Optional)</span>
                        <span style="font-size: 11px; color: var(--text-muted);">e.g. "Focus on tech comedy", "Urdu/Hindi audience tone"</span>
                    </div>
                    <input type="text" id="aiCustomPrompt" placeholder="Add specific guidance or leave blank for automatic viral optimization...">
                </div>

                <!-- Run AI Button -->
                <button class="btn-ai-analyze" id="btnRunAiAnalysis">
                    <svg style="width: 20px; height: 20px; fill: white;" viewBox="0 0 24 24"><path d="M12 2l2.4 7.4h7.6l-6.2 4.5 2.4 7.4-6.2-4.5-6.2 4.5 2.4-7.4-6.2-4.5h7.6z"/></svg>
                    <span>Analyze Video &amp; Generate AI Thumbnail + Metadata</span>
                </button>

                <!-- Dynamic Stepper Progress -->
                <div class="ai-steps-container" id="aiStepsContainer">
                    <h4 style="margin: 0 0 14px 0; color: #f3e8ff; font-size: 15px; display: flex; align-items: center; gap: 8px;">
                        <span class="spinner" style="width: 16px; height: 16px;"></span>
                        Gemini Multimodal Processing Pipeline
                    </h4>
                    <div class="ai-steps-list">
                        <div class="ai-step-item" id="step1">
                            <span class="step-circle">1</span>
                            <span>Extracting 5 High-Emotion Local Video Keyframes (Slots 2 to 6)</span>
                        </div>
                        <div class="ai-step-item" id="step2">
                            <span class="step-circle">2</span>
                            <span>Uploading Video Stream to Gemini Multimodal Engine</span>
                        </div>
                        <div class="ai-step-item" id="step3">
                            <span class="step-circle">3</span>
                            <span>Analyzing Ground-Truth Plot, Facial Expressions &amp; Climactic Context</span>
                        </div>
                        <div class="ai-step-item" id="step4">
                            <span class="step-circle">4</span>
                            <span>Generating Slot 1 Dynamic AI Thumbnail (9:16 / 16:9) &amp; Search-Grounded Titles</span>
                        </div>
                        <div class="ai-step-item" id="step5">
                            <span class="step-circle">5</span>
                            <span>Auto-Selecting Slot 1 AI Thumbnail &amp; Finalizing Metadata</span>
                        </div>
                    </div>
                </div>

                <!-- ============================================== -->
                <!-- GEMINI GENERATED RESULTS DISPLAY               -->
                <!-- ============================================== -->
                <div class="gemini-results-box" id="geminiResultsBox">

                    <!-- AI Mood, Format & Search Grounding Intelligence Card -->
                    <div style="background: rgba(168, 85, 247, 0.08); border: 1px solid rgba(168, 85, 247, 0.35); border-radius: 12px; padding: 16px 20px; margin-bottom: 22px;">
                        <div style="display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 14px;">
                            <div style="display: flex; align-items: center; gap: 12px;">
                                <span style="font-size: 24px;">🎯</span>
                                <div>
                                    <div style="font-size: 11px; text-transform: uppercase; color: #c084fc; font-weight: 700; letter-spacing: 0.5px;">Target Format &amp; Ratio</div>
                                    <div id="aiTargetFormatBadge" style="font-size: 15px; font-weight: 800; color: #fff;">YouTube Shorts (9:16)</div>
                                </div>
                            </div>
                            <div style="display: flex; align-items: center; gap: 12px;">
                                <span style="font-size: 24px;">👤</span>
                                <div>
                                    <div style="font-size: 11px; text-transform: uppercase; color: #38bdf8; font-weight: 700; letter-spacing: 0.5px;">Primary Context / Speaker</div>
                                    <div id="aiPrimaryContext" style="font-size: 15px; font-weight: 800; color: #fff;">Autonomous Evaluation</div>
                                </div>
                            </div>
                            <div style="display: flex; align-items: center; gap: 12px;">
                                <span style="font-size: 24px;">🔍</span>
                                <div>
                                    <div style="font-size: 11px; text-transform: uppercase; color: #4ade80; font-weight: 700; letter-spacing: 0.5px;">Algorithm Grounding</div>
                                    <div style="font-size: 14px; font-weight: 700; color: #4ade80;">Google Search Active</div>
                                </div>
                            </div>
                            <div style="display: flex; align-items: center; gap: 12px;">
                                <span style="font-size: 24px;">⚡</span>
                                <div>
                                    <div style="font-size: 11px; text-transform: uppercase; color: #ffba08; font-weight: 700; letter-spacing: 0.5px;">Engine Model</div>
                                    <div style="font-size: 14px; font-weight: 700; color: #fff;">Gemini 2.5 Flash + Imagen</div>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Title Variations -->
                    <div class="result-group">
                        <div class="result-group-title">
                            <span>Viral Title Recommendations (Click card to select)</span>
                            <span style="font-size: 12px; color: #a855f7;">Ranked by Estimated CTR &amp; Punchline</span>
                        </div>
                        <div class="title-cards-grid" id="titleCardsGrid"></div>
                    </div>

                    <!-- Enhanced Thumbnail Generation Suite (Slot 1 AI Dynamic + Slots 2-6 High-Emotion Local Frames) -->
                    <div class="result-group">
                        <div class="result-group-title" style="flex-wrap: wrap; gap: 8px;">
                            <span>✨ Enhanced Thumbnail Suite: Slot 1 AI Dynamic (Default Selected) + Slots 2–6 High-Emotion Local Frames</span>
                            <span id="aiThumbAspectRatioBadge" style="font-size: 11.5px; background: rgba(16, 185, 129, 0.2); color: #4ade80; border: 1px solid rgba(16, 185, 129, 0.45); padding: 3px 10px; border-radius: 6px; font-weight: 800;">✔ 9:16 Vertical Auto-Detected</span>
                        </div>
                        <p style="font-size: 12px; color: var(--text-secondary); margin: 0 0 12px 0;">
                            <strong>Slot 1 (Default Selected)</strong> is dynamically generated by Gemini AI with dramatic lighting, high contrast, and suspenseful expression tailored to this specific video. <strong>Slots 2 to 6</strong> are 5 native high-emotion keyframes extracted directly from your video stream.
                        </p>
                        <div class="thumbnail-gallery-grid" id="thumbnailGalleryGrid"></div>

                        <!-- Thumbnail Directive Card -->
                        <div style="background: #171624; border: 1px solid rgba(255, 186, 8, 0.35); border-radius: 10px; padding: 16px; margin-top: 14px;">
                            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px;">
                                <span style="font-size: 13px; font-weight: 800; color: #ffba08; display: flex; align-items: center; gap: 8px;">
                                    <span>🎨</span> Thumbnail Directive &amp; Art Direction
                                </span>
                                <span style="font-size: 11px; color: var(--text-muted);">High CTR visual composition</span>
                            </div>
                            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px;">
                                <div style="background: rgba(0,0,0,0.4); padding: 12px; border-radius: 8px; border-left: 3px solid #ffba08;">
                                    <div style="font-size: 11px; color: #ffba08; font-weight: 700; text-transform: uppercase;">Text Overlay (3-4 Words)</div>
                                    <div id="aiThumbOverlayText" style="font-size: 15px; font-weight: 800; color: #fff; margin-top: 4px; letter-spacing: 0.5px;">-</div>
                                </div>
                                <div style="background: rgba(0,0,0,0.4); padding: 12px; border-radius: 8px; border-left: 3px solid #38bdf8;">
                                    <div style="font-size: 11px; color: #38bdf8; font-weight: 700; text-transform: uppercase;">Visual Scene Direction</div>
                                    <div id="aiThumbSceneDir" style="font-size: 12px; color: #e2e8f0; margin-top: 4px; line-height: 1.4;">-</div>
                                </div>
                                <div style="background: rgba(0,0,0,0.4); padding: 12px; border-radius: 8px; border-left: 3px solid #ec4899;">
                                    <div style="font-size: 11px; color: #ec4899; font-weight: 700; text-transform: uppercase;">Recommended Color Theme</div>
                                    <div id="aiThumbColorTheme" style="font-size: 12px; color: #e2e8f0; margin-top: 4px; line-height: 1.4;">-</div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- SEO Description -->
                    <div class="result-group">
                        <div class="result-group-title">
                            <span>Optimized SEO Description (Hooks, Chapters, Hashtags)</span>
                            <span style="font-size: 11px; color: var(--text-muted);">Formatted for YouTube algorithm</span>
                        </div>
                        <textarea id="aiGeneratedDesc" style="height: 160px; font-size: 13px;"></textarea>
                    </div>

                    <!-- Niche Targeted Hashtags -->
                    <div class="result-group">
                        <div class="result-group-title">
                            <span>Niche-Specific Hashtags (5-8 Hyper-Targeted)</span>
                            <span id="aiHashtagCount" style="font-size: 12px; color: #c084fc;">Niche targeted</span>
                        </div>
                        <div class="tags-wrapper" id="aiHashtagsDisplay"></div>
                    </div>

                    <!-- Tags Cloud -->
                    <div class="result-group">
                        <div class="result-group-title">
                            <span>Targeted Search &amp; Discovery Keywords</span>
                            <span id="aiTagCount" style="font-size: 12px; color: var(--text-muted);">SEO tags</span>
                        </div>
                        <div class="tags-wrapper" id="aiTagsDisplay"></div>
                    </div>

                    <!-- Category & Insights -->
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 20px;">
                        <div class="stat-box" style="text-align: left; padding: 14px;">
                            <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Recommended Category</div>
                            <div id="aiCategoryName" style="font-size: 16px; font-weight: 700; color: white; margin-top: 4px;">People &amp; Blogs</div>
                            <div id="aiCategoryId" style="font-size: 11px; color: #a855f7; margin-top: 2px;">ID: 22</div>
                        </div>
                        <div class="stat-box" style="text-align: left; padding: 14px;">
                            <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Audience &amp; Format</div>
                            <div id="aiVideoTypeBadge" style="font-size: 16px; font-weight: 700; color: white; margin-top: 4px;">Long-form Video</div>
                            <div id="aiKidsBadge" style="font-size: 11px; color: #2ba640; margin-top: 2px;">General Audience (Not for kids)</div>
                        </div>
                    </div>

                    <!-- Gemini Strategic Summary -->
                    <div class="card" style="background: #171124; border: 1px solid rgba(168, 85, 247, 0.3); padding: 16px; margin-bottom: 20px;">
                        <div style="font-size: 13px; font-weight: 700; color: #d8b4fe; margin-bottom: 4px;">🧠 Gemini Strategic Insights:</div>
                        <div id="aiSummaryInsights" style="font-size: 13px; color: #e9d5ff; line-height: 1.5;"></div>
                    </div>

                    <!-- Dual Actions -->
                    <div class="ai-actions-row">
                        <button class="btn-populate" id="btnPopulateToManual">
                            <span>📝 Auto-Populate Studio Form</span>
                        </button>
                        <button class="btn-auto-publish" id="btnOneClickPublish">
                            <svg style="width: 20px; height: 20px; fill: white;" viewBox="0 0 24 24"><path d="M9 16h6v-6h4l-7-7-7 7h4zm-4 2h14v2H5z"/></svg>
                            <span>One-Click Auto-Publish to YouTube</span>
                        </button>
                    </div>

                </div>

            </div>

            <!-- ============================================== -->
            <!-- 2. STANDARD MANUAL STUDIO FORM PANEL           -->
            <!-- ============================================== -->
            <div class="card" id="manualStudioSection" style="display: none;">
                <div class="workspace-header">
                    <h2>
                        <svg style="width: 24px; height: 24px; fill: var(--accent-red);" viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm4 18H6V4h7v5h5v11zM8 15.01l1.41 1.41L11 14.84V19h2v-4.16l1.59 1.59L16 15.01 12.01 11 8 15.01z"/></svg>
                        Manual Studio Upload Details
                    </h2>
                    <span style="font-size: 12px; color: var(--text-muted);">Direct YouTube Data API v3 Engine</span>
                </div>

                <form id="uploadForm">
                    <input type="hidden" id="existingVideoFilename" name="existing_video_filename">
                    <input type="hidden" id="selectedThumbnailFilename" name="selected_thumbnail_filename">

                    <div style="display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 22px;">
                        
                        <!-- Left Column: Metadata -->
                        <div class="col-meta">
                            <!-- Title Input -->
                            <div class="form-group">
                                <div class="form-label">
                                    <span>Video Title <strong style="color: var(--accent-red);">*</strong></span>
                                    <span class="char-counter" id="titleCounter">0 / 100</span>
                                </div>
                                <input type="text" id="videoTitle" name="title" placeholder="Add a title that describes your video" maxlength="100" required>
                            </div>

                            <!-- Description Input -->
                            <div class="form-group">
                                <div class="form-label">
                                    <span>Description</span>
                                    <span class="char-counter" id="descCounter">0 / 5000</span>
                                </div>
                                <textarea id="videoDesc" name="description" placeholder="Tell viewers about your video, add links, chapters, and hashtags..."></textarea>
                            </div>

                            <!-- Tags Input -->
                            <div class="form-group">
                                <div class="form-label">
                                    <span>Tags (Press Enter or Comma)</span>
                                    <span style="font-size: 11px; color: var(--text-muted);">Max 500 chars</span>
                                </div>
                                <div class="tags-wrapper" id="tagsWrapper">
                                    <input type="text" id="tagInputField" class="tag-input-field" placeholder="Add tags...">
                                </div>
                                <input type="hidden" name="tags" id="hiddenTags">
                            </div>

                            <!-- Audience / Made for Kids -->
                            <div class="form-group">
                                <div class="toggle-box">
                                    <div>
                                        <div class="toggle-label-title">Audience: Made for Kids</div>
                                        <div class="toggle-label-desc">Required by Children's Online Privacy Protection Act (COPPA)</div>
                                    </div>
                                    <label class="switch">
                                        <input type="checkbox" id="madeForKids" name="made_for_kids">
                                        <span class="slider"></span>
                                    </label>
                                </div>
                            </div>
                        </div>

                        <!-- Right Column: Files & Settings -->
                        <div class="col-files">
                            <!-- Video Dropzone -->
                            <div class="form-group">
                                <div class="form-label">
                                    <span>Select Video File <strong style="color: var(--accent-red);">*</strong></span>
                                    <span style="font-size: 11px; color: var(--text-muted);">MP4, MKV, MOV, WebM</span>
                                </div>
                                <div class="file-dropzone" id="videoDropzone">
                                    <input type="file" id="videoFileInput" name="video_file" accept="video/mp4,video/x-matroska,video/quicktime,video/webm">
                                    <svg class="dropzone-icon" viewBox="0 0 24 24"><path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM14 13v4h-4v-4H7l5-5 5 5h-3z"/></svg>
                                    <div class="dropzone-title">Click or drag video here</div>
                                    <div class="dropzone-subtitle">High Definition & 4K Supported</div>
                                    <div class="selected-file-info" id="videoFileInfo"></div>
                                </div>
                            </div>

                            <!-- Thumbnail Dropzone & Preview -->
                            <div class="form-group">
                                <div class="form-label">
                                    <span>Custom Thumbnail (Optional)</span>
                                    <span style="font-size: 11px; color: var(--text-muted);">JPG, PNG (1280x720)</span>
                                </div>
                                <div class="file-dropzone" style="padding: 12px;" id="thumbDropzone">
                                    <input type="file" id="thumbFileInput" name="thumbnail_file" accept="image/jpeg,image/png,image/webp">
                                    <span style="font-size: 13px;">Choose image file or select from Gemini</span>
                                </div>
                                <div class="thumbnail-preview-box" id="thumbPreviewBox">
                                    <div class="thumbnail-placeholder" id="thumbPlaceholder">
                                        <svg style="width: 28px; height: 28px; fill: #555;" viewBox="0 0 24 24"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>
                                        <span>Real-time Thumbnail Preview</span>
                                    </div>
                                    <img id="thumbPreviewImg" alt="Thumbnail Preview">
                                </div>
                            </div>

                            <!-- Privacy & Category Selector -->
                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;" class="form-group">
                                <div>
                                    <div class="form-label">Visibility</div>
                                    <select name="privacy" id="privacySelect">
                                        <option value="private">Private</option>
                                        <option value="unlisted">Unlisted</option>
                                        <option value="public" selected>Public</option>
                                    </select>
                                </div>
                                <div>
                                    <div class="form-label">Category</div>
                                    <select name="category_id" id="categorySelect">
                                        <option value="20">Gaming</option>
                                        <option value="22" selected>People & Blogs</option>
                                        <option value="28">Science & Tech</option>
                                        <option value="27">Education</option>
                                        <option value="24">Entertainment</option>
                                        <option value="23">Comedy</option>
                                        <option value="10">Music</option>
                                        <option value="17">Sports</option>
                                        <option value="1">Film & Animation</option>
                                        <option value="2">Autos & Vehicles</option>
                                        <option value="26">Howto & Style</option>
                                        <option value="25">News & Politics</option>
                                    </select>
                                </div>
                            </div>

                        </div>
                    </div>

                    <!-- Submit Button -->
                    <div style="margin-top: 10px;">
                        <button type="submit" class="btn-upload" id="submitBtn">
                            <svg style="width: 20px; height: 20px; fill: white;" viewBox="0 0 24 24"><path d="M9 16h6v-6h4l-7-7-7 7h4zm-4 2h14v2H5z"/></svg>
                            <span>Publish to YouTube Channel</span>
                        </button>
                    </div>
                </form>
            </div>

            <!-- Real-time Resumable Progress Card (Shared) -->
            <div class="progress-card" id="progressCard">
                <div class="progress-header">
                    <span class="progress-title" id="progressTitle">Uploading to Server...</span>
                    <span class="progress-percent" id="progressPercent">0%</span>
                </div>
                <div class="progress-bar-container">
                    <div class="progress-bar-fill" id="progressBarFill"></div>
                </div>
                <div class="progress-status-text" id="progressStatusText">
                    Preparing chunked resumable upload stream...
                </div>
            </div>

            <!-- Success Box (Shared) -->
            <div class="success-card" id="successCard">
                <h3 style="margin: 0 0 8px 0; color: #2ba640; display: flex; align-items: center; gap: 8px;">
                    <svg style="width: 24px; height: 24px; fill: #2ba640;" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
                    Video Successfully Uploaded!
                </h3>
                <p style="margin: 0; color: #ccc;" id="successMsg">Your video is now live on your official YouTube channel.</p>
                <div class="success-actions">
                    <a href="#" target="_blank" id="watchBtn" class="btn-link btn-link-primary">Watch on YouTube</a>
                    <a href="#" target="_blank" id="studioBtn" class="btn-link btn-link-secondary">Open in YouTube Studio</a>
                </div>
            </div>

            <!-- Recent Channel Uploads -->
            <div class="card recent-section">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <h3 style="margin: 0;">Recent Channel Uploads</h3>
                    <button id="refreshVideosBtn" style="background: none; border: 1px solid var(--border-color); color: var(--text-secondary); padding: 5px 12px; border-radius: 4px; cursor: pointer;">Refresh List</button>
                </div>
                <div class="recent-grid" id="recentVideosGrid">
                    <div style="color: var(--text-muted); font-size: 13px;">Loading recent videos...</div>
                </div>
            </div>

        </section>
    </main>

    <!-- ============================================== -->
    <!-- FLOATING GEMINI CHAT DRAWER                    -->
    <!-- ============================================== -->
    <button class="chat-fab" id="chatFabBtn">
        <span>✨</span>
        <span>Gemini AI Assistant</span>
    </button>

    <div class="chat-drawer" id="chatDrawer" style="display: none; pointer-events: none; opacity: 0; visibility: hidden; z-index: -100;">
        <div class="chat-header">
            <div class="chat-header-title">
                <span>✨</span>
                <span>Gemini Creator Copilot</span>
                <span class="tab-badge badge-ai" style="font-size: 9px;">3.8 FLASH</span>
            </div>
            <button class="btn-close-chat" id="btnCloseChat">&times;</button>
        </div>
        <div class="chat-body" id="chatMessages">
            <div class="chat-msg chat-msg-ai">
                Hi! I'm your Gemini Creator Copilot. Upload a video for full automated analysis, or ask me anytime to rewrite titles, translate descriptions, refine SEO hooks, or suggest tags!
            </div>
        </div>
        <div class="chat-suggestions">
            <div class="chat-pill" onclick="sendQuickPrompt('Generate 5 shorter viral title variations for this video')">🔥 Shorter Titles</div>
            <div class="chat-pill" onclick="sendQuickPrompt('Translate the current video title and description into Urdu / Hindi')">🌐 Urdu/Hindi Translation</div>
            <div class="chat-pill" onclick="sendQuickPrompt('Suggest 5 engaging questions to ask viewers in the comments')">💬 Comments Hooks</div>
            <div class="chat-pill" onclick="sendQuickPrompt('Give 10 more high-volume trending tags for this topic')">🏷️ Viral Tags</div>
        </div>
        <div class="chat-footer">
            <input type="text" id="chatInput" class="chat-input" placeholder="Ask Gemini anything about your video...">
            <button class="chat-send-btn" id="btnSendChat">
                <svg style="width: 18px; height: 18px; fill: white;" viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
            </button>
        </div>
    </div>

    <!-- ============================================== -->
    <!-- ============================================== -->
    <!-- GEMINI API CONFIG MODAL (10-KEY POOL)          -->
    <!-- ============================================== -->
    <div class="modal-overlay" id="geminiModalOverlay" style="display: none; pointer-events: none; opacity: 0; visibility: hidden; z-index: -100;">
        <div class="modal-card" style="max-width: 640px; width: 95%; max-height: 90vh; overflow-y: auto; padding: 24px;">
            <div class="modal-header" style="margin-bottom: 12px;">
                <h3>
                    <span>⚙️</span>
                    <span>Gemini 10-Key Pool &amp; Auto-Rotation</span>
                </h3>
                <button class="btn-close-chat" id="btnCloseKeyModal">&times;</button>
            </div>
            
            <div style="background: rgba(168, 85, 247, 0.12); border: 1px solid rgba(168, 85, 247, 0.35); border-radius: 8px; padding: 12px 14px; margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; flex-wrap: wrap; gap: 6px;">
                    <div style="font-size: 13px; font-weight: 600; color: #f3e8ff;">
                        Channel: <span id="modalActiveChannelTitle" style="color: #38bdf8;">YouTube Creator</span>
                    </div>
                    <span id="poolCapacityBadge" style="font-size: 11px; background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.4); border-radius: 12px; padding: 2px 8px; font-weight: 600;">
                        0 / 10 Keys Active
                    </span>
                </div>
                <div style="font-size: 11.5px; color: #cbd5e1; line-height: 1.45;">
                    🛡️ <strong>Persistent Database Storage:</strong> Keys are bound directly to this Channel ID in the database and persistent backups. Keys survive code updates, server restarts, and redeployments. <em>Only manual delete removes a key.</em>
                </div>
            </div>

            <!-- 10 Slots Grid -->
            <div style="margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <span style="font-size: 12px; font-weight: 700; color: #e2e8f0; text-transform: uppercase; letter-spacing: 0.5px;">Active 10-Key Pool (Round-Robin &bull; Auto-Failover on 429)</span>
                    <button type="button" id="btnRefreshKeyPool" style="background: transparent; border: none; color: #a78bfa; font-size: 11px; cursor: pointer; text-decoration: underline;">🔄 Refresh</button>
                </div>
                <div id="keySlotsList" style="display: flex; flex-direction: column; gap: 6px; max-height: 260px; overflow-y: auto; padding-right: 2px;">
                    <div style="text-align: center; color: #94a3b8; font-size: 12px; padding: 20px;">Loading key pool...</div>
                </div>
            </div>

            <!-- Add / Replace Key Box -->
            <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; padding: 14px; margin-bottom: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <span style="font-size: 12.5px; font-weight: 600; color: #f3e8ff;">Add Key to Pool</span>
                    <a href="https://aistudio.google.com/app/apikey" target="_blank" style="color: #38bdf8; font-size: 11px; text-decoration: none;">Get Free Key &#8599;</a>
                </div>
                <div style="display: flex; gap: 8px; margin-bottom: 8px; flex-wrap: wrap;">
                    <input type="text" id="geminiApiKeyInput" placeholder="Enter Gemini API key (AIzaSy...)" style="flex: 1; min-width: 200px; margin-bottom: 0;">
                    <button class="btn-ai-analyze" id="btnSaveGeminiKey" style="padding: 10px 16px; white-space: nowrap; font-size: 13px;">
                        <span>+ Add to Pool</span>
                    </button>
                </div>
                <div style="display: flex; gap: 10px; align-items: center;">
                    <div style="font-size: 11px; color: #94a3b8;">Active Model:</div>
                    <select id="geminiModelSelect" style="flex: 1; padding: 6px 10px; font-size: 12px; margin-bottom: 0;">
                        <option value="gemini-3.8-flash" selected>gemini-3.8-flash (Recommended &bull; Fast Multimodal)</option>
                        <option value="gemini-3.5-flash-lite">gemini-3.5-flash-lite (Ultra-fast)</option>
                    </select>
                </div>
            </div>

            <div id="geminiKeyStatusMsg" style="font-size: 12.5px; margin: 8px 0; display: none; padding: 8px 12px; border-radius: 6px;"></div>
        </div>
    </div>

    <!-- Hidden Canvas & Video for Instant Client Frame Capture -->
    <video id="clientVideoDecoder" style="display: none;" preload="metadata" muted></video>
    <canvas id="clientFrameCanvas" style="display: none;"></canvas>

    <script>
        // Global state
        let currentGeminiData = null;
        let selectedThumbnailUrl = null;
        let selectedThumbnailFilename = null;
        let selectedVideoFile = null;
        let tempVideoServerFilename = null;
        let clientExtractedFrames = [];

        // Registry of detached modal elements so they can be hard-removed from root DOM when closed
        window._detachedModals = window._detachedModals || {};

        // Force-strip and hard-remove any invisible blocking backdrops/overlays from root DOM
        window.forceClearBlockingOverlays = function() {
            try {
                document.querySelectorAll('.modal-overlay, #geminiModalOverlay, #clipperJobsModalOverlay, #chatDrawerBackdrop, #chatDrawer').forEach(el => {
                    if (el.id) {
                        window._detachedModals[el.id] = el;
                    }
                    if (!el.classList.contains('active') && !el.classList.contains('open')) {
                        el.style.setProperty('display', 'none', 'important');
                        el.style.setProperty('pointer-events', 'none', 'important');
                        el.style.setProperty('opacity', '0', 'important');
                        el.style.setProperty('visibility', 'hidden', 'important');
                        el.style.setProperty('z-index', '-99999', 'important');
                        el.style.setProperty('width', '0', 'important');
                        el.style.setProperty('height', '0', 'important');
                        if (el.parentNode) {
                            el.parentNode.removeChild(el);
                        }
                    }
                });
                document.querySelectorAll('.mode-tab, button, input, select, textarea, .form-control, .btn-upload, .btn-populate').forEach(el => {
                    el.style.setProperty('pointer-events', 'auto', 'important');
                    el.style.setProperty('touch-action', 'manipulation');
                    if (!el.style.position) el.style.position = 'relative';
                    if (!el.style.zIndex) el.style.zIndex = '9999';
                });
            } catch (e) {}
        };

        // Tab Switching Engine (Globally accessible on window)
        window.switchWorkspaceTab = function(activeTab) {
            try {
                window.forceClearBlockingOverlays();
                const targetKey = (activeTab === 'manual') ? 'manual' : 'gemini';
                const tabs = {
                    'gemini': { tab: document.getElementById('tabGeminiMode'), sec: document.getElementById('geminiStudioSection'), activeCls: 'active-ai' },
                    'manual': { tab: document.getElementById('tabManualMode'), sec: document.getElementById('manualStudioSection'), activeCls: 'active-manual' }
                };

                Object.keys(tabs).forEach(key => {
                    const item = tabs[key];
                    if (item.tab) {
                        item.tab.className = 'mode-tab' + (targetKey === key ? (' ' + item.activeCls) : '');
                        item.tab.style.pointerEvents = 'auto';
                        item.tab.style.cursor = 'pointer';
                        item.tab.style.zIndex = '9999';
                    }
                    if (item.sec) {
                        item.sec.style.display = (targetKey === key) ? 'block' : 'none';
                    }
                });
                try {
                    localStorage.setItem('active_studio_tab', targetKey);
                } catch(e) {}
            } catch (err) {
                console.warn("switchWorkspaceTab error:", err);
            }
        };

        // Mobile touch & click binder for navigation tabs
        function bindTabButton(id, tabName) {
            const btn = document.getElementById(id);
            if (!btn) return;
            btn.style.pointerEvents = 'auto';
            btn.style.cursor = 'pointer';
            btn.style.zIndex = '9999';
            let touched = false;
            btn.addEventListener('touchstart', (e) => {
                // Passive touch start - do not block browser gestures or touch tracking
            }, { passive: true });
            btn.addEventListener('touchend', (e) => {
                touched = true;
                window.switchWorkspaceTab(tabName);
                setTimeout(() => { touched = false; }, 350);
            }, { passive: true });
            btn.addEventListener('click', (e) => {
                if (touched) return;
                window.switchWorkspaceTab(tabName);
            });
        }

        // ==============================================
        // GEMINI 10-KEY POOL & AUTO-ROTATION ENGINE (FRONTEND)
        // ==============================================
        window.currentActiveChannelId = 'default';
        window.currentActiveChannelTitle = 'YouTube Creator';

        const geminiNavPill = document.getElementById('geminiNavPill');
        const geminiDot = document.getElementById('geminiDot');
        const geminiStatusLabel = document.getElementById('geminiStatusLabel');
        const geminiModalOverlay = document.getElementById('geminiModalOverlay') || window._detachedModals['geminiModalOverlay'];
        if (geminiModalOverlay) window._detachedModals['geminiModalOverlay'] = geminiModalOverlay;
        const btnOpenKeyModal = document.getElementById('btnOpenKeyModal');
        const btnCloseKeyModal = document.getElementById('btnCloseKeyModal');
        const btnSaveGeminiKey = document.getElementById('btnSaveGeminiKey');
        const geminiApiKeyInput = document.getElementById('geminiApiKeyInput');
        const geminiModelSelect = document.getElementById('geminiModelSelect');
        const geminiKeyStatusMsg = document.getElementById('geminiKeyStatusMsg');
        const modalActiveChannelTitle = document.getElementById('modalActiveChannelTitle');
        const poolCapacityBadge = document.getElementById('poolCapacityBadge');
        const keySlotsList = document.getElementById('keySlotsList');
        const btnRefreshKeyPool = document.getElementById('btnRefreshKeyPool');

        function openKeyModal() {
            const modalEl = geminiModalOverlay || window._detachedModals['geminiModalOverlay'];
            if (modalEl) {
                if (!document.body.contains(modalEl)) {
                    document.body.appendChild(modalEl);
                }
                modalEl.classList.add('active');
                modalEl.style.display = 'flex';
                modalEl.style.pointerEvents = 'auto';
                modalEl.style.opacity = '1';
                modalEl.style.visibility = 'visible';
                modalEl.style.width = '100vw';
                modalEl.style.height = '100vh';
                modalEl.style.zIndex = '999999';
                if (modalActiveChannelTitle) {
                    modalActiveChannelTitle.textContent = window.currentActiveChannelTitle || 'Active Channel';
                }
                loadChannelKeyPool(window.currentActiveChannelId);
            }
        }

        function closeKeyModal() {
            const modalEl = geminiModalOverlay || window._detachedModals['geminiModalOverlay'];
            if (modalEl) {
                modalEl.classList.remove('active');
                modalEl.style.display = 'none';
                modalEl.style.pointerEvents = 'none';
                modalEl.style.opacity = '0';
                modalEl.style.visibility = 'hidden';
                modalEl.style.zIndex = '-99999';
                if (modalEl.parentNode) {
                    modalEl.parentNode.removeChild(modalEl);
                }
            }
        }

        if (geminiNavPill) geminiNavPill.addEventListener('click', openKeyModal);
        if (btnOpenKeyModal) btnOpenKeyModal.addEventListener('click', openKeyModal);
        if (btnCloseKeyModal) btnCloseKeyModal.addEventListener('click', closeKeyModal);
        if (btnRefreshKeyPool) btnRefreshKeyPool.addEventListener('click', () => loadChannelKeyPool(window.currentActiveChannelId));
        if (geminiModalOverlay) {
            geminiModalOverlay.addEventListener('click', (e) => {
                if (e.target === geminiModalOverlay) closeKeyModal();
            });
        }

        async function loadChannelKeyPool(channelId) {
            const cleanId = channelId || window.currentActiveChannelId || 'default';
            if (modalActiveChannelTitle) {
                modalActiveChannelTitle.textContent = window.currentActiveChannelTitle || cleanId;
            }
            try {
                const res = await fetch(`/api/channel/gemini_keys?channel_id=${encodeURIComponent(cleanId)}`);
                const data = await res.json();
                if (data.success && data.pool) {
                    renderKeyPoolSlots(data.pool);
                    updatePoolNavStatus(data.pool);

                    // Triple redundancy auto-sync from localStorage if DB returned 0 keys
                    if (data.pool.total_active_keys === 0) {
                        tryAutoSyncFromLocal(cleanId);
                    }
                }
            } catch (err) {
                console.error("Error loading channel key pool:", err);
            }
        }

        function renderKeyPoolSlots(pool) {
            if (!keySlotsList) return;
            if (poolCapacityBadge) {
                poolCapacityBadge.textContent = `${pool.total_active_keys} / 10 Keys Active`;
                poolCapacityBadge.style.color = pool.total_active_keys > 0 ? '#4ade80' : '#f87171';
            }

            let html = '';
            (pool.slots || []).forEach((slot) => {
                if (slot.has_key) {
                    const currentBadge = slot.is_current 
                        ? '<span style="background: rgba(168, 85, 247, 0.3); color: #d8b4fe; font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(168, 85, 247, 0.5);">ROTATION ACTIVE</span>' 
                        : '';
                    html += `
                        <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; padding: 8px 12px; gap: 8px;">
                            <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
                                <span style="background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.4); font-size: 11px; font-weight: 700; padding: 2px 6px; border-radius: 4px;">Slot #${slot.slot}</span>
                                <span style="font-family: monospace; font-size: 13px; color: #f8fafc; font-weight: 600;">${slot.masked_key}</span>
                                ${currentBadge}
                            </div>
                            <div style="display: flex; gap: 6px;">
                                <button type="button" onclick="replaceChannelKey(${slot.index})" style="background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.2); color: #cbd5e1; border-radius: 4px; padding: 4px 8px; font-size: 11px; cursor: pointer;">Replace</button>
                                <button type="button" onclick="deleteChannelKey(${slot.index}, '${slot.masked_key}')" style="background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #f87171; border-radius: 4px; padding: 4px 8px; font-size: 11px; cursor: pointer;">Delete</button>
                            </div>
                        </div>
                    `;
                } else {
                    html += `
                        <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.15); border: 1px dashed rgba(255,255,255,0.1); border-radius: 6px; padding: 7px 12px;">
                            <div style="display: flex; align-items: center; gap: 10px;">
                                <span style="background: rgba(255,255,255,0.05); color: #94a3b8; font-size: 11px; padding: 2px 6px; border-radius: 4px;">Slot #${slot.slot}</span>
                                <span style="font-size: 12px; color: #64748b; font-style: italic;">Available Slot</span>
                            </div>
                            <button type="button" onclick="focusAddKeySlot(${slot.slot})" style="background: rgba(168, 85, 247, 0.15); border: 1px solid rgba(168, 85, 247, 0.3); color: #d8b4fe; border-radius: 4px; padding: 3px 8px; font-size: 11px; cursor: pointer;">+ Add Key</button>
                        </div>
                    `;
                }
            });
            keySlotsList.innerHTML = html;
        }

        function updatePoolNavStatus(pool) {
            const hasKeys = pool && pool.total_active_keys > 0;
            if (geminiDot) geminiDot.className = hasKeys ? 'status-dot active' : 'status-dot warning';
            if (geminiStatusLabel) {
                geminiStatusLabel.textContent = hasKeys 
                    ? `Gemini Active (${pool.total_active_keys} Keys Pool)` 
                    : 'Setup Gemini Key';
            }
            if (btnOpenKeyModal) {
                btnOpenKeyModal.textContent = hasKeys 
                    ? `⚙️ Pool: ${pool.total_active_keys} Keys` 
                    : '⚙️ Configure API Key';
            }
        }

        async function checkGeminiStatus() {
            try {
                const res = await fetch(`/api/gemini/status?channel_id=${encodeURIComponent(window.currentActiveChannelId || 'default')}`);
                const data = await res.json();
                if (data.pool_status) {
                    updatePoolNavStatus(data.pool_status);
                } else if (data.has_key) {
                    if (geminiDot) geminiDot.className = 'status-dot active';
                    if (geminiStatusLabel) geminiStatusLabel.textContent = `Gemini Active (${data.masked_key})`;
                    if (btnOpenKeyModal) btnOpenKeyModal.textContent = `⚙️ Key: ${data.masked_key}`;
                } else {
                    if (geminiDot) geminiDot.className = 'status-dot warning';
                    if (geminiStatusLabel) geminiStatusLabel.textContent = 'Setup Gemini Key';
                    if (btnOpenKeyModal) btnOpenKeyModal.textContent = '⚙️ Configure API Key';
                }
            } catch (err) {
                console.error("Gemini status check failed", err);
            }
        }

        window.focusAddKeySlot = function(slotNum) {
            if (geminiApiKeyInput) {
                geminiApiKeyInput.focus();
                geminiApiKeyInput.placeholder = `Enter API key for Slot #${slotNum}...`;
            }
        };

        window.deleteChannelKey = async function(index, maskedKey) {
            if (!confirm(`Are you sure you want to remove key (${maskedKey}) from Slot #${index + 1}?\n\nThis key will be permanently removed from this channel's pool.`)) {
                return;
            }
            try {
                const res = await fetch('/api/channel/gemini_keys/delete', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        channel_id: window.currentActiveChannelId || 'default',
                        index: index
                    })
                });
                const data = await res.json();
                if (data.success) {
                    showKeyStatus(data.message || 'Key deleted successfully', true);
                    loadChannelKeyPool(window.currentActiveChannelId);
                } else {
                    showKeyStatus(data.error || 'Failed to delete key', false);
                }
            } catch (e) {
                showKeyStatus('Error deleting key: ' + e.message, false);
            }
        };

        window.replaceChannelKey = async function(index) {
            const newKey = prompt(`Enter new Gemini API key for Slot #${index + 1}:`);
            if (!newKey || !newKey.trim()) return;
            try {
                showKeyStatus('Verifying & updating Slot #' + (index + 1) + '...', true);
                const res = await fetch('/api/channel/gemini_keys/update', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        channel_id: window.currentActiveChannelId || 'default',
                        index: index,
                        api_key: newKey.trim()
                    })
                });
                const data = await res.json();
                if (data.success) {
                    showKeyStatus(data.message || 'Key updated successfully', true);
                    loadChannelKeyPool(window.currentActiveChannelId);
                } else {
                    showKeyStatus(data.error || 'Failed to update key', false);
                }
            } catch (e) {
                showKeyStatus('Error updating key: ' + e.message, false);
            }
        };

        function showKeyStatus(msg, isSuccess) {
            if (!geminiKeyStatusMsg) return;
            geminiKeyStatusMsg.style.display = 'block';
            geminiKeyStatusMsg.style.background = isSuccess ? 'rgba(34, 197, 94, 0.15)' : 'rgba(239, 68, 68, 0.15)';
            geminiKeyStatusMsg.style.color = isSuccess ? '#4ade80' : '#f87171';
            geminiKeyStatusMsg.style.border = `1px solid ${isSuccess ? 'rgba(34, 197, 94, 0.3)' : 'rgba(239, 68, 68, 0.3)'}`;
            geminiKeyStatusMsg.textContent = msg;
        }

        if (btnSaveGeminiKey) {
            btnSaveGeminiKey.addEventListener('click', async () => {
                const key = geminiApiKeyInput.value.trim();
                const model = geminiModelSelect.value;
                if (!key) {
                    alert("Please enter a valid Gemini API key!");
                    return;
                }
                btnSaveGeminiKey.disabled = true;
                btnSaveGeminiKey.innerHTML = '<span class="spinner" style="width: 14px; height: 14px;"></span> Verifying...';
                if (geminiKeyStatusMsg) geminiKeyStatusMsg.style.display = 'none';

                try {
                    const cleanChId = window.currentActiveChannelId || 'default';
                    const res = await fetch('/api/channel/gemini_keys/add', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            channel_id: cleanChId,
                            api_key: key
                        })
                    });
                    const data = await res.json();
                    if (data.success) {
                        showKeyStatus(`✔ ${data.message || 'Key saved to pool successfully!'}`, true);
                        geminiApiKeyInput.value = '';
                        geminiApiKeyInput.placeholder = 'Enter Gemini API key (AIzaSy...)';
                        // Also sync active model config
                        fetch('/api/gemini/config', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ channel_id: cleanChId, api_key: key, model: model })
                        }).catch(() => {});

                        // Cache raw key to client localStorage array for cold disaster recovery
                        try {
                            const storeKey = 'gemini_local_backup_' + cleanChId;
                            const existingLocal = JSON.parse(localStorage.getItem(storeKey) || '[]');
                            if (!existingLocal.includes(key)) {
                                existingLocal.push(key);
                                localStorage.setItem(storeKey, JSON.stringify(existingLocal.slice(-10)));
                            }
                        } catch(e) {}

                        setTimeout(() => {
                            loadChannelKeyPool(cleanChId);
                            btnSaveGeminiKey.disabled = false;
                            btnSaveGeminiKey.innerHTML = '<span>+ Add to Pool</span>';
                        }, 500);
                    } else {
                        showKeyStatus('Error: ' + (data.error || data.message || 'Failed to verify key'), false);
                        btnSaveGeminiKey.disabled = false;
                        btnSaveGeminiKey.innerHTML = '<span>+ Add to Pool</span>';
                    }
                } catch (err) {
                    showKeyStatus('Network error saving key: ' + err.message, false);
                    btnSaveGeminiKey.disabled = false;
                    btnSaveGeminiKey.innerHTML = '<span>+ Add to Pool</span>';
                }
            });
        }

        async function tryAutoSyncFromLocal(cleanId) {
            try {
                const storeKey = 'gemini_local_backup_' + cleanId;
                const cachedRawKeys = JSON.parse(localStorage.getItem(storeKey) || '[]');
                if (Array.isArray(cachedRawKeys) && cachedRawKeys.length > 0) {
                    console.info(`Cold reboot detected: Auto-syncing ${cachedRawKeys.length} cached keys for channel ${cleanId}`);
                    const syncRes = await fetch('/api/channel/gemini_keys/sync', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            channel_id: cleanId,
                            keys: cachedRawKeys
                        })
                    });
                    const syncData = await syncRes.json();
                    if (syncData.success && syncData.pool) {
                        renderKeyPoolSlots(syncData.pool);
                        updatePoolNavStatus(syncData.pool);
                    }
                }
            } catch (e) {
                console.warn("Auto-sync from local notice:", e);
            }
        }

        // Video Target Format State & Switcher
        let currentSelectedFormat = 'Short';

        function selectVideoFormat(fmt) {
            currentSelectedFormat = fmt === 'Long' ? 'Long' : 'Short';
            const shortCard = document.getElementById('formatCardShort');
            const longCard = document.getElementById('formatCardLong');
            if (shortCard && longCard) {
                if (currentSelectedFormat === 'Short') {
                    shortCard.classList.add('active');
                    longCard.classList.remove('active');
                } else {
                    longCard.classList.add('active');
                    shortCard.classList.remove('active');
                }
            }
        }

        // AI Video File Selection & Instant Frame Extraction
        const aiVideoInput = document.getElementById('aiVideoFileInput');
        const aiVideoFileInfo = document.getElementById('aiVideoFileInfo');
        const aiVideoDropzone = document.getElementById('aiVideoDropzone');
        const clientVideo = document.getElementById('clientVideoDecoder');
        const clientCanvas = document.getElementById('clientFrameCanvas');

        if (aiVideoInput) {
            aiVideoInput.addEventListener('change', (e) => {
                if (aiVideoInput.files && aiVideoInput.files[0]) {
                    handleAiVideoSelection(aiVideoInput.files[0]);
                }
            });
        }

        if (aiVideoDropzone) {
            aiVideoDropzone.addEventListener('dragover', (e) => { e.preventDefault(); aiVideoDropzone.classList.add('dragover'); });
            aiVideoDropzone.addEventListener('dragleave', () => { aiVideoDropzone.classList.remove('dragover'); });
            aiVideoDropzone.addEventListener('drop', (e) => {
                e.preventDefault();
                aiVideoDropzone.classList.remove('dragover');
                if (e.dataTransfer.files && e.dataTransfer.files[0]) {
                    if (aiVideoInput) aiVideoInput.files = e.dataTransfer.files;
                    handleAiVideoSelection(e.dataTransfer.files[0]);
                }
            });
        }

        function handleAiVideoSelection(file) {
            selectedVideoFile = file;
            const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
            aiVideoFileInfo.textContent = `Selected: ${file.name} (${sizeMb} MB) &bull; Ready for Multimodal Analysis`;
            aiVideoFileInfo.style.display = 'block';

            // Also synchronize with manual file input
            const dt = new DataTransfer();
            dt.items.add(file);
            document.getElementById('videoFileInput').files = dt.files;
            document.getElementById('videoFileInfo').textContent = `Selected: ${file.name} (${sizeMb} MB)`;
            document.getElementById('videoFileInfo').style.display = 'block';

            // Instant Client-side Frame Extraction for 100% genuine face match
            extractClientVideoFrames(file);
        }

        // Extracts 5 high-emotion candidate frames from browser decoder as fallback for Slots 2-6
        async function extractClientVideoFrames(file) {
            clientExtractedFrames = [];
            const objectUrl = URL.createObjectURL(file);
            clientVideo.src = objectUrl;

            await new Promise((resolve) => {
                clientVideo.onloadedmetadata = () => resolve();
            });

            const duration = clientVideo.duration || 10;
            const width = clientVideo.videoWidth || 1280;
            const height = clientVideo.videoHeight || 720;

            // Auto-detect format (Shorts 9:16 vs Long-form 16:9) from native video geometry & duration
            if (width < height) {
                selectVideoFormat('Short');
            } else if (width > height && duration > 90) {
                selectVideoFormat('Long');
            }

            const targetAspect = (currentSelectedFormat === 'Short' || width < height) ? '9:16' : '16:9';
            clientCanvas.width = width;
            clientCanvas.height = height;
            const ctx = clientCanvas.getContext('2d');

            // Sample 5 high-emotion timestamps across the video for Slots 2 to 6
            const fractions = [0.14, 0.30, 0.48, 0.66, 0.84];
            const timestamps = fractions.map(f => Math.min(duration - 0.2, Math.max(0.5, duration * f)));

            for (let i = 0; i < timestamps.length; i++) {
                const ts = timestamps[i];
                clientVideo.currentTime = ts;
                await new Promise((resolve) => {
                    clientVideo.onseeked = () => resolve();
                });

                ctx.drawImage(clientVideo, 0, 0, width, height);
                const blob = await new Promise(resolve => clientCanvas.toBlob(resolve, 'image/jpeg', 0.95));

                const mins = Math.floor(ts / 60);
                const secs = Math.floor(ts % 60);
                const timeStr = `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
                const label = `High-Emotion Frame #${i + 1} (${timeStr})`;

                // Send frame to server to crop to strict 9:16 or 16:9 and store in uploads/thumbnails
                try {
                    const fd = new FormData();
                    fd.append('image_file', blob, `frame_${i+1}_${timeStr.replace(':', 'm')}s.jpg`);
                    fd.append('timestamp', timeStr);
                    fd.append('seconds', ts);
                    fd.append('label', label);
                    fd.append('aspect_ratio', targetAspect);

                    const res = await fetch('/api/save_thumbnail_frame', { method: 'POST', body: fd });
                    const frameData = await res.json();
                    clientExtractedFrames.push(frameData);
                } catch (e) {
                    console.error("Frame save error", e);
                }
            }

            URL.revokeObjectURL(objectUrl);
        }

        // Run Gemini Analysis
        const btnRunAiAnalysis = document.getElementById('btnRunAiAnalysis');
        const aiStepsContainer = document.getElementById('aiStepsContainer');
        const geminiResultsBox = document.getElementById('geminiResultsBox');

        if (btnRunAiAnalysis) {
            btnRunAiAnalysis.addEventListener('click', async () => {
                if (!selectedVideoFile) {
                    alert("Please select or drop a video file first!");
                    return;
                }

                btnRunAiAnalysis.disabled = true;
                aiStepsContainer.style.display = 'block';
                geminiResultsBox.style.display = 'none';

                // Animate steps
                setStepActive('step1');
                setTimeout(() => { setStepCompleted('step1'); setStepActive('step2'); }, 1200);

                const formData = new FormData();
                formData.append('video_file', selectedVideoFile);
                formData.append('instructions', document.getElementById('aiCustomPrompt').value);
                formData.append('format_type', currentSelectedFormat);

                // Step 2 & 3
                setTimeout(() => { setStepCompleted('step2'); setStepActive('step3'); }, 4000);
                setTimeout(() => { setStepCompleted('step3'); setStepActive('step4'); }, 8500);

                try {
                    const res = await fetch('/api/gemini/analyze', {
                        method: 'POST',
                        body: formData
                    });

                    if (!res.ok) {
                        const errData = await res.json();
                        throw new Error(errData.error || "Analysis failed");
                    }

                    setStepCompleted('step4');
                    setStepActive('step5');

                    const metadata = await res.json();
                    currentGeminiData = metadata;
                    tempVideoServerFilename = metadata.video_filename;
                    document.getElementById('existingVideoFilename').value = tempVideoServerFilename;

                    setTimeout(() => {
                        setStepCompleted('step5');
                        renderGeminiResults(metadata);
                        btnRunAiAnalysis.disabled = false;
                    }, 600);

                } catch (err) {
                    alert("Gemini Analysis Error: " + err.message);
                    btnRunAiAnalysis.disabled = false;
                    aiStepsContainer.style.display = 'none';
                }
            });
        }

        function setStepActive(id) {
            document.querySelectorAll('.ai-step-item').forEach(el => el.classList.remove('active'));
            const el = document.getElementById(id);
            if (el) el.classList.add('active');
        }
        function setStepCompleted(id) {
            const el = document.getElementById(id);
            if (el) { el.classList.remove('active'); el.classList.add('completed'); }
        }

        // Render Gemini Results (Slot 1 AI Dynamic Thumbnail + Slots 2-6 High-Emotion Local Video Frames)
        function renderGeminiResults(data) {
            geminiResultsBox.style.display = 'block';

            const aspectRatio = data.thumbnail_aspect_ratio || (data.format_type === 'Short' ? '9:16' : '16:9');
            const isVertical = (aspectRatio === '9:16');

            // Target Format & Primary Context Display
            const fmtEl = document.getElementById('aiTargetFormatBadge');
            if (fmtEl) fmtEl.textContent = (data.format_type === 'Long' ? `🎬 Long Form Video (${aspectRatio})` : `📱 YouTube Shorts (${aspectRatio})`);
            const ctxEl = document.getElementById('aiPrimaryContext');
            if (ctxEl) ctxEl.textContent = data.primary_context || 'Autonomous Evaluation';

            const ratioBadge = document.getElementById('aiThumbAspectRatioBadge');
            if (ratioBadge) {
                ratioBadge.textContent = isVertical
                    ? '✔ 9:16 Vertical Auto-Detected (720×1280)'
                    : '✔ 16:9 Cinematic Auto-Detected (1280×720)';
            }

            // 0. AI Mood & Language Classification
            const moodEl = document.getElementById('aiDetectedMood');
            if (moodEl) moodEl.textContent = data.detected_genre_emotion || data.primary_context || 'Entertainment';
            const langEl = document.getElementById('aiDetectedLang');
            if (langEl) langEl.textContent = data.detected_language || 'Hindi / Hinglish';

            // 1. Title cards (Viral Title + 2 Alternatives)
            const titleCardsGrid = document.getElementById('titleCardsGrid');
            const primaryTitle = data.viral_title || data.primary_title || data.recommended_title || 'Viral Video Hook 🔥';
            const titles = [
                { text: primaryTitle, tag: (data.format_type === 'Long' ? "⭐ Search-Optimized [Hook | Keyword]" : "⭐ High Velocity Short Hook (< 50 Chars)"), isRec: true },
                ...(data.alternative_titles || []).map((t, i) => ({
                    text: t,
                    tag: i === 0 ? "Alternative Catchy Title 1" : "Alternative Catchy Title 2",
                    isRec: false
                }))
            ];

            titleCardsGrid.innerHTML = titles.map((t, idx) => `
                <div class="title-card ${idx === 0 ? 'selected' : ''}" onclick="selectTitleCard(this, '${escapeHtml(t.text)}')">
                    <span class="title-card-text">${escapeHtml(t.text)}</span>
                    <span class="title-card-tag">${t.tag}</span>
                </div>
            `).join('');

            // Set default title
            document.getElementById('videoTitle').value = primaryTitle;
            document.getElementById('titleCounter').textContent = `${primaryTitle.length} / 100`;

            // 2. Enhanced Thumbnail Suite: Slot 1 (AI Dynamic Default Selected) + Slots 2-6 (High-Emotion Local Frames)
            const gallery = document.getElementById('thumbnailGalleryGrid');
            let serverThumbnails = Array.isArray(data.extracted_thumbnails) ? [...data.extracted_thumbnails] : [];
            if (serverThumbnails.length < 6 && clientExtractedFrames.length > 0) {
                for (let i = 0; i < clientExtractedFrames.length && serverThumbnails.length < 6; i++) {
                    const cf = clientExtractedFrames[i];
                    if (cf && cf.filename && !serverThumbnails.some(st => st.filename === cf.filename)) {
                        serverThumbnails.push({
                            ...cf,
                            slot: serverThumbnails.length + 1,
                            is_ai_generated: false,
                            is_recommended: false,
                            selected: false,
                            aspect_ratio: aspectRatio
                        });
                    }
                }
            }
            const allThumbnails = serverThumbnails.slice(0, 6);

            if (allThumbnails.length > 0) {
                gallery.style.gridTemplateColumns = isVertical
                    ? 'repeat(auto-fit, minmax(150px, 1fr))'
                    : 'repeat(auto-fit, minmax(210px, 1fr))';

                gallery.innerHTML = allThumbnails.map((th, idx) => {
                    const slotNum = th.slot || (idx + 1);
                    const isSlot1 = (idx === 0 || Boolean(th.is_ai_generated));
                    const cardAspectCss = isVertical ? '9/16' : '16/9';
                    const topBadgeHtml = isSlot1
                        ? `<span class="thumb-ai-rec-badge" style="background: linear-gradient(135deg, #ec4899, #8b5cf6); box-shadow: 0 2px 10px rgba(236,72,153,0.45);">✨ SLOT 1: AI DYNAMIC (${aspectRatio})</span>`
                        : `<span class="thumb-ai-rec-badge" style="background: rgba(15, 23, 42, 0.88); border: 1px solid rgba(56, 189, 248, 0.5); color: #38bdf8;">🎬 SLOT ${slotNum}: LOCAL FRAME</span>`;
                    const bottomBadgeText = isSlot1
                        ? (th.label || 'AI Dynamic Visual')
                        : `${th.timestamp || 'Frame'} • ${th.label || ('High-Emotion #' + (slotNum - 1))}`;

                    return `
                        <div class="thumb-candidate-card ${isSlot1 ? 'selected gemini-best' : ''}" style="aspect-ratio: ${cardAspectCss};" onclick="selectThumbnailFrame(this, '${th.url}', '${th.filename}', '${aspectRatio}')">
                            <img src="${th.url}" alt="Slot ${slotNum} Thumbnail" style="width: 100%; height: 100%; object-fit: cover;">
                            ${topBadgeHtml}
                            <span class="thumb-badge">${escapeHtml(bottomBadgeText)}</span>
                            <span class="thumb-highlight-badge">✔ Selected</span>
                        </div>
                    `;
                }).join('');

                // Auto-select Slot 1 (AI Dynamic Thumbnail) by default
                selectThumbnailFrame(gallery.firstElementChild, allThumbnails[0].url, allThumbnails[0].filename, aspectRatio);
            } else {
                gallery.innerHTML = '<div style="color: var(--text-muted); font-size: 13px;">No thumbnails extracted. You can upload a custom one.</div>';
            }

            // Thumbnail Directive Art Direction
            const thumbDir = data.thumbnail_directive || {};
            const overlayEl = document.getElementById('aiThumbOverlayText');
            if (overlayEl) overlayEl.textContent = thumbDir.text_overlay || 'WATCH THIS';
            const sceneEl = document.getElementById('aiThumbSceneDir');
            if (sceneEl) sceneEl.textContent = thumbDir.visual_scene_direction || 'High emotion close-up frame with dramatic lighting';
            const colorEl = document.getElementById('aiThumbColorTheme');
            if (colorEl) colorEl.textContent = thumbDir.recommended_color_theme || 'High contrast background with bold font';

            // 3. Description
            const descArea = document.getElementById('aiGeneratedDesc');
            descArea.value = data.description || '';
            document.getElementById('videoDesc').value = data.description || '';
            document.getElementById('descCounter').textContent = `${(data.description || '').length} / 5000`;

            // 4. Niche-Specific Hashtags
            const hashtagsDisplay = document.getElementById('aiHashtagsDisplay');
            const hashtags = data.hashtags || [];
            if (hashtagsDisplay) {
                hashtagsDisplay.innerHTML = hashtags.map(h => `<div class="tag-chip" style="border-color: #c084fc; color: #f3e8ff;"><span>${h.startsWith('#') ? h : '#' + h}</span></div>`).join('');
                const hCount = document.getElementById('aiHashtagCount');
                if (hCount) hCount.textContent = `${hashtags.length} hyper-targeted tags`;
            }

            // 5. SEO Keywords & Search Tags
            const tagsDisplay = document.getElementById('aiTagsDisplay');
            const tags = data.seo_keywords || data.tags || [];
            tagsDisplay.innerHTML = tags.map(t => `<div class="tag-chip"><span>${t}</span></div>`).join('');
            document.getElementById('aiTagCount').textContent = `${tags.length} SEO keywords`;
            
            // Set tags in manual studio
            window.tags = [...tags];
            updateTags();

            // 6. Category & Audience
            document.getElementById('aiCategoryName').textContent = data.category_name || 'Entertainment';
            document.getElementById('aiCategoryId').textContent = `Category ID: ${data.category_id || 24}`;
            document.getElementById('categorySelect').value = String(data.category_id || 24);

            document.getElementById('aiVideoTypeBadge').textContent = (data.video_type === 'Short' ? `YouTube Short (${aspectRatio})` : `Long-form Video (${aspectRatio})`);
            document.getElementById('aiKidsBadge').textContent = data.made_for_kids ? 'Audience: Made for Kids' : 'Audience: General (Not for kids)';
            document.getElementById('madeForKids').checked = Boolean(data.made_for_kids);

            // 7. Strategic Insights
            document.getElementById('aiSummaryInsights').textContent = data.summary_insights || 'Individual video evaluation completed via Gemini 2.5 Flash.';

            // Scroll to results smoothly
            geminiResultsBox.scrollIntoView({ behavior: 'smooth' });
        }

        function escapeHtml(text) {
            if (!text) return '';
            return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
        }

        function selectTitleCard(card, text) {
            document.querySelectorAll('.title-card').forEach(c => c.classList.remove('selected'));
            card.classList.add('selected');
            document.getElementById('videoTitle').value = text;
            document.getElementById('titleCounter').textContent = `${text.length} / 100`;
        }

        function selectThumbnailFrame(card, url, filename, aspectRatio) {
            if (!card) return;
            document.querySelectorAll('.thumb-candidate-card').forEach(c => c.classList.remove('selected'));
            card.classList.add('selected');
            selectedThumbnailUrl = url;
            selectedThumbnailFilename = filename;
            document.getElementById('selectedThumbnailFilename').value = filename;

            // Update preview box in manual studio
            const thumbPreviewBox = document.getElementById('thumbPreviewBox');
            const thumbPreviewImg = document.getElementById('thumbPreviewImg');
            const thumbPlaceholder = document.getElementById('thumbPlaceholder');
            if (thumbPreviewBox && aspectRatio) {
                thumbPreviewBox.style.aspectRatio = (aspectRatio === '9:16') ? '9/16' : '16/9';
                thumbPreviewBox.style.maxWidth = (aspectRatio === '9:16') ? '220px' : '100%';
                thumbPreviewBox.style.margin = (aspectRatio === '9:16') ? '10px auto 0 auto' : '10px 0 0 0';
            }
            thumbPreviewImg.src = url;
            thumbPreviewImg.style.display = 'block';
            thumbPlaceholder.style.display = 'none';
        }

        // Auto-Populate Form Button
        const btnPopulateToManual = document.getElementById('btnPopulateToManual');
        if (btnPopulateToManual) {
            btnPopulateToManual.addEventListener('click', () => {
                window.switchWorkspaceTab('manual');
                const manualStudioSection = document.getElementById('manualStudioSection');
                if (manualStudioSection) manualStudioSection.scrollIntoView({ behavior: 'smooth' });
            });
        }

        // One-Click Auto-Publish Button
        const btnOneClickPublish = document.getElementById('btnOneClickPublish');
        if (btnOneClickPublish) {
            btnOneClickPublish.addEventListener('click', () => {
                window.switchWorkspaceTab('manual');
                const form = document.getElementById('uploadForm');
                if (form) form.dispatchEvent(new Event('submit'));
            });
        }

        // Chat Drawer Toggle
        const chatFabBtn = document.getElementById('chatFabBtn');
        const chatDrawer = document.getElementById('chatDrawer') || window._detachedModals['chatDrawer'];
        if (chatDrawer) window._detachedModals['chatDrawer'] = chatDrawer;
        const btnCloseChat = document.getElementById('btnCloseChat');
        const chatInput = document.getElementById('chatInput');
        const btnSendChat = document.getElementById('btnSendChat');
        const chatMessages = document.getElementById('chatMessages');

        function closeChatDrawerHard() {
            const drawerEl = chatDrawer || window._detachedModals['chatDrawer'];
            if (drawerEl) {
                drawerEl.classList.remove('open');
                drawerEl.style.display = 'none';
                drawerEl.style.pointerEvents = 'none';
                drawerEl.style.opacity = '0';
                drawerEl.style.visibility = 'hidden';
                drawerEl.style.zIndex = '-99999';
                if (drawerEl.parentNode) {
                    drawerEl.parentNode.removeChild(drawerEl);
                }
            }
        }

        if (chatFabBtn && chatDrawer) {
            chatFabBtn.addEventListener('click', () => {
                const drawerEl = chatDrawer || window._detachedModals['chatDrawer'];
                if (!drawerEl) return;
                const willOpen = !drawerEl.classList.contains('open');
                if (willOpen) {
                    if (!document.body.contains(drawerEl)) {
                        document.body.appendChild(drawerEl);
                    }
                    drawerEl.classList.add('open');
                    drawerEl.style.display = 'flex';
                    drawerEl.style.pointerEvents = 'auto';
                    drawerEl.style.opacity = '1';
                    drawerEl.style.visibility = 'visible';
                    drawerEl.style.width = '380px';
                    drawerEl.style.height = 'calc(100vh - 64px)';
                    drawerEl.style.zIndex = '1001';
                } else {
                    closeChatDrawerHard();
                }
            });
        }
        if (btnCloseChat && chatDrawer) {
            btnCloseChat.addEventListener('click', closeChatDrawerHard);
        }

        async function sendChatMessage(text) {
            if (!text || !text.trim()) return;
            if (!chatMessages) return;
            
            // Add user message
            const userMsg = document.createElement('div');
            userMsg.className = 'chat-msg chat-msg-user';
            userMsg.textContent = text;
            chatMessages.appendChild(userMsg);
            if (chatInput) chatInput.value = '';
            chatMessages.scrollTop = chatMessages.scrollHeight;

            // Loading message
            const aiMsg = document.createElement('div');
            aiMsg.className = 'chat-msg chat-msg-ai';
            aiMsg.innerHTML = '<span class="spinner" style="width: 12px; height: 12px; display: inline-block;"></span> Thinking...';
            chatMessages.appendChild(aiMsg);
            chatMessages.scrollTop = chatMessages.scrollHeight;

            try {
                const titleVal = document.getElementById('videoTitle') ? document.getElementById('videoTitle').value : '';
                const catVal = document.getElementById('categorySelect') ? document.getElementById('categorySelect').value : '';
                const privVal = document.getElementById('privacySelect') ? document.getElementById('privacySelect').value : '';
                const res = await fetch('/api/gemini/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        message: text,
                        context: {
                            title: titleVal,
                            category: catVal,
                            privacy: privVal
                        }
                    })
                });
                const data = await res.json();
                
                aiMsg.innerHTML = escapeHtml(data.reply).replace(/\\n/g, '<br>');

                if (data.suggested_title) {
                    const btn = document.createElement('button');
                    btn.className = 'chat-apply-btn';
                    btn.textContent = '✔ Apply Title to Studio';
                    btn.onclick = () => {
                        const vt = document.getElementById('videoTitle');
                        if (vt) vt.value = data.suggested_title;
                        const tc = document.getElementById('titleCounter');
                        if (tc) tc.textContent = `${data.suggested_title.length} / 100`;
                        alert("Title applied to Studio!");
                    };
                    aiMsg.appendChild(document.createElement('br'));
                    aiMsg.appendChild(btn);
                }

                if (data.suggested_description) {
                    const btn = document.createElement('button');
                    btn.className = 'chat-apply-btn';
                    btn.textContent = '✔ Apply Description to Studio';
                    btn.onclick = () => {
                        const vd = document.getElementById('videoDesc');
                        if (vd) vd.value = data.suggested_description;
                        const dc = document.getElementById('descCounter');
                        if (dc) dc.textContent = `${data.suggested_description.length} / 5000`;
                        alert("Description applied to Studio!");
                    };
                    aiMsg.appendChild(document.createElement('br'));
                    aiMsg.appendChild(btn);
                }

            } catch (err) {
                aiMsg.textContent = "Error: " + err.message;
            }
            if (chatMessages) chatMessages.scrollTop = chatMessages.scrollHeight;
        }

        if (btnSendChat && chatInput) {
            btnSendChat.addEventListener('click', () => sendChatMessage(chatInput.value));
        }
        if (chatInput) {
            chatInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') sendChatMessage(chatInput.value);
            });
        }

        function sendQuickPrompt(promptText) {
            if (chatDrawer) {
                chatDrawer.classList.add('open');
                chatDrawer.style.pointerEvents = 'auto';
            }
            sendChatMessage(promptText);
        }

        // ==============================================
        // STANDARD STUDIO JAVASCRIPT LOGIC
        // ==============================================
        const titleInput = document.getElementById('videoTitle');
        const titleCounter = document.getElementById('titleCounter');
        if (titleInput && titleCounter) {
            titleInput.addEventListener('input', () => {
                const len = titleInput.value.length;
                titleCounter.textContent = `${len} / 100`;
                titleCounter.className = 'char-counter ' + (len > 90 ? 'limit-hit' : (len > 75 ? 'limit-near' : ''));
            });
        }

        const descInput = document.getElementById('videoDesc');
        const descCounter = document.getElementById('descCounter');
        if (descInput && descCounter) {
            descInput.addEventListener('input', () => {
                const len = descInput.value.length;
                descCounter.textContent = `${len} / 5000`;
                descCounter.className = 'char-counter ' + (len > 4800 ? 'limit-hit' : (len > 4000 ? 'limit-near' : ''));
            });
        }

        // Tags chips management
        window.tags = [];
        const tagInput = document.getElementById('tagInputField');
        const tagsWrapper = document.getElementById('tagsWrapper');
        const hiddenTags = document.getElementById('hiddenTags');

        function updateTags() {
            if (hiddenTags) hiddenTags.value = window.tags.join(',');
            if (!tagsWrapper) return;
            const existingChips = tagsWrapper.querySelectorAll('.tag-chip');
            existingChips.forEach(c => c.remove());

            window.tags.forEach((tag, idx) => {
                const chip = document.createElement('div');
                chip.className = 'tag-chip';
                chip.innerHTML = `<span>${tag}</span><span class="remove-tag" data-index="${idx}">&times;</span>`;
                if (tagInput) tagsWrapper.insertBefore(chip, tagInput);
                else tagsWrapper.appendChild(chip);
            });
        }

        if (tagInput) {
            tagInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ',') {
                    e.preventDefault();
                    const val = tagInput.value.trim().replace(/^,+|,+$/g, '');
                    if (val && !window.tags.includes(val)) {
                        window.tags.push(val);
                        updateTags();
                    }
                    tagInput.value = '';
                } else if (e.key === 'Backspace' && tagInput.value === '' && window.tags.length > 0) {
                    window.tags.pop();
                    updateTags();
                }
            });
        }

        if (tagsWrapper) {
            tagsWrapper.addEventListener('click', (e) => {
                if (e.target.classList.contains('remove-tag')) {
                    const idx = parseInt(e.target.dataset.index);
                    window.tags.splice(idx, 1);
                    updateTags();
                }
            });
        }

        // Manual File inputs
        const videoInput = document.getElementById('videoFileInput');
        const videoFileInfo = document.getElementById('videoFileInfo');
        if (videoInput) {
            videoInput.addEventListener('change', () => {
                if (videoInput.files && videoInput.files[0]) {
                    const file = videoInput.files[0];
                    const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
                    if (videoFileInfo) {
                        videoFileInfo.textContent = `Selected: ${file.name} (${sizeMb} MB)`;
                        videoFileInfo.style.display = 'block';
                    }
                    if (titleInput && !titleInput.value) {
                        titleInput.value = file.name.replace(/\\.[^/.]+$/, "");
                        titleInput.dispatchEvent(new Event('input'));
                    }
                }
            });
        }

        const thumbInput = document.getElementById('thumbFileInput');
        const thumbPreviewImg = document.getElementById('thumbPreviewImg');
        const thumbPlaceholder = document.getElementById('thumbPlaceholder');

        if (thumbInput) {
            thumbInput.addEventListener('change', () => {
                if (thumbInput.files && thumbInput.files[0]) {
                    const file = thumbInput.files[0];
                    const reader = new FileReader();
                    reader.onload = (e) => {
                        if (thumbPreviewImg) {
                            thumbPreviewImg.src = e.target.result;
                            thumbPreviewImg.style.display = 'block';
                        }
                        if (thumbPlaceholder) {
                            thumbPlaceholder.style.display = 'none';
                        }
                        const selTh = document.getElementById('selectedThumbnailFilename');
                        if (selTh) selTh.value = '';
                    };
                    reader.readAsDataURL(file);
                }
            });
        }

        // Channel Info & Multi-Channel Switcher with Instant Local Cache
        function renderChannelProfileData(data) {
            if (!data) return;
            try {
                if (data.title) {
                    const cTitle = document.getElementById('channelTitle');
                    if (cTitle) cTitle.textContent = data.title;
                    const uName = document.getElementById('userName');
                    if (uName) uName.textContent = data.title;
                }
                if (data.customUrl || data.title) {
                    const handle = document.getElementById('channelHandle');
                    if (handle) handle.textContent = data.customUrl || ('@' + data.title.toLowerCase().replace(/\\s+/g, ''));
                }

                // Render channel profile picture (snippet.thumbnails.default.url or medium/high)
                const avatarUrl = data.thumbnail_url || data.avatar;
                if (avatarUrl) {
                    const avatarLarge = document.getElementById('channelAvatarLarge');
                    if (avatarLarge) {
                        avatarLarge.src = avatarUrl;
                        avatarLarge.alt = data.title || "Channel Profile";
                    }
                    const userAvatar = document.getElementById('userAvatar');
                    if (userAvatar) {
                        userAvatar.src = avatarUrl;
                        userAvatar.alt = data.title || "User Avatar";
                    }
                    const dropAvatar = document.getElementById('dropAvatar');
                    if (dropAvatar) {
                        dropAvatar.src = avatarUrl;
                        dropAvatar.alt = data.title || "Profile";
                    }
                }
                const dropName = document.getElementById('dropChannelName');
                if (dropName && data.title) dropName.textContent = data.title;
                const dropEmail = document.getElementById('dropUserEmail');
                if (dropEmail && data.userEmail) dropEmail.textContent = data.userEmail;

                const statSubs = document.getElementById('statSubscribers');
                if (statSubs) statSubs.textContent = Number(data.subscriberCount || 0).toLocaleString();
                const statViews = document.getElementById('statViews');
                if (statViews) statViews.textContent = Number(data.viewCount || 0).toLocaleString();
                const statVids = document.getElementById('statVideos');
                if (statVids) statVids.textContent = Number(data.videoCount || 0).toLocaleString();

                // Save to localStorage for instant 0ms restoration next time
                try {
                    localStorage.setItem('cached_yt_channel_profile', JSON.stringify({
                        title: data.title,
                        customUrl: data.customUrl,
                        avatar: avatarUrl,
                        thumbnail_url: avatarUrl,
                        userEmail: data.userEmail,
                        subscriberCount: data.subscriberCount,
                        viewCount: data.viewCount,
                        videoCount: data.videoCount,
                        id: data.id,
                        allAccounts: data.allAccounts,
                        allChannels: data.allChannels
                    }));
                } catch(e) {}

                // Render channels & accounts in dropdown
                const listEl = document.getElementById('dropdownChannelsList');
                if (listEl) {
                    let html = '';
                    if (data.allAccounts && data.allAccounts.length > 1) {
                        html += '<div style="font-size: 11px; text-transform: uppercase; color: var(--accent-blue); padding: 6px 12px; font-weight: 500;">Google Accounts</div>';
                        data.allAccounts.forEach(acc => {
                            const isCurAcc = acc.is_active;
                            html += `
                                <div class="dropdown-channel-item ${isCurAcc ? 'active-channel' : ''}" style="margin-bottom: 4px;" onclick="onSwitchAccountClick('${acc.key}')">
                                    <div style="width: 28px; height: 28px; border-radius: 50%; background: #3ea6ff; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: bold; color: #fff;">
                                        ${acc.email ? acc.email[0].toUpperCase() : 'G'}
                                    </div>
                                    <div class="channel-item-details">
                                        <div class="channel-item-title">${acc.email}</div>
                                        <div class="channel-item-subs">${(acc.channels && acc.channels.length) ? acc.channels.length + ' Channel(s)' : 'Connected'}</div>
                                    </div>
                                    ${isCurAcc ? '<span class="active-check-badge">Active</span>' : ''}
                                </div>
                            `;
                        });
                        html += '<div style="font-size: 11px; text-transform: uppercase; color: var(--text-muted); padding: 8px 12px 4px 12px; font-weight: 500;">Channels</div>';
                    }

                    if (data.allChannels && data.allChannels.length > 0) {
                        const fallbackSvg = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='32' height='32' viewBox='0 0 32 32'><circle cx='16' cy='16' r='16' fill='%23383838'/><circle cx='16' cy='12' r='6' fill='%23aaaaaa'/><path d='M6 28 C 6 20, 26 20, 26 28' fill='%23aaaaaa'/></svg>";
                        html += data.allChannels.map(ch => {
                            const isActive = ch.id === data.id;
                            const chImg = ch.thumbnail_url || ch.avatar || fallbackSvg;
                            return `
                                <div class="dropdown-channel-item ${isActive ? 'active-channel' : ''}" onclick="onSwitchChannelClick('${ch.id}', ${isActive})">
                                    <img src="${chImg}" alt="${ch.title || 'Channel'}" onerror="this.src='${fallbackSvg}'">
                                    <div class="channel-item-details">
                                        <div class="channel-item-title">${ch.title}</div>
                                        <div class="channel-item-subs">${Number(ch.subscriberCount || 0).toLocaleString()} subs</div>
                                    </div>
                                    ${isActive ? '<span class="active-check-badge">✓</span>' : ''}
                                </div>
                            `;
                        }).join('');
                    }
                    listEl.innerHTML = html;
                }
            } catch (err) {
                console.error("renderChannelProfileData error:", err);
            }
        }

        async function loadChannelInfo() {
            try {
                // Try instant 0ms render from localStorage first
                try {
                    const cached = localStorage.getItem('cached_yt_channel_profile');
                    if (cached) {
                        renderChannelProfileData(JSON.parse(cached));
                    }
                } catch(e) {}

                const res = await fetch('/api/channel');
                if (!res.ok) {
                    console.warn("Failed to load channel details:", res.status);
                    return;
                }
                const data = await res.json();
                if (data && data.id && data.id !== 'no_channel') {
                    window.currentActiveChannelId = data.id;
                    window.currentActiveChannelTitle = data.title || 'YouTube Creator';
                    checkGeminiStatus();
                }
                renderChannelProfileData(data);
            } catch (err) {
                console.error("loadChannelInfo error:", err);
            }
        }

        async function onSwitchAccountClick(accountKey) {
            try {
                const drop = document.getElementById('accountDropdown');
                if (drop) drop.classList.remove('show');
                const res = await fetch(`/api/switch_account/${encodeURIComponent(accountKey)}`, { method: 'POST' });
                if (res.ok) {
                    await loadChannelInfo();
                    await loadRecentVideos();
                }
            } catch (e) {
                console.error("Switch account error:", e);
            }
        }

        async function onSwitchChannelClick(channelId, isActive) {
            if (isActive) return;
            try {
                const drop = document.getElementById('accountDropdown');
                if (drop) drop.classList.remove('show');
                const res = await fetch(`/api/switch_channel/${channelId}`, { method: 'POST' });
                if (res.ok) {
                    await loadChannelInfo();
                    await loadRecentVideos();
                }
            } catch (e) {
                console.error("Switch channel error:", e);
            }
        }

        // Dropdown toggle & dismiss
        const userPill = document.getElementById('userPill');
        const accountDropdown = document.getElementById('accountDropdown');
        const btnSwitchChannelSidebar = document.getElementById('btnSwitchChannelSidebar');

        if (userPill && accountDropdown) {
            userPill.addEventListener('click', (e) => {
                e.stopPropagation();
                accountDropdown.classList.toggle('show');
            });
        }
        if (btnSwitchChannelSidebar && accountDropdown) {
            btnSwitchChannelSidebar.addEventListener('click', (e) => {
                e.stopPropagation();
                accountDropdown.classList.toggle('show');
            });
        }
        document.addEventListener('click', (e) => {
            if (accountDropdown && !accountDropdown.contains(e.target) && (!userPill || !userPill.contains(e.target))) {
                accountDropdown.classList.remove('show');
            }
        });

        // Recent Uploads
        async function loadRecentVideos() {
            const grid = document.getElementById('recentVideosGrid');
            try {
                const res = await fetch('/api/recent_videos');
                const videos = await res.json();
                if (!videos || videos.length === 0) {
                    grid.innerHTML = '<div style="color: var(--text-muted); font-size: 13px;">No recent videos found.</div>';
                    return;
                }
                grid.innerHTML = videos.map(v => `
                    <div class="video-item-card">
                        <div class="video-thumb-wrap">
                            <img src="${v.thumbnail || 'https://via.placeholder.com/320x180/222/666?text=No+Thumb'}" alt="thumb">
                            <span class="video-privacy-badge">${v.privacy || 'PUBLIC'}</span>
                        </div>
                        <div class="video-details">
                            <div class="video-item-title">${v.title}</div>
                            <div class="video-meta">
                                <span>${new Date(v.publishedAt).toLocaleDateString()}</span>
                                <a href="https://youtu.be/${v.id}" target="_blank" style="color: var(--accent-blue); float: right; text-decoration: none;">Watch &#8599;</a>
                            </div>
                        </div>
                    </div>
                `).join('');
            } catch (err) {
                grid.innerHTML = '<div style="color: var(--text-muted); font-size: 13px;">Unable to fetch uploads list.</div>';
            }
        }

        const refreshVideosBtn = document.getElementById('refreshVideosBtn');
        if (refreshVideosBtn) {
            refreshVideosBtn.addEventListener('click', loadRecentVideos);
        }

        // Upload Form with Resumable Tracking
        const uploadForm = document.getElementById('uploadForm');
        const submitBtn = document.getElementById('submitBtn');
        const progressCard = document.getElementById('progressCard');
        const progressBarFill = document.getElementById('progressBarFill');
        const progressTitle = document.getElementById('progressTitle');
        const progressPercent = document.getElementById('progressPercent');
        const progressStatusText = document.getElementById('progressStatusText');
        const successCard = document.getElementById('successCard');

        if (uploadForm) {
            uploadForm.addEventListener('submit', (e) => {
                e.preventDefault();
                const hasFile = videoInput && videoInput.files && videoInput.files[0];
                const existingInput = document.getElementById('existingVideoFilename');
                const hasExisting = existingInput ? existingInput.value : '';

                if (!hasFile && !hasExisting) {
                    alert("Please select a video file to upload!");
                    return;
                }

                const formData = new FormData(uploadForm);
                if (submitBtn) submitBtn.disabled = true;
                if (progressCard) progressCard.style.display = 'block';
                if (successCard) successCard.style.display = 'none';
                if (progressBarFill) progressBarFill.style.width = '0%';
                if (progressPercent) progressPercent.textContent = '0%';
                if (progressTitle) progressTitle.textContent = 'Uploading to Server...';
                if (progressStatusText) progressStatusText.textContent = 'Streaming media payload to local buffer...';

                if (progressCard) progressCard.scrollIntoView({ behavior: 'smooth' });

                const xhr = new XMLHttpRequest();
                xhr.open('POST', '/api/upload_start', true);

                xhr.upload.onprogress = (evt) => {
                    if (evt.lengthComputable) {
                        const percentComplete = Math.round((evt.loaded / evt.total) * 45);
                        if (progressBarFill) progressBarFill.style.width = percentComplete + '%';
                        if (progressPercent) progressPercent.textContent = percentComplete + '%';
                    }
                };

                xhr.onload = () => {
                    if (xhr.status === 200) {
                        const res = JSON.parse(xhr.responseText);
                        const taskId = res.task_id;
                        if (progressTitle) progressTitle.textContent = 'YouTube Cloud Ingestion...';
                        if (progressStatusText) progressStatusText.textContent = 'Connecting to Google Video Resumable Upload Engine...';
                        pollUploadProgress(taskId);
                    } else {
                        if (submitBtn) submitBtn.disabled = false;
                        if (progressTitle) progressTitle.textContent = 'Upload Failed';
                        if (progressStatusText) progressStatusText.textContent = 'Error: ' + xhr.responseText;
                    }
                };

                xhr.onerror = () => {
                    if (submitBtn) submitBtn.disabled = false;
                    if (progressTitle) progressTitle.textContent = 'Network Error';
                    if (progressStatusText) progressStatusText.textContent = 'Failed to reach local server.';
                };

                xhr.send(formData);
            });
        }

        function pollUploadProgress(taskId) {
            const interval = setInterval(async () => {
                try {
                    const res = await fetch(`/api/upload_status/${taskId}`);
                    const data = await res.json();
                    
                    if (data.status === 'uploading') {
                        const ytPercent = Math.round(45 + (data.progress * 50));
                        progressBarFill.style.width = ytPercent + '%';
                        progressPercent.textContent = ytPercent + '%';
                        progressStatusText.textContent = `Streaming chunks to YouTube: ${Math.round(data.progress * 100)}% complete...`;
                    } else if (data.status === 'processing_thumbnail') {
                        progressBarFill.style.width = '96%';
                        progressPercent.textContent = '96%';
                        progressStatusText.textContent = 'Setting authentic character face thumbnail...';
                    } else if (data.status === 'completed') {
                        clearInterval(interval);
                        progressBarFill.style.width = '100%';
                        progressPercent.textContent = '100%';
                        progressStatusText.textContent = 'Upload fully completed!';
                        setTimeout(() => {
                            progressCard.style.display = 'none';
                            successCard.style.display = 'block';
                            document.getElementById('watchBtn').href = `https://youtu.be/${data.video_id}`;
                            document.getElementById('studioBtn').href = `https://studio.youtube.com/video/${data.video_id}/edit`;
                            submitBtn.disabled = false;
                            loadRecentVideos();
                            successCard.scrollIntoView({ behavior: 'smooth' });
                        }, 800);
                    } else if (data.status === 'error') {
                        clearInterval(interval);
                        submitBtn.disabled = false;
                        progressTitle.textContent = 'YouTube API Error';
                        progressPercent.textContent = '!';
                        progressStatusText.textContent = data.error || 'An error occurred during YouTube upload.';
                    }
                } catch (err) {
                    console.error("Poll error", err);
                }
            }, 1000);
        }

        // ==============================================
        // INITIALIZATION & TAB BINDING (WITH TRY-CATCH)
        // ==============================================
        function initializeApp() {
            try {
                window.forceClearBlockingOverlays();

                // Bind the two core pillar tabs with mobile touch and desktop click listeners
                bindTabButton('tabGeminiMode', 'gemini');
                bindTabButton('tabManualMode', 'manual');

                // Default active tab to Gemini AI Studio Copilot (#geminiStudioSection) on initial page load
                window.switchWorkspaceTab('gemini');
            } catch (e) {
                console.warn("Tab binding warning:", e);
            }

            try {
                loadChannelInfo();
            } catch (e) {
                console.warn("loadChannelInfo warning:", e);
            }

            try {
                loadRecentVideos();
            } catch (e) {
                console.warn("loadRecentVideos warning:", e);
            }

            try {
                checkGeminiStatus();
            } catch (e) {
                console.warn("checkGeminiStatus warning:", e);
            }
        }

        if (document.readyState === 'loading') {
            window.addEventListener('DOMContentLoaded', initializeApp);
        } else {
            initializeApp();
        }
    </script>
</body>
</html>
"""

HTML_SETUP = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
    <title>Setup - YouTube Studio Pro</title>
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
    <style>
        body {
            font-family: 'Roboto', sans-serif;
            background: #0f0f0f;
            color: #fff;
            margin: 0;
            padding: 40px 20px;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            box-sizing: border-box;
        }
        .setup-card {
            background: #1f1f1f;
            border: 1px solid #333;
            border-radius: 12px;
            max-width: 680px;
            width: 100%;
            padding: 36px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.6);
        }
        h1 { color: #ff0000; margin-top: 0; font-size: 26px; display: flex; align-items: center; gap: 10px; }
        p { color: #aaa; line-height: 1.6; }
        .steps { margin: 20px 0; padding-left: 20px; color: #ccc; }
        .steps li { margin-bottom: 12px; }
        code { background: #121212; padding: 2px 6px; border-radius: 4px; color: #3ea6ff; font-size: 13px; }
        .dropzone {
            border: 2px dashed #444;
            border-radius: 8px;
            padding: 24px;
            text-align: center;
            background: #151515;
            margin: 20px 0;
            cursor: pointer;
            position: relative;
        }
        .dropzone:hover { border-color: #ff0000; }
        .dropzone input { position: absolute; top: 0; left: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
        textarea {
            width: 100%;
            height: 120px;
            background: #121212;
            border: 1px solid #444;
            border-radius: 6px;
            color: #fff;
            padding: 10px;
            box-sizing: border-box;
            font-family: monospace;
            font-size: 12px;
        }
        .btn {
            background: #cc0000;
            color: white;
            border: none;
            padding: 14px 24px;
            border-radius: 6px;
            font-weight: bold;
            font-size: 15px;
            width: 100%;
            cursor: pointer;
            margin-top: 14px;
        }
        .btn:hover { background: #ff0000; }
        .alert { background: #2a2010; border-left: 4px solid #ffba08; padding: 12px; border-radius: 4px; color: #ffd166; font-size: 13px; margin: 15px 0; }
    </style>
</head>
<body>
    <div class="setup-card">
        <h1>
            <svg style="width: 28px; height: 28px; fill: #ff0000;" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>
            Google Cloud Credentials Setup
        </h1>
        <p>
            To connect to your YouTube channel (<strong>shoaibgh473@gmail.com</strong>), provide your OAuth 2.0 Web Client credentials.
        </p>
        
        <div class="alert">
            Place <code>client_secret.json</code> in this folder (<code>d:\\y\\client_secret.json</code>), or upload / paste it below.
        </div>

        <form action="/api/save_credentials" method="POST" enctype="multipart/form-data">
            <div class="dropzone">
                <input type="file" name="secret_file" accept=".json" onchange="this.form.submit()">
                <div style="font-size: 15px; font-weight: 500;">Click to select client_secret.json</div>
                <div style="font-size: 12px; color: #888; margin-top: 4px;">Downloaded from Google Cloud Console</div>
            </div>

            <div style="text-align: center; color: #666; margin: 10px 0;">— OR PASTE JSON CONTENT —</div>

            <textarea name="secret_json" placeholder='{"web": {"client_id": "...", "client_secret": "..."}}'></textarea>
            <button type="submit" class="btn">Save & Authorize YouTube Account</button>
        </form>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_MAIN)

@app.route('/setup')
def setup():
    return render_template_string(HTML_SETUP)

@app.route('/api/save_credentials', methods=['POST'])
def save_credentials():
    secret_file = request.files.get('secret_file')
    secret_json = request.form.get('secret_json')

    if secret_file and secret_file.filename != '':
        secret_file.save(CLIENT_SECRETS_FILE)
    elif secret_json and secret_json.strip():
        with open(CLIENT_SECRETS_FILE, 'w') as f:
            f.write(secret_json.strip())
    else:
        return "No credentials provided. Please go back and provide client_secret.json.", 400

    return redirect('/authorize')

@app.route('/authorize')
def authorize():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        return redirect('/setup')

    redirect_uri = get_oauth_redirect_uri()
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

    # Prompt select_account so the user can choose ANY email or add a new account freely
    auth_params = {
        'access_type': 'offline',
        'include_granted_scopes': 'true',
        'prompt': 'select_account consent'
    }

    login_hint = request.args.get('login_hint')
    if login_hint:
        auth_params['login_hint'] = login_hint

    authorization_url, state = flow.authorization_url(**auth_params)
    session['state'] = state
    session['code_verifier'] = getattr(flow, 'code_verifier', None)
    return redirect(authorization_url)

@app.route('/switch_account')
def switch_account():
    # Clear session credentials so user can pick any new or existing Google account
    session.pop('credentials', None)
    session.pop('state', None)
    session.pop('code_verifier', None)
    return redirect('/authorize?prompt=select_account')

@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    redirect_uri = get_oauth_redirect_uri()
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        state=state,
        redirect_uri=redirect_uri
    )
    if 'code_verifier' in session and session['code_verifier']:
        flow.code_verifier = session['code_verifier']

    auth_response = request.url
    if (request.headers.get('X-Forwarded-Proto') == 'https' or request.is_secure) and auth_response.startswith('http://'):
        auth_response = 'https://' + auth_response[7:]

    flow.fetch_token(authorization_response=auth_response)
    credentials = flow.credentials

    # Fetch user email
    user_email = ""
    try:
        import requests
        u_res = requests.get('https://www.googleapis.com/oauth2/v2/userinfo', headers={'Authorization': f'Bearer {credentials.token}'}, timeout=5)
        if u_res.ok:
            user_email = u_res.json().get('email', '')
    except Exception as e:
        print(f"Userinfo fetch notice: {e}")

    creds_dict = {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

    # Fetch YouTube channels for this newly signed-in account
    channels_list = []
    try:
        yt = build('youtube', 'v3', credentials=credentials)
        res = yt.channels().list(mine=True, part='snippet,statistics').execute()
        for ch in res.get('items', []):
            snip = ch.get('snippet', {})
            st = ch.get('statistics', {})
            thumbs = snip.get('thumbnails', {})
            avatar_url = thumbs.get('default', {}).get('url') or thumbs.get('medium', {}).get('url') or thumbs.get('high', {}).get('url') or ''
            channels_list.append({
                'id': ch.get('id'),
                'title': snip.get('title', 'YouTube Creator'),
                'customUrl': snip.get('customUrl', ''),
                'avatar': avatar_url,
                'thumbnail_url': thumbs.get('default', {}).get('url', avatar_url),
                'subscriberCount': st.get('subscriberCount', '0'),
                'videoCount': st.get('videoCount', '0'),
                'viewCount': st.get('viewCount', '0')
            })
    except Exception as ye:
        print(f"Channels fetch during oauth notice: {ye}")

    account_key = save_user_account(user_email, creds_dict, channels_list)
    session.permanent = True
    session['active_account_key'] = account_key
    session['credentials'] = creds_dict
    session['user_email'] = user_email
    if channels_list:
        session['active_channel_id'] = channels_list[0]['id']
    else:
        session.pop('active_channel_id', None)

    try:
        with open(TOKEN_FILE, 'w') as f:
            json.dump(creds_dict, f)
    except Exception:
        pass

    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/authorize')

@app.route('/api/accounts')
def list_accounts():
    accounts = load_accounts_store()
    current_key = session.get('active_account_key', '')
    acc_list = []
    for k, v in accounts.items():
        acc_list.append({
            'key': k,
            'email': v.get('email', k),
            'channels': v.get('channels', []),
            'active_channel_id': v.get('active_channel_id', ''),
            'is_active': (k == current_key)
        })
    return jsonify({'accounts': acc_list, 'active_account_key': current_key})

@app.route('/api/switch_account/<path:account_key>', methods=['POST'])
def switch_active_account(account_key):
    accounts = load_accounts_store()
    account_key = account_key.lower().strip()
    if account_key not in accounts:
        return jsonify({'error': 'Account not found'}), 404

    acc = accounts[account_key]
    session['active_account_key'] = account_key
    session['credentials'] = acc['credentials']
    session['user_email'] = acc.get('email', '')
    session['active_channel_id'] = acc.get('active_channel_id')
    return jsonify({'success': True, 'account': account_key})

@app.route('/api/switch_channel/<channel_id>', methods=['POST'])
def switch_channel(channel_id):
    session['active_channel_id'] = channel_id
    return jsonify({'success': True, 'active_channel_id': channel_id})

@app.route('/api/channel')
def channel_info():
    creds = get_stored_credentials()
    default_avatar = 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="90" height="90" viewBox="0 0 90 90"><circle cx="45" cy="45" r="45" fill="%23282828"/><circle cx="45" cy="34" r="18" fill="%23aaaaaa"/><path d="M15 76 C 15 54, 75 54, 75 76" fill="%23aaaaaa"/></svg>'

    if not creds:
        return jsonify({
            'id': 'disconnected',
            'title': 'Connect Channel',
            'customUrl': '@connect',
            'avatar': default_avatar,
            'thumbnail_url': default_avatar,
            'subscriberCount': 0,
            'videoCount': 0,
            'viewCount': 0,
            'allChannels': [],
            'allAccounts': [],
            'userEmail': '',
            'is_authenticated': False
        })

    try:
        youtube = build('youtube', 'v3', credentials=creds)
        active_id = session.get('active_channel_id')
        items = []

        # 1. Fetch channel details using youtube.channels().list(mine=True, part='snippet,statistics')
        try:
            res = youtube.channels().list(mine=True, part='snippet,statistics').execute()
            items = res.get('items', [])
        except Exception as e:
            print(f"Error calling channels().list(mine=True): {e}")

        # 2. If active_id specified and not found in mine, try id query
        if active_id and not any(ch.get('id') == active_id for ch in items):
            try:
                id_res = youtube.channels().list(id=active_id, part='snippet,statistics').execute()
                if id_res.get('items'):
                    items = id_res.get('items') + items
            except Exception as e:
                print(f"Error querying channel by id {active_id}: {e}")

        user_email = session.get('user_email', '')
        if not user_email:
            try:
                import requests
                u_res = requests.get('https://www.googleapis.com/oauth2/v2/userinfo', headers={'Authorization': f'Bearer {creds.token}'}, timeout=3)
                if u_res.ok:
                    user_email = u_res.json().get('email', '')
                    session['user_email'] = user_email
            except Exception:
                pass

        accounts = load_accounts_store()
        current_key = session.get('active_account_key', '')
        connected_accounts = []
        for k, acc in accounts.items():
            connected_accounts.append({
                'key': k,
                'email': acc.get('email', k),
                'channels': acc.get('channels', []),
                'active_channel_id': acc.get('active_channel_id', ''),
                'is_active': (k == current_key)
            })

        if not items:
            # Check cached channels from user_accounts.json
            cached_channels = []
            account_key = session.get('active_account_key') or (user_email.lower().strip() if user_email else '')
            if account_key and account_key in accounts:
                cached_channels = accounts[account_key].get('channels', [])
            elif accounts:
                first_acc = next(iter(accounts.values()))
                cached_channels = first_acc.get('channels', [])

            if cached_channels:
                c_ch = cached_channels[0]
                return jsonify({
                    'id': c_ch.get('id', 'cached_channel'),
                    'title': c_ch.get('title', 'YouTube Creator'),
                    'customUrl': c_ch.get('customUrl', ''),
                    'avatar': c_ch.get('avatar', default_avatar),
                    'thumbnail_url': c_ch.get('thumbnail_url', c_ch.get('avatar', default_avatar)),
                    'subscriberCount': c_ch.get('subscriberCount', '0'),
                    'videoCount': c_ch.get('videoCount', '0'),
                    'viewCount': c_ch.get('viewCount', '0'),
                    'uploadsPlaylist': '',
                    'userEmail': user_email,
                    'has_channel': True,
                    'allChannels': cached_channels,
                    'allAccounts': connected_accounts
                })

            user_name = user_email.split('@')[0] if user_email else "YouTube User"
            user_avatar = f"https://ui-avatars.com/api/?name={user_name}&background=ff0000&color=ffffff&size=128"
            return jsonify({
                'id': 'no_channel',
                'title': user_name,
                'customUrl': f"@{user_name.lower().replace(' ', '')}",
                'avatar': user_avatar,
                'thumbnail_url': user_avatar,
                'subscriberCount': 0,
                'videoCount': 0,
                'viewCount': 0,
                'userEmail': user_email,
                'has_channel': False,
                'allChannels': [],
                'allAccounts': connected_accounts
            })

        # Match active channel or default to primary
        active_ch = None
        if active_id:
            for ch in items:
                if ch.get('id') == active_id:
                    active_ch = ch
                    break
        if not active_ch:
            active_ch = items[0]
            session['active_channel_id'] = active_ch.get('id')

        snippet = active_ch.get('snippet', {})
        stats = active_ch.get('statistics', {})
        thumbs = snippet.get('thumbnails', {})

        # Extract avatar URLs with priority: default -> medium -> high
        default_thumb = thumbs.get('default', {}).get('url', '')
        medium_thumb = thumbs.get('medium', {}).get('url', '')
        high_thumb = thumbs.get('high', {}).get('url', '')
        channel_avatar = default_thumb or medium_thumb or high_thumb or default_avatar

        # Build list of all channels
        all_channels = []
        for ch in items:
            snip = ch.get('snippet', {})
            st = ch.get('statistics', {})
            t = snip.get('thumbnails', {})
            c_avatar = t.get('default', {}).get('url') or t.get('medium', {}).get('url') or t.get('high', {}).get('url') or default_avatar
            all_channels.append({
                'id': ch.get('id'),
                'title': snip.get('title', 'YouTube Creator'),
                'customUrl': snip.get('customUrl', ''),
                'avatar': c_avatar,
                'thumbnail_url': t.get('default', {}).get('url', c_avatar),
                'subscriberCount': st.get('subscriberCount', '0'),
                'videoCount': st.get('videoCount', '0'),
                'viewCount': st.get('viewCount', '0')
            })

        # Persist fetched channel metrics to user_accounts.json for offline / quota exhaustion fallback
        try:
            acc_key = session.get('active_account_key') or (user_email.lower().strip() if user_email else '')
            if acc_key:
                acc_store = load_accounts_store()
                if acc_key in acc_store:
                    acc_store[acc_key]['channels'] = all_channels
                    acc_store[acc_key]['active_channel_id'] = active_ch.get('id')
                    save_accounts_store(acc_store)
                elif user_email:
                    acc_store[acc_key] = {
                        "email": user_email,
                        "credentials": session.get('credentials', {}),
                        "channels": all_channels,
                        "active_channel_id": active_ch.get('id'),
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    save_accounts_store(acc_store)
        except Exception as e:
            print(f"Notice: Failed to update channel cache in user_accounts: {e}")

        uploads_playlist = ''
        try:
            cd_res = youtube.channels().list(id=active_ch.get('id'), part='contentDetails').execute()
            if cd_res.get('items'):
                uploads_playlist = cd_res['items'][0].get('contentDetails', {}).get('relatedPlaylists', {}).get('uploads', '')
        except Exception:
            pass

        return jsonify({
            'id': active_ch.get('id'),
            'title': snippet.get('title', 'YouTube Creator'),
            'customUrl': snippet.get('customUrl') or ('@' + snippet.get('title', 'creator').lower().replace(' ', '')),
            'avatar': channel_avatar,
            'thumbnail_url': default_thumb or channel_avatar,
            'subscriberCount': stats.get('subscriberCount', '0'),
            'videoCount': stats.get('videoCount', '0'),
            'viewCount': stats.get('viewCount', '0'),
            'uploadsPlaylist': uploads_playlist,
            'userEmail': user_email,
            'has_channel': True,
            'allChannels': all_channels,
            'allAccounts': connected_accounts
        })
    except Exception as e:
        print(f"Error in /api/channel: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/recent_videos')
def recent_videos():
    creds = get_stored_credentials()
    if not creds:
        return jsonify([])
    try:
        youtube = build('youtube', 'v3', credentials=creds)
        active_id = session.get('active_channel_id')

        if active_id:
            ch_res = youtube.channels().list(id=active_id, part='contentDetails').execute()
        else:
            ch_res = youtube.channels().list(mine=True, part='contentDetails').execute()

        items = ch_res.get('items', [])
        if not items:
            return jsonify([])
        uploads_id = items[0]['contentDetails']['relatedPlaylists']['uploads']

        pl_res = youtube.playlistItems().list(
            playlistId=uploads_id,
            part='snippet,status',
            maxResults=8
        ).execute()

        video_list = []
        for item in pl_res.get('items', []):
            snip = item.get('snippet', {})
            video_id = snip.get('resourceId', {}).get('videoId')
            video_list.append({
                'id': video_id,
                'title': snip.get('title'),
                'publishedAt': snip.get('publishedAt'),
                'thumbnail': snip.get('thumbnails', {}).get('medium', {}).get('url', ''),
                'privacy': item.get('status', {}).get('privacyStatus', 'public').upper()
            })
        return jsonify(video_list)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==============================================
# GEMINI AI COPILOT & MULTIMODAL ENDPOINTS
# ==============================================

@app.route('/api/gemini/status')
def gemini_status():
    ch_id = get_active_channel_id_or_default()
    st = gemini_engine.get_gemini_status(ch_id)
    return jsonify(st)

@app.route('/api/gemini/config', methods=['POST'])
def gemini_save_config():
    data = request.get_json(force=True, silent=True) or {}
    api_key = data.get('api_key', '').strip()
    model = data.get('model', 'gemini-3.8-flash').strip()
    ch_id = (data.get('channel_id') or '').strip() or get_active_channel_id_or_default()
    if not api_key:
        return jsonify({'error': 'API key is required'}), 400
    res = gemini_engine.save_gemini_config(api_key, model, channel_id=ch_id)
    return jsonify(res)

@app.route('/api/channel/gemini_keys', methods=['GET'])
def get_channel_gemini_keys():
    try:
        ch_id = request.args.get('channel_id') or get_active_channel_id_or_default()
        pool_status = channel_key_store.get_channel_key_pool_status(ch_id)
        return jsonify({'success': True, 'channel_id': ch_id, 'pool': pool_status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/channel/gemini_keys/add', methods=['POST'])
def add_channel_gemini_key():
    try:
        data = request.get_json(force=True, silent=True) or {}
        ch_id = (data.get('channel_id') or '').strip() or get_active_channel_id_or_default()
        new_key = (data.get('api_key') or '').strip()
        verify = data.get('verify', True)
        if not new_key:
            return jsonify({'success': False, 'error': 'API key cannot be empty'}), 400
        ok, msg = channel_key_store.add_channel_key(ch_id, new_key, verify=verify)
        pool_status = channel_key_store.get_channel_key_pool_status(ch_id)
        return jsonify({'success': ok, 'message': msg, 'channel_id': ch_id, 'pool': pool_status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/channel/gemini_keys/delete', methods=['POST'])
def delete_channel_gemini_key():
    try:
        data = request.get_json(force=True, silent=True) or {}
        ch_id = (data.get('channel_id') or '').strip() or get_active_channel_id_or_default()
        index = int(data.get('index', -1))
        if index < 0 or index >= 10:
            return jsonify({'success': False, 'error': 'Invalid key slot index'}), 400
        ok, msg = channel_key_store.remove_channel_key(ch_id, index)
        pool_status = channel_key_store.get_channel_key_pool_status(ch_id)
        return jsonify({'success': ok, 'message': msg, 'channel_id': ch_id, 'pool': pool_status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/channel/gemini_keys/update', methods=['POST'])
def update_channel_gemini_key():
    try:
        data = request.get_json(force=True, silent=True) or {}
        ch_id = (data.get('channel_id') or '').strip() or get_active_channel_id_or_default()
        index = int(data.get('index', -1))
        new_key = (data.get('api_key') or '').strip()
        verify = data.get('verify', True)
        if index < 0 or index >= 10:
            return jsonify({'success': False, 'error': 'Invalid key slot index'}), 400
        if not new_key:
            return jsonify({'success': False, 'error': 'New API key cannot be empty'}), 400
        ok, msg = channel_key_store.update_channel_key(ch_id, index, new_key, verify=verify)
        pool_status = channel_key_store.get_channel_key_pool_status(ch_id)
        return jsonify({'success': ok, 'message': msg, 'channel_id': ch_id, 'pool': pool_status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/channel/gemini_keys/sync', methods=['POST'])
def sync_channel_gemini_keys():
    try:
        data = request.get_json(force=True, silent=True) or {}
        ch_id = (data.get('channel_id') or '').strip() or get_active_channel_id_or_default()
        client_keys = data.get('keys', [])
        if isinstance(client_keys, list) and client_keys:
            existing_keys = channel_key_store.get_channel_keys(ch_id)
            merged = list(existing_keys)
            for k in client_keys:
                if isinstance(k, str) and k.strip() and k.strip() not in merged and len(merged) < 10:
                    merged.append(k.strip())
            channel_key_store.save_channel_keys_to_db(ch_id, merged, mirror_backup=True)
        pool_status = channel_key_store.get_channel_key_pool_status(ch_id)
        return jsonify({'success': True, 'channel_id': ch_id, 'pool': pool_status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/gemini/analyze', methods=['POST'])
def gemini_analyze():
    video_file = request.files.get('video_file')
    instructions = request.form.get('instructions', '').strip()
    format_type = request.form.get('format_type', 'Short').strip()

    if not video_file or video_file.filename == '':
        return jsonify({'error': 'No video file provided for analysis'}), 400

    task_id = str(uuid.uuid4())
    safe_name = secure_filename(f"gemini_{task_id}_{video_file.filename}")
    video_path = os.path.join(UPLOAD_FOLDER, safe_name)
    video_file.save(video_path)

    try:
        ch_id = get_active_channel_id_or_default(request.form.get('channel_id'))
        metadata = gemini_engine.analyze_video_with_gemini(
            video_path,
            format_type=format_type,
            custom_instructions=instructions,
            channel_id=ch_id
        )
        metadata['video_filename'] = safe_name
        return jsonify(metadata)
    except Exception as e:
        print(f"Gemini analysis error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/save_thumbnail_frame', methods=['POST'])
def save_thumbnail_frame():
    image_file = request.files.get('image_file')
    label = request.form.get('label', 'Authentic Video Frame')
    timestamp = request.form.get('timestamp', '00:00')
    seconds = float(request.form.get('seconds', 0.0))
    aspect_ratio = request.form.get('aspect_ratio', '').strip() or None

    if not image_file or image_file.filename == '':
        return jsonify({'error': 'No frame image provided'}), 400

    filename = secure_filename(f"extracted_{uuid.uuid4().hex[:8]}_{image_file.filename}")
    res = gemini_engine.save_client_frame(
        image_file.read(),
        filename=filename,
        timestamp=timestamp,
        label=label,
        aspect_ratio=aspect_ratio
    )
    res['seconds'] = seconds
    return jsonify(res)

@app.route('/api/thumbnail_file/<filename>')
def serve_thumbnail(filename):
    return send_from_directory(gemini_engine.THUMBNAILS_DIR, secure_filename(filename))

@app.route('/api/gemini/chat', methods=['POST'])
def gemini_chat():
    data = request.get_json(force=True, silent=True) or {}
    message = data.get('message', '').strip()
    context = data.get('context', {})
    if not message:
        return jsonify({'error': 'Message is required'}), 400
    ch_id = get_active_channel_id_or_default(data.get('channel_id'))
    res = gemini_engine.chat_with_gemini(message, studio_context=context, channel_id=ch_id)
    return jsonify(res)

# ==============================================
# YOUTUBE CHUNKED UPLOAD PIPELINE
# ==============================================

def execute_youtube_upload(task_id, creds_dict, video_path, thumb_path, title, description, tags, privacy, category_id, made_for_kids):
    media = None
    try:
        creds = Credentials(**creds_dict)
        youtube = build('youtube', 'v3', credentials=creds)

        body = {
            'snippet': {
                'title': title,
                'description': description,
                'tags': tags,
                'categoryId': str(category_id)
            },
            'status': {
                'privacyStatus': privacy,
                'selfDeclaredMadeForKids': bool(made_for_kids)
            }
        }

        # 5MB chunk size for reliable resumable upload
        chunk_size = 5 * 1024 * 1024
        media = MediaFileUpload(video_path, chunksize=chunk_size, resumable=True)
        insert_request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )

        response = None
        max_retries = 10
        retry_count = 0

        while response is None:
            try:
                status, response = insert_request.next_chunk()
                if status:
                    progress_val = float(status.progress())
                    upload_tasks[task_id]['progress'] = progress_val
                    upload_tasks[task_id]['status'] = 'uploading'
                    upload_tasks[task_id]['status_text'] = f"Streaming to YouTube: {int(progress_val * 100)}% complete"
                retry_count = 0  # Reset retry count on successful chunk transmission
            except HttpError as err:
                if err.resp.status in [500, 502, 503, 504]:
                    retry_count += 1
                    if retry_count > max_retries:
                        raise err
                    sleep_time = min(2 ** retry_count, 60)
                    msg = f"Transient YouTube server error ({err.resp.status}). Auto-resuming in {sleep_time}s (attempt {retry_count}/{max_retries})..."
                    print(msg)
                    upload_tasks[task_id]['status_text'] = msg
                    time.sleep(sleep_time)
                else:
                    raise err
            except (socket.error, socket.timeout, ConnectionResetError, http.client.RemoteDisconnected, httplib2.ServerNotFoundError, ssl.SSLError, Exception) as net_err:
                retry_count += 1
                if retry_count > max_retries:
                    raise net_err
                sleep_time = min(2 ** retry_count, 60)
                msg = f"Network interruption detected. Resuming upload in {sleep_time}s (attempt {retry_count}/{max_retries})..."
                print(msg)
                upload_tasks[task_id]['status_text'] = msg
                time.sleep(sleep_time)

        video_id = response['id']
        upload_tasks[task_id]['video_id'] = video_id

        # Thumbnail processing
        if thumb_path and os.path.exists(thumb_path):
            upload_tasks[task_id]['status'] = 'processing_thumbnail'
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(thumb_path)
                ).execute()
            except Exception as te:
                print(f"Thumbnail upload notice (may require verified channel): {te}")

        upload_tasks[task_id]['status'] = 'completed'
        upload_tasks[task_id]['progress'] = 1.0

    except Exception as e:
        print(f"Upload error: {e}")
        upload_tasks[task_id]['status'] = 'error'
        upload_tasks[task_id]['error'] = str(e)
    finally:
        # Safe cleanup on Windows
        try:
            if media:
                del media
            import gc; gc.collect()
            time.sleep(0.3)
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)
            if video_path and os.path.exists(video_path):
                os.remove(video_path)
        except Exception as cle:
            print(f"Cleanup notice: {cle}")

@app.route('/api/upload_start', methods=['POST'])
def upload_start():
    creds = get_stored_credentials()
    if not creds:
        return jsonify({'error': 'Unauthorized'}), 401

    task_id = str(uuid.uuid4())

    video_file = request.files.get('video_file')
    existing_video_filename = request.form.get('existing_video_filename')

    video_path = None
    if video_file and video_file.filename != '':
        video_filename = secure_filename(f"{task_id}_{video_file.filename}")
        video_path = os.path.join(UPLOAD_FOLDER, video_filename)
        video_file.save(video_path)
    elif existing_video_filename:
        candidate_path = os.path.join(UPLOAD_FOLDER, secure_filename(existing_video_filename))
        if os.path.exists(candidate_path):
            video_path = candidate_path

    if not video_path or not os.path.exists(video_path):
        return jsonify({'error': 'No video file available for upload'}), 400

    thumb_path = None
    thumb_file = request.files.get('thumbnail_file')
    selected_thumbnail_filename = request.form.get('selected_thumbnail_filename')

    if thumb_file and thumb_file.filename != '':
        thumb_filename = secure_filename(f"thumb_{task_id}_{thumb_file.filename}")
        thumb_path = os.path.join(UPLOAD_FOLDER, thumb_filename)
        thumb_file.save(thumb_path)
    elif selected_thumbnail_filename:
        source_thumb = os.path.join(gemini_engine.THUMBNAILS_DIR, secure_filename(selected_thumbnail_filename))
        if os.path.exists(source_thumb):
            thumb_filename = secure_filename(f"thumb_{task_id}_{selected_thumbnail_filename}")
            thumb_path = os.path.join(UPLOAD_FOLDER, thumb_filename)
            shutil.copyfile(source_thumb, thumb_path)

    title = request.form.get('title', 'Untitled Video')
    description = request.form.get('description', '')
    raw_tags = request.form.get('tags', '')
    tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
    privacy = request.form.get('privacy', 'public')
    category_id = request.form.get('category_id', '22')
    made_for_kids = request.form.get('made_for_kids') in ['on', 'true', True]

    upload_tasks[task_id] = {
        'status': 'uploading',
        'progress': 0.0,
        'video_id': None,
        'error': None
    }

    creds_dict = {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    }

    thread = threading.Thread(
        target=execute_youtube_upload,
        args=(task_id, creds_dict, video_path, thumb_path, title, description, tags, privacy, category_id, made_for_kids)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'task_id': task_id})

@app.route('/api/upload_status/<task_id>')
def upload_status(task_id):
    task = upload_tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task)


if __name__ == '__main__':
    print("="*60)
    print("YouTube Creator Studio Pro + Gemini AI Copilot running at http://localhost:5000")
    print("Target Account: shoaibgh473@gmail.com")
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
