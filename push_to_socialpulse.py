"""
Push captured session ke SocialPulse backend.

SocialPulse menerima POST ke endpoint /api/accounts dengan body:
  {
    "platform": "facebook" | "tiktok" | "instagram" | "twitter" | "threads",
    "username": "label_akun",
    "password": "<JSON storage_state STRING>"
  }

Backend (server.js) forward ke service yang tepat per platform:
  - Facebook  → pc-scraper (FacebookAccountManager)  — native storage_state
  - TikTok    → tiktok-pc  (TikTokAccountStore)      — native storage_state
  - Instagram → pc-scraper (InstagramAccountManager) — native storage_state (v3)
  - Twitter   → pc-scraper (TwitterAccountManager)   — native storage_state (v3)
  - Threads   → pc-scraper (ThreadsAccountManager)   — native storage_state (v3.1)

v3 (7 May 2026): IG & Twitter sekarang cookie-based (sebelumnya pakai
instagrapi/twikit dengan user/pass login). Push helper sekarang support
keempat platform dengan flow seragam.

v3.1 (13 May 2026): Tambah platform Threads. Threads share auth dengan
Instagram (sessionid + ds_user_id sama), tapi capture dilakukan terpisah
karena threads.net set cookie tambahan di domain-nya sendiri. Pre-flight
cek sama dengan IG.

Strategi push per platform:
  - Facebook:  kirim storage_state apa adanya (cookies + localStorage)
  - TikTok:    kirim storage_state apa adanya. Parser SocialPulse versi baru
               extract sendiri (termasuk msToken multi-domain priority).
               UNTUK BACKWARD-COMPAT: kalau detect SocialPulse versi lama
               (response error msToken/parser), retry dengan flat dict.
  - Instagram: kirim storage_state apa adanya. Pre-flight: pastikan
               sessionid + ds_user_id ada.
  - Twitter:   kirim storage_state apa adanya. Pre-flight: pastikan
               auth_token + ct0 ada.
  - Threads:   kirim storage_state apa adanya. Pre-flight: pastikan
               sessionid + ds_user_id ada (sama dengan IG).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Platform yang aman di-push (semua sekarang cookie-based di SocialPulse).
PUSHABLE = {"facebook", "tiktok", "instagram", "twitter", "threads"}

# Required cookies per platform — pre-flight check sebelum push.
# Kalau yang wajib ini kurang, kasih error yang actionable ke user
# (bukan kirim payload incomplete dan dapat 400 dari backend yang gak jelas).
REQUIRED_COOKIES = {
    "facebook":  ("c_user", "xs"),
    "tiktok":    ("msToken",),  # plus sessionid, tapi minimum msToken cukup
    "instagram": ("sessionid", "ds_user_id"),
    "twitter":   ("auth_token", "ct0"),
    "threads":   ("sessionid", "ds_user_id"),  # share auth dengan IG
}


def _flatten_cookies(state: dict) -> dict:
    """Convert storage_state cookies array → flat {name: value} dict."""
    out = {}
    for c in (state.get("cookies") or []):
        if isinstance(c, dict):
            n = c.get("name")
            v = c.get("value")
            if n and v not in (None, ""):
                # First-wins kalau duplikat (mis. msToken di .tiktok.com vs www.tiktok.com)
                if n not in out:
                    out[n] = str(v)
    return out


def _select_tiktok_mstoken(state: dict) -> dict:
    """
    Khusus TikTok: msToken muncul di multiple domain. Prioritaskan '.tiktok.com'
    (parent) di atas 'www.tiktok.com' (host-only).

    Returns flat dict yang siap di-stringify untuk POST.
    """
    cookies = state.get("cookies") or []
    flat = {}
    ms_tokens = []  # [(value, domain_score)]

    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain", "")
        if not name or value in (None, ""):
            continue

        if name == "msToken":
            score = 2 if domain.startswith(".tiktok.com") else 1
            ms_tokens.append((str(value), score))
            continue

        if name not in flat:
            flat[name] = str(value)

    if ms_tokens:
        ms_tokens.sort(key=lambda x: -x[1])
        flat["msToken"] = ms_tokens[0][0]

    return flat


def _has_cookie(state: dict, name: str) -> bool:
    """Cek apakah cookie dengan nama tertentu ada (di domain manapun)."""
    for c in (state.get("cookies") or []):
        if isinstance(c, dict) and c.get("name") == name and c.get("value"):
            return True
    return False


def _has_msToken(state: dict) -> bool:
    """Cek apakah storage_state punya cookie msToken (di domain manapun)."""
    for c in (state.get("cookies") or []):
        if isinstance(c, dict) and c.get("name") in ("msToken", "ms_token"):
            if c.get("value"):
                return True
    return False


def _check_required_cookies(state: dict, platform: str) -> Optional[str]:
    """
    Pre-flight: cek apakah semua cookie wajib platform ini ada.
    Return error message kalau ada yang kurang, None kalau lengkap.
    """
    required = REQUIRED_COOKIES.get(platform, ())
    missing = []
    for name in required:
        # TikTok msToken bisa pakai alias 'ms_token' juga
        if platform == "tiktok" and name == "msToken":
            if not (_has_cookie(state, "msToken") or _has_cookie(state, "ms_token")):
                missing.append(name)
        else:
            if not _has_cookie(state, name):
                missing.append(name)
    if missing:
        return (
            f"Cookies wajib untuk {platform} tidak ditemukan: {', '.join(missing)}. "
            f"Login mungkin belum selesai / kurang lengkap. Re-capture dulu, "
            f"pastikan semua redirect & 2FA selesai sebelum klik 'Saya Sudah Login'."
        )
    return None


def _is_parser_error(resp_json: dict, status_code: int) -> bool:
    """
    Detect: apakah ini error dari SocialPulse versi LAMA yang gak support
    storage_state? Kalau iya, kita retry dengan flat dict.

    Heuristik: 400 dengan keyword "msToken" / "ms_token" / "parse" / "cookie kosong".
    """
    if status_code != 400:
        return False
    err_text = (resp_json.get("error") or "").lower()
    parser_keywords = ("mstoken", "ms_token", "parse", "cookie kosong", "format")
    return any(kw in err_text for kw in parser_keywords)


def push_to_socialpulse(
    session_path: Path,
    platform: str,
    label: str,
    api_url: str,
    jwt_token: Optional[str] = None,
    timeout: int = 15,
) -> dict:
    """
    Push satu session file ke SocialPulse.

    api_url: base URL backend SocialPulse, mis. 'http://localhost:3001'
    jwt_token: JWT dari login SocialPulse (kalau auth aktif). Optional.

    Returns dict {ok, status_code, response_json, error?, payload_format?}.

    Strategi (TikTok khusus):
      1. Try storage_state full (lebih lengkap, parser baru bisa terima)
      2. Kalau response error parser → retry pakai flat dict (legacy)
    """
    if platform not in PUSHABLE:
        return {
            "ok": False,
            "error": (
                f"Platform '{platform}' belum di-support untuk auto-push. "
                f"Pushable: {', '.join(sorted(PUSHABLE))}."
            ),
        }

    if not session_path.exists():
        return {"ok": False, "error": f"Session file tidak ada: {session_path}"}

    try:
        state = json.loads(session_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"Gagal baca session JSON: {e}"}

    # Pre-flight: cek required cookies per-platform
    err = _check_required_cookies(state, platform)
    if err:
        return {"ok": False, "error": err}

    headers = {"Content-Type": "application/json"}
    if jwt_token:
        headers["Authorization"] = f"Bearer {jwt_token}"
    url = api_url.rstrip("/") + "/api/accounts"

    # Attempt 1: storage_state penuh (cookies + localStorage + atribut lengkap)
    storage_state_payload = json.dumps(state, ensure_ascii=False)
    body = {
        "platform": platform,
        "username": label,
        "password": storage_state_payload,
    }

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Network error: {e}"}

    try:
        resp_json = resp.json()
    except Exception:
        resp_json = {"raw_text": resp.text[:500]}

    # Success → done
    if resp.ok:
        return {
            "ok": True,
            "status_code": resp.status_code,
            "response_json": resp_json,
            "error": None,
            "payload_format": "storage_state",
        }

    # Failure: kalau ini TikTok dan looks like parser error → retry dengan flat dict
    # (untuk backward-compat dengan SocialPulse versi sebelum parser update).
    # IG & Twitter di-skip retry karena mereka selalu pakai parser baru
    # (v3, 7 May 2026 — gak ada versi lama yang perlu fallback).
    if platform == "tiktok" and _is_parser_error(resp_json, resp.status_code):
        flat = _select_tiktok_mstoken(state)
        if flat.get("msToken") or flat.get("ms_token"):
            body["password"] = json.dumps(flat, ensure_ascii=False)
            try:
                resp2 = requests.post(url, json=body, headers=headers, timeout=timeout)
            except requests.RequestException as e:
                return {"ok": False, "error": f"Network error (retry): {e}"}

            try:
                resp2_json = resp2.json()
            except Exception:
                resp2_json = {"raw_text": resp2.text[:500]}

            return {
                "ok": resp2.ok,
                "status_code": resp2.status_code,
                "response_json": resp2_json,
                "error": None if resp2.ok else (
                    resp2_json.get("error") or f"HTTP {resp2.status_code}"
                ),
                "payload_format": "flat_dict_legacy",
            }

    # No retry possible / not a parser error → return original failure
    return {
        "ok": False,
        "status_code": resp.status_code,
        "response_json": resp_json,
        "error": resp_json.get("error") or f"HTTP {resp.status_code}",
        "payload_format": "storage_state",
    }
