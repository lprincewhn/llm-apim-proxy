import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
import runpy
from unittest.mock import patch

from use_monitoring_routing import single_attempt_policy


class MonitoringPolicyTests(unittest.TestCase):
    def test_fresh_config_matches_single_attempt_migration(self):
        policies = {}

        def arm(method, path, body=None, version=None):
            if method == "GET":
                return {"properties": {"configuration": {"ingress": {"fqdn": "executor.invalid"}}}}
            if "/apis/" in path and path.endswith("/policies/policy"):
                policies[path.split("/apis/")[1].split("/")[0]] = ET.fromstring(
                    body["properties"]["value"]
                )
            return {}

        with patch("azure.arm", side_effect=arm), patch(
            "azure.container_secrets", return_value={"executor-key": "test-placeholder"}
        ), patch.object(Path, "write_text"), patch("builtins.print"):
            runpy.run_path(str(Path(__file__).with_name("configure_apim.py")))
        self.assertIsNone(policies["llm"].find(".//retry"))
        self.assertEqual(len(policies["llm"].find("backend")), 1)
        self.assertEqual(policies["llm"].find("backend")[0].tag, "forward-request")
        self.assertIsNotNone(policies["validation"].find(".//retry"))

    def test_removes_retry_without_dropping_forwarding(self):
        result = ET.fromstring(single_attempt_policy(
            '<policies><inbound/><backend><retry count="1">'
            '<set-variable name="attempt" value="@(1)"/>'
            '<set-variable name="selected" value="old"/>'
            '<set-header name="X-Executor-Key"><value>{{executor-key}}</value></set-header>'
            '<forward-request timeout="5"/>'
            '</retry></backend><outbound/></policies>'
        ))
        self.assertIsNone(result.find(".//retry"))
        self.assertEqual(len(result.findall(".//forward-request")), 1)
        self.assertEqual(len(result.find("backend")), 1)
        self.assertEqual(result.find(".//set-header/value").text, "{{executor-key}}")
        self.assertNotIn("attempt", result.find(".//set-variable[@name='selected']").get("value"))

    def test_idempotent(self):
        xml = ('<policies><inbound/><backend><set-variable name="selected" value="old"/>'
               '<forward-request/></backend></policies>')
        result = single_attempt_policy(xml)
        self.assertEqual(single_attempt_policy(result), result)

    def test_decodes_management_xml_format(self):
        xml = ('<policies><inbound><set-variable name="purpose" value="@(&amp;quot;generate&amp;quot;)"/>'
               '</inbound><backend><set-variable name="selected" value="old"/>'
               '<forward-request/></backend></policies>')
        root = ET.fromstring(single_attempt_policy(xml, encoded=True))
        self.assertEqual(root.find(".//set-variable[@name='purpose']").get("value"), '@("generate")')

    def test_unknown_layout_fails_closed(self):
        for xml in (
            "<policies/>",
            "<policies><backend/></policies>",
            "<policies><backend><retry/><retry/></backend></policies>",
        ):
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                single_attempt_policy(xml)
