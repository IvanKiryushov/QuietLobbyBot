FROM python:3.10-slim

WORKDIR /app

# Копируем и устанавливаем зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код бота
COPY . .

# Команда для запуска
CMD ["python", "src/main.py"]
