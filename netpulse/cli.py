"""Linha de comando: servidor, agente e gestão de dispositivos."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__, agente, parsers_csv, servidor, sondas, ssh
from .alertas import Notificador
from .db import Banco


def _dados(args) -> Path:
    return Path(args.dados)


def _banco(args) -> Banco:
    return Banco(_dados(args) / "netpulse.db")


def cmd_servidor(args) -> int:
    pasta = _dados(args)
    token = servidor.carregar_token(pasta / "token.txt")
    config = {}
    arq = pasta / "alertas.json"
    if arq.exists():
        try:
            config = json.loads(arq.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"Aviso: alertas.json inválido ({e}); seguindo sem alertas.", file=sys.stderr)
    notificador = Notificador(config)
    admin_token = servidor.carregar_token(pasta / "admin_token.txt")
    ssh_config = ssh.carregar_config(pasta / "ssh.json")
    app = servidor.App(_banco(args), token, notificador, admin_token=admin_token, ssh_config=ssh_config)
    print(f"NetPulse {__version__} em http://{args.host}:{args.porta}")
    print(f"Token dos agentes: {token}   (também em {pasta / 'token.txt'})")
    print(f"Token de administrador (ações remotas): {admin_token}   (em {pasta / 'admin_token.txt'}; NÃO coloque nos agentes)")
    if not shutil.which("ping"):
        print("Atenção: o comando 'ping' não foi encontrado; dispositivos sem agente aparecerão como desligados.")
    print(f"SSH configurado em {len(ssh_config)} dispositivo(s)")
    print(f"Alertas: {'ativos' if notificador.ativo else 'desligados (crie dados/alertas.json)'}")
    if args.host not in ("127.0.0.1", "localhost"):
        print("Atenção: o painel não tem login. Exponha só em rede local de confiança.")
    servidor.servir(app, args.host, args.porta, args.intervalo)
    return 0


def cmd_agente(args) -> int:
    token = args.token or os.environ.get("NETPULSE_TOKEN", "")
    if not token:
        print("Erro: informe --token ou a variável NETPULSE_TOKEN.", file=sys.stderr)
        return 1
    return agente.executar(args.servidor, token, Path(args.id_arquivo).expanduser(), args.intervalo, args.uma_vez)


def cmd_adicionar(args) -> int:
    if not sondas.host_valido(args.host):
        print(f"Erro: host inválido: {args.host!r}", file=sys.stderr)
        return 1
    with _banco(args) as banco:
        id_ = banco.adicionar_sonda(args.nome, args.host, time.time(), args.porta)
    print(f"Sonda cadastrada: {args.nome} ({args.host}{':' + str(args.porta) if args.porta else ''}) id={id_}")
    return 0


def cmd_importar(args) -> int:
    try:
        bruto = Path(args.csv).read_bytes()
        try:
            texto = bruto.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = bruto.decode("cp1252", errors="replace")   # CSV salvo pelo Excel/Bloco de Notas em ANSI
    except OSError as e:
        print(f"Erro: não consegui ler {args.csv}: {e}", file=sys.stderr)
        return 1
    linhas, avisos = parsers_csv.ler_hosts(texto)
    for aviso in avisos:
        print("Aviso:", aviso, file=sys.stderr)
    agora = time.time()
    with _banco(args) as banco:
        for ip, nome in linhas:
            banco.adicionar_sonda(nome, ip, agora)
    print(f"{len(linhas)} dispositivo(s) importado(s) como sonda de ping.")
    return 0


def cmd_ssh_configurar(args) -> int:
    with _banco(args) as banco:
        d = banco.dispositivo(args.id)
    if d is None or d["tipo"] != "agente":
        print("Erro: id não encontrado ou sem agente (SSH só vale para computadores com agente; veja: netpulse listar).",
              file=sys.stderr)
        return 1
    try:
        cfg = ssh.salvar_config(_dados(args) / "ssh.json", args.id, {
            "host": args.host, "usuario": args.usuario, "porta": args.porta, "chave": args.chave})
    except ssh.ConfigSshInvalida as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    print(f"SSH salvo: {cfg['usuario']}@{cfg['host']}:{cfg['porta']}. Reinicie o servidor para aplicar.")
    return 0


def cmd_atualizar(args) -> int:
    cfg = ssh.carregar_config(_dados(args) / "ssh.json").get(args.id)
    with _banco(args) as banco:
        d = banco.dispositivo(args.id)
        if d is None or d["tipo"] != "agente" or not cfg:
            print("Erro: dispositivo sem agente ou sem SSH configurado.", file=sys.stderr)
            return 1
        print(f"Atualizando {d['nome']} por SSH ({cfg['usuario']}@{cfg['host']})...")
        acao_id = banco.iniciar_acao(args.id, "atualizar_apps", time.time())
        res = ssh.executar(cfg, d["so"] or "", "atualizar_apps")
        banco.finalizar_acao(acao_id, time.time(), res["codigo"], res["saida"])
    print(res["saida"])
    print("Concluído." if res["codigo"] == 0 else f"Falhou (código {res['codigo']}).")
    return 0 if res["codigo"] == 0 else 1


def cmd_listar(args) -> int:
    with _banco(args) as banco:
        dispositivos = banco.dispositivos()
    for d in dispositivos:
        alvo = d["host"] or (d["relatorio"] or {}).get("hostname", "")
        print(f"{d['id']:<14}{d['tipo']:<8}{d['nome']:<28}{alvo}")
    return 0


def cmd_remover(args) -> int:
    with _banco(args) as banco:
        ok = banco.remover(args.id)
    print("Removido." if ok else "Id não encontrado.")
    return 0 if ok else 1


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="netpulse", description="Monitor leve de computadores e dispositivos da rede.")
    p.add_argument("--versao", action="version", version=f"netpulse {__version__}")
    p.add_argument("--dados", default="dados", help="pasta do banco, token e alertas (padrão: dados)")
    sub = p.add_subparsers(dest="comando", required=True)

    s = sub.add_parser("servidor", help="sobe o painel e recebe os agentes")
    s.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 para acessar de outros aparelhos da rede")
    s.add_argument("--porta", type=int, default=8765)
    s.add_argument("--intervalo", type=int, default=30, help="segundos entre verificações (padrão 30)")
    s.set_defaults(func=cmd_servidor)

    a = sub.add_parser("agente", help="roda no computador monitorado e envia métricas")
    a.add_argument("--servidor", required=True, help="ex.: http://192.168.10.50:8765")
    a.add_argument("--token")
    a.add_argument("--intervalo", type=int, default=60)
    a.add_argument("--uma-vez", action="store_true", help="envia uma vez e sai (para testar)")
    a.add_argument("--id-arquivo", default="~/.netpulse_id")
    a.set_defaults(func=cmd_agente)

    ad = sub.add_parser("adicionar", help="cadastra um dispositivo sem agente (celular, TV, impressora...)")
    ad.add_argument("--nome", required=True)
    ad.add_argument("--host", required=True, help="IP ou nome")
    ad.add_argument("--porta", type=int, help="se informada, testa a porta TCP em vez de ping")
    ad.set_defaults(func=cmd_adicionar)

    i = sub.add_parser("importar", help="importa dispositivos de um CSV (ip;mac;hostname;descricao)")
    i.add_argument("csv")
    i.set_defaults(func=cmd_importar)

    sc = sub.add_parser("ssh-configurar", help="define como acessar um computador por SSH (chave, sem senha)")
    sc.add_argument("id")
    sc.add_argument("--host", required=True, help="IP ou nome do computador")
    sc.add_argument("--usuario", required=True)
    sc.add_argument("--porta", type=int, default=22)
    sc.add_argument("--chave", help="caminho da chave privada, ex.: ~/.ssh/id_ed25519")
    sc.set_defaults(func=cmd_ssh_configurar)

    at = sub.add_parser("atualizar", help="atualiza os aplicativos de um computador por SSH")
    at.add_argument("id")
    at.set_defaults(func=cmd_atualizar)

    sub.add_parser("listar", help="lista dispositivos").set_defaults(func=cmd_listar)
    r = sub.add_parser("remover", help="remove um dispositivo pelo id")
    r.add_argument("id")
    r.set_defaults(func=cmd_remover)
    return p


def main(argv=None) -> int:
    args = construir_parser().parse_args(argv)
    return args.func(args)
