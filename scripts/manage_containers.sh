#!/usr/bin/env bash
# ==============================================================================
# DocuPulse Enterprise AP v4.2 - Container Lifecycle Management Script (Bash)
# ==============================================================================

set -e

ACTION="${1:-help}"

case "$ACTION" in
    build)
        echo "=== [DocuPulse AP] Building Docker Containers ==="
        docker compose build
        ;;
    up)
        echo "=== [DocuPulse AP] Starting Production Cluster (Detached) ==="
        docker compose up -d
        echo "[DocuPulse AP] Cluster online at: http://localhost:8000"
        ;;
    dev)
        echo "=== [DocuPulse AP] Starting Development Cluster with Live Reload ==="
        docker compose -f docker-compose.yml -f docker-compose.dev.yml up
        ;;
    down)
        echo "=== [DocuPulse AP] Stopping Container Cluster ==="
        docker compose down
        ;;
    status)
        echo "=== [DocuPulse AP] Cluster Container Status ==="
        docker compose ps
        ;;
    logs)
        echo "=== [DocuPulse AP] Streaming Cluster Logs ==="
        docker compose logs -f --tail=100
        ;;
    test-health)
        echo "=== [DocuPulse AP] Probing API Health Endpoint ==="
        curl -s http://localhost:8000/health | python -m json.tool || echo "[ERROR] Failed to reach health endpoint."
        ;;
    clean)
        echo "=== [DocuPulse AP] Tearing Down Containers and Volumes ==="
        docker compose down -v --remove-orphans
        ;;
    *)
        echo "Usage: $0 {build|up|dev|down|status|logs|test-health|clean}"
        exit 1
        ;;
esac
