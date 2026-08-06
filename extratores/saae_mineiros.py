import pandas as pd
import re
from datetime import datetime

def extrair_saae_mineiros(caminho_txt):
    print("⚙️  A iniciar extração: SAAE MINEIROS")
    
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
    # ✂️ A TESOURA SAAE MINEIROS
    # =========================================================
    padrao_quebra = r'Autentica[çc][aã]o Mec[êeâa]nica'
    
    faturas_separadas = re.split(padrao_quebra, texto_completo, flags=re.IGNORECASE)
    print(f"   ✂️ O ficheiro foi dividido em {len(faturas_separadas)} blocos.")

    for indice, pagina_texto in enumerate(faturas_separadas, start=1):
        if len(pagina_texto.strip()) < 100:
            continue

        # =========================================================
        # 1. METADADOS SAAE MINEIROS
        # =========================================================
        # N. Duam:11141179 ou N.       11141179
        match_fatura = re.search(r'N\.\s*(?:Duam:)?\s*(\d+)', pagina_texto, re.IGNORECASE)
        num_fatura = match_fatura.group(1) if match_fatura else "NÃO ACHOU"

        # Nº Conta: 030285
        match_conta = re.search(r'Nº Conta:\s*(\d+)', pagina_texto, re.IGNORECASE)
        conta_dv = match_conta.group(1) if match_conta else "NÃO ACHOU"

        # Referência:  2 / 2026
        match_mes = re.search(r'Referência:\s*(\d{1,2})\s*/\s*(\d{4})', pagina_texto, re.IGNORECASE)
        if match_mes:
            mes = match_mes.group(1).zfill(2) # Garante que o mês 2 vira 02
            ano = match_mes.group(2)
            mes_ano_ref = f"{mes}/{ano}"
        else:
            mes_ano_ref = "NÃO ACHOU"

        # Nome: TRIBUNAL DE JUSTICA DO ESTADO DE GOIAS
        match_nome = re.search(r'Nome:\s*(.*?)(?=\s{4,}|$)', pagina_texto, re.IGNORECASE)
        nome = match_nome.group(1).strip() if match_nome else "NÃO ACHOU"

        # Vencimento: \n 10/03/2026
        match_vencimento = re.search(r'Vencimento:\s*(\d{2}/\d{2}/\d{4})', pagina_texto, re.IGNORECASE)
        vencimento = match_vencimento.group(1) if match_vencimento else "NÃO ACHOU"

        # =========================================================
        # 2. VALORES SAAE MINEIROS
        # =========================================================
        # Consumo do mês: 51
        match_cons = re.search(r'Consumo do m[êe]s:\s*(\d+)', pagina_texto, re.IGNORECASE)
        consumo_f = formatar_para_sql(match_cons.group(1)) if match_cons else 0.0

        # SERV. DE CAPTACAO E DIST. AGUA                                                                                                       524,67
        match_agua = re.search(r'CAPTACAO\s*E\s*DIST\.\s*AGUA\s+([\d\.,]+)', pagina_texto, re.IGNORECASE)
        agua_f = formatar_para_sql(match_agua.group(1)) if match_agua else 0.0

        # SERV. DE COLETA E AFASTAMENTO DE ESG                                                                                                 262,34
        match_esgoto = re.search(r'COLETA\s*E\s*AFASTAMENTO\s*DE\s*ESG\s+([\d\.,]+)', pagina_texto, re.IGNORECASE)
        esgoto_f = formatar_para_sql(match_esgoto.group(1)) if match_esgoto else 0.0

        # (-) Valor do Pagamento \n R$ 843,20 (A mesma regra robusta que criámos antes)
        match_total = re.search(r'\(-\)\s*Valor do Pagamento\s*R\$\s*([\d\.,]+)', pagina_texto, re.IGNORECASE)
        total_f = formatar_para_sql(match_total.group(1)) if match_total else 0.0

        taxas_extras_f = round(total_f - agua_f - esgoto_f, 2)

        if num_fatura != "NÃO ACHOU" or conta_dv != "NÃO ACHOU":
            print(f"   🔎 RAIO-X SAAE MINEIROS (Bloco {indice}):")
            print(f"      ├─ Conta:      {conta_dv}")
            print(f"      ├─ Fatura:     {num_fatura}")
            print(f"      ├─ Vencim.:    {vencimento}")
            print(f"      └─ Total:      R$ {total_f}")
            
            dados_finais.append({
                "CONCESSIONARIA": "SAAE_MINEIROS",
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
        print(f"   ✅ SAAE MINEIROS: Extração concluída! Foram capturadas {len(df)} faturas deste arquivo.")
        return df
    else:
        print("   ❌ SAAE MINEIROS: O ficheiro não continha faturas válidas.")
        return None