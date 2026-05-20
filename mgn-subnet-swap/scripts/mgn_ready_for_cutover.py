#!/usr/bin/env python3
"""
Review AWS MGN source servers and optionally move READY_FOR_TEST servers to READY_FOR_CUTOVER.

Default behavior is dry-run. Pass --execute to call ChangeServerLifeCycleState.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from typing import Any

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - dependency is validated at runtime
    boto3 = None
    BotoCoreError = ClientError = Exception


READY_FOR_TEST = "READY_FOR_TEST"
READY_FOR_CUTOVER = "READY_FOR_CUTOVER"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect AWS MGN source servers and optionally change any server in "
            "READY_FOR_TEST lifecycle state to READY_FOR_CUTOVER."
        )
    )
    parser.add_argument(
        "--region", help="AWS region. Uses normal boto3 resolution if omitted."
    )
    parser.add_argument(
        "--profile", help="AWS profile. Uses normal boto3 resolution if omitted."
    )
    parser.add_argument(
        "--account-id",
        help="MGN account ID to pass to ChangeServerLifeCycleState. Usually omitted.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform writes. Without this flag, the script only reports intended changes.",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived MGN source servers. Archived servers are skipped by default.",
    )
    parser.add_argument(
        "--application-id",
        action="append",
        default=[],
        help="Only include source servers in this MGN application ID. Repeatable.",
    )
    parser.add_argument(
        "--source-server-id-regex",
        help="Only include MGN source servers whose sourceServerID matches this regex.",
    )
    parser.add_argument(
        "--server-name-regex",
        help=(
            "Only include source servers whose hostname, FQDN, AWS instance ID, "
            "or Name tag matches this regex."
        ),
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Only include source servers with this exact tag. Repeatable.",
    )
    parser.add_argument(
        "--output",
        choices=["json", "text"],
        default="text",
        help="Output format. Default: text.",
    )
    return parser.parse_args()


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def compile_regex(pattern: str | None, label: str) -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise SystemExit(f"Invalid {label} regex: {exc}") from exc


def parse_tag_filters(tag_args: list[str]) -> dict[str, str]:
    tags: dict[str, str] = {}
    for item in tag_args:
        if "=" not in item:
            raise SystemExit(f"Invalid --tag value {item!r}; expected KEY=VALUE")
        key, value = item.split("=", 1)
        if not key:
            raise SystemExit(f"Invalid --tag value {item!r}; tag key cannot be empty")
        tags[key] = value
    return tags


def make_session(args: argparse.Namespace):
    if boto3 is None:
        raise SystemExit("boto3 is required. Install with: pip install boto3")
    kwargs: dict[str, str] = {}
    if args.profile:
        kwargs["profile_name"] = args.profile
    if args.region:
        kwargs["region_name"] = args.region
    return boto3.Session(**kwargs)


def describe_all_source_servers(mgn_client) -> list[dict[str, Any]]:
    servers: list[dict[str, Any]] = []
    request: dict[str, Any] = {}
    while True:
        response = mgn_client.describe_source_servers(**request)
        servers.extend(response.get("items", []))
        next_token = response.get("nextToken")
        if not next_token:
            return servers
        request["nextToken"] = next_token


def source_server_names(server: dict[str, Any]) -> list[str]:
    names: list[str] = []
    tags = server.get("tags") or {}
    if tags.get("Name"):
        names.append(str(tags["Name"]))

    hints = (server.get("sourceProperties") or {}).get("identificationHints") or {}
    for key in ("hostname", "fqdn", "awsInstanceID"):
        value = hints.get(key)
        if value:
            names.append(str(value))
    return names


def source_server_lifecycle_state(server: dict[str, Any]) -> str | None:
    lifecycle = server.get("lifeCycle") or {}
    state = lifecycle.get("state")
    return str(state) if state else None


def source_server_matches(
    server: dict[str, Any],
    args: argparse.Namespace,
    tag_filters: dict[str, str],
    source_id_re: re.Pattern[str] | None,
    name_re: re.Pattern[str] | None,
) -> tuple[bool, str | None]:
    source_server_id = server.get("sourceServerID", "")
    if server.get("isArchived") and not args.include_archived:
        return False, "archived"

    if args.application_id:
        application_id = server.get("applicationID")
        if application_id not in args.application_id:
            return False, "application-filter"

    if source_id_re and not source_id_re.search(str(source_server_id)):
        return False, "source-server-id-filter"

    if name_re:
        names = source_server_names(server)
        if not any(name_re.search(name) for name in names):
            return False, "server-name-filter"

    server_tags = server.get("tags") or {}
    for key, value in tag_filters.items():
        if server_tags.get(key) != value:
            return False, f"tag-filter:{key}"

    return True, None


def result_base(server: dict[str, Any]) -> dict[str, Any]:
    return {
        "sourceServerID": server.get("sourceServerID"),
        "names": source_server_names(server),
        "applicationID": server.get("applicationID"),
        "archived": bool(server.get("isArchived")),
        "lifeCycleState": source_server_lifecycle_state(server),
    }


def change_to_ready_for_cutover(
    mgn_client, server: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    result = result_base(server)
    source_server_id = server["sourceServerID"]

    if result["lifeCycleState"] != READY_FOR_TEST:
        result["status"] = "skipped"
        result["reason"] = "not-ready-for-test"
        return result

    if not args.execute:
        result["status"] = "would-update"
        result["newLifeCycleState"] = READY_FOR_CUTOVER
        return result

    request: dict[str, Any] = {
        "sourceServerID": source_server_id,
        "lifeCycle": {"state": READY_FOR_CUTOVER},
    }
    if args.account_id:
        request["accountID"] = args.account_id

    response = mgn_client.change_server_life_cycle_state(**request)
    result["status"] = "updated"
    result["newLifeCycleState"] = (
        source_server_lifecycle_state(response) or READY_FOR_CUTOVER
    )
    return result


def print_text_report(results: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1

    print("Summary:")
    for status in sorted(counts):
        print(f"  {status}: {counts[status]}")
    print()

    for result in results:
        source_id = result.get("sourceServerID") or "-"
        names = ", ".join(result.get("names") or []) or "-"
        state = result.get("lifeCycleState") or "-"
        print(f"{result['status']}: {source_id} names={names} lifecycle={state}")
        if result.get("reason"):
            print(f"  reason: {result['reason']}")
        if result.get("newLifeCycleState"):
            print(f"  new lifecycle: {result['newLifeCycleState']}")


def main() -> int:
    args = parse_args()

    tag_filters = parse_tag_filters(args.tag)
    source_id_re = compile_regex(
        args.source_server_id_regex, "--source-server-id-regex"
    )
    name_re = compile_regex(args.server_name_regex, "--server-name-regex")

    try:
        session = make_session(args)
        mgn_client = session.client("mgn")

        results: list[dict[str, Any]] = []
        for server in describe_all_source_servers(mgn_client):
            matches, reason = source_server_matches(
                server=server,
                args=args,
                tag_filters=tag_filters,
                source_id_re=source_id_re,
                name_re=name_re,
            )
            if not matches:
                result = result_base(server)
                result["status"] = "skipped"
                result["reason"] = reason
                results.append(result)
                continue

            try:
                results.append(change_to_ready_for_cutover(mgn_client, server, args))
            except (BotoCoreError, ClientError, RuntimeError) as exc:
                result = result_base(server)
                result["status"] = "error"
                result["reason"] = str(exc)
                results.append(result)

        if args.output == "json":
            print(json.dumps(results, indent=2, default=json_default))
        else:
            print_text_report(results)

    except (BotoCoreError, ClientError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
