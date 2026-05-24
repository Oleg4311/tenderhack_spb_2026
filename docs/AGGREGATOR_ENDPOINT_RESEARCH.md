# Aggregator Endpoint Research

Дата проверки: 2026-05-24.

Цель: найти источник товаров Ozon/Wildberries/Яндекс Маркета через агрегаторы, не через прямой парсинг маркетплейсов и не через внешний API агрегатора. Допустимый вариант для проекта: публичный веб/XHR/internal JSON без пользовательской авторизации, без bearer-токена, без обхода TLS pinning и без подбора токенов.

## Короткий вывод

На первом проходе по приоритетным источникам `Cheaper`, `YoloPrice`, `Palert` открытого JSON-поиска товаров без авторизации не найдено.

Текущий код проекта дополняет выдачу через `price.ru`, но `price.ru` кладет товары в секции `wildberries`/`ozon` только когда сам возвращает магазин WB/Ozon в `shop_info`. Для запросов вроде `шины 205/55 R16` агрегатор возвращает в основном Яндекс Маркет и обычные интернет-магазины, поэтому WB/Ozon секции не пополняются.

## Таблица

| source | supports_ozon | supports_wb | supports_yandex | web_search_available | mobile_app_available | endpoint_found | endpoint_url | method | requires_auth | returns_json | pagination | usable_for_project | notes |
|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---|---:|---|
| Cheaper | yes | yes | yes | unknown | yes | no | n/a | n/a | unknown | no | unknown | no | `https://cheaper.ru/` timed out from this environment on HTTP and HTTPS. RuStore/Google Play pages confirm marketplaces and mobile app package `ru.cheaper`, but no public web/XHR endpoint could be tested. |
| YoloPrice | yes | yes | yes | no | yes | partial | `/api.SearchService/RunSearch`, `/api.SuggestsService/GetSuggests`, `/api.SearchService/RunSearchResults` inside APK | gRPC/local mobile SDK | yes/unknown runtime | not as public web JSON | likely stream/page via gRPC | no | Web is Tilda landing only. APK/XAPK strings show Flutter app and gRPC service paths, but no public base URL suitable for backend HTTP use. The app appears to start/use a mobile SDK server/runtime; using it would mean emulating app internals, not a stable public XHR. |
| Palert | yes | yes | yes | auth/pro plan | PWA | yes | `https://api.palert.ru/v1/watch`, `https://api.palert.ru/v1/prices`, `https://app.palert.ru/rest/*` | POST/REST | yes | yes for REST errors | n/a | no | Landing and bundle show API examples requiring `Authorization: Bearer *****`. Palert text says product-name search is available from Pro and up. `app.palert.ru/rest/search/products/offers` without auth returned `ERROR_METHOD_NOT_FOUND`; no public unauthenticated product search found. |
| MarketParser | yes | yes | yes | not checked in this pass | unknown | no | n/a | n/a | likely | unknown | unknown | no | Deferred by priority. Described as paid API/cabinet; do not use until explicit research proves public unauthenticated web JSON. |
| MPStats | yes | yes | yes | not checked in this pass | unknown | no | n/a | n/a | likely | unknown | unknown | no | Deferred by priority. Likely closed SaaS analytics. |
| SellerFox | yes | yes | yes | not checked in this pass | unknown | no | n/a | n/a | likely | unknown | unknown | no | Deferred by priority. Likely closed SaaS analytics. |
| Topseller | yes | yes | yes | not checked in this pass | unknown | no | n/a | n/a | likely | unknown | unknown | no | Deferred by priority. Likely SaaS/cabinet. |
| Sellmonitor | yes | yes | yes | not checked in this pass | unknown | no | n/a | n/a | likely | unknown | unknown | no | Deferred by priority. Likely paid service. |
| Wbcon | yes | yes | yes | not checked in this pass | unknown | no | n/a | n/a | likely | unknown | unknown | no | Deferred by priority. API/cabinet, not public товарный поиск. |
| MP Manager | yes | yes | yes | not checked in this pass | unknown | no | n/a | n/a | likely | unknown | unknown | no | Deferred by priority. Likely SaaS/cabinet. |

## Проверенные детали

### Cheaper

- Primary: `https://cheaper.ru/`
- Tests:
  - `http://cheaper.ru/`
  - `https://cheaper.ru/`
  - `https://www.cheaper.ru/`
- Result: timeout from current runtime. No web JS/XHR could be collected.
- App references found via official/search pages:
  - Google Play package: `ru.cheaper`
  - RuStore package: `ru.cheaper`
- Decision: not usable until a reachable web endpoint or inspectable APK with public unauthenticated endpoint is found.

### YoloPrice

- Primary: `https://yoloprice.com/ru`
- Web result: landing page, not a searchable web app. JS is Tilda/CDN only; no usable product-search XHR.
- Official app references:
  - Google Play: `com.yolo_price_mobile`
  - RuStore: `com.yolo_price_mobile`
  - App Store/AppGallery/Xiaomi links are present on the landing.
- XAPK inspection:
  - Package: `com.yolo_price_mobile`
  - Found service paths:
    - `/api.SearchService/RunSearch`
    - `/api.SearchService/RunSearchResults`
    - `/api.SearchService/GetExtendedInfo`
    - `/api.SuggestsService/GetSuggests`
    - `/api.ConfigService/InitSDK`
    - `/api.ConfigService/GetSDKConfig`
  - Found store markers: `ozon`, `wildberries`, `yamarket`, `https://www.ozon.ru/`, `https://www.wildberries.ru/`, `https://market.yandex.ru/`.
  - Found public status JSON only: `https://storage.googleapis.com/public-assets-yoloprice/website/app_status.json`, not product data.
- Decision: not usable as backend adapter. The discovered endpoints are mobile gRPC/SDK internals, not public web JSON.

### Palert

- Primary:
  - `https://palert.ru/`
  - `https://palert.io/`
  - `https://app.palert.ru/`
- Landing bundle endpoints:
  - `https://api.palert.ru/v1/watch`
  - `https://api.palert.ru/v1/prices`
- API examples in bundle require:
  - `Authorization: Bearer *****`
- Product-name search is explicitly described as paid/pro functionality on the public page text.
- Tested unauthenticated app REST guesses:
  - `https://app.palert.ru/rest/search?query=iphone%2015` -> `404 {"error":"ERROR_METHOD_NOT_FOUND"}`
  - `https://app.palert.ru/rest/products?query=iphone%2015` -> `404 {"error":"ERROR_METHOD_NOT_FOUND"}`
  - `https://app.palert.ru/rest/offers?query=iphone%2015` -> `404 {"error":"ERROR_METHOD_NOT_FOUND"}`
- `api.palert.ru` did not resolve from current runtime during direct tests, but the bundle examples are enough to classify the public API as Bearer-authenticated.
- Decision: not usable for this project as unauthenticated товарный поиск.

## Implementation Policy

No adapter should be registered as active unless `usable_for_project=true`.

Allowed:
- Browser-visible web/XHR JSON that returns product search data without account cookies or bearer tokens.
- Normal browser headers.

Not allowed:
- External paid API keys.
- User bearer tokens.
- Session-cookie dependent private endpoints.
- TLS pinning bypass.
- Emulating closed mobile SDK flows as a backend data source.
