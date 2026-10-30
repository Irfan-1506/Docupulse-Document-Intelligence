<#
.SYNOPSIS
    DocuPulse Enterprise AP v4.2 - Container Lifecycle Management Script (PowerShell)
.DESCRIPTION
    Helper tool to build, run, monitor, and clean up the DocuPulse Docker Compose cluster.
.EXAMPLE
    .\scripts\manage_containers.ps1 build
    .\scripts\manage_containers.ps1 up
    .\scripts\manage_containers.ps1 logs
    .\scripts\manage_containers.ps1 down
#>

param(
    [Parameter(Position=0, Mandatory=$true)]
    [ValidateSet("build", "up", "dev", "down", "status", "logs", "test-health", "clean")]
    [string]$Action
)

$ErrorActionPreference = "Stop"

switch ($Action) {
    "build" {
        Write-Host "=== [DocuPulse AP] Building Docker Containers ===" -ForegroundColor Cyan
        docker compose build
    }
    "up" {
        Write-Host "=== [DocuPulse AP] Starting Production Cluster (Detached) ===" -ForegroundColor Green
        docker compose up -d
        Write-Host "[DocuPulse AP] Cluster online at: http://localhost:8000" -ForegroundColor Yellow
    }
    "dev" {
        Write-Host "=== [DocuPulse AP] Starting Development Cluster with Live Reload ===" -ForegroundColor Magenta
        docker compose -f docker-compose.yml -f docker-compose.dev.yml up
    }
    "down" {
        Write-Host "=== [DocuPulse AP] Stopping Container Cluster ===" -ForegroundColor Red
        docker compose down
    }
    "status" {
        Write-Host "=== [DocuPulse AP] Cluster Container Status ===" -ForegroundColor Cyan
        docker compose ps
    }
    "logs" {
        Write-Host "=== [DocuPulse AP] Streaming Cluster Logs ===" -ForegroundColor Yellow
        docker compose logs -f --tail=100
    }
    "test-health" {
        Write-Host "=== [DocuPulse AP] Probing API Health Endpoint ===" -ForegroundColor Cyan
        try {
            $response = Invoke-RestMethod -Uri "http://localhost:8000/health" -Method Get
            Write-Host "[HEALTH OK] Response:" -ForegroundColor Green
            $response | ConvertTo-Json -Depth 4 | Write-Host
        } catch {
            Write-Host "[HEALTH FAILED] Could not reach http://localhost:8000/health: $_" -ForegroundColor Red
        }
    }
    "clean" {
        Write-Host "=== [DocuPulse AP] Tearing Down Containers and Volumes ===" -ForegroundColor Red
        docker compose down -v --remove-orphans
    }
}
