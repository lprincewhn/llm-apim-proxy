from datetime import datetime, timezone
import unittest

import aligned_metric_experiment as aligned
import deploy


NOW = datetime(2026, 9, 16, 3, 30, 15, tzinfo=timezone.utc)


def payload(counts):
    return {"value": [{
        "name": {"value": "AzureOpenAIRequests"}, "errorCode": "Success",
        "timeseries": [
            {"metadatavalues": [
                {"name": {"value": key}, "value": value}
                for key, value in {
                    "ApiName": "OpenAI", "ModelDeploymentName": "gpt-5.1",
                    "OperationName": "chatcompletions_create", "StatusCode": status,
                }.items()
            ], "data": [{"timeStamp": "2026-09-16T03:29:00Z", "total": count}]}
            for status, count in counts.items()
        ],
    }]}


class AlignedMetricTests(unittest.TestCase):
    def test_exact_boundaries(self):
        for counts, violation in (
            ({"200": 4, "404": 1}, 1),
            ({"200": 3, "404": 1}, 0),
            ({"200": 5, "404": 1}, 0),
            ({"200": 0, "404": 5}, 1),
            ({"200": 20, "404": 1, "429": 1, "500": 2, "599": 1}, 1),
            ({"200": 4, "404": 1, "401": 50, "403": 50}, 1),
            ({"401": 5, "403": 10}, 0),
            ({"400": 5, "404": 1}, 0),
        ):
            with self.subTest(counts=counts):
                self.assertEqual(aligned.calculate(payload(counts), NOW)["violation"], violation)

    def test_weighted_counts_and_no_traffic(self):
        result = aligned.calculate(payload({"200": 900, "404": 100}), NOW)
        self.assertEqual(result["error_rate_pct"], 10)
        self.assertIsNone(aligned.calculate(payload({"200": 0}), NOW)["error_rate_pct"])
        self.assertIsNone(aligned.calculate(payload({"401": 1}), NOW)["error_rate_pct"])

    def test_invalid_or_missing_telemetry_fails_closed(self):
        for body in (
            payload({}), payload({"unknown": 8}), payload({"600": 5}),
            payload({"404": -1}), payload({"404": float("nan")}), payload({"200": None}),
            {**payload({"200": 5}), "nextLink": "continuation"},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    aligned.calculate(body, NOW)
        body = payload({"200": 5})
        body["value"][0]["timeseries"] *= 2
        with self.assertRaises(ValueError):
            aligned.calculate(body, NOW)

    def test_window_and_operation_filter(self):
        start, end = aligned.source_window(NOW)
        self.assertEqual((end-start).total_seconds(), 300)
        self.assertEqual(end.second, 0)
        body = payload({"404": 5, "200": 1})
        body["value"][0]["timeseries"][0]["metadatavalues"][2]["value"] = "embeddings_create"
        self.assertEqual(aligned.calculate(body, NOW)["errors"], 0)
        body["value"][0]["timeseries"][0]["metadatavalues"][2]["value"] = "responses_create"
        self.assertEqual(aligned.calculate(body, NOW)["errors"], 5)
        body["value"][0]["timeseries"][0]["data"][0]["timeStamp"] = "2026-09-16T03:30:00Z"
        with self.assertRaises(ValueError):
            aligned.calculate(body, NOW)

    def test_computed_rule_and_unchanged_controller(self):
        rule = aligned.metric_rule()
        self.assertFalse(rule["properties"]["enabled"])
        criterion = rule["properties"]["criteria"]["allOf"][0]
        self.assertEqual(criterion["metricName"], "ViolationCount")
        self.assertEqual(criterion["threshold"], 1)
        original = deploy.workflow_definition()
        changed = aligned.experiment_definition(original)
        def actions(body):
            return body["actions"]["Access_check_or_alert"]["else"]["actions"][
                "Health_check_or_alert"
            ]["else"]["actions"]
        for name in actions(original):
            if name not in ("Parse_alert", "Allowlisted_workspace", "Unhealthy_backend"):
                self.assertEqual(actions(original)[name], actions(changed)[name])
        self.assertEqual(original, deploy.workflow_definition())


if __name__ == "__main__":
    unittest.main()
