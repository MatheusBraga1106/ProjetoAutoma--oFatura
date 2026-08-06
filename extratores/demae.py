import pandas as pd
import re
from datetime import datetime

def extrair_demae(caminho_txt):
    print("⚙️  A iniciar extração: DEMAE")
    
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
    # ✂️ A TESOURA DEMAE
    # =========================================================
    # A frase "AUTENTICAÇÂO NO VERSO" aparece no rodapé do canhoto de pagamento, 
    # o que a torna um excelente ponto de corte para dividir as faturas do arquivo.
    padrao_quebra = r'AUTENTICAÇ[AÃÂ]O NO VERSO'
    
    faturas_separadas = re.split(padrao_quebra, texto_completo, flags=re.IGNORECASE)
    print(f"   ✂️ O ficheiro foi dividido em {len(faturas_separadas)} blocos.")

    for indice, pagina_texto in enumerate(faturas_separadas, start=1):
        if len(pagina_texto.strip()) < 100:
            continue

        # =========================================================
        # 1. METADADOS DEMAE
        # =========================================================
        # Fatura nº: 6827883
        match_fatura = re.search(r'Fatura\s*n[º°o]:\s*(\d+)', pagina_texto, re.IGNORECASE)
        num_fatura = match_fatura.group(1) if match_fatura else "NÃO ACHOU"

        # Data de Vencimento: 20/04/2021
        match_vencimento = re.search(r'Data\s*de\s*Vencimento:\s*(\d{2}/\d{2}/\d{4})', pagina_texto, re.IGNORECASE)
        vencimento = match_vencimento.group(1) if match_vencimento else "NÃO ACHOU"

        # Matrícula: 21544-9
        match_conta = re.search(r'Matrícula:\s*([\d-]+)', pagina_texto, re.IGNORECASE)
        conta_dv = match_conta.group(1) if match_conta else "NÃO ACHOU"

        # Referência: 03/2021
        match_mes = re.search(r'Referência:\s*(\d{2}/\d{4})', pagina_texto, re.IGNORECASE)
        mes_ano_ref = match_mes.group(1) if match_mes else "NÃO ACHOU"

        # MORADOR:  TRIBUNAL DE JUSTIÇA DO ESTADO DE GOIÁS
        match_nome = re.search(r'MORADOR:\s*([^\n]+)', pagina_texto, re.IGNORECASE)
        nome = match_nome.group(1).strip() if match_nome else "NÃO ACHOU"

        # =========================================================
        # 2. VALORES DEMAE
        # =========================================================
        # Captura o valor na linha da tabela que contém a palavra "(Atual)"
        # Ex: 03/2021 (Atual)                   Lido              2394          63 
        match_cons = re.search(r'\(Atual\).*?Lido\s+\d+\s+([\d\.,]+)', pagina_texto, re.IGNORECASE)
        consumo_f = formatar_para_sql(match_cons.group(1)) if match_cons else 0.0

        # FATURAMENTO AGUA                                                                                                       497,84
        match_agua = re.search(r'FATURAMENTO AGUA\s+([\d\.,]+)', pagina_texto, re.IGNORECASE)
        agua_f = formatar_para_sql(match_agua.group(1)) if match_agua else 0.0

        # Procura por Esgoto (caso exista em outras faturas, mesmo não estando neste exemplo específico)
        match_esgoto = re.search(r'ESGOTO\s+([\d\.,]+)', pagina_texto, re.IGNORECASE)
        esgoto_f = formatar_para_sql(match_esgoto.group(1)) if match_esgoto else 0.0

        # TOTAL A PAGAR                                                                                                          536,69
        match_total = re.search(r'TOTAL A PAGAR\s+([\d\.,]+)', pagina_texto, re.IGNORECASE)
        total_f = formatar_para_sql(match_total.group(1)) if match_total else 0.0

        # O que sobrar do total menos água e esgoto, vai para taxas extras (ex: Taxa de lixo)
        taxas_extras_f = round(total_f - agua_f - esgoto_f, 2)

        if num_fatura != "NÃO ACHOU" or conta_dv != "NÃO ACHOU":
            print(f"   🔎 RAIO-X DEMAE (Bloco {indice}):")
            print(f"      ├─ Conta:      {conta_dv}")
            print(f"      ├─ Fatura:     {num_fatura}")
            print(f"      ├─ Vencim.:    {vencimento}")
            print(f"      └─ Total:      R$ {total_f}")
            
            dados_finais.append({
                "CONCESSIONARIA": "DEMAE",
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
        print(f"   ✅ DEMAE: Extração concluída! Foram capturadas {len(df)} faturas deste arquivo.")
        return df
    else:
        print("   ❌ DEMAE: O ficheiro não continha faturas válidas.")
        return None