import unittest
from unittest.mock import patch

from erc8004_registry.client import list_registrations, update_agent_uri
from erc8004_registry.codec import (
    decode_agent_uri,
    encode_agent_uri,
    normalize_registration,
    sanitize_agent_uri,
)
from erc8004_registry.constants import RADIUS_TESTNET
from erc8004_registry.patching import build_ans_pointer_patch, merge_registration_patch


RAILWAY_WEB = "https://rad-hermes-railway-template-production.up.railway.app/"
RAILWAY_A2A = (
    "https://rad-hermes-railway-template-production.up.railway.app/"
    ".well-known/agent-card.json"
)
RAILWAY_DID = "did:web:rad-hermes-railway-template-production.up.railway.app"


def current_registration() -> dict:
    return {
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "name": "Agent 0 (Hermes)",
        "description": "Autonomous AI agent on the Radius EVM payment network.",
        "image": "https://api.dicebear.com/9.x/shapes/svg?seed=agent0-hermes",
        "services": [
            {"endpoint": RAILWAY_WEB, "name": "web", "version": "v1"},
            {"endpoint": RAILWAY_A2A, "name": "A2A", "version": "0.3.0"},
            {"endpoint": RAILWAY_DID, "name": "DID", "version": "v1"},
        ],
        "x402Support": True,
        "active": True,
        "registrations": [
            {
                "agentId": 0,
                "agentRegistry": (
                    "eip155:72344:"
                    "0x5cd923Ce1244d5498Bf3f9E0F3a374C2567F1A31"
                ),
            }
        ],
        "supportedTrust": ["reputation", "crypto-economic"],
        "agentWallet": "0x4D8020F43A9EFb829DBe4Cb93cbb29d5B52aEc6b",
    }


def ans_patch() -> dict:
    return build_ans_pointer_patch(
        ans_name="ans://v1.0.0.agent0.72344.xyz",
        ans_agent_id="b9bfc282-95e6-4fc4-8ba6-e1c380b6d29f",
        agent_host="agent0.72344.xyz",
        status="PENDING_DNS",
        a2a_url="https://agent0.72344.xyz/a2a",
        web_url="https://agent0.72344.xyz/",
        agent_card_url="https://agent0.72344.xyz/.well-known/agent-card.json",
        did="did:web:agent0.72344.xyz",
    )


class ERC8004PatchingTests(unittest.TestCase):
    def test_update_agent_uri_rejects_missing_full_replacement_ack(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "full-replacement write path.*replace_full_registration=true.*partial updates",
        ):
            update_agent_uri(0, current_registration(), network="testnet")

    def test_empty_full_replacement_registration_is_rejected_clearly(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "requires a complete registration object.*"
            "erc8004_patch_agent_registration.*erc8004_add_ans_pointer.*name",
        ):
            update_agent_uri(
                0,
                {},
                replace_full_registration=True,
                network="testnet",
            )

    def test_token_uri_normalization_and_decode(self) -> None:
        encoded = encode_agent_uri(current_registration())
        self.assertEqual(sanitize_agent_uri(f'"{encoded}"'), encoded)
        decoded = decode_agent_uri(f'"{encoded}"')
        self.assertEqual(decoded["name"], "Agent 0 (Hermes)")

    def test_normalize_registration_rejects_missing_required_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "registration.name is required"):
            normalize_registration({}, network=RADIUS_TESTNET)

    def test_placeholder_service_add_is_rejected_clearly(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            r"patch\.services_add\[0\] must include non-empty name and endpoint",
        ):
            merge_registration_patch(
                current_registration(),
                {"services_add": [{}]},
                network=RADIUS_TESTNET,
            )

    def test_empty_patch_arrays_are_noops(self) -> None:
        merged, diff = merge_registration_patch(
            current_registration(),
            {
                "services_add": [],
                "services_update": [],
                "aliases_add": [],
                "externalRegistrations_add": [],
                "external_registrations_add": [],
            },
            network=RADIUS_TESTNET,
        )

        self.assertEqual(merged["services"], current_registration()["services"])
        self.assertEqual(diff["servicesAdded"], [])
        self.assertEqual(diff["aliasesAdded"], [])
        self.assertEqual(diff["externalRegistrationsAdded"], [])

    def test_ans_patch_adds_expected_metadata_and_preserves_existing(self) -> None:
        merged, diff = merge_registration_patch(
            current_registration(),
            ans_patch(),
            network=RADIUS_TESTNET,
        )

        service_endpoints = {
            service["endpoint"] for service in merged["services"]
        }
        self.assertIn(RAILWAY_WEB, service_endpoints)
        self.assertIn(RAILWAY_A2A, service_endpoints)
        self.assertIn(RAILWAY_DID, service_endpoints)
        self.assertIn("https://agent0.72344.xyz/", service_endpoints)
        self.assertIn("https://agent0.72344.xyz/a2a", service_endpoints)
        self.assertIn("did:web:agent0.72344.xyz", service_endpoints)
        self.assertIn("ans://v1.0.0.agent0.72344.xyz", service_endpoints)

        aliases = {(item["type"], item["endpoint"]) for item in merged["aliases"]}
        self.assertIn(("web", "https://agent0.72344.xyz/"), aliases)
        self.assertIn(("a2a", "https://agent0.72344.xyz/a2a"), aliases)
        self.assertIn(("did", "did:web:agent0.72344.xyz"), aliases)
        self.assertIn(("ans", "ans://v1.0.0.agent0.72344.xyz"), aliases)

        external = merged["externalRegistrations"][0]
        self.assertEqual(external["registry"], "godaddy-ans")
        self.assertEqual(
            external["registryId"], "b9bfc282-95e6-4fc4-8ba6-e1c380b6d29f"
        )
        self.assertEqual(len(diff["servicesAdded"]), 4)
        self.assertEqual(len(diff["aliasesAdded"]), 4)
        self.assertEqual(len(diff["externalRegistrationsAdded"]), 1)

    def test_ans_patch_is_idempotent(self) -> None:
        first, _ = merge_registration_patch(
            current_registration(),
            ans_patch(),
            network=RADIUS_TESTNET,
        )
        second, diff = merge_registration_patch(first, ans_patch(), network=RADIUS_TESTNET)

        self.assertEqual(first, second)
        self.assertEqual(diff["servicesAdded"], [])
        self.assertEqual(diff["aliasesAdded"], [])
        self.assertEqual(diff["externalRegistrationsAdded"], [])
        self.assertEqual(diff["servicesUpdated"], [])
        self.assertEqual(diff["aliasesUpdated"], [])
        self.assertEqual(diff["externalRegistrationsUpdated"], [])

    def test_list_registrations_caps_limit_and_continues_on_malformed_items(self) -> None:
        encoded = encode_agent_uri(current_registration())

        def fake_call(_config, signature, *args):
            if signature == "totalSupply()(uint256)":
                return "0x65"
            agent_id = int(args[0])
            if agent_id == 1:
                return "not-a-data-uri"
            return encoded

        with patch("erc8004_registry.client._call", side_effect=fake_call):
            result = list_registrations(
                network="testnet",
                start_id=0,
                limit=101,
                include_decoded=True,
            )

        self.assertEqual(result["limit"], 100)
        self.assertEqual(len(result["items"]), 100)
        self.assertIn("registration", result["items"][0])
        self.assertIn("error", result["items"][1])
        self.assertEqual(result["items"][2]["registration"]["name"], "Agent 0 (Hermes)")


if __name__ == "__main__":
    unittest.main()
