#!/bin/bash
clear
DATA1=$(date +%s)
echo "Horário de início da execução: $(date)"
echo "Convertendo os arquivos PDF em TXT. Aguarde..."
echo "------------------------------------------------"

# O -iname busca por .pdf ou .PDF
# O -print0 junto com o read -d '' garante que espaços e o "Ç" não quebrem o script
find ./ -type f -iname "*.pdf" -print0 | while IFS= read -r -d '' arquivo; do
    # Remove a extensão original (.pdf ou .PDF) e coloca .txt
    nome_txt="${arquivo%.*}.txt"
    
    echo "Processando: $arquivo"
    # Faz a conversão
    pdftotext -layout "$arquivo" "$nome_txt"
done

DATA2=$(date +%s)
DIFERENCA=$((DATA2 - DATA1))
echo "------------------------------------------------"
echo "Conversão finalizada!"
echo "Tempo de execução em segundos: $DIFERENCA"