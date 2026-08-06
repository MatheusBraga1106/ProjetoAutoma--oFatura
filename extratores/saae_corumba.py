import pandas as pd
import re
from datetime import datetime

def extrair_saae_corumba(caminho_txt):
    print("⚙️  A iniciar extração: SAAE CORUMBÁ")
    
    with open(caminho_txt, 'r', encoding='utf-8', errors='replace') as f:
        texto_completo = f.read()

    dados_finais = []
    data_lote = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def formatar_para_sql(valor_str):
        if not valor_str: return 0.0
        valor_str = valor_str.strip()
        if valor_str.startswith(','):
            valor_str = '0' + valor_str
        try:
            return float(valor_str.replace('.', '').replace(',', '.'))
        except ValueError:
            return 0.0

    # =========================================================
    # ✂️ A TESOURA SAAE CORUMBÁ
    # =========================================================
    # A fatura termina com "Autenticação Mecênica" (com erro de digitação no original)
    padrao_quebra = r'Autentica[çc][aã]o Mec[êeâa]nica'
    
    faturas_separadas = re.split(padrao_quebra, texto_completo, flags=re.IGNORECASE)
    print(f"   ✂️ O ficheiro foi dividido em {len(faturas_separadas)} blocos.")

    for indice, pagina_texto in enumerate(faturas_separadas, start=1):
        if len(pagina_texto.strip()) < 100:
            continue

        # =========================================================
        # 1. METADADOS SAAE CORUMBÁ
        # =========================================================
        # N.       605732
        match_fatura = re.search(r'N\.\s+(\d+)\s+Parcela', pagina_texto, re.IGNORECASE)
        num_fatura = match_fatura.group(1) if match_fatura else "NÃO ACHOU"

        # Nº Conta: 2338
        match_conta = re.search(r'Nº Conta:\s*(\d+)', pagina_texto, re.IGNORECASE)
        conta_dv = match_conta.group(1) if match_conta else "NÃO ACHOU"

        # Referência  11 / 2021
        match_mes = re.search(r'Referência\s*(\d{1,2})\s*/\s*(\d{4})', pagina_texto, re.IGNORECASE)
        if match_mes:
            mes = match_mes.group(1).zfill(2) # Garante que mês 1 vira 01
            ano = match_mes.group(2)
            mes_ano_ref = f"{mes}/{ano}"
        else:
            mes_ano_ref = "NÃO ACHOU"

        # Nome: FORUM DE CORUMBÁ DE GOIÁS (Para no primeiro grande bloco de espaços)
        match_nome = re.search(r'Nome:\s*(.*?)(?=\s{4,}|$)', pagina_texto, re.IGNORECASE)
        nome = match_nome.group(1).strip() if match_nome else "NÃO ACHOU"

        # Vencimento: 10/01/2022 (Fica geralmente isolado após a palavra Vencimento)
        match_vencimento = re.search(r'Vencimento:[^\d]*(\d{2}/\d{2}/\d{4})', pagina_texto, re.IGNORECASE)
        vencimento = match_vencimento.group(1) if match_vencimento else "NÃO ACHOU"

        # =========================================================
        # 2. VALORES SAAE CORUMBÁ
        # =========================================================
        # Consumo do mês: 35
        match_cons = re.search(r'Consumo do mês:\s*(\d+)', pagina_texto, re.IGNORECASE)
        consumo_f = formatar_para_sql(match_cons.group(1)) if match_cons else 0.0

        # TARIFA DE AGUA    187,25
        match_agua = re.search(r'TARIFA DE [AÁ]GUA\s+([\d\.,]+)', pagina_texto, re.IGNORECASE)
        agua_f = formatar_para_sql(match_agua.group(1)) if match_agua else 0.0

        # Esgoto (Pode não ter, mas deixamos mapeado)
        match_esgoto = re.search(r'ESGOTO\s+([\d\.,]+)', pagina_texto, re.IGNORECASE)
        esgoto_f = formatar_para_sql(match_esgoto.group(1)) if match_esgoto else 0.0

        # (-) Valor do Pagamento (O valor fica na linha de baixo, às vezes com "Declaração:")
        # (-) Valor do Pagamento
        #        Declaração:                                                  R$ 165,92
        match_total = re.search(r'\(-\)\s*Valor do Pagamento\s*(?:Declara[çc][aã]o:)?\s*R\$\s*([\d\.,]+)', pagina_texto, re.IGNORECASE)
        total_f = formatar_para_sql(match_total.group(1)) if match_total else 0.0

        taxas_extras_f = round(total_f - agua_f - esgoto_f, 2)

        if num_fatura != "NÃO ACHOU" or conta_dv != "NÃO ACHOU":
            print(f"   🔎 RAIO-X SAAE CORUMBÁ (Bloco {indice}):")
            print(f"      ├─ Conta:      {conta_dv}")
            print(f"      ├─ Fatura:     {num_fatura}")
            print(f"      ├─ Vencim.:    {vencimento}")
            print(f"      └─ Total:      R$ {total_f}")
            
            dados_finais.append({
                "CONCESSIONARIA": "SAAE_CORUMBA",
                "NUM_FATURA": num_fatura if num_fatura != "NÃO ACHOU" else "",
                "MES_ANO_REF": mes_ano_ref if mes_ano_ref != "NÃO ACHOU" else "",
                "VENCIMENTO": vencimento if vencimento != "NÃO ACHOU" else "",
                "COD_ORGAO_AGRUPADOR": "", 
                "COD_ORGAO_PAGADOR": "",
                "NOME_ORGAO_AGRUPADOR": "",
                "CONTA_DV": conta_dv if conta_dv != "NÃO ACHOU" else "",
                "NOME_CLIENTE": nome if nome != "NÃO ACHOU" else "",
                "LOGRADOURO": "", 
                "CONSUMO_M3": consumo_f,
                "VALOR_AGUA": agua_f,
                "VALOR_ESGOTO": esgoto_f,
                "VALOR_TAXAS_EXTRAS": taxas_extras_f,
                "VALOR_TOTAL": total_f,
                "DATA_PROCESSAMENTO": data_lote
            })

    if dados_finais:
        df = pd.DataFrame(dados_finais)
        print(f"   ✅ SAAE CORUMBÁ: Extração concluída! Foram capturadas {len(df)} faturas deste arquivo.")
        return df
    else:
        print("   ❌ SAAE CORUMBÁ: O ficheiro não continha faturas válidas.")
        return None