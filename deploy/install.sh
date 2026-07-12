#!/usr/bin/env bash
# Install Telegram Business neuroagent on Ubuntu VPS.
# Run as root: sudo bash deploy/install.sh

set -euo pipefail

APP_USER="${APP_USER:-tgbot}"
APP_DIR="${APP_DIR:-/opt/telegram-business-bot}"
REPO_URL="${REPO_URL:-}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip git curl \
  libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 \
  libasound2t64 libxshmfence1 fonts-liberation

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  echo "==> Creating user ${APP_USER}"
  useradd --system --create-home --home-dir "/home/${APP_USER}" --shell /usr/sbin/nologin "${APP_USER}"
fi

echo "==> Preparing ${APP_DIR}"
mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

if [[ -n "${REPO_URL}" ]]; then
  if [[ ! -d "${APP_DIR}/.git" ]]; then
    echo "==> Cloning ${REPO_URL}"
    sudo -u "${APP_USER}" git clone "${REPO_URL}" "${APP_DIR}"
  else
    echo "==> Updating existing repo in ${APP_DIR}"
    sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only
  fi
elif [[ ! -f "${APP_DIR}/bot.py" ]]; then
  echo "Copy project files to ${APP_DIR} or set REPO_URL=https://..." >&2
  exit 1
fi

echo "==> Creating virtualenv and installing Python deps"
sudo -u "${APP_USER}" python3 -m venv "${APP_DIR}/venv"
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install --upgrade pip
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/integrations/syntx/requirements-syntx.txt"

echo "==> Installing Playwright Chromium"
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/playwright" install chromium
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/playwright" install-deps chromium || true

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "==> Creating .env from env.example (fill secrets before start)"
  cp "${APP_DIR}/env.example" "${APP_DIR}/.env"
  chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
fi

echo "==> Installing systemd units"
install -m 644 "${APP_DIR}/deploy/syntx-bridge.service" /etc/systemd/system/syntx-bridge.service
install -m 644 "${APP_DIR}/deploy/telegram-business-bot.service" /etc/systemd/system/telegram-business-bot.service
systemctl daemon-reload
systemctl enable syntx-bridge.service telegram-business-bot.service

echo ""
echo "Done. Next steps:"
echo "  1. Edit ${APP_DIR}/.env"
echo "  2. Fill knowledge/ templates"
echo "  3. First SyntX login (headed): see deploy/README.md"
echo "  4. systemctl start syntx-bridge telegram-business-bot"
echo "  5. curl -sf http://127.0.0.1:8000/health"
