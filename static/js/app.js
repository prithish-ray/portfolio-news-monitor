/* =====================================================================
   AI Portfolio News Monitor — Frontend JavaScript
   ===================================================================== */

'use strict';

// ── State ────────────────────────────────────────────────────────────────────
let currentSessionId  = null;
let evtSource         = null;
window.uploadedReport = null;
const MAX_COMPANIES   = 3;

// ── DOM helpers ───────────────────────────────────────────────────────────────
const $  = id  => document.getElementById(id);
const qs = sel => document.querySelector(sel);

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {

  // Model radio → update badge
  document.querySelectorAll('input[name=model]').forEach(radio => {
    radio.addEventListener('change', () => {
      document.querySelectorAll('.model-option').forEach(el => el.classList.remove('selected'));
      radio.closest('.model-option').classList.add('selected');
      const label = radio.closest('.model-option').querySelector('.model-name').textContent;
      $('activeBadge').textContent = label.split('—')[0].trim();
    });
    if (radio.checked) {
      const label = radio.closest('.model-option').querySelector('.model-name').textContent;
      $('activeBadge').textContent = label.split('—')[0].trim();
    }
  });

  // Form submit
  $('analyzeForm').addEventListener('submit', e => {
    e.preventDefault();
    startAnalysis();
  });

  // File upload: click-to-browse
  const jsonUpload = $('jsonUpload');
  if (jsonUpload) {
    jsonUpload.addEventListener('change', e => {
      const file = e.target.files[0];
      if (file) handleReportFile(file);
    });
  }

  // File upload: drag-and-drop
  const uploadDrop = $('uploadDrop');
  if (uploadDrop) {
    uploadDrop.addEventListener('dragover', e => {
      e.preventDefault();
      uploadDrop.classList.add('dragover');
    });
    uploadDrop.addEventListener('dragleave', () => uploadDrop.classList.remove('dragover'));
    uploadDrop.addEventListener('drop', e => {
      e.preventDefault();
      uploadDrop.classList.remove('dragover');
      const file = e.dataTransfer?.files?.[0];
      if (file && file.name.endsWith('.json')) {
        handleReportFile(file);
      } else if (file) {
        alert('Please drop a .json report file.');
      }
    });
  }

  // Custom searches toggle
  const chkCustom = $('chkCustomSearches');
  if (chkCustom) {
    chkCustom.addEventListener('change', () => {
      $('customSearchesPanel').style.display = chkCustom.checked ? 'block' : 'none';
    });
  }
});

// ── Report upload handling ────────────────────────────────────────────────────
function handleReportFile(file) {
  const reader = new FileReader();
  reader.onload = evt => {
    try {
      const data = JSON.parse(evt.target.result);
      if (!data.results || !Array.isArray(data.results)) {
        alert('This doesn\'t look like a valid portfolio report JSON.');
        return;
      }
      window.uploadedReport = data;
      autoFillFromReport(data);
      showUploadMeta(data, file.name);
    } catch (err) {
      alert('Could not parse JSON: ' + err.message);
    }
  };
  reader.readAsText(file);
}

function autoFillFromReport(data) {
  // Repopulate company rows
  const inputs = (data.results || []).map(r => r.company_input || '').filter(Boolean);
  const container = $('companyInputs');
  container.innerHTML = '';
  inputs.slice(0, MAX_COMPANIES).forEach(val => {
    const row = document.createElement('div');
    row.className = 'company-row';
    row.innerHTML =
      '<input type="text" class="company-input" placeholder="e.g. AAPL or Apple Inc" maxlength="80" />' +
      '<button type="button" class="btn-remove" onclick="removeCompany(this)" title="Remove">✕</button>';
    container.appendChild(row);
    row.querySelector('input').value = val;
  });
  if (!inputs.length) addCompany();
  updateAddBtn();

  // Restore search area flags from report
  if (data.search_exchange_filings !== undefined) $('chkExchangeFilings').checked = data.search_exchange_filings;
  if (data.search_company_news     !== undefined) $('chkCompanyNews').checked     = data.search_company_news;
  if (data.search_industry_news    !== undefined) $('chkIndustryNews').checked    = data.search_industry_news;

  // Restore custom searches
  const customSearches = data.custom_searches || [];
  $('customSearchList').innerHTML = '';
  if (customSearches.length) {
    $('chkCustomSearches').checked = true;
    $('customSearchesPanel').style.display = 'block';
    customSearches.forEach(s => addCustomSearch(s));
  }
}

function showUploadMeta(data, filename) {
  let lastRunStr = 'unknown date';
  let daysBack   = 7;
  if (data.generated_at) {
    try {
      const dt = new Date(data.generated_at);
      lastRunStr = dt.toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit'
      });
      const diffMs = Date.now() - dt.getTime();
      daysBack = Math.max(1, Math.ceil(diffMs / 86400000) + 1);
    } catch (_) {}
  }

  const companies = (data.results || []).map(r => r.company_input || '').filter(Boolean);

  $('uploadMeta').innerHTML =
    '<strong>📂 ' + escHtml(filename) + '</strong><br>' +
    'Last run: ' + escHtml(lastRunStr) + '<br>' +
    'Companies: ' + escHtml(companies.join(', '));

  $('uploadInfo').style.display = 'block';
  $('uploadLabel').textContent  = '✅ Report loaded';

  // Update hints
  const hint = 'since last run (' + daysBack + ' day' + (daysBack !== 1 ? 's' : '') + ' back)';
  if ($('hintExchange'))  $('hintExchange').textContent  = 'SEC / TDnet / NSE / BSE / Investegate (' + hint + ')';
  if ($('hintNews'))      $('hintNews').textContent      = 'Company news (' + hint + ')';
  if ($('hintIndustry'))  $('hintIndustry').textContent  = 'Sector & country news (' + hint + ')';
}

function clearUpload() {
  window.uploadedReport = null;
  const jsonUpload = $('jsonUpload');
  if (jsonUpload) jsonUpload.value = '';
  $('uploadInfo').style.display = 'none';
  $('uploadLabel').textContent  = '⬆️ Drop JSON or click to browse';
  // Reset hints
  if ($('hintExchange'))  $('hintExchange').textContent  = 'SEC / TDnet / NSE / BSE / Investegate (last 7 days)';
  if ($('hintNews'))      $('hintNews').textContent      = 'Recent news, earnings, contracts (last 7 days)';
  if ($('hintIndustry'))  $('hintIndustry').textContent  = 'Sector & country news relevant to company (last 7 days)';
}

// ── Custom search rows ─────────────────────────────────────────────────────────
function addCustomSearch(value) {
  value = value || '';
  const list = $('customSearchList');
  const row  = document.createElement('div');
  row.className = 'custom-search-row';
  row.innerHTML =
    '<input type="text" class="custom-search-input" ' +
    'placeholder="e.g. SGFIN credit rating 2026" maxlength="200" value="' + escHtml(value) + '" />' +
    '<button type="button" class="btn-remove-search" onclick="this.parentElement.remove()" title="Remove">✕</button>';
  list.appendChild(row);
  if (!value) row.querySelector('input').focus();
}

function getCustomSearches() {
  return Array.from(document.querySelectorAll('.custom-search-input'))
    .map(i => i.value.trim()).filter(Boolean);
}

// ── Company rows ───────────────────────────────────────────────────────────────
function addCompany() {
  const rows = document.querySelectorAll('.company-row');
  if (rows.length >= MAX_COMPANIES) return;
  const row = document.createElement('div');
  row.className = 'company-row';
  row.innerHTML =
    '<input type="text" class="company-input" placeholder="e.g. MSFT or Microsoft" maxlength="80" />' +
    '<button type="button" class="btn-remove" onclick="removeCompany(this)" title="Remove">✕</button>';
  $('companyInputs').appendChild(row);
  row.querySelector('input').focus();
  updateAddBtn();
}

function removeCompany(btn) {
  const rows = document.querySelectorAll('.company-row');
  if (rows.length <= 1) {
    rows[0].querySelector('input').value = '';
    return;
  }
  btn.closest('.company-row').remove();
  updateAddBtn();
}

function updateAddBtn() {
  const count = document.querySelectorAll('.company-row').length;
  $('addCompanyBtn').disabled    = count >= MAX_COMPANIES;
  $('addCompanyBtn').textContent = count >= MAX_COMPANIES
    ? 'Maximum ' + MAX_COMPANIES + ' companies reached'
    : '+ Add company';
}

// ── Form submission ────────────────────────────────────────────────────────────
async function startAnalysis() {
  const companies = Array.from(document.querySelectorAll('.company-input'))
    .map(i => i.value.trim()).filter(Boolean);
  if (!companies.length) {
    alert('Please enter at least one company ticker or name.');
    return;
  }

  const model                  = qs('input[name=model]:checked')?.value || 'llama-3.1-8b-instant';
  const search_exchange_filings = $('chkExchangeFilings').checked;
  const search_company_news     = $('chkCompanyNews').checked;
  const search_industry_news    = $('chkIndustryNews').checked;
  const search_custom           = $('chkCustomSearches').checked;
  const custom_searches         = getCustomSearches();

  const anySource = search_exchange_filings || search_company_news ||
                    search_industry_news || (search_custom && custom_searches.length > 0);
  if (!anySource) {
    alert('Please select at least one search area (or add custom search queries).');
    return;
  }

  clearResults(false);
  setAnalyzing(true);

  const body = {
    companies,
    model,
    search_exchange_filings,
    search_company_news,
    search_industry_news,
    search_custom,
    custom_searches,
    existing_report: window.uploadedReport || null,
  };

  try {
    const resp = await fetch('/analyze', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Server error');
    currentSessionId = data.session_id;
    connectSSE(currentSessionId, companies.length);
  } catch (err) {
    setAnalyzing(false);
    addTrailItem('❌ Failed to start: ' + err.message, 'error');
    showProgressPanel();
  }
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE(sessionId, expectedCount) {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/stream/' + sessionId);
  evtSource.onmessage = e => {
    try { handleEvent(JSON.parse(e.data)); }
    catch (err) { console.error('SSE parse error:', err); }
  };
  evtSource.onerror = () => {
    addTrailItem('⚠️ Connection interrupted — results may be incomplete.', 'warn');
    evtSource.close();
    setAnalyzing(false);
  };
}

function handleEvent(evt) {
  const { type, data } = evt;
  switch (type) {
    case 'progress':
      updateProgress(data.message, data.percentage);
      break;

    case 'company_info': {
      const meta = [data.info.sector, data.info.country]
        .filter(v => v && v !== 'Unknown').join(', ') || 'data via web search';
      addTrailItem('🏢 Company identified: <strong>' + escHtml(data.info.name) + '</strong> (' +
        escHtml(data.info.ticker) + ') — ' + escHtml(meta));
      break;
    }

    case 'need_choice':
      showChoiceModal(data);
      break;

    case 'search_terms':
      addTrailItem('🔑 Search queries: ' + data.terms.slice(0, 3).join(' · '));
      break;

    case 'company_result':
      renderCompanyCard(data.result);
      break;

    case 'complete':
      onComplete(data);
      break;

    case 'error':
      addTrailItem('❌ Error: ' + data.message, 'error');
      setAnalyzing(false);
      hideChoiceModal();
      if (evtSource) evtSource.close();
      break;

    case 'stream_end':
      if (evtSource) evtSource.close();
      break;

    case 'heartbeat':
    default:
      break;
  }
}

// ── Company disambiguation modal ──────────────────────────────────────────────
function showChoiceModal(data) {
  $('choicePrompt').textContent =
    'Multiple companies match "' + data.input + '". Please select the correct one:';
  const list = $('choiceList');
  list.innerHTML = '';
  (data.candidates || []).forEach(c => {
    const btn = document.createElement('button');
    btn.className = 'choice-btn';
    btn.type = 'button';
    const exch = c.exchange_full || c.exchange || '';
    btn.innerHTML =
      '<span class="choice-symbol">' + escHtml(c.symbol) + '</span>' +
      '<span class="choice-name">'   + escHtml(c.name)   + '</span>' +
      (exch ? '<span class="choice-exch">' + escHtml(exch) + '</span>' : '') +
      (c.currency ? '<span class="choice-currency">' + escHtml(c.currency) + '</span>' : '');
    btn.onclick = () => resolveChoice(data.session_id, c.symbol);
    list.appendChild(btn);
  });
  $('choiceModal').style.display = 'flex';
}

function hideChoiceModal() {
  $('choiceModal').style.display = 'none';
}

async function resolveChoice(sessionId, symbol) {
  hideChoiceModal();
  addTrailItem('✅ Selected: <strong>' + escHtml(symbol) + '</strong>');
  try {
    await fetch('/resolve/' + sessionId, {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ symbol }),
    });
  } catch (err) {
    console.error('resolve failed:', err);
  }
}

// ── Progress trail ─────────────────────────────────────────────────────────────
function showProgressPanel() {
  $('emptyState').style.display    = 'none';
  $('progressPanel').style.display = 'block';
}

function updateProgress(message, pct) {
  showProgressPanel();
  $('progressBar').style.width  = Math.min(pct, 100) + '%';
  $('progressPct').textContent  = Math.round(pct) + '%';
  $('progressTitle').textContent = message.replace(/[\u{1F300}-\u{1FFFF}]/gu, '').trim() || 'Analysing…';
  addTrailItem(message);
}

function addTrailItem(html, type) {
  type = type || 'info';
  showProgressPanel();
  const trail = $('progressTrail');
  trail.querySelectorAll('.trail-item.latest').forEach(el => el.classList.remove('latest'));
  const now  = new Date();
  const ts   = String(now.getHours()).padStart(2,'0') + ':' +
               String(now.getMinutes()).padStart(2,'0') + ':' +
               String(now.getSeconds()).padStart(2,'0');
  const item = document.createElement('div');
  item.className = 'trail-item latest ' + type;
  item.innerHTML = '<span class="trail-ts">' + ts + '</span><span class="trail-msg">' + html + '</span>';
  trail.appendChild(item);
  trail.scrollTop = trail.scrollHeight;
}

// ── Company result card ────────────────────────────────────────────────────────
function renderCompanyCard(result) {
  const info      = result.company_info || {};
  const analysis  = result.analysis     || {};
  const sources   = result.sources      || {};
  const devs      = analysis.developments || [];
  const ticker    = (info.ticker || result.company_input || '?').substring(0, 10);
  const name      = info.name || result.company_input;
  const sentiment = analysis.overall_sentiment || 'Neutral';
  const newDevs   = analysis.new_developments_count || 0;

  // Accumulate one-liners for the listen bar
  if (!window._portfolioOneLiners) window._portfolioOneLiners = [];
  const oneLiner = analysis.one_line_overall || '';
  if (oneLiner) window._portfolioOneLiners.push(oneLiner);

  const sentimentIcon = { Positive: '📈', Negative: '📉', Neutral: '➡️', Mixed: '⚖️' }[sentiment] || '➡️';

  const newBadgeHtml = (newDevs > 0 && window.uploadedReport)
    ? '<span class="new-devs-badge">+' + newDevs + ' new</span>'
    : '';

  // Developments HTML
  // Header shows: one_line_summary (short, visible collapsed)
  // Body shows:   title (full detail, visible expanded)
  let devsHtml = '';
  if (devs.length) {
    devsHtml = '<div class="developments-title">📋 Developments (' + devs.length + ')' + newBadgeHtml + '</div>';
    devs.forEach(function(d, i) {
      const impact       = d.impact || 'Neutral';
      const headerText   = d.one_line_summary || d.title || '';
      const expandedText = d.title || d.one_line_summary || '';

      // Format date compactly: "2026-05-08" → "8 May 2026"
      let dateBadge = '';
      if (d.date) {
        try {
          const dt = new Date(d.date);
          if (!isNaN(dt)) {
            dateBadge = '<span class="dev-date">' +
              dt.toLocaleDateString(undefined, {day:'numeric', month:'short', year:'numeric'}) +
              '</span>';
          }
        } catch (_) {}
      }

      // "Open source" link
      const openLink = d.source_url
        ? '<a class="dev-open-link" href="' + escHtml(d.source_url) + '" target="_blank" rel="noopener">↗ Open source</a>'
        : '';

      devsHtml +=
        '<div class="development-item" id="dev-' + escHtml(result.company_input) + '-' + i + '">' +
          '<div class="dev-header" onclick="toggleDev(this)">' +
            '<span class="impact-pill ' + impact + '">' + impact + '</span>' +
            '<span class="dev-title">' + escHtml(headerText) + '</span>' +
            dateBadge +
            '<span class="type-pill">' + escHtml(formatDevType(d.type)) + '</span>' +
            '<span class="dev-chevron">▾</span>' +
          '</div>' +
          '<div class="dev-body">' +
            '<div class="dev-detail">' + escHtml(expandedText) + '</div>' +
            '<div class="dev-impact-label">' +
              '<span class="impact-pill ' + impact + '" style="font-size:11px">' + impact + '</span>' +
              ' Impact on ' + escHtml(name) + ':' +
            '</div>' +
            '<div class="dev-impact-explanation">' + escHtml(d.impact_explanation || '') + '</div>' +
            '<div class="dev-footer">' +
              (d.source ? '<span class="dev-source">Source: ' + escHtml(d.source) + '</span>' : '') +
              openLink +
            '</div>' +
          '</div>' +
        '</div>';
    });
  } else {
    devsHtml = '<p style="font-size:13px;color:#64748b;">No significant developments identified.</p>';
  }

  // Sources HTML — use summary field for link text
  const allSources = [
    ...(sources.filings || []).map(s => Object.assign({}, s, {_group: '📄 Filings'})),
    ...(sources.news    || []).map(s => Object.assign({}, s, {_group: '📰 News'})),
  ];
  let sourcesHtml = '';
  if (allSources.length) {
    ['📄 Filings', '📰 News'].forEach(group => {
      const grpItems = allSources.filter(s => s._group === group);
      if (!grpItems.length) return;
      sourcesHtml += '<div class="source-category">' + group + '</div>';
      grpItems.forEach(s => {
        const displayText = s.summary || s.title || s.url;
        sourcesHtml += '<a class="source-link" href="' + escHtml(s.url) + '" ' +
          'target="_blank" rel="noopener" title="' + escHtml(s.title || s.url) + '">' +
          escHtml(displayText) + '</a>';
      });
    });
  }

  // Search terms
  const terms    = analysis.search_terms || [];
  const termsHtml = terms.length
    ? '<div class="search-terms">' + terms.map(t => '<span class="search-term">' + escHtml(t) + '</span>').join('') + '</div>'
    : '';

  const priceId = 'price-' + ticker.replace(/[^A-Za-z0-9]/g, '_');

  const card = document.createElement('div');
  card.className = 'company-card';
  card.id = 'card-' + result.company_input;
  card.innerHTML =
    '<div class="card-header">' +
      '<div class="card-ticker">' +
        escHtml(ticker) +
        '<div class="price-change loading" id="' + priceId + '">fetching…</div>' +
      '</div>' +
      '<div class="card-meta">' +
        '<div class="card-name">' + escHtml(name) + '</div>' +
        '<div class="card-sub">' + escHtml(
          [info.industry, info.sector, info.country].filter(v => v && v !== 'Unknown').join(' · ') ||
          'Company data unavailable — results based on web search'
        ) + '</div>' +
      '</div>' +
      '<div class="card-counts">' +
        (result.filings_count ? '<span class="count-pill filings">📄 ' + result.filings_count + ' filing(s)</span>' : '') +
        (result.news_count    ? '<span class="count-pill news">📰 '    + result.news_count    + ' article(s)</span>'   : '') +
      '</div>' +
    '</div>' +

    '<div class="sentiment-banner ' + sentiment + '">' +
      sentimentIcon + ' Overall: <strong>' + sentiment + '</strong>' +
    '</div>' +

    '<div class="card-body">' +
      (analysis.one_line_overall
        ? '<div class="card-one-liner">' + escHtml(analysis.one_line_overall) + '</div>'
        : '') +
      (analysis.overall_summary
        ? '<div class="card-overview">' + escHtml(analysis.overall_summary) + '</div>'
        : '') +

      '<div class="summary-row">' +
        '<div class="summary-box">' +
          '<div class="summary-box-title">📄 Filings summary</div>' +
          '<p>' + escHtml(analysis.filings_summary || 'N/A') + '</p>' +
        '</div>' +
        '<div class="summary-box">' +
          '<div class="summary-box-title">📰 News summary</div>' +
          '<p>' + escHtml(analysis.news_summary || 'N/A') + '</p>' +
        '</div>' +
      '</div>' +

      devsHtml +

      (termsHtml
        ? '<div><div class="developments-title">🔎 Search queries used</div>' + termsHtml + '</div>'
        : '') +

      (sourcesHtml
        ? '<div>' +
            '<button class="sources-toggle" onclick="toggleSources(this)">' +
              '🔗 View sources (' + allSources.length + ') ▾' +
            '</button>' +
            '<div class="sources-panel">' + sourcesHtml + '</div>' +
          '</div>'
        : '') +
    '</div>';

  $('resultsArea').appendChild(card);
  fetchPriceChange(ticker, priceId);
}

// ── Live price-change badge ───────────────────────────────────────────────────
async function fetchPriceChange(ticker, elementId) {
  const el = $(elementId);
  if (!el) return;
  try {
    const resp = await fetch('/quote/' + encodeURIComponent(ticker));
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const d = await resp.json();
    if (d.error || d.change_pct == null) {
      el.textContent = ''; el.className = 'price-change flat'; return;
    }
    const pct  = d.change_pct;
    const sign = pct > 0 ? '+' : '';
    const cls  = pct > 0 ? 'up' : pct < 0 ? 'down' : 'flat';
    el.textContent = sign + pct.toFixed(2) + '%';
    el.className   = 'price-change ' + cls;
    el.title       = d.last_price + ' ' + (d.currency || 'USD') + ' (prev close ' + d.prev_close + ')';
  } catch (err) {
    el.textContent = ''; el.className = 'price-change flat';
  }
}

// ── Completion ────────────────────────────────────────────────────────────────
function onComplete(data) {
  setAnalyzing(false);
  if (evtSource) evtSource.close();
  $('downloadBar').style.display = 'flex';
  $('downloadCount').textContent  = data.company_count || '?';
  $('clearBtn').style.display     = 'block';
  showListenBar();
}

// ── Listen / TTS ─────────────────────────────────────────────────────────────
let _audioObjectUrl = null;
let _listenLoading  = false;

function showListenBar() {
  // Build one-line summary text from rendered cards for display
  const summaryLines = window._portfolioOneLiners || [];
  if (summaryLines.length) {
    $('listenSummary').innerHTML = summaryLines
      .map(l => '<span class="listen-line">• ' + escHtml(l) + '</span>')
      .join('');
  }
  $('listenBar').style.display = 'flex';
}

async function toggleListen() {
  const audio = $('portfolioAudio');
  const btn   = $('listenBtn');

  // If already playing, stop
  if (!audio.paused) {
    audio.pause();
    audio.currentTime = 0;
    setListenState('idle');
    return;
  }

  // If we already have the audio loaded, just play it
  if (_audioObjectUrl) {
    audio.src = _audioObjectUrl;
    audio.play();
    setListenState('playing');
    return;
  }

  if (_listenLoading) return;

  // Fetch audio from server
  _listenLoading = true;
  setListenState('loading');

  try {
    const resp = await fetch('/tts/' + currentSessionId);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({error: 'Server error'}));
      throw new Error(err.error || 'HTTP ' + resp.status);
    }
    const blob = await resp.blob();
    _audioObjectUrl = URL.createObjectURL(blob);
    audio.src = _audioObjectUrl;

    audio.onended = () => setListenState('idle');
    audio.ontimeupdate = () => {
      if (audio.duration) {
        const pct = (audio.currentTime / audio.duration) * 100;
        $('listenProgressBar').style.width = pct + '%';
      }
    };

    await audio.play();
    setListenState('playing');
  } catch (err) {
    setListenState('idle');
    addTrailItem('❌ Audio error: ' + err.message, 'error');
  } finally {
    _listenLoading = false;
  }
}

function setListenState(state) {
  const icon  = $('listenIcon');
  const label = $('listenLabel');
  const wrap  = $('listenProgressWrap');
  if (state === 'loading') {
    icon.innerHTML  = '<span class="spinner" style="width:14px;height:14px;border-width:2px"></span>';
    label.textContent = 'Generating audio…';
    wrap.style.display = 'none';
  } else if (state === 'playing') {
    icon.innerHTML  = '⏹';
    label.textContent = 'Stop';
    wrap.style.display = 'block';
  } else {
    icon.innerHTML  = '▶';
    label.textContent = 'Listen to Summary';
    wrap.style.display = 'none';
    $('listenProgressBar').style.width = '0%';
  }
}

// ── Download ──────────────────────────────────────────────────────────────────
function downloadReport() {
  if (!currentSessionId) return;
  window.location.href = '/download/' + currentSessionId;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function toggleDev(header) {
  header.closest('.development-item').classList.toggle('open');
}

function toggleSources(btn) {
  const panel = btn.nextElementSibling;
  panel.classList.toggle('open');
  btn.textContent = btn.textContent.includes('▾')
    ? btn.textContent.replace('▾', '▴')
    : btn.textContent.replace('▴', '▾');
}

function setAnalyzing(on) {
  const btn = $('analyzeBtn');
  const icon = $('analyzeBtnIcon');
  const txt  = $('analyzeBtnText');
  btn.disabled    = on;
  icon.innerHTML  = on ? '<span class="spinner"></span>' : '🔍';
  txt.textContent = on ? 'Analysing…' : 'Run Analysis';
}

function clearResults(resetForm) {
  resetForm = resetForm !== false;
  if (evtSource) { evtSource.close(); evtSource = null; }
  currentSessionId = null;
  hideChoiceModal();

  // Reset audio state
  const audio = $('portfolioAudio');
  if (audio) { audio.pause(); audio.src = ''; }
  if (_audioObjectUrl) { URL.revokeObjectURL(_audioObjectUrl); _audioObjectUrl = null; }
  _listenLoading = false;
  window._portfolioOneLiners = [];
  $('listenBar').style.display = 'none';
  $('listenSummary').innerHTML = '';
  setListenState('idle');
  $('progressPanel').style.display = 'none';
  $('downloadBar').style.display   = 'none';
  $('clearBtn').style.display      = 'none';
  $('emptyState').style.display    = '';
  $('resultsArea').innerHTML       = '';
  $('progressTrail').innerHTML     = '';
  $('progressBar').style.width     = '0%';
  $('progressPct').textContent     = '0%';
  if (resetForm) setAnalyzing(false);
}

function formatDevType(type) {
  // "company_news" → "Company News", "industry_news" → "Industry News", "filing" → "Filing", etc.
  return (type || 'news')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}

function escHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g,  '&amp;')
    .replace(/</g,  '&lt;')
    .replace(/>/g,  '&gt;')
    .replace(/"/g,  '&quot;')
    .replace(/'/g,  '&#39;');
}
