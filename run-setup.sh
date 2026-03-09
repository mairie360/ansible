#!/bin/bash

# Création du dossier de logs s'il n'existe pas
mkdir -p ./logs

# Génération d'un timestamp (ex: 20260212_1752)
TIMESTAMP=$(date +"%Y%m%d_%H%M")
LOG_FILE="./logs/ansible_${1:-prod}_${TIMESTAMP}.log"

echo "-------------------------------------------------------"
echo "Début du déploiement : $(date)"
echo "Fichier de log : $LOG_FILE"
echo "-------------------------------------------------------"

if [ "$1" == "dev" ]; then
    echo "Mode DEV activé..."
    # -vv permet d'avoir plus de détails sans polluer totalement la console
    ansible-playbook -i hosts.yml dev-setup-server.yml -e @secrets.yml -vv | tee -a "$LOG_FILE"
else
    echo "Mode PROD activé..."
    # tee -a permet d'afficher dans le terminal ET d'écrire dans le fichier
    ansible-playbook -i hosts.yml setup-server.yml -e @secrets.yml --ask-pass --ask-become-pass | tee -a "$LOG_FILE"
fi

echo "-------------------------------------------------------"
echo "Déploiement terminé. Log complet disponible dans $LOG_FILE"
echo "-------------------------------------------------------"