from __future__ import annotations

import secrets
import jwt
from fastapi import HTTPException, Request, Response, status

from app.config import get_app_settings

AUTH_COOKIE_NAME = "access_token"

_jwk_client = None


def get_jwk_client() -> jwt.PyJWKClient | None:
    global _jwk_client
    if _jwk_client is None:
        settings = get_app_settings()
        if settings.ark_jwks_url:
            _jwk_client = jwt.PyJWKClient(settings.ark_jwks_url, cache_keys=True, max_cached_keys=5)
    return _jwk_client


def is_jti_revoked(jti: str) -> bool:
    from app.config import get_sqlite_settings
    from app.db import db_connection

    try:
        with db_connection(get_sqlite_settings()) as conn:
            row = conn.execute("SELECT 1 FROM revoked_sessions WHERE jti = ?", (jti,)).fetchone()
            return row is not None
    except Exception:
        return False


def is_user_revoked(user_id: str) -> bool:
    from app.config import get_sqlite_settings
    from app.db import db_connection

    try:
        with db_connection(get_sqlite_settings()) as conn:
            row = conn.execute("SELECT 1 FROM revoked_users WHERE user_id = ?", (user_id,)).fetchone()
            return row is not None
    except Exception:
        return False


def revoke_session(jti: str) -> None:
    from app.config import get_sqlite_settings
    from app.db import db_connection

    with db_connection(get_sqlite_settings()) as conn:
        conn.execute("INSERT OR IGNORE INTO revoked_sessions (jti) VALUES (?)", (jti,))


def revoke_user(user_id: str) -> None:
    from app.config import get_sqlite_settings
    from app.db import db_connection

    with db_connection(get_sqlite_settings()) as conn:
        conn.execute("INSERT OR IGNORE INTO revoked_users (user_id) VALUES (?)", (user_id,))


def is_valid_token(token: str | None) -> bool:
    if not token:
        return False

    settings = get_app_settings()
    if token == settings.access_token:
        return True

    # Check third-party JWT auth
    jwk_client = get_jwk_client()
    if not jwk_client:
        return False

    try:
        # Load signing key from JWKS based on kid in the JWT token header
        try:
            signing_key = jwk_client.get_signing_key_from_jwt(token)
            keys_to_try = [signing_key]
        except Exception:
            # Fallback: try all signing keys if kid is missing or lookup fails
            keys_to_try = jwk_client.get_signing_keys()
            if not keys_to_try:
                raise

        decoded_payload = None
        last_err = None
        for k in keys_to_try:
            try:
                decoded_payload = jwt.decode(token, k.key, algorithms=["RS256"], options={"verify_exp": True})
                break
            except Exception as e:
                last_err = e

        if decoded_payload is None:
            if last_err:
                raise last_err
            else:
                raise jwt.PyJWTError("No signing keys in JWKS matched")

        payload = decoded_payload

        # Business validation: status must be active
        status_val = payload.get("status")
        if status_val != "active":
            import logging

            logging.error(f"JWT status check failed: expected 'active', got {status_val!r}. Payload: {payload}")
            return False

        # Check blacklist (revoked sessions and users)
        jti = payload.get("jti")
        user_id = payload.get("sub")

        if jti and is_jti_revoked(jti):
            return False
        if user_id and is_user_revoked(user_id):
            return False

        return True
    except jwt.PyJWTError as e:
        import logging

        logging.warning(f"JWT validation failed: {e}")
        return False
    except Exception as e:
        import logging

        logging.error(f"JWT validation failed with unexpected error: {e}", exc_info=True)
        return False


def get_session_token(request: Request) -> str | None:
    # 1. Check Query Params (direct link)
    token = request.query_params.get("token")
    if token:
        return str(token)

    # 2. Check Session (managed by SessionMiddleware)
    access_token = request.session.get("access_token")
    if access_token:
        return str(access_token)

    session_token = request.session.get("token")
    if session_token:
        return str(session_token)

    # 3. Check Cookies (fallback/persistence)
    cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        return str(cookie_token)

    # 4. Check Authorization Header (Bearer JWT)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1].strip()

    return None


def require_access_token(request: Request) -> str:
    token = get_session_token(request)

    if not token or not is_valid_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing access token",
        )

    # Save to session if valid and not already there
    if request.session.get("access_token") != token:
        request.session["access_token"] = token
    if request.session.get("token") != token:
        request.session["token"] = token

    # Write user identity and roles to request state
    settings = get_app_settings()
    if token == settings.access_token:
        request.state.user_id = "admin"
        request.state.roles = ["admin"]
    else:
        try:
            payload = jwt.decode(token, options={"verify_signature": False})
            request.state.user_id = payload.get("sub")
            request.state.roles = ["user"]
        except Exception:
            request.state.user_id = None
            request.state.roles = []

    return str(token)


def require_admin(request: Request) -> str:
    token = require_access_token(request)
    if "admin" not in getattr(request.state, "roles", []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Admin role required",
        )
    return token


def login_user(response: Response, request: Request, token: str) -> bool:
    if is_valid_token(token):
        # Keep validated access token server-side; never write user-supplied token directly to cookie.
        request.session["access_token"] = token

        # Use a server-generated opaque value for cookie/session persistence.
        session_token = secrets.token_urlsafe(32)
        request.session["token"] = session_token
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=session_token,
            httponly=True,
            max_age=60 * 60 * 24 * 7,  # 7 days
            samesite="lax",
        )
        return True
    return False


def logout_user(response: Response, request: Request):
    request.session.clear()
    response.delete_cookie(AUTH_COOKIE_NAME)
