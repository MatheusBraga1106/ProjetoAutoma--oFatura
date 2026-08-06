import pandas as pd
import re
import os

def extrair_saneago_analitica(caminho_txt):
    nome_arquivo = os.path.basename(caminho_txt)
    print(f"  Iniciando extração: SANEAGO ANALÍTICA ({nome_arquivo})")
    
    dados_finais = []
    
    # 1. DATA GLOBAL (Via Nome do Arquivo)
    mes_ano_global = ""
    match_data_arquivo = re.search(r'(\d{2})[_\.\-](\d{4})', nome_arquivo)
    if match_data_arquivo:
        mes_ano_global = f"{match_data_arquivo.group(1)}/{match_data_arquivo.group(2)}"
    
    bloco_atual = {}
    
    with open(caminho_txt, 'r', encoding='utf-8', errors='replace') as f:
        for linha in f:
            linha_upper = linha.strip().upper()
            
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
                
                bloco_atual = {
                    'CONTA_DV': conta_formatada,
                    'MES_ANO_REF': mes_ano_global 
                }
                continue
                
            # =========================================================
            # REGEX ULTRA TOLERANTE: Hidrômetro
            # =========================================================
            if bloco_atual.get('CONTA_DV'):
                match_hidro = re.search(r'HIDR.*?METRO.*?:\s*([A-Z0-9]+)', linha_upper)
                if match_hidro:
                    bloco_atual['NUM_HIDROMETRO_EXTRAIDO'] = match_hidro.group(1).strip()

    # Salva o último cliente do arquivo após o fim do loop
    if bloco_atual.get('CONTA_DV') and bloco_atual.get('NUM_HIDROMETRO_EXTRAIDO'):
         dados_finais.append(bloco_atual.copy())

    if dados_finais:
        return pd.DataFrame(dados_finais)
    else:
        return None