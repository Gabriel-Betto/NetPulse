"""Coleta de métricas da máquina local usando só a biblioteca padrão.

Cada métrica tem uma função que lê o sistema e, quando possível, uma função
``parse_*`` pura (recebe texto, devolve dados) para poder ser testada em qualquer SO.
"""

from __future__ import annotations

import base64
import ctypes
import os
import platform
import re
import shutil
import subprocess
import time

VERSAO_AGENTE = "1.0.0"
FS_REAIS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "ntfs", "ntfs3"}


def sistema() -> str:
    """'windows', 'linux', 'macos' ou o nome que o Python reportar, em minúsculas."""
    nome = platform.system().lower()
    return "macos" if nome == "darwin" else nome


# ---------------------------------------------------------------- memória

def parse_meminfo(texto: str) -> dict | None:
    valores = {}
    for linha in texto.splitlines():
        m = re.match(r"(\w+):\s+(\d+)\s*kB", linha)
        if m:
            valores[m.group(1)] = int(m.group(2)) * 1024
    total, disponivel = valores.get("MemTotal"), valores.get("MemAvailable")
    if not total or disponivel is None:
        return None
    usada = total - disponivel
    return {"total": total, "usada": usada, "percentual": round(usada / total * 100, 1)}


class _MemoriaWindows(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def memoria() -> dict | None:
    try:
        if sistema() == "linux":
            with open("/proc/meminfo", encoding="utf-8") as arq:
                return parse_meminfo(arq.read())
        if sistema() == "windows":
            st = _MemoriaWindows()
            st.dwLength = ctypes.sizeof(st)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            usada = st.ullTotalPhys - st.ullAvailPhys
            return {"total": st.ullTotalPhys, "usada": usada,
                    "percentual": round(usada / st.ullTotalPhys * 100, 1)}
    except (OSError, ValueError, AttributeError):
        pass
    return None


# -------------------------------------------------------------------- CPU

def parse_cpu_stat(texto: str) -> tuple[int, int] | None:
    """(ocioso, total) da primeira linha 'cpu' de /proc/stat."""
    for linha in texto.splitlines():
        if linha.startswith("cpu "):
            n = [int(x) for x in linha.split()[1:9]]
            if len(n) < 4:
                return None
            return n[3] + (n[4] if len(n) > 4 else 0), sum(n)
    return None


def percentual_cpu(antes: tuple[int, int], depois: tuple[int, int]) -> float | None:
    total = depois[1] - antes[1]
    if total <= 0:
        return None
    ocioso = depois[0] - antes[0]
    return round(max(0.0, min(100.0, (total - ocioso) / total * 100)), 1)


def _tempos_cpu_windows() -> tuple[int, int]:
    ocioso, kernel, usuario = (ctypes.c_ulonglong() for _ in range(3))
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(ocioso), ctypes.byref(kernel), ctypes.byref(usuario)):
        raise OSError("GetSystemTimes falhou")
    return ocioso.value, kernel.value + usuario.value  # kernel já inclui o tempo ocioso


def cpu(amostra_s: float = 1.0) -> float | None:
    try:
        if sistema() == "linux":
            def ler():
                with open("/proc/stat", encoding="utf-8") as arq:
                    return parse_cpu_stat(arq.read())
            a = ler()
            time.sleep(amostra_s)
            b = ler()
            return percentual_cpu(a, b) if a and b else None
        if sistema() == "windows":
            a = _tempos_cpu_windows()
            time.sleep(amostra_s)
            return percentual_cpu(a, _tempos_cpu_windows())
    except (OSError, ValueError, AttributeError):
        pass
    return None


# ------------------------------------------------------------------ discos

def parse_mounts(texto: str) -> list[str]:
    """Pontos de montagem de discos 'de verdade' (sem tmpfs, proc, overlay...)."""
    vistos, pontos = set(), []
    for linha in texto.splitlines():
        partes = linha.split()
        if len(partes) < 3 or partes[2] not in FS_REAIS:
            continue
        dispositivo, ponto = partes[0], partes[1].replace("\\040", " ")
        if dispositivo in vistos or ponto.startswith(("/snap", "/var/lib/docker")):
            continue
        vistos.add(dispositivo)
        pontos.append(ponto)
    return pontos


def _pontos_de_disco() -> list[str]:
    if sistema() == "windows":
        import string
        return [f"{letra}:\\" for letra in string.ascii_uppercase
                if ctypes.windll.kernel32.GetDriveTypeW(f"{letra}:\\") == 3]  # 3 = disco fixo
    pontos = ["/"]
    try:
        with open("/proc/mounts", encoding="utf-8") as arq:
            pontos += [p for p in parse_mounts(arq.read()) if p != "/"]
    except OSError:
        pass
    return pontos


def discos() -> list[dict]:
    resultado = []
    for ponto in _pontos_de_disco():
        try:
            uso = shutil.disk_usage(ponto)
        except OSError:
            continue
        if uso.total <= 0:
            continue
        resultado.append({"ponto": ponto, "total": uso.total, "livre": uso.free,
                          "percentual": round((uso.total - uso.free) / uso.total * 100, 1)})
    return resultado


# ------------------------------------------------------------------ uptime

def uptime_s() -> int | None:
    try:
        if sistema() == "linux":
            with open("/proc/uptime", encoding="utf-8") as arq:
                return int(float(arq.read().split()[0]))
        if sistema() == "windows":
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return int(ctypes.windll.kernel32.GetTickCount64() // 1000)
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    return None


# ------------------------------------------------------------ atualizações

def parse_apt_upgradable(texto: str) -> int:
    return sum(1 for linha in texto.splitlines() if "upgradable from" in linha)


def _ps_codificado(script: str) -> list[str]:
    """Comando PowerShell em base64 (UTF-16LE): evita qualquer problema de aspas."""
    cod = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", cod]


PS_ATUALIZACOES = ("(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()"
                   ".Search(\"IsInstalled=0 and Type='Software' and IsHidden=0\").Updates.Count")


def _reiniciar_windows() -> bool:
    import winreg
    chaves = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
    ]
    for chave in chaves:
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, chave))
            return True
        except OSError:
            continue
    return False


def atualizacoes(executar=subprocess.run) -> dict:
    """{'pendentes': int|None, 'reiniciar': bool}. ``None`` = não foi possível saber."""
    pendentes, reiniciar = None, False
    try:
        if sistema() == "windows":
            r = executar(_ps_codificado(PS_ATUALIZACOES), capture_output=True, text=True, timeout=180)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                pendentes = int(r.stdout.strip())
            reiniciar = _reiniciar_windows()
        elif sistema() == "linux":
            if shutil.which("apt"):
                r = executar(["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    pendentes = parse_apt_upgradable(r.stdout)
            reiniciar = os.path.exists("/var/run/reboot-required")
    except (OSError, subprocess.SubprocessError, ImportError):
        pass
    return {"pendentes": pendentes, "reiniciar": reiniciar}


# ------------------------------------------------------------------- rede

def parse_ip_route_get(texto: str) -> dict:
    """Saída de ``ip route get 1.1.1.1``: interface, IP local e gateway."""
    r = {}
    for chave, campo in (("dev", "interface"), ("src", "ip"), ("via", "gateway")):
        m = re.search(rf"\b{chave}\s+(\S+)", texto)
        if m:
            r[campo] = m.group(1)
    return r


def parse_netsh_wlan(texto: str) -> str | None:
    """Nome (SSID) da rede Wi-Fi conectada. O netsh só mostra a linha SSID quando conectado."""
    m = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", texto, re.MULTILINE)
    return m.group(1) if m else None


def _ip_local() -> str | None:
    """IP usado para sair para a internet (não envia nenhum pacote)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def rede(executar=subprocess.run) -> dict:
    """{'tipo': 'wifi'|'cabo'|None, 'interface', 'ip', 'gateway', 'ssid'}."""
    info = {"tipo": None, "interface": None, "ip": _ip_local(), "gateway": None, "ssid": None}
    try:
        if sistema() == "linux":
            r = executar(["ip", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                info.update(parse_ip_route_get(r.stdout))
            if info["interface"]:
                wifi = os.path.isdir(f"/sys/class/net/{info['interface']}/wireless")
                info["tipo"] = "wifi" if wifi else "cabo"
                if wifi and shutil.which("iwgetid"):
                    s = executar(["iwgetid", "-r"], capture_output=True, text=True, timeout=5)
                    info["ssid"] = s.stdout.strip() or None
        elif sistema() == "windows":
            w = executar(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=10)
            info["ssid"] = parse_netsh_wlan(w.stdout) if w.returncode == 0 else None
            info["tipo"] = "wifi" if info["ssid"] else "cabo"
            g = executar(_ps_codificado(
                "(Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Sort-Object RouteMetric | Select-Object -First 1).NextHop"),
                capture_output=True, text=True, timeout=20)
            if g.returncode == 0 and g.stdout.strip():
                info["gateway"] = g.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return info


# -------------------------------------------------------------- aplicativos

def parse_winget_upgrade(texto: str) -> list[dict]:
    """Tabela do ``winget upgrade``. As colunas são achadas pela posição do cabeçalho
    (funciona em qualquer idioma). Devolve [{'nome','id','versao','disponivel'}]."""
    linhas = texto.replace("\r", "").splitlines()
    sep = next((i for i, l in enumerate(linhas) if re.match(r"^-{8,}\s*$", l)), None)
    if sep is None or sep == 0:
        return []
    inicios = [m.start() for m in re.finditer(r"\S+(?: \S+)*", linhas[sep - 1])]
    if len(inicios) < 4:
        return []
    apps = []
    for linha in linhas[sep + 1:]:
        if not linha.strip() or re.match(r"^\d+\s", linha.strip()):
            break  # linha em branco ou o resumo final ("3 atualizações disponíveis")
        cols = [linha[a:b].strip() for a, b in zip(inicios, inicios[1:] + [None])]
        if len(cols) >= 4 and cols[0] and cols[3]:
            apps.append({"nome": cols[0], "id": cols[1], "versao": cols[2], "disponivel": cols[3]})
    return apps


def parse_apt_lista(texto: str) -> list[dict]:
    apps = []
    for linha in texto.splitlines():
        m = re.match(r"^([^/\s]+)/\S+\s+(\S+)\s.*upgradable from:\s*(\S+?)\]?\s*$", linha)
        if m:
            apps.append({"nome": m.group(1), "id": m.group(1), "versao": m.group(3), "disponivel": m.group(2)})
    return apps


def _instalados_windows(limite: int = 400) -> list[dict]:
    import winreg
    raizes = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
              (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
    vistos = {}
    for raiz, caminho in raizes:
        try:
            chave = winreg.OpenKey(raiz, caminho)
        except OSError:
            continue
        with chave:
            for i in range(winreg.QueryInfoKey(chave)[0]):
                try:
                    with winreg.OpenKey(chave, winreg.EnumKey(chave, i)) as sub:
                        nome = winreg.QueryValueEx(sub, "DisplayName")[0]
                        try:
                            versao = str(winreg.QueryValueEx(sub, "DisplayVersion")[0])
                        except OSError:
                            versao = ""
                        try:
                            sistema_ = winreg.QueryValueEx(sub, "SystemComponent")[0] == 1
                        except OSError:
                            sistema_ = False
                except OSError:
                    continue
                if nome and not sistema_ and nome not in vistos:
                    vistos[nome] = versao
    return [{"nome": n, "versao": v} for n, v in sorted(vistos.items(), key=lambda x: x[0].lower())][:limite]


def aplicativos(executar=subprocess.run) -> dict:
    """{'total': int|None, 'instalados': [...], 'atualizaveis': [...]}; None = não deu para saber."""
    resultado = {"total": None, "instalados": [], "atualizaveis": None}
    try:
        if sistema() == "windows":
            resultado["instalados"] = _instalados_windows()
            resultado["total"] = len(resultado["instalados"])
            if shutil.which("winget"):
                r = executar(["winget", "upgrade", "--accept-source-agreements", "--disable-interactivity"],
                             capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
                resultado["atualizaveis"] = parse_winget_upgrade(r.stdout or "")
        elif sistema() == "linux" and shutil.which("apt"):
            r = executar(["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                resultado["atualizaveis"] = parse_apt_lista(r.stdout)
            if shutil.which("dpkg-query"):
                d = executar(["dpkg-query", "-f", "${binary:Package}\n", "-W"], capture_output=True, text=True, timeout=30)
                if d.returncode == 0:
                    resultado["total"] = len([l for l in d.stdout.splitlines() if l.strip()])
    except (OSError, subprocess.SubprocessError, ImportError):
        pass
    return resultado


class CacheAtualizacoes:
    """A consulta de atualizações é lenta: refaz só a cada ``ttl_s`` segundos."""

    def __init__(self, ttl_s: float = 6 * 3600, buscar=atualizacoes, relogio=time.monotonic):
        self.ttl_s, self._buscar, self._relogio = ttl_s, buscar, relogio
        self._valor, self._quando = None, None

    def obter(self) -> dict:
        agora = self._relogio()
        if self._valor is None or agora - self._quando >= self.ttl_s:
            self._valor, self._quando = self._buscar(), agora
        return self._valor


def coletar(cache: CacheAtualizacoes | None = None, amostra_cpu_s: float = 1.0,
            cache_apps: CacheAtualizacoes | None = None) -> dict:
    return {
        "versao_agente": VERSAO_AGENTE,
        "hostname": platform.node() or "desconhecido",
        "so": sistema(),
        "so_versao": platform.platform(),
        "cpu": cpu(amostra_cpu_s),
        "ram": memoria(),
        "discos": discos(),
        "uptime_s": uptime_s(),
        "rede": rede(),
        "atualizacoes": (cache or CacheAtualizacoes()).obter(),
        "aplicativos": (cache_apps or CacheAtualizacoes(buscar=aplicativos)).obter(),
    }
