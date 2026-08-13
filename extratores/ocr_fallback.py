import os
import io
import glob

import pymupdf
import pytesseract
from PIL import Image

# Marcador gravado na primeira linha de qualquer .txt gerado por este
# módulo. É assim que o roteador do Main.py sabe que aquele texto veio de
# OCR (não do pdftotext) e precisa avisar o extrator disso.
MARCADOR_OCR = "#### TEXTO_GERADO_VIA_OCR ####"

DPI_PADRAO = 400
PSM_PADRAO = 4  # "assume uma única coluna de texto de tamanhos variados"
IDIOMA_PADRAO = "por"


def localizar_tesseract():
    """Acha o executável do tesseract. Tenta o PATH primeiro; se não
    achar, cai no caminho padrão de instalação via winget/instalador
    oficial no Windows."""
    candidatos = [
        "tesseract",
        r"C:\Users\matheusdias\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    ]
    for c in candidatos:
        if os.path.isabs(c) and os.path.exists(c):
            return c
    return candidatos[0]  # deixa o pytesseract tentar resolver via PATH


pytesseract.pytesseract.tesseract_cmd = localizar_tesseract()


def eh_texto_util(caminho_txt, min_chars=50):
    """Um .txt é considerado 'útil' se existir e tiver conteúdo real.
    PDFs que na verdade são imagem escaneada geram um .txt vazio (0 bytes)
    quando passados pelo pdftotext, porque não existe camada de texto."""
    if not os.path.exists(caminho_txt):
        return False
    try:
        conteudo = open(caminho_txt, encoding="utf-8", errors="replace").read().strip()
    except OSError:
        return False
    return len(conteudo) >= min_chars


def eh_texto_ocr(caminho_txt):
    """Detecta se um .txt já existente foi gerado por este módulo (em vez
    de vir do pdftotext), lendo só a primeira linha."""
    try:
        with open(caminho_txt, encoding="utf-8", errors="replace") as f:
            primeira_linha = f.readline().strip()
    except OSError:
        return False
    return primeira_linha == MARCADOR_OCR


def detectar_faturas_sem_texto(pasta_raiz, min_chars=50):
    """Varre pasta_raiz procurando PDFs cujo .txt pareado não existe ou
    está vazio/quase vazio (PDF-imagem, sem camada de texto extraível).
    Retorna uma lista de (caminho_pdf, caminho_txt)."""
    faltantes = []
    for raiz, _subpastas, arquivos in os.walk(pasta_raiz):
        for arquivo in arquivos:
            if not arquivo.lower().endswith(".pdf"):
                continue
            caminho_pdf = os.path.join(raiz, arquivo)
            caminho_txt = os.path.join(raiz, os.path.splitext(arquivo)[0] + ".txt")
            if not eh_texto_util(caminho_txt, min_chars=min_chars):
                faltantes.append((caminho_pdf, caminho_txt))
    return faltantes


def ocr_pdf(caminho_pdf, dpi=DPI_PADRAO, psm=PSM_PADRAO, lang=IDIOMA_PADRAO, callback_pagina=None):
    """Rasteriza cada página do PDF (via PyMuPDF, sem depender do poppler)
    e roda o Tesseract em cima da imagem. Retorna o texto de todas as
    páginas concatenado com quebra de linha entre elas."""
    doc = pymupdf.open(caminho_pdf)
    zoom = dpi / 72
    mat = pymupdf.Matrix(zoom, zoom)
    config = f"--psm {psm}"

    paginas_texto = []
    for i, pagina in enumerate(doc):
        pix = pagina.get_pixmap(matrix=mat)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        texto = pytesseract.image_to_string(img, lang=lang, config=config)
        paginas_texto.append(texto)
        if callback_pagina:
            callback_pagina(i + 1, doc.page_count)
    doc.close()
    return "\n".join(paginas_texto)


def gerar_txt_via_ocr(caminho_pdf, caminho_txt=None, forcar=False, **kwargs_ocr):
    """Gera (ou regenera, se forcar=True) o .txt de um PDF via OCR e grava
    no disco, pronto pra ser lido pelo pipeline normal do Main.py como
    qualquer outro .txt. Idempotente: se o .txt já existe com conteúdo
    útil e forcar=False, não faz nada e retorna None.
    """
    if caminho_txt is None:
        caminho_txt = os.path.splitext(caminho_pdf)[0] + ".txt"

    if not forcar and eh_texto_util(caminho_txt):
        return None

    texto = ocr_pdf(caminho_pdf, **kwargs_ocr)
    with open(caminho_txt, "w", encoding="utf-8") as f:
        f.write(MARCADOR_OCR + "\n")
        f.write(texto)
    return caminho_txt
