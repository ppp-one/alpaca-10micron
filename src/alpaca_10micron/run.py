import argparse
import logging
import logging.handlers
import sys
from dataclasses import replace

import uvicorn

from alpaca_10micron.config import load_config
from alpaca_10micron.main import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ASCOM Alpaca driver for 10Micron mounts.")
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Configuration file (default: config.yaml). See config.example.yaml.",
    )
    parser.add_argument("--host", type=str, help="Override server.host from the configuration.")
    parser.add_argument("--port", type=int, help="Override server.port from the configuration.")
    parser.add_argument("--log-file", type=str, help="Also write the log to this file.")
    parser.add_argument(
        "--log-level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args()


def setup_logging(level: str, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )


def main():
    args = parse_args()
    setup_logging(args.log_level, args.log_file)
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        sys.exit(
            f"Configuration file {args.config!r} not found. "
            "Copy config.example.yaml to config.yaml and set mount.host."
        )
    except ValueError as exc:
        sys.exit(f"Invalid configuration in {args.config!r}: {exc}")

    server = config.server
    if args.host or args.port:
        server = replace(server, host=args.host or server.host, port=args.port or server.port)
        config = replace(config, server=server)

    logging.info(
        "Starting the 10Micron Alpaca driver on %s:%d for the mount at %s:%d",
        server.host,
        server.port,
        config.mount.host,
        config.mount.port,
    )
    # One process only: the driver owns the single connection to the mount. Passing the
    # app object (not an import string) also makes uvicorn refuse --workers and --reload.
    uvicorn.run(
        create_app(config),
        host=server.host,
        port=server.port,
        # Must exceed the slowest client poll interval, or clients see connection resets.
        timeout_keep_alive=300,
        log_config=None,
    )


if __name__ == "__main__":
    main()
