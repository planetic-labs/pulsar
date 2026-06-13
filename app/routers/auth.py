from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import login_user, logout_user
from app.config import get_app_settings
from app.core import templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Auth"])


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None) -> Response:
    """Renders the login page with an optional error message."""
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.post("/login")
async def login_post(
    request: Request,
    response: Response,
    token: str | None = Form(None),
    email: str | None = Form(None),
    code: str | None = Form(None),
) -> Response:
    """Handles authentication via access token or Ark Messenger email confirmation."""
    # 1. Fallback / Static access token login
    if token:
        if login_user(response, request, token):
            return RedirectResponse(url="/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid access token"})

    # 2. Ark Messenger 2-step login (email + code)
    if email and code:
        app_settings = get_app_settings()
        if not app_settings.ark_jwks_url:
            return templates.TemplateResponse(
                request, "login.html", {"error": "Email authentication is not configured"}
            )

        base_url = app_settings.ark_jwks_url.rsplit("/.well-known/jwks.json", 1)[0]
        verify_url = f"{base_url}/api/v1/auth/verify-code"

        async with httpx.AsyncClient() as client:
            try:
                res = await client.post(verify_url, json={"email": email, "code": code}, timeout=10.0)
                if res.status_code == 200:
                    data = res.json()
                    next_step = data.get("next")
                    if next_step == "home":
                        access_token = data.get("access_token")
                        refresh_token = data.get("refresh_token")
                        if access_token and login_user(response, request, access_token, refresh_token=refresh_token):
                            return RedirectResponse(url="/", status_code=303)
                        else:
                            return templates.TemplateResponse(
                                request, "login.html", {"error": "Failed to log in with returned token"}
                            )
                    elif next_step == "setup_profile":
                        return templates.TemplateResponse(
                            request,
                            "login.html",
                            {
                                "error": (
                                    "Профиль еще не заполнен. Пожалуйста, завершите настройку профиля в мессенджере."
                                ),
                                "email": email,
                                "code": code,
                            },
                        )
                    else:
                        return templates.TemplateResponse(
                            request, "login.html", {"error": f"Unexpected login state: {next_step}"}
                        )
                elif res.status_code == 401:
                    try:
                        detail = res.json().get("detail", "Неверный пинкод или email")
                    except Exception:
                        detail = "Неверный пинкод или email"
                    return templates.TemplateResponse(
                        request, "login.html", {"error": detail, "email": email, "code": code}
                    )
                else:
                    return templates.TemplateResponse(
                        request, "login.html", {"error": f"Ошибка сервера авторизации: {res.status_code}"}
                    )
            except Exception as e:
                logger.error(f"Error calling Ark Messenger verify-code: {e}")
                return templates.TemplateResponse(
                    request, "login.html", {"error": "Не удалось связаться с сервером авторизации"}
                )

    return templates.TemplateResponse(request, "login.html", {"error": "Не указаны учетные данные"})


@router.post("/api/v1/auth/identify")
async def api_auth_identify(request: Request) -> Response:
    """Triggers user identification sequence in Ark Messenger."""
    app_settings = get_app_settings()
    if not app_settings.ark_jwks_url:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Email authentication is not configured"
        )

    base_url = app_settings.ark_jwks_url.rsplit("/.well-known/jwks.json", 1)[0]
    identify_url = f"{base_url}/api/v1/auth/identify"

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from e

    email = body.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Missing email field")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(identify_url, json={"email": email}, timeout=10.0)
            return Response(content=response.content, status_code=response.status_code, media_type="application/json")
        except Exception as e:
            logger.error(f"Error calling Ark Messenger identify: {e}")
            raise HTTPException(status_code=502, detail="Error communicating with authentication server") from e


@router.get("/logout")
def logout(request: Request, response: Response) -> Response:
    """Logs out the user and redirects to the login screen."""
    logout_user(response, request)
    return RedirectResponse(url="/login")


@router.post("/api/v1/webhooks/revocation")
async def handle_revocation_webhook(request: Request) -> dict[str, str]:
    """Handles user/session revocation webhooks sent by the identity provider."""
    import hashlib
    import hmac
    import json

    from app.auth import revoke_session, revoke_user

    app_settings = get_app_settings()

    # 1. Check if webhook secret is configured
    webhook_secret = app_settings.ark_webhook_secret
    if not webhook_secret:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Webhook revocation is not configured")

    # 2. Get raw request body for signature verification
    payload = await request.body()

    # 3. Extract signature from headers
    signature = request.headers.get("X-Ark-Signature")
    if not signature:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing signature header")

    # 4. Calculate expected signature
    expected_signature = hmac.new(webhook_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    # 5. Securely compare signatures
    if not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    # 6. Parse JSON payload and revoke session/user
    try:
        data = json.loads(payload)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON payload: {str(e)}") from e

    user_id = data.get("user_id")
    jti = data.get("jti")

    if not user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing user_id in payload")

    if jti:
        revoke_session(jti)
    else:
        revoke_user(user_id)

    return {"status": "ok"}
