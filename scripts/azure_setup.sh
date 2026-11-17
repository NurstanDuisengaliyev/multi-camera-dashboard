#!/usr/bin/env bash
# Deploy the dashboard on a fresh Azure Ubuntu VM.
#
# On the VM:
#   git clone https://github.com/NurstanDuisengaliyev/multi-camera-dashboard.git
#   cd multi-camera-dashboard
#   bash scripts/azure_setup.sh
#
# Safe to run again: it updates the code and rebuilds.
# Everything is also written to logs/azure_deploy.log (proof of deployment).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs data
exec > >(tee -a logs/azure_deploy.log) 2>&1

step() { echo; echo "===== $(date '+%Y-%m-%d %H:%M:%S') $* ====="; }

step "System info"
uname -a
grep PRETTY_NAME /etc/os-release
echo "CPU cores: $(nproc)"
free -h
df -h /

step "1) Install Docker (from Ubuntu packages)"
if ! command -v docker >/dev/null; then
  sudo apt-get update -q
  sudo apt-get install -y -q docker.io docker-compose-v2
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"   # run docker without sudo after next login
fi
sudo docker --version
sudo docker compose version

step "2) Get the latest code"
git pull --ff-only || true
git log --oneline -1

step "3) Build the image and start the container"
sudo docker compose up -d --build
sudo docker images multicam-dashboard

step "4) Wait until the app answers /health"
for i in $(seq 1 30); do
  if curl -fs http://localhost:8000/health; then echo; break; fi
  echo "waiting... ($i)"; sleep 5
done

step "5) Status"
sudo docker compose ps
sudo docker stats --no-stream
# Azure Instance Metadata Service: a local endpoint every Azure VM has.
PUBLIC_IP=$(curl -fs -H Metadata:true --noproxy '*' \
  "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text" \
  || echo "<VM public IP>")
echo
echo "Dashboard: http://$PUBLIC_IP:8000"
