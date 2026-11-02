"""Servidor NetPulse: recebe relatórios dos agentes, testa as sondas e serve o painel."""

from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import regras, sondas, ssh
from .alertas import Notificador
from .db import Banco

PASTA_WEB = Path(__file__).parent / "web"
ID_VALIDO = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_CORPO = 1_000_000
RETENCAO_S = 30 * 24 * 3600


def carregar_token(arquivo: Path) -> str:
    try:
        token = arquivo.read_text(encoding="utf-8").strip()
        if token:
            return token
    except (OSError, ValueError):
        pass
    token = secrets.token_urlsafe(24)
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(token, encoding="utf-8")
    try:
        arquivo.chmod(0o600)
    except OSError:
        pass
    return token


def _num(valor, minimo=0.0, maximo=100.0):
    """Número dentro da faixa ou None (relatório vindo da rede nunca é confiável)."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return float(valor) if minimo <= valor <= maximo else None


def _texto(valor, limite=120):
    return valor[:limite] if isinstance(valor, str) else ""


def limpar_relatorio(bruto: dict) -> dict:
    """Mantém só campos conhecidos, com tipos e tamanhos controlados."""
    ram = bruto.get("ram") if isinstance(bruto.get("ram"), dict) else {}
    rede = bruto.get("rede") if isinstance(bruto.get("rede"), dict) else {}
    atu = bruto.get("atualizacoes") if isinstance(bruto.get("atualizacoes"), dict) else {}
    apps = bruto.get("aplicativos") if isinstance(bruto.get("aplicativos"), dict) else {}

    def lista_apps(itens, campos, limite=500):
        saida = []
        for item in (itens if isinstance(itens, list) else [])[:limite]:
            if isinstance(item, dict):
                saida.append({c: _texto(item.get(c), 120) for c in campos})
        return saida

    discos = []
    for d in (bruto.get("discos") if isinstance(bruto.get("discos"), list) else [])[:26]:
        if isinstance(d, dict) and _num(d.get("percentual")) is not None:
            discos.append({"ponto": _texto(d.get("ponto"), 20), "percentual": _num(d["percentual"]),
                           "total": _num(d.get("total"), 0, 1e18), "livre": _num(d.get("livre"), 0, 1e18) or 0})
    pendentes = atu.get("pendentes")
    return {
        "hostname": _texto(bruto.get("hostname"), 60), "so": _texto(bruto.get("so"), 20),
        "so_versao": _texto(bruto.get("so_versao"), 120), "versao_agente": _texto(bruto.get("versao_agente"), 20),
        "cpu": _num(bruto.get("cpu")),
        "ram": {"percentual": _num(ram.get("percentual")), "total": _num(ram.get("total"), 0, 1e18),
                "usada": _num(ram.get("usada"), 0, 1e18)},
        "discos": discos,
        "uptime_s": int(bruto["uptime_s"]) if _num(bruto.get("uptime_s"), 0, 1e10) is not None else None,
        "rede": {k: (_texto(rede.get(k), 60) or None) for k in ("tipo", "interface", "ip", "gateway", "ssid")},
        "atualizacoes": {"pendentes": int(pendentes) if _num(pendentes, 0, 100000) is not None else None,
                         "reiniciar": bool(atu.get("reiniciar"))},
        "aplicativos": {
            "total": int(apps["total"]) if _num(apps.get("total"), 0, 100000) is not None else None,
            "instalados": lista_apps(apps.get("instalados"), ("nome", "versao")),
            "atualizaveis": (lista_apps(apps.get("atualizaveis"), ("nome", "id", "versao", "disponivel"))
                             if isinstance(apps.get("atualizaveis"), list) else None),
        },
    }


class App:
    def __init__(self, banco: Banco, token: str, notificador: Notificador | None = None,
                 limites: dict | None = None, agora=time.time, testar=sondas.testar, log=print,
                 admin_token: str | None = None, ssh_config: dict | None = None, rodar_ssh=ssh.executar,
                 em_segundo_plano: bool = True):
        self.banco, self.token = banco, token
        self.admin_token = admin_token          # None = ações remotas desligadas
        self.ssh_config = ssh_config or {}
        self._rodar_ssh, self._em_segundo_plano = rodar_ssh, em_segundo_plano
        self.notificador = notificador or Notificador()
        self.limites = {**regras.LIMITES_PADRAO, **(limites or {})}
        self.agora, self._testar, self.log = agora, testar, log
        self.avaliador = regras.Avaliador()
        self._ultima_limpeza = 0.0
        self._lock = threading.RLock()

    # -------------------------------------------------------- entrada
    def processar_relatorio(self, payload: dict) -> dict:
        id_ = payload.get("id")
        if not isinstance(id_, str) or not ID_VALIDO.match(id_):
            raise ValueError("id inválido")
        rel = limpar_relatorio(payload)
        agora = self.agora()
        with self._lock:
            self.banco.registrar_relatorio(id_, rel["hostname"] or id_[:8], rel["so"], agora, rel)
            disco_max = max((d["percentual"] for d in rel["discos"]), default=None)
            self.banco.registrar_metrica(id_, agora, rel["cpu"], rel["ram"]["percentual"], disco_max)
            problemas = self.avaliador.confirmar(id_, regras.avaliar_relatorio(rel, self.limites))
            eventos = regras.sincronizar(self.banco, id_, problemas, agora)  # fecha 'offline' se voltou
        self._avisar(rel["hostname"] or id_[:8], eventos)
        return {"ok": True}

    def _avisar(self, nome: str, eventos: list[dict]):
        if not eventos:
            return
        quando = self.agora()
        completos = [{**e, "dispositivo": nome, "quando": quando} for e in eventos]
        for e in completos:
            self.log(f"[{time.strftime('%H:%M:%S')}] {e['tipo']}: {e['dispositivo']} - {e['detalhe']}")
        self.notificador.notificar_em_segundo_plano(completos)

    # ------------------------------------------------------ verificação
    def verificar(self):
        """Um ciclo: marca agentes sumidos como offline e testa as sondas."""
        agora = self.agora()
        for d in self.banco.dispositivos():
            with self._lock:
                if d["tipo"] == "agente":
                    sem_contato = agora - d["ultimo_contato"] if d["ultimo_contato"] else None
                    if sem_contato is None or sem_contato > self.limites["offline_apos_s"]:
                        brutos = [regras.problema_offline(d["nome"], sem_contato)]
                        eventos = regras.sincronizar(self.banco, d["id"], brutos, agora,
                                                     {"offline": d["ultimo_contato"] or agora}, {"offline"})
                        self._avisar(d["nome"], eventos)
                else:
                    latencia = self._testar(d["host"], d["porta"])
                    falhas = self.banco.registrar_sonda(d["id"], agora, latencia)
                    if latencia is not None:
                        self.banco.registrar_metrica(d["id"], agora, latencia_ms=latencia)
                    brutos = []
                    if falhas >= self.limites["sonda_falhas"]:
                        sem = agora - d["ultimo_contato"] if d["ultimo_contato"] else None
                        brutos = [regras.problema_offline(d["nome"], sem)]
                    eventos = regras.sincronizar(self.banco, d["id"], brutos, agora,
                                                 {"offline": d["ultimo_contato"] or agora}, {"offline"})
                    self._avisar(d["nome"], eventos)
        if agora - self._ultima_limpeza > 3600:
            self.banco.limpar_antigos(agora - RETENCAO_S)
            self._ultima_limpeza = agora

    # ------------------------------------------------------ ações remotas
    def solicitar_acao(self, id_: str, acao: str) -> dict:
        """Dispara uma ação fixa por SSH. Levanta ValueError com mensagem para o usuário."""
        d = self.banco.dispositivo(id_)
        if d is None or d["tipo"] != "agente":
            raise ValueError("dispositivo não encontrado ou sem agente")
        cfg = self.ssh_config.get(id_)
        if not cfg:
            raise ValueError("SSH não configurado para este dispositivo (use: netpulse ssh-configurar)")
        so = d["so"] or ""
        ssh.comando_remoto(so, acao)  # valida a combinação sistema/ação antes de registrar
        with self._lock:
            if self.banco.acao_em_andamento(id_):
                raise ValueError("já existe uma ação em andamento neste dispositivo")
            acao_id = self.banco.iniciar_acao(id_, acao, self.agora())

        def trabalho():
            res = self._rodar_ssh(cfg, so, acao)
            self.banco.finalizar_acao(acao_id, self.agora(), res["codigo"], res["saida"])
            self.log(f"[{time.strftime('%H:%M:%S')}] acao {acao} em {d['nome']}: código {res['codigo']}")

        if self._em_segundo_plano:
            threading.Thread(target=trabalho, daemon=True).start()
        else:
            trabalho()
        return {"ok": True, "acao_id": acao_id}

    # ----------------------------------------------------------- saída
    def disponibilidade(self, id_: str, janela_s: float, criado_em: float) -> float | None:
        agora = self.agora()
        inicio = max(agora - janela_s, criado_em)
        total = agora - inicio
        if total < 60:
            return None
        fora = 0.0
        for a, f in self.banco.intervalos_offline(id_, inicio):
            fora += max(0.0, min(f or agora, agora) - max(a, inicio))
        return round(max(0.0, 100 * (1 - fora / total)), 2)

    def visao(self) -> list[dict]:
        agora = self.agora()
        abertos: dict[str, list[dict]] = {}
        for p in self.banco.problemas(so_abertos=True, limite=1000):
            abertos.setdefault(p["dispositivo_id"], []).append(p)
        saida = []
        for d in self.banco.dispositivos():
            probs = abertos.get(d["id"], [])
            status = regras.status_de(probs)
            rel = d["relatorio"] or {}
            offline = next((p for p in probs if p["codigo"] == "offline"), None)
            saida.append({
                "id": d["id"], "nome": d["nome"], "tipo": d["tipo"], "host": d["host"], "porta": d["porta"],
                "so": d["so"], "status": status, "ultimo_contato": d["ultimo_contato"],
                "ligado_ha_s": None if offline else rel.get("uptime_s"),
                "offline_ha_s": int(agora - offline["aberto_em"]) if offline else None,
                "disponibilidade_24h": self.disponibilidade(d["id"], 86400, d["criado_em"]),
                "disponibilidade_7d": self.disponibilidade(d["id"], 7 * 86400, d["criado_em"]),
                "latencia_ms": d["latencia_ms"], "relatorio": rel or None,
                "ssh": d["id"] in self.ssh_config and bool(self.admin_token),
                "problemas": [{"codigo": p["codigo"], "gravidade": p["gravidade"], "detalhe": p["detalhe"],
                               "desde": p["aberto_em"]} for p in probs],
            })
        return saida


def criar_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NetPulse"

        def log_message(self, formato, *args):  # silencia o log padrão
            pass

        def _json(self, codigo: int, dados):
            corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def _autorizado(self) -> bool:
            enviado = self.headers.get("Authorization", "")
            return hmac.compare_digest(enviado.encode(), f"Bearer {app.token}".encode())

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                corpo = (PASTA_WEB / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(corpo)))
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(corpo)
            elif url.path == "/api/dispositivos":
                self._json(200, app.visao())
            elif url.path == "/api/acoes":
                self._json(200, app.banco.acoes(limite=30))
            elif url.path == "/api/problemas":
                abertos = parse_qs(url.query).get("abertos", ["0"])[0] == "1"
                self._json(200, app.banco.problemas(so_abertos=abertos, limite=100))
            elif m := re.fullmatch(r"/api/dispositivos/([A-Za-z0-9-]+)/historico", url.path):
                try:
                    horas = min(max(float(parse_qs(url.query).get("horas", ["24"])[0]), 0.1), 720)
                except ValueError:
                    horas = 24
                self._json(200, app.banco.historico(m.group(1), app.agora() - horas * 3600))
            else:
                self._json(404, {"erro": "não encontrado"})

        def _descartar_corpo(self):
            """Lê (e joga fora) o corpo antes de responder um erro. Se o servidor responde e fecha com
            dados ainda não lidos, o Windows reseta a conexão e o agente nunca vê o 401/400."""
            try:
                restante = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                restante = 0
            if restante > MAX_CORPO:
                self.close_connection = True
                return
            while restante > 0:
                pedaco = self.rfile.read(min(restante, 65536))
                if not pedaco:
                    break
                restante -= len(pedaco)

        def _admin(self) -> bool:
            if not app.admin_token:
                return False
            enviado = self.headers.get("Authorization", "")
            return hmac.compare_digest(enviado.encode(), f"Bearer {app.admin_token}".encode())

        def _ler_json(self):
            tamanho = int(self.headers.get("Content-Length", "0"))
            if not 0 < tamanho <= MAX_CORPO:
                raise ValueError("corpo inválido ou grande demais")
            payload = json.loads(self.rfile.read(tamanho))
            if not isinstance(payload, dict):
                raise ValueError("esperado um objeto JSON")
            return payload

        def do_POST(self):
            caminho = urlparse(self.path).path
            m = re.fullmatch(r"/api/dispositivos/([A-Za-z0-9-]+)/acao", caminho)
            if m:
                if not self._admin():
                    self._descartar_corpo()
                    return self._json(401, {"erro": "token de administrador inválido ou ações remotas desligadas"})
                try:
                    acao = self._ler_json().get("acao")
                    return self._json(200, app.solicitar_acao(m.group(1), acao if isinstance(acao, str) else ""))
                except (ValueError, json.JSONDecodeError) as e:
                    return self._json(400, {"erro": str(e)})
            if caminho != "/api/relatorio":
                self._descartar_corpo()
                return self._json(404, {"erro": "não encontrado"})
            if not self._autorizado():
                self._descartar_corpo()
                return self._json(401, {"erro": "token inválido"})
            try:
                self._json(200, app.processar_relatorio(self._ler_json()))
            except (ValueError, json.JSONDecodeError) as e:
                self._json(400, {"erro": str(e)})

    return Handler


def criar_servidor(app: App, host: str = "127.0.0.1", porta: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, porta), criar_handler(app))


def servir(app: App, host: str, porta: int, intervalo_s: int = 30):
    servidor = criar_servidor(app, host, porta)
    parar = threading.Event()

    def laco():
        while not parar.wait(intervalo_s):
            try:
                app.verificar()
            except Exception as e:  # noqa: BLE001 - o monitor não pode morrer por um erro de ciclo
                app.log(f"erro no ciclo de verificação: {type(e).__name__}: {e}")

    threading.Thread(target=laco, daemon=True).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()
        servidor.server_close()
