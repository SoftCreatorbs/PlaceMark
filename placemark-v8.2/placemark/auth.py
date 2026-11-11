"""Cadastro de aluno, login, logout e proteção de rotas por papel."""
import functools
import re
import secrets
import sqlite3
import time

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import seguranca
from db import get_db

bp = Blueprint("auth", __name__)

# Hash "falso" para o login demorar o mesmo tempo com e-mail existente ou não
# (assim um atacante não descobre quais e-mails têm conta pelo tempo de resposta)
_HASH_FALSO = generate_password_hash("placemark-hash-de-tempo-constante")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------- Proteção CSRF ----------
def gerar_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


# Rotas chamadas por outros servidores (não por formulários do site). Elas se protegem de outro jeito:
# o webhook do Mercado Pago nunca confia no aviso e confirma tudo consultando a API.
CSRF_ISENTOS = {"mp.webhook", "saque.webhook"}


def checar_csrf():
    if request.method == "POST" and request.endpoint not in CSRF_ISENTOS:
        token = request.form.get("csrf_token", "")
        esperado = session.get("csrf", "")
        # Os dois precisam existir E ser iguais (token vazio nunca vale)
        if not token or not esperado or not secrets.compare_digest(token, esperado):
            abort(400, "Formulário expirado. Recarregue a página e tente de novo.")


# ---------- Usuário logado ----------
@bp.before_app_request
def carregar_usuario():
    uid = session.get("usuario_id")
    g.usuario = None
    g.empresa = None
    if uid is not None:
        db = get_db()
        g.usuario = db.execute("SELECT * FROM usuarios WHERE id = ? AND ativo = 1", (uid,)).fetchone()
        if g.usuario is None:
            session.clear()  # conta bloqueada ou apagada: derruba a sessão
        elif g.usuario["papel"] == "admin" and time.time() - session.get("criado", 0) > seguranca.SESSAO_ADMIN_HORAS * 3600:
            session.clear()  # sessão de admin dura no máximo 12 horas
            g.usuario = None
            flash("Por segurança, entre de novo (a sessão de admin expira em 12 horas).", "aviso")
        elif g.usuario["papel"] in ("empresa", "admin"):
            g.empresa = db.execute("SELECT * FROM empresas WHERE usuario_id = ?", (uid,)).fetchone()


def login_obrigatorio(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.usuario is None:
            flash("Entre na sua conta para continuar.", "aviso")
            return redirect(url_for("auth.login", proximo=request.path))
        return view(**kwargs)
    return wrapped


def papel_obrigatorio(*papeis):
    def decorator(view):
        @functools.wraps(view)
        @login_obrigatorio
        def wrapped(**kwargs):
            if g.usuario["papel"] not in papeis:
                abort(403)
            return view(**kwargs)
        return wrapped
    return decorator


def destino_inicial(usuario):
    return {
        "admin": url_for("admin.dashboard"),
        "empresa": url_for("empresa.dashboard"),
    }.get(usuario["papel"], url_for("aluno.meus_cursos"))


# ---------- Validação compartilhada (aluno e empresa) ----------
def validar_conta(form):
    dados = {k: form.get(k, "").strip() for k in ("nome", "email", "telefone")}
    dados["email"] = dados["email"].lower()
    senha, confirma = form.get("senha", ""), form.get("confirma", "")
    erros = validar_senha(senha, confirma, dados["email"])
    if len(dados["nome"]) < 3:
        erros.append("Informe seu nome completo.")
    if not EMAIL_RE.match(dados["email"]):
        erros.append("E-mail inválido.")
    if not erros and get_db().execute("SELECT 1 FROM usuarios WHERE email = ?", (dados["email"],)).fetchone():
        erros.append("Esse e-mail já está cadastrado.")
    return dados, senha, erros


def validar_senha(senha, confirma, email=""):
    erros = []
    if len(senha) < 8 or not re.search(r"\d", senha) or not re.search(r"[A-Za-z]", senha):
        erros.append("A senha precisa de pelo menos 8 caracteres, com letras e números.")
    elif len(senha) > 128:
        erros.append("A senha pode ter no máximo 128 caracteres.")
    elif senha.lower() in seguranca.SENHAS_COMUNS or (email and email.split("@")[0].lower() in senha.lower() and len(email.split("@")[0]) >= 4):
        erros.append("Essa senha é muito fácil de adivinhar. Escolha outra.")
    if senha != confirma:
        erros.append("As senhas não coincidem.")
    return erros


def criar_usuario(dados, senha, papel):
    return get_db().execute(
        "INSERT INTO usuarios (nome, email, senha_hash, telefone, papel) VALUES (?, ?, ?, ?, ?)",
        (dados["nome"], dados["email"], generate_password_hash(senha), dados["telefone"], papel),
    ).lastrowid


def entrar(usuario_id):
    session.clear()  # sessão nova a cada login (evita "sequestro de sessão")
    session["usuario_id"] = usuario_id
    session["criado"] = int(time.time())
    session.permanent = True


# ---------- Rotas ----------
@bp.route("/cadastro", methods=("GET", "POST"))
def cadastro():
    if g.usuario:
        return redirect(destino_inicial(g.usuario))
    dados = {}
    if request.method == "POST":
        espera = seguranca.bloqueado("cadastro-ip", seguranca.ip())
        if espera:
            flash(f"Muitas contas criadas daqui. Aguarde {espera} minuto(s).", "erro")
            return render_template("cadastro.html", dados=dados), 429
        dados, senha, erros = validar_conta(request.form)
        if not erros:
            seguranca.registrar_tentativa("cadastro-ip", seguranca.ip())
            try:
                uid = criar_usuario(dados, senha, "aluno")
                get_db().commit()
            except sqlite3.IntegrityError:
                erros.append("Esse e-mail já está cadastrado.")
            else:
                entrar(uid)
                seguranca.registrar("cadastro", "Conta de aluno criada", uid)
                flash("Conta criada! Bem-vindo(a).", "sucesso")
                return redirect(seguranca.destino_interno(request.args.get("proximo"), url_for("cursos")))
        for e in erros:
            flash(e, "erro")
    return render_template("cadastro.html", dados=dados)


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.usuario:
        return redirect(destino_inicial(g.usuario))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        senha = request.form.get("senha", "")[:128]
        meu_ip = seguranca.ip()

        espera = seguranca.bloqueado("login-email", email) or seguranca.bloqueado("login-ip", meu_ip)
        if espera:
            seguranca.registrar("login_bloqueado", email)
            flash(f"Muitas tentativas erradas. Por segurança, aguarde {espera} minuto(s) e tente de novo.", "erro")
            return render_template("login.html"), 429

        usuario = get_db().execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        senha_ok = check_password_hash(usuario["senha_hash"] if usuario else _HASH_FALSO, senha)

        if usuario is None or not senha_ok:
            seguranca.registrar_tentativa("login-email", email)
            seguranca.registrar_tentativa("login-ip", meu_ip)
            seguranca.registrar("login_falhou", email, usuario["id"] if usuario else None)
            flash("E-mail ou senha incorretos.", "erro")
        elif not usuario["ativo"]:
            flash("Esta conta está bloqueada. Fale com o suporte.", "erro")
        else:
            seguranca.zerar_tentativas("login-email", email)
            entrar(usuario["id"])
            seguranca.registrar("login", usuario["papel"], usuario["id"])
            flash(f"Bem-vindo(a), {usuario['nome'].split()[0]}!", "sucesso")
            return redirect(seguranca.destino_interno(request.args.get("proximo"), destino_inicial(usuario)))

    return render_template("login.html")


@bp.route("/sair", methods=("POST",))
def sair():
    if g.usuario:
        seguranca.registrar("logout")
    session.clear()
    flash("Você saiu da sua conta.", "sucesso")
    return redirect(url_for("inicio"))


@bp.route("/conta/senha", methods=("GET", "POST"))
@login_obrigatorio
def trocar_senha():
    if request.method == "POST":
        espera = seguranca.bloqueado("senha-usuario", g.usuario["id"])
        if espera:
            flash(f"Muitas tentativas. Aguarde {espera} minuto(s).", "erro")
            return render_template("conta/senha.html"), 429
        atual = request.form.get("atual", "")
        nova, confirma = request.form.get("senha", ""), request.form.get("confirma", "")
        if not check_password_hash(g.usuario["senha_hash"], atual):
            seguranca.registrar_tentativa("senha-usuario", g.usuario["id"])
            flash("A senha atual está incorreta.", "erro")
        elif atual == nova:
            flash("A nova senha precisa ser diferente da atual.", "erro")
        else:
            erros = validar_senha(nova, confirma, g.usuario["email"])
            if erros:
                for e in erros:
                    flash(e, "erro")
            else:
                conexao = get_db()
                conexao.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                                (generate_password_hash(nova), g.usuario["id"]))
                conexao.commit()
                seguranca.registrar("senha_alterada")
                entrar(g.usuario["id"])  # nova sessão
                flash("Senha alterada com sucesso.", "sucesso")
                return redirect(destino_inicial(g.usuario))
    return render_template("conta/senha.html")
