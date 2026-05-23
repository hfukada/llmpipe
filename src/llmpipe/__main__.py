from __future__ import annotations

import argparse
import logging

import uvicorn

from .config import load
from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="llmpipe")
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="info")
    ns = parser.parse_args()

    logging.basicConfig(
        level=ns.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load(ns.config)
    host = ns.host or config.host
    port = ns.port or config.port
    app = create_app(config)
    uvicorn.run(app, host=host, port=port, log_level=ns.log_level)


if __name__ == "__main__":
    main()
