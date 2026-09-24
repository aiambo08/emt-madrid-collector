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

- [Funcionalidades](#funcionalidades)
- [Requisitos previos](#requisitos-previos)
- [Inicio rápido paso a paso (Docker)](#inicio-rápido-paso-a-paso-docker)
- [Ejecución local sin Docker](#ejecución-local-sin-docker)
- [Comandos disponibles](#comandos-disponibles)
- [Obtener credenciales EMT MobilityLabs](#obtener-credenciales-emt-mobilitylabs)
- [Configuración (.env)](#configuración-env)
- [Cuota de la API y dimensionado](#cuota-de-la-api-y-dimensionado)
- [Cómo funciona un ciclo](#cómo-funciona-un-ciclo)
- [Endpoints de la API utilizados](#endpoints-de-la-api-utilizados)
- [Despliegue 24/7 en un VPS](#despliegue-247-en-un-vps)
- [Esquema de datos](#esquema-de-datos)
- [Fiabilidad del dataset](#fiabilidad-del-dataset-gaps-idempotencia-logs)
- [Volumen y retención](#volumen-y-retención)
- [Detector y predictor de bus bunching](#detector-y-predictor-de-bus-bunching)
- [Predicción de saturación del servicio](#predicción-de-saturación-del-servicio)
- [Optimización de frecuencias](#optimización-de-frecuencias)
- [Bot de Telegram](#bot-de-telegram)
- [Validación con el histórico real](#validación-con-el-histórico-real)
- [Desarrollo](#desarrollo)

## Funcionalidades

| Área | Qué hace el sistema |
| --- | --- |
| **Recolección periódica** | Un proceso 24/7 (APScheduler) lanza un ciclo cada `COLLECT_INTERVAL_SECONDS` (60 s por defecto). Nunca se solapan dos ciclos (`max_instances=1`). |
| **Selección de objetivos** | Sigue líneas completas (`EMT_LINES`: descubre automáticamente todas sus paradas en ambos sentidos) y/o paradas concretas (`EMT_STOPS`). La lista de paradas se cachea en la tabla `stops` y se refresca cada `EMT_STOPS_REFRESH_HOURS`. |
| **Posiciones de buses** | De cada respuesta de llegadas extrae la posición GPS de cada bus y la guarda en `bus_positions` (una fila por línea + bus + instante de muestra, deduplicando buses vistos desde varias paradas). |
| **Llegadas estimadas** | Guarda en `arrival_estimates` cada estimación (parada, línea, bus, segundos hasta llegada, distancia, destino, desvío). |
| **Autenticación EMT** | Login con email/contraseña o con credenciales de App (`X-ClientId`/`passKey`); renovación automática del token antes de caducar y re-login ante HTTP 401 o códigos `8x`. Errores de login con mensaje explicativo (códigos `84`, `92`, `99`). |
| **Resiliencia** | Reintentos con backoff exponencial ante errores de red, 5xx y 429; rate limit local (`EMT_MAX_REQUESTS_PER_MINUTE`); aviso cuando la configuración supera la cuota diaria. |
| **Registro de ciclos** | Cada ciclo queda en `collection_cycles` con inicio, fin, estado (`ok`/`partial`/`empty`/`failed`), paradas consultadas/fallidas, peticiones, reintentos, re-logins y filas insertadas. |
| **Registro de huecos (gaps)** | Todo lo que falta queda anotado en `collection_gaps`: paradas que no respondieron, ciclos fallidos/vacíos, ticks del scheduler perdidos o saltados, errores de base de datos. |
| **Idempotencia** | Claves naturales + `INSERT … ON CONFLICT DO NOTHING`: reinicios, reintentos y ejecuciones manuales no duplican datos. |
| **Dos marcas temporales** | `sample_ts` (hora del servidor EMT) e `ingested_at` (hora del recolector) en todas las filas, para medir latencias y huecos. |
| **Persistencia** | PostgreSQL 16 con TimescaleDB (hypertables automáticas si la extensión existe; funciona igual en Postgres normal y SQLite para pruebas). |
| **Logging estructurado** | JSON por línea (`structlog`) con eventos `emt.login`, `emt.retry`, `emt.reauth`, `cycle.start`, `cycle.end`, `stop.failed`, `scheduler.job_missed`, … |
| **Despliegue** | Dockerfile (usuario no root) + `docker-compose.yml` (TimescaleDB con healthcheck + recolector, volumen persistente, `restart: unless-stopped`, apagado limpio con `SIGTERM`). |
| **CLI** | `run`, `once`, `init-db`, `check`, `lines`, `stats` (ver [Comandos disponibles](#comandos-disponibles)). |
| **Diagnóstico del histórico** | `emt-collector stats`: cobertura temporal, ciclos esperados vs observados, gaps por tipo, rutas detectadas, pasos inferidos por día, headway mediano y ciclo estimado, con avisos sobre lo que falta para que los análisis funcionen. Sin llamadas a la API. |
| **Compresión/retención Timescale** | `init-db`/`run` aplican políticas de compresión (`DB_COMPRESS_AFTER_DAYS`, 7 por defecto) y retención (`DB_RETENTION_DAYS`, desactivada por defecto) a las hypertables. |
| **Análisis periódico** | `emt-analysis run`: ejecuta bunching, saturación y frecuencias sobre los últimos N días, guarda cada ejecución en su carpeta con `summary.json` y puede repetirse cada N horas (servicio Compose opcional `analysis`). |
| **Bus bunching** | `emt-bunching`: detecta agrupamientos por línea/parada/destino, entrena un predictor a 15 minutos con evaluación temporal y genera informes HTML, JSON y CSV. Incluye demo sintética sin credenciales. |
| **Saturación del servicio** | `emt-saturation`: marca intervalos entre buses ≥ 1,5× la mediana de la ruta y hora (mín. 12 min), predice la probabilidad de que la siguiente llegada cierre uno y los minutos de espera; backtest cronológico, informe HTML y demo sintética. No mide ocupación. |
| **Optimización de frecuencias** | `emt-frequency`: por línea/parada/sentido y hora local calcula intervalo medio, regularidad (CV), espera media de pasajero y buses en servicio implícitos (ciclo / intervalo); propone redistribuir las mismas horas-bus entre franjas para minimizar la espera ponderada por demanda (proxy, uniforme o CSV propio). Informe HTML comparando actual vs propuesto y demo sintética. |
| **Bot de Telegram** | `emt-bot` (servicio Compose opcional `bot`): `/llegadas <parada> [línea]` en tiempo real desde la API EMT, `/riesgo <parada> [línea]` con los modelos de bunching y saturación entrenados sobre el histórico real (nunca sintéticos), `/estado` del recolector desde la BD y alertas opcionales con umbral de probabilidad y cooldown. Token en `TELEGRAM_BOT_TOKEN`; acceso restringible por chat. |

## Requisitos previos

- Docker 24+ con el plugin `docker compose` (o Python 3.10+ y un PostgreSQL accesible para la
  ejecución local).
- Una cuenta gratuita en <https://mobilitylabs.emtmadrid.es> (ver
  [Obtener credenciales](#obtener-credenciales-emt-mobilitylabs)).

## Inicio rápido paso a paso (Docker)

Ruta recomendada. Todos los comandos se ejecutan desde la raíz del repositorio.

**Paso 1 — Clonar el repositorio**

```bash
git clone https://github.com/aiambo08/emt-madrid-collector.git
cd emt-madrid-collector
```

**Paso 2 — Crear el fichero `.env`**

```bash
cp .env.example .env
```

**Paso 3 — Rellenar `.env`** (editor de texto). Mínimo imprescindible:

```dotenv
# Credenciales EMT: rellena SOLO una de las dos opciones
EMT_EMAIL=tu-email@example.com      # opción A: usuario del portal (cuota ~20.000 hits/día)
EMT_PASSWORD=tu-password
EMT_CLIENT_ID=                      # opción B: App de MobilityLabs (cuota ~150.000 hits/día)
EMT_PASS_KEY=                       #   si se rellenan, tienen prioridad sobre la opción A

# Qué recolectar: líneas (etiqueta pública) y/o paradas concretas
EMT_LINES=27,45
EMT_STOPS=

# Contraseña de PostgreSQL (obligatoria, elígela tú)
POSTGRES_PASSWORD=
```

Genera una contraseña de Postgres con `openssl rand -hex 24` (o en PowerShell
`-join ((48..57)+(97..122) | Get-Random -Count 32 | % {[char]$_})`). No uses comillas alrededor
de los valores.

**Paso 4 — Comprobar credenciales y configuración (no escribe en la BD)**

```bash
docker compose run --rm collector check
```

Salida esperada:

```json
{"login": "ok", "daily_quota": 20000, "used_today": 0, "lines": ["27", "45"], "explicit_stops": [], "interval_seconds": 60}
```

Si termina con `EMT API error: login failed (code 84)` revisa `EMT_CLIENT_ID`/`EMT_PASS_KEY`;
con `code 92`, `EMT_EMAIL`/`EMT_PASSWORD` (tabla completa en
[Errores de login](#errores-de-login-habituales)).

**Paso 5 — Ajustar el ritmo a la cuota** (ver
[Cuota de la API y dimensionado](#cuota-de-la-api-y-dimensionado)). Con la cuota de usuario
(20.000/día) y dos líneas completas, fija por ejemplo `COLLECT_INTERVAL_SECONDS=300` o limita
las paradas con `EMT_STOPS`.

**Paso 6 — Probar un único ciclo completo** (crea el esquema e inserta datos)

```bash
docker compose up -d db
docker compose run --rm collector once
```

Imprime un JSON con `status`, `stops_requested`, `stops_ok`, `positions_inserted`,
`arrivals_inserted` y `stats.requests`; ese número de peticiones es lo que costará cada ciclo
en cuota.

**Paso 7 — Arrancar el recolector 24/7**

```bash
docker compose up -d --build
docker compose logs -f collector
```

Logs esperados en el primer arranque (JSON, uno por línea):

```json
{"event":"db.ready","timescale":true,...}
{"event":"emt.login","token_ttl_seconds":86399,"daily_quota":20000,"used_today":12,...}
{"event":"collector.targets_resolved","stops":61,"lines":["27","45"],...}
{"event":"cycle.end","status":"ok","stops_requested":61,"stops_ok":61,"positions_inserted":38,"arrivals_inserted":112,"duration_seconds":8.4,...}
```

**Paso 8 — Verificar que se acumulan datos**

```bash
docker compose exec db psql -U emt -d emt -c \
  "SELECT status, count(*), max(finished_at) FROM collection_cycles GROUP BY 1;"
docker compose exec db psql -U emt -d emt -c \
  "SELECT count(*) AS posiciones, max(sample_ts) FROM bus_positions;"
```

**Operación diaria**

| Acción | Comando |
| --- | --- |
| Ver logs | `docker compose logs -f collector` |
| Parar / arrancar | `docker compose stop collector` / `docker compose start collector` |
| Aplicar cambios de `.env` | `docker compose up -d` (recrea el contenedor) |
| Actualizar el código | `git pull && docker compose up -d --build` |
| Backup | `docker compose exec db pg_dump -U emt -Fc emt > emt_$(date +%F).dump` |
| Consola SQL | `docker compose exec db psql -U emt -d emt` |

## Ejecución local sin Docker

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e .
cp .env.example .env               # y rellenarlo igual que en el paso 3
emt-collector check                # o: python -m emt_collector check
```

Para la base de datos, o bien un PostgreSQL propio (`POSTGRES_HOST`, `POSTGRES_PASSWORD`, …
en `.env`), o bien SQLite para pruebas rápidas:

```bash
DATABASE_URL=sqlite+pysqlite:///emt.db LOG_FORMAT=console emt-collector once
```

El comando `emt-collector` sólo existe con el entorno virtual activado (o dentro del
contenedor); fuera de él usa `python -m emt_collector …`.

## Comandos disponibles

`emt-collector <comando>` (en Docker: `docker compose run --rm collector <comando>`).

| Comando | Qué hace | Necesita | Salida / código de salida |
| --- | --- | --- | --- |
| `check` | Hace login, muestra cuota diaria y consumo, y la configuración de líneas/paradas. No toca la BD. | Credenciales EMT + `EMT_LINES` y/o `EMT_STOPS` | JSON; `0` ok, `1` error de API, `2` configuración inválida |
| `lines` | Imprime el catálogo de líneas de la EMT (ids, etiquetas, cabeceras). | Credenciales EMT | JSON |
| `init-db` | Crea/actualiza tablas e hypertables y sale. | Base de datos | `0` |
| `once` | Ejecuta un ciclo completo de recolección y sale. Útil para probar o para cron. | Credenciales, objetivos y BD | JSON del ciclo; `0` si `ok`/`partial`/`empty`, `1` si `failed` |
| `run` | Proceso permanente: crea el esquema, aplica las políticas Timescale y recolecta cada `COLLECT_INTERVAL_SECONDS` (mín. 10 s). Es el comando por defecto del contenedor. | Credenciales, objetivos y BD | Logs JSON; termina limpiamente con `SIGTERM`/`Ctrl+C` |
| `stats [--days N]` | Diagnóstico del histórico almacenado en los últimos N días (30 por defecto): ver [Validación con el histórico real](#validación-con-el-histórico-real). | Base de datos | JSON; `0` sin avisos, `3` con avisos (`hints`) |

Análisis (instalados con `pip install -e ".[analysis]"`; en Docker ya están en la imagen:
`docker compose run --rm --entrypoint emt-bunching collector demo --output /reports/demo`):

| Comando | Qué hace |
| --- | --- |
| `emt-bunching demo\|analyze\|predict` | [Detector y predictor de bus bunching](#detector-y-predictor-de-bus-bunching) |
| `emt-saturation demo\|analyze\|predict` | [Predicción de saturación del servicio](#predicción-de-saturación-del-servicio) |
| `emt-frequency demo\|analyze` | [Optimización de frecuencias](#optimización-de-frecuencias) |
| `emt-analysis run` | Los tres `analyze` de una vez (opcionalmente cada N horas): ver [Validación con el histórico real](#validación-con-el-histórico-real) |
| `emt-bot run\|check` | [Bot de Telegram](#bot-de-telegram) |

## Obtener credenciales EMT MobilityLabs

1. Regístrate en <https://mobilitylabs.emtmadrid.es> (gratuito) y verifica el email.
2. Con el email y la contraseña del portal ya puedes autenticarte (`EMT_EMAIL`,
   `EMT_PASSWORD`). Esta modalidad "genérica" tiene una cuota diaria reducida (20.000 hits/día
   según devuelve el propio login).
3. **Recomendado**: en el portal, entra en *Developers Portal* → *Mis aplicaciones* → *Nueva
   aplicación*. En el panel de la App aparecen `x-ClientId` y `passKey`; cópialos en
   `EMT_CLIENT_ID` / `EMT_PASS_KEY` (son dos valores distintos de tu email/contraseña). La cuota
   sube (150.000 hits/día en el momento de escribir esto) y no expones tu email/contraseña.
4. Comprueba la cuota real con `emt-collector check`: imprime `daily_quota` y `used_today` tal
   y como los devuelve el login.

La EMT pide citar *EMT Madrid MobilityLabs* como fuente de los datos.

### Errores de login habituales

Si `EMT_CLIENT_ID`/`EMT_PASS_KEY` están definidos tienen prioridad sobre `EMT_EMAIL`/`EMT_PASSWORD`.
`emt-collector check` termina con `EMT API error: login failed (code XX)` y este significado
(observado contra la API real; la descripción suele venir vacía):

| Código | HTTP | Causa |
| --- | --- | --- |
| `84` | 403 | `X-ClientId`/`passKey` inválidos: revisa `EMT_CLIENT_ID`/`EMT_PASS_KEY` (sin espacios ni comillas, sin intercambiarlos, copiados del panel de la App). Para descartar, comenta ambas variables y prueba con email/contraseña. |
| `92` | 200 | Usuario no encontrado o contraseña incorrecta (`EMT_EMAIL`/`EMT_PASSWORD`). |
| `99` | 200 | La API no recibió credenciales (`.env` no cargado o variables mal escritas). |

## Configuración (.env)

| Variable | Por defecto | Descripción |
| --- | --- | --- |
| `EMT_EMAIL` / `EMT_PASSWORD` | – | Credenciales de usuario del portal. |
| `EMT_CLIENT_ID` / `EMT_PASS_KEY` | – | Credenciales de App (prioritarias si están). |
| `EMT_LINES` | – | Líneas a seguir, separadas por comas (`27,45,C1`). Acepta etiqueta o id (`027`). |
| `EMT_STOPS` | – | Paradas adicionales (ids EMT). En ellas se guardan todas las líneas que pasan. |
| `EMT_STOPS_REFRESH_HOURS` | `24` | Frecuencia de refresco de la lista de paradas. |
| `COLLECT_INTERVAL_SECONDS` | `60` | Periodo del scheduler (mín. 10 para `run`). |
| `EMT_MAX_REQUESTS_PER_MINUTE` | `100` | Límite cliente de peticiones/minuto. |
| `EMT_DAILY_REQUEST_BUDGET` | `150000` | Presupuesto diario para el aviso de cuota (se usa el menor entre este valor y la cuota devuelta por el login). |
| `EMT_REQUEST_TIMEOUT_SECONDS` | `15` | Timeout HTTP. |
| `EMT_MAX_RETRIES` | `4` | Reintentos ante red/5xx/429. |
| `EMT_BASE_URL` | `https://openapi.emtmadrid.es` | Base de la API. |
| `POSTGRES_PASSWORD` | – | Obligatoria. Con `POSTGRES_USER`/`POSTGRES_DB` (`emt`), `POSTGRES_HOST` (`localhost`; `db` en Compose) y `POSTGRES_PORT` (`5432`) forma la URL de conexión, escapando cualquier carácter. |
| `DATABASE_URL` | – | URL SQLAlchemy completa; si está definida tiene prioridad sobre `POSTGRES_*`. También vale `sqlite+pysqlite:///emt.db` para pruebas. |
| `DB_USE_TIMESCALE` | `true` | Crear hypertables si la extensión está disponible. |
| `DB_COMPRESS_AFTER_DAYS` | `7` | Comprimir chunks de `bus_positions`/`arrival_estimates` con más de N días (Timescale). `0` quita la política. |
| `DB_RETENTION_DAYS` | `0` | **Borrar** datos con más de N días (Timescale). `0` = sin retención: el histórico se conserva entero. |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `LOG_FORMAT=console` para desarrollo. |

Regla de filtrado: de las paradas descubiertas a partir de `EMT_LINES` sólo se guardan las
llegadas de las líneas configuradas (una parada de la 27 también recibe la 45, la 147…), para
mantener el dataset acotado. En las paradas listadas explícitamente en `EMT_STOPS` se guarda
todo lo que pasa por ellas.

## Cuota de la API y dimensionado

Coste: **1 petición por parada y ciclo** (más ~3 peticiones por línea al refrescar paradas,
una vez al día). Peticiones/día = `paradas × 86400 / COLLECT_INTERVAL_SECONDS`.

| Cuota diaria | Intervalo | Paradas máximas | Ejemplo |
| --- | --- | --- | --- |
| 20.000 (usuario) | 60 s | ~13 | Un puñado de paradas en `EMT_STOPS` |
| 20.000 (usuario) | 300 s | ~69 | Una línea completa (ambos sentidos) |
| 20.000 (usuario) | 600 s | ~138 | Dos líneas completas |
| 150.000 (App) | 60 s | ~104 | Una o dos líneas completas |
| 150.000 (App) | 120 s | ~208 | Tres o cuatro líneas completas |

La cuota real la devuelve el login (`daily_quota` en `emt-collector check`); dimensiona con ese
valor, no con el de la tabla. Los análisis necesitan muestreo de 60 s, así que con la App es
preferible **ampliar paradas manteniendo el intervalo**: por ejemplo las ~84 paradas de las
líneas 27 y 45 (`EMT_LINES=27,45`, `EMT_STOPS=`) suponen ~121.000 hits/día, dentro de los
150.000. Con cuota de usuario (20.000) hay que quedarse en `EMT_STOPS` con ≤ 13 paradas.

Cómo saber cuántas paradas tienes: `emt-collector once` devuelve `stops_requested`, y el log
`collector.targets_resolved` muestra `stops`. Si la proyección supera la cuota aparece el aviso
`collector.daily_budget_exceeded` en el log (el recolector **no** se detiene solo: al agotar la
cuota la API empezará a fallar y los ciclos quedarán registrados como gaps).

## Cómo funciona un ciclo

1. Al arrancar, `emt-collector run` crea el esquema (`init_schema`) y programa un job de
   APScheduler cada `COLLECT_INTERVAL_SECONDS` con `max_instances=1` y `coalesce=True`
   (si un ciclo tarda más que el intervalo no se solapan ejecuciones; el tick saltado queda
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

## Despliegue 24/7 en un VPS

Cualquier VPS pequeño (1 vCPU, 1–2 GB RAM, 20+ GB disco) es suficiente.

```bash
# 1. Docker + compose plugin (Debian/Ubuntu)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# 2. Código y configuración: pasos 1–6 del inicio rápido
git clone https://github.com/aiambo08/emt-madrid-collector.git
cd emt-madrid-collector
cp .env.example .env && nano .env      # credenciales, EMT_LINES, contraseña de Postgres
docker compose run --rm collector check

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

Con TimescaleDB, `init-db` y `run` aplican (y reaplican de forma idempotente al cambiar el
`.env`) estas políticas a `bus_positions` y `arrival_estimates`:

- **Compresión** de chunks con más de `DB_COMPRESS_AFTER_DAYS` días (7 por defecto; segmentada
  por `line, bus_id` y `line, stop_id`). Las consultas y los análisis siguen funcionando sobre
  datos comprimidos; el ahorro típico documentado por Timescale es de un orden de magnitud.
- **Retención**: borrado de datos con más de `DB_RETENTION_DAYS` días. Desactivada por defecto
  (`0`) porque el histórico es el objetivo del proyecto; actívala solo si el disco manda.

El log `db.timescale_policies` muestra los valores aplicados; `0` en cualquiera de las dos quita
la política. Sin Timescale, planifica particionado o archivado (p. ej. `COPY … TO` Parquet mensual) cuando
las tablas superen unos pocos GB.

## Detector y predictor de bus bunching

La segunda fase utiliza `arrival_estimates`, `collection_cycles` y `collection_gaps`, sin
consumir cuota EMT ni modificar la base de datos.

- **Detector:** al menos 3 buses distintos de la misma línea, parada y destino en 3 minutos,
  tras un intervalo de al menos 20 minutos entre pasos inferidos. Umbrales configurables.
- **Predictor:** regresión logística sobre retardos de la serie, intervalos entre buses,
  estimaciones de llegada y calendario de Madrid; riesgo de inicio en los próximos 15 minutos.
- **Demostración:** informe HTML autónomo con episodios, evolución del riesgo, comparación por
  línea/hora y métricas frente a un baseline. Entrenamiento/evaluación en orden temporal.

**Prueba inmediata, sin API ni base de datos** (en el entorno virtual):

```bash
pip install -e ".[analysis]"
emt-bunching demo --output reports/demo
```

Abre `reports/demo/report.html` en el navegador. **La demo usa datos sintéticos**; sus métricas
no demuestran rendimiento sobre el servicio real. Si no se reconoce el ejecutable, usa
`python -m emt_collector.bunching.cli demo --output reports/demo`.

**Con tu histórico real** (conexión de BD configurada en `.env`; sustituye las fechas):

```bash
emt-bunching analyze --start 2026-09-01T00:00:00Z --end 2026-09-22T00:00:00Z --output reports/history
emt-bunching predict --model reports/history/model.json
```

Si faltan datos, `analyze` genera el informe del detector y explica por qué no ha entrenado.
Necesita al menos 7 días entre instantes etiquetados y ambas clases en entrenamiento y
evaluación. No utiliza el modelo sintético para predecir con datos reales.

Los pasos se **infieren de ETAs y distancia a parada** (ETA ≤ 60 s o ≤ 150 m), y además cuando
un bus **desaparece** de la parada tras anunciar una ETA ≤ `--vanish-seconds` (180 s por
defecto; `0` desactiva esta vía): el paso se sitúa en `última muestra + ETA`. Ambas vías
comparten el cooldown por bus, y `emt-collector stats` informa de cuántos pasos vienen de cada
una (`passages` vs `vanish_passages`). No son pasos confirmados por un sensor.
Sin observaciones regulares, no se puede distinguir un hueco del servicio de uno de datos.
Conserva el muestreo de 60 segundos en pocas paradas estables para esta fase.

**[Guía completa: Docker, datos reales, parámetros, evaluación y limitaciones](docs/bunching.md)**.

## Predicción de saturación del servicio

La API de EMT no informa de la ocupación, así que la tercera fase usa un **proxy**: un intervalo
entre buses muy superior al habitual concentra la demanda en el bus siguiente. `emt-saturation`
reutiliza los pasos inferidos de la fase anterior y no consume cuota EMT.

- **Etiqueta:** intervalo saturado si `headway ≥ max(1,5 × mediana(ruta, hora local), 12 min)`.
  La mediana se calcula por línea, parada, destino y hora de Madrid, con respaldo en la mediana
  de la ruta cuando hay menos de 5 intervalos en esa hora.
- **Modelos:** regresión logística (probabilidad de que la siguiente llegada cierre un intervalo
  saturado) y regresión ridge (minutos de espera hasta esa llegada), con features causales,
  referencia horaria y evaluación en orden temporal frente a baselines por línea/hora.
- **Salidas:** informe HTML autónomo, `summary.json`, `headways.json`, `backtest.csv`,
  `predictions.json` y `model.json`.

```bash
pip install -e ".[analysis]"
emt-saturation demo --output reports/saturation-demo                     # datos sintéticos
emt-saturation analyze --start 2026-09-24T00:00:00Z --output reports/saturation   # tu histórico
emt-saturation predict --model reports/saturation/model.json
```

Necesita al menos 7 días de histórico con muestreo de 60 s; con menos, `analyze` genera el
informe descriptivo y explica por qué no ha entrenado. **No es una medida de pasajeros**: lee
[docs/saturation.md](docs/saturation.md) antes de interpretar las probabilidades.

## Optimización de frecuencias

`emt-frequency` convierte el histórico en un **cuadro de oferta observada** por línea, parada,
sentido y hora local, y propone un reparto alternativo de la misma flota:

- **Servicio observado:** intervalo medio `H̄`, coeficiente de variación `CV`, tasa de intervalos
  saturados y espera media de un pasajero que llega al azar `H̄ · (1 + CV²) / 2`.
- **Buses en servicio:** el tiempo de ciclo se estima como la mediana del tiempo que tarda el
  mismo bus en volver a pasar por la parada en el mismo sentido; `buses = ciclo / H̄`.
- **Propuesta:** con las mismas horas-bus totales, asigna buses enteros a cada franja (greedy
  por ganancia marginal, óptimo para este objetivo convexo) minimizando la espera ponderada por
  demanda, con intervalo planificado acotado (3–20 min por defecto). También muestra la espera
  que se alcanzaría solo con regularidad (`CV` objetivo), para comparar mover buses frente a
  corregir bunching.
- **Demanda:** la API no da pasajeros. `--demand proxy` (buses observados/día × (1 + tasa de
  saturación)), `--demand uniform` o `--demand-csv perfil.csv` con columnas `hour,weight[,line]`.

```bash
pip install -e ".[analysis]"
emt-frequency demo --output reports/frequency-demo                       # datos sintéticos
emt-frequency analyze --start 2026-09-24T00:00:00Z --output reports/frequency --demand-csv demanda.csv
```

Salidas: `report.html`, `summary.json`, `plan.csv` y `demand.csv`. Requiere ≥ 3 días por ruta y
≥ 5 intervalos completos por hora; sin pesos de demanda externos el resultado es un ejercicio de
regularización de la oferta, no una recomendación operativa. Detalles y límites en
[docs/frequency.md](docs/frequency.md).

## Bot de Telegram

`emt-bot` expone por Telegram lo que ya calcula el sistema, sin lógica analítica propia:

| Comando | Fuente | Qué devuelve |
| --- | --- | --- |
| `/llegadas <parada> [línea]` | API EMT (una petición) | Próximas llegadas con línea, destino, minutos, bus y distancia. |
| `/riesgo <parada> [línea]` | Modelos de `emt-analysis run` + BD | Probabilidad de bunching en los próximos 15 min y de que la siguiente llegada cierre un intervalo saturado, con espera prevista. Solo modelos entrenados con el histórico real; los sintéticos se ignoran. |
| `/estado` | BD | Último ciclo (estado, paradas OK/fallidas, llegadas), ciclos y gaps de la última hora, modelos cargados. |

Puesta en marcha: crea el bot con [@BotFather](https://t.me/BotFather), pon el token en
`TELEGRAM_BOT_TOKEN` y:

```bash
docker compose --profile bot run --rm bot check     # valida token, BD y modelos
docker compose --profile bot up -d bot              # long polling, sin puertos abiertos
```

Con `TELEGRAM_ALLOWED_CHAT_IDS` limitas quién puede consultar y con `TELEGRAM_ALERT_CHAT_IDS`
recibes alertas cada `TELEGRAM_ALERT_EVERY_MINUTES` cuando un modelo supera
`TELEGRAM_ALERT_PROBABILITY` (una por ruta y tipo cada `TELEGRAM_ALERT_COOLDOWN_MINUTES`). El
bot lee los modelos del volumen `reports` que escribe el servicio `analysis` y los recarga solo
cuando cambia `latest.json`. Guía completa en [docs/telegram.md](docs/telegram.md).

## Validación con el histórico real

Las demos son sintéticas: **ninguna métrica de los informes demo dice nada del servicio real**.
La validación se hace en dos pasos sobre tu base de datos, sin consumir cuota EMT.

**1. Diagnóstico** (Docker: `docker compose run --rm collector stats`; local: `emt-collector stats`):

```json
{
 "window": {"start": "…", "end": "…"},
 "history": {"arrivals": 81234, "days": 6.9, "stops": 4, "lines": 6, "buses": 210, "positions": 45210, …},
 "cycles": {"total": 9870, "by_status": {"ok": 9850, "partial": 20}, "expected_per_day": 1440.0,
            "observed_per_day": 1430.4, "requests_per_day": 7180, "gaps_by_kind": {"network": 20}, …},
 "routes": [{"line": "27", "stop_id": "1170", "destination": "PLAZA CASTILLA", "passages": 612,
             "vanish_passages": 140, "passages_per_day": 88.7, "median_headway_minutes": 6.0,
             "cycle_returns": 35, "median_cycle_minutes": 96.0, …}, …],
 "hints": ["Solo 6.9 días de histórico; los modelos necesitan al menos 7."]
}
```

Qué mirar:

| Campo | Significado | Qué hacer si va mal |
| --- | --- | --- |
| `history.days`, `hints` sobre días | Ventana con datos. | Esperar: bunching y saturación exigen ≥ 7 días. |
| `cycles.observed_per_day` vs `expected_per_day` | Ciclos realmente ejecutados por día. | Si es < 90 %: el recolector estuvo parado o salta ciclos (`scheduler.job_skipped_overrun`): menos paradas o más intervalo. |
| `cycles.by_status`, `gaps_by_kind` | Ciclos fallidos/parciales y su causa (`network`, `api`, `auth`, `db`, `scheduler`…). | > 5 % → revisar `collection_gaps` y los logs. |
| `cycles.requests_per_day` | Consumo real de cuota. | Debe quedar por debajo de `daily_quota` del login. |
| `routes[].passages_per_day` | Pasos inferidos por día en cada línea/parada/sentido. | < 40 → pocos headways por hora; elegir paradas con más frecuencia o sumar paradas de la misma ruta. |
| `routes[].vanish_passages` | Pasos inferidos solo por desaparición del bus. | Si son casi todos, la parada recibe ETAs poco fiables: probar otra parada o ajustar `--vanish-seconds`. |
| `routes[].cycle_returns`, `median_cycle_minutes` | Veces que el mismo bus vuelve a pasar y ciclo estimado. | < 10 → `emt-frequency` no podrá estimar buses en servicio; hace falta más histórico o paradas de ambos sentidos. |

El comando termina con código `3` mientras haya avisos, así que sirve como chequeo en cron.

**2. Análisis** cuando `hints` ya no avise de días o cobertura (sustituye la fecha por
`history.first_sample`):

```bash
emt-analysis run --output reports --days 14          # bunching + saturación + frecuencias
# o cada uno por separado, con todos sus parámetros:
emt-bunching analyze   --start 2026-09-23T15:00:00Z --output reports/bunching-real
emt-saturation analyze --start 2026-09-23T15:00:00Z --output reports/saturation-real
emt-frequency analyze  --start 2026-09-23T15:00:00Z --output reports/frequency-real
```

`emt-analysis run` escribe `reports/<fecha>Z/{bunching,saturation,frequency}/`, un
`summary.json` por ejecución (estado `ok`, `insufficient_data` o `error` de cada análisis) y
`reports/latest.json` apuntando a la última; no sobreescribe ejecuciones anteriores. Acepta
`--stop` (repetible), `--only bunching|saturation|frequency` y `--every-hours N` para quedarse
en bucle. Para tenerlo siempre en marcha junto al recolector:

```bash
docker compose --profile analysis up -d analysis      # cada 24 h sobre los últimos 14 días
docker compose cp analysis:/reports ./reports         # traer los informes al host
```

Con los primeros informes reales compara frente a la demo: número de rutas y observaciones,
episodios de bunching detectados y su hora, proporción de intervalos saturados por hora,
métricas del backtest frente al baseline (si el modelo no supera al baseline, no lo uses) y
rutas descartadas por `emt-frequency`. Lo esperable es tener que recalibrar umbrales
(`--vanish-seconds`, cooldown, 1,5× mediana) con los primeros datos.

## Desarrollo

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,analysis]"
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
├── db/repository.py   # inserts idempotentes, ciclos, gaps, hypertables, políticas Timescale
├── collector.py       # lógica de un ciclo de recolección
├── scheduler.py       # APScheduler + señales
├── config.py          # Settings (pydantic-settings, .env)
├── logging_setup.py   # structlog
├── __main__.py        # CLI: run | once | init-db | check | lines | stats
├── analysis/          # stats.py (diagnóstico del histórico) y cli.py (emt-analysis run)
├── bunching/          # data.py (carga), detector.py (pasos/episodios), features, model, report, cli
├── saturation/        # headways, etiquetas, modelos, report, cli (emt-saturation)
├── frequency/         # servicio observado, ciclo, optimizador, report, cli (emt-frequency)
└── telegram/          # api (Bot API por httpx), models (carga de model.json), data, handlers, bot, cli (emt-bot)
```

## Licencia y atribución

MIT. Datos: *EMT Madrid MobilityLabs* (<https://mobilitylabs.emtmadrid.es>).
