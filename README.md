# PriceHunt TenderHack 2026

Интеллектуальный runtime-сервис поиска цен в открытых источниках для Tender Hack SPB 2026.

Проект ищет товары по запросу, категории и региону, собирает данные из Wildberries, Ozon, Яндекс Маркета и открытых сайтов Рунета, нормализует результат и отдаёт единый JSON для frontend.

## Что Реализовано

- Runtime-парсинг без БД, авторизации и постоянного кэша.
- Источники:
  - Wildberries
  - Ozon
  - Яндекс Маркет
  - Рунет-пул по категориям
- Категории:
  - `clothes`
  - `tires`
  - `office`
- Pipeline:
  - нормализация запроса;
  - расширение синонимами;
  - параллельный запуск источников;
  - HTTP/API/HTML parsing;
  - JSON-LD, microdata, embedded JSON;
  - Playwright fallback;
  - XHR/fetch JSON capture;
  - detail-page enrichment;
  - sitemap fallback для Рунета;
  - grouped response.

## Архитектура

```text
frontend React/Nginx :3000
        |
        | /api/*
        v
backend FastAPI :8000
        |
        v
backend/app/parsers/
  common.py             models, scoring, utilities
  http_client.py        httpx client, retry, proxy, rate limit
  browser.py            Playwright fallback + XHR capture
  extractors.py         JSON-LD/microdata/HTML/geo extraction
  query_normalizer.py   typo/layout/synonym/tire normalization
  wildberries.py
  ozon.py
  yandex_market.py
  runet.py
  service.py            orchestrator
```

## Запуск Через Docker

```bash
docker compose up --build
```

После запуска:

| Сервис         | URL                                      |
| -------------- | ---------------------------------------- |
| Frontend       | http://localhost:3000                    |
| Backend API    | http://localhost:8000                    |
| Swagger        | http://localhost:8000/docs               |
| Health         | http://localhost:8000/api/health         |
| Parsers health | http://localhost:8000/api/parsers/health |
| Parsers smoke  | http://localhost:8000/api/parsers/smoke  |

Если Docker показывает старые volumes/containers:

```bash
docker compose down -v --remove-orphans
docker compose up --build
```

## Почему Docker Может Долго Собираться

Обычный `backend/Dockerfile` теперь использует лёгкий `python:3.12-slim`, `requirements.runtime.txt` и не скачивает Chromium. Это быстрый режим для демо и разработки: HTTP/API/HTML/JSON-LD/sitemap парсеры работают, а Playwright fallback будет пропущен, если браузер недоступен в контейнере.

Полный browser-режим лежит отдельно:

```bash
docker compose build backend --build-arg BUILDKIT_INLINE_CACHE=1
```

Если нужен Chromium внутри backend-контейнера, используйте `backend/Dockerfile.playwright`. Он основан на `mcr.microsoft.com/playwright/python`, но этот base image очень большой: один слой около 700+ MB. На медленном канале первый pull может занимать 5-15 минут. После кеширования повторные сборки быстрее.

Для переключения compose на полный browser-режим поменяйте в `docker-compose.yml`:

```yaml
dockerfile: Dockerfile.playwright
```

Если `pip install` всё равно долгий, проверьте, что compose использует `backend/Dockerfile`, а не `Dockerfile.playwright`. Быстрый runtime-файл не ставит `pymorphy3`, `trafilatura`, `rapidfuzz`, `playwright` и legacy crawler-зависимости.

## Переменные Окружения

Поддерживаются легальные прокси для HTTP и Playwright:

```env
PROXY_URL=http://user:pass@host:port
PROXY_LIST=http://user:pass@host1:port,http://user:pass@host2:port
BROWSER_CONCURRENCY=2
```

В `docker-compose.yml` уже проброшены:

```yaml
PROXY_URL
PROXY_LIST
```

`BROWSER_CONCURRENCY` можно добавить при необходимости.

## API

### POST `/api/search`

Request:

```json
{
  "query": "шины 205 55 r16",
  "category": "tires",
  "region": "Москва",
  "limit": 10
}
```

Response:

```json
{
  "query": "шины 205 55 r16",
  "normalizedQuery": "шины 205/55 R16",
  "expandedQueries": [],
  "region": "Москва",
  "category": "tires",
  "groups": {
    "wildberries": {
      "status": "ok",
      "count": 3,
      "errorReason": "",
      "diagnostics": {},
      "items": []
    },
    "ozon": {
      "status": "blocked",
      "count": 0,
      "errorReason": "Ozon anti-bot or CAPTCHA",
      "diagnostics": {},
      "items": []
    },
    "yandex_market": {},
    "runet": {}
  },
  "summary": {
    "totalFound": 0,
    "minPrice": 0,
    "maxPrice": 0,
    "sourcesUsed": []
  }
}
```

Статусы источников:

- `ok` - товары найдены;
- `empty` - источник доступен, но товары не извлечены;
- `blocked` - источник ограничил доступ/CAPTCHA/anti-bot;
- `error` - ошибка или timeout источника.

### GET `/api/parsers/health`

Показывает состояние последнего запуска:

```json
{
  "sources": [
    {
      "source": "wildberries",
      "status": "ok",
      "lastError": "",
      "lastLatencyMs": 1200,
      "lastItemsCount": 10
    }
  ]
}
```

### GET `/api/parsers/smoke`

Короткая live-проверка источников с `limit=2`:

```bash
curl "http://localhost:8000/api/parsers/smoke?q=шины%20205%2055%20r16&category=tires&region=Москва"
```

## Формат Товара

Каждый item возвращается в единой структуре:

```json
{
  "source": "wildberries",
  "sourceType": "marketplace",
  "realSourceHost": "wildberries.ru",
  "title": "",
  "brand": "",
  "model": "",
  "sku": "",
  "productId": "",
  "category": "",
  "breadcrumbs": [],
  "price": 0,
  "oldPrice": 0,
  "discountPercent": 0,
  "currency": "RUB",
  "availability": "",
  "seller": "",
  "rating": 0,
  "reviewsCount": 0,
  "images": [],
  "mainImage": "",
  "url": "",
  "characteristics": {},
  "description": "",
  "deliveryInfo": "",
  "region": "Москва",
  "geo": {
    "requestedRegion": "Москва",
    "detectedRegion": "",
    "city": "",
    "deliveryRegion": "",
    "storeAddress": "",
    "pickupAddress": "",
    "warehouse": "",
    "latitude": null,
    "longitude": null
  },
  "relevanceScore": 0,
  "relevanceDetails": {},
  "completenessScore": 0,
  "collectedAt": ""
}
```

`characteristics` не фиксируется жёсткой схемой. Для каждого товара возвращается тот набор характеристик, который удалось извлечь именно из его карточки/JSON/detail page.

## Источники И Fallback

### Wildberries

- public search endpoints `search.wb.ru`;
- nmId/productId extraction;
- card/detail endpoint;
- basket `card.json`;
- image URL generation;
- Playwright fallback.

### Ozon

- composer/page JSON попытки;
- HTML search;
- embedded JSON;
- Playwright rendered HTML;
- XHR/fetch JSON capture;
- product links + detail enrichment.

Если Ozon возвращает anti-bot/CAPTCHA и товарные XHR не пойманы, группа получает `status="blocked"`.

### Яндекс Маркет

- HTML search;
- embedded JSON / initial state;
- Playwright fallback;
- XHR/fetch JSON capture;
- product links + detail enrichment.

Если Маркет ограничивает доступ и товарные XHR не пойманы, группа получает `status="blocked"`.

### Рунет

Пул по категориям:

- tires: `4tochki.ru`, `autoopt.ru`, `shina-guide.ru`, `tyres-auto.ru`
- office: `foroffice.ru`, `oldi.ru`, `price.ru`, `officemag.ru`
- clothes: `1click.ru`, `bonprix.ru`, `kari.com`, `sneakerhead.ru`

Fallback:

- host-specific search URL;
- generic search URL;
- rendered HTML;
- product links;
- detail page;
- JSON-LD/microdata/embedded JSON;
- robots.txt sitemap discovery;
- sitemap.xml / sitemap_index.xml;
- host-specific enrichment для `4tochki.ru`, `foroffice.ru`, `oldi.ru`, `price.ru`.

## Нормализация Запроса

Локально, без внешних API:

- исправление частых опечаток;
- нормализация раскладки;
- удаление мусора;
- синонимы;
- нормализация шин.

Примеры:

- `резина` -> `шины`
- `покрышки` -> `шины`
- `мфу` -> `многофункциональное устройство`
- `орг техника` -> `оргтехника`
- `принтер лазерный` -> `лазерный принтер`
- `кросовки` -> `кроссовки`
- `205 55 r16` -> `205/55 R16`

## Релевантность

Внешние LLM и тяжёлые ML-модели не используются.

Локальный скоринг учитывает:

- title;
- brand/model/sku/productId;
- breadcrumbs;
- seller;
- description;
- deliveryInfo;
- все индивидуальные `characteristics`;
- token match;
- title boost;
- char n-gram similarity;
- числовые совпадения вроде `205/55 R16`.

Дополнительно у товара есть:

- `relevanceScore`;
- `relevanceDetails`;
- `completenessScore`.

## Ограничения

Проект не использует:

- БД;
- постоянный кэш;
- авторизацию;
- внешние LLM API;
- Google/Bing/Yandex Search API;
- low-code/no-code.

CAPTCHA solving, обход авторизации, чужие cookies/tokens и агрессивный spam scraping не реализованы. Если источник жёстко закрывает доступ, API возвращает `status="blocked"` и diagnostics, а остальные источники продолжают работу.

## Локальный Запуск Без Docker

Backend:

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

## Проверка

```bash
curl -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query":"шины 205 55 r16","category":"tires","region":"Москва","limit":10}'

curl -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query":"лазерный принтер canon","category":"office","region":"Москва","limit":10}'

curl -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query":"мужская футболка хлопок","category":"clothes","region":"Москва","limit":10}'

curl http://localhost:8000/api/parsers/health
curl "http://localhost:8000/api/parsers/smoke?q=шапка&category=clothes&region=Москва"
```
