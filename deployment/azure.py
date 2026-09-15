"""Azure management helpers. Secrets remain in memory, never artifact files."""

import json
import subprocess
import urllib.error
import urllib.request

SUB = "10564893-ecc3-4a6d-b505-53bcbe89dd8e"
RG = "rg-svhwb107-apim-lab"
ROOT = f"/subscriptions/{SUB}/resourceGroups/{RG}"
APIM = f"{ROOT}/providers/Microsoft.ApiManagement/service/apim-svhwb107-0915"
ACR = f"{ROOT}/providers/Microsoft.ContainerRegistry/registries/acrsvhwb1070915"
APP = f"{ROOT}/providers/Microsoft.App/containerApps/exec-svhwb107"
MI = f"{ROOT}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-svhwb107-exec"
TOKEN = None


def cli(*args):
    result = subprocess.run(
        ["az", *args, "--subscription", SUB, "-o", "json", "--only-show-errors"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def arm(method, path, body=None, version="2024-05-01", headers=None):
    global TOKEN
    if TOKEN is None:
        TOKEN = cli("account", "get-access-token",
                    "--resource", "https://management.azure.com/")["accessToken"]
    request = urllib.request.Request(
        "https://management.azure.com" + path + "?api-version=" + version,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        # Management error bodies can echo supplied secret properties.
        raise RuntimeError(f"ARM {method} {path}: HTTP {exc.code}") from None


def container_secrets():
    return {
        item["name"]: item["value"]
        for item in arm("POST", APP + "/listSecrets", {}, "2024-03-01")["value"]
    }
