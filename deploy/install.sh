#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
  echo 'Запустите установщик через sudo bash deploy/install.sh' >&2
  exit 1
fi
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$source_dir/pyproject.toml" || ! -f "$source_dir/requirements.lock" ]]; then
  echo 'Нужен полный каталог исходников Scout Trading.' >&2
  exit 1
fi
apt-get update
apt-get install -y python3 python3-venv nodejs npm ca-certificates
if ! id scout >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/scout --shell /usr/sbin/nologin scout
fi
if [[ "$(getent passwd scout | cut -d: -f6)" != '/var/lib/scout' ]]; then
  echo 'Пользователь scout уже существует с другим домашним каталогом. Установка остановлена.' >&2
  exit 1
fi
install -d -m 755 /opt/scout-trading
install -d -m 750 -o root -g scout /etc/scout-trading
install -d -m 700 -o scout -g scout /var/lib/scout
if [[ "$source_dir" != '/opt/scout-trading' ]]; then
  cp -R -- "$source_dir/scout" /opt/scout-trading/
  install -m 644 "$source_dir/pyproject.toml" "$source_dir/requirements.lock" /opt/scout-trading/
fi
npm install -g @openai/codex@0.161.0
python3 -m venv /opt/scout-trading/.venv
/opt/scout-trading/.venv/bin/pip install -r /opt/scout-trading/requirements.lock
/opt/scout-trading/.venv/bin/pip install --no-deps /opt/scout-trading
if [[ ! -f /etc/scout-trading/env ]]; then
  install -m 640 -o root -g scout "$source_dir/.env.example" /etc/scout-trading/env
  sed -i 's|^DATA_DIR=.*|DATA_DIR=/var/lib/scout|' /etc/scout-trading/env
fi
install -m 644 "$source_dir/deploy/scout-trading.service" /etc/systemd/system/scout-trading.service
systemctl daemon-reload
echo 'Установлено. Заполните /etc/scout-trading/env, авторизуйте Codex под scout и запустите systemctl enable --now scout-trading. Команды — в README.md.'
