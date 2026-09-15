"""Remove business request retries without resetting routes, secrets or APIs."""

import html
import xml.etree.ElementTree as ET
from pathlib import Path

from azure import APIM, arm


def single_attempt_policy(xml, encoded=False):
    root = ET.fromstring(xml)
    if encoded:
        for node in root.iter():
            node.attrib.update({key: html.unescape(value) for key, value in node.attrib.items()})
            if node.text:
                node.text = html.unescape(node.text)
    backend = root.find("backend")
    inbound = root.find("inbound")
    if backend is None or inbound is None:
        raise ValueError("Missing inbound or backend policy")
    retries = backend.findall("retry")
    if len(retries) > 1:
        raise ValueError("Unexpected multiple retry blocks")
    if retries:
        retry = retries[0]
        position = list(backend).index(retry)
        backend.remove(retry)
        for offset, child in enumerate(retry):
            backend.insert(position + offset, child)
    if root.find(".//retry") is not None:
        raise ValueError("Unexpected nested retry policy")
    for child in list(backend):
        if child.tag != "forward-request":
            backend.remove(child)
            inbound.append(child)
    forwards = backend.findall("forward-request")
    if len(forwards) != 1:
        raise ValueError("Expected one direct forward-request")
    selected = inbound.find("set-variable[@name='selected']")
    if selected is None:
        raise ValueError("Missing backend selector")
    selected.set(
        "value",
        '@{ if ((string)context.Variables["purpose"] == "embedding") { return "embedding"; } '
        'return (string)JObject.Parse((string)context.Variables["route"])["primary"]; }',
    )
    return ET.tostring(root, encoding="unicode")


def main():
    route_id = APIM + "/namedValues/chat-route"
    route_before = arm("GET", route_id)["properties"]["value"]
    policy_id = APIM + "/apis/llm/policies/policy"
    original = arm("GET", policy_id)
    updated = single_attempt_policy(
        original["properties"]["value"], encoded=original["properties"]["format"] == "xml"
    )
    arm("PUT", policy_id, {"properties": {"format": "rawxml", "value": updated}})
    deployed = arm("GET", policy_id)["properties"]["value"]
    if ET.fromstring(deployed).find(".//retry") is not None:
        raise RuntimeError("APIM still contains a business retry policy")
    if arm("GET", route_id)["properties"]["value"] != route_before:
        raise RuntimeError("Route changed concurrently; inspect before proceeding")
    Path(__file__).with_name("llm-policy.xml").write_text(updated, encoding="utf-8")
    print("Business API now forwards once; route and secrets were not changed.")


if __name__ == "__main__":
    main()
