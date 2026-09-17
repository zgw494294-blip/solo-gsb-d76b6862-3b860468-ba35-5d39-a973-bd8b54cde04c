"""Standalone initializer: create schema, bootstrap default pool, verify.

Invoked by the container entrypoint before Uvicorn starts; safe to re-run.
"""
from __future__ import annotations

import logging

from .config import load_settings
from .db import connect, init_db, write_tx
from .services import bootstrap_default_pool, reconcile_all_pools
from .timeutil import now_us


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    init_db(settings)
    conn = connect(settings.db_path, settings.busy_timeout_ms)
    try:
        if settings.init_default_pool:
            with write_tx(conn):
                bootstrap_default_pool(conn, "default", settings.default_pool_total, now_us())
        reports = reconcile_all_pools(conn, raise_on_mismatch=True)
        for r in reports:
            state = "OK" if r["balanced"] else "BROKEN"
            print(
                f"[init] pool={r['pool_id']} invariant={state} "
                f"total={r['stored']['total']} available={r['stored']['available']} "
                f"held={r['stored']['held']} used={r['stored']['used']} "
                f"ledger_entries={r['from_ledger']['entries']}"
            )
    finally:
        conn.close()
    print("[init] database ready at", settings.db_path)


if __name__ == "__main__":
    main()
