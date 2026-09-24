# Análisis de impacto

`emt-impact` compara el servicio observado **antes y después de un evento** (cambio de
frecuencias, obra, corte de tráfico, huelga, cambio de `EMT_STOPS`…) para cada ruta (línea,
parada, destino) del histórico del recolector. Trabaja sobre `arrival_estimates`,
`collection_cycles` y `collection_gaps`, sin consumir cuota EMT ni modificar la base de datos.

## 1. Qué calcula

Cada ruta se divide en dos ventanas: `[evento − before_days, evento)` y
`[evento, evento + after_days)`, esta última recortada al momento actual. Por ventana:

| Métrica | Definición |
| --- | --- |
| Intervalo medio | media de los intervalos completos entre buses distintos (mismos pasos inferidos que `emt-saturation`) cuyo segundo paso cae en la ventana |
| Espera media | `E[H²] / 2E[H]`: espera de un pasajero que llega al azar; penaliza la irregularidad |
| Tasa de saturación | fracción de intervalos `≥ max(1,5 × mediana(ruta, hora), 12 min)`. La mediana de referencia se calcula **solo con la ventana anterior**, de modo que la tasa posterior se mide contra el servicio habitual previo |
| Episodios/día | episodios de bunching (definición de `emt-bunching`) que empiezan en la ventana, divididos por los días locales con datos del recolector |

Además se guarda el intervalo medio por hora local de cada ventana para el gráfico del informe.

Una ruta solo entra en la comparación si **cada** ventana tiene ≥ `--min-headways` (20)
intervalos y ≥ `--min-days` (2) días con datos; el resto se lista como descartada con el motivo.

## 2. Cómo mide la incertidumbre

- **Cambio** = después − antes de cada métrica.
- **Intervalo de confianza bootstrap percentil** (`--confidence` 0,95, `--bootstrap-samples`
  1.000, semilla fija): se remuestrean con reemplazo los intervalos de cada ventana (cada uno
  con su etiqueta de saturación, para mantenerlos pareados) y los días con su número de episodios;
  el IC son los percentiles 2,5 y 97,5 de la diferencia. Un cambio es **significativo** si el IC
  no contiene el cero.
- **Mann-Whitney U** (bilateral, `scipy.stats.mannwhitneyu`) compara la distribución completa de
  intervalos antes y después; se informa su p-valor junto al intervalo medio. Es no paramétrico,
  así que no supone normalidad, pero sí independencia entre intervalos (aproximación razonable
  para intervalos consecutivos de un mismo día; con pocos días el p-valor será optimista).

## 3. Rutas de control y diferencias en diferencias

Un antes/después simple confunde el evento con todo lo demás que cambió a la vez (clima,
calendario escolar, obras en toda la red, cambio de cuota del recolector). Para descontarlo,
marca como **control** rutas que no deberían verse afectadas por el evento:

- `--control-stop <parada>` (repetible): todas las rutas de esa parada.
- `--control-line <línea>` (repetible): todas las rutas de esa línea.

Las demás rutas cargadas son **tratadas**. Con al menos una ruta de cada tipo se calcula, por
métrica:

```text
efecto neto = media(Δ tratadas) − media(Δ control)
```

con un IC que combina los remuestreos bootstrap de todas las rutas implicadas. Es el estimador
clásico de diferencias en diferencias; asume que, sin evento, tratadas y control habrían
evolucionado igual (tendencias paralelas). Elige controles de líneas comparables (misma zona,
frecuencia parecida) y comprueba en el informe que sus cambios son pequeños.

Sin controles, el informe muestra solo el antes/después de cada ruta, y **un cambio
significativo no implica causalidad**.

## 4. Comandos

```bash
pip install -e ".[analysis]"          # numpy + scipy (+ scikit-learn para el resto de fases)

# Demo sintética: línea 27 tratada (intervalos −20 %, muchas menos incidencias) y 45 de control
emt-impact demo --output reports/impact-demo [--days 14] [--seed 11]

# Histórico real: evento con zona horaria, ventanas y rutas afectadas/control
emt-impact analyze --event 2026-10-06T00:00:00+02:00 \
  --before-days 7 --after-days 7 \
  --stop 1182 --stop 1183 --control-stop 1170 \
  --output reports/impact-1006

# Dentro del análisis periódico (carpeta impact/ junto a bunching/, saturation/, frequency/)
emt-analysis run --output reports --days 14 --event 2026-10-06T00:00:00+02:00 --control-line 45
```

Opciones comunes: `--min-headways`, `--min-days`, `--bootstrap-samples`, `--confidence`,
`--ratio` y `--min-headway-minutes` (umbral de saturación), y los parámetros de inferencia de
pasos `--max-gap-seconds`, `--near-seconds`, `--near-metres`, `--vanish-seconds`. Sin `--stop`
se cargan todas las paradas del histórico. Códigos de salida: `0` hay rutas tratadas comparadas,
`3` ninguna, `2` error (directorio existente, evento futuro, ventanas < 1 día, BD inaccesible).

En `emt-analysis run`, el evento debe caer dentro de la ventana `--days`; las ventanas antes y
después se ajustan al histórico disponible (evento − inicio y ahora − evento).

## 5. Salidas

| Fichero | Contenido |
| --- | --- |
| `report.html` | Fuente (demo sintética / base de datos), evento y ventanas, efecto neto (DiD), resumen por ruta, detalle antes/después con IC y p-valor, gráfico de intervalo medio por hora, rutas descartadas |
| `summary.json` | Todo lo anterior en JSON: `source`, `event`, `start`, `end_exclusive`, `parameters`, `routes[]` (rol, ventanas, `changes[]` con `delta`, `ci_low`, `ci_high`, `p_value`, `significant`), `difference_in_differences[]`, `skipped[]` |
| `changes.csv` | Una fila por ruta y métrica, para hoja de cálculo |

El bot de Telegram (`/impacto [parada]`) lee el `summary.json` de la carpeta `impact/` del
último `emt-analysis run` y, como con los modelos, ignora los resultados sintéticos.

## 6. Cómo interpretar el informe

1. Mira primero **rutas descartadas**: si son muchas, no hay histórico suficiente en alguna
   ventana (recolector parado, evento demasiado reciente).
2. Con controles, la tabla **Diferencias en diferencias** es el resultado principal: signo,
   magnitud y si el IC excluye el cero. Un efecto neto negativo en intervalo medio, espera y
   saturación indica mejora.
3. Sin controles, compara el cambio de cada ruta con lo que cabría esperar por calendario
   (fin de semana, festivos) antes de atribuirlo al evento.
4. Los **episodios de bunching por día** son pocos y muy variables: con una semana por ventana
   rara vez serán significativos aunque el resto de métricas lo sean. Es lo esperable, no un
   fallo.
5. El p-valor de Mann-Whitney y el IC bootstrap pueden discrepar en casos límite; el IC es el
   criterio de «significativo» porque se aplica a todas las métricas.

## 7. Límites

- Los pasos son **inferidos** de estimaciones de llegada (ETA ≤ 60 s / ≤ 150 m o bus que
  desaparece); un cambio en la calidad de las ETAs de la EMT aparecería como cambio de servicio.
- Ventanas cortas mezclan días laborables y fin de semana en proporciones distintas si el evento
  no cae en el mismo día de la semana; usa múltiplos de 7 días.
- Las horas de la ventana posterior sin referencia en la anterior (p. ej. la ruta no circulaba
  a esa hora antes) cuentan como no saturadas.
- El análisis es observacional: sin controles bien elegidos y sin conocer otros cambios
  simultáneos, no permite afirmar que el evento causó la diferencia.
- Un único evento por ejecución; para varios eventos lanza varios `analyze` con distintas
  ventanas.
