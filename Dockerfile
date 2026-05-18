FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ is mounted at runtime so CSV files survive container restarts
VOLUME ["/app/data"]

CMD ["python", "chatbot.py"]
