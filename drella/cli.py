"""`drella` command line.

    drella token create --name NAME   create a token and print it once
    drella serve                      run the API (and its queue loop)
"""

import argparse
import logging
import sys

from drella import crypto
from drella.config import Settings
from drella.db import make_engine, make_sessionmaker, migrate
from drella.models import Token


def create_token(settings: Settings, name: str) -> str:
    engine = make_engine(settings.database_url)
    migrate(engine)
    token = crypto.new_token()
    with make_sessionmaker(engine)() as s:
        s.add(Token(name=name, lookup_hash=crypto.lookup_hash(token), salt=crypto.new_salt()))
        s.commit()
    return token


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="drella")
    sub = parser.add_subparsers(dest="command", required=True)
    token = sub.add_parser("token", help="manage tokens").add_subparsers(
        dest="action", required=True
    )
    create = token.add_parser("create", help="create a token and print it once")
    create.add_argument("--name", required=True)
    serve = sub.add_parser("serve", help="run the API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.command == "token":
        print(create_token(Settings(), args.name))
        print("Store it now: it won't be shown again.", file=sys.stderr)
    elif args.command == "serve":
        import uvicorn

        from drella.app import create_app

        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
