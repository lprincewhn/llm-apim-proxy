"""Shared, validated direct Foundry targets; callers cannot supply endpoints."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from azure import ROOT

MODEL_IDENTITY_ID = ROOT + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-svhwb107-exec"
MODEL_IDENTITY_CLIENT_ID = "3dfb189c-3492-4eb9-80f8-4bc97213bf76"
API_VERSION = "2024-10-21"


def load_backends():
    backends = json.loads(Path(__file__).with_name("config.json").read_text())["backends"]
    if set(backends) != {"eastus2", "sweden"}:
        raise ValueError("Expected exactly the configured two failover backends")
    for name, backend in backends.items():
        endpoint = urlsplit(backend["endpoint"])
        if (
            endpoint.scheme != "https"
            or not re.fullmatch(r"[a-z0-9-]+\.openai\.azure\.com", endpoint.netloc)
            or endpoint.path not in ("", "/")
            or endpoint.query or endpoint.fragment
            or not re.fullmatch(r"[A-Za-z0-9._-]+", backend["deployment"])
            or backend["kind"] != "chat"
        ):
            raise ValueError(f"Invalid direct Foundry target: {name}")
    return backends


def backend_url(backend):
    return (
        backend["endpoint"].rstrip("/") + "/openai/deployments/"
        + backend["deployment"] + "/chat/completions?api-version=" + API_VERSION
    )
