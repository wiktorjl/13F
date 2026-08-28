'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const PAGE_SIZE = 100;
const MAX_PAGE = 100000;
// Public prefix the app is served under (e.g. "/13f" behind a reverse proxy): derived from this script's own URL
// so one build works at any mount point. Every history URL and API request carries it.
const BASE_PATH = new URL(document.currentScript?.src ?? '/', location.href).pathname.replace(/\/[^/]*$/, '');
// The dashboard is the landing page: holdings at the root, the other views one segment below it. The root
// view's URL is BASE_PATH + '/' ('/' when unprefixed). About is a static page: it carries no query state.
const VIEW_ROUTES = {holdings: '', initiations: '/initiations', movers: '/movers', about: '/about'};
// Pre-landing-page URLs still resolve (the server serves them too) but are never emitted.
const LEGACY_ROUTE_VIEWS = {'/dashboard': 'holdings', '/dashboard/initiations': 'initiations', '/dashboard/movers': 'movers'};
const ROUTE_VIEWS = {
  ...Object.fromEntries(Object.entries(VIEW_ROUTES).map(([view, path]) => [path || '/', view])),
  ...LEGACY_ROUTE_VIEWS
};
const VIEW_PATHS = Object.fromEntries(Object.entries(VIEW_ROUTES).map(([view, path]) => [view, BASE_PATH + (path || '/')]));
// Securities without a ticker are hidden unless the URL says ?unmapped=include (a documented switch, no control).
const UNMAPPED_VALUES = new Set(['exclude', 'include']);
const VIEW_TITLES = {holdings: 'Top Holdings', initiations: 'Fresh Initiations', movers: 'Top Movers', about: 'About'};
const SIDES = new Set(['gainers', 'losers']);
// Sort column -> the direction selected when the column is first clicked. The metric column starts at the view's
// own default (viewDirection), which is also the direction the URL and the API assume when none is given.
const SORT_DEFAULTS = new Map([
  ['metric', null], ['ticker', 'asc'], ['name', 'asc'], ['price', 'desc'], ['day', 'desc'], ['ytd', 'desc'],
  ['sector', 'asc']
]);
const DIRECTION_VALUES = new Set(['asc', 'desc']);
const SORT_GLYPHS = new Map([['asc', '↑'], ['desc', '↓']]);
const METRIC_LABELS = {holdings: 'Avg Weight', initiations: 'New Holders', movers: 'Weight Change'};
const DIRECTIONS = new Map([['up', ['▲', 'Up']], ['down', ['▼', 'Down']], ['flat', ['—', 'Unchanged']]]);
const SECTOR_ABBREVIATIONS = new Map([
  ['Technology', 'Tech'], ['Health Care', 'Hlth'], ['Finance', 'Fin'], ['Consumer Discretionary', 'Disc'],
  ['Consumer Staples', 'Stpl'], ['Industrials', 'Indu'], ['Energy', 'Enrg'], ['Utilities', 'Util'],
  ['Real Estate', 'RE'], ['Basic Materials', 'Matl'], ['Telecommunications', 'Tele'], ['Miscellaneous', 'Misc'],
  ['ETF', 'ETF']
]);
const NAME_TOKENS = new Map([
  ['INC', 'Inc'], ['CO', 'Co'], ['LP', 'LP'], ['LLC', 'LLC'], ['PLC', 'PLC'], ['SA', 'SA'], ['NV', 'NV'], ['AG', 'AG'],
  ['SE', 'SE'], ['ETF', 'ETF'], ['ADR', 'ADR'], ['ADS', 'ADS'], ['USA', 'USA'], ['US', 'US'], ['AB', 'AB'], ['ASA', 'ASA'],
  ['SPA', 'SpA'], ['NA', 'NA'], ['DE', 'DE'], ['DEL', 'Del'], ['TR', 'Tr'], ['FD', 'Fd'], ['FDS', 'Fds']
]);

// `meta` caches the one /api/meta response the About page needs (null until it has arrived successfully).
const state = {view: 'holdings', horizon: 1, side: 'gainers', sort: 'metric', direction: 'desc', page: 1, unmapped: 'exclude', count: 0, requestId: 0, meta: null};

const fmtInt = new Intl.NumberFormat('en-US', {maximumFractionDigits: 0});
const MONTHS = new Map([
  ['JAN', 'Jan'], ['FEB', 'Feb'], ['MAR', 'Mar'], ['APR', 'Apr'], ['MAY', 'May'], ['JUN', 'Jun'], ['JUL', 'Jul'],
  ['AUG', 'Aug'], ['SEP', 'Sep'], ['OCT', 'Oct'], ['NOV', 'Nov'], ['DEC', 'Dec']
]);
const fmtMoney = new Intl.NumberFormat('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[character]));
const toNumber = (value) => {
  if (value == null || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};
// Round to the digits actually displayed so sign and colour never disagree with the printed digits
// (-0.04 -> -0, which signOf/changeClass treat as unsigned/neutral).
const roundTo = (value, digits) => Number(value.toFixed(digits));
const signOf = (value) => value > 0 ? '+' : value < 0 ? '−' : '';
const fmtWeight = (value) => value == null ? '—' : `${Math.abs(value) >= 1 ? value.toFixed(1) : value.toFixed(2)}%`;
const fmtNewHolders = (value) => value == null ? '—' : `${fmtInt.format(value)} new`;
// Movers: the sign follows the server's direction (losers are selected on the unrounded change and can
// arrive as -0.0, which `value < 0` would print as '+'); the value alone decides only when no direction is given.
const fmtWeightChange = (value, direction) => {
  if (value == null) return '—';
  const negative = direction === 'down' || (direction !== 'up' && (value < 0 || Object.is(value, -0)));
  return `${negative ? '−' : '+'}${Math.abs(value) >= 1 ? Math.abs(value).toFixed(1) : Math.abs(value).toFixed(2)}pp`;
};
const fmtPrice = (value) => value == null ? '—' : `$${fmtMoney.format(value)}`;
const fmtPct = (value) => {
  if (value == null) return '—';
  const rounded = roundTo(value, 1);
  return `${signOf(rounded)}${Math.abs(rounded).toFixed(1)}%`;
};
const changeClass = (value) => value > 0 ? 'positive' : value < 0 ? 'negative' : 'neutral';
const displayPct = (value) => {
  const number = toNumber(value);
  return number == null ? null : roundTo(number, 1);
};

function fmtMetric(view, value, direction) {
  const number = toNumber(value);
  if (view === 'initiations') return fmtNewHolders(number);
  if (view === 'movers') return fmtWeightChange(number, direction);
  return fmtWeight(number);
}

function sectorAbbreviation(sector) {
  const text = String(sector ?? '').trim();
  if (!text) return '—';
  return SECTOR_ABBREVIATIONS.get(text) || Array.from(text).slice(0, 4).join('');
}

function titleCaseToken(token) {
  const match = /^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$/.exec(token);
  const fixed = match ? NAME_TOKENS.get(match[2].toUpperCase()) : null;
  if (fixed) return `${match[1]}${fixed}${match[3]}`;
  return token.toLowerCase().replace(/[a-z][a-z']*/g, run => run[0].toUpperCase() + run.slice(1));
}

function displayName(name, issuer) {
  const raw = String(name || issuer || '').trim();
  if (!raw) return '—';
  if (/[a-z]/.test(raw)) return raw;
  return raw.split(/\s+/).map(token => token === '&' ? token : titleCaseToken(token)).join(' ');
}

const yahooUrl = (ticker) => `https://finance.yahoo.com/quote/${encodeURIComponent(ticker.replace(/\//g, '-'))}`;

function rowHtml(row, view) {
  const ticker = String(row.ticker ?? '').trim();
  const direction = DIRECTIONS.has(row.direction) ? row.direction : 'flat';
  const [glyph, label] = DIRECTIONS.get(direction);
  const name = displayName(row.name, row.issuer);
  const href = ticker ? esc(yahooUrl(ticker)) : '';
  const price = toNumber(row.price);
  const day = displayPct(row.day_change);
  const ytd = displayPct(row.ytd_change);
  // `unpriced` marks the "—" cells so the phone layout can drop its " today" / " YTD" suffixes.
  const unpriced = (value) => value == null ? ' unpriced' : '';
  // One DOM for both layouts: the cells are grouped into three lines for phones; on desktop the lines are
  // display: contents and every cell is placed in its own grid column by class, so this order is irrelevant there.
  return `<div class="dash-row" role="row" data-cusip="${esc(row.cusip)}">
    <div class="dash-line dash-line-1" role="presentation">
      ${ticker ? `<span class="dash-cell dash-ticker" role="cell"><a href="${href}" target="_blank" rel="noreferrer">${esc(ticker)}</a></span>` : '<span class="dash-cell dash-ticker dash-missing" role="cell">—</span>'}
      <span class="dash-cell dash-direction ${direction}" role="cell" aria-label="${label}">${glyph}</span>
      <span class="dash-cell dash-metric" role="cell">${esc(fmtMetric(view, row.metric, direction))}</span>
    </div>
    <div class="dash-line dash-line-2" role="presentation">
      <span class="dash-cell dash-name" role="cell">${ticker ? `<a href="${href}" target="_blank" rel="noreferrer">${esc(name)}</a>` : esc(name)}</span>
      <span class="dash-cell dash-sector" role="cell">${esc(sectorAbbreviation(row.sector))}</span>
    </div>
    <div class="dash-line dash-line-3" role="presentation">
      <span class="dash-cell dash-price${unpriced(price)}" role="cell">${esc(fmtPrice(price))}</span>
      <span class="dash-cell dash-day ${changeClass(day)}${unpriced(day)}" role="cell">${esc(fmtPct(day))}</span>
      <span class="dash-cell dash-ytd ${changeClass(ytd)}${unpriced(ytd)}" role="cell">${esc(fmtPct(ytd))}</span>
    </div>
  </div>`;
}

async function api(path, params = '') {
  const query = String(params);
  let response;
  try {
    response = await fetch(`${BASE_PATH}${path}${query ? `?${query}` : ''}`);
  } catch (_) {
    throw new Error('unreachable');
  }
  if (!response.ok) throw new Error(`status ${response.status}`);
  const data = await response.json();
  if (data == null || typeof data !== 'object') throw new Error('empty');
  return data;
}

const boundedInt = (value, min, max, fallback) => {
  const number = /^\d+$/.test(String(value ?? '')) ? Number(value) : NaN;
  return Number.isInteger(number) && number >= min && number <= max ? number : fallback;
};

// The view's default direction (what an omitted `direction` means in URLs and API requests): largest first,
// except movers losers where the most negative change comes first.
const viewDirection = (route) => route.view === 'movers' && route.side === 'losers' ? 'asc' : 'desc';
// The direction a column starts in when it is selected.
const startDirection = (route, sort) => SORT_DEFAULTS.get(sort) || viewDirection(route);
const flip = (direction) => direction === 'asc' ? 'desc' : 'asc';

// A path with the public prefix removed when present; the bare prefix is the root. Unprefixed paths still
// resolve (the server accepts them too).
function unprefixedPath(pathname) {
  const prefixed = BASE_PATH && (pathname === BASE_PATH || pathname.startsWith(`${BASE_PATH}/`));
  return (prefixed ? pathname.slice(BASE_PATH.length) : pathname) || '/';
}

// The view named by a path: the root, '/initiations', '/movers', '/about', or a legacy '/dashboard…' alias.
// Anything else, including a trailing slash on a sub-path, is the holdings default.
function viewFromPath(pathname) {
  return ROUTE_VIEWS[unprefixedPath(pathname)] || 'holdings';
}

// The About page has no query state: whatever the URL carries is ignored and never re-emitted.
const ABOUT_ROUTE = {view: 'about', horizon: 1, side: 'gainers', sort: 'metric', direction: 'desc', page: 1, unmapped: 'exclude'};

function routeFromUrl(url = new URL(location.href)) {
  const view = viewFromPath(url.pathname);
  if (view === 'about') return {...ABOUT_ROUTE};
  const params = url.searchParams;
  const movers = view === 'movers';
  const route = {
    view,
    horizon: movers ? boundedInt(params.get('horizon'), 1, 4, 1) : 1,
    side: movers && SIDES.has(params.get('side')) ? params.get('side') : 'gainers',
    sort: SORT_DEFAULTS.has(params.get('sort')) ? params.get('sort') : 'metric',
    page: boundedInt(params.get('page'), 1, MAX_PAGE, 1),
    unmapped: UNMAPPED_VALUES.has(params.get('unmapped')) ? params.get('unmapped') : 'exclude'
  };
  const direction = params.get('direction');
  route.direction = DIRECTION_VALUES.has(direction) ? direction : viewDirection(route);
  return route;
}

function routeUrl(route) {
  if (route.view === 'about') return VIEW_PATHS.about;
  const params = new URLSearchParams();
  if (route.view === 'movers') {
    if (route.horizon !== 1) params.set('horizon', String(route.horizon));
    if (route.side !== 'gainers') params.set('side', route.side);
  }
  if (route.sort !== 'metric') params.set('sort', route.sort);
  if (route.direction !== viewDirection(route)) params.set('direction', route.direction);
  if (route.page > 1) params.set('page', String(route.page));
  if (route.unmapped === 'include') params.set('unmapped', route.unmapped);
  return `${VIEW_PATHS[route.view]}${params.size ? `?${params}` : ''}`;
}

// A view's default route (first page, default sort) reached from the header links; only the unmapped switch travels.
function viewRoute(view) {
  const route = {view, horizon: 1, side: 'gainers', sort: 'metric', page: 1, unmapped: state.unmapped};
  route.direction = viewDirection(route);
  return route;
}

// Side/horizon changes keep the sort. The metric column keeps whether it is at its start direction or flipped
// rather than the literal direction, so Gainers (largest first) -> Losers lands on most-negative first, not least.
function moversRoute(horizon, side) {
  const flipped = state.direction !== startDirection(state, state.sort);
  const route = {view: 'movers', horizon, side, sort: state.sort, page: 1, unmapped: state.unmapped};
  const base = startDirection(route, state.sort);
  route.direction = flipped ? flip(base) : base;
  return route;
}

// Header link target: the active column flips direction, any other column starts at its own direction.
function sortRoute(sort) {
  const direction = sort === state.sort ? flip(state.direction) : startDirection(state, sort);
  return {view: state.view, horizon: state.horizon, side: state.side, sort, direction, page: 1, unmapped: state.unmapped};
}

function setActive(link, active) {
  link.classList.toggle('active', active);
  if (active) link.setAttribute('aria-current', 'page');
  else link.removeAttribute('aria-current');
}

function renderChrome() {
  const about = state.view === 'about';
  document.title = `13F Dashboard — ${VIEW_TITLES[state.view]}`;
  $('#dashLogo').href = routeUrl(viewRoute('holdings'));
  $$('#dashNav a').forEach(link => {
    setActive(link, link.dataset.view === state.view);
    link.href = routeUrl(viewRoute(link.dataset.view));
  });
  // About replaces the table and everything around it; the table chrome comes back on the next view change.
  $('#dashAbout').hidden = !about;
  $('#dashTable').hidden = about;
  if (about) {
    $('#dashControls').hidden = true;
    $('#dashStatus').hidden = true;
    $('#dashPager').hidden = true;
    return;
  }
  $('#dashControls').hidden = state.view !== 'movers';
  $$('#dashSide a').forEach(link => {
    setActive(link, link.dataset.side === state.side);
    link.href = routeUrl(moversRoute(state.horizon, link.dataset.side));
  });
  $$('#dashHorizon a').forEach(link => {
    setActive(link, Number(link.dataset.horizon) === state.horizon);
    link.href = routeUrl(moversRoute(Number(link.dataset.horizon), state.side));
  });
  renderHead();
}

function renderHead() {
  $$('#dashHead [data-sort]').forEach(cell => {
    const sort = cell.dataset.sort;
    const active = sort === state.sort;
    const link = $('a', cell);
    const label = sort === 'metric' ? METRIC_LABELS[state.view] : link.dataset.label || link.textContent.trim();
    link.dataset.label = label;
    link.textContent = label;
    if (active) {
      const glyph = document.createElement('span');
      glyph.className = 'dash-sort-glyph';
      glyph.setAttribute('aria-hidden', 'true');
      glyph.textContent = ` ${SORT_GLYPHS.get(state.direction)}`;
      link.append(glyph);
    }
    link.classList.toggle('active', active);
    link.href = routeUrl(sortRoute(sort));
    cell.setAttribute('aria-sort', active ? (state.direction === 'asc' ? 'ascending' : 'descending') : 'none');
  });
  revealActiveSort();
}

// On phones #dashHead is one sideways-scrolling line: bring the active sort link inside its side padding by
// scrolling the row itself, never the document. Desktop's header does not scroll, so this is a no-op there.
function revealActiveSort() {
  const head = $('#dashHead');
  const cell = $('[data-sort] .dash-sort.active', head)?.closest('[data-sort]');
  if (!cell || head.scrollWidth <= head.clientWidth) return;
  const style = getComputedStyle(head);
  const headRect = head.getBoundingClientRect();
  const rect = cell.getBoundingClientRect();
  const right = headRect.right - parseFloat(style.paddingRight);
  const left = headRect.left + parseFloat(style.paddingLeft);
  if (rect.right > right) head.scrollLeft += Math.ceil(rect.right - right);
  else if (rect.left < left) head.scrollLeft -= Math.ceil(left - rect.left);
}

function setStatus(text, error = false) {
  const status = $('#dashStatus');
  status.classList.toggle('error', error);
  if (error) status.innerHTML = `Data unavailable. <a class="dash-retry" href="${esc(routeUrl(state))}">Retry</a>.`;
  else status.textContent = text;
  status.hidden = false;
}

function setPagerLink(link, enabled, page) {
  link.classList.toggle('disabled', !enabled);
  if (enabled) link.removeAttribute('aria-disabled');
  else link.setAttribute('aria-disabled', 'true');
  link.href = enabled ? routeUrl({...state, page}) : '#';
}

function renderPager() {
  const pages = Math.ceil(state.count / PAGE_SIZE);
  $('#dashPager').hidden = !(state.count > PAGE_SIZE);
  setPagerLink($('#dashPrev'), state.page > 1, state.page - 1);
  setPagerLink($('#dashNext'), state.page < pages, state.page + 1);
}

function renderRows(data) {
  const rows = Array.isArray(data.rows) ? data.rows : [];
  state.count = Math.max(0, Math.trunc(toNumber(data.count) ?? 0));
  $('#dashRows').innerHTML = rows.map(row => rowHtml(row, state.view)).join('');
  if (rows.length) $('#dashStatus').hidden = true;
  else setStatus('No rows.');
  renderPager();
}

// "31-MAR-2026" (the period label) -> "31 Mar 2026"; anything else is shown as given.
function fmtPeriod(label) {
  const match = /^(\d{1,2})-([A-Z]{3})-(\d{4})$/i.exec(String(label ?? '').trim());
  const month = match && MONTHS.get(match[2].toUpperCase());
  return month ? `${Number(match[1])} ${month} ${match[3]}` : String(label ?? '—');
}

// "about 8,700": the latest quarter's manager count rounded to the nearest hundred.
const fmtManagers = (value) => value == null ? '—' : `about ${fmtInt.format(Math.round(value / 100) * 100)}`;

function renderAbout(meta) {
  const periods = Array.isArray(meta?.periods) ? meta.periods.filter(period => period && period.label) : [];
  const dated = periods.filter(period => period.period_date).sort((a, b) => String(a.period_date).localeCompare(String(b.period_date)));
  const first = dated[0] ?? periods[periods.length - 1];
  const last = dated[dated.length - 1] ?? periods[0];
  const quarters = periods.length || toNumber(meta?.period_count);
  $('#aboutQuarters').textContent = quarters ? fmtInt.format(quarters) : '—';
  $('#aboutSpan').textContent = first && last ? `${fmtPeriod(first.label)} to ${fmtPeriod(last.label)}` : '—';
  $('#aboutManagers').textContent = fmtManagers(toNumber(meta?.distinct_managers));
}

// /api/meta is fetched once and kept; a failed fetch leaves the dashes in place and is retried on the next visit.
async function loadAbout() {
  if (state.meta) {
    renderAbout(state.meta);
    return;
  }
  renderAbout(null);
  let meta;
  try {
    meta = await api('/api/meta');
  } catch (_) {
    return;
  }
  state.meta = meta;
  renderAbout(meta);
}

async function load() {
  // Bumping the request id also drops any row response still in flight when About takes over.
  const requestId = ++state.requestId;
  if (state.view === 'about') {
    loadAbout();
    return;
  }
  const params = new URLSearchParams({view: state.view});
  if (state.view === 'movers') {
    params.set('horizon', String(state.horizon));
    params.set('side', state.side);
  }
  // The API defaults match the URL's (sort=metric, the view's direction), so default views send nothing extra;
  // any other column travels with an explicit direction so the request is unambiguous on its own.
  if (state.sort !== 'metric') params.set('sort', state.sort);
  if (state.sort !== 'metric' || state.direction !== viewDirection(state)) params.set('direction', state.direction);
  if (state.unmapped === 'include') params.set('unmapped', state.unmapped);
  params.set('page', String(state.page));
  params.set('size', String(PAGE_SIZE));
  $('#dashRows').innerHTML = '';
  $('#dashPager').hidden = true;
  setStatus('Loading…');
  let data;
  try {
    data = await api('/api/dashboard', params);
  } catch (_) {
    if (requestId === state.requestId) setStatus('', true);
    return;
  }
  if (requestId !== state.requestId) return;
  renderRows(data);
}

function navigate(route, push = true) {
  const pageChange = route.view === state.view && route.page !== state.page;
  Object.assign(state, route);
  const target = routeUrl(state);
  if (push && `${location.pathname}${location.search}` !== target) history.pushState(null, '', target);
  renderChrome();
  if (pageChange) {
    const rows = $('#dashRows');
    rows.focus({preventScroll: true});
    rows.scrollIntoView({block: 'start'});
  }
  load();
}

function handleClick(event) {
  const link = event.target.closest('a');
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (link.classList.contains('dash-retry')) {
    event.preventDefault();
    load();
    return;
  }
  if (link.matches('#dashPrev, #dashNext')) {
    event.preventDefault();
    if (link.getAttribute('aria-disabled') === 'true') return;
    navigate(routeFromUrl(new URL(link.href, location.href)));
    return;
  }
  if (!link.matches('#dashLogo, #dashNav a, #dashSide a, #dashHorizon a, #dashHead a')) return;
  event.preventDefault();
  navigate(routeFromUrl(new URL(link.href, location.href)));
}

function init() {
  $('#dashRows').tabIndex = -1;
  document.addEventListener('click', handleClick);
  window.addEventListener('popstate', () => navigate(routeFromUrl(), false));
  const route = routeFromUrl();
  // A legacy /dashboard… entry URL is rewritten in place to its canonical path; the query is kept as typed.
  if (Object.hasOwn(LEGACY_ROUTE_VIEWS, unprefixedPath(location.pathname))) {
    history.replaceState(null, '', `${VIEW_PATHS[route.view]}${location.search}`);
  }
  navigate(route, false);
}

init();
