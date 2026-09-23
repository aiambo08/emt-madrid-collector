# Optimización de frecuencias

`emt-frequency` describe la **oferta observada** de cada ruta (línea, parada, destino) por hora
local y propone **redistribuir la misma flota entre franjas** para reducir la espera media de los
pasajeros. Trabaja sobre el histórico del recolector (`arrival_estimates`, `collection_cycles`,
`collection_gaps`), sin consumir cuota EMT ni modificar la base de datos.

## 1. Qué calcula

Por ruta y hora local (Europe/Madrid), con los intervalos completos entre buses distintos que ya
usa la fase de saturación:

| Magnitud | Definición |
| --- | --- |
| Intervalo medio `H̄` | media de los intervalos cuya llegada final cae en esa hora |
| Regularidad `CV` | desviación típica / media de esos intervalos (0 = perfectamente regular) |
| Saturados | intervalos `≥ max(1,5 × mediana(ruta, hora), 12 min)` (misma etiqueta que `emt-saturation`) |
| Espera actual | `H̄ · (1 + CV²) / 2`: espera media de un pasajero que llega al azar |
| Espera regular | `H̄ / 2`: la misma oferta sin bunching |
| Ciclo | mediana del tiempo que tarda **el mismo bus** en volver a pasar por la parada en el mismo sentido (20–240 min) |
| Buses actuales | `ciclo / H̄`, buses en servicio implícitos en esa hora |

La espera `E[W] = E[H²] / 2E[H]` es exacta para llegadas aleatorias de pasajeros; expresarla
con media y `CV` la hace comparable entre horas y separa el efecto de la frecuencia (`H̄`) del
de la regularidad (`CV`).

## 2. Cómo optimiza

1. Presupuesto: suma de los buses implícitos de todas las franjas con datos (`horas-bus`).
2. Cada hora recibe al menos `ciclo / intervalo máximo` buses y como mucho
   `ciclo / intervalo mínimo` (3–20 min por defecto, `--min/--max-planned-headway-minutes`).
3. Los buses restantes se asignan uno a uno a la hora donde más reduce
   `peso × espera`. Como la espera es convexa en el número de buses, este reparto greedy es el
   óptimo entero para ese objetivo.
4. El `CV` de cada hora se mantiene: la propuesta mueve buses, no corrige el bunching. El
   informe muestra aparte la espera que se obtendría con `CV ≤ objetivo` (`--target-cv`, 0,3)
   para comparar ambas palancas.

Si el presupuesto no alcanza para el mínimo de todas las horas, el suelo baja a 1 bus por hora.
La propuesta nunca aumenta las horas-bus totales.

## 3. Peso de demanda

La API no informa de pasajeros. El peso por hora se elige con `--demand`:

| Modo | Peso de la hora | Cuándo usarlo |
| --- | --- | --- |
| `proxy` (defecto) | buses observados por día × (1 + tasa de intervalos saturados) | sin datos externos: asume que la oferta programada ya refleja la demanda y refuerza donde el servicio se degrada |
| `uniform` | 1 | minimizar la espera media sin ponderar; tiende a igualar intervalos |
| CSV (`--demand-csv`) | fila `hour,weight` (opcional `line`; la fila de la línea prima sobre la general) | aforos, validaciones de tarjeta o cualquier perfil horario propio |

Con `proxy` la propuesta suele parecerse a la oferta actual (el peso crece con los buses que
ya circulan): es un ejercicio de **regularización**, útil para detectar franjas con
sobreoferta o infraoferta relativa, no una estimación de demanda. Las conclusiones operativas
requieren un perfil externo. El fichero usado queda en `demand.csv` dentro de la salida.

## 4. Demo sin API ni base de datos

```bash
pip install -e ".[analysis]"
emt-frequency demo --days 14 --seed 7 --output reports/frequency-demo
```

La demo simula dos líneas con una flota que rota tras su ciclo (lo que permite estimar el ciclo
real), una franja valle con exceso de frecuencia (línea 27, 10–13 h) y una nocturna recortada
en exceso (línea 45, ≥ 20 h), y usa un perfil de demanda sintético con puntas de mañana y tarde.
**Sus resultados no dicen nada del servicio real.**

Con Docker:

```bash
docker build -t emt-madrid-collector .
docker run --name frequency-demo --entrypoint emt-frequency emt-madrid-collector demo --output /reports/demo
docker cp frequency-demo:/reports/demo ./reports/frequency-demo
docker rm frequency-demo
```

## 5. Análisis con tu histórico

```bash
emt-frequency analyze --start 2026-09-24T00:00:00Z --output reports/frequency \
  --stop 1170 --stop 1182 --demand-csv demanda.csv
```

Requisitos y umbrales (`summary.json` conserva los usados):

- Muestreo de 60 s en pocas paradas estables (mismas condiciones que bunching y saturación).
- `--min-days` (3) días con pasos inferidos por ruta y `--min-hour-samples` (5) intervalos
  completos por hora; las horas sin ese mínimo no entran en el plan.
- Al menos un bus que vuelva a pasar por la parada dentro de 20–240 min; si no, la ruta se
  descarta con motivo en el informe ("Rutas descartadas").
- `--service-start-hour`/`--service-end-hour` (6–23) acotan las franjas consideradas.

Salidas: `report.html` (autónomo, sin red), `summary.json`, `plan.csv` (una fila por ruta y
hora con todas las magnitudes) y `demand.csv`. Código de salida 0 con al menos una ruta
planificada, 3 si ninguna, 2 ante errores de argumentos o de base de datos.

## 6. Limitaciones

- **Una parada por sentido:** los pasos se infieren en la parada observada; el ciclo incluye
  la regulación en cabecera y puede sobrestimar el tiempo de vuelta si la flota espera en
  terminal. Los "buses" son equivalentes de horas-bus, no vehículos contados.
- **Sin demanda real:** con `proxy` o `uniform` el resultado describe la oferta, no a los
  pasajeros. Cualquier decisión de frecuencias necesita un perfil externo.
- **La regularidad se toma como dada:** el mayor ahorro de espera suele estar en el `CV`
  (bunching), que no se resuelve moviendo buses entre franjas. Compara las columnas "espera
  propuesta" y "espera regular" antes de sacar conclusiones.
- **Restricciones ausentes:** no considera turnos de conductores, capacidad de cocheras,
  tiempos de viaje variables por hora ni límites de capacidad de los vehículos.
- La demo es sintética; los ahorros que muestra son consecuencia de cómo se construyó.
