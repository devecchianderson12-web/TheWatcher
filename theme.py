"""Tema oscuro azul grisáceo (no negro) con bordes redondeados en toda la interfaz.
Un solo lugar para los colores: el resto del programa los toma de acá."""
import sys

from qt import QApplication, QColor, QEvent, QMenu, QObject, QPainterPath, QPalette, QRectF, QRegion, Qt

BG = "#1c2230"          # fondo de ventanas
PANEL = "#242b3a"       # barras y paneles
RAISED = "#2d3546"      # botones y campos
HOVER = "#37415a"
BORDER = "#3c475c"
TEXT = "#e3e8f1"
MUTED = "#98a3b7"
ACCENT = "#4c8dff"
ACCENT_HOVER = "#66a0ff"
ACCENT_SOFT = "#2c4a7a"
TILE = "#151a24"        # fondo de una cámara sin imagen

RADIUS = 10             # radio general
TILE_RADIUS = 10        # radio de los recuadros de las cámaras
MENU_RADIUS = 10


def qc(hex_color, alpha=255):
    c = QColor(hex_color)
    c.setAlpha(alpha)
    return c


STYLE = f"""
QWidget {{ color: {TEXT}; }}
QDialog, QMessageBox {{ background: {BG}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 8px; padding: 5px 8px; }}

QPushButton {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: {RADIUS}px;
               padding: 6px 14px; }}
QPushButton:hover {{ background: {HOVER}; }}
QPushButton:pressed {{ background: {ACCENT_SOFT}; }}
QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT_HOVER}; color: white; }}
QPushButton:default {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ background: {PANEL}; color: {MUTED}; border-color: {PANEL}; }}

QLineEdit, QSpinBox, QComboBox {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 8px; padding: 4px 8px; selection-background-color: {ACCENT}; selection-color: white; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ background: {PANEL}; color: {MUTED}; }}
QSpinBox {{ padding-right: 20px; }}
QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: 18px; border: none;
    background: transparent; }}
QSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 8px; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 8px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {HOVER}; }}
QSpinBox::up-arrow {{ width: 0; height: 0; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid {MUTED}; }}
QSpinBox::down-arrow {{ width: 0; height: 0; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {MUTED}; }}
QComboBox::down-arrow {{ width: 0; height: 0; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {MUTED}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT}; selection-color: white; outline: 0; }}

QListWidget {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: {RADIUS}px; padding: 4px;
               outline: 0; }}
QListWidget::item {{ border-radius: 7px; padding: 5px 8px; }}
QListWidget::item:hover {{ background: {RAISED}; }}
QListWidget::item:selected {{ background: {ACCENT}; color: white; }}

QProgressBar {{ background: {RAISED}; border: none; border-radius: 6px; text-align: center; color: {TEXT};
                min-height: 12px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}

QSlider::groove:horizontal {{ background: {RAISED}; height: 6px; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 16px; height: 16px; margin: -5px 0; border-radius: 8px; }}

QMenu {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: {MENU_RADIUS}px;
         padding: 6px; }}
QMenu::item {{ padding: 6px 26px 6px 26px; border-radius: 7px; background: transparent; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::item:disabled {{ color: {MUTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 10px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
"""


def _palette():
    pal = QPalette()
    roles = (("Window", BG), ("WindowText", TEXT), ("Base", RAISED), ("AlternateBase", PANEL),
             ("Text", TEXT), ("Button", RAISED), ("ButtonText", TEXT), ("ToolTipBase", PANEL),
             ("ToolTipText", TEXT), ("Highlight", ACCENT), ("HighlightedText", "#ffffff"),
             ("Link", ACCENT_HOVER), ("PlaceholderText", MUTED), ("BrightText", "#ffffff"))
    for role, color in roles:
        r = getattr(QPalette.ColorRole, role, None)
        if r is not None:
            pal.setColor(r, QColor(color))
    for role in ("WindowText", "Text", "ButtonText"):
        pal.setColor(QPalette.ColorGroup.Disabled, getattr(QPalette.ColorRole, role), QColor(MUTED))
    return pal


class _RoundPopups(QObject):
    """Menús emergentes con esquinas redondeadas.
    Windows: ventana translúcida (borde suave, sin sombra cuadrada).
    Linux: máscara redondeada (funciona aunque no haya compositor, p. ej. IceWM en antiX)."""
    TRANSLUCENT = sys.platform == "win32"

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.Polish and isinstance(obj, QMenu) and not obj.property("_tw_round"):
            obj.setProperty("_tw_round", True)
            if self.TRANSLUCENT:
                obj.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                obj.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
                obj.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        elif (not self.TRANSLUCENT and t in (QEvent.Type.Show, QEvent.Type.Resize)
              and isinstance(obj, QMenu)):
            path = QPainterPath()
            path.addRoundedRect(QRectF(obj.rect()), MENU_RADIUS, MENU_RADIUS)
            obj.setMask(QRegion(path.toFillPolygon().toPolygon()))
        return False


_filter = None


def apply(app=None):
    """Aplica el tema a toda la aplicación (una sola vez)."""
    global _filter
    app = app or QApplication.instance()
    if app is None or _filter is not None:
        return
    try:
        app.setStyle("Fusion")                 # igual en Windows 8.1/10/11 y Linux
    except Exception:
        pass
    app.setPalette(_palette())
    app.setStyleSheet(STYLE)
    _filter = _RoundPopups(app)
    app.installEventFilter(_filter)
