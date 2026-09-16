"""Register the optional session proxy without changing live routing or failover."""

import argparse
import sys

from azure import APIM, arm

BACKEND_NAME = "llm-proxy"
ENDPOINT = "https://proxy.svhw.tech"
KEY_NAME = "proxy-api-key"
MODEL = "gpt-5.1"


def configure_proxy(key=None):
    if key is not None:
        key = key.strip()
        if not key or any(character.isspace() for character in key):
            raise ValueError("A nonempty, single-token proxy API key is required")
        arm("PUT", APIM + "/namedValues/" + KEY_NAME, {"properties": {
            "displayName": KEY_NAME, "secret": True, "value": key,
        }})
    else:
        existing = arm("GET", APIM + "/namedValues/" + KEY_NAME)
        if not existing["properties"].get("secret"):
            raise ValueError("The proxy API key named value must be secret")
    arm("PUT", APIM + "/backends/" + BACKEND_NAME, {"properties": {
        "protocol": "http",
        "url": ENDPOINT,
        "description": (
            "Session proxy; POST /v1/responses with model " + MODEL
            + ". Registered only; not in the Foundry automatic failover pool."
        ),
        "credentials": {"header": {"api-key": ["{{" + KEY_NAME + "}}"]}},
        "tls": {"validateCertificateChain": True, "validateCertificateName": True},
    }})
    print("Registered llm-proxy with a secret API key reference; live routing unchanged.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key-stdin", action="store_true",
        help="Create/rotate the secret from stdin; otherwise reuse the existing secret",
    )
    args = parser.parse_args()
    configure_proxy(sys.stdin.read() if args.key_stdin else None)
