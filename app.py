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

clipper_jobs = {}

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
os.makedirs(os.path.join(UPLOAD_FOLDER, "clipper_shorts"), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, "clipper_temp"), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, "clipper_jobs"), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, "clipper_cuts"), exist_ok=True)

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

def get_active_channel_id_or_default() -> str:
    """Returns active channel ID from request, session, or accounts store, fallback to 'default'."""
    ch_id = None
    try:
        if request:
            ch_id = request.args.get('channel_id')
            if not ch_id and request.is_json:
                data = request.get_json(silent=True) or {}
                ch_id = data.get('channel_id')
            if not ch_id:
                ch_id = request.headers.get('X-Channel-Id') or request.form.get('channel_id')
    except Exception:
        pass

    if not ch_id and 'active_channel_id' in session and session['active_channel_id']:
        ch_id = session['active_channel_id']

    if not ch_id:
        try:
            acc_key = session.get('active_account_key')
            store = load_accounts_store()
            if acc_key and acc_key in store and store[acc_key].get('active_channel_id'):
                ch_id = store[acc_key]['active_channel_id']
            elif store:
                for acc in store.values():
                    if acc.get('active_channel_id'):
                        ch_id = acc['active_channel_id']
                        break
                    elif acc.get('channels') and len(acc['channels']) > 0:
                        ch_id = acc['channels'][0].get('id')
                        break
        except Exception:
            pass

    return str(ch_id).strip() if ch_id else "default"

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
    <title>YouTube Creator Studio Pro + Gemini AI</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
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
            min-width: 1200px;
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
        .mode-nav-tabs {
            display: flex;
            gap: 10px;
            background: var(--bg-surface);
            padding: 8px;
            border-radius: var(--card-radius);
            border: 1px solid var(--border-color);
        }
        .mode-tab {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            padding: 12px 18px;
            border-radius: 8px;
            border: none;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            color: var(--text-secondary);
            background: transparent;
            transition: all 0.2s;
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
            z-index: 1001;
            display: flex;
            flex-direction: column;
            transition: right 0.3s cubic-bezier(0.16, 1, 0.3, 1);
            pointer-events: none;
        }
        .chat-drawer.open { right: 0; pointer-events: auto; }
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
            top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0,0,0,0.75);
            backdrop-filter: blur(4px);
            z-index: 2000;
            align-items: center;
            justify-content: center;
            pointer-events: none;
        }
        .modal-overlay.active, .modal-overlay.open,
        .modal-overlay[style*="display: flex"],
        .modal-overlay[style*="display: block"],
        #clipperJobsModalOverlay[style*="display: flex"],
        #clipperJobsModalOverlay[style*="display: block"] {
            pointer-events: auto !important;
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
            }
            .sidebar {
                order: 2;
            }
            .workspace {
                order: 1;
                min-width: 0;
                width: 100%;
            }
            .mode-nav-tabs {
                flex-wrap: wrap;
                gap: 8px;
            }
            .mode-tab {
                min-width: 130px;
                padding: 10px 14px;
                font-size: 13px;
            }
            .top-navbar {
                padding: 0 14px;
            }
            .chat-drawer {
                width: 100vw;
                right: -100vw;
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
            
            <!-- Mode Switcher Tabs -->
            <div class="mode-nav-tabs">
                <button type="button" class="mode-tab active-ai" id="tabGeminiMode" onclick="switchWorkspaceTab('gemini')">
                    <span>✨</span>
                    <span>Gemini AI Studio Copilot</span>
                    <span class="tab-badge badge-ai">PRO MULTIMODAL</span>
                </button>
                <button type="button" class="mode-tab" id="tabClipperMode" onclick="switchWorkspaceTab('clipper')">
                    <span>🎬</span>
                    <span>YouTube URL to Shorts</span>
                    <span class="tab-badge" style="background: linear-gradient(135deg, #ff0055, #ff5500); color: white;">AUTO-CLIPPER</span>
                </button>
                <button type="button" class="mode-tab" id="tabTrimmerMode" onclick="switchWorkspaceTab('trimmer')">
                    <span>✂️</span>
                    <span>Timeline Video Trimmer</span>
                    <span class="tab-badge" style="background: linear-gradient(135deg, #06b6d4, #3b82f6); color: white;">ORIGINAL ASPECT RATIO</span>
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
            <div class="card" id="geminiStudioSection">
                <div class="ai-banner">
                    <div class="ai-banner-left">
                        <div class="ai-banner-title">
                            <span>✨ Gemini 3.8 Flash Multimodal Video Ingestion</span>
                        </div>
                        <div class="ai-banner-desc">
                            Upload any Short or Long-form video. Gemini analyzes spoken dialogue, narrative pacing, story hooks, and extracts 100% authentic character face thumbnails directly from your video stream.
                        </div>
                    </div>
                    <button class="btn-populate" id="btnOpenKeyModal" style="padding: 8px 14px; font-size: 13px;">
                        ⚙️ Configure API Key
                    </button>
                </div>

                <!-- Video Format Selector (Shorts vs Long Form) -->
                <div class="form-group" style="margin-bottom: 16px;">
                    <div class="form-label" style="margin-bottom: 8px;">
                        <span style="font-size: 14px; font-weight: 700; color: #f3e8ff;">🎯 Video Target Format & SEO Engine:</span>
                        <span style="font-size: 11px; color: #c084fc;">Select format to activate tailored algorithm rules</span>
                    </div>
                    <div class="format-selector-grid">
                        <div class="format-card active" id="formatCardShort" onclick="selectVideoFormat('Short')">
                            <div class="format-card-header">
                                <span class="format-icon">📱</span>
                                <span class="format-badge-pill">High Velocity Hook</span>
                            </div>
                            <div class="format-card-title">YouTube Shorts</div>
                            <div class="format-card-desc">&lt; 60s Vertical &bull; Curiosity hook &lt; 50 chars &bull; 2 viral hashtags &bull; 8-12 search tags</div>
                        </div>
                        <div class="format-card" id="formatCardLong" onclick="selectVideoFormat('Long')">
                            <div class="format-card-header">
                                <span class="format-icon">🎬</span>
                                <span class="format-badge-pill" style="background: rgba(56, 189, 248, 0.2); color: #38bdf8; border-color: rgba(56, 189, 248, 0.4);">Deep Search Ranking</span>
                            </div>
                            <div class="format-card-title">Long Form Video</div>
                            <div class="format-card-desc">Standard Landscape &bull; [Hook] | [High Volume Keyword] &bull; 3-paragraph summary &bull; 15-20 search tags</div>
                        </div>
                    </div>
                </div>

                <!-- Video Dropzone for Gemini -->
                <div class="form-group">
                    <div class="ai-dropzone" id="aiVideoDropzone">
                        <input type="file" id="aiVideoFileInput" accept="video/mp4,video/x-matroska,video/quicktime,video/webm">
                        <svg class="ai-icon" viewBox="0 0 24 24"><path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z"/></svg>
                        <div style="font-size: 18px; font-weight: 700; color: #f3e8ff; margin-bottom: 6px;">
                            Drag & Drop Video to Ingest with Gemini AI
                        </div>
                        <div style="font-size: 13px; color: #c084fc;">
                            Supports MP4, MKV, WebM, MOV &bull; Seconds to Hours &bull; Shorts & Long-Form
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
                    <span>Analyze Video & Generate Human-Grade Metadata</span>
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
                            <span>Extracting 100% Authentic Character Face Keyframes (Canvas & Stream Decoder)</span>
                        </div>
                        <div class="ai-step-item" id="step2">
                            <span class="step-circle">2</span>
                            <span>Uploading Video to Gemini 3.8 Flash Multimodal Engine</span>
                        </div>
                        <div class="ai-step-item" id="step3">
                            <span class="step-circle">3</span>
                            <span>Analyzing Spoken Dialogue, Audio Tone, Story Beats & Expressions</span>
                        </div>
                        <div class="ai-step-item" id="step4">
                            <span class="step-circle">4</span>
                            <span>Formulating High-CTR Viral Titles & Rich SEO Description</span>
                        </div>
                        <div class="ai-step-item" id="step5">
                            <span class="step-circle">5</span>
                            <span>Ranking Optimal Authentic Thumbnail & Finalizing Metadata</span>
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
                                    <div style="font-size: 11px; text-transform: uppercase; color: #c084fc; font-weight: 700; letter-spacing: 0.5px;">Target Format Strategy</div>
                                    <div id="aiTargetFormatBadge" style="font-size: 15px; font-weight: 800; color: #fff;">YouTube Shorts</div>
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
                                    <div style="font-size: 14px; font-weight: 700; color: #fff;">Gemini 2.5 Flash</div>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Title Variations -->
                    <div class="result-group">
                        <div class="result-group-title">
                            <span>Viral Title Recommendations (Click card to select)</span>
                            <span style="font-size: 12px; color: #a855f7;">Ranked by Estimated CTR & Punchline</span>
                        </div>
                        <div class="title-cards-grid" id="titleCardsGrid"></div>
                    </div>

                    <!-- Authentic Video Thumbnails Picker -->
                    <div class="result-group">
                        <div class="result-group-title">
                            <span>100% Authentic Character Face Thumbnail Picker</span>
                            <span style="font-size: 12px; color: #2ba640;">✔ Guaranteed 100% Match from Real Video Stream</span>
                        </div>
                        <p style="font-size: 12px; color: var(--text-secondary); margin: 0 0 10px 0;">
                            These frames were extracted directly from the uploaded video. Click any frame to set it as your official YouTube video thumbnail.
                        </p>
                        <div class="thumbnail-gallery-grid" id="thumbnailGalleryGrid"></div>

                        <!-- Thumbnail Directive Card -->
                        <div style="background: #171624; border: 1px solid rgba(255, 186, 8, 0.35); border-radius: 10px; padding: 16px; margin-top: 14px;">
                            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px;">
                                <span style="font-size: 13px; font-weight: 800; color: #ffba08; display: flex; align-items: center; gap: 8px;">
                                    <span>🎨</span> Thumbnail Directive & Art Direction
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
                            <span>Targeted Search & Discovery Keywords</span>
                            <span id="aiTagCount" style="font-size: 12px; color: var(--text-muted);">SEO tags</span>
                        </div>
                        <div class="tags-wrapper" id="aiTagsDisplay"></div>
                    </div>

                    <!-- Category & Insights -->
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 20px;">
                        <div class="stat-box" style="text-align: left; padding: 14px;">
                            <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Recommended Category</div>
                            <div id="aiCategoryName" style="font-size: 16px; font-weight: 700; color: white; margin-top: 4px;">People & Blogs</div>
                            <div id="aiCategoryId" style="font-size: 11px; color: #a855f7; margin-top: 2px;">ID: 22</div>
                        </div>
                        <div class="stat-box" style="text-align: left; padding: 14px;">
                            <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Audience & Format</div>
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
            <!-- 2. AI MOVIE-TO-SHORTS AUTO-CLIPPER ENGINE      -->
            <!-- ============================================== -->
            <div class="card" id="clipperSection" style="display: none;">
                <!-- Clipper Hero Header -->
                <div class="ai-banner" style="background: linear-gradient(135deg, rgba(255, 0, 85, 0.12), rgba(30, 10, 40, 0.6)); border-color: rgba(255, 0, 85, 0.4);">
                    <div class="ai-banner-left">
                        <div class="ai-banner-title" style="color: #ffe4e6;">
                            <span>🎬 AI Movie-to-Shorts Auto-Clipper Engine</span>
                        </div>
                        <div class="ai-banner-desc" style="color: #fda4af;">
                            Paste any YouTube movie or video link. Gemini analyzes narrative story beats, automatically segments into 100% copyright-safe dynamic multi-scene montages (8-14 fast-paced 3-6s sub-clips per Part, original movie audio 100% muted to bypass Content ID), reframes 16:9 to 9:16 vertical with actor face centering, and layers neural Hindi/English voiceover plus copyright-free cinematic tension background music.
                        </div>
                    </div>
                    <div style="display: flex; gap: 8px;">
                        <span class="tab-badge" style="background: rgba(16, 185, 129, 0.2); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.4); padding: 6px 12px; font-size: 11px;">100% COPYRIGHT SAFE</span>
                    </div>
                </div>

                <!-- URL Input Section -->
                <div style="background: var(--bg-elevated); border: 1px solid var(--border-color); border-radius: 10px; padding: 20px; margin-bottom: 20px;">
                    <label style="display: block; font-size: 14px; font-weight: 600; margin-bottom: 8px;">
                        🎥 YouTube Video / Movie URL
                    </label>
                    <div style="display: flex; gap: 10px;">
                        <input type="url" id="clipperUrlInput" placeholder="Paste YouTube Link (e.g. https://www.youtube.com/watch?v=... or https://youtu.be/...)" class="form-control" style="flex: 1; font-size: 14px; padding: 12px 16px;">
                        <button type="button" class="btn-populate" id="btnPasteUrl" style="padding: 0 18px; font-size: 13px;">
                            📋 Paste
                        </button>
                    </div>

                    <!-- Options Grid -->
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-top: 18px;">
                        <!-- Option 1: Max Shorts Count -->
                        <div style="background: var(--bg-input); padding: 14px; border-radius: 8px; border: 1px solid var(--border-color);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                <span style="font-size: 13px; font-weight: 600;">Max Shorts Count</span>
                                <span id="clipperMaxShortsBadge" style="font-size: 12px; font-weight: 700; color: #fda4af; background: rgba(255,0,85,0.15); padding: 2px 8px; border-radius: 6px;">5 Parts (AI Optimal)</span>
                            </div>
                            <input type="range" id="clipperMaxShortsSlider" min="1" max="20" value="5" style="width: 100%; accent-color: #ff0055;">
                            <div style="display: flex; justify-content: space-between; font-size: 11px; color: var(--text-muted); margin-top: 4px;">
                                <span>1 Part</span>
                                <span>10 Parts</span>
                                <span>20 Parts</span>
                            </div>
                        </div>

                        <!-- Option 2: Target Duration -->
                        <div style="background: var(--bg-input); padding: 14px; border-radius: 8px; border: 1px solid var(--border-color);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                <span style="font-size: 13px; font-weight: 600;">Target Duration</span>
                                <span style="font-size: 11px; color: #10b981;">Shorts (50-70s) &amp; Long (1-20m)</span>
                            </div>
                            <select id="clipperDurationSelect" class="form-control" style="width: 100%; padding: 8px 12px; font-size: 13px;">
                                <optgroup label="⚡ Shorts Montages (9:16 Vertical / Fast-Paced)">
                                    <option value="50">50 Seconds (8-10 Cuts, Fast Paced)</option>
                                    <option value="58" selected>58 Seconds (10-12 Cuts, Recommended)</option>
                                    <option value="70">70 Seconds (12-14 Cuts, Extended Climax)</option>
                                </optgroup>
                                <optgroup label="🎬 Long Video Montages (Cinematic Recaps)">
                                    <option value="60">1 Minute Montage (12-16 Cuts)</option>
                                    <option value="120">2 Minutes Montage (18-24 Cuts)</option>
                                    <option value="180">3 Minutes Montage (25-32 Cuts)</option>
                                    <option value="300">5 Minutes Montage (35-45 Cuts)</option>
                                    <option value="600">10 Minutes Deep Recap (50-70 Cuts)</option>
                                    <option value="900">15 Minutes Extended Feature (70-90 Cuts)</option>
                                    <option value="1200">20 Minutes Full Feature Montage (100+ Cuts)</option>
                                </optgroup>
                            </select>
                        </div>

                        <!-- Option 3: Voiceover Language -->
                        <div style="background: var(--bg-input); padding: 14px; border-radius: 8px; border: 1px solid var(--border-color);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                <span style="font-size: 13px; font-weight: 600;">Voiceover Language</span>
                                <span style="font-size: 11px; color: #2ba640;">Gemini 3.8 TTS Active</span>
                            </div>
                            <select id="clipperLanguageSelect" class="form-control" style="width: 100%; padding: 8px 12px; font-size: 13px;">
                                <option value="Hindi" selected>Hindi (हिन्दी - Cinematic Storyteller)</option>
                                <option value="English">English (Christopher / Hollywood Narrator)</option>
                            </select>
                        </div>

                        <!-- Option 4: Pipeline Format Mode -->
                        <div style="background: var(--bg-input); padding: 14px; border-radius: 8px; border: 1px solid var(--border-color);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                <span style="font-size: 13px; font-weight: 600;">Pipeline Format Mode</span>
                                <span style="font-size: 11px; color: #38bdf8;">Unbundled 4-Step</span>
                            </div>
                            <select id="clipperFormatModeSelect" class="form-control" style="width: 100%; padding: 8px 12px; font-size: 13px;">
                                <option value="standard_first" selected>Standard Preview First (Instant ~15s, 1-Click 9:16 Convert)</option>
                                <option value="auto_vertical">Auto-Convert to 9:16 Vertical (Auto Face-Centering)</option>
                            </select>
                        </div>
                    </div>

                    <!-- Gemini 3.8 Flash TTS Studio Card -->
                    <div style="margin-top: 18px; background: linear-gradient(135deg, rgba(168, 85, 247, 0.1), rgba(236, 72, 153, 0.05)); border: 1px solid rgba(168, 85, 247, 0.35); border-radius: 10px; padding: 16px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span style="font-size: 18px;">🎙️</span>
                                <div>
                                    <span style="font-size: 13px; font-weight: 700; color: #f3e8ff;">Gemini 3.8 Flash TTS Studio</span>
                                    <span style="font-size: 11px; color: #c084fc; margin-left: 6px; background: rgba(168, 85, 247, 0.2); padding: 1px 6px; border-radius: 4px;">Words-Per-Second Calibrated</span>
                                </div>
                            </div>
                            <div id="clipperWpsBadge" style="font-size: 11px; font-weight: 700; color: #a7f3d0; background: rgba(16, 185, 129, 0.2); border: 1px solid rgba(16, 185, 129, 0.4); padding: 3px 10px; border-radius: 12px;">
                                ⚡ Calibrated Pace: 2.23 Words/Sec (42.1s sample)
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px;">
                            <!-- Voice Selector -->
                            <div>
                                <label style="display: block; font-size: 11px; font-weight: 600; color: #e9d5ff; margin-bottom: 4px;">
                                    Voice Actor
                                </label>
                                <select id="clipperTtsVoiceSelect" class="form-control" style="width: 100%; padding: 7px 10px; font-size: 12px;">
                                    <option value="Kore" selected>Kore (Female - Firm, Dramatic Storyteller)</option>
                                    <option value="Fenrir">Fenrir (Male - Deep Movie Trailer Voice)</option>
                                    <option value="Puck">Puck (Male - Dynamic &amp; Expressive)</option>
                                    <option value="Algenib">Algenib (Male - Suspense &amp; Mystery)</option>
                                    <option value="Charon">Charon (Male - Dark &amp; Brooding Atmospheric)</option>
                                    <option value="Aoede">Aoede (Female - Sophisticated &amp; Clear)</option>
                                    <option value="Algieba">Algieba (Female - High-Tension Action)</option>
                                </select>
                            </div>

                            <!-- Tone Preset Selector -->
                            <div>
                                <label style="display: block; font-size: 11px; font-weight: 600; color: #e9d5ff; margin-bottom: 4px;">
                                    Tone Preset
                                </label>
                                <select id="clipperTtsToneSelect" class="form-control" style="width: 100%; padding: 7px 10px; font-size: 12px;">
                                    <option value="Suspense / Thriller" selected>Suspense / Thriller (Tense dramatic pauses)</option>
                                    <option value="Movie Trailer">Movie Trailer (Booming cinematic delivery)</option>
                                    <option value="Narrative Deep">Narrative Deep (Rich storytelling baritone)</option>
                                    <option value="Fast-Paced Action">Fast-Paced Action (Urgent rapid pace)</option>
                                    <option value="Emotional Drama">Emotional Drama (Poignant heartfelt narrative)</option>
                                </select>
                            </div>

                            <!-- Buttons & Audio Player -->
                            <div style="display: flex; flex-direction: column; justify-content: flex-end; gap: 6px;">
                                <div style="display: flex; gap: 8px;">
                                    <button type="button" class="btn-populate" id="btnPreviewTtsVoice" style="flex: 1; padding: 7px 12px; font-size: 12px; background: rgba(168, 85, 247, 0.25); border: 1px solid #a855f7; color: #f3e8ff;">
                                        <span id="btnPreviewTtsIcon">🔊</span> <span id="btnPreviewTtsText">Test Voice</span>
                                    </button>
                                    <button type="button" class="btn-populate" id="btnCalibrateWps" style="flex: 1; padding: 7px 12px; font-size: 12px; background: rgba(16, 185, 129, 0.2); border: 1px solid #10b981; color: #a7f3d0;">
                                        <span id="btnCalibrateIcon">⚡</span> <span id="btnCalibrateText">Calibrate (100w)</span>
                                    </button>
                                </div>
                                <audio id="clipperTtsAudioPlayer" controls style="display: none; width: 100%; height: 28px; margin-top: 4px;"></audio>
                            </div>
                        </div>
                    </div>

                    <!-- Action Buttons -->
                    <div style="display: flex; gap: 12px; margin-top: 20px; flex-wrap: wrap;">
                        <button type="button" class="btn-upload" id="btnAnalyzeClipper" style="flex: 1; background: linear-gradient(135deg, #ff0055, #9333ea);">
                            <span id="clipperBtnIcon">🚀</span>
                            <span id="clipperBtnText">Analyze Narrative &amp; Plan Chronological Shorts</span>
                        </button>
                        <button type="button" class="btn-populate" id="btnViewSavedJobs" style="background: rgba(255,255,255,0.06); border: 1px solid var(--border-color); padding: 0 18px; font-size: 13px; display: inline-flex; align-items: center; gap: 6px;">
                            <span>📂 Checkpoints / Saved Jobs</span>
                        </button>
                    </div>
                </div>

                <!-- Gemini Quota / Pause Alert Banner -->
                <div id="clipperQuotaBanner" style="display: none; background: rgba(245, 158, 11, 0.12); border: 1px solid #f59e0b; border-radius: 8px; padding: 16px 20px; margin-bottom: 20px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
                        <div style="display: flex; align-items: flex-start; gap: 12px; max-width: 720px;">
                            <span style="font-size: 24px;">⚠️</span>
                            <div>
                                <div style="font-weight: 700; font-size: 14px; color: #fbbf24;" id="clipperQuotaBannerTitle">Gemini API Quota Limit Reached (429)</div>
                                <div style="font-size: 12px; color: #fde68a; margin-top: 4px; line-height: 1.4;" id="clipperQuotaBannerDesc">
                                    Your progress has been safely saved to a local disk checkpoint. You can update your Gemini API key in API Settings or wait for quota reset, then click <strong>Resume Job</strong> to continue from the exact scene without re-analyzing!
                                </div>
                            </div>
                        </div>
                        <div style="display: flex; gap: 8px; align-items: center;">
                            <button type="button" class="btn-populate" id="btnResumeClipperJob" style="background: linear-gradient(135deg, #f59e0b, #d97706); color: #000; font-weight: 700; border: none; padding: 9px 18px; font-size: 13px;">
                                ▶️ Resume Job
                            </button>
                            <button type="button" class="btn-populate" id="btnOpenKeyModalFromClipper" style="background: rgba(255,255,255,0.08); border: 1px solid #f59e0b; color: #fbbf24; padding: 9px 14px; font-size: 13px;">
                                ⚙️ Update API Key
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Live Analysis Progress Indicator -->
                <div id="clipperAnalysisProgress" style="display: none; background: #1a1020; border: 1px solid #ff0055; border-radius: 8px; padding: 18px; margin-bottom: 20px;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <div class="spinner" style="border-top-color: #ff0055;"></div>
                        <div>
                            <div id="clipperProgressStep" style="font-weight: 600; font-size: 14px; color: #ffe4e6;">Extracting YouTube video streams and chapters...</div>
                            <div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">Gemini is analyzing dramatic turning points and writing chronological storytelling scripts...</div>
                        </div>
                    </div>
                </div>

                <!-- Video Metadata Preview Box (Shown after analysis) -->
                <div id="clipperMovieMetaCard" style="display: none; background: var(--bg-elevated); border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; margin-bottom: 20px;">
                    <div style="display: flex; gap: 16px; align-items: center;">
                        <img id="clipperMovieThumb" src="" alt="Thumbnail" style="width: 120px; aspect-ratio: 16/9; object-fit: cover; border-radius: 6px;">
                        <div style="flex: 1;">
                            <h4 id="clipperMovieTitle" style="margin: 0 0 6px 0; font-size: 15px; font-weight: 600;">Movie Title</h4>
                            <div style="display: flex; gap: 12px; font-size: 12px; color: var(--text-muted); flex-wrap: wrap;">
                                <span id="clipperMovieDuration">⏱️ Duration: 00:00:00</span>
                                <span id="clipperMovieChannel">👤 Channel</span>
                                <span id="clipperMoviePartsCount" style="color: #ff0055; font-weight: 700;">🎬 5 Chronological Shorts Planned</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Batch Operations Bar -->
                <div id="clipperBatchBar" style="display: none; justify-content: space-between; align-items: center; background: #1e122b; border: 1px solid rgba(168, 85, 247, 0.4); border-radius: 8px; padding: 14px 18px; margin-bottom: 20px; flex-wrap: wrap; gap: 12px;">
                    <div>
                        <div style="font-size: 14px; font-weight: 700; color: #f3e8ff;">Ready to Generate Vertical Shorts</div>
                        <div style="font-size: 12px; color: var(--text-muted);">Downloads exact clips (no full download), tracks faces, reframes 9:16, and records voiceover.</div>
                    </div>
                    <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                        <button type="button" class="btn-populate" id="btnResumeBatchShorts" style="display: none; background: linear-gradient(135deg, #f59e0b, #d97706); color: #000; font-weight: 700; border: none; padding: 10px 18px; font-size: 13px;">
                            ▶️ Resume Job
                        </button>
                        <button type="button" class="btn-populate" id="btnGenerateAllShorts" style="background: linear-gradient(135deg, #a855f7, #ec4899); border: none;">
                            ⚡ Generate All Parts Sequentially
                        </button>
                        <button type="button" class="btn-upload" id="btnUploadAllShorts" style="display: none; padding: 10px 18px; font-size: 13px; width: auto; background: var(--accent-red);">
                            🚀 Upload All Parts to YouTube
                        </button>
                    </div>
                </div>

                <!-- Batch Progress Bar (While generating parts) -->
                <div id="clipperBatchProgressCard" style="display: none; background: #161224; border: 1px solid #a855f7; border-radius: 8px; padding: 16px; margin-bottom: 20px;">
                    <div style="display: flex; justify-content: space-between; font-size: 13px; font-weight: 600; margin-bottom: 8px;">
                        <span id="clipperBatchStepText">Processing Shorts...</span>
                        <span id="clipperBatchPercentText" style="color: #a855f7;">0%</span>
                    </div>
                    <div style="width: 100%; height: 8px; background: #2a2238; border-radius: 4px; overflow: hidden;">
                        <div id="clipperBatchProgressBar" style="width: 0%; height: 100%; background: linear-gradient(90deg, #ff0055, #a855f7); transition: width 0.4s ease;"></div>
                    </div>
                </div>

                <!-- Chronological Queue / Scenes Container -->
                <div id="clipperQueueContainer" style="display: none;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                        <h3 style="font-size: 16px; font-weight: 700; display: flex; align-items: center; gap: 8px; margin: 0;">
                            <span>📋 Chronological Parts Queue</span>
                            <span id="clipperQueueBadge" style="font-size: 11px; background: #2a1b38; color: #c084fc; padding: 2px 8px; border-radius: 10px;">0 Parts</span>
                        </h3>
                        <span style="font-size: 12px; color: var(--text-muted);">Strict Story Timeline Order (Part 1 ➔ Part N)</span>
                    </div>
                    <div id="clipperScenesGrid" style="display: flex; flex-direction: column; gap: 16px;">
                        <!-- Populated dynamically with scene cards -->
                    </div>
                </div>

                <!-- Saved Jobs Modal Overlay -->
                <div id="clipperJobsModalOverlay" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.75); z-index: 9999; align-items: center; justify-content: center; padding: 20px; pointer-events: none;">
                    <div style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 12px; width: 100%; max-width: 680px; max-height: 80vh; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,0.6);">
                        <div style="display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; border-bottom: 1px solid var(--border-color); background: var(--bg-elevated);">
                            <h3 style="margin: 0; font-size: 15px; font-weight: 700; display: flex; align-items: center; gap: 8px;">
                                <span>📂 Saved Clipper Checkpoints</span>
                            </h3>
                            <button type="button" id="btnCloseJobsModal" style="background: none; border: none; font-size: 22px; color: var(--text-muted); cursor: pointer; line-height: 1;">&times;</button>
                        </div>
                        <div id="clipperJobsListContainer" style="padding: 16px 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; max-height: 60vh;">
                            <div style="text-align: center; color: var(--text-muted); padding: 30px;">Loading saved checkpoints...</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ============================================== -->
            <!-- 3. TIMELINE VIDEO TRIMMER & SLICER (ORIGINAL RESOLUTION) -->
            <!-- ============================================== -->
            <div class="card" id="trimmerSection" style="display: none; padding: 24px; background: var(--bg-surface); border-radius: var(--card-radius); border: 1px solid var(--border-color);">
                
                <!-- Hero Banner -->
                <div style="background: linear-gradient(135deg, rgba(6, 182, 212, 0.12), rgba(59, 130, 246, 0.12)); border: 1px solid rgba(6, 182, 212, 0.35); border-radius: 12px; padding: 18px 24px; margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px;">
                    <div>
                        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 6px;">
                            <span style="font-size: 24px;">✂️</span>
                            <h3 style="margin: 0; font-size: 18px; font-weight: 700; color: #e0f2fe;">Timeline Video Trimmer &amp; Narrative Slicer</h3>
                            <span style="background: linear-gradient(135deg, #06b6d4, #3b82f6); color: white; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 4px;">100% NATIVE RESOLUTION</span>
                            <span style="background: rgba(16, 185, 129, 0.2); color: #6ee7b7; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 4px; border: 1px solid rgba(16, 185, 129, 0.3);">NO 9:16 RESIZING</span>
                        </div>
                        <p style="margin: 0; font-size: 13px; color: #94a3b8; max-width: 820px; line-height: 1.45;">
                            Upload any length video (10m to 2h+). Keep 100% of your video's original resolution and aspect ratio (no vertical crop, no distortion).
                            Manually split at any second, delete unwanted filler, or let Gemini auto-detect dramatic story scenes and stitch only keeper clips seamlessly in real time.
                        </p>
                    </div>
                </div>

                <!-- Cinema Explainer Copilot Card (Story-First Dynamic Cutting) -->
                <div style="background: linear-gradient(135deg, rgba(30, 27, 75, 0.85), rgba(15, 23, 42, 0.98)); border: 1px solid #7c3aed; border-radius: 12px; padding: 20px 24px; margin-bottom: 24px; box-shadow: 0 4px 24px rgba(124, 58, 237, 0.2);">
                    <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 16px;">
                        <div style="display: flex; align-items: center; gap: 12px;">
                            <span style="font-size: 28px;">🎬</span>
                            <div>
                                <h3 style="margin: 0; font-size: 18px; font-weight: 700; color: #ede9fe;">Cinema Explainer Storyboard Copilot (Pure Story-First Dynamic Cutting)</h3>
                                <p style="margin: 0; font-size: 12px; color: #a78bfa;">Gemini full editorial autonomy &bull; Organic 8-25 min pacing &bull; Micro-cuts (2-5s) &amp; Medium cuts (6-12s) &bull; Calibrated Hindi voice sync</p>
                            </div>
                        </div>
                        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                            <span style="background: rgba(124, 58, 237, 0.25); color: #c4b5fd; font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 6px; border: 1px solid rgba(124, 58, 237, 0.4);">DYNAMIC 8-25 MIN PACING</span>
                            <span style="background: rgba(6, 182, 212, 0.2); color: #67e8f9; font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 6px; border: 1px solid rgba(6, 182, 212, 0.3);">NO FIXED CLIP SLOTS</span>
                            <span style="background: rgba(16, 185, 129, 0.2); color: #6ee7b7; font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 6px; border: 1px solid rgba(16, 185, 129, 0.3);">WPS HINDI SYNC</span>
                        </div>
                    </div>

                    <!-- Explainer Input Controls Grid -->
                    <div style="display: grid; grid-template-columns: 2fr 1fr 1fr 1fr auto; gap: 10px; align-items: end; margin-bottom: 14px;">
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: #cbd5e1; display: block; margin-bottom: 4px;">Movie YouTube URL:</label>
                            <input type="text" id="trimmerExplainerYtUrl" class="form-control" style="width: 100%; padding: 8px 12px; font-size: 13px;" placeholder="https://www.youtube.com/watch?v=...">
                        </div>
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: #cbd5e1; display: block; margin-bottom: 4px;">Explainer Runtime Pacing:</label>
                            <select id="trimmerExplainerDuration" class="form-control" style="width: 100%; padding: 8px 10px; font-size: 12px;">
                                <option value="dynamic" selected>🎬 Natural Story Flow (8 to 25 Min Flexible)</option>
                                <option value="600">⚡ Compact Explainer (~10 Min)</option>
                                <option value="900">🍿 Medium Deep Dive (~15 Min)</option>
                                <option value="1200">🔥 Full Epic Explainer (~20 Min)</option>
                            </select>
                        </div>
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: #cbd5e1; display: block; margin-bottom: 4px;">Voice Character:</label>
                            <select id="trimmerExplainerVoice" class="form-control" style="width: 100%; padding: 8px 10px; font-size: 12px;">
                                <option value="Kore" selected>Kore (Hindi Deep Male)</option>
                                <option value="Fenrir">Fenrir (Dramatic Intense)</option>
                                <option value="Aoede">Aoede (Expressive Female)</option>
                                <option value="Puck">Puck (Fast-Paced Punch)</option>
                                <option value="Charon">Charon (Deep Mysterious)</option>
                            </select>
                        </div>
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: #cbd5e1; display: block; margin-bottom: 4px;">Tone Style:</label>
                            <select id="trimmerExplainerTone" class="form-control" style="width: 100%; padding: 8px 10px; font-size: 12px;">
                                <option value="Narrative Deep Storytelling" selected>Narrative Deep Storytelling</option>
                                <option value="Suspense / Thriller">Suspense / Thriller</option>
                                <option value="Movie Trailer Dramatic">Movie Trailer Dramatic</option>
                                <option value="Emotional Cinema Drama">Emotional Cinema Drama</option>
                            </select>
                        </div>
                        <div>
                            <button type="button" id="btnTrimmerPlanExplainer" class="btn-upload" style="background: linear-gradient(135deg, #7c3aed, #0284c7); padding: 9px 20px; font-size: 13.5px; font-weight: 700; white-space: nowrap; height: 38px; display: flex; align-items: center; gap: 8px; border-radius: 6px; box-shadow: 0 0 14px rgba(124, 58, 237, 0.4);">
                                <span id="btnPlanExplainerIcon">🎬</span>
                                <span id="btnPlanExplainerText">Generate Cinema Explainer Storyboard</span>
                            </button>
                        </div>
                    </div>

                    <!-- Storyboard Results & 4-Phase Visual Breakdown (Initially Hidden) -->
                    <div id="trimmerExplainerStoryboardContainer" style="display: none; margin-top: 16px; border-top: 1px solid rgba(124, 58, 237, 0.3); padding-top: 16px;">
                        <!-- Movie Metadata Header -->
                        <div style="background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
                            <div style="display: flex; align-items: center; gap: 14px;">
                                <img id="trimmerExplainerThumb" src="" alt="Thumbnail" style="width: 72px; height: 44px; object-fit: cover; border-radius: 4px; border: 1px solid #334155; display: none;">
                                <div>
                                    <div id="trimmerExplainerTitle" style="font-size: 14px; font-weight: 700; color: #f8fafc;">Movie Title</div>
                                    <div id="trimmerExplainerMetaSub" style="font-size: 11px; color: #94a3b8;">Runtime: 02:14:41 &bull; Channel: Cinema Channel</div>
                                </div>
                            </div>
                            <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                                <span id="trimmerExplainerBadgeDuration" style="background: rgba(168, 85, 247, 0.2); border: 1px solid #a855f7; color: #c084fc; font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 12px;">Duration: 14m 32s</span>
                                <span id="trimmerExplainerBadgeClips" style="background: rgba(6, 182, 212, 0.15); border: 1px solid #06b6d4; color: #38bdf8; font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 12px;">Dynamic Cuts: 52</span>
                                <span id="trimmerExplainerBadgeWps" style="background: rgba(234, 179, 8, 0.15); border: 1px solid #eab308; color: #fde047; font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 12px;">WPS: 2.35 w/s</span>
                                <span id="trimmerExplainerBadgeWords" style="background: rgba(16, 185, 129, 0.15); border: 1px solid #10b981; color: #6ee7b7; font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 12px;">Words: 2049 words</span>
                                <button type="button" id="btnInjectExplainerCuts" class="btn-populate" style="background: linear-gradient(135deg, #10b981, #059669); border: none; color: white; padding: 6px 14px; font-size: 12px; font-weight: 700; border-radius: 6px; display: flex; align-items: center; gap: 6px;">
                                    <span>⚡</span> <span>Inject All Cuts into Timeline</span>
                                </button>
                            </div>
                        </div>

                        <!-- Summary quote -->
                        <div id="trimmerExplainerSummary" style="font-size: 12px; color: #cbd5e1; font-style: italic; background: rgba(255,255,255,0.02); border-left: 3px solid #7c3aed; padding: 8px 14px; margin-bottom: 14px; border-radius: 0 6px 6px 0;"></div>

                        <!-- 4-Phase Storyboard Dynamic Grid -->
                        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px;" id="trimmerExplainerPhasesGrid">
                            <!-- Populated dynamically by renderExplainerStoryboardUI -->
                        </div>

                        <!-- Auto-Injection Guidance Banner -->
                        <div id="trimmerExplainerAutoInjectNotice" style="margin-top: 12px; padding: 10px 14px; background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.35); border-radius: 6px; font-size: 12px; color: #a7f3d0; display: flex; align-items: center; gap: 8px;">
                            <span>💡</span>
                            <span><b>Instant Auto-Cut:</b> Drag &amp; drop or select your movie file below. All narrative keeper cuts and calibrated Hindi voiceover script will be automatically injected into the timeline!</span>
                        </div>
                    </div>
                </div>

                <!-- Video Input / Source Selector -->
                <div style="background: var(--bg-elevated); border: 1px dashed #0284c7; border-radius: 10px; padding: 20px; margin-bottom: 20px; text-align: center;" id="trimmerDropzone">
                    <input type="file" id="trimmerVideoFileInput" accept="video/mp4,video/x-matroska,video/quicktime,video/webm" style="display: none;">
                    <div style="display: flex; flex-direction: column; align-items: center; gap: 8px;">
                        <span style="font-size: 36px;">🎬</span>
                        <div style="font-size: 15px; font-weight: 600; color: #f0f9ff;">Drag &amp; Drop Video Here, or <button type="button" class="btn-populate" id="btnBrowseTrimmerFile" style="padding: 4px 12px; font-size: 13px; display: inline-block;">Browse Local File</button></div>
                        <div style="font-size: 12px; color: var(--text-muted);">Supports MP4, MKV, MOV, WEBM &bull; Any duration from 10 minutes to 2+ hours</div>
                        <div id="trimmerFileInfoBadge" style="display: none; margin-top: 10px; padding: 8px 16px; background: rgba(6, 182, 212, 0.15); border: 1px solid #06b6d4; border-radius: 20px; font-size: 13px; color: #bae6fd; font-weight: 600;"></div>
                    </div>
                </div>

                <!-- Main HTML5 Video Player Area -->
                <div style="position: relative; background: #000; border-radius: 10px; overflow: hidden; border: 1px solid var(--border-color); margin-bottom: 16px;">
                    <video id="trimmerPlayer" playsinline preload="auto" style="width: 100%; max-height: 480px; display: block; object-fit: contain; margin: 0 auto; background: #000;"></video>
                    
                    <!-- Live Overlay HUD -->
                    <div style="position: absolute; top: 12px; left: 14px; display: flex; gap: 8px; z-index: 5;">
                        <span id="trimmerHudClipBadge" style="background: rgba(0,0,0,0.75); color: #38bdf8; font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 6px; border: 1px solid rgba(56, 189, 248, 0.4); backdrop-filter: blur(4px);">
                            Clip 1 of 1
                        </span>
                        <span id="trimmerHudResBadge" style="background: rgba(0,0,0,0.75); color: #a7f3d0; font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 6px; border: 1px solid rgba(16, 185, 129, 0.4); backdrop-filter: blur(4px);">
                            Native Resolution
                        </span>
                    </div>

                    <div style="position: absolute; top: 12px; right: 14px; display: flex; gap: 8px; z-index: 5;">
                        <span id="trimmerHudTimeBadge" style="background: rgba(0,0,0,0.75); color: #fff; font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 6px; border: 1px solid rgba(255, 255, 255, 0.2); backdrop-filter: blur(4px);">
                            00:00:00 / 00:00:00
                        </span>
                    </div>
                </div>

                <!-- Precision Transport & Editing Toolbar -->
                <div style="background: var(--bg-elevated); border: 1px solid var(--border-color); border-radius: 8px; padding: 12px 18px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
                    <!-- Left: Manual Cut & Delete Tools -->
                    <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                        <button type="button" id="btnTrimmerSplit" class="btn-upload" style="background: linear-gradient(135deg, #0284c7, #2563eb); padding: 8px 16px; font-size: 13px; font-weight: 700; display: flex; align-items: center; gap: 6px;">
                            <span>✂️</span> <span>Cut / Split at Playhead</span>
                        </button>
                        <button type="button" id="btnTrimmerDeleteClip" class="btn-populate" style="background: rgba(239, 68, 68, 0.15); border: 1px solid #ef4444; color: #fca5a5; padding: 8px 14px; font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 6px;">
                            <span>🗑️</span> <span>Delete Active Clip</span>
                        </button>
                        <button type="button" id="btnTrimmerReset" class="btn-populate" style="background: rgba(255,255,255,0.06); border: 1px solid var(--border-color); padding: 8px 12px; font-size: 12px; color: var(--text-secondary);">
                            <span>🔄</span> <span>Reset All</span>
                        </button>
                    </div>

                    <!-- Center: Frame Stepping / Transport -->
                    <div style="display: flex; gap: 6px; align-items: center;">
                        <button type="button" id="btnTrimmerPrevClip" class="btn-populate" style="padding: 6px 10px; font-size: 12px;" title="Previous Keeper Clip">⏮️</button>
                        <button type="button" id="btnTrimmerStepBack" class="btn-populate" style="padding: 6px 10px; font-size: 12px;" title="-1 Second">⏪ -1s</button>
                        <button type="button" id="btnTrimmerPlayPause" class="btn-populate" style="padding: 6px 14px; font-size: 13px; font-weight: 700; background: rgba(6, 182, 212, 0.2); border-color: #06b6d4; color: #38bdf8;">▶️ Play</button>
                        <button type="button" id="btnTrimmerStepFwd" class="btn-populate" style="padding: 6px 10px; font-size: 12px;" title="+1 Second">+1s ⏩</button>
                        <button type="button" id="btnTrimmerNextClip" class="btn-populate" style="padding: 6px 10px; font-size: 12px;" title="Next Keeper Clip">⏭️</button>
                    </div>

                    <!-- Right: Gemini Auto-Cut Trigger -->
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <select id="trimmerFocusSelect" class="form-control" style="width: auto; padding: 6px 10px; font-size: 12px;">
                            <option value="Key Dramatic Highlights">✨ Dramatic Turning Points</option>
                            <option value="Action &amp; Climax Moments">💥 Action &amp; Climax Scenes</option>
                            <option value="Dialogue &amp; Story Arc">🗣️ Dialogue &amp; Storyline</option>
                            <option value="Viral 60s Trailer">🔥 Viral 60s Trailer</option>
                            <option value="5-10 Min Summary">🎬 5-10 Min Summary</option>
                        </select>
                        <select id="trimmerTargetDurSelect" class="form-control" style="width: auto; padding: 6px 10px; font-size: 12px;">
                            <option value="0">Auto Length</option>
                            <option value="60">1 Minute</option>
                            <option value="180">3 Minutes</option>
                            <option value="300" selected>5 Minutes</option>
                            <option value="600">10 Minutes</option>
                        </select>
                        <button type="button" id="btnTrimmerGeminiAutoCut" class="btn-populate" style="background: linear-gradient(135deg, #a855f7, #ec4899); border: none; color: white; padding: 8px 14px; font-size: 13px; font-weight: 700; display: flex; align-items: center; gap: 6px; box-shadow: 0 0 10px rgba(168, 85, 247, 0.4);">
                            <span>🤖</span> <span>Gemini Auto-Cut &amp; Trim</span>
                        </button>
                    </div>
                </div>

                <!-- Interactive Visual Timeline Track Container -->
                <div style="background: #141414; border: 1px solid var(--border-color); border-radius: 8px; padding: 14px 16px; margin-bottom: 16px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-size: 12px; color: var(--text-muted);">
                        <span>Timeline Track &bull; Click to scrub &bull; Keeper clips highlighted</span>
                        <span id="trimmerTimelinePlayheadTime" style="color: #38bdf8; font-weight: 700;">Playhead: 00:00:00</span>
                    </div>

                    <!-- Visual Timeline Bar -->
                    <div id="trimmerTimelineTrack" style="position: relative; width: 100%; height: 50px; background: #222; border-radius: 6px; overflow: hidden; cursor: pointer; border: 1px solid #444; user-select: none;">
                        <div id="trimmerClipsContainer" style="position: absolute; inset: 0;"></div>
                        <!-- Playhead Cursor Line -->
                        <div id="trimmerPlayhead" style="position: absolute; top: 0; bottom: 0; left: 0%; width: 3px; background: #ff0055; box-shadow: 0 0 8px #ff0055; z-index: 10; pointer-events: none; transition: left 0.05s linear;">
                            <div style="width: 11px; height: 11px; background: #ff0055; border-radius: 50%; position: absolute; top: -4px; left: -4px;"></div>
                        </div>
                    </div>

                    <!-- Timeline Stats Bar -->
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 10px; font-size: 12px; flex-wrap: wrap; gap: 10px; padding: 6px 10px; background: rgba(255,255,255,0.02); border-radius: 6px;">
                        <span id="trimmerStatOrig">⏱️ Original Length: 00:00</span>
                        <span id="trimmerStatKept" style="color: #38bdf8; font-weight: 700;">✂️ Kept Montage: 00:00</span>
                        <span id="trimmerStatRemoved" style="color: #f43f5e; font-weight: 700;">🗑️ Filler Discarded: 00:00 (0%)</span>
                        <span id="trimmerStatCount" style="color: #a855f7; font-weight: 700;">🎬 Keeper Clips: 1</span>
                    </div>
                </div>

                <!-- Keeper Clips Deck -->
                <div style="background: var(--bg-elevated); border: 1px solid var(--border-color); border-radius: 8px; padding: 14px 18px; margin-bottom: 20px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                        <h4 style="margin: 0; font-size: 14px; font-weight: 700; color: #f3e8ff;">🎬 Keeper Clips Sequence (Plays Seamlessly Back-to-Back)</h4>
                        <span style="font-size: 12px; color: var(--text-muted);">Click any clip to jump player &bull; Edit timestamps if needed</span>
                    </div>
                    <div id="trimmerClipsDeck" style="display: flex; flex-direction: column; gap: 8px; max-height: 260px; overflow-y: auto;"></div>
                </div>

                <!-- Export & Audio Options -->
                <div style="background: #181524; border: 1px solid #7c3aed; border-radius: 10px; padding: 18px 22px; margin-bottom: 20px;">
                    <h4 style="margin: 0 0 12px 0; font-size: 15px; font-weight: 700; color: #ede9fe;">🚀 Export Settings (100% Original Resolution Preserved)</h4>
                    
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px;">
                        <!-- Audio Mode -->
                        <div>
                            <label style="font-size: 13px; font-weight: 600; color: #ddd; display: block; margin-bottom: 6px;">Audio Narration Mode:</label>
                            <div style="display: flex; flex-direction: column; gap: 8px; font-size: 13px;">
                                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                                    <input type="radio" name="trimmerAudioMode" value="original" checked>
                                    <span>🎵 <b>Original Video Audio</b> (Keep native voices and sounds)</span>
                                </label>
                                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                                    <input type="radio" name="trimmerAudioMode" value="tts">
                                    <span>🎙️ <b>Gemini 3.8 Flash Neural Voiceover</b> (Hindi / English Story Narration)</span>
                                </label>
                                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                                    <input type="radio" name="trimmerAudioMode" value="tts_bgm">
                                    <span>🎧 <b>Ducked BGM + Neural Voiceover</b> (0% original sound, 100% copyright safe)</span>
                                </label>
                            </div>
                        </div>

                        <!-- Voice & Tone Settings (for TTS) -->
                        <div id="trimmerTtsOptionsContainer" style="display: none; background: rgba(0,0,0,0.25); border: 1px solid rgba(168, 85, 247, 0.3); border-radius: 8px; padding: 12px;">
                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 8px;">
                                <div>
                                    <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Voice:</label>
                                    <select id="trimmerTtsVoiceSelect" class="form-control" style="width: 100%; padding: 6px 8px; font-size: 12px;">
                                        <option value="Kore">Kore (Hindi Deep Male)</option>
                                        <option value="Fenrir">Fenrir (Dramatic Intense Male)</option>
                                        <option value="Puck">Puck (Fast-Paced Male)</option>
                                        <option value="Algenib">Algenib (Authoritative Male)</option>
                                        <option value="Charon">Charon (Deep Mysterious Male)</option>
                                        <option value="Aoede">Aoede (Expressive Female)</option>
                                        <option value="Algieba">Algieba (Soft Dramatic Female)</option>
                                    </select>
                                </div>
                                <div>
                                    <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Tone Style:</label>
                                    <select id="trimmerTtsToneSelect" class="form-control" style="width: 100%; padding: 6px 8px; font-size: 12px;">
                                        <option value="Suspense / Thriller">Suspense / Thriller</option>
                                        <option value="Movie Trailer Dramatic">Movie Trailer Dramatic</option>
                                        <option value="Narrative Deep Storytelling">Narrative Deep Storytelling</option>
                                        <option value="Fast-Paced Action Punch">Fast-Paced Action Punch</option>
                                        <option value="Emotional Cinema Drama">Emotional Cinema Drama</option>
                                    </select>
                                </div>
                            </div>
                            <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Recap Story Narration Script:</label>
                            <textarea id="trimmerNarrationScript" class="form-control" style="width: 100%; height: 60px; font-size: 12px; resize: vertical;" placeholder="Script automatically generated by Gemini or enter your own custom narration..."></textarea>
                        </div>
                    </div>

                    <!-- Export Button -->
                    <button type="button" id="btnExportFinalVideo" class="btn-upload" style="width: 100%; background: linear-gradient(135deg, #0284c7, #7c3aed); padding: 14px; font-size: 16px; font-weight: 700; border-radius: 8px; display: flex; justify-content: center; align-items: center; gap: 10px; box-shadow: 0 4px 15px rgba(2, 132, 199, 0.4);">
                        <span id="btnExportIcon">🚀</span>
                        <span id="btnExportText">Export Final Video (Preserve Original Resolution)</span>
                    </button>
                </div>

                <!-- Export Progress & Download Card -->
                <div id="trimmerExportCard" style="display: none; background: #0f172a; border: 1px solid #38bdf8; border-radius: 10px; padding: 20px; margin-bottom: 20px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                        <span id="trimmerExportStepText" style="font-size: 14px; font-weight: 600; color: #e0f2fe;">Processing Export...</span>
                        <span id="trimmerExportPercentText" style="font-size: 14px; font-weight: 700; color: #38bdf8;">0%</span>
                    </div>
                    <div style="width: 100%; height: 8px; background: #1e293b; border-radius: 4px; overflow: hidden; margin-bottom: 16px;">
                        <div id="trimmerExportProgressBar" style="width: 0%; height: 100%; background: linear-gradient(90deg, #06b6d4, #38bdf8); transition: width 0.3s ease;"></div>
                    </div>

                    <!-- Completed Video Preview & Download -->
                    <div id="trimmerExportResultBox" style="display: none;">
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: center;">
                            <div>
                                <video id="trimmerExportedPlayer" controls playsinline style="width: 100%; max-height: 280px; border-radius: 8px; background: #000; object-fit: contain;"></video>
                            </div>
                            <div>
                                <h4 style="margin: 0 0 8px 0; color: #38bdf8; font-size: 16px;">✅ Export Ready!</h4>
                                <div id="trimmerExportMetaDetails" style="font-size: 13px; color: #cbd5e1; margin-bottom: 14px; line-height: 1.6;"></div>
                                <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                                    <a id="btnDownloadExportedVideo" href="#" download class="btn-upload" style="text-decoration: none; padding: 10px 18px; font-size: 13px; background: #10b981; display: inline-flex; align-items: center; gap: 6px;">
                                        <span>⬇️</span> <span>Download MP4</span>
                                    </a>
                                    <button type="button" id="btnSendExportToYouTube" class="btn-populate" style="padding: 10px 18px; font-size: 13px; background: rgba(255, 0, 85, 0.2); border-color: #ff0055; color: #fda4af;">
                                        <span>📤</span> <span>Send to YouTube Upload</span>
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

            </div>

            <!-- ============================================== -->
            <!-- 4. STANDARD MANUAL STUDIO FORM PANEL           -->
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

    <div class="chat-drawer" id="chatDrawer">
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
    <div class="modal-overlay" id="geminiModalOverlay">
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

        // Tab Switching Engine (Globally accessible on window)
        window.switchWorkspaceTab = function(activeTab) {
            try {
                const tabs = {
                    'gemini': { tab: document.getElementById('tabGeminiMode'), sec: document.getElementById('geminiStudioSection'), activeCls: 'active-ai' },
                    'clipper': { tab: document.getElementById('tabClipperMode'), sec: document.getElementById('clipperSection'), activeCls: 'active-ai' },
                    'trimmer': { tab: document.getElementById('tabTrimmerMode'), sec: document.getElementById('trimmerSection'), activeCls: 'active-ai' },
                    'manual': { tab: document.getElementById('tabManualMode'), sec: document.getElementById('manualStudioSection'), activeCls: 'active-manual' }
                };

                Object.keys(tabs).forEach(key => {
                    const item = tabs[key];
                    if (item.tab) {
                        item.tab.className = 'mode-tab' + (activeTab === key ? (' ' + item.activeCls) : '');
                    }
                    if (item.sec) {
                        item.sec.style.display = (activeTab === key) ? 'block' : 'none';
                    }
                });
                try {
                    localStorage.setItem('active_studio_tab', activeTab);
                } catch(e) {}
            } catch (err) {
                console.warn("switchWorkspaceTab error:", err);
            }
        };

        // Mobile touch & click binder for navigation tabs
        function bindTabButton(id, tabName) {
            const btn = document.getElementById(id);
            if (!btn) return;
            let touched = false;
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
        const geminiModalOverlay = document.getElementById('geminiModalOverlay');
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
            if (geminiModalOverlay) {
                geminiModalOverlay.style.display = 'flex';
                geminiModalOverlay.style.pointerEvents = 'auto';
                if (modalActiveChannelTitle) {
                    modalActiveChannelTitle.textContent = window.currentActiveChannelTitle || 'Active Channel';
                }
                loadChannelKeyPool(window.currentActiveChannelId);
            }
        }

        function closeKeyModal() {
            if (geminiModalOverlay) {
                geminiModalOverlay.style.display = 'none';
                geminiModalOverlay.style.pointerEvents = 'none';
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

        // Extracts high-resolution keyframes directly from video stream
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
            clientCanvas.width = width;
            clientCanvas.height = height;
            const ctx = clientCanvas.getContext('2d');

            // Sample 6 timestamps across the video (e.g. 8%, 20%, 36%, 52%, 70%, 88%)
            const fractions = [0.08, 0.20, 0.36, 0.52, 0.70, 0.88];
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
                const label = i === 1 ? 'Primary Character Face' : (i === 3 ? 'Peak Action Scene' : `Authentic Scene (${timeStr})`);

                // Send frame to server to store in uploads/thumbnails
                try {
                    const fd = new FormData();
                    fd.append('image_file', blob, `frame_${i+1}_${timeStr.replace(':', 'm')}s.jpg`);
                    fd.append('timestamp', timeStr);
                    fd.append('seconds', ts);
                    fd.append('label', label);

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

        // Render Gemini Results
        function renderGeminiResults(data) {
            geminiResultsBox.style.display = 'block';

            // Target Format & Primary Context Display
            const fmtEl = document.getElementById('aiTargetFormatBadge');
            if (fmtEl) fmtEl.textContent = (data.format_type === 'Long' ? '🎬 Long Form Video' : '📱 YouTube Shorts');
            const ctxEl = document.getElementById('aiPrimaryContext');
            if (ctxEl) ctxEl.textContent = data.primary_context || 'Autonomous Evaluation';

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

            // 2. Thumbnails Picker & Thumbnail Directive
            const gallery = document.getElementById('thumbnailGalleryGrid');
            const allThumbnails = (clientExtractedFrames.length > 0) ? clientExtractedFrames : (data.extracted_thumbnails || []);

            if (allThumbnails.length > 0) {
                gallery.innerHTML = allThumbnails.map((th, idx) => {
                    const isRec = idx === 0 || th.is_recommended;
                    return `
                        <div class="thumb-candidate-card ${isRec ? 'selected gemini-best' : ''}" onclick="selectThumbnailFrame(this, '${th.url}', '${th.filename}')">
                            <img src="${th.url}" alt="Frame">
                            ${isRec ? '<span class="thumb-ai-rec-badge">⭐ 100% Authentic Face Match</span>' : ''}
                            <span class="thumb-badge">${th.timestamp || 'Frame'}</span>
                            <span class="thumb-highlight-badge">✔ Selected</span>
                        </div>
                    `;
                }).join('');

                // Select first
                selectThumbnailFrame(gallery.firstElementChild, allThumbnails[0].url, allThumbnails[0].filename);
            } else {
                gallery.innerHTML = '<div style="color: var(--text-muted); font-size: 13px;">No thumbnails extracted. You can upload a custom one.</div>';
            }

            // Thumbnail Directive Art Direction
            const thumbDir = data.thumbnail_directive || {};
            const overlayEl = document.getElementById('aiThumbOverlayText');
            if (overlayEl) overlayEl.textContent = thumbDir.text_overlay || 'WATCH THIS';
            const sceneEl = document.getElementById('aiThumbSceneDir');
            if (sceneEl) sceneEl.textContent = thumbDir.visual_scene_direction || 'High emotion close-up frame with clear lighting';
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

            document.getElementById('aiVideoTypeBadge').textContent = (data.video_type === 'Short' ? 'YouTube Short' : 'Long-form Video');
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

        function selectThumbnailFrame(card, url, filename) {
            if (!card) return;
            document.querySelectorAll('.thumb-candidate-card').forEach(c => c.classList.remove('selected'));
            card.classList.add('selected');
            selectedThumbnailUrl = url;
            selectedThumbnailFilename = filename;
            document.getElementById('selectedThumbnailFilename').value = filename;

            // Update preview box in manual studio
            const thumbPreviewImg = document.getElementById('thumbPreviewImg');
            const thumbPlaceholder = document.getElementById('thumbPlaceholder');
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
        const chatDrawer = document.getElementById('chatDrawer');
        const btnCloseChat = document.getElementById('btnCloseChat');
        const chatInput = document.getElementById('chatInput');
        const btnSendChat = document.getElementById('btnSendChat');
        const chatMessages = document.getElementById('chatMessages');

        if (chatFabBtn && chatDrawer) {
            chatFabBtn.addEventListener('click', () => {
                chatDrawer.classList.toggle('open');
                chatDrawer.style.pointerEvents = chatDrawer.classList.contains('open') ? 'auto' : 'none';
            });
        }
        if (btnCloseChat && chatDrawer) {
            btnCloseChat.addEventListener('click', () => {
                chatDrawer.classList.remove('open');
                chatDrawer.style.pointerEvents = 'none';
            });
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

        // ==============================================================
        // AI MOVIE-TO-SHORTS AUTO-CLIPPER ENGINE JAVASCRIPT CONTROLLER
        // ==============================================================
        let currentClipperJobId = null;
        let currentClipperVideoInfo = null;
        let currentClipperScenes = [];
        let completedClipperShorts = {}; // keyed by part number

        const clipperUrlInput = document.getElementById('clipperUrlInput');
        const btnPasteUrl = document.getElementById('btnPasteUrl');
        const clipperMaxShortsSlider = document.getElementById('clipperMaxShortsSlider');
        const clipperMaxShortsBadge = document.getElementById('clipperMaxShortsBadge');
        const clipperDurationSelect = document.getElementById('clipperDurationSelect');
        const clipperLanguageSelect = document.getElementById('clipperLanguageSelect');
        const btnAnalyzeClipper = document.getElementById('btnAnalyzeClipper');
        const clipperBtnText = document.getElementById('clipperBtnText');
        const clipperBtnIcon = document.getElementById('clipperBtnIcon');
        const clipperAnalysisProgress = document.getElementById('clipperAnalysisProgress');
        const clipperProgressStep = document.getElementById('clipperProgressStep');

        // Checkpoint & Quota banner elements
        const clipperQuotaBanner = document.getElementById('clipperQuotaBanner');
        const clipperQuotaBannerTitle = document.getElementById('clipperQuotaBannerTitle');
        const clipperQuotaBannerDesc = document.getElementById('clipperQuotaBannerDesc');
        const btnResumeClipperJob = document.getElementById('btnResumeClipperJob');
        const btnOpenKeyModalFromClipper = document.getElementById('btnOpenKeyModalFromClipper');
        const btnViewSavedJobs = document.getElementById('btnViewSavedJobs');
        const btnResumeBatchShorts = document.getElementById('btnResumeBatchShorts');

        // Saved jobs modal elements
        const clipperJobsModalOverlay = document.getElementById('clipperJobsModalOverlay');
        const btnCloseJobsModal = document.getElementById('btnCloseJobsModal');
        const clipperJobsListContainer = document.getElementById('clipperJobsListContainer');

        const clipperMovieMetaCard = document.getElementById('clipperMovieMetaCard');
        const clipperMovieThumb = document.getElementById('clipperMovieThumb');
        const clipperMovieTitle = document.getElementById('clipperMovieTitle');
        const clipperMovieDuration = document.getElementById('clipperMovieDuration');
        const clipperMovieChannel = document.getElementById('clipperMovieChannel');
        const clipperMoviePartsCount = document.getElementById('clipperMoviePartsCount');

        const clipperBatchBar = document.getElementById('clipperBatchBar');
        const btnGenerateAllShorts = document.getElementById('btnGenerateAllShorts');
        const btnUploadAllShorts = document.getElementById('btnUploadAllShorts');
        const clipperBatchProgressCard = document.getElementById('clipperBatchProgressCard');
        const clipperBatchStepText = document.getElementById('clipperBatchStepText');
        const clipperBatchPercentText = document.getElementById('clipperBatchPercentText');
        const clipperBatchProgressBar = document.getElementById('clipperBatchProgressBar');

        const clipperQueueContainer = document.getElementById('clipperQueueContainer');
        const clipperQueueBadge = document.getElementById('clipperQueueBadge');
        const clipperScenesGrid = document.getElementById('clipperScenesGrid');

        // Slider value badge listener
        if (clipperMaxShortsSlider && clipperMaxShortsBadge) {
            clipperMaxShortsSlider.addEventListener('input', (e) => {
                const val = e.target.value;
                clipperMaxShortsBadge.textContent = `${val} Parts (AI Optimal)`;
            });
        }

        // Paste URL button listener
        if (btnPasteUrl && clipperUrlInput) {
            btnPasteUrl.addEventListener('click', async () => {
                try {
                    const text = await navigator.clipboard.readText();
                    if (text && (text.includes('youtube.com') || text.includes('youtu.be'))) {
                        clipperUrlInput.value = text.trim();
                    } else if (text) {
                        clipperUrlInput.value = text.trim();
                    }
                } catch (e) {
                    alert('Please paste the YouTube URL directly into the input box.');
                }
            });
        }

        // Gemini key button from clipper banner
        if (btnOpenKeyModalFromClipper) {
            btnOpenKeyModalFromClipper.addEventListener('click', openKeyModal);
        }

        // Saved Jobs Modal Open / Close
        if (btnViewSavedJobs && clipperJobsModalOverlay) {
            btnViewSavedJobs.addEventListener('click', async () => {
                clipperJobsModalOverlay.style.display = 'flex';
                await loadSavedJobsList();
            });
        }

        if (btnCloseJobsModal && clipperJobsModalOverlay) {
            btnCloseJobsModal.addEventListener('click', () => {
                clipperJobsModalOverlay.style.display = 'none';
            });
            clipperJobsModalOverlay.addEventListener('click', (e) => {
                if (e.target === clipperJobsModalOverlay) {
                    clipperJobsModalOverlay.style.display = 'none';
                }
            });
        }

        async function loadSavedJobsList() {
            if (!clipperJobsListContainer) return;
            clipperJobsListContainer.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 30px;"><div class="spinner" style="margin: 0 auto 10px auto;"></div>Loading saved checkpoints...</div>';
            try {
                const res = await fetch('/api/clipper/jobs');
                const data = await res.json();
                if (!res.ok || !data.success) {
                    throw new Error(data.error || 'Failed to fetch saved jobs');
                }
                const jobs = data.jobs || [];
                if (jobs.length === 0) {
                    clipperJobsListContainer.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 30px;">No saved checkpoints found in uploads/clipper_jobs/ yet. Analyze a video to create one.</div>';
                    return;
                }

                clipperJobsListContainer.innerHTML = jobs.map(job => {
                    const dateStr = job.updated_at ? new Date(job.updated_at * 1000).toLocaleString() : 'Recently';
                    let statusBadge = '<span class="status-badge status-planned">⏳ Planned</span>';
                    if (job.status === 'PAUSED_QUOTA_LIMIT') {
                        statusBadge = '<span class="status-badge" style="background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #f59e0b;">⚠️ Paused (Quota)</span>';
                    } else if (job.status === 'COMPLETED') {
                        statusBadge = '<span class="status-badge status-ready">✅ Completed</span>';
                    } else if (job.status === 'PROCESSING') {
                        statusBadge = '<span class="status-badge" style="background: rgba(168, 85, 247, 0.2); color: #c084fc;">⚙️ Processing</span>';
                    }

                    return `
                        <div style="background: var(--bg-elevated); border: 1px solid var(--border-color); border-radius: 8px; padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;">
                            <div style="display: flex; align-items: center; gap: 12px; min-width: 240px; flex: 1;">
                                <img src="${escapeHtml(job.thumbnail || '')}" alt="" style="width: 70px; aspect-ratio: 16/9; object-fit: cover; border-radius: 4px; background: #222;" onerror="this.style.display='none'">
                                <div>
                                    <div style="font-weight: 600; font-size: 13px; color: #f3f4f6; margin-bottom: 2px;">${escapeHtml(job.title)}</div>
                                    <div style="font-size: 11px; color: var(--text-muted); display: flex; gap: 8px; align-items: center;">
                                        <span>📅 ${escapeHtml(dateStr)}</span>
                                        <span>🎬 ${job.completed_count} / ${job.total_scenes} Parts Done</span>
                                        <span>🌐 ${escapeHtml(job.language || 'Hindi')}</span>
                                    </div>
                                </div>
                            </div>
                            <div style="display: flex; align-items: center; gap: 10px;">
                                ${statusBadge}
                                <button type="button" class="btn-populate" onclick="openAndResumeSavedJob('${job.job_id}')" style="padding: 6px 14px; font-size: 12px;">
                                    Load &amp; Resume &#8594;
                                </button>
                            </div>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                clipperJobsListContainer.innerHTML = `<div style="text-align: center; color: #f87171; padding: 20px;">Error loading jobs: ${escapeHtml(err.message)}</div>`;
            }
        }

        window.openAndResumeSavedJob = async function(jobId) {
            clipperJobsModalOverlay.style.display = 'none';
            try {
                const res = await fetch(`/api/clipper/job/${jobId}`);
                const data = await res.json();
                if (!res.ok || !data.success) {
                    throw new Error(data.error || 'Failed to load checkpoint');
                }

                const job = data.job;
                currentClipperJobId = job.job_id;
                currentClipperVideoInfo = job.video_info || { title: job.title, url: job.url, thumbnail: job.thumbnail, duration_str: '' };
                currentClipperScenes = job.scenes || [];
                completedClipperShorts = {};

                // Process completed shorts
                const rawCompleted = job.completed_shorts || {};
                if (Array.isArray(rawCompleted)) {
                    rawCompleted.forEach(s => { completedClipperShorts[s.part] = s; });
                } else if (typeof rawCompleted === 'object') {
                    Object.keys(rawCompleted).forEach(k => {
                        const s = rawCompleted[k];
                        completedClipperShorts[s.part || parseInt(k)] = s;
                    });
                }

                if (job.url) clipperUrlInput.value = job.url;

                // Render Metadata Card
                clipperMovieThumb.src = currentClipperVideoInfo.thumbnail || '';
                clipperMovieTitle.textContent = currentClipperVideoInfo.title || 'Movie';
                clipperMovieDuration.textContent = `⏱️ Duration: ${currentClipperVideoInfo.duration_str || 'Full Movie'}`;
                clipperMovieChannel.textContent = `👤 Channel: ${currentClipperVideoInfo.channel || 'YouTube'}`;
                clipperMoviePartsCount.textContent = `🎬 ${currentClipperScenes.length} Chronological Shorts Planned`;
                clipperMovieMetaCard.style.display = 'block';

                // Render Scenes Queue
                renderClipperScenesQueue(currentClipperScenes);
                clipperBatchBar.style.display = 'flex';
                clipperQueueContainer.style.display = 'block';
                clipperQueueBadge.textContent = `${currentClipperScenes.length} Parts (${Object.keys(completedClipperShorts).length} Done)`;

                // Mark already completed shorts
                Object.keys(completedClipperShorts).forEach(p => {
                    markPartAsCompleted(parseInt(p), completedClipperShorts[p]);
                });

                // Check quota paused state
                if (job.status === 'PAUSED_QUOTA_LIMIT' || (job.error && job.error.includes('429'))) {
                    clipperQuotaBanner.style.display = 'block';
                    clipperQuotaBannerTitle.textContent = 'Gemini API Quota Limit (429) — Job Safely Paused';
                    clipperQuotaBannerDesc.innerHTML = `Loaded saved job <code>${escapeHtml(currentClipperJobId)}</code>. Progress is preserved. Update your API key in Settings or wait for quota reset, then click <strong>Resume Job</strong>.`;
                    if (btnResumeBatchShorts) btnResumeBatchShorts.style.display = 'inline-flex';
                } else {
                    clipperQuotaBanner.style.display = 'none';
                    if (btnResumeBatchShorts) btnResumeBatchShorts.style.display = 'none';
                }

            } catch (err) {
                alert('Failed to load checkpoint: ' + err.message);
            }
        };

        // Resume Job Handlers
        if (btnResumeClipperJob) {
            btnResumeClipperJob.addEventListener('click', resumeCurrentClipperJob);
        }
        if (btnResumeBatchShorts) {
            btnResumeBatchShorts.addEventListener('click', resumeCurrentClipperJob);
        }

        async function resumeCurrentClipperJob() {
            if (!currentClipperJobId) {
                alert('No active job ID found. Please select or analyze a video first.');
                return;
            }

            if (btnResumeClipperJob) {
                btnResumeClipperJob.disabled = true;
                btnResumeClipperJob.innerHTML = '<span class="spinner" style="width: 14px; height: 14px; display: inline-block;"></span> Resuming...';
            }
            if (btnResumeBatchShorts) {
                btnResumeBatchShorts.disabled = true;
                btnResumeBatchShorts.innerHTML = '<span class="spinner" style="width: 14px; height: 14px; display: inline-block;"></span> Resuming...';
            }

            try {
                const res = await fetch('/api/clipper/resume', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ job_id: currentClipperJobId })
                });
                const data = await res.json();
                if (res.status === 429 || data.status === 'PAUSED_QUOTA_LIMIT') {
                    alert('⚠️ Gemini API Quota is still active (429). Please update your API key in Settings or wait a moment before resuming.');
                    clipperQuotaBanner.style.display = 'block';
                    return;
                }
                if (!res.ok || !data.success) {
                    throw new Error(data.error || 'Failed to resume job');
                }

                clipperQuotaBanner.style.display = 'none';
                if (btnResumeBatchShorts) btnResumeBatchShorts.style.display = 'none';

                if (data.scenes && data.scenes.length) {
                    currentClipperScenes = data.scenes;
                    renderClipperScenesQueue(currentClipperScenes);
                }

                // Poll batch status until completed or paused
                pollClipperBatchStatus(currentClipperJobId);

            } catch (err) {
                console.error('Resume error:', err);
                alert('Resume error: ' + err.message);
            } finally {
                if (btnResumeClipperJob) {
                    btnResumeClipperJob.disabled = false;
                    btnResumeClipperJob.innerHTML = '▶️ Resume Job';
                }
                if (btnResumeBatchShorts) {
                    btnResumeBatchShorts.disabled = false;
                    btnResumeBatchShorts.innerHTML = '▶️ Resume Job';
                }
            }
        }

        let currentClipperWps = 2.23;

        // Gemini 3.8 Flash TTS Studio - Preview & Calibration
        const btnPreviewTtsVoice = document.getElementById('btnPreviewTtsVoice');
        const btnCalibrateWps = document.getElementById('btnCalibrateWps');
        const clipperTtsAudioPlayer = document.getElementById('clipperTtsAudioPlayer');
        const clipperWpsBadge = document.getElementById('clipperWpsBadge');

        if (btnPreviewTtsVoice) {
            btnPreviewTtsVoice.addEventListener('click', async () => {
                const voice = document.getElementById('clipperTtsVoiceSelect')?.value || 'Kore';
                const tone = document.getElementById('clipperTtsToneSelect')?.value || 'Suspense / Thriller';
                const lang = clipperLanguageSelect?.value || 'Hindi';
                const icon = document.getElementById('btnPreviewTtsIcon');
                const text = document.getElementById('btnPreviewTtsText');

                btnPreviewTtsVoice.disabled = true;
                if (icon) icon.innerHTML = '<span class="spinner" style="width: 12px; height: 12px; display: inline-block;"></span>';
                if (text) text.textContent = 'Generating...';

                try {
                    const res = await fetch('/api/clipper/tts/preview', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ voice, tone, language: lang })
                    });
                    const d = await res.json();
                    if (!res.ok || !d.success) throw new Error(d.error || 'TTS preview failed');

                    if (clipperTtsAudioPlayer && d.audio_url) {
                        clipperTtsAudioPlayer.src = d.audio_url;
                        clipperTtsAudioPlayer.style.display = 'block';
                        clipperTtsAudioPlayer.play().catch(e => console.log('Autoplay:', e));
                    }
                } catch (e) {
                    alert('Voice preview failed: ' + e.message);
                } finally {
                    btnPreviewTtsVoice.disabled = false;
                    if (icon) icon.textContent = '🔊';
                    if (text) text.textContent = 'Test Voice';
                }
            });
        }

        if (btnCalibrateWps) {
            btnCalibrateWps.addEventListener('click', async () => {
                const voice = document.getElementById('clipperTtsVoiceSelect')?.value || 'Kore';
                const tone = document.getElementById('clipperTtsToneSelect')?.value || 'Suspense / Thriller';
                const lang = clipperLanguageSelect?.value || 'Hindi';
                const icon = document.getElementById('btnCalibrateIcon');
                const text = document.getElementById('btnCalibrateText');

                btnCalibrateWps.disabled = true;
                if (icon) icon.innerHTML = '<span class="spinner" style="width: 12px; height: 12px; display: inline-block;"></span>';
                if (text) text.textContent = 'Calibrating 100w...';

                try {
                    const res = await fetch('/api/clipper/tts/calibrate', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ voice, tone, language: lang, job_id: currentClipperJobId })
                    });
                    const d = await res.json();
                    if (!res.ok || !d.success) throw new Error(d.error || 'Calibration failed');

                    const cal = d.result;
                    currentClipperWps = cal.wps;
                    if (clipperWpsBadge) {
                        clipperWpsBadge.innerHTML = `⚡ Calibrated Pace: <strong>${cal.wps} Words/Sec</strong> (${cal.duration}s sample)`;
                        clipperWpsBadge.style.background = 'rgba(16, 185, 129, 0.3)';
                    }
                    if (clipperTtsAudioPlayer && cal.audio_url) {
                        clipperTtsAudioPlayer.src = cal.audio_url;
                        clipperTtsAudioPlayer.style.display = 'block';
                        clipperTtsAudioPlayer.play().catch(e => console.log('Autoplay:', e));
                    }
                    alert(`✅ Calibration Successful!\nVoice: ${cal.voice} (${cal.tone})\nPace: ${cal.wps} words/second (${cal.word_count} words in ${cal.duration}s)`);
                } catch (e) {
                    alert('Calibration failed: ' + e.message);
                } finally {
                    btnCalibrateWps.disabled = false;
                    if (icon) icon.textContent = '⚡';
                    if (text) text.textContent = 'Calibrate (100w)';
                }
            });
        }

        // Analyze & Plan Chronological Shorts
        if (btnAnalyzeClipper) {
            btnAnalyzeClipper.addEventListener('click', async () => {
                const url = clipperUrlInput.value.trim();
                if (!url) {
                    alert('Please enter a valid YouTube video or movie URL.');
                    clipperUrlInput.focus();
                    return;
                }

                btnAnalyzeClipper.disabled = true;
                clipperBtnIcon.innerHTML = '<span class="spinner" style="width: 14px; height: 14px; display: inline-block;"></span>';
                clipperBtnText.textContent = 'Analyzing Narrative Arc with Gemini...';
                clipperAnalysisProgress.style.display = 'block';
                clipperProgressStep.textContent = 'Extracting video stream details and chapter markers...';

                // Reset state
                clipperMovieMetaCard.style.display = 'none';
                clipperBatchBar.style.display = 'none';
                clipperQueueContainer.style.display = 'none';
                clipperQuotaBanner.style.display = 'none';
                currentClipperScenes = [];
                completedClipperShorts = {};

                try {
                    const maxShorts = parseInt(clipperMaxShortsSlider.value) || 5;
                    const targetDuration = parseInt(clipperDurationSelect.value) || 58;
                    const language = clipperLanguageSelect.value || 'Hindi';
                    const voiceName = document.getElementById('clipperTtsVoiceSelect')?.value || 'Kore';
                    const toneStyle = document.getElementById('clipperTtsToneSelect')?.value || 'Suspense / Thriller';
                    const wps = currentClipperWps || 2.23;

                    setTimeout(() => {
                        if (clipperProgressStep) {
                            clipperProgressStep.textContent = `Gemini is discovering key dramatic moments in chronological order (${language})...`;
                        }
                    }, 2500);

                    const res = await fetch('/api/clipper/analyze', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            url: url,
                            max_shorts: maxShorts,
                            target_duration: targetDuration,
                            language: language,
                            voice_name: voiceName,
                            tone_style: toneStyle,
                            wps: wps
                        })
                    });

                    const data = await res.json();
                    if (!res.ok || !data.success) {
                        throw new Error(data.error || 'Failed to analyze video');
                    }

                    currentClipperJobId = data.job_id;
                    currentClipperVideoInfo = data.video_info;
                    currentClipperScenes = data.scenes || [];

                    // Populate any already completed parts
                    completedClipperShorts = {};
                    if (data.completed_shorts && Array.isArray(data.completed_shorts)) {
                        data.completed_shorts.forEach(s => {
                            if (s && s.part) completedClipperShorts[s.part] = s;
                        });
                    }

                    // Render Movie Metadata Box
                    clipperMovieThumb.src = currentClipperVideoInfo.thumbnail || '';
                    clipperMovieTitle.textContent = currentClipperVideoInfo.title || 'Movie';
                    clipperMovieDuration.textContent = `⏱️ Duration: ${currentClipperVideoInfo.duration_str}`;
                    clipperMovieChannel.textContent = `👤 Channel: ${currentClipperVideoInfo.channel || 'YouTube'}`;
                    clipperMoviePartsCount.textContent = `🎬 ${currentClipperScenes.length} Chronological Shorts Planned`;
                    clipperMovieMetaCard.style.display = 'block';

                    // Render Scenes Queue
                    renderClipperScenesQueue(currentClipperScenes);
                    clipperBatchBar.style.display = 'flex';
                    clipperQueueContainer.style.display = 'block';
                    clipperQueueBadge.textContent = `${currentClipperScenes.length} Parts`;

                    // Mark completed parts immediately in UI
                    currentClipperScenes.forEach(sc => {
                        if (completedClipperShorts[sc.part]) {
                            markPartAsCompleted(sc.part, completedClipperShorts[sc.part]);
                        }
                    });

                    // If quota limit occurred during analysis
                    if (data.status === 'PAUSED_QUOTA_LIMIT' || (data.error && data.error.includes('429'))) {
                        clipperQuotaBanner.style.display = 'block';
                        clipperQuotaBannerTitle.textContent = 'Gemini API Quota Limit Reached (429) — Job Safely Saved';
                        clipperQuotaBannerDesc.innerHTML = `Narrative analysis checkpoint saved (<code>${escapeHtml(currentClipperJobId)}</code>). Fallback storyline segments are loaded below. Update your Gemini key in Settings or wait for quota reset, then click <strong>Resume Job</strong> to generate with full AI scripts.`;
                        if (btnResumeBatchShorts) btnResumeBatchShorts.style.display = 'inline-flex';
                    } else {
                        clipperQuotaBanner.style.display = 'none';
                        if (btnResumeBatchShorts) btnResumeBatchShorts.style.display = 'none';
                    }

                } catch (err) {
                    console.error('Clipper analysis error:', err);
                    alert('Error analyzing video: ' + err.message);
                } finally {
                    btnAnalyzeClipper.disabled = false;
                    clipperBtnIcon.textContent = '🚀';
                    clipperBtnText.textContent = 'Analyze Narrative & Plan Chronological Shorts';
                    clipperAnalysisProgress.style.display = 'none';
                }
            });
        }

        function getBeatStyle(beat) {
            const b = (beat || '').toLowerCase();
            if (b.includes('hook')) return 'background: rgba(239, 68, 68, 0.2); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.4);';
            if (b.includes('setup')) return 'background: rgba(245, 158, 11, 0.2); color: #fde047; border: 1px solid rgba(245, 158, 11, 0.4);';
            if (b.includes('tension')) return 'background: rgba(168, 85, 247, 0.2); color: #d8b4fe; border: 1px solid rgba(168, 85, 247, 0.4);';
            if (b.includes('action')) return 'background: rgba(244, 63, 94, 0.2); color: #fda4af; border: 1px solid rgba(244, 63, 94, 0.4);';
            if (b.includes('twist')) return 'background: rgba(217, 70, 239, 0.2); color: #f0abfc; border: 1px solid rgba(217, 70, 239, 0.4);';
            if (b.includes('reaction')) return 'background: rgba(6, 182, 212, 0.2); color: #67e8f9; border: 1px solid rgba(6, 182, 212, 0.4);';
            if (b.includes('climax')) return 'background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.4);';
            if (b.includes('cliffhanger')) return 'background: rgba(234, 179, 8, 0.2); color: #fef08a; border: 1px solid rgba(234, 179, 8, 0.4);';
            return 'background: rgba(255, 255, 255, 0.08); color: #e2e8f0; border: 1px solid rgba(255, 255, 255, 0.15);';
        }

        window.playCutPreview = function(partNum, cutUrl, cutTitle) {
            const container = document.getElementById(`cutsPlayerContainer_${partNum}`);
            const video = document.getElementById(`cutActiveVideo_${partNum}`);
            const label = document.getElementById(`cutActiveTitle_${partNum}`);
            if (!container || !video) return;

            video.src = cutUrl;
            if (label) label.textContent = `▶️ Playing: ${cutTitle}`;
            container.style.display = 'block';
            video.play().catch(e => console.log('Autoplay deferred:', e));
        };

        function updateSceneCutsGallery(partNum, cuts) {
            const grid = document.getElementById(`sceneCutsGrid_${partNum}`);
            if (!grid || !cuts || !cuts.length) return;
            grid.innerHTML = cuts.map((c, ci) => `
                <div style="display: flex; flex-direction: column; gap: 3px; padding: 6px 8px; border-radius: 6px; ${getBeatStyle(c.beat)}">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span style="font-weight: 700; font-size: 11px;">${escapeHtml(c.beat || `Cut ${ci+1}`)}</span>
                        <span style="font-size: 10px; opacity: 0.85;">⏱️ ${c.duration}s</span>
                    </div>
                    <div style="font-size: 10px; font-family: monospace; opacity: 0.9;">${c.start_time} - ${c.end_time}</div>
                    <div style="font-size: 10px; opacity: 0.8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(c.description || '')}">${escapeHtml(c.description || '')}</div>
                    ${c.url ? `
                        <button type="button" onclick="playCutPreview(${partNum}, '${c.url}', '${escapeHtml(c.beat || `Cut ${ci+1}`)} (${c.duration}s)')" style="margin-top: 4px; font-size: 10px; color: #38bdf8; background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 4px; padding: 3px 6px; cursor: pointer; display: inline-flex; align-items: center; gap: 4px; font-weight: 600;">
                            ▶️ Play Raw Cut
                        </button>
                    ` : ''}
                </div>
            `).join('');
        }

        function renderClipperScenesQueue(scenes) {
            clipperScenesGrid.innerHTML = scenes.map((scene, idx) => `
                <div class="scene-item-card" id="sceneCard_${scene.part}">
                    <div class="scene-header">
                        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
                            <span class="part-pill">Part ${scene.part}</span>
                            <span class="timestamp-pill">⏱️ ${scene.start_time} - ${scene.end_time} (${scene.duration}s)</span>
                            <span style="font-size: 11px; font-weight: 600; background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); padding: 3px 8px; border-radius: 4px;">🎬 ${(scene.sub_clips || []).length || 8} Cuts (0% Orig Audio)</span>
                        </div>
                        <span class="status-badge status-planned" id="sceneStatusBadge_${scene.part}">⏳ Planned</span>
                    </div>

                    <div class="scene-body">
                        <!-- Video Media Box -->
                        <div class="scene-video-box" id="sceneMediaBox_${scene.part}">
                            <div class="placeholder-916">
                                <span style="font-size: 28px;">🎬</span>
                                <span style="font-weight: 600; font-size: 13px;">Standard / Vertical Montage</span>
                                <span style="font-size: 11px; color: var(--text-muted);">${(scene.sub_clips || []).length || 8} Fast Cuts (3-6s) • Muted Audio • BGM &amp; Voiceover</span>
                            </div>
                        </div>

                        <!-- Metadata Box -->
                        <div class="scene-meta-box">
                            <div>
                                <label class="field-label">Short Title (Viral Title + Part Tag)</label>
                                <input type="text" id="sceneTitle_${scene.part}" value="${escapeHtml(scene.title)}" class="form-control" style="font-size: 13px; font-weight: 600;">
                            </div>

                            <div>
                                <label class="field-label">🔥 3-Second Retention Hook</label>
                                <input type="text" id="sceneHook_${scene.part}" value="${escapeHtml(scene.hook)}" class="form-control" style="font-size: 12px; color: #ffedd5; background: rgba(255,100,0,0.08); border-color: rgba(255,100,0,0.3);">
                            </div>

                            <div>
                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                                    <label class="field-label" style="margin: 0;">🎬 Smart Director Storyboard (${(scene.sub_clips || []).length} Cuts • 50-65s Total)</label>
                                    <span style="font-size: 11px; color: #10b981; font-weight: 600;">⚡ High-Speed Sliced (0% Movie Audio)</span>
                                </div>
                                <div id="sceneCutsGrid_${scene.part}" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 8px; margin-top: 6px; max-height: 140px; overflow-y: auto; padding: 8px; background: rgba(0,0,0,0.3); border-radius: 8px; border: 1px solid rgba(255,255,255,0.08);">
                                    ${(scene.downloaded_cuts && scene.downloaded_cuts.length ? scene.downloaded_cuts : (scene.sub_clips || [])).map((c, ci) => `
                                        <div style="display: flex; flex-direction: column; gap: 3px; padding: 6px 8px; border-radius: 6px; ${getBeatStyle(c.beat)}">
                                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                                <span style="font-weight: 700; font-size: 11px;">${escapeHtml(c.beat || `Cut ${ci+1}`)}</span>
                                                <span style="font-size: 10px; opacity: 0.85;">⏱️ ${c.duration}s</span>
                                            </div>
                                            <div style="font-size: 10px; font-family: monospace; opacity: 0.9;">${c.start_time} - ${c.end_time}</div>
                                            <div style="font-size: 10px; opacity: 0.8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(c.description || '')}">${escapeHtml(c.description || '')}</div>
                                            ${c.url ? `
                                                <button type="button" onclick="playCutPreview(${scene.part}, '${c.url}', '${escapeHtml(c.beat || `Cut ${ci+1}`)} (${c.duration}s)')" style="margin-top: 4px; font-size: 10px; color: #38bdf8; background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 4px; padding: 3px 6px; cursor: pointer; display: inline-flex; align-items: center; gap: 4px; font-weight: 600;">
                                                    ▶️ Play Raw Cut
                                                </button>
                                            ` : ''}
                                        </div>
                                    `).join('')}
                                </div>

                                <!-- Direct Interactive Cuts Player -->
                                <div id="cutsPlayerContainer_${scene.part}" style="display: none; margin-top: 8px; border-radius: 8px; overflow: hidden; background: #000; border: 1px solid rgba(168, 85, 247, 0.4);">
                                    <div style="display: flex; justify-content: space-between; align-items: center; background: #161224; padding: 6px 10px; font-size: 11px; font-weight: 600; color: #d8b4fe;">
                                        <span id="cutActiveTitle_${scene.part}">▶️ Direct Cut Preview</span>
                                        <button type="button" onclick="document.getElementById('cutsPlayerContainer_${scene.part}').style.display='none'; document.getElementById('cutActiveVideo_${scene.part}').pause();" style="background: none; border: none; color: #94a3b8; cursor: pointer; font-size: 13px;">✕</button>
                                    </div>
                                    <video id="cutActiveVideo_${scene.part}" controls playsinline style="width: 100%; max-height: 180px; object-fit: contain; background: #000;"></video>
                                </div>
                            </div>

                            <div>
                                <label class="field-label">🎙️ Cohesive Story Recap Script (~80-110 words)</label>
                                <textarea id="sceneScript_${scene.part}" class="form-control" rows="3" style="font-size: 12px; line-height: 1.4;">${escapeHtml(scene.script)}</textarea>
                            </div>

                            <div>
                                <label class="field-label">🏷️ Tags</label>
                                <input type="text" id="sceneTags_${scene.part}" value="${(scene.tags || []).join(', ')}" class="form-control" style="font-size: 12px;">
                            </div>

                            <div class="scene-actions-row">
                                <button type="button" class="btn-populate" id="btnGenPart_${scene.part}" onclick="generateSinglePart(${scene.part})">
                                    <span>⚡ Generate Part ${scene.part} (Recap Montage)</span>
                                </button>
                                <button type="button" class="btn-populate" id="btnConvertVertPart_${scene.part}" style="display: none; background: linear-gradient(135deg, #0ea5e9, #6366f1); padding: 8px 14px; font-size: 12px;" onclick="convertPartToVertical(${scene.part})">
                                    <span>📱 Convert to 9:16 Vertical Short (Face-Centering)</span>
                                </button>
                                <button type="button" class="btn-upload" id="btnUploadPart_${scene.part}" style="display: none; padding: 10px 18px; font-size: 13px; width: auto; background: var(--accent-red);" onclick="uploadSinglePart(${scene.part})">
                                    <span>🚀 Upload Part ${scene.part} to YouTube</span>
                                </button>
                                <a id="btnWatchPart_${scene.part}" href="#" target="_blank" class="btn-link btn-link-primary" style="display: none; padding: 10px 16px; font-size: 13px;">
                                    <span>Watch on YouTube &#8599;</span>
                                </a>
                            </div>
                        </div>
                    </div>
                </div>
            `).join('');
        }

        function markPartAsCompleted(partNum, shortObj) {
            const badge = document.getElementById(`sceneStatusBadge_${partNum}`);
            const mediaBox = document.getElementById(`sceneMediaBox_${partNum}`);
            const btn = document.getElementById(`btnGenPart_${partNum}`);
            const convertBtn = document.getElementById(`btnConvertVertPart_${partNum}`);
            const uploadBtn = document.getElementById(`btnUploadPart_${partNum}`);

            const isVertical = shortObj.format === 'vertical_916' || shortObj.vertical_ready;

            if (badge) {
                badge.className = 'status-badge status-ready';
                if (isVertical) {
                    badge.innerHTML = '✅ 9:16 Vertical Short Ready';
                    badge.style.background = 'rgba(16, 185, 129, 0.2)';
                    badge.style.color = '#34d399';
                } else {
                    badge.innerHTML = '🎬 16:9 Standard Preview Ready';
                    badge.style.background = 'rgba(56, 189, 248, 0.2)';
                    badge.style.color = '#38bdf8';
                }
            }

            if (mediaBox && shortObj && shortObj.video_url) {
                const vidStyle = isVertical 
                    ? 'width: 100%; height: 100%; object-fit: cover; border-radius: 8px; box-shadow: 0 4px 15px rgba(0,0,0,0.5);'
                    : 'width: 100%; height: 100%; object-fit: contain; background: #000; border-radius: 8px; box-shadow: 0 4px 15px rgba(0,0,0,0.5);';

                mediaBox.innerHTML = `
                    <div style="position: relative; width: 100%; height: 100%;">
                        <video src="${shortObj.video_url}" poster="${shortObj.thumbnail_url || ''}" controls playsinline preload="metadata" style="${vidStyle}"></video>
                        <div style="position: absolute; top: 8px; left: 8px; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px); padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; color: #fff;">
                            ${isVertical ? '📱 9:16 Vertical' : '🎬 16:9 Standard'}
                        </div>
                    </div>
                `;
            }

            if (btn) btn.style.display = 'none';

            if (convertBtn) {
                convertBtn.style.display = isVertical ? 'none' : 'inline-flex';
                convertBtn.disabled = false;
                convertBtn.innerHTML = '<span>📱 Convert to 9:16 Vertical Short (Face-Centering)</span>';
            }

            if (shortObj.downloaded_cuts && shortObj.downloaded_cuts.length) {
                updateSceneCutsGallery(partNum, shortObj.downloaded_cuts);
            }

            if (uploadBtn) {
                uploadBtn.style.display = 'inline-flex';
                uploadBtn.innerHTML = `<span>🚀 Upload Part ${partNum} to YouTube</span>`;
                let dlBtn = document.getElementById(`btnDlPart_${partNum}`);
                if (!dlBtn && uploadBtn.parentNode) {
                    dlBtn = document.createElement('a');
                    dlBtn.id = `btnDlPart_${partNum}`;
                    dlBtn.href = shortObj.video_url;
                    dlBtn.download = shortObj.filename || `short_part_${partNum}.mp4`;
                    dlBtn.className = 'btn-action btn-secondary';
                    dlBtn.style.textDecoration = 'none';
                    dlBtn.style.padding = '8px 14px';
                    dlBtn.style.fontSize = '12px';
                    dlBtn.style.display = 'inline-flex';
                    dlBtn.style.alignItems = 'center';
                    dlBtn.style.gap = '6px';
                    dlBtn.innerHTML = '📥 Download MP4';
                    uploadBtn.parentNode.insertBefore(dlBtn, uploadBtn.nextSibling);
                } else if (dlBtn) {
                    dlBtn.href = shortObj.video_url;
                    dlBtn.download = shortObj.filename || `short_part_${partNum}.mp4`;
                }
            }
            updateBatchUploadVisibility();
        }

        // Convert Standard Short to 9:16 Vertical Short on Demand (Step 4)
        window.convertPartToVertical = async function(partNum) {
            const convertBtn = document.getElementById(`btnConvertVertPart_${partNum}`);
            const badge = document.getElementById(`sceneStatusBadge_${partNum}`);

            if (convertBtn) {
                convertBtn.disabled = true;
                convertBtn.innerHTML = '<span class="spinner" style="width: 12px; height: 12px; display: inline-block;"></span> Reframing to 9:16 (~5s)...';
            }
            if (badge) {
                badge.textContent = '⚙️ Converting 9:16...';
                badge.style.background = 'rgba(99, 102, 241, 0.2)';
                badge.style.color = '#a5b4fc';
            }

            try {
                const res = await fetch('/api/clipper/convert_vertical', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        job_id: currentClipperJobId,
                        part: partNum
                    })
                });
                const data = await res.json();
                if (!res.ok || !data.success) {
                    throw new Error(data.error || 'Failed to convert to 9:16 vertical');
                }

                completedClipperShorts[partNum] = data.short;
                markPartAsCompleted(partNum, data.short);
            } catch (err) {
                alert('Conversion error: ' + err.message);
                if (convertBtn) {
                    convertBtn.disabled = false;
                    convertBtn.innerHTML = '<span>📱 Convert to 9:16 Vertical Short (Face-Centering)</span>';
                }
            }
        };

        // Generate Single Part
        window.generateSinglePart = async function(partNum) {
            const btn = document.getElementById(`btnGenPart_${partNum}`);
            const badge = document.getElementById(`sceneStatusBadge_${partNum}`);
            const mediaBox = document.getElementById(`sceneMediaBox_${partNum}`);

            const scene = currentClipperScenes.find(s => s.part === partNum);
            if (!scene) return;

            // Update scene object with any edits the user made on the UI
            scene.title = document.getElementById(`sceneTitle_${partNum}`).value.trim();
            scene.hook = document.getElementById(`sceneHook_${partNum}`).value.trim();
            scene.script = document.getElementById(`sceneScript_${partNum}`).value.trim();
            const tagsInput = document.getElementById(`sceneTags_${partNum}`).value.trim();
            scene.tags = tagsInput.split(',').map(t => t.trim()).filter(Boolean);

            btn.disabled = true;
            btn.innerHTML = '<span class="spinner" style="width: 14px; height: 14px; display: inline-block;"></span> 5% Starting...';
            badge.className = 'status-badge';
            badge.style.background = 'rgba(255, 0, 85, 0.2)';
            badge.style.color = '#fda4af';
            badge.textContent = '⚙️ 5%';

            mediaBox.innerHTML = `
                <div class="placeholder-916">
                    <div class="spinner" style="border-top-color: #ff0055; width: 30px; height: 30px;"></div>
                    <span id="partStepTitle_${partNum}" style="font-size: 12px; font-weight: 600; margin-top: 8px; color: #fff;">5% Initializing pipeline...</span>
                    <span id="partStepDesc_${partNum}" style="font-size: 11px; color: var(--text-muted); text-align: center; padding: 0 10px;">Downloading cuts &amp; voiceover sync</span>
                    <div style="width: 80%; height: 6px; background: rgba(255,255,255,0.12); border-radius: 4px; margin-top: 10px; overflow: hidden;">
                        <div id="partProgressBar_${partNum}" style="width: 5%; height: 100%; background: linear-gradient(90deg, #ff0055, #ff5e8e); transition: width 0.4s ease;"></div>
                    </div>
                </div>
            `;

            const activeJobId = currentClipperJobId || 'job_' + Date.now();
            currentClipperJobId = activeJobId;
            const formatMode = (document.getElementById('clipperFormatModeSelect')?.value) || 'standard_first';
            const autoVertical = formatMode === 'auto_vertical';

            try {
                const res = await fetch('/api/clipper/generate_short', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        url: currentClipperVideoInfo.url,
                        scene: scene,
                        language: clipperLanguageSelect.value || 'Hindi',
                        video_title: currentClipperVideoInfo.title || '',
                        job_id: activeJobId,
                        auto_vertical: autoVertical,
                        voice_name: document.getElementById('clipperTtsVoiceSelect')?.value || 'Kore',
                        tone_style: document.getElementById('clipperTtsToneSelect')?.value || 'Suspense / Thriller',
                        wps: currentClipperWps || 2.23
                    })
                });

                const data = await res.json();
                if (!res.ok || !data.success) {
                    throw new Error(data.error || 'Failed to start short generation');
                }

                // If already completed synchronously or from cache
                if (data.status === 'completed' && data.short) {
                    completedClipperShorts[partNum] = data.short;
                    markPartAsCompleted(partNum, data.short);
                    return;
                }

                // Poll status every 1.5 seconds
                const pollInterval = setInterval(async () => {
                    try {
                        const sRes = await fetch(`/api/clipper/status/${encodeURIComponent(activeJobId)}/${partNum}`);
                        if (!sRes.ok) return;
                        const sData = await sRes.json();

                        const pct = sData.progress || 10;
                        const stepMsg = sData.current_step || 'Processing...';
                        const titleEl = document.getElementById(`partStepTitle_${partNum}`);
                        const descEl = document.getElementById(`partStepDesc_${partNum}`);
                        const barEl = document.getElementById(`partProgressBar_${partNum}`);

                        if (titleEl) titleEl.textContent = `${pct}% Complete`;
                        if (descEl) descEl.textContent = stepMsg;
                        if (barEl) barEl.style.width = `${pct}%`;

                        badge.textContent = `⚙️ ${pct}%`;
                        btn.innerHTML = `<span class="spinner" style="width: 14px; height: 14px; display: inline-block;"></span> ${pct}% Processing...`;

                        // Update cuts gallery in real-time as cuts finish downloading
                        if (sData.downloaded_cuts && sData.downloaded_cuts.length) {
                            updateSceneCutsGallery(partNum, sData.downloaded_cuts);
                        }

                        if (sData.status === 'completed' && sData.short) {
                            clearInterval(pollInterval);
                            completedClipperShorts[partNum] = sData.short;
                            markPartAsCompleted(partNum, sData.short);
                        } else if (sData.status === 'PAUSED_QUOTA_LIMIT' || sData.is_quota_error) {
                            clearInterval(pollInterval);
                            clipperQuotaBanner.style.display = 'block';
                            clipperQuotaBannerTitle.textContent = `Gemini Quota Exceeded at Part ${partNum} — Safely Paused`;
                            clipperQuotaBannerDesc.innerHTML = `Progress saved. Update your Gemini API key in Settings or wait for quota reset, then click <strong>Resume Job</strong>.`;
                            badge.className = 'status-badge';
                            badge.style.background = 'rgba(245, 158, 11, 0.2)';
                            badge.style.color = '#fbbf24';
                            badge.textContent = '⚠️ Paused (Quota)';
                            btn.disabled = false;
                            btn.textContent = `▶️ Retry Part ${partNum}`;
                            if (btnResumeBatchShorts) btnResumeBatchShorts.style.display = 'inline-flex';
                        } else if (sData.status === 'error') {
                            clearInterval(pollInterval);
                            alert(`Error generating Part ${partNum}: ` + (sData.error || 'Failed to render short'));
                            badge.className = 'status-badge';
                            badge.style.background = 'rgba(239, 68, 68, 0.2)';
                            badge.style.color = '#f87171';
                            badge.textContent = '❌ Failed';
                            btn.disabled = false;
                            btn.textContent = `⚡ Retry Part ${partNum}`;
                        }
                    } catch (pollErr) {
                        console.warn('Status poll warning:', pollErr);
                    }
                }, 1500);

            } catch (err) {
                console.error(`Part ${partNum} generation error:`, err);
                alert(`Error generating Part ${partNum}: ` + err.message);
                badge.className = 'status-badge';
                badge.style.background = 'rgba(239, 68, 68, 0.2)';
                badge.style.color = '#f87171';
                badge.textContent = '❌ Failed';
                btn.disabled = false;
                btn.textContent = `⚡ Retry Part ${partNum}`;
            }
        };

        // Upload Single Part to YouTube
        window.uploadSinglePart = async function(partNum) {
            const shortObj = completedClipperShorts[partNum];
            if (!shortObj) {
                alert('Please generate the short first before uploading.');
                return;
            }

            const uploadBtn = document.getElementById(`btnUploadPart_${partNum}`);
            const badge = document.getElementById(`sceneStatusBadge_${partNum}`);
            const watchBtn = document.getElementById(`btnWatchPart_${partNum}`);

            const title = document.getElementById(`sceneTitle_${partNum}`).value.trim();
            const script = document.getElementById(`sceneScript_${partNum}`).value.trim();
            const tags = document.getElementById(`sceneTags_${partNum}`).value.trim();

            uploadBtn.disabled = true;
            uploadBtn.innerHTML = '<span class="spinner" style="width: 14px; height: 14px; display: inline-block;"></span> Uploading to YouTube...';
            badge.textContent = '🚀 Uploading...';

            try {
                const res = await fetch('/api/clipper/upload_short', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        filename: shortObj.filename,
                        title: title,
                        script: script,
                        tags: tags,
                        privacy: 'public',
                        made_for_kids: false,
                        category_id: '24'
                    })
                });

                const data = await res.json();
                if (!res.ok || data.error) {
                    throw new Error(data.error || 'Failed to start upload');
                }

                // Poll task
                const taskId = data.task_id;
                const pollInterval = setInterval(async () => {
                    try {
                        const statusRes = await fetch(`/api/upload_status/${taskId}`);
                        const statusData = await statusRes.json();
                        if (statusData.status === 'uploading') {
                            const pctText = statusData.status_text || `Uploading ${Math.round((statusData.progress || 0) * 100)}%`;
                            badge.textContent = `🚀 ${pctText}`;
                        } else if (statusData.status === 'completed') {
                            clearInterval(pollInterval);
                            badge.className = 'status-badge status-uploaded';
                            badge.textContent = '🎉 Uploaded Live';
                            uploadBtn.style.display = 'none';
                            watchBtn.href = `https://youtu.be/${statusData.video_id}`;
                            watchBtn.style.display = 'inline-flex';
                            loadRecentVideos();
                        } else if (statusData.status === 'error') {
                            clearInterval(pollInterval);
                            uploadBtn.disabled = false;
                            uploadBtn.innerHTML = `🚀 Upload Part ${partNum}`;
                            badge.textContent = '❌ Upload Error';
                            alert('YouTube upload error: ' + (statusData.error || 'Unknown error'));
                        }
                    } catch (e) {
                        console.error('Status poll error', e);
                    }
                }, 1200);

            } catch (err) {
                console.error(`Part ${partNum} upload error:`, err);
                alert(`Error uploading Part ${partNum}: ` + err.message);
                uploadBtn.disabled = false;
                uploadBtn.innerHTML = `🚀 Upload Part ${partNum} to YouTube`;
            }
        };

        function updateBatchUploadVisibility() {
            const total = currentClipperScenes.length;
            const completed = Object.keys(completedClipperShorts).length;
            if (total > 0 && completed === total) {
                btnUploadAllShorts.style.display = 'inline-flex';
                btnUploadAllShorts.textContent = `🚀 One-Click Upload All ${total} Parts to YouTube`;
            }
        }

        function pollClipperBatchStatus(jobId) {
            clipperBatchProgressCard.style.display = 'block';
            const pollInterval = setInterval(async () => {
                try {
                    const res = await fetch(`/api/clipper/job_status/${jobId}`);
                    if (!res.ok) return;
                    const data = await res.json();

                    clipperBatchStepText.textContent = data.current_step || 'Processing...';
                    const pct = data.progress || 0;
                    clipperBatchPercentText.textContent = `${pct}%`;
                    clipperBatchProgressBar.style.width = `${pct}%`;

                    // Update completed parts
                    if (data.completed_shorts && Array.isArray(data.completed_shorts)) {
                        data.completed_shorts.forEach(shortObj => {
                            const p = shortObj.part;
                            if (!completedClipperShorts[p]) {
                                completedClipperShorts[p] = shortObj;
                                markPartAsCompleted(p, shortObj);
                            }
                        });
                    }

                    if (data.status === 'completed' || data.status === 'COMPLETED') {
                        clearInterval(pollInterval);
                        clipperBatchStepText.textContent = '🎉 All chronological Shorts successfully generated!';
                        clipperBatchProgressBar.style.width = '100%';
                        clipperBatchPercentText.textContent = '100%';
                        if (btnGenerateAllShorts) {
                            btnGenerateAllShorts.disabled = false;
                            btnGenerateAllShorts.textContent = '⚡ Re-Generate All Parts';
                        }
                        updateBatchUploadVisibility();
                    } else if (data.status === 'PAUSED_QUOTA_LIMIT') {
                        clearInterval(pollInterval);
                        clipperQuotaBanner.style.display = 'block';
                        clipperQuotaBannerTitle.textContent = '⚠️ Gemini Quota Reached: Job Safely Paused';
                        clipperQuotaBannerDesc.innerHTML = `${escapeHtml(data.error || 'Gemini API 429 quota reached')}. Progress saved in checkpoint. Update your API key and click <strong>Resume Job</strong>.`;
                        if (btnResumeBatchShorts) btnResumeBatchShorts.style.display = 'inline-flex';
                        if (btnGenerateAllShorts) {
                            btnGenerateAllShorts.disabled = false;
                            btnGenerateAllShorts.textContent = '▶️ Resume Job';
                        }
                    } else if (data.status === 'error') {
                        clearInterval(pollInterval);
                        if (btnGenerateAllShorts) {
                            btnGenerateAllShorts.disabled = false;
                            btnGenerateAllShorts.textContent = '⚡ Generate All Parts Sequentially';
                        }
                        alert('Batch processing notice: ' + (data.error || 'Unknown issue'));
                    }
                } catch (e) {
                    console.error('Job status poll error:', e);
                }
            }, 1800);
        }

        // Batch Generate All Parts Sequentially via Background Engine
        if (btnGenerateAllShorts) {
            btnGenerateAllShorts.addEventListener('click', async () => {
                if (!currentClipperScenes || currentClipperScenes.length === 0) return;

                btnGenerateAllShorts.disabled = true;
                btnGenerateAllShorts.innerHTML = '<span class="spinner" style="width: 14px; height: 14px; display: inline-block;"></span> Generating All Parts...';
                clipperBatchProgressCard.style.display = 'block';

                try {
                    const res = await fetch('/api/clipper/start_batch_job', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            url: currentClipperVideoInfo.url,
                            scenes: currentClipperScenes,
                            language: clipperLanguageSelect.value || 'Hindi',
                            video_title: currentClipperVideoInfo.title || '',
                            job_id: currentClipperJobId
                        })
                    });

                    const data = await res.json();
                    if (!res.ok || !data.success) {
                        throw new Error(data.error || 'Failed to start batch job');
                    }

                    currentClipperJobId = data.job_id;
                    pollClipperBatchStatus(currentClipperJobId);

                } catch (err) {
                    console.error('Batch start error:', err);
                    alert('Failed to start batch clipping: ' + err.message);
                    btnGenerateAllShorts.disabled = false;
                    btnGenerateAllShorts.textContent = '⚡ Generate All Parts Sequentially';
                }
            });
        }

        // Batch Upload All Parts Sequentially
        if (btnUploadAllShorts) {
            btnUploadAllShorts.addEventListener('click', async () => {
                if (!confirm(`Are you sure you want to sequentially upload all ${currentClipperScenes.length} parts to your connected YouTube channel?`)) {
                    return;
                }

                btnUploadAllShorts.disabled = true;
                btnUploadAllShorts.innerHTML = '<span class="spinner" style="width: 14px; height: 14px; display: inline-block;"></span> Uploading All Parts...';

                for (const scene of currentClipperScenes) {
                    if (completedClipperShorts[scene.part]) {
                        await uploadSinglePart(scene.part);
                        // Brief pause between uploads
                        await new Promise(r => setTimeout(r, 2000));
                    }
                }
            });
        }

        // ==============================================================
        // TIMELINE VIDEO TRIMMER & NARRATIVE SLICER ENGINE
        // ==============================================================
        const trimmerDropzone = document.getElementById('trimmerDropzone');
        const trimmerVideoInput = document.getElementById('trimmerVideoFileInput');
        const btnBrowseTrimmerFile = document.getElementById('btnBrowseTrimmerFile');
        const trimmerFileInfoBadge = document.getElementById('trimmerFileInfoBadge');
        const trimmerPlayer = document.getElementById('trimmerPlayer');
        const trimmerHudClipBadge = document.getElementById('trimmerHudClipBadge');
        const trimmerHudResBadge = document.getElementById('trimmerHudResBadge');
        const trimmerHudTimeBadge = document.getElementById('trimmerHudTimeBadge');
        const btnTrimmerSplit = document.getElementById('btnTrimmerSplit');
        const btnTrimmerDeleteClip = document.getElementById('btnTrimmerDeleteClip');
        const btnTrimmerReset = document.getElementById('btnTrimmerReset');
        const btnTrimmerPrevClip = document.getElementById('btnTrimmerPrevClip');
        const btnTrimmerStepBack = document.getElementById('btnTrimmerStepBack');
        const btnTrimmerPlayPause = document.getElementById('btnTrimmerPlayPause');
        const btnTrimmerStepFwd = document.getElementById('btnTrimmerStepFwd');
        const btnTrimmerNextClip = document.getElementById('btnTrimmerNextClip');
        const trimmerFocusSelect = document.getElementById('trimmerFocusSelect');
        const trimmerTargetDurSelect = document.getElementById('trimmerTargetDurSelect');
        const btnTrimmerGeminiAutoCut = document.getElementById('btnTrimmerGeminiAutoCut');
        const trimmerTimelineTrack = document.getElementById('trimmerTimelineTrack');
        const trimmerClipsContainer = document.getElementById('trimmerClipsContainer');
        const trimmerPlayhead = document.getElementById('trimmerPlayhead');
        const trimmerTimelinePlayheadTime = document.getElementById('trimmerTimelinePlayheadTime');
        const trimmerStatOrig = document.getElementById('trimmerStatOrig');
        const trimmerStatKept = document.getElementById('trimmerStatKept');
        const trimmerStatRemoved = document.getElementById('trimmerStatRemoved');
        const trimmerStatCount = document.getElementById('trimmerStatCount');
        const trimmerClipsDeck = document.getElementById('trimmerClipsDeck');
        const trimmerTtsOptionsContainer = document.getElementById('trimmerTtsOptionsContainer');
        const trimmerTtsVoiceSelect = document.getElementById('trimmerTtsVoiceSelect');
        const trimmerTtsToneSelect = document.getElementById('trimmerTtsToneSelect');
        const trimmerNarrationScript = document.getElementById('trimmerNarrationScript');
        const btnExportFinalVideo = document.getElementById('btnExportFinalVideo');
        const btnExportIcon = document.getElementById('btnExportIcon');
        const btnExportText = document.getElementById('btnExportText');
        const trimmerExportCard = document.getElementById('trimmerExportCard');
        const trimmerExportStepText = document.getElementById('trimmerExportStepText');
        const trimmerExportPercentText = document.getElementById('trimmerExportPercentText');
        const trimmerExportProgressBar = document.getElementById('trimmerExportProgressBar');
        const trimmerExportResultBox = document.getElementById('trimmerExportResultBox');
        const trimmerExportedPlayer = document.getElementById('trimmerExportedPlayer');
        const trimmerExportMetaDetails = document.getElementById('trimmerExportMetaDetails');
        const btnDownloadExportedVideo = document.getElementById('btnDownloadExportedVideo');
        const btnSendExportToYouTube = document.getElementById('btnSendExportToYouTube');

        // State variables
        let trimmerTotalDuration = 0;
        let trimmerVideoWidth = 1920;
        let trimmerVideoHeight = 1080;
        let trimmerAspectRatio = '16:9';
        let trimmerServerFilename = null;
        let trimmerLocalFile = null;
        let trimmerKeeperClips = [];
        let trimmerActiveClipIndex = 0;
        let nextClipId = 1;
        let exportPollInterval = null;

        // 20-Minute Movie Explainer Copilot Elements & State
        let currentExplainerStoryboard = null;
        const trimmerExplainerYtUrl = document.getElementById('trimmerExplainerYtUrl');
        const trimmerExplainerDuration = document.getElementById('trimmerExplainerDuration');
        const trimmerExplainerVoice = document.getElementById('trimmerExplainerVoice');
        const trimmerExplainerTone = document.getElementById('trimmerExplainerTone');
        const btnTrimmerPlanExplainer = document.getElementById('btnTrimmerPlanExplainer');
        const btnPlanExplainerIcon = document.getElementById('btnPlanExplainerIcon');
        const btnPlanExplainerText = document.getElementById('btnPlanExplainerText');
        const trimmerExplainerStoryboardContainer = document.getElementById('trimmerExplainerStoryboardContainer');
        const trimmerExplainerThumb = document.getElementById('trimmerExplainerThumb');
        const trimmerExplainerTitle = document.getElementById('trimmerExplainerTitle');
        const trimmerExplainerMetaSub = document.getElementById('trimmerExplainerMetaSub');
        const trimmerExplainerBadgeWps = document.getElementById('trimmerExplainerBadgeWps');
        const trimmerExplainerBadgeDuration = document.getElementById('trimmerExplainerBadgeDuration');
        const trimmerExplainerBadgeWords = document.getElementById('trimmerExplainerBadgeWords');
        const trimmerExplainerBadgeClips = document.getElementById('trimmerExplainerBadgeClips');
        const trimmerExplainerSummary = document.getElementById('trimmerExplainerSummary');
        const trimmerExplainerPhasesGrid = document.getElementById('trimmerExplainerPhasesGrid');
        const btnInjectExplainerCuts = document.getElementById('btnInjectExplainerCuts');

        function renderExplainerStoryboardUI(storyboard) {
            if (!storyboard || !storyboard.keeper_clips) return;
            if (trimmerExplainerStoryboardContainer) trimmerExplainerStoryboardContainer.style.display = 'block';

            if (trimmerExplainerThumb && storyboard.thumbnail) {
                trimmerExplainerThumb.src = storyboard.thumbnail;
                trimmerExplainerThumb.style.display = 'block';
            }
            if (trimmerExplainerTitle) {
                trimmerExplainerTitle.textContent = storyboard.title || 'Cinema Explainer Storyboard';
            }
            if (trimmerExplainerMetaSub) {
                trimmerExplainerMetaSub.innerHTML = `Runtime: <b>${storyboard.duration_str || 'Full Movie'}</b> &bull; Channel: <b>${storyboard.channel || 'Cinema Channel'}</b> &bull; Voice: <b>${storyboard.voice_name || 'Kore'} (${storyboard.tone_style || 'Narrative Deep'})</b>`;
            }
            if (trimmerExplainerBadgeDuration) {
                const totalDur = storyboard.total_duration_sec || storyboard.target_duration || 0;
                trimmerExplainerBadgeDuration.textContent = `⏱️ Explainer: ${formatSecs(totalDur)}`;
            }
            if (trimmerExplainerBadgeClips) {
                trimmerExplainerBadgeClips.textContent = `🎬 Dynamic Cuts: ${storyboard.total_clips || storyboard.keeper_clips.length}`;
            }
            if (trimmerExplainerBadgeWps) {
                trimmerExplainerBadgeWps.textContent = `⚡ WPS: ${(storyboard.calibrated_wps || 2.35).toFixed(2)} w/s`;
            }
            if (trimmerExplainerBadgeWords) {
                trimmerExplainerBadgeWords.textContent = `📝 Words: ${storyboard.total_words || 0}`;
            }
            if (trimmerExplainerSummary) {
                trimmerExplainerSummary.textContent = storyboard.summary || 'Pure story-first cinema explainer covering all narrative beats without rigid cuts.';
            }

            if (trimmerExplainerPhasesGrid) {
                trimmerExplainerPhasesGrid.innerHTML = '';
                const clips = storyboard.keeper_clips || [];

                const phaseConfigs = [
                    {
                        key: "Phase 1",
                        label: "Phase 1: Setup & Hook",
                        subtitle: "Inciting Incident & World Building",
                        color: "#38bdf8",
                        bg: "rgba(56, 189, 248, 0.08)",
                        border: "rgba(56, 189, 248, 0.3)",
                        icon: "🎬"
                    },
                    {
                        key: "Phase 2",
                        label: "Phase 2: Rising Stakes",
                        subtitle: "Dangerous Escalation & Trials",
                        color: "#a855f7",
                        bg: "rgba(168, 85, 247, 0.08)",
                        border: "rgba(168, 85, 247, 0.3)",
                        icon: "🔥"
                    },
                    {
                        key: "Phase 3",
                        label: "Phase 3: Major Twists",
                        subtitle: "Darkest Hour & Revelation",
                        color: "#f59e0b",
                        bg: "rgba(245, 158, 11, 0.08)",
                        border: "rgba(245, 158, 11, 0.3)",
                        icon: "⚡"
                    },
                    {
                        key: "Phase 4",
                        label: "Phase 4: Epic Climax",
                        subtitle: "High-Octane Showdown & Closure",
                        color: "#10b981",
                        bg: "rgba(16, 185, 129, 0.08)",
                        border: "rgba(16, 185, 129, 0.3)",
                        icon: "🏆"
                    }
                ];

                let groups = {};
                if (storyboard.phase_groups && Object.keys(storyboard.phase_groups).length > 0) {
                    const keys = Object.keys(storyboard.phase_groups);
                    keys.forEach((k, idx) => {
                        const targetCfg = phaseConfigs[idx] || phaseConfigs[0];
                        groups[targetCfg.key] = storyboard.phase_groups[k] || [];
                    });
                } else {
                    const p1 = clips.filter(c => String(c.phase || '').includes('1') || String(c.title || '').toLowerCase().includes('phase 1'));
                    const p2 = clips.filter(c => String(c.phase || '').includes('2') || String(c.title || '').toLowerCase().includes('phase 2'));
                    const p3 = clips.filter(c => String(c.phase || '').includes('3') || String(c.title || '').toLowerCase().includes('phase 3'));
                    const p4 = clips.filter(c => String(c.phase || '').includes('4') || String(c.title || '').toLowerCase().includes('phase 4'));

                    if (p1.length || p2.length || p3.length || p4.length) {
                        groups["Phase 1"] = p1;
                        groups["Phase 2"] = p2;
                        groups["Phase 3"] = p3;
                        groups["Phase 4"] = p4;
                    } else {
                        const cLen = clips.length;
                        const q1 = Math.max(1, Math.round(cLen * 0.25));
                        const q2 = Math.max(1, Math.round(cLen * 0.50));
                        const q3 = Math.max(1, Math.round(cLen * 0.75));
                        groups["Phase 1"] = clips.slice(0, q1);
                        groups["Phase 2"] = clips.slice(q1, q2);
                        groups["Phase 3"] = clips.slice(q2, q3);
                        groups["Phase 4"] = clips.slice(q3);
                    }
                }

                phaseConfigs.forEach(cfg => {
                    const phaseClips = groups[cfg.key] || [];
                    const phaseDur = phaseClips.reduce((acc, c) => acc + (parseFloat(c.duration) || (parseFloat(c.end) - parseFloat(c.start)) || 0), 0);
                    const microCount = phaseClips.filter(c => (parseFloat(c.duration) || 0) <= 5.0).length;

                    const col = document.createElement('div');
                    col.style.background = cfg.bg;
                    col.style.border = `1px solid ${cfg.border}`;
                    col.style.borderRadius = '8px';
                    col.style.padding = '10px';
                    col.style.display = 'flex';
                    col.style.flexDirection = 'column';
                    col.style.gap = '8px';

                    col.innerHTML = `
                        <div style="display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 1px solid ${cfg.border}; padding-bottom: 6px;">
                            <div>
                                <div style="font-weight: 700; color: ${cfg.color}; font-size: 12.5px; display: flex; align-items: center; gap: 4px;">
                                    <span>${cfg.icon}</span> <span>${cfg.label}</span>
                                </div>
                                <div style="color: #94a3b8; font-size: 10px;">${cfg.subtitle}</div>
                            </div>
                            <div style="text-align: right;">
                                <div style="color: #f1f5f9; font-weight: 700; font-size: 11px;">${formatSecs(phaseDur)}</div>
                                <div style="color: #94a3b8; font-size: 9.5px;">${phaseClips.length} cuts (${microCount} micro)</div>
                            </div>
                        </div>
                        <div style="max-height: 320px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; padding-right: 2px;">
                            ${phaseClips.map(c => {
                                const dur = parseFloat(c.duration) || (parseFloat(c.end) - parseFloat(c.start)) || 0;
                                const isMicro = dur <= 5.0;
                                const badgeStyle = isMicro
                                    ? 'background: rgba(6,182,212,0.18); color: #38bdf8; border: 1px solid rgba(6,182,212,0.4);'
                                    : 'background: rgba(168,85,247,0.18); color: #c084fc; border: 1px solid rgba(168,85,247,0.4);';
                                const badgeText = isMicro ? `⚡ MICRO ${dur.toFixed(1)}s` : `🎬 SCENE ${dur.toFixed(1)}s`;
                                const beatTag = c.beat || (isMicro ? '[Hook]' : '[Action]');
                                const targetWords = c.target_words || Math.round(dur * (storyboard.calibrated_wps || 2.35));
                                const narr = (c.narration || c.reason || '').trim();

                                return `
                                    <div style="background: rgba(0,0,0,0.35); padding: 6px 8px; border-radius: 5px; border: 1px solid rgba(255,255,255,0.06); font-size: 11px;">
                                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 3px; gap: 6px;">
                                            <span style="font-weight: 600; color: #f8fafc; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${c.title || 'Scene Beat'}</span>
                                            <span style="${badgeStyle} font-size: 9px; padding: 1px 5px; border-radius: 4px; font-weight: 700; white-space: nowrap;">${badgeText}</span>
                                        </div>
                                        <div style="display: flex; justify-content: space-between; align-items: center; font-size: 10px; color: #94a3b8; font-family: monospace; margin-bottom: 3px;">
                                            <span style="color: ${cfg.color}; font-weight: 600;">${beatTag}</span>
                                            <span>${formatSecs(c.start)} - ${formatSecs(c.end)}</span>
                                        </div>
                                        ${narr ? `<div style="color: #cbd5e1; font-size: 10.5px; line-height: 1.3; background: rgba(255,255,255,0.03); padding: 4px 6px; border-radius: 3px; border-left: 2px solid ${cfg.color}; margin-top: 2px;">
                                            ${narr}
                                            <span style="color: #64748b; font-size: 9.5px; margin-left: 4px;">(${targetWords}w)</span>
                                        </div>` : ''}
                                    </div>
                                `;
                            }).join('')}
                        </div>
                    `;
                    trimmerExplainerPhasesGrid.appendChild(col);
                });
            }
        }

        function applyExplainerStoryboardToTimeline(storyboard) {
            if (!storyboard || !storyboard.keeper_clips || storyboard.keeper_clips.length === 0) return;
            const clips = storyboard.keeper_clips;

            nextClipId = 1;
            trimmerKeeperClips = clips.map((c, idx) => {
                let s = Math.max(0, parseFloat(c.start) || 0);
                let e = parseFloat(c.end) || (s + 60);
                if (trimmerTotalDuration > 0) {
                    if (s >= trimmerTotalDuration) s = Math.max(0, trimmerTotalDuration - 30);
                    if (e > trimmerTotalDuration) e = trimmerTotalDuration;
                }
                const dur = Math.max(0.5, e - s);
                return {
                    id: nextClipId++,
                    start: s,
                    end: e,
                    duration: dur,
                    title: c.title || `Scene ${idx + 1}`,
                    reason: c.reason || c.phase || 'Narrative Keeper Cut',
                    narration: c.narration || ''
                };
            }).filter(c => c.end > c.start + 0.3);

            if (trimmerKeeperClips.length === 0) {
                resetTrimmerClips();
                return;
            }

            trimmerActiveClipIndex = 0;

            if (trimmerNarrationScript) {
                trimmerNarrationScript.value = storyboard.full_script || clips.map(c => c.narration || '').filter(Boolean).join('\\n\\n');
            }

            const ttsBgmRadio = document.querySelector('input[name="trimmerAudioMode"][value="tts_bgm"]');
            if (ttsBgmRadio) {
                ttsBgmRadio.checked = true;
                if (trimmerTtsOptionsContainer) trimmerTtsOptionsContainer.style.display = 'block';
            }

            if (trimmerTtsVoiceSelect && storyboard.voice_name) {
                trimmerTtsVoiceSelect.value = storyboard.voice_name;
            }
            if (trimmerTtsToneSelect && storyboard.tone_style) {
                trimmerTtsToneSelect.value = storyboard.tone_style;
            }

            renderTrimmerTimelineUI();
            updateTrimmerStats();
            updateTrimmerDeck();

            if (trimmerKeeperClips.length > 0) {
                trimmerPlayer.currentTime = trimmerKeeperClips[0].start;
            }

            const totalKept = trimmerKeeperClips.reduce((acc, c) => acc + c.duration, 0);
            const noticeEl = document.getElementById('trimmerExplainerAutoInjectNotice');
            if (noticeEl) {
                noticeEl.innerHTML = `✅ <b>Injected into Timeline:</b> ${trimmerKeeperClips.length} keeper cuts (${formatSecs(totalKept)}) with synchronized narration script! Discarded all filler scenes.`;
                noticeEl.style.background = 'rgba(16, 185, 129, 0.2)';
                noticeEl.style.borderColor = '#10b981';
            }
        }

        // Toggle TTS voiceover options UI when audio mode changes
        document.querySelectorAll('input[name="trimmerAudioMode"]').forEach(radio => {
            radio.addEventListener('change', (e) => {
                const mode = e.target.value;
                if (mode === 'tts' || mode === 'tts_bgm') {
                    trimmerTtsOptionsContainer.style.display = 'block';
                } else {
                    trimmerTtsOptionsContainer.style.display = 'none';
                }
            });
        });

        // File selection handling
        if (btnBrowseTrimmerFile && trimmerVideoInput) {
            btnBrowseTrimmerFile.addEventListener('click', () => trimmerVideoInput.click());
        }
        if (trimmerDropzone) {
            trimmerDropzone.addEventListener('click', (e) => {
                if (e.target !== btnBrowseTrimmerFile && trimmerVideoInput) trimmerVideoInput.click();
            });
            trimmerDropzone.addEventListener('dragover', (e) => { e.preventDefault(); trimmerDropzone.style.borderColor = '#38bdf8'; });
            trimmerDropzone.addEventListener('dragleave', () => { trimmerDropzone.style.borderColor = '#0284c7'; });
            trimmerDropzone.addEventListener('drop', (e) => {
                e.preventDefault();
                trimmerDropzone.style.borderColor = '#0284c7';
                if (e.dataTransfer.files && e.dataTransfer.files[0]) {
                    handleTrimmerVideoFile(e.dataTransfer.files[0]);
                }
            });
        }

        if (trimmerVideoInput) {
            trimmerVideoInput.addEventListener('change', (e) => {
                if (trimmerVideoInput.files && trimmerVideoInput.files[0]) {
                    handleTrimmerVideoFile(trimmerVideoInput.files[0]);
                }
            });
        }

        function formatSecs(sec) {
            sec = Math.max(0, Math.round(sec));
            const m = Math.floor(sec / 60);
            const s = sec % 60;
            const h = Math.floor(m / 60);
            const remM = m % 60;
            if (h > 0) {
                return `${h.toString().padStart(2, '0')}:${remM.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
            }
            return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
        }

        async function handleTrimmerVideoFile(file) {
            trimmerLocalFile = file;
            const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
            
            // 0-second instant local playback
            const objectUrl = URL.createObjectURL(file);
            trimmerPlayer.src = objectUrl;
            
            trimmerPlayer.onloadedmetadata = () => {
                trimmerTotalDuration = trimmerPlayer.duration || 60;
                trimmerVideoWidth = trimmerPlayer.videoWidth || 1920;
                trimmerVideoHeight = trimmerPlayer.videoHeight || 1080;
                
                // Aspect Ratio calculation
                const ratio = (trimmerVideoWidth / Math.max(1, trimmerVideoHeight)).toFixed(2);
                trimmerAspectRatio = `${trimmerVideoWidth}x${trimmerVideoHeight} (${ratio}:1)`;
                
                trimmerHudResBadge.textContent = `${trimmerVideoWidth}x${trimmerVideoHeight} (Original Aspect Ratio)`;
                trimmerFileInfoBadge.innerHTML = `🎥 <b>${file.name}</b> &bull; ${formatSecs(trimmerTotalDuration)} &bull; ${sizeMb} MB &bull; ${trimmerAspectRatio}`;
                trimmerFileInfoBadge.style.display = 'inline-block';

                // Auto-inject 20-minute explainer cuts if storyboard exists, else initialize full video as 1 clip
                if (currentExplainerStoryboard && currentExplainerStoryboard.keeper_clips && currentExplainerStoryboard.keeper_clips.length > 0) {
                    applyExplainerStoryboardToTimeline(currentExplainerStoryboard);
                } else {
                    resetTrimmerClips();
                }
            };

            // Upload in background to server for FFmpeg slicing
            uploadTrimmerFileToServer(file);
        }

        async function uploadTrimmerFileToServer(file) {
            const formData = new FormData();
            formData.append('video_file', file);
            try {
                trimmerFileInfoBadge.innerHTML += ` &bull; <span id="trimmerUploadStatusSpan" style="color: #facc15;">Uploading to cloud...</span>`;
                const res = await fetch('/api/trimmer/upload', {
                    method: 'POST',
                    body: formData
                });
                const data = await res.json();
                if (data.success) {
                    trimmerServerFilename = data.filename;
                    const statusSpan = document.getElementById('trimmerUploadStatusSpan');
                    if (statusSpan) {
                        statusSpan.style.color = '#6ee7b7';
                        statusSpan.textContent = 'Ready for Slicing & Export';
                    }
                }
            } catch (err) {
                console.warn('Background trimmer upload notice:', err);
            }
        }

        function resetTrimmerClips() {
            nextClipId = 1;
            trimmerKeeperClips = [{
                id: nextClipId++,
                start: 0,
                end: trimmerTotalDuration,
                duration: trimmerTotalDuration,
                title: 'Full Original Sequence',
                reason: 'Uncut source clip'
            }];
            trimmerActiveClipIndex = 0;
            renderTrimmerTimelineUI();
            updateTrimmerStats();
            updateTrimmerDeck();
        }

        if (btnTrimmerReset) {
            btnTrimmerReset.addEventListener('click', resetTrimmerClips);
        }

        // Timeline rendering
        function renderTrimmerTimelineUI() {
            if (!trimmerClipsContainer) return;
            trimmerClipsContainer.innerHTML = '';
            if (trimmerTotalDuration <= 0) return;

            const colors = [
                'linear-gradient(135deg, #0284c7, #2563eb)',
                'linear-gradient(135deg, #7c3aed, #9333ea)',
                'linear-gradient(135deg, #059669, #10b981)',
                'linear-gradient(135deg, #d97706, #f59e0b)',
                'linear-gradient(135deg, #e11d48, #f43f5e)',
                'linear-gradient(135deg, #4f46e5, #6366f1)'
            ];

            // Render each keeper clip block
            trimmerKeeperClips.forEach((clip, idx) => {
                const startPct = Math.max(0, (clip.start / trimmerTotalDuration) * 100);
                const widthPct = Math.max(0.5, ((clip.end - clip.start) / trimmerTotalDuration) * 100);
                const color = colors[idx % colors.length];

                const block = document.createElement('div');
                block.className = 'trimmer-clip-block';
                block.style.position = 'absolute';
                block.style.left = `${startPct}%`;
                block.style.width = `${widthPct}%`;
                block.style.top = '2px';
                block.style.bottom = '2px';
                block.style.background = color;
                block.style.borderRadius = '4px';
                block.style.border = (idx === trimmerActiveClipIndex) ? '2px solid #fff' : '1px solid rgba(255,255,255,0.2)';
                block.style.boxShadow = (idx === trimmerActiveClipIndex) ? '0 0 10px rgba(56, 189, 248, 0.6)' : 'none';
                block.style.display = 'flex';
                block.style.alignItems = 'center';
                block.style.padding = '0 6px';
                block.style.fontSize = '11px';
                block.style.fontWeight = '700';
                block.style.color = '#fff';
                block.style.overflow = 'hidden';
                block.style.whiteSpace = 'nowrap';
                block.style.textOverflow = 'ellipsis';
                block.style.cursor = 'pointer';
                block.title = `${clip.title || 'Clip ' + (idx + 1)}: ${formatSecs(clip.start)} - ${formatSecs(clip.end)} (${clip.duration.toFixed(1)}s)`;
                block.textContent = `#${idx + 1} (${clip.duration.toFixed(0)}s)`;

                block.addEventListener('click', (e) => {
                    e.stopPropagation();
                    trimmerActiveClipIndex = idx;
                    trimmerPlayer.currentTime = clip.start;
                    trimmerPlayer.play();
                    renderTrimmerTimelineUI();
                    updateTrimmerDeck();
                });

                trimmerClipsContainer.appendChild(block);
            });
        }

        function updateTrimmerStats() {
            const keptTotal = trimmerKeeperClips.reduce((acc, c) => acc + (c.end - c.start), 0);
            const fillerTotal = Math.max(0, trimmerTotalDuration - keptTotal);
            const fillerPct = trimmerTotalDuration > 0 ? ((fillerTotal / trimmerTotalDuration) * 100).toFixed(1) : 0;

            if (trimmerStatOrig) trimmerStatOrig.textContent = `⏱️ Original: ${formatSecs(trimmerTotalDuration)}`;
            if (trimmerStatKept) trimmerStatKept.textContent = `✂️ Kept: ${formatSecs(keptTotal)}`;
            if (trimmerStatRemoved) trimmerStatRemoved.textContent = `🗑️ Filler Discarded: ${formatSecs(fillerTotal)} (${fillerPct}%)`;
            if (trimmerStatCount) trimmerStatCount.textContent = `🎬 Keeper Clips: ${trimmerKeeperClips.length}`;
            if (trimmerHudClipBadge) trimmerHudClipBadge.textContent = `Clip ${trimmerActiveClipIndex + 1} of ${trimmerKeeperClips.length}`;
        }

        function updateTrimmerDeck() {
            if (!trimmerClipsDeck) return;
            trimmerClipsDeck.innerHTML = '';
            trimmerKeeperClips.forEach((clip, idx) => {
                const card = document.createElement('div');
                card.style.display = 'flex';
                card.style.justifyContent = 'space-between';
                card.style.alignItems = 'center';
                card.style.padding = '8px 14px';
                card.style.background = (idx === trimmerActiveClipIndex) ? 'rgba(2, 132, 199, 0.15)' : 'rgba(255,255,255,0.03)';
                card.style.border = (idx === trimmerActiveClipIndex) ? '1px solid #0284c7' : '1px solid rgba(255,255,255,0.06)';
                card.style.borderRadius = '6px';
                card.style.gap = '12px';

                card.innerHTML = `
                    <div style="display: flex; align-items: center; gap: 10px; flex: 1;">
                        <span style="font-weight: 700; color: #38bdf8; font-size: 13px;">#${idx + 1}</span>
                        <div>
                            <div style="font-size: 13px; font-weight: 600; color: #f1f5f9;">${clip.title || 'Scene ' + (idx + 1)}</div>
                            <div style="font-size: 11px; color: var(--text-muted);">${clip.reason || 'Keeper clip'}</div>
                        </div>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span style="font-size: 12px; font-weight: 700; color: #bae6fd; background: rgba(0,0,0,0.3); padding: 4px 8px; border-radius: 4px;">
                            ${formatSecs(clip.start)} ➔ ${formatSecs(clip.end)} (${clip.duration.toFixed(1)}s)
                        </span>
                        <button type="button" class="btn-populate" style="padding: 4px 8px; font-size: 12px;" onclick="playKeeperClipByIndex(${idx})">▶️</button>
                        <button type="button" class="btn-populate" style="padding: 4px 8px; font-size: 12px; color: #f87171; border-color: rgba(239,68,68,0.4);" onclick="deleteKeeperClipByIndex(${idx})">🗑️</button>
                    </div>
                `;
                trimmerClipsDeck.appendChild(card);
            });
        }

        window.playKeeperClipByIndex = (idx) => {
            if (idx >= 0 && idx < trimmerKeeperClips.length) {
                trimmerActiveClipIndex = idx;
                trimmerPlayer.currentTime = trimmerKeeperClips[idx].start;
                trimmerPlayer.play();
                renderTrimmerTimelineUI();
                updateTrimmerDeck();
            }
        };

        window.deleteKeeperClipByIndex = (idx) => {
            if (trimmerKeeperClips.length <= 1) {
                alert('You must have at least one keeper clip. Reset timeline if you want to restore full video.');
                return;
            }
            trimmerKeeperClips.splice(idx, 1);
            if (trimmerActiveClipIndex >= trimmerKeeperClips.length) {
                trimmerActiveClipIndex = trimmerKeeperClips.length - 1;
            }
            trimmerPlayer.currentTime = trimmerKeeperClips[trimmerActiveClipIndex].start;
            renderTrimmerTimelineUI();
            updateTrimmerStats();
            updateTrimmerDeck();
        };

        // Manual Split / Cut at Playhead
        if (btnTrimmerSplit) {
            btnTrimmerSplit.addEventListener('click', () => {
                if (trimmerKeeperClips.length === 0) return;
                const cur = trimmerPlayer.currentTime;

                // Find which clip contains playhead
                const clipIdx = trimmerKeeperClips.findIndex(c => cur > c.start + 0.3 && cur < c.end - 0.3);
                if (clipIdx === -1) {
                    alert('Playhead must be inside a clip with at least 0.5s margin to cut.');
                    return;
                }

                const target = trimmerKeeperClips[clipIdx];
                const clipA = {
                    id: nextClipId++,
                    start: target.start,
                    end: cur,
                    duration: cur - target.start,
                    title: `${target.title || 'Scene'} (Part A)`,
                    reason: target.reason
                };
                const clipB = {
                    id: nextClipId++,
                    start: cur,
                    end: target.end,
                    duration: target.end - cur,
                    title: `${target.title || 'Scene'} (Part B)`,
                    reason: target.reason
                };

                trimmerKeeperClips.splice(clipIdx, 1, clipA, clipB);
                trimmerActiveClipIndex = clipIdx;
                renderTrimmerTimelineUI();
                updateTrimmerStats();
                updateTrimmerDeck();
            });
        }

        // Delete Active Clip
        if (btnTrimmerDeleteClip) {
            btnTrimmerDeleteClip.addEventListener('click', () => {
                if (trimmerKeeperClips.length <= 1) {
                    alert('Cannot delete the only remaining clip.');
                    return;
                }
                deleteKeeperClipByIndex(trimmerActiveClipIndex);
            });
        }

        // Transport Controls
        if (btnTrimmerPlayPause) {
            btnTrimmerPlayPause.addEventListener('click', () => {
                if (trimmerPlayer.paused) trimmerPlayer.play();
                else trimmerPlayer.pause();
            });
            trimmerPlayer.addEventListener('play', () => btnTrimmerPlayPause.innerHTML = '⏸️ Pause');
            trimmerPlayer.addEventListener('pause', () => btnTrimmerPlayPause.innerHTML = '▶️ Play');
        }

        if (btnTrimmerStepBack) {
            btnTrimmerStepBack.addEventListener('click', () => {
                trimmerPlayer.currentTime = Math.max(0, trimmerPlayer.currentTime - 1);
            });
        }
        if (btnTrimmerStepFwd) {
            btnTrimmerStepFwd.addEventListener('click', () => {
                trimmerPlayer.currentTime = Math.min(trimmerTotalDuration, trimmerPlayer.currentTime + 1);
            });
        }
        if (btnTrimmerPrevClip) {
            btnTrimmerPrevClip.addEventListener('click', () => {
                if (trimmerActiveClipIndex > 0) {
                    playKeeperClipByIndex(trimmerActiveClipIndex - 1);
                }
            });
        }
        if (btnTrimmerNextClip) {
            btnTrimmerNextClip.addEventListener('click', () => {
                if (trimmerActiveClipIndex < trimmerKeeperClips.length - 1) {
                    playKeeperClipByIndex(trimmerActiveClipIndex + 1);
                }
            });
        }

        // Click on timeline scrubber
        if (trimmerTimelineTrack) {
            trimmerTimelineTrack.addEventListener('click', (e) => {
                if (trimmerTotalDuration <= 0) return;
                const rect = trimmerTimelineTrack.getBoundingClientRect();
                const clickX = e.clientX - rect.left;
                const targetTime = (clickX / rect.width) * trimmerTotalDuration;

                // Check if inside a keeper clip or filler
                const foundIdx = trimmerKeeperClips.findIndex(c => targetTime >= c.start && targetTime <= c.end);
                if (foundIdx !== -1) {
                    trimmerActiveClipIndex = foundIdx;
                    trimmerPlayer.currentTime = targetTime;
                } else {
                    // Inside deleted filler gap! Snap to start of next keeper clip
                    const nextIdx = trimmerKeeperClips.findIndex(c => c.start > targetTime);
                    if (nextIdx !== -1) {
                        trimmerActiveClipIndex = nextIdx;
                        trimmerPlayer.currentTime = trimmerKeeperClips[nextIdx].start;
                    } else if (trimmerKeeperClips.length > 0) {
                        trimmerActiveClipIndex = 0;
                        trimmerPlayer.currentTime = trimmerKeeperClips[0].start;
                    }
                }
                renderTrimmerTimelineUI();
                updateTrimmerDeck();
            });
        }

        // SEAMLESS BACK-TO-BACK REAL-TIME PLAYBACK ENGINE
        if (trimmerPlayer) {
            trimmerPlayer.addEventListener('timeupdate', () => {
                if (trimmerTotalDuration <= 0 || trimmerKeeperClips.length === 0) return;
                const cur = trimmerPlayer.currentTime;

                // Update playhead UI
                const playheadPct = Math.min(100, Math.max(0, (cur / trimmerTotalDuration) * 100));
                if (trimmerPlayhead) trimmerPlayhead.style.left = `${playheadPct}%`;
                if (trimmerTimelinePlayheadTime) trimmerTimelinePlayheadTime.textContent = `Playhead: ${formatSecs(cur)}`;
                if (trimmerHudTimeBadge) trimmerHudTimeBadge.textContent = `${formatSecs(cur)} / ${formatSecs(trimmerTotalDuration)}`;

                const activeClip = trimmerKeeperClips[trimmerActiveClipIndex];
                if (activeClip) {
                    // If reached end of active keeper clip: jump seamlessly to next keeper clip!
                    if (cur >= activeClip.end - 0.05) {
                        if (trimmerActiveClipIndex < trimmerKeeperClips.length - 1) {
                            trimmerActiveClipIndex++;
                            trimmerPlayer.currentTime = trimmerKeeperClips[trimmerActiveClipIndex].start;
                            renderTrimmerTimelineUI();
                            updateTrimmerDeck();
                            updateTrimmerStats();
                        } else {
                            // End of all clips
                            trimmerPlayer.pause();
                            trimmerActiveClipIndex = 0;
                            trimmerPlayer.currentTime = trimmerKeeperClips[0].start;
                            renderTrimmerTimelineUI();
                            updateTrimmerDeck();
                            updateTrimmerStats();
                        }
                    } else if (cur < activeClip.start - 0.1) {
                        // Check if jumped into another clip or gap
                        const found = trimmerKeeperClips.findIndex(c => cur >= c.start && cur <= c.end);
                        if (found !== -1) {
                            trimmerActiveClipIndex = found;
                            renderTrimmerTimelineUI();
                            updateTrimmerDeck();
                            updateTrimmerStats();
                        } else {
                            // In gap: snap forward to next clip start
                            const next = trimmerKeeperClips.find(c => c.start > cur);
                            if (next) {
                                trimmerActiveClipIndex = trimmerKeeperClips.indexOf(next);
                                trimmerPlayer.currentTime = next.start;
                                renderTrimmerTimelineUI();
                                updateTrimmerDeck();
                                updateTrimmerStats();
                            }
                        }
                    }
                }
            });
        }

        // GEMINI AUTO-DISCOVERY CUT & DELETE
        if (btnTrimmerGeminiAutoCut) {
            btnTrimmerGeminiAutoCut.addEventListener('click', async () => {
                if (trimmerTotalDuration <= 0) {
                    alert('Please upload or load a video first.');
                    return;
                }

                btnTrimmerGeminiAutoCut.disabled = true;
                btnTrimmerGeminiAutoCut.innerHTML = '<span class="spinner" style="width: 14px; height: 14px; display: inline-block;"></span> Analyzing Story Arc...';

                const payload = {
                    filename: trimmerServerFilename || (trimmerLocalFile ? trimmerLocalFile.name : ''),
                    duration: trimmerTotalDuration,
                    focus_style: trimmerFocusSelect ? trimmerFocusSelect.value : 'Key Dramatic Highlights',
                    target_duration: parseInt(trimmerTargetDurSelect ? trimmerTargetDurSelect.value : '0') || 0,
                    language: 'Hindi'
                };

                try {
                    const res = await fetch('/api/trimmer/gemini_autocut', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    const data = await res.json();
                    if (data.success && data.keeper_clips && data.keeper_clips.length > 0) {
                        nextClipId = 1;
                        trimmerKeeperClips = data.keeper_clips.map(c => ({
                            id: nextClipId++,
                            start: c.start,
                            end: c.end,
                            duration: c.duration,
                            title: c.title,
                            reason: c.reason
                        }));
                        trimmerActiveClipIndex = 0;

                        // Automatically update narration script
                        if (data.script && trimmerNarrationScript) {
                            trimmerNarrationScript.value = data.script;
                        }

                        // Seamlessly update player to start of first keeper clip
                        trimmerPlayer.currentTime = trimmerKeeperClips[0].start;
                        trimmerPlayer.play();

                        renderTrimmerTimelineUI();
                        updateTrimmerStats();
                        updateTrimmerDeck();

                        alert(`Gemini Auto-Cut Complete! Applied ${trimmerKeeperClips.length} narrative keeper cuts and discarded ${data.filler_removed_percent}% filler.`);
                    } else {
                        alert('Gemini auto-cut did not return clips. Please check logs.');
                    }
                } catch (err) {
                    alert('Error during Gemini Auto-Cut: ' + err.message);
                } finally {
                    btnTrimmerGeminiAutoCut.disabled = false;
                    btnTrimmerGeminiAutoCut.innerHTML = '<span>🤖</span> <span>Gemini Auto-Cut &amp; Trim</span>';
                }
            });
        }

        // CINEMA EXPLAINER STORYBOARD COPILOT (PURE STORY-FIRST DYNAMIC PACING)
        if (btnTrimmerPlanExplainer) {
            btnTrimmerPlanExplainer.addEventListener('click', async () => {
                const url = (trimmerExplainerYtUrl ? trimmerExplainerYtUrl.value : '').trim();
                if (!url) {
                    alert('Please enter a YouTube movie URL to generate the Cinema Explainer storyboard.');
                    if (trimmerExplainerYtUrl) trimmerExplainerYtUrl.focus();
                    return;
                }

                btnTrimmerPlanExplainer.disabled = true;
                if (btnPlanExplainerIcon) btnPlanExplainerIcon.innerHTML = '<span class="spinner" style="width: 14px; height: 14px; display: inline-block;"></span>';
                if (btnPlanExplainerText) btnPlanExplainerText.textContent = 'Generating Cinema Storyboard...';

                try {
                    const durVal = trimmerExplainerDuration ? trimmerExplainerDuration.value : 'dynamic';
                    const payload = {
                        youtube_url: url,
                        target_duration: durVal === 'dynamic' ? 'dynamic' : (parseInt(durVal) || 'dynamic'),
                        voice_name: trimmerExplainerVoice ? trimmerExplainerVoice.value : 'Kore',
                        tone_style: trimmerExplainerTone ? trimmerExplainerTone.value : 'Narrative Deep Storytelling',
                        language: 'Hindi'
                    };

                    const res = await fetch('/api/trimmer/plan_explainer', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    let data;
                    try {
                        data = await res.json();
                    } catch (parseErr) {
                        throw new Error(`Server returned non-JSON response (HTTP ${res.status}).`);
                    }

                    if (!data.success && data.error) {
                        throw new Error(data.error);
                    }

                    currentExplainerStoryboard = data;
                    renderExplainerStoryboardUI(data);

                    if (trimmerTotalDuration > 0) {
                        applyExplainerStoryboardToTimeline(data);
                        alert(`Cinema Explainer Ready!\nAnalyzed "${data.title}" and auto-injected ${data.total_clips} dynamic cuts totaling ${formatSecs(data.total_duration_sec)} into your timeline with millisecond-synced Hindi voiceover!`);
                    } else {
                        const noticeEl = document.getElementById('trimmerExplainerAutoInjectNotice');
                        if (noticeEl) {
                            noticeEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        }
                    }
                } catch (err) {
                    alert('Cinema Explainer Error: ' + err.message);
                } finally {
                    btnTrimmerPlanExplainer.disabled = false;
                    if (btnPlanExplainerIcon) btnPlanExplainerIcon.innerHTML = '🎬';
                    if (btnPlanExplainerText) btnPlanExplainerText.textContent = 'Generate Cinema Explainer Storyboard';
                }
            });
        }

        if (btnInjectExplainerCuts) {
            btnInjectExplainerCuts.addEventListener('click', () => {
                if (!currentExplainerStoryboard) {
                    alert('Please generate a Cinema Explainer storyboard first using a YouTube URL.');
                    return;
                }
                if (trimmerTotalDuration <= 0) {
                    alert('Please select or drag your movie video file below first.');
                    return;
                }
                applyExplainerStoryboardToTimeline(currentExplainerStoryboard);
            });
        }

        // EXPORT FINAL VIDEO PIPELINE (100% ORIGINAL RESOLUTION PRESERVED)
        if (btnExportFinalVideo) {
            btnExportFinalVideo.addEventListener('click', async () => {
                if (trimmerKeeperClips.length === 0) {
                    alert('No clips to export.');
                    return;
                }

                if (!trimmerServerFilename) {
                    alert('Video is still uploading to server. Please wait a few moments and try again.');
                    return;
                }

                const audioModeInput = document.querySelector('input[name="trimmerAudioMode"]:checked');
                const audioMode = audioModeInput ? audioModeInput.value : 'original';
                const payload = {
                    filename: trimmerServerFilename,
                    keeper_clips: trimmerKeeperClips,
                    audio_mode: audioMode,
                    voice_name: trimmerTtsVoiceSelect ? trimmerTtsVoiceSelect.value : 'Kore',
                    tone_style: trimmerTtsToneSelect ? trimmerTtsToneSelect.value : 'Narrative Deep',
                    script: trimmerNarrationScript ? trimmerNarrationScript.value : ''
                };

                btnExportFinalVideo.disabled = true;
                if (btnExportIcon) btnExportIcon.textContent = '⏳';
                if (btnExportText) btnExportText.textContent = 'Starting Export...';
                if (trimmerExportCard) trimmerExportCard.style.display = 'block';
                if (trimmerExportResultBox) trimmerExportResultBox.style.display = 'none';
                if (trimmerExportProgressBar) trimmerExportProgressBar.style.width = '5%';
                if (trimmerExportPercentText) trimmerExportPercentText.textContent = '5%';
                if (trimmerExportStepText) trimmerExportStepText.textContent = 'Initiating FFmpeg original-size export...';

                try {
                    const res = await fetch('/api/trimmer/export', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    const data = await res.json();
                    if (data.success && data.task_id) {
                        pollTrimmerExport(data.task_id);
                    } else {
                        throw new Error(data.error || 'Failed to start export');
                    }
                } catch (err) {
                    alert('Export failed: ' + err.message);
                    btnExportFinalVideo.disabled = false;
                    if (btnExportIcon) btnExportIcon.textContent = '🚀';
                    if (btnExportText) btnExportText.textContent = 'Export Final Video (Preserve Original Resolution)';
                }
            });
        }

        function pollTrimmerExport(taskId) {
            if (exportPollInterval) clearInterval(exportPollInterval);
            exportPollInterval = setInterval(async () => {
                try {
                    const res = await fetch(`/api/trimmer/export_status/${taskId}`);
                    const task = await res.json();
                    
                    if (task.progress !== undefined && trimmerExportProgressBar && trimmerExportPercentText) {
                        trimmerExportProgressBar.style.width = `${task.progress}%`;
                        trimmerExportPercentText.textContent = `${task.progress}%`;
                    }
                    if (task.step && trimmerExportStepText) {
                        trimmerExportStepText.textContent = task.step;
                    }

                    if (task.status === 'completed') {
                        clearInterval(exportPollInterval);
                        if (btnExportFinalVideo) btnExportFinalVideo.disabled = false;
                        if (btnExportIcon) btnExportIcon.textContent = '🚀';
                        if (btnExportText) btnExportText.textContent = 'Export Final Video (Preserve Original Resolution)';

                        // Display result
                        const r = task.result;
                        if (trimmerExportResultBox) trimmerExportResultBox.style.display = 'block';
                        if (trimmerExportedPlayer) trimmerExportedPlayer.src = r.video_url;
                        if (btnDownloadExportedVideo) btnDownloadExportedVideo.href = r.download_url;
                        
                        const meta = r.metadata || {};
                        if (trimmerExportMetaDetails) {
                            trimmerExportMetaDetails.innerHTML = `
                                <div><b>File:</b> ${r.filename}</div>
                                <div><b>Resolution:</b> ${meta.width || trimmerVideoWidth}x${meta.height || trimmerVideoHeight} (${meta.aspect_ratio || '16:9'} Native)</div>
                                <div><b>Duration:</b> ${meta.duration_str || formatSecs(meta.duration || 0)}</div>
                                <div><b>Size:</b> ${meta.size_mb || 0} MB</div>
                                <div style="color: #6ee7b7; font-weight: 600; margin-top: 4px;">✅ Zero Aspect-Ratio Distortion &bull; 100% Native Size Maintained</div>
                            `;
                        }

                        // Connect YouTube upload button
                        if (btnSendExportToYouTube) {
                            btnSendExportToYouTube.onclick = () => {
                                tabManualMode.click();
                                document.getElementById('videoTitle').value = `Highlights Montage (${formatSecs(meta.duration || 0)})`;
                                document.getElementById('existingVideoFilename').value = r.filename;
                                document.getElementById('videoFileInfo').textContent = `Using Exported Video: ${r.filename} (${meta.size_mb || 0} MB)`;
                                document.getElementById('videoFileInfo').style.display = 'block';
                                window.scrollTo({ top: 0, behavior: 'smooth' });
                            };
                        }
                    } else if (task.status === 'error') {
                        clearInterval(exportPollInterval);
                        if (btnExportFinalVideo) btnExportFinalVideo.disabled = false;
                        if (btnExportIcon) btnExportIcon.textContent = '🚀';
                        if (btnExportText) btnExportText.textContent = 'Export Final Video (Preserve Original Resolution)';
                        if (trimmerExportStepText) {
                            trimmerExportStepText.textContent = `Error: ${task.error || 'Export failed'}`;
                            trimmerExportStepText.style.color = '#f87171';
                        }
                    }
                } catch (err) {
                    console.warn('Poll error:', err);
                }
            }, 1200);
        }

        // ==============================================
        // INITIALIZATION & TAB BINDING (WITH TRY-CATCH)
        // ==============================================
        function initializeApp() {
            try {
                // Bind all tabs with mobile touch and desktop click listeners
                bindTabButton('tabGeminiMode', 'gemini');
                bindTabButton('tabClipperMode', 'clipper');
                bindTabButton('tabTrimmerMode', 'trimmer');
                bindTabButton('tabManualMode', 'manual');

                // Restore active workspace tab
                const savedTab = localStorage.getItem('active_studio_tab') || 'gemini';
                window.switchWorkspaceTab(savedTab);
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
        metadata = gemini_engine.analyze_video_with_gemini(video_path, format_type=format_type, custom_instructions=instructions)
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

    if not image_file or image_file.filename == '':
        return jsonify({'error': 'No frame image provided'}), 400

    filename = secure_filename(f"extracted_{uuid.uuid4().hex[:8]}_{image_file.filename}")
    res = gemini_engine.save_client_frame(image_file.read(), filename=filename, timestamp=timestamp, label=label)
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
    res = gemini_engine.chat_with_gemini(message, studio_context=context)
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

# ==============================================================
# AI MOVIE-TO-SHORTS AUTO-CLIPPER ENGINE BACKEND ROUTES
# ==============================================================

@app.route('/api/clipper/analyze', methods=['POST'])
def clipper_analyze():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get('url') or '').strip()
    max_shorts = int(data.get('max_shorts') or 5)
    target_duration = int(data.get('target_duration') or 58)
    language = (data.get('language') or 'Hindi').strip()
    voice_name = (data.get('voice_name') or 'Kore').strip()
    tone_style = (data.get('tone_style') or 'Suspense / Thriller').strip()
    wps = float(data.get('wps') or 2.4)
    job_id = (data.get('job_id') or '').strip() or str(uuid.uuid4())

    if not url:
        return jsonify({'success': False, 'error': 'YouTube URL is required'}), 400

    try:
        creds = get_stored_credentials()
        video_info = clipper_engine.extract_youtube_info(url, credentials=creds)
        scenes, status, quota_error = clipper_engine.analyze_movie_narrative_for_shorts(
            youtube_url=url,
            video_info=video_info,
            max_shorts=max_shorts,
            target_duration=target_duration,
            language=language,
            job_id=job_id,
            wps=wps,
            voice_name=voice_name,
            tone_style=tone_style,
            channel_id=get_active_channel_id_or_default()
        )

        ckpt = clipper_engine.load_job_checkpoint(job_id) or {}
        raw_completed = ckpt.get('completed_shorts') or {}
        completed_list = list(raw_completed.values()) if isinstance(raw_completed, dict) else (raw_completed if isinstance(raw_completed, list) else [])

        clipper_jobs[job_id] = {
            'job_id': job_id,
            'status': status,
            'progress': int((len(completed_list) / max(len(scenes), 1)) * 100) if scenes else 0,
            'current_step': 'Analysis complete' if status != 'PAUSED_QUOTA_LIMIT' else 'Paused: Gemini API quota limit reached',
            'completed_shorts': completed_list,
            'total_parts': len(scenes),
            'error': quota_error,
            'url': url,
            'scenes': scenes,
            'video_info': video_info,
            'voice_name': voice_name,
            'tone_style': tone_style,
            'wps': wps,
            'target_duration': target_duration
        }

        return jsonify({
            'success': True,
            'job_id': job_id,
            'status': status,
            'error': quota_error,
            'video_info': video_info,
            'scenes': scenes,
            'completed_shorts': completed_list,
            'voice_name': voice_name,
            'tone_style': tone_style,
            'wps': wps
        })
    except Exception as e:
        print(f"Clipper analysis error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 200


clipper_part_tasks = {}

@app.route('/api/clipper/generate_short', methods=['POST'])
def clipper_generate_short():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get('url') or '').strip()
    scene = data.get('scene') or {}
    language = (data.get('language') or 'Hindi').strip()
    video_title = (data.get('video_title') or '').strip()
    job_id = (data.get('job_id') or '').strip() or str(uuid.uuid4())
    part_num = int(scene.get('part', 1))
    sync_mode = bool(data.get('sync', False))
    auto_vertical = bool(data.get('auto_vertical', False))
    voice_name = (data.get('voice_name') or 'Kore').strip()
    tone_style = (data.get('tone_style') or 'Suspense / Thriller').strip()
    wps = float(data.get('wps') or 2.4) if data.get('wps') else None

    if not url or not scene:
        return jsonify({'success': False, 'error': 'URL and scene data are required'}), 400

    task_key = f"{job_id}_{part_num}"

    # 1. If already completed in disk checkpoint, return short immediately
    ckpt = clipper_engine.load_job_checkpoint(job_id)
    if ckpt and isinstance(ckpt.get('completed_shorts'), dict) and str(part_num) in ckpt['completed_shorts']:
        completed_short = ckpt['completed_shorts'][str(part_num)]
        clipper_part_tasks[task_key] = {
            'status': 'completed',
            'progress': 100,
            'current_step': 'Complete!',
            'short': completed_short,
            'downloaded_cuts': completed_short.get('downloaded_cuts', []),
            'error': None
        }
        return jsonify({'success': True, 'status': 'completed', 'short': completed_short, 'job_id': job_id, 'part': part_num})

    # 2. If already processing in background, return current status
    if task_key in clipper_part_tasks and clipper_part_tasks[task_key].get('status') == 'processing':
        return jsonify({
            'success': True,
            'status': 'processing',
            'progress': clipper_part_tasks[task_key].get('progress', 10),
            'current_step': clipper_part_tasks[task_key].get('current_step', 'Processing...'),
            'downloaded_cuts': clipper_part_tasks[task_key].get('downloaded_cuts', []),
            'job_id': job_id,
            'part': part_num,
            'task_key': task_key
        })

    # 3. Synchronous mode (if explicitly requested by CLI/tests)
    if sync_mode:
        try:
            short_obj = clipper_engine.process_single_short_pipeline(
                youtube_url=url,
                scene=scene,
                language=language,
                video_title=video_title,
                job_id=job_id,
                auto_vertical=auto_vertical,
                voice_name=voice_name,
                tone_style=tone_style,
                wps=wps
            )
            clipper_part_tasks[task_key] = {
                'status': 'completed',
                'progress': 100,
                'current_step': 'Complete!',
                'short': short_obj,
                'downloaded_cuts': short_obj.get('downloaded_cuts', []),
                'error': None
            }
            return jsonify({'success': True, 'status': 'completed', 'short': short_obj, 'job_id': job_id, 'part': part_num})
        except Exception as e:
            err_msg = str(e)
            return jsonify({'success': False, 'error': err_msg}), 500

    # 4. Asynchronous Background Mode (Default — prevents any Gunicorn timeout & browser freeze)
    clipper_part_tasks[task_key] = {
        'status': 'processing',
        'progress': 5,
        'current_step': f'Queued Part {part_num} for targeted 4-step montage generation...',
        'short': None,
        'downloaded_cuts': [],
        'error': None
    }

    def run_async_pipeline():
        def progress_cb(pct, msg):
            if task_key in clipper_part_tasks:
                clipper_part_tasks[task_key]['progress'] = pct
                clipper_part_tasks[task_key]['current_step'] = msg
                if scene.get('downloaded_cuts'):
                    clipper_part_tasks[task_key]['downloaded_cuts'] = scene['downloaded_cuts']

        try:
            short_obj = clipper_engine.process_single_short_pipeline(
                youtube_url=url,
                scene=scene,
                language=language,
                video_title=video_title,
                job_id=job_id,
                progress_callback=progress_cb,
                auto_vertical=auto_vertical,
                voice_name=voice_name,
                tone_style=tone_style,
                wps=wps
            )
            clipper_part_tasks[task_key]['status'] = 'completed'
            clipper_part_tasks[task_key]['progress'] = 100
            clipper_part_tasks[task_key]['current_step'] = 'Complete!'
            clipper_part_tasks[task_key]['short'] = short_obj
            if scene.get('downloaded_cuts'):
                clipper_part_tasks[task_key]['downloaded_cuts'] = scene['downloaded_cuts']
        except Exception as e:
            import traceback
            tb_str = traceback.format_exc()
            err_msg = str(e)
            print(f"Async Part {part_num} error: {err_msg}\n{tb_str}")
            is_quota = any(w in err_msg.lower() for w in ['429', 'resource_exhausted', 'quota', 'rate limit'])
            clipper_part_tasks[task_key]['status'] = 'PAUSED_QUOTA_LIMIT' if is_quota else 'error'
            clipper_part_tasks[task_key]['error'] = err_msg
            clipper_part_tasks[task_key]['is_quota_error'] = is_quota

    import threading
    t = threading.Thread(target=run_async_pipeline, daemon=True)
    t.start()

    return jsonify({
        'success': True,
        'status': 'processing',
        'job_id': job_id,
        'part': part_num,
        'task_key': task_key,
        'message': f'Generation for Part {part_num} started in background'
    }), 200


@app.route('/api/clipper/status/<job_id>/<int:part_num>', methods=['GET'])
def clipper_part_status(job_id, part_num):
    task_key = f"{job_id}_{part_num}"

    # Check active memory task
    if task_key in clipper_part_tasks:
        return jsonify(clipper_part_tasks[task_key])

    # Check disk checkpoint
    ckpt = clipper_engine.load_job_checkpoint(job_id)
    if ckpt and isinstance(ckpt.get('completed_shorts'), dict) and str(part_num) in ckpt['completed_shorts']:
        return jsonify({
            'status': 'completed',
            'progress': 100,
            'current_step': 'Complete!',
            'short': ckpt['completed_shorts'][str(part_num)],
            'error': None
        })

    return jsonify({
        'status': 'not_found',
        'progress': 0,
        'current_step': 'Not started',
        'short': None,
        'error': None
    }), 404


@app.route('/api/clipper/debug_logs', methods=['GET'])
def api_clipper_debug_logs():
    return jsonify({
        'success': True,
        'logs': getattr(clipper_engine, '_RECENT_LOGS', [])[-100:]
    })


@app.route('/api/clipper/diagnostics', methods=['GET'])
def api_clipper_diagnostics():
    import shutil, time, subprocess
    url = request.args.get('url', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
    diag = {
        'ffmpeg_bin': clipper_engine.get_ffmpeg_bin(),
        'ffprobe_bin': shutil.which('ffprobe'),
        'python_version': sys.version,
    }
    
    # Test direct stream URL resolution
    t0 = time.time()
    try:
        stream_url = clipper_engine.get_direct_stream_url(url)
        diag['stream_url_retrieval_sec'] = round(time.time() - t0, 2)
        diag['has_stream_url'] = bool(stream_url)
        diag['stream_url_preview'] = (stream_url[:80] + '...') if stream_url else None
    except Exception as e:
        diag['stream_url_error'] = str(e)

    # Test direct FFmpeg slice
    if stream_url:
        t1 = time.time()
        test_out = os.path.join(clipper_engine.TEMP_DIR, f"diag_test_{uuid.uuid4().hex[:6]}.mp4")
        try:
            cmd = [
                diag['ffmpeg_bin'], '-y',
                '-ss', '00:00:10',
                '-to', '00:00:14',
                '-i', stream_url,
                '-c', 'copy',
                '-avoid_negative_ts', 'make_zero',
                test_out
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25)
            diag['slice_sec'] = round(time.time() - t1, 2)
            diag['slice_code'] = proc.returncode
            diag['slice_size'] = os.path.getsize(test_out) if os.path.exists(test_out) else 0
            if os.path.exists(test_out):
                os.remove(test_out)
        except Exception as e:
            diag['slice_error'] = str(e)

    return jsonify(diag)


@app.route('/api/clipper/test_pipeline', methods=['GET', 'POST'])
def api_clipper_test_pipeline():
    import time
    url = request.args.get('url', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
    report = {'url': url, 'steps': {}}
    t_start = time.time()
    
    # 1. Test voiceover generation
    t0 = time.time()
    vo_path = os.path.join(clipper_engine.TEMP_DIR, f"test_vo_{uuid.uuid4().hex[:6]}.mp3")
    try:
        vo_ok = clipper_engine.generate_voiceover_audio("This is a quick test of neural voiceover narration.", vo_path, language="English")
        report['steps']['voiceover'] = {
            'success': vo_ok,
            'time_sec': round(time.time() - t0, 2),
            'size': os.path.getsize(vo_path) if os.path.exists(vo_path) else 0
        }
    except Exception as e:
        report['steps']['voiceover'] = {'error': str(e), 'time_sec': round(time.time() - t0, 2)}
        
    # 2. Test BGM synthesis
    t0 = time.time()
    try:
        bgm_path = clipper_engine.ensure_background_music_exists()
        report['steps']['bgm'] = {
            'path': bgm_path,
            'time_sec': round(time.time() - t0, 2),
            'size': os.path.getsize(bgm_path) if os.path.exists(bgm_path) else 0
        }
    except Exception as e:
        report['steps']['bgm'] = {'error': str(e), 'time_sec': round(time.time() - t0, 2)}
        
    # 3. Test act segment download (20 seconds)
    t0 = time.time()
    act_path = os.path.join(clipper_engine.TEMP_DIR, f"test_act_{uuid.uuid4().hex[:6]}.mp4")
    try:
        act_ok = clipper_engine.download_clip_section(url, "00:00:10", "00:00:30", act_path)
        report['steps']['act_download'] = {
            'success': act_ok,
            'time_sec': round(time.time() - t0, 2),
            'size': os.path.getsize(act_path) if os.path.exists(act_path) else 0
        }
    except Exception as e:
        report['steps']['act_download'] = {'error': str(e), 'time_sec': round(time.time() - t0, 2)}
        
    # 4. Test reframe to 9:16
    if os.path.exists(act_path) and os.path.getsize(act_path) > 10000:
        t0 = time.time()
        norm_path = os.path.join(clipper_engine.TEMP_DIR, f"test_norm_{uuid.uuid4().hex[:6]}.mp4")
        try:
            rf_ok = clipper_engine.reframe_subclip_to_vertical_916(act_path, norm_path)
            report['steps']['reframe_916'] = {
                'success': rf_ok,
                'time_sec': round(time.time() - t0, 2),
                'size': os.path.getsize(norm_path) if os.path.exists(norm_path) else 0
            }
            if os.path.exists(norm_path):
                os.remove(norm_path)
        except Exception as e:
            report['steps']['reframe_916'] = {'error': str(e), 'time_sec': round(time.time() - t0, 2)}
            
    # Clean up act segment and vo
    if os.path.exists(act_path):
        os.remove(act_path)
    if os.path.exists(vo_path):
        os.remove(vo_path)
        
    report['total_sec'] = round(time.time() - t_start, 2)
    return jsonify(report)


@app.route('/api/clipper/start_batch_job', methods=['POST'])
def clipper_start_batch_job():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get('url') or '').strip()
    scenes = data.get('scenes') or []
    language = (data.get('language') or 'Hindi').strip()
    video_title = (data.get('video_title') or '').strip()
    job_id = (data.get('job_id') or '').strip() or str(uuid.uuid4())

    if not url or not scenes:
        return jsonify({'success': False, 'error': 'URL and scenes are required'}), 400

    checkpoint = clipper_engine.load_job_checkpoint(job_id) or {
        'job_id': job_id,
        'url': url,
        'scenes': scenes,
        'options': {'language': language},
        'completed_shorts': {},
        'status': 'PROCESSING'
    }

    raw_completed = checkpoint.get('completed_shorts') or {}
    if isinstance(raw_completed, list):
        completed_dict = {str(s.get('part', i+1)): s for i, s in enumerate(raw_completed)}
    elif isinstance(raw_completed, dict):
        completed_dict = raw_completed
    else:
        completed_dict = {}

    clipper_jobs[job_id] = {
        'job_id': job_id,
        'status': 'processing',
        'progress': int((len(completed_dict) / max(len(scenes), 1)) * 100),
        'current_step': f'Queued {len(scenes)} chronological shorts...',
        'completed_shorts': list(completed_dict.values()),
        'total_parts': len(scenes),
        'error': None
    }

    def run_batch_clipping():
        total = len(scenes)
        for idx, scene in enumerate(scenes):
            part = scene.get('part', idx + 1)
            if str(part) in completed_dict:
                continue

            clipper_jobs[job_id]['current_step'] = f"Processing Part {part} of {total}: downloading clip section, reframing 9:16 with face centering, and generating voiceover..."
            clipper_jobs[job_id]['progress'] = int((len(completed_dict) / total) * 100)
            try:
                short_obj = clipper_engine.process_single_short_pipeline(
                    youtube_url=url,
                    scene=scene,
                    language=language,
                    video_title=video_title,
                    job_id=job_id
                )
                completed_dict[str(part)] = short_obj
                clipper_jobs[job_id]['completed_shorts'] = list(completed_dict.values())
                clipper_jobs[job_id]['progress'] = int((len(completed_dict) / total) * 100)

                # Persist checkpoint immediately
                checkpoint['completed_shorts'] = completed_dict
                checkpoint['status'] = 'PROCESSING'
                clipper_engine.save_job_checkpoint(job_id, checkpoint)
            except Exception as e:
                err_msg = str(e)
                print(f"Error processing Part {part} in batch: {err_msg}")
                if any(w in err_msg.lower() for w in ['429', 'resource_exhausted', 'quota', 'rate limit']):
                    clipper_jobs[job_id]['status'] = 'PAUSED_QUOTA_LIMIT'
                    clipper_jobs[job_id]['error'] = f"Paused at Part {part}: Gemini API Quota limit reached (429)."
                    checkpoint['status'] = 'PAUSED_QUOTA_LIMIT'
                    checkpoint['error'] = clipper_jobs[job_id]['error']
                    clipper_engine.save_job_checkpoint(job_id, checkpoint)
                    return
                else:
                    clipper_jobs[job_id]['error'] = f"Notice on Part {part}: {err_msg}"
                    checkpoint['error'] = err_msg
                    clipper_engine.save_job_checkpoint(job_id, checkpoint)

        clipper_jobs[job_id]['status'] = 'completed'
        clipper_jobs[job_id]['progress'] = 100
        clipper_jobs[job_id]['current_step'] = f'Successfully generated all {total} chronological Shorts!'
        checkpoint['status'] = 'COMPLETED'
        checkpoint['error'] = None
        clipper_engine.save_job_checkpoint(job_id, checkpoint)

    th = threading.Thread(target=run_batch_clipping)
    th.daemon = True
    th.start()

    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/clipper/resume', methods=['POST'])
def clipper_resume():
    data = request.get_json(force=True, silent=True) or {}
    job_id = (data.get('job_id') or '').strip()
    new_api_key = (data.get('gemini_api_key') or '').strip()

    if not job_id:
        return jsonify({'success': False, 'error': 'job_id is required to resume a job'}), 400

    # If new API key is provided, persist it immediately
    if new_api_key:
        try:
            cfg = gemini_engine.get_gemini_config()
            cfg['api_key'] = new_api_key
            gemini_engine.save_gemini_config(cfg)
            os.environ['GEMINI_API_KEY'] = new_api_key
        except Exception as ke:
            print(f"Warning: Failed to update gemini config in resume: {ke}")

    checkpoint = clipper_engine.load_job_checkpoint(job_id)
    if not checkpoint:
        return jsonify({'success': False, 'error': f'Job checkpoint {job_id} not found on disk'}), 404

    url = checkpoint.get('url') or ''
    scenes = checkpoint.get('scenes') or []
    options = checkpoint.get('options') or {}
    language = options.get('language') or 'Hindi'
    video_info = checkpoint.get('video_info') or {}
    video_title = video_info.get('title') or ''

    raw_completed = checkpoint.get('completed_shorts') or {}
    if isinstance(raw_completed, list):
        completed_dict = {str(s.get('part', i+1)): s for i, s in enumerate(raw_completed)}
    elif isinstance(raw_completed, dict):
        completed_dict = raw_completed
    else:
        completed_dict = {}

    # If narrative analysis was paused and scripts were algorithmic or missing, attempt Gemini re-analysis
    needs_script_analysis = checkpoint.get('status') == 'PAUSED_QUOTA_LIMIT' and (not scenes or all(not s.get('script') for s in scenes))
    if needs_script_analysis:
        try:
            scenes, status, quota_error = clipper_engine.analyze_movie_narrative_for_shorts(
                youtube_url=url,
                video_info=video_info,
                max_shorts=options.get('max_shorts', 5),
                target_duration=options.get('target_duration', 50),
                language=language,
                job_id=job_id
            )
            checkpoint['scenes'] = scenes
            checkpoint['status'] = status
            checkpoint['error'] = quota_error
            clipper_engine.save_job_checkpoint(job_id, checkpoint)
            if status == 'PAUSED_QUOTA_LIMIT':
                return jsonify({
                    'success': False,
                    'status': 'PAUSED_QUOTA_LIMIT',
                    'error': quota_error or 'Gemini API quota still exceeded. Please update API key in Settings.',
                    'job_id': job_id,
                    'scenes': scenes
                }), 429
        except Exception as e:
            return jsonify({'success': False, 'error': f'Resume analysis failed: {e}'}), 500

    # Start or resume background batch generation
    clipper_jobs[job_id] = {
        'job_id': job_id,
        'status': 'processing',
        'progress': int((len(completed_dict) / max(len(scenes), 1)) * 100),
        'current_step': f"Resuming job: {len(completed_dict)}/{len(scenes)} parts already done. Resuming...",
        'completed_shorts': list(completed_dict.values()),
        'total_parts': len(scenes),
        'error': None
    }
    checkpoint['status'] = 'PROCESSING'
    checkpoint['error'] = None
    clipper_engine.save_job_checkpoint(job_id, checkpoint)

    def run_resumed_clipping():
        total = len(scenes)
        for idx, scene in enumerate(scenes):
            part = scene.get('part', idx + 1)
            if str(part) in completed_dict:
                continue

            clipper_jobs[job_id]['current_step'] = f"Processing Part {part} of {total}: downloading clip section, reframing 9:16 with face centering, and generating voiceover..."
            clipper_jobs[job_id]['progress'] = int((len(completed_dict) / total) * 100)

            try:
                short_obj = clipper_engine.process_single_short_pipeline(
                    youtube_url=url,
                    scene=scene,
                    language=language,
                    video_title=video_title,
                    job_id=job_id
                )
                completed_dict[str(part)] = short_obj
                clipper_jobs[job_id]['completed_shorts'] = list(completed_dict.values())
                clipper_jobs[job_id]['progress'] = int((len(completed_dict) / total) * 100)

                # Persist checkpoint immediately
                checkpoint['completed_shorts'] = completed_dict
                checkpoint['status'] = 'PROCESSING'
                clipper_engine.save_job_checkpoint(job_id, checkpoint)
            except Exception as e:
                err_msg = str(e)
                print(f"Error processing Part {part} in resumed batch: {err_msg}")
                if any(w in err_msg.lower() for w in ['429', 'resource_exhausted', 'quota', 'rate limit']):
                    clipper_jobs[job_id]['status'] = 'PAUSED_QUOTA_LIMIT'
                    clipper_jobs[job_id]['error'] = f"Paused at Part {part}: Gemini API Quota limit reached (429)."
                    checkpoint['status'] = 'PAUSED_QUOTA_LIMIT'
                    checkpoint['error'] = clipper_jobs[job_id]['error']
                    clipper_engine.save_job_checkpoint(job_id, checkpoint)
                    return
                else:
                    clipper_jobs[job_id]['error'] = f"Notice on Part {part}: {err_msg}"
                    checkpoint['error'] = err_msg
                    clipper_engine.save_job_checkpoint(job_id, checkpoint)

        clipper_jobs[job_id]['status'] = 'completed'
        clipper_jobs[job_id]['progress'] = 100
        clipper_jobs[job_id]['current_step'] = f'Successfully generated all {total} chronological Shorts!'
        checkpoint['status'] = 'COMPLETED'
        checkpoint['error'] = None
        clipper_engine.save_job_checkpoint(job_id, checkpoint)

    th = threading.Thread(target=run_resumed_clipping)
    th.daemon = True
    th.start()

    return jsonify({
        'success': True,
        'job_id': job_id,
        'scenes': scenes,
        'video_info': video_info,
        'completed_shorts': list(completed_dict.values())
    })


@app.route('/api/clipper/jobs', methods=['GET'])
def clipper_list_saved_jobs():
    try:
        jobs = clipper_engine.list_saved_jobs()
        return jsonify({'success': True, 'jobs': jobs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/clipper/job/<job_id>', methods=['GET'])
def clipper_get_saved_job(job_id):
    checkpoint = clipper_engine.load_job_checkpoint(job_id)
    if not checkpoint:
        mem_job = clipper_jobs.get(job_id)
        if mem_job:
            return jsonify({'success': True, 'job': mem_job})
        return jsonify({'success': False, 'error': f'Job {job_id} not found'}), 404
    return jsonify({'success': True, 'job': checkpoint})


@app.route('/api/clipper/job_status/<job_id>')
def clipper_job_status(job_id):
    job = clipper_jobs.get(job_id)
    if not job:
        checkpoint = clipper_engine.load_job_checkpoint(job_id)
        if checkpoint:
            raw_completed = checkpoint.get('completed_shorts') or {}
            if isinstance(raw_completed, dict):
                completed_list = list(raw_completed.values())
            elif isinstance(raw_completed, list):
                completed_list = raw_completed
            else:
                completed_list = []
            scenes = checkpoint.get('scenes') or []
            total_parts = len(scenes)
            status = checkpoint.get('status', 'unknown')
            return jsonify({
                'job_id': job_id,
                'status': status,
                'progress': int((len(completed_list) / max(total_parts, 1)) * 100) if total_parts else 0,
                'current_step': f"Job {status}: {len(completed_list)}/{total_parts} parts generated",
                'completed_shorts': completed_list,
                'total_parts': total_parts,
                'error': checkpoint.get('error'),
                'scenes': scenes,
                'video_info': checkpoint.get('video_info')
            })
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)



@app.route('/api/clipper/upload_short', methods=['POST'])
def clipper_upload_short():
    creds = get_stored_credentials()
    if not creds:
        return jsonify({'error': 'Unauthorized. Please connect your YouTube channel first.'}), 401

    data = request.get_json(force=True, silent=True) or {}
    filename = data.get('filename', '').strip()
    title = data.get('title', 'YouTube Short #Shorts').strip()
    script = data.get('script', '').strip()
    raw_tags = data.get('tags', '')
    privacy = data.get('privacy', 'public').strip()
    made_for_kids = bool(data.get('made_for_kids', False))
    category_id = data.get('category_id', '24')

    if not filename:
        return jsonify({'error': 'Filename is required'}), 400

    source_path = os.path.join(clipper_engine.CLIPPER_DIR, secure_filename(filename))
    if not os.path.exists(source_path):
        return jsonify({'error': 'Short video file not found'}), 404

    task_id = str(uuid.uuid4())
    target_upload_path = os.path.join(UPLOAD_FOLDER, f"upload_{task_id}_{secure_filename(filename)}")
    shutil.copyfile(source_path, target_upload_path)

    thumb_name = filename.replace("short_", "thumb_").replace(".mp4", ".jpg")
    source_thumb = os.path.join(clipper_engine.CLIPPER_DIR, thumb_name)
    target_thumb_path = None
    if os.path.exists(source_thumb):
        target_thumb_path = os.path.join(UPLOAD_FOLDER, f"thumb_{task_id}_{thumb_name}")
        shutil.copyfile(source_thumb, target_thumb_path)

    tags = [t.strip() for t in raw_tags.split(',') if t.strip()] if isinstance(raw_tags, str) else (raw_tags or [])
    if 'Shorts' not in tags:
        tags.append('Shorts')

    description = (
        f"{title}\n\n"
        f"🎬 Story Recap:\n{script}\n\n"
        f"🔔 Subscribe to the channel for more viral breakdowns!\n\n"
        f"#Shorts #YouTubeShorts #Viral"
    )

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
        args=(task_id, creds_dict, target_upload_path, target_thumb_path, title, description, tags, privacy, category_id, made_for_kids)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'task_id': task_id})


@app.route('/api/clipper/sync_rendered_short', methods=['POST'])
def clipper_sync_rendered_short():
    """
    Hybrid Architecture Endpoint:
    Receives locally processed and rendered MP4 video & thumbnail from Local Worker,
    saves into uploads/clipper_shorts/, and updates the job checkpoint so the web UI
    can instantly stream and display the completed Short.
    """
    try:
        job_id = request.form.get('job_id')
        part_num = request.form.get('part', '1')
        short_json_str = request.form.get('short_json')
        short_data = json.loads(short_json_str) if short_json_str else {}

        video_file = request.files.get('video')
        thumb_file = request.files.get('thumbnail')

        if not video_file:
            return jsonify({'success': False, 'error': 'No video file provided'}), 400

        os.makedirs(clipper_engine.CLIPPER_DIR, exist_ok=True)
        filename = secure_filename(video_file.filename)
        save_video_path = os.path.join(clipper_engine.CLIPPER_DIR, filename)
        video_file.save(save_video_path)

        if thumb_file:
            tname = secure_filename(thumb_file.filename)
            save_thumb_path = os.path.join(clipper_engine.CLIPPER_DIR, tname)
            thumb_file.save(save_thumb_path)

        # Update in-memory and disk checkpoints
        if job_id:
            ckpt = clipper_engine.load_job_checkpoint(job_id) or {}
            completed = ckpt.get('completed_shorts') or {}
            if isinstance(completed, list):
                completed = {str(s.get('part', i+1)): s for i, s in enumerate(completed)}
            elif not isinstance(completed, dict):
                completed = {}

            if not short_data:
                short_data = {
                    'part': int(part_num),
                    'filename': filename,
                    'video_url': f'/api/clipper/media/{filename}',
                    'thumbnail_url': f'/api/clipper/media/{filename.replace("short_", "thumb_").replace(".mp4", ".jpg")}',
                    'status': 'ready'
                }
            completed[str(part_num)] = short_data
            ckpt['completed_shorts'] = completed
            clipper_engine.save_job_checkpoint(job_id, ckpt)

            task_key = f"{job_id}_{part_num}"
            generation_tasks[task_key] = {
                'status': 'completed',
                'progress': 100,
                'current_step': f'Part {part_num} ready (rendered via Local Worker)!',
                'short': short_data,
                'error': None
            }

        return jsonify({'success': True, 'filename': filename, 'video_url': f'/api/clipper/media/{filename}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/clipper/media/<path:filename>')
def clipper_serve_media(filename):
    resp = send_from_directory(clipper_engine.CLIPPER_DIR, secure_filename(filename), conditional=True)
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp


@app.route('/api/clipper/cut/<path:filename>')
def clipper_serve_cut(filename):
    resp = send_from_directory(clipper_engine.CUTS_DIR, secure_filename(filename), conditional=True)
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp


@app.route('/api/clipper/convert_vertical', methods=['POST'])
def clipper_convert_vertical():
    data = request.get_json(force=True, silent=True) or {}
    job_id = (data.get('job_id') or '').strip()
    part_num = int(data.get('part', 1))

    if not job_id:
        return jsonify({'success': False, 'error': 'job_id is required'}), 400

    ckpt = clipper_engine.load_job_checkpoint(job_id)
    if not ckpt:
        return jsonify({'success': False, 'error': f'Job {job_id} not found'}), 404

    completed = ckpt.get('completed_shorts') or {}
    part_data = completed.get(str(part_num))
    if not part_data:
        return jsonify({'success': False, 'error': f'Part {part_num} has not been generated yet'}), 400

    standard_path = part_data.get('standard_filepath') or part_data.get('filepath')
    if not standard_path or not os.path.exists(standard_path):
        return jsonify({'success': False, 'error': f'Source video file not found on server'}), 404

    task_key = f"{job_id}_{part_num}"
    try:
        vertical_short = clipper_engine.convert_recap_to_vertical_step4(
            standard_video_path=standard_path,
            job_id=job_id,
            part_num=part_num
        )
        if task_key in clipper_part_tasks:
            clipper_part_tasks[task_key]['short'] = vertical_short
            clipper_part_tasks[task_key]['current_step'] = '9:16 Vertical Short ready!'
        return jsonify({'success': True, 'short': vertical_short, 'part': part_num, 'job_id': job_id})
    except Exception as e:
        import traceback
        traceback.print_exc()

# ==============================================================
# GEMINI 3.8 FLASH TTS STUDIO & CALIBRATION ENDPOINTS
# ==============================================================

@app.route('/api/clipper/tts/voices', methods=['GET'])
def clipper_get_voices():
    return jsonify({
        'success': True,
        'voices': clipper_engine.GEMINI_TTS_VOICES,
        'tones': list(clipper_engine.TONE_PROMPT_PRESETS.keys()),
        'default_voice': 'Kore',
        'default_tone': 'Suspense / Thriller'
    })


@app.route('/api/clipper/tts/preview', methods=['POST'])
def clipper_tts_preview():
    data = request.get_json(force=True, silent=True) or {}
    voice = (data.get('voice') or 'Kore').strip()
    tone = (data.get('tone') or 'Suspense / Thriller').strip()
    language = (data.get('language') or 'Hindi').strip()
    custom_text = (data.get('text') or '').strip()

    sample_text = custom_text or (
        "नमस्ते! मैं जेमिनी 3.8 फ्लैश टीटीएस हूँ। यह आवाज आपकी सिनेमाई यूट्यूब शॉर्ट्स के लिए बिल्कुल तैयार है!"
        if language.lower().startswith('hi') else
        "Hello! I am Gemini 3.8 Flash TTS. This voice is calibrated and ready for your cinematic YouTube Shorts!"
    )
    unique_id = uuid.uuid4().hex[:6]
    filename = f"preview_{voice}_{unique_id}.mp3"
    output_path = os.path.join(clipper_engine.TEMP_DIR, filename)

    ok = clipper_engine.generate_gemini_tts_audio(
        text=sample_text,
        output_path=output_path,
        voice_name=voice,
        tone_style=tone,
        language=language
    )
    if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        return jsonify({
            'success': True,
            'audio_url': f'/api/clipper/tts_sample/{filename}',
            'voice': voice,
            'tone': tone
        })
    return jsonify({'success': False, 'error': 'Failed to generate voice preview audio'}), 500


@app.route('/api/clipper/tts/calibrate', methods=['POST'])
def clipper_tts_calibrate():
    data = request.get_json(force=True, silent=True) or {}
    voice = (data.get('voice') or 'Kore').strip()
    tone = (data.get('tone') or 'Suspense / Thriller').strip()
    language = (data.get('language') or 'Hindi').strip()
    job_id = (data.get('job_id') or '').strip()

    result = clipper_engine.calibrate_voice_speed(voice_name=voice, tone_style=tone, language=language)
    if job_id:
        try:
            ckpt = clipper_engine.load_job_checkpoint(job_id)
            if ckpt:
                ckpt['wps'] = result['wps']
                ckpt['voice_name'] = voice
                ckpt['tone_style'] = tone
                clipper_engine.save_job_checkpoint(job_id, ckpt)
        except Exception as ce:
            print(f"Failed to persist calibration to job {job_id}: {ce}")

    return jsonify({
        'success': True,
        'result': result
    })


@app.route('/api/clipper/tts_sample/<path:filename>')
def clipper_serve_tts_sample(filename):
    resp = send_from_directory(clipper_engine.TEMP_DIR, secure_filename(filename), conditional=True)
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp


# ==============================================================
# TIMELINE VIDEO TRIMMER & SLICER ENGINE (ORIGINAL SIZE PRESERVED)
# ==============================================================
trimmer_export_tasks: Dict[str, Dict[str, Any]] = {}

@app.route('/api/trimmer/upload', methods=['POST'])
def trimmer_upload():
    """
    Uploads video file (any length from 10m to 2h+) for timeline trimming,
    probes and returns exact original resolution, aspect ratio, duration, and metadata.
    """
    file = request.files.get('video_file')
    if not file or file.filename == '':
        return jsonify({'error': 'No video file provided'}), 400

    video_id = str(uuid.uuid4())[:12]
    safe_name = secure_filename(f"trimmer_{video_id}_{file.filename}")
    saved_path = os.path.join(clipper_engine.TRIMMER_VIDEOS_DIR, safe_name)
    file.save(saved_path)

    meta = clipper_engine.get_video_metadata(saved_path)
    return jsonify({
        'success': True,
        'video_id': video_id,
        'filename': safe_name,
        'stream_url': f"/api/trimmer/stream/{safe_name}",
        'metadata': meta
    })


@app.route('/api/trimmer/stream/<path:filename>')
def trimmer_stream(filename):
    """
    Streams trimmer video with full HTTP Range request support for smooth scrubbing.
    """
    safe_name = secure_filename(filename)
    for cand_dir in [clipper_engine.TRIMMER_VIDEOS_DIR, clipper_engine.CLIPPER_DIR, UPLOAD_FOLDER]:
        if os.path.exists(os.path.join(cand_dir, safe_name)):
            resp = send_from_directory(cand_dir, safe_name, conditional=True)
            resp.headers['Accept-Ranges'] = 'bytes'
            return resp
    return jsonify({'error': 'Video file not found'}), 404


@app.route('/api/trimmer/gemini_autocut', methods=['POST'])
def trimmer_gemini_autocut():
    """
    Discovers key narrative turning points and timestamps with Gemini.
    Automatically marks keeper cuts, discards filler, and generates synchronized story script.
    """
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get('filename')
    duration = float(data.get('duration', 0.0))
    title = data.get('title', '')
    focus_style = data.get('focus_style', 'Key Dramatic Highlights')
    target_duration = data.get('target_duration')
    language = data.get('language', 'Hindi')
    custom_prompt = data.get('custom_prompt', '')

    if target_duration:
        try:
            target_duration = int(target_duration)
        except Exception:
            target_duration = None

    video_path = ""
    if filename:
        safe_name = secure_filename(filename)
        for cand_dir in [clipper_engine.TRIMMER_VIDEOS_DIR, clipper_engine.CLIPPER_DIR, UPLOAD_FOLDER]:
            p = os.path.join(cand_dir, safe_name)
            if os.path.exists(p):
                video_path = p
                break

    result = clipper_engine.analyze_video_timeline_autocut(
        video_path=video_path,
        duration=duration,
        title=title,
        focus_style=focus_style,
        target_duration=target_duration,
        language=language,
        custom_prompt=custom_prompt
    )
    return jsonify(result)


@app.route('/api/trimmer/plan_explainer', methods=['POST'])
def trimmer_plan_explainer():
    """
    Extracts 100% accurate YouTube metadata and calls Gemini with calibrated 100-word WPS
    to generate an organic 8-25 minute cinema explainer storyboard with micro/medium cuts.
    """
    data = request.get_json(force=True, silent=True) or {}
    youtube_url = (data.get('youtube_url') or '').strip()
    target_duration_raw = data.get('target_duration', 'dynamic')
    if target_duration_raw == 'dynamic' or not target_duration_raw:
        target_duration = 'dynamic'
    else:
        try:
            target_duration = int(target_duration_raw)
        except (ValueError, TypeError):
            target_duration = 'dynamic'
    language = (data.get('language') or 'Hindi').strip()
    voice_name = (data.get('voice_name') or 'Kore').strip()
    tone_style = (data.get('tone_style') or 'Narrative Deep Storytelling').strip()
    custom_instructions = (data.get('custom_instructions') or '').strip()

    if not youtube_url:
        return jsonify({'success': False, 'error': 'YouTube URL is required'}), 200

    try:
        creds = get_stored_credentials()
        ch_id = (data.get('channel_id') or '').strip() or get_active_channel_id_or_default()
        storyboard = clipper_engine.generate_cinema_explainer_storyboard(
            youtube_url=youtube_url,
            credentials=creds,
            target_duration=target_duration,
            language=language,
            voice_name=voice_name,
            tone_style=tone_style,
            custom_instructions=custom_instructions,
            channel_id=ch_id
        )
        return jsonify(storyboard)
    except Exception as e:
        print(f"Trimmer plan explainer error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 200


def _execute_trimmer_export_worker(task_id: str, payload: Dict[str, Any]):
    filename = payload.get('filename')
    keeper_clips = payload.get('keeper_clips', [])
    audio_mode = payload.get('audio_mode', 'original')
    voice_name = payload.get('voice_name', 'Kore')
    tone_style = payload.get('tone_style', 'Narrative Deep')
    script = payload.get('script', '')
    ch_id = payload.get('channel_id') or get_active_channel_id_or_default()

    def progress_cb(pct: int, msg: str):
        if task_id in trimmer_export_tasks:
            trimmer_export_tasks[task_id]['progress'] = pct
            trimmer_export_tasks[task_id]['step'] = msg

    try:
        video_path = ""
        if filename:
            safe_name = secure_filename(filename)
            for cand_dir in [clipper_engine.TRIMMER_VIDEOS_DIR, clipper_engine.CLIPPER_DIR, UPLOAD_FOLDER]:
                p = os.path.join(cand_dir, safe_name)
                if os.path.exists(p):
                    video_path = p
                    break

        if not video_path or not os.path.exists(video_path):
            raise FileNotFoundError(f"Source video file not found: {filename}")

        out_filename = f"trimmed_montage_{task_id}.mp4"
        out_path = os.path.join(clipper_engine.TRIMMER_EXPORTS_DIR, out_filename)

        meta = clipper_engine.export_timeline_trimmed_video(
            source_video_path=video_path,
            keeper_clips=keeper_clips,
            output_path=out_path,
            audio_mode=audio_mode,
            voice_name=voice_name,
            tone_style=tone_style,
            script=script,
            progress_callback=progress_cb,
            channel_id=ch_id
        )

        trimmer_export_tasks[task_id]['status'] = 'completed'
        trimmer_export_tasks[task_id]['progress'] = 100
        trimmer_export_tasks[task_id]['step'] = 'Trimmed video exported successfully at original resolution!'
        trimmer_export_tasks[task_id]['result'] = {
            'filename': out_filename,
            'video_url': f"/api/trimmer/media/{out_filename}",
            'download_url': f"/api/trimmer/media/{out_filename}?download=1",
            'metadata': meta
        }
    except Exception as e:
        print(f"Trimmer export error: {e}")
        if task_id in trimmer_export_tasks:
            trimmer_export_tasks[task_id]['status'] = 'error'
            trimmer_export_tasks[task_id]['error'] = str(e)


@app.route('/api/trimmer/export', methods=['POST'])
def trimmer_export():
    """
    Exports keeper clips stitched seamlessly without aspect-ratio resizing.
    Preserves 100% original video resolution.
    """
    data = request.get_json(force=True, silent=True) or {}
    task_id = str(uuid.uuid4())[:8]

    trimmer_export_tasks[task_id] = {
        'status': 'processing',
        'progress': 0,
        'step': 'Starting trimmer export...',
        'error': None,
        'result': None
    }

    thread = threading.Thread(target=_execute_trimmer_export_worker, args=(task_id, data))
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/trimmer/export_status/<task_id>', methods=['GET'])
def trimmer_export_status(task_id):
    task = trimmer_export_tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Export task not found'}), 404
    return jsonify(task)


@app.route('/api/trimmer/media/<path:filename>')
def trimmer_serve_media(filename):
    safe_name = secure_filename(filename)
    resp = send_from_directory(clipper_engine.TRIMMER_EXPORTS_DIR, safe_name, conditional=True)
    resp.headers['Accept-Ranges'] = 'bytes'
    if request.args.get('download'):
        resp.headers['Content-Disposition'] = f'attachment; filename="{safe_name}"'
    return resp


if __name__ == '__main__':
    print("="*60)
    print("YouTube Creator Studio Pro + Gemini AI Copilot running at http://localhost:5000")
    print("Target Account: shoaibgh473@gmail.com")
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
