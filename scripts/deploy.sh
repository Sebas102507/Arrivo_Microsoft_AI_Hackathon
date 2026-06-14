#!/usr/bin/env bash
# Arrivo — one-shot deploy. Requires: az CLI, logged in (az login).
set -euo pipefail

RG="${RG:-arrivo-rg}"
LOCATION="${LOCATION:-australiaeast}"

echo ">> Creating resource group $RG in $LOCATION"
az group create -n "$RG" -l "$LOCATION" -o none

echo ">> Deploying infrastructure (this takes ~3-5 min)"
az deployment group create \
  -g "$RG" \
  -f "$(dirname "$0")/../infra/main.bicep" \
  -p "$(dirname "$0")/../infra/main.parameters.json" \
  -o none

echo ">> Fetching outputs and keys"
OUT=$(az deployment group show -g "$RG" -n main --query properties.outputs -o json)
AI_NAME=$(echo "$OUT"      | python3 -c "import sys,json;print(json.load(sys.stdin)['aiServicesName']['value'])")
OPENAI_EP=$(echo "$OUT"    | python3 -c "import sys,json;print(json.load(sys.stdin)['openAiEndpoint']['value'])")
SEARCH_NAME=$(echo "$OUT"  | python3 -c "import sys,json;print(json.load(sys.stdin)['searchName']['value'])")
SEARCH_EP=$(echo "$OUT"    | python3 -c "import sys,json;print(json.load(sys.stdin)['searchEndpoint']['value'])")
CHAT_DEP=$(echo "$OUT"     | python3 -c "import sys,json;print(json.load(sys.stdin)['chatDeploymentName']['value'])")
EMBED_DEP=$(echo "$OUT"    | python3 -c "import sys,json;print(json.load(sys.stdin)['embedDeploymentName']['value'])")
PROJECT_EP=$(echo "$OUT"   | python3 -c "import sys,json;print(json.load(sys.stdin)['projectEndpoint']['value'])")
SEARCH_CONN=$(echo "$OUT"  | python3 -c "import sys,json;print(json.load(sys.stdin)['searchConnectionName']['value'])")

AI_KEY=$(az cognitiveservices account keys list -g "$RG" -n "$AI_NAME" --query key1 -o tsv)
SEARCH_KEY=$(az search admin-key show -g "$RG" --service-name "$SEARCH_NAME" --query primaryKey -o tsv)

ENVFILE="$(dirname "$0")/../backend/.env"
cat > "$ENVFILE" <<EOF
AZURE_OPENAI_ENDPOINT=$OPENAI_EP
AZURE_OPENAI_API_KEY=$AI_KEY
AZURE_OPENAI_CHAT_DEPLOYMENT=$CHAT_DEP
AZURE_OPENAI_EMBED_DEPLOYMENT=$EMBED_DEP
AZURE_SEARCH_ENDPOINT=$SEARCH_EP
AZURE_SEARCH_API_KEY=$SEARCH_KEY
AZURE_SEARCH_INDEX=arrivo-kb
FOUNDRY_PROJECT_ENDPOINT=$PROJECT_EP
FOUNDRY_SEARCH_CONNECTION_NAME=$SEARCH_CONN
EOF

echo ">> Granting the current user the 'Azure AI User' role on the Foundry account (for the Agent Service)"
AI_ID=$(az cognitiveservices account show -g "$RG" -n "$AI_NAME" --query id -o tsv)
ME=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)
if [ -n "$ME" ]; then
  az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
    --role "Azure AI User" --scope "$AI_ID" -o none 2>/dev/null \
    || echo "   (role may already exist or require an admin — assign 'Azure AI User' manually if create_v2.py 401s)"
fi

echo ">> Done. Credentials written to backend/.env"
echo ">> Next: pip install -r backend/requirements.txt"
echo ">>       cd ingestion && python ingest.py            # build the knowledge base"
echo ">>       python foundry/create_v2.py                 # create the Foundry agents over that knowledge"
echo ">>       cd backend && uvicorn app:app --reload      # run the app  (POST /api/ask)"
