# Agente Rotina

Uma secretária pessoal no Telegram, com IA do Google Gemini. Você escreve do jeito que fala ("reunião sexta às 14h", "hoje não vou à academia", "salva esse link") e ela organiza a agenda, guarda as notas e manda lembretes.

## O que ela faz

- **Agenda:** cria, remarca, renomeia e cancela compromissos, inclusive os que se repetem toda semana ("inglês ter e qui às 19h").
- **Rotina fixa:** sabe dos seus horários de toda semana (academia, aula, trabalho) e avisa quando algo novo bate com eles. Também dá para desmarcar a rotina só em um dia.
- **Lembretes automáticos:** avisa 1 dia e 1 hora antes de cada compromisso, 1 hora antes dos itens da rotina e manda um resumo do dia às 8h.
- **Notas e links:** guarda ideias e links por categoria e marca um "Ler: ..." na agenda.
- **Freela:** registra para quem você trabalhou, o que fez e quanto tempo levou (em `freela.csv`).
- **Backup:** todo domingo à noite manda os arquivos de dados para o seu próprio chat. Também dá para pedir com `/backup`.
- **Privacidade:** responde só ao seu chat. Os dados ficam em arquivos na sua máquina.

Comandos: `/resumo` (agenda de hoje) e `/backup`. O resto é conversa normal.

## Do que você precisa

- Python 3.10 ou mais novo (testado no 3.12)
- Uma conta no Telegram
- Uma chave gratuita da API do Gemini: <https://aistudio.google.com/apikey>

## Instalação

### 1. Baixe o projeto

```bash
git clone https://github.com/SEU-USUARIO/agente-rotina.git
cd agente-rotina
```

### 2. Crie o ambiente e instale as dependências

Windows:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Crie o seu bot no Telegram

1. No Telegram, abra o **@BotFather** e mande `/newbot`.
2. Escolha um nome e um usuário para o bot. Ele vai te dar um **token** parecido com `123456789:ABC...`.
3. Abra o **@userinfobot** e mande qualquer mensagem. Ele responde com o seu **id** (um número). Esse é o `ALLOWED_CHAT_ID`.

### 4. Configure as chaves

Copie o arquivo de exemplo e preencha:

```bash
cp token.env.example token.env      # no Windows: copy token.env.example token.env
```

```env
TELEGRAM_TOKEN=token do BotFather
ALLOWED_CHAT_ID=seu id do Telegram
GEMINI_API_KEY=sua chave do Gemini
GEMINI_MODELS=gemini-3.8-flash
```

Em `GEMINI_MODELS` dá para pôr mais de um modelo separado por vírgula. Se o primeiro estiver fora do ar, o bot tenta o próximo. Para ver quais modelos a sua chave pode usar:

```bash
python listar_modelos.py
```

> ⚠️ **Nunca publique o `token.env`.** Quem tiver o token controla o seu bot. Ele já está no `.gitignore`.

### 5. Rode

```bash
python bot.py
```

Abra o seu bot no Telegram, mande `/resumo` e pronto.

No Windows dá para dar dois cliques no **`iniciar_bot.bat`**: ele ativa o ambiente e reinicia o bot sozinho se ele cair. Para o bot abrir junto com o computador, coloque um atalho do `.bat` na pasta de inicialização (`Win + R` → `shell:startup`).

O bot só funciona enquanto o computador estiver ligado e o programa aberto.

## Personalize para a sua rotina

Tudo fica no começo do [`bot.py`](bot.py):

| O quê | Onde | Exemplo |
|---|---|---|
| Sua rotina fixa | `ROTINA_FIXA` | `{"dias": [0], "inicio": "11:00", "fim": "12:00", "titulo": "Natação", "categoria": "ACADEMIA", "bloqueia": True, "aviso": True}` |
| Categorias | `CATEGORIAS` | `["TRABALHO", "MESTRADO", "INGLES", ...]` |
| Hora do resumo do dia | `HORA_RESUMO` | `8` |
| Categorias que só recebem o aviso de 1h | `SO_AVISO_1H` | `{"ACADEMIA"}` |
| Personalidade, regras e o que é cada categoria | `SYSTEM_BASE` | texto em português |

Na `ROTINA_FIXA`:

- `dias`: 0 = segunda, 1 = terça, ..., 6 = domingo
- `bloqueia: True`: não deixa marcar outra coisa nesse horário sem confirmar
- `aviso: True`: avisa 1 hora antes
- `pergunta_fim: True`: quando o bloco termina, pergunta para quem foi o freela

O código ainda tem o nome **Isabella** e os nomes dos lugares de trabalho e estudo dela em alguns textos (`SYSTEM_BASE`, docstrings das ferramentas e `_texto_resumo`). Troque pelo seu nome e pelos seus contextos com um "localizar e substituir".

Se você mudar as categorias, atualize também as explicações delas em `SYSTEM_BASE`, porque é por elas que a IA sabe em qual categoria pôr cada coisa.

## Arquivos de dados

O bot cria estes arquivos na pasta, e eles **não** vão para o Git:

| Arquivo | Conteúdo |
|---|---|
| `agenda.json` | compromissos |
| `notas.md` | notas e links |
| `freela.csv` | registro de freelas (abre no Excel) |
| `cancelados.json` | eventos cancelados (para recuperar se precisar) |
| `excecoes_rotina.json` | dias em que a rotina fixa foi desmarcada |
| `estado.json` | controle interno dos avisos |

## Limitações

- A memória da conversa some quando o bot reinicia. A agenda e as notas ficam salvas.
- Ainda não sincroniza com o Google Agenda.
- Feito para uma pessoa só (um `ALLOWED_CHAT_ID`).
