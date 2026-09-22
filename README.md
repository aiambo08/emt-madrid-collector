# emt-madrid-collector

Pipeline de datos en Python que consulta la API de **EMT Madrid MobilityLabs** cada minuto y
persiste en PostgreSQL/TimescaleDB las **posiciones de los autobuses** y las **llegadas
estimadas** a parada, construyendo un histórico propio para análisis posteriores (bus
bunching, predicción de saturación, optimización de frecuencias, bots, etc.).

```
┌────────────┐   cada 60 s   ┌──────────────────┐   INSERT … ON CONFLICT DO NOTHING   ┌───────────────┐
│ APScheduler│ ─────────────▶│ Collector        │ ──────────────────────────────────▶ │ Postgres /    │
│ (proceso   │               │  · EMTClient     │                                     │ TimescaleDB   │
│  24/7)     │               │  · login+reauth  │  cycles · gaps · positions · arrivals│               │
└────────────┘               │  · retry/backoff │                                     └───────────────┘
                             │  · rate limit    │
                             └──────────────────┘
```

## Contenido

- [Cómo funciona](#cómo-funciona)
- [Endpoints de la API utilizados](#endpoints-de-la-api-utilizados)
- [Obtener credenciales](#obtener-credenciales-emt-mobilitylabs)
- [Configuración (.env)](#configuración-env)
- [Arrancar con Docker](#arrancar-con-docker)
- [Despliegue 24/7 en un VPS](#despliegue-247-en-un-vps)
- [Esquema de datos](#esquema-de-datos)
- [Fiabilidad del dataset](#fiabilidad-del-dataset-gaps-idempotencia-logs)
- [Volumen y retención](#volumen-y-retención)
- [Desarrollo](#desarrollo)

## Cómo funciona

1. Al arrancar, `emt-collector run` crea el esquema (`init_schema`) y programa un job de
   APScheduler cada `COLLECT_INTERVAL_SECONDS` (60 s) con `max_instances=1` y `coalesce=True`
   (si un ciclo tarda más de un minuto no se solapan ejecuciones; el tick saltado queda
   registrado como gap `scheduler/job_skipped_overrun`, y los ticks perdidos por otras causas
   como `scheduler/job_missed`).
2. Cada ciclo resuelve los **objetivos**: para cada línea de `EMT_LINES` obtiene sus paradas
   (ambos sentidos) y las une con `EMT_STOPS`. La lista se cachea en la tabla `stops` y se
   refresca cada `EMT_STOPS_REFRESH_HOURS`.
3. Para cada parada llama al endpoint de llegadas. Cada elemento `Arrive` de la respuesta trae
   la línea, el id de bus, la estimación (s), la distancia y **la posición GPS del bus**
   (`geometry`), de donde salen las dos tablas:
   - `arrival_estimates`: una fila por (parada, línea, bus, timestamp de muestra).
   - `bus_positions`: una fila por (línea, bus, timestamp de muestra). Un bus visible desde
     varias paradas del mismo ciclo se guarda una sola vez.
4. Todo se inserta con `ON CONFLICT DO NOTHING` sobre la clave natural, de modo que reintentos
   y reinicios no duplican datos.
5. Se cierra el ciclo en `collection_cycles` (inicio/fin, estado, contadores) y se anotan los
   huecos en `collection_gaps`.

`sample_ts` es siempre la hora del servidor de la EMT (campo `datetime` de la respuesta,
hora local de Madrid convertida a UTC) e `ingested_at` la hora del recolector, para poder
medir latencias y detectar huecos.

## Endpoints de la API utilizados

Base: `https://openapi.emtmadrid.es` (documentación oficial:
<https://apidocs.emtmadrid.es/> y
<https://gitlab.com/mobilitylabsmadrid/helps_and_utilities/openapi_documents>).

| Uso | Método y ruta | Notas |
| --- | --- | --- |
| Login | `GET /v1/mobilitylabs/user/login/` | Cabeceras `email`+`password` o `X-ClientId`+`passKey`. Devuelve `data[0].accessToken`, `tokenSecExpiration` y `apiCounter` (cuota diaria). |
| Catálogo de líneas | `GET /v2/transport/busemtmad/lines/info/{YYYYMMDD}/` | Para traducir etiquetas públicas (`27`) a ids internos (`027`). 1 llamada al refrescar objetivos. |
| Paradas de una línea | `GET /v1/transport/busemtmad/lines/{lineId}/stops/{direction}/` | `direction` 1 (A→B) o 2 (B→A). 2 llamadas por línea al refrescar objetivos. |
| Llegadas + posición de buses | `POST /v2/transport/busemtmad/stops/{stopId}/arrives/{lineId?}/` | Cuerpo `{"cultureInfo":"ES","Text_StopRequired_YN":"N","Text_EstimationsRequired_YN":"Y","Text_IncidencesRequired_YN":"N"}`. Sin `lineId` devuelve todas las líneas de la parada. **1 llamada por parada y ciclo.** |

La API de MobilityLabs **no expone un endpoint de "posiciones de todos los buses de una
línea"**: la posición de cada bus viene dentro de la respuesta de llegadas a parada
(`Arrive[].geometry`). Por eso el recolector recorre las paradas de las líneas configuradas
y deduplica los buses vistos.

Manejo de errores del cliente (`src/emt_collector/api/client.py`):

- **Token caducado / inválido**: HTTP 401 o `code` `8x` en el JSON → re-login automático y
  reintento de la petición (una vez). Además se renueva proactivamente 60 s antes de
  `tokenSecExpiration`.
- **Red / 5xx / 429**: reintentos con backoff exponencial con jitter (1 s → 30 s,
  `EMT_MAX_RETRIES`).
- **Rate limit**: token bucket cliente (`EMT_MAX_REQUESTS_PER_MINUTE`). Al resolver objetivos
  se avisa en el log si las peticiones proyectadas superan `EMT_DAILY_REQUEST_BUDGET` o la cuota
  devuelta por el login.

## Obtener credenciales EMT MobilityLabs

1. Regístrate en <https://mobilitylabs.emtmadrid.es> (gratuito) y verifica el email.
2. Con el email y la contraseña del portal ya puedes autenticarte (`EMT_EMAIL`,
   `EMT_PASSWORD`). Esta modalidad "genérica" tiene una cuota diaria reducida (del orden de
   20.000 hits/día según el propio login).
3. **Recomendado**: en el portal, crea una *App* (menú *Mis aplicaciones* → nueva app). Obtendrás
   un `X-ClientId` y un `passKey`; ponlos en `EMT_CLIENT_ID` / `EMT_PASS_KEY`. La cuota sube
   (150.000 hits/día en el momento de escribir esto) y no expones tu email/contraseña.
4. Comprueba la cuota real con `emt-collector check`: imprime `daily_quota` y `used_today` tal
   y como los devuelve el login.

La EMT pide citar *EMT Madrid MobilityLabs* como fuente de los datos.

## Configuración (.env)

```bash
cp .env.example .env
$EDITOR .env
```

| Variable | Por defecto | Descripción |
| --- | --- | --- |
| `EMT_EMAIL` / `EMT_PASSWORD` | – | Credenciales de usuario del portal. |
| `EMT_CLIENT_ID` / `EMT_PASS_KEY` | – | Credenciales de App (prioritarias si están). |
| `EMT_LINES` | – | Líneas a seguir, separadas por comas (`27,45,C1`). Acepta etiqueta o id (`027`). |
| `EMT_STOPS` | – | Paradas adicionales. En ellas se guardan todas las líneas que pasan. |
| `EMT_STOPS_REFRESH_HOURS` | `24` | Frecuencia de refresco de la lista de paradas. |
| `COLLECT_INTERVAL_SECONDS` | `60` | Periodo del scheduler (mín. 10). |
| `EMT_MAX_REQUESTS_PER_MINUTE` | `100` | Límite cliente. |
| `EMT_DAILY_REQUEST_BUDGET` | `150000` | Presupuesto diario para el aviso de cuota. |
| `EMT_REQUEST_TIMEOUT_SECONDS` | `15` | Timeout HTTP. |
| `EMT_MAX_RETRIES` | `4` | Reintentos ante red/5xx. |
| `POSTGRES_PASSWORD` | – | Obligatoria. Con `POSTGRES_USER`/`POSTGRES_DB` (`emt`) y `POSTGRES_HOST` (`localhost`; `db` en Compose) forma la URL de conexión, escapando cualquier carácter. |
| `DATABASE_URL` | – | URL SQLAlchemy completa; si está definida tiene prioridad sobre `POSTGRES_*`. También vale `sqlite+pysqlite:///emt.db` para pruebas. |
| `DB_USE_TIMESCALE` | `true` | Crear hypertables si la extensión está disponible. |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `LOG_FORMAT=console` para desarrollo. |

De las paradas descubiertas a partir de `EMT_LINES` sólo se guardan las llegadas de las líneas
configuradas (una parada de la 27 también recibe la 45, la 147…), para mantener el dataset
acotado. En las paradas listadas explícitamente en `EMT_STOPS` se guarda todo lo que pasa por
ellas, aunque también pertenezcan a una línea configurada.

Requisitos por comando: `init-db` sólo necesita la base de datos; `lines` sólo credenciales;
`check` credenciales y `EMT_LINES` y/o `EMT_STOPS`; `once` y `run` además la base de datos
(`run` valida también `COLLECT_INTERVAL_SECONDS`).

## Arrancar con Docker

```bash
cp .env.example .env            # credenciales EMT + POSTGRES_PASSWORD (obligatoria)
docker compose up -d --build    # levanta TimescaleDB + recolector
docker compose logs -f collector
```

Primer arranque esperado (logs JSON, uno por línea):

```json
{"event":"db.ready","timescale":true,...}
{"event":"emt.login","token_ttl_seconds":86400,"daily_quota":150000,"used_today":12,...}
{"event":"collector.targets_resolved","stops":61,"lines":["27","45"],...}
{"event":"cycle.end","status":"ok","stops_requested":61,"stops_ok":61,"positions_inserted":38,"arrivals_inserted":112,"duration_seconds":8.4,...}
```

Comandos útiles (dentro del contenedor o con el paquete instalado):

```bash
emt-collector check     # login, cuota y configuración; no escribe en la BD
emt-collector once      # un único ciclo (para cron o pruebas); exit 1 si falla
emt-collector init-db   # crear/actualizar el esquema y salir
emt-collector lines     # catálogo de líneas en JSON
emt-collector run       # proceso long-running (por defecto en Docker)
```

Consultar la base de datos:

```bash
docker compose exec db psql -U emt -d emt -c \
  "SELECT status, count(*), avg(extract(epoch from finished_at-started_at))::int AS avg_s
     FROM collection_cycles GROUP BY 1;"
```

## Despliegue 24/7 en un VPS

Cualquier VPS pequeño (1 vCPU, 1–2 GB RAM, 20+ GB disco) es suficiente.

```bash
# 1. Docker + compose plugin (Debian/Ubuntu)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# 2. Código y configuración
git clone https://github.com/aiambo08/emt-madrid-collector.git
cd emt-madrid-collector
cp .env.example .env && nano .env      # credenciales, EMT_LINES, contraseña de Postgres

# 3. Arrancar
docker compose up -d --build
docker compose logs -f collector
```

- `restart: unless-stopped` en ambos servicios hace que sobrevivan a reinicios del host
  (asegúrate de que el servicio `docker` esté habilitado: `sudo systemctl enable docker`).
- El proceso maneja `SIGTERM` y termina el ciclo en curso antes de salir, así que
  `docker compose restart collector` o un `docker compose pull && up -d` no dejan ciclos a
  medias sin registrar.
- `POSTGRES_PASSWORD` no tiene valor por defecto: Compose se niega a arrancar sin ella y el
  recolector se conecta con esas mismas credenciales. El puerto 5432 no se publica por
  defecto.
- **Volumen `pgdata` ya existente** (creado con una versión anterior que usaba la contraseña
  `emt`): Postgres sólo aplica `POSTGRES_PASSWORD` al inicializar el volumen, así que cambia el
  rol antes de arrancar el collector con la nueva contraseña:
  `docker compose up -d db && docker compose exec db psql -U emt -d emt -c "ALTER ROLE emt PASSWORD '<nueva>'"`.
  Alternativa sin migrar: fija `DATABASE_URL` en `.env` con la contraseña antigua (tiene
  prioridad sobre `POSTGRES_*`).
- **Backups**: `docker compose exec db pg_dump -U emt -Fc emt > emt_$(date +%F).dump` en un
  cron diario; el volumen `pgdata` contiene todo el histórico.
- Actualizar: `git pull && docker compose up -d --build`.
- Alternativa sin proceso permanente: cron cada minuto con
  `docker compose run --rm collector once`, pero el scheduler embebido es más simple y registra
  los ticks perdidos.

Monitorización mínima recomendada: alerta si en los últimos 10 minutos no hay filas en
`collection_cycles` con `status IN ('ok','partial')`.

```sql
SELECT max(finished_at) FROM collection_cycles WHERE status IN ('ok','partial');
```

## Esquema de datos

Todas las marcas temporales son `TIMESTAMPTZ` en UTC. Definición en
`src/emt_collector/db/models.py`.

### `bus_positions` (hypertable por `sample_ts`)

| Columna | Tipo | Descripción |
| --- | --- | --- |
| `line` | text | Etiqueta pública de la línea (`27`). |
| `bus_id` | int | Identificador del vehículo (`Arrive.bus`). |
| `sample_ts` | timestamptz | Hora del servidor EMT en la respuesta (la muestra). |
| `ingested_at` | timestamptz | Hora en que el recolector procesó la respuesta. |
| `lat`, `lon` | double | Posición GPS del bus (`Arrive.geometry`). |
| `destination` | text | Cabecera de destino. |
| `position_type` | text | `Arrive.positionTypeBus`. |
| `observed_from_stop` | text | Parada cuya respuesta aportó la posición. |
| `cycle_id` | bigint | Ciclo que la generó. |

Clave primaria (natural, idempotencia): **`(line, bus_id, sample_ts)`**.

### `arrival_estimates` (hypertable por `sample_ts`)

| Columna | Tipo | Descripción |
| --- | --- | --- |
| `stop_id` | text | Parada. |
| `line` | text | Línea. |
| `bus_id` | int | Vehículo. |
| `sample_ts` | timestamptz | Hora del servidor EMT (la muestra). |
| `ingested_at` | timestamptz | Hora de ingesta. |
| `estimate_seconds` | int | Segundos hasta la llegada; `NULL` cuando la API devuelve el sentinela `999999` (sin estimación). |
| `distance_m` | int | Distancia del bus a la parada. |
| `destination` | text | Destino. |
| `is_head` | bool | Si el bus está en cabecera. |
| `deviation` | int | Desvío reportado por la API. |
| `cycle_id` | bigint | Ciclo que la generó. |

Clave primaria: **`(stop_id, line, bus_id, sample_ts)`**.

### `collection_cycles`

Una fila por tick del scheduler: `started_at`, `finished_at`, `status`
(`running|ok|partial|empty|failed`), `stops_requested/ok/failed`, `api_requests`,
`api_retries`, `reauths`, `positions_inserted`, `arrivals_inserted`, `error`.

### `collection_gaps`

Huecos del dataset: `occurred_at`, `scope` (`cycle` | `stop` | `scheduler`), `kind`
(`network`, `auth`, `api_error_<code>`, `empty`, `all_stops_failed`, `db_error`,
`job_missed`, `job_skipped_overrun`, `job_error`, `stops_refresh_failed`,
`targets_unresolved`), `stop_id`, `line`,
`detail`, `cycle_id`.

### `stops`

Caché de metadatos de parada: `stop_id`, `name`, `lat`, `lon`, `lines` (csv informativo),
`updated_at`.

### Consultas de ejemplo

```sql
-- Huecos de más de 3 minutos entre ciclos correctos (caídas del recolector)
SELECT started_at, started_at - lag(started_at) OVER (ORDER BY started_at) AS gap
FROM collection_cycles WHERE status IN ('ok','partial')
ORDER BY gap DESC NULLS LAST LIMIT 20;

-- Latencia muestra→ingesta
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM ingested_at - sample_ts))
FROM arrival_estimates WHERE sample_ts > now() - interval '1 day';

-- Trayectoria de un bus
SELECT sample_ts, lat, lon FROM bus_positions
WHERE line = '27' AND bus_id = 540 AND sample_ts::date = current_date ORDER BY sample_ts;
```

## Fiabilidad del dataset (gaps, idempotencia, logs)

- **Gaps explícitos.** Cualquier ciclo fallido o vacío, cualquier parada que no respondió
  (tras agotar reintentos) y cualquier tick perdido por el scheduler queda en
  `collection_gaps` con su motivo. El análisis posterior puede excluir esos intervalos sin
  confundir "no había buses" con "no había datos".
- **Idempotencia.** Claves naturales `(line, bus_id, sample_ts)` y
  `(stop_id, line, bus_id, sample_ts)` con `INSERT … ON CONFLICT DO NOTHING`. Reiniciar el
  proceso, ejecutar `once` a mano o que la EMT devuelva la misma muestra dos veces no genera
  duplicados; los contadores `*_inserted` reflejan sólo filas nuevas.
- **Logging estructurado** (JSON por defecto, `structlog`): `emt.login`, `emt.retry`,
  `emt.reauth`, `emt.token_expired_proactive_reauth`, `stop.failed`, `cycle.start`,
  `cycle.end` (con `positions_inserted`, `arrivals_inserted`, `requests`, `retries`,
  `reauths`, `duration_seconds`), `cycle.overrun`, `scheduler.job_missed`, etc.
- **Sin solapes.** `max_instances=1` + `coalesce=True`; si un ciclo excede el intervalo se
  registra `cycle.overrun` y el siguiente tick se salta y se anota como gap.

## Volumen y retención

Peticiones: **1 por parada y ciclo**. Con `COLLECT_INTERVAL_SECONDS=60` cada parada cuesta
1.440 hits/día. Con la cuota de App (150.000/día) el máximo teórico es ~100 paradas por minuto;
con la cuota genérica (~20.000/día) unas 13 paradas. Una línea urbana tiene 40–70 paradas
(ambos sentidos), así que **seguir 1–2 líneas completas cada minuto agota la cuota de App**.
Opciones: seleccionar paradas (`EMT_STOPS`), aumentar el intervalo (120 s duplica la cobertura)
o usar varias Apps/recolectores. Consultar todas las líneas de la red (~4.700 paradas) cada
minuto supondría ~6,8 M hits/día, fuera de cualquier cuota.

Filas: cada respuesta de parada trae típicamente 2 estimaciones por línea (los dos próximos
buses). Orden de magnitud por parada seguida: ~2–4 filas/min en `arrival_estimates` y
~1–2 filas/min nuevas en `bus_positions`. Con 100 paradas y 1 línea por parada:

| Tabla | Filas/día | Tamaño aprox./mes (Postgres, con índices) |
| --- | --- | --- |
| `arrival_estimates` | ~300–550 k | 1,5–3 GB |
| `bus_positions` | ~150–250 k | 0,7–1,5 GB |
| `collection_cycles` | 1.440 | despreciable |

Recomendaciones con TimescaleDB (opcional, no lo activa el recolector):

```sql
-- Compresión de chunks de más de 7 días (reduce ~10x)
ALTER TABLE arrival_estimates SET (timescaledb.compress, timescaledb.compress_segmentby = 'line, stop_id');
SELECT add_compression_policy('arrival_estimates', INTERVAL '7 days');
ALTER TABLE bus_positions SET (timescaledb.compress, timescaledb.compress_segmentby = 'line, bus_id');
SELECT add_compression_policy('bus_positions', INTERVAL '7 days');

-- Retención (sólo si no quieres histórico ilimitado)
SELECT add_retention_policy('arrival_estimates', INTERVAL '365 days');
```

Sin Timescale, planifica particionado o archivado (p. ej. `COPY … TO` Parquet mensual) cuando
las tablas superen unos pocos GB.

## Desarrollo

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest -q
```

Los tests usan `httpx.MockTransport` (sin red) y SQLite en memoria (sin Postgres). Para una
prueba local contra la API real sin Docker:

```bash
DATABASE_URL=sqlite+pysqlite:///emt.db LOG_FORMAT=console emt-collector once
```

Estructura:

```
src/emt_collector/
├── api/client.py      # EMTClient: login, reauth, backoff, rate limit, endpoints
├── api/models.py      # modelos pydantic de las respuestas
├── db/models.py       # tablas SQLAlchemy
├── db/repository.py   # inserts idempotentes, ciclos, gaps, hypertables
├── collector.py       # lógica de un ciclo de recolección
├── scheduler.py       # APScheduler + señales
├── config.py          # Settings (pydantic-settings, .env)
├── logging_setup.py   # structlog
└── __main__.py        # CLI: run | once | init-db | check | lines
```

## Licencia y atribución

MIT. Datos: *EMT Madrid MobilityLabs* (<https://mobilitylabs.emtmadrid.es>).
