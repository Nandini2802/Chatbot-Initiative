// Azure Bicep — skeleton for Phase 1A production infrastructure.
// Full parameterisation should be done before first staging deploy.

@description('Environment name')
param environment string = 'staging'

@description('Azure region')
param location string = resourceGroup().location

@description('App name prefix')
param appName string = 'danube-ai'

// ── Azure Container Apps Environment ──────────────────────────────────────────
resource containerAppsEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: '${appName}-env-${environment}'
  location: location
  properties: {}
}

// ── Azure Container App — FastAPI service ─────────────────────────────────────
resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: '${appName}-${environment}'
  location: location
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
      }
      secrets: []
    }
    template: {
      containers: [
        {
          name: 'danube-ai'
          image: 'danubeacr.azurecr.io/danube-ai:latest'
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            { name: 'ENVIRONMENT', value: environment }
            { name: 'STORAGE_BACKEND', value: 'azure' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 10
        rules: [
          {
            name: 'http-scaling'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
}

// ── Azure Storage Account ─────────────────────────────────────────────────────
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: '${replace(appName, '-', '')}${environment}'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

output containerAppUrl string = containerApp.properties.configuration.ingress.fqdn
