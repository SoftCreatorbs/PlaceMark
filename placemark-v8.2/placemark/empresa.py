"""Painel da empresa: cadastro, cursos, aulas e vendas."""
import functools
import re
from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import db
import seguranca
from auth import criar_usuario, entrar, papel_obrigatorio, validar_conta
from util import CATEGORIAS, para_centavos

bp = Blueprint("empresa", __name__, url_prefix="/empresa")


def garantir_loja_oficial():
    """Admin que abre o painel de vendas ganha a loja oficial da plataforma (sem plano)."""
    conexao = db.get_db()
    loja = conexao.execute("SELECT * FROM empresas WHERE oficial = 1 LIMIT 1").fetchone()
    if loja is None:
        conexao.execute(
            """INSERT INTO empresas (usuario_id, nome_fantasia, documento, descricao, status, oficial)
               VALUES (?, 'Placemark Oficial', '—', 'Cursos oficiais da Placemark.', 'aprovada', 1)""",
            (g.usuario["id"],))
        conexao.commit()
        loja = conexao.execute("SELECT * FROM empresas WHERE oficial = 1 LIMIT 1").fetchone()
    return loja


def empresa_aprovada(view):
    """Vendedores aprovados (e o admin, na loja oficial). Os outros veem a tela de status."""
    @functools.wraps(view)
    @papel_obrigatorio("empresa", "admin")
    def wrapped(**kwargs):
        if g.usuario["papel"] == "admin":
            g.empresa = garantir_loja_oficial()  # toda a equipe gerencia a mesma loja oficial
        if g.empresa is None or g.empresa["status"] != "aprovada":
            return render_template("empresa/aguardando.html")
        g.plano = db.plano_da_empresa(g.empresa)
        g.vagas_usadas = db.cursos_ocupando_vaga(g.empresa["id"])
        return view(**kwargs)
    return wrapped


def curso_da_empresa(curso_id):
    c = db.get_db().execute(
        "SELECT * FROM cursos WHERE id = ? AND empresa_id = ?", (curso_id, g.empresa["id"])
    ).fetchone()
    if c is None:
        abort(404)
    return c


# ---------- Cadastro ----------
@bp.route("/cadastro", methods=("GET", "POST"))
def cadastro():
    if g.usuario:
        flash("Saia da conta atual para cadastrar uma empresa.", "aviso")
        return redirect(url_for("vender"))
    dados = {}
    if request.method == "POST":
        espera = seguranca.bloqueado("cadastro-ip", seguranca.ip())
        if espera:
            flash(f"Muitas contas criadas daqui. Aguarde {espera} minuto(s).", "erro")
            return redirect(url_for("empresa.cadastro"))
        dados, senha, erros = validar_conta(request.form)
        dados["nome_fantasia"] = request.form.get("nome_fantasia", "").strip()
        dados["documento"] = request.form.get("documento", "").strip()
        dados["descricao"] = request.form.get("descricao", "").strip()
        digitos = re.sub(r"\D", "", dados["documento"])
        if len(dados["nome_fantasia"]) < 2:
            erros.append("Informe o nome da empresa.")
        if len(digitos) not in (11, 14):
            erros.append("Informe um CNPJ (14 dígitos) ou CPF (11 dígitos) válido.")
        if not erros:
            conexao = db.get_db()
            uid = criar_usuario(dados, senha, "empresa")
            conexao.execute(
                "INSERT INTO empresas (usuario_id, nome_fantasia, documento, descricao) VALUES (?, ?, ?, ?)",
                (uid, dados["nome_fantasia"], dados["documento"], dados["descricao"]),
            )
            conexao.commit()
            seguranca.registrar_tentativa("cadastro-ip", seguranca.ip())
            seguranca.registrar("cadastro_vendedor", dados["nome_fantasia"], uid)
            entrar(uid)
            flash("Cadastro enviado! Nossa equipe vai analisar sua empresa.", "sucesso")
            return redirect(url_for("empresa.dashboard"))
        for e in erros:
            flash(e, "erro")
    preco_minimo = db.get_db().execute("SELECT MIN(preco_mensal) FROM planos_vendedor WHERE ativo = 1").fetchone()[0]
    return render_template("empresa/cadastro.html", dados=dados, preco_minimo=preco_minimo)


# ---------- Painel ----------
@bp.route("/")
@empresa_aprovada
def dashboard():
    conexao = db.get_db()
    eid = g.empresa["id"]
    resumo = conexao.execute(
        """SELECT COUNT(*) AS vendas, COALESCE(SUM(valor_empresa), 0) AS receita,
                  COALESCE(SUM(CASE WHEN criado_em >= date('now', 'localtime', 'start of month') THEN valor_empresa END), 0) AS receita_mes
           FROM pedidos WHERE empresa_id = ? AND status = 'pago'""", (eid,)).fetchone()
    alunos = conexao.execute(
        """SELECT COUNT(*) FROM acessos x JOIN cursos c ON c.id = x.curso_id
           WHERE c.empresa_id = ? AND (x.expira_em IS NULL OR x.expira_em > datetime('now', 'localtime'))""",
        (eid,)).fetchone()[0]
    publicados = conexao.execute(
        "SELECT COUNT(*) FROM cursos WHERE empresa_id = ? AND status = 'publicado'", (eid,)).fetchone()[0]
    recentes = conexao.execute(
        """SELECT p.*, c.titulo, u.nome AS aluno_nome FROM pedidos p
           JOIN cursos c ON c.id = p.curso_id JOIN usuarios u ON u.id = p.aluno_id
           WHERE p.empresa_id = ? AND p.status = 'pago' ORDER BY p.id DESC LIMIT 5""", (eid,)).fetchall()
    import mercadopago as mp
    import saques
    return render_template("empresa/dashboard.html", resumo=resumo, alunos=alunos,
                           publicados=publicados, recentes=recentes, saldo=saques.saldo(eid),
                           carteira=mp.modo_carteira())


@bp.route("/cursos")
@empresa_aprovada
def cursos():
    lista = db.get_db().execute(
        """SELECT c.*, (SELECT COUNT(*) FROM aulas a WHERE a.curso_id = c.id) AS total_aulas,
                  (SELECT COUNT(*) FROM pedidos p WHERE p.curso_id = c.id AND p.status = 'pago') AS total_vendas
           FROM cursos c WHERE c.empresa_id = ? ORDER BY c.id DESC""", (g.empresa["id"],)).fetchall()
    return render_template("empresa/cursos.html", cursos=lista)


@bp.route("/cursos/novo", methods=("GET", "POST"))
@bp.route("/cursos/<int:curso_id>/editar", methods=("GET", "POST"))
@empresa_aprovada
def curso_form(curso_id=None):
    curso = curso_da_empresa(curso_id) if curso_id else None
    dados = dict(curso) if curso else {}

    if request.method == "POST":
        dados = {k: request.form.get(k, "").strip() for k in
                 ("titulo", "descricao", "categoria", "carga_horas", "preco_mensal", "preco_vitalicio")}
        erros = []
        if len(dados["titulo"]) < 3:
            erros.append("O título precisa de pelo menos 3 caracteres.")
        if len(dados["descricao"]) < 20:
            erros.append("Escreva uma descrição com pelo menos 20 caracteres.")
        if dados["categoria"] not in CATEGORIAS:
            erros.append("Escolha uma categoria.")
        try:
            horas = int(dados["carga_horas"] or 0)
            if horas < 0:
                raise ValueError
        except ValueError:
            erros.append("Carga horária inválida.")
            horas = 0
        try:
            mensal = para_centavos(dados["preco_mensal"]) if request.form.get("vende_mensal") else None
            vitalicio = para_centavos(dados["preco_vitalicio"]) if request.form.get("vende_vitalicio") else None
            if mensal is None and vitalicio is None:
                erros.append("Ative pelo menos um plano (mensal ou vitalício) e informe o preço.")
        except ValueError as e:
            erros.append(str(e) if "mínimo" in str(e) else "Preço inválido. Use o formato 49,90.")
            mensal = vitalicio = None

        if not erros:
            conexao = db.get_db()
            campos = (dados["titulo"], dados["descricao"], dados["categoria"], horas, mensal, vitalicio)
            if curso:
                # Curso rejeitado volta a ser rascunho ao ser editado
                novo_status = "rascunho" if curso["status"] == "rejeitado" else curso["status"]
                conexao.execute(
                    """UPDATE cursos SET titulo=?, descricao=?, categoria=?, carga_horas=?,
                       preco_mensal=?, preco_vitalicio=?, status=? WHERE id=?""",
                    campos + (novo_status, curso["id"]))
                conexao.commit()
                flash("Curso atualizado.", "sucesso")
                return redirect(url_for("empresa.cursos"))
            novo_id = conexao.execute(
                """INSERT INTO cursos (titulo, descricao, categoria, carga_horas, preco_mensal, preco_vitalicio, empresa_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""", campos + (g.empresa["id"],)).lastrowid
            conexao.commit()
            flash("Curso criado! Agora adicione as aulas.", "sucesso")
            return redirect(url_for("empresa.aulas", curso_id=novo_id))
        for e in erros:
            flash(e, "erro")
        dados["_mensal_on"] = bool(request.form.get("vende_mensal"))
        dados["_vitalicio_on"] = bool(request.form.get("vende_vitalicio"))

    return render_template("empresa/curso_form.html", curso=curso, dados=dados, categorias=CATEGORIAS)


@bp.route("/cursos/<int:curso_id>/enviar", methods=("POST",))
@empresa_aprovada
def enviar_analise(curso_id):
    curso = curso_da_empresa(curso_id)
    conexao = db.get_db()
    if curso["status"] not in ("rascunho", "rejeitado"):
        flash("Este curso já foi enviado.", "aviso")
    elif not conexao.execute("SELECT 1 FROM aulas WHERE curso_id = ?", (curso_id,)).fetchone():
        flash("Adicione pelo menos uma aula antes de enviar para análise.", "erro")
    elif g.empresa["oficial"]:
        # Cursos da loja oficial não passam por análise
        conexao.execute("UPDATE cursos SET status = 'publicado', motivo_rejeicao = NULL WHERE id = ?", (curso_id,))
        conexao.commit()
        flash("Curso publicado no catálogo.", "sucesso")
    elif g.plano is None:
        flash("Assine um plano para colocar cursos à venda.", "erro")
        return redirect(url_for("empresa.plano"))
    elif g.plano["limite_cursos"] is not None and g.vagas_usadas >= g.plano["limite_cursos"]:
        flash(f"Seu plano {g.plano['nome']} permite {g.plano['limite_cursos']} curso(s) à venda. Faça upgrade para vender mais.", "erro")
        return redirect(url_for("empresa.plano"))
    else:
        conexao.execute("UPDATE cursos SET status = 'em_analise', motivo_rejeicao = NULL WHERE id = ?", (curso_id,))
        conexao.commit()
        flash("Curso enviado para análise. Avisaremos quando for publicado.", "sucesso")
    return redirect(url_for("empresa.cursos"))


@bp.route("/cursos/<int:curso_id>/aulas", methods=("GET", "POST"))
@empresa_aprovada
def aulas(curso_id):
    curso = curso_da_empresa(curso_id)
    conexao = db.get_db()
    if request.method == "POST":
        titulo = request.form.get("titulo", "").strip()
        video = request.form.get("video_url", "").strip()
        conteudo = request.form.get("conteudo", "").strip()
        if len(titulo) < 3:
            flash("Dê um título para a aula.", "erro")
        elif video and not video.startswith("https://"):
            flash("O link do vídeo precisa começar com https://", "erro")
        else:
            ordem = conexao.execute("SELECT COALESCE(MAX(ordem), 0) + 1 FROM aulas WHERE curso_id = ?", (curso_id,)).fetchone()[0]
            conexao.execute("INSERT INTO aulas (curso_id, titulo, video_url, conteudo, ordem) VALUES (?, ?, ?, ?, ?)",
                            (curso_id, titulo, video, conteudo, ordem))
            conexao.commit()
            flash("Aula adicionada.", "sucesso")
            return redirect(url_for("empresa.aulas", curso_id=curso_id))
    lista = conexao.execute("SELECT * FROM aulas WHERE curso_id = ? ORDER BY ordem, id", (curso_id,)).fetchall()
    return render_template("empresa/aulas.html", curso=curso, aulas=lista)


@bp.route("/aulas/<int:aula_id>/excluir", methods=("POST",))
@empresa_aprovada
def excluir_aula(aula_id):
    conexao = db.get_db()
    aula = conexao.execute(
        """SELECT a.* FROM aulas a JOIN cursos c ON c.id = a.curso_id
           WHERE a.id = ? AND c.empresa_id = ?""", (aula_id, g.empresa["id"])).fetchone()
    if aula is None:
        abort(404)
    conexao.execute("DELETE FROM aulas WHERE id = ?", (aula_id,))
    conexao.commit()
    flash("Aula removida.", "sucesso")
    return redirect(url_for("empresa.aulas", curso_id=aula["curso_id"]))


@bp.route("/vendas")
@empresa_aprovada
def vendas():
    lista = db.get_db().execute(
        """SELECT p.*, c.titulo, u.nome AS aluno_nome FROM pedidos p
           JOIN cursos c ON c.id = p.curso_id JOIN usuarios u ON u.id = p.aluno_id
           WHERE p.empresa_id = ? AND p.status IN ('pago', 'reembolsado') ORDER BY p.id DESC""", (g.empresa["id"],)).fetchall()
    return render_template("empresa/vendas.html", vendas=lista)


# ---------- Plano do vendedor ----------
@bp.route("/plano")
@empresa_aprovada
def plano():
    if g.empresa["oficial"]:
        flash("A loja oficial não precisa de plano.", "aviso")
        return redirect(url_for("empresa.dashboard"))
    planos = db.get_db().execute("SELECT * FROM planos_vendedor WHERE ativo = 1 ORDER BY ordem, preco_mensal").fetchall()
    historico = db.get_db().execute(
        """SELECT a.*, p.nome FROM assinaturas_vendedor a JOIN planos_vendedor p ON p.id = a.plano_id
           WHERE a.empresa_id = ? ORDER BY a.id DESC LIMIT 12""", (g.empresa["id"],)).fetchall()
    return render_template("empresa/plano.html", planos=planos, historico=historico)


@bp.route("/plano/<int:plano_id>", methods=("GET", "POST"))
@empresa_aprovada
def assinar_plano(plano_id):
    if g.empresa["oficial"]:
        return redirect(url_for("empresa.dashboard"))
    conexao = db.get_db()
    novo = conexao.execute("SELECT * FROM planos_vendedor WHERE id = ? AND ativo = 1", (plano_id,)).fetchone()
    if novo is None:
        abort(404)
    # Não deixa fazer downgrade para um plano menor que os cursos já à venda
    if novo["limite_cursos"] is not None and g.vagas_usadas > novo["limite_cursos"]:
        flash(f"Você tem {g.vagas_usadas} cursos à venda ou em análise, e o plano {novo['nome']} permite "
              f"{novo['limite_cursos']}. Retire cursos do ar antes de trocar.", "erro")
        return redirect(url_for("empresa.plano"))

    if request.method == "POST":
        metodo = request.form.get("metodo")
        if metodo not in ("pix", "cartao"):
            flash("Escolha uma forma de pagamento.", "erro")
            return redirect(request.url)
        # ---- PAGAMENTO SIMULADO (trocar pelo gateway depois) ----
        base = datetime.now()
        mesmo_plano = g.plano and g.plano["id"] == novo["id"]
        if mesmo_plano and g.plano["expira_em"]:
            base = max(base, datetime.strptime(g.plano["expira_em"], "%Y-%m-%d %H:%M:%S"))  # renovação soma 30 dias
        expira = (base + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conexao.execute("UPDATE empresas SET plano_id = ?, plano_expira_em = ? WHERE id = ?",
                        (novo["id"], expira, g.empresa["id"]))
        conexao.execute("INSERT INTO assinaturas_vendedor (empresa_id, plano_id, valor, metodo) VALUES (?, ?, ?, ?)",
                        (g.empresa["id"], novo["id"], novo["preco_mensal"], metodo))
        conexao.commit()
        flash(f"Plano {novo['nome']} ativo! Agora você já pode colocar cursos à venda.", "sucesso")
        return redirect(url_for("empresa.cursos"))

    return render_template("empresa/assinar_plano.html", novo=novo)
