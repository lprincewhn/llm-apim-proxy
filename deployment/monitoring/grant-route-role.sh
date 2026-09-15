#!/usr/bin/env bash
# Run explicitly as an RBAC administrator, NOT as the Contributor deployment identity.
set -euo pipefail
subscription='10564893-ecc3-4a6d-b505-53bcbe89dd8e'
group='rg-svhwb107-apim-lab'
workflow='llm-monitored-failover'
scope="/subscriptions/$subscription/resourceGroups/$group/providers/Microsoft.ApiManagement/service/apim-svhwb107-0915/namedValues/chat-route"
principal="$(az resource show --subscription "$subscription" \
  --resource-group "$group" --resource-type Microsoft.Logic/workflows \
  --name "$workflow" --api-version 2019-05-01 \
  --query identity.principalId -o tsv --only-show-errors)"
if [[ -z "$principal" || "$principal" == "null" ]]; then
  printf '%s\n' 'Logic App system-assigned identity not found.' >&2
  exit 1
fi
# Confirmed built-in API Management Service Contributor role.
role='312a565d-c81f-4fd8-895a-4e21e48d571c'
az role assignment create --subscription "$subscription" \
  --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal \
  --role "$role" --scope "$scope" --only-show-errors -o none
printf '%s\n' 'Named-value-scoped role granted. Alerts stay disabled until MI verification succeeds.'
