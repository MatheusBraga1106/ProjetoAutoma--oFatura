import pandas as pd
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

def extrair_saneago(caminho_txt):
    print("  Iniciando extração: SANEAGO PRINCIPAL")
    
    with open(caminho_txt, 'r', encoding='utf-8', errors='replace') as f:
        linhas = f.readlines()

    dados_finais = []
    data_lote = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Memória do Cabeçalho
    meta_n_fatura = ""
    meta_mes_ano = ""
    meta_vencimento = ""
    meta_nome_agrupador = ""
    meta_cod_agrupador = ""
    meta_cod_pagador = ""
    
    # Estados da Máquina
    dentro_da_tabela = False
    capturando_orgao = False
    buffer_nome_orgao = []

    def formatar_para_sql(valor_str):
        if not valor_str: return Decimal('0.00')
        try:
            valor_limpo = str(valor_str).replace('.', '').replace(',', '.')
            return Decimal(valor_limpo)
        except (ValueError, InvalidOperation):
            return Decimal('0.00')

    for linha in linhas:
        linha_bruta = linha.rstrip('\n')
        linha_limpa = linha_bruta.strip()
        linha_upper = linha_limpa.upper()
        
        # =========================================================
        # 1. CAPTURA DE METADADOS
        # =========================================================
        match_fatura = re.search(r'N[º°Oo]?\s*FATURA:\s*(\d+)', linha_bruta, re.IGNORECASE)
        if match_fatura: meta_n_fatura = match_fatura.group(1)

        match_mes = re.search(r'M[ÊE]S/ANO\s*REF:?\s*([\d/]+)', linha_bruta, re.IGNORECASE)
        if match_mes: meta_mes_ano = match_mes.group(1)

        match_venc = re.search(r'VENCIMENTO:\s*([\d/]+)', linha_bruta, re.IGNORECASE)
        if match_venc: meta_vencimento = match_venc.group(1)

        # GATILHO BLINDADO: Ignora espaços esmagados e variações de acento (ORGAO/ÓRGÃO)
        if re.search(r'NOME.*?DO.*?[OÓ]RG[AÃ]O.*?AGRUPADOR', linha_upper):
            capturando_orgao = True
            buffer_nome_orgao = []
            continue

        if capturando_orgao:
            # Trava de segurança flexível para pular a linha do cabeçalho das colunas
            if not linha_limpa or re.search(r'C[OÓ]D.*?[OÓ]RG', linha_upper):
                continue
                
            match_orgao = re.search(r'^(.*?)\s+(\d{3,})\s+(\d{3,})\s*$', linha_limpa)
            if match_orgao:
                buffer_nome_orgao.append(match_orgao.group(1).strip())
                meta_nome_agrupador = " ".join(buffer_nome_orgao).replace("  ", " ")
                meta_cod_agrupador = match_orgao.group(2)
                meta_cod_pagador = match_orgao.group(3)
                capturando_orgao = False
            else:
                buffer_nome_orgao.append(linha_limpa)
            if match_orgao:
                buffer_nome_orgao.append(match_orgao.group(1).strip())
                meta_nome_agrupador = " ".join(buffer_nome_orgao).replace("  ", " ")
                meta_cod_agrupador = match_orgao.group(2)
                meta_cod_pagador = match_orgao.group(3)
                capturando_orgao = False
            else:
                buffer_nome_orgao.append(linha_limpa)

        # =========================================================
        # 2. GATILHO E MÁQUINA DE ESTADOS
        # =========================================================
        # GATILHO INFALÍVEL: Procura pelas colunas exatas da mesma linha
        match_gatilho = re.search(r'CONTA.*?DV.*?NOME.*?CLIENTE', linha_upper)
        if match_gatilho:
            dentro_da_tabela = True
            continue

        if dentro_da_tabela:
            # Desliga ao encontrar os rodapés
            if "TOTAL" in linha_upper or "INFORMAÇÕES" in linha_upper or "RETENÇÕES" in linha_upper or "FATURADO" in linha_upper:
                dentro_da_tabela = False
                continue

            match_conta = re.match(r'^(\d+\s\d)', linha_limpa)
            
            if match_conta:
                conta_dv = match_conta.group(1).strip()
                
                # Quebra a linha em "Pedaços" sempre que houver 2 ou mais espaços juntos
                pecas = re.split(r'\s{2,}', linha_limpa)
                
                # Extração Reversa: Pega os números do final da linha de trás para frente
                numeros = []
                for p in reversed(pecas):
                    # Se for exclusivamente composto por dígitos, pontos e vírgulas, é um número
                    if re.match(r'^[\d.,]+$', p) and any(c.isdigit() for c in p):
                        numeros.insert(0, p)
                    else:
                        break # Parou de ser número, chegamos no endereço
                
                # Todo o resto da linha (menos a conta no índice 0) é texto (Nome e Endereço)
                idx_limite = len(pecas) - len(numeros)
                texto_parts = pecas[1:idx_limite]
                
                if len(texto_parts) == 1:
                    nome = texto_parts[0]
                    logradouro = ""
                elif len(texto_parts) >= 2:
                    nome = texto_parts[0]
                    logradouro = " ".join(texto_parts[1:])
                else:
                    nome = ""
                    logradouro = ""

                # =========================================================
                # INTELIGÊNCIA FINANCEIRA (Heurística da Vírgula)
                # =========================================================
                valores_financeiros = [n for n in numeros if ',' in n]
                valores_inteiros = [n for n in numeros if ',' not in n]
                
                # O Consumo é o primeiro número inteiro (Sem Vírgula)
                val_consumo = valores_inteiros[0] if len(valores_inteiros) > 0 else "0"
                
                # Os valores são os números com vírgula, pela ordem da Saneago: Água -> Esgoto -> SMRSU
                val_agua = valores_financeiros[0] if len(valores_financeiros) > 0 else "0.00"
                val_esgoto = valores_financeiros[1] if len(valores_financeiros) > 1 else "0.00"
                val_smrsu = valores_financeiros[2] if len(valores_financeiros) > 2 else "0.00"

                # Matemática Absoluta
                consumo_f = formatar_para_sql(val_consumo)
                agua_f = formatar_para_sql(val_agua)
                esgoto_f = formatar_para_sql(val_esgoto)
                smrsu_f = formatar_para_sql(val_smrsu)
                
                base_calculo_f = agua_f + esgoto_f
                valor_total_f = agua_f + esgoto_f + smrsu_f
                
                dados_finais.append({
                    "CONCESSIONARIA": "SANEAGO", 
                    "NUM_FATURA": meta_n_fatura,
                    "MES_ANO_REF": meta_mes_ano,
                    "VENCIMENTO": meta_vencimento,
                    "COD_ORGAO_AGRUPADOR": meta_cod_agrupador,
                    "COD_ORGAO_PAGADOR": meta_cod_pagador,
                    "NOME_ORGAO_AGRUPADOR": meta_nome_agrupador,
                    "CONTA_DV": conta_dv,
                    "NUM_HIDROMETRO": "", 
                    "NOME_CLIENTE": nome,
                    "LOGRADOURO": logradouro,
                    "CONSUMO_M3": float(consumo_f),
                    "VALOR_AGUA": float(agua_f),
                    "VALOR_ESGOTO": float(esgoto_f),
                    "BASE_CALCULO": float(base_calculo_f), 
                    "VALOR_TAXAS_EXTRAS": float(smrsu_f), 
                    "VALOR_TOTAL": float(valor_total_f),
                    "DATA_PROCESSAMENTO": data_lote
                })

    if dados_finais:
        df = pd.DataFrame(dados_finais)
        print(f"✅ SANEAGO: Extração concluída com sucesso! ({len(df)} faturas)")
        return df
    else:
        print("❌ SANEAGO: Nenhum dado encontrado no arquivo.")
        return None