"""Leitura do CSV de IPs (mesmo formato do projeto inventario-rede)."""

from __future__ import annotations

import csv
import io

from .sondas import host_valido


def ler_hosts(texto: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Devolve ([(ip, nome)], avisos). Aceita ``;`` ou ``,``; colunas ip e hostname/nome/descricao."""
    texto = texto.lstrip("﻿")
    primeira = texto.splitlines()[0] if texto.strip() else ""
    sep = ";" if primeira.count(";") >= primeira.count(",") else ","
    leitor = csv.DictReader(io.StringIO(texto), delimiter=sep)
    campos = {(c or "").strip().lower(): c for c in (leitor.fieldnames or [])}
    if "ip" not in campos:
        return [], ["o CSV precisa ter uma coluna 'ip'"]
    hosts, avisos = [], []
    for n, linha in enumerate(leitor, start=2):
        ip = (linha.get(campos["ip"]) or "").strip()
        nome = next(((linha.get(campos[c]) or "").strip() for c in ("hostname", "nome", "descricao", "descrição")
                     if c in campos and (linha.get(campos[c]) or "").strip()), ip)
        if not host_valido(ip):
            avisos.append(f"linha {n}: ip inválido {ip!r} (ignorada)")
            continue
        hosts.append((ip, nome))
    return hosts, avisos
