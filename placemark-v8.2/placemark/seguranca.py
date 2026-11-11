"""Segurança da Placemark.

- Chave secreta forte gerada automaticamente (assina os cookies de login)
- Bloqueio de força bruta (muitas tentativas de login/cadastro = espera)
- Cabeçalhos de proteção do navegador (CSP com nonce, anti-clickjacking etc.)
- Registro de atividades (quem fez o quê, de qual IP)
- Backup diário automático do banco
"""
import os
import secrets
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from flask import g, request, session, url_for

import db

# ---------------- Regras de bloqueio (tentativas, janela em minutos) ----------------
LIMITES = {
    "login-email": (5, 15),     # 5 senhas erradas para o mesmo e-mail -> espera 15 min
    "login-ip": (20, 15),       # 20 erros do mesmo IP (qualquer e-mail) -> espera 15 min
    "cadastro-ip": (8, 60),     # 8 contas novas por IP por hora
    "instalar-ip": (10, 15),
    "senha-usuario": (5, 15),   # troca de senha com senha atual errada
    "webhook-ip": (300, 10),    # avisos do Mercado Pago (bem mais do que o normal) por IP
    "saque-senha": (5, 15),     # senha errada ao confirmar um saque
}

SENHAS_COMUNS = {
    "12345678", "123456789", "1234567890", "senha123", "senha1234", "senha12345", "password1",
    "password123", "qwerty123", "abc12345", "abcd1234", "a1234567", "12345678a", "11111111a",
    "admin123", "admin1234", "mudar123", "brasil123", "teste123", "teste1234", "iloveyou1",
    "placemark1", "placemark123", "102030405060", "aa123456", "q1w2e3r4", "1q2w3e4r", "qwer1234",
}

SESSAO_ADMIN_HORAS = 12


# ---------------- Chave secreta ----------------
def chave_secreta(pasta_dados):
    """Usa SECRET_KEY do ambiente; senão cria uma chave aleatória e guarda junto do banco."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    arquivo = Path(pasta_dados) / ".chave_secreta"
    if arquivo.exists():
        return arquivo.read_text().strip()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    chave = secrets.token_hex(32)
    arquivo.write_text(chave)
    try:
        os.chmod(arquivo, 0o600)  # só o dono do arquivo consegue ler (Linux/Mac)
    except OSError:
        pass
    return chave


# ---------------- IP de quem está acessando ----------------
def ip():
    return request.remote_addr or "?"


# ---------------- Força bruta ----------------
def _limpar_antigas(conexao):
    conexao.execute("DELETE FROM tentativas WHERE criado_em < datetime('now', 'localtime', '-1 day')")


def bloqueado(tipo, valor):
    """Retorna quantos minutos faltam se estiver bloqueado; 0 se pode tentar."""
    limite, janela = LIMITES[tipo]
    linha = db.get_db().execute(
        f"""SELECT COUNT(*) AS n, MIN(criado_em) AS primeira FROM tentativas
            WHERE chave = ? AND criado_em > datetime('now', 'localtime', '-{int(janela)} minutes')""",
        (f"{tipo}:{valor}",)).fetchone()
    if linha["n"] < limite:
        return 0
    faltam = db.get_db().execute(
        f"SELECT CAST((julianday(?, '+{int(janela)} minutes') - julianday('now', 'localtime')) * 1440 AS INTEGER) + 1",
        (linha["primeira"],)).fetchone()[0]
    return max(1, faltam)


def registrar_tentativa(tipo, valor):
    conexao = db.get_db()
    conexao.execute("INSERT INTO tentativas (chave) VALUES (?)", (f"{tipo}:{valor}",))
    _limpar_antigas(conexao)
    conexao.commit()


def zerar_tentativas(tipo, valor):
    conexao = db.get_db()
    conexao.execute("DELETE FROM tentativas WHERE chave = ?", (f"{tipo}:{valor}",))
    conexao.commit()


# ---------------- Registro de atividades ----------------
def registrar(acao, detalhe="", usuario_id=None):
    if usuario_id is None and getattr(g, "usuario", None):
        usuario_id = g.usuario["id"]
    conexao = db.get_db()
    conexao.execute("INSERT INTO auditoria (usuario_id, acao, detalhe, ip) VALUES (?, ?, ?, ?)",
                    (usuario_id, acao, detalhe[:300], ip()))
    conexao.commit()


# ---------------- Redirecionamento seguro ----------------
def voltar(padrao):
    """Volta para a página anterior SÓ se ela for deste site (evita redirecionar para sites falsos)."""
    anterior = request.referrer or ""
    if anterior:
        u = urlparse(anterior)
        if u.netloc == request.host and u.scheme in ("http", "https"):
            return anterior
    return padrao


def destino_interno(proximo, padrao):
    """Aceita apenas caminhos internos como /cursos/1 (nunca //site.com ou https://site.com)."""
    if proximo and proximo.startswith("/") and not proximo.startswith(("//", "/\\")):
        return proximo
    return padrao


# ---------------- Backup diário ----------------
def backup(caminho_banco, manter=14, forcar=False):
    """Copia o banco para PlacemarkDados/backups (um por dia, guarda os últimos 14)."""
    pasta = Path(caminho_banco).parent / "backups"
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / f"placemark-{date.today().isoformat()}.sqlite"
    if destino.exists() and not forcar:
        return None
    origem = sqlite3.connect(caminho_banco)
    copia = sqlite3.connect(destino)
    try:
        origem.backup(copia)  # cópia segura mesmo com o site ligado
    finally:
        copia.close()
        origem.close()
    for antigo in sorted(pasta.glob("placemark-*.sqlite"))[:-manter]:
        antigo.unlink()
    return destino


def listar_backups(caminho_banco):
    pasta = Path(caminho_banco).parent / "backups"
    if not pasta.exists():
        return []
    return [(p.name, round(p.stat().st_size / 1024)) for p in sorted(pasta.glob("placemark-*.sqlite"), reverse=True)]


# ---------------- Cabeçalhos de proteção ----------------
def _cabecalhos(resposta):
    nonce = getattr(g, "csp_nonce", "")
    resposta.headers["Content-Security-Policy"] = "; ".join([
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",                     # só scripts do próprio site
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data:",
        "frame-src https://www.youtube-nocookie.com https://player.vimeo.com",  # vídeos das aulas
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",                                  # ninguém coloca o site num iframe
    ])
    resposta.headers["X-Frame-Options"] = "DENY"
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resposta.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    resposta.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if os.environ.get("PRODUCAO") == "1":
        resposta.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Páginas de quem está logado não ficam guardadas no navegador (botão "voltar" depois de sair)
    if getattr(g, "usuario", None) and not request.path.startswith("/static/"):
        resposta.headers["Cache-Control"] = "no-store"
    resposta.headers.pop("Server", None)
    return resposta


def init_app(app):
    app.after_request(_cabecalhos)

    @app.before_request
    def _nonce():
        g.csp_nonce = secrets.token_urlsafe(16)

    app.jinja_env.globals["csp_nonce"] = lambda: getattr(g, "csp_nonce", "")

    # Atrás de um proxy (Render, Railway, Cloudflare): pega o IP real do visitante
    if os.environ.get("PROXY") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    try:
        backup(app.config["DATABASE"])
    except Exception as erro:  # backup nunca pode derrubar o site
        app.logger.warning("Backup automático falhou: %s", erro)
