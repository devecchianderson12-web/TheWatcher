"""Widgets: vídeo por cámara (clic derecho = calidad) y gestor de cámaras."""
import uuid

import time

from qt import (event_pos, pyqtSignal, QAction, QActionGroup, QApplication, QByteArray, QColor,
               QDialog, QDialogButtonBox, QDrag, qexec, QFontMetrics, QFormLayout, QHBoxLayout, QImage,
               QLineEdit, QListWidget, QMenu, QMimeData, QObject, QPainter, QPainterPath, QPen, QPixmap,
               QPoint, QPushButton, QRect, QRectF, Qt, QVBoxLayout, QWidget)

import theme
from stream_manager import QUALITIES, QUALITY_SUB, CameraWorker, cover_crop, fit_rect
from sysmon import SysMonitor


PERF_OPACITY = ((1.0, "100 %"), (0.8, "80 %"), (0.6, "60 %"), (0.4, "40 %"), (0.2, "20 %"))
PERF_INTERVALS = ((1.0, "Cada 1 s"), (2.0, "Cada 2 s (recomendado)"), (5.0, "Cada 5 s (mínimo consumo)"))
FPS_OPTIONS = ((8, "8 fps (ahorra CPU)"), (12, "12 fps"), (15, "15 fps"), (20, "20 fps"),
               (25, "25 fps"), (30, "30 fps"), (0, "Todos los fotogramas"))
FIT_OPTIONS = (("stretch", "Estirar (sin barras negras)"), ("fill", "Llenar (recorta los bordes)"),
               ("fit", "Original (con barras negras)"))
RES_OPTIONS = ((0, "Automática"), (720, "720p"), (480, "480p"), (360, "360p"), (240, "240p"))
PERF_ITEMS = (("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU"), ("net", "Tráfico de red"))
PERF_DEFAULT_ITEMS = ("cpu", "ram", "gpu")
NET_UNITS = (("auto_B", "Automático en bytes (KB/s · MB/s)"), ("auto_b", "Automático en bits (Kbps · Mbps)"),
             ("KB", "Kilobytes (KB/s)"), ("MB", "Megabytes (MB/s)"),
             ("Kb", "Kilobits (Kbps)"), ("Mb", "Megabits (Mbps)"), ("Gb", "Gigabits (Gbps)"))
NET_DIRS = (("both", "Bajada y subida"), ("down", "Solo bajada"), ("up", "Solo subida"))


def perf_items(cfg):
    """Qué muestra el monitor, en orden fijo; nunca vacío."""
    raw = cfg.get("perf_items")
    keys = [k for k, _ in PERF_ITEMS if isinstance(raw, (list, tuple)) and k in raw]
    return keys or list(PERF_DEFAULT_ITEMS)


def fmt_rate(bps, unit):
    """bytes/s -> «1.2 MB/s». Bytes en base 1024 (como Windows); bits en base 1000 (como los routers)."""
    if unit in ("auto_b", "Kb", "Mb", "Gb"):
        v, base, names = bps * 8.0, 1000.0, ("bps", "Kbps", "Mbps", "Gbps")
        fixed = {"Kb": 1, "Mb": 2, "Gb": 3}.get(unit)
    else:
        v, base, names = float(bps), 1024.0, ("B/s", "KB/s", "MB/s", "GB/s")
        fixed = {"KB": 1, "MB": 2}.get(unit)
    if fixed is None:
        i = 0
        while v >= base and i < 3:
            v, i = v / base, i + 1
    else:
        v, i = v / base ** fixed, fixed
    return (f"{v:.1f}" if v < 10 else f"{v:.0f}") + " " + names[i]


def fill_perf_menu(pmenu, app, show_toggle=True):
    """Menú del monitor de rendimiento (desde la pastilla o desde el clic derecho de una cámara)."""
    cfg = app.cfg
    if show_toggle:
        pshow = pmenu.addAction("Mostrar el monitor")
        pshow.setCheckable(True)
        pshow.setChecked(bool(cfg.get("perf_monitor", True)))
        pshow.triggered.connect(lambda _=False: app.toggle_perf())
        pmenu.addSeparator()
    sec = pmenu.addAction("Qué mostrar")
    sec.setEnabled(False)
    items = perf_items(cfg)
    for key, label in PERF_ITEMS:
        act = pmenu.addAction(label)
        act.setCheckable(True)
        act.setChecked(key in items)
        act.setEnabled(not (key in items and len(items) == 1))     # siempre queda al menos uno
        act.triggered.connect(lambda on=False, k=key: app.set_perf_item(k, on))
    unit_now = cfg.get("perf_net_unit", "auto_B")
    umenu = pmenu.addMenu("Unidad del tráfico de red")
    ugroup = QActionGroup(umenu)
    ugroup.setExclusive(True)
    for u, label in NET_UNITS:
        uact = QAction(label, umenu)
        uact.setCheckable(True)
        uact.setChecked(u == unit_now)
        uact.triggered.connect(lambda _=False, u=u: app.set_perf_net(unit=u))
        ugroup.addAction(uact)
        umenu.addAction(uact)
    dmenu = pmenu.addMenu("Dirección del tráfico")
    dgroup = QActionGroup(dmenu)
    dgroup.setExclusive(True)
    for d, label in NET_DIRS:
        dact = QAction(label, dmenu)
        dact.setCheckable(True)
        dact.setChecked(d == cfg.get("perf_net_dir", "both"))
        dact.triggered.connect(lambda _=False, d=d: app.set_perf_net(direction=d))
        dgroup.addAction(dact)
        dmenu.addAction(dact)
    pmenu.addSeparator()
    op_now = float(cfg.get("perf_opacity", 0.85))
    omenu = pmenu.addMenu(f"Opacidad ({op_now * 100:.0f} %)")
    ogroup = QActionGroup(omenu)
    ogroup.setExclusive(True)
    for v, label in PERF_OPACITY:
        oact = QAction(label, omenu)
        oact.setCheckable(True)
        oact.setChecked(abs(v - op_now) < 0.01)
        oact.triggered.connect(lambda _=False, v=v: app.set_perf_opacity(v))
        ogroup.addAction(oact)
        omenu.addAction(oact)
    omenu.addSeparator()
    omenu.addAction("Aumentar (+10 %)", lambda: app.step_perf_opacity(0.1))
    omenu.addAction("Disminuir (−10 %)", lambda: app.step_perf_opacity(-0.1))
    imenu = pmenu.addMenu("Actualización")
    igroup = QActionGroup(imenu)
    igroup.setExclusive(True)
    for secs, label in PERF_INTERVALS:
        iact = QAction(label, imenu)
        iact.setCheckable(True)
        iact.setChecked(abs(secs - float(cfg.get("perf_interval", 2.0))) < 0.01)
        iact.triggered.connect(lambda _=False, secs=secs: app.set_perf_interval(secs))
        igroup.addAction(iact)
        imenu.addAction(iact)
    return pmenu


def _corner_pixmaps(r, dpr, color):
    """Las 4 esquinas «de afuera» de un rectángulo redondeado, pintadas del color del fondo.
    Se dibujan encima del vídeo: redondea cada cámara con 4 copias diminutas por cuadro (casi gratis)."""
    out = []
    for corner in range(4):                                   # sup-izq, sup-der, inf-izq, inf-der
        pm = QPixmap(int(r * dpr), int(r * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cx = r if corner in (0, 2) else 0
        cy = r if corner in (0, 1) else 0
        outside = QPainterPath()
        outside.addRect(QRectF(0, 0, r, r))
        circle = QPainterPath()
        circle.addEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
        p.fillPath(outside.subtracted(circle), color)
        p.end()
        out.append(pm)
    return out


class _Signals(QObject):
    frame = pyqtSignal(object)
    status = pyqtSignal(str)


class VideoWidget(QWidget):
    def __init__(self, cam, app):
        super().__init__()
        self.cam, self.app = cam, app
        self.setMinimumSize(160, 90)
        self.setAcceptDrops(True)
        self._press = None
        self._drop = False
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        f = self.font()
        f.setPointSize(9)
        self._font = f                                  # se crea una vez, no en cada repintado
        self._label_key, self._label_pm = None, None
        self._img = None
        self._arr = None
        self._pending = False                 # ya hay un fotograma esperando a ser dibujado
        self._n, self._t0, self.fps_now = 0, time.monotonic(), 0.0
        self._status = "Conectando…"
        self._sig = _Signals()
        self._sig.frame.connect(self._on_frame)
        self._sig.status.connect(self._on_status)
        cfg = app.cfg
        self.worker = CameraWorker(cam, cfg["url_template"], cfg["hwaccel"], cfg["fps_grid"],
                                   self._emit_frame, self._sig.status.emit)
        self.worker.fit_mode = cfg.get("fit_mode", "stretch")
        self.worker.start()

    def _emit_frame(self, arr):
        """Hilo de la cámara. Si la UI aún no dibujó el anterior, lo descarta: nunca se acumula cola."""
        if self._pending:
            return
        self._pending = True
        self._sig.frame.emit(arr)

    # ---- señales (hilo de la UI) ----
    def _on_frame(self, arr):
        self._pending = False
        self._n += 1
        now = time.monotonic()
        if now - self._t0 >= 1.0:                          # FPS realmente mostrados
            self.fps_now, self._n, self._t0 = self._n / (now - self._t0), 0, now
        h, w, _ = arr.shape
        self._arr = arr  # mantiene vivo el buffer que usa QImage
        self._img = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        self._status = ""
        self.update()

    def _on_status(self, text):
        self._img, self._arr, self._status = None, None, text
        self._pending, self.fps_now = False, 0.0
        self.update()

    # ---- calidad ----
    def set_quality(self, quality, save=True):
        self.worker.set_quality(quality)
        self.cam["quality"] = quality
        if save:
            self.app.save()
        self.update()

    def set_resolution(self, height):
        """Tope de resolución mostrada. No reconecta: se aplica al siguiente fotograma."""
        self.worker.max_h = height
        if height:
            self.cam["res"] = height
        else:
            self.cam.pop("res", None)
        self.app.save()
        self.update()

    def shutdown(self):
        self.worker.stop()

    # ---- eventos ----
    # ---- arrastrar y soltar: intercambiar posición (sin recargar streams) ----
    MIME = "application/x-icsee-cam"

    def mousePressEvent(self, e):
        self._press = event_pos(e) if e.button() == Qt.MouseButton.LeftButton else None

    def mouseReleaseEvent(self, e):
        self._press = None

    def mouseMoveEvent(self, e):
        if (self._press is None or not (e.buttons() & Qt.MouseButton.LeftButton)
                or self.app.zoomed is not None or len(self.app.widgets) < 2):
            return
        if (event_pos(e) - self._press).manhattanLength() < QApplication.startDragDistance():
            return
        self._press = None
        mime = QMimeData()
        mime.setData(self.MIME, QByteArray(str(self.cam["id"]).encode()))
        drag = QDrag(self)
        drag.setMimeData(mime)
        if self._img is not None:                 # miniatura de un solo fotograma
            pm = QPixmap.fromImage(self._img).scaledToWidth(180)
        else:
            pm = QPixmap(180, 100)
            pm.fill(theme.qc(theme.TILE))
        drag.setPixmap(pm)
        drag.setHotSpot(pm.rect().center())
        qexec(drag, Qt.DropAction.MoveAction)

    def _drag_id(self, e):
        m = e.mimeData()
        return bytes(m.data(self.MIME)).decode() if m.hasFormat(self.MIME) else None

    def dragEnterEvent(self, e):
        src = self._drag_id(e)
        if src and src != self.cam["id"]:
            self._drop = True
            self.update()
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._drop = False
        self.update()

    def dropEvent(self, e):
        src = self._drag_id(e)
        self._drop = False
        self.update()
        if src and src != self.cam["id"]:
            e.acceptProposedAction()
            self.app.swap_cameras(src, self.cam["id"])

    def resizeEvent(self, e):
        dpr = self.devicePixelRatioF()
        self.worker.target = (max(16, int(self.width() * dpr)), max(16, int(self.height() * dpr)))

    def mouseDoubleClickEvent(self, e):
        self.app.toggle_zoom(self)

    def contextMenuEvent(self, e):
        qexec(self.build_menu(), e.globalPos())

    def build_menu(self):
        menu = QMenu(self)
        group = QActionGroup(menu)
        group.setExclusive(True)
        for q in QUALITIES:
            act = QAction(q, menu)
            act.setCheckable(True)
            act.setChecked(q == self.worker.quality)
            act.triggered.connect(lambda _=False, q=q: self.set_quality(q))
            group.addAction(act)
            menu.addAction(act)
        res_menu = menu.addMenu("Resolución")
        rgroup = QActionGroup(res_menu)
        rgroup.setExclusive(True)
        for h, label in RES_OPTIONS:
            ract = QAction(label, res_menu)
            ract.setCheckable(True)
            ract.setChecked(h == self.worker.max_h)
            ract.triggered.connect(lambda _=False, h=h: self.set_resolution(h))
            rgroup.addAction(ract)
            res_menu.addAction(ract)
        fit_menu = menu.addMenu("Ajuste de imagen")
        fgroup = QActionGroup(fit_menu)
        fgroup.setExclusive(True)
        for mode, label in FIT_OPTIONS:
            fact = QAction(label, fit_menu)
            fact.setCheckable(True)
            fact.setChecked(mode == self.app.cfg.get("fit_mode", "stretch"))
            fact.triggered.connect(lambda _=False, m=mode: self.app.set_fit_mode(m))
            fgroup.addAction(fact)
            fit_menu.addAction(fact)
        fps_menu = menu.addMenu("Fluidez (FPS)")
        ggroup = QActionGroup(fps_menu)
        ggroup.setExclusive(True)
        for value, label in FPS_OPTIONS:
            gact = QAction(label, fps_menu)
            gact.setCheckable(True)
            gact.setChecked(value == self.app.cfg.get("fps_grid", 12))
            gact.triggered.connect(lambda _=False, v=value: self.app.set_fps(v))
            ggroup.addAction(gact)
            fps_menu.addAction(gact)
        fps_menu.addSeparator()
        show = fps_menu.addAction("Mostrar FPS reales en la imagen")
        show.setCheckable(True)
        show.setChecked(bool(self.app.cfg.get("show_fps")))
        show.triggered.connect(lambda _=False: self.app.toggle_show_fps())
        menu.addAction("FPS de la cámara (máx. 15)…", lambda: self.app.camera_fps(self.cam))
        fill_perf_menu(menu.addMenu("Monitor de rendimiento"), self.app)
        menu.addSeparator()
        if self.app.cfg.get("layout_mode") in ("focus", "pano"):
            menu.addAction("Hacer principal (la grande)", lambda: self.app.make_primary(self.cam))
        menu.addAction("Salir de pantalla completa" if self.app.isFullScreen() else "Pantalla completa (F11)",
                       self.app.toggle_fullscreen)
        menu.addAction("Personalizar cuadrícula…", self.app.customize_layout)
        menu.addAction("Administrar cámaras…", self.app.manage_cameras)
        return menu

    _corners = {}                                  # (dpr) -> pixmaps de las 4 esquinas (compartidos)
    _TILE = None

    def _corner_set(self, dpr):
        cs = VideoWidget._corners.get(dpr)
        if cs is None:
            cs = VideoWidget._corners[dpr] = _corner_pixmaps(theme.TILE_RADIUS, dpr, theme.qc(theme.BG))
        return cs

    def _render_label(self, text, dpr):
        w, h = QFontMetrics(self._font).horizontalAdvance(text) + 20, 22
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qc(theme.BG, 190))
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        p.setFont(self._font)
        p.setPen(theme.qc(theme.TEXT))
        p.drawText(QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, text)
        p.end()
        return pm

    def paintEvent(self, ev):
        p = QPainter(self)
        img = self._img
        mode = self.app.cfg.get("fit_mode", "stretch")
        if VideoWidget._TILE is None:
            VideoWidget._TILE = theme.qc(theme.TILE)
        if img is None or mode == "fit":                       # con «estirar/llenar» la imagen cubre todo:
            p.fillRect(self.rect(), VideoWidget._TILE)         # no se pinta el fondo (menos trabajo por cuadro)
        if img is not None:
            iw, ih, W, H = img.width(), img.height(), self.width(), self.height()
            if mode == "fit":                                  # proporción original, con barras
                p.drawImage(QRect(*fit_rect(iw, ih, W, H)), img)
            elif mode == "fill":                               # llena el recuadro recortando los bordes
                p.drawImage(QRect(0, 0, W, H), img, QRect(*cover_crop(iw, ih, W, H)))
            else:                                              # estirar: sin barras negras
                p.drawImage(QRect(0, 0, W, H), img)
        if self._status:
            p.setFont(self._font)
            p.setPen(theme.qc(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status)
        dpr = self.devicePixelRatioF()
        show = bool(self.app.cfg.get("show_fps") and self.fps_now)
        key = (self.cam.get("name"), self.worker.quality, self.worker.max_h,
               round(self.fps_now) if show else None, round(self.worker.src_fps) if show else None, dpr)
        if key != self._label_key:                             # la etiqueta solo se redibuja si cambia
            label = f"{self.cam.get('name', 'Cámara')}  ·  {self.worker.quality}"
            if self.worker.max_h:
                label += f"  ·  {self.worker.max_h}p"
            if show:
                src = self.worker.src_fps
                label += f"  ·  {self.fps_now:.0f}" + (f"/{src:.0f}" if src else "") + " fps"
            self._label_key, self._label_pm = key, self._render_label(label, dpr)
        p.drawPixmap(8, 8, self._label_pm)
        W, H = self.width(), self.height()
        r = theme.TILE_RADIUS
        if W > 2 * r and H > 2 * r:                            # esquinas redondeadas
            tl, tr, bl, br = self._corner_set(dpr)
            p.drawPixmap(0, 0, tl)
            p.drawPixmap(W - r, 0, tr)
            p.drawPixmap(0, H - r, bl)
            p.drawPixmap(W - r, H - r, br)
        if self._drop:                            # resalta el destino mientras arrastras
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(QPen(theme.qc(theme.ACCENT), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5), r, r)


class _PerfSignals(QObject):
    sample = pyqtSignal(object)


class PerfBadge(QWidget):
    """Pastilla redondeada arriba al centro: CPU / RAM / GPU / tráfico de red (lo que elijas).
    Clic (izquierdo o derecho) = menú para elegir qué mostrar y en qué unidad.
    Pensada para gastar casi nada: no tiene temporizador propio (la muestra llega del hilo del monitor),
    solo se redibuja si cambia algún texto, y el dibujo se guarda en un pixmap, de modo que cada
    repintado (p. ej. cuando llega un cuadro de la cámara que está debajo) cuesta un único drawPixmap."""
    H, PAD, GAP = 26, 12, 1

    def __init__(self, parent, opacity=0.85, interval=2.0):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Clic: elegir qué mostrar (CPU, RAM, GPU, red) y la unidad")
        f = self.font()
        f.setPointSize(9)
        f.setBold(True)
        self.setFont(f)
        self._sig = _PerfSignals()
        self._sig.sample.connect(self._on_sample)
        self.mon = SysMonitor(on_sample=self._emit_sample, interval=interval)
        self._opacity = opacity
        self.items, self.unit, self.direction = list(PERF_DEFAULT_ITEMS), "auto_B", "both"
        self._vals, self._cells = (0.0, 0.0, None, None), None
        self._widths = {}                           # ancho mínimo de cada celda (no «baila» con los números)
        self._pm, self._pm_dpr = None, 0.0
        self.menu_provider = None                   # función que llena el menú (la pone la ventana)
        self._relayout()
        self.hide()

    def _emit_sample(self, vals):                     # se ejecuta en el hilo del monitor
        try:
            self._sig.sample.emit(vals)
        except RuntimeError:                          # la ventana ya se cerró
            pass

    def set_active(self, on):
        """Activo = visible y midiendo. Inactivo = oculto y sin hilo (coste cero)."""
        if on:
            self.mon.start()
            self.reposition()
            self.show()
            self.raise_()
        else:
            self.mon.stop()
            self.hide()

    def configure(self, items, unit, direction):
        self.items, self.unit, self.direction = list(items), unit, direction
        self.mon.want_gpu = "gpu" in self.items
        self._widths = {}
        self._cells = None
        self._on_sample(self._vals, force=True)

    def set_opacity(self, value):
        self._opacity = value
        self.update()

    def set_interval(self, seconds):
        self.mon.interval = seconds

    def reposition(self):
        p = self.parentWidget()
        if p is not None:
            self.move((p.width() - self.width()) // 2, 12)

    # ---- textos ----
    @staticmethod
    def _color(v):
        if v is None:
            return theme.qc(theme.MUTED)
        v = round(v)
        return QColor(120, 220, 150) if v < 60 else QColor(255, 200, 90) if v < 85 else QColor(255, 110, 110)

    def _net_text(self, net):
        if net is None:
            return "Red --"
        rx, tx = net
        parts = []
        if self.direction in ("both", "down"):
            parts.append("↓ " + fmt_rate(rx, self.unit))
        if self.direction in ("both", "up"):
            parts.append("↑ " + fmt_rate(tx, self.unit))
        return "   ".join(parts)

    def _make_cells(self, vals):
        cpu, ram, gpu, net = vals
        out = []
        for key in self.items:
            if key == "net":
                out.append(("net", self._net_text(net), QColor(140, 190, 255)))
            else:
                v = {"cpu": cpu, "ram": ram, "gpu": gpu}[key]
                t = "n/d" if v is None else f"{v:.0f}%"
                out.append((key, f"{key.upper()} {t}", self._color(v)))
        return tuple(out)

    def _template(self, key):
        if key != "net":
            return f"{key.upper()} 100%"
        sample = fmt_rate(999.0 * (1024 ** 2 if self.unit in ("auto_B", "KB", "MB") else 1e6 / 8), self.unit)
        one = "↓ " + sample.replace("9", "8")
        return one + "   " + one if self.direction == "both" else one

    def _relayout(self):
        fm = self.fontMetrics()
        total = 0
        for key in self.items:
            w = max(self._widths.get(key, 0), fm.horizontalAdvance(self._template(key)) + 2 * self.PAD)
            for k, t, _ in (self._cells or ()):
                if k == key:
                    w = max(w, fm.horizontalAdvance(t) + 2 * self.PAD)
            self._widths[key] = w
            total += w
        total += 8 + self.GAP * max(0, len(self.items) - 1)
        if total != self.width() or self.height() != self.H:
            self.setFixedSize(total, self.H)
            self.reposition()

    def _on_sample(self, vals, force=False):
        self._vals = vals
        cells = self._make_cells(vals)
        if cells == self._cells and not force:       # nada cambió: no se redibuja
            return
        self._cells = cells
        self._relayout()
        self._pm = None                               # se regenera una sola vez al pintar
        self.update()

    def _render(self, dpr):
        w, h = self.width(), self.height()
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(theme.qc(theme.BORDER), 1))
        p.setBrush(theme.qc(theme.PANEL))           # la transparencia la da la opacidad elegida
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), h / 2, h / 2)    # bordes totalmente redondeados
        p.setFont(self.font())
        x = 4
        cells = self._cells or self._make_cells(self._vals)
        for n, (key, text, color) in enumerate(cells):
            cw = self._widths.get(key, 60)
            if n:
                p.setPen(QPen(theme.qc(theme.BORDER), 1))
                p.drawLine(x, 7, x, h - 7)
                x += self.GAP
            p.setPen(color)
            p.drawText(QRect(x, 0, cw, h), Qt.AlignmentFlag.AlignCenter, text)
            x += cw
        p.end()
        self._pm, self._pm_dpr = pm, dpr

    def paintEvent(self, ev):
        dpr = self.devicePixelRatioF()
        if self._pm is None or dpr != self._pm_dpr:
            self._render(dpr)
        p = QPainter(self)
        p.setOpacity(self._opacity)                   # opacidad de toda la pastilla
        p.drawPixmap(0, 0, self._pm)

    # ---- menú ----
    def build_menu(self):
        menu = QMenu(self)
        if self.menu_provider:
            self.menu_provider(menu)
        return menu

    def _popup(self):
        menu = self.build_menu()
        if menu.actions():
            qexec(menu, self.mapToGlobal(self.rect().bottomLeft()) + QPoint(0, 6))

    def mousePressEvent(self, e):
        if e.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            e.accept()
            self._popup()

    def mouseDoubleClickEvent(self, e):           # que no maximice la cámara de abajo
        e.accept()

    def contextMenuEvent(self, e):
        e.accept()


class CamerasDialog(QDialog):
    FIELDS = [("name", "Nombre"), ("ip", "IP"), ("port", "Puerto"), ("user", "Usuario"),
              ("password", "Contraseña"), ("channel", "Canal"),
              ("url_sub", "URL Transmisión (opcional)"), ("url_main", "URL Convencional (opcional)"),
              ("ctrl_user", "Usuario iCSee (para FPS)"), ("ctrl_password", "Contraseña iCSee (para FPS)")]

    def __init__(self, cams, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cámaras")
        self.cams = [dict(c) for c in cams]
        self._loading = False

        self.list = QListWidget()
        add, rem = QPushButton("Agregar"), QPushButton("Quitar")
        add.clicked.connect(self._add)
        rem.clicked.connect(self._remove)
        left = QVBoxLayout()
        left.addWidget(self.list)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(rem)
        left.addLayout(row)

        form = QFormLayout()
        self.edits = {}
        for key, label in self.FIELDS:
            ed = QLineEdit()
            if key in ("password", "ctrl_password"):
                ed.setEchoMode(QLineEdit.EchoMode.Password)
            ed.textEdited.connect(lambda t, k=key: self._commit(k, t))
            self.edits[key] = ed
            form.addRow(label, ed)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        right = QVBoxLayout()
        right.addLayout(form)
        right.addWidget(buttons)

        root = QHBoxLayout(self)
        root.addLayout(left, 1)
        root.addLayout(right, 2)
        self.list.currentRowChanged.connect(self._load)
        self._refresh()
        self.resize(720, 360)

    def _refresh(self, select=0):
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems([c.get("name", "Cámara") for c in self.cams])
        self.list.blockSignals(False)
        if self.cams:
            self.list.setCurrentRow(min(select, len(self.cams) - 1))
        self._load(self.list.currentRow())

    def _load(self, row):
        self._loading = True
        for key, _ in self.FIELDS:
            self.edits[key].setEnabled(row >= 0)
            self.edits[key].setText(str(self.cams[row].get(key, "")) if row >= 0 else "")
        self._loading = False

    def _commit(self, key, text):
        row = self.list.currentRow()
        if self._loading or row < 0:
            return
        if key in ("port", "channel"):
            text = int(text) if text.isdigit() else text
        self.cams[row][key] = text
        if key == "name":
            self.list.item(row).setText(text)

    def _add(self):
        n = len(self.cams) + 1
        self.cams.append({"id": uuid.uuid4().hex[:8], "name": f"Cámara {n}", "ip": "192.168.1.10", "port": 554,
                          "user": "admin", "password": "", "channel": 1, "quality": QUALITY_SUB})
        self._refresh(len(self.cams) - 1)

    def _remove(self):
        row = self.list.currentRow()
        if row >= 0:
            del self.cams[row]
            self._refresh(row)

    def result_cams(self):
        for c in self.cams:                      # limpia vacíos y tipos
            c.setdefault("id", uuid.uuid4().hex[:8])
            for k in ("url_sub", "url_main", "ctrl_user", "ctrl_password"):
                if not c.get(k):
                    c.pop(k, None)
            for k, d in (("port", 554), ("channel", 1)):
                if not isinstance(c.get(k), int):
                    c[k] = d
        return self.cams
