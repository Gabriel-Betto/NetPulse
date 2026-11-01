"""Armazenamento em SQLite: dispositivos, histórico de métricas e problemas."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS dispositivos (
    id TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    tipo TEXT NOT NULL CHECK (tipo IN ('agente', 'sonda')),
    host TEXT,
    porta INTEGER,
    so TEXT,
    ultimo_contato REAL,
    ultimo_relatorio TEXT,
    latencia_ms REAL,
    falhas INTEGER NOT NULL DEFAULT 0,
    criado_em REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS metricas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispositivo_id TEXT NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    cpu REAL, ram REAL, disco REAL, latencia_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_metricas ON metricas (dispositivo_id, ts);
CREATE TABLE IF NOT EXISTS problemas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispositivo_id TEXT NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    codigo TEXT NOT NULL,
    gravidade TEXT NOT NULL,
    detalhe TEXT NOT NULL,
    aberto_em REAL NOT NULL,
    fechado_em REAL
);
CREATE INDEX IF NOT EXISTS idx_problemas ON problemas (dispositivo_id, fechado_em);
CREATE TABLE IF NOT EXISTS acoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispositivo_id TEXT NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    acao TEXT NOT NULL,
    inicio REAL NOT NULL,
    fim REAL,
    codigo INTEGER,
    saida TEXT
);
"""


class Banco:
    def __init__(self, caminho: str | Path = ":memory:"):
        if str(caminho) != ":memory:":
            Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(caminho), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._lock:
            self._con.executescript(ESQUEMA)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.fechar()

    def fechar(self):
        with self._lock:
            self._con.close()

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._con.execute(sql, params)
            self._con.commit()
            return cur

    def _linhas(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._con.execute(sql, params).fetchall()]

    # ------------------------------------------------------ dispositivos
    def registrar_relatorio(self, id_: str, nome: str, so: str, agora: float, relatorio: dict):
        with self._lock:
            existe = self._con.execute("SELECT 1 FROM dispositivos WHERE id = ?", (id_,)).fetchone()
            if existe:
                self._con.execute(
                    "UPDATE dispositivos SET nome=?, so=?, ultimo_contato=?, ultimo_relatorio=? WHERE id=?",
                    (nome, so, agora, json.dumps(relatorio), id_))
            else:
                self._con.execute(
                    "INSERT INTO dispositivos (id, nome, tipo, so, ultimo_contato, ultimo_relatorio, criado_em)"
                    " VALUES (?, ?, 'agente', ?, ?, ?, ?)",
                    (id_, nome, so, agora, json.dumps(relatorio), agora))
            self._con.commit()

    def adicionar_sonda(self, nome: str, host: str, agora: float, porta: int | None = None) -> str:
        with self._lock:
            ja = self._con.execute(
                "SELECT id FROM dispositivos WHERE tipo='sonda' AND host=? AND IFNULL(porta,0)=?",
                (host, porta or 0)).fetchone()
            if ja:
                return ja["id"]
            id_ = uuid.uuid4().hex[:12]
            self._con.execute(
                "INSERT INTO dispositivos (id, nome, tipo, host, porta, criado_em) VALUES (?, ?, 'sonda', ?, ?, ?)",
                (id_, nome, host, porta, agora))
            self._con.commit()
            return id_

    def registrar_sonda(self, id_: str, agora: float, latencia_ms: float | None):
        """Guarda o resultado de um teste; devolve o número de falhas seguidas."""
        with self._lock:
            if latencia_ms is None:
                self._con.execute("UPDATE dispositivos SET falhas = falhas + 1 WHERE id = ?", (id_,))
            else:
                self._con.execute(
                    "UPDATE dispositivos SET falhas = 0, ultimo_contato = ?, latencia_ms = ? WHERE id = ?",
                    (agora, latencia_ms, id_))
            self._con.commit()
            return self._con.execute("SELECT falhas FROM dispositivos WHERE id = ?", (id_,)).fetchone()["falhas"]

    def dispositivos(self) -> list[dict]:
        linhas = self._linhas("SELECT * FROM dispositivos ORDER BY nome COLLATE NOCASE")
        for linha in linhas:
            bruto = linha.pop("ultimo_relatorio")
            linha["relatorio"] = json.loads(bruto) if bruto else None
        return linhas

    def dispositivo(self, id_: str) -> dict | None:
        return next((d for d in self.dispositivos() if d["id"] == id_), None)

    def remover(self, id_: str) -> bool:
        return self._exec("DELETE FROM dispositivos WHERE id = ?", (id_,)).rowcount > 0

    # ----------------------------------------------------------- métricas
    def registrar_metrica(self, id_: str, ts: float, cpu=None, ram=None, disco=None, latencia_ms=None):
        self._exec("INSERT INTO metricas (dispositivo_id, ts, cpu, ram, disco, latencia_ms) VALUES (?,?,?,?,?,?)",
                   (id_, ts, cpu, ram, disco, latencia_ms))

    def historico(self, id_: str, desde: float) -> list[dict]:
        return self._linhas(
            "SELECT ts, cpu, ram, disco, latencia_ms FROM metricas WHERE dispositivo_id=? AND ts>=? ORDER BY ts",
            (id_, desde))

    def limpar_antigos(self, antes_de: float) -> int:
        n = self._exec("DELETE FROM metricas WHERE ts < ?", (antes_de,)).rowcount
        n += self._exec("DELETE FROM problemas WHERE fechado_em IS NOT NULL AND fechado_em < ?", (antes_de,)).rowcount
        return n

    # ---------------------------------------------------------- problemas
    def abertos_de(self, id_: str) -> dict[str, dict]:
        linhas = self._linhas("SELECT * FROM problemas WHERE dispositivo_id=? AND fechado_em IS NULL", (id_,))
        return {linha["codigo"]: linha for linha in linhas}

    def abrir_problema(self, id_: str, codigo: str, gravidade: str, detalhe: str, ts: float):
        self._exec("INSERT INTO problemas (dispositivo_id, codigo, gravidade, detalhe, aberto_em) VALUES (?,?,?,?,?)",
                   (id_, codigo, gravidade, detalhe, ts))

    def fechar_problema(self, problema_id: int, ts: float):
        self._exec("UPDATE problemas SET fechado_em = ? WHERE id = ?", (ts, problema_id))

    def atualizar_detalhe(self, problema_id: int, detalhe: str):
        self._exec("UPDATE problemas SET detalhe = ? WHERE id = ?", (detalhe, problema_id))

    def intervalos_offline(self, id_: str, desde: float) -> list[tuple[float, float | None]]:
        linhas = self._linhas(
            "SELECT aberto_em, fechado_em FROM problemas WHERE dispositivo_id=? AND codigo='offline' "
            "AND (fechado_em IS NULL OR fechado_em > ?)", (id_, desde))
        return [(l["aberto_em"], l["fechado_em"]) for l in linhas]

    def problemas(self, so_abertos: bool = False, limite: int = 200) -> list[dict]:
        filtro = "WHERE p.fechado_em IS NULL" if so_abertos else ""
        return self._linhas(
            "SELECT p.*, d.nome AS dispositivo FROM problemas p JOIN dispositivos d ON d.id = p.dispositivo_id "
            f"{filtro} ORDER BY p.aberto_em DESC LIMIT ?", (limite,))

    # ------------------------------------------------------------- ações
    def iniciar_acao(self, id_: str, acao: str, ts: float) -> int:
        return self._exec("INSERT INTO acoes (dispositivo_id, acao, inicio) VALUES (?,?,?)", (id_, acao, ts)).lastrowid

    def finalizar_acao(self, acao_id: int, ts: float, codigo: int, saida: str):
        self._exec("UPDATE acoes SET fim=?, codigo=?, saida=? WHERE id=?", (ts, codigo, saida, acao_id))

    def acao_em_andamento(self, id_: str) -> bool:
        return bool(self._linhas("SELECT 1 FROM acoes WHERE dispositivo_id=? AND fim IS NULL", (id_,)))

    def acoes(self, id_: str | None = None, limite: int = 30) -> list[dict]:
        filtro, params = ("WHERE a.dispositivo_id = ?", (id_, limite)) if id_ else ("", (limite,))
        return self._linhas(
            "SELECT a.*, d.nome AS dispositivo FROM acoes a JOIN dispositivos d ON d.id = a.dispositivo_id "
            f"{filtro} ORDER BY a.inicio DESC LIMIT ?", params)
