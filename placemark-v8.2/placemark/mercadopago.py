"""Pagamentos reais com o Mercado Pago.

Dois modos (Admin → Saques → Como o dinheiro das vendas é recebido):

CARTEIRA (padrão)
1. O aluno paga no checkout do Mercado Pago e TODO o dinheiro cai na conta da Placemark.
2. A parte do vendedor (venda menos a comissão) vira saldo dele no site (veja saques.py).
3. Para sacar, o vendedor conecta a conta Mercado Pago dele (OAuth) e pede o saque.
   A Placemark envia um Pix pela API de saques do Mercado Pago, já descontando a taxa de saque.

SPLIT
1. Cada vendedor conecta a conta Mercado Pago dele antes de vender.
2. O Mercado Pago divide na hora: a parte do vendedor cai na conta dele e a comissão
   (marketplace_fee) cai na conta da Placemark. O vendedor saca pelo app do Mercado Pago.

Nos dois modos o Mercado Pago avisa o site (webhook) e o site CONFIRMA o pagamento consultando
a API antes de liberar o curso: um aviso falso nunca libera nada.

Só usa a biblioteca padrão do Python (não precisa instalar nada além do Flask).
"""
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

import db
import seguranca
from auth import papel_obrigatorio
from empresa import empresa_aprovada

API = "https://api.mercadopago.com"
URL_AUTORIZAR = "https://auth.mercadopago.com/authorization"
FORMATO = "%Y-%m-%d %H:%M:%S"

bp = Blueprint("mp", __name__)


class ErroMP(Exception):
    """Falha ao falar com o Mercado Pago (rede fora, credencial errada etc.).
    status = código HTTP (None = sem resposta); detalhe = texto devolvido pelo Mercado Pago."""

    def __init__(self, mensagem, status=None, detalhe=""):
        super().__init__(mensagem)
        self.status = status
        self.detalhe = detalhe or ""


# =====================================================================
# Comunicação com a API (dá para trocar nos testes: mercadopago.transporte = falso)
# =====================================================================
def _http(metodo, caminho, token=None, dados=None, params=None, cabecalhos=None):
    url = API + caminho
    if params:
        url += "?" + urllib.parse.urlencode(params)
    cabecalhos_req = {"Accept": "application/json", "User-Agent": "Placemark/1.0"}
    corpo = None
    if dados is not None:
        corpo = json.dumps(dados).encode("utf-8")
        cabecalhos_req["Content-Type"] = "application/json"
    if token:
        cabecalhos_req["Authorization"] = f"Bearer {token}"
    cabecalhos_req.update(cabecalhos or {})
    req = urllib.request.Request(url, data=corpo, method=metodo, headers=cabecalhos_req)
    try:
        with urllib.request.urlopen(req, timeout=20) as resposta:
            texto = resposta.read().decode("utf-8")
            return json.loads(texto) if texto else {}
    except urllib.error.HTTPError as erro:
        detalhe = erro.read().decode("utf-8", errors="replace")[:300]
        current_app.logger.warning("Mercado Pago %s %s -> %s %s", metodo, caminho, erro.code, detalhe)
        raise ErroMP(f"O Mercado Pago recusou o pedido (erro {erro.code}).", erro.code, detalhe) from erro
    except (urllib.error.URLError, TimeoutError, ValueError) as erro:
        current_app.logger.warning("Mercado Pago inacessível: %s", erro)
        raise ErroMP("Não foi possível falar com o Mercado Pago. Verifique a internet.") from erro


transporte = _http


def chamar(metodo, caminho, token=None, dados=None, params=None, cabecalhos=None):
    if cabecalhos:
        return transporte(metodo, caminho, token=token, dados=dados, params=params, cabecalhos=cabecalhos)
    return transporte(metodo, caminho, token=token, dados=dados, params=params)


# =====================================================================
# Configuração (Admin → Mercado Pago). Variáveis de ambiente têm prioridade.
# =====================================================================
CAMPOS = {
    "mp_site_url": "MP_SITE_URL",
    "mp_client_id": "MP_CLIENT_ID",
    "mp_client_secret": "MP_CLIENT_SECRET",
    "mp_access_token": "MP_ACCESS_TOKEN",
    "mp_modo_teste": "MP_MODO_TESTE",
}
SECRETOS = ("mp_client_secret", "mp_access_token")


def cfg(chave):
    return os.environ.get(CAMPOS[chave]) or db.config(chave, "") or ""


def salvar_cfg(chave, valor):
    conexao = db.get_db()
    conexao.execute(
        "INSERT INTO configuracoes (chave, valor) VALUES (?, ?) ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
        (chave, valor))


def modo_teste():
    return cfg("mp_modo_teste") == "1"


def ativo():
    """Pagamento real ligado? (precisa do endereço público e do token da Placemark)."""
    return bool(cfg("mp_site_url") and cfg("mp_access_token"))


def modo_carteira():
    """True = as vendas caem na conta da Placemark e viram saldo do vendedor (saque por Pix).
    False = split: cada venda cai direto na conta Mercado Pago do vendedor."""
    return db.config("mp_modo_recebimento", "carteira") != "split"


def oauth_pronto():
    """Vendedores já conseguem conectar a conta deles?"""
    return ativo() and bool(cfg("mp_client_id") and cfg("mp_client_secret"))


def url_publica(endpoint, **valores):
    """Endereço completo que o Mercado Pago consegue acessar (ex.: https://placemark.com.br/...)."""
    return cfg("mp_site_url").rstrip("/") + url_for(endpoint, **valores)


def mascarar(segredo):
    return "••••" + segredo[-4:] if segredo else ""


# =====================================================================
# Contas dos vendedores (OAuth)
# =====================================================================
def _guardar_tokens(empresa_id, resposta):
    expira = datetime.now() + timedelta(seconds=int(resposta.get("expires_in") or 15552000))
    conexao = db.get_db()
    conexao.execute(
        """UPDATE empresas SET mp_user_id = ?, mp_access_token = ?, mp_refresh_token = ?, mp_public_key = ?,
                  mp_expira_em = ?, mp_conectado_em = COALESCE(mp_conectado_em, datetime('now', 'localtime')),
                  mp_teste = ?
           WHERE id = ?""",
        (str(resposta.get("user_id") or ""), resposta["access_token"], resposta.get("refresh_token"),
         resposta.get("public_key"), expira.strftime(FORMATO), 1 if modo_teste() else 0, empresa_id))
    conexao.commit()


def _renovar(empresa):
    resposta = chamar("POST", "/oauth/token", dados={
        "client_id": cfg("mp_client_id"), "client_secret": cfg("mp_client_secret"),
        "grant_type": "refresh_token", "refresh_token": empresa["mp_refresh_token"],
    })
    if not resposta.get("access_token"):
        raise ErroMP("O Mercado Pago não renovou o acesso do vendedor.")
    _guardar_tokens(empresa["id"], resposta)
    return resposta["access_token"]


def _guardar_titular(empresa_id, token):
    """Guarda quem é o dono da conta Mercado Pago conectada (nome, e-mail e CPF/CNPJ).
    O saque por Pix só é aceito para uma chave desse mesmo titular."""
    try:
        conta = chamar("GET", "/users/me", token=token)
    except ErroMP:
        return
    ident = conta.get("identification") or {}
    numero = "".join(ch for ch in str(ident.get("number") or "") if ch.isdigit())
    tipo = (ident.get("type") or "").upper()
    if tipo not in ("CPF", "CNPJ"):
        tipo = "CPF" if len(numero) == 11 else "CNPJ" if len(numero) == 14 else None
    nome = " ".join(x for x in (conta.get("first_name"), conta.get("last_name")) if x) or conta.get("nickname")
    conexao = db.get_db()
    conexao.execute("UPDATE empresas SET mp_documento = ?, mp_documento_tipo = ?, mp_email = ?, mp_nome = ? WHERE id = ?",
                    (numero or None, tipo if numero else None, conta.get("email"), nome, empresa_id))
    conexao.commit()


def token_do_vendedor(empresa):
    """Token usado para cobrar em nome do vendedor. None = vendedor ainda não conectou."""
    if empresa["oficial"]:
        return cfg("mp_access_token") or None   # loja oficial recebe na conta da própria Placemark
    if not empresa["mp_access_token"]:
        return None
    expira = empresa["mp_expira_em"]
    perto_de_vencer = expira and datetime.strptime(expira, FORMATO) < datetime.now() + timedelta(days=7)
    if perto_de_vencer and empresa["mp_refresh_token"] and oauth_pronto():
        try:
            return _renovar(empresa)
        except ErroMP:
            if datetime.strptime(expira, FORMATO) < datetime.now():
                return None  # venceu de vez: o vendedor precisa conectar de novo
    return empresa["mp_access_token"]


def vendedor_recebe(empresa):
    """O curso pode ser vendido com pagamento real?"""
    if not ativo():
        return False
    if modo_carteira() or empresa["oficial"]:
        return True   # o dinheiro cai na conta da Placemark: o vendedor não precisa estar conectado
    return token_do_vendedor(empresa) is not None


def conta_que_recebe(empresa):
    """Em qual conta o pagamento de um pedido novo vai cair: 'placemark' ou 'vendedor'."""
    return "placemark" if modo_carteira() or empresa["oficial"] else "vendedor"


def token_cobranca(empresa, conta):
    """Token da conta onde o pedido foi (ou será) cobrado."""
    if conta == "placemark" or empresa["oficial"]:
        return cfg("mp_access_token") or None
    return token_do_vendedor(empresa)


# =====================================================================
# Pedidos: criar cobrança e confirmar pagamento
# =====================================================================
def criar_cobranca(pedido, curso, empresa, aluno):
    """Cria a preferência de pagamento no Mercado Pago e devolve o link do checkout."""
    conta = pedido["mp_conta"]
    token = token_cobranca(empresa, conta)
    if token is None:
        raise ErroMP("Este vendedor ainda não ativou os recebimentos.")
    nome_plano = "acesso vitalício" if pedido["plano"] == "vitalicio" else "assinatura de 30 dias"
    dados = {
        "items": [{
            "id": f"curso-{curso['id']}",
            "title": f"{curso['titulo']} ({nome_plano})"[:250],
            "quantity": 1,
            "currency_id": "BRL",
            "unit_price": round(pedido["valor"] / 100, 2),
        }],
        "payer": {"email": aluno["email"], "name": aluno["nome"]},
        "external_reference": str(pedido["id"]),
        "notification_url": url_publica("mp.webhook", p=pedido["id"]),
        "back_urls": {k: url_publica("mp.retorno_pagamento", pedido=pedido["id"]) for k in ("success", "failure", "pending")},
        "auto_return": "approved",
        "statement_descriptor": "PLACEMARK",
    }
    if conta == "vendedor" and not empresa["oficial"] and pedido["comissao"] > 0:
        dados["marketplace_fee"] = round(pedido["comissao"] / 100, 2)  # a comissão da Placemark
    resposta = chamar("POST", "/checkout/preferences", token=token, dados=dados)
    link = (resposta.get("sandbox_init_point") if modo_teste() else None) or resposta.get("init_point")
    if not link:
        raise ErroMP("O Mercado Pago não devolveu o link de pagamento.")
    conexao = db.get_db()
    conexao.execute("UPDATE pedidos SET mp_preferencia_id = ? WHERE id = ?", (resposta.get("id"), pedido["id"]))
    conexao.commit()
    return link


def _pedido_completo(pedido_id):
    return db.get_db().execute(
        """SELECT p.*, e.oficial, e.mp_access_token, e.mp_refresh_token, e.mp_expira_em, e.id AS emp_id
           FROM pedidos p JOIN empresas e ON e.id = p.empresa_id WHERE p.id = ?""", (pedido_id,)).fetchone()


def _empresa(empresa_id):
    return db.get_db().execute("SELECT * FROM empresas WHERE id = ?", (empresa_id,)).fetchone()


def _metodo(pagamento):
    if pagamento.get("payment_method_id") == "pix":
        return "pix"
    if pagamento.get("payment_type_id") == "ticket":
        return "boleto"
    return "cartao"


def aplicar_pagamento(pedido, pagamento):
    """Aplica o status de um pagamento JÁ CONFIRMADO pela API. Pode rodar várias vezes (idempotente)."""
    from aluno import conceder_acesso, revogar_acesso

    if str(pagamento.get("external_reference")) != str(pedido["id"]):
        return "ignorado"
    valor_pago = round(float(pagamento.get("transaction_amount") or 0) * 100)
    if valor_pago != pedido["valor"] or pagamento.get("currency_id", "BRL") != "BRL":
        seguranca.registrar("pagamento_divergente", f"Pedido #{pedido['id']}: pago {valor_pago}, esperado {pedido['valor']}")
        return "divergente"

    status = pagamento.get("status")
    conexao = db.get_db()
    pid = str(pagamento.get("id"))
    if status == "approved":
        # Só um aviso consegue mudar de "pendente" para "pago": evita liberar duas vezes
        mudou = conexao.execute(
            """UPDATE pedidos SET status = 'pago', mp_pagamento_id = ?, metodo = ?, atualizado_em = datetime('now', 'localtime')
               WHERE id = ? AND status IN ('pendente', 'recusado')""",
            (pid, _metodo(pagamento), pedido["id"])).rowcount
        if mudou:
            conceder_acesso(pedido["aluno_id"], pedido["curso_id"], pedido["plano"])
            seguranca.registrar("pagamento_aprovado", f"Pedido #{pedido['id']} (MP {pid})", pedido["aluno_id"])
        conexao.commit()
    elif status in ("refunded", "charged_back"):
        mudou = conexao.execute(
            "UPDATE pedidos SET status = 'reembolsado', atualizado_em = datetime('now', 'localtime') WHERE id = ? AND status = 'pago'",
            (pedido["id"],)).rowcount
        if mudou:
            revogar_acesso(pedido["aluno_id"], pedido["curso_id"])
            seguranca.registrar("pagamento_estornado", f"Pedido #{pedido['id']} ({status})", pedido["aluno_id"])
        conexao.commit()
    elif status in ("rejected", "cancelled"):
        conexao.execute(
            "UPDATE pedidos SET status = 'recusado', atualizado_em = datetime('now', 'localtime') WHERE id = ? AND status = 'pendente'",
            (pedido["id"],))
        conexao.commit()
    return status


def verificar(pedido_id, pagamento_id=None):
    """Pergunta ao Mercado Pago como está o pagamento do pedido e atualiza o site."""
    pedido = _pedido_completo(pedido_id)
    if pedido is None:
        return None
    token = token_cobranca(_empresa(pedido["empresa_id"]), pedido["mp_conta"])
    if token is None:
        raise ErroMP("A conta Mercado Pago que recebeu este pedido está desconectada.")
    if pagamento_id:
        if not str(pagamento_id).isdigit():
            return None
        pagamentos = [chamar("GET", f"/v1/payments/{pagamento_id}", token=token)]
    else:
        busca = chamar("GET", "/v1/payments/search", token=token,
                       params={"external_reference": str(pedido_id), "sort": "date_created", "criteria": "desc"})
        pagamentos = busca.get("results") or []
    # Um aprovado vale mais que tentativas recusadas do mesmo pedido
    pagamentos.sort(key=lambda p: {"approved": 0, "refunded": 1, "charged_back": 1}.get(p.get("status"), 2))
    resultado = None
    for pagamento in pagamentos[:1]:
        resultado = aplicar_pagamento(_pedido_completo(pedido_id), pagamento)
    return resultado


# =====================================================================
# Rotas
# =====================================================================
@bp.route("/mercadopago/webhook", methods=("POST", "GET"))
def webhook():
    """O Mercado Pago chama aqui quando um pagamento muda. Nunca confiamos no conteúdo do aviso:
    só usamos o número do pagamento para CONSULTAR a API com o token do vendedor."""
    espera = seguranca.bloqueado("webhook-ip", seguranca.ip())
    if espera:
        return "muitas requisicoes", 429
    seguranca.registrar_tentativa("webhook-ip", seguranca.ip())
    corpo = request.get_json(silent=True) or {}
    tipo = corpo.get("type") or request.args.get("type") or request.args.get("topic")
    pagamento_id = (corpo.get("data") or {}).get("id") or request.args.get("data.id") or request.args.get("id")
    pedido_id = request.args.get("p", type=int)
    if tipo != "payment" or not pagamento_id or not pedido_id:
        return "ok", 200  # outros avisos (merchant_order etc.) são ignorados
    try:
        verificar(pedido_id, pagamento_id)
    except ErroMP:
        return "tente de novo", 503   # o Mercado Pago reenvia depois
    return "ok", 200


@bp.route("/pagamento/retorno")
@papel_obrigatorio("aluno")
def retorno_pagamento():
    """Para onde o aluno volta depois de pagar no Mercado Pago."""
    pedido = db.get_db().execute("SELECT * FROM pedidos WHERE id = ? AND aluno_id = ?",
                                 (request.args.get("pedido", type=int), g.usuario["id"])).fetchone()
    if pedido is None:
        abort(404)
    pagamento_id = request.args.get("payment_id") or request.args.get("collection_id")
    if pagamento_id and pagamento_id != "null" and pedido["status"] != "pago":
        try:
            verificar(pedido["id"], pagamento_id)
        except ErroMP as erro:
            flash(str(erro), "erro")
    return _mostrar_resultado(pedido["id"])


@bp.route("/pedido/<int:pedido_id>/verificar", methods=("POST",))
@papel_obrigatorio("aluno")
def verificar_pedido(pedido_id):
    pedido = db.get_db().execute("SELECT * FROM pedidos WHERE id = ? AND aluno_id = ?",
                                 (pedido_id, g.usuario["id"])).fetchone()
    if pedido is None:
        abort(404)
    if pedido["status"] in ("pendente", "recusado"):
        try:
            verificar(pedido_id)
        except ErroMP as erro:
            flash(str(erro), "erro")
            return redirect(url_for("aluno.meus_cursos"))
    return _mostrar_resultado(pedido_id)


def _mostrar_resultado(pedido_id):
    pedido = db.get_db().execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,)).fetchone()
    if pedido["status"] == "pago":
        flash("Pagamento aprovado! Seu acesso já está liberado.", "sucesso")
        return redirect(url_for("aluno.aprender", curso_id=pedido["curso_id"]))
    if pedido["status"] == "recusado":
        flash("O pagamento não foi aprovado. Você pode tentar de novo com outra forma de pagamento.", "erro")
        return redirect(url_for("curso", curso_id=pedido["curso_id"]))
    flash("Recebemos seu pedido. Assim que o Mercado Pago confirmar o pagamento, o curso é liberado "
          "(Pix costuma levar segundos; boleto, até 3 dias úteis).", "aviso")
    return redirect(url_for("aluno.meus_cursos"))


# ---------- Vendedor: conectar a conta Mercado Pago ----------
@bp.route("/empresa/recebimentos")
@empresa_aprovada
def recebimentos():
    """Endereço antigo: agora tudo fica na página Saque."""
    return redirect(url_for("mp.sacar"))


@bp.route("/empresa/sacar")
@empresa_aprovada
def sacar():
    """Endereço antigo: o saque agora fica no painel Financeiro."""
    return redirect(url_for("saque.painel"))


@bp.route("/empresa/recebimentos/conectar", methods=("POST",))
@papel_obrigatorio("empresa")
def conectar():
    if g.empresa is None or g.empresa["status"] != "aprovada":
        abort(403)
    if not oauth_pronto():
        flash("Os recebimentos pelo Mercado Pago ainda não foram ativados pela Placemark. Tente mais tarde.", "aviso")
        return redirect(url_for("mp.sacar"))
    estado = secrets.token_urlsafe(24)
    session["mp_estado"] = estado
    session["mp_estado_empresa"] = g.empresa["id"]
    parametros = {
        "client_id": cfg("mp_client_id"), "response_type": "code", "platform_id": "mp",
        "state": estado, "redirect_uri": url_publica("mp.oauth_retorno"),
    }
    return redirect(URL_AUTORIZAR + "?" + urllib.parse.urlencode(parametros))


@bp.route("/mercadopago/retorno")
@papel_obrigatorio("empresa")
def oauth_retorno():
    estado = session.pop("mp_estado", None)
    empresa_id = session.pop("mp_estado_empresa", None)
    recebido = request.args.get("state", "")
    if not estado or not recebido or not secrets.compare_digest(estado, recebido) \
            or g.empresa is None or empresa_id != g.empresa["id"]:
        flash("Conexão não confirmada (link expirado ou inválido). Clique em Conectar de novo.", "erro")
        return redirect(url_for("mp.sacar"))
    codigo = request.args.get("code")
    if not codigo:
        flash("Você não autorizou a conexão com o Mercado Pago.", "aviso")
        return redirect(url_for("mp.sacar"))
    dados = {
        "client_id": cfg("mp_client_id"), "client_secret": cfg("mp_client_secret"),
        "grant_type": "authorization_code", "code": codigo, "redirect_uri": url_publica("mp.oauth_retorno"),
    }
    if modo_teste():
        dados["test_token"] = "true"
    try:
        resposta = chamar("POST", "/oauth/token", dados=dados)
        if not resposta.get("access_token"):
            raise ErroMP("O Mercado Pago não liberou o acesso.")
    except ErroMP as erro:
        flash(f"Não deu para conectar: {erro}", "erro")
        return redirect(url_for("mp.sacar"))
    _guardar_tokens(g.empresa["id"], resposta)
    _guardar_titular(g.empresa["id"], resposta["access_token"])
    seguranca.registrar("mp_conectado", f"{g.empresa['nome_fantasia']} (MP {resposta.get('user_id')})")
    if modo_carteira():
        flash("Mercado Pago conectado! Agora você já pode sacar o seu saldo.", "sucesso")
    else:
        flash("Mercado Pago conectado! As próximas vendas caem direto na sua conta.", "sucesso")
    return redirect(url_for("mp.sacar"))


@bp.route("/empresa/recebimentos/desconectar", methods=("POST",))
@papel_obrigatorio("empresa")
def desconectar():
    if g.empresa is None:
        abort(403)
    conexao = db.get_db()
    conexao.execute("""UPDATE empresas SET mp_user_id = NULL, mp_access_token = NULL, mp_refresh_token = NULL,
                       mp_public_key = NULL, mp_expira_em = NULL, mp_conectado_em = NULL,
                       mp_documento = NULL, mp_documento_tipo = NULL, mp_email = NULL, mp_nome = NULL
                       WHERE id = ?""", (g.empresa["id"],))
    conexao.commit()
    seguranca.registrar("mp_desconectado", g.empresa["nome_fantasia"])
    if modo_carteira():
        flash("Mercado Pago desconectado. Suas vendas continuam normais; para sacar, conecte de novo.", "aviso")
    else:
        flash("Mercado Pago desconectado. Seus cursos ficam sem botão de compra até você conectar de novo.", "aviso")
    return redirect(url_for("mp.sacar"))


# ---------- Admin: credenciais da Placemark ----------
@bp.route("/admin/mercadopago", methods=("GET", "POST"))
@papel_obrigatorio("admin")
def admin_config():
    if request.method == "POST":
        if request.form.get("acao") == "testar":
            try:
                conta = chamar("GET", "/users/me", token=cfg("mp_access_token"))
                flash(f"Conexão OK! Conta Mercado Pago: {conta.get('nickname') or conta.get('email') or conta.get('id')}.", "sucesso")
            except ErroMP as erro:
                flash(f"Não funcionou: {erro} Confira o Access Token.", "erro")
            return redirect(url_for("mp.admin_config"))

        site = request.form.get("mp_site_url", "").strip().rstrip("/")
        client_id = request.form.get("mp_client_id", "").strip()
        secret = request.form.get("mp_client_secret", "").strip()
        token = request.form.get("mp_access_token", "").strip()
        erros = []
        if site and not site.startswith("https://"):
            erros.append("O endereço do site precisa começar com https:// (o Mercado Pago não aceita http nem 127.0.0.1).")
        if client_id and not client_id.isdigit():
            erros.append("O Client ID é só números (ex.: 1234567890123456).")
        if token and not token.startswith(("APP_USR-", "TEST-")):
            erros.append("O Access Token começa com APP_USR- (produção) ou TEST- (teste).")
        if erros:
            for e in erros:
                flash(e, "erro")
            return redirect(url_for("mp.admin_config"))
        salvar_cfg("mp_site_url", site)
        salvar_cfg("mp_client_id", client_id)
        if secret:   # campo vazio = mantém o que já estava salvo
            salvar_cfg("mp_client_secret", secret)
        if token:
            salvar_cfg("mp_access_token", token)
        salvar_cfg("mp_modo_teste", "1" if request.form.get("mp_modo_teste") else "0")
        if request.form.get("apagar_segredos"):
            salvar_cfg("mp_client_secret", "")
            salvar_cfg("mp_access_token", "")
        db.get_db().commit()
        seguranca.registrar("mp_configurado", f"site={site} teste={'sim' if request.form.get('mp_modo_teste') else 'não'}")
        flash("Configuração do Mercado Pago salva.", "sucesso")
        return redirect(url_for("mp.admin_config"))

    conexao = db.get_db()
    conectados = conexao.execute(
        "SELECT COUNT(*) FROM empresas WHERE oficial = 0 AND mp_access_token IS NOT NULL").fetchone()[0]
    sem_conta = conexao.execute(
        """SELECT nome_fantasia FROM empresas WHERE oficial = 0 AND status = 'aprovada'
           AND mp_access_token IS NULL ORDER BY id DESC LIMIT 20""").fetchall()
    valores = {k: cfg(k) for k in CAMPOS}
    via_ambiente = {k for k, env in CAMPOS.items() if os.environ.get(env)}
    redirect_uri = url_publica("mp.oauth_retorno") if cfg("mp_site_url") else None
    return render_template("admin/mercadopago.html", v=valores, mascarar=mascarar, via_ambiente=via_ambiente,
                           ativo=ativo(), oauth_pronto=oauth_pronto(), conectados=conectados,
                           sem_conta=sem_conta, redirect_uri=redirect_uri, carteira=modo_carteira())
