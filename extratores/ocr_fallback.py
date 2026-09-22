import os
import io
import glob
import shutil

import pymupdf
import pytesseract
from PIL import Image

# As faturas vêm de pastas locais controladas pelo próprio usuário (não são
# uploads de terceiros), então desativamos a trava de "decompression bomb"
# do Pillow: em 400 DPI, páginas maiores que A4 passam do limite padrão
# (~179M pixels) e derrubam o OCR sem motivo real de segurança aqui.
Image.MAX_IMAGE_PIXELS = None

# Marcador gravado na primeira linha de qualquer .txt gerado por este
# módulo. É assim que o roteador do Main.py sabe que aquele texto veio de
# OCR (não do pdftotext) e precisa avisar o extrator disso.
MARCADOR_OCR = "#### TEXTO_GERADO_VIA_OCR ####"

DPI_PADRAO = 400
PSM_PADRAO = 4  # "assume uma única coluna de texto de tamanhos variados"
IDIOMA_PADRAO = "por"


def localizar_tesseract():
    """Acha o executável do tesseract, nesta ordem:
    1. env TESSERACT_CMD (caminho explícito, se quem faz o deploy quiser fixar);
    2. PATH (é o caso do container Linux: /usr/bin/tesseract via apt);
    3. só no Windows, os caminhos padrão do winget/instalador oficial.
    Se nada disso achar, devolve "tesseract" e deixa o pytesseract falhar
    com a mensagem dele (a UI mostra isso no banner de status)."""
    explicito = os.environ.get("TESSERACT_CMD", "").strip()
    if explicito:
        return explicito
    no_path = shutil.which("tesseract")
    if no_path:
        return no_path
    if os.name == "nt":
        for c in (
            r"C:\Users\matheusdias\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        ):
            if os.path.exists(c):
                return c
    return "tesseract"


pytesseract.pytesseract.tesseract_cmd = localizar_tesseract()


def caminho_longo(caminho):
    """Aplica o prefixo '\\\\?\\' do Windows a um caminho absoluto, para não
    esbarrar no limite clássico de 260 caracteres (MAX_PATH) em pastas
    profundamente aninhadas — o mesmo truque que o Main.py já usa ao ler
    os .txt, aqui replicado para as operações de PDF (PyMuPDF) e escrita
    de .txt deste módulo."""
    caminho_abs = os.path.abspath(caminho)
    if os.name == 'nt' and not caminho_abs.startswith('\\\\?\\'):
        caminho_abs = '\\\\?\\' + caminho_abs
    return caminho_abs


CARACTERE_SUBSTITUICAO = "�"  # "�" — surge quando o .txt foi gravado numa
# codificação (ex.: Latin-1) diferente da que o pipeline usa pra ler (UTF-8).
# Não é um caractere que apareça naturalmente em português; qualquer
# ocorrência é sinal de que o .txt precisa ser regravado, não de que falta
# texto nele.


def tem_encoding_corrompido(conteudo):
    """Verdadeiro se o texto tiver o caractere de substituição Unicode, sinal
    de erro de codificação na conversão (pdftotext gravando Latin-1/cp1252
    enquanto o resto do pipeline lê como UTF-8, por ex.). Isso corrompe
    justamente os acentos do português (Ç, Ó, Â, É...), então até uma única
    ocorrência já indica que o arquivo saiu errado."""
    return CARACTERE_SUBSTITUICAO in conteudo


def eh_texto_util(caminho_txt, min_chars=50):
    """Um .txt é considerado 'útil' se existir, tiver conteúdo real e não
    estar com a codificação corrompida. PDFs que na verdade são imagem
    escaneada geram um .txt vazio (0 bytes) quando passados pelo pdftotext,
    porque não existe camada de texto. Já um .txt com "�" no meio das
    palavras existe e tem tamanho, mas está corrompido e precisa ser
    reconvertido — do contrário o dado ruim segue direto pro CSV final."""
    caminho_txt = caminho_longo(caminho_txt)
    if not os.path.exists(caminho_txt):
        return False
    try:
        conteudo = open(caminho_txt, encoding="utf-8", errors="replace").read().strip()
    except OSError:
        return False
    if len(conteudo) < min_chars:
        return False
    return not tem_encoding_corrompido(conteudo)


def eh_texto_ocr(caminho_txt):
    """Detecta se um .txt já existente foi gerado por este módulo (em vez
    de vir do pdftotext), lendo só a primeira linha."""
    try:
        with open(caminho_longo(caminho_txt), encoding="utf-8", errors="replace") as f:
            primeira_linha = f.readline().strip()
    except OSError:
        return False
    return primeira_linha == MARCADOR_OCR


def detectar_faturas_sem_texto(pasta_raiz, min_chars=50):
    """Varre pasta_raiz procurando PDFs cujo .txt pareado não existe, está
    vazio/quase vazio (PDF-imagem, sem camada de texto extraível) ou está
    com a codificação corrompida (cheio de "�" nos acentos). Nos três
    casos o .txt precisa ser (re)gerado. Retorna uma lista de
    (caminho_pdf, caminho_txt)."""
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
    doc = pymupdf.open(caminho_longo(caminho_pdf))
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
    with open(caminho_longo(caminho_txt), "w", encoding="utf-8") as f:
        f.write(MARCADOR_OCR + "\n")
        f.write(texto)
    return caminho_txt
