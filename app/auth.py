from __future__ import annotations

from fastapi import HTTPException, Request, status, Response
from app.config import get_app_settings

AUTH_COOKIE_NAME = "access_token"

def get_session_token(request: Request) -> str | None:
    # 1. Check Query Params (direct link)
    token = request.query_params.get("token")
    if token:
        return token
    
    # 2. Check Session (managed by SessionMiddleware)
    token = request.session.get("token")
    if token:
        return token
        
    # 3. Check Cookies (fallback/persistence)
    return request.cookies.get(AUTH_COOKIE_NAME)

def require_access_token(request: Request) -> str:
    settings = get_app_settings()
    token = get_session_token(request)
    
    if token != settings.access_token:
        # We don't raise 401 here to allow redirect to login in the route handler if needed,
        # or we can raise it and catch it in an exception handler.
        # For simplicity in main.py, let's raise 401.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing access token",
        )
    
    # If token was in query params but not in session, save it to session
    if request.query_params.get("token") == settings.access_token:
        request.session["token"] = token
        
    return token

def login_user(response: Response, request: Request, token: str) -> bool:
    settings = get_app_settings()
    if token == settings.access_token:
        request.session["token"] = token
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            httponly=True,
            max_age=60 * 60 * 24 * 7, # 7 days
            samesite="lax"
        )
        return True
    return False

def logout_user(response: Response, request: Request):
    request.session.clear()
    response.delete_cookie(AUTH_COOKIE_NAME)

