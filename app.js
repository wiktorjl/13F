// Public prefix the app is served under (e.g. "/13f" behind a reverse proxy): derived from this script's own
// URL so one build works at any mount point. History URLs are relative queries and need no prefix.
const BASE_PATH = new URL(document.currentScript?.src ?? '/', location.href).pathname.replace(/\/[^/]*$/, '');
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const VIEW_ROUTES = {funds: 'funds', overview: 'stocks', netadds: 'netadds', holdings: 'positions'};
const ROUTE_VIEWS = Object.fromEntries(Object.entries(VIEW_ROUTES).map(([panel, route]) => [route, panel]));
const SECURITY_MODES = new Set(['overview', 'netadds', 'holdings']);
const VIEW_LABELS = {funds: 'Funds', overview: 'Securities · Browse', netadds: 'Securities · Quarterly changes', holdings: 'Securities · Positions'};
const state = {
  meta: null,
  params: new URLSearchParams(),
  page: 1,
  size: 50,
  count: 0,
  sort: 'value_desc',
  aggregateRows: [],
  aggregatePage: 1,
  aggregateCount: 0,
  aggregateSort: 'value',
  aggregateDirection: 'desc',
  fundPage: 1,
  fundCount: 0,
  fundSort: 'value',
  fundDirection: 'desc',
  netPage: 1,
  netCount: 0,
  netSort: 'latest',
  netDirection: 'desc',
  netPeriods: [],
  detail: null,
  detailPage: 1,
  detailCount: 0,
  detailSort: 'current_value',
  detailDirection: 'desc',
  detailNeedsFocus: false,
  restoreNetSortFocus: false,
  restoreDetailSortFocus: false,
  fundRequestId: 0,
  aggregateRequestId: 0,
  netRequestId: 0,
  holdingsRequestId: 0,
  detailRequestId: 0,
  returnFocus: null,
  activePanel: 'funds',
  securityMode: 'overview',
  previousTab: 'funds'
};

const fmtInt = new Intl.NumberFormat('en-US', {maximumFractionDigits: 0});
const fmtCompact = new Intl.NumberFormat('en-US', {notation: 'compact', maximumFractionDigits: 2});
const fmtSnapshotDate = new Intl.DateTimeFormat('en-US', {dateStyle: 'medium', timeZone: 'UTC'});
const fmtPct = (value) => value == null ? '—' : `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(1)}%`;
const fmtMoney = (value) => {
  const number = Number(value || 0);
  const absolute = Math.abs(number);
  const sign = number < 0 ? '−' : '';
  if (absolute >= 1e12) return `${sign}$${(absolute / 1e12).toFixed(2)}T`;
  if (absolute >= 1e9) return `${sign}$${(absolute / 1e9).toFixed(2)}B`;
  if (absolute >= 1e6) return `${sign}$${(absolute / 1e6).toFixed(1)}M`;
  if (absolute >= 1e3) return `${sign}$${(absolute / 1e3).toFixed(1)}K`;
  return `${sign}$${fmtInt.format(absolute)}`;
};
const fmtSigned = (value) => `${Number(value) > 0 ? '+' : Number(value) < 0 ? '−' : ''}${fmtInt.format(Math.abs(Number(value || 0)))}`;
const fmtSignedMoney = (value) => `${Number(value) > 0 ? '+' : ''}${fmtMoney(value)}`;
const fmtMarketCap = (value) => value == null ? '—' : fmtMoney(value);
const fmtSignalReturn = (value) => {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  const number = Number(value);
  return `${number > 0 ? '+' : number < 0 ? '−' : ''}${Math.abs(number).toFixed(2)}%`;
};
const fmtSignalCoverage = (value) => {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  return `${Number(value).toFixed(Number(value) % 1 ? 1 : 0)}%`;
};
const fmtPortfolioWeight = (value) => {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  const number = Number(value);
  const decimals = Math.abs(number) >= 10 ? 1 : Math.abs(number) >= 1 ? 2 : 3;
  return `${number.toFixed(decimals)}%`;
};
const fmtWeight = (value) => {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  const number = Number(value);
  const absolute = Math.abs(number);
  const decimals = absolute >= 10 ? 1 : absolute >= 1 ? 2 : 3;
  return `${number > 0 ? '+' : number < 0 ? '−' : ''}${absolute.toFixed(decimals)} pp`;
};
const fmtPositionChange = (value) => {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  const number = Number(value);
  const absolute = Math.abs(number);
  const sign = number > 0 ? '+' : number < 0 ? '−' : '';
  if (absolute >= 1000) {
    return `${sign}${fmtCompact.format(absolute)}%`;
  }
  const decimals = absolute >= 100 ? 0 : absolute >= 10 ? 1 : 2;
  return `${sign}${absolute.toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: decimals})}%`;
};
const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[character]));
const COVERAGE_CLASSES = new Map([
  ['COMPLETE', 'complete'], ['NOTICE', 'notice'], ['PARTIAL', 'partial'],
  ['INFERRED', 'inferred'], ['MIXED', 'mixed'], ['MISSING', 'missing']
]);
const CHANGE_CLASSES = new Map([
  ['INCREASED', 'increased'], ['REDUCED', 'reduced'], ['NEW', 'new'],
  ['EXITED', 'exited'], ['UNCHANGED', 'unchanged'], ['NOT_COMPARABLE', 'not_comparable']
]);
const POSITION_CLASSES = new Map([['PUT', 'put'], ['CALL', 'call']]);

async function api(path, params = new URLSearchParams()) {
  let response;
  try {
    response = await fetch(BASE_PATH + path + (params.size ? `?${params}` : ''));
  } catch (_) {
    throw new Error('Could not reach the data service. Check your connection and try again.');
  }
  const raw = await response.text();
  let data;
  try {
    data = raw ? JSON.parse(raw) : null;
  } catch (_) {
    if (!response.ok) throw new Error(`The data service returned an error (${response.status}).`);
    throw new Error('The data service returned an unreadable response. Please try again.');
  }
  if (!response.ok) {
    const detail = typeof data?.error === 'string' && data.error.trim() ? data.error.trim() : null;
    throw new Error(detail || `The data service returned an error (${response.status}).`);
  }
  if (data == null) throw new Error('The data service returned an empty response. Please try again.');
  return data;
}

function showError(error) {
  $('#errorMessage').textContent = error?.message || String(error);
  $('#errorToast').hidden = false;
}

function clearError() {
  $('#errorToast').hidden = true;
  $('#errorMessage').textContent = '';
}

function setTableBusy(bodySelector, message, colspan) {
  const body = $(bodySelector);
  const wrapper = body.closest('.table-wrap');
  if (wrapper) {
    wrapper.scrollTop = 0;
    wrapper.scrollLeft = 0;
  }
  body.innerHTML = `<tr><td colspan="${colspan}"><div class="loading" role="status">${esc(message)}</div></td></tr>`;
  wrapper?.setAttribute('aria-busy', 'true');
}

function finishTable(bodySelector) {
  $(bodySelector).closest('.table-wrap')?.setAttribute('aria-busy', 'false');
}

function formatPeriod(label) {
  const match = String(label || '').match(/^(\d{2})-([A-Z]{3})-(\d{4})$/);
  if (!match) return label || '—';
  const monthNumber = {JAN: 1, FEB: 2, MAR: 3, APR: 4, MAY: 5, JUN: 6,
    JUL: 7, AUG: 8, SEP: 9, OCT: 10, NOV: 11, DEC: 12}[match[2]];
  if (!monthNumber) return label;
  const month = `${match[2][0]}${match[2].slice(1).toLowerCase()}`;
  return `Q${Math.ceil(monthNumber / 3)} ${match[3]} · ${month} ${Number(match[1])}`;
}

function formatQuarter(label) {
  return formatPeriod(label).split(' · ')[0];
}

function stockNameHtml(ticker, issuer) {
  const symbol = ticker ? esc(ticker) : '—';
  const missingClass = ticker ? '' : ' ticker-missing';
  const missingLabel = ticker ? '' : ' aria-label="Ticker unavailable"';
  return `<span class="ticker-symbol${missingClass}"${missingLabel}>${symbol}</span><span class="ticker-separator" aria-hidden="true">·</span><span>${esc(issuer || 'Unknown')}</span>`;
}

function selectedPeriod() {
  const period = state.params.get('period');
  return period || state.meta.latest_period;
}

function contextPeriod() {
  if (state.detail && !$('#detail').hidden) return $('#detailPeriod').value || state.meta.latest_period;
  if (state.activePanel === 'netadds') return state.meta.latest_period;
  return selectedPeriod();
}

function entityHref(type, id, period = contextPeriod()) {
  const params = new URLSearchParams({[type]: id, period});
  return `?${params}`;
}

function fundStarHtml(starred, showEmpty = false) {
  const active = Number(starred) === 1;
  if (!active && !showEmpty) return '';
  const label = active ? 'Featured research set fund' : 'Not in the featured research set';
  return `<span class="fund-star${active ? '' : ' unstarred'}" aria-hidden="true">${active ? '★' : '☆'}</span><span class="sr-only">${label}: </span>`;
}

function fundLink(cik, name, starred = false, period = contextPeriod()) {
  return `<a class="group-link fund-open" data-cik="${esc(cik)}" data-period="${esc(period)}" href="${esc(entityHref('fund', cik, period))}">${fundStarHtml(starred)}${esc(name || 'Unknown')}</a>`;
}

function stockLink(cusip, ticker, issuer, period = contextPeriod()) {
  return `<a class="group-link stock-open" data-cusip="${esc(cusip)}" data-period="${esc(period)}" href="${esc(entityHref('stock', cusip, period))}">${stockNameHtml(ticker, issuer)}</a>`;
}

async function init() {
  $('#dismissError').addEventListener('click', clearError);
  try {
    state.meta = await api('/api/meta');
    clearError();
    const periods = state.meta.periods || [];
    const latest = periods[0];
    const oldest = periods[periods.length - 1];
    $('#statPeriod').textContent = `${formatQuarter(oldest?.label)} → ${formatQuarter(latest?.label)}`;
    $('#statValue').textContent = fmtMoney(state.meta.total_value);
    $('#statPositions').textContent = fmtInt.format(state.meta.holding_count);
    $('#statManagers').textContent = fmtInt.format(state.meta.distinct_managers);
    $('#statIssuers').textContent = fmtInt.format(state.meta.distinct_issuers);
    $('#sourceSummary').textContent = `SEC Form 13F · ${periods.length} quarters · ${formatPeriod(oldest?.label)} to ${formatPeriod(latest?.label)}`;
    const marketCapDate = state.meta.market_cap_retrieved_at ? new Date(state.meta.market_cap_retrieved_at) : null;
    const marketCapLabel = marketCapDate && !Number.isNaN(marketCapDate.valueOf())
      ? `${state.meta.market_cap_source || 'Market-cap'} snapshot retrieved ${fmtSnapshotDate.format(marketCapDate)}`
      : 'Market-cap snapshot unavailable';
    const discrepancyCount = Number(state.meta.data_quality_summary_discrepancy_manager_period_count || 0);
    const qualityLabel = discrepancyCount
      ? `${fmtInt.format(discrepancyCount)} manager-periods with SEC summary discrepancies excluded`
      : 'SEC summary reconciliation passed';
    $('#netDataNote').textContent = `Paired cohort: complete filings in both quarters · ${qualityLabel} · ${marketCapLabel}`;
    $('#latestPeriodOption').textContent = `Latest available — ${formatPeriod(latest?.label)}`;
    periods.slice(1).forEach(period => $('[name=period]').insertAdjacentHTML('beforeend', `<option value="${esc(period.label)}">${esc(formatPeriod(period.label))}</option>`));
    (state.meta.states || []).forEach(value => $('[name=state]').insertAdjacentHTML('beforeend', `<option>${esc(value)}</option>`));
    $('#detailPeriod').innerHTML = periods.map(period => `<option value="${esc(period.label)}">${esc(formatPeriod(period.label))}</option>`).join('');
    updateFundSignalSource(state.meta);
    wireEvents();
    state.params = formParams();
    updateFilterStatus();
    await routeFromUrl();
  } catch (error) {
    showError(error);
  }
}

function formParams() {
  const params = new URLSearchParams();
  new FormData($('#filtersForm')).forEach((value, key) => {
    if (String(value).trim()) params.set(key, value);
  });
  return params;
}

function updateFilterStatus() {
  const count = [...state.params.keys()].length;
  $('#activeFilterCount').textContent = count ? `${count} active filter${count === 1 ? '' : 's'}` : 'No filters applied';
  const selected = state.params.get('period');
  const period = selected ? formatQuarter(selected) : `Latest · ${formatQuarter(state.meta?.latest_period)}`;
  const positionLabels = {SHARES: 'Non-option positions', PUT: 'Long puts', CALL: 'Long calls'};
  const scope = positionLabels[state.params.get('put_call')] || 'All long positions';
  const parts = [period, scope];
  if (state.params.get('q')) parts.push(`Search “${state.params.get('q')}”`);
  const advancedNames = new Set(['manager', 'issuer', 'cusip', 'state', 'min_value', 'max_value']);
  const advancedCount = [...state.params.keys()].filter(key => advancedNames.has(key)).length;
  if (advancedCount) parts.push(`${advancedCount} additional`);
  $('#activeFilterSummary').textContent = parts.join(' · ');
  if ([...state.params.keys()].some(key => advancedNames.has(key))) $('#advancedFilters').open = true;
}

async function refresh() {
  state.params = formParams();
  state.page = 1;
  state.fundPage = 1;
  state.aggregatePage = 1;
  updateFilterStatus();
  await loadView(state.activePanel || state.previousTab);
}

function loadView(view) {
  if (view === 'funds') return loadFunds();
  if (view === 'overview') return loadOverview();
  if (view === 'netadds') return loadNetAdds();
  if (view === 'holdings') return loadHoldings();
  return Promise.resolve();
}

async function loadFunds() {
  const requestId = ++state.fundRequestId;
  setTableBusy('#fundsBody', 'Loading fund directory…', 11);
  $('#fundSummary').textContent = 'Loading funds…';
  const params = new URLSearchParams(state.params);
  const query = $('#fundSearch').value.trim();
  if (query) params.set('fund_q', query);
  params.set('scope', $('#fundScope').value);
  if ($('#fundStarFilter').value !== 'all') params.set('starred', $('#fundStarFilter').value);
  params.set('page', state.fundPage);
  params.set('size', state.size);
  params.set('sort', state.fundSort);
  params.set('direction', state.fundDirection);
  try {
    const data = await api('/api/funds', params);
    if (requestId !== state.fundRequestId) return;
    clearError();
    state.fundCount = data.count;
    updateFundSignalSource(data);
    const scopeLabel = $('#fundScope').value === 'research' ? 'research universe' : 'all filers';
    $('#fundSummary').textContent = `${fmtInt.format(data.count)} matching funds / managers · ${scopeLabel} · ${fmtInt.format(data.starred_count || 0)} featured`;
    $('#fundsBody').innerHTML = data.rows.length ? data.rows.map(row => `<tr>
      <td class="numeric star-cell">${fundStarHtml(row.starred, true)}</td>
      <td class="identity-column">${fundLink(row.cik, row.manager_name, row.starred)}</td>
      <td class="numeric">${fmtInt.format(row.filings)}</td><td class="numeric">${fmtInt.format(row.positions)}</td>
      <td class="numeric">${fmtInt.format(row.securities)}</td><td class="numeric"><strong>${fmtMoney(row.value)}</strong></td>
      <td class="numeric ${Number(row.signal_return) > 0 ? 'positive' : Number(row.signal_return) < 0 ? 'negative' : ''}">${fmtSignalReturn(row.signal_return)}</td>
      <td class="numeric ${Number(row.signal_pnl) > 0 ? 'positive' : Number(row.signal_pnl) < 0 ? 'negative' : ''}">${row.signal_pnl == null ? '—' : fmtSignedMoney(row.signal_pnl)}</td>
      <td class="numeric">${fmtSignalCoverage(row.signal_coverage)}${Number(row.eligible_signals) > 0 ? `<div class="cell-sub">${fmtInt.format(row.priced_signals)} / ${fmtInt.format(row.eligible_signals)} eligible</div>` : ''}</td>
      <td>${esc(row.latest_filing)}</td><td>${coverageBadge(row.coverage_status)}</td></tr>`).join('') : '<tr><td colspan="11"><div class="loading">No funds match these filters.</div></td></tr>';
    const pages = Math.max(1, Math.ceil(data.count / state.size));
    $('#fundPageInfo').textContent = `Page ${state.fundPage} of ${fmtInt.format(pages)}`;
    $('#prevFundPage').disabled = state.fundPage <= 1;
    $('#nextFundPage').disabled = state.fundPage >= pages;
  } catch (error) {
    if (requestId !== state.fundRequestId) return;
    $('#fundsBody').innerHTML = '<tr><td colspan="11"><div class="loading">Could not load funds.</div></td></tr>';
    showError(error);
  } finally {
    if (requestId === state.fundRequestId) finishTable('#fundsBody');
  }
}

function updateFundSignalSource(data = {}) {
  const metadata = data.signal_metadata || data.signal_price_metadata || {};
  const source = data.signal_price_source || data.price_source || metadata.source || metadata.price_source;
  const rawDate = data.signal_price_date || data.signal_price_as_of || data.latest_price_date ||
    data.latest_close_date || data.price_latest_date || data.price_as_of || data.prices_as_of ||
    metadata.latest_date || metadata.latest_close_date || metadata.as_of;
  const parsedDate = rawDate ? new Date(rawDate) : null;
  const date = parsedDate && !Number.isNaN(parsedDate.valueOf()) ? fmtSnapshotDate.format(parsedDate) : rawDate;
  let label = 'Price source and latest close date are unavailable.';
  if (source && date) label = `Price source: ${source}; latest close: ${date}.`;
  else if (source) label = `Price source: ${source}.`;
  else if (date) label = `Latest available close: ${date}.`;
  $('#fundSignalSource').textContent = label;
}

async function loadOverview() {
  const requestId = ++state.aggregateRequestId;
  setTableBusy('#breakdownBody', 'Loading security totals…', 4);
  $('#breakdownTitle').textContent = 'Loading security totals…';
  $('#breakdownSummary').textContent = 'Loading securities…';
  try {
    const summaryParams = new URLSearchParams(state.params);
    summaryParams.set('size', '10');
    summaryParams.set('page', '1');
    const group = 'issuer';
    const [summary, aggregate] = await Promise.all([
      api('/api/holdings', summaryParams),
      api('/api/aggregate', new URLSearchParams([...state.params,
        ['group', group], ['page', state.aggregatePage], ['size', state.size],
        ['sort', state.aggregateSort], ['direction', state.aggregateDirection]]))
    ]);
    if (requestId !== state.aggregateRequestId) return;
    clearError();
    $('#filteredValue').textContent = fmtMoney(summary.value);
    $('#filteredPositions').textContent = fmtInt.format(summary.count);
    $('#filteredManagers').textContent = fmtInt.format(summary.managers);
    $('#filteredIssuers').textContent = fmtInt.format(summary.issuers);
    state.aggregateRows = aggregate.rows;
    state.aggregateCount = aggregate.count;
    $('#breakdownTitle').textContent = 'Security totals';
    $('#breakdownSummary').textContent = `${fmtInt.format(aggregate.count)} securities · all columns sortable`;
    renderBreakdown();
    const pages = Math.max(1, Math.ceil(aggregate.count / state.size));
    $('#aggregatePageInfo').textContent = `Page ${state.aggregatePage} of ${fmtInt.format(pages)}`;
    $('#prevAggregatePage').disabled = state.aggregatePage <= 1;
    $('#nextAggregatePage').disabled = state.aggregatePage >= pages;
  } catch (error) {
    if (requestId !== state.aggregateRequestId) return;
    $('#breakdownBody').innerHTML = '<tr><td colspan="4"><div class="loading">Could not load security totals.</div></td></tr>';
    showError(error);
  } finally {
    if (requestId === state.aggregateRequestId) finishTable('#breakdownBody');
  }
}

function renderBreakdown() {
  const rows = state.aggregateRows;
  $('#breakdownBody').innerHTML = rows.length ? rows.map(row => {
    const name = stockLink(row.key, row.ticker, row.name);
    return `<tr><td class="identity-column"><div class="cell-main" title="${esc(row.name)}">${name}</div></td><td class="numeric">${fmtInt.format(row.positions)}</td><td class="numeric">${fmtInt.format(row.managers)}</td><td class="numeric"><strong>${fmtMoney(row.value)}</strong></td></tr>`;
  }).join('') : '<tr><td colspan="4"><div class="loading">No matching securities.</div></td></tr>';
}

function netMetric() {
  return $('[name=netMetric]:checked')?.value || 'value';
}

function formatNetMetric(value, metric = netMetric()) {
  if (value == null) return '—';
  if (metric === 'portfolio') return fmtWeight(value);
  if (metric === 'position') return fmtPositionChange(value);
  return fmtSignedMoney(value);
}

function netHistoryStatus(status) {
  return {
    NEW_OR_ZERO_BASE: ['New / zero base', 'A current position was reported without a positive prior reported value.'],
    NOT_HELD: ['Not held', 'No position was reported in either snapshot for this comparison.'],
    NO_REPORTED_VALUE: ['No reported value', 'Neither snapshot has a positive reported position value.'],
    EXITED: ['Exited', 'The prior reported position was fully exited.']
  }[status] || ['', ''];
}

function sortHeader(label, attribute, key, activeKey, direction, numeric = false, identity = false) {
  const active = key === activeKey;
  const ariaSort = active ? (direction === 'asc' ? 'ascending' : 'descending') : 'none';
  return `<th scope="col" class="${numeric ? 'numeric ' : ''}${identity ? 'identity-column ' : ''}${active ? 'active-sort' : ''}" data-${attribute}="${esc(key)}" aria-sort="${ariaSort}"><button class="sort-button" type="button">${esc(label)} <i aria-hidden="true">${active ? (direction === 'asc' ? '↑' : '↓') : ''}</i></button></th>`;
}

function renderNetHeader(periods, metric) {
  const metricSuffix = metric === 'portfolio' ? '(portfolio pp)' : metric === 'position' ? '(% vs prior value)' : '(reported value)';
  const releaseHeaders = [...periods].reverse().map((period, index) => sortHeader(
    `${index === 0 ? 'Latest · ' : ''}${formatPeriod(period.label)} ${metricSuffix}`,
    'net-sort', index === 0 ? 'latest' : `release_${period.id}`,
    state.netSort, state.netDirection, true
  )).join('');
  const overallLabel = `Multi-release change ${metricSuffix}`;
  $('#netHead').innerHTML = `<tr>
    ${sortHeader('Overall rank', 'net-sort', 'rank', state.netSort, state.netDirection, true)}
    ${sortHeader('Ticker / security', 'net-sort', 'issuer', state.netSort, state.netDirection, false, true)}
    ${sortHeader(overallLabel, 'net-sort', 'overall', state.netSort, state.netDirection, true)}
    ${releaseHeaders}
    ${sortHeader('Chronological trend', 'net-sort', 'trend', state.netSort, state.netDirection)}
    ${sortHeader('Funds increasing units (latest)', 'net-sort', 'adding', state.netSort, state.netDirection, true)}
    ${sortHeader('Funds decreasing units (latest)', 'net-sort', 'cutting', state.netSort, state.netDirection, true)}
    ${sortHeader('Paired funds holding (latest)', 'net-sort', 'current_funds', state.netSort, state.netDirection, true)}
    ${sortHeader('Paired-cohort value (latest)', 'net-sort', 'current_value', state.netSort, state.netDirection, true)}
    ${sortHeader('Market cap', 'net-sort', 'market_cap', state.netSort, state.netDirection, true)}
  </tr>`;
}

function miniBarChart(values, periods, metric) {
  const numbers = values.map(value => value == null ? null : Number(value));
  const maximum = Math.max(1e-12, ...numbers.filter(value => value != null).map(Math.abs));
  const width = 92;
  const height = 34;
  const baseline = 17;
  const gap = Math.min(5, width / (2 * Math.max(1, numbers.length)));
  const barWidth = Math.max(.25, Math.min(18, (width - gap * Math.max(0, numbers.length - 1)) / Math.max(1, numbers.length)));
  const usedWidth = numbers.length * barWidth + Math.max(0, numbers.length - 1) * gap;
  const start = (width - usedWidth) / 2;
  const bars = numbers.map((value, index) => {
    if (value == null) return '';
    const barHeight = value === 0 ? 1 : Math.max(2, Math.abs(value) / maximum * 14);
    const x = start + index * (barWidth + gap);
    const y = value >= 0 ? baseline - barHeight : baseline;
    const className = value > 0 ? 'positive-bar' : value < 0 ? 'negative-bar' : 'zero-bar';
    return `<rect class="${className}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}"><title>${esc(formatPeriod(periods[index]?.label))}: ${esc(formatNetMetric(value, metric))}</title></rect>`;
  }).join('');
  const description = numbers.map((value, index) => `${formatPeriod(periods[index]?.label)} ${formatNetMetric(value, metric)}`).join('; ');
  return `<svg class="mini-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Quarterly changes, oldest to newest: ${esc(description)}"><line class="axis" x1="1" y1="${baseline}" x2="${width - 1}" y2="${baseline}"></line>${bars}</svg>`;
}

async function loadNetAdds() {
  const requestId = ++state.netRequestId;
  const metric = netMetric();
  const expectedColumns = 9 + Math.max(3, state.netPeriods.length);
  setTableBusy('#netBody', 'Loading quarterly changes…', expectedColumns);
  $('#netSummary').textContent = 'Loading quarterly changes…';
  const params = new URLSearchParams({
    position: $('#netPosition').value,
    metric,
    min_activity: $('#netActivity').value || '0',
    page: state.netPage,
    size: state.size,
    sort: state.netSort,
    direction: state.netDirection
  });
  const bounds = {
    min_adding_funds: '#netMinAdding',
    max_adding_funds: '#netMaxAdding',
    min_cutting_funds: '#netMinCutting',
    max_cutting_funds: '#netMaxCutting',
    min_market_cap: '#netMinMarketCap',
    max_market_cap: '#netMaxMarketCap'
  };
  Object.entries(bounds).forEach(([name, selector]) => {
    const value = $(selector).value.trim();
    if (value) params.set(name, value);
  });
  const search = $('#netSearch').value.trim();
  if (search) params.set('stock_q', search);
  try {
    const data = await api('/api/net-adds', params);
    if (requestId !== state.netRequestId) return;
    clearError();
    state.netCount = data.count;
    const indexedPeriods = (data.periods || []).map((period, index) => ({period, index})).sort((a, b) =>
      String(a.period.period_date || a.period.label).localeCompare(String(b.period.period_date || b.period.label))
    );
    state.netPeriods = indexedPeriods.map(item => item.period);
    renderNetHeader(state.netPeriods, metric);
    const unit = metric === 'portfolio' ? 'aggregate portfolio-weight change' : metric === 'position' ? 'change relative to prior reported value' : 'reported value change (includes price movement)';
    const availability = metric === 'position' ? ' · zero-base releases shown as unavailable' : '';
    const sortedBy = state.netSort === 'latest' ? ' · sorted by latest release' : '';
    const latestCohort = state.netPeriods.at(-1)?.comparable_managers;
    const cohort = latestCohort == null ? '' : ` · latest paired cohort ${fmtInt.format(latestCohort)} managers`;
    $('#netSummary').textContent = `${fmtInt.format(data.count)} securities · ${state.netPeriods.length} paired releases${cohort} · SH-unit rows · ${unit}${sortedBy}${availability}`;
    const colspan = 9 + state.netPeriods.length;
    $('#netBody').innerHTML = data.rows.length ? data.rows.map(row => {
      const rawHistory = row.history || [];
      const rawStatuses = row.history_status || [];
      const history = indexedPeriods.map(item => rawHistory[item.index]);
      const statuses = indexedPeriods.map(item => rawStatuses[item.index]);
      const historyCells = [...history].reverse().map((value, reverseIndex) => {
        const statusIndex = statuses.length - reverseIndex - 1;
        const [statusLabel, statusTitle] = netHistoryStatus(statuses[statusIndex]);
        const status = metric === 'position' && value == null && statusLabel ? `<div class="cell-sub">${esc(statusLabel)}</div>` : '';
        const title = statusTitle ? ` title="${esc(statusTitle)}"` : '';
        return `<td class="numeric ${Number(value) > 0 ? 'positive' : Number(value) < 0 ? 'negative' : ''}"${title}>${formatNetMetric(value, metric)}${status}</td>`;
      }).join('');
      const coverage = metric === 'position' ? `<div class="cell-sub">${fmtInt.format(row.defined_releases || 0)} / ${state.netPeriods.length} releases defined</div>` : '';
      return `<tr>
        <td class="numeric"><strong>${row.net_rank == null ? '—' : fmtInt.format(row.net_rank)}</strong></td>
        <td class="identity-column">${stockLink(row.cusip, row.ticker, row.issuer, state.netPeriods.at(-1)?.label || state.meta.latest_period)}</td>
        <td class="numeric ${Number(row.overall) > 0 ? 'positive' : Number(row.overall) < 0 ? 'negative' : ''}"><strong>${formatNetMetric(row.overall, metric)}</strong>${coverage}</td>
        ${historyCells}<td>${miniBarChart(history, state.netPeriods, metric)}</td>
        <td class="numeric">${fmtInt.format(row.adding_funds)}</td><td class="numeric">${fmtInt.format(row.cutting_funds)}</td>
        <td class="numeric">${fmtInt.format(row.current_funds)}</td><td class="numeric">${fmtMoney(row.current_value)}</td>
        <td class="numeric">${fmtMarketCap(row.market_cap)}</td>
      </tr>`;
    }).join('') : `<tr><td colspan="${colspan}"><div class="loading">No securities match these ranking filters.</div></td></tr>`;
    const pages = Math.max(1, Math.ceil(data.count / state.size));
    $('#netPageInfo').textContent = `Page ${state.netPage} of ${fmtInt.format(pages)}`;
    $('#prevNetPage').disabled = state.netPage <= 1;
    $('#nextNetPage').disabled = state.netPage >= pages;
    if (state.restoreNetSortFocus) {
      state.restoreNetSortFocus = false;
      requestAnimationFrame(() => $(`#netHead [data-net-sort="${CSS.escape(state.netSort)}"] .sort-button`)?.focus());
    }
  } catch (error) {
    if (requestId !== state.netRequestId) return;
    $('#netBody').innerHTML = `<tr><td colspan="${expectedColumns}"><div class="loading">Could not load quarterly changes.</div></td></tr>`;
    showError(error);
  } finally {
    if (requestId === state.netRequestId) finishTable('#netBody');
  }
}

async function loadHoldings() {
  const requestId = ++state.holdingsRequestId;
  setTableBusy('#holdingsBody', 'Loading positions…', 11);
  $('#rowSummary').textContent = 'Loading positions…';
  const params = new URLSearchParams(state.params);
  params.set('page', state.page);
  params.set('size', state.size);
  params.set('sort', state.sort);
  try {
    const data = await api('/api/holdings', params);
    if (requestId !== state.holdingsRequestId) return;
    clearError();
    state.count = data.count;
    $('#rowSummary').textContent = `${fmtInt.format(data.count)} effective positions · ${fmtMoney(data.value)}`;
    $('#holdingsBody').innerHTML = data.rows.length ? data.rows.map(rowHtml).join('') : '<tr><td colspan="11"><div class="loading">No positions match these filters.</div></td></tr>';
    const pages = Math.max(1, Math.ceil(data.count / state.size));
    $('#pageInfo').textContent = `Page ${state.page} of ${fmtInt.format(pages)}`;
    $('#prevPage').disabled = state.page <= 1;
    $('#nextPage').disabled = state.page >= pages;
  } catch (error) {
    if (requestId !== state.holdingsRequestId) return;
    $('#holdingsBody').innerHTML = '<tr><td colspan="11"><div class="loading">Could not load positions.</div></td></tr>';
    showError(error);
  } finally {
    if (requestId === state.holdingsRequestId) finishTable('#holdingsBody');
  }
}

function rowHtml(row) {
  const filing = secFilingHref(row.cik, row.accession);
  const positionType = String(row.put_call || '').toUpperCase();
  const positionClass = POSITION_CLASSES.get(positionType);
  const tag = positionClass
    ? `<span class="badge ${positionClass}">LONG ${esc(positionType)}</span>`
    : positionType ? '<span class="badge">POSITION TYPE UNAVAILABLE</span>' : '<span class="badge">SHARES / OTHER</span>';
  const filingLabel = esc(row.submission_type || 'SEC filing');
  const filingHtml = filing
    ? `<a href="${esc(filing)}" target="_blank" rel="noopener noreferrer">${filingLabel}<span class="sr-only"> (opens in a new tab)</span></a>`
    : `<span>${filingLabel}</span><span class="sr-only">; filing link unavailable</span>`;
  return `<tr><td class="identity-column">${stockLink(row.cusip, row.ticker, row.issuer, row.period)}</td><td>${esc(row.class)}</td><td>${esc(row.cusip)}</td>
    <td>${fundLink(row.cik, row.manager_name, row.starred, row.period)}</td><td>${esc(row.cik)}</td><td>${esc(formatPeriod(row.period))}</td><td>${tag}</td>
    <td class="numeric">${row.shares == null ? '—' : fmtInt.format(row.shares)}<div class="cell-sub">${esc(row.shares_type)}</div></td><td class="numeric"><strong>${fmtMoney(row.value)}</strong></td><td>${esc(row.filing_date)}</td>
    <td>${coverageBadge(row.coverage_status)}<div class="cell-sub">${filingHtml}</div></td></tr>`;
}

function secFilingHref(cik, accession) {
  const cikValue = String(cik ?? '').trim();
  const accessionValue = String(accession ?? '').trim();
  if (!/^\d{1,10}$/.test(cikValue) || !/^\d{10}-\d{2}-\d{6}$/.test(accessionValue)) return null;
  const cikPath = cikValue.replace(/^0+/, '');
  if (!cikPath) return null;
  const accessionPath = accessionValue.replaceAll('-', '');
  return `https://www.sec.gov/Archives/edgar/data/${cikPath}/${accessionPath}/${accessionValue}-index.html`;
}

function coverageBadge(value) {
  const normalized = String(value || 'MISSING').toUpperCase();
  const safeValue = COVERAGE_CLASSES.has(normalized) ? normalized : 'UNKNOWN';
  const safeClass = COVERAGE_CLASSES.get(normalized) || 'unknown';
  return `<span class="coverage coverage-${safeClass}">${esc(safeValue)}</span>`;
}

function statusBadge(value) {
  const labels = {INCREASED: 'INCREASED', REDUCED: 'REDUCED', NEW: 'NEW', EXITED: 'EXITED', UNCHANGED: 'UNCHANGED', NOT_COMPARABLE: 'NOT COMPARABLE'};
  const normalized = String(value || '').toUpperCase();
  const safeClass = CHANGE_CLASSES.get(normalized) || 'unknown';
  return `<span class="change-badge change-${safeClass}">${labels[normalized] || 'UNKNOWN'}</span>`;
}

function positionLabel(type, unit) {
  const position = Number(type) === 1 ? 'LONG PUT' : Number(type) === 2 ? 'LONG CALL' : 'SHARES / OTHER';
  const unitLabel = unit == null ? '' : ` · ${Number(unit) === 0 ? 'SH' : Number(unit) === 1 ? 'PRN' : 'OTHER'}`;
  return `${position}${unitLabel}`;
}

function showPanel(id) {
  const detail = id === 'detail';
  const funds = id === 'funds';
  const securities = SECURITY_MODES.has(id);
  for (const [selector, active] of [['#funds', funds], ['#securities', securities], ['#detail', detail]]) {
    const panel = $(selector);
    panel.hidden = !active;
    panel.classList.toggle('active', active);
  }
  $$('[data-primary-tab]').forEach(tab => {
    const active = tab.dataset.primaryTab === (funds ? 'funds' : securities ? 'securities' : '');
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  $$('.security-mode-panel').forEach(panel => {
    const active = securities && panel.id === id;
    panel.hidden = !active;
    panel.classList.toggle('active', active);
  });
  $$('[data-mode]').forEach(tab => {
    const active = securities && tab.dataset.mode === id;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  $('.view-navigation').hidden = detail;
  $('#sharedFilters').hidden = id === 'netadds' || detail;
}

async function activateView(id, push = true) {
  if (!(id in VIEW_ROUTES)) id = 'funds';
  state.detailRequestId += 1;
  state.detail = null;
  state.activePanel = id;
  if (SECURITY_MODES.has(id)) state.securityMode = id;
  state.previousTab = id;
  showPanel(id);
  if (push) {
    const target = `?view=${encodeURIComponent(VIEW_ROUTES[id])}`;
    const current = `${location.search}${location.hash}`;
    if (current !== target) history.pushState({view: id}, '', target);
  }
  document.title = `13F Explorer — ${VIEW_LABELS[id]}`;
  await loadView(id);
}

function openFund(cik, push = true, period = null) { return openDetail('fund', cik, push, period); }
function openStock(cusip, push = true, period = null) { return openDetail('stock', cusip, push, period); }

function openDetail(type, id, push = true, period = null) {
  const returnView = history.state?.returnView;
  state.previousTab = push ? state.activePanel : (Object.hasOwn(VIEW_ROUTES, returnView) ? returnView : (type === 'stock' ? 'overview' : 'funds'));
  if (push) state.returnFocus = document.activeElement;
  state.detail = {type, id};
  state.detailPage = 1;
  state.detailSort = 'current_value';
  state.detailDirection = 'desc';
  state.detailNeedsFocus = true;
  $('#detailSearch').value = '';
  $('#detailChange').value = '';
  $('#detailPosition').value = '';
  const requestedPeriod = period || selectedPeriod();
  const chosen = [...$('#detailPeriod').options].some(option => option.value === requestedPeriod)
    ? requestedPeriod : state.meta.latest_period;
  $('#detailPeriod').value = chosen;
  showPanel('detail');
  if (push) history.pushState({detail: true, returnView: state.previousTab}, '', entityHref(type, id, chosen));
  loadDetail();
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  window.scrollTo({top: $('#detail').offsetTop, behavior: reduceMotion ? 'auto' : 'smooth'});
}

async function loadDetail() {
  if (!state.detail) return;
  const requestId = ++state.detailRequestId;
  setTableBusy('#detailBody', 'Loading quarter comparison…', 13);
  $('#detailSummary').textContent = 'Loading changes…';
  const {type, id} = state.detail;
  const params = new URLSearchParams({
    period: $('#detailPeriod').value,
    page: state.detailPage,
    size: state.size,
    sort: state.detailSort,
    direction: state.detailDirection
  });
  params.set(type === 'stock' ? 'cusip' : 'cik', id);
  const search = $('#detailSearch').value.trim();
  if (search) params.set(type === 'stock' ? 'fund_q' : 'security_q', search);
  if ($('#detailChange').value) params.set('change', $('#detailChange').value);
  if ($('#detailPosition').value) params.set('position', $('#detailPosition').value);
  try {
    const data = await api(`/api/${type}-detail`, params);
    if (requestId !== state.detailRequestId) return;
    clearError();
    state.detailCount = data.count;
    renderDetail(data);
  } catch (error) {
    if (requestId !== state.detailRequestId) return;
    $('#detailBody').innerHTML = '<tr><td colspan="13"><div class="loading">Could not load this comparison.</div></td></tr>';
    showError(error);
  } finally {
    if (requestId === state.detailRequestId) finishTable('#detailBody');
  }
}

function renderDetail(data) {
  const isStock = state.detail.type === 'stock';
  const entity = isStock ? data.security : data.manager;
  $('#detailEyebrow').textContent = isStock ? 'Security holders' : (entity.starred ? 'Featured research set · fund positions' : 'Fund positions');
  const title = isStock && entity.ticker ? `${entity.ticker} · ${entity.issuer}` : (isStock ? entity.issuer : entity.name);
  $('#detailTitle').innerHTML = !isStock && entity.starred ? `${fundStarHtml(true)}${esc(title)}` : esc(title);
  $('#detailSubtitle').textContent = isStock ? `${entity.class} · CUSIP ${entity.cusip}` : `CIK ${entity.cik} · ${entity.state_country || 'Location unavailable'}`;
  $('#detailSearchLabel').textContent = isStock ? 'Find fund or CIK' : 'Find ticker, security, or CUSIP';
  $('#detailSearch').placeholder = isStock ? 'Filter managers' : 'Filter securities';
  $('#detailAddedLabel').textContent = isStock ? 'Funds increasing units / new' : 'Positions increased / new';
  $('#detailReducedLabel').textContent = isStock ? 'Funds decreasing units / exited' : 'Positions reduced / exited';
  const history = data.history || [];
  const currentHistory = history.find(row => row.period === data.current_period.label) || {};
  $('#detailValue').textContent = fmtMoney(currentHistory.value || 0);
  const summary = data.summary || {};
  const added = isStock && summary.added_or_new != null
    ? Number(summary.added_or_new) : Number(summary.increased || 0) + Number(summary.new || 0);
  const reduced = isStock && summary.reduced_or_exited != null
    ? Number(summary.reduced_or_exited) : Number(summary.reduced || 0) + Number(summary.exited || 0);
  const hasPrior = Boolean(data.previous_period);
  const comparisonAvailable = hasPrior && (isStock || (data.current_coverage === 'COMPLETE' && data.previous_coverage === 'COMPLETE'));
  $('#detailAdded').textContent = comparisonAvailable ? fmtInt.format(added) : '—';
  $('#detailReduced').textContent = comparisonAvailable ? fmtInt.format(reduced) : '—';
  if (!data.previous_period) {
    $('#detailComparison').textContent = 'No prior quarter';
  } else if (isStock) {
    $('#detailComparison').textContent = `${formatPeriod(data.previous_period.label)} → ${formatPeriod(data.current_period.label)}`;
  } else {
    $('#detailComparison').innerHTML = `<span class="coverage-inline"><span>Prior ${esc(formatPeriod(data.previous_period.label))}</span>${coverageBadge(data.previous_coverage)}<span aria-hidden="true">→</span><span>Current ${esc(formatPeriod(data.current_period.label))}</span>${coverageBadge(data.current_coverage)}</span>`;
  }
  $('#detailSummary').classList.toggle('comparison-warning', !comparisonAvailable);
  const excludedManagers = isStock ? Number(summary.not_comparable || 0) : 0;
  if (!hasPrior) {
    $('#detailSummary').textContent = 'No earlier quarter is available for comparison; the historical snapshot remains browseable.';
  } else if (comparisonAvailable) {
    $('#detailSummary').textContent = `${fmtInt.format(data.count)} reported-position comparisons${excludedManagers ? ` · ${fmtInt.format(excludedManagers)} managers excluded from directional counts for incomplete coverage` : ''}`;
  } else {
    $('#detailSummary').textContent = `Quarter comparison unavailable because one or both filings are not complete; ${fmtInt.format(data.count)} reported-position rows remain browseable.`;
  }
  renderHistory(history, isStock);
  renderDetailTable(data.rows, isStock);
  const pages = Math.max(1, Math.ceil(data.count / state.size));
  $('#detailPageInfo').textContent = `Page ${state.detailPage} of ${fmtInt.format(pages)}`;
  $('#prevDetailPage').disabled = state.detailPage <= 1;
  $('#nextDetailPage').disabled = state.detailPage >= pages;
  document.title = `13F Explorer — ${$('#detailTitle').textContent}`;
  if (state.detailNeedsFocus) {
    state.detailNeedsFocus = false;
    requestAnimationFrame(() => $('#detailTitle').focus());
  } else if (state.restoreDetailSortFocus) {
    state.restoreDetailSortFocus = false;
    requestAnimationFrame(() => $(`#detailHead [data-detail-sort="${CSS.escape(state.detailSort)}"] .sort-button`)?.focus());
  }
}

function renderHistory(rows, isStock) {
  $('#detailHistoryMetric').textContent = isStock ? 'Reporting funds' : 'Coverage / positions';
  $('#detailHistoryCaption').textContent = isStock
    ? 'Reported security value and reporting fund count by quarter'
    : 'Reported fund value, filing coverage, and position count by quarter';
  $('#detailHistory').innerHTML = rows.length ? rows.map(row => {
    const metric = isStock
      ? `<span class="numeric-value">${fmtInt.format(row.funds || 0)}</span>`
      : `<span class="coverage-inline">${coverageBadge(row.coverage_status)}<span>${fmtInt.format(row.positions || 0)} positions</span></span>`;
    return `<tr><td>${esc(formatPeriod(row.period))}</td><td class="numeric"><strong>${fmtMoney(row.value)}</strong></td><td class="${isStock ? 'numeric' : ''}">${metric}</td></tr>`;
  }).join('') : '<tr><td colspan="3">No history available.</td></tr>';
}

function detailHeader(isStock) {
  const columns = isStock
    ? [['manager', 'Fund / manager'], ['cik', 'CIK'], ['position', 'Position'], ['current_shares', 'Current units'], ['previous_shares', 'Prior units'], ['delta', 'Unit change'], ['percent', 'Unit change %'], ['current_value', 'Current value'], ['previous_value', 'Prior value'], ['current_weight', 'Current portfolio weight'], ['previous_weight', 'Prior portfolio weight'], ['weight_change', 'Weight change'], ['status', 'Status']]
    : [['issuer', 'Ticker / issuer'], ['cusip', 'CUSIP'], ['position', 'Position'], ['current_shares', 'Current units'], ['previous_shares', 'Prior units'], ['delta', 'Unit change'], ['percent', 'Unit change %'], ['current_value', 'Current value'], ['previous_value', 'Prior value'], ['current_weight', 'Current portfolio weight'], ['previous_weight', 'Prior portfolio weight'], ['weight_change', 'Weight change'], ['status', 'Status']];
  const numeric = new Set(['current_shares', 'previous_shares', 'delta', 'percent', 'current_value', 'previous_value', 'current_weight', 'previous_weight', 'weight_change']);
  return `<tr>${columns.map(([key, label], index) => sortHeader(label, 'detail-sort', key, state.detailSort, state.detailDirection, numeric.has(key), index === 0)).join('')}</tr>`;
}

function renderDetailTable(rows, isStock) {
  $('#detailHead').innerHTML = detailHeader(isStock);
  const period = $('#detailPeriod').value;
  $('#detailBody').innerHTML = rows.length ? rows.map(row => {
    const identity = isStock ? fundLink(row.cik, row.manager_name, row.starred, period) : stockLink(row.cusip, row.ticker, row.issuer, period);
    const coverage = isStock
      ? `<div class="cell-sub coverage-inline"><span>Coverage:</span><span>current</span>${coverageBadge(row.current_coverage)}<span>prior</span>${coverageBadge(row.previous_coverage)}</div>`
      : '';
    return `<tr>
    <td class="identity-column">${identity}${coverage}</td>
    <td>${esc(isStock ? row.cik : row.cusip)}</td><td>${positionLabel(row.position_type, row.shares_type)}</td>
    <td class="numeric">${row.current_shares == null ? '—' : fmtInt.format(row.current_shares)}</td><td class="numeric">${row.previous_shares == null ? '—' : fmtInt.format(row.previous_shares)}</td>
    <td class="numeric ${Number(row.delta_shares) > 0 ? 'positive' : Number(row.delta_shares) < 0 ? 'negative' : ''}">${fmtSigned(row.delta_shares)}</td><td class="numeric">${fmtPct(row.delta_percent)}</td>
    <td class="numeric">${fmtMoney(row.current_value)}</td><td class="numeric">${fmtMoney(row.previous_value)}</td>
    <td class="numeric">${fmtPortfolioWeight(row.current_weight)}</td><td class="numeric">${fmtPortfolioWeight(row.previous_weight)}</td>
    <td class="numeric ${Number(row.weight_change) > 0 ? 'positive' : Number(row.weight_change) < 0 ? 'negative' : ''}">${fmtWeight(row.weight_change)}</td>
    <td>${statusBadge(row.status)}</td></tr>`;
  }).join('') : '<tr><td colspan="13"><div class="loading">No changes match these filters.</div></td></tr>';
}

function markSortHeaders(selector, dataKey, activeField, direction) {
  $$(selector).forEach(header => {
    const active = header.dataset[dataKey] === activeField;
    header.classList.toggle('active-sort', active);
    header.setAttribute('aria-sort', active ? (direction === 'asc' ? 'ascending' : 'descending') : 'none');
    const indicator = header.querySelector('i');
    if (indicator) indicator.textContent = active ? (direction === 'asc' ? '↑' : '↓') : '';
  });
}

function nextSortDirection(currentField, currentDirection, nextField) {
  if (currentField === nextField) return currentDirection === 'desc' ? 'asc' : 'desc';
  return new Set(['name', 'issuer', 'cusip', 'cik', 'state', 'latest', 'coverage', 'rank', 'period', 'position', 'form']).has(nextField) ? 'asc' : 'desc';
}

function handleSort(header) {
  if (header.dataset.sort) {
    const field = header.dataset.sort;
    const current = state.sort.replace(/_(asc|desc)$/, '');
    const direction = nextSortDirection(current, state.sort.endsWith('_asc') ? 'asc' : 'desc', field);
    state.sort = `${field}_${direction}`;
    state.page = 1;
    markSortHeaders('[data-sort]', 'sort', field, direction);
    loadHoldings();
    return;
  }
  if (header.dataset.aggSort) {
    const field = header.dataset.aggSort;
    state.aggregateDirection = nextSortDirection(state.aggregateSort, state.aggregateDirection, field);
    state.aggregateSort = field;
    state.aggregatePage = 1;
    markSortHeaders('[data-agg-sort]', 'aggSort', field, state.aggregateDirection);
    loadOverview();
    return;
  }
  if (header.dataset.fundSort) {
    const field = header.dataset.fundSort;
    state.fundDirection = nextSortDirection(state.fundSort, state.fundDirection, field);
    state.fundSort = field;
    state.fundPage = 1;
    markSortHeaders('[data-fund-sort]', 'fundSort', field, state.fundDirection);
    loadFunds();
    return;
  }
  if (header.dataset.netSort) {
    const field = header.dataset.netSort;
    state.netDirection = state.netSort !== field && field === 'latest'
      ? 'desc' : nextSortDirection(state.netSort, state.netDirection, field);
    state.netSort = field;
    state.netPage = 1;
    state.restoreNetSortFocus = true;
    loadNetAdds();
    return;
  }
  if (header.dataset.detailSort) {
    const field = header.dataset.detailSort;
    state.detailDirection = nextSortDirection(state.detailSort, state.detailDirection, field);
    state.detailSort = field;
    state.detailPage = 1;
    state.restoreDetailSortFocus = true;
    loadDetail();
  }
}

function debounce(callback, wait = 250) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), wait);
  };
}

async function suggest(kind, value) {
  if (value.length < 2) return;
  try {
    const rows = await api('/api/suggest', new URLSearchParams({kind, q: value}));
    $(`#${kind}Suggestions`).innerHTML = rows.map(row => `<option value="${esc(row.name)}">${esc(row.key)}</option>`).join('');
  } catch (_) {
    // Suggestions are optional; the primary search still works if they fail.
  }
}

async function routeFromUrl() {
  const url = new URL(location.href);
  if (url.searchParams.get('stock')) {
    openStock(url.searchParams.get('stock'), false, url.searchParams.get('period'));
    return;
  }
  if (url.searchParams.get('fund')) {
    openFund(url.searchParams.get('fund'), false, url.searchParams.get('period'));
    return;
  }
  const panel = ROUTE_VIEWS[url.searchParams.get('view')] || 'funds';
  await activateView(panel, false);
  if (state.returnFocus?.isConnected) {
    const target = state.returnFocus;
    state.returnFocus = null;
    requestAnimationFrame(() => target.focus());
  }
}

function wireManualTabKeyboard(containerSelector, tabSelector) {
  $(containerSelector).addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const tabs = $$(tabSelector);
    const current = Math.max(0, tabs.indexOf(document.activeElement));
    let next = current;
    if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
    if (event.key === 'ArrowRight') next = (current + 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    event.preventDefault();
    tabs[next].focus();
  });
}

function wireEvents() {
  $('#filtersForm').addEventListener('submit', event => { event.preventDefault(); refresh(); });
  $('#reset').addEventListener('click', () => {
    $('#filtersForm').reset();
    $('#fundSearch').value = '';
    $('#fundScope').value = 'research';
    $('#fundStarFilter').value = 'all';
    $('#advancedFilters').open = false;
    refresh();
  });
  $('#prevFundPage').addEventListener('click', () => { if (state.fundPage > 1) { state.fundPage--; loadFunds(); } });
  $('#nextFundPage').addEventListener('click', () => { if (state.fundPage * state.size < state.fundCount) { state.fundPage++; loadFunds(); } });
  $('#prevAggregatePage').addEventListener('click', () => { if (state.aggregatePage > 1) { state.aggregatePage--; loadOverview(); } });
  $('#nextAggregatePage').addEventListener('click', () => { if (state.aggregatePage * state.size < state.aggregateCount) { state.aggregatePage++; loadOverview(); } });
  $('#prevPage').addEventListener('click', () => { if (state.page > 1) { state.page--; loadHoldings(); } });
  $('#nextPage').addEventListener('click', () => { if (state.page * state.size < state.count) { state.page++; loadHoldings(); } });
  $('#prevNetPage').addEventListener('click', () => { if (state.netPage > 1) { state.netPage--; loadNetAdds(); } });
  $('#nextNetPage').addEventListener('click', () => { if (state.netPage * state.size < state.netCount) { state.netPage++; loadNetAdds(); } });
  $('#prevDetailPage').addEventListener('click', () => { if (state.detailPage > 1) { state.detailPage--; loadDetail(); } });
  $('#nextDetailPage').addEventListener('click', () => { if (state.detailPage * state.size < state.detailCount) { state.detailPage++; loadDetail(); } });

  $('#fundSearch').addEventListener('input', debounce(() => { state.fundPage = 1; loadFunds(); }, 300));
  $('#fundScope').addEventListener('change', () => { state.fundPage = 1; loadFunds(); });
  $('#fundStarFilter').addEventListener('change', () => { state.fundPage = 1; loadFunds(); });
  $('#netSearch').addEventListener('input', debounce(() => { state.netPage = 1; loadNetAdds(); }, 300));
  ['#netActivity', '#netMinAdding', '#netMaxAdding', '#netMinCutting', '#netMaxCutting', '#netMinMarketCap', '#netMaxMarketCap']
    .forEach(selector => $(selector).addEventListener('input', debounce(() => { state.netPage = 1; loadNetAdds(); }, 300)));
  $('#netPosition').addEventListener('change', () => { state.netPage = 1; state.netSort = 'latest'; state.netDirection = 'desc'; loadNetAdds(); });
  $$('[name=netMetric]').forEach(input => input.addEventListener('change', () => { state.netPage = 1; state.netSort = 'latest'; state.netDirection = 'desc'; loadNetAdds(); }));
  $('#resetNet').addEventListener('click', () => {
    $('[name=netMetric][value=value]').checked = true;
    $('#netPosition').value = 'SHARES';
    $('#netSearch').value = '';
    $('#netActivity').value = '0';
    ['#netMinAdding', '#netMaxAdding', '#netMinCutting', '#netMaxCutting', '#netMinMarketCap', '#netMaxMarketCap']
      .forEach(selector => { $(selector).value = ''; });
    state.netPage = 1;
    state.netSort = 'latest';
    state.netDirection = 'desc';
    loadNetAdds();
  });

  $('#detailSearch').addEventListener('input', debounce(() => { state.detailPage = 1; loadDetail(); }, 300));
  $('#detailChange').addEventListener('change', () => { state.detailPage = 1; loadDetail(); });
  $('#detailPosition').addEventListener('change', () => { state.detailPage = 1; loadDetail(); });
  $('#detailPeriod').addEventListener('change', () => {
    state.detailPage = 1;
    const url = new URL(location.href);
    url.searchParams.set('period', $('#detailPeriod').value);
    history.replaceState(history.state, '', `${url.search}${url.hash}`);
    loadDetail();
  });
  $('#detailBack').addEventListener('click', () => {
    if (history.state?.detail) history.back();
    else {
      const panel = state.previousTab || 'funds';
      history.replaceState({view: panel}, '', `?view=${VIEW_ROUTES[panel]}`);
      activateView(panel, false);
      requestAnimationFrame(() => $('#tab-' + panel).focus());
    }
  });

  $('#tab-funds').addEventListener('click', () => activateView('funds'));
  $('#tab-securities').addEventListener('click', () => activateView(state.securityMode));
  $$('[data-mode]').forEach(tab => tab.addEventListener('click', () => activateView(tab.dataset.mode)));
  wireManualTabKeyboard('.primary-tabs', '[data-primary-tab]');
  wireManualTabKeyboard('.workspace-tabs', '[data-mode]');

  document.addEventListener('click', event => {
    const sortButton = event.target.closest('.sort-button');
    if (sortButton) {
      handleSort(sortButton.closest('th'));
      return;
    }
    const fund = event.target.closest('.fund-open');
    const stock = event.target.closest('.stock-open');
    if (!fund && !stock) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    if (fund) openFund(fund.dataset.cik, true, fund.dataset.period || null);
    else openStock(stock.dataset.cusip, true, stock.dataset.period || null);
  });

  $('[name=manager]').addEventListener('input', debounce(event => suggest('manager', event.target.value)));
  $('[name=issuer]').addEventListener('input', debounce(event => suggest('issuer', event.target.value)));
  window.addEventListener('popstate', routeFromUrl);
}

init();
