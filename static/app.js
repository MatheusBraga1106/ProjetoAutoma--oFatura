(() => {
    "use strict";

    const zonaDrop = document.getElementById("zona-drop");
    const inputArquivos = document.getElementById("input-arquivos");
    const inputPasta = document.getElementById("input-pasta");
    const listaSelecionados = document.getElementById("lista-selecionados");
    const ulSelecionados = document.getElementById("ul-selecionados");
    const contagemSelecionados = document.getElementById("contagem-selecionados");
    const botaoProcessar = document.getElementById("botao-processar");
    const botaoLimpar = document.getElementById("botao-limpar");

    const areaProgresso = document.getElementById("area-progresso");
    const barraPreenchida = document.getElementById("barra-progresso-preenchida");
    const textoProgresso = document.getElementById("texto-progresso");
    const ulProgresso = document.getElementById("ul-progresso");

    const areaResultados = document.getElementById("area-resultados");
    const tbodyResultados = document.getElementById("tbody-resultados");
    const detalheErros = document.getElementById("detalhe-erros");
    const ulErros = document.getElementById("ul-erros");

    const bannerStatus = document.getElementById("banner-status");
    const statusUpload = document.getElementById("status-upload");

    function mostrarStatusUpload(texto, tipo = "aviso") {
        statusUpload.textContent = texto;
        statusUpload.className = `status-inline ${tipo}`;
        statusUpload.hidden = false;
    }

    function limparStatusUpload() {
        statusUpload.hidden = true;
        statusUpload.textContent = "";
    }

    // Map<chave, File> — chave = caminho relativo (upload de pasta) ou nome
    const arquivosSelecionados = new Map();

    function chaveDoArquivo(file) {
        return file.webkitRelativePath || file.name;
    }

    function adicionarArquivos(fileList) {
        let ignorados = 0;
        for (const file of fileList) {
            if (!file.name.toLowerCase().endsWith(".pdf")) {
                ignorados++;
                continue;
            }
            arquivosSelecionados.set(chaveDoArquivo(file), file);
        }
        if (ignorados) {
            mostrarStatusUpload(`${ignorados} arquivo(s) ignorado(s) por não serem PDF.`, "aviso");
        } else {
            limparStatusUpload();
        }
        renderizarSelecionados();
    }

    function renderizarSelecionados() {
        ulSelecionados.innerHTML = "";
        for (const chave of arquivosSelecionados.keys()) {
            const li = document.createElement("li");
            li.textContent = chave;
            ulSelecionados.appendChild(li);
        }
        contagemSelecionados.textContent = arquivosSelecionados.size;
        listaSelecionados.hidden = arquivosSelecionados.size === 0;
        botaoProcessar.disabled = arquivosSelecionados.size === 0;
        botaoLimpar.disabled = arquivosSelecionados.size === 0;
    }

    inputArquivos.addEventListener("change", (evento) => adicionarArquivos(evento.target.files));
    inputPasta.addEventListener("change", (evento) => adicionarArquivos(evento.target.files));

    ["dragenter", "dragover"].forEach((tipo) => {
        zonaDrop.addEventListener(tipo, (evento) => {
            evento.preventDefault();
            zonaDrop.classList.add("arrastando");
        });
    });
    ["dragleave", "drop"].forEach((tipo) => {
        zonaDrop.addEventListener(tipo, (evento) => {
            evento.preventDefault();
            zonaDrop.classList.remove("arrastando");
        });
    });
    zonaDrop.addEventListener("drop", (evento) => {
        const arquivos = evento.dataTransfer && evento.dataTransfer.files;
        if (arquivos && arquivos.length) adicionarArquivos(arquivos);
    });

    botaoLimpar.addEventListener("click", () => {
        arquivosSelecionados.clear();
        inputArquivos.value = "";
        inputPasta.value = "";
        limparStatusUpload();
        renderizarSelecionados();
    });

    botaoProcessar.addEventListener("click", iniciarProcessamento);

    async function iniciarProcessamento() {
        if (arquivosSelecionados.size === 0) return;

        botaoProcessar.disabled = true;
        botaoLimpar.disabled = true;

        const formData = new FormData();
        for (const [chave, file] of arquivosSelecionados) {
            formData.append("arquivos", file, chave);
        }

        limparStatusUpload();
        let resposta;
        try {
            resposta = await fetch("/pipeline/jobs", { method: "POST", body: formData });
        } catch (erro) {
            mostrarStatusUpload("Falha ao enviar os arquivos: " + erro, "erro");
            botaoProcessar.disabled = false;
            botaoLimpar.disabled = false;
            return;
        }

        if (!resposta.ok) {
            const detalhe = await resposta.json().catch(() => ({}));
            mostrarStatusUpload("Erro ao criar o processamento: " + (detalhe.detail || resposta.statusText), "erro");
            botaoProcessar.disabled = false;
            botaoLimpar.disabled = false;
            return;
        }

        const { job_id: jobId, total_arquivos: totalArquivos } = await resposta.json();

        areaProgresso.hidden = false;
        areaResultados.hidden = true;
        ulProgresso.innerHTML = "";
        barraPreenchida.style.width = "0%";
        textoProgresso.textContent = `0 / ${totalArquivos} arquivos`;
        areaProgresso.scrollIntoView({ behavior: "smooth", block: "start" });

        acompanharJob(jobId, totalArquivos);
    }

    function acompanharJob(jobId, totalArquivos) {
        const eventSource = new EventSource(`/pipeline/jobs/${jobId}/eventos`);
        let processados = 0;
        const itensPorArquivo = new Map();

        eventSource.addEventListener("progresso", (evento) => {
            const dados = JSON.parse(evento.data);
            processados = dados.indice;

            let li = itensPorArquivo.get(dados.arquivo);
            if (!li) {
                li = document.createElement("li");
                const nome = document.createElement("span");
                nome.textContent = dados.arquivo;
                const estado = document.createElement("span");
                estado.className = "estado";
                li.appendChild(nome);
                li.appendChild(estado);
                itensPorArquivo.set(dados.arquivo, li);
                ulProgresso.prepend(li);
            }

            const estadoSpan = li.querySelector(".estado");
            if (dados.status === "ok") {
                estadoSpan.textContent = `✅ ${dados.empresa}`;
                estadoSpan.className = "estado ok";
            } else {
                estadoSpan.textContent = `❌ ${dados.detalhe || "erro"}`;
                estadoSpan.className = "estado erro";
            }

            barraPreenchida.style.width = `${Math.round((processados / totalArquivos) * 100)}%`;
            textoProgresso.textContent = `${processados} / ${totalArquivos} arquivos`;
        });

        eventSource.addEventListener("concluido", (evento) => {
            const resultado = JSON.parse(evento.data);
            barraPreenchida.style.width = "100%";
            textoProgresso.textContent = `${totalArquivos} / ${totalArquivos} arquivos — concluído`;
            renderizarResultados(jobId, resultado);
            eventSource.close();
            botaoProcessar.disabled = false;
            botaoLimpar.disabled = false;
        });

        eventSource.addEventListener("erro", (evento) => {
            const dados = JSON.parse(evento.data);
            mostrarStatusUpload("Erro no processamento: " + dados.erro, "erro");
            eventSource.close();
            botaoProcessar.disabled = false;
            botaoLimpar.disabled = false;
        });

        eventSource.onerror = () => {
            // Conexão caiu antes do evento "concluido"/"erro" (ex.: job sumiu
            // do processo). Encerra silenciosamente pra não travar a UI.
            eventSource.close();
            botaoProcessar.disabled = false;
            botaoLimpar.disabled = false;
        };
    }

    function renderizarResultados(jobId, resultado) {
        tbodyResultados.innerHTML = "";
        const empresas = resultado.empresas || {};
        const nomes = Object.keys(empresas).sort();

        for (const empresa of nomes) {
            const info = empresas[empresa];
            const tr = document.createElement("tr");

            const semCadastro = info.contas_json_encontrado === false
                ? "contas.json ausente"
                : (info.linhas_sem_correspondencia || 0);

            tr.innerHTML = `
                <td>${empresa}</td>
                <td>${info.adicionadas ?? 0}</td>
                <td>${info.duplicadas ?? 0}</td>
                <td class="${info.suspeitas ? "suspeita" : ""}">${info.suspeitas ?? 0}</td>
                <td>${info.descartadas ?? 0}</td>
                <td>${semCadastro}</td>
                <td><a class="link-csv" href="/pipeline/jobs/${jobId}/csv/${empresa}" target="_blank" rel="noopener">Baixar</a></td>
            `;
            tbodyResultados.appendChild(tr);
        }

        const arquivosComErro = (resultado.arquivos || []).filter((a) => a.status === "erro");
        ulErros.innerHTML = "";
        if (arquivosComErro.length) {
            for (const item of arquivosComErro) {
                const li = document.createElement("li");
                li.textContent = `${item.arquivo_origem}: ${item.erro}`;
                ulErros.appendChild(li);
            }
            detalheErros.hidden = false;
        } else {
            detalheErros.hidden = true;
        }

        areaResultados.hidden = false;
        areaResultados.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    async function carregarStatusSistema() {
        try {
            const resposta = await fetch("/pipeline/status-sistema");
            if (!resposta.ok) return;
            const status = await resposta.json();

            const avisos = [];
            if (!status.contas_json_encontrado) {
                avisos.push("contas.json não encontrado — as faturas serão salvas sem UNIDADE JUDICIÁRIA/ENDEREÇO/distribuição de valores.");
            }
            if (!status.tesseract_resolvido) {
                avisos.push("Tesseract (OCR) não encontrado — faturas-imagem sem camada de texto não poderão ser processadas.");
            }
            if (!status.pdftotext_resolvido) {
                avisos.push("pdftotext não encontrado — extração de texto de PDFs nativos pode falhar.");
            }
            if (status.distribuidoras_nao_implementadas && status.distribuidoras_nao_implementadas.length) {
                avisos.push(`Extratores ainda não implementados: ${status.distribuidoras_nao_implementadas.join(", ")}.`);
            }

            if (avisos.length === 0) {
                bannerStatus.hidden = true;
                return;
            }

            bannerStatus.className = "banner";
            bannerStatus.innerHTML = "<strong>Atenção:</strong><ul>" +
                avisos.map((a) => `<li>${a}</li>`).join("") + "</ul>";
            bannerStatus.hidden = false;
        } catch (erro) {
            // Status é só informativo — falha aqui não deve travar o resto da página.
        }
    }

    carregarStatusSistema();

    // ================= TROCA DE ABAS =================
    const abaBotoes = document.querySelectorAll(".aba-botao");
    const abas = document.querySelectorAll(".aba");

    function ativarAba(botao) {
        const alvo = botao.dataset.aba;
        abaBotoes.forEach((b) => {
            const ativo = b === botao;
            b.classList.toggle("ativo", ativo);
            b.setAttribute("aria-selected", String(ativo));
            b.tabIndex = ativo ? 0 : -1;
        });
        abas.forEach((aba) => { aba.hidden = aba.id !== `aba-${alvo}`; });

        // Só busca os dados na primeira vez que a aba é aberta.
        if (alvo === "dados") window.Dados.iniciar();
        if (alvo === "dashboards") window.Dashboard.iniciar();
        if (alvo === "erros") window.Erros.iniciar();
    }

    abaBotoes.forEach((botao, indice) => {
        botao.addEventListener("click", () => ativarAba(botao));
        // Padrão ARIA de abas: setas/Home/End trocam de aba sem precisar de Tab.
        botao.addEventListener("keydown", (evento) => {
            const total = abaBotoes.length;
            const destinos = {
                ArrowRight: (indice + 1) % total,
                ArrowLeft: (indice - 1 + total) % total,
                Home: 0,
                End: total - 1,
            };
            if (!(evento.key in destinos)) return;
            evento.preventDefault();
            const proximo = abaBotoes[destinos[evento.key]];
            proximo.focus();
            ativarAba(proximo);
        });
    });
})();
