FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей
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

# Делаем скрипт исполняемым
RUN chmod +x start.sh || echo "No start.sh found, using direct python"

# Запускаем бота (используем start.sh если есть, иначе прямо python)
CMD ["sh", "-c", "if [ -f start.sh ]; then ./start.sh; else python main.py; fi"]
