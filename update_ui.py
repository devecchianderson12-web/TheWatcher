"""Cartel de actualización (abajo a la derecha, bordes redondeados) y su lógica.

- Busca una versión nueva al rato de abrir y después cada 3 horas, en segundo plano.
- Solo aparece si en el Release hay una versión más nueva de verdad.
- «×» lo cierra; vuelve a aparecer en el siguiente chequeo si sigue habiendo versión nueva.
- Nunca queda trabado: la descarga corta sola si no recibe datos, cada paso tiene tiempo límite
  y ante cualquier error muestra «Reintentar» (y se oculta solo al rato)."""
import threading
import time

from qt import (pyqtSignal, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QObject, QPainter,
                QPainterPath, QPropertyAnimation, QPushButton, QRectF, Qt, QTimer, QVBoxLayout, QWidget)

import theme
import updater

FIRST_CHECK_MS = 20_000              # primer chequeo, 20 s después de abrir
CHECK_EVERY_MS = 3 * 60 * 60_000     # después, cada 3 horas
ERROR_HIDE_MS = 60_000               # un error se oculta solo al minuto
DEB_WAIT_S = 4 * 60                  # el sistema tiene 4 min para empezar (corre cada minuto)
DEB_INSTALL_S = 12 * 60              # y 12 min para terminar

STYLE = f"""
QLabel#title {{ color: #ffffff; font-weight: bold; background: transparent; }}
QLabel#text {{ color: {theme.MUTED}; background: transparent; }}
QPushButton#go {{ background: {theme.ACCENT}; color: white; border: none; border-radius: 10px; padding: 6px 16px;
                 font-weight: bold; }}
QPushButton#go:hover {{ background: {theme.ACCENT_HOVER}; }}
QPushButton#go:disabled {{ background: {theme.ACCENT_SOFT}; color: #b9cdee; }}
QPushButton#close {{ background: transparent; color: {theme.MUTED}; border: none; border-radius: 10px;
                    font-size: 15px; padding: 0 6px; }}
QPushButton#close:hover {{ background: {theme.HOVER}; color: white; }}
"""


class _Bridge(QObject):
    found = pyqtSignal(object)             # info de la versión nueva (o None)
    progress = pyqtSignal(int, int)
    downloaded = pyqtSignal(str)
    failed = pyqtSignal(str)


class UpdateToast(QWidget):
    """Cartel flotante dentro de la ventana principal."""
    W, RADIUS = 330, 14

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(STYLE)
        self.setFixedWidth(self.W)
        self.title = QLabel("Hay una actualización")
        self.title.setObjectName("title")
        self.text = QLabel("")
        self.text.setObjectName("text")
        self.text.setWordWrap(True)
        self.go = QPushButton("Actualizar")
        self.go.setObjectName("go")
        self.go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("close")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setToolTip("Cerrar (te lo vuelvo a avisar más tarde)")
        self._pct = -1                                   # barra de progreso (-1 = sin barra)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.title, 1)
        top.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignTop)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 4, 0, 0)
        bottom.addStretch(1)
        bottom.addWidget(self.go)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 12, 14)
        lay.setSpacing(4)
        lay.addLayout(top)
        lay.addWidget(self.text)
        lay.addLayout(bottom)

        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(0.0)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b"opacity", self)
        self._anim.setDuration(220)
        self._anim.finished.connect(self._anim_done)
        self._hiding = False
        self.hide()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, self.RADIUS, self.RADIUS)
        p.fillPath(path, theme.qc(theme.PANEL, 248))
        p.setPen(theme.qc(theme.BORDER))
        p.drawPath(path)
        if self._pct >= 0:                               # barra fina en el borde inferior
            bar = QPainterPath()
            w = (self.width() - 2 * self.RADIUS) * self._pct / 100.0
            bar.addRoundedRect(QRectF(self.RADIUS, self.height() - 7, max(w, 4), 3), 1.5, 1.5)
            p.fillPath(bar, theme.qc(theme.ACCENT))

    def set_progress(self, pct):
        self._pct = pct
        self.update()

    def reposition(self):
        par = self.parentWidget()
        if par is None:
            return
        self.adjustSize()
        bar = getattr(par, "toolbar", None)
        bottom = bar.y() if bar is not None and bar.isVisible() else par.height()
        self.move(par.width() - self.width() - 16, bottom - self.height() - 16)

    def pop(self):
        self._hiding = False
        self.reposition()
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()

    def fade(self):
        if not self.isVisible():
            return
        self._hiding = True
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _anim_done(self):
        if self._hiding:
            self.hide()


class Updater(QObject):
    """Chequeo periódico + descarga + instalación. Una sola operación a la vez."""

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.toast = UpdateToast(window)
        self.toast.go.clicked.connect(self._on_go)
        self.toast.close_btn.clicked.connect(self._on_close)
        self.b = _Bridge()
        self.b.found.connect(self._on_found)
        self.b.progress.connect(self._on_progress)
        self.b.downloaded.connect(self._on_downloaded)
        self.b.failed.connect(self._on_failed)
        self.info = None
        self.busy = False                     # chequeando, descargando o instalando
        self.working = False                  # descargando o instalando (el cartel no se puede cerrar)
        self._cancel = False
        self._deb_since = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_now)
        self.err_timer = QTimer(self)
        self.err_timer.setSingleShot(True)
        self.err_timer.timeout.connect(self._hide_error)
        self.deb_timer = QTimer(self)
        self.deb_timer.timeout.connect(self._poll_deb)
        self._failed_state = False

    def start(self):
        if updater.kind() is None:            # desde el código o instalación sin permisos: no hace nada
            return
        updater.cleanup()                     # restos de una actualización anterior
        QTimer.singleShot(FIRST_CHECK_MS, self.check_now)
        self.timer.start(CHECK_EVERY_MS)

    def reposition(self):
        if self.toast.isVisible():
            self.toast.reposition()

    def shutdown(self):
        self._cancel = True

    # ---- chequeo ----
    def check_now(self):
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                info = updater.check()
            except Exception:
                info = None                   # sin Internet o GitHub caído: se reintenta en el próximo chequeo
            try:
                self.b.found.emit(info)
            except RuntimeError:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _on_found(self, info):
        self.busy = False
        if not info:
            if not self.working and not self._failed_state:
                self.toast.fade()             # ya no hay nada nuevo (p. ej. se actualizó por otro lado)
            return
        if self.working:
            return
        self.info = info
        self._show_offer()

    def _show_offer(self):
        self._failed_state = False
        self.err_timer.stop()
        self.toast.title.setText("Hay una actualización")
        self.toast.text.setText(f"The Watcher {self.info['version']} está disponible "
                                f"(tenés la {updater.VERSION}).")
        self.toast.go.setText("Actualizar")
        self.toast.go.setEnabled(True)
        self.toast.close_btn.show()
        self.toast.set_progress(-1)
        self.toast.pop()

    def _on_close(self):
        if self.working:
            return
        self.err_timer.stop()
        self._failed_state = False
        self.toast.fade()

    # ---- actualizar ----
    def _on_go(self):
        if self.working or not self.info:
            return
        self.working = self.busy = True
        self._cancel = False
        self._failed_state = False
        self.err_timer.stop()
        self.toast.close_btn.hide()
        self.toast.go.setEnabled(False)
        self.toast.title.setText(f"Actualizando a la {self.info['version']}")
        if updater.kind() == "deb":
            self._start_deb()
            return
        self.toast.go.setText("Descargando…")
        self.toast.text.setText("Descargando la versión nueva…")
        self.toast.set_progress(0)
        self.toast.reposition()
        info = dict(self.info)

        def run():
            try:
                path = updater.download(info, lambda d, t: self.b.progress.emit(d, t), lambda: self._cancel)
                self.b.downloaded.emit(path)
            except Exception as e:
                try:
                    self.b.failed.emit(str(e) if isinstance(e, updater.UpdateError) else type(e).__name__)
                except RuntimeError:
                    pass
        threading.Thread(target=run, daemon=True).start()

    def _on_progress(self, done, total):
        if total:
            pct = int(done * 100 / total)
            self.toast.set_progress(pct)
            self.toast.text.setText(f"Descargando… {pct} %  ({done / 1048576:.0f} de {total / 1048576:.0f} MB)")
        else:
            self.toast.text.setText(f"Descargando… {done / 1048576:.0f} MB")

    def _on_downloaded(self, path):
        self.toast.go.setText("Instalando…")
        self.toast.text.setText("Instalando. The Watcher se va a cerrar y abrir solo en unos segundos.")
        self.toast.set_progress(100)
        try:
            updater.install(path)
        except Exception as e:
            updater.cleanup()
            self._on_failed(str(e) if isinstance(e, updater.UpdateError) else type(e).__name__)
            return
        QTimer.singleShot(600, self.win.close)           # el ayudante espera a que cierre y reemplaza

    # ---- Linux .deb: instala el sistema ----
    def _start_deb(self):
        try:
            self._deb_since = updater.request_system_update()
        except Exception as e:
            self._on_failed(str(e))
            return
        self.toast.go.setText("Esperando…")
        self.toast.text.setText("Le pedí al sistema que la instale (empieza en menos de 1 minuto).")
        self.toast.set_progress(-1)
        self.toast.reposition()
        self.deb_timer.start(2000)

    def _poll_deb(self):
        state, detail = updater.system_update_state(self._deb_since)
        waited = time.time() - self._deb_since
        if state == "ok":
            self.deb_timer.stop()
            self.toast.go.setText("Listo")
            self.toast.text.setText("Actualizado. The Watcher se vuelve a abrir…")
            try:
                updater.restart()
            except Exception:
                pass
            QTimer.singleShot(800, self.win.close)
        elif state == "error":
            self.deb_timer.stop()
            self._on_failed(detail or "el sistema no pudo instalarla")
        elif state == "descargando":
            self.toast.go.setText("Descargando…")
            self.toast.text.setText("El sistema está descargando la versión nueva…")
            if waited > DEB_INSTALL_S:
                self.deb_timer.stop()
                self._on_failed("tardó demasiado")
        elif state == "instalando":
            self.toast.go.setText("Instalando…")
            self.toast.text.setText("Instalando. The Watcher se va a reiniciar solo.")
            if waited > DEB_INSTALL_S:
                self.deb_timer.stop()
                self._on_failed("tardó demasiado")
        elif waited > DEB_WAIT_S:                        # nadie tomó el pedido
            self.deb_timer.stop()
            self._on_failed("el sistema no respondió (reinstalá el .deb una vez a mano)")

    # ---- errores: nunca queda trabado ----
    def _on_failed(self, reason):
        self.working = self.busy = False
        self._failed_state = True
        self.deb_timer.stop()
        self.toast.title.setText("No se pudo actualizar")
        self.toast.text.setText(f"Motivo: {reason}.")
        self.toast.go.setText("Reintentar")
        self.toast.go.setEnabled(True)
        self.toast.close_btn.show()
        self.toast.set_progress(-1)
        self.toast.pop()
        self.err_timer.start(ERROR_HIDE_MS)

    def _hide_error(self):
        if not self.working:
            self._failed_state = False
            self.toast.fade()
