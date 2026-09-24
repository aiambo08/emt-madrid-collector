# Bot de Telegram

`emt-bot` es un servicio opcional que responde en Telegram con **llegadas en tiempo real** (API
EMT), **riesgo previsto** de bunching e intervalo saturado (modelos entrenados por
`emt-analysis run` sobre el histórico real) y **estado del recolector** (tablas
`collection_cycles` / `collection_gaps`). Opcionalmente envía **alertas** a chats concretos cuando
un modelo prevé riesgo alto. No duplica lógica analítica: reutiliza `EMTClient`, `load_history`,
`build_series` y los `predict` de cada paquete.

## 1. Crear el bot y obtener el token

1. En Telegram, habla con [@BotFather](https://t.me/BotFather) → `/newbot` → nombre y usuario
   (debe terminar en `bot`). BotFather devuelve un token con forma `123456789:AAF...`.
2. Copia el token en `.env`:

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456789:AAF...
   ```

   El token es un secreto: no lo pegues en logs, informes ni commits. El bot nunca lo imprime
   (los errores de la API de Telegram se registran sin la URL).

3. Comprueba token, base de datos y modelos sin arrancar el bucle:

   ```bash
   docker compose --profile bot run --rm bot check
   # local: emt-bot check
   ```

   Salida esperada (`0` ok, `1` error de Telegram, `2` configuración inválida):

   ```text
   bot: @mi_emt_bot
   reports: /reports (latest.json ausente)
   modelo bunching: no
   modelo saturación: no
   último ciclo: 2026-10-01T08:29:14+00:00
   ```

## 2. Arrancar

```bash
docker compose --profile bot up -d bot
docker compose logs -f bot        # bot.ready, bot.replied, bot.alert_sent, ...
```

El servicio `bot` usa la misma imagen, lee `.env`, conecta a `db` y monta el volumen `reports`
en solo lectura (`REPORTS_DIR=/reports`) para cargar los modelos que escribe el servicio
`analysis`. Sin `analysis` en marcha, `/llegadas` y `/estado` funcionan igualmente; `/riesgo`
responde que aún no hay modelo.

Local sin Docker: `pip install -e ".[analysis]"` y `emt-bot run [--reports reports]`.

## 3. Comandos

| Comando | Fuente | Respuesta |
| --- | --- | --- |
| `/llegadas <parada> [línea]` | API EMT (1 petición) | Próximas llegadas ordenadas por ETA: línea, destino, minutos, id de bus y distancia. Los buses sin estimación válida (`estimateArrive` ≥ 999999) se listan como "sin estimación". Máx. 12 filas. |
| `/riesgo <parada> [línea]` | Modelos + BD (sin API) | Por cada línea/destino de la parada: probabilidad de bunching en los próximos 15 min, probabilidad de que la siguiente llegada cierre un intervalo saturado, espera prevista y minutos desde el último paso. |
| `/impacto [parada]` | `impact/summary.json` del último `emt-analysis run --event` (sin API) | Efecto neto del evento (tratadas − control, si hay controles) y antes → después de cada ruta tratada (máx. 6; filtra por parada) para intervalo medio, espera, saturación y episodios/día; `*` marca los cambios cuyo IC no incluye el cero. Solo análisis sobre el histórico real; ver [impact.md](impact.md). |
| `/estado` | BD (sin API) | Último ciclo (hora, estado, paradas OK/fallidas, llegadas insertadas), ciclos/gaps/llegadas de la última hora, modelos e impacto cargados. Marca con ⚠️ si el último ciclo no fue `ok` o es más antiguo que 3 intervalos (mín. 5 min). |
| `/start`, `/help`, `/ayuda` | — | Ayuda. |

Las paradas son códigos EMT (los mismos de `EMT_STOPS`, p. ej. `1182`); la línea es la etiqueta
pública (`27`, `45`, `N2`…). El orden es siempre **parada primero**: como paradas y líneas son
numéricas, no se puede adivinar cuál es cuál. El bot registra sus comandos en Telegram
(`setMyCommands`), así que aparecen al escribir `/`.

Respuestas de `/riesgo` que no son predicciones:

- "Ningún modelo cubre…": no hay `reports/latest.json`, el análisis no terminó con `ok`, o el
  modelo es sintético (`source: synthetic`, p. ej. de `emt-bunching demo`). **Los modelos
  sintéticos se ignoran siempre**; solo se sirven los entrenados con `source: database`.
- "sin muestras recientes suficientes": la ruta está en el modelo pero no tiene cobertura
  completa en la ventana de features (`insufficient_coverage`: gaps o recolector parado) o el
  modelo no la vio al entrenar (`unseen_route`); vuelve a ejecutar `emt-analysis run`.

El riesgo es una predicción sobre pasos inferidos de ETAs, no una medida de ocupación. Ver
[bunching.md](bunching.md) y [saturation.md](saturation.md) para la definición exacta.

## 4. Quién puede usarlo

| Variable | Efecto |
| --- | --- |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Vacío: cualquier chat puede consultar. Con ids (`12345,-100987`): los demás chats se ignoran en silencio (log `bot.chat_rejected`). |
| `TELEGRAM_ALERT_CHAT_IDS` | Chats que reciben alertas. Vacío: alertas desactivadas. |

Para conocer el id de un chat: escribe cualquier cosa al bot con las listas vacías y mira
`chat_id` en el log `bot.replied`; para grupos el id es negativo y el bot debe estar añadido al
grupo. Los ids deben ser enteros separados por comas (se valida al arrancar).

## 5. Alertas

Cada `TELEGRAM_ALERT_EVERY_MINUTES` (5 por defecto) el bot evalúa todas las rutas cubiertas por
los modelos cargados y envía un mensaje por ruta cuando:

- bunching: probabilidad ≥ `TELEGRAM_ALERT_PROBABILITY` (0,6) en los próximos minutos del
  horizonte del modelo; o
- saturación: probabilidad ≥ `TELEGRAM_ALERT_PROBABILITY` de que la siguiente llegada cierre un
  intervalo saturado.

Cada (tipo, línea, parada, destino) se avisa como mucho una vez cada
`TELEGRAM_ALERT_COOLDOWN_MINUTES` (30). El cooldown vive en memoria: al reiniciar el bot puede
repetir la alerta. Si un chat bloquea al bot, el fallo se registra (`bot.alert_failed`) y se
sigue enviando al resto.

## 6. Cómo funciona

- Long polling (`getUpdates`, 25 s) con `httpx`; sin webhooks ni puertos abiertos, así que
  funciona detrás de NAT. Solo se piden actualizaciones de tipo `message`.
- Ante un error de Telegram (red, 5xx, 429) el bucle espera 5 s y sigue; el offset ya avanzado
  evita reprocesar mensajes.
- Los modelos se leen de `REPORTS_DIR/latest.json` y se recargan solo cuando cambia el fichero
  (mtime/tamaño), de modo que una ejecución nueva de `emt-analysis run` entra en servicio sin
  reiniciar el bot. Las rutas de `summary.json` escritas dentro del contenedor `analysis`
  (`/reports/...`) se resuelven también desde otro punto de montaje.
- Respuestas en HTML de Telegram, escapadas, partidas en trozos de ≤ 4096 caracteres.
- `/llegadas` es lo único que consume cuota EMT: una petición por consulta, con el mismo
  cliente (token cacheado, reintentos, rate limit) del recolector.

## 7. Variables

```dotenv
TELEGRAM_BOT_TOKEN=                # obligatorio para emt-bot
TELEGRAM_ALLOWED_CHAT_IDS=         # vacío = todos
TELEGRAM_ALERT_CHAT_IDS=           # vacío = sin alertas
TELEGRAM_ALERT_EVERY_MINUTES=5
TELEGRAM_ALERT_PROBABILITY=0.6
TELEGRAM_ALERT_COOLDOWN_MINUTES=30
REPORTS_DIR=reports                # en Docker: /reports (volumen compartido con analysis)
```

`emt-bot` necesita además las credenciales EMT (`/llegadas`) y la base de datos
(`/riesgo`, `/estado`), igual que el recolector.
