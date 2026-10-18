# ⚡ GPV ZOE Zaporizhzhia — Дані та наочні графіки відключень для Запоріжжя

[![Update Outage Data](https://github.com/milkmann/gpv-zoe-zaporizhzhia/actions/workflows/update.yml/badge.svg)](https://github.com/milkmann/gpv-zoe-zaporizhzhia/actions/workflows/update.yml)

Автоматичний парсер та генератор актуальних даних погодинних відключень електроенергії (ГПВ) для споживачів міста **Запоріжжя та області**, що агрегує публікації з офіційного вебресурсу ПАТ «Запоріжжяобленерго» ([zoe.com.ua/outage](https://www.zoe.com.ua/outage/)).

Вихідний розклад формується у двох форматах:
1. **Стандартизований JSON** (схема DTEK / OE) — для повної сумісності з популярною інтеграцією Home Assistant:
   👉 **[ALERTua/ha-svitlo-yeah (Світло Є)](https://github.com/ALERTua/ha-svitlo-yeah)**.
2. **Наочні графічні таблиці (PNG)** — для зручного перегляду з телефону, публікації в Telegram або відображення на настінних екранах/віджетах.

---

## 🔗 Пряме посилання на файл даних (Raw JSON)

```text
https://github.com/milkmann/gpv-zoe-zaporizhzhia/raw/main/data/Zaporizhzhiaoblenergo.json
```

---

## 🖼️ Наочні графічні розклади (PNG)

Таблиці генеруються автоматично на основі останнього опублікованого графіку:

* **Зведена таблиця всіх черг на сьогодні:** [gpv-all-today.png](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-all-today.png)
* **Графіки за чергами (сьогодні + завтра):**
  - [Черга 1.1](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-1-1-emergency.png) | [Черга 1.2](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-1-2-emergency.png)
  - [Черга 2.1](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-2-1-emergency.png) | [Черга 2.2](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-2-2-emergency.png)
  - [Черга 3.1](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-3-1-emergency.png) | [Черга 3.2](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-3-2-emergency.png)
  - [Черга 4.1](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-4-1-emergency.png) | [Черга 4.2](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-4-2-emergency.png)
  - [Черга 5.1](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-5-1-emergency.png) | [Черга 5.2](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-5-2-emergency.png)
  - [Черга 6.1](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-6-1-emergency.png) | [Черга 6.2](https://raw.githubusercontent.com/milkmann/gpv-zoe-zaporizhzhia/main/images/Zaporizhzhiaoblenergo/gpv-6-2-emergency.png)

---

## 📊 Архітектура та схема роботи

```mermaid
flowchart TD
    subgraph SRC["🌐 1. Джерело даних"]
        ZOE["Вебсайт АТ «Запоріжжяобленерго»<br/>zoe.com.ua/outage"]
    end

    subgraph GHA["⚙️ 2. Хмарний пайплайн (GitHub Actions)"]
        CRON["Планувальник<br/>(запуск кожні 15 хвилин)"] --> RUN["scripts/parser.py"]
        ZOE -.->|HTML-публікації| RUN
        RUN --> PNG["scripts/render_png.py<br/>Генерація наочних PNG-таблиць"]
        PNG --> CHECK{"Дані змінилися?"}
        CHECK -- Так --> COMMIT["Автоматичний комміт<br/>data/ та images/"]
        CHECK -- Ні --> IDLE["Пропуск (без зайвих коммітів)"]
    end

    subgraph STORE["📦 3. Хмарне сховище"]
        RAW["GitHub Raw JSON<br/>(Zaporizhzhiaoblenergo.json)"]
        IMGS["Графічні таблиці PNG<br/>(gpv-*.png)"]
        COMMIT --> RAW
        COMMIT --> IMGS
    end

    subgraph CLIENTS["🏠 4. Споживачі"]
        INT["Home Assistant (ha-svitlo-yeah)"]
        TG["Telegram-боти, сповіщення, віджети"]
        RAW -.->|HTTP GET| INT
        IMGS -.->|Пряме посилання| TG
        INT --> DASH["Дашборди, графіки, календарі"]
    end
```

---

## ⚙️ Особливості роботи парсера

1. **Регулярне опитування джерела:**
   - Робочий процес GitHub Actions перевіряє офіційний сайт щохвилини/кожні **15 хвилин**.
   - Нові комміти фіксуються виключно за умови фактичної зміни розкладу або статусу відключень.
2. **Оперативна обробка коригувань протягом доби:**
   - При публікації оновлених розпоряджень протягом дня парсер автоматично обирає найостаннішу версію для поточної та наступної доби.
3. **Повна сітка черг (12 груп):**
   - Формуються дані для всіх 6 черг з урахуванням підгруп: `GPV1.1` – `GPV6.2`.
4. **Врахування 30-хвилинних інтервалів:**
   - Зрушення на пів години (наприклад, `10:30 – 13:30`) автоматично транслюються у відповідні маркери `first` (перші пів години) та `second` (другі пів години).
5. **Коректна обробка періодів без обмежень:**
   - Якщо офіційно повідомлено про відсутність застосування графіків для населення, слоти заповнюються значенням `yes` («Світло є»), запобігаючи неправдивим тривогам або зависанню застарілих даних.
6. **Автономність та стійкість:**
   - Рішення реалізовано на Python 3 з автоматичним кешуванням хешів.
   - У випадку мережевих збоїв або тимчасової недоступності сайту-джерела наявний стан зберігається без руйнування JSON-файлу.

---

## 🏠 Підключення до Home Assistant

Файл може використовуватися як основне або резервне джерело даних для інтеграції `svitlo_yeah`:

```python
# custom_components/svitlo_yeah/const.py
DTEK_PROVIDER_URLS = {
    ...
    "zaporizhzhia": [
        "https://github.com/milkmann/gpv-zoe-zaporizhzhia/raw/main/data/Zaporizhzhiaoblenergo.json",
        "https://github.com/yaroslav2901/OE_OUTAGE_DATA/raw/main/data/Zaporizhzhiaoblenergo.json",
    ],
}
```

---

## 🌐 Офіційні ресурси обленерго
* Вебсторінка з оголошеннями ГПВ: [https://www.zoe.com.ua/outage/](https://www.zoe.com.ua/outage/)
* Інформаційний канал Telegram: [@Zaporizhzhyaoblenergo_news](https://t.me/Zaporizhzhyaoblenergo_news)
* Telegram-бот обслуговування: [@ZPenergoBot](https://t.me/ZPenergoBot)
