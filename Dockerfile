FROM python:3.11-slim

WORKDIR /app

RUN groupadd -g 1000 app \
    && useradd -u 1000 -g 1000 -m app \
    && mkdir -p /app/data \
    && chown -R app:app /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY VERSION CHANGES ./
COPY app ./app
COPY .env.example ./.env.example
COPY .env.example ./.env.example

RUN chown -R app:app /app

USER app

EXPOSE 9527

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9527"]
