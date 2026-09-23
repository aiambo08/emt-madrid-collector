# Bus bunching: detección, predicción y demostración

## 1. Qué entrega esta fase

`emt-bunching` trabaja aparte del recolector. Lee el histórico sin llamar a EMT y sin crear
tablas ni modificar registros. El proceso `emt-collector run` puede seguir activo.

| Comando | Entrada | Resultado |
| --- | --- | --- |
| `demo` | Generador sintético reproducible; sin credenciales | Detector, entrenamiento, backtest y un informe para demostrar el funcionamiento |
| `analyze` | Histórico de PostgreSQL/TimescaleDB o SQLite | Episodios, modelo si hay datos suficientes, métricas y reporte |
| `predict` | Modelo de `analyze` + muestras recientes de la misma BD | JSON de riesgo por línea, parada y destino en el siguiente horizonte |

El predictor produce una probabilidad de **inicio de un nuevo episodio durante los próximos
15 minutos**, reevaluable cada 5 minutos. El informe agrega las probabilidades del backtest por
línea y hora de Madrid. No calcula una hora exacta de llegada ni una previsión de 24 horas.

## 2. Demo local en tres pasos

Desde la raíz del repositorio, con el entorno virtual activado:

```bash
pip install -e ".[analysis]"
emt-bunching demo --days 28 --seed 42 --output reports/demo
```

Abre `reports/demo/report.html` haciendo doble clic. No necesita servidor ni internet. Tiene
gráficos navegables con teclado, tema claro/oscuro, tablas de valores y filtro de episodios
por línea. El informe identifica la fuente **DEMO SINTÉTICA**.

Si el ejecutable no aparece en el PATH:

```bash
python -m emt_collector.bunching.cli demo --output reports/demo
```

El generador simula 28 días de dos paradas ficticias, dos buses próximos por respuesta,
servicio regular, agrupamientos más frecuentes en horas punta, ruido en los ETAs y cortes de
telemetría. La semilla fija permite repetir el ejemplo. No descarga datos ni escribe en la BD.
Cada ejecución requiere un directorio de salida nuevo para no mezclar modelos e informes.

## 3. Demo con Docker, sin configurar `.env`

```bash
docker build -t emt-madrid-collector .
docker run --name bunching-demo --entrypoint emt-bunching emt-madrid-collector demo --output /reports/demo
mkdir -p reports
docker cp bunching-demo:/reports/demo ./reports/demo
docker rm bunching-demo
```

En PowerShell, sustituye `mkdir -p reports` por
`New-Item -ItemType Directory -Force reports`. El contenedor se ejecuta como usuario no root y
tiene permiso de escritura en `/reports`. No uses `--rm` antes de copiar los resultados.

## 4. Entrenar y evaluar con tu histórico

### Preparar la recolección

Elige unas pocas paradas representativas, preferiblemente fuera de cabecera, y mantén los
mismos objetivos durante varias semanas. Conserva `COLLECT_INTERVAL_SECONDS=60`.
Con email/contraseña y cuota de 20.000 hits/día, 10 paradas suponen unas 14.400 consultas/día,
más login, reintentos y catálogo. Comprueba siempre la cuota real.

```dotenv
EMT_LINES=
EMT_STOPS=PARADA_1,PARADA_2
COLLECT_INTERVAL_SECONDS=60
EMT_DAILY_REQUEST_BUDGET=20000
```

Sustituye los identificadores por paradas reales. Dejar `EMT_LINES` vacío evita descubrir
automáticamente todas las paradas de las líneas. El análisis conserva separadas las líneas
y destinos que pasan por las paradas elegidas.

### Con Python

Configura `DATABASE_URL` o `POSTGRES_*` en `.env`, igual que para el collector. No requiere
credenciales EMT. La conexión puede ser de solo lectura.

```bash
emt-bunching analyze --start 2026-09-01T00:00:00Z --end 2026-09-22T00:00:00Z --output reports/history
```

Las fechas son ejemplos: elige un periodo con datos. `--start` es inclusivo y `--end`
exclusivo; ambos necesitan zona horaria (`Z` o un desplazamiento como `+02:00`). Sin `--end`
se usa el momento actual. Puedes limitar la consulta a paradas concretas:

```bash
emt-bunching analyze --start 2026-09-01T00:00:00Z --stop 123 --stop 456 --output reports/selected
```

Las paradas de este ejemplo son ilustrativas. El límite de extracción es un millón de
observaciones; si se supera, el comando pide reducir el periodo o seleccionar paradas.
El análisis carga ese intervalo en memoria. No está diseñado para entrenar todas las líneas
de Madrid en una sola ejecución.

### Con Docker Compose y tu BD existente

```bash
docker compose build collector
docker compose up -d db
docker compose run --name bunching-history --entrypoint emt-bunching collector analyze --start 2026-09-01T00:00:00Z --end 2026-09-22T00:00:00Z --output /reports/history
mkdir -p reports
docker cp bunching-history:/reports/history ./reports/history
docker rm bunching-history
```

En PowerShell utiliza `New-Item -ItemType Directory -Force reports`. Si el comando devuelve
código 3 por datos insuficientes, copia igualmente el informe: contiene el diagnóstico y
los episodios que el detector pudo identificar.

## 5. Leer resultados

| Archivo | Contenido |
| --- | --- |
| `report.html` | Fuente, periodo, métricas, episodio ilustrativo, riesgo por línea/hora, última predicción y hasta 200 episodios recientes |
| `events.json` | Todos los episodios: línea/parada/destino, inicio/fin, paso anterior, buses, hueco y duración |
| `summary.json` | Parámetros efectivos, recuentos, partición temporal, métricas del modelo/baseline o motivo de no entrenar |
| `backtest.csv` | Cada ventana de evaluación: instante UTC, objetivo binario, probabilidad y baseline |
| `model.json` | Modelo versionado con coeficientes, escalado, rutas conocidas, parámetros, fuente y evaluación; solo si se pudo entrenar |
| `predictions.json` | Riesgo al final del periodo analizado; un periodo histórico no representa el estado actual |

**Métricas:** precisión y recall al umbral 0,5, average precision (AP), ROC AUC en JSON, Brier
(menor es mejor) y prevalencia. Las métricas se calculan por ventana, no por episodio.
Las ventanas se solapan y no son observaciones independientes: no se dan intervalos de
confianza ni una cifra de precisión de producción. El modelo no se promociona automáticamente
por superar al baseline.

El gráfico línea/hora muestra la media de probabilidades **por ventana y parada** y la
fracción de etiquetas positivas en el tramo de evaluación. Si se añaden paradas, cambia
su población. No es la probabilidad de que ocurra un episodio en cualquier punto de la línea.
La misma incidencia puede detectarse en varias paradas; no se fusiona como un único episodio.

## 6. Predecir sobre las muestras recientes

Después de entrenar un modelo con `analyze`:

```bash
emt-bunching predict --model reports/history/model.json
```

Con Docker (ejecutado desde la raíz del repositorio):

```bash
docker compose run --rm --entrypoint emt-bunching -v "$PWD/reports/history:/models:ro" collector predict --model /models/model.json
```

Devuelve una fila por línea/parada/destino con `as_of`, `until`, `probability` y `status`:

```json
{
  "line": "27",
  "stop_id": "123",
  "destination": "PLAZA CASTILLA",
  "as_of": "2026-09-22T08:00:00Z",
  "until": "2026-09-22T08:15:00Z",
  "probability": 0.72,
  "status": "ok"
}
```

Es un ejemplo de formato, no una predicción real. `insufficient_coverage` y `unseen_route`
devuelven `probability: null`, nunca cero. La falta de servicio nocturno o de datos recientes
no se interpreta como ausencia de riesgo. No se extrapola a rutas que no participaron en
el entrenamiento. Los modelos de fuente `synthetic` no se aceptan en este comando.

`--at 2026-09-22T08:00:00Z` permite reproducir un instante pasado, siempre posterior al final
de las etiquetas de entrenamiento. Se excluyen muestras no ingeridas/confirmadas antes del
instante pedido. Para operación continua, programa este comando cada cinco minutos con
cron o el programador del sistema; guarda su JSON para el futuro bot o cuadro de mando.
El modelo es el mismo que se evaluó, entrenado únicamente con el tramo inicial; vuelve a
ejecutar `analyze` con un periodo más reciente para actualizarlo.

## 7. Definición del detector y cobertura

1. Agrupa por **línea normalizada + parada + destino**. Descarta destinos vacíos para no
   mezclar sentidos desconocidos.
2. Infiere un paso cuando el bus tiene ETA entre 0 y 60 segundos, distancia entre 0 y 150 m
   y no figura en cabecera (`is_head=true`). El instante aproximado es `sample_ts + ETA`.
3. El primer acercamiento se conserva; muestras repetidas del mismo bus se suprimen hasta
   que transcurren más de 10 minutos sin observaciones cercanas de ese vehículo.
4. Busca un intervalo ≥20 minutos desde el paso anterior, seguido de al menos 3 vehículos
   distintos en una ventana total ≤180 segundos. No confunde tres muestras de un bus con
   tres vehículos. Necesita un paso anterior: el principio del dataset no cuenta como hueco.
5. Exige observaciones continuas de esa combinación durante el intervalo y el grupo.
   Interrupciones >90 s o un `collection_gap` relevante invalidan el episodio.

La cobertura se basa en respuestas con llegadas de esa misma línea/parada/destino, incluso
cuando los buses están lejos. Un gap de otra parada o línea no invalida esta combinación.
Los gaps sin parada/línea se consideran globales. Datos con disponibilidad retrasada más de
120 s se excluyen; la disponibilidad es el máximo de `sample_ts`, `ingested_at` y
`collection_cycles.finished_at` cuando hay un ciclo asociado. Ciclos sin finalizar se excluyen.

**Limitaciones de observación:** el collector histórico no registra cada respuesta vacía
individual por parada; esos tramos no permiten confirmar cobertura. Además, un bus puede
pasar entre dos muestras sin llegar a observarse cerca, una ETA puede variar o un vehículo
puede quedarse parado. La ausencia de pasos inferidos no demuestra que ningún bus pasara.
Este detector proporciona **candidatos basados en estimaciones**, que deben contrastarse con
pasos reales antes de evaluar impacto operativo. No usa distancias entre GPS de distintos
sentidos como sustituto de intervalos en una parada.

Muestrear cada cinco minutos no permite validar agrupamientos de tres minutos. No aumentes
`--max-gap-seconds` únicamente para forzar resultados: reduciría la capacidad para excluir
huecos. Para esta fase es preferible menos paradas con muestreo frecuente.

## 8. Modelo de series temporales y prevención de fuga de información

Se utiliza una **regresión logística regularizada** con features temporales supervisadas:

- Tiempo desde el último paso; último intervalo entre buses; media/desviación de intervalos
  y número de pasos en los últimos 60 minutos.
- ETA próxima, separación entre hasta tres ETAs y número de buses próximos disponibles.
- Hora y día semanal cíclicos en `Europe/Madrid`, fin de semana y codificación de la ruta.

Cada cinco minutos se genera una fila. Su etiqueta es 1 si comienza un episodio en
`(t, t + 15 min]`. No basta con comprobar la ventana futura de quince minutos: también se
exige cobertura durante los 3 minutos del grupo, la tolerancia de ETA y la latencia máxima.
Las ventanas al final del histórico sin futuro completo se excluyen.

Las features solo usan muestras y pasos ya disponibles en `t`; no se interpolan desde el
futuro. Se usa el 70% inicial de instantes para entrenamiento y el 30% final para evaluación,
compartiendo el mismo corte entre todas las rutas. Se purgan filas de entrenamiento cuyas
etiquetas alcanzan la evaluación. El escalador y las categorías solo se ajustan en
entrenamiento. El baseline es la frecuencia histórica por línea/hora, suavizada hacia la
prevalencia global, calculada también solo con entrenamiento.

Requisitos mínimos para entrenar:

- Al menos 100 instantes etiquetados y 7 días entre el primero y el último.
- Al menos 10 ventanas positivas y 10 negativas en entrenamiento.
- Ambas clases en evaluación y ninguna ruta de evaluación desconocida en entrenamiento.
- Cobertura de 60 minutos y al menos dos pasos para construir cada vector de features.

Estos son mínimos técnicos, no garantía estadística. Para validar el uso real conviene
acumular varias semanas, contrastar las etiquetas con pasos confirmados y repetir el
backtest en periodos posteriores. Una red neuronal no resuelve la falta de ground truth;
este modelo inicial permite inspeccionar y comparar el comportamiento con pocos recursos.

## 9. Parámetros y códigos de salida

Opciones compartidas por `demo` y `analyze`:

| Opción | Predeterminado | Unidad |
| --- | --- | --- |
| `--gap-minutes` | 20 | Intervalo mínimo previo entre pasos |
| `--cluster-seconds` | 180 | Ventana total del grupo |
| `--min-buses` | 3 | Vehículos distintos |
| `--horizon-minutes` | 15 | Horizonte de predicción |
| `--max-gap-seconds` | 90 | Separación máxima de muestras para cobertura |
| `--near-seconds` | 60 | ETA máxima para inferir un paso |
| `--near-metres` | 150 | Distancia máxima a parada |
| `--output` | Obligatorio | Directorio nuevo de resultados |

Los parámetros se guardan en el modelo; `predict` utiliza exactamente la misma definición.
Los otros parámetros de la API Python están en `bunching/domain.py`.

| Código | Significado |
| --- | --- |
| 0 | Modelo entrenado / predicción disponible |
| 2 | Configuración, conexión, modelo incompatible o salida ya existente |
| 3 | Datos insuficientes para entrenar (se conserva el informe) / todas las predicciones abstienen |

Los modelos son JSON validado, no pickle. Un modelo existente nunca se sobrescribe de forma
implícita. La plantilla HTML incluida en el paquete se generó con el kit de artefactos de
Devin; el reporte resultante es independiente de ese servicio.

## 10. Comprobación del desarrollo

```bash
pip install -e ".[dev,analysis]"
ruff check .
ruff format --check .
mypy
pytest -q
```

Las pruebas cubren umbrales, duplicados, sentidos/líneas/paradas, cabeceras, datos inválidos,
gaps explícitos y silenciosos, desfases de ingesta, cambios de hora, etiquetas futuras,
purga temporal, invariancia del entrenamiento ante cambios en evaluación, exportación del
modelo y CLI sin datos. La consulta de extracción está en `bunching/data.py` y usa filtros
temporales, join con ciclos y un límite explícito; no hay consultas de escritura.
