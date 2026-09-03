#!/bin/bash
set -e

# Charger automatiquement le .env s'il existe
if [ -f .env ]; then
    set -a && source .env && set +a
fi

TARGET="${1:-argocd-hub}"
PLAYBOOK="${2:-hub-setup.yml}"

mkdir -p ./logs

# Génération d'un timestamp (ex: 20260212_1752)
TIMESTAMP=$(date +"%Y%m%d_%H%M")
LOG_FILE="./logs/ansible_${1:-prod}_${TIMESTAMP}.log"

echo "-------------------------------------------------------"
echo "Début du déploiement : $(date)"
echo "Fichier de log : $LOG_FILE"
echo "-------------------------------------------------------"
