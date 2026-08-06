import pandas as pd
import re
from datetime import datetime

def extrair_sae(caminho_txt):
    with open(caminho_txt, 'r', encoding='utf-8', errors='replace') as f:
        texto_completo = f.read()

    dados_finais = []
    data_lote = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def formatar_para_sql(valor_str):
        if not valor_str: return 0.0
        try:
            return float(valor_str.replace('.', '').replace(',', '.'))
        except ValueError:
            return 0.0

    # =========================================================
    # 1. METADADOS (REGEX AFROUXADO / TOLERANTE A ERROS)
    # =========================================================
    
    # Tolerância a falhas no acento de "Mês" e espaços extras
    match_mes = re.search(r'M[êe]s/Ano Faturamento:?\s*(\d{2}/\d{4})', texto_completo, re.IGNORECASE)
    mes_ano_ref = match_mes.group(1) if match_mes else "NÃO ACHOU"

    # Tolerância: Procura a palavra FATURA e pega o primeiro número grande (6+ dígitos) que aparecer perto
    match_fatura = re.search(r'\bFATURA\b[^\d]*(\d{5,})', texto_completo, re.IGNORECASE)
    num_fatura = match_fatura.group(1) if match_fatura else "NÃO ACHOU"

    # Tolerância: Procura "Vencimento", pula qualquer coisa, acha "Valor" e pega a data
    match_venc = re.search(r'Vencimento.*?Valor.*?(\d{2}/\d{2}/\d{4})', texto_completo, re.IGNORECASE | re.DOTALL)
    vencimento = match_venc.group(1) if match_venc else "NÃO ACHOU"

    # Tolerância a acentos em "Matrícula" e "Dígito"
    match_mat = re.search(r'Matr[íi]cula[^\d]*(\d{4,})\s*[-/]?\s*(\d{1,2})', texto_completo, re.IGNORECASE)
    if match_mat:
        conta_dv = f"{match_mat.group(1)} {match_mat.group(2)}"
    else:
        conta_dv = "NÃO ACHOU"

    # O nome costuma ficar antes de VIA DO CONTRIBUINTE
    match_nome = re.search(r'([^\n]+)\s*VIA DO CONTRIBUINTE', texto_completo, re.IGNORECASE)
    nome = match_nome.group(1).strip() if match_nome else "NÃO ACHOU"

    # =========================================================
    # PRINT DE DEBUG (O RAIOS-X)
    # =========================================================
    print(f"   🔎 RAIO-X SAE:")
    print(f"      ├─ Matrícula: {conta_dv}")
    print(f"      ├─ Fatura:    {num_fatura}")
    print(f"      ├─ Mês/Ano:   {mes_ano_ref}")
    print(f"      ├─ Vencim.:   {vencimento}")
    print(f"      └─ Nome:      {nome}")

    # Endereço
    logradouro = ""
    match_log = re.search(r'VIA DO CONTRIBUINTE\s*(.*?CEP:?\s*\d{5}-\d{3})', texto_completo, re.DOTALL | re.IGNORECASE)
    if match_log:
        log_bruto = match_log.group(1)
        log_bruto = re.sub(r'^\s*\(\d+\)\s*', '', log_bruto)
        logradouro = re.sub(r'\s+', ' ', log_bruto).strip()

    # =========================================================
    # 2. VALORES FINANCEIROS E DE CONSUMO
    # =========================================================
    match_cons = re.search(r'Consumo Faturado:\s*(\d+)', texto_completo, re.IGNORECASE)
    consumo_f = formatar_para_sql(match_cons.group(1)) if match_cons else 0.0

    match_agua = re.search(r'FATURAMENTO AGUA\s*-\s*([\d\.,]+)', texto_completo, re.IGNORECASE)
    agua_f = formatar_para_sql(match_agua.group(1)) if match_agua else 0.0

    match_esgoto = re.search(r'FATURAMENTO ESGOTO\s*-\s*([\d\.,]+)', texto_completo, re.IGNORECASE)
    esgoto_f = formatar_para_sql(match_esgoto.group(1)) if match_esgoto else 0.0

    match_total = re.search(r'TOTAL A PAGAR\s*([\d\.,]+)', texto_completo, re.IGNORECASE)
    total_f = formatar_para_sql(match_total.group(1)) if match_total else 0.0

    taxas_extras_f = round(total_f - agua_f - esgoto_f, 2)

    # =========================================================
    # 3. SALVAMENTO (Agora a regra é mais flexível)
    # =========================================================
    # Se ele achar PELO MENOS a Conta ou a Fatura, ele salva a linha
    if num_fatura != "NÃO ACHOU" or conta_dv != "NÃO ACHOU": 
        dados_finais.append({
            "CONCESSIONARIA": "SAE CATALÃO",
            "NUM_FATURA": num_fatura if num_fatura != "NÃO ACHOU" else "",
            "MES_ANO_REF": mes_ano_ref if mes_ano_ref != "NÃO ACHOU" else "",
            "VENCIMENTO": vencimento if vencimento != "NÃO ACHOU" else "",
            "COD_ORGAO_AGRUPADOR": "", 
            "COD_ORGAO_PAGADOR": "",
            "NOME_ORGAO_AGRUPADOR": "",
            "CONTA_DV": conta_dv if conta_dv != "NÃO ACHOU" else "",
            "NOME_CLIENTE": nome if nome != "NÃO ACHOU" else "",
            "LOGRADOURO": logradouro,
            "CONSUMO_M3": consumo_f,
            "VALOR_AGUA": agua_f,
            "VALOR_ESGOTO": esgoto_f,
            "VALOR_TAXAS_EXTRAS": taxas_extras_f,
            "VALOR_TOTAL": total_f,
            "DATA_PROCESSAMENTO": data_lote
        })

    if dados_finais:
        df = pd.DataFrame(dados_finais)
        print("   ✅ SAE: Dados salvos!")
        return df
    else:
        print("   ❌ SAE: O arquivo está irreconhecível.")
        return None