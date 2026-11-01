"""Sondas: testam se um dispositivo está respondendo (ping ou porta TCP).

Servem para o que não tem agente: celulares, TVs, impressoras, câmeras, switches.
"""

from __future__ import annotations

import re
import socket
import subprocess
import time

from .coletores import sistema

HOST_VALIDO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")  # nunca começa com '-'
_TEMPO = re.compile(r"(?:time|tempo|zeit)\s*([=<])\s*([\d.,]+)\s*ms", re.IGNORECASE)


def host_valido(host: str) -> bool:
    return bool(HOST_VALIDO.match(host or ""))


def parse_latencia_ping(saida: str) -> float | None:
    m = _TEMPO.search(saida)
    if not m:
        return None
    valor = float(m.group(2).replace(",", "."))
    return 0.5 if m.group(1) == "<" else valor


def ping(host: str, timeout_s: int = 2, executar=subprocess.run) -> float | None:
    """Latência em ms, ou None se não respondeu."""
    if not host_valido(host):
        return None
    if sistema() == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_s * 1000), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout_s), host]
    try:
        r = executar(cmd, capture_output=True, text=True, timeout=timeout_s + 3)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    # ping respondeu mas sem tempo legível: considera online
    return parse_latencia_ping(r.stdout) or 0.5


def tcp(host: str, porta: int, timeout_s: float = 2.0) -> float | None:
    if not host_valido(host) or not 0 < porta < 65536:
        return None
    inicio = time.perf_counter()
    try:
        with socket.create_connection((host, porta), timeout=timeout_s):
            return round((time.perf_counter() - inicio) * 1000, 1)
    except OSError:
        return None


def testar(host: str, porta: int | None = None) -> float | None:
    return tcp(host, porta) if porta else ping(host)
