"""FastAPI application and routes."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.responses import JSONResponse

from . import services
from .config import Settings, load_settings
from .db import get_conn, init_for_startup
from .errors import ApiError
from .schemas import CreatePoolRequest, CreateReservationRequest, SettleReservationRequest
from .timeutil import now_us

settings: Settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema creation + default pool + invariant check run inside the
    # container start, before Uvicorn accepts traffic.
    init_for_startup(settings)
    yield


app = FastAPI(
    title="Local Compute Quota Reservation API",
    version="1.0.0",
    description="TTL quota reservations with idempotency, settlement, "
    "immutable ledger and automatic expiry recycling.",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_body())


def get_db() -> Any:
    # Yield-based dependency so FastAPI owns the generator lifecycle and
    # closes the connection after the response is sent.
    yield from get_conn(settings)


DbConn = Annotated[sqlite3.Connection, Depends(get_db)]
PoolId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)]


def _require_idem(key: str | None) -> str:
    if not key:
        raise ApiError(
            422,
            "missing_idempotency_key",
            "Idempotency-Key header is required for this operation",
        )
    return key


def _json(status_code: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=body)


# ---------------------------------------------------------------------------
# Health / meta
# ---------------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {"service": "quota-reservation", "docs": "/docs", "health": "/health"}


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

@app.api_route("/pools/{pool_id}", methods=["PUT", "POST"], tags=["pools"], status_code=201)
def create_pool(
    conn: DbConn,
    pool_id: PoolId,
    body: CreatePoolRequest,
    idem_key: IdempotencyHeader = None,
):
    code, payload = services.create_pool(
        conn,
        pool_id=pool_id,
        total=body.total,
        idem_key=idem_key,
        request_hash=services.canonical_hash({"total": body.total}),
        ts_us=now_us(),
    )
    return _json(code, payload)


@app.get("/pools/{pool_id}/status", tags=["pools"])
def pool_status(conn: DbConn, pool_id: PoolId) -> dict[str, Any]:
    return services.pool_status(conn, pool_id, now_us())


@app.get("/pools/{pool_id}/verify", tags=["pools"])
def verify_pool(conn: DbConn, pool_id: PoolId) -> dict[str, Any]:
    """Recompute balances from the immutable ledger and report the invariant."""
    return services.reconcile_pool(conn, pool_id)


@app.get("/pools/{pool_id}/ledger", tags=["pools"])
def list_ledger(
    conn: DbConn,
    pool_id: PoolId,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    return services.list_ledger(conn, pool_id, now_us(), after_seq=after_seq, limit=limit)


@app.get("/pools/{pool_id}/reservations", tags=["reservations"])
def list_reservations(
    conn: DbConn,
    pool_id: PoolId,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    if status_filter and status_filter not in {"held", "settled", "released", "expired"}:
        raise ApiError(422, "invalid_status",
                       "status must be one of held, settled, released, expired")
    return services.list_reservations(conn, pool_id, now_us(),
                                      status=status_filter, limit=limit)


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

@app.post("/pools/{pool_id}/reservations", tags=["reservations"], status_code=201)
def create_reservation(
    conn: DbConn,
    pool_id: PoolId,
    body: CreateReservationRequest,
    idem_key: IdempotencyHeader = None,
):
    key = _require_idem(idem_key)
    ttl = body.ttl_seconds if body.ttl_seconds is not None else settings.default_ttl_seconds
    if ttl > settings.max_ttl_seconds:
        raise ApiError(
            422,
            "ttl_too_large",
            f"ttl_seconds must be <= {settings.max_ttl_seconds}",
            max_ttl_seconds=settings.max_ttl_seconds,
        )
    code, payload = services.create_reservation(
        conn,
        pool_id=pool_id,
        amount=body.amount,
        ttl_seconds=ttl,
        idem_key=key,
        ts_us=now_us(),
    )
    return _json(code, payload)


def _get_or_404(conn: sqlite3.Connection, pool_id: str, reservation_id: str) -> dict[str, Any]:
    result = services.get_reservation(conn, pool_id, reservation_id, now_us())
    if result is None:
        raise ApiError(404, "reservation_not_found", "reservation not found",
                       reservation_id=reservation_id)
    return result


@app.get("/pools/{pool_id}/reservations/{reservation_id}", tags=["reservations"])
def get_reservation(
    conn: DbConn,
    pool_id: PoolId,
    reservation_id: Annotated[str, Path(min_length=8, max_length=64)],
) -> dict[str, Any]:
    return _get_or_404(conn, pool_id, reservation_id)


@app.post("/pools/{pool_id}/reservations/{reservation_id}/settle", tags=["reservations"])
def settle_reservation(
    conn: DbConn,
    pool_id: PoolId,
    reservation_id: Annotated[str, Path(min_length=8, max_length=64)],
    body: SettleReservationRequest,
    idem_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    key = _require_idem(idem_key)
    return services.settle_reservation(
        conn,
        pool_id=pool_id,
        reservation_id=reservation_id,
        used_amount=body.used_amount,
        idem_key=key,
        ts_us=now_us(),
    )


@app.post("/pools/{pool_id}/reservations/{reservation_id}/release", tags=["reservations"])
def release_reservation(
    conn: DbConn,
    pool_id: PoolId,
    reservation_id: Annotated[str, Path(min_length=8, max_length=64)],
    idem_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    key = _require_idem(idem_key)
    return services.release_reservation(
        conn,
        pool_id=pool_id,
        reservation_id=reservation_id,
        idem_key=key,
        ts_us=now_us(),
    )
