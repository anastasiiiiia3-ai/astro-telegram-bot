FROM python:3.11-slim

WORKDIR /app

# Установка только необходимых системных зависимостей
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем все файлы проекта
COPY . .

# Проверяем наличие шрифта
RUN ls -la DejaVuSans.ttf || echo "WARNING: Font file not found"

# Запускаем бота напрямую
CMD ["python", "-u", "main.py"]
