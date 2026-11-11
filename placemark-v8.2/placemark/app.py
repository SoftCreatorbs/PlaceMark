"""Marketplace de cursos — empresas vendem, alunos compram, admin modera."""
import os
from datetime import timedelta

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for
from markupsafe import Markup

import admin
import aluno
import auth
import db
import empresa
import mercadopago
import saques
import seguranca
import util


def create_app():
    app = Flask(__name__)
    app.config.update(
        DATABASE=db.caminho_padrao(),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,  # recusa envios maiores que 2 MB
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("PRODUCAO") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    os.makedirs(app.instance_path, exist_ok=True)
    # Chave secreta forte e única, criada sozinha na 1ª vez (ou SECRET_KEY do ambiente)
    app.config["SECRET_KEY"] = seguranca.chave_secreta(os.path.dirname(app.config["DATABASE"]))

    db.init_app(app)
    seguranca.init_app(app)
    for bp in (auth.bp, aluno.bp, empresa.bp, admin.bp, mercadopago.bp, saques.bp):
        app.register_blueprint(bp)
    app.before_request(auth.checar_csrf)

    app.jinja_env.globals.update(csrf_token=auth.gerar_csrf, iniciais=util.iniciais, tom=util.tom)
    app.jinja_env.globals["marca"] = lambda: Markup('<svg class="logo-pin" viewBox="0 0 64 64" aria-hidden="true"><defs><linearGradient id="pm-grad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8b7dff"/><stop offset="1" stop-color="#4b3de0"/></linearGradient></defs><path d="M13 25a19 19 0 1 1 19 19h-6v8.5l-6.5 8-6.5-8V25z" fill="url(#pm-grad)"/><circle cx="32" cy="25" r="7.5" fill="#c6f432"/></svg>')  # logo da Placemark (pino)
    app.jinja_env.globals["ic"] = lambda nome, classe="": Markup(
        f'<svg class="ic {classe}" aria-hidden="true" focusable="false"><use href="#i-{nome}"></use></svg>')
    app.jinja_env.filters["reais"] = util.reais
    app.jinja_env.filters["metodo"] = lambda m: util.METODOS.get(m, m)
    app.jinja_env.filters["data"] = lambda t: f"{t[8:10]}/{t[5:7]}/{t[:4]}" if t else "—"

    # ---------- Primeira execução: criar o admin pelo navegador ----------
    @app.before_request
    def exigir_instalacao():
        if app.config.get("_tem_admin") or request.endpoint in ("instalar", "static"):
            return None
        if db.existe_admin():
            app.config["_tem_admin"] = True
            return None
        return redirect(url_for("instalar"))

    @app.route("/instalar", methods=("GET", "POST"))
    def instalar():
        if db.existe_admin():
            abort(404)  # depois de instalado, esta página deixa de existir
        dados = {}
        if request.method == "POST":
            espera = seguranca.bloqueado("instalar-ip", seguranca.ip())
            if espera:
                flash(f"Muitas tentativas. Aguarde {espera} minuto(s).", "erro")
                return render_template("instalar.html", dados=dados, caminho_banco=app.config["DATABASE"]), 429
            seguranca.registrar_tentativa("instalar-ip", seguranca.ip())
            dados, senha, erros = auth.validar_conta(request.form)
            if not erros:
                uid = auth.criar_usuario(dados, senha, "admin")
                seguranca.registrar("instalacao", f"Admin {dados['email']} criado na instalação", uid)
                db.get_db().commit()
                if request.form.get("exemplo"):
                    db.criar_dados_exemplo()
                auth.entrar(uid)
                app.config["_tem_admin"] = True
                flash("Tudo pronto! Você é o administrador da Placemark.", "sucesso")
                return redirect(url_for("admin.dashboard"))
            for e in erros:
                flash(e, "erro")
        return render_template("instalar.html", dados=dados, caminho_banco=app.config["DATABASE"])

    # ---------- Páginas públicas ----------
    @app.route("/")
    def inicio():
        conexao = db.get_db()
        base = db.CURSOS_PUBLICOS_SQL
        vitrines = [
            ("Mais procurados", "Os cursos que mais vendem agora", conexao.execute(base + " ORDER BY total_alunos DESC, c.id LIMIT 10").fetchall(), url_for("cursos", ordem="relevancia")),
            ("Novidades", "Acabaram de chegar na Placemark", conexao.execute(base + " ORDER BY c.id DESC LIMIT 10").fetchall(), url_for("cursos", ordem="novos")),
            ("Pague uma vez, acesse para sempre", "Cursos com acesso vitalício", conexao.execute(base + " AND c.preco_vitalicio IS NOT NULL ORDER BY total_alunos DESC LIMIT 10").fetchall(), url_for("cursos", acesso="vitalicio")),
        ]
        vitrines = [v for v in vitrines if v[2]]
        contagem = dict(conexao.execute(f"SELECT categoria, COUNT(*) FROM ({base}) GROUP BY categoria").fetchall())
        total = sum(contagem.values())
        destaque = vitrines[0][2][0] if vitrines else None
        return render_template("inicio.html", vitrines=vitrines, total_cursos=total, destaque=destaque,
                               categorias=util.CATEGORIAS, contagem=contagem,
                               preco_plano=conexao.execute("SELECT MIN(preco_mensal) FROM planos_vendedor WHERE ativo = 1").fetchone()[0])

    @app.route("/cursos")
    def cursos():
        from urllib.parse import urlencode
        busca = request.args.get("q", "").strip()
        cats = [c for c in request.args.getlist("cat") if c in util.CATEGORIAS]
        if request.args.get("categoria") in util.CATEGORIAS:  # links antigos
            cats.append(request.args["categoria"])
        preco = request.args.get("preco", "")
        acesso = request.args.get("acesso", "")
        ordem = request.args.get("ordem", "relevancia")

        menor_preco = "MIN(COALESCE(c.preco_mensal, 999999999), COALESCE(c.preco_vitalicio, 999999999))"
        sql, params = db.CURSOS_PUBLICOS_SQL, []
        if busca:
            sql += " AND (c.titulo LIKE ? OR c.descricao LIKE ? OR e.nome_fantasia LIKE ?)"
            params += [f"%{busca}%"] * 3
        if cats:
            sql += f" AND c.categoria IN ({','.join('?' * len(cats))})"
            params += cats
        if preco in util.FAIXAS_PRECO:
            minimo, maximo = util.FAIXAS_PRECO[preco][1:]
            sql += f" AND {menor_preco} >= ? AND {menor_preco} < ?"
            params += [minimo, maximo]
        if acesso == "mensal":
            sql += " AND c.preco_mensal IS NOT NULL"
        elif acesso == "vitalicio":
            sql += " AND c.preco_vitalicio IS NOT NULL"
        ordenacao = {
            "menor": f"{menor_preco} ASC", "maior": f"{menor_preco} DESC",
            "novos": "c.id DESC", "az": "c.titulo COLLATE NOCASE",
        }.get(ordem, "total_alunos DESC, c.id DESC")
        lista = db.get_db().execute(sql + " ORDER BY " + ordenacao, params).fetchall()

        contagem = dict(db.get_db().execute(
            f"SELECT categoria, COUNT(*) FROM ({db.CURSOS_PUBLICOS_SQL}) GROUP BY categoria").fetchall())

        # Filtros ativos viram "chips" que podem ser removidos um a um
        atuais = {"q": busca, "cat": cats, "preco": preco, "acesso": acesso, "ordem": ordem if ordem != "relevancia" else ""}
        def url_sem(chave, valor=None):
            p = {k: (list(v) if isinstance(v, list) else v) for k, v in atuais.items()}
            if chave == "cat":
                p["cat"] = [c for c in p["cat"] if c != valor]
            else:
                p[chave] = ""
            limpos = [(k, x) for k, v in p.items() for x in (v if isinstance(v, list) else [v]) if x]
            return url_for("cursos") + ("?" + urlencode(limpos) if limpos else "")
        chips = [(c, url_sem("cat", c)) for c in cats]
        if preco in util.FAIXAS_PRECO:
            chips.append((util.FAIXAS_PRECO[preco][0], url_sem("preco")))
        if acesso in ("mensal", "vitalicio"):
            chips.append(("Assinatura mensal" if acesso == "mensal" else "Acesso vitalício", url_sem("acesso")))
        total_filtros = len(cats) + (preco in util.FAIXAS_PRECO) + (acesso in ("mensal", "vitalicio"))

        return render_template("cursos.html", cursos=lista, busca=busca, cats=cats, preco=preco,
                               acesso=acesso, ordem=ordem, categorias=util.CATEGORIAS, contagem=contagem,
                               faixas=util.FAIXAS_PRECO, ordens=util.ORDENS, chips=chips,
                               total_filtros=total_filtros, url_sem_busca=url_sem("q"))

    @app.route("/cursos/<int:curso_id>")
    def curso(curso_id):
        conexao = db.get_db()
        c = conexao.execute(db.CURSOS_PUBLICOS_SQL + " AND c.id = ?", (curso_id,)).fetchone()
        # A empresa dona e o admin conseguem pré-visualizar cursos ainda não publicados
        if c is None and g.usuario:
            c = conexao.execute(
                """SELECT c.*, e.nome_fantasia AS empresa_nome, e.usuario_id,
                          (SELECT COUNT(*) FROM aulas a WHERE a.curso_id = c.id) AS total_aulas,
                          0 AS total_alunos
                   FROM cursos c JOIN empresas e ON e.id = c.empresa_id WHERE c.id = ?""",
                (curso_id,),
            ).fetchone()
            if c and not (g.usuario["papel"] == "admin" or c["usuario_id"] == g.usuario["id"]):
                c = None
        if c is None:
            abort(404)
        aulas = conexao.execute("SELECT id, titulo FROM aulas WHERE curso_id = ? ORDER BY ordem, id", (curso_id,)).fetchall()
        acesso = db.tem_acesso(g.usuario["id"], curso_id) if g.usuario else None
        return render_template("curso.html", curso=c, aulas=aulas, acesso=acesso)

    @app.route("/vender")
    def vender():
        planos = db.get_db().execute("SELECT * FROM planos_vendedor WHERE ativo = 1 ORDER BY ordem, preco_mensal").fetchall()
        return render_template("vender.html", comissao=db.config("comissao_percentual", "10"), planos=planos)

    # ---------- Carrinho de plano do vendedor -> checkout Kiwify ----------
    @app.route("/carrinho/plano/<int:plano_id>")
    def carrinho_plano(plano_id):
        conexao = db.get_db()
        plano = conexao.execute("SELECT * FROM planos_vendedor WHERE id = ? AND ativo = 1", (plano_id,)).fetchone()
        if plano is None:
            abort(404)
        outros = conexao.execute(
            "SELECT * FROM planos_vendedor WHERE ativo = 1 AND id != ? ORDER BY ordem, preco_mensal", (plano_id,)).fetchall()
        return render_template("carrinho.html", plano=plano, outros=outros,
                               comissao=db.config("comissao_percentual", "10"))

    @app.route("/carrinho/plano/<int:plano_id>/comprar")
    def comprar_plano(plano_id):
        plano = db.get_db().execute("SELECT * FROM planos_vendedor WHERE id = ? AND ativo = 1", (plano_id,)).fetchone()
        if plano is None:
            abort(404)
        if plano["checkout_url"]:
            return redirect(plano["checkout_url"])  # vai direto para o checkout da Kiwify
        # Sem link configurado: vendedor logado usa o pagamento simulado; os outros veem um aviso
        if g.usuario and g.usuario["papel"] == "empresa":
            return redirect(url_for("empresa.assinar_plano", plano_id=plano_id))
        flash("O checkout deste plano ainda não foi configurado. Tente novamente mais tarde.", "aviso")
        return redirect(url_for("carrinho_plano", plano_id=plano_id))

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def erro(e):
        return render_template("erro.html", erro=e), e.code

    return app


app = create_app()

if __name__ == "__main__":
    # O modo debug mostra detalhes de erros: ótimo no seu computador, PERIGOSO na internet.
    # Ele só liga se PRODUCAO não estiver definido, e o site só aceita conexões deste computador.
    modo_debug = os.environ.get("PRODUCAO") != "1" and os.environ.get("PLACEMARK_DEBUG", "1") == "1"
    app.run(host="127.0.0.1", port=5000, debug=modo_debug, use_reloader=False)  # sem recarregamento automático: evita o site fechar sozinho no Windows
