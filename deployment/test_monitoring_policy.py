import unittest
import copy
import json
import io
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from configure_apim import build_policy, configure, native_operations
from backends import MODEL_IDENTITY_CLIENT_ID, MODEL_IDENTITY_ID, backend_url, load_backends
from smoke import read_stream


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

    def test_model_identity_without_executor(self):
        root = ET.fromstring(build_policy())
        auth = root.find(".//authentication-managed-identity")
        self.assertEqual(auth.get("client-id"), MODEL_IDENTITY_CLIENT_ID)
        self.assertEqual(auth.get("resource"), "https://cognitiveservices.azure.com")
        for name in ("Authorization", "api-key", "Ocp-Apim-Subscription-Key", "X-Executor-Key"):
            self.assertEqual(root.find(f".//set-header[@name='{name}']").get("exists-action"), "delete")
        self.assertNotIn("{{executor-", build_policy())
        for name, backend in load_backends().items():
            self.assertIsNotNone(root.find(f".//set-backend-service[@backend-id='llm-{name}']"))
            self.assertIn("/openai/deployments/" + backend["deployment"], build_policy())
            self.assertIn(backend["endpoint"], backend_url(backend))

    def run_configure(self, initialize_route=False, identity=None):
        writes = []

        def arm(method, path, body=None, version=None, headers=None):
            if method == "GET":
                if path.endswith("/operations"):
                    return {"value": [{"name": name} for name in (
                        "intent", "rewrite", "generate", "embedding", "native-sweden",
                    )]}
                return {"identity": identity or {}, "properties": {}}
            writes.append((path, body if method != "DELETE" else {"deleted": True}))
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

    def test_configuration_does_not_patch_already_attached_identity(self):
        writes = self.run_configure(identity={
            "type": "SystemAssigned, UserAssigned",
            "userAssignedIdentities": {MODEL_IDENTITY_ID: {"principalId": "existing"}},
        })
        self.assertFalse(any("identity" in body for _, body in writes))

    def test_route_initialization_is_explicit(self):
        writes = self.run_configure(initialize_route=True)
        self.assertEqual(sum(path.endswith("/namedValues/chat-route") for path, _ in writes), 1)

    def test_generated_snapshot_matches_source(self):
        self.assertEqual(Path(__file__).with_name("llm-policy.xml").read_text(), build_policy())

    def test_native_body_query_and_stream_passthrough(self):
        root = ET.fromstring(build_policy())
        self.assertIsNone(root.find(".//set-body"))
        self.assertNotIn("context.Request.Body", build_policy())
        self.assertIsNone(root.find(".//set-query-parameter[@name='api-version']"))
        self.assertTrue(all(node.get("copy-unmatched-params") == "true"
                            for node in root.findall(".//rewrite-uri")))
        forward = root.find(".//forward-request")
        self.assertEqual(forward.get("buffer-response"), "false")
        self.assertEqual(forward.get("buffer-request-body"), "false")
        self.assertEqual(forward.get("timeout"), "120")
        self.assertIsNone(root.find(".//set-variable[@name='budget']"))
        self.assertEqual(root.find(".//set-query-parameter[@name='subscription-key']").get(
            "exists-action"), "delete")

    def test_native_api_replaces_custom_operations(self):
        writes = self.run_configure()
        api = next(body["properties"] for path, body in writes if path.endswith("/apis/llm"))
        self.assertEqual(api["path"], "openai")
        self.assertEqual(api["subscriptionKeyParameterNames"]["header"], "api-key")
        operations = native_operations()
        self.assertEqual(operations["native-eastus2"]["urlTemplate"],
                         "/deployments/gpt-5.1/chat/completions")
        self.assertEqual(operations["native-embedding"]["urlTemplate"],
                         "/deployments/text-embedding-3-small/embeddings")
        removed = [path.rsplit("/", 1)[1] for path, body in writes if body.get("deleted")]
        self.assertEqual(removed, ["intent", "rewrite", "generate", "embedding"])
        self.assertNotIn("native-sweden", removed)

    def test_chat_primary_must_be_enabled(self):
        root = ET.fromstring(build_policy())
        guard = root.find(".//choose/when").get("condition")
        self.assertIn('["enabled"]', guard)
        self.assertIn('["primary"]', guard)
        self.assertIn(".Any(item => (string)item ==", guard)

    def test_stream_parser_preserves_chunks_and_done(self):
        result = read_stream(io.BytesIO(
            b'data: {"choices":[]}\n\n'
            b'data: {"choices":[{"delta":{"content":"O"},"finish_reason":null}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"K"},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n'
        ))
        self.assertEqual(result, {
            "content": "OK", "finish_reason": "stop", "done": True, "chunks": 3,
        })
        self.assertFalse(read_stream(io.BytesIO(b'data: {"choices":[]}\n'))["done"])

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
