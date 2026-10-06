# Video Integrity Checker

Aplicación de escritorio (Python + PyQt6) para comprobar en masa si los archivos **MKV, MP4 y AVI** de una biblioteca están sanos o corruptos, sin necesidad de abrirlos y verlos uno por uno.

Pensada para bibliotecas grandes (decenas de miles de archivos, varios TB): analiza por carpetas, trabaja en paralelo por disco, guarda los resultados y se puede detener y reanudar.

<!-- Añade aquí una captura de pantalla: ![Captura](docs/captura.png) -->

## Características

- **Solo lectura.** Nunca modifica ni escribe en los vídeos (ver [Garantías de solo lectura](#garantías-de-solo-lectura)).
- **Selección de carpeta** con opción de incluir subdirectorios.
- **Tabla en vivo** con los archivos según se procesan: estado, ruta, tamaño, duración, detalle y tiempo, con barra de progreso por archivo en el nivel completo.
- **Dos niveles de comprobación** (rápido y completo).
- **Clasificación de resultados** en OK, AVISOS, TRUNCADO, CORRUPTO e ILEGIBLE.
- **Un análisis a la vez por disco físico** (configurable) para no degradar el rendimiento de discos mecánicos, y prioridad baja (`nice`/`ionice`) opcional.
- **Reanudable:** los resultados se guardan en SQLite y los archivos ya analizados (mismo tamaño y fecha de modificación) se pueden omitir.
- **Informe final** en HTML o CSV.
- Filtro de la tabla (todos, solo problemas, solo OK, avisos, truncados, corruptos, ilegibles), contadores y estimación del tiempo restante.

## Requisitos

- Linux (desarrollado y probado para Manjaro)
- Python 3.9 o superior
- `ffmpeg` y `ffprobe`
- PyQt6

### Instalación de dependencias

```bash
# Arch / Manjaro
sudo pacman -S ffmpeg python-pyqt6

# Debian / Ubuntu
sudo apt install ffmpeg python3-pyqt6

# Fedora (ffmpeg completo desde RPM Fusion)
sudo dnf install ffmpeg python3-pyqt6

# Alternativa con pip para PyQt6
pip install PyQt6
```

## Uso

```bash
python3 video_integrity_checker.py
```

1. Elige la carpeta a comprobar con **Examinar…** (o escribe la ruta).
2. Marca **Incluir subdirectorios** si quieres analizar todo lo que cuelga de ella.
3. Elige el nivel de comprobación y las opciones.
4. Pulsa **Iniciar**. Puedes **Detener** en cualquier momento.
5. Al terminar, la aplicación ofrece generar el informe; también puedes hacerlo con **Generar informe…** cuando quieras.

### Opciones

| Opción | Descripción |
|---|---|
| Nivel | *Rápido* o *Completo* (ver más abajo). |
| Simultáneos por disco | Número de análisis en paralelo por cada dispositivo (1 a 4). Con discos mecánicos, lo recomendable es 1. |
| Omitir ya analizados | Reutiliza el resultado guardado si el archivo tiene el mismo tamaño y fecha de modificación y se analizó con un nivel igual o superior. |
| Baja prioridad | Ejecuta ffmpeg/ffprobe con `nice -n 10` e `ionice -c2 -n7`, si están disponibles. Útil si la máquina hace además de servidor de archivos. |

## Niveles de comprobación

| Nivel | Qué hace | Coste | Detecta |
|---|---|---|---|
| **Rápido** | `ffprobe` (estructura del contenedor) y decodificación de los últimos 10 segundos. En archivos muy cortos se decodifica entero. | Lee muy poco de cada archivo | Cabeceras rotas, archivos truncados, MP4 sin `moov`, AVI con tamaño declarado superior al real |
| **Completo** | Lo anterior más decodificación íntegra de todas las pistas de vídeo y audio (`ffmpeg -f null -`). | Lee el archivo entero; en bibliotecas grandes puede tardar días | Corrupción en el interior del vídeo o audio |

> En un análisis completo el cuello de botella suele ser la velocidad de lectura del disco (o de la red si la biblioteca está montada por SMB/NFS). Si es posible, ejecútalo directamente en la máquina que tiene los discos.

## Estados

| Estado | Significado |
|---|---|
| **OK** | Sin errores detectados. |
| **AVISOS** | Anomalías menores (pocos errores de decodificación, sin pista de vídeo, duración desconocida…). Probablemente se reproduce. |
| **TRUNCADO** | El archivo termina antes de lo que indica su cabecera (descarga o copia interrumpida). |
| **CORRUPTO** | Estructura inválida o errores de decodificación por encima del umbral. |
| **ILEGIBLE** | Error de lectura (E/S, permisos, tiempo de espera). Puede ser un problema del disco o del montaje y no del archivo. |
| **CANCELADO** | Archivo interrumpido al detener el análisis. |

La clasificación se basa en heurísticas sobre los mensajes de ffmpeg y ffprobe, así que puede haber falsos positivos o negativos. Ante un resultado dudoso, comprueba el detalle y reproduce el archivo.

Constantes ajustables al principio del script:

| Constante | Valor por defecto | Descripción |
|---|---|---|
| `ERROR_THRESHOLD` | `3` | Número de errores de decodificación a partir del cual un archivo se marca como CORRUPTO. |
| `TAIL_SECONDS` | `10` | Segundos finales que se decodifican en el nivel rápido. |
| `EXTS` | `.mkv`, `.mp4`, `.avi` | Extensiones analizadas. |

## Garantías de solo lectura

- La aplicación solo ejecuta `ffprobe` y `ffmpeg ... -f null -`. Este último decodifica y descarta el resultado, sin generar ningún archivo. El código comprueba que el comando de ffmpeg termine siempre en `-f null -`.
- Lo único que abre directamente es la cabecera de 12 bytes de los AVI, en modo lectura.
- Se compara el tamaño y la fecha de modificación de cada archivo antes y después del análisis. Si cambian, se avisa en el detalle y el resultado no se guarda.
- Los resultados se guardan fuera de la biblioteca, en `~/.local/share/video_integrity_checker/results.db`.
- Los informes solo se escriben donde indiques, y la aplicación se niega a guardarlos dentro de la carpeta analizada.
- Se ignoran las carpetas que empiezan por `.Trash`.

Para una garantía adicional, puedes montar la biblioteca en solo lectura (`mount -o ro` o la opción `ro` en `/etc/fstab`).

## Informes

- **HTML:** resumen por estado, listado de archivos con problemas (ordenados por gravedad) y listado plegable de los correctos.
- **CSV:** una fila por archivo (`estado;ruta;tamano_bytes;duracion_s;codec_video;detalle`), separado por `;` y con codificación UTF-8 con BOM para abrirlo directamente en hojas de cálculo.

## Limitaciones conocidas

- Si la biblioteca está bajo una unión de discos (mergerfs, por ejemplo), todo aparece como un único dispositivo y se analizará un archivo a la vez por defecto.
- La detección de truncado en AVI mediante la cabecera RIFF solo cubre el primer segmento del archivo; en AVI muy grandes (OpenDML) un truncado posterior puede pasar desapercibido en el nivel rápido.
- La tabla muestra todos los archivos de la sesión en memoria; con cientos de miles de archivos puede ralentizarse.
- Los patrones de truncado y errores se basan en los mensajes en inglés de ffmpeg (la aplicación fuerza `LC_ALL=C`).

## Cómo funciona

1. Escanea la carpeta (recursivamente si se pide) y agrupa los archivos por dispositivo (`st_dev`).
2. Consulta la base de datos SQLite para omitir los ya analizados.
3. Lanza hilos de trabajo, uno o varios por dispositivo, que ejecutan `ffprobe` y `ffmpeg` y clasifican el resultado.
4. Envía cada resultado a la interfaz mediante señales de Qt y lo guarda en la base de datos.

## Licencia

Añade aquí la licencia que prefieras (por ejemplo, MIT).
