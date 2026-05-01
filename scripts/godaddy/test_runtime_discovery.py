import importlib.util
import builtins
import json
import unittest
import unittest.mock
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "godaddy-ans"
EXPECTED_ANS_TOOLS = [
    "godaddy_ans_capabilities",
    "godaddy_ans_prepare_registration",
    "godaddy_ans_register",
    "godaddy_ans_search",
    "godaddy_ans_get_agent",
    "godaddy_ans_resolve",
    "godaddy_ans_revoke",
    "godaddy_ans_verify_acme",
    "godaddy_ans_verify_dns",
    "godaddy_dns_set_records",
    "godaddy_ans_get_identity_certificates",
    "godaddy_ans_submit_identity_csr",
    "godaddy_ans_get_server_certificates",
    "godaddy_ans_submit_server_csr",
    "godaddy_ans_get_csr_status",
    "godaddy_ans_events",
]


class _ToolCtx:
    def __init__(self) -> None:
        self.tools = []
        self.hooks = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_hook(self, name, callback) -> None:
        self.hooks.append((name, callback))


def _load_godaddy_ans_plugin():
    plugin_path = PLUGIN_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location("godaddy_ans_plugin", plugin_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GoDaddyRuntimeDiscoveryTests(unittest.TestCase):
    def test_plugin_manifest_and_registered_tools_stay_in_sync(self) -> None:
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "godaddy-ans")
        self.assertEqual(manifest["provides_tools"], EXPECTED_ANS_TOOLS)

        module = _load_godaddy_ans_plugin()
        ctx = _ToolCtx()
        module.register(ctx)

        self.assertEqual([tool["name"] for tool in ctx.tools], EXPECTED_ANS_TOOLS)
        self.assertTrue(all(tool["toolset"] == "godaddy-ans" for tool in ctx.tools))
        self.assertIn("pre_llm_call", [name for name, _callback in ctx.hooks])

    def test_capabilities_tool_describes_mcp_and_ans_without_credentials(self) -> None:
        module = _load_godaddy_ans_plugin()
        ctx = _ToolCtx()
        module.register(ctx)
        handlers = {tool["name"]: tool["handler"] for tool in ctx.tools}

        payload = json.loads(handlers["godaddy_ans_capabilities"]({}))

        self.assertIn("godaddy_mcp", payload)
        self.assertIn("godaddy_ans", payload)
        self.assertEqual(payload["godaddy_ans"]["toolset"], "godaddy-ans")
        self.assertIn("domain availability checks", payload["godaddy_mcp"]["use_for"])
        self.assertEqual(payload["godaddy_ans"]["default_environment"], "production")
        self.assertEqual(
            payload["godaddy_ans"]["registration_tool_status"]["status"],
            "swagger_aligned",
        )
        self.assertIn(
            "identityCsrPEM is a base64-encoded PEM CSR.",
            payload["godaddy_ans"]["registration_tool_status"]["payload_requirements"],
        )
        self.assertIn(
            "functions must be nested under AgentEndpoint objects, not top-level.",
            payload["godaddy_ans"]["registration_tool_status"]["payload_requirements"],
        )
        self.assertIn(
            "DNS.1 = <agentHost>",
            payload["godaddy_ans"]["registration_tool_status"]["csr_requirements"]["dns_san"],
        )
        self.assertIn("http_01", payload["godaddy_ans"]["acme_validation_workflow"])
        self.assertIn("verify_dns", payload["godaddy_ans"]["acme_validation_workflow"])
        self.assertIn("godaddy_ans_search", payload["godaddy_ans"]["tools"])
        self.assertIn("godaddy_dns_set_records", payload["godaddy_ans"]["tools"])
        self.assertIn("godaddy_ans_revoke", payload["godaddy_ans"]["tools"])
        self.assertIn("godaddy_ans_events", payload["godaddy_ans"]["tools"])
        self.assertIn(
            "godaddy_ans_prepare_registration",
            payload["godaddy_ans"]["credential_status"]["offline_tools"],
        )

    def test_plugin_injects_ans_routing_context_for_godaddy_turns(self) -> None:
        module = _load_godaddy_ans_plugin()
        ctx = _ToolCtx()
        module.register(ctx)
        hooks = {name: callback for name, callback in ctx.hooks}

        reminder = hooks["pre_llm_call"](user_message='demo the ans plugin by searching for "payment" agents')

        self.assertIn("godaddy_ans_search", reminder)
        self.assertIn("production", reminder)
        self.assertIn("Swagger", reminder)
        self.assertIn("base64 CSR", reminder)
        self.assertIn("no top-level functions", reminder)
        self.assertIn("Do not", reminder)
        self.assertIn("GODADDY_API_KEY", reminder)

    def test_registration_tool_descriptions_surface_swagger_contract(self) -> None:
        module = _load_godaddy_ans_plugin()
        ctx = _ToolCtx()
        module.register(ctx)
        schemas = {tool["name"]: tool["schema"] for tool in ctx.tools}

        register_description = schemas["godaddy_ans_register"]["description"]
        prepare_description = schemas["godaddy_ans_prepare_registration"]["description"]
        verify_acme_description = schemas["godaddy_ans_verify_acme"]["description"]
        verify_dns_description = schemas["godaddy_ans_verify_dns"]["description"]
        set_records_description = schemas["godaddy_dns_set_records"]["description"]

        self.assertIn("Swagger-aligned", register_description)
        self.assertIn("godaddy_ans_prepare_registration", register_description)
        self.assertIn("base64-encoded PEM", prepare_description)
        self.assertIn("DNS and URI SANs", prepare_description)
        self.assertIn("HTTP-01", verify_acme_description)
        self.assertIn("DNS-01", verify_acme_description)
        self.assertIn("HTTPS, TLSA, _ans", verify_dns_description)
        self.assertIn("PUT /v1/domains/{domain}/records/{type}/{name}", set_records_description)

    def test_ans_search_module_imports_without_cryptography_installed(self) -> None:
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ModuleNotFoundError(name)
            return real_import(name, *args, **kwargs)

        ans_path = REPO_ROOT / "scripts" / "godaddy" / "ans.py"
        spec = importlib.util.spec_from_file_location("godaddy_ans_no_crypto", ans_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        with unittest.mock.patch("builtins.__import__", side_effect=guarded_import):
            spec.loader.exec_module(module)

        fake_response = {
            "status_code": 200,
            "url": "https://api.godaddy.com/v1/agents",
            "body": {"agents": [{"agentDisplayName": "Payment Bot"}]},
        }
        with unittest.mock.patch.object(module, "_json_request", return_value=fake_response):
            result = module.search_agents(query="payment")

        self.assertEqual(result["query"]["matched"], 1)

    def test_always_loaded_context_names_both_godaddy_surfaces(self) -> None:
        hermes_context = (REPO_ROOT / "HERMES.md").read_text(encoding="utf-8")
        agents_context = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

        for content in (hermes_context, agents_context):
            self.assertIn("GoDaddy MCP", content)
            self.assertIn("godaddy-ans", content)
            self.assertIn("godaddy_ans_search", content)
            self.assertIn("Swagger-aligned", content)
            self.assertNotIn("known-broken", content)

    def test_entrypoint_installs_godaddy_skill_in_directory_layout_and_checks_runtime(self) -> None:
        entrypoint = (REPO_ROOT / "scripts" / "entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn('skill_target_dir="${SKILLS_DIR}/${skill_name}"', entrypoint)
        self.assertIn("GoDaddy MCP and ANS runtime surfaces", entrypoint)
        self.assertIn("STRICT_GODADDY_RUNTIME", entrypoint)
        self.assertIn("godaddy_ans_capabilities", entrypoint)
        self.assertIn("godaddy_ans_events", entrypoint)
        self.assertIn("pre_llm_call", entrypoint)
        self.assertIn("importlib.util", entrypoint)
        self.assertIn('plugins_cfg["enabled"] = enabled_plugins', entrypoint)
        self.assertIn('plugins_cfg["disabled"] = disabled_plugins', entrypoint)
        self.assertIn("Removed bundled plugins from plugins.disabled", entrypoint)
        self.assertIn("plugins.enabled does not include godaddy-ans", entrypoint)

        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY scripts/godaddy /app/scripts/godaddy", dockerfile)


if __name__ == "__main__":
    unittest.main()
