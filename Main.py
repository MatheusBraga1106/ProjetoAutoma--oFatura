import os
import sys

import pandas as pd

# Terminal sempre atualizado em tempo real (sem esperar o buffer encher),
# mesmo quando a saída é redirecionada/capturada por outro processo.
sys.stdout.reconfigure(line_buffering=True)

from pipeline import (
    PASTA_BASE,
    EMPRESAS_CONHECIDAS,
    EXTRATORES_POR_EMPRESA,
    EXTRATORES_NAO_IMPLEMENTADOS,
    identificar_distribuidora,
    mesclar_saneago_com_analitica,
    enriquecer_com_contas_json,
    filtrar_linhas_vazias,
    marcar_suspeitas,
    salvar_incremental,
)
from extratores.ocr_fallback import eh_texto_ocr, detectar_faturas_sem_texto, gerar_txt_via_ocr
from extratores.pdftotext_fallback import converter_pdf_para_txt


def rastrear_e_processar_pastas(pasta_raiz, pasta_saida):
    os.makedirs(pasta_saida, exist_ok=True)

    # Gavetas de armazenamento em memória, uma por distribuidora conhecida
    # (implementada ou não — ver EXTRATORES_NAO_IMPLEMENTADOS em pipeline.py).
    dados_por_empresa = {empresa: [] for empresa in EMPRESAS_CONHECIDAS}

    print(f"🕷️ A iniciar rastreamento na pasta raiz: '{pasta_raiz}'\n")

    # =========================================================
    # ETAPA 1 - PDFTOTEXT: antes de rotear, tenta extrair o .txt de
    # qualquer PDF ainda sem texto pareado via pdftotext (rápido, só
    # funciona em PDFs com camada de texto real).
    # =========================================================
    faturas_sem_texto = detectar_faturas_sem_texto(pasta_raiz)
    if faturas_sem_texto:
        total_pdftotext = len(faturas_sem_texto)
        print(f"📄 {total_pdftotext} fatura(s) sem texto extraível — tentando via pdftotext...\n")
        for i, (caminho_pdf, caminho_txt) in enumerate(faturas_sem_texto, start=1):
            print(f"   [{i}/{total_pdftotext}] pdftotext: {os.path.basename(caminho_pdf)}")
            converter_pdf_para_txt(caminho_pdf, caminho_txt)
        print()

    # =========================================================
    # ETAPA 2 - FALLBACK OCR: para o que o pdftotext não resolveu
    # (fatura-imagem, sem camada de texto), gera o .txt via Tesseract.
    # O .txt sai marcado e cai no walk abaixo como qualquer outro.
    # =========================================================
    faturas_sem_texto = detectar_faturas_sem_texto(pasta_raiz)
    if faturas_sem_texto:
        total_ocr = len(faturas_sem_texto)
        print(f"🖨️ {total_ocr} fatura(s) sem texto extraível — gerando via OCR (Tesseract)...\n")
        for i, (caminho_pdf, caminho_txt) in enumerate(faturas_sem_texto, start=1):
            print(f"   [{i}/{total_ocr}] OCR: {os.path.basename(caminho_pdf)}")
            try:
                gerar_txt_via_ocr(caminho_pdf, caminho_txt)
            except Exception as e:
                print(f"   ⚠️ Falhou o OCR deste arquivo, seguindo para o próximo: {e}")
        print()

    total_txt = sum(
        1 for _raiz, _subpastas, arquivos in os.walk(pasta_raiz)
        for f in arquivos if f.lower().endswith('.txt')
    )
    arquivos_processados = 0

    for diretorio_atual, subpastas, arquivos in os.walk(pasta_raiz):
        arquivos_txt = [f for f in arquivos if f.lower().endswith('.txt')]

        for arquivo in arquivos_txt:
            arquivos_processados += 1
            # Cria o caminho absoluto (C:\...)
            caminho_completo = os.path.abspath(os.path.join(diretorio_atual, arquivo))

            # 🛡️ TRUQUE ANTI-LIMITE DO WINDOWS (MAX_PATH > 260 caracteres)
            if os.name == 'nt' and not caminho_completo.startswith('\\\\?\\'):
                caminho_completo = '\\\\?\\' + caminho_completo

            print(f" [{arquivos_processados}/{total_txt}] Lendo: {arquivo} (Pasta: {os.path.basename(diretorio_atual)})")

            # =========================================================
            # ROTEADOR (tabela compartilhada com a API, em pipeline.py)
            # =========================================================
            empresa_identificada = identificar_distribuidora(f"{arquivo} {diretorio_atual}")

            if empresa_identificada is None:
                print(" ⏭ Ignorado: Não foi possível identificar a empresa.")
                continue

            if empresa_identificada in EXTRATORES_NAO_IMPLEMENTADOS:
                print(f" ⏭ Extrator {empresa_identificada} ainda não criado. A saltar...")
                continue

            is_ocr = eh_texto_ocr(caminho_completo)
            df_extraido = EXTRATORES_POR_EMPRESA[empresa_identificada](caminho_completo, is_ocr)

            # =========================================================
            # GUARDA O RESULTADO NA GAVETA CORRETA
            # =========================================================
            if df_extraido is not None and not df_extraido.empty:
                df_extraido['ARQUIVO_ORIGEM'] = arquivo
                df_extraido['PASTA_ORIGEM'] = diretorio_atual
                dados_por_empresa[empresa_identificada].append(df_extraido)

    # =========================================================
    # RELACIONAMENTO 1: SANEAGO NORMAL + SANEAGO ANALÍTICA (HIDRÔMETROS)
    # =========================================================
    if len(dados_por_empresa["SANEAGO"]) > 0 and len(dados_por_empresa["SANEAGO_ANALITICA"]) > 0:
        print("\n🔗 Relacionando faturas da SANEAGO com hidrômetros analíticos...")
        df_saneago = pd.concat(dados_por_empresa["SANEAGO"], ignore_index=True)
        df_analitica = pd.concat(dados_por_empresa["SANEAGO_ANALITICA"], ignore_index=True)
        dados_por_empresa["SANEAGO"] = [mesclar_saneago_com_analitica(df_saneago, df_analitica)]
        dados_por_empresa["SANEAGO_ANALITICA"] = []

    # =========================================================
    # EXPORTAÇÃO INCREMENTAL E ENRIQUECIMENTO VIA JSON
    # =========================================================
    print("\n" + "="*70)
    print("🛡️ A SALVAR NA BASE DE DADOS E ENRIQUECER COM JSON")
    print("="*70)

    caminho_json = os.path.join(PASTA_BASE, "contas.json")

    for empresa, lista_de_dfs in dados_por_empresa.items():
        if len(lista_de_dfs) == 0 or empresa == "SANEAGO_ANALITICA":
            continue

        df_novo_lote = pd.concat(lista_de_dfs, ignore_index=True)

        df_novo_lote, info_enriquecimento = enriquecer_com_contas_json(df_novo_lote, caminho_json)
        if not info_enriquecimento["contas_json_encontrado"]:
            print(f" ⚠️ {empresa}: contas.json não encontrado — seguindo sem enriquecimento "
                  f"(UNIDADE JUDICIÁRIA/ENDEREÇO ficam em branco).")
        elif info_enriquecimento["linhas_sem_correspondencia"] > 0:
            print(f" ⚠️ {empresa}: {info_enriquecimento['linhas_sem_correspondencia']} "
                  f"fatura(s) sem conta correspondente no contas.json.")

        df_novo_lote, descartadas = filtrar_linhas_vazias(df_novo_lote)
        if descartadas:
            print(f" 🗑️ {empresa}: {descartadas} linha(s) vazia(s)/residual(is) descartada(s).")

        if df_novo_lote.empty:
            continue

        df_novo_lote = marcar_suspeitas(df_novo_lote)
        qtd_suspeitas = int(df_novo_lote['SUSPEITO'].sum())
        if qtd_suspeitas:
            print(f" 🔍 {empresa}: {qtd_suspeitas} fatura(s) marcada(s) como SUSPEITO "
                  f"(valores não batem) — revisar.")

        # =========================================================
        # LÓGICA ANTI-DUPLICATA E SALVAMENTO CSV
        # =========================================================
        caminho_csv = os.path.join(pasta_saida, f"banco_dados_{empresa.lower()}.csv")
        resultado = salvar_incremental(df_novo_lote, caminho_csv)

        if resultado["arquivo_novo"]:
            print(f"✨ {empresa}: {resultado['adicionadas']} faturas gravadas. (Novo ficheiro criado)")
        elif resultado["adicionadas"] > 0:
            print(f"➕ {empresa}: {resultado['adicionadas']} faturas ADICIONADAS. "
                  f"(Ignoradas {resultado['duplicadas']} duplicatas)")
        else:
            print(f"⏩ {empresa}: Nenhuma fatura nova. (Ignoradas {resultado['duplicadas']} faturas)")

    print("="*70 + "\n")


if __name__ == "__main__":
    PASTA_RAIZ_DADOS = os.path.join(PASTA_BASE, "dados_entrada")
    PASTA_SAIDA = os.path.join(PASTA_BASE, "dados_saida")

    rastrear_e_processar_pastas(PASTA_RAIZ_DADOS, PASTA_SAIDA)
