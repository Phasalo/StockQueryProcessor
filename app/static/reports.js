'use strict';

let tables = [];
let controller;

function initTables(search = '') {
    if (!document.querySelector('[data-report-table]')) return;
    if (!window.jQuery || !$.fn.DataTable) {
        document.getElementById('status').textContent = 'Не удалось загрузить DataTables. Проверьте доступ к интернету и обновите страницу.';
        document.getElementById('report-search').disabled = true;
        return;
    }
    tables = Array.from(document.querySelectorAll('[data-report-table]'), element => {
        const table = $(element).DataTable({
            order: [], paging: false, info: false, dom: 't',
            language: {emptyTable: element.dataset.empty, zeroRecords: 'Совпадений не найдено'}
        });
        table.search(search).draw();
        return table;
    });
    document.getElementById('report-search').value = search;
}

function busy(value) {
    document.querySelector('main').setAttribute('aria-busy', String(value));
    document.querySelectorAll('select, #report-search').forEach(element => { element.disabled = value; });
    const download = document.getElementById('download');
    const unavailable = value || !download?.hasAttribute('href');
    download?.classList.toggle('disabled', unavailable);
    download?.setAttribute('aria-disabled', String(unavailable));
}

async function loadPage() {
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    const search = document.getElementById('report-search')?.value || '';
    const focusId = document.activeElement.id;
    busy(true);
    document.getElementById('status').textContent = 'Загрузка…';
    try {
        const response = await fetch(location.href, {signal});
        if (!response.ok) throw new Error('Не удалось загрузить данные. Обновите страницу или выберите другие фильтры.');
        const html = await response.text();
        if (signal.aborted) return;
        const next = new DOMParser().parseFromString(html, 'text/html');
        tables.forEach(table => table.destroy());
        tables = [];
        document.querySelector('main').replaceWith(next.querySelector('main'));
        document.title = next.title;
        document.body.dataset.page = next.body.dataset.page;
        busy(false);
        initTables(search);
        document.getElementById(focusId)?.focus();
    } catch (error) {
        if (error.name === 'AbortError') return;
        busy(false);
        document.getElementById('status').textContent = error.message;
        document.getElementById('download')?.classList.add('disabled');
        document.getElementById('download')?.setAttribute('aria-disabled', 'true');
    }
}

document.addEventListener('change', event => {
    if (!event.target.matches('.report-filters select')) return;
    const next = new URL(location.href);
    if (event.target.id === 'film-brand') {
        next.searchParams.set('brand', event.target.value);
        history.pushState(null, '', next.pathname + next.search);
        loadPage();
        return;
    }
    const parts = location.pathname.split('/');
    const index = {'film-type': 2, 'film-format': 3, 'film-thickness': 4}[event.target.id];
    parts[index] = encodeURIComponent(event.target.value);
    next.searchParams.delete('brand');
    history.pushState(null, '', parts.join('/') + next.search);
    loadPage();
});

document.addEventListener('input', event => {
    if (event.target.id === 'report-search') {
        tables.forEach(table => table.search(event.target.value).draw());
    }
});

function openRow(event) {
    if (event.target.closest('#download.disabled')) {
        event.preventDefault();
        return;
    }
    if (event.type === 'keydown' && event.key !== 'Enter') return;
    const row = event.target.closest('tr[data-href]');
    if (row) {
        event.preventDefault();
        location.href = row.dataset.href;
    }
}

document.addEventListener('click', openRow);
document.addEventListener('keydown', openRow);
window.addEventListener('popstate', loadPage);
initTables();
