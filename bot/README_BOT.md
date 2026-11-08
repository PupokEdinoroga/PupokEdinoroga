# Торговый бот: запуск на VPS

Бот торгует **только проверенными стратегиями** и сам решает, куда пускать деньги:
1. Новая стратегия торгует **на бумаге** (виртуально, по реальным ценам).
2. После 30+ сделок — **экзамен**: средний результат после издержек ≥ 0,3% и t-стат ≥ 2.
3. Сдала — бот пишет в Telegram; ты включаешь `mode: live` в `config.yaml`.
4. Живая стратегия начала терять (последние 30 сделок в среднем хуже −0,5%) — бот **сам** возвращает её на бумагу.

Стратегии сейчас:
| Стратегия | Что делает | Режим по умолчанию |
|---|---|---|
| `listing_short` | шорт нового листинга перпа через 24 ч, держит 3 дня; 20 $ позиция, плечо 2, изолированная маржа (максимум потери на сделку 10 $) | paper |
| `listing_short_50m` | то же, но только при обороте ≥ 50 млн $ за сутки (сравнение вариантов) | paper |
| `hour_drift` | в 00:00 и 16:00 UTC лонг корзины из 5 самых оборотных альтов на 1 час (смена 4ч свечи) | только paper |
| `funding_arb` | лонг спот + шорт перп при высоком фандинге | только paper |

**Ожидания честно:** на 100 $ это порядка нескольких долларов в месяц в хорошем сценарии, бывают минусовые недели.
Первые 2 недели — только бумага.

---

## 1. VPS
- Любой провайдер (Hetzner, DigitalOcean, Vultr, Aeza и т. п.), самый дешёвый тариф: 1 CPU, 1 ГБ RAM, Ubuntu 22.04/24.04.
- **Регион выбирай в стране, где Binance официально работает и где это разрешено правилами твоего аккаунта.**
  Не используй VPS, чтобы обойти запрет Binance для твоей страны — биржа может заблокировать аккаунт вместе с деньгами.
- Проверка, что Binance доступен с сервера:
  ```bash
  curl -s https://fapi.binance.com/fapi/v1/ping   # должно вернуть {}
  ```
  Если ответ `451` или `403` — регион не подходит, смени регион VPS.

## 2. Установка
```bash
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/PupokEdinoroga/PupokEdinoroga.git
cd PupokEdinoroga && git checkout claude/backtester-breakout-continue-uwflc3
cd bot
python3 -m venv .venv && . .venv/bin/activate
pip install ccxt pyyaml requests
python test_bot.py            # должно закончиться «ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ»
cp config.example.yaml config.yaml
```

## 3. Telegram (2 минуты)
1. Напиши `@BotFather` → `/newbot` → получишь **token**.
2. Напиши своему новому боту любое сообщение.
3. Открой в браузере `https://api.telegram.org/bot<TOKEN>/getUpdates` и найди `"chat":{"id":...}` — это **chat_id**.
4. Впиши оба в `config.yaml` → раздел `telegram`.

## 4. Ключи Binance (только для живого режима — для бумаги не нужны)
- Binance → Управление API → создать ключ.
- Разрешения: **«Чтение» и «Фьючерсы»**. **НЕ включай «Вывод средств».**
- Ограничь ключ IP-адресом своего VPS.
- Впиши `api_key` и `api_secret` в `config.yaml`. Файл `config.yaml` никому не отправляй и не загружай в git.

## 5. Пробный запуск
```bash
python main.py --once       # один проход: в Telegram придёт «Бот запущен»
```

## 6. Работа 24/7 (systemd)
```bash
sudo tee /etc/systemd/system/tradebot.service >/dev/null <<EOF
[Unit]
Description=trade bot
After=network-online.target

[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python main.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now tradebot
journalctl -u tradebot -f       # живой журнал (выход: Ctrl+C)
```

## Кнопки в Telegram
После запуска бот присылает клавиатуру (отвечает только на твой chat_id):
📊 Отчёт · 📂 Позиции (с текущим результатом) · 📼 Регистратор (объём, свободное место, последняя запись) ·
🩺 Состояние (жив ли бот, последняя ошибка) · ⏸ Пауза / ▶️ Продолжить (запрет/разрешение новых сделок).
Команды текстом: /report /positions /recorder /status /pause /resume.

## Управление
| Что | Как |
|---|---|
| Остановить новые сделки, не трогая открытые | `touch STOP` в папке `bot` (убрать: `rm STOP`) |
| Остановить бота | `sudo systemctl stop tradebot` |
| Отчёт сейчас | `.venv/bin/python main.py --report` |
| Перевести стратегию в деньги | в `config.yaml`: `mode: live`, затем `sudo systemctl restart tradebot` |
| Обновить код | `git pull && sudo systemctl restart tradebot` |
| Вернуть стратегию после автоотключения | `.venv/bin/python main.py --reset listing_short` |
| Отключить стратегию | в `config.yaml`: `mode: off` |

## Лимиты риска (`config.yaml` → `risk`)
- `capital_usd` — сколько денег отдано боту (сумма маржи открытых живых позиций не превысит её);
- `max_open_positions` — максимум живых позиций одновременно;
- `daily_loss_limit_usd` — при таком убытке за сутки (UTC) живые входы останавливаются до следующего дня.

---

## Регистратор стакана (для проверки скальпинга по плотностям)
Пишет ленту сделок и стакан самых волатильных/активных монет, размечает аномальные заявки (≥ 10× медианы) и «переставляшей». Через 1–2 недели — проверка стратегий на этих данных:
```bash
.venv/bin/pip install websocket-client
python test_recorder.py            # «ВСЕ ПРОВЕРКИ РЕГИСТРАТОРА ПРОЙДЕНЫ»
sudo tee /etc/systemd/system/obrecorder.service >/dev/null <<UNIT
[Unit]
Description=order book recorder
After=network-online.target

[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python recorder.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now obrecorder
journalctl -u obrecorder -f
```
- Место: ~0,3–0,6 ГБ в день на 20 монет (gzip). Нужен VPS с диском от 20 ГБ; старше `keep_days` удаляется само.
- Раз в сутки в Telegram приходит сводка: сколько записано сделок, снимков, аномальных заявок.
- Проверка стратегий по записанному: `.venv/bin/python analyze_ob.py ob_data` (или пришли архив папки `ob_data` — разберём вместе).
