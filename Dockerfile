FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir flask requests websocket-client

COPY app.py .
COPY templates ./templates

EXPOSE 7082

CMD ["python", "app.py"]