# Azure VM guide

How to deploy, use and maintain the dashboard on the Azure VM.
Everything below is run **on the VM** (after `ssh`) unless it says "Azure portal" or "laptop".

---

## 1. First deployment of this version (Caddy + HTTPS + phones)

### 1.1 Azure portal: open the ports
VM → **Networking** → **Add inbound port rule**:

| Port | Protocol | Why |
|---|---|---|
| 80  | TCP | Caddy gets the HTTPS certificate here, and redirects `http://` to `https://` |
| 443 | TCP | HTTPS: the dashboard and the phone page |

Old rules for **8000** and **8443** are not needed any more and can be deleted.

Same thing with the Azure CLI (on the laptop):
```bash
az vm open-port -g <resource-group> -n <vm-name> --port 80  --priority 1010
az vm open-port -g <resource-group> -n <vm-name> --port 443 --priority 1020
```

### 1.2 Connect and get the new code
```bash
ssh <user>@<VM-public-IP>
cd multi-camera-dashboard
git checkout main
git pull
```
The deploy script moved from `scripts/` to `deploy/`, so pull **before** running it.
(This needs the pull request with these changes merged into `main`. Before it is merged,
use `git fetch origin claude/intelligent-ritchie-p2c31b && git checkout claude/intelligent-ritchie-p2c31b`.)

### 1.3 Deploy
```bash
bash deploy/azure_setup.sh
```
What it does:
1. installs Docker if missing;
2. `git pull`;
3. creates `.env` on the first run: random dashboard password, random phone token, and
   `SITE_ADDRESS=<ip-with-dashes>.sslip.io` (free host name pointing to the VM's IP);
4. builds the image and starts two containers: `multicam-dashboard` and `multicam-caddy`
   (the first build takes several minutes: PyTorch + YOLO are downloaded);
5. waits for `/health` and for HTTPS to work;
6. prints the addresses.

Expected end of the output:
```
HTTPS OK
Dashboard:    https://20-1-2-3.sslip.io
Phone camera: https://20-1-2-3.sslip.io/phone
```

### 1.4 Get the login and token
```bash
cat .env
```
`.env` holds the secrets. It is not in git, and the deploy log doesn't contain them. Don't share it.

### 1.5 Try it
- **Laptop:** open `https://<ip-with-dashes>.sslip.io`, log in as `admin` with `DASHBOARD_PASSWORD`.
- **Phone:** open the same address, log in, tap **📱 Add a phone camera**, enter a name,
  tap **Start streaming**, allow the camera. The phone appears on the dashboard.
- **Someone else's phone:** send them the link behind the "Add a phone camera" button
  (`https://.../phone#token=...`). It lets them stream, but not see the dashboard.

---

## 2. Everyday commands

```bash
cd multi-camera-dashboard
sudo docker compose ps                  # are both containers running / healthy?
sudo docker compose logs -f dashboard   # app logs (cameras, alerts, phones)
sudo docker compose logs -f caddy       # HTTPS / certificate logs
sudo docker stats --no-stream           # CPU and memory use
sudo docker compose restart dashboard   # restart the app (e.g. after editing config.yaml)
sudo docker compose down                # stop everything
sudo docker compose up -d               # start again (no rebuild)
```
Logs are also in `logs/app.log`, the event database is `data/events.db`.
The containers restart by themselves after a crash or a VM reboot.

## 3. Updating to a newer version of the code

```bash
cd multi-camera-dashboard
bash deploy/azure_setup.sh      # pulls, rebuilds, restarts; keeps .env, data and certificates
```

## 4. Changing settings

| What | How |
|---|---|
| Thresholds, video cameras, phone limits | edit `config.yaml`, then `sudo docker compose restart dashboard` |
| Dashboard password | edit `DASHBOARD_PASSWORD` in `.env`, then `sudo docker compose up -d` |
| Phone token (e.g. a link was shared too widely) | edit `PHONE_TOKEN` in `.env`, then `sudo docker compose up -d`; phones must use the new link |
| Make a random value | `openssl rand -hex 12` |

`docker compose up -d` (not `restart`) is needed after editing `.env`, so the containers
are recreated with the new values.

## 5. If the VM's public IP changes

Azure can give a new IP after the VM was deallocated (unless the IP is "Static").
Then the `sslip.io` address no longer points to the VM. Fix:
```bash
nano .env                       # set SITE_ADDRESS=<new-ip-with-dashes>.sslip.io
sudo docker compose up -d       # Caddy gets a certificate for the new name
```
To avoid this: Azure portal → the VM's Public IP address → Configuration → Assignment: **Static**.

---

## 6. Troubleshooting

| Problem | What to check |
|---|---|
| Script keeps printing "waiting for certificate" | Ports 80 and 443 not open in Azure (check the network security group of the VM's network interface **and** of the subnet). Then `sudo docker compose logs caddy` |
| Browser: "connection not private" on the sslip.io address | Caddy has no certificate yet: `sudo docker compose logs caddy` (look for "certificate obtained") |
| Login box keeps coming back | Wrong password: `cat .env`. If `.env` was edited, run `sudo docker compose up -d` |
| Phone page: "Wrong phone token" | Token on the phone is old: open the link from the dashboard button again |
| Phone page: "Phone streaming is switched off" | `PHONE_TOKEN` is empty in `.env` |
| Phone page: "The camera needs HTTPS" | The page was opened with `http://` or by `IP:8000`; use `https://<ip-with-dashes>.sslip.io/phone` |
| Phone: "Camera permission denied" | iPhone Settings → Safari → Camera → Allow (or Ask), then reload |
| Phone streams but FPS is low | Slow network or VM busy: choose 480 px / 10 FPS on the phone page; check `sudo docker stats` |
| Phone card shows "Waiting for phone" | Safari went to the background or the screen locked; open the page again (it reconnects) |
| Dashboard container keeps restarting | `sudo docker compose logs dashboard` |
| `git pull` refuses | `git status`: local changes to tracked files. Save them elsewhere, then `git checkout -- <file>` |
| Build fails: "no space left on device" | `df -h`; free space with `sudo docker system prune` |
| Port 80 or 443 "already in use" | Another web server runs on the VM: `sudo ss -ltnp \| grep -E ':80 \|:443 '`, stop it (e.g. `sudo systemctl disable --now nginx`) |

Useful checks on the VM:
```bash
curl -s http://localhost:8000/health                  # app itself (bypasses Caddy)
curl -sI https://$(grep SITE_ADDRESS .env | cut -d= -f2)/health    # through Caddy and HTTPS
```

## 7. Cost note

Stop (deallocate) the VM in the Azure portal when you don't need it; a stopped but not
deallocated VM is still billed. With a dynamic IP, see section 5 after starting it again.
