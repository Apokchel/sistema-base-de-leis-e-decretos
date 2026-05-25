import os
import requests
import urllib.parse
import re
import time
from playwright.sync_api import sync_playwright
import pdfplumber
import ocrmypdf
import logging

# Silencia logs verbosos do ocrmypdf (mantém só warnings/errors)
logging.getLogger("ocrmypdf").setLevel(logging.WARNING)

# Configurações do Scraper
BASE_URL = "https://www.quadra.sp.gov.br"

# Base path do disco P: para salvar as leis
BASE_SAVE_PATH = r"P:\001 - Gabinete\2 - RH\RH\4 - Legislação e Descrição de Cargos\Legislação Site"

# Mapeamento das categorias principais
CATEGORIAS = {
    "Leis Municipais": {
        "pasta": os.path.join(BASE_SAVE_PATH, "Leis_Municipais"),
        "url_base": f"{BASE_URL}/legislacao/leis-municipais" 
    },
    "Decretos Municipais": {
        "pasta": os.path.join(BASE_SAVE_PATH, "Decretos_Municipais"),
        "url_base": f"{BASE_URL}/legislacao/decretos-municipais"
    }
}

def setup_directories():
    """Cria as pastas para cada categoria se elas não existirem e prepara o índice."""
    if not os.path.exists(BASE_SAVE_PATH):
        os.makedirs(BASE_SAVE_PATH)
        
    # Inicializa ou limpa o arquivo de índice geral
    indice_path = os.path.join(BASE_SAVE_PATH, "INDICE_GERAL.md")
    if not os.path.exists(indice_path):
        with open(indice_path, "w", encoding="utf-8") as f:
            f.write("# 📚 Índice Geral de Legislação Municipal\n\n")
            f.write("> Arquivo gerado automaticamente pelo robô para facilitar a busca do Esquadrão Jurídico.\n\n")

    for categoria, dados in CATEGORIAS.items():
        pasta = dados["pasta"]
        if not os.path.exists(pasta):
            os.makedirs(pasta)
            print(f"📁 Pasta criada: '{pasta}'")

def download_file(url, pasta_destino, filename):
    """Faz o download de um arquivo PDF a partir de uma URL."""
    try:
        filepath = os.path.join(pasta_destino, filename)
        if os.path.exists(filepath):
            print(f"  ⏭️ Já existe (pulando download): {filename}")
            return

        response = requests.get(url, stream=True)
        response.raise_for_status()
        
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"  ✅ Salvo: {filename}")
    except Exception as e:
        print(f"  ❌ Erro ao baixar {filename}: {e}")

def _ler_texto_pdf(pdf_path):
    """Lê texto de um PDF usando pdfplumber. Retorna string (possivelmente vazia)."""
    textos = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                textos.append(text)
    return "\n".join(textos).strip()

def aplicar_ocr_no_pdf(pdf_path):
    """Aplica OCR no PDF in-place (adiciona camada de texto invisível).
    Usa Tesseract via ocrmypdf. Idempotente: skip_text pula páginas que já têm texto."""
    try:
        ocrmypdf.ocr(
            pdf_path,
            pdf_path,
            language="por",
            skip_text=True,
            optimize=0,
            progress_bar=False,
        )
        return True
    except ocrmypdf.exceptions.PriorOcrFoundError:
        # PDF já tem OCR — não é falha
        return True
    except Exception as e:
        print(f"  ⚠️ Falha no OCR de {os.path.basename(pdf_path)}: {e}")
        return False

def extract_pdf_text(pdf_path, txt_path):
    """Lê um PDF e salva o conteúdo em um arquivo .txt paralelo.
    Se o PDF for escaneado (sem camada de texto), aplica OCR automaticamente."""
    if os.path.exists(txt_path):
        return # Já extraímos o texto antes

    try:
        texto_final = _ler_texto_pdf(pdf_path)

        # Se não veio texto, é PDF escaneado → roda OCR e tenta de novo
        if not texto_final:
            print(f"  🔎 PDF sem texto detectado, aplicando OCR (pt-br)...")
            if aplicar_ocr_no_pdf(pdf_path):
                texto_final = _ler_texto_pdf(pdf_path)

        with open(txt_path, "w", encoding="utf-8") as f_txt:
            if texto_final:
                f_txt.write(texto_final)
                print(f"  📝 Arquivo de texto (.txt) gerado para leitura rápida da IA.")
            else:
                f_txt.write("[Aviso: Documento provavelmente escaneado como imagem. O texto não pôde ser lido pela máquina mesmo após OCR. Revisão humana necessária.]")
                # Registra o PDF na lista de pendentes de OCR
                ocr_log_path = os.path.join(BASE_SAVE_PATH, "PENDENTES_DE_OCR.txt")
                with open(ocr_log_path, "a", encoding="utf-8") as f_ocr:
                    f_ocr.write(f"{pdf_path}\n")
                print(f"  ⚠️ OCR não recuperou texto — registrado em PENDENTES_DE_OCR.txt")
    except Exception as e:
        print(f"  ⚠️ Erro ao extrair texto do PDF {os.path.basename(pdf_path)}: {e}")

def scrape_categoria(nome_categoria, dados_categoria, page):
    """Varre a página usando Playwright para ler o JS carregado."""
    print(f"\n🚀 Iniciando extração de: {nome_categoria}")
    url_base = dados_categoria["url_base"]
    pasta_destino = dados_categoria["pasta"]
    
    categoria_slug = url_base.split('/')[-1] # ex: 'leis-municipais'
    
    # 1. Busca subpáginas de anos (Lidando com Paginação)
    valid_subpages = []
    pagina_atual = 1
    
    while True:
        # Navega para a página correta
        url_paginada = url_base if pagina_atual == 1 else f"{url_base}?&pagina={pagina_atual}"
        page.goto(url_paginada)
        page.wait_for_timeout(3000)
        
        # Encontra todos os links de subpáginas de anos
        links_elementos = page.eval_on_selector_all(
            "a",
            "(elements) => elements.map(el => ({href: el.getAttribute('href'), text: el.innerText}))"
        )
        
        novos_links = 0
        for p in links_elementos:
            href = p.get('href', '')
            if href and re.search(r'(19|20)\d{2}', href) and not href.lower().endswith('.pdf'):
                if categoria_slug in href:
                    # Verifica se o link já não foi capturado
                    if not any(v.get('href') == href for v in valid_subpages):
                        valid_subpages.append(p)
                        novos_links += 1
                        
        print(f"    - Página {pagina_atual}: {novos_links} anos encontrados.")
        
        # Se não achou nenhum ano novo nesta aba, significa que as páginas acabaram
        if novos_links == 0:
            break
            
        pagina_atual += 1
                
    # Ordena as páginas de anos do menor (mais antigo) para o maior (mais novo)
    def extrair_ano(p):
        match = re.search(r'(20\d{2}|19\d{2})', p.get('href', '') + p.get('text', ''))
        return int(match.group(1)) if match else 9999
        
    valid_subpages.sort(key=extrair_ano)
    
    # Extrai URLs únicas das subpáginas
    sub_paginas = []
    for item in valid_subpages:
        full_url = urllib.parse.urljoin(BASE_URL, item.get('href', ''))
        if full_url not in sub_paginas and "quadra.sp.gov.br" in full_url:
            sub_paginas.append(full_url)
                
    print(f"📅 Encontradas {len(sub_paginas)} subpáginas de anos para {nome_categoria}.")

    # 2. Visita cada subpágina de ano e baixa os PDFs
    for sub_url in sub_paginas:
        # Extrai o ano da URL usando regex (ex: "leis-2018-749" -> "2018")
        match_ano = re.search(r'(19|20)\d{2}', sub_url.split('/')[-1])
        ano_str = match_ano.group() if match_ano else "Diversos"
        
        print(f"\n  👉 Lendo página do ano: {ano_str}...")
            
        # Cria a subpasta do ano
        pasta_ano = os.path.join(pasta_destino, ano_str)
        if not os.path.exists(pasta_ano):
            os.makedirs(pasta_ano)
            print(f"    📁 Subpasta criada: '{pasta_ano}'")
        
        # Acessa a página do ano
        try:
            page.goto(sub_url, wait_until="networkidle")
            # Tempo extra para garantir que a tabela JS foi totalmente renderizada
            page.wait_for_timeout(5000) 
        except Exception as e:
            print(f"    ⚠️ Erro ao abrir {sub_url}: {e}")
            continue
            
        # Pega todos os links da página principal e de possíveis iframes (eCrie costuma usar iframes)
        pdfs_elements = page.eval_on_selector_all("a", "elements => elements.map(e => ({href: e.href, text: e.innerText}))")
        
        for frame in page.frames:
            if frame != page.main_frame:
                try:
                    frame_links = frame.eval_on_selector_all("a", "elements => elements.map(e => ({href: e.href, text: e.innerText}))")
                    pdfs_elements.extend(frame_links)
                except:
                    pass
        
        pdfs_encontrados = []
        for p_link in pdfs_elements:
            h = p_link.get('href', '')
            t = p_link.get('text', '').strip()
            if h and h.lower().endswith('.pdf'):
                # Ignora links globais que aparecem em todas as páginas (pelo título, não pela URL)
                if "lei orgânica" in t.lower() or "constituição" in t.lower():
                    continue
                    
                full_url = urllib.parse.urljoin(BASE_URL, h)
                # Evita duplicados na mesma página
                if not any(d['url'] == full_url for d in pdfs_encontrados):
                    pdfs_encontrados.append({"url": full_url, "title": t})
                
        if not pdfs_encontrados:
            print(f"    Nenhum PDF encontrado na página de {ano_str}. (Pode estar vazio ou usando outro formato de link)")
            continue
            
        # Inverte a lista de PDFs para começar da lei mais antiga (final da página) para a mais nova (topo da página)
        pdfs_encontrados.reverse()
            
        print(f"    Encontrados {len(pdfs_encontrados)} leis/decretos reais. Iniciando download para {pasta_ano}...")
        for index, pdf_data in enumerate(pdfs_encontrados):
            pdf_url = pdf_data["url"]
            title = pdf_data["title"]
            
            # Tenta encontrar número e ano no título
            match = re.search(r'(\d+)[^\d]*(\d{2,4})', title)
            if match:
                numero = match.group(1)
                ano_match = match.group(2)
                if len(ano_match) == 2 or len(ano_match) == 4:
                    ano_usar = ano_match if len(ano_match) == 4 else (f"19{ano_match}" if int(ano_match) > 50 else f"20{ano_match}")
                else:
                    ano_usar = ano_str
                filename = f"{numero}_{ano_usar}.pdf"
            else:
                # Fallback: procura na URL do arquivo
                url_filename = os.path.basename(urllib.parse.urlparse(pdf_url).path)
                match_url = re.search(r'(\d+)[^\d]*(\d{2,4})', url_filename)
                if match_url:
                    numero = match_url.group(1)
                    filename = f"{numero}_{ano_str}.pdf"
                else:
                    filename = f"doc_{index+1}_{ano_str}.pdf"
                
            download_file(pdf_url, pasta_ano, filename)
            
            # Gera a cópia em TXT pareada
            pdf_path = os.path.join(pasta_ano, filename)
            txt_filename = filename.replace('.pdf', '.txt')
            txt_path = os.path.join(pasta_ano, txt_filename)
            extract_pdf_text(pdf_path, txt_path)
            
            # Adiciona o registro no Índice Geral
            indice_path = os.path.join(BASE_SAVE_PATH, "INDICE_GERAL.md")
            with open(indice_path, "a", encoding="utf-8") as f_md:
                # Limpa quebras de linha do título para não quebrar o layout
                clean_title = title.replace('\n', ' ').replace('\r', '').strip()
                f_md.write(f"- **[{nome_categoria} - {ano_str}]** `{filename}`: {clean_title}\n")

def run_scraper():
    """Função principal que orquestra a extração."""
    setup_directories()
    
    print("🤖 Iniciando o Navegador Invisível (Playwright)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Cria contexto ignorando possíveis erros de certificado e com viewport padrão
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        
        for nome_categoria, dados in CATEGORIAS.items():
            scrape_categoria(nome_categoria, dados, page)
            
        browser.close()

if __name__ == "__main__":
    run_scraper()
