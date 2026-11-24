"""Lectura RTSP + decodificación con PyAV (FFmpeg incluido). Sin dependencias externas."""
import collections
import errno
import threading
import time

import av
import numpy as np

try:
    from av.codec.hwaccel import HWAccel, hwdevices_available
except Exception:  # versión de PyAV sin soporte hwaccel
    HWAccel = None

_FFERR = getattr(av.error, "FFmpegError", Exception)
OPEN_TIMEOUT, READ_TIMEOUT = 20.0, 20.0   # por Wi-Fi débil la cámara se traba hasta 8 s: con 8 s de margen reconectaba sin parar


def _reason(err):
    """Motivo corto del corte para mostrar en pantalla (sin el texto del error: trae la URL con la contraseña)."""
    if err is None:
        return "se cortó"
    if isinstance(err, getattr(av.error, "HTTPUnauthorizedError", ())) or "401" in str(err):
        return "usuario o contraseña incorrectos"
    if isinstance(err, (getattr(av.error, "ExitError", ()), TimeoutError)) or "timed out" in str(err).lower():
        return "la cámara no responde"
    if isinstance(err, ConnectionRefusedError):
        return "conexión rechazada"
    code = getattr(err, "errno", None)
    if code in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
        return "esta PC no tiene red (revisá el Wi-Fi)"
    if code:
        return f"error de red {code}"
    return type(err).__name__

QUALITY_SUB = "Transmisión"    # substream: fluido, baja resolución
QUALITY_MAIN = "Convencional"  # mainstream: alta definición
QUALITIES = (QUALITY_SUB, QUALITY_MAIN)


def build_url(cam: dict, quality: str, template: str) -> str:
    """iCSee/XM: stream=0 principal, stream=1 secundario."""
    sub = quality == QUALITY_SUB
    custom = cam.get("url_sub" if sub else "url_main")
    if custom:
        return custom
    return template.format(
        ip=cam.get("ip", ""), port=cam.get("port", 554),
        user=cam.get("user", "admin"), password=cam.get("password", ""),
        channel=cam.get("channel", 1), stream=1 if sub else 0,
    )


def fit_size(fw, fh, tw, th, max_h=0):
    """Tamaño de salida: cabe en el widget, nunca amplía y respeta el tope de resolución."""
    s = min(tw / fw, th / fh, 1.0)
    if max_h:
        s = min(s, max_h / fh)
    return max(16, int(fw * s)), max(16, int(fh * s))


class Pacer:
    """Decide qué fotogramas mostrar para no superar `fps`. Sigue un calendario fijo sobre la marca de tiempo
    de la cámara (no la hora de llegada): así las ráfagas del WiFi no descartan cuadros de una cámara que
    envía justo a `fps`."""

    def __init__(self):
        self.next_t = 0.0

    def accept(self, now, fps):
        interval = 1.0 / fps if fps else 0.0              # fps = 0 -> mostrar todos
        if now < self.next_t - 1.0:                       # la marca de tiempo volvió atrás: recalibra
            self.next_t = now
        if now < self.next_t - interval * 0.1:
            return False
        self.next_t = self.next_t + interval if now - self.next_t < interval else now + interval
        return True


class Steady:
    """Muestra los cuadros al ritmo de la cámara (sus marcas de tiempo), pegado al vivo.
    «Vivo» = el menor (llegada − marca) de los últimos WINDOW segundos: el cuadro que llegó más rápido.
    Cada cuadro se muestra DELAY s después de su momento en vivo (colchón contra las ráfagas del WiFi).
    Si un cuadro llega más de BEHIND s tarde (la PC no da abasto o se juntó atraso al conectar), se decodifica
    pero no se dibuja, hasta alcanzar el vivo: el atraso nunca se acumula."""
    DELAY = 0.15
    BEHIND = 0.3
    WINDOW = 20.0
    MAX_GAP = 1.0                     # aunque esté atrasado, muestra al menos un cuadro por segundo

    def __init__(self):
        self.offs = collections.deque()       # (llegada, llegada - marca), crecientes: el primero es el vivo
        self.last_shown = 0.0

    def plan(self, ts, now):
        """Hora (monotonic) en que toca mostrar el cuadro con marca `ts` que llegó en `now`, o None = saltearlo."""
        off = now - ts
        if self.offs and abs(off - self.offs[0][1]) > 5.0:      # salto de marcas de tiempo: empieza de nuevo
            self.offs.clear()
        while self.offs and self.offs[-1][1] >= off:
            self.offs.pop()
        self.offs.append((now, off))
        while self.offs[0][0] < now - self.WINDOW:
            self.offs.popleft()
        live = self.offs[0][1]
        if off - live > self.DELAY + self.BEHIND and now - self.last_shown < self.MAX_GAP:
            return None
        self.last_shown = now
        return ts + live + self.DELAY


class Presenter(threading.Thread):
    """Entrega a la UI cada cuadro a su hora. Corre aparte para que la lectura del stream nunca espere
    (si esperara, el atraso se juntaría en la red). Si hay varios cuadros vencidos, muestra solo el último."""

    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.queue = collections.deque()
        self.cond = threading.Condition()
        self.stopped = False

    def put(self, due, arr):
        with self.cond:
            self.queue.append((due, arr))
            if len(self.queue) > 16:                       # nunca acumula
                self.queue.popleft()
            self.cond.notify()

    def stop(self):
        with self.cond:
            self.stopped = True
            self.queue.clear()
            self.cond.notify()

    def run(self):
        while True:
            with self.cond:
                while not self.queue and not self.stopped:
                    self.cond.wait()
                if self.stopped:
                    return
                due, arr = self.queue[0]
                now = time.monotonic()
                if due > now:
                    self.cond.wait(due - now)              # despierta antes si llega otro cuadro o se corta
                    continue
                self.queue.popleft()
                if self.queue and self.queue[0][0] <= now:
                    continue                               # ya vence otro más nuevo: este se saltea
            self.emit(arr)


def out_size(mode, fw, fh, tw, th, max_h=0):
    """Tamaño de la imagen que se entrega a la UI según el modo de ajuste."""
    if mode == "fit":
        return fit_size(fw, fh, tw, th, max_h)
    if mode == "fill":                                   # cubre todo el recuadro (se recortará al dibujar)
        s = min(max(tw / fw, th / fh), 1.0)
        w, h = fw * s, fh * s
    else:                                                # stretch: exactamente el recuadro (sin barras)
        w, h = min(tw, fw), min(th, fh)
    if max_h and h > max_h:
        k = max_h / h
        w, h = w * k, h * k
    return max(16, int(w)), max(16, int(h))


def cover_crop(iw, ih, W, H):
    """Región (x, y, ancho, alto) de la imagen que llena un recuadro WxH sin deformar."""
    s = max(W / iw, H / ih)
    sw, sh = min(iw, W / s), min(ih, H / s)
    return int((iw - sw) / 2), int((ih - sh) / 2), int(sw), int(sh)


def fit_rect(iw, ih, W, H):
    """Rectángulo (x, y, ancho, alto) centrado que conserva la proporción (con barras)."""
    s = min(W / iw, H / ih)
    w, h = int(iw * s), int(ih * s)
    return (W - w) // 2, (H - h) // 2, w, h


def _pick_hw():
    if HWAccel is None:
        return None
    try:
        available = set(hwdevices_available())
    except Exception:
        return None
    for name in ("d3d11va", "dxva2", "videotoolbox", "vaapi", "cuda"):
        if name in available:
            return name
    return None


class CameraWorker(threading.Thread):
    """Un hilo por cámara. Cambiar calidad = reabrir el stream dentro del hilo,
    así la interfaz nunca se bloquea."""

    def __init__(self, cam, template, use_hw, fps, on_frame, on_status):
        super().__init__(daemon=True)
        self.cam, self.template = cam, template
        self.use_hw = use_hw
        self.fps = fps
        self.target = (640, 360)          # tamaño del widget (px); lo actualiza la UI
        self.src_fps = 0.0                       # fps que envía la cámara (lo informa el stream)
        self.fit_mode = "stretch"                 # lo fija la UI (stretch | fill | fit)
        self.max_h = int(cam.get("res", 0) or 0)   # tope de resolución mostrada (0 = automática)
        self.on_frame, self.on_status = on_frame, on_status
        q = cam.get("quality", QUALITY_SUB)
        self.quality = q if q in QUALITIES else QUALITY_SUB
        self._stop = threading.Event()
        self._reload = threading.Event()
        self._paused = False

    # ---- control desde la UI (no bloqueante) ----
    def set_quality(self, quality):
        self.quality = quality
        self._reload.set()

    def set_paused(self, paused):
        self._paused = paused
        self._reload.set()

    def stop(self):
        self._stop.set()
        self._reload.set()

    # ---- hilo ----
    def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            if self._paused:
                self._stop.wait(0.2)
                continue
            self._reload.clear()
            got, err = False, None
            try:
                got = self._session(build_url(self.cam, self.quality, self.template))
            except Exception as e:
                err = e
            if self._stop.is_set():
                break
            if self._reload.is_set() or self._paused:
                backoff = 1.0
                continue
            self.on_status(f"Sin señal · {_reason(err)} · reconectando…")
            backoff = 1.0 if got else min(backoff * 2, 15.0)
            self._reload.wait(backoff)

    def _session(self, url) -> bool:
        sub = self.quality == QUALITY_SUB
        self.on_status("Conectando…")
        options = {"rtsp_transport": "tcp", "fflags": "nobuffer+discardcorrupt",
                   "flags": "low_delay", "max_delay": "500000",
                   "analyzeduration": "2000000"}          # analiza 2 s (no 5): abre antes
        kwargs = {}
        hw_name = _pick_hw() if self.use_hw else None
        if hw_name:
            kwargs["hwaccel"] = HWAccel(device_type=hw_name, allow_software_fallback=True)
        try:
            container = av.open(url, options=options, timeout=(OPEN_TIMEOUT, READ_TIMEOUT), **kwargs)
        except TypeError:                       # PyAV sin parámetro hwaccel
            container = av.open(url, options=options, timeout=(OPEN_TIMEOUT, READ_TIMEOUT))
        got = False
        wait_key = 0.0                           # tras un error se espera un cuadro clave (sin manchas grises),
                                                 # como mucho 4 s por si la cámara no marca los cuadros clave
        pacer, steady = Pacer(), Steady()
        presenter = Presenter(self.on_frame)
        presenter.start()
        try:
            vs = container.streams.video[0]
            vs.codec_context.thread_count = 2 if sub else 0
            try:
                rate = vs.average_rate or vs.guessed_rate
                self.src_fps = float(rate) if rate else 0.0
            except Exception:
                self.src_fps = 0.0
            for packet in container.demux(vs):
                if self._stop.is_set() or self._reload.is_set():
                    break
                try:
                    frames = packet.decode()
                except _FFERR:
                    if hw_name and not got:      # la GPU no pudo: reintenta en CPU
                        self.use_hw = False
                        return False
                    wait_key = wait_key or time.monotonic() + 4.0
                    continue
                for frame in frames:
                    if getattr(frame, "is_corrupt", False):
                        wait_key = wait_key or time.monotonic() + 4.0
                        continue
                    if wait_key:
                        if not frame.key_frame and time.monotonic() < wait_key:
                            continue
                        wait_key = 0.0
                    ts = frame.time              # segundos según la cámara (None si el stream no los trae)
                    if not pacer.accept(time.monotonic() if ts is None else ts, self.fps):
                        continue                 # descarta sin convertir = ahorra CPU
                    due = None if ts is None else steady.plan(ts, time.monotonic())
                    if ts is not None and due is None:
                        continue                 # atrasado: no se dibuja, así alcanza el vivo
                    w, h = out_size(self.fit_mode, frame.width, frame.height, *self.target, self.max_h)
                    arr = frame.to_ndarray(width=w, height=h, format="rgb24")
                    if not arr.flags["C_CONTIGUOUS"]:
                        arr = np.ascontiguousarray(arr)
                    if due is None:
                        self.on_frame(arr)
                    else:
                        presenter.put(due, arr)  # lo muestra a su hora; la lectura sigue sin esperar
                    got = True
        finally:
            presenter.stop()
            container.close()
        return got
