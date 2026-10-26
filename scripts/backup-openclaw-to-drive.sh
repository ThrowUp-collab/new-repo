#!/usr/bin/env bash
# Ежедневный бэкап состояния OpenClaw в Google Drive (через rclone) с шифрованием.
#
# Установка:
#   sudo apt install rclone age          # Debian/Ubuntu
#   brew install rclone age              # macOS
#   rclone config                        # new remote -> drive -> OAuth
#   age-keygen -o ~/.age/openclaw.key    # один раз, ключ храни ОТДЕЛЬНО от Drive
#   chmod +x backup-openclaw-to-drive.sh
#
# Запуск по cron (на VPS, а не на ноутбуке, который спит):
#   0 4 * * * /home/user/bin/backup-openclaw-to-drive.sh >> /home/user/backup.log 2>&1
#
# Восстановление:
#   age -d -i ~/.age/openclaw.key openclaw-2026-10-26.tar.gz.age | tar -xzf - -C ~
#   openclaw doctor --fix && openclaw gateway restart

set -euo pipefail

STATE_DIR="${OPENCLAW_STATE_DIR:-$HOME/.openclaw}"
REMOTE="${BACKUP_REMOTE:-gdrive:openclaw-backups}"
RECIPIENT_FILE="${AGE_RECIPIENT_FILE:-$HOME/.age/openclaw.pub}"
KEEP_DAYS="${KEEP_DAYS:-7}"
STAMP="$(date +%F)"
TMP_DIR="$(mktemp -d)"
ARCHIVE="$TMP_DIR/openclaw-$STAMP.tar.gz"

cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

die() { echo "ОШИБКА: $*" >&2; exit 1; }

command -v tar    >/dev/null || die "нет tar"
command -v rclone >/dev/null || die "нет rclone (apt install rclone)"
command -v age    >/dev/null || die "нет age (apt install age)"
[ -d "$STATE_DIR" ]            || die "нет каталога состояния: $STATE_DIR"
[ -f "$RECIPIENT_FILE" ]       || die "нет публичного ключа age: $RECIPIENT_FILE (age-keygen -o ~/.age/openclaw.key)"

# Что кладём в архив: конфиг, память, скиллы, сессии.
# Что НЕ кладём: кэши, воркtree, логи — они большие и восстанавливаются сами.
echo "[1/4] Собираю архив из $STATE_DIR"
tar -czf "$ARCHIVE" \
  -C "$STATE_DIR" \
  --exclude='./cache' \
  --exclude='./logs' \
  --exclude='./tmp' \
  --exclude='./worktrees' \
  --exclude='./node_modules' \
  . 2>/dev/null || true

SIZE_KB=$(( $(wc -c < "$ARCHIVE") / 1024 ))
[ "$SIZE_KB" -gt 8 ] || die "архив подозрительно мал ($SIZE_KB КБ) — вероятно, каталог пуст"

echo "[2/4] Шифрую (age, $SIZE_KB КБ)"
age --encrypt --recipients-file "$RECIPIENT_FILE" \
    --output "$ARCHIVE.age" "$ARCHIVE"
rm -f "$ARCHIVE"

echo "[3/4] Загружаю в $REMOTE"
rclone copy "$ARCHIVE.age" "$REMOTE" --quiet

echo "[4/4] Ротация: удаляю бэкапы старше $KEEP_DAYS дней"
rclone delete "$REMOTE" --min-age "${KEEP_DAYS}d" --include "openclaw-*.tar.gz.age" --quiet || true

echo "Готово: $REMOTE/$(basename "$ARCHIVE").age"
echo "Проверь восстановление хотя бы раз: age -d -i <ключ> <файл> | tar -tzf - | head"
