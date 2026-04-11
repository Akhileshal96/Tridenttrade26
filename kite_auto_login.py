"""Automated Kite access token renewal using TOTP.

Mimics the manual browser login flow entirely via HTTP:
  1. POST credentials to Zerodha login
  2. POST TOTP code (generated from secret key)
  3. Extract request_token from redirect URL
  4. Call generate_session() to get access_token
  5. Save to .env + invalidate cached kite instance

Required .env keys:
    KITE_USER_ID       — Zerodha user ID (e.g. ZY1234)
    KITE_PASSWORD      — Zerodha login password
    KITE_TOTP_SECRET   — TOTP secret key from Zerodha security settings
    KITE_API_KEY       — Kite Connect API key
    KITE_API_SECRET    — Kite Connect API secret
"""

import os
import re
import time

import pyotp
import requests
from kiteconnect import KiteConnect

from env_utils import set_env_value
from log_store import append_log

_LOGIN_URL = "https://kite.zerodha.com/api/login"
_TWOFA_URL = "https://kite.zerodha.com/api/twofa"


def _get_required(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(f"{key} missing in .env")
    return val


def auto_renew_kite_token() -> tuple[bool, str]:
    """Perform full automated login and return (success, message).

    Returns (True, access_token) on success, (False, error_message) on failure.
    Safe to call from asyncio via asyncio.to_thread().
    """
    try:
        user_id     = _get_required("KITE_USER_ID")
        password    = _get_required("KITE_PASSWORD")
        totp_secret = _get_required("KITE_TOTP_SECRET")
        api_key     = _get_required("KITE_API_KEY")
        api_secret  = _get_required("KITE_API_SECRET")
    except RuntimeError as e:
        return False, str(e)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "X-Kite-Version": "3",
    })

    # ── Step 1: Submit user ID + password ──────────────────────────────────
    try:
        r1 = session.post(
            _LOGIN_URL,
            data={"user_id": user_id, "password": password},
            timeout=15,
        )
        r1.raise_for_status()
        j1 = r1.json()
    except Exception as e:
        return False, f"login_post_failed: {e}"

    if j1.get("status") != "success":
        msg = j1.get("message") or j1.get("error") or str(j1)
        return False, f"login_rejected: {msg}"

    request_id = j1.get("data", {}).get("request_id") or ""
    if not request_id:
        return False, "login_missing_request_id"

    # ── Step 2: Submit TOTP code ────────────────────────────────────────────
    # Generate code; if we're within 2 seconds of a 30s boundary, wait for
    # the next code to avoid a stale-code rejection.
    totp = pyotp.TOTP(totp_secret)
    remaining = 30 - (int(time.time()) % 30)
    if remaining <= 2:
        time.sleep(remaining + 1)
    twofa_code = totp.now()

    try:
        r2 = session.post(
            _TWOFA_URL,
            data={
                "user_id":    user_id,
                "request_id": request_id,
                "twofa_value": twofa_code,
                "twofa_type": "totp",
                "skip_session": "",
            },
            timeout=15,
        )
        r2.raise_for_status()
        j2 = r2.json()
    except Exception as e:
        return False, f"twofa_post_failed: {e}"

    if j2.get("status") != "success":
        msg = j2.get("message") or j2.get("error") or str(j2)
        return False, f"twofa_rejected: {msg}"

    # ── Step 3: Extract request_token from redirect URL ────────────────────
    # After 2FA, the session cookie is set. Hit the Kite login URL to get
    # the redirect containing request_token.
    login_url = (
        f"https://kite.zerodha.com/connect/login"
        f"?api_key={api_key}&v=3"
    )
    try:
        r3 = session.get(login_url, timeout=15, allow_redirects=True)
    except Exception as e:
        return False, f"redirect_fetch_failed: {e}"

    # request_token is in the final URL after redirect
    final_url = r3.url
    match = re.search(r"request_token=([A-Za-z0-9]+)", final_url)
    if not match:
        # Also check response text for embedded token
        match = re.search(r"request_token=([A-Za-z0-9]+)", r3.text)
    if not match:
        return False, f"request_token_not_found_in_redirect url={final_url[:120]}"

    request_token = match.group(1)

    # ── Step 4: Generate access token ──────────────────────────────────────
    try:
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data["access_token"]
    except Exception as e:
        return False, f"generate_session_failed: {e}"

    # ── Step 5: Validate token before persisting ──────────────────────────
    # Call margins() with a fresh KiteConnect instance. If this fails the
    # token is bad (clock-skew, API error, etc.) and we must NOT overwrite
    # the .env — the current (possibly still-valid) token stays intact.
    try:
        test_kite = KiteConnect(api_key=api_key)
        test_kite.set_access_token(access_token)
        test_kite.margins()
    except Exception as e:
        return False, f"token_validation_failed: {e}"

    # ── Step 6: Persist + invalidate cached instance ───────────────────────
    try:
        set_env_value("KITE_ACCESS_TOKEN", access_token)
        os.environ["KITE_ACCESS_TOKEN"] = access_token
        from broker_zerodha import invalidate_kite
        invalidate_kite()
    except Exception as e:
        return False, f"save_token_failed: {e}"

    append_log("INFO", "AUTH", "kite_token_auto_renewed via TOTP")
    return True, access_token
