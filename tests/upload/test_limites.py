"""Limites: quantidade de arquivos, tamanho do upload, memória."""

import threading
import time

import httpx
import psutil
import pytest

import utilitarios as u


def test_mais_de_1000_arquivos_aceito(servidor):
    """O Starlette limita o multipart a 1000 arquivos por padrão; o acervo real
    tem 1117 PDFs, então selecionar dados_entrada/ inteira na UI falhava. O
    limite agora é UPLOAD_MAX_ARQUIVOS (default 5000)."""
    lote = [(f"AGUA - DESCONHECIDA/{i}.pdf", b"%PDF-1.4") for i in range(1001)]
    r = u.criar_job(servidor.url, lote, timeout=120)
    assert r.status_code == 200, r.text
    assert r.json()["total_arquivos"] == 1001


def test_1000_arquivos_sao_aceitos(servidor):
    # distribuidora não identificável => cada arquivo falha rápido, sem OCR
    lote = [(f"AGUA - DESCONHECIDA/{i}.pdf", b"%PDF-1.4") for i in range(1000)]
    t = time.time()
    r = u.criar_job(servidor.url, lote, timeout=120)
    assert r.status_code == 200, r.text
    assert r.json()["total_arquivos"] == 1000
    eventos = u.ler_eventos(servidor.url, r.json()["job_id"], timeout=300)
    print(f"\nMEDIDA 1000 arquivos triviais: {time.time() - t:.1f}s")
    u.validar_sequencia_sse(eventos, [n for n, _ in lote])


def _pico_rss(pid, parar: threading.Event, saida: list):
    proc = psutil.Process(pid)
    pico = 0
    while not parar.is_set():
        try:
            pico = max(pico, proc.memory_info().rss)
        except psutil.Error:
            break
        time.sleep(0.05)
    saida.append(pico)


@pytest.mark.lento
def test_upload_gigante_e_rejeitado_ou_nao_estoura_memoria(servidor_limpo):
    tamanho = 300 * 1024 * 1024
    base = psutil.Process(servidor_limpo.pid).memory_info().rss
    parar, pico = threading.Event(), []
    monitor = threading.Thread(target=_pico_rss, args=(servidor_limpo.pid, parar, pico))
    monitor.start()
    try:
        r = u.criar_job(servidor_limpo.url, [("AGUA - SAAE MINEIROS/grande.pdf", b"%PDF-1.4\n" + b"0" * tamanho)])
        if r.status_code == 200:
            u.aguardar_job(servidor_limpo.url, r.json()["job_id"], timeout=600)
    finally:
        parar.set()
        monitor.join()
    extra_mb = (pico[0] - base) / 1e6
    print(f"\nMEDIDA upload 300MB: status={r.status_code} RSS extra={extra_mb:.0f}MB")
    assert httpx.get(servidor_limpo.url + "/health", timeout=10).status_code == 200
    assert r.status_code == 413 or extra_mb < 100


@pytest.mark.lento
def test_pdf_com_pagina_enorme_nao_estoura_memoria(servidor_limpo):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page(width=2000, height=2000)  # ~28" x 28"; a 400 DPI = 11111 x 11111 px
    conteudo = doc.tobytes()
    doc.close()
    base = psutil.Process(servidor_limpo.pid).memory_info().rss
    parar, pico = threading.Event(), []
    monitor = threading.Thread(target=_pico_rss, args=(servidor_limpo.pid, parar, pico))
    monitor.start()
    t = time.time()
    try:
        r = u.criar_job(servidor_limpo.url, [("AGUA - SAAE MINEIROS/pagina enorme.pdf", conteudo)])
        u.aguardar_job(servidor_limpo.url, r.json()["job_id"], timeout=900)
    finally:
        parar.set()
        monitor.join()
    extra_mb = (pico[0] - base) / 1e6
    print(f"\nMEDIDA página 2000x2000pt ({len(conteudo)} bytes): {time.time() - t:.0f}s, RSS extra={extra_mb:.0f}MB "
          "(tesseract roda em subprocesso, não entra nessa conta)")
    assert extra_mb < 500
