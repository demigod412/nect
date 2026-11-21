#!/usr/bin/env bash
# ==============================================================================
# NECTRA Platform — One-Command Production Updater (Python Backend)
# Run on your server to pull the latest updates from GitHub and restart services:
#   cd /var/www/nectra-platform && sudo bash update.sh
# ==============================================================================

set -euo pipefail

# ANSI Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "\n${CYAN}================================================================${NC}"
echo -e "${CYAN}             Updating Platform to Latest Version                ${NC}"
echo -e "${CYAN}================================================================${NC}\n"

# 1. Determine working directory
INSTALL_DIR="/var/www/nectra-platform"
if [ ! -d "$INSTALL_DIR" ]; then
  INSTALL_DIR="$(pwd)"
fi

cd "$INSTALL_DIR"
echo -e "${BOLD}[1/4] Operating in: ${INSTALL_DIR}${NC}"

# Auto-repair git configuration if .git directory was omitted
if [ ! -d "$INSTALL_DIR/.git" ]; then
  echo -e "${YELLOW}[!] Git metadata missing. Initializing git link to GitHub repository...${NC}"
  git init
  git remote add origin https://github.com/demigod412/nectra.git 2>/dev/null || git remote set-url origin https://github.com/demigod412/nectra.git
  git branch -M main || true
fi

# 2. Pull latest code from GitHub main branch
echo -e "\n${BOLD}[2/4] Fetching latest commits from GitHub...${NC}"
git fetch origin main
git reset --hard origin/main
git branch --set-upstream-to=origin/main main || true
echo -e "${GREEN}[✓] Successfully updated to latest commit $(git rev-parse --short HEAD).${NC}"

# 3. Update Python dependencies
echo -e "\n${BOLD}[3/4] Updating Python dependencies...${NC}"
if command -v pip3 >/dev/null 2>&1; then
  pip3 install -r py_server/requirements.txt
elif command -v python3 >/dev/null 2>&1; then
  python3 -m pip install -r py_server/requirements.txt
fi

# 4. Update systemd service configuration if present
if [ -f "/etc/systemd/system/nectra.service" ]; then
  CURRENT_USER=$(grep "^User=" /etc/systemd/system/nectra.service | cut -d= -f2 || echo "root")
  cat > /etc/systemd/system/nectra.service << EOF
[Unit]
Description=NECTRA Platform Service (Python FastAPI)
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 -m uvicorn app:app --app-dir py_server --host 0.0.0.0 --port 3000
Restart=always
RestartSec=5s

StartLimitIntervalSec=60s
StartLimitBurst=5

Environment=PORT=3000
KillMode=mixed
TimeoutStopSec=15s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
fi

# 5. Restart supervisor service
echo -e "\n${BOLD}[4/4] Restarting platform service...${NC}"
RESTARTED=0
if systemctl list-unit-files 2>/dev/null | grep -q "nectra.service"; then
  systemctl daemon-reload
  systemctl restart nectra.service
  sleep 2
  if systemctl is-active --quiet nectra.service; then
    echo -e "${GREEN}[✓] Systemd service nectra.service is ACTIVE and running!${NC}"
    RESTARTED=1
  else
    echo -e "${RED}[!] Systemd service failed to start. Checking journal logs:${NC}"
    journalctl -u nectra.service -n 20 --no-pager
    exit 1
  fi
fi

if [ "$RESTARTED" -eq 0 ]; then
  echo -e "${YELLOW}[!] nectra.service not active. You can run manually via:${NC}"
  echo -e "    python3 -m uvicorn app:app --app-dir py_server --host 0.0.0.0 --port 3000"
fi

echo -e "\n${GREEN}================================================================${NC}"
echo -e "${GREEN}       ✓ Platform update completed successfully!                ${NC}"
echo -e "${GREEN}================================================================${NC}\n"
