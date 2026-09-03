#!/bin/bash
set -e

# Charger automatiquement le .env s'il existe
if [ -f .env ]; then
    set -a && source .env && set +a
fi

TARGET="${1:-argocd-hub}"
PLAYBOOK="${2:-hub-setup.yml}"

mkdir -p ./logs

# Génération d'un timestamp
TIMESTAMP=$(date +"%Y%m%d_%H%M")
LOG_FILE="./logs/ansible_${TARGET}_${TIMESTAMP}.log"

echo "-------------------------------------------------------"
echo "Début du déploiement : $(date)"
echo "Cible : $TARGET"
echo "Playbook : $PLAYBOOK"
echo "Fichier de log : $LOG_FILE"
echo "-------------------------------------------------------"

# Lancement d'Ansible avec affichage en direct et sauvegarde dans le log
ansible-playbook -i hosts.yml "$PLAYBOOK" --limit "$TARGET" -vv | tee "$LOG_FILE"

echo "-------------------------------------------------------"
echo "Déploiement terminé avec succès pour $TARGET !"
echo "-------------------------------------------------------"
