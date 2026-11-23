"""Control de la cámara por su protocolo nativo (DVRIP / NetSurveillance, puerto 34567).
Es el mismo que usan iCSee y XMEye. Sirve para leer y cambiar los FPS que la cámara codifica
(no solo los que se muestran), respetando el máximo que admite cada cámara."""
import hashlib
import json
import socket
import struct

PORT = 34567
LOGIN, CONFIG_SET, CONFIG_GET = 1000, 1040, 1042
OK = (100, 515)                 # 515 = correcto, pero la cámara pide reiniciar
STREAMS = (("MainFormat", "Convencional"), ("ExtraFormat", "Transmisión"))
LIMIT = 15                      # tope propio: más FPS satura el Wi-Fi de las cámaras y se cortan


class CamError(Exception):
    pass


def sofia_hash(password: str) -> str:
    """Hash de contraseña propio de estas cámaras (MD5 reducido a 8 caracteres)."""
    md5 = hashlib.md5(password.encode("utf-8")).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(chars[(md5[i] + md5[i + 1]) % 62] for i in range(0, 16, 2))


class Cam:
    def __init__(self, host, port=PORT, timeout=6.0):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise CamError(f"No se pudo conectar a {host}:{port} ({e})") from e
        self.session, self.seq = 0, 0

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise CamError("La cámara cerró la conexión")
            buf += chunk
        return buf

    def request(self, msg, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        head = struct.pack("<BB2xII2xHI", 255, 0, self.session, self.seq, msg, len(payload) + 2)
        try:
            self.sock.sendall(head + payload + b"\x0a\x00")
            self.seq += 1
            _, _, session, _, _, _, _, length = struct.unpack("<BB2xIIBBHI", self._recv(20))
            body = self._recv(length) if length else b""
        except OSError as e:
            raise CamError(f"Error de red con la cámara ({e})") from e
        if session:
            self.session = session
        try:
            return json.loads(body.rstrip(b"\x00\x0a").decode("utf-8", "replace"))
        except ValueError as e:
            raise CamError("Respuesta inválida de la cámara") from e

    def login(self, user, password):
        r = self.request(LOGIN, {"EncryptType": "MD5", "LoginType": "DVRIP-Web",
                                 "PassWord": sofia_hash(password), "UserName": user})
        if r.get("Ret") not in OK:
            raise CamError("Usuario o contraseña incorrectos" if r.get("Ret") in (203, 205)
                           else f"La cámara rechazó el acceso (código {r.get('Ret')})")
        self.session = int(r.get("SessionID", "0"), 16)

    def _named(self, msg, name, extra=None):
        data = {"Name": name, "SessionID": "0x%08X" % self.session}
        if extra is not None:
            data[name] = extra
        r = self.request(msg, data)
        if r.get("Ret") not in OK:
            raise CamError(f"La cámara rechazó «{name}» (código {r.get('Ret')})")
        return r.get(name)

    def get(self, name):
        return self._named(CONFIG_GET, name)

    def set(self, name, value):
        self._named(CONFIG_SET, name, value)


def _max_fps(cam):
    """Máximo de FPS: el de la cámara (25 en PAL, 30 en NTSC; si no se sabe, 30) con tope en LIMIT."""
    top = 30
    try:
        fmt = (cam.get("General.Location") or {}).get("VideoFormat", "")
        if fmt.upper() == "PAL":
            top = 25
    except CamError:
        pass
    return min(top, LIMIT)


def _streams(encode):
    out = []
    for ch, cfg in enumerate(encode or []):
        for key, label in STREAMS:
            v = (cfg.get(key) or {}).get("Video")
            if v and "FPS" in v:
                out.append({"channel": ch, "key": key, "label": label,
                            "resolution": v.get("Resolution", "?"), "fps": int(v["FPS"])})
    return out


WIFI_BITRATE, WIFI_GOP = 1536, 8          # HD «para WiFi»: keyframes más chicos y la mitad de seguido
STD_BITRATE, STD_GOP = 2048, 4            # valores de fábrica habituales, al desactivarlo


def _low_lux(cam):
    """Modo poca luz (baja los FPS a propósito para aclarar la imagen). None si la cámara no lo tiene."""
    try:
        v = (cam.get("Camera.ParamEx") or [{}])[0].get("LowLuxMode")
    except CamError:
        return None
    return None if v is None else bool(v)


def _wifi_tuned(encode):
    v = ((encode or [{}])[0].get("MainFormat") or {}).get("Video") or {}
    return (v.get("BitRateControl") == "VBR" and int(v.get("GOP", 0)) >= WIFI_GOP
            and int(v.get("BitRate", 0)) <= WIFI_BITRATE)


def _state(cam, encode, top):
    streams = _streams(encode)
    if not streams:
        raise CamError("La cámara no informó la configuración de video")
    return {"max": top, "streams": streams, "low_lux": _low_lux(cam), "wifi": _wifi_tuned(encode)}


def read_fps(host, user, password, port=PORT):
    """Lee los FPS actuales de cada stream, el máximo admitido y los ajustes de fluidez.
    Devuelve {"max": int, "streams": [{channel, key, label, resolution, fps}, ...],
    "low_lux": bool | None, "wifi": bool}."""
    with Cam(host, port) as cam:
        cam.login(user, password)
        return _state(cam, cam.get("Simplify.Encode"), _max_fps(cam))


def write_fps(host, user, password, wanted, port=PORT, low_lux=None, wifi=None):
    """Pone los FPS pedidos ({"MainFormat": 25, "ExtraFormat": 15}), recortados al máximo.
    low_lux: True/False enciende o apaga el modo poca luz (None = no tocar).
    wifi: True = HD a 1536 kbps VBR con keyframe cada 8 s (menos tirones por WiFi); False = 2048 / 4 s.
    Devuelve lo que la cámara aceptó de verdad (algunas recortan por resolución)."""
    with Cam(host, port) as cam:
        cam.login(user, password)
        encode = cam.get("Simplify.Encode")
        top = _max_fps(cam)
        for cfg in encode:
            for key, value in wanted.items():
                v = (cfg.get(key) or {}).get("Video")
                if v and "FPS" in v:
                    v["FPS"] = max(1, min(int(value), top))
            main = (cfg.get("MainFormat") or {}).get("Video")
            if main and wifi is not None and wifi != _wifi_tuned([cfg]):
                main["BitRateControl"] = "VBR"
                main["BitRate"], main["GOP"] = (WIFI_BITRATE, WIFI_GOP) if wifi else (STD_BITRATE, STD_GOP)
        cam.set("Simplify.Encode", encode)
        if low_lux is not None and _low_lux(cam) not in (None, low_lux):
            px = cam.get("Camera.ParamEx")
            for ch in px:
                ch["LowLuxMode"] = int(low_lux)
            cam.set("Camera.ParamEx", px)
        return _state(cam, cam.get("Simplify.Encode"), top)
