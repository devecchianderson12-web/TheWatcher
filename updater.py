"""Actualización automática desde el Release de GitHub (sin Qt: lo usa update_ui.py).

Cómo se instala cada versión nueva, dejando solo la nueva:
  - Windows con instalador: corre el instalador nuevo en silencio sobre el mismo programa.
  - Windows portable: reemplaza la carpeta (conserva config.json y portable.flag).
  - Linux .deb: lo instala el sistema (root) con /usr/libexec/thewatcher-update, que se ejecuta cada minuto.
  - Linux .tar.gz: reemplaza la carpeta del programa.
Los archivos descargados se borran siempre, salga bien o mal."""
import json
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

from version import VERSION

REPO = "devecchianderson12-web/TheWatcher"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
UA = f"TheWatcher-updater/{VERSION}"
NET_TIMEOUT = 30            # segundos sin recibir nada = descarga trabada
DEB_DIR = "/var/lib/thewatcher/update"   # estado y resultado del actualizador del sistema (.deb, solo root)
REQ_DIR = "/var/lib/thewatcher/pedido"   # acá el programa deja el pedido (cualquier usuario puede escribir)


class UpdateError(Exception):
    pass


def parse_version(text):
    try:
        return tuple(int(x) for x in str(text).strip().lstrip("vV").split("."))
    except ValueError:
        return None


def is_newer(remote, local=VERSION):
    r, l = parse_version(remote), parse_version(local)
    return bool(r and l and r > l)


def app_dir():
    return os.path.dirname(os.path.realpath(sys.executable))


def kind():
    """Cómo está instalado el programa, o None si no se puede actualizar solo (p. ej. desde el código)."""
    if not getattr(sys, "frozen", False):
        return None
    here = app_dir()
    if sys.platform == "win32":
        return "win-portable" if os.path.exists(os.path.join(here, "portable.flag")) else "win-setup"
    if sys.platform.startswith("linux"):
        if here.startswith("/opt/thewatcher"):
            return "deb" if os.access(REQ_DIR, os.W_OK) else None
        return "tar" if os.access(here, os.W_OK) and os.access(os.path.dirname(here), os.W_OK) else None
    return None


def _asset_name(k, version):
    return {"win-setup": "TheWatcher-Setup-Win81.exe",
            "win-portable": "TheWatcher-Win81-portable.zip",
            "deb": f"thewatcher_{version}_amd64.deb",
            "tar": "TheWatcher-Linux-x86_64.tar.gz"}.get(k)


def _ssl_context():
    ctx = ssl.create_default_context()
    if sys.platform.startswith("linux"):           # el programa empaquetado puede no hallar los certificados
        for ca in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
            if os.path.exists(ca):
                try:
                    ctx.load_verify_locations(ca)
                except ssl.SSLError:
                    pass
    return ctx


def _open(url, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **({"Accept": accept} if accept else {})})
    return urllib.request.urlopen(req, timeout=NET_TIMEOUT, context=_ssl_context())


def check():
    """Busca una versión más nueva. Devuelve {"version", "url", "size"} o None. Lanza UpdateError si falla."""
    k = kind()
    if k is None:
        return None
    try:
        with _open(API, "application/vnd.github+json") as r:
            data = json.load(r)
    except Exception as e:
        raise UpdateError(f"no se pudo consultar GitHub ({type(e).__name__})") from e
    version = str(data.get("tag_name", "")).lstrip("vV")
    if not is_newer(version):
        return None
    want = _asset_name(k, version)
    for a in data.get("assets") or []:
        if a.get("name") == want and a.get("browser_download_url"):
            return {"version": version, "url": a["browser_download_url"], "size": int(a.get("size") or 0)}
    return None                                  # el Release todavía no tiene el archivo para este sistema


def work_dir():
    return os.path.join(tempfile.gettempdir(), "TheWatcher-update")


def cleanup():
    """Borra restos de una actualización anterior (se llama al abrir el programa)."""
    shutil.rmtree(work_dir(), ignore_errors=True)


def download(info, progress, cancelled):
    """Descarga a la carpeta temporal. progress(hechos, total). Verifica el tamaño. Devuelve la ruta."""
    d = work_dir()
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, info["url"].rsplit("/", 1)[-1])
    total, done = info.get("size") or 0, 0
    try:
        with _open(info["url"]) as r, open(path, "wb") as f:
            total = total or int(r.headers.get("Content-Length") or 0)
            while True:
                if cancelled():
                    raise UpdateError("cancelado")
                chunk = r.read(256 * 1024)       # cada lectura tiene su propio límite de tiempo
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                progress(done, total)
    except UpdateError:
        cleanup()
        raise
    except Exception as e:
        cleanup()
        raise UpdateError(f"la descarga se cortó ({type(e).__name__})") from e
    if total and done != total:
        cleanup()
        raise UpdateError("la descarga llegó incompleta")
    return path


def _detached(cmd, **kw):
    """Lanza un proceso que sigue vivo cuando este programa se cierra."""
    if sys.platform == "win32":
        flags = 0x00000200 | 0x08000000    # NEW_PROCESS_GROUP | NO_WINDOW (con DETACHED el .cmd se cuelga)
        subprocess.Popen(cmd, creationflags=flags, close_fds=True, **kw)
    else:
        subprocess.Popen(cmd, start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def _win_helper(lines):
    """Escribe y lanza un .cmd que espera a que este programa se cierre, hace el trabajo y se borra."""
    d = work_dir()
    pid = os.getpid()
    script = os.path.join(d, "actualizar.cmd")
    name = os.path.basename(sys.executable)
    body = ["@echo off", "chcp 65001 >nul", "cd /d \"%TEMP%\"", "set n=0",
            ":esperar",                                   # hasta 60 s; después sigue igual
            f'tasklist /FI "PID eq {pid}" /NH | find /I "{name}" >nul || goto listo',
            "set /a n+=1", "if %n% GEQ 60 goto listo", "ping -n 2 127.0.0.1 >nul", "goto esperar",
            ":listo",
            *lines,
            f'(goto) 2>nul & rd /s /q "{d}"']           # se borra a sí mismo y a la descarga
    with open(script, "w", encoding="utf-8", newline="\r\n") as f:     # CRLF exactos (sin \r\r\n)
        f.write("\n".join(body) + "\n")
    _detached(["cmd.exe", "/c", script])


def _sh_helper(lines):
    d = work_dir()
    script = os.path.join(d, "actualizar.sh")
    body = ["#!/bin/sh", "n=0",                         # espera hasta 60 s a que se cierre
            f'while kill -0 {os.getpid()} 2>/dev/null && [ $n -lt 120 ]; do sleep 0.5; n=$((n+1)); done',
            *lines,
            f'rm -rf "{d}"']
    with open(script, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
    os.chmod(script, 0o755)
    _detached(["/bin/sh", script])


def install(path):
    """Deja todo listo para reemplazar el programa. Después hay que cerrarlo (el ayudante espera a que cierre).
    Para «deb» no se usa: ver request_system_update()."""
    k = kind()
    here = app_dir()
    exe = os.path.realpath(sys.executable)
    if k == "win-setup":
        _win_helper([f'start "" /wait "{path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /CLOSEAPPLICATIONS',
                     f'start "" "{exe}"'])
    elif k == "win-portable":
        new = os.path.join(work_dir(), "nuevo")
        _extract_zip(path, new)
        src = _single_root(new)
        _win_helper([f'robocopy "{src}" "{here}" /MIR /XF config.json config.json.tmp portable.flag '
                     f'/R:5 /W:1 /NFL /NDL /NJH /NJS /NP >nul',
                     f'start "" "{exe}"'])
    elif k == "tar":
        new = os.path.join(work_dir(), "nuevo")
        with tarfile.open(path) as t:
            for m in t.getmembers():               # nada fuera de la carpeta de destino
                if m.name.startswith(("/", "..")) or "/../" in m.name or m.issym() and m.linkname.startswith("/"):
                    raise UpdateError("el archivo descargado no es válido")
            try:
                t.extractall(new, filter="data")
            except TypeError:                      # Python sin filtros de tar
                t.extractall(new)
        src = _single_root(new)
        old = here + ".viejo"
        _sh_helper([
            f'for f in config.json portable.flag; do [ -e "{here}/$f" ] && cp -p "{here}/$f" "{src}/"; done',
            f'rm -rf "{old}"',
            f'if mv "{here}" "{old}" && mv "{src}" "{here}"; then rm -rf "{old}"; '
            f'else [ -d "{here}" ] || mv "{old}" "{here}"; fi',
            f'(cd "{here}" && nohup "{here}/{os.path.basename(exe)}" >/dev/null 2>&1 &)'])
    else:
        raise UpdateError("esta instalación no se puede actualizar sola")


def _extract_zip(path, dest):
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/"):
                    raise UpdateError("el archivo descargado no es válido")
            z.extractall(dest)
    except zipfile.BadZipFile as e:
        raise UpdateError("el archivo descargado está dañado") from e


def _single_root(folder):
    """Si el comprimido trae una sola carpeta (TheWatcher/), devuelve esa."""
    items = os.listdir(folder)
    if len(items) == 1 and os.path.isdir(os.path.join(folder, items[0])):
        return os.path.join(folder, items[0])
    return folder


# ---- Linux .deb: lo instala el sistema ----
def request_system_update():
    """Deja el pedido para /usr/libexec/thewatcher-update (corre como root cada minuto). Devuelve la hora."""
    stamp = time.time()
    try:
        with open(os.path.join(REQ_DIR, "request"), "w") as f:
            f.write(str(stamp))
    except OSError as e:
        raise UpdateError("no se pudo pedir la actualización al sistema") from e
    return stamp


def system_update_state(since):
    """("esperando"|"descargando"|"instalando"|"ok"|"error", detalle) del actualizador del sistema."""
    for name in ("result", "status"):
        p = os.path.join(DEB_DIR, name)
        try:
            if os.path.getmtime(p) + 1 < since:
                continue                        # de un pedido anterior
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read().strip()
        except OSError:
            continue
        state, _, detail = text.partition(" ")
        return state, detail
    return "esperando", ""


def restart():
    """Vuelve a abrir el programa (ya actualizado) en un proceso nuevo."""
    exe = "/usr/bin/thewatcher" if kind() == "deb" and os.path.exists("/usr/bin/thewatcher") \
        else os.path.realpath(sys.executable)
    _detached([exe])
