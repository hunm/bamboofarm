document.addEventListener('DOMContentLoaded', () => {
    // ----------------------------------------------------------------
    // Element refs
    // ----------------------------------------------------------------
    const printersGrid = document.getElementById('printers-grid');
    const jobsBody = document.getElementById('jobs-body');
    const addPrinterBtn = document.getElementById('add-printer-btn');
    const printerModal = document.getElementById('add-printer-modal');
    const closePrinterModal = document.getElementById('close-printer-modal');
    const addPrinterForm = document.getElementById('add-printer-form');
    const fileUpload = document.getElementById('file-upload');
    const themeToggleBtn = document.getElementById('theme-toggle-btn');

    // Camera modal refs
    const cameraModal = document.getElementById('camera-modal');
    const closeCameraModal = document.getElementById('close-camera-modal');
    const cameraStream = document.getElementById('camera-stream');
    const cameraError = document.getElementById('camera-error');
    const cameraModalTitle = document.getElementById('camera-modal-title');
    const cameraPrinterSerial = document.getElementById('camera-printer-serial');
    const snapshotBtn = document.getElementById('snapshot-btn');
    const cameraRefreshBtn = document.getElementById('camera-refresh-btn');

    // Info modal refs
    const infoModal = document.getElementById('info-modal');
    const closeInfoModal = document.getElementById('close-info-modal');
    const infoModalTitle = document.getElementById('info-modal-title');
    const infoModalModel = document.getElementById('info-modal-model');
    const infoModalBody = document.getElementById('info-modal-body');

    let activePrinterId = null;
    let activePrinterName = '';
    let activePrinterSerial = '';

    // ----------------------------------------------------------------
    // Theme Toggle
    // ----------------------------------------------------------------
    const savedTheme = localStorage.getItem('bambu-theme') || 'dark';
    if (savedTheme === 'light') {
        document.documentElement.setAttribute('data-theme', 'light');
    }

    themeToggleBtn.addEventListener('click', () => {
        const isLight = document.documentElement.getAttribute('data-theme') === 'light';
        if (isLight) {
            document.documentElement.removeAttribute('data-theme');
            localStorage.setItem('bambu-theme', 'dark');
        } else {
            document.documentElement.setAttribute('data-theme', 'light');
            localStorage.setItem('bambu-theme', 'light');
        }
    });


    // ----------------------------------------------------------------
    // Data fetch & render
    // ----------------------------------------------------------------
    async function fetchData() {
        try {
            const [printersRes, jobsRes] = await Promise.all([
                fetch('/api/printers'),
                fetch('/api/jobs'),
            ]);
            const printers = await printersRes.json();
            const jobs = await jobsRes.json();

            renderPrinters(printers);
            renderJobs(jobs, printers);
        } catch (error) {
            console.error('Failed to fetch data', error);
        }
    }

    // ----------------------------------------------------------------
    // Helpers
    // ----------------------------------------------------------------
    function formatRemainingTime(minutes) {
        if (minutes == null || minutes <= 0) return null;
        const h = Math.floor(minutes / 60);
        const m = minutes % 60;
        if (h > 0) return `${h}ч ${m}мин`;
        return `${m}мин`;
    }

    function computeETA(minutes) {
        if (minutes == null || minutes <= 0) return null;
        const eta = new Date(Date.now() + minutes * 60_000);
        return eta.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function tempClass(value, target) {
        if (value == null) return '';
        if (target != null && target > 0 && value >= target * 0.8) return 'hot';
        if (value > 50) return 'hot';
        return '';
    }

    function getPrintStageName(stageNum) {
        const stages = {
            1: "Выравнивание стола",
            2: "Нагрев стола",
            3: "Проверка осей",
            4: "Смена филамента",
            5: "Пауза (M400)",
            6: "Нет филамента",
            7: "Нагрев сопла",
            8: "Калибровка экструзии",
            9: "Сканирование поверхности",
            10: "Проверка 1-го слоя",
            11: "Определение типа стола",
            12: "Калибровка Lidar",
            13: "Парковка (Homing)",
            14: "Очистка сопла",
            16: "Пауза пользователя",
            22: "Выгрузка филамента",
            24: "Загрузка филамента",
            25: "Калибровка моторов",
            26: "Пауза: ошибка AMS",
            29: "Охлаждение камеры"
        };
        return stages[stageNum] || (`Подготовка (Этап ${stageNum})`);
    }

    function renderPrinters(printers) {
        if (printers.length === 0) {
            printersGrid.innerHTML = '<p style="color: var(--text-secondary)">No printers added yet.</p>';
            return;
        }

        printersGrid.innerHTML = printers.map(p => {
            const jobStr = p.current_job
                ? `<div class="detail-row"><span>Job:</span> <span>${p.current_job.filename}</span></div>`
                : '';
            const progressStr = p.current_job ? `
                <div class="detail-row"><span>Progress:</span> <span>${p.current_job.progress}%</span></div>
                <div class="progress-container"><div class="progress-bar" style="width: ${p.current_job.progress}%"></div></div>
            ` : '';

            // Build live print info panel
            let printInfoHtml = '';
            const info = p.print_info;
            if (info && info.last_update) {
                const remaining = formatRemainingTime(info.remaining_time_min);
                const eta = computeETA(info.remaining_time_min);
                const gcodeState = info.gcode_state || '';
                const isPrinting = gcodeState === 'RUNNING' || gcodeState === 'PAUSE' || gcodeState === 'PREPARE';

                let timeBlock = '';
                if (remaining && isPrinting) {
                    timeBlock = `
                        <div class="time-remaining">
                            <span class="time-icon">⏱</span>
                            <span class="time-value">${remaining}</span>
                            <span class="time-label">осталось</span>
                        </div>
                    `;
                    if (eta) {
                        timeBlock += `
                            <div class="eta-line">
                                <span>Завершение ≈</span>
                                <span class="eta-value">${eta}</span>
                            </div>
                        `;
                    }
                }

                // State badge (only when not idle)
                let stateBadge = '';
                if (gcodeState && gcodeState !== 'IDLE') {
                    const stateLabels = { RUNNING: '▶ Печать', PAUSE: '⏸ Пауза', FINISH: '✓ Готово', FAILED: '✕ Ошибка', PREPARE: '⚙ Подготовка' };
                    const label = stateLabels[gcodeState] || gcodeState;
                    stateBadge = `<span class="print-state-badge state-${gcodeState}">${label}</span>`;
                }

                // Detailed Preparation Stage
                let stageHtml = '';
                if (info.stg_cur && info.stg_cur > 0 && gcodeState !== 'IDLE' && gcodeState !== 'FINISH') {
                    stageHtml = `<div class="stage-info">ℹ ${getPrintStageName(info.stg_cur)}</div>`;
                }

                // Temperatures
                const nozzle = info.nozzle_temper;
                const nozzleTarget = info.nozzle_target_temper;
                const bed = info.bed_temper;
                const bedTarget = info.bed_target_temper;
                const chamber = info.chamber_temper;

                let tempHtml = '';
                if (nozzle != null || bed != null) {
                    tempHtml = `<div class="temp-row">`;
                    if (nozzle != null) {
                        const nzTgt = nozzleTarget != null ? `/${Math.round(nozzleTarget)}` : '';
                        tempHtml += `
                            <div class="temp-item">
                                <span class="temp-label">Сопло</span>
                                <span class="temp-value ${tempClass(nozzle, nozzleTarget)}">${Math.round(nozzle)}${nzTgt}°</span>
                            </div>`;
                    }
                    if (bed != null) {
                        const bdTgt = bedTarget != null ? `/${Math.round(bedTarget)}` : '';
                        tempHtml += `
                            <div class="temp-item">
                                <span class="temp-label">Стол</span>
                                <span class="temp-value ${tempClass(bed, bedTarget)}">${Math.round(bed)}${bdTgt}°</span>
                            </div>`;
                    }
                    if (chamber != null) {
                        tempHtml += `
                            <div class="temp-item">
                                <span class="temp-label">Камера</span>
                                <span class="temp-value">${Math.round(chamber)}°</span>
                            </div>`;
                    }
                    tempHtml += `</div>`;
                }

                // Layers
                let layerHtml = '';
                if (info.layer_num != null && info.total_layer_num != null && info.total_layer_num > 0) {
                    layerHtml = `
                        <div class="layer-info">
                            <span>Слой</span>
                            <span class="layer-count">${info.layer_num} / ${info.total_layer_num}</span>
                        </div>`;
                }

                printInfoHtml = `
                    <div class="print-info">
                        ${stateBadge}
                        ${stageHtml}
                        ${timeBlock}
                        ${tempHtml}
                        ${layerHtml}
                    </div>
                `;
            }

            const isOnline = p.status !== 'OFFLINE';

            return `
                <div class="printer-card glass-panel">
                    <div class="printer-header">
                        <div class="printer-name">${p.name}</div>
                        <div class="status-badge status-${p.status}">${p.status.replace('_', ' ')}</div>
                    </div>
                    <div class="printer-details">
                        <div class="detail-row"><span>IP:</span> <span>${p.ip}</span></div>
                        <div class="detail-row"><span>Serial:</span> <span class="serial-text">${p.serial}</span></div>
                        ${jobStr}
                        ${progressStr}
                    </div>
                    ${printInfoHtml}
                    <div class="printer-card-actions">
                        <button
                            class="btn camera-btn ${isOnline ? '' : 'disabled'}"
                            data-id="${p.id}"
                            data-name="${p.name}"
                            data-serial="${p.serial}"
                            ${isOnline ? '' : 'disabled'}
                            title="${isOnline ? 'View camera' : 'Printer offline'}"
                        >
                            📷 Camera
                        </button>
                        <button
                            class="btn info-btn"
                            data-id="${p.id}"
                            data-name="${p.name}"
                            title="Printer details"
                        >
                            ℹ️ Info
                        </button>
                        <button
                            class="btn ready-btn"
                            data-id="${p.id}"
                            title="Set status to READY"
                            ${p.status === 'WAITING_CLEAN' ? '' : 'style="display:none;"'}
                        >
                            ✔ Ready
                        </button>
                    </div>
                </div>
            `;
        }).join('');

        // Attach camera button listeners
        document.querySelectorAll('.camera-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                openCameraModal(
                    parseInt(btn.dataset.id),
                    btn.dataset.name,
                    btn.dataset.serial,
                );
            });
        });

        // Attach info button listeners
        document.querySelectorAll('.info-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                openInfoModal(parseInt(btn.dataset.id), btn.dataset.name);
            });
        });

        // Attach ready button listeners
        document.querySelectorAll('.ready-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                try {
                    await fetch(`/api/printers/${btn.dataset.id}/ready`, { method: 'POST' });
                    fetchData();
                } catch (error) {
                    console.error('Error setting printer ready', error);
                }
            });
        });
    }

    function renderJobs(jobs, printers) {
        if (jobs.length === 0) {
            jobsBody.innerHTML = '<tr><td colspan="6" style="text-align:center; color: var(--text-secondary)">No jobs in queue.</td></tr>';
            return;
        }

        const printerMap = {};
        printers.forEach(p => printerMap[p.id] = p.name);

        jobsBody.innerHTML = jobs.map(j => {
            const remaining = formatRemainingTime(j.remaining_time_min);
            const eta = computeETA(j.remaining_time_min);
            const timeCell = j.status === 'PRINTING' && remaining
                ? `<span class="time-value" style="color: #60a5fa; font-weight: 600;">${remaining}</span>${eta ? `<br><small style="color: var(--text-secondary)">≈ ${eta}</small>` : ''}`
                : '-';

            const progressCell = j.status === 'PRINTING'
                ? `<span style="font-weight:600; font-variant-numeric:tabular-nums;">${j.progress}%</span>`
                : j.status === 'COMPLETED'
                    ? '<span style="color: var(--success); font-weight:600;">100%</span>'
                    : j.status === 'UPLOADING'
                        ? '<span style="color: #06b6d4; font-weight:500; font-size:0.85rem;">Загрузка на принтер…</span>'
                        : '-';

            return `
            <tr>
                <td>#${j.id}</td>
                <td>${j.filename}</td>
                <td>${j.printer_id ? printerMap[j.printer_id] : '-'}</td>
                <td>${progressCell}</td>
                <td>${timeCell}</td>
                <td><span class="status-badge status-${j.status}">${j.status}</span></td>
            </tr>
        `}).join('');
    }

    // ----------------------------------------------------------------
    // Add Printer Modal
    // ----------------------------------------------------------------
    addPrinterBtn.onclick = () => printerModal.classList.add('show');
    closePrinterModal.onclick = () => printerModal.classList.remove('show');
    window.addEventListener('click', e => {
        if (e.target === printerModal) printerModal.classList.remove('show');
        if (e.target === cameraModal) closeCameraStream();
        if (e.target === infoModal) infoModal.classList.remove('show');
    });

    addPrinterForm.onsubmit = async (e) => {
        e.preventDefault();
        const name = document.getElementById('p-name').value;
        const ip = document.getElementById('p-ip').value;
        const serial = document.getElementById('p-serial').value;
        const code = document.getElementById('p-code').value;

        try {
            await fetch(
                `/api/printers?name=${encodeURIComponent(name)}&ip=${encodeURIComponent(ip)}&serial=${encodeURIComponent(serial)}&access_code=${encodeURIComponent(code)}`,
                { method: 'POST' }
            );
            printerModal.classList.remove('show');
            addPrinterForm.reset();
            fetchData();
        } catch (error) {
            console.error('Error adding printer', error);
        }
    };

    // ----------------------------------------------------------------
    // File Upload
    // ----------------------------------------------------------------
    fileUpload.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;

        const formData = new FormData();
        formData.append('file', file);

        try {
            const btn = document.querySelector('.upload-btn');
            btn.innerHTML = 'Uploading…';
            await fetch('/api/upload', { method: 'POST', body: formData });
            btn.innerHTML = '↑ Upload Print File';
            fileUpload.value = '';
            fetchData();
        } catch (error) {
            console.error('Error uploading file', error);
            alert('Upload failed!');
        }
    };

    // ----------------------------------------------------------------
    // Camera Modal
    // ----------------------------------------------------------------
    function openCameraModal(printerId, printerName, printerSerial) {
        activePrinterId = printerId;
        activePrinterName = printerName;
        activePrinterSerial = printerSerial;

        cameraModalTitle.textContent = `${printerName} — Camera`;
        cameraPrinterSerial.textContent = printerSerial;

        startCameraStream();
        cameraModal.classList.add('show');
    }

    function startCameraStream() {
        cameraError.style.display = 'none';
        cameraStream.style.display = 'block';

        // Setting src triggers the browser to open the MJPEG connection
        cameraStream.src = `/api/printers/${activePrinterId}/camera?_ts=${Date.now()}`;

        cameraStream.onerror = () => {
            cameraStream.style.display = 'none';
            cameraError.style.display = 'flex';
        };
    }

    function closeCameraStream() {
        // Clear src to terminate the MJPEG connection from browser side
        cameraStream.src = '';
        cameraStream.onerror = null;
        cameraError.style.display = 'none';
        cameraStream.style.display = 'block';
        cameraModal.classList.remove('show');
        activePrinterId = null;
    }

    closeCameraModal.onclick = closeCameraStream;

    cameraRefreshBtn.onclick = () => {
        if (activePrinterId !== null) startCameraStream();
    };

    snapshotBtn.onclick = async () => {
        if (activePrinterId === null) return;
        try {
            const response = await fetch(`/api/printers/${activePrinterId}/snapshot`);
            if (!response.ok) throw new Error('Snapshot failed');
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${activePrinterName}_snapshot_${Date.now()}.jpg`;
            a.click();
            URL.revokeObjectURL(url);
        } catch (err) {
            console.error('Snapshot error:', err);
            alert('Could not capture snapshot. Is the printer online?');
        }
    };

    // ----------------------------------------------------------------
    // Printer Info Modal
    // ----------------------------------------------------------------
    closeInfoModal.onclick = () => infoModal.classList.remove('show');

    async function openInfoModal(printerId, printerName) {
        infoModalTitle.textContent = `${printerName} — Info`;
        infoModalModel.textContent = 'Загрузка…';
        infoModalBody.innerHTML = '<p style="color: var(--text-secondary); text-align:center;">Загрузка данных…</p>';
        infoModal.classList.add('show');

        try {
            const res = await fetch(`/api/printers/${printerId}/details`);
            if (!res.ok) throw new Error('Failed to fetch details');
            const data = await res.json();
            renderInfoBody(data);
        } catch (err) {
            console.error('Info modal error:', err);
            infoModalBody.innerHTML = '<p style="color: var(--danger); text-align:center;">Не удалось загрузить данные</p>';
        }
    }

    function renderInfoBody(data) {
        const hw = data.hw_info || {};
        const pi = data.print_info || {};

        const model = hw.model || data.model || 'Unknown';
        infoModalModel.textContent = model;

        function card(label, value, cls = '') {
            if (value == null || value === '') return '';
            return `<div class="info-card"><span class="info-label">${label}</span><span class="info-value ${cls}">${value}</span></div>`;
        }

        // ─── General section ───
        let generalHtml = `<span class="info-section-label">Основное</span><div class="info-grid">`;
        generalHtml += card('Модель', model, 'accent');
        generalHtml += card('Серийный номер', data.serial, 'small');
        generalHtml += card('IP адрес', data.ip);
        generalHtml += card('Статус', data.status);
        generalHtml += card('Прошивка', hw.firmware_version || data.firmware_version);
        generalHtml += card('WiFi', hw.wifi_signal);
        generalHtml += card('SD карта', hw.sdcard != null ? (hw.sdcard ? 'Установлена' : 'Отсутствует') : null);
        generalHtml += card('Камера', hw.has_camera ? 'Есть' : 'Нет');
        generalHtml += card('Lifecycle', hw.lifecycle);
        generalHtml += `</div>`;

        // ─── Temperatures section ───
        let tempHtml = '';
        if (pi.nozzle_temper != null || pi.bed_temper != null) {
            tempHtml = `<span class="info-section-label">Температуры</span><div class="info-grid">`;
            if (pi.nozzle_temper != null) {
                const tgt = pi.nozzle_target_temper != null ? ` / ${Math.round(pi.nozzle_target_temper)}°` : '';
                tempHtml += card('Сопло', `${Math.round(pi.nozzle_temper)}°${tgt}`);
            }
            if (pi.bed_temper != null) {
                const tgt = pi.bed_target_temper != null ? ` / ${Math.round(pi.bed_target_temper)}°` : '';
                tempHtml += card('Стол', `${Math.round(pi.bed_temper)}°${tgt}`);
            }
            if (pi.chamber_temper != null) {
                tempHtml += card('Камера', `${Math.round(pi.chamber_temper)}°`);
            }
            tempHtml += `</div>`;
        }

        // ─── Fans section ───
        let fanHtml = '';
        if (hw.heatbreak_fan_speed != null || pi.fan_speed != null) {
            fanHtml = `<span class="info-section-label">Вентиляторы</span><div class="info-grid">`;
            fanHtml += card('Охлаждение', pi.fan_speed != null ? pi.fan_speed : null);
            fanHtml += card('Heatbreak', hw.heatbreak_fan_speed);
            fanHtml += card('Aux Fan 1', hw.big_fan1_speed);
            fanHtml += card('Aux Fan 2', hw.big_fan2_speed);
            const speedNames = { 1: 'Тихий', 2: 'Стандарт', 3: 'Спорт', 4: 'Турбо' };
            fanHtml += card('Скорость печати', speedNames[hw.speed_level] || hw.speed_level);
            fanHtml += `</div>`;
        }

        // ─── AMS section ───
        let amsHtml = '';
        if (hw.ams_installed && hw.ams_trays && hw.ams_trays.length > 0) {
            amsHtml = `<span class="info-section-label">AMS — Филаменты</span><div class="ams-tray-list">`;
            for (const tray of hw.ams_trays) {
                const color = tray.color ? `#${tray.color.substring(0, 6)}` : '#555';
                const remain = tray.remain != null ? `${tray.remain}%` : '';
                amsHtml += `
                    <div class="ams-tray-item">
                        <div class="ams-color-swatch" style="background: ${color};"></div>
                        <span class="ams-tray-name">${tray.name || tray.type}</span>
                        <span class="ams-tray-meta">${remain}</span>
                    </div>`;
            }
            amsHtml += `</div>`;
        } else if (hw.ams_installed === false) {
            amsHtml = `<span class="info-section-label">AMS</span><p style="color: var(--text-secondary); font-size:0.85rem;">Не установлен</p>`;
        }

        // ─── Lights ───
        let lightsHtml = '';
        if (hw.lights && hw.lights.length > 0) {
            lightsHtml = `<span class="info-section-label">Подсветка</span><div class="info-grid">`;
            for (const l of hw.lights) {
                lightsHtml += card(l.node || 'LED', l.mode || '—');
            }
            lightsHtml += `</div>`;
        }

        infoModalBody.innerHTML = generalHtml + tempHtml + fanHtml + amsHtml + lightsHtml;
    }

    // ----------------------------------------------------------------
    // Initial load & polling
    // ----------------------------------------------------------------
    fetchData();
    setInterval(fetchData, 5000);
});
