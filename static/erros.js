/* Aba "Erros" — form pra reportar um erro numa fatura já extraída, e lista
 * dos erros já reportados (persistidos em erros_reportados.db via API). */
(() => {
    "use strict";

    const campoConcessionaria = document.getElementById("erro-concessionaria");
    const campoNumFatura = document.getElementById("erro-num-fatura");
    const campoContaDv = document.getElementById("erro-conta-dv");
    const campoMesAno = document.getElementById("erro-mes-ano");
    const campoMensagem = document.getElementById("erro-mensagem");
    const botaoEnviar = document.getElementById("botao-enviar-erro");
    const formStatus = document.getElementById("erro-form-status");

    const selectStatus = document.getElementById("select-status-erro");
    const tbody = document.getElementById("tbody-erros");
    const textoVazio = document.getElementById("texto-vazio-erros");

    let iniciado = false;

    // Lista fechada em vez de texto livre: "SAAE Corumbá", "corumba" e
    // "SAAE_CORUMBA" viravam três distribuidoras diferentes no banco.
    async function carregarDistribuidoras() {
        const resposta = await fetch("/dados/empresas");
        if (!resposta.ok) return;
        const { empresas } = await resposta.json();
        for (const info of empresas) garantirOpcao(info.empresa);
    }

    function garantirOpcao(valor) {
        if (!valor || [...campoConcessionaria.options].some((o) => o.value === valor)) return;
        const opcao = document.createElement("option");
        opcao.value = valor;
        opcao.textContent = valor;
        campoConcessionaria.appendChild(opcao);
    }

    function mostrarStatusForm(texto, ehErro) {
        formStatus.textContent = texto;
        formStatus.hidden = false;
        formStatus.className = ehErro ? "texto-progresso erro-form-erro" : "texto-progresso erro-form-ok";
    }

    async function enviarErro() {
        const mensagem = campoMensagem.value.trim();
        if (!mensagem) {
            mostrarStatusForm("Descreva o que está errado antes de enviar.", true);
            return;
        }

        botaoEnviar.disabled = true;
        try {
            const resposta = await fetch("/erros", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    mensagem,
                    concessionaria: campoConcessionaria.value.trim(),
                    num_fatura: campoNumFatura.value.trim(),
                    conta_dv: campoContaDv.value.trim(),
                    mes_ano_ref: campoMesAno.value.trim(),
                }),
            });
            if (!resposta.ok) {
                const detalhe = await resposta.json().catch(() => ({}));
                mostrarStatusForm("Erro ao enviar: " + (detalhe.detail || resposta.statusText), true);
                return;
            }
            campoConcessionaria.value = "";
            campoNumFatura.value = "";
            campoContaDv.value = "";
            campoMesAno.value = "";
            campoMensagem.value = "";
            mostrarStatusForm("Reporte enviado. Obrigado!", false);
            if (selectStatus.value === "aberto" || selectStatus.value === "") {
                carregarErros();
            }
        } catch (erro) {
            mostrarStatusForm("Falha ao enviar: " + erro, true);
        } finally {
            botaoEnviar.disabled = false;
        }
    }

    botaoEnviar.addEventListener("click", enviarErro);

    function celula(texto) {
        const td = document.createElement("td");
        td.textContent = texto == null || texto === "" ? "—" : texto;
        return td;
    }

    async function carregarErros() {
        const parametros = selectStatus.value ? `?status=${encodeURIComponent(selectStatus.value)}` : "";
        const resposta = await fetch(`/erros${parametros}`);
        if (!resposta.ok) return;
        const { erros } = await resposta.json();
        renderizarTabela(erros);
    }

    function renderizarTabela(erros) {
        tbody.innerHTML = "";
        textoVazio.hidden = erros.length > 0;

        for (const erro of erros) {
            const tr = document.createElement("tr");
            tr.appendChild(celula(erro.data_criacao));
            tr.appendChild(celula(erro.concessionaria));
            tr.appendChild(celula(erro.num_fatura));
            tr.appendChild(celula(erro.conta_dv));
            tr.appendChild(celula(erro.mes_ano_ref));
            tr.appendChild(celula(erro.mensagem));

            const tdStatus = document.createElement("td");
            const badge = document.createElement("span");
            badge.className = erro.status === "resolvido" ? "badge-resolvido" : "badge-suspeita";
            badge.textContent = erro.status === "resolvido" ? "resolvido" : "aberto";
            tdStatus.appendChild(badge);
            tr.appendChild(tdStatus);

            const tdAcao = document.createElement("td");
            const botaoToggle = document.createElement("button");
            botaoToggle.className = "botao secundario";
            botaoToggle.type = "button";
            botaoToggle.textContent = erro.status === "resolvido" ? "Reabrir" : "Marcar resolvido";
            botaoToggle.setAttribute(
                "aria-label",
                `${botaoToggle.textContent}: reporte de ${erro.data_criacao}${erro.num_fatura ? `, fatura ${erro.num_fatura}` : ""}`
            );
            botaoToggle.addEventListener("click", () => alternarStatus(erro.id, erro.status));
            tdAcao.appendChild(botaoToggle);
            tr.appendChild(tdAcao);

            tbody.appendChild(tr);
        }
    }

    async function alternarStatus(id, statusAtual) {
        const novoStatus = statusAtual === "resolvido" ? "aberto" : "resolvido";
        const resposta = await fetch(`/erros/${id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: novoStatus }),
        });
        if (resposta.ok) carregarErros();
    }

    selectStatus.addEventListener("change", carregarErros);

    window.Erros = {
        iniciar() {
            if (iniciado) return;
            iniciado = true;
            carregarDistribuidoras();
            carregarErros();
        },
        // Chamado pela aba "Dados" (botão "Reportar erro" de cada linha) pra
        // pré-preencher o form com a fatura já identificada.
        preencher({ concessionaria, numFatura, contaDv, mesAno }) {
            garantirOpcao(concessionaria);
            campoConcessionaria.value = concessionaria || "";
            campoNumFatura.value = numFatura || "";
            campoContaDv.value = contaDv || "";
            campoMesAno.value = mesAno || "";
            campoMensagem.value = "";
            campoMensagem.focus();
        },
    };
})();
