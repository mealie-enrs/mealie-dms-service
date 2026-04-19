FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir confluent-kafka==2.4.0 redis==5.0.8

COPY services/stream_consumer/consumer.py .

CMD ["python", "consumer.py"]
