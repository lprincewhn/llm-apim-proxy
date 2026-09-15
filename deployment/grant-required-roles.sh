#!/usr/bin/env bash
# Run only as an authorized Owner or User Access Administrator.
set -euo pipefail
SUB=10564893-ecc3-4a6d-b505-53bcbe89dd8e
PRINCIPAL=8f1e45f4-0ac1-400d-b403-87ab38dac147
for account in svhw-openai-eastus2 svhw2-swedencentral svhw2-westus3; do
  az role assignment create --subscription "$SUB" \
    --assignee-object-id "$PRINCIPAL" \
    --assignee-principal-type ServicePrincipal \
    --role 'Cognitive Services OpenAI User' \
    --scope "/subscriptions/$SUB/resourceGroups/jump-server_group/providers/Microsoft.CognitiveServices/accounts/$account" \
    --output none
done
