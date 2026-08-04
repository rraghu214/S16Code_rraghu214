from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="s16code")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.getenv("S16_PORT", "8113")))
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        uvicorn.run("s16code.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
