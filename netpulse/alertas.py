"""Avisos: Telegram, webhook genérico e e-mail (SMTP). Falha de envio nunca derruba o monitor."""

from __future__ import annotations

import json
import smtplib
import ssl
import threading
import time
import urllib.request
from email.message import EmailMessage

ICONES = {"CRÍTICO": "🔴", "ALERTA": "🟠", "INFO": "🔵"}


def formatar(evento: dict) -> str:
    if evento["tipo"] == "resolvido":
        return f"✅ Resolvido em {evento['dispositivo']}: {evento['detalhe']}"
    icone = ICONES.get(evento["gravidade"], "⚠️")
    return f"{icone} {evento['gravidade']} em {evento['dispositivo']}: {evento['detalhe']}"


def _post_json(url: str, corpo: dict, timeout: float = 8) -> None:
    req = urllib.request.Request(url, data=json.dumps(corpo).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def _enviar_email(cfg: dict, assunto: str, texto: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["de"], cfg["para"], assunto
    msg.set_content(texto)
    with smtplib.SMTP(cfg["servidor"], cfg.get("porta", 587), timeout=10) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if cfg.get("usuario"):
            smtp.login(cfg["usuario"], cfg["senha"])
        smtp.send_message(msg)


class Notificador:
    """Lê ``alertas.json``. Exemplo: {"telegram": {"token": "...", "chat_id": "..."},
    "webhook": {"url": "..."}, "email": {"servidor": "...", "de": "...", "para": "..."}}"""

    def __init__(self, config: dict | None = None, post=_post_json, email=_enviar_email, log=print):
        self.config, self._post, self._email, self._log = config or {}, post, email, log

    @property
    def ativo(self) -> bool:
        return any(self.config.get(c) for c in ("telegram", "webhook", "email"))

    def notificar(self, evento: dict) -> list[str]:
        """Envia por todos os canais configurados; devolve a lista dos que falharam."""
        texto, falhas = formatar(evento), []
        envios = []
        if self.config.get("telegram"):
            t = self.config["telegram"]
            envios.append(("telegram", lambda: self._post(
                f"https://api.telegram.org/bot{t['token']}/sendMessage",
                {"chat_id": t["chat_id"], "text": texto})))
        if self.config.get("webhook"):
            envios.append(("webhook", lambda: self._post(self.config["webhook"]["url"], {**evento, "texto": texto})))
        if self.config.get("email"):
            envios.append(("email", lambda: self._email(self.config["email"], f"NetPulse: {texto[:80]}", texto)))
        for canal, enviar in envios:
            try:
                enviar()
            except Exception as e:  # noqa: BLE001 - qualquer falha de rede/config vira só um log
                falhas.append(canal)
                self._log(f"[{time.strftime('%H:%M:%S')}] falha ao avisar por {canal}: {type(e).__name__}")
        return falhas

    def notificar_em_segundo_plano(self, eventos: list[dict]) -> None:
        if eventos and self.ativo:
            threading.Thread(target=lambda: [self.notificar(e) for e in eventos], daemon=True).start()
