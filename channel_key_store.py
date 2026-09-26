"""
channel_key_store.py - Persistent 10-Key Pool Per Channel with Auto-Rotation
=============================================================================
1. Permanent Channel-Key Storage:
   - Schema: channel_id (PK), keys_json (TEXT list of up to 10 keys), updated_at (TEXT).
   - Supports PostgreSQL (via DATABASE_URL / POSTGRES_URL) and persistent SQLite.
   - Triple redundancy: Primary Database Table + Persistent Backup JSON + Account metadata.
   - Keys survive deploys, container restarts, and code redeployments.
2. Only Manual Delete / Overwrite:
   - Stored keys remain active until explicitly edited or removed by user in UI.
3. Auto-Rotation & Failover:
   - Sequential Round-Robin across pool.
   - Automatic immediate failover on HTTP 429 / RESOURCE_EXHAUSTED.
"""

import os
import re
import json
import time
import sqlite3
import logging
from typing import List, Dict, Any, Optional, Tuple, Callable

logger = logging.getLogger("ChannelKeyStore")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [ChannelKeyStore] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

BACKUP_JSON_FILE = os.path.join(UPLOADS_DIR, "channel_keys_backup.json")
CONFIG_FILE = os.path.join(BASE_DIR, "gemini_config.json")
ACCOUNTS_STORE_FILE = os.path.join(BASE_DIR, "user_accounts.json")

# In-memory round-robin pointer per channel
_ROTATION_INDICES: Dict[str, int] = {}

# Active database connection mode
_DB_MODE = "sqlite"  # 'postgres' or 'sqlite'
_POSTGRES_CONN_STR = None


def _get_sqlite_db_path() -> str:
    """Finds persistent directory for SQLite database."""
    candidates = [
        "/var/data/channel_keys.db",
        "/data/channel_keys.db",
        os.path.join(os.environ.get("PERSISTENT_DATA_DIR", ""), "channel_keys.db") if os.environ.get("PERSISTENT_DATA_DIR") else "",
        os.path.join(UPLOADS_DIR, "channel_keys.db"),
        os.path.join(BASE_DIR, "channel_keys.db")
    ]
    for c in candidates:
        if c:
            parent = os.path.dirname(c)
            if parent and (os.path.exists(parent) or parent == ""):
                return c
    return os.path.join(BASE_DIR, "channel_keys.db")


def _get_db_connection():
    """Returns a connection to either PostgreSQL or SQLite."""
    global _DB_MODE, _POSTGRES_CONN_STR
    pg_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if pg_url:
        if pg_url.startswith("postgres://"):
            pg_url = pg_url.replace("postgres://", "postgresql://", 1)
        try:
            import psycopg2
            conn = psycopg2.connect(pg_url)
            _DB_MODE = "postgres"
            _POSTGRES_CONN_STR = pg_url
            return conn
        except Exception as e:
            logger.warning(f"PostgreSQL connection via DATABASE_URL failed: {e}. Falling back to SQLite.")

    # SQLite fallback
    sqlite_path = _get_sqlite_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(sqlite_path)), exist_ok=True)
    conn = sqlite3.connect(sqlite_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _DB_MODE = "sqlite"
    return conn


def init_channel_key_db():
    """
    Initializes channel_gemini_keys table and rehydrates from persistent backups
    if the database is new or empty.
    """
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        if _DB_MODE == "postgres":
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_gemini_keys (
                    channel_id VARCHAR(128) PRIMARY KEY,
                    keys_json TEXT NOT NULL,
                    updated_at VARCHAR(64) NOT NULL
                );
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_gemini_keys (
                    channel_id TEXT PRIMARY KEY,
                    keys_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
        conn.commit()
        conn.close()
        logger.info(f"Initialized channel_gemini_keys table (Mode: {_DB_MODE})")

        # Auto-rehydrate from backups if DB table has 0 records
        rehydrate_from_backups_if_empty()
    except Exception as e:
        logger.error(f"Error initializing channel_gemini_keys DB: {e}")


def mask_key(key: str) -> str:
    """Masks API key showing first 4 and last 4 characters."""
    if not key or not isinstance(key, str):
        return ""
    key = key.strip()
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


def load_backup_json() -> Dict[str, Any]:
    """Loads backup JSON dictionary mapping channel_id -> list of keys."""
    if os.path.exists(BACKUP_JSON_FILE):
        try:
            with open(BACKUP_JSON_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error reading {BACKUP_JSON_FILE}: {e}")
    return {}


def save_backup_json(data: Dict[str, Any]):
    """Mirrors channel keys to persistent backup JSON file."""
    try:
        with open(BACKUP_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Error writing {BACKUP_JSON_FILE}: {e}")


def rehydrate_from_backups_if_empty():
    """
    If the database table has no keys, restores from backup JSON,
    gemini_config.json, or environment variable so redeployments never wipe keys.
    """
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM channel_gemini_keys")
        row = cur.fetchone()
        count = row[0] if row else 0
        conn.close()

        if count > 0:
            return  # Database already populated

        logger.info("channel_gemini_keys table is empty. Rehydrating from persistent backups...")
        backup_data = load_backup_json()
        rehydrated_any = False

        if backup_data and isinstance(backup_data, dict):
            for cid, k_list in backup_data.items():
                if isinstance(k_list, list) and k_list:
                    save_channel_keys_to_db(cid, k_list, mirror_backup=False)
                    rehydrated_any = True

        # Check gemini_config.json
        if not rehydrated_any and os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    k = cfg.get("api_key", "").strip()
                    if k:
                        save_channel_keys_to_db("default", [k], mirror_backup=True)
                        rehydrated_any = True
            except Exception:
                pass

        # Check GEMINI_API_KEY environment variable
        if not rehydrated_any:
            env_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if env_key:
                save_channel_keys_to_db("default", [env_key], mirror_backup=True)
    except Exception as e:
        logger.warning(f"Rehydrate check notice: {e}")


def _is_valid_real_gemini_key(k: Any) -> bool:
    if not isinstance(k, str):
        return False
    ks = k.strip()
    if len(ks) < 25:
        return False
    lower_k = ks.lower()
    if any(tok in lower_k for tok in ["dummy", "testkey", "fakekey", "testing", "your_api_key", "placeholder"]):
        return False
    return True


def get_first_authenticated_channel_id() -> str:
    """
    Returns the first channel_id that has at least one valid Gemini API key
    from user_accounts.json, channel_gemini_keys DB table, or backup JSON.
    Falls back to 'default'.
    """
    # 1. Check user_accounts.json first for real connected YouTube channels (UC...)
    if os.path.exists(ACCOUNTS_STORE_FILE):
        try:
            with open(ACCOUNTS_STORE_FILE, "r", encoding="utf-8") as f:
                acc_store = json.load(f)
            if isinstance(acc_store, dict):
                for acc in acc_store.values():
                    if isinstance(acc, dict):
                        cid = str(acc.get("active_channel_id") or "").strip()
                        if cid and cid.lower() != "default" and not cid.lower().startswith("test_chan"):
                            return cid
                        ch_list = acc.get("channels") or []
                        if isinstance(ch_list, list) and ch_list:
                            c0 = str(ch_list[0].get("id") or "").strip()
                            if c0 and c0.lower() != "default" and not c0.lower().startswith("test_chan"):
                                return c0
        except Exception:
            pass

    # 2. Check channel_gemini_keys DB table for non-test channels with valid real keys
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT channel_id, keys_json FROM channel_gemini_keys ORDER BY updated_at DESC")
        rows = cur.fetchall() or []
        conn.close()
        for r in rows:
            cid = str(r[0] if isinstance(r, (tuple, list)) else r["channel_id"]).strip()
            raw_j = r[1] if isinstance(r, (tuple, list)) else r["keys_json"]
            if not cid or cid.lower() == "default" or cid.lower().startswith("test_chan"):
                continue
            try:
                k_list = json.loads(raw_j)
                if isinstance(k_list, list) and any(_is_valid_real_gemini_key(k) for k in k_list):
                    return cid
            except Exception:
                pass
        for r in rows:
            cid = str(r[0] if isinstance(r, (tuple, list)) else r["channel_id"]).strip()
            raw_j = r[1] if isinstance(r, (tuple, list)) else r["keys_json"]
            if cid.lower().startswith("test_chan"):
                continue
            try:
                k_list = json.loads(raw_j)
                if cid and isinstance(k_list, list) and any(_is_valid_real_gemini_key(k) for k in k_list):
                    return cid
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"get_first_authenticated_channel_id DB notice: {e}")

    # 3. Check backup JSON
    backup_data = load_backup_json()
    if isinstance(backup_data, dict):
        for cid, k_list in backup_data.items():
            if cid and cid.lower() != "default" and not cid.lower().startswith("test_chan") and isinstance(k_list, list) and any(_is_valid_real_gemini_key(k) for k in k_list):
                return str(cid).strip()
        for cid, k_list in backup_data.items():
            if cid and not cid.lower().startswith("test_chan") and isinstance(k_list, list) and any(_is_valid_real_gemini_key(k) for k in k_list):
                return str(cid).strip()

    return "default"


def get_channel_keys(channel_id: Optional[str] = None, strict_channel_only: bool = False) -> List[str]:
    """
    Retrieves the array of up to 10 Gemini API keys for a channel ID.
    Prioritizes keys bound to `channel_id`, and (unless strict_channel_only=True)
    appends any additional failover keys from other configured channels in DB,
    backup JSON, user_accounts.json, gemini_config.json, and GEMINI_API_KEY env.
    """
    clean_id = (channel_id or "").strip() or "default"
    keys: List[str] = []

    # 1. Query Database for exact channel_id
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        if _DB_MODE == "postgres":
            cur.execute("SELECT keys_json FROM channel_gemini_keys WHERE channel_id = %s", (clean_id,))
        else:
            cur.execute("SELECT keys_json FROM channel_gemini_keys WHERE channel_id = ?", (clean_id,))
        row = cur.fetchone()

        if row:
            raw_json = row[0] if isinstance(row, (tuple, list)) else row["keys_json"]
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                keys.extend(k.strip() for k in parsed if _is_valid_real_gemini_key(k))

        # 1b. Pool additional failover keys from any configured channel in DB
        if not strict_channel_only:
            cur.execute("SELECT channel_id, keys_json FROM channel_gemini_keys ORDER BY updated_at DESC")
            all_rows = cur.fetchall() or []
            for r in all_rows:
                row_cid = str(r[0] if isinstance(r, (tuple, list)) else r["channel_id"]).strip()
                if row_cid.lower().startswith("test_chan"):
                    continue
                raw_j = r[1] if isinstance(r, (tuple, list)) else r["keys_json"]
                try:
                    p_list = json.loads(raw_j)
                    if isinstance(p_list, list):
                        for k in p_list:
                            if _is_valid_real_gemini_key(k):
                                keys.append(k.strip())
                except Exception:
                    pass
        conn.close()
    except Exception as e:
        logger.warning(f"DB query error for channel {clean_id}: {e}")

    # 2. Check Backup JSON
    backup_data = load_backup_json()
    if isinstance(backup_data, dict):
        if clean_id in backup_data and isinstance(backup_data[clean_id], list):
            for k in backup_data[clean_id]:
                if _is_valid_real_gemini_key(k):
                    keys.append(k.strip())
        if not strict_channel_only:
            for ch_k, k_list in backup_data.items():
                if str(ch_k).lower().startswith("test_chan"):
                    continue
                if isinstance(k_list, list):
                    for k in k_list:
                        if _is_valid_real_gemini_key(k):
                            keys.append(k.strip())

    if strict_channel_only:
        seen_s = set()
        dedup_s = []
        for k in keys:
            if k not in seen_s:
                seen_s.add(k)
                dedup_s.append(k)
        return dedup_s[:10]

    # 3. Check user_accounts.json if keys were saved on account objects
    if os.path.exists(ACCOUNTS_STORE_FILE):
        try:
            with open(ACCOUNTS_STORE_FILE, "r", encoding="utf-8") as f:
                acc_store = json.load(f)
            if isinstance(acc_store, dict):
                for acc in acc_store.values():
                    if isinstance(acc, dict):
                        acc_keys = acc.get("gemini_keys") or acc.get("api_keys") or []
                        if isinstance(acc_keys, list):
                            for k in acc_keys:
                                if _is_valid_real_gemini_key(k):
                                    keys.append(k.strip())
                        single_k = acc.get("gemini_api_key") or acc.get("api_key")
                        if _is_valid_real_gemini_key(single_k):
                            keys.append(single_k.strip())
        except Exception:
            pass

    # 4. Check gemini_config.json
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                k = cfg.get("api_key", "").strip()
                if _is_valid_real_gemini_key(k):
                    keys.append(k)
                extra_pool = cfg.get("keys_pool") or cfg.get("keys") or []
                if isinstance(extra_pool, list):
                    for ek in extra_pool:
                        if _is_valid_real_gemini_key(ek):
                            keys.append(ek.strip())
        except Exception:
            pass

    # 5. Check GEMINI_API_KEY / GOOGLE_API_KEY environment variables
    for env_var in ["GEMINI_API_KEY", "GOOGLE_API_KEY"]:
        env_key = os.environ.get(env_var, "").strip()
        if env_key:
            for k in env_key.split(","):
                if _is_valid_real_gemini_key(k):
                    keys.append(k.strip())

    # Deduplicate while preserving priority order and limit to 10
    seen = set()
    deduped = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
            if len(deduped) >= 10:
                break
    return deduped


def save_channel_keys_to_db(channel_id: str, keys: List[str], mirror_backup: bool = True) -> bool:
    """
    Saves an array of up to 10 Gemini API keys bound to channel_id.
    Writes to primary DB table and mirrors to persistent backup JSON.
    """
    clean_id = (channel_id or "").strip() or "default"
    # Deduplicate and limit to 10
    clean_keys = []
    seen = set()
    for k in keys:
        if isinstance(k, str) and k.strip() and k.strip() not in seen:
            clean_keys.append(k.strip())
            seen.add(k.strip())
            if len(clean_keys) >= 10:
                break

    keys_json = json.dumps(clean_keys)
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Save to Database
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        if _DB_MODE == "postgres":
            cur.execute("""
                INSERT INTO channel_gemini_keys (channel_id, keys_json, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (channel_id) DO UPDATE
                SET keys_json = EXCLUDED.keys_json, updated_at = EXCLUDED.updated_at;
            """, (clean_id, keys_json, now_str))
        else:
            cur.execute("""
                INSERT INTO channel_gemini_keys (channel_id, keys_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (channel_id) DO UPDATE
                SET keys_json = excluded.keys_json, updated_at = excluded.updated_at;
            """, (clean_id, keys_json, now_str))
        conn.commit()
        conn.close()
        logger.info(f"Saved {len(clean_keys)} keys to DB for channel '{clean_id}'")
    except Exception as e:
        logger.error(f"Error saving keys to DB for {clean_id}: {e}")
        return False

    # 2. Mirror to persistent backup JSON
    if mirror_backup:
        try:
            b_data = load_backup_json()
            b_data[clean_id] = clean_keys
            save_backup_json(b_data)
        except Exception as e:
            logger.warning(f"Error mirroring to backup JSON: {e}")

    # 3. If primary key is set, keep gemini_config.json in sync for legacy compatibility
    if clean_keys:
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            else:
                cfg = {}
            cfg["api_key"] = clean_keys[0]
            cfg["updated_at"] = now_str
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    return True


def verify_gemini_key_online(key: str) -> Tuple[bool, str]:
    """Tests if a Gemini API key is active and authorized."""
    if not key or not isinstance(key, str) or len(key.strip()) < 10:
        return False, "Key is too short or empty"
    clean_k = key.strip()
    try:
        from google import genai
        client = genai.Client(api_key=clean_k)
        # Fast lightweight ping
        client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents="PING"
        )
        return True, "Key verified successfully"
    except Exception as e:
        err = str(e)
        if "API_KEY_INVALID" in err or "API key not valid" in err:
            return False, "Google Gemini reported: API key is invalid"
        elif "PERMISSION_DENIED" in err:
            return False, "Permission denied for this key. Please check Google AI Studio."
        # If rate limited (429) or 503, the key itself IS valid, just busy
        if "429" in err or "RESOURCE_EXHAUSTED" in err or "503" in err:
            return True, "Key is valid (currently experiencing quota/high demand)"
        return True, f"Key saved with warning: {err[:80]}"


def add_channel_key(channel_id: str, new_key: str, verify: bool = True) -> Tuple[bool, str]:
    """
    Adds a new Gemini API key to the channel's 10-key pool.
    Enforces maximum of 10 keys per channel.
    """
    clean_key = (new_key or "").strip()
    if not clean_key:
        return False, "API key cannot be empty"

    current_keys = get_channel_keys(channel_id, strict_channel_only=True)
    if clean_key in current_keys:
        return False, "This API key is already in this channel's pool"

    if len(current_keys) >= 10:
        return False, "Maximum of 10 API keys reached for this channel. Remove or replace an existing key first."

    if verify:
        ok, msg = verify_gemini_key_online(clean_key)
        if not ok:
            return False, msg

    current_keys.append(clean_key)
    saved = save_channel_keys_to_db(channel_id, current_keys, mirror_backup=True)
    if saved:
        return True, f"Successfully added key ({mask_key(clean_key)}) to channel pool (Slot #{len(current_keys)})"
    return False, "Failed to save key to database"


def remove_channel_key(channel_id: str, index: int) -> Tuple[bool, str]:
    """
    Removes a key from the channel's pool by its index (0 to 9).
    Manual delete only.
    """
    current_keys = get_channel_keys(channel_id, strict_channel_only=True)
    if not current_keys:
        current_keys = get_channel_keys(channel_id)
    if index < 0 or index >= len(current_keys):
        return False, f"Invalid key index: {index}"

    removed_key = current_keys.pop(index)
    save_channel_keys_to_db(channel_id, current_keys, mirror_backup=True)
    return True, f"Removed key ({mask_key(removed_key)}) from channel pool"


def update_channel_key(channel_id: str, index: int, new_key: str, verify: bool = True) -> Tuple[bool, str]:
    """
    Replaces an existing key at slot `index` with a new key.
    """
    clean_key = (new_key or "").strip()
    if not clean_key:
        return False, "New key cannot be empty"

    current_keys = get_channel_keys(channel_id, strict_channel_only=True)
    if not current_keys:
        current_keys = get_channel_keys(channel_id)
    if index < 0 or index >= len(current_keys):
        return False, f"Invalid key slot: {index}"

    if verify:
        ok, msg = verify_gemini_key_online(clean_key)
        if not ok:
            return False, msg

    current_keys[index] = clean_key
    save_channel_keys_to_db(channel_id, current_keys, mirror_backup=True)
    return True, f"Updated Slot #{index + 1} with new key ({mask_key(clean_key)})"


def get_next_channel_key(channel_id: Optional[str] = None) -> Optional[str]:
    """
    Returns the next Gemini API key in round-robin sequence for this channel.
    """
    clean_id = (channel_id or "").strip() or get_first_authenticated_channel_id()
    keys = get_channel_keys(clean_id)
    if not keys:
        return None

    global _ROTATION_INDICES
    current_idx = _ROTATION_INDICES.get(clean_id, 0)
    selected_key = keys[current_idx % len(keys)]
    _ROTATION_INDICES[clean_id] = (current_idx + 1) % len(keys)
    return selected_key


def get_channel_key_pool_status(channel_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Returns detailed status of the channel's 10-key pool for the UI modal.
    """
    clean_id = (channel_id or "").strip()
    if not clean_id or clean_id.lower() == "default":
        clean_id = get_first_authenticated_channel_id()
    keys = get_channel_keys(clean_id)
    curr_idx = _ROTATION_INDICES.get(clean_id, 0) % max(len(keys), 1) if keys else 0

    slots = []
    for i in range(10):
        if i < len(keys):
            slots.append({
                "slot": i + 1,
                "index": i,
                "has_key": True,
                "masked_key": mask_key(keys[i]),
                "is_current": (i == curr_idx),
                "status": "Active"
            })
        else:
            slots.append({
                "slot": i + 1,
                "index": i,
                "has_key": False,
                "masked_key": "",
                "is_current": False,
                "status": "Empty"
            })

    return {
        "channel_id": clean_id,
        "total_active_keys": len(keys),
        "max_keys": 10,
        "current_rotation_index": curr_idx,
        "current_key_masked": mask_key(keys[curr_idx]) if keys else "",
        "slots": slots
    }


def execute_with_channel_key_rotation(
    channel_id: Optional[str],
    operation_name: Any,
    action_fn: Optional[Callable[[Any, str], Any]] = None
) -> Any:
    """
    Executes action_fn(client, api_key) with automatic round-robin and
    instant failover across all available keys in the channel's pool.
    """
    if action_fn is None and callable(operation_name):
        action_fn = operation_name
        operation_name = "GeminiOperation"
    clean_id = (channel_id or "").strip()
    if not clean_id or clean_id.lower() == "default":
        clean_id = get_first_authenticated_channel_id()
    keys = get_channel_keys(clean_id)

    if not keys:
        raise ValueError(
            f"No Gemini API keys found for channel '{clean_id}'. "
            "Please add at least one Gemini API key in the Gemini Settings panel."
        )

    from google import genai

    global _ROTATION_INDICES
    start_idx = _ROTATION_INDICES.get(clean_id, 0) % len(keys)
    total_keys = len(keys)
    last_err = None

    for attempt in range(total_keys):
        idx = (start_idx + attempt) % total_keys
        key = keys[idx]
        masked = mask_key(key)

        try:
            client = genai.Client(api_key=key)
            res = action_fn(client, key)
            if res is not False and res is not None:
                _ROTATION_INDICES[clean_id] = (idx + 1) % total_keys
                return res
            logger.warning(
                f"[{operation_name}] Key Slot #{idx + 1} ({masked}) returned empty/false result. "
                f"Rotating to next key in pool ({attempt + 1}/{total_keys})..."
            )
        except Exception as e:
            last_err = e
            logger.warning(
                f"[{operation_name}] Key Slot #{idx + 1} ({masked}) encountered error ({e}). "
                f"Rotating to next key in channel pool ({attempt + 1}/{total_keys})..."
            )
            continue

    if last_err:
        raise RuntimeError(
            f"All {total_keys} Gemini API keys in channel pool '{clean_id}' failed or hit quota limits. "
            f"Last error: {last_err}."
        )
    return False


# Automatically initialize DB tables on import
init_channel_key_db()
