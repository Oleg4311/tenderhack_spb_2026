FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ── Шаг 1: системные зависимости Chromium ──────────────────────────────────
# Отдельный слой: пересобирается только при изменении этого списка
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Основные библиотеки Chromium
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    libx11-6 libxext6 libxcb1 libx11-xcb1 \
    # Шрифты и утилиты
    fonts-liberation fonts-noto-color-emoji \
    ca-certificates curl wget \
    && rm -rf /var/lib/apt/lists/*

# ── Шаг 2: Python-пакеты (без playwright — он большой, идёт отдельно) ───────
COPY requirements.runtime.txt .
RUN pip install --no-cache-dir --timeout 300 --retries 5 \
    -r requirements.runtime.txt

# ── Шаг 3: playwright Python-пакет (38 MB, медленная сеть — даём 10 мин) ───
RUN pip install --no-cache-dir --timeout 600 --retries 5 \
    playwright==1.49.1

# ── Шаг 4: скачать браузер Chromium (~130 MB) ────────────────────────────
# PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0 — явно разрешаем скачивание
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0
RUN playwright install chromium

# ── Шаг 5: код приложения ────────────────────────────────────────────────
COPY app/ app/

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=3).read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
