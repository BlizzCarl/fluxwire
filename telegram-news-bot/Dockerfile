FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Состояние и модель хранятся в /data, чтобы переживать перезапуски и обновления
ENV STATE_FILE=/data/state.json \
    MODEL_CACHE=/data/models \
    PYTHONUNBUFFERED=1
VOLUME /data

CMD ["python", "bot.py", "--loop"]
