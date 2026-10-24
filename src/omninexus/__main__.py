"""Console entry point."""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omninexus", description="OmniNexus local LLM core")
    parser.add_argument("--host", default=None, help="Bind host (default: config/OMNI_HOST)")
    parser.add_argument(
        "--port", type=int, default=None, help="Bind port (default: config/OMNI_PORT)"
    )
    parser.add_argument("--config", default=None, help="Path to config YAML")
    parser.add_argument("--reload", action="store_true", help="Development auto-reload")
    args = parser.parse_args(argv)

    if args.config:
        os.environ["OMNI_CONFIG"] = args.config

    from omninexus.config import load_config

    try:
        config = load_config()
    except Exception as exc:
        print(f"OmniNexus configuration error: {exc}", file=sys.stderr)
        return 2

    host = args.host or config.server.host
    port = args.port or config.server.port
    print(f"OmniNexus {host}:{port}  (docs: http://{host}:{port}/docs)")
    print("Local-first: archives stay in the local data directory; keys stay in your environment.")

    import uvicorn

    if args.reload and not args.config:
        os.environ["OMNI_CONFIG"] = str(config.config_path)
    uvicorn.run(
        "omninexus.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        access_log=False,
        log_level=config.server.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
