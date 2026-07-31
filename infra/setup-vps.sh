#!/usr/bin/env bash
# Prepara a droplet para receber deploy. Roda UMA VEZ, na máquina.
#
#   bash infra/setup-vps.sh
#
# O deploy em si NÃO passa por aqui: quem sobe código é o GitHub Actions.
# Este script só deixa a máquina pronta para receber.
set -euo pipefail

echo "==> Docker"
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
else
    echo "    ja instalado: $(docker --version)"
fi
systemctl enable --now docker

echo "==> Diretorio da aplicacao"
mkdir -p /opt/pmlab/data
# O container roda como UID 1000 (usuario sem privilegio). O volume vem do
# host, entao precisa pertencer a esse UID ou o DuckDB nao consegue escrever.
chown -R 1000:1000 /opt/pmlab/data

echo "==> Firewall"
# O painel NAO tem autenticacao. Ele fica preso em 127.0.0.1 pelo compose, e o
# firewall e a segunda tranca. Para ver de casa, use tunel:
#     ssh -L 8787:127.0.0.1:8787 root@IP
if command -v ufw >/dev/null 2>&1; then
    ufw allow OpenSSH >/dev/null 2>&1 || true
    ufw --force enable >/dev/null 2>&1 || true
    ufw status | head -5
else
    echo "    ufw ausente; confira que a porta 8787 nao esta publica"
fi

echo "==> Swap"
# 2 GB de RAM com polars e pyarrow carregados fica apertado. Sem swap, um pico
# mata o processo no meio de uma coleta de dias — e o OOM killer nao avisa.
if ! swapon --show | grep -q .; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "    2G de swap criados"
else
    echo "    swap ja existe"
fi

echo "==> Fuso horario"
timedatectl set-timezone UTC

echo
echo "Pronto. A maquina aceita deploy."
echo "Falta configurar os secrets no GitHub: SSH_HOST, SSH_USER, SSH_PRIVATE_KEY."
