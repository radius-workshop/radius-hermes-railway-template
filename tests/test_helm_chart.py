"""Render deploy/helm/hermes-agent and check its security and wiring invariants.

Skipped when helm is not installed. scripts/test_helm_chart.sh installs the chart
on a real cluster.
"""
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "hermes-agent"
HELM = shutil.which("helm")


def render(*args: str) -> list[dict]:
    out = subprocess.run(
        [HELM, "template", "t", str(CHART), "--namespace", "ns", *args],
        capture_output=True, text=True, check=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def render_error(*args: str) -> str:
    result = subprocess.run(
        [HELM, "template", "t", str(CHART), *args], capture_output=True, text=True,
    )
    assert result.returncode != 0, "render unexpectedly succeeded"
    return result.stderr


def find(docs: list[dict], kind: str, name: str | None = None) -> dict:
    for doc in docs:
        if doc["kind"] == kind and (name is None or doc["metadata"]["name"] == name):
            return doc
    raise AssertionError(f"{kind} {name or ''} not rendered")


def kinds(docs: list[dict]) -> list[str]:
    return sorted(doc["kind"] for doc in docs)


@unittest.skipUnless(HELM, "helm not installed")
class HelmChartTests(unittest.TestCase):
    def test_lint_and_examples(self):
        subprocess.run([HELM, "lint", str(CHART), "--set", "agent.existingSecret=x"],
                       capture_output=True, check=True)
        for example in sorted((CHART / "examples").glob("*.yaml")):
            with self.subTest(example=example.name):
                render("-f", str(example))

    def test_minimal_install_is_core_kubernetes_only(self):
        docs = render("--set", "agent.existingSecret=s")
        self.assertEqual(kinds(docs), ["ConfigMap", "Pod", "Service", "ServiceAccount", "StatefulSet"])

    def test_agent_pod_is_hardened(self):
        sts = find(render("--set", "agent.existingSecret=s"), "StatefulSet")
        self.assertEqual(sts["spec"]["replicas"], 1)
        pod = sts["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertFalse(pod["enableServiceLinks"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["runAsUser"], 10001)
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        container = pod["containers"][0]
        sc = container["securityContext"]
        self.assertTrue(sc["readOnlyRootFilesystem"])
        self.assertFalse(sc["allowPrivilegeEscalation"])
        self.assertEqual(sc["capabilities"]["drop"], ["ALL"])
        mounts = {m["mountPath"] for m in container["volumeMounts"]}
        self.assertEqual(mounts, {"/data", "/tmp"})
        self.assertEqual(sts["spec"]["volumeClaimTemplates"][0]["metadata"]["name"], "data")
        self.assertEqual(container["envFrom"][1], {"secretRef": {"name": "s"}})

    def test_config_drops_empty_values_and_sets_public_url(self):
        docs = render("--set", "agent.existingSecret=s", "--set", "agent.config.LLM_MODEL=",
                      "--set", "agent.config.TELEGRAM_ALLOWED_USERS=1",
                      "--set", "ingress.enabled=true", "--set", "ingress.host=agent.example.com",
                      "--set", "ingress.tlsSecretName=tls")
        data = find(docs, "ConfigMap")["data"]
        self.assertNotIn("LLM_MODEL", data)
        self.assertEqual(data["TELEGRAM_ALLOWED_USERS"], "1")
        self.assertEqual(data["PUBLIC_URL"], "https://agent.example.com")
        self.assertEqual(data["PORT"], "3000")

    def test_secret_is_required(self):
        self.assertIn("agent.existingSecret", render_error())
        self.assertIn("egress.existingSecret", render_error(
            "--set", "agent.existingSecret=s", "--set", "egress.enabled=true",
            "--set", "egress.secrets={OPENROUTER_API_KEY}"))
        self.assertIn("set by the chart", render_error(
            "--set", "agent.existingSecret=s", "--set", "agent.config.PORT=1"))

    def test_schema_rejects_typos_and_replica_changes(self):
        self.assertIn("additional property", render_error(
            "--set", "agent.existingSecret=s", "--set", "agent.replicas=2").lower())
        self.assertIn("additional property", render_error(
            "--set", "agent.existingSecret=s", "--set", "persistance.enabled=false").lower())

    def test_egress_proxy_wiring(self):
        docs = render("--set", "agent.existingSecret=a", "--set", "egress.enabled=true",
                      "--set", "egress.existingSecret=e",
                      "--set", "egress.secrets={OPENROUTER_API_KEY,SLACK_BOT_TOKEN}")
        agent_cfg = find(docs, "ConfigMap", "t-hermes-agent")["data"]
        self.assertEqual(agent_cfg["EGRESS_PROXY_URL"], "http://t-hermes-agent-egress.ns.svc.cluster.local:9090")
        self.assertEqual(agent_cfg["EGRESS_SECRETS"], "OPENROUTER_API_KEY,SLACK_BOT_TOKEN")
        egress_cfg = find(docs, "ConfigMap", "t-hermes-agent-egress")["data"]
        self.assertEqual(egress_cfg["HERMES_SERVICE_ROLE"], "egress")
        deploy = find(docs, "Deployment", "t-hermes-agent-egress")
        container = deploy["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["envFrom"][1], {"secretRef": {"name": "e"}})
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        ports = {p["port"] for p in find(docs, "Service", "t-hermes-agent-egress")["spec"]["ports"]}
        self.assertEqual(ports, {9090, 9091})

    def test_network_policies(self):
        docs = render("--set", "agent.existingSecret=a", "--set", "egress.enabled=true",
                      "--set", "egress.existingSecret=e", "--set", "egress.secrets={OPENROUTER_API_KEY}",
                      "--set", "networkPolicy.enabled=true", "--set", "networkPolicy.agentDirectEgress=false")
        agent = find(docs, "NetworkPolicy", "t-hermes-agent")["spec"]
        self.assertEqual(agent["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(agent["ingress"][0]["ports"], [{"protocol": "TCP", "port": 3000}])
        # DNS and the egress proxy only: no internet rule.
        self.assertEqual(len(agent["egress"]), 2)
        self.assertNotIn("ipBlock", str(agent["egress"]))
        proxy = find(docs, "NetworkPolicy", "t-hermes-agent-egress")["spec"]
        internet = proxy["egress"][1]["to"][0]["ipBlock"]
        self.assertEqual(internet["cidr"], "0.0.0.0/0")
        self.assertIn("169.254.0.0/16", internet["except"])

    def test_hooks_for_later_integrations(self):
        docs = render(
            "--set", "agent.existingSecret=a",
            "--set", "agent.runtimeClassName=gvisor",
            "--set", "serviceAccount.annotations.iam\\.gke\\.io/gcp-service-account=x@p.iam.gserviceaccount.com",
            "--set", "image.digest=sha256:" + "a" * 64,
            "--set-json", 'extraObjects=[{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"{{ .Release.Name }}-extra"}}]',
        )
        pod = find(docs, "StatefulSet")["spec"]["template"]["spec"]
        self.assertEqual(pod["runtimeClassName"], "gvisor")
        self.assertTrue(pod["containers"][0]["image"].endswith("@sha256:" + "a" * 64))
        sa = find(docs, "ServiceAccount")
        self.assertIn("iam.gke.io/gcp-service-account", sa["metadata"]["annotations"])
        find(docs, "ConfigMap", "t-extra")

    def test_existing_claim_and_no_persistence(self):
        sts = find(render("--set", "agent.existingSecret=a", "--set", "persistence.existingClaim=railway-data"),
                   "StatefulSet")
        self.assertNotIn("volumeClaimTemplates", sts["spec"])
        volumes = {v["name"]: v for v in sts["spec"]["template"]["spec"]["volumes"]}
        self.assertEqual(volumes["data"]["persistentVolumeClaim"]["claimName"], "railway-data")
        sts = find(render("--set", "agent.existingSecret=a", "--set", "persistence.enabled=false"), "StatefulSet")
        volumes = {v["name"]: v for v in sts["spec"]["template"]["spec"]["volumes"]}
        self.assertIn("emptyDir", volumes["data"])


if __name__ == "__main__":
    unittest.main()
