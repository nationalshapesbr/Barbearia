# ┌──────────────────────────────────────────────────────────────────┐
# │                     1. IMPORTS E DEPENDÊNCIAS                    │
# └──────────────────────────────────────────────────────────────────┘
import sqlite3,hashlib,os,secrets
from datetime import date,datetime,timedelta
from functools import wraps
from flask import Flask,render_template,request,redirect,url_for,session,jsonify,g,flash

# ┌──────────────────────────────────────────────────────────────────┐
# │                     2. CONFIGURAÇÕES DO APP                      │
# └──────────────────────────────────────────────────────────────────┘
app=Flask(__name__)
app.secret_key=os.environ.get("SECRET_KEY") or secrets.token_hex(32) # Chave secreta para sessões
SENHA_HASH=hashlib.sha256(os.environ.get("ADMIN_PASS","admin123").encode()).hexdigest() # Hash da senha admin
DB=os.path.join("/tmp" if os.environ.get("RENDER") else ".","gustico.db") # Caminho do banco SQLite
LIMITE=25 # Limite máximo de clientes na fila
LOGIN_TENTATIVAS={} # Controle de tentativas de login: {ip: {"count": N, "bloqueado_ate": datetime}}

# ┌──────────────────────────────────────────────────────────────────┐
# │                  3. FILTROS DE TEMPLATE (VISUAL)                 │
# │        Formatam datas e horas para exibição no HTML              │
# └──────────────────────────────────────────────────────────────────┘
@app.template_filter("datebr") # Converte data para formato BR (dd/mm/aaaa)
def datebr_filter(v):
    if not v or len(str(v))<10: return v or ""
    s=str(v)[:10]; return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"

@app.template_filter("horabr") # Extrai hora no formato HH:MM
def horabr_filter(v):
    if not v or len(str(v))<16: return v or ""
    return str(v)[11:16]

@app.template_filter("duracaobr") # Calcula duração entre dois timestamps (ex: 1h30min)
def duracaobr_filter(ini,fim):
    try:
        a=datetime.strptime(str(ini)[:19],"%Y-%m-%d %H:%M:%S")
        b=datetime.strptime(str(fim)[:19],"%Y-%m-%d %H:%M:%S")
        m=int((b-a).total_seconds())//60
        return f"{m//60}h{m%60:02d}min" if m>=60 else f"{m}min"
    except(ValueError,TypeError): return "—"

@app.template_filter("brl") # Formata número no padrão BR: 1.234.567,89
def brl_filter(v,decimais=2):
    try:
        n=float(v)
        if decimais==0:
            s=f"{n:,.0f}".replace(",","X").replace(".",",").replace("X",".")
        else:
            s=f"{n:,.2f}".replace(",","X").replace(".",",").replace("X",".")
        return s
    except(ValueError,TypeError): return "0,00"

# ┌──────────────────────────────────────────────────────────────────┐
# │                 4. BANCO DE DADOS — CONEXÃO                      │
# │        Abre, fecha e inicializa o banco SQLite                   │
# └──────────────────────────────────────────────────────────────────┘
def get_db(): # Conexão reutilizável por request via flask.g
    if "db" not in g:
        g.db=sqlite3.connect(DB); g.db.row_factory=sqlite3.Row; g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext # Fecha conexão ao final de cada request
def close_db(_=None):
    db=g.pop("db",None)
    if db: db.close()

def init_db(): # Cria tabelas e dados iniciais (executado ao iniciar o app)
    db=sqlite3.connect(DB); db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS servicos(id INTEGER PRIMARY KEY AUTOINCREMENT,nome TEXT NOT NULL,preco REAL NOT NULL DEFAULT 0,duracao INTEGER NOT NULL DEFAULT 30,ativo INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS fila(id INTEGER PRIMARY KEY AUTOINCREMENT,nome TEXT NOT NULL,data TEXT NOT NULL,ordem INTEGER NOT NULL,status TEXT DEFAULT 'aguardando',chegada TEXT DEFAULT(datetime('now','localtime')),inicio TEXT,fim TEXT);
    CREATE TABLE IF NOT EXISTS fila_servicos(fila_id INTEGER REFERENCES fila(id) ON DELETE CASCADE,servico_id INTEGER REFERENCES servicos(id),PRIMARY KEY(fila_id,servico_id));
    CREATE TABLE IF NOT EXISTS pagamentos(id INTEGER PRIMARY KEY AUTOINCREMENT,fila_id INTEGER REFERENCES fila(id),valor REAL NOT NULL,metodo TEXT DEFAULT 'dinheiro',pago_em TEXT DEFAULT(datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS config(chave TEXT PRIMARY KEY,valor TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS caixa_historico(id INTEGER PRIMARY KEY AUTOINCREMENT,abertura TEXT NOT NULL,fechamento TEXT,total REAL DEFAULT 0,atendimentos INTEGER DEFAULT 0);""")
    for c,v in [("fila_aberta","1"),("fila_fechada_manual","0"),("caixa_aberto","0"),("caixa_abertura",""),("caixa_fechamento","")]:
        db.execute("INSERT OR IGNORE INTO config VALUES(?,?)",(c,v))
    for n,p,d in [("Corte Simples",35,30),("Corte Degradê",45,40),("Corte + Barba",65,60),("Barba",30,25),("Corte Infantil",30,25),("Corte Feminino",50,45)]:
        db.execute("INSERT INTO servicos(nome,preco,duracao) SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM servicos WHERE nome=?)",(n,p,d,n))
    db.commit(); db.close()

# ┌──────────────────────────────────────────────────────────────────┐
# │              5. BANCO DE DADOS — HELPERS / UTILITÁRIOS           │
# │        Funções auxiliares para ler/gravar configs e datas        │
# └──────────────────────────────────────────────────────────────────┘
def cfg(k,d=""): r=get_db().execute("SELECT valor FROM config WHERE chave=?",(k,)).fetchone(); return r["valor"] if r else d # Lê config
def set_cfg(k,v): get_db().execute("INSERT OR REPLACE INTO config VALUES(?,?)",(k,str(v))); get_db().commit() # Salva config
def hoje(): return date.today().isoformat() # Data de hoje (YYYY-MM-DD)
def agora(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S") # Data e hora atual

# ┌──────────────────────────────────────────────────────────────────┐
# │                   6. AUTENTICAÇÃO DO ADMIN                       │
# │        Decorador que protege rotas administrativas               │
# └──────────────────────────────────────────────────────────────────┘
def admin_required(f):
    @wraps(f)
    def w(*a,**k):
        if not session.get("admin"): return redirect(url_for("login"))
        return f(*a,**k)
    return w

# ┌──────────────────────────────────────────────────────────────────┐
# │               7. BANCO DE DADOS — LÓGICA DA FILA                │
# │        Consultas, cálculo de espera e controle automático        │
# └──────────────────────────────────────────────────────────────────┘
def pegar_fila(data,so_ativos=False): # Busca clientes com serviços e pagamentos (JOIN completo)
    filtro="AND f.status IN ('aguardando','atendendo')" if so_ativos else "AND f.status != 'cancelado'"
    return get_db().execute(f"""
        SELECT f.*,GROUP_CONCAT(s.nome,' + ') AS servicos,COALESCE(SUM(s.preco),0) AS total,
        COALESCE(SUM(s.duracao),0) AS duracao,p.id AS pago_id,p.valor AS pago_valor,p.metodo
        FROM fila f LEFT JOIN fila_servicos fs ON fs.fila_id=f.id LEFT JOIN servicos s ON s.id=fs.servico_id
        LEFT JOIN pagamentos p ON p.fila_id=f.id WHERE f.data=? {filtro}
        GROUP BY f.id ORDER BY CASE f.status WHEN 'atendendo' THEN 0 WHEN 'aguardando' THEN 1 ELSE 2 END,
        CASE WHEN f.status='concluido' THEN f.fim END ASC,f.ordem""",(data,)).fetchall()

def calc_espera(fila): # Calcula tempo estimado de espera para cada cliente
    espera,acum={},0
    for r in fila:
        dur=r["duracao"] or 30
        if r["status"] in ("concluido","atendendo"):
            espera[r["id"]]=0
            if r["status"]=="atendendo":
                if r["inicio"]:
                    try: acum+=max(0,dur-int((datetime.now()-datetime.strptime(r["inicio"],"%Y-%m-%d %H:%M:%S")).total_seconds())//60)
                    except(ValueError,TypeError): acum+=dur
                else: acum+=dur
        else: espera[r["id"]]=acum; acum+=dur
    return espera

def contar_ativos(): return get_db().execute("SELECT COUNT(*) AS c FROM fila WHERE data=? AND status IN ('aguardando','atendendo')",(hoje(),)).fetchone()["c"]

def checar_limite(): # Fecha fila automaticamente ao atingir o limite
    if contar_ativos()>=LIMITE and cfg("fila_aberta")=="1": set_cfg("fila_aberta","0"); set_cfg("fila_fechada_manual","0")

def checar_reabrir(): # Reabre fila automaticamente se abaixo do limite (não fechada manualmente)
    if contar_ativos()<LIMITE and cfg("fila_aberta")=="0" and cfg("fila_fechada_manual")!="1": set_cfg("fila_aberta","1")

def garantir_caixa(): # Abre caixa automaticamente ao registrar pagamento
    if cfg("caixa_aberto")!="1": set_cfg("caixa_aberto","1"); set_cfg("caixa_abertura",agora()); set_cfg("caixa_fechamento","")

# ┌──────────────────────────────────────────────────────────────────┐
# │              8. ROTAS PÚBLICAS — PÁGINAS DO CLIENTE              │
# │        Página inicial, entrar na fila, fila ao vivo              │
# └──────────────────────────────────────────────────────────────────┘
@app.route("/") # Página pública: formulário de entrada + fila ao vivo
def index():
    servicos=get_db().execute("SELECT * FROM servicos WHERE ativo=1 ORDER BY nome").fetchall()
    fila=pegar_fila(hoje(),so_ativos=True); espera=calc_espera(fila); now=datetime.now()
    espera_ate={k:(now+timedelta(minutes=v)).strftime("%Y-%m-%d %H:%M:%S") for k,v in espera.items() if v>0}
    return render_template("index.html",servicos=servicos,fila=fila,espera=espera,espera_ate=espera_ate,fila_aberta=cfg("fila_aberta")=="1",agora_ts=now.isoformat())

@app.route("/api/fila-publica") # API JSON: fila ao vivo para atualização automática (polling 5s)
def api_fila_publica():
    fila=pegar_fila(hoje(),so_ativos=True); espera=calc_espera(fila); now=datetime.now()
    items=[]
    for i,f in enumerate(fila,1):
        em=espera.get(f["id"],0)
        items.append({"pos":i,"nome":f["nome"],"servicos":f["servicos"] or "—","status":f["status"],
            "espera_min":em,"espera_ate":(now+timedelta(minutes=em)).strftime("%Y-%m-%d %H:%M:%S") if em>0 else "",
            "inicio":f["inicio"] or "","duracao":f["duracao"] or 30})
    return jsonify(fila=items,fila_aberta=cfg("fila_aberta")=="1",total=len(items),hora=now.strftime("%H:%M:%S"))

@app.route("/entrar",methods=["POST"]) # Adiciona cliente na fila
def entrar():
    db=get_db(); nome=request.form.get("nome","").strip(); ids=request.form.getlist("servico_ids")
    if cfg("fila_aberta")!="1": flash("Fila fechada!","erro"); return redirect(url_for("index"))
    if not nome or not ids: flash("Preencha nome e escolha um serviço.","erro"); return redirect(url_for("index"))
    if contar_ativos()>=LIMITE: set_cfg("fila_aberta","0"); flash("Fila lotada!","erro"); return redirect(url_for("index"))
    r=db.execute("SELECT COALESCE(MAX(ordem),0)+1 AS p FROM fila WHERE data=? AND status!='cancelado'",(hoje(),)).fetchone()
    cur=db.execute("INSERT INTO fila(nome,data,ordem) VALUES(?,?,?)",(nome,hoje(),r["p"]))
    for sid in ids: db.execute("INSERT OR IGNORE INTO fila_servicos VALUES(?,?)",(cur.lastrowid,int(sid)))
    db.commit(); checar_limite(); flash(f"✅ {nome}, você é o #{r['p']} da fila!","ok"); return redirect(url_for("index"))

# ┌──────────────────────────────────────────────────────────────────┐
# │             9. ROTAS ADMIN — PÁGINAS DO BARBEIRO                 │
# │        Login, logout, dashboard, financeiro, serviços            │
# └──────────────────────────────────────────────────────────────────┘
@app.route("/admin/login",methods=["GET","POST"]) # Login do admin (bloqueia após 3 tentativas por 10min)
def login():
    ip=request.remote_addr; info=LOGIN_TENTATIVAS.get(ip,{})
    if info.get("bloqueado_ate") and datetime.now()<info["bloqueado_ate"]:
        restante=int((info["bloqueado_ate"]-datetime.now()).total_seconds()//60)+1
        flash(f"Acesso bloqueado. Tente novamente em {restante} minuto(s).","erro")
        return render_template("login.html")
    if request.method=="POST":
        if hashlib.sha256(request.form.get("senha","").encode()).hexdigest()==SENHA_HASH:
            LOGIN_TENTATIVAS.pop(ip,None); session["admin"]=True; return redirect(url_for("admin_fila"))
        info["count"]=info.get("count",0)+1
        if info["count"]>=3:
            info["bloqueado_ate"]=datetime.now()+timedelta(minutes=10); info["count"]=0
            flash("3 tentativas erradas. Acesso bloqueado por 10 minutos.","erro")
        else:
            flash(f"Senha incorreta. {3-info['count']} tentativa(s) restante(s).","erro")
        LOGIN_TENTATIVAS[ip]=info
    return render_template("login.html")

@app.route("/admin/logout") # Logout do admin
def logout(): session.clear(); return redirect(url_for("login"))

@app.route("/admin") # Dashboard: gerenciamento da fila
@app.route("/admin/fila")
@admin_required
def admin_fila():
    data=request.args.get("data",hoje()); fila=pegar_fila(data)
    total=get_db().execute("SELECT COALESCE(SUM(p.valor),0) AS t FROM pagamentos p JOIN fila f ON f.id=p.fila_id WHERE f.data=?",(data,)).fetchone()["t"]
    return render_template("admin_fila.html",fila=fila,espera=calc_espera(fila),data=data,hoje=hoje(),total_dia=total,
                           fila_aberta=cfg("fila_aberta")=="1",pagina="fila",ativos=contar_ativos(),limite=LIMITE)

@app.route("/admin/financeiro") # Painel financeiro: receitas, gráficos, caixa
@admin_required
def admin_financeiro():
    db=get_db(); mes_atual=date.today().strftime("%Y-%m"); mes=request.args.get("mes",mes_atual); ontem=(date.today()-timedelta(days=1)).isoformat()
    # Lista de meses disponíveis no banco
    meses_db=db.execute("SELECT DISTINCT substr(data,1,7) AS m FROM fila WHERE status='concluido' ORDER BY m DESC").fetchall()
    meses_list=[r["m"] for r in meses_db]
    if mes not in meses_list and mes!=mes_atual: mes=mes_atual
    if mes_atual not in meses_list: meses_list.insert(0,mes_atual)
    # Nomes dos meses em PT
    nomes_mes={"01":"Janeiro","02":"Fevereiro","03":"Março","04":"Abril","05":"Maio","06":"Junho","07":"Julho","08":"Agosto","09":"Setembro","10":"Outubro","11":"Novembro","12":"Dezembro"}
    meses_fmt=[{"valor":m,"label":nomes_mes.get(m[5:],"?")+"/"+m[:4],"atual":m==mes} for m in meses_list]
    por_dia=db.execute("SELECT f.data AS dia,COALESCE(SUM(p.valor),0) AS total,COUNT(DISTINCT f.id) AS qtd FROM fila f LEFT JOIN pagamentos p ON p.fila_id=f.id WHERE f.status='concluido' AND f.data LIKE ? GROUP BY f.data ORDER BY f.data",(f"{mes}%",)).fetchall()
    total_mes=sum(r["total"] for r in por_dia)
    total_geral=db.execute("SELECT COALESCE(SUM(valor),0) AS t FROM pagamentos").fetchone()["t"]
    total_hoje=db.execute("SELECT COALESCE(SUM(p.valor),0) AS t FROM pagamentos p JOIN fila f ON f.id=p.fila_id WHERE f.data=?",(hoje(),)).fetchone()["t"]
    total_ontem=db.execute("SELECT COALESCE(SUM(p.valor),0) AS t FROM pagamentos p JOIN fila f ON f.id=p.fila_id WHERE f.data=?",(ontem,)).fetchone()["t"]
    atend_hoje=db.execute("SELECT COUNT(*) AS c FROM fila WHERE data=? AND status='concluido'",(hoje(),)).fetchone()["c"]
    atend_ontem=db.execute("SELECT COUNT(*) AS c FROM fila WHERE data=? AND status='concluido'",(ontem,)).fetchone()["c"]
    top_servicos=db.execute("""SELECT s.nome,COUNT(*) AS qtd FROM fila_servicos fs
        JOIN fila f ON f.id=fs.fila_id JOIN servicos s ON s.id=fs.servico_id
        WHERE f.status='concluido' AND f.data LIKE ? GROUP BY s.id ORDER BY qtd DESC LIMIT 6""",(f"{mes}%",)).fetchall()
    total_srv=sum(r["qtd"] for r in top_servicos) if top_servicos else 1
    top_srv=[{"nome":r["nome"],"qtd":r["qtd"],"pct":round(r["qtd"]/total_srv*100)} for r in top_servicos]
    metodos=db.execute("""SELECT p.metodo,COALESCE(SUM(p.valor),0) AS total,COUNT(*) AS qtd FROM pagamentos p
        JOIN fila f ON f.id=p.fila_id WHERE f.data LIKE ? GROUP BY p.metodo ORDER BY total DESC""",(f"{mes}%",)).fetchall()
    met_data=[{"metodo":r["metodo"],"total":r["total"],"qtd":r["qtd"]} for r in metodos]
    return render_template("admin_financeiro.html",por_dia=por_dia,total_mes=total_mes,total_geral=total_geral,
        total_hoje=total_hoje,total_ontem=total_ontem,atend_hoje=atend_hoje,atend_ontem=atend_ontem,
        top_servicos=top_srv,metodos=met_data,dias_grafico=[{"dia":r["dia"],"total":r["total"],"qtd":r["qtd"]} for r in por_dia],
        hoje=hoje(),mes_selecionado=mes,meses=meses_fmt,caixa_aberto=cfg("caixa_aberto")=="1",caixa_abertura=cfg("caixa_abertura"),caixa_fechamento=cfg("caixa_fechamento"),pagina="financeiro")

@app.route("/admin/caixa-historico") # Histórico de aberturas/fechamentos de caixa
@admin_required
def admin_caixa_historico():
    db=get_db(); de=request.args.get("de",""); ate=request.args.get("ate","")
    q,p="SELECT * FROM caixa_historico WHERE 1=1",[]
    if de: q+=" AND abertura >= ?"; p.append(de)
    if ate: q+=" AND abertura <= ?"; p.append(ate+" 23:59:59")
    return render_template("admin_caixa_historico.html",registros=db.execute(q+" ORDER BY id DESC LIMIT 100",p).fetchall(),data_de=de,data_ate=ate,pagina="financeiro")

@app.route("/admin/servicos") # Gerenciamento de serviços (CRUD)
@admin_required
def admin_servicos():
    return render_template("admin_servicos.html",servicos=get_db().execute("SELECT * FROM servicos ORDER BY nome").fetchall(),pagina="servicos")

# ┌──────────────────────────────────────────────────────────────────┐
# │           10. APIs DA FILA — AÇÕES DO ADMIN NA FILA              │
# │        Chamar, concluir, cancelar, reordenar clientes            │
# └──────────────────────────────────────────────────────────────────┘
@app.route("/api/status/<int:fid>",methods=["POST"]) # Altera status (aguardando → atendendo → concluido)
@admin_required
def api_status(fid):
    st=request.json.get("status","")
    if st not in ("aguardando","atendendo","concluido","cancelado"): return jsonify(erro="inválido"),400
    u={"status":st}
    if st=="atendendo": u["inicio"]=agora()
    if st=="concluido": u["fim"]=agora()
    get_db().execute(f"UPDATE fila SET {','.join(f'{k}=?' for k in u)} WHERE id=?",[*u.values(),fid]); get_db().commit()
    if st in ("concluido","cancelado"): checar_reabrir()
    return jsonify(ok=True)

@app.route("/api/cancelar/<int:fid>",methods=["POST"]) # Remove cliente da fila
@admin_required
def api_cancelar(fid):
    get_db().execute("UPDATE fila SET status='cancelado' WHERE id=?",(fid,)); get_db().commit(); checar_reabrir(); return jsonify(ok=True)

@app.route("/api/reordenar",methods=["POST"]) # Reordena clientes (drag & drop)
@admin_required
def api_reordenar():
    for i,fid in enumerate(request.json.get("ids",[]),1): get_db().execute("UPDATE fila SET ordem=? WHERE id=?",(i,fid))
    get_db().commit(); return jsonify(ok=True)

@app.route("/api/fila/toggle",methods=["POST"]) # Abre/fecha fila manualmente
@admin_required
def api_toggle_fila():
    abrir=request.json.get("abrir"); set_cfg("fila_aberta","1" if abrir else "0"); set_cfg("fila_fechada_manual","0" if abrir else "1"); return jsonify(ok=True)

# ┌──────────────────────────────────────────────────────────────────┐
# │          11. APIs FINANCEIRAS — PAGAMENTOS E CAIXA               │
# │        Registrar pagamento, abrir/fechar caixa                   │
# └──────────────────────────────────────────────────────────────────┘
@app.route("/api/pagar/<int:fid>",methods=["POST"]) # Registra pagamento de um cliente
@admin_required
def api_pagar(fid):
    d=request.json; db=get_db(); garantir_caixa()
    db.execute("DELETE FROM pagamentos WHERE fila_id=?",(fid,))
    db.execute("INSERT INTO pagamentos(fila_id,valor,metodo) VALUES(?,?,?)",(fid,float(d["valor"]),d.get("metodo","dinheiro"))); db.commit()
    return jsonify(ok=True)

@app.route("/api/caixa/toggle",methods=["POST"]) # Abre ou fecha o caixa (salva histórico ao fechar)
@admin_required
def api_toggle_caixa():
    abrir=request.json.get("abrir",False); db=get_db()
    if abrir: set_cfg("caixa_aberto","1"); set_cfg("caixa_abertura",agora()); set_cfg("caixa_fechamento","")
    else:
        t=db.execute("SELECT COALESCE(SUM(p.valor),0) AS t FROM pagamentos p JOIN fila f ON f.id=p.fila_id WHERE f.data=?",(hoje(),)).fetchone()["t"]
        a=db.execute("SELECT COUNT(*) AS c FROM fila WHERE data=? AND status='concluido'",(hoje(),)).fetchone()["c"]
        db.execute("INSERT INTO caixa_historico(abertura,fechamento,total,atendimentos) VALUES(?,?,?,?)",(cfg("caixa_abertura"),agora(),t,a))
        set_cfg("caixa_aberto","0"); set_cfg("caixa_fechamento",agora()); db.commit()
    return jsonify(ok=True)

# ┌──────────────────────────────────────────────────────────────────┐
# │        12. APIs DE EXCLUSÃO — LIMPEZA DE DADOS FINANCEIROS       │
# │        Excluir por dia, tudo, ou registros de caixa              │
# └──────────────────────────────────────────────────────────────────┘
@app.route("/api/financeiro/excluir-dia",methods=["POST"]) # Exclui registros financeiros de um dia específico
@admin_required
def api_excluir_dia():
    dia=request.json.get("dia",""); db=get_db()
    if not dia: return jsonify(erro="dia obrigatório"),400
    ids=[r["id"] for r in db.execute("SELECT id FROM fila WHERE data=? AND status='concluido'",(dia,)).fetchall()]
    for fid in ids: db.execute("DELETE FROM pagamentos WHERE fila_id=?",(fid,))
    db.execute("DELETE FROM fila_servicos WHERE fila_id IN (SELECT id FROM fila WHERE data=? AND status='concluido')",(dia,))
    db.execute("DELETE FROM fila WHERE data=? AND status='concluido'",(dia,))
    db.commit(); return jsonify(ok=True)

@app.route("/api/financeiro/excluir-tudo",methods=["POST"]) # Exclui TODOS os registros financeiros
@admin_required
def api_excluir_tudo():
    db=get_db()
    db.execute("DELETE FROM pagamentos")
    db.execute("DELETE FROM fila_servicos WHERE fila_id IN (SELECT id FROM fila WHERE status='concluido')")
    db.execute("DELETE FROM fila WHERE status='concluido'")
    db.execute("DELETE FROM caixa_historico")
    db.commit(); return jsonify(ok=True)

@app.route("/api/caixa-historico/<int:cid>/excluir",methods=["POST"]) # Exclui um registro de caixa individual
@admin_required
def api_excluir_caixa(cid):
    get_db().execute("DELETE FROM caixa_historico WHERE id=?",(cid,)); get_db().commit(); return jsonify(ok=True)

@app.route("/api/caixa-historico/excluir-tudo",methods=["POST"]) # Limpa todo o histórico de caixa
@admin_required
def api_excluir_caixa_tudo():
    get_db().execute("DELETE FROM caixa_historico"); get_db().commit(); return jsonify(ok=True)

# ┌──────────────────────────────────────────────────────────────────┐
# │             13. APIs DE SERVIÇOS — CRUD DE SERVIÇOS              │
# │        Criar, editar, ativar/desativar serviços                  │
# └──────────────────────────────────────────────────────────────────┘
@app.route("/api/servico",methods=["POST"]) # Cria novo serviço
@admin_required
def api_novo_servico():
    d=request.json; get_db().execute("INSERT INTO servicos(nome,preco,duracao) VALUES(?,?,?)",(d["nome"],float(d["preco"]),int(d.get("duracao",30)))); get_db().commit(); return jsonify(ok=True)

@app.route("/api/servico/<int:sid>",methods=["PUT"]) # Edita serviço existente
@admin_required
def api_editar_servico(sid):
    d=request.json; get_db().execute("UPDATE servicos SET nome=?,preco=?,duracao=?,ativo=? WHERE id=?",(d["nome"],float(d["preco"]),int(d.get("duracao",30)),int(d.get("ativo",1)),sid)); get_db().commit(); return jsonify(ok=True)

@app.route("/api/servico/<int:sid>/toggle",methods=["POST"]) # Ativa/desativa serviço
@admin_required
def api_toggle_servico(sid):
    get_db().execute("UPDATE servicos SET ativo=? WHERE id=?",(int(request.json.get("ativo",1)),sid)); get_db().commit(); return jsonify(ok=True)

# ┌──────────────────────────────────────────────────────────────────┐
# │                    14. INICIALIZAÇÃO DO APP                      │
# └──────────────────────────────────────────────────────────────────┘
init_db() # Cria tabelas e dados iniciais no banco
if __name__=="__main__": app.run(debug=True,port=5000) # Servidor de desenvolvimento na porta 5000
