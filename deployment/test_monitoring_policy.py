import unittest
import copy
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from configure_apim import build_policy, configure
from backends import API_VERSION, MODEL_IDENTITY_CLIENT_ID, MODEL_IDENTITY_ID, backend_url, load_backends


class MonitoringPolicyTests(unittest.TestCase):
    def test_policy_forwards_once_to_current_primary(self):
        root = ET.fromstring(build_policy())
        self.assertIsNone(root.find(".//retry"))
        self.assertEqual(len(root.find("backend")), 1)
        self.assertEqual(root.find("backend")[0].tag, "forward-request")
        selected = root.find(".//set-variable[@name='selected']").get("value")
        self.assertIn('["primary"]', selected)
        self.assertNotIn("attempt", selected)
        self.assertNotIn("/fault/", build_policy())
        self.assertEqual(root.find(".//set-header[@name='X-Lab-Attempts']/value").text, "1")

    def test_model_identity_and_budget_without_executor(self):
        root = ET.fromstring(build_policy())
        auth = root.find(".//authentication-managed-identity")
        self.assertEqual(auth.get("client-id"), MODEL_IDENTITY_CLIENT_ID)
        self.assertEqual(auth.get("resource"), "https://cognitiveservices.azure.com")
        for name in ("Authorization", "api-key", "Ocp-Apim-Subscription-Key", "X-Executor-Key"):
            self.assertEqual(root.find(f".//set-header[@name='{name}']").get("exists-action"), "delete")
        self.assertIn("X-Remaining-Budget-Ms", build_policy())
        self.assertNotIn("{{executor-", build_policy())
        self.assertEqual(
            root.find(".//set-query-parameter[@name='api-version']/value").text, API_VERSION
        )
        for name, backend in load_backends().items():
            self.assertIsNotNone(root.find(f".//set-backend-service[@backend-id='llm-{name}']"))
            self.assertIn("/openai/deployments/" + backend["deployment"], build_policy())
            self.assertIn(backend["endpoint"], backend_url(backend))

    def run_configure(self, initialize_route=False):
        writes = []

        def arm(method, path, body=None, version=None):
            if method == "GET":
                return {"properties": {"configuration": {"ingress": {"fqdn": "executor.invalid"}}}}
            writes.append((path, body))
            return {}

        with patch("configure_apim.arm", side_effect=arm), patch.object(Path, "write_text"), patch("builtins.print"):
            configure(initialize_route=initialize_route)
        return writes

    def test_default_configuration_preserves_route_and_omits_demo_api(self):
        paths = [path for path, _ in self.run_configure()]
        self.assertFalse(any("/namedValues/chat-route" in path for path in paths))
        self.assertFalse(any("/apis/validation" in path for path in paths))
        self.assertTrue(any("/apis/llm/policies/policy" in path for path in paths))
        self.assertFalse(any("executor-" in path for path in paths))

    def test_configuration_attaches_existing_model_identity(self):
        writes = self.run_configure()
        identity_updates = [body["identity"] for _, body in writes if "identity" in body]
        self.assertEqual(identity_updates, [{
            "type": "UserAssigned", "userAssignedIdentities": {MODEL_IDENTITY_ID: {}},
        }])

    def test_route_initialization_is_explicit(self):
        writes = self.run_configure(initialize_route=True)
        self.assertEqual(sum(path.endswith("/namedValues/chat-route") for path, _ in writes), 1)

    def test_generated_snapshot_matches_source(self):
        self.assertEqual(Path(__file__).with_name("llm-policy.xml").read_text(), build_policy())

    def test_backend_config_rejects_untrusted_origins_and_paths(self):
        original = load_backends()
        for field, value in (
            ("endpoint", "https://svhw2-swedencentral.openai.azure.com.attacker.invalid"),
            ("endpoint", "https://svhw2-swedencentral.openai.azure.com/openai"),
            ("endpoint", "http://svhw2-swedencentral.openai.azure.com"),
            ("deployment", "../other"),
        ):
            config = copy.deepcopy(original)
            config["sweden"][field] = value
            with self.subTest(field=field, value=value), patch.object(
                Path, "read_text", return_value=json.dumps({"backends": config})
            ), self.assertRaises(ValueError):
                load_backends()
