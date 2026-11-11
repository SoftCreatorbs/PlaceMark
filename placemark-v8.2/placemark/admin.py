"""Painel do admin (só a equipe da plataforma): moderação, usuários, vendas e comissão."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import db
import seguranca
from auth import papel_obrigatorio

bp = Blueprint("admin", __name__, url_prefix="/admin")

somente_admin = papel_obrigatorio("admin")


@bp.route("/")
@somente_admin
def dashboard():
    c = db.get_db()
    numeros = {
        "faturamento": c.execute("SELECT COALESCE(SUM(valor), 0) FROM pedidos WHERE status = 'pago'").fetchone()[0],
        "comissao": c.execute("SELECT COALESCE(SUM(comissao), 0) FROM pedidos WHERE status = 'pago'").fetchone()[0],
        "vendas": c.execute("SELECT COUNT(*) FROM pedidos WHERE status = 'pago'").fetchone()[0],
        "alunos": c.execute("SELECT COUNT(*) FROM usuarios WHERE papel = 'aluno'").fetchone()[0],
        "empresas_pendentes": c.execute("SELECT COUNT(*) FROM empresas WHERE status = 'pendente'").fetchone()[0],
        "cursos_analise": c.execute("SELECT COUNT(*) FROM cursos WHERE status = 'em_analise'").fetchone()[0],
        "receita_planos": c.execute("SELECT COALESCE(SUM(valor), 0) FROM assinaturas_vendedor").fetchone()[0],
        "vendedores_ativos": c.execute(
            "SELECT COUNT(*) FROM empresas WHERE oficial = 0 AND plano_expira_em > datetime('now', 'localtime')").fetchone()[0],
    }
    recentes = c.execute(
        """SELECT p.*, cu.titulo, u.nome AS aluno_nome, e.nome_fantasia AS empresa_nome
           FROM pedidos p JOIN cursos cu ON cu.id = p.curso_id JOIN usuarios u ON u.id = p.aluno_id
           JOIN empresas e ON e.id = p.empresa_id ORDER BY p.id DESC LIMIT 6""").fetchall()
    return render_template("admin/dashboard.html", n=numeros, recentes=recentes,
                           comissao_pct=db.config("comissao_percentual", "10"))


# ---------- Empresas ----------
@bp.route("/empresas")
@somente_admin
def empresas():
    status = request.args.get("status", "pendente")
    sql = """SELECT e.*, u.nome AS responsavel, u.email, p.nome AS plano_nome,
                    (e.plano_expira_em > datetime('now', 'localtime')) AS plano_ativo,
                    (SELECT COUNT(*) FROM cursos c WHERE c.empresa_id = e.id) AS total_cursos
             FROM empresas e JOIN usuarios u ON u.id = e.usuario_id
             LEFT JOIN planos_vendedor p ON p.id = e.plano_id
             WHERE e.oficial = 0"""
    params = []
    if status in ("pendente", "aprovada", "rejeitada", "bloqueada"):
        sql += " AND e.status = ?"
        params.append(status)
    lista = db.get_db().execute(sql + " ORDER BY e.id DESC", params).fetchall()
    planos = db.get_db().execute("SELECT * FROM planos_vendedor ORDER BY ordem, preco_mensal").fetchall()
    return render_template("admin/empresas.html", empresas=lista, status=status, planos=planos)


@bp.route("/empresas/<int:empresa_id>/status", methods=("POST",))
@somente_admin
def empresa_status(empresa_id):
    novo = request.form.get("status")
    if novo not in ("aprovada", "rejeitada", "bloqueada"):
        abort(400)
    c = db.get_db()
    if c.execute("UPDATE empresas SET status = ? WHERE id = ?", (novo, empresa_id)).rowcount == 0:
        abort(404)
    c.commit()
    seguranca.registrar("vendedor_" + novo, f"Vendedor #{empresa_id}")
    flash({"aprovada": "Empresa aprovada.", "rejeitada": "Empresa rejeitada.",
           "bloqueada": "Empresa bloqueada. Os cursos dela saíram do catálogo."}[novo], "sucesso")
    return redirect(seguranca.voltar(url_for("admin.empresas")))


@bp.route("/empresas/<int:empresa_id>/plano", methods=("POST",))
@somente_admin
def ativar_plano(empresa_id):
    """Ativa/renova 30 dias de plano para quem pagou na Kiwify (até termos o webhook)."""
    from datetime import datetime, timedelta
    c = db.get_db()
    empresa = c.execute("SELECT * FROM empresas WHERE id = ? AND oficial = 0", (empresa_id,)).fetchone()
    plano = c.execute("SELECT * FROM planos_vendedor WHERE id = ?", (request.form.get("plano_id", type=int),)).fetchone()
    if empresa is None or plano is None:
        abort(404)
    base = datetime.now()
    if empresa["plano_id"] == plano["id"] and empresa["plano_expira_em"]:
        base = max(base, datetime.strptime(empresa["plano_expira_em"], "%Y-%m-%d %H:%M:%S"))
    expira = (base + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE empresas SET plano_id = ?, plano_expira_em = ? WHERE id = ?", (plano["id"], expira, empresa_id))
    c.execute("INSERT INTO assinaturas_vendedor (empresa_id, plano_id, valor, metodo) VALUES (?, ?, ?, 'kiwify')",
              (empresa_id, plano["id"], plano["preco_mensal"]))
    c.commit()
    seguranca.registrar("plano_ativado", f"{plano['nome']} para {empresa['nome_fantasia']} (#{empresa_id})")
    flash(f"Plano {plano['nome']} ativo para {empresa['nome_fantasia']} até {expira[8:10]}/{expira[5:7]}/{expira[:4]}.", "sucesso")
    return redirect(seguranca.voltar(url_for("admin.empresas")))


# ---------- Cursos ----------
@bp.route("/cursos")
@somente_admin
def cursos():
    status = request.args.get("status", "em_analise")
    sql = """SELECT c.*, e.nome_fantasia AS empresa_nome,
                    (SELECT COUNT(*) FROM aulas a WHERE a.curso_id = c.id) AS total_aulas
             FROM cursos c JOIN empresas e ON e.id = c.empresa_id"""
    params = []
    if status in ("rascunho", "em_analise", "publicado", "rejeitado"):
        sql += " WHERE c.status = ?"
        params.append(status)
    lista = db.get_db().execute(sql + " ORDER BY c.id DESC", params).fetchall()
    return render_template("admin/cursos.html", cursos=lista, status=status)


@bp.route("/cursos/<int:curso_id>/moderar", methods=("POST",))
@somente_admin
def moderar_curso(curso_id):
    acao = request.form.get("acao")
    c = db.get_db()
    if acao == "aprovar":
        r = c.execute("UPDATE cursos SET status = 'publicado', motivo_rejeicao = NULL WHERE id = ?", (curso_id,))
        msg = "Curso publicado."
    elif acao == "rejeitar":
        motivo = request.form.get("motivo", "").strip() or "Não atende às diretrizes da plataforma."
        r = c.execute("UPDATE cursos SET status = 'rejeitado', motivo_rejeicao = ? WHERE id = ?", (motivo, curso_id))
        msg = "Curso rejeitado. A empresa verá o motivo."
    elif acao == "despublicar":
        r = c.execute("UPDATE cursos SET status = 'rascunho' WHERE id = ?", (curso_id,))
        msg = "Curso retirado do catálogo."
    else:
        abort(400)
    if r.rowcount == 0:
        abort(404)
    c.commit()
    seguranca.registrar("curso_" + acao, f"Curso #{curso_id}")
    flash(msg, "sucesso")
    return redirect(seguranca.voltar(url_for("admin.cursos")))


# ---------- Usuários ----------
@bp.route("/usuarios")
@somente_admin
def usuarios():
    busca = request.args.get("q", "").strip()
    sql, params = "SELECT * FROM usuarios", []
    if busca:
        sql += " WHERE nome LIKE ? OR email LIKE ?"
        params = [f"%{busca}%"] * 2
    lista = db.get_db().execute(sql + " ORDER BY id DESC LIMIT 200", params).fetchall()
    return render_template("admin/usuarios.html", usuarios=lista, busca=busca)


@bp.route("/usuarios/<int:usuario_id>/alternar", methods=("POST",))
@somente_admin
def alternar_usuario(usuario_id):
    c = db.get_db()
    u = c.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if u is None:
        abort(404)
    if u["papel"] == "admin":
        flash("Contas de admin só podem ser alteradas pelo terminal.", "erro")
    else:
        c.execute("UPDATE usuarios SET ativo = ? WHERE id = ?", (0 if u["ativo"] else 1, usuario_id))
        c.commit()
        seguranca.registrar("usuario_" + ("bloqueado" if u["ativo"] else "reativado"), f"{u['email']} (#{usuario_id})")
        flash(f"Conta de {u['nome']} {'bloqueada' if u['ativo'] else 'reativada'}.", "sucesso")
    return redirect(seguranca.voltar(url_for("admin.usuarios")))


# ---------- Vendas ----------
@bp.route("/vendas")
@somente_admin
def vendas():
    c = db.get_db()
    lista = c.execute(
        """SELECT p.*, cu.titulo, u.nome AS aluno_nome, e.nome_fantasia AS empresa_nome
           FROM pedidos p JOIN cursos cu ON cu.id = p.curso_id JOIN usuarios u ON u.id = p.aluno_id
           JOIN empresas e ON e.id = p.empresa_id ORDER BY p.id DESC LIMIT 500""").fetchall()
    totais = c.execute(
        """SELECT COALESCE(SUM(valor), 0) AS valor, COALESCE(SUM(comissao), 0) AS comissao,
                  COALESCE(SUM(valor_empresa), 0) AS empresas FROM pedidos WHERE status = 'pago'""").fetchone()
    planos = c.execute(
        """SELECT a.*, p.nome AS plano_nome, e.nome_fantasia AS empresa_nome
           FROM assinaturas_vendedor a JOIN planos_vendedor p ON p.id = a.plano_id
           JOIN empresas e ON e.id = a.empresa_id ORDER BY a.id DESC LIMIT 200""").fetchall()
    total_planos = sum(p["valor"] for p in planos)
    return render_template("admin/vendas.html", vendas=lista, totais=totais, planos=planos, total_planos=total_planos)


# ---------- Configurações ----------
@bp.route("/config", methods=("GET", "POST"))
@somente_admin
def config():
    c = db.get_db()
    if request.method == "POST":
        try:
            pct = int(request.form.get("comissao_percentual", ""))
            if not 0 <= pct <= 50:
                raise ValueError
        except ValueError:
            flash("A comissão precisa ser um número inteiro entre 0 e 50.", "erro")
        else:
            c.execute("UPDATE configuracoes SET valor = ? WHERE chave = 'comissao_percentual'", (str(pct),))
            c.commit()
            seguranca.registrar("comissao_alterada", f"{pct}%")
            flash(f"Comissão atualizada para {pct}%. Vale para as próximas vendas.", "sucesso")
            return redirect(url_for("admin.config"))
    return render_template("admin/config.html", comissao=db.config("comissao_percentual", "10"))


# ---------- Planos dos vendedores ----------
@bp.route("/planos", methods=("GET", "POST"))
@somente_admin
def planos():
    from util import link_checkout_valido, para_centavos
    c = db.get_db()
    if request.method == "POST":
        plano_id = request.form.get("id", type=int)
        nome = request.form.get("nome", "").strip()
        descricao = request.form.get("descricao", "").strip()
        limite_txt = request.form.get("limite_cursos", "").strip()
        ativo = 1 if request.form.get("ativo") else 0
        checkout_url = request.form.get("checkout_url", "").strip() or None
        erros = []
        if not link_checkout_valido(checkout_url):
            erros.append("O link do checkout precisa começar com https:// (ex.: https://pay.kiwify.com.br/...).")
        if len(nome) < 2:
            erros.append("Dê um nome ao plano.")
        try:
            preco = para_centavos(request.form.get("preco_mensal"))
            if preco is None:
                raise ValueError
        except ValueError:
            erros.append("Preço inválido. Use o formato 50,00.")
            preco = None
        try:
            limite = int(limite_txt) if limite_txt else None
            if limite is not None and limite < 1:
                raise ValueError
        except ValueError:
            erros.append("O limite precisa ser um número maior que zero (ou vazio para ilimitado).")
            limite = None
        if erros:
            for e in erros:
                flash(e, "erro")
        else:
            if plano_id:
                c.execute("""UPDATE planos_vendedor SET nome=?, preco_mensal=?, limite_cursos=?, descricao=?, ativo=?,
                             checkout_url=? WHERE id=?""", (nome, preco, limite, descricao, ativo, checkout_url, plano_id))
                flash(f"Plano {nome} atualizado. Vale para novas assinaturas e renovações.", "sucesso")
            else:
                ordem = c.execute("SELECT COALESCE(MAX(ordem), 0) + 1 FROM planos_vendedor").fetchone()[0]
                c.execute("""INSERT INTO planos_vendedor (nome, preco_mensal, limite_cursos, descricao, ativo, ordem, checkout_url)
                             VALUES (?, ?, ?, ?, ?, ?, ?)""", (nome, preco, limite, descricao, ativo, ordem, checkout_url))
                flash(f"Plano {nome} criado.", "sucesso")
            c.commit()
            seguranca.registrar("plano_editado", nome)
            return redirect(url_for("admin.planos"))
    lista = c.execute(
        """SELECT p.*, (SELECT COUNT(*) FROM empresas e WHERE e.plano_id = p.id
                        AND e.plano_expira_em > datetime('now', 'localtime')) AS assinantes
           FROM planos_vendedor p ORDER BY p.ordem, p.preco_mensal""").fetchall()
    return render_template("admin/planos.html", planos=lista)


# ---------- Segurança ----------
NOMES_ACOES = {
    "login": "Entrou", "logout": "Saiu", "login_falhou": "Senha errada", "login_bloqueado": "Login bloqueado (força bruta)",
    "cadastro": "Nova conta de aluno", "cadastro_vendedor": "Novo vendedor", "senha_alterada": "Trocou a senha",
    "instalacao": "Instalação", "vendedor_aprovada": "Aprovou vendedor", "vendedor_rejeitada": "Rejeitou vendedor",
    "vendedor_bloqueada": "Bloqueou vendedor", "curso_aprovar": "Publicou curso", "curso_rejeitar": "Rejeitou curso",
    "curso_despublicar": "Despublicou curso", "usuario_bloqueado": "Bloqueou usuário", "usuario_reativado": "Reativou usuário",
    "plano_ativado": "Ativou plano", "plano_editado": "Editou plano", "comissao_alterada": "Mudou comissão",
    "backup_manual": "Fez backup",
    "mp_configurado": "Configurou Mercado Pago", "mp_conectado": "Vendedor conectou Mercado Pago",
    "mp_desconectado": "Vendedor desconectou Mercado Pago", "pagamento_aprovado": "Pagamento aprovado",
    "pagamento_estornado": "Pagamento estornado", "pagamento_divergente": "⚠ Pagamento com valor errado",
}


@bp.route("/seguranca")
@somente_admin
def seguranca_painel():
    import os
    from flask import current_app
    c = db.get_db()
    tipo = request.args.get("tipo", "")
    sql = """SELECT a.*, u.nome, u.email FROM auditoria a LEFT JOIN usuarios u ON u.id = a.usuario_id"""
    params = []
    if tipo == "alertas":
        sql += " WHERE a.acao IN ('login_falhou', 'login_bloqueado')"
    elif tipo == "admin":
        sql += " WHERE a.acao NOT IN ('login', 'logout', 'login_falhou', 'login_bloqueado', 'cadastro')"
    eventos = c.execute(sql + " ORDER BY a.id DESC LIMIT 200", params).fetchall()
    numeros = {
        "falhas_24h": c.execute("SELECT COUNT(*) FROM auditoria WHERE acao = 'login_falhou' AND criado_em > datetime('now', 'localtime', '-1 day')").fetchone()[0],
        "bloqueios_24h": c.execute("SELECT COUNT(*) FROM auditoria WHERE acao = 'login_bloqueado' AND criado_em > datetime('now', 'localtime', '-1 day')").fetchone()[0],
        "logins_24h": c.execute("SELECT COUNT(*) FROM auditoria WHERE acao = 'login' AND criado_em > datetime('now', 'localtime', '-1 day')").fetchone()[0],
        "admins": c.execute("SELECT COUNT(*) FROM usuarios WHERE papel = 'admin'").fetchone()[0],
    }
    ips_suspeitos = c.execute(
        """SELECT ip, COUNT(*) AS n, MAX(criado_em) AS ultima FROM auditoria
           WHERE acao IN ('login_falhou', 'login_bloqueado') AND criado_em > datetime('now', 'localtime', '-1 day')
           GROUP BY ip HAVING n >= 3 ORDER BY n DESC LIMIT 10""").fetchall()
    producao = os.environ.get("PRODUCAO") == "1"
    checklist = [
        ("Senhas criptografadas (hash)", True, "Nunca guardamos a senha em texto."),
        ("Proteção contra formulários falsos (CSRF)", True, "Todo formulário tem um código secreto."),
        ("Bloqueio de força bruta", True, "5 senhas erradas = 15 min de espera."),
        ("Chave secreta única", True, "Gerada automaticamente para este servidor."),
        ("Cabeçalhos de proteção do navegador", True, "Bloqueia scripts de fora e iframes."),
        ("Backup diário automático", bool(seguranca.listar_backups(current_app.config["DATABASE"])), "Últimos 14 dias guardados."),
        ("Modo produção (sem detalhes de erro)", producao, "Ligue PRODUCAO=1 quando publicar."),
        ("HTTPS (cadeado no navegador)", producao and request.is_secure, "Vem com a hospedagem ao publicar."),
    ]
    return render_template("admin/seguranca.html", eventos=eventos, n=numeros, ips=ips_suspeitos, tipo=tipo,
                           nomes=NOMES_ACOES, checklist=checklist,
                           backups=seguranca.listar_backups(current_app.config["DATABASE"]))


@bp.route("/seguranca/backup", methods=("POST",))
@somente_admin
def backup_agora():
    from flask import current_app
    destino = seguranca.backup(current_app.config["DATABASE"], forcar=True)
    seguranca.registrar("backup_manual", destino.name)
    flash(f"Backup feito: {destino.name}", "sucesso")
    return redirect(url_for("admin.seguranca_painel"))
