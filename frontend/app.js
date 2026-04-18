document.addEventListener('DOMContentLoaded', () => {
    // Elements
    const printersGrid = document.getElementById('printers-grid');
    const jobsBody = document.getElementById('jobs-body');
    const addPrinterBtn = document.getElementById('add-printer-btn');
    const modal = document.getElementById('add-printer-modal');
    const closeBtn = document.querySelector('.close-btn');
    const addPrinterForm = document.getElementById('add-printer-form');
    const fileUpload = document.getElementById('file-upload');

    // Fetch and render data
    async function fetchData() {
        try {
            const [printersRes, jobsRes] = await Promise.all([
                fetch('/api/printers'),
                fetch('/api/jobs')
            ]);
            
            const printers = await printersRes.json();
            const jobs = await jobsRes.json();
            
            renderPrinters(printers);
            renderJobs(jobs, printers);
        } catch (error) {
            console.error("Failed to fetch data", error);
        }
    }

    function renderPrinters(printers) {
        if (printers.length === 0) {
            printersGrid.innerHTML = '<p style="color: var(--text-secondary)">No printers added yet.</p>';
            return;
        }

        printersGrid.innerHTML = printers.map(p => {
            const jobStr = p.current_job ? `<div class="detail-row"><span>Job:</span> <span>${p.current_job.filename}</span></div>` : '';
            const progressStr = p.current_job ? `
                <div class="detail-row"><span>Progress:</span> <span>${p.current_job.progress}%</span></div>
                <div class="progress-container"><div class="progress-bar" style="width: ${p.current_job.progress}%"></div></div>
            ` : '';

            return `
                <div class="printer-card glass-panel">
                    <div class="printer-header">
                        <div class="printer-name">${p.name}</div>
                        <div class="status-badge status-${p.status}">${p.status.replace('_', ' ')}</div>
                    </div>
                    <div class="printer-details">
                        <div class="detail-row"><span>IP:</span> <span>${p.ip}</span></div>
                        ${jobStr}
                        ${progressStr}
                    </div>
                </div>
            `;
        }).join('');
    }

    function renderJobs(jobs, printers) {
        if (jobs.length === 0) {
            jobsBody.innerHTML = '<tr><td colspan="5" style="text-align:center; color: var(--text-secondary)">No jobs in queue.</td></tr>';
            return;
        }

        const printerMap = {};
        printers.forEach(p => printerMap[p.id] = p.name);

        jobsBody.innerHTML = jobs.map(j => `
            <tr>
                <td>#${j.id}</td>
                <td>${j.filename}</td>
                <td>${j.printer_id ? printerMap[j.printer_id] : '-'}</td>
                <td>
                    ${j.status === 'PRINTING' ? 
                        `<div class="progress-container" style="width: 100px; margin: 0;"><div class="progress-bar" style="width: ${j.progress}%"></div></div>` 
                        : (j.status === 'COMPLETED' ? '100%' : '0%')}
                </td>
                <td><span class="status-badge status-${j.status}">${j.status}</span></td>
            </tr>
        `).join('');
    }

    // Modal Logic
    addPrinterBtn.onclick = () => modal.classList.add('show');
    closeBtn.onclick = () => modal.classList.remove('show');
    window.onclick = (e) => {
        if (e.target == modal) modal.classList.remove('show');
    }

    // Add Printer Submit
    addPrinterForm.onsubmit = async (e) => {
        e.preventDefault();
        const name = document.getElementById('p-name').value;
        const ip = document.getElementById('p-ip').value;
        const serial = document.getElementById('p-serial').value;
        const code = document.getElementById('p-code').value;

        try {
            await fetch(`/api/printers?name=${encodeURIComponent(name)}&ip=${encodeURIComponent(ip)}&serial=${encodeURIComponent(serial)}&access_code=${encodeURIComponent(code)}`, {
                method: 'POST'
            });
            modal.classList.remove('show');
            addPrinterForm.reset();
            fetchData();
        } catch (error) {
            console.error("Error adding printer", error);
        }
    };

    // File Upload
    fileUpload.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;

        const formData = new FormData();
        formData.append('file', file);

        try {
            const btn = document.querySelector('.upload-btn');
            const originalText = btn.innerHTML;
            btn.innerHTML = 'Uploading...';
            
            await fetch('/api/upload', {
                method: 'POST',
                body: formData
            });
            
            btn.innerHTML = originalText;
            fileUpload.value = '';
            fetchData();
        } catch (error) {
            console.error("Error uploading file", error);
            alert("Upload failed!");
        }
    };

    // Initial fetch and poll
    fetchData();
    setInterval(fetchData, 3000); // Poll every 3 seconds
});
