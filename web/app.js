const byId = id => document.getElementById(id);
const companyMap = new Map();
let activeRun = null;
let pollTimer = null;
let transcriptProviderConfigured = false;

async function api(url, options={}) {
  const response = await fetch(url, options);
  const type = response.headers.get('content-type') || '';
  const body = type.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) throw new Error(body?.error || body || `Request failed (${response.status})`);
  return body;
}

function companyLabel(c) { return `${c.company_name} (${c.ticker || (c.cik ? `CIK ${c.cik}` : 'SEC CIK pending')})`; }

async function searchCompanies(input, list) {
  const q = input.value.trim();
  try {
    const data = await api(`/api/companies?q=${encodeURIComponent(q)}&limit=50`);
    list.replaceChildren();
    data.companies.forEach(c => {
      const label = companyLabel(c);
      companyMap.set(label, c);
      const opt = document.createElement('option'); opt.value = label; list.append(opt);
    });
    return data;
  } catch (err) { console.error(err); return null; }
}

function selectedCompany(input) { return companyMap.get(input.value.trim()) || null; }

async function showCompanyMeta(inputId, metaId) {
  const company = selectedCompany(byId(inputId));
  if (!company) {
    byId(metaId).textContent = 'Choose an exact company name from the search list';
    return null;
  }
  const baseText = `Ticker ${company.ticker || 'unlisted'} · ${company.cik ? `CIK ${company.cik}` : 'SEC CIK not mapped; research is disabled'} · rank ${company.rank ?? 'unknown'} (${company.reporting_year ?? 'year unavailable'})`;
  byId(metaId).textContent = baseText;
  if (company.cik) {
    try {
      const data = await api(`/api/documents?cik=${encodeURIComponent(company.cik)}`);
      if (data.documents && data.documents.length > 0) {
        byId(metaId).textContent = `${baseText} · ✓ ${data.documents.length} cached document(s) ready (<10s comparison)`;
      }
    } catch (_) {}
  }
  return company;
}

async function initialize() {
  const health = await api('/api/health');
  transcriptProviderConfigured = Boolean(health.transcript_provider);
  byId('health-label').textContent = health.sec_configured ? 'SEC access configured' : 'Local mode · SEC setup needed';
  const universe = health.universe;
  byId('universe-title').textContent = `${universe.count} records · ${universe.research_ready_count ?? universe.count} SEC-ready`;
  byId('universe-info').textContent = `${universe.reporting_years.join(', ') || 'No year'} ranking · ${universe.sources[0] || 'source not supplied'}`;
  const notices = [];
  if (!health.sec_configured) notices.push('Set SEC_USER_AGENT in .env with your app name and contact email to enable EDGAR retrieval.');
  if (universe.count < 500) notices.push(`The bundled 2026 ranking seed has ${universe.count} rows, not the full 500. Import your full licensed CSV to search the complete universe.`);
  if (universe.unresolved_count) notices.push(`${universe.unresolved_count} company row(s) have no mapped SEC CIK and cannot run SEC research yet. Run scripts/import_universe.py --resolve-ciks after setting SEC_USER_AGENT; private or delisted companies may need manual mapping.`);
  if (!health.vision_processing && !health.ocr_processing) notices.push('Scanned PDF pages need Tesseract + Poppler, or a vision-capable model enabled in .env. Text-layer PDFs continue to work.');
  if (!health.pdf_support) notices.push('Install pypdf to enable PDF uploads.');
  if (!health.pdf_export) notices.push('Install reportlab to enable PDF memo export.');
  if (!health.transcript_provider) notices.push('No transcript service is configured; transcripts can be uploaded manually.');
  if (!health.embedding_configured) notices.push('Semantic embedding service is not configured; retrieval uses local feature hashing plus lexical matching.');
  if (!health.llm_configured) notices.push('Risk and strategy findings use conservative local keyword rules until an LLM endpoint is configured.');
  else if (health.embedding_index?.stale_chunks) notices.push(`${health.embedding_index.stale_chunks} stored evidence chunk(s) use an older embedding model. Run scripts/reindex_embeddings.py after changing provider configuration.`);
  if (notices.length) {
    byId('setup-notice').textContent = notices.join(' ');
    byId('setup-notice').classList.remove('hidden');
  }
  companyMap.clear();
  await searchCompanies(byId('company-one'), byId('company-options-one'));
  const data = await api('/api/companies?limit=500');
  const select = byId('upload-company');
  select.replaceChildren(new Option('Choose from comparison universe', ''));
  data.companies.forEach(c => {
    const option = document.createElement('option'); option.value = c.cik || ''; option.textContent = companyLabel(c); option.disabled = !c.cik; select.append(option);
  });
  await renderEvaluation();
  await refreshCacheWarmStatus();
}

function rangeYears(from, to) {
  const start = Number(from), end = Number(to);
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1990 || end < start || end - start > 14) return null;
  return Array.from({length: end - start + 1}, (_, i) => start + i);
}

let cacheWarmTimer = null;
function renderCacheWarmStatus(status) {
  const host = byId('cache-progress');
  const button = byId('warm-cache-button');
  const cancelBtn = byId('cancel-warm-cache-button');
  const state = status.state || 'idle';
  const busy = ['queued', 'running', 'cancelling'].includes(state);
  button.disabled = busy;
  if (cancelBtn) {
    cancelBtn.classList.toggle('hidden', !busy);
    cancelBtn.disabled = state === 'cancelling';
    cancelBtn.textContent = state === 'cancelling' ? 'Cancelling…' : 'Cancel preparation';
  }
  if (state === 'idle') { host.textContent = 'No bulk cache preparation has been started.'; return; }
  const done = Number(status.companies_done || 0), total = Number(status.companies_total || 0);
  const years = (status.years || []).map(y => `FY${y}`).join(', ');
  const parts = [`${state.toUpperCase()} · ${done}/${total} companies · ${status.filings_added || 0} new filings · ${status.facts_added || 0} fact sets`, years && `Years: ${years}`, status.company && `Current: ${status.company}`].filter(Boolean);
  if (status.errors?.length) parts.push(`${status.errors.length} company error(s); first: ${status.errors[0].company}: ${status.errors[0].error}`);
  if (status.coverage_gaps?.length) parts.push(`${status.coverage_gaps.length} company/year filing gap(s); first: ${status.coverage_gaps[0].company} FY${(status.coverage_gaps[0].years || []).join(', FY')}`);
  if (status.fatal_error) parts.push(status.fatal_error);
  host.textContent = parts.join(' · ');
  if (busy && cacheWarmTimer === null) cacheWarmTimer = setInterval(refreshCacheWarmStatus, 2500);
  if (!busy && cacheWarmTimer !== null) { clearInterval(cacheWarmTimer); cacheWarmTimer = null; }
}

async function refreshCacheWarmStatus() {
  try { renderCacheWarmStatus(await api('/api/cache/warm')); }
  catch (err) { byId('cache-progress').textContent = `Could not read cache preparation status: ${err.message}`; }
}

byId('cancel-warm-cache-button').addEventListener('click', async () => {
  const cancelBtn = byId('cancel-warm-cache-button');
  cancelBtn.disabled = true;
  cancelBtn.textContent = 'Cancelling…';
  try {
    await api('/api/cache/warm/cancel', {method:'POST'});
    await refreshCacheWarmStatus();
  } catch (err) {
    byId('cache-progress').textContent = `Cancel failed: ${err.message}`;
  }
});

byId('warm-cache-button').addEventListener('click', async () => {
  const years = rangeYears(byId('year-from').value, byId('year-to').value);
  if (!years) { byId('cache-progress').textContent = 'Choose a valid year range first.'; return; }
  if (years.length > 3) { byId('cache-progress').textContent = 'Bulk preparation is limited to three years per job. Choose a smaller range to control time and storage.'; return; }
  const button = byId('warm-cache-button');
  button.disabled = true;
  byId('cache-progress').textContent = 'Starting bulk SEC retrieval…';
  try {
    await api('/api/cache/warm', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({years})});
    await refreshCacheWarmStatus();
  } catch (err) {
    button.disabled = false;
    byId('cache-progress').textContent = err.message;
  }
});

byId('company-one').addEventListener('input', async () => {
  await searchCompanies(byId('company-one'), byId('company-options-one'));
  showCompanyMeta('company-one', 'company-one-meta');
});
byId('company-two').addEventListener('input', async () => {
  await searchCompanies(byId('company-two'), byId('company-options-two'));
  showCompanyMeta('company-two', 'company-two-meta');
});
byId('company-one').addEventListener('change', () => showCompanyMeta('company-one', 'company-one-meta'));
byId('company-two').addEventListener('change', () => showCompanyMeta('company-two', 'company-two-meta'));

const fastDemoToggle = byId('fast-demo');
const fetchSecToggle = byId('fetch-sec');
function syncResearchMode() {
  fetchSecToggle.disabled = fastDemoToggle.checked;
  fetchSecToggle.closest('.toggle-line').classList.toggle('disabled', fastDemoToggle.checked);
}
fastDemoToggle.addEventListener('change', syncResearchMode);
syncResearchMode();

byId('check-filings').addEventListener('click', async () => {
  const host = byId('availability-results');
  const a = await showCompanyMeta('company-one', 'company-one-meta');
  const b = await showCompanyMeta('company-two', 'company-two-meta');
  const years = rangeYears(byId('year-from').value, byId('year-to').value);
  const forms = [...document.querySelectorAll('input[name="doctype"]:checked')].map(el => el.value).filter(x => ['10-K','10-Q'].includes(x));
  if (!a || !b || !years) { host.textContent = 'Choose two companies and a valid fiscal-year range first.'; return; }
  if (!a.cik || !b.cik) { host.textContent = 'A selected company has no mapped SEC CIK. Resolve its CIK or upload documents under a supported company first.'; return; }
  if (!forms.length) { host.textContent = 'No SEC filing type is selected. Transcripts and other documents must be uploaded.'; return; }
  const button = byId('check-filings'); button.disabled = true; host.textContent = 'Checking recent SEC submissions…';
  host.classList.remove('error');
  try {
    const results = await Promise.all([a,b].map(async company => {
      const data = await api(`/api/filings?cik=${encodeURIComponent(company.cik)}&forms=${encodeURIComponent(forms.join(','))}`);
      const filings = data.filings.filter(f => {
        const date = f.period || f.filed || '';
        return years.includes(Number(date.slice(0,4)));
      });
      return {company, filings};
    }));
    host.replaceChildren();
    results.forEach(({company,filings}) => {
      const card = document.createElement('div'); card.className='availability-card';
      const header = document.createElement('strong'); header.textContent = `${company.company_name}: ${filings.length} matching filing(s)`; card.append(header);
      if (!filings.length) { const p=document.createElement('p'); p.textContent='No selected filing found in the SEC submissions currently available. Upload a source document if you have one.'; card.append(p); }
      filings.forEach(f => {
        const row = document.createElement('p');
        const link = document.createElement('a');
        try { const source = new URL(f.url); if (source.protocol !== 'https:') throw new Error('Unsafe source URL'); link.href=source.href; }
        catch { link.href='#'; link.textContent='Invalid SEC source URL'; row.append(link); card.append(row); return; }
        link.target='_blank'; link.rel='noopener noreferrer'; link.textContent=`${f.form} · period ${f.period || 'unavailable'} · filed ${f.filed}`;
        row.append(link); card.append(row);
      });
      host.append(card);
    });
    if (document.querySelector('input[name="doctype"][value="transcript"]:checked')) {
      const note=document.createElement('p'); note.className='availability-note'; note.textContent='Transcripts are separate from SEC filings. Meridian retrieves them from a configured transcript service, or you can upload them.'; host.append(note);
    }
  } catch (err) { host.textContent = err.message; host.classList.add('error'); }
  finally { button.disabled = false; }
});

byId('research-form').addEventListener('submit', async event => {
  event.preventDefault();
  const a = await showCompanyMeta('company-one', 'company-one-meta');
  const b = await showCompanyMeta('company-two', 'company-two-meta');
  const years = rangeYears(byId('year-from').value, byId('year-to').value);
  const types = [...document.querySelectorAll('input[name="doctype"]:checked')].map(el => el.value);
  const error = byId('form-error'); error.textContent = '';
  if (!a || !b) { error.textContent = 'Choose both companies from the configured universe.'; return; }
  if (!a.cik || !b.cik) { error.textContent = 'One selected company has no mapped SEC CIK. Resolve the CIK before starting research.'; return; }
  if (a.cik === b.cik) { error.textContent = 'Select two different companies.'; return; }
  if (!years) { error.textContent = 'Choose a valid range of up to 15 fiscal years.'; return; }
  if (!types.length) { error.textContent = 'Choose at least one document type.'; return; }
  const button = byId('start-button'); button.disabled = true; button.querySelector('span').textContent = 'Starting research…';
  byId('results').classList.add('hidden');
  byId('progress-panel').classList.remove('hidden');
  byId('progress-title').textContent = `${a.company_name} vs ${b.company_name}`;
  const fastMode = fastDemoToggle.checked;
  byId('pipeline-message').classList.toggle('info', fastMode);
  byId('pipeline-message').textContent = fastMode
    ? 'Fast presentation mode is using saved documents and facts only; it will not contact the SEC for new filings.'
    : '';
  try {
    const response = await api('/api/research', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({company_ciks:[a.cik,b.cik],years,document_types:types,
        fetch_sec:fastMode ? false : fetchSecToggle.checked, fast_mode:fastMode})});
    activeRun = response.run_id;
    await pollRun();
  } catch (err) {
    byId('pipeline-message').classList.remove('info');
    byId('pipeline-message').textContent = err.message;
    byId('run-state').textContent = 'FAILED';
  } finally {
    button.disabled = false; button.querySelector('span').textContent = 'Generate research memo';
  }
});

const stageNames = [
  ['company_resolution','Resolve companies'], ['document_acquisition','Acquire SEC filings'],
  ['transcript_acquisition','Acquire transcripts'], ['xbrl_ingestion','Extract financial facts'], ['document_extraction','Extract documents'],
  ['indexing','Index evidence'], ['evidence_retrieval','Retrieve evidence'],
  ['risk_analysis','Risk Agent'], ['strategy_analysis','Strategy Agent'],
  ['financial_analysis','Financial Agent'], ['synthesis','Synthesize memo'],
  ['evidence_validation','Validate claims']
];

function renderProgress(traces, status, request = {}) {
  const terminalStatuses = new Set(['completed', 'reused', 'skipped', 'failed']);
  const companyCount = Math.max(1, request.company_ciks?.length || 2);
  const yearCount = Math.max(1, request.years?.length || 1);
  const docTypes = new Set(request.document_types || []);
  const fetchSec = request.fetch_sec !== false;
  const secFormsSelected = [...docTypes].some(type => type === '10-K' || type === '10-Q');
  const expectedCounts = {
    company_resolution: 1,
    document_acquisition: companyCount,
    transcript_acquisition: docTypes.has('transcript') && fetchSec && transcriptProviderConfigured
      ? companyCount * yearCount : companyCount,
    xbrl_ingestion: companyCount,
    document_extraction: companyCount,
    indexing: companyCount,
    evidence_retrieval: companyCount * 2,
    risk_analysis: companyCount,
    strategy_analysis: companyCount,
    financial_analysis: 1,
    synthesis: 1,
    evidence_validation: 1
  };
  const stateFor = id => {
    if (id === 'document_acquisition' && (!fetchSec || !secFormsSelected)) return 'skipped';
    if (id === 'transcript_acquisition' && (!fetchSec || !docTypes.has('transcript'))) return 'skipped';
    const rows = traces.filter(t => t.stage === id);
    const terminal = rows.filter(t => terminalStatuses.has(t.status));
    if (terminal.length < (expectedCounts[id] || 1)) return '';
    if (terminal.some(t => t.status === 'failed')) return 'failed';
    if (terminal.every(t => t.status === 'skipped')) return 'skipped';
    if (terminal.every(t => t.status === 'reused')) return 'reused';
    return 'done';
  };
  const states = new Map(stageNames.map(([id]) => [id, stateFor(id)]));
  const active = ['queued', 'running'].includes(status)
    ? (stageNames.find(([id]) => !states.get(id))?.[0] || '') : '';
  const html = stageNames.map(([id,label]) => {
    const state = states.get(id) || (id === active ? 'active' : '');
    const shown = state === 'failed' ? 'Failed' : state === 'done' ? 'Complete' : state === 'reused' ? 'Reused' : state === 'skipped' ? 'Skipped' : state === 'active' ? 'Running' : 'Waiting';
    return `<div class="stage ${state}"><strong>${escapeHtml(label)}</strong><small>${shown}</small></div>`;
  }).join('');
  byId('stage-grid').innerHTML = html;
  const finished = [...states.values()].filter(Boolean).length;
  byId('progress-bar').style.width = `${Math.min(100, Math.round(finished/stageNames.length*100))}%`;
  byId('run-state').textContent = status.replaceAll('_',' ').toUpperCase();
}

async function pollRun() {
  if (pollTimer) clearTimeout(pollTimer);
  const [run, traceData] = await Promise.all([api(`/api/runs/${activeRun}`), api(`/api/runs/${activeRun}/traces`)]);
  renderProgress(traceData.traces, run.status, run.request);
  if (run.status === 'queued' || run.status === 'running') {
    pollTimer = setTimeout(pollRun, 1400); return;
  }
  if (run.status === 'failed') {
    byId('pipeline-message').classList.remove('info');
    byId('pipeline-message').textContent = run.error || 'Research run failed.';
    renderTraceTable(traceData.traces); return;
  }
  renderResult(run.result, traceData.traces);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

function formatMetric(metric) {
  const value = Number(metric.value), unit = String(metric.unit || '');
  if (unit === '%') return `${value.toFixed(2)}%`;
  const currency = unit.toUpperCase().match(/\b[A-Z]{3}\b/)?.[0];
  const number = value.toLocaleString(undefined, {maximumFractionDigits: unit.toLowerCase().includes('share') ? 2 : 0});
  if (currency) {
    try { return new Intl.NumberFormat(undefined, {style:'currency',currency,maximumFractionDigits:unit.toLowerCase().includes('share') ? 2 : 0}).format(value); }
    catch (_) { return `${number} ${currency}`; }
  }
  return unit.toLowerCase().includes('share') ? `${number} ${unit}` : `${number}${unit ? ` ${unit}` : ''}`;
}

function renderFinancialCharts(metricsByCompany) {
  const host = byId('financial-charts'); host.replaceChildren();
  let chartCount = 0;
  Object.values(metricsByCompany).forEach(company => {
    const groups = new Map();
    Object.entries(company.periods || {}).forEach(([period, values]) => {
      const revenue = values.revenue;
      if (!revenue || revenue.value == null || !Number.isFinite(Number(revenue.value))) return;
      const unit = revenue.unit || 'reported units';
      if (!groups.has(unit)) groups.set(unit, []);
      groups.get(unit).push({period, value:Number(revenue.value)});
    });
    groups.forEach((values, unit) => {
      if (!values.length) return;
      chartCount++;
      values.sort((a,b) => a.period.localeCompare(b.period));
      const width=620, height=220, left=42, right=12, top=30, bottom=42;
      const plotW=width-left-right, plotH=height-top-bottom;
      const max=Math.max(1,...values.map(x=>Math.abs(x.value)));
      const slot=plotW/values.length, barW=Math.min(64,slot*.54);
      const bars=values.map((item,i)=>{
        const h=Math.max(1,Math.abs(item.value)/max*(plotH-10));
        const x=left+i*slot+(slot-barW)/2, y=top+plotH-h;
        const label=Number(item.value).toLocaleString(undefined,{maximumFractionDigits:0});
        return `<rect x="${x}" y="${y}" width="${barW}" height="${h}" rx="4" fill="#247454"><title>${escapeHtml(item.period)}: ${escapeHtml(label)} ${escapeHtml(unit)}</title></rect><text x="${x+barW/2}" y="${height-17}" text-anchor="middle">${escapeHtml(item.period)}</text>`;
      }).join('');
      const card=document.createElement('div'); card.className='chart-card';
      card.innerHTML=`<strong>${escapeHtml(company.company)} · ${escapeHtml(unit)}</strong><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Reported revenue for ${escapeHtml(company.company)} in ${escapeHtml(unit)}"><line x1="${left}" y1="${top+plotH}" x2="${width-right}" y2="${top+plotH}" stroke="#dce5df"/><text x="${left-5}" y="${top+8}" text-anchor="end">${escapeHtml(max.toLocaleString(undefined,{maximumFractionDigits:0}))}</text>${bars}</svg>`;
      host.append(card);
    });
  });
  if (!chartCount) { const empty=document.createElement('p'); empty.className='empty-state'; empty.textContent='No reported revenue facts were available for the selected periods.'; host.append(empty); }
}

function renderReviewGroup(host, heading, count, items, describe) {
  const group = document.createElement('section'); group.className = 'review-group';
  const title = document.createElement('h4'); title.textContent = `${heading} · ${count ?? items.length}`; group.append(title);
  if (!items.length) {
    const empty = document.createElement('p'); empty.className = 'muted';
    empty.textContent = 'No candidates were extracted from the available selected-period sources. This does not establish that none exist.';
    group.append(empty); host.append(group); return;
  }
  items.slice(0, 12).forEach(item => {
    const card = document.createElement('article'); card.className = 'review-item';
    const label = document.createElement('strong'); label.textContent = describe.title(item); card.append(label);
    const detail = document.createElement('p'); detail.textContent = describe.body(item); card.append(detail);
    if (item.source_url) {
      try {
        const source = new URL(item.source_url);
        if (source.protocol === 'https:') {
          const link = document.createElement('a'); link.href = source.href; link.target = '_blank';
          link.rel = 'noopener noreferrer'; link.textContent = 'Open filing'; card.append(link);
        }
      } catch { /* omit untrusted or malformed source URLs */ }
    }
    card.dataset.reviewStatus = item.review_status || item.support_status || 'pending';
    group.append(card);
  });
  if (count > items.length) {
    const more = document.createElement('p'); more.className = 'fineprint';
    more.textContent = `${count - items.length} additional candidate(s) not shown here.`; group.append(more);
  }
  host.append(group);
}

function renderAnalystReview(memo) {
  const review = memo.financial?.analyst_review || {};
  const claims = review.claim_reviews || memo.validation?.claim_review_queue || {count:0,items:[]};
  const custom = review.custom_tags || {count:0,items:[]};
  const dimensioned = review.dimensioned_facts || {count:0,items:[]};
  const series = review.segment_series || {count:0,items:[]};
  const restatements = review.restatement_candidates || {count:0,items:[]};
  const total = custom.count + dimensioned.count + series.count + restatements.count + claims.count;
  byId('analyst-review-summary').textContent = total
    ? `${total} review item(s) found. These are candidates for a human reviewer; the app does not approve custom-tag mappings, segment comparability, restatement status, or semantic entailment.`
    : 'No review candidates were returned from the selected documents. Missing or unparseable filing data can also produce an empty queue.';
  const host = byId('analyst-review-queue'); host.replaceChildren();
  renderReviewGroup(host, 'Issuer-specific XBRL tags', custom.count, custom.items || [], {
    title: item => `${item.company || 'Company'} · ${item.name || 'Unknown tag'} · ${item.fiscal_period || 'period unavailable'}`,
    body: item => `Value ${item.raw_value || item.value || 'unavailable'} ${item.unit_ref || ''}. ${item.review_reason || ''}`
  });
  renderReviewGroup(host, 'Dimensioned facts', dimensioned.count, dimensioned.items || [], {
    title: item => `${item.company || 'Company'} · ${item.name || 'Unknown tag'}`,
    body: item => {
      const dimensions = (item.dimensions || []).map(d => `${d.dimension || '?'}=${d.member || '?'}`).join('; ');
      return `${item.raw_value || item.value || 'unavailable'} ${item.unit_ref || ''} · ${dimensions || 'dimensions unavailable'} · ${item.review_reason || ''}`;
    }
  });
  renderReviewGroup(host, 'Same-tag, same-dimension period series', series.count, series.items || [], {
    title: item => `${item.tag || 'Unknown tag'} · ${(item.dimensions || []).map(d => d.member || d.dimension).join(', ')}`,
    body: item => `${(item.periods || []).map(p => `${p.end}: ${p.value} ${item.unit_ref || ''}`).join(' · ')}. ${item.review_reason || ''}`
  });
  renderReviewGroup(host, 'Changed same-period facts', restatements.count, restatements.items || [], {
    title: item => `${item.company || 'Company'} · ${item.metric || item.tag} · FY${item.fiscal_year}`,
    body: item => `${item.as_filed?.value} ${item.unit || ''} (filed ${item.as_filed?.filed || 'unknown'}) → ${item.latest_comparative?.value} ${item.unit || ''} (filed ${item.latest_comparative?.filed || 'unknown'}). Possible restatement or reclassification; inspect both filings.`
  });
  renderReviewGroup(host, 'Claims needing entailment review', claims.count, claims.items || [], {
    title: item => `${item.company || 'Company'} · ${item.support_status || 'review'}`,
    body: item => `${item.claim || ''} ${item.reason || ''} Evidence IDs: ${(item.evidence_ids || []).join(', ') || 'none'}.`
  });
}

function evidenceButtons(ids=[]) { return ids.map(id => `<button class="evidence-link" data-evidence="${escapeHtml(id)}">${escapeHtml(id.slice(0,8))}</button>`).join(' ') || '<span class="empty-state">—</span>'; }

function renderResult(result, traces) {
  if (!result) return;
  byId('results').classList.remove('hidden');
  byId('result-title').textContent = result.memo.title;
  byId('result-subtitle').textContent = `${result.memo.periods.join(' · ')} · ${result.status.replaceAll('_',' ')}`;
  byId('executive-summary').textContent = result.memo.executive_summary;
  byId('comparative-assessment').textContent = result.memo.comparative_assessment;
  byId('claim-validation').textContent = `Evidence checks: ${result.memo.validation.claims_supported}/${result.memo.validation.claims_checked} claims retained; ${result.memo.validation.claims_removed} unsupported claim(s) removed; ${result.memo.validation.claims_requiring_analyst_review || 0} require semantic or accounting review.`;
  byId('download-link').href = `/api/runs/${activeRun}/memo.md`;
  byId('download-pdf-link').href = `/api/runs/${activeRun}/memo.pdf`;
  const tbody = byId('financial-table').querySelector('tbody'); tbody.replaceChildren();
  Object.entries(result.memo.financial.metrics || {}).forEach(([cik, company]) => Object.entries(company.periods || {}).forEach(([period, metrics]) => Object.entries(metrics).forEach(([name, metric]) => {
    if (metric.value == null) return;
    const tr = document.createElement('tr');
    const value = formatMetric(metric);
    const formula = metric.formula || (metric.tag ? `Reported SEC XBRL tag: ${metric.tag}` : 'Reported SEC XBRL fact');
    tr.innerHTML = `<td>${escapeHtml(company.company)}</td><td>${escapeHtml(period)}</td><td>${escapeHtml(name.replaceAll('_',' '))}</td><td class="value">${escapeHtml(value)}</td><td><small>${escapeHtml(formula)}</small></td><td>${evidenceButtons(metric.evidence_ids)}</td>`;
    tbody.append(tr);
  })));
  renderFinancialCharts(result.memo.financial?.metrics || {});
  renderAnalystReview(result.memo);
  renderFindings('risk-findings', result.memo.risks || []);
  renderFindings('strategy-findings', result.memo.strategies || []);
  renderCoverage(result.memo.coverage || {});
  renderEvidence(result.memo.evidence || []);
  renderTraceTable(traces);
  document.querySelectorAll('[data-evidence]').forEach(button => button.addEventListener('click', () => {
    const target = document.querySelector(`[data-evidence-id="${CSS.escape(button.dataset.evidence)}"]`);
    if (target) { target.open = true; target.scrollIntoView({behavior:'smooth',block:'center'}); }
  }));
  renderEvaluation();
  byId('results').scrollIntoView({behavior:'smooth',block:'start'});
}

function renderFindings(elementId, agents) {
  const host = byId(elementId); host.replaceChildren();
  agents.forEach(agent => {
    const heading = document.createElement('strong'); heading.textContent = agent.company; heading.className = 'finding-company'; host.append(heading);
    if (!agent.findings?.length) { const p = document.createElement('p'); p.className='empty-state'; p.textContent='No evidence-backed finding produced.'; host.append(p); return; }
    agent.findings.forEach(f => {
      const item = document.createElement('div'); item.className='finding';
      item.innerHTML = `<div class="finding-head"><span>${escapeHtml(f.category)}</span><span class="severity ${f.severity==='high'?'high':''}">${escapeHtml(f.severity)}</span></div><p>${escapeHtml(f.description)}</p><small>${escapeHtml(f.period)} · ${evidenceButtons(f.evidence_ids)}</small>`;
      host.append(item);
    });
  });
}

function renderCoverage(coverage) {
  const host = byId('coverage-list'); host.replaceChildren();
  Object.values(coverage).forEach(item => {
    const card = document.createElement('div'); card.className='coverage-item';
    const docs = item.documents || [];
    const gaps = [];
    if (item.missing_periods?.length) gaps.push(`missing periods: ${item.missing_periods.join(', ')}`);
    if (item.missing_document_types?.length) gaps.push(`missing document types: ${item.missing_document_types.join(', ')}`);
    const warning = gaps.length ? gaps.join(' · ') : 'Selected periods and document types have coverage';
    card.innerHTML = `<strong>${escapeHtml(item.company)}</strong><p>${docs.length} stored document(s) · ${escapeHtml(warning)}</p>${item.transcript_note?`<p class="warning">${escapeHtml(item.transcript_note)}</p>`:''}`;
    if (item.processing_limitations?.length) {
      const p = document.createElement('p'); p.className='warning'; p.textContent=item.processing_limitations.join(' '); card.append(p);
    }
    host.append(card);
  });
}

function renderEvidence(evidence) {
  const host = byId('evidence-list'); host.replaceChildren();
  evidence.forEach(item => {
    const details = document.createElement('details'); details.className='evidence-item'; details.dataset.evidenceId=item.evidence_id;
    const sourceLink = item.source_url ? `<a target="_blank" rel="noopener noreferrer" href="${escapeHtml(item.source_url)}">Open source document ↗</a>` : item.source_document_id ? `<a href="/api/documents/${encodeURIComponent(item.source_document_id)}/original" download>Download archived original ↧</a>` : '<small>Source URL unavailable</small>';
    details.innerHTML = `<summary><strong>${escapeHtml(item.evidence_id.slice(0,12))}</strong> · ${escapeHtml(item.company)} · ${escapeHtml(item.document_type)} ${escapeHtml(item.fiscal_period)} · ${escapeHtml(item.section || item.table_id || 'location unavailable')}</summary><p>${escapeHtml(item.content)}</p><small>${item.page_number ? `Page ${item.page_number}` : 'Page unavailable'} · ${escapeHtml(item.extraction_method)}</small><br>${sourceLink}`;
    host.append(details);
  });
}

function renderTraceTable(traces) {
  const tbody = byId('trace-table').querySelector('tbody'); tbody.replaceChildren();
  traces.forEach(t => {
    const tr = document.createElement('tr');
    const detail = t.output_json || t.error || '';
    const pretty = typeof detail === 'string' ? detail : JSON.stringify(JSON.parse(detail),null,2);
    tr.innerHTML = `<td>${escapeHtml(t.stage)}</td><td>${escapeHtml(t.agent || '—')}</td><td>${escapeHtml(t.status)}</td><td>${t.latency_ms == null ? '—' : `${t.latency_ms} ms`}</td><td><div class="trace-output">${escapeHtml(pretty)}</div>${t.token_usage ? `<small>${t.token_usage} tokens</small>`:''}</td>`;
    tbody.append(tr);
  });
}

async function renderEvaluation() {
  try {
    const result = await api('/api/evaluation');
    const values = [
      ['Context precision', result.context_precision], ['Context recall', result.context_recall],
      ['Hallucination rate', result.hallucination_rate]
    ];
    byId('evaluation-results').innerHTML = values.map(([name,value]) => `<div class="eval-metric"><span>${name}</span><strong>${value == null ? 'N/A' : `${(value*100).toFixed(1)}%`}</strong></div>`).join('') + `<div class="eval-metric"><span>Fixture cases</span><strong>${result.dataset_size}</strong></div>`;
    byId('evaluation-method').textContent = `${result.dataset_name} · ${result.methodology.hallucination_rate} Limitations: ${result.limitations.join(' ')}`;
  } catch (error) { byId('evaluation-results').textContent = error.message; }
}

byId('upload-form').addEventListener('submit', async event => {
  event.preventDefault();
  const result = byId('upload-result'); result.textContent = 'Extracting and indexing…';
  const form = new FormData(); form.append('company_cik', byId('upload-company').value);
  form.append('document_type', byId('upload-type').value); form.append('fiscal_period', byId('upload-period').value.trim());
  form.append('file', byId('upload-file').files[0]);
  try {
    const response = await api('/api/upload', {method:'POST',body:form});
    result.textContent = `${response.created ? 'Indexed' : 'Already stored'} · ${response.block_count} evidence block(s) · ${response.extraction_methods.join(', ')}`;
  } catch (err) { result.textContent = err.message; result.style.color='#b34040'; }
});

byId('universe-form').addEventListener('submit', async event => {
  event.preventDefault(); const result = byId('universe-result'); result.textContent='Importing…';
  const form = new FormData(); form.append('file', byId('universe-file').files[0]); form.append('source_url', byId('universe-source').value.trim());
  try {
    const data = await api('/api/universe/import',{method:'POST',body:form});
    result.textContent = `Loaded ${data.companies_loaded} companies.`;
    byId('universe-title').textContent = `${data.source.count} configured company record(s)`;
    byId('universe-info').textContent = `${data.source.reporting_years.join(', ')} ranking · ${data.source.sources[0] || 'source not supplied'}`;
    await initialize();
  } catch (err) { result.textContent=err.message; result.style.color='#b34040'; }
});

initialize().catch(error => {
  byId('health-label').textContent='Research engine unavailable';
  byId('setup-notice').textContent=`Could not connect to the Meridian API: ${error.message}`;
  byId('setup-notice').classList.remove('hidden');
});
