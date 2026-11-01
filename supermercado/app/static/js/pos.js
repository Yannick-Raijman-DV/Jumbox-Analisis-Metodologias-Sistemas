// Lógica del Punto de Venta (POS) split-screen 60/40
(() => {
    const carrito = new Map(); // id -> {id, nombre, sku, precio, maxStock, cantidad}
    const itemsContainer = document.getElementById("pos-ticket-items");
    const vacioPlaceholder = document.getElementById("pos-ticket-empty");
    const subtotalEl = document.getElementById("pos-subtotal");
    const totalEl = document.getElementById("pos-total");
    const puntosEl = document.getElementById("pos-puntos");
    const btnCobrar = document.getElementById("pos-btn-cobrar");
    const btnVaciar = document.getElementById("pos-btn-vaciar");
    const inputBuscar = document.getElementById("pos-buscar");
    const selectCliente = document.getElementById("pos-cliente");
    const selectPago = document.getElementById("pos-metodo-pago");
    const chkEnvio = document.getElementById("pos-con-envio");
    const panelEnvio = document.getElementById("pos-panel-envio");
    const inputDireccion = document.getElementById("pos-envio-direccion");
    const inputTelefono = document.getElementById("pos-envio-telefono");
    const inputObs = document.getElementById("pos-envio-obs");
    const alertaBox = document.getElementById("pos-alerta");

    if (!itemsContainer) return;

    function mostrarAlerta(msg, tipo = "error") {
        alertaBox.textContent = msg;
        alertaBox.className =
            "mb-3 px-4 py-2.5 rounded-xl text-body-sm font-medium " +
            (tipo === "error"
                ? "bg-error-container text-on-error-container border border-error/30"
                : "bg-secondary-container text-on-secondary-container");
        alertaBox.classList.remove("hidden");
        setTimeout(() => alertaBox.classList.add("hidden"), 5000);
    }

    function formatear(n) {
        return "$" + Number(n).toFixed(2);
    }

    function renderizar() {
        itemsContainer.innerHTML = "";
        let total = 0;

        if (carrito.size === 0) {
            vacioPlaceholder.classList.remove("hidden");
            btnCobrar.disabled = true;
        } else {
            vacioPlaceholder.classList.add("hidden");
            btnCobrar.disabled = false;
        }

        carrito.forEach((item) => {
            const sub = item.precio * item.cantidad;
            total += sub;

            const fila = document.createElement("div");
            fila.className = "flex items-center justify-between gap-2 p-3 bg-surface-container-low rounded-xl border border-outline-variant/60";
            fila.innerHTML = `
                <div class="min-w-0 flex-1">
                    <p class="font-semibold text-on-surface truncate">${item.nombre}</p>
                    <p class="font-label-sm text-xs text-on-surface-variant">${item.sku} • ${formatear(item.precio)} c/u</p>
                </div>
                <div class="flex items-center gap-1.5">
                    <button type="button" data-accion="restar" data-id="${item.id}" class="w-7 h-7 rounded-lg bg-white border border-outline-variant hover:bg-surface-container-high font-bold">-</button>
                    <span class="w-8 text-center font-label-md font-bold">${item.cantidad}</span>
                    <button type="button" data-accion="sumar" data-id="${item.id}" class="w-7 h-7 rounded-lg bg-white border border-outline-variant hover:bg-surface-container-high font-bold">+</button>
                </div>
                <div class="w-20 text-right font-label-md font-bold text-primary">${formatear(sub)}</div>
            `;
            itemsContainer.appendChild(fila);
        });

        subtotalEl.textContent = formatear(total);
        totalEl.textContent = formatear(total);
        puntosEl.textContent = "+" + Math.floor(total / 100) + " pts";
    }

    function agregarProducto(card) {
        const id = Number(card.dataset.id);
        const maxStock = Number(card.dataset.stock);
        const vencido = card.dataset.vencido === "1";

        if (vencido) {
            mostrarAlerta("Este producto está vencido y bloqueado para la venta.");
            return;
        }
        if (maxStock <= 0) {
            mostrarAlerta("Sin stock en góndola. Trasladá unidades desde el depósito.");
            return;
        }

        const existente = carrito.get(id);
        if (existente) {
            if (existente.cantidad + 1 > maxStock) {
                mostrarAlerta(`Stock máximo disponible en góndola: ${maxStock} u.`);
                return;
            }
            existente.cantidad += 1;
        } else {
            carrito.set(id, {
                id,
                nombre: card.dataset.nombre,
                sku: card.dataset.sku,
                precio: Number(card.dataset.precio),
                maxStock,
                cantidad: 1,
            });
        }
        renderizar();
    }

    document.querySelectorAll(".pos-producto-card").forEach((card) => {
        card.addEventListener("click", () => agregarProducto(card));
    });

    itemsContainer.addEventListener("click", (ev) => {
        const btn = ev.target.closest("button[data-accion]");
        if (!btn) return;
        const id = Number(btn.dataset.id);
        const item = carrito.get(id);
        if (!item) return;

        if (btn.dataset.accion === "sumar") {
            if (item.cantidad + 1 > item.maxStock) {
                mostrarAlerta(`Stock máximo disponible en góndola: ${item.maxStock} u.`);
                return;
            }
            item.cantidad += 1;
        } else {
            item.cantidad -= 1;
            if (item.cantidad <= 0) carrito.delete(id);
        }
        renderizar();
    });

    if (btnVaciar) {
        btnVaciar.addEventListener("click", () => {
            carrito.clear();
            renderizar();
        });
    }

    // Filtro por texto / código de barras (Enter agrega directo si hay 1 coincidencia)
    function filtrarCatalogo(enterPresionado = false) {
        const q = (inputBuscar.value || "").trim().toLowerCase();
        const catActiva = document.querySelector(".pos-cat-btn.activo")?.dataset.cat || "todas";
        const visibles = [];

        document.querySelectorAll(".pos-producto-card").forEach((card) => {
            const coincideTexto =
                !q ||
                card.dataset.nombre.toLowerCase().includes(q) ||
                card.dataset.sku.toLowerCase().includes(q);
            const coincideCat = catActiva === "todas" || card.dataset.categoria === catActiva;
            const mostrar = coincideTexto && coincideCat;
            card.classList.toggle("hidden", !mostrar);
            if (mostrar) visibles.push(card);
        });

        if (enterPresionado && visibles.length === 1) {
            agregarProducto(visibles[0]);
            inputBuscar.value = "";
            filtrarCatalogo(false);
        }
    }

    if (inputBuscar) {
        inputBuscar.addEventListener("input", () => filtrarCatalogo(false));
        inputBuscar.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter") {
                ev.preventDefault();
                filtrarCatalogo(true);
            }
        });
    }

    document.querySelectorAll(".pos-cat-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".pos-cat-btn").forEach((b) => {
                b.classList.remove("activo", "bg-primary", "text-white");
                b.classList.add("bg-surface-container-low", "text-on-surface");
            });
            btn.classList.add("activo", "bg-primary", "text-white");
            btn.classList.remove("bg-surface-container-low", "text-on-surface");
            filtrarCatalogo(false);
        });
    });

    // Autocompletar dirección al elegir cliente y alternar panel de envío
    if (chkEnvio) {
        chkEnvio.addEventListener("change", () => {
            panelEnvio.classList.toggle("hidden", !chkEnvio.checked);
        });
    }

    if (selectCliente) {
        selectCliente.addEventListener("change", () => {
            const opt = selectCliente.selectedOptions[0];
            if (opt && inputDireccion && !inputDireccion.value) {
                inputDireccion.value = opt.dataset.direccion || "";
            }
            if (opt && inputTelefono && !inputTelefono.value) {
                inputTelefono.value = opt.dataset.telefono || "";
            }
        });
    }

    // Confirmar cobro
    btnCobrar.addEventListener("click", async () => {
        if (carrito.size === 0) return;
        btnCobrar.disabled = true;

        const payload = {
            items: Array.from(carrito.values()).map((i) => ({
                producto_id: i.id,
                cantidad: i.cantidad,
            })),
            cliente_id: selectCliente.value || null,
            metodo_pago: selectPago.value,
            envio: chkEnvio.checked
                ? {
                      direccion: inputDireccion.value,
                      telefono: inputTelefono.value,
                      observaciones: inputObs.value,
                  }
                : null,
        };

        try {
            const resp = await fetch(btnCobrar.dataset.url, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await resp.json();
            if (!resp.ok || !data.ok) {
                mostrarAlerta(data.error || "No se pudo registrar la venta.");
                btnCobrar.disabled = false;
                return;
            }
            window.location.href = data.ticket_url;
        } catch (_err) {
            mostrarAlerta("Error de red al procesar la venta.");
            btnCobrar.disabled = false;
        }
    });

    renderizar();
})();
