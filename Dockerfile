FROM python:3.11-slim

WORKDIR /app

# Logs en tiempo real (sin buffer) — necesario para docker logs / Railway
ENV PYTHONUNBUFFERED=1

# Dependencias primero para aprovechar la cache de capas
COPY pyproject.toml README.md LICENSE ./
COPY poly_utils/ poly_utils/
COPY update_utils/ update_utils/
COPY trading/ trading/
RUN pip install --no-cache-dir ".[trading]"

COPY update.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# data/ y processed/ viven en un único volumen montado en /app/data
# (processed se enlaza a data/processed en el entrypoint — Railway solo
# permite un volumen por servicio)
ENTRYPOINT ["./docker-entrypoint.sh"]
