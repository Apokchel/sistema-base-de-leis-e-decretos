from flask import Flask, request, jsonify, render_template, send_file
import os
import sys
import re
import time
import unicodedata
import threading
import urllib.parse
import difflib
import html
from datetime import datetime

# Garante que prints com emoji não quebrem em consoles Windows (cp1252)
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

app = Flask(__name__)

DIRETORIO_BASE = r"P:\001 - Gabinete\2 - RH\RH\4 - Legislação e Descrição de Cargos\Legislação Site"

documentos_cache = []
indice_referencias = {}   # {(tipo, numero, ano): caminho_pdf}  e  {(tipo, numero): caminho_pdf}
vocabulario = []          # Lista ordenada de palavras únicas (sem acento) para "você quis dizer"
ultima_indexacao = None
_reindex_lock = threading.Lock()

# Pesos do ranqueamento
PESO_NOME = 50
PESO_FRASE_EXATA = 10
PESO_TERMO = 1

LIMITE_RESULTADOS = 100
MAX_SNIPPETS_POR_DOC = 3
DISTANCIA_MINIMA_SNIPPETS = 400
LIMIAR_TEXTO_CURTO = 400  # docs com menos chars que isso ficam marcados como "curto"

# Marca que o main.py escreve em .txt quando OCR também falha
MARCA_OCR_FALHOU = "[Aviso:"

# Regex de referências cruzadas (executa no texto original com acentos)
# Captura: "Lei nº 749/2018", "Lei 749 de 2018", "Decreto nº 123/2020", etc.
PADRAO_REFERENCIA = re.compile(
    r'\b(Lei|Decreto)s?'
    r'(?:\s+(?:Complementar(?:es)?|Municipa(?:l|is)|Federa(?:l|is)|Estadua(?:l|is)|Ordin[áa]ria))?'
    r'\s+'
    r'(?:n[º°.o]?\s*)?'
    r'(\d{1,5})'
    r'\s*(?:/|,?\s+de\s+(?:\d{1,2}\s+de\s+[a-zç]+\s+de\s+)?)'
    r'(\d{4})\b',
    re.IGNORECASE
)


def remover_acentos(texto):
    try:
        texto = str(texto)
        return ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')
    except Exception:
        return texto


def avaliar_qualidade(conteudo):
    """Classifica o texto extraído. Retorna 'ok', 'curto' ou 'vazio'."""
    if not conteudo or conteudo.strip().startswith(MARCA_OCR_FALHOU):
        return "vazio"
    tamanho_util = len(conteudo.strip())
    if tamanho_util < LIMIAR_TEXTO_CURTO:
        return "curto"
    return "ok"


def carregar_documentos():
    """Reconstrói o cache e o índice reverso lendo todos os .txt do disco.
    Swap atômico no final para não corromper leituras concorrentes."""
    global documentos_cache, indice_referencias, ultima_indexacao
    t_inicio = time.time()
    print(f"🔄 [1/3] Escaneando diretório '{DIRETORIO_BASE}' (pode demorar em disco de rede)...", flush=True)

    # os.walk é bem mais rápido que glob recursivo em disco de rede
    arquivos_txt = []
    for raiz, _dirs, arquivos in os.walk(DIRETORIO_BASE):
        for nome in arquivos:
            if nome.lower().endswith('.txt') and 'PENDENTES' not in nome:
                arquivos_txt.append(os.path.join(raiz, nome))

    print(f"   ✓ {len(arquivos_txt)} arquivos .txt encontrados em {time.time()-t_inicio:.1f}s", flush=True)
    print(f"🔄 [2/3] Lendo conteúdos (com progresso a cada 200 arquivos)...", flush=True)

    nova_lista = []
    t_leitura = time.time()
    for i, txt_path in enumerate(arquivos_txt, 1):
        if i % 200 == 0:
            print(f"   ... {i}/{len(arquivos_txt)} ({time.time()-t_leitura:.0f}s)", flush=True)

        try:
            with open(txt_path, 'r', encoding='utf-8') as f:
                conteudo = f.read()

            pdf_path = txt_path.replace('.txt', '.pdf')
            if not os.path.exists(pdf_path):
                continue

            nome = os.path.basename(pdf_path)
            partes = txt_path.split(os.sep)

            tipo = "Documento"
            ano = ""
            categoria_display = "Documento"
            try:
                idx = partes.index("Legislação Site")
                pasta_tipo = partes[idx + 1]
                ano = partes[idx + 2]
                if "Lei" in pasta_tipo:
                    tipo = "Lei"
                elif "Decreto" in pasta_tipo:
                    tipo = "Decreto"
                categoria_display = f"{pasta_tipo} ({ano})"
            except Exception:
                pass

            numero_match = re.match(r'(\d+)_', nome)
            numero = numero_match.group(1) if numero_match else ""

            qualidade = avaliar_qualidade(conteudo)

            nova_lista.append({
                "nome": nome,
                "caminho_pdf": pdf_path,
                "tipo": tipo,
                "ano": ano,
                "numero": numero,
                "categoria": categoria_display,
                "qualidade": qualidade,
                "nome_busca": remover_acentos(nome.lower()),
                "conteudo_busca": remover_acentos(conteudo.lower()),
                "conteudo_original": conteudo
            })
        except Exception:
            pass

    # Constrói índice reverso para resolver "Lei 749/2018" → caminho do PDF
    novo_indice = {}
    for doc in nova_lista:
        if doc["numero"] and doc["tipo"] in ("Lei", "Decreto"):
            chave_completa = (doc["tipo"], doc["numero"], doc["ano"])
            novo_indice[chave_completa] = doc["caminho_pdf"]
            # Chave parcial (sem ano) — só se ainda não houver conflito
            chave_parcial = (doc["tipo"], doc["numero"])
            if chave_parcial not in novo_indice or novo_indice[chave_parcial] == doc["caminho_pdf"]:
                novo_indice[chave_parcial] = doc["caminho_pdf"]
            else:
                # Conflito: marca como ambíguo (None significa "não resolver sem ano")
                novo_indice[chave_parcial] = None

    # Constrói vocabulário (palavras únicas, normalizadas, len >= 4) para sugestões
    vocab_set = set()
    for doc in nova_lista:
        for palavra in re.findall(r'[a-z]{4,}', doc["conteudo_busca"]):
            vocab_set.add(palavra)
    novo_vocab = sorted(vocab_set)

    documentos_cache = nova_lista
    indice_referencias = novo_indice
    globals()["vocabulario"] = novo_vocab
    ultima_indexacao = datetime.now()
    print(f"🔄 [3/3] Construindo índices secundários (vocabulário, referências)...", flush=True)
    print(f"✅ {len(nova_lista)} documentos carregados em {time.time()-t_inicio:.1f}s "
          f"({ultima_indexacao.strftime('%H:%M:%S')}). "
          f"Vocabulário: {len(novo_vocab)} palavras, {len(novo_indice)} refs.", flush=True)
    return len(nova_lista)


def sugerir_termos(termos, max_sugestoes=3):
    """Para cada termo, sugere palavras próximas do vocabulário.
    Retorna lista de sugestões (palavras únicas, sem duplicar o termo original)."""
    if not vocabulario:
        return []
    sugestoes = []
    vistas = set(termos)
    for termo in termos:
        if len(termo) < 4:
            continue
        # Se o termo já existe no vocabulário, não precisa sugerir
        if termo in vocabulario:
            continue
        candidatas = difflib.get_close_matches(termo, vocabulario, n=max_sugestoes, cutoff=0.75)
        for c in candidatas:
            if c not in vistas:
                sugestoes.append(c)
                vistas.add(c)
    return sugestoes[:max_sugestoes]


def parse_query(query_original):
    """Parser de query com operadores:
       - "frase exata" → frase obrigatória
       - -palavra → exclui docs que contenham
       - tipo:Lei | ano:2018 | numero:749 → filtros inline
       - resto → termos AND
    Retorna dict com tudo já normalizado (sem acento, lowercase)."""
    q = query_original

    # 1. Extrai frases entre aspas (preserva ordem das palavras)
    frases_exatas = re.findall(r'"([^"]+)"', q)
    q = re.sub(r'"[^"]+"', ' ', q)

    # 2. Extrai filtros inline (case-insensitive na chave, preserva valor)
    filtros_inline = {}
    for m in re.finditer(r'\b(tipo|ano|numero):(\S+)', q, re.IGNORECASE):
        filtros_inline[m.group(1).lower()] = m.group(2)
    q = re.sub(r'\b(?:tipo|ano|numero):\S+', ' ', q, flags=re.IGNORECASE)

    # 3. Extrai exclusões (-palavra)
    exclusoes = re.findall(r'(?:^|\s)-(\S+)', q)
    q = re.sub(r'(?:^|\s)-\S+', ' ', q)

    # 4. Resto = termos AND
    termos = [t for t in q.lower().split() if t]

    # Normaliza tudo
    frases_norm = [remover_acentos(f.lower().strip()) for f in frases_exatas if f.strip()]
    termos_norm = [remover_acentos(t) for t in termos]
    exclusoes_norm = [remover_acentos(e.lower()) for e in exclusoes]

    # Frase implícita = a query sem operadores (para snippets e ranqueamento de "frase exata")
    frase_implicita = " ".join(termos_norm)

    return {
        "frases_exatas": frases_norm,
        "termos": termos_norm,
        "exclusoes": exclusoes_norm,
        "filtros_inline": filtros_inline,
        "frase_implicita": frase_implicita,
        # Query é vazia só se NÃO houver nada — nem termos, nem frases, nem filtros
        "vazia": not (frases_norm or termos_norm or filtros_inline),
    }


def calcular_score(doc, query):
    """Retorna 0 se não bate. Senão, score numérico para ranquear."""
    conteudo = doc["conteudo_busca"]
    nome = doc["nome_busca"]

    # Exclusões têm prioridade absoluta
    for excl in query["exclusoes"]:
        if excl in conteudo or excl in nome:
            return 0

    # Frases exatas têm que aparecer no conteúdo
    for frase in query["frases_exatas"]:
        if frase not in conteudo:
            return 0

    # Cada termo solto tem que aparecer em algum lugar (nome ou conteúdo)
    for termo in query["termos"]:
        if termo not in conteudo and termo not in nome:
            return 0

    score = 0

    # Boost por match no nome (busca por número de lei)
    for termo in query["termos"]:
        if termo in nome:
            score += PESO_NOME
        if doc["numero"] and termo == doc["numero"]:
            score += PESO_NOME

    # Frases exatas — boost grande
    for frase in query["frases_exatas"]:
        score += conteudo.count(frase) * PESO_FRASE_EXATA * 2

    # Frase implícita (multi-termo) — boost médio
    if len(query["termos"]) > 1 and query["frase_implicita"]:
        score += conteudo.count(query["frase_implicita"]) * PESO_FRASE_EXATA

    # Ocorrências individuais
    for termo in query["termos"]:
        score += conteudo.count(termo) * PESO_TERMO

    # Se não há termos/frases mas a query tem filtros, todos os docs que passaram
    # nos filtros recebem score base 1 (ordenação fica pela ordem de inserção)
    if score == 0 and not query["termos"] and not query["frases_exatas"]:
        score = 1

    return score


def gerar_snippets(doc, query):
    """Gera até MAX_SNIPPETS_POR_DOC trechos, priorizando matches de frase exata."""
    conteudo_busca = doc["conteudo_busca"]
    conteudo_orig = doc["conteudo_original"]

    posicoes = []
    # Prioridade 1: frases exatas
    for frase in query["frases_exatas"]:
        start = 0
        while True:
            idx = conteudo_busca.find(frase, start)
            if idx == -1:
                break
            posicoes.append((idx, len(frase)))
            start = idx + len(frase)

    # Prioridade 2: frase implícita
    if len(query["termos"]) > 1 and query["frase_implicita"]:
        start = 0
        while True:
            idx = conteudo_busca.find(query["frase_implicita"], start)
            if idx == -1:
                break
            posicoes.append((idx, len(query["frase_implicita"])))
            start = idx + len(query["frase_implicita"])

    # Prioridade 3: termos individuais
    for termo in query["termos"]:
        start = 0
        count = 0
        while True:
            idx = conteudo_busca.find(termo, start)
            if idx == -1 or count > 20:
                break
            posicoes.append((idx, len(termo)))
            start = idx + len(termo)
            count += 1

    if not posicoes:
        return []

    posicoes.sort()
    selecionadas = []
    for pos in posicoes:
        if not selecionadas or pos[0] - selecionadas[-1][0] >= DISTANCIA_MINIMA_SNIPPETS:
            selecionadas.append(pos)
        if len(selecionadas) >= MAX_SNIPPETS_POR_DOC:
            break

    termos_para_grifar = list(query["frases_exatas"])
    if query["frase_implicita"] and len(query["termos"]) > 1:
        termos_para_grifar.append(query["frase_implicita"])
    termos_para_grifar.extend(query["termos"])

    snippets = []
    for idx, tam in selecionadas:
        start = max(0, idx - 120)
        end = min(len(conteudo_orig), idx + tam + 180)
        trecho = conteudo_orig[start:end].replace('\n', ' ').strip()

        # Escapa HTML do conteúdo original antes de inserir nossas tags <mark>/<a>.
        # Sem isso, qualquer "<script>" no texto extraído por OCR viraria HTML real
        # quando renderizado via innerHTML no frontend (XSS).
        trecho = html.escape(trecho)

        # Aplica <mark> nos termos
        trecho_busca = remover_acentos(trecho.lower())
        marcacoes = []
        for termo_g in termos_para_grifar:
            for m in re.finditer(re.escape(termo_g), trecho_busca):
                marcacoes.append((m.start(), m.end()))
        marcacoes.sort()
        marcacoes_limpas = []
        for m in marcacoes:
            if not marcacoes_limpas or m[0] >= marcacoes_limpas[-1][1]:
                marcacoes_limpas.append(m)
        for s_pos, e_pos in reversed(marcacoes_limpas):
            trecho = trecho[:s_pos] + "<mark>" + trecho[s_pos:e_pos] + "</mark>" + trecho[e_pos:]

        # Aplica links de referências cruzadas (depois do mark — o regex casa só onde não foi grifado)
        trecho = aplicar_links_referencias(trecho)

        snippets.append("..." + trecho + "...")

    return snippets


def aplicar_links_referencias(trecho_html):
    """Substitui menções a 'Lei nº X/YYYY' / 'Decreto nº X/YYYY' por links para o PDF.
    Recebe trecho que já pode conter <mark>...</mark>; é seguro porque o regex
    procura padrões que não contêm tags HTML."""
    def substituir(match):
        texto_original = match.group(0)
        tipo_match = match.group(1).capitalize()  # "Lei" ou "Decreto"
        # Normaliza singular (se vier "Leis" ou "Decretos", trata como o tipo base)
        tipo = "Lei" if tipo_match.lower().startswith("lei") else "Decreto"
        numero = match.group(2)
        ano = match.group(3)

        # Tenta resolver: primeiro com ano, depois sem
        caminho = indice_referencias.get((tipo, numero, ano))
        if not caminho:
            caminho = indice_referencias.get((tipo, numero))
        if not caminho:
            return texto_original  # não achou — deixa como está

        url = f"/abrir_pdf?caminho={urllib.parse.quote(caminho)}"
        return (f'<a href="{url}" target="_blank" class="ref-link" '
                f'title="Abrir {tipo} {numero}/{ano}">{texto_original}</a>')

    return PADRAO_REFERENCIA.sub(substituir, trecho_html)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/metadados')
def metadados():
    tipos = sorted({d["tipo"] for d in documentos_cache if d["tipo"]})
    anos = sorted({d["ano"] for d in documentos_cache if d["ano"]}, reverse=True)
    qualidades = {}
    for d in documentos_cache:
        qualidades[d["qualidade"]] = qualidades.get(d["qualidade"], 0) + 1
    return jsonify({
        "tipos": tipos,
        "anos": anos,
        "qualidades": qualidades,
        "total": len(documentos_cache),
        "ultima_indexacao": ultima_indexacao.isoformat() if ultima_indexacao else None,
    })


@app.route('/api/reindexar', methods=['POST'])
def reindexar():
    if not _reindex_lock.acquire(blocking=False):
        return jsonify({"status": "ja_rodando", "mensagem": "Reindexação em andamento"}), 409
    try:
        total = carregar_documentos()
        return jsonify({
            "status": "ok",
            "total": total,
            "ultima_indexacao": ultima_indexacao.isoformat() if ultima_indexacao else None,
        })
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500
    finally:
        _reindex_lock.release()


@app.route('/api/search')
def search():
    query_original = request.args.get('q', '').strip()
    filtro_tipo = request.args.get('tipo', '').strip()
    filtro_ano = request.args.get('ano', '').strip()
    filtro_qualidade = request.args.get('qualidade', '').strip()  # "ok" para esconder OCR ruim
    ordenar = request.args.get('ordenar', 'recente').strip()

    if not query_original:
        return jsonify({"resultados": [], "sugestoes": []})

    query = parse_query(query_original)
    if query["vazia"]:
        return jsonify({"resultados": [], "sugestoes": []})

    # Filtros inline da query sobrescrevem os dos dropdowns
    tipo_efetivo = query["filtros_inline"].get("tipo", filtro_tipo)
    ano_efetivo = query["filtros_inline"].get("ano", filtro_ano)
    numero_efetivo = query["filtros_inline"].get("numero", "")

    # Normaliza "lei"/"leis" → "Lei", "decreto"/"decretos" → "Decreto"
    if tipo_efetivo:
        t_lower = tipo_efetivo.lower()
        if t_lower.startswith("lei"):
            tipo_efetivo = "Lei"
        elif t_lower.startswith("decreto"):
            tipo_efetivo = "Decreto"

    candidatos = []
    for doc in documentos_cache:
        if tipo_efetivo and doc["tipo"] != tipo_efetivo:
            continue
        if ano_efetivo and doc["ano"] != ano_efetivo:
            continue
        if numero_efetivo and doc["numero"] != numero_efetivo:
            continue
        if filtro_qualidade and doc["qualidade"] != filtro_qualidade:
            continue

        score = calcular_score(doc, query)
        if score > 0:
            candidatos.append((score, doc))

    if ordenar == 'relevancia':
        candidatos.sort(key=lambda x: (
            x[0],
            int(x[1]["ano"]) if x[1]["ano"].isdigit() else 0,
            int(x[1]["numero"]) if x[1]["numero"].isdigit() else 0
        ), reverse=True)
    else:
        candidatos.sort(key=lambda x: (
            int(x[1]["ano"]) if x[1]["ano"].isdigit() else 0,
            int(x[1]["numero"]) if x[1]["numero"].isdigit() else 0,
            x[0]
        ), reverse=True)

    resultados = []
    for score, doc in candidatos[:LIMITE_RESULTADOS]:
        snippets = gerar_snippets(doc, query)
        snippet_html = "  <br><br>  ".join(snippets) if snippets else "<i>Match no nome/metadados</i>"
        resultados.append({
            "nome": doc["nome"],
            "categoria": doc["categoria"],
            "tipo": doc["tipo"],
            "ano": doc["ano"],
            "qualidade": doc["qualidade"],
            "caminho_pdf": doc["caminho_pdf"],
            "snippet": snippet_html,
            "score": score,
        })

    # Se nada bateu, oferece sugestões de termos próximos para corrigir typos
    sugestoes = []
    if not resultados and query["termos"]:
        sugestoes = sugerir_termos(query["termos"])

    return jsonify({"resultados": resultados, "sugestoes": sugestoes})


@app.route('/abrir_pdf')
def abrir_pdf():
    """Serve PDF do disco. Protegido contra path traversal: só permite
    arquivos efetivamente dentro de DIRETORIO_BASE."""
    caminho = request.args.get('caminho', '').strip()
    if not caminho:
        return "Caminho não fornecido", 400

    try:
        caminho_abs = os.path.abspath(caminho)
        base_abs = os.path.abspath(DIRETORIO_BASE)
    except Exception:
        return "Caminho inválido", 400

    # Garante que o caminho resolvido está realmente dentro de DIRETORIO_BASE
    # (impede ?caminho=../../Windows/system32/...)
    if os.path.commonpath([caminho_abs, base_abs]) != base_abs:
        return "Acesso negado", 403

    if not caminho_abs.lower().endswith('.pdf'):
        return "Apenas PDFs são permitidos", 403

    if not os.path.exists(caminho_abs):
        return "PDF não encontrado", 404

    return send_file(caminho_abs)


if __name__ == '__main__':
    carregar_documentos()
    print("\n🚀 Servidor do QuadraBusca rodando (waitress, production-grade)")
    print("👉 Acesse no seu navegador: http://127.0.0.1:5002\n")
    from waitress import serve
    serve(app, host='0.0.0.0', port=5002, threads=8)
