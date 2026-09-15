"""Offline contract tests of the generated WDL, with deterministic HTTP fixtures.

The small expression/action interpreter covers only this workflow's WDL subset;
it is not Azure schema validation or a substitute for a disabled-cloud test run.
"""

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("monitoring_deploy", Path(__file__).with_name("deploy.py"))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
NOW = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)


def timestamp(minutes=0):
    return (NOW + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def event(backend="eastus2"):
    return {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {
            "essentials": {
                "alertRule": deploy.ALERT_NAMES[0], "monitorCondition": "Fired",
                "alertTargetIDs": [deploy.WORKSPACE], "firedDateTime": timestamp(-1),
            },
            "alertContext": {
                "condition": {"allOf": [{"dimensions": [{"name": "Backend", "value": backend}]}]},
            },
        },
    }


def check_schema(value, schema):
    types = {
        "object": dict, "array": list, "string": str,
        "integer": int,
    }
    expected = schema.get("type")
    if expected and (not isinstance(value, types[expected])
                     or (expected == "integer" and isinstance(value, bool))):
        raise ValueError("schema type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("schema enum")
    if isinstance(value, dict):
        if not all(key in value for key in schema.get("required", [])):
            raise ValueError("schema required")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                check_schema(value[key], child)
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            raise ValueError("schema array size")
        if schema.get("uniqueItems") and len({json.dumps(x) for x in value}) != len(value):
            raise ValueError("schema uniqueness")
        for item in value:
            check_schema(item, schema.get("items", {}))
    if "minimum" in schema and value < schema["minimum"]:
        raise ValueError("schema minimum")


class StopRun(Exception):
    def __init__(self, status):
        self.status = status


class WdlFixture:
    """Execute the actual generated guard expressions, not a parallel policy."""

    def __init__(self, payload=None, route=None, switch=True):
        self.payload = payload if payload is not None else event()
        self.route = copy.deepcopy(route or {
            "primary": "eastus2", "enabled": ["eastus2", "sweden"],
            "version": 3, "custom": {"preserve": [1, 2]},
        })
        self.properties = {
            "displayName": "chat-route", "secret": False, "tags": ["keep"],
            "value": json.dumps(self.route),
        }
        self.parameters = {
            "backendUrls": deploy.backend_urls(),
            "workspaceId": deploy.WORKSPACE, "switchEnabled": switch,
        }
        healthy = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
        self.probes = [(200, healthy), (200, copy.deepcopy(healthy))]
        self.responses = {}
        self.visited = []
        self.writes = []
        self.probe_requests = []
        self.conflict = False
        self.deny_read = False
        self.deny_write = False
        self.etag = '"route-version-3"'
        self.now = NOW
        self.expire_on_probe = False

    def function(self, name, args):
        def set_property(obj, key, value):
            result = copy.deepcopy(obj)
            result[key] = value
            return result

        functions = {
            "triggerBody": lambda: self.payload,
            "body": lambda name: self.responses[name]["body"],
            "outputs": lambda name: self.responses[name].get("compose", self.responses[name]),
            "parameters": lambda name: self.parameters[name],
            "equals": lambda a, b: a == b,
            "not": lambda a: not a,
            "and": lambda *items: all(items),
            "or": lambda *items: any(items),
            "if": lambda condition, yes, no: yes if condition else no,
            "empty": lambda value: value is None or value == "" or value == [] or value == {},
            "contains": lambda container, value: value in container,
            "coalesce": lambda *items: next((x for x in items if x is not None), None),
            "createArray": lambda *items: list(items),
            "concat": lambda *items: "".join(items),
            "toLower": lambda value: value.lower(),
            "utcNow": lambda: self.now.isoformat().replace("+00:00", "Z"),
            "addMinutes": lambda value, minutes: (
                datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(minutes=minutes)
            ).isoformat(),
            "ticks": lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp(),
            "greaterOrEquals": lambda a, b: a >= b,
            "lessOrEquals": lambda a, b: a <= b,
            "json": json.loads,
            "string": lambda value: json.dumps(value, separators=(",", ":"))
            if isinstance(value, (dict, list)) else str(value),
            "add": lambda a, b: a + b,
            "setProperty": set_property,
        }
        return functions[name](*args)

    def expression(self, value):
        tokens = re.findall(r"'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*|-?\d+|[(),?\[\]]", value[1:])
        index = 0

        def take():
            nonlocal index
            token = tokens[index]
            index += 1
            return token

        def parse_value():
            nonlocal index
            token = take()
            if token.startswith("'"):
                result = token[1:-1].replace("''", "'")
            elif token.lstrip("-").isdigit():
                result = int(token)
            elif token in ("true", "false", "null"):
                result = {"true": True, "false": False, "null": None}[token]
            else:
                if take() != "(":
                    raise ValueError("Expected function arguments")
                args = []
                if tokens[index] != ")":
                    args.append(parse_value())
                    while tokens[index] == ",":
                        take()
                        args.append(parse_value())
                if take() != ")":
                    raise ValueError("Expected closing parenthesis")
                result = self.function(token, args)
            while index < len(tokens) and tokens[index] in ("?", "["):
                optional = tokens[index] == "?"
                if optional:
                    take()
                if take() != "[":
                    raise ValueError("Expected property selector")
                key = parse_value()
                if take() != "]":
                    raise ValueError("Expected closing bracket")
                if optional:
                    result = result.get(key) if isinstance(result, dict) else None
                else:
                    result = result[key]
            return result

        result = parse_value()
        if index != len(tokens):
            raise ValueError("Expression has unconsumed tokens")
        return result

    def resolve(self, value):
        if isinstance(value, str) and value.startswith("@"):
            return self.expression(value)
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    def actions(self, actions):
        for name, action in actions.items():
            for predecessor, statuses in action["runAfter"].items():
                if predecessor not in self.visited or statuses != ["Succeeded"]:
                    raise ValueError("Unsatisfied success-only dependency")
            self.visited.append(name)
            kind = action["type"]
            if kind == "If":
                child = action["actions"] if self.expression(action["expression"]) else action["else"]["actions"]
                self.actions(child)
                self.responses[name] = {}
            elif kind == "Terminate":
                raise StopRun(action["inputs"]["runStatus"])
            elif kind == "ParseJson":
                value = self.resolve(action["inputs"]["content"])
                if isinstance(value, str):
                    value = json.loads(value)
                check_schema(value, action["inputs"]["schema"])
                self.responses[name] = {"body": value}
            elif kind == "Compose":
                self.responses[name] = {"compose": self.resolve(action["inputs"])}
            elif kind == "Http":
                inputs = self.resolve(action["inputs"])
                if inputs["uri"].startswith("https://management.azure.com"):
                    if inputs["uri"] != deploy.route_uri():
                        raise ValueError("Unexpected management resource")
                    if inputs["authentication"] != {
                        "type": "ManagedServiceIdentity",
                        "audience": "https://management.azure.com/",
                    }:
                        raise ValueError("ARM must use the system identity")
                    if inputs["method"] == "GET":
                        if self.deny_read:
                            raise ValueError("403")
                        self.responses[name] = {
                            "statusCode": 200, "headers": {"ETag": self.etag},
                            "body": {"properties": copy.deepcopy(self.properties)},
                        }
                    else:
                        if self.deny_write or self.conflict:
                            raise ValueError("403 or 412")
                        if inputs["headers"]["If-Match"] != self.etag:
                            raise ValueError("No exact ETag")
                        self.writes.append(inputs)
                        self.properties = inputs["body"]["properties"]
                        self.responses[name] = {"statusCode": 200}
                else:
                    if inputs["uri"] not in deploy.backend_urls().values():
                        raise ValueError("Unexpected model resource")
                    if inputs["authentication"] != {
                        "type": "ManagedServiceIdentity",
                        "audience": "https://cognitiveservices.azure.com",
                        "identity": deploy.MODEL_IDENTITY_ID,
                    }:
                        raise ValueError("Model must use the shared user identity")
                    self.probe_requests.append(inputs)
                    if self.expire_on_probe:
                        self.now += timedelta(minutes=6)
                    status, body = self.probes.pop(0)
                    if status >= 400:
                        raise ValueError("HTTP failed")
                    self.responses[name] = {"statusCode": status, "body": copy.deepcopy(body)}
            else:
                raise ValueError("Unsupported test action")

    def run(self):
        try:
            self.actions(deploy.workflow_definition()["actions"])
        except StopRun as stop:
            return stop.status
        except (ValueError, KeyError, TypeError, IndexError):
            return "Failed"
        return "Succeeded"


class WorkflowTests(unittest.TestCase):
    def test_switch_both_directions_and_preserve_unrelated_route_fields(self):
        for primary, backup in (("eastus2", "sweden"), ("sweden", "eastus2")):
            with self.subTest(primary=primary):
                fixture = WdlFixture(payload=event(primary))
                fixture.properties["value"] = json.dumps({**fixture.route, "primary": primary})
                self.assertEqual(fixture.run(), "Succeeded")
                self.assertEqual(len(fixture.writes), 1)
                route = json.loads(fixture.properties["value"])
                self.assertEqual(route["primary"], backup)
                self.assertEqual(route["enabled"], [backup])
                self.assertEqual(route["version"], 4)
                self.assertEqual(route["lastSwitchAt"], timestamp())
                self.assertEqual(route["custom"], {"preserve": [1, 2]})
                self.assertIn(primary, route["reason"])
                self.assertEqual(fixture.properties["tags"], ["keep"])
                self.assertEqual(fixture.visited.count("Probe_one"), 1)
                self.assertEqual(fixture.visited.count("Probe_two"), 1)
                self.assertEqual(
                    [request["uri"] for request in fixture.probe_requests],
                    [deploy.backend_urls()[backup]] * 2,
                )

    def test_reject_untrusted_alert_contracts_before_arm_read(self):
        mutations = [
            lambda p: p.update(schemaId="not-common"),
            lambda p: p["data"]["essentials"].update(alertRule="other-rule"),
            lambda p: p["data"]["essentials"].update(alertTargetIDs=[deploy.APIM]),
            lambda p: p["data"]["essentials"].update(alertTargetIDs=[deploy.WORKSPACE, deploy.APIM]),
            lambda p: p["data"]["alertContext"]["condition"]["allOf"][0].update(dimensions=[]),
            lambda p: p["data"]["alertContext"]["condition"]["allOf"][0]["dimensions"][0].update(
                name="Region",
            ),
            lambda p: p["data"]["alertContext"]["condition"]["allOf"][0]["dimensions"][0].update(
                value="embedding",
            ),
        ]
        for mutate in mutations:
            fixture = WdlFixture()
            mutate(fixture.payload)
            self.assertEqual(fixture.run(), "Failed")
            self.assertNotIn("Read_route", fixture.visited)
            self.assertFalse(fixture.writes)

    def test_resolved_stale_future_alerts_do_not_read_or_write(self):
        for override in (
            {"monitorCondition": "Resolved"},
            {"firedDateTime": timestamp(-11)},
            {"firedDateTime": timestamp(1)},
        ):
            fixture = WdlFixture()
            fixture.payload["data"]["essentials"].update(override)
            self.assertEqual(fixture.run(), "Cancelled")
            self.assertNotIn("Read_route", fixture.visited)
            self.assertFalse(fixture.writes)

    def test_invalid_timestamp_fails_closed(self):
        fixture = WdlFixture()
        fixture.payload["data"]["essentials"]["firedDateTime"] = "not-a-time"
        self.assertEqual(fixture.run(), "Failed")
        self.assertFalse(fixture.writes)

    def test_current_primary_backup_enabled_and_cooldown_guards(self):
        routes = [
            {"primary": "sweden"},
            {"enabled": ["eastus2"]},
            {"enabled": ["sweden"]},
            {"lastSwitchAt": timestamp(-9)},
            {"lastSwitchAt": timestamp(1)},
        ]
        for update in routes:
            fixture = WdlFixture()
            fixture.properties["value"] = json.dumps({**fixture.route, **update})
            self.assertEqual(fixture.run(), "Cancelled")
            self.assertNotIn("Probe_one", fixture.visited)
            self.assertFalse(fixture.writes)

    def test_quarantine_prevents_automatic_failback(self):
        fixture = WdlFixture()
        fixture.route.update(primary="sweden", enabled=["sweden"], lastSwitchAt=timestamp(-20))
        fixture.properties["value"] = json.dumps(fixture.route)
        fixture.payload = event("sweden")
        self.assertEqual(fixture.run(), "Cancelled")
        self.assertFalse(fixture.writes)

    def test_real_gpt_contract_and_status_twice(self):
        bad = [
            (401, {}),
            (403, {}),
            (500, {}),
            (429, {}),
            (202, {}),
            (200, {"healthy": True}),
            (200, {"choices": []}),
            (200, {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}),
            (200, {"choices": [{"message": {"content": "OK"}, "finish_reason": "length"}]}),
            (200, {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}),
            (200, {"choices": [{"message": {"content": "NOT OK"}, "finish_reason": "stop"}]}),
            (200, "<html>bad gateway</html>"),
        ]
        for position in (0, 1):
            for response in bad:
                with self.subTest(position=position, response=response):
                    fixture = WdlFixture()
                    fixture.probes[position] = response
                    self.assertEqual(fixture.run(), "Failed")
                    self.assertFalse(fixture.writes)
                    if position == 0:
                        self.assertNotIn("Probe_two", fixture.visited)

    def test_role_missing_etag_missing_conflicts_and_invalid_routes_fail_closed(self):
        for attr, value in (("deny_read", True), ("deny_write", True), ("conflict", True),
                            ("etag", None), ("etag", "*")):
            fixture = WdlFixture()
            setattr(fixture, attr, value)
            self.assertEqual(fixture.run(), "Failed")
            self.assertFalse(fixture.writes)
        for update in ({"primary": "embedding"}, {"version": "3"}, {"lastSwitchAt": "nonsense"}):
            fixture = WdlFixture()
            fixture.properties["value"] = json.dumps({**fixture.route, **update})
            self.assertEqual(fixture.run(), "Failed")
            self.assertFalse(fixture.writes)

    def test_dry_run_checks_models_without_mutation(self):
        fixture = WdlFixture(switch=False)
        self.assertEqual(fixture.run(), "Cancelled")
        self.assertIn("Validate_probe_two", fixture.visited)
        self.assertIn("DryRunNoWrite", fixture.visited)
        self.assertFalse(fixture.writes)

    def test_recheck_staleness_after_slow_probes(self):
        fixture = WdlFixture()
        fixture.expire_on_probe = True
        self.assertEqual(fixture.run(), "Cancelled")
        self.assertIn("ExpiredBeforeWrite", fixture.visited)
        self.assertFalse(fixture.writes)

    def test_access_check_is_unchanged_value_write_not_health_fallback(self):
        fixture = WdlFixture(payload={"schemaId": "monitoring.accessCheck.v1"})
        before = copy.deepcopy(fixture.properties)
        self.assertEqual(fixture.run(), "Succeeded")
        self.assertEqual(fixture.properties, before)
        self.assertEqual(len(fixture.writes), 1)
        self.assertNotIn("Probe_one", fixture.visited)
        fixture = WdlFixture(payload={"schemaId": "monitoring.accessCheck.v1"})
        fixture.deny_write = True
        self.assertEqual(fixture.run(), "Failed")

    def test_maintenance_probes_both_targets_without_reading_or_changing_quarantine(self):
        for backend in deploy.BACKENDS:
            fixture = WdlFixture(
                payload={
                    "schemaId": "monitoring.healthCheck.v1", "backend": backend,
                    "backendUrls": {backend: "https://attacker.example"},
                },
                route={"primary": "sweden", "enabled": ["sweden"], "version": 2},
                switch=False,
            )
            before = copy.deepcopy(fixture.properties)
            self.assertEqual(fixture.run(), "Succeeded")
            self.assertEqual(fixture.properties, before)
            self.assertFalse(fixture.writes)
            self.assertEqual(
                [request["uri"] for request in fixture.probe_requests],
                [deploy.backend_urls()[backend]] * 2,
            )
            for name in ("Read_route", "Read_access_route", "Read_committed_route",
                         "Write_route", "Verify_access_write", "Probe_one", "Probe_two"):
                self.assertNotIn(name, fixture.visited)

    def test_maintenance_rejects_invalid_targets_and_enabled_switch_before_probes(self):
        for backend, switch in (
            ("embedding", False), ("https://attacker.example", False),
            (None, False), ("eastus2", True),
        ):
            fixture = WdlFixture(
                payload={"schemaId": "monitoring.healthCheck.v1", "backend": backend},
                switch=switch,
            )
            self.assertEqual(fixture.run(), "Failed")
            self.assertFalse(fixture.probe_requests)
            self.assertFalse(fixture.writes)
            self.assertNotIn("Read_route", fixture.visited)

    def test_maintenance_requires_two_real_ok_stop_responses(self):
        for position in (0, 1):
            for response in (
                (401, {}), (403, {}), (429, {}), (500, {}), (202, {}),
                (200, {"choices": [{"message": {"content": "OK"}, "finish_reason": "length"}]}),
                (200, {"choices": [{"message": {"content": "NOT OK"}, "finish_reason": "stop"}]}),
            ):
                fixture = WdlFixture(
                    payload={"schemaId": "monitoring.healthCheck.v1", "backend": "eastus2"},
                    switch=False,
                )
                fixture.probes[position] = response
                self.assertEqual(fixture.run(), "Failed")
                self.assertFalse(fixture.writes)
                self.assertNotIn("Read_route", fixture.visited)
                if position == 0:
                    self.assertNotIn("Health_probe_two", fixture.visited)


class TemplateTests(unittest.TestCase):
    def test_disabled_deployment_no_secrets_and_shared_model_identity(self):
        template = deploy.deployment_template()
        json.dumps(template)
        workflow, group, *rules = template["resources"]
        self.assertEqual(template["parameters"], {})
        self.assertEqual(workflow["identity"], {
            "type": "SystemAssigned, UserAssigned",
            "userAssignedIdentities": {deploy.MODEL_IDENTITY_ID: {}},
        })
        self.assertEqual(
            workflow["properties"]["parameters"]["backendUrls"]["value"],
            deploy.backend_urls(),
        )
        self.assertEqual(
            workflow["properties"]["definition"]["parameters"]["backendUrls"],
            {"type": "Object", "defaultValue": deploy.backend_urls()},
        )
        self.assertIs(workflow["properties"]["parameters"]["switchEnabled"]["value"], False)
        self.assertEqual(template["outputs"], {})
        self.assertEqual(len(rules), 2)
        for rule in rules:
            self.assertIs(rule["properties"]["enabled"], False)
            self.assertEqual(rule["properties"]["evaluationFrequency"], "PT1M")
            self.assertEqual(rule["properties"]["windowSize"], "PT5M")
            self.assertEqual(rule["properties"]["scopes"], [deploy.WORKSPACE])
            criterion = rule["properties"]["criteria"]["allOf"][0]
            self.assertEqual(criterion["metricMeasureColumn"], "ViolationCount")
            self.assertEqual(criterion["dimensions"][0]["values"], ["eastus2", "sweden"])
        receiver = group["properties"]["logicAppReceivers"][0]
        self.assertEqual(receiver["resourceId"], deploy.WORKFLOW)
        self.assertTrue(receiver["useCommonAlertSchema"])
        self.assertTrue(receiver["callbackUrl"].startswith("[listCallbackUrl("))
        self.assertNotIn("roleAssignments", json.dumps(template))
        self.assertNotIn("clientSecret", json.dumps(template))
        for obsolete in ("executorKey", "executorBaseUri", "/execute/", "X-Budget-Ms",
                         "X-Idle-Ms", "secureString", "SecureString", "azurecontainerapps.io"):
            self.assertNotIn(obsolete, json.dumps(template))
        source = Path(deploy.__file__).read_text()
        for obsolete in ("container_secrets", "executorKey", "executorBaseUri", "APP"):
            self.assertNotIn(obsolete, source)

    def test_query_is_filtered_dimensioned_and_sample_gated(self):
        for kind in ("latency", "errors"):
            query = deploy.alert_query(kind)
            self.assertIn("ApiManagementGatewayLogs", query)
            self.assertIn("BackendUrl", query)
            self.assertIn("ApiId == 'llm'", query)
            self.assertIn("Samples >= 5", query)
            self.assertIn("by Backend", query)
            self.assertIn(deploy.APIM, query)
            self.assertNotIn("percentile", query)
        self.assertIn("AverageBackendMs >= 3200", deploy.alert_query("latency"))
        self.assertIn("ErrorRatePct >= 20.0", deploy.alert_query("errors"))
        self.assertIn("toint(ResponseCode)", deploy.alert_query("errors"))
        self.assertIn("toint(BackendResponseCode)", deploy.alert_query("errors"))
        self.assertIn("GatewayStatus == 429", deploy.alert_query("errors"))
        self.assertIn("BackendStatus == 429", deploy.alert_query("errors"))
        self.assertIn("between (500 .. 599)", deploy.alert_query("errors"))
        for kind in ("latency", "errors"):
            query = deploy.alert_query(kind)
            self.assertIn("GatewayStatus !in (401, 403) and BackendStatus !in (401, 403)", query)
            self.assertLess(query.index("!in (401, 403)"), query.index("| summarize"))
        with self.assertRaises(ValueError):
            deploy.alert_query("bad")

    def test_query_matches_exact_direct_origin_and_deployment_without_query(self):
        for kind in ("latency", "errors"):
            query = deploy.alert_query(kind)
            self.assertIn('tostring(split(tostring(BackendUrl), "?")[0])', query)
            matches = re.findall(r"BackendRequestUrl == '([^']+)', '([^']+)'", query)
            self.assertEqual(dict(matches), {
                url.split("?", 1)[0]: name for name, url in deploy.backend_urls().items()
            })
            self.assertNotIn("/execute/", query)
            self.assertNotIn("extract(", query)
            self.assertNotIn("BackendId", query)
            for name, url in deploy.backend_urls().items():
                def classify(request_url):
                    return dict(matches).get(request_url.split("?", 1)[0], "")

                origin, path = url.split("/openai/", 1)
                self.assertEqual(classify(url), name)
                self.assertEqual(classify(url.split("?", 1)[0] + "?api-version=other"), name)
                for untrusted in (
                    origin + "/execute/" + name,
                    "https://old.azurecontainerapps.io/execute/" + name,
                    origin + ".attacker.example/openai/" + path,
                    "https://attacker.example/openai/" + path,
                    origin + "/openai/deployments/other/chat/completions",
                    url.split("?", 1)[0] + "/extra",
                    url.replace("https://", "http://"),
                ):
                    self.assertEqual(classify(untrusted), "")

    def test_callback_cannot_override_trusted_probe_urls(self):
        fixture = WdlFixture()
        fixture.payload["backendUrls"] = {"sweden": "https://attacker.example"}
        fixture.payload["url"] = "https://attacker.example"
        self.assertEqual(fixture.run(), "Succeeded")
        self.assertEqual(
            [request["uri"] for request in fixture.probe_requests],
            [deploy.backend_urls()["sweden"]] * 2,
        )

    def test_http_security_identity_no_retries_and_nesting_limit(self):
        definition = deploy.workflow_definition()
        trigger = definition["triggers"]["monitor_alert"]
        self.assertEqual(trigger["runtimeConfiguration"]["concurrency"]["runs"], 1)

        def visit(actions, depth=1):
            self.assertLessEqual(depth, 8)
            for name, action in actions.items():
                kind = action["type"]
                if kind in ("Http", "Compose", "ParseJson"):
                    expected = ["inputs", "outputs"] if kind == "Http" else ["inputs"]
                    self.assertEqual(
                        action["runtimeConfiguration"]["secureData"]["properties"], expected,
                    )
                if kind == "Http":
                    inputs = action["inputs"]
                    self.assertEqual(inputs["retryPolicy"], {"type": "none"})
                    self.assertEqual(action["operationOptions"], "DisableAsyncPattern")
                    if inputs["uri"].startswith("https://management.azure.com"):
                        self.assertEqual(inputs["uri"], deploy.route_uri())
                        self.assertEqual(inputs["authentication"], {
                            "type": "ManagedServiceIdentity",
                            "audience": "https://management.azure.com/",
                        })
                        if inputs["method"] == "PUT":
                            self.assertIn("If-Match", inputs["headers"])
                    else:
                        selected = "body('Parse_health_check')['backend']" if name.startswith("Health_") \
                            else "outputs('Backup')"
                        self.assertEqual(inputs["uri"], f"@parameters('backendUrls')[{selected}]")
                        self.assertEqual(inputs["authentication"], {
                            "type": "ManagedServiceIdentity",
                            "audience": "https://cognitiveservices.azure.com",
                            "identity": deploy.MODEL_IDENTITY_ID,
                        })
                        self.assertEqual(inputs["headers"], {"Content-Type": "application/json"})
                        self.assertIn("120 seconds", action["description"])
                        self.assertIn("no custom total/idle deadline", action["description"])
                        self.assertEqual(inputs["body"]["max_completion_tokens"], 32)
                        self.assertEqual(inputs["body"]["reasoning_effort"], "none")
                if kind == "If":
                    visit(action["actions"], depth + 1)
                    visit(action["else"]["actions"], depth + 1)

        visit(definition["actions"])

    def test_import_and_template_command_do_not_call_azure(self):
        with patch.object(deploy, "arm") as arm_mock:
            with patch("sys.argv", ["deploy.py", "template"]), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(deploy.main(), 0)
            arm_mock.assert_not_called()

    def test_switch_mode_preserves_all_parameters_and_assigned_identities(self):
        initial = {
            "location": "eastus2",
            "identity": {
                "type": "SystemAssigned, UserAssigned",
                "principalId": "f11678b8-01cb-4b24-a17f-584925f02b65",
                "tenantId": "fixture-tenant",
                "userAssignedIdentities": {
                    deploy.MODEL_IDENTITY_ID: {"principalId": "model-principal", "clientId": "model-client"},
                    "/fixture/other-identity": {"principalId": "other-principal", "clientId": "other-client"},
                },
            },
            "properties": {"definition": deploy.workflow_definition(), "state": "Enabled", "parameters": {
                "backendUrls": {"value": deploy.backend_urls()},
                "workspaceId": {"value": deploy.WORKSPACE},
                "switchEnabled": {"value": False},
                "other": {"value": "preserved"},
            }},
        }
        final = copy.deepcopy(initial)
        final["properties"]["parameters"]["switchEnabled"]["value"] = True
        with patch.object(deploy, "arm", side_effect=[initial, {}, final]) as mocked:
            deploy.switch_mode(True)
        parameters = mocked.call_args_list[1].args[2]["properties"]["parameters"]
        self.assertEqual(mocked.call_args_list[1].args[0], "PUT")
        self.assertEqual(
            mocked.call_args_list[1].args[2]["properties"]["definition"],
            initial["properties"]["definition"],
        )
        self.assertEqual(mocked.call_args_list[1].args[2]["identity"], {
            "type": "SystemAssigned, UserAssigned",
            "userAssignedIdentities": {
                deploy.MODEL_IDENTITY_ID: {}, "/fixture/other-identity": {},
            },
        })
        self.assertEqual(
            parameters["backendUrls"]["value"], deploy.backend_urls(),
        )
        self.assertIs(initial["properties"]["parameters"]["switchEnabled"]["value"], False)
        self.assertEqual(parameters["other"]["value"], "preserved")
        self.assertEqual(parameters["workspaceId"]["value"], deploy.WORKSPACE)

    def test_enable_never_skips_permissions_or_schema_gate(self):
        with patch("sys.argv", ["deploy.py", "enable-alerts"]), \
                patch.object(deploy, "arm") as arm_mock, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(deploy.main(), 1)
            arm_mock.assert_not_called()
        with patch("sys.argv", ["deploy.py", "enable-alerts", "--confirm-log-schema"]), \
                patch.object(deploy, "verify_access", side_effect=RuntimeError("Denied")), \
                patch.object(deploy, "switch_mode") as mode, \
                patch.object(deploy, "arm") as arm_mock, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(deploy.main(), 1)
            mode.assert_not_called()
            arm_mock.assert_not_called()

    def test_maintenance_command_requires_disabled_alerts_and_switch_without_updates(self):
        with patch.object(deploy, "arm") as mocked:
            with self.assertRaises(ValueError):
                deploy.health_check("embedding")
            mocked.assert_not_called()
        with patch.object(deploy, "require_disabled_alerts", side_effect=RuntimeError("Enabled")), \
                patch.object(deploy, "arm") as mocked, patch.object(deploy, "invoke_and_wait") as invoke:
            with self.assertRaises(RuntimeError):
                deploy.health_check("eastus2")
            mocked.assert_not_called()
            invoke.assert_not_called()
        with patch.object(deploy, "require_disabled_alerts"), \
                patch.object(deploy, "arm", return_value={
                    "properties": {"parameters": {"switchEnabled": {"value": True}}},
                }) as mocked, patch.object(deploy, "invoke_and_wait") as invoke:
            with self.assertRaises(RuntimeError):
                deploy.health_check("eastus2")
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(mocked.call_args.args[0], "GET")
            invoke.assert_not_called()

    def test_maintenance_command_verifies_probe_statuses_and_no_route_execution(self):
        safe_workflow = {"properties": {"parameters": {"switchEnabled": {"value": False}}}}
        successful = {
            name: "Succeeded" for name in (
                "Health_probe_one", "Validate_health_probe_one",
                "Health_probe_two", "Validate_health_probe_two",
            )
        }
        for override in ({}, {"Validate_health_probe_two": "Skipped"}, {"Read_route": "Succeeded"},
                         {"Write_route": "Succeeded"}, {"Verify_access_write": "Succeeded"}):
            with patch.object(deploy, "require_disabled_alerts"), \
                    patch.object(deploy, "arm", return_value=safe_workflow) as mocked, \
                    patch.object(deploy, "invoke_and_wait", return_value=("Succeeded", "health-run")) as invoke, \
                    patch.object(deploy, "inspect_run", return_value=("Succeeded", {**successful, **override})), \
                    patch("sys.stdout", new_callable=io.StringIO):
                if override:
                    with self.assertRaises(RuntimeError):
                        deploy.health_check("eastus2")
                else:
                    self.assertEqual(deploy.health_check("eastus2"), "health-run")
                invoke.assert_called_once_with(
                    {"schemaId": "monitoring.healthCheck.v1", "backend": "eastus2"}, return_run=True,
                )
                self.assertEqual(mocked.call_count, 1)
                self.assertEqual(mocked.call_args.args[0], "GET")

    def test_maintenance_cli_requires_explicit_backend(self):
        with patch("sys.argv", ["deploy.py", "health-check"]), \
                patch.object(deploy, "arm") as mocked, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(deploy.main(), 1)
            mocked.assert_not_called()
        with patch("sys.argv", ["deploy.py", "health-check", "--backend", "sweden"]), \
                patch.object(deploy, "health_check") as health:
            self.assertEqual(deploy.main(), 0)
            health.assert_called_once_with("sweden")

    def test_validate_precedes_deployment_and_disabled_default(self):
        responses = [
            {},
            {},
            {"properties": {"provisioningState": "Succeeded"}},
            {"identity": {"principalId": "fixture-mi"}},
            {"properties": {"enabled": False}},
            {"properties": {"enabled": False}},
        ]
        with patch.object(deploy, "arm", side_effect=responses) as mocked, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            deploy.deploy()
        calls = mocked.call_args_list
        self.assertTrue(calls[0].args[1].endswith("/validate"))
        self.assertEqual(calls[1].args[0], "PUT")
        self.assertEqual(calls[1].args[2]["properties"]["parameters"], {})
        self.assertFalse(any("/containerApps/" in call.args[1] for call in calls))
        self.assertIn("DISABLED", output.getvalue())

    def test_grant_script_is_explicit_narrow_and_not_called_by_deployer(self):
        script = Path(__file__).with_name("grant-route-role.sh").read_text()
        self.assertIn("/namedValues/chat-route", script)
        self.assertIn("--assignee-object-id", script)
        self.assertIn("--assignee-principal-type ServicePrincipal", script)
        self.assertIn("API Management Service Contributor", script)
        self.assertIn("312a565d-c81f-4fd8-895a-4e21e48d571c", script)
        self.assertNotIn("subprocess", Path(deploy.__file__).read_text())

    def test_ignored_smoke_requires_observed_noop_and_no_arm_or_probe_actions(self):
        safe_workflow = {"properties": {"parameters": {"switchEnabled": {"value": False}}}}
        for unexpected in (None, "Write_route", "Read_access_route", "Probe_one",
                           "Health_probe_one", "Health_probe_two"):
            states = {"ResolvedNoOp": "Succeeded"}
            if unexpected:
                states[unexpected] = "Succeeded"
            with patch.object(deploy, "require_disabled_alerts"), \
                    patch.object(deploy, "arm", return_value=safe_workflow), \
                    patch.object(deploy, "invoke_and_wait", return_value=("Cancelled", "fixture-run")) as invoke, \
                    patch.object(deploy, "inspect_run", return_value=("Cancelled", states)), \
                    patch("sys.stdout", new_callable=io.StringIO):
                if unexpected:
                    with self.assertRaises(RuntimeError):
                        deploy.ignored_smoke()
                else:
                    self.assertEqual(deploy.ignored_smoke(), "fixture-run")
                sent = invoke.call_args.args[0]
                self.assertEqual(sent["data"]["essentials"]["monitorCondition"], "Resolved")
                self.assertEqual(sent["schemaId"], "azureMonitorCommonAlertSchema")

    def test_inspect_run_prints_only_status_not_sensitive_links_or_bodies(self):
        responses = [
            {"properties": {"status": "Cancelled"}},
            {"value": [{
                "name": "ResolvedNoOp",
                "properties": {
                    "status": "Succeeded", "inputsLink": "SECRET-LINK", "outputs": "SECRET-BODY",
                },
            }]},
        ]
        with patch.object(deploy, "arm", side_effect=responses), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            state, actions = deploy.inspect_run("fixture-run")
        self.assertEqual(state, "Cancelled")
        self.assertEqual(actions, {"ResolvedNoOp": "Succeeded"})
        self.assertNotIn("SECRET", output.getvalue())
        with patch.object(deploy, "arm") as arm_mock:
            with self.assertRaises(ValueError):
                deploy.inspect_run("../other-resource")
            arm_mock.assert_not_called()

    def test_inspection_follows_same_resource_pagination_including_explicit_https_port(self):
        continuation = (
            "https://management.azure.com:443" + deploy.WORKFLOW
            + "/runs/fixture-run/actions?api-version=" + deploy.LOGIC_API
            + "&$skiptoken=SECRET-CONTINUATION"
        )
        responses = [
            {"properties": {"status": "Cancelled"}},
            {"value": [{"name": "Write_route", "properties": {"status": "Skipped"}}],
             "nextLink": continuation},
            {"value": [{"name": "ResolvedNoOp", "properties": {"status": "Succeeded"}}]},
        ]
        with patch.object(deploy, "arm", side_effect=responses) as mocked, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            state, actions = deploy.inspect_run("fixture-run")
        self.assertEqual(state, "Cancelled")
        self.assertEqual(actions, {"Write_route": "Skipped", "ResolvedNoOp": "Succeeded"})
        self.assertEqual(mocked.call_count, 3)
        self.assertNotIn("SECRET-CONTINUATION", output.getvalue())


if __name__ == "__main__":
    unittest.main()
