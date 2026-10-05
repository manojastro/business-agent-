"""Operational commands.

    python -m app.cli migrate                  # app + analytics migrations, checkpoint tables
    python -m app.cli seed-demo                # demo tenants, data, users (prints generated passwords once)
    python -m app.cli seed-evals --split development --split heldout
    python -m app.cli create-admin --email you@example.com --tenant acme-retail
    python -m app.cli run-evals --suite synthetic-heldout --systems fixed_dashboard single_pass full_graph
    python -m app.cli export-openapi docs/openapi.json
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

BACKEND = Path(__file__).resolve().parents[1]


def _alembic(name: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"), ini_section=name)
    cfg.set_main_option("script_location", str(BACKEND / "migrations" / name))
    return cfg


def cmd_migrate(_: argparse.Namespace) -> None:
    from app.agents.graph import setup_checkpointer

    command.upgrade(_alembic("app"), "head")
    command.upgrade(_alembic("analytics"), "head")
    setup_checkpointer()
    print("migrations applied (app, analytics, checkpoints)")


def cmd_seed_demo(args: argparse.Namespace) -> None:
    from app.config import get_settings
    from app.db.session import session_scope
    from app.seed.app_seed import seed_demo

    pw = get_settings().demo_user_password or None
    if pw and len(pw) < 12:
        sys.exit("DEMO_USER_PASSWORD must be at least 12 characters")
    with session_scope() as s:
        issued = seed_demo(s, pw, reset_passwords=args.reset_passwords, load_data=not args.skip_data)
    if issued:
        print("\nDemo credentials (shown once; store them safely):")
        for email, p in issued:
            print(f"  {email:24s} {p if not pw else '(DEMO_USER_PASSWORD)'}")
    else:
        print("demo users already exist; passwords unchanged (use --reset-passwords to rotate)")


def cmd_seed_evals(args: argparse.Namespace) -> None:
    from app.db.session import session_scope
    from app.seed.app_seed import seed_evals

    with session_scope() as s:
        seed_evals(s, args.split or ["development", "heldout"], load_data=not args.skip_data)


def cmd_create_admin(args: argparse.Namespace) -> None:
    from sqlalchemy import select

    from app.db.models import AuditEvent, Tenant
    from app.db.session import session_scope
    from app.seed.app_seed import ensure_membership, ensure_user

    pw = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("New admin password (min 12 chars): ")
    with session_scope() as s:
        tenant = s.scalar(select(Tenant).where(Tenant.slug == args.tenant))
        if tenant is None:
            sys.exit(f"unknown tenant {args.tenant}")
        user, _ = ensure_user(s, args.email, args.name or args.email, pw, rotate=True)
        ensure_membership(s, user, tenant, "admin")
        s.add(AuditEvent(tenant_id=tenant.id, action="create_admin", object_type="user", object_id=str(user.id),
                         detail={"email": args.email}))
    print(f"admin {args.email} ready for tenant {args.tenant}")


def cmd_run_evals(args: argparse.Namespace) -> None:
    from app.evals.runner import run_suite

    summary = run_suite(args.suite, args.systems, limit=args.limit, model_mode=args.model_mode)
    print(json.dumps(summary, indent=2))
    if args.report:
        from app.evals.report import write_markdown

        write_markdown(summary, Path(args.report))
        print(f"wrote {args.report}")


def cmd_export_openapi(args: argparse.Namespace) -> None:
    from app.main import create_app

    Path(args.path).write_text(json.dumps(create_app().openapi(), indent=2), encoding="utf-8")
    print(f"wrote {args.path}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="metric-investigator")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sd = sub.add_parser("seed-demo")
    sd.add_argument("--reset-passwords", action="store_true")
    sd.add_argument("--skip-data", action="store_true")
    sd.set_defaults(fn=cmd_seed_demo)
    se = sub.add_parser("seed-evals")
    se.add_argument("--split", action="append", choices=["development", "heldout"])
    se.add_argument("--skip-data", action="store_true")
    se.set_defaults(fn=cmd_seed_evals)
    ca = sub.add_parser("create-admin")
    ca.add_argument("--email", required=True)
    ca.add_argument("--tenant", required=True)
    ca.add_argument("--name")
    ca.set_defaults(fn=cmd_create_admin)
    re_ = sub.add_parser("run-evals")
    re_.add_argument("--suite", default="synthetic-heldout")
    re_.add_argument("--systems", nargs="+", default=["fixed_dashboard", "single_pass", "full_graph"])
    re_.add_argument("--limit", type=int)
    re_.add_argument("--model-mode", choices=["fixture", "real"])
    re_.add_argument("--report")
    re_.set_defaults(fn=cmd_run_evals)
    eo = sub.add_parser("export-openapi")
    eo.add_argument("path")
    eo.set_defaults(fn=cmd_export_openapi)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
