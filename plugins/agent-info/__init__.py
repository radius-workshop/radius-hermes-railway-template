import json
import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def _get_base_url() -> str:
    if os.environ.get("PUBLIC_URL"):
        return os.environ["PUBLIC_URL"].rstrip("/")
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        return f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}".rstrip("/")
    return f"http://localhost:{os.environ.get('PORT', '3000')}"


def _did_web_to_base_url(did: str) -> str | None:
    if not isinstance(did, str) or not did.startswith("did:web:"):
        return None
    did_path = did.split("#", 1)[0][8:]
    if not did_path:
        return None
    parts = did_path.split(":")
    host = parts[0].replace("%3A", ":")
    if len(parts) == 1:
        return f"https://{host}"
    return f"https://{host}/{'/'.join(parts[1:])}"


def _normalize_target(agent: str | None) -> str:
    if not agent or not str(agent).strip():
        return _get_base_url()

    value = str(agent).strip().rstrip("/")
    if value.startswith("did:web:"):
        base_url = _did_web_to_base_url(value)
        if not base_url:
            raise ValueError(f"Unsupported DID target: {value}")
        return base_url

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        for suffix in (
            "/.well-known/agent-card.json",
            "/.well-known/agent-registration.json",
            "/.well-known/agent-skills/index.json",
            "/.well-known/did.json",
        ):
            if path.endswith(suffix):
                trimmed = path[: -len(suffix)].rstrip("/")
                return f"{base}{trimmed}" if trimmed else base
        return value

    raise ValueError(
        "Invalid agent target. Expected a base URL, did:web DID, or a discovery document URL."
    )


def _fetch_json(url: str):
    req = Request(
        url,
        headers={
            "Accept": "application/json, application/did+json, text/plain;q=0.8, */*;q=0.5",
            "User-Agent": "hermes-agent-info-plugin/1.0",
        },
    )
    with urlopen(req, timeout=10) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read().decode(charset)
    return json.loads(body)


def _fetch_text(url: str) -> str:
    req = Request(
        url,
        headers={
            "Accept": "text/markdown, text/plain;q=0.9, */*;q=0.5",
            "User-Agent": "hermes-agent-info-plugin/1.0",
        },
    )
    with urlopen(req, timeout=10) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def _get_agent_info(agent: str | None, include_skill_docs: bool) -> dict:
    base_url = _normalize_target(agent)
    agent_card = _fetch_json(f"{base_url}/.well-known/agent-card.json")
    skills_index = _fetch_json(f"{base_url}/.well-known/agent-skills/index.json")
    agent_registration = _fetch_json(f"{base_url}/.well-known/agent-registration.json")
    did_document = _fetch_json(f"{base_url}/.well-known/did.json")
    wallet_address = (
        agent_registration.get("wallet")
        or agent_registration.get("owner")
        or agent_card.get("provider", {}).get("wallet")
    )
    registration_did = None
    for service in agent_registration.get("services", []):
        if isinstance(service, dict) and service.get("name") == "DID":
            registration_did = service.get("endpoint")
            break

    response = {
        "agent": agent or "self",
        "base_url": base_url,
        "did": did_document.get("id")
        or agent_card.get("provider", {}).get("did")
        or registration_did,
        "wallet_address": wallet_address,
        "agent_card": agent_card,
        "skills": skills_index,
        "erc_8004_identity": agent_registration,
        "did_document": did_document,
    }

    if include_skill_docs:
        skill_docs = []
        for skill in skills_index.get("skills", []):
            skill_url = skill.get("url")
            if not skill_url:
                continue
            try:
                skill_docs.append(
                    {
                        "name": skill.get("name"),
                        "url": skill_url,
                        "content": _fetch_text(skill_url),
                    }
                )
            except Exception as err:
                skill_docs.append(
                    {
                        "name": skill.get("name"),
                        "url": skill_url,
                        "error": str(err),
                    }
                )
        response["skill_documents"] = skill_docs

    return response


def register(ctx):
    def get_agent_info(params, **kwargs):
        params = params or {}
        agent = params.get("agent")
        include_skill_docs = params.get("include_skill_docs", True)
        try:
            return json.dumps(
                _get_agent_info(agent, bool(include_skill_docs)),
                indent=2,
            )
        except Exception as err:
            return f"Error retrieving agent info: {err}"

    ctx.register_tool(
        name="get_agent_info",
        toolset="agent-info",
        schema={
            "name": "get_agent_info",
            "description": (
                "Fetch an agent's public discovery metadata in one call, including its A2A "
                "agent card, published skills, ERC-8004 registration, DID document, and "
                "wallet address when publicly advertised. If no agent is provided, the current agent is used."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": (
                            "Optional target agent. Accepts a base URL, a did:web DID, or one "
                            "of the agent's discovery document URLs. Defaults to the current agent."
                        ),
                    },
                    "include_skill_docs": {
                        "type": "boolean",
                        "description": (
                            "Whether to fetch each published SKILL.md document in addition to the "
                            "skills index. Defaults to true."
                        ),
                    },
                },
                "required": [],
            },
        },
        handler=get_agent_info,
    )
