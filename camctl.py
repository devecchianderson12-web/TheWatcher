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


def read_fps(host, user, password, port=PORT):
    """Lee los FPS actuales de cada stream y el máximo admitido.
    Devuelve {"max": int, "streams": [{channel, key, label, resolution, fps}, ...]}."""
    with Cam(host, port) as cam:
        cam.login(user, password)
        encode = cam.get("Simplify.Encode")
        streams = _streams(encode)
        if not streams:
            raise CamError("La cámara no informó la configuración de video")
        return {"max": _max_fps(cam), "streams": streams}


def write_fps(host, user, password, wanted, port=PORT):
    """Pone los FPS pedidos ({"MainFormat": 25, "ExtraFormat": 15}), recortados al máximo,
    y devuelve lo que la cámara aceptó de verdad (algunas recortan por resolución)."""
    with Cam(host, port) as cam:
        cam.login(user, password)
        encode = cam.get("Simplify.Encode")
        top = _max_fps(cam)
        for cfg in encode:
            for key, value in wanted.items():
                v = (cfg.get(key) or {}).get("Video")
                if v and "FPS" in v:
                    v["FPS"] = max(1, min(int(value), top))
        cam.set("Simplify.Encode", encode)
        streams = _streams(cam.get("Simplify.Encode"))
        return {"max": top, "streams": streams}
