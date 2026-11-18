"""Monitor de rendimiento del sistema SIN dependencias (ctypes en Windows; /proc y sysfs en Linux).
 - CPU y RAM: llamadas de microsegundos (GetSystemTimes / GlobalMemoryStatusEx).
 - GPU: Windows: contadores «GPU Engine» (todas las marcas, 10 1709+) o NVML (NVIDIA). Linux: NVML (NVIDIA) o
   sysfs (AMD). Intel no expone un porcentaje estándar.
   Si no hay forma de medirla, la GPU se informa como None («n/d»).
 - Red: bytes por segundo recibidos/enviados por las placas físicas (Wi-Fi, USB, Ethernet), sin contar
   la interfaz local ni las virtuales/filtros (que duplicarían el tráfico).
   Windows: GetIfTable2 (contadores de 64 bits). Linux: /proc/net/dev."""
import ctypes
import glob
import os
import re
import sys
import threading
import time

IS_WIN = sys.platform == "win32"
_ENG_RE = re.compile(r"luid_0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+_phys_\d+_eng_\d+")


def _pct(last, cur):
    """last/cur = (inactivo, total) acumulados -> % de uso entre ambas lecturas."""
    if not last:
        return None
    di, dt = cur[0] - last[0], cur[1] - last[1]
    if dt <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - di / dt)))


if IS_WIN:
    from ctypes import wintypes

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    class _PdhUnion(ctypes.Union):
        _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double),
                    ("largeValue", ctypes.c_longlong), ("AnsiStringValue", ctypes.c_char_p),
                    ("WideStringValue", ctypes.c_wchar_p)]

    class _PdhValue(ctypes.Structure):
        _fields_ = [("CStatus", wintypes.DWORD), ("u", _PdhUnion)]

    class _PdhItem(ctypes.Structure):
        _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _PdhValue)]

    class _WinCpuRam:
        def __init__(self):
            k32 = ctypes.windll.kernel32
            self._times = k32.GetSystemTimes
            self._times.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
            self._times.restype = wintypes.BOOL
            self._mem = k32.GlobalMemoryStatusEx
            self._mem.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
            self._mem.restype = wintypes.BOOL
            self._last = None

        @staticmethod
        def _ft(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        def cpu(self):
            idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not self._times(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
                return None
            cur = (self._ft(idle), self._ft(kern) + self._ft(user))   # «kernel» ya incluye el tiempo inactivo
            last, self._last = self._last, cur
            return _pct(last, cur)

        def ram(self):
            m = _MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(m)
            return float(m.dwMemoryLoad) if self._mem(ctypes.byref(m)) else None

    class _GpuPdh:
        """Contadores «GPU Engine» (Windows 10 1709+): uso = motor más cargado, como el Administrador de tareas."""
        FMT_DOUBLE = 0x200
        MORE_DATA = 0x800007D2

        def __init__(self):
            pdh = ctypes.WinDLL("pdh")
            vp, dw = ctypes.c_void_p, wintypes.DWORD
            pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(vp)]
            pdh.PdhAddEnglishCounterW.argtypes = [vp, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(vp)]
            pdh.PdhCollectQueryData.argtypes = [vp]
            pdh.PdhCloseQuery.argtypes = [vp]
            pdh.PdhGetFormattedCounterArrayW.argtypes = [vp, dw, ctypes.POINTER(dw), ctypes.POINTER(dw), vp]
            for f in (pdh.PdhOpenQueryW, pdh.PdhAddEnglishCounterW, pdh.PdhCollectQueryData,
                      pdh.PdhCloseQuery, pdh.PdhGetFormattedCounterArrayW):
                f.restype = ctypes.c_long
            self._pdh = pdh
            self._q, self._c = vp(), vp()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._q)) != 0:
                raise OSError("PdhOpenQuery")
            if pdh.PdhAddEnglishCounterW(self._q, r"\GPU Engine(*)\Utilization Percentage", 0,
                                         ctypes.byref(self._c)) != 0:
                pdh.PdhCloseQuery(self._q)
                raise OSError("este Windows no tiene contadores de GPU")
            pdh.PdhCollectQueryData(self._q)

        def sample(self):
            pdh = self._pdh
            if pdh.PdhCollectQueryData(self._q) != 0:
                return None
            size, count = wintypes.DWORD(0), wintypes.DWORD(0)
            st = pdh.PdhGetFormattedCounterArrayW(self._c, self.FMT_DOUBLE, ctypes.byref(size),
                                                  ctypes.byref(count), None)
            if (st & 0xFFFFFFFF) != self.MORE_DATA or not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if pdh.PdhGetFormattedCounterArrayW(self._c, self.FMT_DOUBLE, ctypes.byref(size),
                                                ctypes.byref(count), buf) != 0:
                return None
            items = ctypes.cast(buf, ctypes.POINTER(_PdhItem))
            per_engine = {}
            for i in range(count.value):
                it = items[i]
                m = _ENG_RE.search(it.szName or "")
                if m and it.FmtValue.CStatus in (0, 1):
                    per_engine[m.group(0)] = per_engine.get(m.group(0), 0.0) + it.FmtValue.u.doubleValue
            return min(100.0, max(per_engine.values())) if per_engine else 0.0

        def close(self):
            try:
                self._pdh.PdhCloseQuery(self._q)
            except Exception:
                pass

    class _GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                    ("d", ctypes.c_ubyte * 8)]

    class _MIB_IF_ROW2(ctypes.Structure):
        _fields_ = [("InterfaceLuid", ctypes.c_ulonglong), ("InterfaceIndex", ctypes.c_ulong),
                    ("InterfaceGuid", _GUID), ("Alias", ctypes.c_wchar * 257),
                    ("Description", ctypes.c_wchar * 257), ("PhysicalAddressLength", ctypes.c_ulong),
                    ("PhysicalAddress", ctypes.c_ubyte * 32), ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
                    ("Mtu", ctypes.c_ulong), ("Type", ctypes.c_ulong), ("TunnelType", ctypes.c_int),
                    ("MediaType", ctypes.c_int), ("PhysicalMediumType", ctypes.c_int),
                    ("AccessType", ctypes.c_int), ("DirectionType", ctypes.c_int), ("Flags", ctypes.c_ubyte),
                    ("OperStatus", ctypes.c_int), ("AdminStatus", ctypes.c_int),
                    ("MediaConnectState", ctypes.c_int), ("NetworkGuid", _GUID), ("ConnectionType", ctypes.c_int),
                    ("TransmitLinkSpeed", ctypes.c_ulonglong), ("ReceiveLinkSpeed", ctypes.c_ulonglong),
                    ("InOctets", ctypes.c_ulonglong), ("InUcastPkts", ctypes.c_ulonglong),
                    ("InNUcastPkts", ctypes.c_ulonglong), ("InDiscards", ctypes.c_ulonglong),
                    ("InErrors", ctypes.c_ulonglong), ("InUnknownProtos", ctypes.c_ulonglong),
                    ("InUcastOctets", ctypes.c_ulonglong), ("InMulticastOctets", ctypes.c_ulonglong),
                    ("InBroadcastOctets", ctypes.c_ulonglong), ("OutOctets", ctypes.c_ulonglong),
                    ("OutUcastPkts", ctypes.c_ulonglong), ("OutNUcastPkts", ctypes.c_ulonglong),
                    ("OutDiscards", ctypes.c_ulonglong), ("OutErrors", ctypes.c_ulonglong),
                    ("OutUcastOctets", ctypes.c_ulonglong), ("OutMulticastOctets", ctypes.c_ulonglong),
                    ("OutBroadcastOctets", ctypes.c_ulonglong), ("OutQLen", ctypes.c_ulonglong)]

    class _WinNet:
        """Contadores de bytes de cada placa física (GetIfTable2, Windows Vista+: también 8.1)."""
        HARDWARE, FILTER = 0x01, 0x02
        LOOPBACK = 24

        def __init__(self):
            ip = ctypes.WinDLL("iphlpapi")
            ip.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
            ip.GetIfTable2.restype = ctypes.c_ulong
            ip.FreeMibTable.argtypes = [ctypes.c_void_p]
            ip.FreeMibTable.restype = None
            self._ip = ip
            if ctypes.sizeof(_MIB_IF_ROW2) != 1352:           # la estructura tiene que coincidir con Windows
                raise OSError("MIB_IF_ROW2 inesperada")

        def read(self):
            ptr = ctypes.c_void_p()
            if self._ip.GetIfTable2(ctypes.byref(ptr)) != 0 or not ptr.value:
                return None
            try:
                n = ctypes.c_ulong.from_address(ptr.value).value
                rows = (_MIB_IF_ROW2 * n).from_address(ptr.value + 8)      # la tabla empieza alineada a 8
                out = {}
                for r in rows:
                    if (r.Flags & self.HARDWARE) and not (r.Flags & self.FILTER) and r.Type != self.LOOPBACK:
                        out[r.InterfaceLuid] = (r.InOctets, r.OutOctets)
                return out
            finally:
                self._ip.FreeMibTable(ptr)

    class _WinBackend:
        def __init__(self):
            self._cr = _WinCpuRam()
            try:
                self._net = _NetRate(_WinNet().read)
            except Exception:
                self._net = None
            self._gpu = None
            self.gpu_source = "no disponible"
            for cls, label in ((_GpuPdh, "contadores de Windows"), (_GpuNvml, "NVIDIA NVML")):
                try:
                    self._gpu, self.gpu_source = cls(), label
                    break
                except Exception:
                    continue

        def cpu(self):
            return self._cr.cpu()

        def ram(self):
            return self._cr.ram()

        def gpu(self):
            return self._gpu.sample() if self._gpu else None

        def net(self):
            return self._net.sample() if self._net else None

        def close(self):
            if self._gpu:
                self._gpu.close()


class _NetRate:
    """(bytes/s recibidos, bytes/s enviados) entre dos lecturas. Se calcula por placa: si una aparece,
    desaparece o reinicia sus contadores (p. ej. al reconectar el Wi-Fi) no produce picos falsos."""

    def __init__(self, reader):
        self._read, self._last, self._t = reader, None, 0.0

    def sample(self):
        cur, now = self._read(), time.monotonic()
        if cur is None:
            return None
        last, t0 = self._last, self._t
        self._last, self._t = cur, now
        if last is None or now - t0 <= 0:
            return None
        rx = tx = 0
        for k, (r, t) in cur.items():
            if k in last:
                dr, dt_ = r - last[k][0], t - last[k][1]
                rx += dr if dr > 0 else 0
                tx += dt_ if dt_ > 0 else 0
        dt = now - t0
        return rx / dt, tx / dt


def _linux_net(proc="/proc/net/dev", sysnet="/sys/class/net"):
    """Placas físicas (tienen «device» en sysfs): Wi-Fi interna, adaptador USB, Ethernet."""
    out = {}
    with open(proc) as f:
        for line in f.readlines()[2:]:
            name, _, data = line.partition(":")
            name = name.strip()
            v = data.split()
            if name == "lo" or len(v) < 9 or not os.path.exists(os.path.join(sysnet, name, "device")):
                continue
            out[name] = (int(v[0]), int(v[8]))
    return out


def _nvml_names():
    if IS_WIN:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        return ("nvml.dll", os.path.join(pf, "NVIDIA Corporation", "NVSMI", "nvml.dll"))
    return ("libnvidia-ml.so.1", "libnvidia-ml.so")


class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _GpuNvml:
    """NVIDIA a través de la biblioteca NVML, que instala el controlador (Windows 8.1+ y Linux)."""

    def __init__(self):
        lib = None
        for name in _nvml_names():
            try:
                lib = ctypes.CDLL(name)
                break
            except OSError:
                continue
        if lib is None or lib.nvmlInit_v2() != 0:
            raise OSError("sin NVML")
        n = ctypes.c_uint(0)
        lib.nvmlDeviceGetCount_v2(ctypes.byref(n))
        self._h = []
        for i in range(n.value):
            h = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h)) == 0:
                self._h.append(h)
        if not self._h:
            raise OSError("NVML sin dispositivos")
        self._lib = lib

    def sample(self):
        best = None
        for h in self._h:
            u = _NvmlUtil()
            if self._lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == 0:
                best = max(best or 0, u.gpu)
        return None if best is None else float(best)

    def close(self):
        try:
            self._lib.nvmlShutdown()
        except Exception:
            pass


class _GpuSysfs:
    """AMD (amdgpu) en Linux: uso en /sys/class/drm/cardN/device/gpu_busy_percent."""

    def __init__(self, root="/sys/class/drm"):
        self._files = sorted(glob.glob(os.path.join(root, "card[0-9]*", "device", "gpu_busy_percent")))
        if not self._files:
            raise OSError("sin sysfs de GPU")

    def sample(self):
        best = None
        for f in self._files:
            try:
                with open(f) as fh:
                    v = float(fh.read().strip())
            except (OSError, ValueError):
                continue
            best = v if best is None else max(best, v)
        return best

    def close(self):
        pass


class _LinuxBackend:
    """Solo para pruebas fuera de Windows."""
    def __init__(self):
        self._last, self._gpu, self.gpu_source = None, None, "no disponible"
        self._net = _NetRate(_linux_net)
        for cls, label in ((_GpuNvml, "NVIDIA NVML"), (_GpuSysfs, "AMD (sysfs)")):
            try:
                self._gpu, self.gpu_source = cls(), label
                break
            except Exception:
                continue

    def cpu(self):
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        cur = (vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals[:8]))
        last, self._last = self._last, cur
        return _pct(last, cur)

    def ram(self):
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        return 100.0 * (1.0 - info["MemAvailable"] / info["MemTotal"])

    def gpu(self):
        return self._gpu.sample() if self._gpu else None

    def net(self):
        return self._net.sample()

    def close(self):
        if self._gpu:
            self._gpu.close()


def _make_backend():
    return _WinBackend() if IS_WIN else _LinuxBackend()


class SysMonitor:
    """Hilo que mide cada `interval` segundos mientras está activo.
    snapshot() -> (cpu %, ram %, gpu % | None, (bajada B/s, subida B/s) | None).
    La GPU solo se mide si se muestra (want_gpu); la red se lee siempre (cuesta microsegundos)."""

    def __init__(self, on_sample=None, interval=2.0):
        self.cpu, self.ram, self.gpu, self.net = 0.0, 0.0, None, None
        self.gpu_source = ""
        self.want_gpu = True
        self.on_sample, self.interval = on_sample, interval   # se pueden cambiar en caliente
        self._thread, self._stop = None, threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            return
        self._stop = threading.Event()               # un evento nuevo por ejecución: sin carreras
        self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        return self.cpu, self.ram, self.gpu, self.net

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:
            return None

    def _run(self, stop):
        backend = None
        try:
            backend = _make_backend()
            self.gpu_source = backend.gpu_source
            backend.cpu()                          # lecturas de referencia: el primer valor ya es real
            self._safe(backend.net)
            stop.wait(0.4)
            while not stop.is_set():
                c, r = self._safe(backend.cpu), self._safe(backend.ram)
                if c is not None:
                    self.cpu = c
                if r is not None:
                    self.ram = r
                self.gpu = self._safe(backend.gpu) if self.want_gpu else None
                self.net = self._safe(backend.net)
                if self.on_sample:
                    try:
                        self.on_sample(self.snapshot())
                    except Exception:
                        pass
                stop.wait(max(0.5, float(self.interval)))
        finally:
            if backend:
                backend.close()
