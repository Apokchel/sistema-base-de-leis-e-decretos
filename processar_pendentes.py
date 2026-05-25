"""
Processa em lote todos os PDFs escaneados listados em PENDENTES_DE_OCR.txt.
Aplica OCR (Tesseract via ocrmypdf, idioma pt-br), re-extrai o texto com
pdfplumber e atualiza o .txt pareado. Remove do arquivo de pendentes os que
forem processados com sucesso.

Uso:
    python processar_pendentes.py            # 4 workers (padrão)
    python processar_pendentes.py --jobs 8   # ajustar paralelismo
    python processar_pendentes.py --limit 50 # processar só os primeiros 50 (teste)
"""
import os
import sys
import argparse
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed

import ocrmypdf
import pdfplumber

# Mesmo BASE_SAVE_PATH do main.py
BASE_SAVE_PATH = r"P:\001 - Gabinete\2 - RH\RH\4 - Legislação e Descrição de Cargos\Legislação Site"
PENDENTES_PATH = os.path.join(BASE_SAVE_PATH, "PENDENTES_DE_OCR.txt")

logging.getLogger("ocrmypdf").setLevel(logging.WARNING)
logging.getLogger("pdfminer").setLevel(logging.ERROR)


def processar_um_pdf(pdf_path):
    """Roda OCR e re-extrai texto. Retorna (pdf_path, status, mensagem)."""
    if not os.path.exists(pdf_path):
        return (pdf_path, "ausente", "arquivo nao encontrado")

    txt_path = pdf_path[:-4] + ".txt" if pdf_path.lower().endswith(".pdf") else pdf_path + ".txt"

    try:
        ocrmypdf.ocr(
            pdf_path,
            pdf_path,
            language="por",
            skip_text=True,
            optimize=0,
            progress_bar=False,
        )
    except ocrmypdf.exceptions.PriorOcrFoundError:
        pass  # já tem OCR, segue para extrair
    except Exception as e:
        return (pdf_path, "erro_ocr", str(e))

    try:
        textos = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    textos.append(t)
        texto_final = "\n".join(textos).strip()

        if not texto_final:
            return (pdf_path, "vazio", "OCR rodou mas texto continua vazio")

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(texto_final)
        return (pdf_path, "ok", f"{len(texto_final)} chars")
    except Exception as e:
        return (pdf_path, "erro_extracao", str(e))


def carregar_pendentes():
    if not os.path.exists(PENDENTES_PATH):
        print(f"❌ Arquivo de pendentes não encontrado: {PENDENTES_PATH}")
        return []
    with open(PENDENTES_PATH, "r", encoding="utf-8") as f:
        linhas = [l.strip() for l in f if l.strip()]
    # Deduplica preservando ordem
    vistos = set()
    unicos = []
    for l in linhas:
        if l not in vistos:
            vistos.add(l)
            unicos.append(l)
    return unicos


def salvar_pendentes_restantes(pendentes_restantes):
    """Reescreve o arquivo só com os que ainda não foram resolvidos."""
    with open(PENDENTES_PATH, "w", encoding="utf-8") as f:
        for p in pendentes_restantes:
            f.write(f"{p}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=4, help="PDFs processados em paralelo (default: 4)")
    parser.add_argument("--limit", type=int, default=None, help="Processar só os primeiros N (para teste)")
    args = parser.parse_args()

    pendentes = carregar_pendentes()
    if args.limit:
        pendentes = pendentes[: args.limit]

    total = len(pendentes)
    if total == 0:
        print("✅ Nada a processar — lista de pendentes vazia.")
        return

    print(f"🚀 Iniciando OCR de {total} PDFs com {args.jobs} workers...")
    print(f"   (Cada PDF é OCR'd in-place, texto re-extraído para .txt pareado)\n")

    resolvidos = set()
    contadores = {"ok": 0, "vazio": 0, "erro_ocr": 0, "erro_extracao": 0, "ausente": 0}
    feitos = 0

    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        futuros = {ex.submit(processar_um_pdf, p): p for p in pendentes}
        for fut in as_completed(futuros):
            pdf_path, status, msg = fut.result()
            feitos += 1
            contadores[status] = contadores.get(status, 0) + 1
            nome = os.path.basename(pdf_path)
            icone = {"ok": "✅", "vazio": "⚠️", "erro_ocr": "❌", "erro_extracao": "❌", "ausente": "👻"}.get(status, "?")
            print(f"  [{feitos}/{total}] {icone} {nome} — {status} ({msg})")
            if status == "ok":
                resolvidos.add(pdf_path)

    # Atualiza PENDENTES_DE_OCR.txt: mantém só os não resolvidos
    todos_pendentes = carregar_pendentes()  # relê para não perder os que não estavam no batch
    restantes = [p for p in todos_pendentes if p not in resolvidos]
    salvar_pendentes_restantes(restantes)

    print("\n" + "=" * 60)
    print(f"📊 Resumo:")
    for k, v in contadores.items():
        print(f"   {k}: {v}")
    print(f"\n   Pendentes restantes no arquivo: {len(restantes)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
