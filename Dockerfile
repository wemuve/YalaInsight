FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend backend
COPY frontend frontend
COPY deploy deploy
RUN useradd --system --uid 10001 yala && chown -R yala /srv
USER yala
WORKDIR /srv/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8000')+'/health').status==200 else 1)"
# deploy/start.sh applies database migrations and the first-run bootstrap (safe with several
# instances), then starts the server. PORT is injected by most hosts (Render, Fly…); default 8000.
CMD ["sh", "/srv/deploy/start.sh"]
