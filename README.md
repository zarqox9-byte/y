# YouTube Creator Studio Pro (Desktop-Web Studio)

A YouTube Studio desktop-mode web application built with Python Flask and YouTube Data API v3 for managing and uploading videos to your YouTube channel (`shoaibgh473@gmail.com`).

---

## 🚀 Status

- **Web Server:** Running at [http://localhost:5000](http://localhost:5000)
- **Environment:** Python 3.12 with `google-api-python-client`, `google-auth-oauthlib`, `flask`
- **Working Directory:** `d:\y`

---

## 📋 Features Included

1. **One-Click Google Login / OAuth 2.0**:
   - Targets `shoaibgh473@gmail.com` with `login_hint`.
   - Scopes: `youtube.upload`, `youtube`, `youtube.force-ssl`, `youtube.readonly`.
   - Auto-refreshes credentials and caches them in `token.json`.

2. **Channel Overview Sidebar**:
   - Channel branding (Avatar, Title, Handle).
   - Live Statistics (Subscriber count, Total views, Total videos).
   - Quota tracking meter (~1,600 / 10,000 daily points).

3. **Complete Upload Studio**:
   - Multi-format file selector (MP4, MKV, MOV, WebM).
   - Real-time custom thumbnail selector with instant image preview.
   - Title input with live character counter (100 char limit).
   - Description box with live character counter (5,000 char limit).
   - Interactive Tag Chips (type and press Enter/comma).
   - Privacy status dropdown (`Public`, `Unlisted`, `Private`).
   - Category selector (Gaming, Tech, Education, Entertainment, etc.).
   - Audience toggle (COPPA "Made for Kids" declaration).
   - Two-phase real-time resumable upload progress bar (Server buffer -> YouTube chunked ingestion).
   - Instant direct links to watch on YouTube or edit in official YouTube Studio.

4. **Recent Uploads Shelf**:
   - Displays recently published videos with thumbnail, privacy badge, and watch links.

---

## 🔑 Providing Google Cloud Credentials

If `client_secret.json` is not present, visiting [http://localhost:5000](http://localhost:5000) will automatically display a **Credentials Setup screen** where you can:
- Drop or select your `client_secret.json` file, or
- Paste the JSON content directly.
- Or place `client_secret.json` directly into `d:\y\client_secret.json`.

---

## 🛠️ Manual Start Command

If you restart your computer, run:
```powershell
python d:\y\app.py
```
Then visit [http://localhost:5000](http://localhost:5000).
