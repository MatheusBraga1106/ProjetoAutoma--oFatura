import os
import subprocess


def localizar_pdftotext():
    """Acha o executável do pdftotext (poppler/xpdf). Tenta o PATH primeiro;
    se não achar, cai nos caminhos conhecidos de instalação — o Git for
    Windows já traz um pdftotext.exe dentro do mingw64, mesmo que ele não
    esteja no PATH que o Python do Windows enxerga."""
    candidatos = [
        "pdftotext",
        r"C:\Users\matheusdias\AppData\Local\Programs\Git\mingw64\bin\pdftotext.exe",
        r"C:\Program Files\poppler\Library\bin\pdftotext.exe",
    ]
    for c in candidatos:
        if os.path.isabs(c) and os.path.exists(c):
            return c
    return candidatos[0]  # deixa o subprocess tentar resolver via PATH


PDFTOTEXT_CMD = localizar_pdftotext()


def converter_pdf_para_txt(caminho_pdf, caminho_txt=None):
    """Roda 'pdftotext -layout' num PDF, gerando o .txt pareado. É a
    primeira tentativa (bem mais rápida que OCR); se o PDF for imagem
    escaneada (sem camada de texto), o .txt sai vazio ou curto e o
    pipeline cai pro fallback de OCR em ocr_fallback.py. Não propaga erro:
    se o pdftotext falhar ou não existir no sistema, só deixa a fatura
    para o OCR resolver."""
    if caminho_txt is None:
        caminho_txt = os.path.splitext(caminho_pdf)[0] + ".txt"

    try:
        subprocess.run(
            # Sem "-enc UTF-8", o pdftotext do Git for Windows grava em
            # Latin-1 nesse ambiente e todo acento vira "�" quando o
            # resto do pipeline lê o .txt como UTF-8.
            [PDFTOTEXT_CMD, "-layout", "-enc", "UTF-8", caminho_pdf, caminho_txt],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass

    return caminho_txt
