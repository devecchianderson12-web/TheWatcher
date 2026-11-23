"""Diálogo «FPS de la cámara»: cambia los cuadros que la cámara codifica (no solo los mostrados),
hasta el máximo que admite cada una (25 en PAL, 30 en NTSC)."""
import threading

from qt import (pyqtSignal, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout,
               QLabel, QLineEdit, QObject, QPushButton, QSlider, QSpinBox, Qt, QVBoxLayout, QWidget)

import camctl


class _Signals(QObject):
    done = pyqtSignal(object, object)       # (resultado, error)


class CameraFpsDialog(QDialog):
    def __init__(self, cam, parent=None):
        super().__init__(parent)
        self.cam = cam
        self.setWindowTitle(f"FPS de la cámara · {cam.get('name', 'Cámara')}")
        self._sig = _Signals()
        self._sig.done.connect(self._on_done)
        self._rows = {}                       # key de stream -> QSpinBox
        self._busy = False

        self.user = QLineEdit(cam.get("ctrl_user") or cam.get("user", ""))
        self.password = QLineEdit(cam.get("ctrl_password") or "")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(cam.get("ctrl_port", camctl.PORT)))
        form = QFormLayout()
        form.addRow("IP", QLabel(str(cam.get("ip", ""))))
        form.addRow("Usuario (iCSee)", self.user)
        form.addRow("Contraseña (iCSee)", self.password)
        form.addRow("Puerto", self.port)

        self.read_btn = QPushButton("Leer de la cámara")
        self.read_btn.clicked.connect(self._read)
        self.streams = QWidget()
        self.grid = QGridLayout(self.streams)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.steady = QCheckBox("FPS fijos también con poca luz (apaga el modo poca luz; de noche se ve más oscuro)")
        self.wifi = QCheckBox("HD optimizado para WiFi (1536 kbps, keyframe cada 8 s: menos tirones)")
        for box in (self.steady, self.wifi):
            box.setEnabled(False)
        self.status = QLabel("Ingresá el usuario y la contraseña de la cámara y tocá «Leer de la cámara».")
        self.status.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply
                                        | QDialogButtonBox.StandardButton.Close)
        self.apply_btn = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_btn.setText("Aplicar a la cámara")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        self.buttons.rejected.connect(self.reject)

        top = QHBoxLayout()
        top.addLayout(form, 1)
        top.addWidget(self.read_btn, 0, Qt.AlignmentFlag.AlignBottom)
        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self.streams)
        root.addWidget(self.steady)
        root.addWidget(self.wifi)
        root.addWidget(self.status)
        root.addWidget(self.buttons)
        self.resize(560, 340)
        if self.password.text():               # ya hay datos guardados: lee solo
            self._read()

    # ---- trabajo en segundo plano (la red no congela la ventana) ----
    def _creds(self):
        return (str(self.cam.get("ip", "")), self.user.text().strip(), self.password.text(), self.port.value())

    def _run(self, text, fn, *args):
        if self._busy:
            return
        self._busy = True
        self.read_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.status.setText(text)

        def work():
            try:
                self._sig.done.emit(fn(*args), None)
            except camctl.CamError as e:
                self._sig.done.emit(None, str(e))
            except Exception as e:                       # nada debe tumbar la ventana
                self._sig.done.emit(None, f"Error inesperado: {e}")
        threading.Thread(target=work, daemon=True).start()

    def _read(self):
        ip, user, pw, port = self._creds()
        self._run("Leyendo la cámara…", camctl.read_fps, ip, user, pw, port)

    def _apply(self):
        ip, user, pw, port = self._creds()
        wanted = {key: spin.value() for key, spin in self._rows.items()}
        low_lux = not self.steady.isChecked() if self.steady.isEnabled() else None
        self._run("Guardando en la cámara…", camctl.write_fps, ip, user, pw, wanted, port,
                  low_lux, self.wifi.isChecked())

    def _on_done(self, result, error):
        self._busy = False
        self.read_btn.setEnabled(True)
        if error:
            self.status.setText(f"⚠ {error}")
            self.apply_btn.setEnabled(bool(self._rows))
            return
        self._remember()
        asked = {key: spin.value() for key, spin in self._rows.items()}
        self._show(result)
        clipped = [s for s in result["streams"] if s["key"] in asked and s["fps"] < asked[s["key"]]]
        msg = f"Máximo de esta cámara: {result['max']} fps."
        if asked:
            msg = "✔ Guardado en la cámara. " + msg
            if clipped:
                msg += " La cámara recortó: " + ", ".join(f"{s['label']} a {s['fps']} fps" for s in clipped)
                msg += " (su tope a esa resolución)."
        self.status.setText(msg)

    def _remember(self):
        """Guarda usuario/contraseña de control en la cámara (config.json) si funcionaron."""
        ip, user, pw, port = self._creds()
        self.cam["ctrl_user"], self.cam["ctrl_password"] = user, pw
        if port != camctl.PORT:
            self.cam["ctrl_port"] = port
        else:
            self.cam.pop("ctrl_port", None)
        if hasattr(self.parent(), "save"):
            self.parent().save()

    def _show(self, result):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows = {}
        top = result["max"]
        seen = set()
        r = 0
        for s in result["streams"]:
            if s["key"] in seen:                 # un ajuste por stream (todas las cámaras de 1 canal)
                continue
            seen.add(s["key"])
            spin = QSpinBox()
            spin.setRange(1, top)
            spin.setSuffix(" fps")
            spin.setValue(min(s["fps"], top))
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(1, top)
            slider.setValue(spin.value())
            slider.valueChanged.connect(spin.setValue)
            spin.valueChanged.connect(slider.setValue)
            self.grid.addWidget(QLabel(f"<b>{s['label']}</b> ({s['resolution']})"), r, 0)
            self.grid.addWidget(slider, r, 1)
            self.grid.addWidget(spin, r, 2)
            self._rows[s["key"]] = spin
            r += 1
        self.grid.setColumnStretch(1, 1)
        low_lux = result.get("low_lux")
        self.steady.setEnabled(low_lux is not None)     # algunas cámaras no tienen modo poca luz
        self.steady.setChecked(low_lux is False)
        self.wifi.setEnabled(True)
        self.wifi.setChecked(bool(result.get("wifi")))
        self.apply_btn.setEnabled(bool(self._rows))
