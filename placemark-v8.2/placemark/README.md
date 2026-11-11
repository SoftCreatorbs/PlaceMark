# Placemark — Marketplace de cursos

Visual escuro no estilo marketplace. A logo (pino) e o ícone ficam em `static/marca/` (SVG e PNG).

Qualquer pessoa ou empresa pode vender cursos (assinatura mensal ou acesso vitalício). Para isso, o vendedor
paga um **plano mensal** que define quantos cursos pode ter à venda, mais uma **comissão por venda** (10% por padrão).
Alunos compram e assistem. A equipe Placemark modera tudo pelo painel de admin e vende os próprios cursos na **Loja oficial**.

## Planos dos vendedores (o admin pode editar em Admin → Planos)
| Plano | Preço | Cursos à venda |
|---|---|---|
| Standard | R$ 50/mês | 1 |
| Pro | R$ 120/mês | 5 |
| Premium | R$ 250/mês | ilimitados |

Se o plano vence, os cursos do vendedor saem do catálogo, mas quem já comprou continua com acesso.

## Checkout da Kiwify (planos dos vendedores)
1. Entre como admin e vá em **Admin → Planos**.
2. Em cada plano, cole o link do checkout da Kiwify no campo **"Link do checkout Kiwify"** (ex.: `https://pay.kiwify.com.br/AbC123`) e clique em **Salvar**.
3. Pronto: em **Vender cursos**, o botão **"Começar com..."** abre o carrinho e **"Comprar agora"** leva direto para a Kiwify.

Por enquanto, depois que o vendedor paga na Kiwify, ative o plano dele em **Admin → Vendedores → Ativar plano** (+30 dias).
O próximo passo é ligar o **webhook da Kiwify** para isso acontecer sozinho (exige o site publicado na internet).
Planos sem link continuam usando o pagamento simulado.

## Carteira Placemark e saques por Pix (padrão)
Todo pagamento de aluno cai na **conta Mercado Pago da Placemark**. A parte de cada vendedor
(venda menos a comissão) vira **saldo** dele no site, e ele saca por Pix pelo painel **Financeiro**.

- **Painel Financeiro do vendedor**: disponível para saque, a liberar, vendas acumuladas, comissão,
  quanto já sacou, gráfico de ganhos (30 dias ou 12 meses) e extrato dos saques.
- **Prazo de liberação** (padrão 7 dias): cada venda só pode ser sacada depois desse prazo, para proteger
  a Placemark de estornos e contestações de cartão.
- **Para sacar**, o vendedor precisa estar com a **conta Mercado Pago conectada** e confirmar com a senha.
  O Pix só vai para uma chave do **mesmo CPF/CNPJ** dessa conta.
- **Taxa de saque** (padrão R$ 2,00 + 0%): já sai descontada. O vendedor pede R$ 100,00 e recebe R$ 98,00.
- **Envio automático**: o site manda o Pix pela API de saques do Mercado Pago (Money Out). Cada saque tem
  uma chave de idempotência, então reenviar nunca paga duas vezes. Se o Mercado Pago recusar o envio
  (API não liberada, assinatura exigida em produção, falta de saldo na conta), o saque cai na **fila manual**.
- **Fila manual (Admin → Saques)**: a equipe faz o Pix pelo app do Mercado Pago e clica em **Marcar pago**
  com o código do comprovante. Também dá para reenviar, consultar ou cancelar (o valor volta ao saldo).
- Tudo é ajustável em **Admin → Saques → Regras de saque**, inclusive voltar para o **split automático**.

Antes de ligar em produção: peça ao Mercado Pago a liberação da API de saques (Pix) para a sua aplicação e
as chaves para assinar os envios. Guardar dinheiro de terceiros também tem regras próprias: confirme com
o seu contador e com o Mercado Pago como enquadrar a operação.

## Pagamento real com Mercado Pago
No modo **split** (opcional), o próprio Mercado Pago divide o dinheiro na hora:
**a parte do vendedor cai na conta Mercado Pago dele** e **a comissão cai na conta da Placemark**.
No modo **carteira** (padrão), tudo cai na conta da Placemark e o vendedor saca como explicado acima.

1. **Admin → Mercado Pago**: cole o endereço público do site (https), o Client ID, o Client Secret e o
   Access Token da aplicação da Placemark (criada em mercadopago.com.br/developers → Suas integrações).
2. Na aplicação do Mercado Pago, em **URLs de redirecionamento**, cole o endereço que aparece na página do admin
   (`https://seu-site/mercadopago/retorno`).
3. Cada vendedor entra em **Painel → Financeiro → Conectar Mercado Pago** e autoriza.
4. Pronto: os botões de compra passam a abrir o checkout do Mercado Pago. O curso é liberado quando o
   Mercado Pago avisa o site, e o site **confere o pagamento direto na API** antes de liberar (aviso falso não libera).

Comece com o **Modo teste** marcado e as credenciais de teste. O Mercado Pago **não funciona com 127.0.0.1**:
o site precisa estar publicado (ou num túnel https, como o Cloudflare Tunnel) para os avisos chegarem.
Sem Mercado Pago configurado, o site continua com o pagamento simulado, mas **só no seu computador**:
com `PRODUCAO=1` o simulado é desligado. No servidor, dá para usar variáveis de ambiente em vez da página:
`MP_SITE_URL`, `MP_CLIENT_ID`, `MP_CLIENT_SECRET`, `MP_ACCESS_TOKEN`, `MP_MODO_TESTE=1`.

## Como rodar (Windows, no terminal do VS Code, dentro desta pasta)
Só na primeira vez no computador:
```
python -m pip install flask
```
Sempre que quiser ligar o site:
```
python app.py
```
Abra http://127.0.0.1:5000

- **Na primeira vez**, o site cria o banco sozinho e abre a tela **"Criar administrador"** no navegador.
  Lá você cria sua conta de admin e escolhe se quer dados de exemplo. Essa tela só aparece uma vez.
- **Não é preciso rodar `init-db` nem `create-admin`.**
- Os dados ficam em `C:\Users\<você>\PlacemarkDados\placemark.sqlite`, **fora da pasta do site**.
  Para atualizar o site, extraia a versão nova e rode `python app.py`: usuários, cursos e vendas continuam lá.
- Se a pasta antiga tiver o banco em `instance/`, ele é trazido automaticamente na primeira vez.
- Para desligar o site: Ctrl + C no terminal (cuidado: Ctrl + C no terminal desliga, não copia).

Comandos opcionais:
| Comando | Para quê |
|---|---|
| `python -m flask --app app seed-demo` | Cria o vendedor de exemplo (empresa@demo.com / Demo1234) |
| `python -m flask --app app create-admin` | Cria mais um admin para a equipe |
| `python -m flask --app app init-db` | ⚠️ APAGA tudo e começa do zero (pede confirmação) |

## Os 3 tipos de conta
- **Aluno**: compra cursos (pagamento simulado), acessa "Meus cursos" e assiste às aulas.
- **Vendedor** (empresa ou pessoa física): cadastra-se em "Vender cursos", espera a aprovação, assina um plano, cria cursos e aulas e acompanha as vendas.
- **Admin**: só pode ser criado pelo terminal. Aprova vendedores e cursos, edita os planos, bloqueia contas, vê toda a receita e muda a comissão.
  Pelo botão **"Loja oficial"**, o admin cria cursos da Placemark sem plano e sem análise.

## Fluxo de um curso
Rascunho → (vendedor com plano envia) Em análise → (admin aprova) Publicado
                                      ↘ (admin rejeita, com motivo) Rejeitado → empresa edita e reenvia

## Estrutura
- `app.py`: páginas públicas (início, catálogo, curso, vender)
- `auth.py`: login, cadastro de aluno, permissões por papel e CSRF
- `aluno.py`: checkout (Mercado Pago ou simulado), meus cursos e sala de aula
- `mercadopago.py`: pagamento real, conexão dos vendedores (OAuth), webhook, modo carteira ou split
- `saques.py`: saldo dos vendedores, painel Financeiro, saques por Pix (API do Mercado Pago) e fila do admin
- `empresa.py`: painel da empresa
- `admin.py`: painel do admin
- `db.py`: banco (criação e atualização automáticas, lista `MIGRACOES`) e comandos
- `util.py`: dinheiro, links de vídeo, faixas de preço e ordenação do catálogo
- `templates/_icones.html`: ícones próprios em SVG (use `{{ ic('nome') }}` nos templates)
- `templates/_capa.html`: capas geradas dos cursos (cor e padrão por curso)
- `schema.sql`: tabelas (valores em centavos)

## Ainda falta (próximas etapas)
1. Cobrança mensal automática de alunos e vendedores (hoje a renovação é manual)
2. Planos dos vendedores também pelo Mercado Pago (hoje é Kiwify + ativação manual)
3. "Esqueci minha senha" e e-mails de aviso
4. Publicar online (HTTPS, SECRET_KEY e PostgreSQL)

## Segurança

### O que já protege o site
| Ataque | Proteção |
|---|---|
| Vazamento de senhas | Senhas guardadas só como hash (scrypt/pbkdf2). Senhas fáceis ("senha123") são recusadas |
| Adivinhar senha (força bruta) | 5 senhas erradas no mesmo e-mail ou 20 no mesmo IP = 15 min de espera |
| Formulário falso em outro site (CSRF) | Todo formulário tem um código secreto por sessão |
| Injeção de SQL | Todas as consultas usam parâmetros (`?`), nunca texto colado |
| Injeção de código na página (XSS) | Textos sempre escapados + Content-Security-Policy com nonce |
| Site dentro de iframe (clickjacking) | `X-Frame-Options: DENY` e `frame-ancestors 'none'` |
| Redirecionar para site falso | Só aceita destinos internos (`/algo`) |
| Sequestro de sessão | Sessão nova a cada login; admin expira em 12 h; conta bloqueada é expulsa na hora |
| Cookie roubado/forjado | Chave secreta aleatória única, cookie HttpOnly + SameSite (+ Secure em produção) |
| Perda de dados | Backup diário automático (14 dias) em `PlacemarkDados/backups` + botão "Fazer backup agora" |

O admin vê tudo em **Admin → Segurança**: senhas erradas, bloqueios, IPs suspeitos e quem fez cada ação.

### Ao publicar na internet (obrigatório)
1. **Nunca** rode `python app.py` no servidor (é o modo de teste). Use o Waitress:
   ```
   python -m pip install waitress
   set PRODUCAO=1
   set PROXY=1
   python -m waitress --listen=0.0.0.0:8000 app:app
   ```
   (`PROXY=1` só se estiver atrás de Render, Railway, Cloudflare etc.)
2. **HTTPS** obrigatório (Render/Railway já dão; ou coloque o domínio na Cloudflare).
3. Guarde a pasta `PlacemarkDados` (banco, backups e `.chave_secreta`) fora do alcance público e faça cópia dos backups em outro lugar (Google Drive, por exemplo).
4. Mantenha o Python e o Flask atualizados: `python -m pip install -U flask waitress`.
5. Use senhas fortes e únicas nas contas de admin e ative verificação em 2 etapas no e-mail da equipe.
