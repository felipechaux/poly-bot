#!/usr/bin/env bash
# One-time server setup (Ubuntu 24.04): swap, Docker, repo checkout. Needs bash (not sh):
#   ssh ubuntu@HOST "sudo bash -s" < deploy/bootstrap.sh
# (Lightsail user-data runs under /bin/sh, so run it over SSH instead.)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# 1 GB swap (micro_3_0 has 1 GB RAM)
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 && echo 'vm.swappiness=10' > /etc/sysctl.d/99-swap.conf
fi

timedatectl set-timezone America/Bogota
apt-get update -y
apt-get install -y docker.io docker-compose-v2 git
systemctl enable --now docker
usermod -aG docker ubuntu

sudo -u ubuntu git clone https://github.com/felipechaux/poly-bot.git /home/ubuntu/invest-bot || true
sudo -u ubuntu mkdir -p /home/ubuntu/invest-bot/data
touch /var/log/bootstrap-done
