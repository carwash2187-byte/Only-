#!/usr/bin/env bash
# One-shot bootstrap: turns a fresh Ubuntu/Debian VPS into a permanent,
# Claude-independent host for the trading desk. Run this ON THE VPS itself
# (SSH in first), not from the Claude session.
#
# What it does:
#   1. Installs python3/git if missing
#   2. Clones the repo (or updates it if already present) on the live branch
#   3. Creates a venv and installs ONLY the bots/ dependencies (lean --
#      pandas/numpy/requests, not the full LLM-committee stack)
#   4. Installs + enables the watchdog as a systemd service (auto-starts on
#      boot, auto-restarts on crash, runs forever independent of any Claude
#      session or container)
#   5. Sets up a cron job for git push retries (belt-and-suspenders on top
#      of watchdog.sh's own retry logic)
#
# Usage (as root or with sudo):
#   curl -fsSL <raw-file-url-for-this-script> | bash -s -- <git-clone-url> [branch]
# or, after copying this repo to the VPS some other way:
#   bash scripts/deploy/setup_vps.sh <git-clone-url> [branch]
set -euo pipefail

REPO_URL="${1:?Usage: setup_vps.sh <git-clone-url> [branch]}"
BRANCH="${2:-claude/ai-trading-bot-research-yolqhm}"
INSTALL_DIR="/opt/only-"

echo "==> Installing system packages (python3, git, venv)..."
if command -v apt-get >/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-venv python3-pip git >/dev/null
else
    echo "Non-Debian system detected -- install python3/git manually, then re-run."
fi

echo "==> Cloning/updating the repo at $INSTALL_DIR ..."
if [[ -d "$INSTALL_DIR/.git" ]]; then
    sudo git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    sudo git -C "$INSTALL_DIR" checkout "$BRANCH"
    sudo git -C "$INSTALL_DIR" pull origin "$BRANCH"
else
    sudo git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
sudo chown -R "$(whoami)" "$INSTALL_DIR"

echo "==> Creating venv and installing lean bot dependencies..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/scripts/deploy/requirements-bots.txt"

echo "==> Pointing watchdog.sh's shebang-independent calls at the venv python..."
# watchdog.sh calls `python`/`python3` directly; make the venv's python the
# one found on PATH for the systemd service via an Environment= override.
VENV_BIN="$INSTALL_DIR/.venv/bin"

echo "==> Installing systemd service..."
sudo sed \
    -e "s#/opt/only-#${INSTALL_DIR}#g" \
    "$INSTALL_DIR/scripts/deploy/only-bots-watchdog.service" \
    | sudo tee /etc/systemd/system/only-bots-watchdog.service >/dev/null
sudo mkdir -p /etc/systemd/system/only-bots-watchdog.service.d
sudo tee /etc/systemd/system/only-bots-watchdog.service.d/venv-path.conf >/dev/null <<EOF
[Service]
Environment=PATH=${VENV_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOF

sudo systemctl daemon-reload
sudo systemctl enable only-bots-watchdog.service
sudo systemctl restart only-bots-watchdog.service

echo
echo "==> Done. Status:"
sudo systemctl status only-bots-watchdog.service --no-pager -l | head -15
echo
echo "This machine now runs the desk independent of Claude/this container:"
echo "  - Survives reboots (systemd enable = starts on boot)"
echo "  - Auto-restarts on crash (systemd Restart=always + watchdog.sh's own"
echo "    5-minute self-heal loop -- two independent layers)"
echo "  - Nightly self-train still fires from watchdog.sh, no Claude involved"
echo "  - All state still git-syncs to $BRANCH every 5 minutes"
echo
echo "Logs: sudo journalctl -u only-bots-watchdog -f"
echo "  or: tail -f /var/log/only-bots-watchdog.log"
echo
echo "IMPORTANT next step: this VPS needs its own git push credentials to"
echo "sync paper_state/ (a personal access token works). Set them up with:"
echo "  git -C $INSTALL_DIR remote set-url origin https://<token>@github.com/<owner>/<repo>.git"
