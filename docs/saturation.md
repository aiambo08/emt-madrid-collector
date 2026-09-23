# Predicción de saturación del servicio

`emt-saturation` estima, por línea, parada y destino, la probabilidad de que la **siguiente
llegada cierre un intervalo entre buses anormalmente largo** y los **minutos de espera** hasta
esa llegada. Trabaja sobre el histórico del recolector (`arrival_estimates`,
`collection_cycles`, `collection_gaps`), sin consumir cuota EMT ni modificar la base de datos.

## 1. Qué mide y qué no

La API de llegadas de EMT no informa de la ocupación del autobús. "Saturación" es aquí un
**proxy de calidad de servicio**: cuando el intervalo entre dos buses consecutivos se alarga muy
por encima de lo habitual, el siguiente bus acumula la demanda de todo ese tiempo. Nada en este
módulo mide pasajeros; las probabilidades son de intervalo anómalo, no de bus lleno.

Definición de **intervalo saturado**, con los parámetros por defecto:

```text
headway >= max(1.5 × mediana(ruta, hora local de llegada), 12 minutos)
```

- **Ruta:** `(línea, parada, destino)`; sentidos y líneas nunca se mezclan.
- **Headway:** minutos entre dos pasos inferidos consecutivos de buses distintos, ambos con
  cobertura completa de muestreo (sin gaps, sin muestras tardías).
- **Mediana de referencia:** por hora local (Europe/Madrid) de la llegada que cierra el
  intervalo. Si una hora tiene menos de 5 intervalos, se usa la mediana global de la ruta.
- Los parámetros `--ratio`, `--min-headway-minutes` y `--max-wait-minutes` cambian la definición
  y quedan guardados en `summary.json` y `model.json`.

## 2. Demo en local sin API ni base de datos

```bash
pip install -e ".[analysis]"
emt-saturation demo --days 28 --seed 42 --output reports/saturation-demo
```

Abre `reports/saturation-demo/report.html`. La demo genera dos rutas sintéticas con frecuencia
distinta por hora, incidencias que alargan el intervalo (seguidas de una recuperación) y cortes
de telemetría, y termina con un backtest. **Sus métricas no demuestran nada sobre el servicio
real.**

## 3. Demo con Docker, sin configurar `.env`

```bash
docker build -t emt-madrid-collector .
docker run --name saturation-demo --entrypoint emt-saturation emt-madrid-collector demo --output /reports/demo
docker cp saturation-demo:/reports/demo ./reports/saturation-demo
docker rm saturation-demo
```

## 4. Análisis con tu histórico real

Requisitos:

- `.env` con `DATABASE_URL` (o los `POSTGRES_*`) apuntando a la base de datos del recolector.
- Muestreo de 60 s en pocas paradas estables: la inferencia de pasos exige una ETA ≤ 60 s y una
  distancia ≤ 150 m, y cualquier hueco de más de 90 s entre muestras rompe los intervalos.
- Al menos **7 días** entre el primer y el último instante etiquetado, ≥ 100 instantes y
  ≥ 10 ejemplos de cada clase en el tramo de entrenamiento (y ambas clases en evaluación). Con
  menos, `analyze` produce igualmente el informe descriptivo (intervalos, referencias, intervalos
  saturados) y explica por qué no entrena; código de salida 3.

```bash
emt-saturation analyze --start 2026-09-24T00:00:00Z --end 2026-10-08T00:00:00Z --output reports/saturation
emt-saturation analyze --start 2026-09-24T00:00:00Z --stop 1170 --stop 1182 --output reports/saturation-1170
```

Con Docker Compose y la base de datos del propio despliegue:

```bash
docker compose build collector
docker compose up -d db
docker compose run --name saturation-history --entrypoint emt-saturation collector analyze --start 2026-09-24T00:00:00Z --output /reports/history
docker cp saturation-history:/reports/history ./reports/saturation
docker rm saturation-history
```

### Ficheros generados

| Fichero | Contenido |
| --- | --- |
| `report.html` | Informe autónomo (sin red): definición, métricas, un día de ejemplo, comparación por línea/hora, referencias horarias, última predicción e intervalos saturados. |
| `summary.json` | Parámetros, recuentos, error de entrenamiento (si lo hay) y evaluación completa. |
| `headways.json` | Intervalos saturados con referencia y umbral aplicados. |
| `backtest.csv` | Una fila por ventana de evaluación: etiqueta, probabilidad, espera real y prevista, baselines. |
| `predictions.json` | Predicción por ruta al final del periodo analizado. |
| `model.json` | Modelo exportado (solo si entrenó): coeficientes, escalado, referencias horarias y evaluación. |

## 5. Cómo se construyen los ejemplos y se evalúa

1. Se infieren pasos a partir de ETAs y distancia (misma lógica que `emt-bunching`).
2. Cada `step_seconds` (5 min) se crea un instante `at` con las features causales (retardo desde
   el último bus, últimos intervalos, ETAs de los buses que se acercan, calendario de Madrid) más
   la referencia horaria de la ruta y el cociente `retardo / referencia`.
3. La etiqueta de ese instante es el **intervalo que se cierra con la siguiente llegada real**:
   si ese intervalo es saturado → 1; y `wait_minutes` = minutos desde `at` hasta esa llegada
   (máximo 90 min). Solo se etiquetan instantes cuya llegada posterior se observó con cobertura
   completa.
4. Los instantes se ordenan cronológicamente: el 70 % inicial entrena; el resto evalúa. Se
   purgan las ventanas cuya etiqueta cruza la frontera. Escalado, referencias horarias y
   baselines se ajustan **solo** con datos de entrenamiento.
5. Modelos lineales exportables a JSON: regresión logística (probabilidad de saturación) y
   regresión ridge (espera). Baselines: prevalencia por línea/hora y espera media por ruta/hora.

Como una ventana se crea cada 5 min, los intervalos largos generan más ventanas: la
**prevalencia de ventanas** es mayor que la fracción de intervalos saturados. Ambas cifras
aparecen en el informe.

## 6. Predicción con el modelo entrenado

```bash
emt-saturation predict --model reports/saturation/model.json
emt-saturation predict --model reports/saturation/model.json --at 2026-10-08T07:30:00Z
```

Con Docker (desde la raíz del repositorio):

```bash
docker compose run --rm --entrypoint emt-saturation -v "$PWD/reports/saturation:/models:ro" collector predict --model /models/model.json
```

Devuelve, por ruta entrenada: minutos desde el último bus, umbral vigente, probabilidad de que la
siguiente llegada cierre un intervalo saturado, espera prevista y `status` (`ok`,
`insufficient_coverage` si faltan muestras recientes, `unseen_route` si la ruta no estaba en
el entrenamiento). Rechaza modelos sintéticos y predicciones anteriores al fin del
entrenamiento. Código de salida 3 si ninguna ruta tiene cobertura.

## 7. Limitaciones

- No es ocupación: un intervalo largo con poca demanda no llena el bus, y un bus puede ir lleno
  con intervalos regulares.
- Pasos inferidos, no confirmados: un bus que no llega a ETA ≤ 60 s y ≤ 150 m no cuenta.
- Sin cobertura no hay intervalo: los gaps del recolector eliminan ejemplos, no crean falsos
  positivos.
- Con 4 paradas y pocos días las referencias horarias se apoyarán mucho en la mediana global;
  revisa la sección "Intervalo de referencia" del informe antes de sacar conclusiones.
- Modelos lineales, pensados para ser auditables y exportables; no se ajustan hiperparámetros.
