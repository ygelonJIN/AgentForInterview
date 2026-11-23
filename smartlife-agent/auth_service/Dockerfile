FROM python:3.12-slim

WORKDIR /srv
COPY auth_service/pyproject.toml auth_service/README.md ./
COPY auth_service ./auth_service
RUN pip install --no-cache-dir .

ENV AUTH_DB_PATH=/data/accounts.db \
    AUTH_SECRET_FILE=/data/auth_secret.key

VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "auth_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
