import pandas as pd
import re
import os

def extrair_saneago_analitica(caminho_txt):
    nome_arquivo = os.path.basename(caminho_txt)
    print(f"  Iniciando extração: SANEAGO ANALÍTICA ({nome_arquivo})")

    dados_finais = []

    # =========================================================
    # PLANO B: DATA VIA NOME DO ARQUIVO
    # Só é usado se a fatura não tiver a REFERÊNCIA gravada dentro do texto.
    # =========================================================
    mes_ano_arquivo = ""
    match_data_arquivo = re.search(r'(\d{2})[_\.\-](\d{4})', nome_arquivo)
    if match_data_arquivo:
        mes_ano_arquivo = f"{match_data_arquivo.group(1)}/{match_data_arquivo.group(2)}"

    # Mês/Ano "vigente" enquanto percorremos o arquivo. Atualiza sempre que uma
    # nova linha REFERÊNCIA aparece, para suportar arquivos com mais de um
    # órgão pagador/referência (cada um com seu próprio bloco de contas).
    mes_ano_ref_atual = ""

    bloco_atual = {}

    with open(caminho_txt, 'r', encoding='utf-8', errors='replace') as f:
        for linha in f:
            linha_upper = linha.strip().upper()

            # =========================================================
            # REFERÊNCIA (MÊS/ANO) — direto do cabeçalho da fatura
            # Ex: "ÓRGÃO PAGADOR: 2130 - TRIBUNAL DE JUSTICA/GO   REFERÊNCIA: 10/2021"
            # Tolerante a falha de acentuação (REFERENCIA / REFERÊNCIA)
            # =========================================================
            match_ref = re.search(r'REFER[ÊE]NCIA:?\s*(\d{2}/\d{4})', linha_upper)
            if match_ref:
                mes_ano_ref_atual = match_ref.group(1)

            # =========================================================
            # REGEX ULTRA TOLERANTE: Conta
            # Ignora caracteres sujos entre o "N" e os números
            # =========================================================
            match_conta = re.search(r'CONTA\s*N.*?(\d+)\s*-\s*(\d+)', linha_upper)
            if match_conta:
                # Se já tem um cliente montado, salva uma CÓPIA exata na lista
                if bloco_atual.get('CONTA_DV') and bloco_atual.get('NUM_HIDROMETRO_EXTRAIDO'):
                    dados_finais.append(bloco_atual.copy())

                # Força o formato exato "12345 6" cortando o hífen
                conta_formatada = f"{match_conta.group(1)} {match_conta.group(2)}"

                # Usa a referência lida dentro da própria fatura; só recorre ao
                # nome do arquivo se, até este ponto, nenhuma REFERÊNCIA tiver
                # sido encontrada no texto.
                mes_ano_bloco = mes_ano_ref_atual or mes_ano_arquivo

                bloco_atual = {
                    'CONTA_DV': conta_formatada,
                    'MES_ANO_REF': mes_ano_bloco
                }
                continue

            # =========================================================
            # REGEX ULTRA TOLERANTE: Hidrômetro
            # Âncora no "TIPO DE CONSUMO" que sempre vem depois na mesma linha,
            # para não capturar essa própria palavra quando o campo vem em branco.
            # =========================================================
            if bloco_atual.get('CONTA_DV'):
                match_hidro = re.search(r'HIDR[ÔO]METRO:?\s*([A-Z0-9]*)\s*TIPO\s*DE\s*CONSUMO', linha_upper)
                if match_hidro:
                    bloco_atual['NUM_HIDROMETRO_EXTRAIDO'] = match_hidro.group(1).strip()

    # Salva o último cliente do arquivo após o fim do loop
    if bloco_atual.get('CONTA_DV') and bloco_atual.get('NUM_HIDROMETRO_EXTRAIDO'):
        dados_finais.append(bloco_atual.copy())

    if dados_finais:
        df = pd.DataFrame(dados_finais)
        sem_referencia = int((df['MES_ANO_REF'] == "").sum())
        if sem_referencia:
            print(f"   ⚠️ SANEAGO ANALÍTICA: {sem_referencia} conta(s) sem REFERÊNCIA identificada (nem no texto, nem no nome do arquivo).")
        print(f"   ✅ SANEAGO ANALÍTICA: {len(df)} conta(s) capturada(s) deste arquivo.")
        return df
    else:
        print("   ❌ SANEAGO ANALÍTICA: O ficheiro não continha contas válidas.")
        return None
