import unittest
from unittest.mock import patch

from configure_proxy import BACKEND_NAME, ENDPOINT, KEY_NAME, configure_proxy


class ProxyBackendTests(unittest.TestCase):
    def test_registration_uses_secret_reference_and_preserves_live_route(self):
        with patch("configure_proxy.arm") as arm, patch("builtins.print"):
            configure_proxy("unit-test-placeholder\n")
        self.assertEqual(arm.call_count, 2)
        secret, backend = [call.args for call in arm.call_args_list]
        self.assertTrue(secret[1].endswith("/namedValues/" + KEY_NAME))
        self.assertTrue(secret[2]["properties"]["secret"])
        self.assertEqual(secret[2]["properties"]["value"], "unit-test-placeholder")
        self.assertTrue(backend[1].endswith("/backends/" + BACKEND_NAME))
        properties = backend[2]["properties"]
        self.assertEqual(properties["url"], ENDPOINT)
        self.assertEqual(properties["credentials"], {
            "header": {"api-key": ["{{proxy-api-key}}"]},
        })
        self.assertNotIn("unit-test-placeholder", str(properties))
        self.assertEqual(properties["tls"], {
            "validateCertificateChain": True, "validateCertificateName": True,
        })
        self.assertIn("gpt-5.1", properties["description"])

    def test_redeployment_reuses_existing_secret(self):
        with patch("configure_proxy.arm", return_value={
            "properties": {"secret": True},
        }) as arm, patch("builtins.print"):
            configure_proxy()
        self.assertEqual([call.args[0] for call in arm.call_args_list], ["GET", "PUT"])
        self.assertTrue(arm.call_args.args[1].endswith("/backends/" + BACKEND_NAME))

    def test_rejects_empty_or_multiline_key_before_cloud_writes(self):
        for value in ("", " \n", "first\nsecond", "first second"):
            with self.subTest(value=value), patch("configure_proxy.arm") as arm:
                with self.assertRaises(ValueError):
                    configure_proxy(value)
                arm.assert_not_called()

    def test_refuses_nonsecret_named_value(self):
        with patch("configure_proxy.arm", return_value={
            "properties": {"secret": False},
        }) as arm:
            with self.assertRaises(ValueError):
                configure_proxy()
        self.assertEqual(arm.call_count, 1)
