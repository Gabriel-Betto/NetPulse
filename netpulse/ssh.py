"""Atualização remota por SSH, com o cliente ``ssh`` do sistema (OpenSSH).

Regras de segurança desta v1:
  * só ações fixas e pré-definidas (nunca um comando digitado);
  * somente chave SSH (``BatchMode``): nenhuma senha é guardada ou pedida;
  * o endereço de cada máquina vem do ``ssh.json`` do administrador, nunca do que o agente informa;
  * a chave privada nunca é lida pelo NetPulse: só o caminho dela é passado ao ``ssh``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .sondas import host_valido

USUARIO_VALIDO = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$")
LIMITE_SAIDA = 4000

# O texto de cada comando é fixo. Nenhum valor externo é colocado dentro dele.
ACOES = {
    "linux": {
        "atualizar_apps": "sudo -n sh -c 'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get -y upgrade'",
    },
    "windows": {
        "atualizar_apps": "winget upgrade --all --silent --accept-package-agreements --accept-source-agreements",
    },
}


class ConfigSshInvalida(ValueError):
    pass


def validar_config(cfg: dict) -> dict:
    host, usuario = cfg.get("host", ""), cfg.get("usuario", "")
    if not host_valido(host):
        raise ConfigSshInvalida(f"host inválido: {host!r}")
    if not USUARIO_VALIDO.match(usuario):
        raise ConfigSshInvalida(f"usuário inválido: {usuario!r}")
    porta = cfg.get("porta", 22)
    if not isinstance(porta, int) or not 0 < porta < 65536:
        raise ConfigSshInvalida(f"porta inválida: {porta!r}")
    chave = cfg.get("chave")
    if chave is not None and (not isinstance(chave, str) or "\n" in chave or chave.startswith("-")):
        raise ConfigSshInvalida("caminho da chave inválido")
    return {"host": host, "usuario": usuario, "porta": porta, "chave": chave}


def carregar_config(arquivo: Path) -> dict[str, dict]:
    """{id_do_dispositivo: {host, usuario, porta, chave}}; entradas inválidas são ignoradas."""
    try:
        bruto = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ok = {}
    for id_, cfg in (bruto.items() if isinstance(bruto, dict) else []):
        try:
            ok[id_] = validar_config(cfg)
        except (ConfigSshInvalida, AttributeError):
            continue
    return ok


def salvar_config(arquivo: Path, id_: str, cfg: dict) -> dict:
    cfg = validar_config(cfg)
    try:
        atual = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        atual = {}
    atual[id_] = cfg
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(json.dumps(atual, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def comando_remoto(so: str, acao: str) -> str:
    try:
        return ACOES[so][acao]
    except KeyError:
        raise ConfigSshInvalida(f"ação {acao!r} não existe para o sistema {so!r}") from None


def montar_ssh(cfg: dict, comando: str) -> list[str]:
    cfg = validar_config(cfg)
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=accept-new", "-p", str(cfg["porta"])]
    if cfg["chave"]:
        cmd += ["-i", str(Path(cfg["chave"]).expanduser()), "-o", "IdentitiesOnly=yes"]
    return cmd + [f"{cfg['usuario']}@{cfg['host']}", comando]


def executar(cfg: dict, so: str, acao: str, rodar=subprocess.run, timeout_s: int = 1800) -> dict:
    """Roda a ação e devolve {'codigo': int, 'saida': str}. Nunca levanta por falha do ssh."""
    cmd = montar_ssh(cfg, comando_remoto(so, acao))
    try:
        r = rodar(cmd, capture_output=True, text=True, timeout=timeout_s, encoding="utf-8", errors="replace")
        saida = ((r.stdout or "") + (r.stderr or "")).strip()
        return {"codigo": r.returncode, "saida": saida[-LIMITE_SAIDA:]}
    except subprocess.TimeoutExpired:
        return {"codigo": -1, "saida": f"tempo esgotado após {timeout_s}s"}
    except FileNotFoundError:
        return {"codigo": -1, "saida": "o cliente 'ssh' não está instalado neste computador"}
    except OSError as e:
        return {"codigo": -1, "saida": f"falha ao executar ssh: {e}"}
