FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN groupadd -g 1000 app \
    && useradd -u 1000 -g 1000 -m app \
    && mkdir -p /app/data \
    && chown -R app:app /app

COPY .git ./.git
RUN cd /app && git describe --tags --always 2>/dev/null > /app/VERSION || echo "dev" > /app/VERSION
RUN cd /app && git log --oneline --format="- %s" $(git describe --tags --abbrev=0 2>/dev/null || echo root)..HEAD 2>/dev/null > /app/CHANGES || echo "- initial commit" > /app/CHANGES
RUN rm -rf /app/.git

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY .env.example ./.env.example

RUN chown -R app:app /app

USER app

EXPOSE 9527

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9527"]
