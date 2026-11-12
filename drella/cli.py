"""`drella` command line. Run inside Drella's pod (`kubectl exec`): it's the admin path.

    drella token create --name NAME [--github-profile P] [--role R]
                                          create a token, print it once
    drella token list                     tokens: id, GitHub profile, role, name
    drella token set-profile ID PROFILE   change a token's GitHub profile
    drella token set-role ID ROLE         change a token's role
    drella serve                          run the API (and its queue loop)

A token's GitHub profile decides what the GitHub App tokens minted for its jobs may do
(drella/github.py: PROFILES). New tokens are `reader` unless --github-profile says
otherwise. Its role decides what it may do in Drella itself (issue #74): a `member`
(the default) creates and reads its own jobs; an `operator`'s jobs also get a
job-scoped API token, with which they can queue and read their caller's jobs.
"""

import argparse
import logging
import sys

from sqlalchemy import select

from drella import crypto
from drella.config import Settings
from drella.db import make_engine, make_sessionmaker, migrate
from drella.github import DEFAULT_PROFILE, PROFILES
from drella.models import MEMBER, ROLES, Token


def _sessions(settings: Settings):
    engine = make_engine(settings.database_url)
    migrate(engine)
    return make_sessionmaker(engine)


def create_token(
    settings: Settings, name: str, github_profile: str = DEFAULT_PROFILE, role: str = MEMBER
) -> str:
    if github_profile not in PROFILES:
        raise ValueError(f"no GitHub profile '{github_profile}'")
    if role not in ROLES:
        raise ValueError(f"no role '{role}'")
    token = crypto.new_token()
    with _sessions(settings)() as s:
        s.add(
            Token(
                name=name,
                lookup_hash=crypto.lookup_hash(token),
                salt=crypto.new_salt(),
                github_profile=github_profile,
                role=role,
            )
        )
        s.commit()
    return token


def list_tokens(settings: Settings) -> list[tuple[int, str, str, str]]:
    with _sessions(settings)() as s:
        return [
            (t.id, t.name, t.github_profile, t.role)
            for t in s.scalars(select(Token).order_by(Token.id))
        ]


def set_profile(settings: Settings, token_id: int, github_profile: str) -> None:
    """Takes effect for the token's jobs that haven't launched yet, too: the profile is
    read when a job's GitHub token is minted."""
    if github_profile not in PROFILES:
        raise ValueError(f"no GitHub profile '{github_profile}'")
    with _sessions(settings)() as s:
        token = s.get(Token, token_id)
        if token is None:
            raise ValueError(f"no token with id {token_id}")
        token.github_profile = github_profile
        s.commit()


def set_role(settings: Settings, token_id: int, role: str) -> None:
    """Takes effect at once: an operator's jobs created from now on get an API token,
    and the API tokens its running jobs already have stop working if it's no longer an
    operator."""
    if role not in ROLES:
        raise ValueError(f"no role '{role}'")
    with _sessions(settings)() as s:
        token = s.get(Token, token_id)
        if token is None:
            raise ValueError(f"no token with id {token_id}")
        token.role = role
        s.commit()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="drella")
    sub = parser.add_subparsers(dest="command", required=True)
    token = sub.add_parser("token", help="manage tokens").add_subparsers(
        dest="action", required=True
    )
    profiles = ", ".join(f"{p.name} ({p.description})" for p in PROFILES.values())
    create = token.add_parser("create", help="create a token and print it once")
    create.add_argument("--name", required=True)
    create.add_argument(
        "--github-profile",
        choices=sorted(PROFILES),
        default=DEFAULT_PROFILE,
        help=f"what its jobs' GitHub App tokens may do (default {DEFAULT_PROFILE}): {profiles}",
    )
    create.add_argument(
        "--role",
        choices=ROLES,
        default=MEMBER,
        help=f"what it may do in Drella (default {MEMBER}): an operator's jobs can queue and"
        " read its jobs",
    )
    token.add_parser("list", help="list tokens (never the tokens themselves)")
    setp = token.add_parser("set-profile", help="change a token's GitHub profile")
    setp.add_argument("id", type=int, help="the token's id (drella token list)")
    setp.add_argument("profile", choices=sorted(PROFILES))
    setr = token.add_parser("set-role", help="change a token's role")
    setr.add_argument("id", type=int, help="the token's id (drella token list)")
    setr.add_argument("role", choices=ROLES)
    serve = sub.add_parser("serve", help="run the API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.command == "token" and args.action == "create":
        print(create_token(Settings(), args.name, args.github_profile, args.role))
        print(
            f"Store it now: it won't be shown again. GitHub profile: {args.github_profile}."
            f" Role: {args.role}.",
            file=sys.stderr,
        )
    elif args.command == "token" and args.action == "list":
        for tid, name, profile, role in list_tokens(Settings()):
            print(f"{tid}\t{profile}\t{role}\t{name}")
    elif args.command == "token" and args.action == "set-profile":
        try:
            set_profile(Settings(), args.id, args.profile)
        except ValueError as e:
            sys.exit(f"drella: {e}")
        print(f"token {args.id}: GitHub profile {args.profile}", file=sys.stderr)
    elif args.command == "token" and args.action == "set-role":
        try:
            set_role(Settings(), args.id, args.role)
        except ValueError as e:
            sys.exit(f"drella: {e}")
        print(f"token {args.id}: role {args.role}", file=sys.stderr)
    elif args.command == "serve":
        import uvicorn

        from drella.app import create_app

        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
