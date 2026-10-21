"""Command line entry point: ``python -m shorturl``."""
from __future__ import annotations

import argparse
from typing import Optional, Sequence

from .server import create_server


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, serve until interrupted, and return the exit status."""
    parser = argparse.ArgumentParser(
        prog="shorturl", description="Run the URL shortener HTTP service."
    )
    parser.add_argument("--db", default="shorturl.db", help="SQLite file (default: %(default)s)")
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8080, help="port to bind (default: %(default)s)")
    parser.add_argument(
        "--base-url",
        help="public address used in short_url and the self-loop check "
        "(default: the bound address)",
    )
    args = parser.parse_args(argv)

    server = create_server(args.db, args.host, args.port, args.base_url)
    print(f"Serving {server.base_url} (database: {args.db}). Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
