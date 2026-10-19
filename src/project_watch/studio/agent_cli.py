"""CLI for the running Studio instance, with credentials only in environment."""

import argparse
import json

from project_watch.studio.agent_bridge import AgentClientError, client_from_env


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read/maintain one local ProjectWatch Studio project")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("runtime")
    sub.add_parser("project")
    search = sub.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    for action in ("pause", "resume"):
        change = sub.add_parser(action)
        change.add_argument("--version", type=int, required=True)
        change.add_argument("--idempotency-key", required=True)
    receipt = sub.add_parser("receipt")
    receipt.add_argument("operation_id")
    upload = sub.add_parser("submit-file")
    upload.add_argument("path")
    upload.add_argument("--version", type=int, required=True)
    upload.add_argument("--idempotency-key", required=True)
    source = sub.add_parser("source-status")
    source.add_argument("source_id")
    args = parser.parse_args(argv)
    try:
        client = client_from_env()
        if args.command == "runtime":
            result = client.runtime()
        elif args.command == "project":
            result = client.project()
        elif args.command == "search":
            result = client.search(args.query)
        elif args.command == "receipt":
            result = client.receipt(args.operation_id)
        elif args.command == "submit-file":
            result = client.submit_file(args.path, args.version, args.idempotency_key)
        elif args.command == "source-status":
            result = client.source_status(args.source_id)
        else:
            result = client.change_status("paused" if args.command == "pause" else "active",
                                          args.version, args.idempotency_key)
    except (AgentClientError, ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
