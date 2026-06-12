#!/bin/sh
# Loop: ejecuta update.py, espera UPDATE_INTERVAL_SECONDS y repite.
# Ambas etapas son resumibles, así que un fallo a mitad no pierde progreso.

INTERVAL="${UPDATE_INTERVAL_SECONDS:-900}"

# Un solo volumen (/app/data) para Docker y Railway: processed/ se redirige
# a data/processed mediante symlink.
mkdir -p /app/data/processed
if [ ! -L /app/processed ]; then
    rm -rf /app/processed
    ln -s /app/data/processed /app/processed
fi

while true; do
    echo "[entrypoint] $(date -u '+%Y-%m-%d %H:%M:%S') — ejecutando update.py"
    python update.py || echo "[entrypoint] update.py falló; se reintenta en el próximo ciclo"
    echo "[entrypoint] ciclo terminado; durmiendo ${INTERVAL}s"
    sleep "$INTERVAL"
done
