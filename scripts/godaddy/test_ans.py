import importlib.util
import base64
import io
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parent / "ans.py"
SPEC = importlib.util.spec_from_file_location("godaddy_ans", SCRIPT_PATH)
ans = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ans)


class GoDaddyAnsBootstrapTests(unittest.TestCase):
    def test_ans_base_url_defaults_to_production(self) -> None:
        self.assertEqual(ans._ans_base_url({}), "https://api.godaddy.com")
        self.assertEqual(
            ans._ans_base_url({"GODADDY_ANS_ENV": "ote"}),
            "https://api.ote-godaddy.com",
        )

    def test_build_registration_bundle_uses_hermes_a2a_and_optional_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "HERMES_HOME": tmpdir,
                "PUBLIC_URL": "https://agent.example.com",
                "AGENT_NAME": "Radius Hermes",
                "AGENT_DESCRIPTION": "Agent description",
                "AGENT_MCP_ENDPOINT": "https://agent.example.com/mcp",
                "GODADDY_ANS_INCLUDE_MCP": "true",
                "GODADDY_ANS_MCP_TRANSPORT": "streamable-http",
            }
            result = ans.build_registration_bundle(env=env)

        payload = result["payload"]
        self.assertEqual(payload["agentDisplayName"], "Radius Hermes")
        self.assertEqual(payload["agentHost"], "agent.example.com")
        self.assertEqual(payload["version"], "1.0.0")
        self.assertEqual(len(payload["endpoints"]), 2)
        self.assertNotIn("functions", payload)

        a2a_endpoint = payload["endpoints"][0]
        self.assertEqual(a2a_endpoint["protocol"], "A2A")
        self.assertEqual(a2a_endpoint["agentUrl"], "https://agent.example.com/a2a")
        self.assertEqual(
            a2a_endpoint["metaDataUrl"],
            "https://agent.example.com/.well-known/agent-card.json",
        )

        mcp_endpoint = payload["endpoints"][1]
        self.assertEqual(mcp_endpoint["protocol"], "MCP")
        self.assertEqual(mcp_endpoint["agentUrl"], "https://agent.example.com/mcp")
        self.assertEqual(mcp_endpoint["transports"], ["STREAMABLE-HTTP"])

    def test_registration_bundle_nests_functions_and_generates_swagger_csrs(self) -> None:
        from cryptography import x509

        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir) / "well-known-skills"
            (skills_root / "payment-routing").mkdir(parents=True)
            env = {
                "HERMES_HOME": tmpdir,
                "PUBLIC_URL": "https://agent.example.com",
                "AGENT_NAME": "Radius Hermes",
            }
            result = ans.build_registration_bundle(env=env)

        payload = result["payload"]
        a2a_endpoint = payload["endpoints"][0]
        self.assertNotIn("functions", payload)
        self.assertEqual(
            a2a_endpoint["functions"],
            [{"id": "payment-routing", "name": "Payment Routing"}],
        )

        for field in ("identityCsrPEM", "serverCsrPEM"):
            csr_pem = base64.b64decode(payload[field])
            self.assertTrue(csr_pem.startswith(b"-----BEGIN CERTIFICATE REQUEST-----"))
            csr = x509.load_pem_x509_csr(csr_pem)
            san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            self.assertIn("agent.example.com", san.get_values_for_type(x509.DNSName))
            self.assertIn(
                "ans://v1.0.0.agent.example.com",
                san.get_values_for_type(x509.UniformResourceIdentifier),
            )

    def test_registration_bundle_normalizes_swagger_bounded_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "HERMES_HOME": tmpdir,
                "GODADDY_ANS_AGENT_HOST": "https://Agent.Example.com",
                "GODADDY_ANS_VERSION": "v1.2.3",
                "GODADDY_ANS_DISPLAY_NAME": "X" * 80,
                "GODADDY_ANS_DESCRIPTION": "Y" * 180,
            }
            result = ans.build_registration_bundle(env=env)

        payload = result["payload"]
        self.assertEqual(payload["agentHost"], "agent.example.com")
        self.assertEqual(payload["version"], "1.2.3")
        self.assertEqual(len(payload["agentDisplayName"]), 64)
        self.assertEqual(len(payload["agentDescription"]), 150)

    def test_registration_bundle_rejects_non_swagger_registration_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "HERMES_HOME": tmpdir,
                "PUBLIC_URL": "https://agent.example.com",
                "GODADDY_ANS_VERSION": "1.2.3-beta.1",
            }
            with self.assertRaisesRegex(ValueError, "Semantic Versioning"):
                ans.build_registration_bundle(env=env)

    def test_build_registration_bundle_persists_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "HERMES_HOME": tmpdir,
                "PUBLIC_URL": "https://agent.example.com",
                "AGENT_NAME": "Radius Hermes",
                "GODADDY_ANS_INCLUDE_MCP": "false",
            }
            result = ans.build_registration_bundle(env=env)
            summary = result["summary"]

            for key in (
                "payload_path",
                "identity_key_path",
                "identity_csr_path",
                "server_key_path",
                "server_csr_path",
            ):
                self.assertTrue(Path(summary[key]).exists())

            payload_text = Path(summary["payload_path"]).read_text(encoding="utf-8")
            self.assertIn("identityCsrPEM", payload_text)
            self.assertIn("serverCsrPEM", payload_text)
            self.assertTrue(result["validation"]["valid"])
            self.assertIn("credential_status", summary)

    def test_validate_registration_payload_rejects_top_level_functions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = ans.build_registration_bundle(
                env={"HERMES_HOME": tmpdir, "PUBLIC_URL": "https://agent.example.com"}
            )

        payload = dict(result["payload"])
        payload["functions"] = []
        validation = ans.validate_registration_payload(payload)

        self.assertFalse(validation["valid"])
        self.assertIn(
            {"path": "functions", "message": "must be nested inside endpoint objects"},
            validation["issues"],
        )

    def test_register_agent_dry_run_does_not_call_api_and_reports_missing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"HERMES_HOME": tmpdir, "PUBLIC_URL": "https://agent.example.com"}
            with unittest.mock.patch.object(ans, "_json_request") as request:
                result = ans.register_agent(env=env, dry_run=True)

        request.assert_not_called()
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["ready_to_submit"])
        self.assertTrue(result["bundle"]["validation"]["valid"])
        self.assertEqual(
            result["credential_status"]["missing"],
            ["GODADDY_API_KEY", "GODADDY_API_SECRET"],
        )

    def test_register_agent_missing_credentials_does_not_call_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"HERMES_HOME": tmpdir, "PUBLIC_URL": "https://agent.example.com"}
            with unittest.mock.patch.object(ans, "_json_request") as request:
                result = ans.register_agent(env=env)

        request.assert_not_called()
        self.assertFalse(result["submitted"])
        self.assertEqual(result["error_type"], "missing_credentials")

    def test_register_agent_with_credentials_submits_valid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "HERMES_HOME": tmpdir,
                "PUBLIC_URL": "https://agent.example.com",
                "GODADDY_API_KEY": "key",
                "GODADDY_API_SECRET": "secret",
            }
            with unittest.mock.patch.object(
                ans,
                "_json_request",
                return_value={"status_code": 202, "url": "x", "body": {}},
            ) as request:
                result = ans.register_agent(env=env)

        self.assertTrue(result["submitted"])
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], ("POST", "/v1/agents/register"))

    def test_json_request_adds_403_authorization_diagnostic(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.godaddy.com/v1/agents/register",
            403,
            "Forbidden",
            {},
            io.BytesIO(b"<html>Forbidden</html>"),
        )
        with unittest.mock.patch.object(ans.urllib.request, "urlopen", side_effect=error):
            result = ans._json_request(
                "POST",
                "/v1/agents/register",
                body={},
                env={"GODADDY_API_KEY": "key", "GODADDY_API_SECRET": "secret"},
            )

        self.assertEqual(result["status_code"], 403)
        self.assertEqual(result["diagnostic"]["error_type"], "authorization_failed")

    def test_search_agents_uses_server_side_filters_for_loose_query(self) -> None:
        calls = []

        def fake_request(method, path, *, query=None, **kwargs):
            calls.append(query or {})
            if (query or {}).get("agentDisplayName") == "payment":
                return {
                    "status_code": 200,
                    "url": "https://api.godaddy.com/v1/agents?agentDisplayName=payment",
                    "body": {
                        "agents": [
                            {
                                "agentDisplayName": "Payments Router",
                                "agentHost": "pay.example.com",
                            },
                            {
                                "agentDescription": "Handles payment settlement",
                                "agentHost": "settle.example.com",
                            },
                        ]
                    },
                }
            return {
                "status_code": 200,
                "url": "https://api.godaddy.com/v1/agents",
                "body": {"agents": []},
            }

        with unittest.mock.patch.object(ans, "_json_request", side_effect=fake_request):
            result = ans.search_agents(query="payments")

        self.assertEqual(result["query"]["text"], "payments")
        self.assertEqual(result["query"]["mode"], "server_side_filters")
        self.assertEqual(result["query"]["searched_fields"], ["agentDisplayName", "agentHost"])
        self.assertEqual(result["query"]["matched"], 2)
        self.assertIn({"agentDisplayName": "payment", "limit": 20}, calls)
        self.assertNotIn({"limit": 20}, calls)

    def test_search_agents_broadens_empty_loose_query(self) -> None:
        calls = []

        def fake_request(method, path, *, query=None, **kwargs):
            calls.append(query or {})
            if (query or {}).get("agentDisplayName") == "med":
                return {
                    "status_code": 200,
                    "url": "https://api.godaddy.com/v1/agents?agentDisplayName=med",
                    "body": {
                        "agents": [
                            {
                                "agentDisplayName": "Medical Director",
                                "agentHost": "medical.example.com",
                            }
                        ]
                    },
                }
            return {
                "status_code": 200,
                "url": "https://api.godaddy.com/v1/agents",
                "body": {"agents": []},
            }

        with unittest.mock.patch.object(ans, "_json_request", side_effect=fake_request):
            result = ans.search_agents(query="medicine")

        self.assertTrue(result["query"]["broadened"])
        self.assertIn("med", result["query"]["terms"])
        self.assertEqual(result["query"]["matched"], 1)
        self.assertIn({"agentDisplayName": "medicine", "limit": 20}, calls)
        self.assertIn({"agentDisplayName": "med", "limit": 20}, calls)

    def test_search_agents_bounds_swagger_pagination_and_status_all(self) -> None:
        with unittest.mock.patch.object(
            ans,
            "_json_request",
            return_value={"status_code": 200, "url": "x", "body": {"agents": []}},
        ) as request:
            ans.search_agents(
                agent_display_name="x" * 80,
                agent_host="https://Agent.Example.com",
                limit=500,
                offset=-1,
                status=["ACTIVE", "ALL"],
            )

        query = request.call_args.kwargs["query"]
        self.assertEqual(query["agentDisplayName"], "x" * 64)
        self.assertEqual(query["agentHost"], "agent.example.com")
        self.assertEqual(query["limit"], 100)
        self.assertEqual(query["offset"], 0)
        self.assertEqual(query["status"], "ALL")

    def test_search_agents_rejects_non_swagger_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "status"):
            ans.search_agents(status=["ACTIVE", "PENDING_VALIDATION"])

    def test_resolve_agent_uses_swagger_post_body_and_allows_latest_version(self) -> None:
        with unittest.mock.patch.object(
            ans,
            "_json_request",
            return_value={"status_code": 200, "url": "x", "body": {}},
        ) as request:
            ans.resolve_agent("https://Agent.Example.com", "")

        self.assertEqual(request.call_args.args[:2], ("POST", "/v1/agents/resolution"))
        self.assertEqual(
            request.call_args.kwargs["body"],
            {"agentHost": "agent.example.com", "version": ""},
        )

    def test_certificate_csr_submission_encodes_raw_pem_for_swagger_payload(self) -> None:
        pem = "-----BEGIN CERTIFICATE REQUEST-----\nabc\n-----END CERTIFICATE REQUEST-----\n"
        with unittest.mock.patch.object(
            ans,
            "_json_request",
            return_value={"status_code": 202, "url": "x", "body": {}},
        ) as request:
            ans.submit_identity_csr("agent-1", pem)

        self.assertEqual(
            request.call_args.kwargs["body"],
            {"csrPEM": base64.b64encode(pem.strip().encode("utf-8")).decode("ascii")},
        )

    def test_revoke_agent_normalizes_swagger_reason(self) -> None:
        with unittest.mock.patch.object(
            ans,
            "_json_request",
            return_value={"status_code": 200, "url": "x", "body": {}},
        ) as request:
            ans.revoke_agent("agent-1", "key_compromise", comments="x" * 250)

        self.assertEqual(request.call_args.args[:2], ("POST", "/v1/agents/agent-1/revoke"))
        self.assertEqual(
            request.call_args.kwargs["body"],
            {"reason": "KEY_COMPROMISE", "comments": "x" * 200},
        )

    def test_events_bounds_swagger_limit(self) -> None:
        with unittest.mock.patch.object(
            ans,
            "_json_request",
            return_value={"status_code": 200, "url": "x", "body": {}},
        ) as request:
            ans.get_events(provider_id="provider-1", last_log_id="cursor-1", limit=500)

        self.assertEqual(request.call_args.args[:2], ("GET", "/v1/agents/events"))
        self.assertEqual(
            request.call_args.kwargs["query"],
            {"providerId": "provider-1", "lastLogId": "cursor-1", "limit": 200},
        )

    def test_set_dns_records_replaces_one_type_and_name(self) -> None:
        with unittest.mock.patch.object(
            ans,
            "_json_request",
            return_value={"status_code": 200, "url": "x", "body": None},
        ) as request:
            ans.set_dns_records(
                domain="Example.com",
                record_type="txt",
                name="_acme-challenge",
                records=[{"data": "challenge-token", "ttl": "600"}],
                shopper_id="shopper-1",
            )

        self.assertEqual(
            request.call_args.args[:2],
            ("PUT", "/v1/domains/example.com/records/TXT/_acme-challenge"),
        )
        self.assertEqual(
            request.call_args.kwargs["body"],
            [{"data": "challenge-token", "ttl": 600}],
        )
        self.assertEqual(
            request.call_args.kwargs["headers"],
            {"X-Shopper-Id": "shopper-1"},
        )

    def test_set_dns_records_rejects_unsupported_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "record_type"):
            ans.set_dns_records(
                domain="example.com",
                record_type="CAA",
                name="@",
                records=[{"data": "0 issue ca.example"}],
            )


if __name__ == "__main__":
    unittest.main()
