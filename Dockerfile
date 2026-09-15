FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV AI_LLM_HOME=/data \
    AILLM_HOST=0.0.0.0

VOLUME ["/data"]

EXPOSE 18123

CMD ["python", "server.py"]