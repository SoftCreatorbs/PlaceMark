"""Área do aluno: compra (Mercado Pago ou simulada), meus cursos e sala de aula."""
import os
from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import db
from auth import papel_obrigatorio
from util import video_embed

bp = Blueprint("aluno", __name__)

FORMATO = "%Y-%m-%d %H:%M:%S"


def curso_publico(curso_id):
    c = db.get_db().execute(db.CURSOS_PUBLICOS_SQL + " AND c.id = ?", (curso_id,)).fetchone()
    if c is None:
        abort(404)
    return c


def conceder_acesso(aluno_id, curso_id, plano):
    """Libera o curso. Mensal soma 30 dias ao período atual (se ainda estiver ativo)."""
    conexao = db.get_db()
    atual = conexao.execute("SELECT * FROM acessos WHERE aluno_id = ? AND curso_id = ?", (aluno_id, curso_id)).fetchone()
    if plano == "vitalicio" or (atual and atual["expira_em"] is None):
        expira = None
    else:
        base = datetime.now()
        if atual and atual["expira_em"]:
            base = max(base, datetime.strptime(atual["expira_em"], FORMATO))
        expira = (base + timedelta(days=30)).strftime(FORMATO)
    conexao.execute(
        """INSERT INTO acessos (aluno_id, curso_id, plano, expira_em) VALUES (?, ?, ?, ?)
           ON CONFLICT (aluno_id, curso_id) DO UPDATE SET plano = excluded.plano, expira_em = excluded.expira_em""",
        (aluno_id, curso_id, "vitalicio" if expira is None else plano, expira),
    )


def revogar_acesso(aluno_id, curso_id):
    """Pagamento estornado: o acesso acaba agora."""
    db.get_db().execute(
        "UPDATE acessos SET expira_em = datetime('now', 'localtime', '-1 second') WHERE aluno_id = ? AND curso_id = ?",
        (aluno_id, curso_id))


@bp.route("/comprar/<int:curso_id>/<plano>", methods=("GET", "POST"))
@papel_obrigatorio("aluno")
def checkout(curso_id, plano):
    import mercadopago as mp

    if plano not in ("mensal", "vitalicio"):
        abort(404)
    curso = curso_publico(curso_id)
    valor = curso["preco_" + plano]
    if valor is None:
        abort(404)

    conexao = db.get_db()
    acesso_atual = conexao.execute(
        "SELECT * FROM acessos WHERE aluno_id = ? AND curso_id = ?", (g.usuario["id"], curso_id)
    ).fetchone()
    if acesso_atual and acesso_atual["expira_em"] is None:
        flash("Você já tem acesso vitalício a este curso.", "aviso")
        return redirect(url_for("aluno.aprender", curso_id=curso_id))

    empresa = conexao.execute("SELECT * FROM empresas WHERE id = ?", (curso["empresa_id"],)).fetchone()
    # real = Mercado Pago; simulado = só no seu computador enquanto o Mercado Pago não está configurado
    if mp.ativo():
        modo = "real" if mp.vendedor_recebe(empresa) else "indisponivel"
    else:
        modo = "indisponivel" if os.environ.get("PRODUCAO") == "1" else "simulado"

    if request.method == "POST":
        if modo == "indisponivel":
            flash("Este vendedor ainda não ativou os pagamentos. Tente novamente mais tarde.", "erro")
            return redirect(url_for("curso", curso_id=curso_id))
        pct = int(db.config("comissao_percentual", "10"))
        comissao = round(valor * pct / 100)

        if modo == "real":
            pedido_id = conexao.execute(
                """INSERT INTO pedidos (aluno_id, curso_id, empresa_id, plano, valor, comissao, valor_empresa, metodo, status, mp_conta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'mercadopago', 'pendente', ?)""",
                (g.usuario["id"], curso_id, curso["empresa_id"], plano, valor, comissao, valor - comissao,
                 mp.conta_que_recebe(empresa)),
            ).lastrowid
            conexao.commit()
            pedido = conexao.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,)).fetchone()
            try:
                link = mp.criar_cobranca(pedido, curso, empresa, g.usuario)
            except mp.ErroMP as erro:
                conexao.execute("UPDATE pedidos SET status = 'recusado' WHERE id = ?", (pedido_id,))
                conexao.commit()
                flash(f"Não foi possível abrir o pagamento agora. {erro}", "erro")
                return redirect(request.url)
            return redirect(link)  # vai para o checkout do Mercado Pago

        # ---- PAGAMENTO SIMULADO (só para testar no computador) ----
        metodo = request.form.get("metodo")
        if metodo not in ("pix", "cartao"):
            flash("Escolha uma forma de pagamento.", "erro")
            return redirect(request.url)
        conexao.execute(
            """INSERT INTO pedidos (aluno_id, curso_id, empresa_id, plano, valor, comissao, valor_empresa, metodo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (g.usuario["id"], curso_id, curso["empresa_id"], plano, valor, comissao, valor - comissao, metodo),
        )
        conceder_acesso(g.usuario["id"], curso_id, plano)
        conexao.commit()
        flash("Pagamento aprovado! Seu acesso já está liberado.", "sucesso")
        return redirect(url_for("aluno.aprender", curso_id=curso_id))

    return render_template("aluno/checkout.html", curso=curso, plano=plano, valor=valor,
                           acesso_atual=acesso_atual, modo=modo, teste=mp.modo_teste())


@bp.route("/minha-conta")
@papel_obrigatorio("aluno")
def meus_cursos():
    conexao = db.get_db()
    acessos = conexao.execute(
        """SELECT x.*, c.titulo, c.id AS curso_id, e.nome_fantasia AS empresa_nome,
                  (x.expira_em IS NULL OR x.expira_em > datetime('now', 'localtime')) AS ativo,
                  CAST(julianday(x.expira_em) - julianday('now', 'localtime') AS INTEGER) AS dias_restantes,
                  (SELECT COUNT(*) FROM aulas a WHERE a.curso_id = c.id) AS total_aulas
           FROM acessos x JOIN cursos c ON c.id = x.curso_id JOIN empresas e ON e.id = c.empresa_id
           WHERE x.aluno_id = ? ORDER BY ativo DESC, x.criado_em DESC""",
        (g.usuario["id"],),
    ).fetchall()
    pedidos = conexao.execute(
        """SELECT p.*, c.titulo FROM pedidos p JOIN cursos c ON c.id = p.curso_id
           WHERE p.aluno_id = ? ORDER BY p.id DESC LIMIT 10""",
        (g.usuario["id"],),
    ).fetchall()
    return render_template("aluno/meus_cursos.html", acessos=acessos, pedidos=pedidos)


@bp.route("/aprender/<int:curso_id>")
@bp.route("/aprender/<int:curso_id>/<int:aula_id>")
@papel_obrigatorio("aluno")
def aprender(curso_id, aula_id=None):
    acesso = db.tem_acesso(g.usuario["id"], curso_id)
    if acesso is None:
        flash("Seu acesso a este curso não está ativo. Escolha um plano para continuar.", "aviso")
        return redirect(url_for("curso", curso_id=curso_id))

    conexao = db.get_db()
    curso = conexao.execute("SELECT * FROM cursos WHERE id = ?", (curso_id,)).fetchone()
    aulas = conexao.execute("SELECT * FROM aulas WHERE curso_id = ? ORDER BY ordem, id", (curso_id,)).fetchall()
    atual = None
    if aulas:
        atual = next((a for a in aulas if a["id"] == aula_id), None) if aula_id else aulas[0]
        if atual is None:
            abort(404)
    return render_template("aluno/aprender.html", curso=curso, aulas=aulas, atual=atual,
                           acesso=acesso, embed=video_embed(atual["video_url"]) if atual else None)
