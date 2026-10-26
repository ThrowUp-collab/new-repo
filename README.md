# new-repo

Мост между Arena-агентом и OpenClaw.

- `docs/arena-x-openclaw.md` — можно ли соединить Arena и OpenClaw: четыре рабочих паттерна, безопасность.
- `docs/hosting-4gb-colab.md` — слабое железо и бесплатный Colab/Drive: что реально работает, а что нет.
- `openclaw/openclaw.json` — шаблон конфига: бесплатный «мозг» + fallback-цепочка + hardened gateway.
- `openclaw/skills/arena-handoff/SKILL.md` — скилл, который учит OpenClaw передавать задачи в Arena.
- `notebooks/colab_free_brain.ipynb` — Colab: проверка ключей с настоящим tool-calling + генератор конфига.
- `scripts/backup-openclaw-to-drive.sh` — шифрованный бэкап состояния в Google Drive (rclone + age).
- `tasks/` — очередь задач между агентами (протокол в `tasks/README.md`).
