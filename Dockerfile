FROM python:3.11-slim

WORKDIR /app

# Библиотеки для reportlab (PDF)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6 libjpeg62-turbo zlib1g \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

ENV PORT=10000
EXPOSE 10000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
