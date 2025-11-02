# Зафиксированный, стабильный Python
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Установим системные утилиты, чтобы reportlab не ругался (минимум)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libfreetype6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости (кэш слоёв будет работать лучше)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Теперь код
COPY . .

# Render даёт переменную PORT, но если её нет — fallback 10000
ENV PORT=10000

# Никаких “процесс-менеджеров” не нужно
CMD sh -c 'uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}'
