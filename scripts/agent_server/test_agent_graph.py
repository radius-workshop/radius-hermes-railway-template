import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from scripts.agent_server import main


class AgentGraphTests(unittest.TestCase):
    def _sample_agent_card(self) -> dict:
        return {
            "name": "Hermes Agent",
            "description": "A test agent",
            "provider": {
                "name": "Hermes Agent",
                "url": main.BASE_URL,
                "did": "did:web:test.example",
            },
            "supported_interfaces": [
                {
                    "protocol_binding": "JSONRPC",
                    "url": f"{main.BASE_URL}/a2a",
                    "protocol_version": "1.0",
                }
            ],
            "capabilities": {
                "streaming": True,
                "push_notifications": True,
                "extended_agent_card": False,
                "a2a_modes": ["direct", "delegated"],
            },
            "skills": [
                {
                    "id": "radius-wallet",
                    "name": "radius-wallet",
                    "description": "Wallet skill",
                }
            ],
        }

    def _sample_graph_payload(self) -> dict:
        return {
            "generated_at": "2026-04-22T00:00:00Z",
            "agent": {"id": "agent", "label": "Hermes Agent", "description": "A test agent"},
            "stats": {
                "node_count": 3,
                "edge_count": 2,
                "external_count": 1,
                "internal_count": 2,
                "published_skill_count": 1,
                "plugin_count": 1,
            },
            "nodes": [
                {
                    "id": "agent",
                    "label": "Hermes Agent",
                    "kind": "agent",
                    "category": "core",
                    "external": False,
                    "detail": "A test agent",
                    "href": f"{main.BASE_URL}/",
                    "status": "active",
                    "tags": ["core"],
                },
                {
                    "id": "surface:a2a",
                    "label": "A2A Endpoint",
                    "kind": "surface",
                    "category": "interfaces",
                    "external": True,
                    "detail": "JSON-RPC request endpoint",
                    "href": f"{main.BASE_URL}/a2a",
                    "status": "public",
                    "tags": ["public"],
                },
                {
                    "id": "skill:radius-wallet",
                    "label": "Radius Wallet",
                    "kind": "skill",
                    "category": "skills",
                    "external": False,
                    "detail": "Wallet skill",
                    "href": f"{main.BASE_URL}/.well-known/agent-skills/radius-wallet/SKILL.md",
                    "status": "published",
                    "tags": ["bundled", "published"],
                },
            ],
            "edges": [
                {"source": "agent", "target": "surface:a2a", "kind": "exposes"},
                {"source": "agent", "target": "skill:radius-wallet", "kind": "contains"},
            ],
        }

    def test_api_catalog_exposes_linkset_json_for_public_apis(self) -> None:
        with TestClient(main.app) as client:
            response = client.get("/.well-known/api-catalog")

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/linkset+json", response.headers["content-type"])

        payload = response.json()
        self.assertEqual(set(payload), {"linkset"})
        self.assertIsInstance(payload["linkset"], list)
        self.assertGreaterEqual(len(payload["linkset"]), 3)

        linksets_by_anchor = {
            item.get("anchor"): item.get("links", []) for item in payload["linkset"]
        }
        expected_anchors = {
            f"{main.BASE_URL}/a2a",
            f"{main.BASE_URL}/.well-known/agent-card.json",
            f"{main.BASE_URL}/.well-known/agent-skills/index.json",
        }
        self.assertTrue(expected_anchors.issubset(linksets_by_anchor))

        for anchor, links in linksets_by_anchor.items():
            self.assertTrue(str(anchor).startswith(main.BASE_URL), anchor)
            self.assertIsInstance(links, list)
            rels = {link.get("rel") for link in links}
            self.assertTrue(
                {"service-desc", "service-doc", "status"} & rels,
                f"{anchor} missing discovery relations",
            )
            for link in links:
                self.assertIn("rel", link)
                self.assertIn("href", link)
                self.assertTrue(str(link["href"]).startswith(main.BASE_URL))

    def test_generated_skills_index_uses_v020_schema_and_entry_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_root = Path(tmp)
            published_dir = skills_root / "radius-wallet"
            draft_dir = skills_root / "draft-skill"
            published_dir.mkdir()
            draft_dir.mkdir()
            skill_md = (
                "---\n"
                "published: true\n"
                "description: Inspect Radius wallet balances.\n"
                "---\n"
                "# Radius Wallet\n"
            )
            published_path = published_dir / "SKILL.md"
            published_path.write_text(skill_md, encoding="utf-8")
            (draft_dir / "SKILL.md").write_text(
                "---\npublished: false\ndescription: Draft.\n---\n# Draft\n",
                encoding="utf-8",
            )

            with patch.object(main, "SKILLS_ROOT", str(skills_root)):
                payload = json.loads(main._build_index())

        self.assertEqual(
            payload["$schema"],
            "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
        )
        self.assertEqual(len(payload["skills"]), 1)
        skill = payload["skills"][0]
        self.assertEqual(
            set(skill),
            {"name", "type", "description", "url", "digest"},
        )
        self.assertEqual(skill["name"], "radius-wallet")
        self.assertEqual(skill["type"], "skill-md")
        self.assertEqual(skill["description"], "Inspect Radius wallet balances.")
        self.assertEqual(
            skill["url"],
            f"{main.BASE_URL}/.well-known/agent-skills/radius-wallet/SKILL.md",
        )
        self.assertRegex(skill["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_graph_payload_exposes_runtime_nodes_and_redacts_secrets(self) -> None:
        published_index = json.dumps(
            {
                "skills": [
                    {
                        "name": "radius-wallet",
                        "description": "Wallet skill",
                        "url": f"{main.BASE_URL}/.well-known/agent-skills/radius-wallet/SKILL.md",
                    }
                ]
            }
        )
        plugins = [
            {
                "name": "radius-cast",
                "description": "Radius wallet tools",
                "tools": ["radius_balance", "radius_send_sbc"],
                "path": "/tmp/radius-cast",
            },
            {
                "name": "gen-jwt",
                "description": "JWT signer",
                "tools": ["generate_a2a_token"],
                "path": "/tmp/gen-jwt",
            },
        ]

        with patch.dict(
            os.environ,
            {
                "JWT_API_KEY": "super-secret-api-key",
                "TELEGRAM_BOT_TOKEN": "123:abc",
                "DISCORD_ALLOWED_USERS": "111,222",
                "WEBHOOK_SECRET": "secret-hook",
                "BYTEROVER_API_KEY": "memory-key",
            },
            clear=False,
        ), patch.object(
            main, "_build_agent_card_payload", return_value=self._sample_agent_card()
        ), patch.object(
            main, "_get_index", return_value=published_index
        ), patch.object(
            main,
            "_scan_bundled_skills",
            return_value={
                "radius-wallet": {
                    "name": "radius-wallet",
                    "description": "Wallet skill",
                    "published": True,
                    "origin": "bundled",
                }
            },
        ), patch.object(
            main,
            "_load_vendored_manifest",
            return_value={
                "skills": [
                    {
                        "name": "vendor-skill",
                        "description": "Vendored skill",
                        "published": False,
                    }
                ]
            },
        ), patch.object(
            main, "_load_plugin_manifests", return_value=plugins
        ), patch.object(
            main, "_read_config", return_value={"toolsets": ["gen-jwt", "radius-cast"]}
        ), patch.object(
            main, "_wallet_address", return_value="0xabc123"
        ):
            payload = main._build_agent_graph_payload()

        nodes = {node["id"]: node for node in payload["nodes"]}
        serialized = json.dumps(payload)

        self.assertIn("surface:a2a", nodes)
        self.assertIn("surface:token", nodes)
        self.assertIn("capability:wallet", nodes)
        self.assertIn("channel:telegram", nodes)
        self.assertIn("tool:radius_balance", nodes)
        self.assertIn("skill:radius-wallet", nodes)
        self.assertEqual(nodes["tool:radius_balance"]["category"], "capabilities")
        self.assertEqual(nodes["tool:radius_send_sbc"]["category"], "capabilities")
        self.assertEqual(nodes["tool:generate_a2a_token"]["category"], "capabilities")
        self.assertNotIn("super-secret-api-key", serialized)
        self.assertNotIn("111,222", serialized)
        self.assertNotIn("secret-hook", serialized)

        edges = {
            (edge["source"], edge["target"], edge["kind"]) for edge in payload["edges"]
        }
        self.assertIn(("capability:wallet", "tool:radius_balance", "provides"), edges)
        self.assertIn(("capability:wallet", "tool:radius_send_sbc", "provides"), edges)
        self.assertIn(("capability:jwt", "tool:generate_a2a_token", "provides"), edges)
        self.assertNotIn(("plugin:radius-cast", "tool:radius_balance", "provides"), edges)

    def test_graph_payload_skips_token_surface_without_exchange_key(self) -> None:
        with patch.dict(
            os.environ,
            {"JWT_API_KEY": "", "JWT_EXCHANGE_KEY": "", "TELEGRAM_BOT_TOKEN": ""},
            clear=False,
        ), patch.object(
            main, "_build_agent_card_payload", return_value=self._sample_agent_card()
        ), patch.object(
            main, "_get_index", return_value=json.dumps({"skills": []})
        ), patch.object(
            main, "_scan_bundled_skills", return_value={}
        ), patch.object(
            main, "_load_vendored_manifest", return_value={"skills": []}
        ), patch.object(
            main, "_scan_vendored_skills", return_value={"skills": []}
        ), patch.object(
            main,
            "_load_plugin_manifests",
            return_value=[
                {
                    "name": "agent-info",
                    "description": "Discovery aggregator",
                    "tools": ["get_agent_info"],
                    "path": "/tmp/agent-info",
                },
                {
                    "name": "gen-jwt",
                    "description": "JWT signer",
                    "tools": ["generate_a2a_token"],
                    "path": "/tmp/gen-jwt",
                },
            ],
        ), patch.object(
            main, "_read_config", return_value={"toolsets": ["gen-jwt"]}
        ), patch.object(
            main, "_wallet_address", return_value=None
        ):
            payload = main._build_agent_graph_payload()

        nodes = {node["id"]: node for node in payload["nodes"]}
        self.assertNotIn("surface:token", nodes)
        self.assertEqual(nodes["tool:get_agent_info"]["category"], "capabilities")
        self.assertEqual(nodes["tool:get_agent_info"]["status"], "bundled")
        self.assertEqual(nodes["tool:generate_a2a_token"]["category"], "capabilities")
        self.assertEqual(nodes["tool:generate_a2a_token"]["status"], "enabled")

    def test_homepage_renders_graph_tab_and_module_script(self) -> None:
        wallet_summary = {
            "address": "0xabc123",
            "sbc": "5.0",
            "rusd": "7.5",
            "error": None,
        }
        published_index = json.dumps(
            {
                "skills": [
                    {
                        "name": "radius-wallet",
                        "description": "Wallet skill",
                        "url": f"{main.BASE_URL}/.well-known/agent-skills/radius-wallet/SKILL.md",
                    }
                ]
            }
        )

        with patch.object(
            main, "_build_agent_card_payload", return_value=self._sample_agent_card()
        ), patch.object(
            main, "_get_index", return_value=published_index
        ), patch.object(
            main, "_build_agent_graph_payload", return_value=self._sample_graph_payload()
        ), patch.object(
            main, "get_did", return_value="did:web:test.example"
        ), patch.object(
            main, "_get_wallet_summary", new=AsyncMock(return_value=(wallet_summary, True))
        ):
            with TestClient(main.app) as client:
                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("id='agent-graph-root'", response.text)
        self.assertIn("/agent-graph.json", response.text)
        self.assertIn("/static/js/homepage.js", response.text)


if __name__ == "__main__":
    unittest.main()
