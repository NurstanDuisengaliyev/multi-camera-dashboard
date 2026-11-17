#!/usr/bin/env bash
# Create the Azure VM for the dashboard with the Azure CLI (run on your own computer).
# This is exactly how our VM was created; the portal "Create a virtual machine" form does the same.
#
#   az login
#   bash scripts/azure_create_vm.sh
#
# Notes from our Azure for Students subscription:
#  - A policy allows only 5 regions (denmarkeast, centralindia, indiasouthcentral, norwayeast, austriaeast).
#    Check yours:  az policy assignment list --query "[].parameters.listOfAllowedLocations.value"
#  - Newest sizes (e.g. D2als_v7) have 0 quota; older families (Dasv4, Dsv3, Fsv2) have 4 vCPUs.
#    Check:        az vm list-usage -l centralindia -o table
set -euo pipefail

RG=multicam-rg
VM=multicam-vm
LOCATION=centralindia            # closest allowed region to Kazakhstan
SIZE=Standard_D2as_v4            # 2 vCPU (AMD), 8 GB RAM, full cores (not burstable)
KEY=~/.ssh/multicam-vm_key

# SSH key pair (private key stays on this computer, public key goes to the VM).
[ -f "$KEY" ] || ssh-keygen -t ed25519 -f "$KEY" -N "" -C "$VM"

az group create -n "$RG" -l "$LOCATION" -o none

# The VM + its disk, network card, virtual network, public IP and firewall (NSG) with SSH open.
az vm create -g "$RG" -n "$VM" -l "$LOCATION" \
  --image Canonical:ubuntu-24_04-lts:server:latest --size "$SIZE" \
  --admin-username azureuser --ssh-key-values "$KEY.pub" \
  --storage-sku StandardSSD_LRS --os-disk-size-gb 30 \
  --os-disk-delete-option Delete --nic-delete-option Delete \
  --public-ip-sku Standard --nsg-rule SSH

# Firewall rules for Caddy: 80 (certificate + redirect to HTTPS) and 443 (HTTPS).
# The app's port 8000 stays closed; it is only reachable from inside the VM.
NSG="${VM}NSG"
az network nsg rule create -g "$RG" --nsg-name "$NSG" -n allow-http-80 --priority 1020 \
  --protocol Tcp --destination-port-ranges 80 --access Allow --direction Inbound
az network nsg rule create -g "$RG" --nsg-name "$NSG" -n allow-https-443 --priority 1030 \
  --protocol Tcp --destination-port-ranges 443 --access Allow --direction Inbound

# Auto-shutdown every day at 19:00 UTC+5 (protects the credit if we forget to stop the VM).
# Created as a resource in the VM's region (`az vm auto-shutdown` uses the resource group's region).
az provider register -n Microsoft.DevTestLab --wait
VM_ID=$(az vm show -g "$RG" -n "$VM" --query id -o tsv)
az resource create -g "$RG" -n "shutdown-computevm-$VM" -l "$LOCATION" \
  --resource-type Microsoft.DevTestLab/schedules \
  --properties "{\"status\":\"Enabled\",\"taskType\":\"ComputeVmShutdownTask\",
    \"dailyRecurrence\":{\"time\":\"1900\"},\"timeZoneId\":\"West Asia Standard Time\",
    \"targetResourceId\":\"$VM_ID\",\"notificationSettings\":{\"status\":\"Disabled\"}}"

IP=$(az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv)
echo
echo "VM ready:   ssh -i $KEY azureuser@$IP"
echo "Then:       git clone <repo> && cd multi-camera-dashboard && bash deploy/azure_setup.sh"
echo "Dashboard:  https://${IP//./-}.sslip.io"
echo
echo "Stop (no compute cost):  az vm deallocate -g $RG -n $VM"
echo "Start:                   az vm start -g $RG -n $VM"
echo "Delete everything:       az group delete -n $RG"
