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

# Con argumentos (command: en docker-compose) ejecuta solo eso.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Railway solo permite un volumen por servicio y no se comparte entre
# servicios, así que dashboard y trader corren DENTRO de este contenedor
# junto al loop de datos. Cada uno con su propio loop de reinicio por si
# crashea. Desactivables con RUN_DASHBOARD/RUN_TRADER=false.
if [ "${RUN_DASHBOARD:-true}" = "true" ]; then
    (while true; do
        python -m trading.dashboard || echo "[entrypoint] dashboard cayó; reinicio en 10s"
        sleep 10
    done) &
fi
if [ "${RUN_TRADER:-true}" = "true" ]; then
    (while true; do
        python -m trading.strategy || echo "[entrypoint] trader cayó; reinicio en 10s"
        sleep 10
    done) &
fi

while true; do
    echo "[entrypoint] $(date -u '+%Y-%m-%d %H:%M:%S') — ejecutando update.py"
    python update.py || echo "[entrypoint] update.py falló; se reintenta en el próximo ciclo"
    echo "[entrypoint] ciclo terminado; durmiendo ${INTERVAL}s"
    sleep "$INTERVAL"
done
