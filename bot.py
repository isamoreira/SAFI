import os
import csv
import json
import uuid
import asyncio
import threading
import unicodedata
from datetime import datetime, date, timedelta
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import (ApplicationBuilder, MessageHandler, CommandHandler,
                          filters, ContextTypes)

PASTA = Path(__file__).parent
load_dotenv(PASTA / "token.env")

TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED = int(os.environ["ALLOWED_CHAT_ID"])
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-3.8-flash").split(",")]
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

HORA_RESUMO = 8              # resumo do dia no Telegram, a partir das 8h
SO_AVISO_1H = {"ACADEMIA"}   # categorias de rotina: só o aviso de 1 hora antes

ARQ_NOTAS = PASTA / "notas.md"
ARQ_AGENDA = PASTA / "agenda.json"
ARQ_ESTADO = PASTA / "estado.json"
ARQ_FREELA = PASTA / "freela.csv"
ARQ_CANCELADOS = PASTA / "cancelados.json"      # eventos cancelados ficam guardados aqui
ARQ_EXCECOES = PASTA / "excecoes_rotina.json"   # dias em que a rotina fixa não vale

LOCK = threading.Lock()   # evita dois trechos mexendo nos arquivos ao mesmo tempo
CATEGORIAS = ["TRABALHO", "MESTRADO", "INGLES", "ACADEMIA", "FREELA", "OUTRO"]
DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
        "sexta-feira", "sábado", "domingo"]
DIAS_CURTOS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
DIAS_MAP = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4,
            "sab": 5, "sáb": 5, "dom": 6}

# ============================================================================
# ROTINA FIXA DA ISABELLA  (fica só aqui no bot; NÃO vai para o Google Agenda)
# dias: 0=seg 1=ter 2=qua 3=qui 4=sex 5=sáb 6=dom
# bloqueia=True  -> não deixa marcar outro evento nesse horário sem confirmação
# aviso=True     -> avisa pelo Telegram 1h antes do início
# pergunta_fim   -> ao terminar o bloco, pergunta para quem foi o freela
# ============================================================================
ROTINA_FIXA = [
    {"dias": [0], "inicio": "11:00", "fim": "12:00", "titulo": "Natação",
     "categoria": "ACADEMIA", "bloqueia": True, "aviso": True},
    {"dias": [1], "inicio": "11:00", "fim": "12:00", "titulo": "Academia",
     "categoria": "ACADEMIA", "bloqueia": True, "aviso": True},
    {"dias": [1], "inicio": "13:40", "fim": "16:40", "titulo": "Aula do mestrado",
     "categoria": "MESTRADO", "bloqueia": True, "aviso": True},
    {"dias": [2, 3, 4], "inicio": "18:00", "fim": "19:00", "titulo": "Academia",
     "categoria": "ACADEMIA", "bloqueia": True, "aviso": True},
    {"dias": [2, 3, 4], "inicio": "09:00", "fim": "17:00", "titulo": "FUNDACE",
     "categoria": "TRABALHO", "bloqueia": False, "aviso": False},
    {"dias": [2, 3], "inicio": "13:00", "fim": "17:00", "titulo": "Freela dos alunos",
     "categoria": "FREELA", "bloqueia": False, "aviso": False, "pergunta_fim": True},
]


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower())
                   if unicodedata.category(c) != "Mn")


# ---------------- ARQUIVOS (por enquanto no lugar do Google) ----------------
def _escrever_json(caminho: Path, dados) -> None:
    """Grava em arquivo temporário e troca no fim: nunca deixa o arquivo pela metade."""
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, caminho)


def _ler_json(caminho: Path, padrao):
    if caminho.exists():
        return json.loads(caminho.read_text(encoding="utf-8"))
    return padrao


def _ler_agenda() -> list:
    eventos = _ler_json(ARQ_AGENDA, [])
    novos = False
    for e in eventos:
        if "id" not in e:            # eventos antigos ganham um id
            e["id"] = uuid.uuid4().hex[:6]
            novos = True
    if novos:
        _escrever_json(ARQ_AGENDA, eventos)
    return eventos


def _gravar_agenda(eventos: list) -> None:
    _escrever_json(ARQ_AGENDA, eventos)


def _ler_estado() -> dict:
    return _ler_json(ARQ_ESTADO, {})


def _gravar_estado(estado: dict) -> None:
    _escrever_json(ARQ_ESTADO, estado)


def _excecoes() -> list:
    return _ler_json(ARQ_EXCECOES, [])


def _rotina_do_dia(d: date) -> list:
    """Rotina fixa que vale naquele dia (descontando os dias desmarcados)."""
    desmarcadas = {(x["data"], x["titulo"]) for x in _excecoes()}
    return [r for r in ROTINA_FIXA
            if d.weekday() in r["dias"] and (d.isoformat(), r["titulo"]) not in desmarcadas]


def _rotina_texto() -> str:
    partes = []
    for r in ROTINA_FIXA:
        dias = ",".join(DIAS_CURTOS[i] for i in r["dias"])
        partes.append(f"{r['titulo']} ({dias}) {r['inicio']}-{r['fim']} [{r['categoria']}]")
    return "; ".join(partes)


def _categoria(c: str):
    c = (c or "OUTRO").upper().strip()
    return c if c in CATEGORIAS else None


def _validar(data: str, hora: str):
    """Devolve uma mensagem de erro se data/hora estiverem em formato errado."""
    try:
        datetime.strptime(data, "%Y-%m-%d")
        if hora:
            datetime.strptime(hora, "%H:%M")
    except ValueError:
        return "Data deve ser AAAA-MM-DD e hora HH:MM. Nada foi alterado."
    return None


def _norm_hora(hora: str) -> str:
    return datetime.strptime(hora, "%H:%M").strftime("%H:%M") if hora else ""


def _checar_horario(eventos: list, data: str, hora: str, cat: str, ignorar_id: str = ""):
    """Devolve (mensagem_de_conflito_ou_None, observação)."""
    if not hora:
        return None, ""
    for e in eventos:
        if e.get("id") != ignorar_id and e["data"] == data and e["hora"] == hora:
            return f"CONFLITO: já existe '{e['titulo']}' em {data} às {hora}.", ""
    dia = datetime.strptime(data, "%Y-%m-%d").date()
    nota = ""
    for r in _rotina_do_dia(dia):
        if r["inicio"] <= hora < r["fim"]:
            if r["bloqueia"]:
                return f"CONFLITO com a rotina fixa: {r['titulo']} {r['inicio']}-{r['fim']}.", ""
            if cat not in ("TRABALHO", "FREELA"):
                nota = f" (Atenção: cai no horário da rotina '{r['titulo']}'.)"
    return None, nota


def _janela_ambigua(data: str, hora: str) -> list:
    """Nomes dos blocos 'flexíveis' da rotina (FUNDACE, freela) que contêm esse horário.
    Nesses horários a categoria do evento é ambígua: precisa perguntar."""
    if not hora:
        return []
    dia = datetime.strptime(data, "%Y-%m-%d").date()
    return [r["titulo"] for r in _rotina_do_dia(dia)
            if not r["bloqueia"] and r["inicio"] <= hora < r["fim"]]


def _msg_perguntar(janelas, quando: str) -> str:
    return (f"PRECISA PERGUNTAR: {quando} cai no horário de {', '.join(sorted(set(janelas)))}. "
            "Nada foi criado. Pergunte à Isabella se é do trabalho (TRABALHO), de freela "
            "(FREELA, e para qual aluno/cliente) ou de outra coisa; só depois de ela responder, "
            "chame a ferramenta de novo com a categoria certa e categoria_confirmada=True.")


# ---------------- FERRAMENTAS (o que o agente consegue FAZER) ----------------
def salvar_nota(texto: str, categoria: str = "OUTRO") -> str:
    """Salva uma nota, ideia ou link na caixa de entrada.
    categoria: TRABALHO, MESTRADO, INGLES, ACADEMIA, FREELA ou OUTRO."""
    cat = _categoria(categoria)
    if not cat:
        return f"Categoria inválida. Use uma destas: {', '.join(CATEGORIAS)}."
    with LOCK:
        with open(ARQ_NOTAS, "a", encoding="utf-8") as f:
            f.write(f"- [{datetime.now():%Y-%m-%d %H:%M}] [{cat}] {texto}\n")
    return "Nota salva."


def criar_evento(titulo: str, data: str, hora: str = "", categoria: str = "OUTRO",
                 forcar: bool = False, categoria_confirmada: bool = False) -> str:
    """Cria UM compromisso na agenda.
    data no formato AAAA-MM-DD. hora no formato HH:MM (vazio se não houver hora).
    categoria: TRABALHO, MESTRADO, INGLES, ACADEMIA, FREELA ou OUTRO.
    Se já houver evento na mesma data e hora, ou se bater com a rotina fixa
    (natação, academia, aula), retorna CONFLITO e não cria, a menos que
    forcar=True (use só depois de a Isabella confirmar).
    Se o horário cair dentro da FUNDACE ou do bloco de freela, a ferramenta devolve
    PRECISA PERGUNTAR: pergunte a categoria à Isabella e só então chame de novo com
    categoria_confirmada=True. Nunca use categoria_confirmada=True sem ela ter respondido."""
    cat = _categoria(categoria)
    if not cat:
        return f"Categoria inválida. Use uma destas: {', '.join(CATEGORIAS)}."
    erro = _validar(data, hora)
    if erro:
        return erro
    hora = _norm_hora(hora)
    with LOCK:
        eventos = _ler_agenda()
        conflito, nota = _checar_horario(eventos, data, hora, cat)
        if conflito and not forcar:
            return conflito + " Nada foi criado."
        janelas = _janela_ambigua(data, hora)
        if janelas and not categoria_confirmada:
            return _msg_perguntar(janelas, f"{data} às {hora}")
        eventos.append({"id": uuid.uuid4().hex[:6], "titulo": titulo, "data": data,
                        "hora": hora, "categoria": cat,
                        "criado_em": datetime.now().isoformat(timespec="minutes"),
                        "avisados": []})
        _gravar_agenda(eventos)
    return f"Evento criado: [{cat}] {titulo} em {data} {hora}".strip() + nota


def criar_evento_recorrente(titulo: str, dias_semana: str, hora: str,
                            categoria: str = "OUTRO", data_inicio: str = "",
                            semanas: int = 8, categoria_confirmada: bool = False) -> str:
    """Cria um compromisso que se repete toda semana (ex.: inglês ter e qui às 19:00).
    NÃO use para a rotina fixa que já está cadastrada (natação, academia, FUNDACE, aula).
    dias_semana: abreviações separadas por vírgula, entre seg,ter,qua,qui,sex,sab,dom.
    hora no formato HH:MM. data_inicio AAAA-MM-DD (vazio = hoje).
    semanas = por quantas semanas criar (padrão 8).
    categoria: TRABALHO, MESTRADO, INGLES, ACADEMIA, FREELA ou OUTRO.
    Se algum dia cair dentro da FUNDACE ou do bloco de freela, devolve PRECISA PERGUNTAR:
    pergunte a categoria e só então chame de novo com categoria_confirmada=True."""
    cat = _categoria(categoria)
    if not cat:
        return f"Categoria inválida. Use uma destas: {', '.join(CATEGORIAS)}."
    erro = _validar(data_inicio or date.today().isoformat(), hora)
    if erro:
        return erro
    hora = _norm_hora(hora)
    dias = {DIAS_MAP[d[:3]] for d in dias_semana.lower().replace(" ", "").split(",")
            if d[:3] in DIAS_MAP}
    if not dias:
        return "Não entendi os dias da semana. Nada foi criado."
    inicio = datetime.strptime(data_inicio, "%Y-%m-%d").date() if data_inicio else date.today()
    if not categoria_confirmada:
        janelas = []
        for i in range(int(semanas) * 7):
            dia = inicio + timedelta(days=i)
            if dia.weekday() in dias:
                janelas += _janela_ambigua(dia.isoformat(), hora)
        if janelas:
            return _msg_perguntar(janelas, f"{dias_semana} às {hora}")
    criados, conflitos = 0, []
    with LOCK:
        eventos = _ler_agenda()
        existentes = {(e["data"], e["hora"], e["titulo"]) for e in eventos}
        ocupados = {(e["data"], e["hora"]): e["titulo"] for e in eventos if e["hora"]}
        for i in range(int(semanas) * 7):
            dia = inicio + timedelta(days=i)
            if dia.weekday() not in dias:
                continue
            iso = dia.isoformat()
            if (iso, hora, titulo) in existentes:
                continue
            if (iso, hora) in ocupados:
                conflitos.append(f"{dia:%d/%m} com '{ocupados[(iso, hora)]}'")
            for r in _rotina_do_dia(dia):
                if r["bloqueia"] and r["inicio"] <= hora < r["fim"]:
                    conflitos.append(f"{dia:%d/%m} com a rotina '{r['titulo']}'")
            eventos.append({"id": uuid.uuid4().hex[:6], "titulo": titulo, "data": iso,
                            "hora": hora, "categoria": cat,
                            "criado_em": datetime.now().isoformat(timespec="minutes"),
                            "avisados": []})
            criados += 1
        _gravar_agenda(eventos)
    msg = f"{criados} eventos criados para as próximas {semanas} semanas."
    if conflitos:
        msg += " Atenção, horário coincide com: " + "; ".join(conflitos[:5])
    return msg


def listar_eventos(data_inicio: str, data_fim: str = "") -> str:
    """Lista compromissos (com o id de cada um) E a rotina fixa. data_inicio AAAA-MM-DD.
    Para um período (ex.: a semana), informe também data_fim AAAA-MM-DD (máximo 31 dias).
    Sem data_fim, lista só o dia de data_inicio."""
    try:
        d0 = datetime.strptime(data_inicio, "%Y-%m-%d").date()
        d1 = datetime.strptime(data_fim or data_inicio, "%Y-%m-%d").date()
    except ValueError:
        return "Datas devem estar no formato AAAA-MM-DD."
    if (d1 - d0).days > 31:
        d1 = d0 + timedelta(days=31)
    with LOCK:
        eventos = _ler_agenda()
    itens = []
    for e in eventos:
        if d0.isoformat() <= e["data"] <= d1.isoformat():
            itens.append((e["data"], e["hora"] or "00:00",
                          f"{e['data']} {e['hora'] or 'dia todo'} - [{e['categoria']}] "
                          f"{e['titulo']} (id {e['id']})"))
    d = d0
    while d <= d1:
        for r in _rotina_do_dia(d):
            itens.append((d.isoformat(), r["inicio"],
                          f"{d.isoformat()} {r['inicio']}-{r['fim']} - [{r['categoria']}] "
                          f"{r['titulo']} (rotina fixa)"))
        d += timedelta(days=1)
    if not itens:
        return "Nenhum compromisso nesse período."
    return "\n".join(t for _, _, t in sorted(itens))


def buscar_eventos(texto: str, dias: int = 60) -> str:
    """Procura compromissos futuros pelo nome (parte do título) ou pela categoria e mostra
    o id de cada um. Use antes de cancelar ou editar um evento. dias = até onde olhar (padrão 60)."""
    hoje = date.today()
    limite = (hoje + timedelta(days=int(dias))).isoformat()
    t = _sem_acento(texto)
    with LOCK:
        eventos = _ler_agenda()
    achados = sorted((e for e in eventos
                      if hoje.isoformat() <= e["data"] <= limite
                      and (t in _sem_acento(e["titulo"]) or t == _sem_acento(e["categoria"]))),
                     key=lambda e: (e["data"], e["hora"] or "00:00"))
    if not achados:
        return "Nenhum evento encontrado com esse nome."
    return "\n".join(f"id {e['id']} | {e['data']} {e['hora'] or 'dia todo'} | "
                     f"[{e['categoria']}] {e['titulo']}" for e in achados[:30])


def editar_evento(id_evento: str, novo_titulo: str = "", nova_data: str = "",
                  nova_hora: str = "", nova_categoria: str = "", forcar: bool = False) -> str:
    """Remarca ou edita UM evento pelo id (descubra o id com buscar_eventos ou listar_eventos).
    Preencha só o que muda: novo_titulo, nova_data (AAAA-MM-DD), nova_hora (HH:MM),
    nova_categoria. Se o novo horário bater com outro evento ou com a rotina fixa, retorna
    CONFLITO e não altera, a menos que forcar=True (só depois de a Isabella confirmar).
    Ao mudar data/hora, os avisos do Telegram são refeitos para o novo horário."""
    cat = None
    if nova_categoria:
        cat = _categoria(nova_categoria)
        if not cat:
            return f"Categoria inválida. Use uma destas: {', '.join(CATEGORIAS)}."
    with LOCK:
        eventos = _ler_agenda()
        alvo = next((e for e in eventos if e["id"] == id_evento), None)
        if not alvo:
            return "Não achei nenhum evento com esse id. Nada foi alterado."
        data = nova_data or alvo["data"]
        hora = nova_hora or alvo["hora"]
        erro = _validar(data, hora)
        if erro:
            return erro
        hora = _norm_hora(hora)
        mudou_horario = (data != alvo["data"] or hora != alvo["hora"])
        nota = ""
        if mudou_horario:
            conflito, nota = _checar_horario(eventos, data, hora, cat or alvo["categoria"],
                                             ignorar_id=alvo["id"])
            if conflito and not forcar:
                return conflito + " Nada foi alterado."
        antes = f"{alvo['data']} {alvo['hora'] or 'dia todo'} {alvo['titulo']}"
        alvo.update({"titulo": novo_titulo or alvo["titulo"], "data": data, "hora": hora,
                     "categoria": cat or alvo["categoria"]})
        if mudou_horario:
            alvo["avisados"] = []
            alvo["criado_em"] = datetime.now().isoformat(timespec="minutes")
        _gravar_agenda(eventos)
    return (f"Evento atualizado: de '{antes}' para "
            f"{alvo['data']} {alvo['hora'] or 'dia todo'} [{alvo['categoria']}] {alvo['titulo']}.{nota}")


def cancelar_eventos(ids: list[str]) -> str:
    """Cancela um ou mais eventos pelos ids (descubra-os com buscar_eventos ou listar_eventos).
    Os eventos cancelados ficam guardados em cancelados.json. Só use quando a Isabella
    pediu claramente; se houver dúvida sobre qual evento, pergunte antes."""
    with LOCK:
        eventos = _ler_agenda()
        removidos = [e for e in eventos if e["id"] in ids]
        if not removidos:
            return "Não achei nenhum evento com esses ids. Nada foi cancelado."
        _gravar_agenda([e for e in eventos if e["id"] not in ids])
        arquivo = _ler_json(ARQ_CANCELADOS, [])
        agora = datetime.now().isoformat(timespec="minutes")
        for e in removidos:
            e["cancelado_em"] = agora
            arquivo.append(e)
        _escrever_json(ARQ_CANCELADOS, arquivo)
    nomes = "; ".join(f"{e['data']} {e['hora'] or 'dia todo'} {e['titulo']}" for e in removidos[:10])
    return f"{len(removidos)} evento(s) cancelado(s): {nomes}"


def ignorar_rotina(titulo: str, data: str, motivo: str = "") -> str:
    """Desmarca um item da ROTINA FIXA (natação, academia, aula do mestrado, FUNDACE, freela)
    só em um dia específico (ex.: 'hoje não tem academia', 'sexta não vou à FUNDACE').
    titulo: parte do nome do item. data: AAAA-MM-DD. motivo: opcional.
    A rotina volta ao normal nas outras semanas."""
    try:
        d = datetime.strptime(data, "%Y-%m-%d").date()
    except ValueError:
        return "Data deve ser AAAA-MM-DD. Nada foi alterado."
    base = [r for r in ROTINA_FIXA if d.weekday() in r["dias"]]
    achados = [r for r in base if _sem_acento(titulo) in _sem_acento(r["titulo"])]
    if not achados:
        existentes = ", ".join(f"{r['titulo']} {r['inicio']}" for r in base) or "nada"
        return f"Não há item da rotina com esse nome em {data}. Nesse dia a rotina tem: {existentes}."
    with LOCK:
        ex = _excecoes()
        for r in achados:
            if not any(x["data"] == data and x["titulo"] == r["titulo"] for x in ex):
                ex.append({"data": data, "titulo": r["titulo"], "motivo": motivo})
        _escrever_json(ARQ_EXCECOES, ex)
    return "Rotina desmarcada em " + data + ": " + ", ".join(r["titulo"] for r in achados) + "."


def restaurar_rotina(titulo: str, data: str) -> str:
    """Desfaz ignorar_rotina: volta um item da rotina fixa a valer em um dia. data AAAA-MM-DD."""
    with LOCK:
        ex = _excecoes()
        mantidas = [x for x in ex if not (x["data"] == data and
                                          _sem_acento(titulo) in _sem_acento(x["titulo"]))]
        if len(mantidas) == len(ex):
            return "Não havia nada desmarcado com esse nome nesse dia."
        _escrever_json(ARQ_EXCECOES, mantidas)
    return f"Rotina restaurada em {data}."


def registrar_freela(cliente: str, descricao: str, horas: float = 0.0, data: str = "") -> str:
    """Registra um trabalho de freela feito para um aluno/cliente.
    cliente: nome do aluno ou cliente (OBRIGATÓRIO: se a Isabella não disse, pergunte antes).
    descricao: o que foi feito. horas: tempo gasto (0 se não souber). data AAAA-MM-DD (vazio = hoje)."""
    dia = data or date.today().isoformat()
    with LOCK:
        novo = not ARQ_FREELA.exists()
        with open(ARQ_FREELA, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if novo:
                w.writerow(["data", "cliente", "descricao", "horas"])
            w.writerow([dia, cliente, descricao, horas])
    return f"Freela registrado: {cliente} — {descricao} ({horas}h) em {dia}."


FERRAMENTAS = [salvar_nota, criar_evento, criar_evento_recorrente, listar_eventos,
               buscar_eventos, editar_evento, cancelar_eventos,
               ignorar_rotina, restaurar_rotina, registrar_freela]


# ---------------- CÉREBRO ----------------
SYSTEM_BASE = (
    "Você é a secretária pessoal da Isabella e está conversando diretamente com ela, "
    "em português, tratando-a por 'você'. Responda em no máximo 3 linhas, objetiva e gentil.\n"
    "Ferramentas: salvar_nota, criar_evento, criar_evento_recorrente, listar_eventos, "
    "buscar_eventos, editar_evento, cancelar_eventos, ignorar_rotina, restaurar_rotina, "
    "registrar_freela. Regras:\n"
    f"- Rotina fixa da Isabella (já cadastrada no sistema, NÃO fica no Google Agenda; "
    f"considere ao sugerir horários e nunca recrie): {_rotina_texto()}.\n"
    "- Compromisso único com data (e hora) -> criar_evento. "
    "Atividade que se repete toda semana e NÃO está na rotina fixa -> criar_evento_recorrente. "
    "Anotação, ideia ou link -> salvar_nota.\n"
    "- Calcule 'sexta', 'amanhã', 'semana que vem' a partir da data de hoje abaixo.\n"
    "- Categorias: TRABALHO = FUNDACE, Casa Civil, Monitora Bahia, reuniões e entregas do trabalho. "
    "MESTRADO = UFBA, disciplinas, orientadora, LTI Digital, AIDA Lab, artigos, pesquisa. "
    "INGLES = aulas e estudo de inglês. ACADEMIA = academia, natação e exercício. "
    "FREELA = trabalhos, reuniões e entregas para alunos/clientes freelancer. "
    "OUTRO = todo o resto (concurso, portfólio, instituto, MEI, vida pessoal).\n"
    "- FREELA: sempre que ela falar de um freela (feito ou a fazer) e não disser PARA QUEM "
    "(aluno/cliente), pergunte antes de registrar ou agendar. Ao contar que fez um freela, "
    "use registrar_freela (um registro por aluno/cliente) e ponha o nome no título dos eventos.\n"
    "- Categoria incerta: se a mensagem não deixar claro de que área é o compromisso "
    "(ex.: só 'reunião' ou 'compromisso'), PERGUNTE a categoria antes de criar, em uma "
    "pergunta curta (trabalho, mestrado, freela ou outra). Isso vale principalmente quando o "
    "horário cai na FUNDACE ou no bloco de freela. Se a ferramenta devolver PRECISA PERGUNTAR, "
    "pergunte e aguarde a resposta dela.\n"
    "- Mudanças: para remarcar, renomear ou cancelar, primeiro ache o evento com buscar_eventos "
    "ou listar_eventos (eles mostram o id) e depois use editar_evento ou cancelar_eventos. "
    "Se houver mais de um candidato, pergunte qual. Se for cancelar VÁRIOS eventos de uma vez, "
    "diga quantos são e peça confirmação antes. Nunca invente um id.\n"
    "- Rotina fixa que não vai acontecer em um dia ('hoje não vou à academia', 'sexta sem FUNDACE'): "
    "use ignorar_rotina; se ela voltar atrás, restaurar_rotina. Itens da rotina não são eventos: "
    "não use cancelar_eventos para eles.\n"
    "- Se faltar um dado essencial (data, por exemplo), faça UMA pergunta curta. "
    "Se faltar só a hora, crie o evento sem hora.\n"
    "- Link: salve com salvar_nota (com a categoria) e crie um evento de leitura "
    "'Ler: <assunto>' sem hora, em até 3 dias, a menos que ela indique outra data.\n"
    "- Se uma ferramenta avisar de CONFLITO, conte isso a ela e pergunte o que prefere. "
    "Só use forcar=True se ela confirmar.\n"
    "- O sistema já avisa a Isabella no Telegram antes dos compromissos (1 dia e 1 hora) e da "
    "rotina (1 hora), e manda um resumo do dia às 8h.\n"
    "- Só diga que fez algo DEPOIS que a ferramenta retornar sucesso. Nunca invente.\n"
    "- Confirme assim: '✅ Reunião sexta (09/10) 14:00 criada.'"
)


def agora_texto() -> str:
    d = datetime.now()
    return f"{DIAS[d.weekday()]}, {d:%d/%m/%Y} {d:%H:%M}"


HISTORICO = []        # memória da conversa (some quando o bot reinicia)
MAX_MENSAGENS = 20


def _freela_pendente() -> bool:
    with LOCK:
        return _ler_estado().get("freela_pendente") == date.today().isoformat()


def pensar(texto: str) -> str:
    HISTORICO.append(types.Content(role="user", parts=[types.Part(text=texto)]))
    sistema = SYSTEM_BASE + f"\nHoje é {agora_texto()}."
    if _freela_pendente():
        sistema += ("\nATENÇÃO: no fim do bloco de freela de hoje você perguntou à Isabella para "
                    "quem foi o trabalho. Se a mensagem dela responde isso, registre com "
                    "registrar_freela (um por aluno/cliente); se faltar aluno/cliente ou tempo, pergunte.")
    config = types.GenerateContentConfig(system_instruction=sistema, tools=FERRAMENTAS)
    ultimo_erro = None
    for modelo in MODELS:
        try:
            resp = client.models.generate_content(model=modelo, contents=HISTORICO, config=config)
            resposta = resp.text or "(sem resposta)"
            HISTORICO.append(types.Content(role="model", parts=[types.Part(text=resposta)]))
            del HISTORICO[:-MAX_MENSAGENS]
            if modelo != MODELS[0]:
                print(f"(usei o modelo reserva: {modelo})")
            return resposta
        except Exception as e:
            ultimo_erro = e
            if any(c in str(e) for c in ("503", "429", "UNAVAILABLE", "404", "NOT_FOUND")):
                print(f"{modelo} indisponível, tentando o próximo...")
                continue
            break
    HISTORICO.pop()
    raise ultimo_erro


# ---------------- AVISOS (rodam sozinhos a cada minuto) ----------------
def _avisos_do_evento(e: dict):
    """Lista de (chave, quando_comeca, quando_expira, texto) de um evento."""
    rotulo = f"[{e['categoria']}] {e['titulo']}"
    if e["hora"]:
        dt = datetime.strptime(f"{e['data']} {e['hora']}", "%Y-%m-%d %H:%M")
        avisos = [
            ("1dia", dt - timedelta(days=1), dt - timedelta(hours=1),
             f"🔔 Lembrete: {rotulo} — {dt:%d/%m às %H:%M}"),
            ("1h", dt - timedelta(hours=1), dt,
             f"⏰ Em 1 hora: {rotulo} — {dt:%H:%M}"),
        ]
        if e["categoria"] in SO_AVISO_1H:
            avisos = [a for a in avisos if a[0] != "1dia"]
        return avisos
    dia = datetime.strptime(e["data"], "%Y-%m-%d")
    return [("vespera", dia - timedelta(hours=6), dia, f"🔔 Amanhã (dia todo): {rotulo}")]


def _texto_resumo(eventos: list, agora: datetime) -> str:
    hoje = agora.date()
    itens = [(e["hora"] or "00:00", f"{e['hora'] or 'dia todo'} · [{e['categoria']}] {e['titulo']}")
             for e in eventos if e["data"] == hoje.isoformat()]
    for r in _rotina_do_dia(hoje):
        itens.append((r["inicio"], f"{r['inicio']}–{r['fim']} · [{r['categoria']}] {r['titulo']}"))
    cab = f"☀️ Bom dia, Isabella! {DIAS[agora.weekday()]}, {agora:%d/%m}."
    if not itens:
        return cab + "\nNenhum compromisso hoje."
    return cab + "\n" + "\n".join(t for _, t in sorted(itens))


async def verificar(context: ContextTypes.DEFAULT_TYPE):
    agora = datetime.now()
    hoje = agora.date().isoformat()
    mensagens = []
    with LOCK:
        eventos = _ler_agenda()
        estado = _ler_estado()
        mudou = False
        estado_mudou = False

        # 1) avisos dos eventos avulsos
        for e in eventos:
            try:
                avisos = _avisos_do_evento(e)
            except (KeyError, ValueError):
                continue
            # evento sem 'criado_em' (antigo) não gera avisos atrasados
            criado = datetime.fromisoformat(e["criado_em"]) if e.get("criado_em") else agora
            ja = e.setdefault("avisados", [])
            for chave, comeca, expira, texto in avisos:
                if chave in ja:
                    continue
                # só avisa se a janela do aviso ainda estava no futuro quando o evento foi criado
                if comeca <= agora < expira and criado < comeca:
                    mensagens.append(texto)
                    ja.append(chave)
                    mudou = True

        # 2) avisos da rotina fixa (1h antes) e pergunta do freela (ao fim do bloco)
        chaves = [k for k in estado.get("rotina_avisados", []) if k.startswith(hoje)]
        for r in _rotina_do_dia(agora.date()):
            ini = datetime.strptime(f"{hoje} {r['inicio']}", "%Y-%m-%d %H:%M")
            fim = datetime.strptime(f"{hoje} {r['fim']}", "%Y-%m-%d %H:%M")
            if r.get("aviso"):
                chave = f"{hoje}|aviso|{r['titulo']}|{r['inicio']}"
                if chave not in chaves and ini - timedelta(hours=1) <= agora < ini:
                    falta = max(1, int((ini - agora).total_seconds() // 60))
                    mensagens.append(f"⏰ Em {falta} min: [{r['categoria']}] {r['titulo']} — {r['inicio']}")
                    chaves.append(chave)
                    estado_mudou = True
            if r.get("pergunta_fim"):
                chave = f"{hoje}|fim|{r['titulo']}|{r['fim']}"
                if chave not in chaves and fim <= agora < fim + timedelta(hours=2):
                    mensagens.append("💼 Acabou o bloco de freela. Para quem você trabalhou hoje? "
                                     "Me diga o aluno/cliente, o que fez e quanto tempo levou.")
                    chaves.append(chave)
                    estado["freela_pendente"] = hoje
                    estado_mudou = True
        if chaves != estado.get("rotina_avisados"):
            estado["rotina_avisados"] = chaves
            estado_mudou = True

        # 3) resumo do dia
        if HORA_RESUMO <= agora.hour < 12 and estado.get("ultimo_resumo") != hoje:
            mensagens.append(_texto_resumo(eventos, agora))
            estado["ultimo_resumo"] = hoje
            estado_mudou = True

        # 4) backup semanal: domingo, a partir das 20h
        fazer_backup = False
        if agora.weekday() == 6 and agora.hour >= 20 and estado.get("ultimo_backup") != hoje:
            fazer_backup = True
            estado["ultimo_backup"] = hoje
            estado_mudou = True

        if estado_mudou:
            _gravar_estado(estado)
        if mudou:
            _gravar_agenda(eventos)
    for m in mensagens:
        try:
            await context.bot.send_message(chat_id=ALLOWED, text=m)
        except Exception as e:
            print("ERRO ao enviar aviso:", e)
    if fazer_backup:
        await _enviar_backup(context.bot)


# ---------------- BACKUP (cópia dos seus dados no próprio Telegram) ----------------
async def _enviar_backup(bot) -> int:
    """Manda os arquivos de dados para o chat. Devolve quantos foram enviados."""
    enviados = 0
    for caminho in (ARQ_AGENDA, ARQ_FREELA, ARQ_NOTAS, ARQ_EXCECOES, ARQ_CANCELADOS):
        if caminho.exists():
            try:
                with open(caminho, "rb") as f:
                    await bot.send_document(chat_id=ALLOWED, document=f, filename=caminho.name,
                                            caption=f"Backup {datetime.now():%d/%m/%Y}")
                enviados += 1
            except Exception as e:
                print("ERRO no backup:", e)
    return enviados


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED:
        return
    n = await _enviar_backup(context.bot)
    if n == 0:
        await update.message.reply_text("Ainda não há dados para guardar.")


# ---------------- TELEGRAM ----------------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED:
        return
    try:
        resposta = await asyncio.to_thread(pensar, update.message.text)
    except Exception as e:
        print("ERRO:", e)
        resposta = "Tive um problema para pensar agora. Veja o terminal."
    await update.message.reply_text(resposta)


async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED:
        return
    with LOCK:
        texto = _texto_resumo(_ler_agenda(), datetime.now())
    await update.message.reply_text(texto)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    if app.job_queue is None:
        raise SystemExit('Faltou instalar: pip install "python-telegram-bot[job-queue]"')
    app.add_handler(CommandHandler("resumo", cmd_resumo))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.job_queue.run_repeating(verificar, interval=60, first=5)
    print("Bot rodando... (Ctrl+C para parar)")
    app.run_polling()


if __name__ == "__main__":
    main()
