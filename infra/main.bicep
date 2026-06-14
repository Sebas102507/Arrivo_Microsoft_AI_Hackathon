// Arrivo — Settlement Reasoning Agent infrastructure
// Deploys: Azure AI Foundry (AI Services) account + model deployments + Azure AI Search
// Usage: az deployment group create -g <rg> -f infra/main.bicep -p infra/main.parameters.json

@description('Base name used for all resources (lowercase, letters/numbers only)')
param baseName string = 'arrivo'

@description('Azure region. australiaeast keeps data local and demos well for an AU project.')
param location string = 'australiaeast'

@description('Chat model to deploy')
param chatModelName string = 'gpt-4o'
param chatModelVersion string = '2024-11-20'

@description('Embedding model for the knowledge base')
param embedModelName string = 'text-embedding-3-small'
param embedModelVersion string = '1'

@description('Azure AI Search SKU. "free" = $0 (1 per subscription, 50MB). Use "basic" if free is taken.')
@allowed(['free', 'basic'])
param searchSku string = 'free'

var suffix = uniqueString(resourceGroup().id)

// ---------- Azure AI Foundry (AI Services) ----------
resource aiServices 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: '${baseName}-foundry-${suffix}'
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: '${baseName}-foundry-${suffix}'
    publicNetworkAccess: 'Enabled'
  }
}

resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiServices
  name: chatModelName
  sku: { name: 'Standard', capacity: 30 }
  properties: {
    model: { format: 'OpenAI', name: chatModelName, version: chatModelVersion }
  }
}

resource embedDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiServices
  name: embedModelName
  dependsOn: [chatDeployment] // deployments must be created sequentially
  sku: { name: 'GlobalStandard', capacity: 30 }
  properties: {
    model: { format: 'OpenAI', name: embedModelName, version: embedModelVersion }
  }
}

// ---------- Foundry project (home for the Agent Service + knowledge connections) ----------
// The Foundry Agent Service ("Reasoning Agents" track) lives inside a project. The agent we
// create connects to the Azure AI Search index below as its grounded knowledge source.
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: aiServices
  name: '${baseName}-project'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Arrivo'
    description: 'Settlement reasoning agent — grounded on official AU government sources.'
  }
}

// ---------- Azure AI Search (knowledge base / grounding layer) ----------
resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: '${baseName}-search-${suffix}'
  location: location
  sku: { name: searchSku }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
  }
}

// ---------- Project → Azure AI Search connection (the agent's knowledge) ----------
// Uses the search admin key so the Agent Service can query the arrivo-kb index with no
// extra RBAC wiring. The connection name is what create_v2.py resolves at deploy time.
resource searchConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: '${baseName}search'
  properties: {
    category: 'CognitiveSearch'
    target: 'https://${search.name}.search.windows.net'
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: search.listAdminKeys().primaryKey
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: search.id
      Location: location
    }
  }
}

// ---------- Outputs (keys are fetched by scripts/setup_env.sh, not exposed here) ----------
output aiServicesName string = aiServices.name
output aiServicesEndpoint string = aiServices.properties.endpoint
output openAiEndpoint string = 'https://${aiServices.properties.customSubDomainName}.openai.azure.com/'
output searchName string = search.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output chatDeploymentName string = chatModelName
output embedDeploymentName string = embedModelName

// Foundry Agent Service: data-plane endpoint + the search connection the agent grounds on.
output projectName string = project.name
output projectEndpoint string = 'https://${aiServices.properties.customSubDomainName}.services.ai.azure.com/api/projects/${project.name}'
output searchConnectionName string = searchConnection.name
