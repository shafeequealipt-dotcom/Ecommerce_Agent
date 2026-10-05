#!/usr/bin/env bash
# Pull product folders the bot generated on the Oracle VM into the local NeedKart repo.
# Additive only: never deletes local folders, and never writes to the VM.
#
# Then publishes any product whose images are complete (publish_local.py --auto).
#
# Usage: ./sync_products.sh            (one-off; cron runs this every minute)
#        ./sync_products.sh --dry-run  (show what would be copied; never publishes)
set -euo pipefail

VM_HOST="${ECOM_VM_HOST:-ubuntu@68.233.109.57}"
VM_DIR="${ECOM_VM_PRODUCT_DIR:-/home/ubuntu/ecommerce-agent/needkart_product/}"
SSH_KEY="${ECOM_VM_SSH_KEY:-/Users/naash/Documents/Projects/Personal-2/Orcale/NEW KEYS/ssh-key-2026-07-22.key}"
LOCAL_DIR="${NEEDKART_PRODUCT_DIR:-/Users/naash/Documents/Projects/NeedKart/product}"

if [[ ! -f "$SSH_KEY" ]]; then
  echo "sync_products: SSH key not found at $SSH_KEY (set ECOM_VM_SSH_KEY)" >&2
  exit 1
fi

mkdir -p "$LOCAL_DIR"
rsync -az --update --itemize-changes "$@" \
  -e "ssh -i \"$SSH_KEY\" -o BatchMode=yes -o ConnectTimeout=15" \
  "$VM_HOST:$VM_DIR" "$LOCAL_DIR/"
echo "sync_products: $(date '+%Y-%m-%d %H:%M:%S') synced into $LOCAL_DIR"

# Auto-publish products whose images are in place. Skipped for --dry-run and when publish_local.py
# called us. Store credentials live only in ~/.zshrc, so load them the way a terminal would.
if [[ $# -eq 0 && -z "${ECOM_NO_AUTOPUBLISH:-}" ]]; then
  AGENT_DIR="$(cd "$(dirname "$0")" && pwd)"
  /bin/zsh -c 'source ~/.zshrc >/dev/null 2>&1; cd "$1" && python3 publish_local.py --auto' _ "$AGENT_DIR" || \
    echo "sync_products: auto-publish step failed"
fi
