import json
import subprocess
import sys


def register(ctx):
    schema = {
        "name": "generate_a2a_token",
        "description": (
            "Generate a signed JWT Bearer token for authenticating A2A calls to other agents. "
            "Returns JSON with 'token' (the Bearer token) and 'did' (this agent's DID). "
            "ALWAYS use this tool when you need a JWT for A2A — never write JWT signing code yourself. "
            "Custom JWT code using ecdsa, pyjwt, or cryptography produces DER-encoded signatures "
            "that always fail with HTTP 403."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "audience": {
                    "type": "string",
                    "description": "Optional recipient bare DID; binds the token to that agent. Omit for older peers.",
                },
            },
            "required": [],
        },
    }

    def handle(params, **kwargs):
        cmd = [sys.executable, "/app/scripts/agent_server/gen_jwt.py"]
        audience = (params or {}).get("audience")
        if audience:
            cmd.extend(["--audience", str(audience)])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"Error generating token: {result.stderr.strip()}"
        return result.stdout.strip()

    ctx.register_tool(
        name="generate_a2a_token",
        toolset="gen-jwt",
        schema=schema,
        handler=handle,
    )
