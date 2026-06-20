from __future__ import annotations

import asyncio
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


async def is_jti_revoked(jti: str) -> bool:
    from app.config import get_sqlite_settings
    from app.db import db_connection

    def _sync():
        with db_connection(get_sqlite_settings()) as conn:
            row = conn.execute("SELECT 1 FROM revoked_sessions WHERE jti = ?", (jti,)).fetchone()
            return row is not None

    try:
        return await asyncio.to_thread(_sync)
    except Exception:
        return False


async def is_user_revoked(user_id: str) -> bool:
    from app.config import get_sqlite_settings
    from app.db import db_connection

    def _sync():
        with db_connection(get_sqlite_settings()) as conn:
            row = conn.execute("SELECT 1 FROM revoked_users WHERE user_id = ?", (user_id,)).fetchone()
            return row is not None

    try:
        return await asyncio.to_thread(_sync)
    except Exception:
        return False


async def revoke_session(jti: str) -> None:
    from app.config import get_sqlite_settings
    from app.db import db_connection

    def _sync():
        with db_connection(get_sqlite_settings()) as conn:
            conn.execute("INSERT OR IGNORE INTO revoked_sessions (jti) VALUES (?)", (jti,))

    await asyncio.to_thread(_sync)


async def revoke_user(user_id: str) -> None:
    from app.config import get_sqlite_settings
    from app.db import db_connection

    def _sync():
        with db_connection(get_sqlite_settings()) as conn:
            conn.execute("INSERT OR IGNORE INTO revoked_users (user_id) VALUES (?)", (user_id,))

    await asyncio.to_thread(_sync)


async def is_valid_token(token: str | None) -> bool:
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
        try:
            # Load signing key from JWKS based on kid in the JWT token header
            signing_key = jwk_client.get_signing_key_from_jwt(token)
        except Exception as e:
            import logging

            logging.warning(f"JWT validation failed: could not load signing key: {e}")
            return False

        # Decode & verify signature, exp, iat, aud, iss
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.ark_audience,
            issuer=settings.ark_issuer,
            options={"verify_exp": True, "require": ["exp", "iat", "aud", "iss"]},
        )

        # Business validation: status must be active
        status_val = payload.get("status")
        if status_val != "active":
            import logging

            sub = payload.get("sub")
            logging.error(f"JWT status check failed: expected 'active', got {status_val!r} for sub: {sub}")
            return False

        # Check blacklist (revoked sessions and users)
        jti = payload.get("jti")
        user_id = payload.get("sub")

        if jti and await is_jti_revoked(jti):
            return False
        if user_id and await is_user_revoked(user_id):
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
    # 1. Check Session (managed by SessionMiddleware)
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


def perform_token_refresh(refresh_token: str) -> dict | None:
    import logging

    import httpx

    settings = get_app_settings()
    if not settings.ark_jwks_url:
        return None

    base_url = settings.ark_jwks_url.rsplit("/.well-known/jwks.json", 1)[0]
    refresh_url = f"{base_url}/api/v1/auth/refresh"

    try:
        with httpx.Client() as client:
            res = client.post(refresh_url, json={"refresh_token": refresh_token}, timeout=10.0)
            if res.status_code == 200:
                return res.json()
            elif res.status_code in (400, 401, 403):
                logging.warning(
                    f"Failed to refresh token (invalid token), status code: {res.status_code}, response: {res.text}"
                )
                return None
            else:
                logging.error(
                    f"Failed to refresh token (server error), status code: {res.status_code}, response: {res.text}"
                )
                res.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise e
    except httpx.RequestError as e:
        logging.error(f"Network error calling Ark Messenger refresh-token: {e}")
        raise e
    except Exception as e:
        logging.error(f"Unexpected error calling Ark Messenger refresh-token: {e}", exc_info=True)
        raise e


async def require_access_token(request: Request) -> str:
    import httpx

    token = get_session_token(request)

    if not token or not await is_valid_token(token):
        refresh_token = request.session.get("refresh_token")
        if refresh_token:
            try:
                new_tokens = perform_token_refresh(refresh_token)
                if new_tokens:
                    new_access_token = new_tokens.get("access_token")
                    new_refresh_token = new_tokens.get("refresh_token")
                    if new_access_token and await is_valid_token(new_access_token):
                        request.session["access_token"] = new_access_token
                        if new_refresh_token:
                            request.session["refresh_token"] = new_refresh_token
                        token = new_access_token
                    else:
                        request.session.pop("refresh_token", None)
                        request.session.pop("access_token", None)
                else:
                    request.session.pop("refresh_token", None)
                    request.session.pop("access_token", None)
            except (httpx.HTTPError, httpx.RequestError) as e:
                # Do NOT clear the session on network or 5xx backend errors to prevent auto-logout.
                # Simply raise a temporary unavailable error to allow retry on next request.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authentication service is temporarily unavailable. Please retry.",
                ) from e
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Unexpected error during token refresh.",
                ) from e

    if not token or not await is_valid_token(token):
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
        except jwt.PyJWTError:
            request.state.user_id = None
            request.state.roles = []

    return str(token)


async def require_admin(request: Request) -> str:
    token = await require_access_token(request)
    if "admin" not in getattr(request.state, "roles", []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Admin role required",
        )
    return token


async def login_user(response: Response, request: Request, token: str, refresh_token: str | None = None) -> bool:
    if await is_valid_token(token):
        # Keep validated access token server-side; never write user-supplied token directly to cookie.
        request.session["access_token"] = token
        if refresh_token:
            request.session["refresh_token"] = refresh_token

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
