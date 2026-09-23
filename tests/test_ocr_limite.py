"""Teto de pixels do OCR: página normal mantém 400 DPI; página gigante (PDF
de poucos bytes que declara uma página enorme) é rasterizada menor."""

import pymupdf

from extratores import ocr_fallback


def _pagina(largura_pt, altura_pt):
    doc = pymupdf.open()
    return doc, doc.new_page(width=largura_pt, height=altura_pt)


def test_a4_mantem_400_dpi():
    doc, pagina = _pagina(595, 842)
    assert ocr_fallback.matriz_rasterizacao(pagina).a == 400 / 72
    doc.close()


def test_pagina_gigante_fica_dentro_do_teto():
    doc, pagina = _pagina(14400, 14400)  # maior página que o formato PDF permite
    matriz = ocr_fallback.matriz_rasterizacao(pagina)
    pixels = (14400 * matriz.a) * (14400 * matriz.d)
    assert pixels <= ocr_fallback.LIMITE_PIXELS_PAGINA * 1.001
    doc.close()
