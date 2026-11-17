#!/usr/bin/env bash
# Deploy the dashboard on a fresh Azure Ubuntu VM.
#
# Before the first run, in the Azure portal (VM -> Networking -> Add inbound port rule)
# allow TCP ports 80 and 443. Port 8000 no longer needs to be open.
#
# On the VM:
#   git clone https://github.com/NurstanDuisengaliyev/multi-camera-dashboard.git
#   cd multi-camera-dashboard
#   bash deploy/azure_setup.sh
#
# Safe to run again: it updates the code and rebuilds.
# Everything is also written to logs/azure_deploy.log (proof of deployment). Secrets are not.
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
git pull --ff-only
git log --oneline -1

step "3) Settings (.env)"
# Azure Instance Metadata Service: a local endpoint every Azure VM has.
PUBLIC_IP=$(curl -fs -H Metadata:true --noproxy '*' \
  "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text" \
  || true)
if [ ! -f .env ]; then
  if [ -z "$PUBLIC_IP" ]; then echo "Could not find the public IP; create .env by hand (see .env.example)"; exit 1; fi
  # 20.1.2.3 -> 20-1-2-3.sslip.io: a free host name pointing to our IP, so Caddy can get
  # a real (trusted) HTTPS certificate. Certificates are not given for bare IP addresses.
  cat > .env <<EOF
SITE_ADDRESS=${PUBLIC_IP//./-}.sslip.io
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=$(openssl rand -hex 12)
PHONE_TOKEN=$(openssl rand -hex 12)
EOF
  chmod 600 .env
  echo "created .env with a random dashboard password and phone token"
else
  echo ".env already exists, keeping it"
fi
SITE_ADDRESS=$(grep '^SITE_ADDRESS=' .env | cut -d= -f2-)

step "4) Build the image and start the containers (dashboard + caddy)"
sudo docker compose up -d --build
sudo docker images multicam-dashboard

step "5) Wait until the app answers /health"
for i in $(seq 1 30); do
  if curl -fs http://localhost:8000/health; then echo; break; fi
  echo "waiting... ($i)"; sleep 5
done

step "6) Check HTTPS (Caddy gets the certificate on first start, can take ~30 s)"
for i in $(seq 1 12); do
  if curl -fs -o /dev/null "https://$SITE_ADDRESS/health"; then echo "HTTPS OK"; break; fi
  echo "waiting for certificate... ($i)  (if this never works: are ports 80 and 443 open in Azure?)"
  sleep 5
done

step "7) Status"
sudo docker compose ps
sudo docker stats --no-stream
echo
echo "Dashboard:    https://$SITE_ADDRESS"
echo "Phone camera: https://$SITE_ADDRESS/phone"
echo "Login and phone token: see the .env file (cat .env)"
