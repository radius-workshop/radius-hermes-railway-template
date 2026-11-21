#!/usr/bin/env python3
"""Read-only, secret-safe Railway recovery guide. Requires Python 3.11."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent / "egress"))
import iron_egress as egress  # noqa: E402


class InspectionError(RuntimeError):
    """A deliberately value-free CLI failure."""


def railway_json(arguments: list[str]) -> dict:
    """Capture secret-bearing output; never forward stdout, stderr or exceptions."""
    try:
        result = subprocess.run(
            ["railway", *arguments, "--json"], capture_output=True, text=True,
            timeout=45, check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise InspectionError("Railway inspection could not run; check CLI login and connectivity.") from None
    if result.returncode:
        raise InspectionError("Railway inspection failed; check account access and target IDs.") from None
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise InspectionError("Railway returned an unsupported response.") from None
    if not isinstance(data, dict):
        raise InspectionError("Railway returned an unsupported response.")
    return data


def variables(project: str, environment: str, service: str) -> dict[str, str | None]:
    data = railway_json([
        "variable", "list", "--project", project, "--environment", environment,
        "--service", service,
    ])
    if any(not isinstance(value, (str, type(None))) for value in data.values()):
        raise InspectionError("Railway variable format is unsupported; no values were displayed.")
    return data


def nodes(connection) -> list[dict]:
    if not isinstance(connection, dict):
        return []
    return [edge["node"] for edge in connection.get("edges", [])
            if isinstance(edge, dict) and isinstance(edge.get("node"), dict)]


def choose(items: list[dict], label: str, requested: str | None, interactive: bool) -> dict:
    if requested:
        matches = [item for item in items if requested in (item.get("id"), item.get("name"))]
        if len(matches) == 1:
            return matches[0]
        raise InspectionError(f"The selected {label} was not found in the inspected project.")
    if len(items) == 1:
        return items[0]
    if not items:
        raise InspectionError(f"No {label} is available in the inspected project.")
    if not interactive:
        raise InspectionError(f"Multiple choices exist; specify --{label} explicitly.")
    print(f"\nChoose {label}:")
    for index, item in enumerate(items, 1):
        # JSON quoting prevents terminal control sequences in resource names.
        print(f"  {index}. {json.dumps(item.get('name', 'Unnamed'))}")
    try:
        index = int(input("Number: ")) - 1
        if index < 0 or index >= len(items):
            raise ValueError
        return items[index]
    except (ValueError, EOFError, KeyboardInterrupt):
        raise InspectionError("No valid selection was made; rerun with explicit target arguments.") from None


def discover(args) -> bool:
    """Fill missing targets from project metadata; return whether egress is absent."""
    if all((args.project, args.environment, args.agent, args.egress)):
        return False
    command = ["status"]
    if args.project and args.environment:
        command += ["--project", args.project, "--environment", args.environment]
    data = railway_json(command)
    if args.project and args.project not in (data.get("id"), data.get("name")):
        raise InspectionError("Linked project differs; specify both --project and --environment.")
    if not isinstance(data.get("id"), str):
        raise InspectionError("Railway project metadata is incomplete.")
    args.project = data["id"]
    interactive = sys.stdin.isatty() and not args.json
    environment = choose(nodes(data.get("environments")), "environment", args.environment, interactive)
    args.environment = environment["id"]
    if "serviceInstances" in environment:
        services = [{"id": item.get("serviceId"), "name": item.get("serviceName")}
                    for item in nodes(environment["serviceInstances"])]
    else:
        services = nodes(data.get("services"))
    services = [item for item in services if isinstance(item.get("id"), str) and isinstance(item.get("name"), str)]
    proxy_choices = [item for item in services if "egress" in item["name"].lower()]
    if args.egress:
        proxy_choices = [item for item in services if args.egress in (item["id"], item["name"])]
    if proxy_choices:
        proxy = choose(proxy_choices, "egress", args.egress, interactive)
        args.egress = proxy["id"]
    else:
        args.egress = None
    agent_choices = [item for item in services if item["id"] != args.egress]
    args.agent = choose(agent_choices, "agent", args.agent, interactive)["id"]
    return args.egress is None


def diagnose(agent: dict[str, str | None], proxy: dict[str, str | None]) -> list[dict]:
    """Inspect values in memory and return only fixed diagnostic text.

    A present null is sealed, not missing. No credential validation is implied
    by presence, and custom names/host values are never reflected into output.
    """
    findings = []

    def add(stage: str, code: str, status: str, detail: str, action: str):
        findings.append(dict(stage=stage, code=code, status=status, detail=detail, action=action))

    def present(env, key):
        return key in env and (env[key] is None or bool(env[key].strip()))

    plain_agent = {key: value for key, value in agent.items() if isinstance(value, str)}
    plain_proxy = {key: value for key, value in proxy.items() if isinstance(value, str)}
    for name in ("EGRESS_SECRETS", "EGRESS_TOKEN_SEED", "EGRESS_CA_CERT"):
        if not present(agent, name) or not present(proxy, name):
            add("Proxy", "shared_missing", "blocked", f"{name} is missing on at least one service.",
                "Set the same shared setting on both services.")
        elif agent[name] is None or proxy[name] is None:
            add("Proxy", "shared_sealed", "unknown", f"{name} is sealed; equality cannot be inspected.",
                "Preserve sealed values; verify matching configuration through a runtime proxy check.")
        elif agent[name] != proxy[name]:
            add("Proxy", "shared_mismatch", "blocked", f"{name} differs between services.",
                "Reconcile the shared setting before restarting agent0.")
        else:
            add("Proxy", "shared_matches", "configured", f"{name} matches between services.",
                "Verify the deployed proxy path; matching variables alone are not runtime proof.")

    for env in (plain_agent, plain_proxy):
        seed = env.get("EGRESS_TOKEN_SEED")
        if seed and len(seed.strip()) < egress.MIN_SEED_LENGTH:
            add("Proxy", "seed_invalid", "blocked", "A visible token seed is too short.",
                "Generate a valid shared seed securely and coordinate both services.")
        cert = env.get("EGRESS_CA_CERT", "")
        if cert and ("BEGIN CERTIFICATE" not in cert or "PRIVATE KEY" in cert):
            add("Proxy", "certificate_invalid", "blocked", "A shared certificate has invalid contents.",
                "Use only the CA certificate in EGRESS_CA_CERT; retain the private key on egress.")

    if present(agent, "EGRESS_CA_KEY"):
        add("Isolation", "ca_key_on_agent", "blocked", "The CA private key is configured on the agent.",
            "Remove it from the agent after confirming the egress service holds the correct key.")
    if not present(proxy, "EGRESS_CA_KEY"):
        add("Proxy", "ca_key_missing", "blocked", "The egress CA private key is missing.",
            "Restore the CA private key on the egress service only.")
    elif proxy["EGRESS_CA_KEY"] is None:
        add("Proxy", "ca_key_sealed", "unknown", "The egress CA private key is sealed and already set.",
            "Preserve it; verify proxy startup instead of recreating the key.")
    if agent.get("EGRESS_ENFORCE") is None and "EGRESS_ENFORCE" in agent:
        add("Isolation", "enforcement_sealed", "unknown", "The enforcement setting is sealed.",
            "Verify enforcement is enabled at runtime.")
    elif not egress.is_true(plain_agent.get("EGRESS_ENFORCE"), default=True):
        add("Isolation", "enforcement_disabled", "blocked", "Secret isolation enforcement is disabled.",
            "Enable EGRESS_ENFORCE after fixing credential placement.")
    url = plain_agent.get("EGRESS_PROXY_URL", "")
    try:
        parsed = urlsplit(url)
        valid_url = parsed.scheme == "http" and bool(parsed.hostname) and bool(parsed.port)
    except ValueError:
        valid_url = False
    if not valid_url:
        sealed_url = "EGRESS_PROXY_URL" in agent and agent["EGRESS_PROXY_URL"] is None
        add("Proxy", "proxy_url_unverified", "unknown" if sealed_url else "blocked",
            "The agent proxy URL is missing, sealed or invalid.",
            "Set an HTTP URL using the egress private hostname and port 9090.")

    specs = []
    try:
        if agent.get("EGRESS_SECRETS") is not None:
            specs = egress.managed_specs(plain_agent)
    except (egress.EgressError, TypeError, ValueError, KeyError, AttributeError):
        add("Proxy", "managed_rules_invalid", "blocked", "The managed secret list or injection rules are invalid.",
            "Use supported secret names; local wallet keys cannot use HTTP credential injection.")
    proxy_specs = []
    try:
        if proxy.get("EGRESS_SECRETS") is not None:
            proxy_specs = egress.managed_specs(plain_proxy)
    except (egress.EgressError, TypeError, ValueError, KeyError, AttributeError):
        add("Proxy", "proxy_managed_rules_invalid", "blocked", "The egress service's managed secret list or injection rules are invalid.",
            "Fix the egress injection rules before configuring the agent to use them.")
    managed = {spec.name for spec in specs}
    if "OPENAI_API_KEY" in managed and plain_agent.get("OPENAI_BASE_URL"):
        try:
            host = urlsplit(plain_agent["OPENAI_BASE_URL"]).hostname
        except ValueError:
            host = None
        if host != "api.openai.com" and not present(agent, "EGRESS_HOSTS_OPENAI_API_KEY"):
            add("Proxy", "provider_host_override_missing", "blocked", "The custom OpenAI-compatible provider needs a secret host override.",
                "Set EGRESS_HOSTS_OPENAI_API_KEY to the provider hostname on both services.")
    # Inspect credentials needed by either side, including an egress service
    # staged before agent configuration. Prefer the proxy's own injection spec.
    credential_specs = {spec.name: spec for spec in [*specs, *proxy_specs]}
    for spec in credential_specs.values():
        label = spec.name if spec.name in egress.CATALOG else "A custom managed credential"
        if not present(proxy, spec.name):
            add("Credentials", "credential_missing", "blocked", f"{label} is missing on egress.",
                "Restore the rotated credential on egress only, using hidden input.")
        elif proxy[spec.name] is None:
            add("Credentials", "credential_sealed", "unknown", f"{label} is sealed and already set on egress.",
                "Preserve it and verify provider authentication through the deployed proxy.")
        else:
            seed = plain_proxy.get("EGRESS_TOKEN_SEED")
            is_token = seed and proxy[spec.name] == egress.proxy_token(seed.strip().encode(), spec)
            add("Credentials", "credential_is_token" if is_token else "credential_present",
                "blocked" if is_token else "configured",
                f"{label} holds a proxy token on egress." if is_token else f"{label} is set on egress; validity is unverified.",
                "Restore the real credential on egress." if is_token else "Verify authentication through the deployed proxy.")
    # Agent isolation depends only on the rules the agent actually holds.
    for spec in specs:
        label = spec.name if spec.name in egress.CATALOG else "A custom managed credential"
        if present(agent, spec.name):
            seed = plain_agent.get("EGRESS_TOKEN_SEED")
            value = agent[spec.name]
            if value is None or not seed:
                add("Isolation", "agent_credential_unknown", "unknown", f"{label} is set on the agent; its type cannot be verified.",
                    "The agent normally needs only EGRESS_SECRETS, not an explicit credential value.")
            elif value != egress.proxy_token(seed.strip().encode(), spec):
                add("Isolation", "real_credential_on_agent", "blocked", f"{label} on the agent is not the expected proxy token.",
                    "Confirm egress holds the credential, then remove the agent value.")
    for name in egress.CATALOG:
        if name not in managed and present(agent, name):
            add("Isolation", "unmanaged_credential", "blocked", f"{name} is configured on the agent outside verified managed rules.",
                "Add supported injection rules and move the real credential to egress.")
    override_names = {name for name in agent.keys() | proxy.keys() if name.startswith("EGRESS_HOSTS_")}
    for name in {"EGRESS_CUSTOM_SECRETS", *override_names}:
        if (name in agent and agent[name] is None) or (name in proxy and proxy[name] is None):
            add("Proxy", "rules_sealed", "unknown", "Custom injection rules or host overrides are sealed; equality cannot be verified.",
                "Preserve sealed settings and verify the configured secret hosts at runtime.")
        elif agent.get(name) != proxy.get(name):
            add("Proxy", "rules_mismatch", "blocked", "Custom injection rules or host overrides differ between services.",
                "Use identical injection rules and host overrides on both services.")
    add("Wallet", "remote_signer_unverified", "unknown", "Remote wallet signing is not verified by this guide; the template uses local signing.",
        "Preserve the existing wallet identity and complete a remote signer migration before claiming key isolation.")
    add("Runtime", "runtime_unverified", "unknown", "Provider requests, messaging delivery and deployed secret isolation have not been tested.",
        "Verify proxy connectivity, a bounded model request and the selected messaging channel after repair.")
    return findings


def render(report: dict, as_json: bool = False):
    if as_json:
        print(json.dumps(report, indent=2))
        return
    print("\nagent0 · recovery guide\n")
    if report.get("target"):
        for name, value in report["target"].items():
            print(f"{name.capitalize()}: {json.dumps(value) if value is not None else 'not deployed'}")
        print()
    print("Read-only diagnosis. No credentials changed and no services deployed.")
    print("Setup remains incomplete until runtime and signing checks pass.\n")
    if report.get("error"):
        print(report["error"])
        return
    for finding in report["findings"]:
        print(f"[{finding['status'].upper()}] {finding['stage']}: {finding['detail']}")
        print(f"  Next: {finding['action']}")
    print("\nStart with the first BLOCKED item, then rerun this guide.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "setup", "status"),
                        help="All modes inspect and guide only; setup does not modify Railway.")
    for name in ("project", "environment", "agent", "egress"):
        parser.add_argument(f"--{name}", help="Discover from the linked project when omitted")
    parser.add_argument("--json", action="store_true", help="Emit a value-free diagnostic report")
    args = parser.parse_args(argv)
    report = dict(schema_version=1, mode=args.command, read_only=True, setup_complete=False)
    try:
        absent_proxy = discover(args)
        if args.agent == args.egress:
            raise InspectionError("Agent and egress must be distinct services.")
        report["target"] = {
            "project": args.project, "environment": args.environment,
            "agent": args.agent, "egress": args.egress,
        }
        agent = variables(args.project, args.environment, args.agent)
        proxy = {}
        proxy_error = False
        if not absent_proxy:
            try:
                proxy = variables(args.project, args.environment, args.egress)
            except InspectionError:
                proxy_error = True
        report["findings"] = diagnose(agent, proxy)
        if proxy_error:
            # A failed read does not establish that remote settings are absent.
            report["findings"] = [item for item in report["findings"] if item["code"] not in {
                "shared_missing", "credential_missing", "ca_key_missing", "rules_mismatch",
            }]
        if absent_proxy or proxy_error:
            report["findings"].insert(0, dict(
                stage="Deployment", code="egress_not_deployed" if absent_proxy else "egress_unavailable",
                status="blocked" if absent_proxy else "unknown",
                detail="No egress service was found in the selected environment." if absent_proxy else "The egress service could not be inspected; absence is not confirmed.",
                action="Create a private egress service using the repository's Railway IaC configuration (deploy/railway/egress.ts); no public domain or volume." if absent_proxy else "Check the egress service target and account access, then rerun diagnosis.",
            ))
    except InspectionError as exc:
        report["error"] = str(exc)
        render(report, args.json)
        return 2
    render(report, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
