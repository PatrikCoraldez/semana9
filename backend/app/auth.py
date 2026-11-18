"""Login de clientes con Supabase Auth.

El navegador solo habla con esta API (la CSP de la tienda permite connect-src 'self').
Este módulo reenvía registro, inicio de sesión y renovación a Supabase Auth usando la
clave publicable guardada en .env, y verifica el access token de cada petición
preguntando a Supabase quién es el usuario (GET /auth/v1/user).

El login es opcional: comprar sigue funcionando sin sesión (contrato S09 intacto).
Si el cliente inicia sesión, sus pedidos quedan asociados en order_owners y puede ver
"Mis pedidos" con el estado de notificación y del comprobante.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .billing import billing_view
from .config import Settings
from .db import PUBLIC_DATASET
from .models import BillingTask, Order, OrderOwner
from .security import ApiError, get_session

router = APIRouter()

EMAIL_PATTERN = r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,63}$"
TOKEN_CACHE_SECONDS = 60.0
TOKEN_CACHE_MAX = 1000


@dataclass(frozen=True)
class AuthUser:
    id: uuid.UUID
    email: str


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=6, max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=6, max_length=72)


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=8, max_length=512)


_token_cache: dict[str, tuple[float, AuthUser]] = {}


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    if not settings.auth_configured():
        raise ApiError(503, "AUTH_NOT_CONFIGURED", "Login is not configured on this store")
    return settings


async def _supabase(
    settings: Settings,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    bearer: str | None = None,
) -> httpx.Response:
    headers = {"apikey": settings.auth_supabase_publishable_key.get_secret_value().strip()}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        async with httpx.AsyncClient(timeout=settings.auth_timeout_seconds) as client:
            return await client.request(
                method, f"{settings.auth_supabase_url}/auth/v1{path}", json=json, headers=headers
            )
    except httpx.HTTPError:
        raise ApiError(503, "AUTH_UNAVAILABLE", "Authentication service unavailable") from None


def _auth_error(response: httpx.Response) -> ApiError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = str(body.get("error_code") or body.get("error") or "")
    if response.status_code == 429 or "rate_limit" in code:
        return ApiError(429, "AUTH_RATE_LIMITED", "Too many attempts, try again later")
    if code == "email_not_confirmed":
        return ApiError(403, "EMAIL_NOT_CONFIRMED", "Confirm your email before signing in")
    if code in ("user_already_exists", "email_exists"):
        return ApiError(409, "EMAIL_ALREADY_REGISTERED", "That email is already registered")
    if code == "weak_password":
        return ApiError(422, "WEAK_PASSWORD", "Password is too weak")
    if response.status_code in (400, 401, 403, 422):
        return ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")
    return ApiError(503, "AUTH_UNAVAILABLE", "Authentication service unavailable")


def _session_payload(body: dict[str, Any]) -> dict[str, Any]:
    user = body.get("user") or {}
    try:
        return {
            "access_token": str(body["access_token"]),
            "refresh_token": str(body["refresh_token"]),
            "expires_at": int(body.get("expires_at") or (time.time() + int(body.get("expires_in", 3600)))),
            "user": {"id": str(uuid.UUID(str(user["id"]))), "email": str(user.get("email", ""))},
        }
    except (KeyError, ValueError, TypeError):
        raise ApiError(503, "AUTH_UNAVAILABLE", "Unexpected authentication response") from None


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 4096:
        raise ApiError(401, "UNAUTHORIZED", "Invalid Authorization header")
    return token


async def verify_access_token(settings: Settings, token: str) -> AuthUser:
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    cached = _token_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    response = await _supabase(settings, "GET", "/user", bearer=token)
    if response.status_code in (401, 403):
        raise ApiError(401, "UNAUTHORIZED", "Session expired or invalid")
    if response.status_code != 200:
        raise ApiError(503, "AUTH_UNAVAILABLE", "Authentication service unavailable")
    try:
        body = response.json()
        user = AuthUser(id=uuid.UUID(str(body["id"])), email=str(body.get("email", "")))
    except (KeyError, ValueError, TypeError):
        raise ApiError(503, "AUTH_UNAVAILABLE", "Unexpected authentication response") from None

    if len(_token_cache) >= TOKEN_CACHE_MAX:
        _token_cache.clear()
    _token_cache[key] = (now + TOKEN_CACHE_SECONDS, user)
    return user


async def optional_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthUser | None:
    """Sin header -> cliente invitado (como siempre). Con header inválido -> 401."""
    token = _bearer_token(authorization)
    if token is None:
        return None
    return await verify_access_token(_settings(request), token)


async def required_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthUser:
    token = _bearer_token(authorization)
    if token is None:
        raise ApiError(401, "UNAUTHORIZED", "Sign in required")
    return await verify_access_token(_settings(request), token)


@router.get("/api/v1/auth/status")
async def auth_status(request: Request) -> dict[str, Any]:
    return {"enabled": request.app.state.settings.auth_configured()}


@router.post("/api/v1/auth/signup")
async def signup(payload: Credentials, request: Request) -> JSONResponse:
    settings = _settings(request)
    response = await _supabase(
        settings, "POST", "/signup", json={"email": payload.email, "password": payload.password}
    )
    if response.status_code not in (200, 201):
        raise _auth_error(response)
    body = response.json()
    if body.get("access_token"):
        return JSONResponse(status_code=201, content=_session_payload(body))
    # Supabase exige confirmar el correo: no hay sesión todavía.
    return JSONResponse(status_code=202, content={"confirmation_required": True})


@router.post("/api/v1/auth/login")
async def login(payload: Credentials, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    response = await _supabase(
        settings,
        "POST",
        "/token?grant_type=password",
        json={"email": payload.email, "password": payload.password},
    )
    if response.status_code != 200:
        raise _auth_error(response)
    return _session_payload(response.json())


@router.post("/api/v1/auth/refresh")
async def refresh(payload: RefreshIn, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    response = await _supabase(
        settings,
        "POST",
        "/token?grant_type=refresh_token",
        json={"refresh_token": payload.refresh_token},
    )
    if response.status_code != 200:
        raise ApiError(401, "UNAUTHORIZED", "Session expired, sign in again")
    return _session_payload(response.json())


@router.post("/api/v1/auth/logout", status_code=204)
async def logout(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    token = _bearer_token(authorization)
    if token is None:
        return None
    _token_cache.pop(hashlib.sha256(token.encode("utf-8")).hexdigest(), None)
    settings = request.app.state.settings
    if settings.auth_configured():
        try:
            await _supabase(settings, "POST", "/logout?scope=local", bearer=token)
        except ApiError:
            pass  # cerrar sesión localmente igual
    return None


@router.get("/api/v1/me")
async def me(user: AuthUser = Depends(required_user)) -> dict[str, Any]:
    return {"id": str(user.id), "email": user.email}


@router.get("/api/v1/me/orders")
async def my_orders(
    user: AuthUser = Depends(required_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Order, BillingTask)
            .join(OrderOwner, OrderOwner.order_id == Order.id)
            .outerjoin(BillingTask, BillingTask.order_id == Order.id)
            .where(OrderOwner.user_id == user.id, Order.dataset_id == PUBLIC_DATASET)
            .order_by(Order.created_at.desc())
            .limit(50)
        )
    ).all()
    items = []
    for order, task in rows:
        snapshot = dict(order.snapshot)
        invoice = billing_view(task)
        items.append(
            {
                "id": str(order.id),
                "status": order.status,
                "created_at": snapshot.get("created_at"),
                "total_cents": order.total_cents,
                "units": sum(int(line.get("quantity", 0)) for line in snapshot.get("items", [])),
                "notification_status": order.notification_status,
                "billing_status": invoice["billing_status"],
                "invoice_number": invoice["invoice_number"],
            }
        )
    return {"items": items}
