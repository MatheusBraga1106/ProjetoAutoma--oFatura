import os
import shutil
import subprocess


def localizar_pdftotext():
    """Acha o executável do pdftotext (poppler/xpdf), nesta ordem:
    1. env PDFTOTEXT_CMD (caminho explícito, opcional);
    2. PATH (é o caso do container Linux: /usr/bin/pdftotext via poppler-utils);
    3. só no Windows, os caminhos conhecidos de instalação — o Git for
       Windows já traz um pdftotext.exe dentro do mingw64, mesmo que ele não
       esteja no PATH que o Python do Windows enxerga.
    Se nada disso achar, devolve "pdftotext" e o subprocess falha em
    silêncio (a fatura cai pro OCR, ver converter_pdf_para_txt)."""
    explicito = os.environ.get("PDFTOTEXT_CMD", "").strip()
    if explicito:
        return explicito
    no_path = shutil.which("pdftotext")
    if no_path:
        return no_path
    if os.name == "nt":
        for c in (
            r"C:\Users\matheusdias\AppData\Local\Programs\Git\mingw64\bin\pdftotext.exe",
            r"C:\Program Files\poppler\Library\bin\pdftotext.exe",
        ):
            if os.path.exists(c):
                return c
    return "pdftotext"


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
