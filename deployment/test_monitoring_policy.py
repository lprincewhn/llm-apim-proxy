import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from configure_apim import build_policy, configure


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

    def test_secret_and_budget_are_forwarded_without_client_credentials(self):
        root = ET.fromstring(build_policy())
        self.assertEqual(root.find(".//set-header[@name='X-Executor-Key']/value").text,
                         "{{executor-key}}")
        for name in ("Authorization", "api-key", "Ocp-Apim-Subscription-Key"):
            self.assertEqual(root.find(f".//set-header[@name='{name}']").get("exists-action"), "delete")
        self.assertIn("X-Remaining-Budget-Ms", build_policy())

    def run_configure(self, initialize_route=False):
        writes = []

        def arm(method, path, body=None, version=None):
            if method == "GET":
                return {"properties": {"configuration": {"ingress": {"fqdn": "executor.invalid"}}}}
            writes.append((path, body))
            return {}

        with patch("configure_apim.arm", side_effect=arm), patch(
            "configure_apim.container_secrets", return_value={"executor-key": "test-placeholder"}
        ), patch.object(Path, "write_text"), patch("builtins.print"):
            configure(initialize_route=initialize_route)
        return writes

    def test_default_configuration_preserves_route_and_omits_demo_api(self):
        paths = [path for path, _ in self.run_configure()]
        self.assertFalse(any("/namedValues/chat-route" in path for path in paths))
        self.assertFalse(any("/apis/validation" in path for path in paths))
        self.assertTrue(any("/apis/llm/policies/policy" in path for path in paths))

    def test_route_initialization_is_explicit(self):
        writes = self.run_configure(initialize_route=True)
        self.assertEqual(sum(path.endswith("/namedValues/chat-route") for path, _ in writes), 1)

    def test_generated_snapshot_matches_source(self):
        self.assertEqual(Path(__file__).with_name("llm-policy.xml").read_text(), build_policy())
