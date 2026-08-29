"""Run the fixed-principal HTTP adapter on loopback.

Install the optional server dependencies first:

    pip install "recall-origin[server]"
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from recall_origin.contracts.v1 import PartitionRef, PrincipalContext
from recall_origin.domain.enums import Capability, PrincipalType
from recall_origin.interfaces.http import create_app


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve RecallOrigin locally with exact, fixed scopes.",
    )
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="SQLite database path.",
    )
    parser.add_argument(
        "--scope",
        action="append",
        required=True,
        help="Allowed exact scope as '<kind>:<id>'; repeat for more than one.",
    )
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    scopes = tuple(PartitionRef.parse(value) for value in arguments.scope)
    principal = PrincipalContext(
        tenant_id="local",
        principal_id="local-http-operator",
        principal_type=PrincipalType.HUMAN,
        capabilities=frozenset(Capability),
    )
    app = create_app(
        db=arguments.db.expanduser().resolve(),
        principal=principal,
        allowed_partitions=scopes,
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=arguments.port,
        workers=1,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
