import copy
import unittest

import deploy
import metric_experiment as experiment


class MetricExperimentTests(unittest.TestCase):
    def test_disabled_exact_metric_scope(self):
        props = experiment.metric_rule()["properties"]
        self.assertFalse(props["enabled"])
        self.assertEqual(props["scopes"], [experiment.ACCOUNT])
        self.assertEqual(props["windowSize"], "PT5M")
        self.assertEqual(props["evaluationFrequency"], "PT1M")
        criterion = props["criteria"]["allOf"][0]
        self.assertEqual(criterion["threshold"], 5)
        self.assertEqual(criterion["timeAggregation"], "Total")
        self.assertEqual(
            {d["name"]: d["values"][0] for d in criterion["dimensions"]},
            experiment.DIMENSIONS,
        )

    def test_only_ingress_changes(self):
        original = deploy.workflow_definition()
        snapshot = copy.deepcopy(original)
        result = experiment.experiment_definition(original)
        self.assertEqual(original, snapshot)
        before = original["actions"]["Access_check_or_alert"]["else"]["actions"][
            "Health_check_or_alert"
        ]["else"]["actions"]
        after = result["actions"]["Access_check_or_alert"]["else"]["actions"][
            "Health_check_or_alert"
        ]["else"]["actions"]
        self.assertEqual(set(before), set(after))
        changed = {name for name in before if before[name] != after[name]}
        self.assertEqual(changed, {"Parse_alert", "Allowlisted_workspace", "Unhealthy_backend"})
        self.assertEqual(after["Unhealthy_backend"]["inputs"], "eastus2")
        self.assertEqual(result["parameters"], original["parameters"])

    def test_native_dimension_extension_fields_are_allowed(self):
        definition = experiment.experiment_definition(deploy.workflow_definition())
        actions = definition["actions"]["Access_check_or_alert"]["else"]["actions"][
            "Health_check_or_alert"
        ]["else"]["actions"]
        dimensions = actions["Parse_alert"]["inputs"]["schema"]["properties"]["data"][
            "properties"
        ]["alertContext"]["properties"]["condition"]["properties"]["allOf"]["items"][
            "properties"
        ]["dimensions"]
        # Native Monitor payloads also include operator/type/values (often null).
        for variant in dimensions["items"]["oneOf"]:
            self.assertNotEqual(variant.get("additionalProperties"), False)
            self.assertEqual(variant["required"], ["name", "value"])
        self.assertTrue(dimensions["uniqueItems"])
        self.assertEqual(len(dimensions["items"]["oneOf"]), 4)


if __name__ == "__main__":
    unittest.main()
