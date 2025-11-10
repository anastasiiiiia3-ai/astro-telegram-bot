FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей для geopy и других пакетов
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все файлы проекта
COPY . .

# Проверяем наличие шрифта
RUN ls -la DejaVuSans.ttf || echo "WARNING: Font file not found"

# Запускаем бота (НЕ FastAPI!)
CMD ["python", "main.py"]
