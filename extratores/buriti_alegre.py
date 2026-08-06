import pandas as pd
import re
from datetime import datetime

def extrair_buriti_alegre(caminho_txt):
    print("⚙️  A iniciar extração: BURITI ALEGRE AMBIENTAL")
    
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
    # ✂️ A TESOURA BURITI ALEGRE
    # =========================================================
    padrao_quebra = r'AUTENTICAÇ[AÃÂ]O NO VERSO'
    
    faturas_separadas = re.split(padrao_quebra, texto_completo, flags=re.IGNORECASE)
    print(f"   ✂️ O ficheiro foi dividido em {len(faturas_separadas)} blocos.")

    for indice, pagina_texto in enumerate(faturas_separadas, start=1):
        if len(pagina_texto.strip()) < 100:
            continue

        # =========================================================
        # 1. METADADOS BURITI ALEGRE
        # =========================================================
        match_fatura = re.search(r'FATURA:\s*\d{2}/\d{4}\s*N[º°o]\s*(\d+)', pagina_texto, re.IGNORECASE)
        num_fatura = match_fatura.group(1) if match_fatura else "NÃO ACHOU"

        match_vencimento = re.search(r'VENCIMENTO:\s*(\d{2}/\d{2}/\d{4})', pagina_texto, re.IGNORECASE)
        vencimento = match_vencimento.group(1) if match_vencimento else "NÃO ACHOU"

        match_conta = re.search(r'MATR[ÍI]CULA:\s*(\d+)\s*D[ÍI]GITO:\s*(\d+)', pagina_texto, re.IGNORECASE)
        if match_conta:
            conta_dv = f"{match_conta.group(1)}-{match_conta.group(2)}"
        else:
            conta_dv = "NÃO ACHOU"

        match_mes = re.search(r'M[êe]s/Ano Faturamento:\s*(\d{2}/\d{4})', pagina_texto, re.IGNORECASE)
        mes_ano_ref = match_mes.group(1) if match_mes else "NÃO ACHOU"

        match_nome = re.search(r'NOME:\s*(.*?)(?=\s{4,}|$)', pagina_texto, re.IGNORECASE)
        nome = match_nome.group(1).strip() if match_nome else "NÃO ACHOU"

        # =========================================================
        # 2. VALORES BURITI ALEGRE
        # =========================================================
        match_cons = re.search(r'Consumo Faturado:\s*([\d\.,]+)', pagina_texto, re.IGNORECASE)
        consumo_f = formatar_para_sql(match_cons.group(1)) if match_cons else 0.0

        # Captura: TARIFA AGUA - 282,53
        match_agua = re.search(r'TARIFA AGUA\s*-\s*([\d\.,]+)', pagina_texto, re.IGNORECASE)
        agua_f = formatar_para_sql(match_agua.group(1)) if match_agua else 0.0

        match_esgoto = re.search(r'ESGOTO\s+([\d\.,]+)', pagina_texto, re.IGNORECASE)
        esgoto_f = formatar_para_sql(match_esgoto.group(1)) if match_esgoto else 0.0

        match_total = re.search(r'VALOR\s*\(R\$\):\s*([\d\.,]+)', pagina_texto, re.IGNORECASE)
        total_f = formatar_para_sql(match_total.group(1)) if match_total else 0.0

        taxas_extras_f = round(total_f - agua_f - esgoto_f, 2)

        if num_fatura != "NÃO ACHOU" or conta_dv != "NÃO ACHOU":
            print(f"   🔎 RAIO-X BURITI ALEGRE (Bloco {indice}):")
            print(f"      ├─ Conta:      {conta_dv}")
            print(f"      ├─ Fatura:     {num_fatura}")
            print(f"      ├─ Vencim.:    {vencimento}")
            print(f"      └─ Total:      R$ {total_f}")
            
            dados_finais.append({
                "CONCESSIONARIA": "BURITI_ALEGRE",
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
        print(f"   ✅ BURITI ALEGRE: Extração concluída! Foram capturadas {len(df)} faturas deste arquivo.")
        return df
    else:
        print("   ❌ BURITI ALEGRE: O ficheiro não continha faturas válidas.")
        return None