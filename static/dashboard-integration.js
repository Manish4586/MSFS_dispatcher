(() => {
  'use strict';
  const API = window.FLTPLAN_API || '/msfs/api';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let authState = {authenticated:false, csrf_token:'', guilds:[], selected_guild_id:null};
  let lastRoute = null;
  const PAGE_SIZE = 10;
  let historyRows = [];
  let publishedRows = [];
  let historyPage = 1;
  let publishedPage = 1;

  async function api(path, options={}) {
    const headers = {...(options.headers || {})};
    const method = String(options.method || 'GET').toUpperCase();
    if (options.body) headers['Content-Type'] = 'application/json';
    if (['POST','DELETE','PUT','PATCH'].includes(method) && authState.csrf_token) {
      headers['X-CSRF-Token'] = authState.csrf_token;
    }
    const res = await fetch(`${API}${path}`, {...options, method, headers, credentials:'same-origin'});
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `API error ${res.status}`);
    return body;
  }

  function routePayload(r) {
    const duration = typeof window.fmtMin === 'function' ? window.fmtMin(r.flightMin) : `${Math.round(r.flightMin || 0)} min`;
    return {
      dep:r.dep.icao, arr:r.arr.icao,
      dep_name:r.dep.name || '', arr_name:r.arr.name || '',
      dep_country:r.dep.country || '', arr_country:r.arr.country || '',
      aircraft:r.ac.label, aircraft_category:r.ac.cat || '',
      cruise_kts:r.ac.cruiseKts || null, scenery:r.sceneryLabel || '',
      time_label:r.timeLabel || '', departure_utc:r.depUTC || '',
      distance_nm:Math.round(r.distNm || 0), duration,
      block_minutes:Math.round(r.flightMin || 0), liveries:r.ac.liveries || [],
      max_time_minutes:Number($('maxTime')?.value || 0)
    };
  }


  function renderPager(infoId, buttonsId, total, page, onPage, suffix='') {
    const info = $(infoId), buttons = $(buttonsId);
    if (!info || !buttons) return;
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    page = Math.min(Math.max(1, page), pages);
    if (total === 0) {
      info.textContent = '';
      info.style.display = 'none';
      buttons.innerHTML = '';
      buttons.style.display = 'none';
      return;
    }
    info.textContent = `Showing ${(page - 1) * PAGE_SIZE + 1}–${Math.min(page * PAGE_SIZE, total)} of ${total}${suffix}`;
    info.style.display = '';
    if (pages <= 1) {
      buttons.innerHTML = '';
      buttons.style.display = 'none';
      return;
    }
    buttons.style.display = 'flex';
    let html = `<button type="button" class="pgbtn" data-nav="prev" ${page === 1 ? 'disabled' : ''}>‹ Prev</button>`;
    const visible = new Set([1, pages]);
    for (let p = Math.max(2, page - 2); p <= Math.min(pages - 1, page + 2); p++) visible.add(p);
    let last = 0;
    for (const p of [...visible].sort((a,b) => a-b)) {
      if (last && p - last > 1) html += `<span class="pg-ellipsis" aria-hidden="true">…</span>`;
      html += `<button type="button" class="pgbtn${p === page ? ' active' : ''}" data-page="${p}" aria-current="${p === page ? 'page' : 'false'}">${p}</button>`;
      last = p;
    }
    html += `<button type="button" class="pgbtn" data-nav="next" ${page === pages ? 'disabled' : ''}>Next ›</button>`;
    buttons.innerHTML = html;
    buttons.style.display = 'flex';
    buttons.style.visibility = 'visible';
    buttons.querySelectorAll('.pgbtn').forEach(b => { b.style.display = 'inline-flex'; b.style.alignItems = 'center'; b.style.justifyContent = 'center'; });
    buttons.querySelectorAll('[data-page]').forEach(b => b.addEventListener('click', () => onPage(Number(b.dataset.page))));
    buttons.querySelector('[data-nav="prev"]')?.addEventListener('click', () => onPage(Math.max(1, page - 1)));
    buttons.querySelector('[data-nav="next"]')?.addEventListener('click', () => onPage(Math.min(pages, page + 1)));
  }

  function renderHistoryRows() {
    const body = $('histBody'), empty = $('histEmpty'), table = $('histTable');
    if (!body || !empty || !table) return;
    const total = historyRows.length;
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    historyPage = Math.min(historyPage, pages);
    if ($('histCount')) $('histCount').textContent = `${total} records`;
    if (!total) {
      body.innerHTML = ''; table.style.display = 'none'; empty.style.display = 'block';
      empty.textContent = 'No searches yet — dispatch a route to start building your log.';
      renderPager('pagerInfo', 'pagerBtns', 0, 1, p => { historyPage = p; renderHistoryRows(); }, ' · maximum 10 per page');
      return;
    }
    const start = (historyPage - 1) * PAGE_SIZE;
    const slice = historyRows.slice(start, start + PAGE_SIZE);
    table.style.display = 'table'; empty.style.display = 'none';
    body.innerHTML = slice.map((row, i) => `<tr><td>${total - start - i}</td><td>${new Date(row.created_at).toISOString().slice(11,16)}Z</td><td>${esc(row.dep)}</td><td>${esc(row.arr)}</td><td>${esc(row.aircraft)}</td><td>${esc(row.scenery)}</td><td>${esc(row.time_label)}</td><td>${Number(row.distance_nm).toLocaleString()}</td><td>${esc(row.duration)}</td></tr>`).join('');
    renderPager('pagerInfo', 'pagerBtns', total, historyPage, p => { historyPage = p; renderHistoryRows(); }, ' · maximum 10 per page · expires after 12 hours');
  }

  function renderPublishedRows() {
    const body = $('publishedBody'), empty = $('publishedEmpty');
    if (!body || !empty) return;
    const total = publishedRows.length;
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    publishedPage = Math.min(publishedPage, pages);
    if ($('publishedCount')) $('publishedCount').textContent = `${total} records`;
    if (!total) {
      body.innerHTML = ''; empty.style.display = 'block';
      empty.textContent = 'No routes published in this server yet.';
      renderPager('publishedPagerInfo', 'publishedPagerBtns', 0, 1, p => { publishedPage = p; renderPublishedRows(); }, ' · maximum 10 per page');
      return;
    }
    const start = (publishedPage - 1) * PAGE_SIZE;
    const slice = publishedRows.slice(start, start + PAGE_SIZE);
    body.innerHTML = slice.map(row => `<tr><td>${new Date(row.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</td><td>${esc(row.dep)} → ${esc(row.arr)}</td><td>${esc(row.aircraft)}</td><td>${Number(row.distance_nm).toLocaleString()} NM</td><td>${esc(row.channel_name)}</td><td><a href="${esc(row.message_url)}" target="_blank" rel="noopener">Open ↗</a></td></tr>`).join('');
    empty.style.display = 'none';
    renderPager('publishedPagerInfo', 'publishedPagerBtns', total, publishedPage, p => { publishedPage = p; renderPublishedRows(); }, ' · maximum 10 per page');
  }

  function ensureAuthPanel() {
    let panel = $('fltplanAuthPanel');
    if (panel) return panel;
    panel = document.createElement('div');
    panel.id = 'fltplanAuthPanel';
    panel.className = 'flt-auth-panel';
    const header = document.querySelector('.header-actions');
    if (header) header.insertBefore(panel, header.firstChild);
    return panel;
  }

  function renderAuthPanel(message='') {
    const panel = ensureAuthPanel();
    if (!authState.authenticated) {
      panel.innerHTML = `<a class="btn flt-auth-btn" href="${esc(API)}/auth/login">Sign in with Discord</a>${message ? `<span class="flt-auth-message">${esc(message)}</span>` : ''}`;
      updatePublishAvailability();
      return;
    }
    const user = authState.user || {};
    const guilds = authState.guilds || [];
    let serverControl = '';
    if (guilds.length > 1) {
      serverControl = `<label class="flt-server-label" for="fltGuildSelect">Server</label><select id="fltGuildSelect" class="flt-server-select"><option value="">Choose server…</option>${guilds.map(g=>`<option value="${esc(g.id)}" ${String(g.id)===String(authState.selected_guild_id)?'selected':''}>${esc(g.name)}</option>`).join('')}</select>`;
    } else if (guilds.length === 1) {
      serverControl = `<span class="flt-server-one">${esc(guilds[0].name)}</span>`;
    } else {
      serverControl = `<span class="flt-auth-message">No configured server grants you the Dispatcher role.</span>`;
    }
    panel.innerHTML = `${user.avatar_url ? `<img class="flt-avatar" src="${esc(user.avatar_url)}" alt="">` : ''}<span class="flt-user">${esc(user.username || 'Discord user')}</span>${serverControl}<button type="button" class="btn flt-auth-btn" id="fltLogoutBtn">Sign out</button>${message ? `<span class="flt-auth-message">${esc(message)}</span>` : ''}`;
    $('fltGuildSelect')?.addEventListener('change', async e => {
      const guildId = e.target.value;
      if (!guildId) return;
      e.target.disabled = true;
      try {
        await api('/auth/select-guild', {method:'POST', body:JSON.stringify({guild_id:guildId})});
        authState.selected_guild_id = guildId;
        await refreshAuth();
        await refreshHistory(); await refreshPublished();
      } catch (err) { alert(`Could not select server: ${err.message}`); e.target.disabled = false; }
    });
    $('fltLogoutBtn')?.addEventListener('click', async () => {
      try { await api('/auth/logout', {method:'POST'}); }
      catch (err) { console.error('Logout failed:', err); }
      authState = {authenticated:false, csrf_token:'', guilds:[], selected_guild_id:null};
      renderAuthPanel();
      await refreshHistory(); await refreshPublished();
    });
    updatePublishAvailability();
  }

  function canPublish() {
    return !!(authState.authenticated && authState.selected_guild_id);
  }

  async function refreshAuth() {
    try {
      const state = await api('/auth/me');
      authState = state;
      renderAuthPanel();
    } catch (e) {
      authState = {authenticated:false, csrf_token:'', guilds:[], selected_guild_id:null};
      renderAuthPanel('Authentication status unavailable');
    }
    updatePublishAvailability();
  }

  function ensurePublishedPanel() {
    if ($('publishedPanel')) return;
    const panel = document.createElement('div');
    panel.className='panel history'; panel.id='publishedPanel';
    panel.innerHTML=`<div class="panel-hd">Published to Server <span class="n" id="publishedCount">0 records</span></div>
      <div class="hist-wrap"><table class="hist"><thead><tr><th>Time</th><th>Route</th><th>Aircraft</th><th>Distance</th><th>Channel</th><th>Discord</th></tr></thead><tbody id="publishedBody"></tbody></table><div class="hist-empty" id="publishedEmpty">Sign in with Discord to view published routes.</div></div><div class="pager"><div class="info" id="publishedPagerInfo"></div><div class="btns" id="publishedPagerBtns"></div></div>`;
    const anchor = $('historyPanel');
    if (anchor) anchor.after(panel);
  }

  async function refreshPublished() {
    ensurePublishedPanel();
    if (!authState.authenticated) {
      publishedRows = []; publishedPage = 1;
      $('publishedCount').textContent = '—';
      $('publishedBody').innerHTML = '';
      $('publishedEmpty').textContent = 'Sign in with Discord to view published routes.';
      $('publishedEmpty').style.display = 'block';
      renderPager('publishedPagerInfo', 'publishedPagerBtns', 0, 1, p => { publishedPage = p; renderPublishedRows(); });
      return;
    }
    if (!authState.selected_guild_id) {
      publishedRows = []; publishedPage = 1;
      $('publishedCount').textContent = '—';
      $('publishedBody').innerHTML = '';
      $('publishedEmpty').textContent = 'Choose a configured Discord server to view its published routes.';
      $('publishedEmpty').style.display = 'block';
      renderPager('publishedPagerInfo', 'publishedPagerBtns', 0, 1, p => { publishedPage = p; renderPublishedRows(); });
      return;
    }
    try {
      const rows = await api('/published');
      publishedRows = Array.isArray(rows) ? rows : [];
      if (publishedPage > Math.max(1, Math.ceil(publishedRows.length / PAGE_SIZE))) publishedPage = 1;
      renderPublishedRows();
    } catch(e) {
      $('publishedEmpty').textContent = `Could not load published routes: ${e.message}`;
      $('publishedEmpty').style.display = 'block';
    }
  }

  async function refreshHistory() {
    const body = $('histBody'), empty = $('histEmpty'), table = $('histTable');
    if (!body || !empty || !table) return;
    if (!authState.authenticated) {
      historyRows = []; historyPage = 1;
      body.innerHTML = ''; table.style.display = 'none'; empty.style.display = 'block';
      empty.textContent = 'Sign in with Discord to save and view your search history.';
      if ($('histCount')) $('histCount').textContent = '0 records';
      renderPager('pagerInfo', 'pagerBtns', 0, 1, p => { historyPage = p; renderHistoryRows(); });
      return;
    }
    try {
      const rows = await api('/history');
      historyRows = Array.isArray(rows) ? rows : [];
      if (historyPage > Math.max(1, Math.ceil(historyRows.length / PAGE_SIZE))) historyPage = 1;
      renderHistoryRows();
    } catch(e) { console.error('History load failed:', e); }
  }

  function updatePublishAvailability() {
    const button = $('publishBtn');
    if (!button) return;
    button.disabled = !lastRoute || !canPublish();
    button.title = !authState.authenticated ? 'Sign in with Discord to publish' : (!authState.selected_guild_id ? 'Select an eligible Discord server' : 'Publish route');
  }

  function addControls() {
    const old=$('dispatchBtn');
    if (!old || $('publishBtn')) return;
    old.textContent='Search'; old.id='searchBtn';
    const publish=document.createElement('button');
    publish.className='btn'; publish.id='publishBtn'; publish.textContent='Publish to server ▸';
    publish.style.marginTop='10px'; publish.disabled=true;
    old.after(publish);
    publish.addEventListener('click', async()=>{
      if (!lastRoute) { alert('Search for a route first.'); return; }
      if (!canPublish()) { alert('Sign in with Discord and select an eligible server first.'); return; }
      publish.disabled=true; publish.textContent='Publishing…';
      try {
        const result=await api('/publish',{method:'POST',body:JSON.stringify(routePayload(lastRoute))});
        alert(`Published ${result.record.dep} → ${result.record.arr} to #${result.record.channel_name} in ${result.record.guild_name}.`);
        await refreshPublished();
      } catch(e) { alert(`Publish failed: ${e.message}`); }
      finally { publish.textContent='Publish to server ▸'; updatePublishAvailability(); }
    });
  }

  window.addEventListener('fltplan:route', async e=>{
    const r=e.detail;
    if (!r || r.error) return;
    lastRoute=r;
    updatePublishAvailability();
    if (authState.authenticated) {
      try { await api('/search',{method:'POST',body:JSON.stringify(routePayload(r))}); await refreshHistory(); }
      catch(err) { console.error('Could not save search:',err); }
    }
  });

  addControls(); ensurePublishedPanel(); ensureAuthPanel();
  $('rerollBtn')?.addEventListener('click',()=>{ const b=$('publishBtn'); if(b)b.disabled=true; });
  $('clearHistBtn')?.addEventListener('click',async()=>{
    if (!authState.authenticated) { alert('Sign in with Discord first.'); return; }
    try { await api('/history',{method:'DELETE'}); await refreshHistory(); }
    catch(e) { alert(`Could not clear history: ${e.message}`); }
  });

  (async()=>{
    const params = new URLSearchParams(location.search);
    const authError = params.get('auth_error');
    if (authError) history.replaceState({}, '', location.pathname);
    await refreshAuth();
    if (authError) renderAuthPanel(`Discord sign-in: ${authError}`);
    await refreshHistory(); await refreshPublished();
  })();
  setInterval(async()=>{await refreshAuth(); await refreshHistory(); await refreshPublished();},60000);
})();
