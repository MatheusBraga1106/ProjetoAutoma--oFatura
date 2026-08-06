import pandas as pd
import re
from datetime import datetime

def extrair_codego(caminho_txt):
    print("⚙️  A iniciar extração: CODEGO")
    
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
    # A TESOURA (Separando o TXT em Várias Páginas/Faturas)
    # =========================================================
    # Usamos \s+ para garantir que espaços duplos não quebrem a tesoura
    # E [ÉE] para aceitar a palavra com ou sem acento
    padrao_quebra = r'PREFERENCIALMENTE\s+NAS\s+CASAS\s+LOT[ÉE]RICAS\s+AT[ÉE]\s+O\s+VALOR\s+LIMITE'
    
    # Corta o texto numa lista de pedaços
    faturas_separadas = re.split(padrao_quebra, texto_completo, flags=re.IGNORECASE)
    
    print(f"   ✂️ O ficheiro foi dividido em {len(faturas_separadas)} blocos.")

    # =========================================================
    # O LOOP (Processa cada pedaço individualmente)
    # =========================================================
    for indice, pagina_texto in enumerate(faturas_separadas, start=1):
        
        # Ignora blocos muito pequenos (geralmente o final do arquivo após o último corte)
        if len(pagina_texto.strip()) < 100:
            continue

        # =========================================================
        # 1. METADADOS (Buscando APENAS DENTRO DESTE PEDAÇO)
        # =========================================================
        match_cabecalho = re.search(r'R\$?\s*([\d\.,]+)\s+(\d{2}/\d{2}/\d{4})\s+([\w/-]+)\s*\n\s*Pagador', pagina_texto, re.IGNORECASE)
        total_f = formatar_para_sql(match_cabecalho.group(1)) if match_cabecalho else 0.0
        vencimento = match_cabecalho.group(2) if match_cabecalho else "NÃO ACHOU"
        num_fatura = match_cabecalho.group(3) if match_cabecalho else "NÃO ACHOU"

        match_hidro = re.search(r'Hidrometro:\s*([A-Z0-9]+)', pagina_texto, re.IGNORECASE)
        conta_dv = match_hidro.group(1) if match_hidro else "NÃO ACHOU"

        match_mes = re.search(r'Hidrometro:[^\n]*\n\s*([A-Za-z]+/\d{4})', pagina_texto, re.IGNORECASE)
        mes_ano_ref = match_mes.group(1).title() if match_mes else "NÃO ACHOU"
        
        meses = {
            "Janeiro": "01", "Fevereiro": "02", "Marco": "03", "Março": "03",
            "Abril": "04", "Maio": "05", "Junho": "06", "Julho": "07",
            "Agosto": "08", "Setembro": "09", "Outubro": "10", "Novembro": "11", "Dezembro": "12"
        }
        if mes_ano_ref != "NÃO ACHOU":
            partes_mes = mes_ano_ref.split('/')
            if len(partes_mes) == 2:
                mes_nome = partes_mes[0].strip()
                ano = partes_mes[1].strip()
                mes_num = meses.get(mes_nome, mes_nome)
                mes_ano_ref = f"{mes_num}/{ano}"

        match_nome = re.search(r'Pagador\s+(.*?)\s*Código de Baixa', pagina_texto, re.IGNORECASE)
        nome = match_nome.group(1).strip() if match_nome else "NÃO ACHOU"

        logradouro = ""
        match_log = re.search(r'Pagador[^\n]*\n(.*?CEP:.*?-[A-Z\s]{2,4})', pagina_texto, re.DOTALL | re.IGNORECASE)
        if match_log:
            log_bruto = match_log.group(1)
            log_bruto = re.sub(r'\d{15,}', '', log_bruto)
            logradouro = re.sub(r'\s+', ' ', log_bruto).strip()

        # =========================================================
        # 2. VALORES DE CONSUMO E FATURAMENTO
        # =========================================================
        match_cons = re.search(r'Consumo:\s*M[³3]\s*([\d\.,]+)', pagina_texto, re.IGNORECASE)
        consumo_f = formatar_para_sql(match_cons.group(1)) if match_cons else 0.0

        match_agua = re.search(r'Agua:\s*([\d\.,]+)', pagina_texto, re.IGNORECASE)
        agua_f = formatar_para_sql(match_agua.group(1)) if match_agua else 0.0

        match_esgoto = re.search(r'Coleta/Esgoto:\s*([\d\.,]+)', pagina_texto, re.IGNORECASE)
        esgoto_f = formatar_para_sql(match_esgoto.group(1)) if match_esgoto else 0.0

        taxas_extras_f = round(total_f - agua_f - esgoto_f, 2)

        # =========================================================
        # PRINT DE DEBUG (O RAIOS-X POR PÁGINA)
        # =========================================================
        # Só imprime se encontrou algo, para não sujar a tela com lixo
        if num_fatura != "NÃO ACHOU" or conta_dv != "NÃO ACHOU":
            print(f"   🔎 RAIO-X CODEGO (Bloco {indice}):")
            print(f"      ├─ Hidrómetro: {conta_dv}")
            print(f"      ├─ Fatura:     {num_fatura}")
            print(f"      ├─ Mês/Ano:    {mes_ano_ref}")
            print(f"      ├─ Vencim.:    {vencimento}")
            print(f"      └─ Nome:       {nome}")

            # =========================================================
            # 3. SALVAMENTO DA FATURA INDIVIDUAL
            # =========================================================
            dados_finais.append({
                "CONCESSIONARIA": "CODEGO",
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

    # =========================================================
    # RETORNO PARA O MAESTRO
    # =========================================================
    if dados_finais:
        df = pd.DataFrame(dados_finais)
        print(f"   ✅ CODEGO: Extração concluída! Foram capturadas {len(df)} faturas deste arquivo.")
        return df
    else:
        print("   ❌ CODEGO: O ficheiro não continha faturas válidas.")
        return None