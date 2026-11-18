"""Adaptador S09 -> microservicio de facturación S11 (Supabase Edge Function).

Flujo:
  1. El pedido se confirma y persiste en S09 (orders.py) junto con una fila local
     billing_tasks(status="pending"). No hay llamada remota dentro de esa transacción.
  2. El worker toma tareas pendientes y llama POST {BILLING_BASE_URL}/invoices con el
     snapshot persistido del pedido, X-Billing-Token privado e Idempotency-Key = order.id.
  3. 201/200 con el mismo order_id -> issued. Error definitivo o fallos transitorios
     agotados -> failed. El pedido sigue confirmado en ambos casos.
  4. Reintento manual: `python -m app.billing retry <order_id>` (o `retry-failed`) vuelve
     la tarea a pending; como la clave y el cuerpo son los mismos, el servicio devuelve la
     misma factura (200) y no la duplica.

Nunca se registran el token ni el cuerpo completo.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .db import PUBLIC_DATASET, utcnow
from .models import BillingTask, Order

logger = logging.getLogger(__name__)

MAX_AUTOMATIC_RETRIES = 2  # por ejecución, solo para fallos transitorios
RETRY_BACKOFF_SECONDS = (1.0, 3.0)
BATCH_SIZE = 10


@dataclass(frozen=True)
class BillingOutcome:
    status: str  # "issued" | "failed" | "transient"
    invoice_id: uuid.UUID | None = None
    invoice_number: str | None = None
    error: str | None = None


def build_invoice_request(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Contrato S11 exacto, construido solo con el snapshot persistido (nunca del navegador)."""
    return {
        "order_id": str(uuid.UUID(str(snapshot["id"]))),  # UUID en minúsculas
        "currency": "PEN",
        "items": [
            {
                "product_id": item["product_id"],
                "name": item["name"],
                "quantity": int(item["quantity"]),
                "unit_price_cents": int(item["unit_price_cents"]),
            }
            for item in snapshot["items"]
        ],
        "total_cents": int(snapshot["total_cents"]),
    }


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
        code = body["error"]["code"]
        if isinstance(code, str) and code.isascii() and len(code) <= 40:
            return code
    except Exception:
        pass
    return "UNEXPECTED_RESPONSE"


def _interpret(response: httpx.Response, request_body: dict[str, Any]) -> BillingOutcome:
    status = response.status_code
    if status in (200, 201):
        try:
            body = response.json()
            invoice_id = uuid.UUID(str(body["id"]))
            same_order = body.get("order_id") == request_body["order_id"]
            same_total = body.get("total_cents") == request_body["total_cents"]
            issued = body.get("status") == "issued"
            number = body.get("number")
        except Exception:
            return BillingOutcome("failed", error=f"HTTP {status} INVALID_RESPONSE")
        if not (same_order and same_total and issued and isinstance(number, str)):
            return BillingOutcome("failed", error=f"HTTP {status} RESPONSE_MISMATCH")
        return BillingOutcome("issued", invoice_id=invoice_id, invoice_number=number[:80])
    code = _error_code(response)
    if status == 429 or status >= 500:
        return BillingOutcome("transient", error=f"HTTP {status} {code}")
    # 400/401/404/405/409/413/415/422: reintentar igual no lo arreglará.
    return BillingOutcome("failed", error=f"HTTP {status} {code}")


async def request_invoice_once(
    client: httpx.AsyncClient,
    settings: Settings,
    body: dict[str, Any],
) -> BillingOutcome:
    try:
        response = await client.post(
            f"{settings.billing_base_url}/invoices",
            json=body,
            headers={
                "X-Billing-Token": settings.billing_token.get_secret_value().strip(),
                "Idempotency-Key": body["order_id"],
            },
        )
    except httpx.TimeoutException:
        # Un timeout NO demuestra que la factura no se guardó: el reintento con la
        # misma clave y cuerpo recupera la misma factura.
        return BillingOutcome("transient", error="TIMEOUT")
    except httpx.HTTPError as exc:
        return BillingOutcome("transient", error=f"NETWORK_ERROR {type(exc).__name__}")
    return _interpret(response, body)


async def request_invoice(
    client: httpx.AsyncClient,
    settings: Settings,
    body: dict[str, Any],
) -> tuple[BillingOutcome, int]:
    """Intento + hasta 2 reintentos automáticos para fallos transitorios."""
    attempts = 0
    outcome = BillingOutcome("transient", error="NOT_ATTEMPTED")
    for retry in range(MAX_AUTOMATIC_RETRIES + 1):
        attempts += 1
        outcome = await request_invoice_once(client, settings, body)
        if outcome.status != "transient":
            return outcome, attempts
        if retry < MAX_AUTOMATIC_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS[retry])
    return BillingOutcome("failed", error=f"{outcome.error} (reintentos agotados)"), attempts


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.billing_timeout_seconds),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        follow_redirects=False,
    )


async def _claim_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[uuid.UUID]:
    async with session_factory() as session:
        async with session.begin():
            ids = (
                await session.execute(
                    select(BillingTask.order_id)
                    .where(BillingTask.status == "pending")
                    .order_by(BillingTask.created_at)
                    .limit(BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            if ids:
                await session.execute(
                    update(BillingTask)
                    .where(BillingTask.order_id.in_(ids))
                    .values(status="processing", updated_at=utcnow())
                )
            return list(ids)


async def _finish(
    session_factory: async_sessionmaker[AsyncSession],
    order_id: uuid.UUID,
    outcome: BillingOutcome,
    attempts: int,
) -> None:
    values: dict[str, Any] = {
        "status": outcome.status,
        "attempts": BillingTask.attempts + attempts,
        "last_error": outcome.error,
        "updated_at": utcnow(),
    }
    if outcome.status == "issued":
        values["invoice_id"] = outcome.invoice_id
        values["invoice_number"] = outcome.invoice_number
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(BillingTask).where(BillingTask.order_id == order_id).values(**values)
            )


async def process_billing_batch(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: httpx.AsyncClient,
) -> int:
    if not settings.billing_configured():
        return 0
    claimed = await _claim_pending(session_factory)
    for order_id in claimed:
        async with session_factory() as session:
            order = await session.get(Order, order_id)
        if order is None:
            continue
        body = build_invoice_request(dict(order.snapshot))
        outcome, attempts = await request_invoice(client, settings, body)
        await _finish(session_factory, order_id, outcome, attempts)
        logger.info(
            "billing order=%s status=%s attempts=%s error=%s",
            order_id,
            outcome.status,
            attempts,
            outcome.error or "-",
        )
    return len(claimed)


async def release_stuck_tasks(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Si el worker se cayó a mitad de una llamada, la tarea vuelve a pending (es idempotente)."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(BillingTask)
                .where(BillingTask.status == "processing")
                .values(status="pending", updated_at=utcnow())
            )


async def schedule_retry(
    session_factory: async_sessionmaker[AsyncSession],
    order_id: uuid.UUID | None,
) -> int:
    """Reintento manual: failed -> pending (o crea la tarea si el pedido es anterior a S11)."""
    async with session_factory() as session:
        async with session.begin():
            if order_id is None:
                result = await session.execute(
                    update(BillingTask)
                    .where(BillingTask.status == "failed")
                    .values(status="pending", updated_at=utcnow())
                    .returning(BillingTask.order_id)
                )
                return len(result.all())
            order = await session.get(Order, order_id)
            if order is None or order.dataset_id != PUBLIC_DATASET:
                return 0
            now = utcnow()
            result = await session.execute(
                pg_insert(BillingTask)
                .values(
                    order_id=order_id,
                    dataset_id=order.dataset_id,
                    status="pending",
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["order_id"],
                    set_={"status": "pending", "updated_at": now},
                    where=BillingTask.status.in_(["failed", "pending"]),
                )
                .returning(BillingTask.order_id)
            )
            return 1 if result.first() is not None else 0


def billing_view(task: BillingTask | None) -> dict[str, Any]:
    if task is None:
        return {"billing_status": "not_requested", "invoice_id": None, "invoice_number": None}
    status = "pending" if task.status == "processing" else task.status
    return {
        "billing_status": status,
        "invoice_id": str(task.invoice_id) if task.invoice_id else None,
        "invoice_number": task.invoice_number,
        "attempts": task.attempts,
        "last_error": task.last_error if status == "failed" else None,
    }


async def _cli(argv: list[str] | None = None) -> None:
    from .config import get_settings
    from .db import Database

    parser = argparse.ArgumentParser(prog="python -m app.billing")
    sub = parser.add_subparsers(dest="command", required=True)
    retry = sub.add_parser("retry", help="Reintenta facturar un pedido (mismo cuerpo y clave)")
    retry.add_argument("order_id")
    sub.add_parser("retry-failed", help="Vuelve a pending todas las tareas failed")
    status = sub.add_parser("status", help="Muestra el estado de facturación de un pedido")
    status.add_argument("order_id")
    args = parser.parse_args(argv)

    database = Database(get_settings())
    try:
        if args.command == "retry-failed":
            count = await schedule_retry(database.session_factory, None)
            print(f"{count} tarea(s) vuelven a pending; el worker las procesará.")
        elif args.command == "retry":
            count = await schedule_retry(database.session_factory, uuid.UUID(args.order_id))
            print("Tarea en pending; el worker la procesará." if count else
                  "Nada que reintentar (pedido inexistente, de prueba o ya facturado).")
        else:
            async with database.session_factory() as session:
                task = await session.get(BillingTask, uuid.UUID(args.order_id))
            print(billing_view(task))
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(_cli())
