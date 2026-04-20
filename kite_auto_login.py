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

_MAX_INNER_RETRIES = 3   # full-flow retries inside auto_renew_kite_token
_INNER_RETRY_WAIT  = 8   # seconds between inner retries
_MAX_HOPS          = 10  # redirect hops to follow when hunting request_token
_REQUEST_TIMEOUT   = 12  # seconds per HTTP request


def _get_required(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(f"{key} missing in .env")
    return val


def _fresh_totp(secret: str) -> str:
    """Generate a fresh TOTP, waiting out the last 2 s of any window."""
    totp = pyotp.TOTP(secret)
    remaining = 30 - (int(time.time()) % 30)
    if remaining <= 2:
        time.sleep(remaining + 1)
    return totp.now()


def _extract_request_token(text: str) -> str | None:
    """Pull request_token out of any string (URL, header, body)."""
    m = re.search(r"request_token=([A-Za-z0-9]+)", text)
    return m.group(1) if m else None


def _attempt_login(user_id: str, password: str, totp_secret: str,
                   api_key: str, api_secret: str) -> tuple[bool, str]:
    """One full login attempt. Returns (True, access_token) or (False, reason)."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "X-Kite-Version": "3",
        "Accept": "application/json, text/plain, */*",
    })

    # ── Step 1: Submit user ID + password ─────────────────────────────────
    try:
        r1 = session.post(
            _LOGIN_URL,
            data={"user_id": user_id, "password": password},
            timeout=_REQUEST_TIMEOUT,
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
        return False, f"login_missing_request_id response={str(j1)[:200]}"

    # ── Step 2: Submit TOTP code ───────────────────────────────────────────
    twofa_code = _fresh_totp(totp_secret)
    try:
        r2 = session.post(
            _TWOFA_URL,
            data={
                "user_id":     user_id,
                "request_id":  request_id,
                "twofa_value": twofa_code,
                "twofa_type":  "totp",
                "skip_session": "",
            },
            timeout=_REQUEST_TIMEOUT,
        )
        r2.raise_for_status()
        j2 = r2.json()
    except Exception as e:
        return False, f"twofa_post_failed: {e}"

    if j2.get("status") != "success":
        msg = j2.get("message") or j2.get("error") or str(j2)
        return False, f"twofa_rejected: {msg}"

    # ── Step 3: Hunt for request_token in the redirect chain ──────────────
    #
    # Zerodha may change intermediate redirect steps at any time. Strategy:
    #   a) Follow redirects manually (allow_redirects=False), hop by hop,
    #      searching every Location header, URL, and body for request_token.
    #   b) If that fails after _MAX_HOPS, retry with allow_redirects=True
    #      and catch the final URL even if the destination is unreachable
    #      (our redirect_url on EC2 won't be running, but requests still
    #      records the URL it was about to fetch before the connection error).
    #
    connect_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    request_token = None
    last_location = ""

    # -- 3a: manual hop-by-hop --
    current_url = connect_url
    for hop in range(_MAX_HOPS):
        try:
            r3 = session.get(current_url, timeout=_REQUEST_TIMEOUT, allow_redirects=False)
        except Exception as e:
            append_log("WARN", "AUTH", f"redirect_hop_{hop}_failed: {e} url={current_url[:120]}")
            break

        location = r3.headers.get("Location") or ""
        last_location = location or current_url

        # Search Location header, current URL, and full response body
        for source in (location, str(r3.url), r3.text):
            token = _extract_request_token(source)
            if token:
                request_token = token
                break
        if request_token:
            break

        # Follow zerodha.com intermediate redirects; stop on external redirects
        if location and "zerodha.com" in location:
            append_log("INFO", "AUTH", f"redirect_hop={hop} following url={location[:120]}")
            current_url = location
            continue

        # No more zerodha redirects — we should have found it by now
        append_log("INFO", "AUTH", f"redirect_hop={hop} no_zerodha_location loc={location[:120]}")
        break

    # -- 3b: allow_redirects=True fallback (catches the final URL) --
    if not request_token:
        append_log("INFO", "AUTH", "trying allow_redirects=True fallback to find request_token")
        try:
            r_full = session.get(connect_url, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
            for source in (str(r_full.url), r_full.text):
                token = _extract_request_token(source)
                if token:
                    request_token = token
                    append_log("INFO", "AUTH", "request_token found via allow_redirects fallback")
                    break
        except requests.exceptions.ConnectionError as ce:
            # Redirect landed on our (unreachable) redirect_url — grab it from the exception
            url_in_exc = _extract_request_token(str(ce))
            if url_in_exc:
                request_token = url_in_exc
                append_log("INFO", "AUTH", "request_token found in ConnectionError URL")
        except Exception as e:
            append_log("WARN", "AUTH", f"allow_redirects fallback failed: {e}")

    if not request_token:
        return False, (
            f"request_token_not_found "
            f"last_location={last_location[:120]} "
            f"hops_tried={min(hop+1, _MAX_HOPS)}"
        )

    # ── Step 4: Generate access token ─────────────────────────────────────
    try:
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data["access_token"]
    except Exception as e:
        return False, f"generate_session_failed: {e}"

    # ── Step 5: Validate token before persisting ──────────────────────────
    try:
        test_kite = KiteConnect(api_key=api_key)
        test_kite.set_access_token(access_token)
        test_kite.margins()
    except Exception as e:
        return False, f"token_validation_failed: {e}"

    # ── Step 6: Persist + invalidate cached instance ──────────────────────
    try:
        set_env_value("KITE_ACCESS_TOKEN", access_token)
        os.environ["KITE_ACCESS_TOKEN"] = access_token
        from broker_zerodha import invalidate_kite
        invalidate_kite()
    except Exception as e:
        return False, f"save_token_failed: {e}"

    return True, access_token


def auto_renew_kite_token() -> tuple[bool, str]:
    """Perform full automated login and return (success, message).

    Retries the entire flow up to _MAX_INNER_RETRIES times with fresh TOTP
    codes so transient Zerodha errors and stale TOTP windows don't cause a
    permanent failure.

    Returns (True, access_token) on success, (False, last_error) on failure.
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

    last_error = "no_attempts"
    for attempt in range(1, _MAX_INNER_RETRIES + 1):
        append_log("INFO", "AUTH", f"kite_login_attempt={attempt}/{_MAX_INNER_RETRIES}")
        ok, result = _attempt_login(user_id, password, totp_secret, api_key, api_secret)
        if ok:
            append_log("INFO", "AUTH", f"kite_token_auto_renewed via TOTP attempt={attempt}")
            return True, result
        last_error = result
        append_log("WARN", "AUTH", f"kite_login_attempt={attempt} failed: {result}")
        if attempt < _MAX_INNER_RETRIES:
            # Wait for the next TOTP window before retrying so a stale code
            # on one attempt doesn't bleed into the next.
            remaining = 30 - (int(time.time()) % 30)
            wait = max(_INNER_RETRY_WAIT, remaining + 1)
            append_log("INFO", "AUTH", f"waiting {wait}s before retry")
            time.sleep(wait)

    return False, f"all_{_MAX_INNER_RETRIES}_attempts_failed last_error={last_error}"
