"""Recovery diagnostics must not print credentials or treat sealed keys as absent."""
import contextlib
import argparse
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent0_setup as setup  # noqa: E402


def fixtures():
    shared = {
        "EGRESS_SECRETS": "OPENROUTER_API_KEY",
        "EGRESS_TOKEN_SEED": "fixture-seed-" * 4,
        "EGRESS_CA_CERT": "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----",
    }
    return (
        {**shared, "EGRESS_PROXY_URL": "http://egress.railway.internal:9090"},
        {**shared, "EGRESS_CA_KEY": "private-ca-sentinel", "OPENROUTER_API_KEY": "provider-secret-sentinel"},
    )


class RecoveryGuideTests(unittest.TestCase):
    def test_sealed_credential_is_already_set_not_missing(self):
        agent, proxy = fixtures()
        proxy["OPENROUTER_API_KEY"] = None
        proxy["EGRESS_CA_KEY"] = None
        findings = setup.diagnose(agent, proxy)
        codes = {item["code"] for item in findings}
        self.assertIn("credential_sealed", codes)
        self.assertIn("ca_key_sealed", codes)
        self.assertNotIn("credential_missing", codes)
        self.assertNotIn("ca_key_missing", codes)

    def test_missing_credential_blocks_but_runtime_remains_unverified(self):
        agent, proxy = fixtures()
        del proxy["OPENROUTER_API_KEY"]
        findings = setup.diagnose(agent, proxy)
        self.assertTrue(any(item["code"] == "credential_missing" and item["status"] == "blocked" for item in findings))
        self.assertTrue(any(item["code"] == "runtime_unverified" for item in findings))
        self.assertTrue(any(item["code"] == "remote_signer_unverified" for item in findings))

    def test_sealed_shared_setting_is_unknown_not_mismatch(self):
        agent, proxy = fixtures()
        proxy["EGRESS_TOKEN_SEED"] = None
        findings = setup.diagnose(agent, proxy)
        self.assertTrue(any(item["code"] == "shared_sealed" for item in findings))
        self.assertFalse(any(item["code"] == "shared_mismatch" for item in findings))

    def test_real_agent_key_ca_key_and_disabled_enforcement_detected(self):
        agent, proxy = fixtures()
        agent.update(OPENROUTER_API_KEY="raw-agent-secret", EGRESS_CA_KEY="raw-ca-secret", EGRESS_ENFORCE="false")
        codes = {item["code"] for item in setup.diagnose(agent, proxy)}
        self.assertTrue({"real_credential_on_agent", "ca_key_on_agent", "enforcement_disabled"} <= codes)

    def test_expected_proxy_token_is_accepted_only_on_agent(self):
        agent, proxy = fixtures()
        spec = setup.egress.managed_specs(agent)[0]
        token = setup.egress.proxy_token(agent["EGRESS_TOKEN_SEED"].encode(), spec)
        agent["OPENROUTER_API_KEY"] = token
        self.assertNotIn("real_credential_on_agent", {item["code"] for item in setup.diagnose(agent, proxy)})
        proxy["OPENROUTER_API_KEY"] = token
        self.assertIn("credential_is_token", {item["code"] for item in setup.diagnose(agent, proxy)})

    def test_all_render_modes_hide_secret_values_and_custom_names(self):
        agent, proxy = fixtures()
        agent["OPENROUTER_API_KEY"] = "agent-key-sentinel"
        agent["EGRESS_SECRETS"] = "malformed-secret-sentinel"
        report = {"findings": setup.diagnose(agent, proxy)}
        for as_json in (False, True):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                setup.render(report, as_json)
            for value in ("agent-key-sentinel", "malformed-secret-sentinel", "private-ca-sentinel", "provider-secret-sentinel", agent["EGRESS_TOKEN_SEED"]):
                self.assertNotIn(value, output.getvalue())

    def test_cli_failure_does_not_forward_raw_output(self):
        result = subprocess.CompletedProcess([], 1, "stdout-secret-sentinel", "stderr-secret-sentinel")
        with patch.object(setup.subprocess, "run", return_value=result):
            with self.assertRaises(setup.InspectionError) as error:
                setup.railway_json(["variable", "list"])
        self.assertNotIn("sentinel", str(error.exception))

    def test_timeout_does_not_forward_exception_command(self):
        with patch.object(setup.subprocess, "run", side_effect=subprocess.TimeoutExpired("secret-sentinel", 45)):
            with self.assertRaises(setup.InspectionError) as error:
                setup.railway_json([])
        self.assertNotIn("sentinel", str(error.exception))

    def test_explicit_target_is_used_and_setup_does_not_mutate(self):
        agent, proxy = fixtures()
        results = [subprocess.CompletedProcess([], 0, json.dumps(env), "") for env in (agent, proxy)]
        output = io.StringIO()
        with patch.object(setup.subprocess, "run", side_effect=results) as run:
            with contextlib.redirect_stdout(output):
                code = setup.main(["setup", "--project", "p", "--environment", "production", "--agent", "agent0", "--egress", "egress", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["read_only"])
        self.assertFalse(report["setup_complete"])
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[:3], ["railway", "variable", "list"])
            self.assertIn("--project", command)
            self.assertIn("--environment", command)
            self.assertIn("--service", command)

    def test_json_null_is_preserved(self):
        with patch.object(setup, "railway_json", return_value={"TOKEN": None}):
            self.assertEqual(setup.variables("p", "e", "s"), {"TOKEN": None})

    def test_custom_provider_requires_host_override_without_reflecting_url(self):
        agent, proxy = fixtures()
        agent["EGRESS_SECRETS"] = proxy["EGRESS_SECRETS"] = "OPENAI_API_KEY"
        agent["OPENAI_BASE_URL"] = "https://private-host-sentinel.test/v1?token=url-secret-sentinel"
        findings = setup.diagnose(agent, proxy)
        self.assertIn("provider_host_override_missing", {item["code"] for item in findings})
        self.assertNotIn("sentinel", json.dumps(findings))

    def test_discovery_selects_unambiguous_project_environment_and_services(self):
        data = {"id": "p", "environments": {"edges": [{"node": {
            "id": "e", "name": "production", "serviceInstances": {"edges": [
                {"node": {"serviceId": "a", "serviceName": "agent0"}},
                {"node": {"serviceId": "x", "serviceName": "egress"}},
            ]},
        }}]}}
        args = argparse.Namespace(project=None, environment=None, agent=None, egress=None, json=True)
        with patch.object(setup, "railway_json", return_value=data):
            self.assertFalse(setup.discover(args))
        self.assertEqual((args.project, args.environment, args.agent, args.egress), ("p", "e", "a", "x"))

    def test_absent_egress_is_actionable_and_agent_inspection_continues(self):
        data = {"id": "p", "environments": {"edges": [{"node": {
            "id": "e", "name": "production", "serviceInstances": {"edges": [
                {"node": {"serviceId": "a", "serviceName": "agent0"}},
            ]},
        }}]}}
        agent, _ = fixtures()
        output = io.StringIO()
        with patch.object(setup, "railway_json", side_effect=[data, agent]) as railway:
            with contextlib.redirect_stdout(output):
                code = setup.main(["setup", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(railway.call_count, 2)
        report = json.loads(output.getvalue())
        self.assertEqual(report["findings"][0]["code"], "egress_not_deployed")
        self.assertFalse(report["setup_complete"])

    def test_unavailable_egress_does_not_claim_missing_credentials(self):
        agent, _ = fixtures()
        output = io.StringIO()
        with patch.object(setup, "variables", side_effect=[agent, setup.InspectionError("safe error")]):
            with contextlib.redirect_stdout(output):
                code = setup.main(["doctor", "--project", "p", "--environment", "e", "--agent", "a", "--egress", "x", "--json"])
        self.assertEqual(code, 0)
        codes = {item["code"] for item in json.loads(output.getvalue())["findings"]}
        self.assertIn("egress_unavailable", codes)
        self.assertNotIn("credential_missing", codes)

    def test_sealed_custom_rules_are_unknown_even_when_both_null(self):
        agent, proxy = fixtures()
        agent["EGRESS_HOSTS_OPENROUTER_API_KEY"] = None
        proxy["EGRESS_HOSTS_OPENROUTER_API_KEY"] = None
        findings = setup.diagnose(agent, proxy)
        self.assertIn("rules_sealed", {item["code"] for item in findings})

    def test_noninteractive_ambiguous_selection_requires_explicit_target(self):
        with self.assertRaisesRegex(setup.InspectionError, "--agent"):
            setup.choose([{"id": "a", "name": "one"}, {"id": "b", "name": "two"}], "agent", None, False)

    def test_staged_proxy_credentials_checked_before_agent_configuration(self):
        agent, proxy = fixtures()
        del agent["EGRESS_SECRETS"]
        agent["OPENROUTER_API_KEY"] = "old-agent-key-sentinel"
        proxy["EGRESS_SECRETS"] = "OPENROUTER_API_KEY,TELEGRAM_BOT_TOKEN"
        findings = setup.diagnose(agent, proxy)
        self.assertTrue(any(item["code"] == "credential_missing" and "TELEGRAM_BOT_TOKEN" in item["detail"] for item in findings))
        self.assertTrue(any(item["code"] == "credential_present" and "OPENROUTER_API_KEY" in item["detail"] for item in findings))
        self.assertTrue(any(item["code"] == "unmanaged_credential" and "OPENROUTER_API_KEY" in item["detail"] for item in findings))
        self.assertFalse(any(item["code"] == "real_credential_on_agent" for item in findings))
        self.assertNotIn("sentinel", json.dumps(findings))

    def test_different_proxy_and_agent_lists_keep_isolation_checks_separate(self):
        agent, proxy = fixtures()
        proxy["EGRESS_SECRETS"] = "TELEGRAM_BOT_TOKEN"
        proxy["TELEGRAM_BOT_TOKEN"] = None
        agent["TELEGRAM_BOT_TOKEN"] = "old-agent-key-sentinel"
        findings = setup.diagnose(agent, proxy)
        self.assertIn("shared_mismatch", {item["code"] for item in findings})
        self.assertTrue(any(item["code"] == "credential_sealed" and "TELEGRAM_BOT_TOKEN" in item["detail"] for item in findings))
        self.assertTrue(any(item["code"] == "unmanaged_credential" and "TELEGRAM_BOT_TOKEN" in item["detail"] for item in findings))


if __name__ == "__main__":
    unittest.main()
