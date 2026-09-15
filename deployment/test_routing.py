import unittest

from routing import evaluate


class RoutingTests(unittest.TestCase):
    def step(self, state, now=0, **overrides):
        args = dict(samples=100, p95_ms=1000, error_rate=0, budget_ms=4000, now=now)
        args.update(overrides)
        return evaluate(state, **args)

    def test_two_bad_windows_quarantine(self):
        state = self.step({}, p95_ms=3500)
        self.assertEqual(state["state"], "Suspect")
        state = self.step(state, now=60, p95_ms=3500)
        self.assertEqual(state["state"], "Quarantined")
        self.assertEqual(state["quarantined_until"], 360)

    def test_no_data_never_recovers(self):
        state = dict(state="Quarantined", quarantined_until=100)
        for t in range(200, 1000, 60):
            state = self.step(state, now=t, samples=0)
        self.assertEqual(state["state"], "Quarantined")

    def test_probe_recovery_requires_five_windows(self):
        state = dict(state="Quarantined", quarantined_until=100)
        for i in range(4):
            state = self.step(state, now=200+i*60, probe_ok=True)
            self.assertEqual(state["state"], "Quarantined")
        state = self.step(state, now=500, probe_ok=True)
        self.assertEqual(state["state"], "Recovering")

    def test_manual_disable_is_sticky(self):
        state = self.step(dict(state="ManualDisabled"), probe_ok=True)
        self.assertEqual(state["state"], "ManualDisabled")


if __name__ == "__main__":
    unittest.main()
