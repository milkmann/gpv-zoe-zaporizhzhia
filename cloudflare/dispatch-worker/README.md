# Cloudflare Cron Dispatcher (`zoe-gpv-dispatch`)

Cloudflare Worker із Cron Trigger (`*/10 * * * *`), що кожні 10 хвилин викликає GitHub `workflow_dispatch` для `.github/workflows/update.yml`, щоб оновлення графіків не залежали від затримок планувальника GitHub Actions або доступності домашнього Home Assistant.

Публічний HTTP-запит (`fetch`) лише повертає статус конфігурації (`token_configured`) і ніколи не запускає workflow.

## Налаштування токена GitHub (`GH_TOKEN`)

1. У GitHub: **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Repository access:** **Only select repositories** → `milkmann/gpv-zoe-zaporizhzhia`.
3. **Permissions → Repository permissions:** **Actions → Read and write** (Metadata: Read-only додається автоматично).
4. Задайте термін дії та скопіюйте токен.

## Додавання секрету у Cloudflare

1. Відкрийте **Cloudflare Dashboard → Workers & Pages → `zoe-gpv-dispatch` → Settings → Variables and Secrets → Add**.
2. Тип: **Secret**, ім’я: `GH_TOKEN`, значення: створений токен GitHub → **Deploy**.

## Повторний деплой коду (за потреби)

Воркер вже задеплоєно через Cloudflare API з тригером `*/10 * * * *`. За ручного оновлення через Wrangler:

```bash
wrangler deploy worker.js --name zoe-gpv-dispatch --compatibility-date 2026-11-01
```
