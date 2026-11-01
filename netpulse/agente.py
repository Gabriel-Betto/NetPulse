"""Agente: coleta métricas desta máquina e envia ao servidor NetPulse."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import coletores


def carregar_id(arquivo: Path) -> str:
    """ID estável da máquina: gerado na primeira execução e guardado em disco."""
    try:
        existente = arquivo.read_text(encoding="utf-8").strip()
        if existente:
            return existente
    except (OSError, ValueError):
        pass
    novo = uuid.uuid4().hex
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(novo, encoding="utf-8")
    return novo


def montar_relatorio(agente_id: str, cache=None, amostra_cpu_s: float = 1.0, cache_apps=None) -> dict:
    relatorio = coletores.coletar(cache, amostra_cpu_s, cache_apps)
    relatorio["id"] = agente_id
    return relatorio


def enviar(servidor: str, token: str, relatorio: dict, timeout: float = 10) -> tuple[bool, str]:
    req = urllib.request.Request(
        servidor.rstrip("/") + "/api/relatorio",
        data=json.dumps(relatorio).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} (token errado?)" if e.code == 401 else f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"sem conexão com o servidor: {e}"


def executar(servidor: str, token: str, arquivo_id: Path, intervalo_s: int = 60,
             uma_vez: bool = False, log=print) -> int:
    agente_id = carregar_id(arquivo_id)
    cache = coletores.CacheAtualizacoes()
    cache_apps = coletores.CacheAtualizacoes(buscar=coletores.aplicativos)
    while True:
        ok, msg = enviar(servidor, token, montar_relatorio(agente_id, cache, cache_apps=cache_apps))
        log(f"[{time.strftime('%H:%M:%S')}] {'enviado' if ok else 'FALHA'}: {msg}")
        if uma_vez:
            return 0 if ok else 1
        time.sleep(intervalo_s)
