#!/usr/bin/env python3
"""
Video Integrity Checker
=======================
Comprueba si archivos MKV / MP4 / AVI están sanos o corruptos.

SOLO LECTURA: la aplicación nunca abre un archivo de vídeo para escribir.
Solo lanza `ffprobe` y `ffmpeg ... -f null -` (que decodifica sin generar
ningún archivo de salida), y lee 12 bytes de cabecera de los AVI. Los resultados se guardan en una base de datos
SQLite en ~/.local/share/video_integrity_checker/ y los informes solo se
escriben donde tú indiques (nunca dentro de la carpeta analizada).

Dependencias (Manjaro):  sudo pacman -S ffmpeg python-pyqt6
"""
import csv
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict, namedtuple
from datetime import datetime
from queue import Empty, Queue

from PyQt6.QtCore import QObject, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------
EXTS = {".mkv", ".mp4", ".avi"}
TAIL_SECONDS = 10          # nivel rápido: se decodifican los últimos N segundos
ERROR_THRESHOLD = 3        # a partir de N errores reales de decodificación -> CORRUPTO
DB_PATH = os.path.join(os.path.expanduser("~"), ".local", "share",
                       "video_integrity_checker", "results.db")
ENV = dict(os.environ, LC_ALL="C", LANG="C")

OK, WARN, TRUNC, CORRUPT, UNREAD, CANCEL = (
    "OK", "AVISOS", "TRUNCADO", "CORRUPTO", "ILEGIBLE", "CANCELADO")
RANK = {OK: 0, WARN: 1, TRUNC: 2, CORRUPT: 3, UNREAD: 4}
REPORT_ORDER = [CORRUPT, TRUNC, UNREAD, WARN, CANCEL, OK]
COLORS = {OK: "#c8e6c9", WARN: "#fff3b0", TRUNC: "#ffcc80",
          CORRUPT: "#ef9a9a", UNREAD: "#ce93d8", CANCEL: "#cfd8dc"}

IO_PAT = ("input/output error", "i/o error", "permission denied", "no such file",
          "stale file", "transport endpoint", "read error", "device or resource")
TRUNC_PAT = ("moov atom not found", "truncat", "unexpected end", "premature end",
             "end of file", "incomplete")
BENIGN_PAT = ("non monotonically", "non-monotonic", "pts has no value",
              "timestamps are unset", "past duration", "last message repeated")

File = namedtuple("File", "path rel size mtime_ns dev")


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_dur(s):
    if not s:
        return ""
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_td(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return (f"{d}d " if d else "") + f"{h}:{m:02d}:{s:02d}"


def _float(x):
    try:
        f = float(x)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _norm(line):
    line = re.sub(r"0x[0-9a-fA-F]+", "0x…", line)
    return re.sub(r"\d+", "#", line).strip()


def is_inside(path, root):
    try:
        path, root = os.path.realpath(path), os.path.realpath(root)
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Base de datos de resultados (caché para poder reanudar)
# --------------------------------------------------------------------------
class ResultDB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.Lock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS results (
               path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
               level INTEGER, status TEXT, detail TEXT, duration REAL,
               vcodec TEXT, analyzed_at TEXT)""")
        self.con.commit()

    def get(self, path, size, mtime_ns, level):
        with self.lock:
            row = self.con.execute(
                "SELECT level,status,detail,duration,vcodec,analyzed_at FROM results "
                "WHERE path=? AND size=? AND mtime_ns=?",
                (path, size, mtime_ns)).fetchone()
        if row and row[0] >= level:
            return dict(level=row[0], status=row[1], detail=row[2],
                        duration=row[3], vcodec=row[4], when=row[5])
        return None

    def put(self, path, size, mtime_ns, level, r):
        with self.lock:
            self.con.execute(
                "INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?)",
                (path, size, mtime_ns, level, r["status"], r["detail"],
                 r["duration"], r["vcodec"], r["when"]))
            self.con.commit()


# --------------------------------------------------------------------------
# Motor de análisis (ffprobe / ffmpeg, solo lectura)
# --------------------------------------------------------------------------
class Control:
    """Gestiona la parada y los procesos en ejecución."""

    def __init__(self, low_priority):
        self.stop = threading.Event()
        self.procs, self.lock, self.prefix = set(), threading.Lock(), []
        if low_priority:
            if shutil.which("nice"):
                self.prefix += ["nice", "-n", "10"]
            if shutil.which("ionice"):
                self.prefix += ["ionice", "-c2", "-n7"]

    def register(self, p):
        with self.lock:
            self.procs.add(p)

    def unregister(self, p):
        with self.lock:
            self.procs.discard(p)

    def kill_all(self):
        self.stop.set()
        with self.lock:
            for p in list(self.procs):
                try:
                    p.kill()
                except OSError:
                    pass


def _run(cmd, ctl, timeout):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, text=True, errors="replace", env=ENV)
    ctl.register(p)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return 124, "", "ffprobe: tiempo de espera agotado"
    finally:
        ctl.unregister(p)


def probe_file(path, ctl):
    cmd = ctl.prefix + ["ffprobe", "-v", "error", "-hide_banner", "-of", "json",
                        "-show_entries",
                        "format=duration:stream=codec_type,codec_name,duration", path]
    rc, out, err = _run(cmd, ctl, 180)
    try:
        data = json.loads(out) if out.strip() else {}
    except ValueError:
        data = {}
    streams = data.get("streams", [])
    durs = [_float(data.get("format", {}).get("duration"))]
    durs += [_float(s.get("duration")) for s in streams]
    durs = [d for d in durs if d]
    vids = [s.get("codec_name", "?") for s in streams if s.get("codec_type") == "video"]
    return dict(rc=rc, lines=[l for l in err.splitlines() if l.strip()],
                duration=max(durs) if durs else None, vcodec=", ".join(vids),
                has_video=bool(vids), nstreams=len(streams))


def run_decode(path, ctl, pre_input=(), duration=None, on_progress=None, timeout=None):
    """Decodifica a /dev/null (-f null -). No se escribe ningún archivo."""
    cmd = ctl.prefix + ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-v", "error",
                        "-progress", "pipe:1", *pre_input, "-i", path,
                        "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"]
    assert cmd[-3:] == ["-f", "null", "-"], "el comando debe ser de solo lectura"
    tmp = tempfile.TemporaryFile()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=tmp,
                         stdin=subprocess.DEVNULL, text=True, env=ENV)
    ctl.register(p)
    timed_out, too_many = [], []
    timer = None
    if timeout:
        def _to():
            timed_out.append(1)
            try:
                p.kill()
            except OSError:
                pass
        timer = threading.Timer(timeout, _to)
        timer.daemon = True
        timer.start()
    out_time, last_pct = 0.0, -1
    try:
        for line in p.stdout:
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    out_time = max(out_time, int(line.split("=", 1)[1]) / 1e6)
                except ValueError:
                    continue
                if on_progress and duration:
                    pct = min(99, int(out_time * 100 / duration))
                    if pct != last_pct:
                        last_pct = pct
                        on_progress(pct)
                if os.fstat(tmp.fileno()).st_size > 2_000_000:
                    too_many.append(1)
                    p.kill()
                    break
            if ctl.stop.is_set():
                p.kill()
                break
    finally:
        if timer:
            timer.cancel()
        p.wait()
        ctl.unregister(p)
    tmp.seek(0)
    lines = tmp.read(4_000_000).decode("utf-8", "replace").splitlines()
    tmp.close()
    return dict(rc=p.returncode, out_time=out_time, lines=lines,
                stopped=ctl.stop.is_set(), timed_out=bool(timed_out),
                too_many=bool(too_many))


def avi_declared_size_exceeds(path):
    """True si la cabecera RIFF de un AVI declara más bytes de los que tiene el archivo."""
    try:
        with open(path, "rb") as fh:
            h = fh.read(12)
        if h[:4] == b"RIFF" and h[8:12] == b"AVI ":
            return int.from_bytes(h[4:8], "little") + 8 > os.path.getsize(path)
    except OSError:
        pass
    return False


def scan_lines(lines):
    counts, io_line, trunc_line = Counter(), "", ""
    real = benign = 0
    for ln in lines:
        low = ln.lower()
        if not ln.strip():
            continue
        if not io_line and any(s in low for s in IO_PAT):
            io_line = ln
        if not trunc_line and any(s in low for s in TRUNC_PAT):
            trunc_line = ln
        if any(s in low for s in BENIGN_PAT):
            benign += 1
            continue
        real += 1
        counts[_norm(ln)] += 1
    top = "; ".join(f"«{m[:100]}» x{c}" for m, c in counts.most_common(3))
    return dict(io=io_line, trunc=trunc_line, real=real, benign=benign, top=top)


def analyze_file(path, level, ctl, on_progress):
    """level 1 = estructura + final del archivo; level 2 = decodificación completa."""
    pr = probe_file(path, ctl)
    if ctl.stop.is_set():
        return None
    res = dict(status=OK, detail="", duration=pr["duration"], vcodec=pr["vcodec"])
    a = scan_lines(pr["lines"])
    if pr["rc"] == 124:
        res.update(status=UNREAD, detail="ffprobe: tiempo de espera agotado")
        return res
    if pr["rc"] != 0 or pr["nstreams"] == 0:
        msg = (pr["lines"][0] if pr["lines"] else
               f"ffprobe terminó con código {pr['rc']} / sin pistas")[:220]
        st = UNREAD if a["io"] else TRUNC if a["trunc"] else CORRUPT
        res.update(status=st, detail="Estructura ilegible: " + msg)
        return res

    status, notes = OK, []

    def note(s, msg):
        nonlocal status
        notes.append(msg)
        if RANK[s] > RANK[status]:
            status = s

    if path.lower().endswith(".avi") and avi_declared_size_exceeds(path):
        note(TRUNC, "AVI: la cabecera declara más tamaño del que tiene el archivo")
    if not pr["has_video"]:
        note(WARN, "sin pista de vídeo")
    if a["real"]:
        note(WARN, "ffprobe: " + a["top"])

    dur = pr["duration"]
    if level >= 2 or (dur and dur <= 3 * TAIL_SECONDS):
        mode = "full"
    elif dur:
        mode = "tail"
    else:
        mode = None
        note(WARN, "duración desconocida: no se pudo comprobar el final")

    if mode:
        d = run_decode(
            path, ctl,
            pre_input=["-sseof", f"-{TAIL_SECONDS}"] if mode == "tail" else [],
            duration=dur if mode == "full" else None,
            on_progress=on_progress if mode == "full" else None,
            timeout=240 if mode == "tail" else None)
        if d["stopped"]:
            return None
        b = scan_lines(d["lines"])
        if d["timed_out"]:
            note(WARN, "tiempo agotado al comprobar el final del archivo")
        if d["too_many"]:
            note(CORRUPT, "demasiados errores de decodificación (análisis abortado)")
        if b["io"]:
            note(UNREAD, "error de lectura (E/S): " + b["io"][:150])
        if b["trunc"]:
            note(TRUNC, "indicios de truncado: " + b["trunc"][:150])
        if mode == "full" and not d["timed_out"] and not d["too_many"]:
            if dur - d["out_time"] > max(10, 0.03 * dur):
                note(TRUNC, f"solo se decodificó {fmt_dur(d['out_time'])} de "
                            f"{fmt_dur(dur)} declarados")
        if mode == "tail" and not d["timed_out"] and d["out_time"] < TAIL_SECONDS * 0.5:
            note(TRUNC, "no se puede leer el final del archivo")
        if b["real"] >= ERROR_THRESHOLD:
            note(CORRUPT, f"{b['real']} errores de decodificación: {b['top']}")
        elif b["real"]:
            note(WARN, f"{b['real']} error(es) menor(es): {b['top']}")
        elif d["rc"] != 0 and not (d["timed_out"] or d["too_many"]):
            note(CORRUPT, f"ffmpeg terminó con código {d['rc']}")

    if not notes:
        notes = ["Correcto (decodificación completa)" if mode == "full"
                 else "Correcto (estructura y final del archivo)"]
    res.update(status=status, detail=" | ".join(notes))
    return res


# --------------------------------------------------------------------------
# Ejecutor: escaneo + hilos (1 cola por disco físico)
# --------------------------------------------------------------------------
class Runner(QObject):
    status = pyqtSignal(str)
    scan_done = pyqtSignal(int, int)
    row_started = pyqtSignal(str, str, int)
    row_progress = pyqtSignal(str, int)
    row_done = pyqtSignal(object)
    all_done = pyqtSignal(bool)

    def __init__(self, root, recursive, level, per_dev, skip_cached, low_prio, db):
        super().__init__()
        self.root, self.recursive, self.level = root, recursive, level
        self.per_dev, self.skip_cached, self.db = per_dev, skip_cached, db
        self.ctl = Control(low_prio)

    def start(self):
        threading.Thread(target=self._main, daemon=True).start()

    def stop(self):
        self.ctl.kill_all()

    def _scan(self):
        files, bad = [], []
        if self.recursive:
            walker = os.walk(self.root, onerror=lambda e: None)
        else:
            walker = [next(os.walk(self.root), (self.root, [], []))]
        for dirpath, dirnames, filenames in walker:
            if self.ctl.stop.is_set():
                break
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".Trash"))
            for name in sorted(filenames):
                if os.path.splitext(name)[1].lower() not in EXTS:
                    continue
                p = os.path.join(dirpath, name)
                rel = os.path.relpath(p, self.root)
                try:
                    st = os.stat(p)
                except OSError as e:
                    bad.append(dict(path=p, rel=rel, size=0, status=UNREAD,
                                    detail=f"No se puede acceder: {e}", duration=None,
                                    vcodec="", seconds=0, cached=False))
                    continue
                files.append(File(p, rel, st.st_size, st.st_mtime_ns, st.st_dev))
        return files, bad

    def _main(self):
        self.status.emit("Buscando archivos…")
        files, bad = self._scan()
        self.scan_done.emit(len(files) + len(bad), sum(f.size for f in files))
        for b in bad:
            self.row_done.emit(b)
        queues = defaultdict(Queue)
        for f in files:
            if self.ctl.stop.is_set():
                break
            if self.skip_cached:
                c = self.db.get(f.path, f.size, f.mtime_ns, self.level)
                if c:
                    c.update(path=f.path, rel=f.rel, size=f.size, seconds=0, cached=True)
                    self.row_done.emit(c)
                    continue
            queues[f.dev].put(f)
        threads = []
        for q in queues.values():
            for _ in range(self.per_dev):
                t = threading.Thread(target=self._worker, args=(q,), daemon=True)
                t.start()
                threads.append(t)
        self.status.emit(f"Analizando ({len(queues)} disco(s), {self.per_dev} por disco)…")
        for t in threads:
            t.join()
        self.all_done.emit(self.ctl.stop.is_set())

    def _worker(self, q):
        while not self.ctl.stop.is_set():
            try:
                f = q.get_nowait()
            except Empty:
                return
            self.row_started.emit(f.path, f.rel, f.size)
            t0 = time.time()
            base = dict(path=f.path, rel=f.rel, size=f.size, cached=False)
            try:
                st0 = os.stat(f.path)
                key = (st0.st_size, st0.st_mtime_ns)
                res = analyze_file(f.path, self.level, self.ctl,
                                   lambda pct, p=f.path: self.row_progress.emit(p, pct))
                if res is None:
                    res = dict(status=CANCEL, detail="Interrumpido por el usuario",
                               duration=None, vcodec="")
                else:
                    st1 = os.stat(f.path)
                    res["when"] = datetime.now().isoformat(timespec="seconds")
                    if (st1.st_size, st1.st_mtime_ns) != key:
                        res["detail"] += " | ⚠ el archivo cambió durante el análisis"
                    else:
                        self.db.put(f.path, key[0], key[1], self.level, res)
            except Exception as e:  # noqa: BLE001
                res = dict(status=UNREAD, detail=f"Error: {e}", duration=None, vcodec="")
            res.update(base, seconds=time.time() - t0)
            self.row_done.emit(res)


# --------------------------------------------------------------------------
# Informes
# --------------------------------------------------------------------------
def build_csv(path, results):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["estado", "ruta", "tamano_bytes", "duracion_s", "codec_video", "detalle"])
        for r in results:
            w.writerow([r["status"], r["path"], r["size"],
                        round(r["duration"], 1) if r.get("duration") else "",
                        r.get("vcodec", ""), r["detail"]])


def build_html(path, results, meta):
    e = html.escape
    counts = Counter(r["status"] for r in results)

    def rows(items):
        return "".join(
            f'<tr class="s{e(r["status"])}"><td>{e(r["status"])}</td><td>{e(r["path"])}</td>'
            f'<td>{human_size(r["size"])}</td><td>{fmt_dur(r.get("duration"))}</td>'
            f'<td>{e(r["detail"])}</td></tr>' for r in items)

    head = "<tr><th>Estado</th><th>Archivo</th><th>Tamaño</th><th>Duración</th><th>Detalle</th></tr>"
    bad = [r for r in results if r["status"] != OK]
    good = [r for r in results if r["status"] == OK]
    css = "".join(f".s{k}{{background:{v}}}" for k, v in COLORS.items())
    summary = "".join(f'<tr class="s{k}"><td>{k}</td><td>{counts.get(k, 0)}</td></tr>'
                      for k in REPORT_ORDER if counts.get(k) or k in (OK, CORRUPT, TRUNC))
    doc = f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<title>Informe de integridad de vídeo</title><style>
body{{font-family:sans-serif;margin:2em;color:#222}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #bbb;padding:4px 8px;font-size:13px;text-align:left;vertical-align:top}}
th{{background:#eee}}.sum{{width:auto}}{css}</style></head><body>
<h1>Informe de integridad de vídeo</h1>
<p><b>Carpeta:</b> {e(meta['root'])} ({'con' if meta['recursive'] else 'sin'} subdirectorios)<br>
<b>Nivel:</b> {e(meta['level'])}<br><b>Inicio:</b> {e(meta['started'])} &nbsp;
<b>Generado:</b> {datetime.now():%Y-%m-%d %H:%M:%S}<br>
<b>Archivos:</b> {len(results)} ({human_size(sum(r['size'] for r in results))})</p>
<table class="sum"><tr><th>Estado</th><th>Archivos</th></tr>{summary}</table>
<h2>Archivos con problemas ({len(bad)})</h2>
<table>{head}{rows(sorted(bad, key=lambda r: (REPORT_ORDER.index(r['status']), r['path'])))}</table>
<h2>Archivos correctos ({len(good)})</h2><details><summary>Mostrar listado</summary>
<table>{head}{rows(sorted(good, key=lambda r: r['path']))}</table></details>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


# --------------------------------------------------------------------------
# Interfaz
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    COLS = ["Estado", "Archivo", "Tamaño", "Duración", "Detalle", "Tiempo"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Integrity Checker (solo lectura)")
        self.resize(1250, 750)
        self.cfg = QSettings("VideoIntegrityChecker", "VideoIntegrityChecker")
        self.db = ResultDB(DB_PATH)
        self.runner = None
        self.rows, self.results = {}, {}
        self.counts = Counter()
        self.meta = {}
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    # ---- construcción de la interfaz
    def _build_ui(self):
        c = self.cfg
        self.path_edit = QLineEdit(c.value("path", "", str))
        btn_browse = QPushButton("Examinar…")
        btn_browse.clicked.connect(self.browse)
        self.chk_rec = QCheckBox("Incluir subdirectorios")
        self.chk_rec.setChecked(c.value("recursive", True, bool))
        self.cmb_level = QComboBox()
        self.cmb_level.addItem("Rápido: estructura + final del archivo", 1)
        self.cmb_level.addItem("Completo: decodificación íntegra", 2)
        self.cmb_level.setCurrentIndex(c.value("level_idx", 0, int))
        self.spin_dev = QSpinBox()
        self.spin_dev.setRange(1, 4)
        self.spin_dev.setValue(c.value("per_dev", 1, int))
        self.chk_skip = QCheckBox("Omitir ya analizados (mismo tamaño y fecha)")
        self.chk_skip.setChecked(c.value("skip", True, bool))
        self.chk_low = QCheckBox("Baja prioridad (nice/ionice)")
        self.chk_low.setChecked(c.value("low", True, bool))

        self.btn_start = QPushButton("Iniciar")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop = QPushButton("Detener")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_report = QPushButton("Generar informe…")
        self.btn_report.setEnabled(False)
        self.btn_report.clicked.connect(self.generate_report)
        self.cmb_filter = QComboBox()
        for label, data in [("Mostrar: todos", None), ("Solo problemas", "PROBLEMS"),
                            ("Solo OK", OK), ("Avisos", WARN), ("Truncados", TRUNC),
                            ("Corruptos", CORRUPT), ("Ilegibles", UNREAD)]:
            self.cmb_filter.addItem(label, data)
        self.cmb_filter.currentIndexChanged.connect(self.apply_filter)
        self.chk_scroll = QCheckBox("Seguir último")
        self.chk_scroll.setChecked(True)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        for i, w in enumerate([130, 430, 80, 80, 380, 70]):
            self.table.setColumnWidth(i, w)

        self.bar = QProgressBar()
        self.bar.setFormat("%v / %m archivos")
        self.lbl_counts = QLabel("")
        self.lbl_eta = QLabel("")

        def row(*widgets):
            h = QHBoxLayout()
            for w in widgets:
                h.addWidget(w)
            return h

        lay = QVBoxLayout()
        top = row(QLabel("Carpeta:"), self.path_edit, btn_browse, self.chk_rec)
        top.setStretch(1, 1)
        lay.addLayout(top)
        opts = row(QLabel("Nivel:"), self.cmb_level, QLabel("Simultáneos por disco:"),
                   self.spin_dev, self.chk_skip, self.chk_low)
        opts.addStretch(1)
        lay.addLayout(opts)
        act = row(self.btn_start, self.btn_stop, self.btn_report, self.cmb_filter,
                  self.chk_scroll)
        act.addStretch(1)
        lay.addLayout(act)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.bar)
        lay.addLayout(row(self.lbl_counts, self.lbl_eta))
        root = QWidget()
        root.setLayout(lay)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Listo. Solo lectura: ningún archivo de vídeo se modifica.")

    # ---- acciones
    def browse(self):
        d = QFileDialog.getExistingDirectory(
            self, "Elegir carpeta a comprobar",
            self.path_edit.text() or os.path.expanduser("~"))
        if d:
            self.path_edit.setText(d)

    def save_cfg(self):
        c = self.cfg
        c.setValue("path", self.path_edit.text())
        c.setValue("recursive", self.chk_rec.isChecked())
        c.setValue("level_idx", self.cmb_level.currentIndex())
        c.setValue("per_dev", self.spin_dev.value())
        c.setValue("skip", self.chk_skip.isChecked())
        c.setValue("low", self.chk_low.isChecked())

    def start(self):
        root = self.path_edit.text().strip()
        if not os.path.isdir(root):
            QMessageBox.warning(self, "Carpeta no válida", "Elige una carpeta existente.")
            return
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                QMessageBox.critical(self, "Falta " + tool,
                                     "Instala ffmpeg:\n\nsudo pacman -S ffmpeg")
                return
        self.save_cfg()
        self.table.setRowCount(0)
        self.rows, self.results, self.counts = {}, {}, Counter()
        self.bytes_total = self.bytes_done = self.bytes_fresh = 0
        self.n_total = self.n_done = 0
        self.bar.setMaximum(1)
        self.bar.setValue(0)
        level = self.cmb_level.currentData()
        self.meta = dict(root=root, recursive=self.chk_rec.isChecked(),
                         level=self.cmb_level.currentText(),
                         started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.t0 = time.time()
        self.runner = r = Runner(root, self.chk_rec.isChecked(), level,
                                 self.spin_dev.value(), self.chk_skip.isChecked(),
                                 self.chk_low.isChecked(), self.db)
        r.status.connect(self.statusBar().showMessage)
        r.scan_done.connect(self.on_scan_done)
        r.row_started.connect(self.on_row_started)
        r.row_progress.connect(self.on_row_progress)
        r.row_done.connect(self.on_row_done)
        r.all_done.connect(self.on_all_done)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_report.setEnabled(False)
        self.timer.start(1000)
        r.start()

    def stop(self):
        if self.runner:
            self.statusBar().showMessage("Deteniendo…")
            self.btn_stop.setEnabled(False)
            self.runner.stop()

    def closeEvent(self, ev):
        if self.runner and not self.btn_start.isEnabled():
            if QMessageBox.question(self, "Análisis en curso",
                                    "¿Detener el análisis y salir?") != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self.runner.stop()
        self.save_cfg()
        ev.accept()

    # ---- señales del ejecutor
    def on_scan_done(self, total, nbytes):
        self.n_total, self.bytes_total = total, nbytes
        self.bar.setMaximum(max(total, 1))
        self.statusBar().showMessage(f"{total} archivos encontrados ({human_size(nbytes)}).")

    def _new_row(self, path, rel, size):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, text in enumerate(["ANALIZANDO…", rel, human_size(size), "", "", ""]):
            it = QTableWidgetItem(text)
            it.setToolTip(path)
            if col in (2, 3, 5):
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, col, it)
        self.rows[path] = row
        return row

    def on_row_started(self, path, rel, size):
        row = self._new_row(path, rel, size)
        if self.chk_scroll.isChecked():
            self.table.scrollToItem(self.table.item(row, 0))

    def on_row_progress(self, path, pct):
        row = self.rows.get(path)
        if row is not None:
            self.table.item(row, 0).setText(f"ANALIZANDO {pct}%")

    def on_row_done(self, r):
        row = self.rows.get(r["path"])
        if row is None:
            row = self._new_row(r["path"], r["rel"], r["size"])
        st = r["status"]
        detail = r["detail"]
        if r.get("cached"):
            detail = f"(previo {str(r.get('when', ''))[:10]}) " + detail
        self.table.item(row, 0).setText(st)
        self.table.item(row, 0).setBackground(QBrush(QColor(COLORS.get(st, "#ffffff"))))
        self.table.item(row, 0).setForeground(QBrush(QColor("black")))
        self.table.item(row, 3).setText(fmt_dur(r.get("duration")))
        self.table.item(row, 4).setText(detail)
        self.table.item(row, 4).setToolTip(detail)
        self.table.item(row, 5).setText("" if r.get("cached") else f"{r['seconds']:.1f} s")
        self.table.setRowHidden(row, not self._visible(st))
        self.results[r["path"]] = r
        if st != CANCEL:
            self.counts[st] += 1
            self.n_done += 1
            self.bytes_done += r["size"]
            if not r.get("cached"):
                self.bytes_fresh += r["size"]
            self.bar.setValue(self.n_done)
        self._update_counts()

    def on_all_done(self, stopped):
        self.timer.stop()
        self._tick()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_report.setEnabled(bool(self.results))
        self.statusBar().showMessage("Análisis interrumpido." if stopped else "Análisis terminado.")
        if not stopped and self.results:
            if QMessageBox.question(self, "Análisis terminado",
                                    "¿Generar el informe final ahora?") == QMessageBox.StandardButton.Yes:
                self.generate_report()

    # ---- presentación
    def _visible(self, status):
        f = self.cmb_filter.currentData()
        return f is None or status == f or (f == "PROBLEMS" and status != OK)

    def apply_filter(self):
        for row in range(self.table.rowCount()):
            st = self.table.item(row, 0).text()
            self.table.setRowHidden(row, st.startswith("ANALIZANDO") is False
                                    and not self._visible(st))

    def _update_counts(self):
        c = self.counts
        self.lbl_counts.setText(
            f"OK {c[OK]}  ·  Avisos {c[WARN]}  ·  Truncados {c[TRUNC]}  ·  "
            f"Corruptos {c[CORRUPT]}  ·  Ilegibles {c[UNREAD]}")

    def _tick(self):
        el = time.time() - self.t0
        txt = f"Tiempo {fmt_td(el)}  ·  {human_size(self.bytes_done)} / {human_size(self.bytes_total)}"
        if self.bytes_fresh and el > 5:
            rate = self.bytes_fresh / el
            left = max(self.bytes_total - self.bytes_done, 0)
            txt += f"  ·  {human_size(rate)}/s  ·  quedan ~{fmt_td(left / rate)}"
        self.lbl_eta.setText(txt)

    # ---- informe
    def generate_report(self):
        if not self.results:
            return
        default = os.path.join(os.path.expanduser("~"),
                               f"informe_videos_{datetime.now():%Y%m%d_%H%M%S}.html")
        path, flt = QFileDialog.getSaveFileName(
            self, "Guardar informe", default, "Informe HTML (*.html);;CSV (*.csv)")
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".html", ".csv"):
            ext = ".csv" if "csv" in flt else ".html"
            path += ext
        if is_inside(path, self.meta.get("root", "")):
            QMessageBox.warning(self, "Ubicación no permitida",
                                "El informe quedaría dentro de la carpeta analizada.\n"
                                "Elige otra ubicación para no escribir en tu biblioteca.")
            return
        items = sorted(self.results.values(),
                       key=lambda r: (REPORT_ORDER.index(r["status"]), r["path"]))
        try:
            if ext == ".csv":
                build_csv(path, items)
            else:
                build_html(path, items, self.meta)
        except OSError as e:
            QMessageBox.critical(self, "No se pudo guardar", str(e))
            return
        self.statusBar().showMessage(f"Informe guardado en {path}")


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
