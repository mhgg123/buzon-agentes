# Despliegue

Este proyecto no trae su propio programador de tareas a propósito: use el que
ya tiene. Dos ejemplos.

## cron

```cron
* * * * * cd /ruta/a/buzon-agentes/buzon_agentes && BUZON_AGENTES_CONFIG=~/.config/buzon-agentes/agentes.toml python3 despachador.py once >> /var/log/buzon-agentes.log 2>&1
```

`despachador.py` solo acepta dos comandos: `once` (procesa un mensaje pendiente
y sale) y `status` (imprime un JSON con el conteo de despachos por estado, para
monitoreo — no gasta ninguna ejecución del tope diario).

`despachador.py once` toma un `flock` no bloqueante sobre su propio directorio
de estado, así que ejecutarlo cada minuto (o más seguido, desde un temporizador
systemd) es seguro: si la corrida anterior sigue viva, la nueva simplemente no
hace nada y sale con código 0.

## systemd (usuario)

```ini
# ~/.config/systemd/user/buzon-agentes.service
[Unit]
Description=Despachador del buzón de agentes

[Service]
Type=oneshot
WorkingDirectory=/ruta/a/buzon-agentes/buzon_agentes
Environment=BUZON_AGENTES_CONFIG=%h/.config/buzon-agentes/agentes.toml
ExecStart=/usr/bin/python3 despachador.py once
```

```ini
# ~/.config/systemd/user/buzon-agentes.timer
[Unit]
Description=Cada 15 segundos

[Timer]
OnBootSec=15
OnUnitActiveSec=15

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now buzon-agentes.timer
loginctl enable-linger "$USER"   # para que corra sin una sesion abierta
```

## Variables de entorno

| Variable | Qué controla | Por omisión |
|---|---|---|
| `BUZON_DB` | ruta del archivo SQLite | `~/.local/state/buzon-agentes/mail.sqlite3` |
| `BUZON_AGENTES_CONFIG` | ruta de `agentes.toml` | `~/.config/buzon-agentes/agentes.toml` |
| `BUZON_AGENT_WORKDIR` | directorio de trabajo para corridas de lectura | `.` |
| `BUZON_DISPATCH_MAX_RUNS` | tope de ejecuciones automáticas por día (1-60) | 20 |
| `BUZON_RUN_TIMEOUT` | segundos de plazo por ejecución | 120 |
| `BUZON_WRITE_MAX_FILES` | archivos máximos por commit de escritura | 10 |

El directorio que contenga `BUZON_DB` debe ser privado (permisos `0700`): el
buzón se niega a arrancar si no lo es.
