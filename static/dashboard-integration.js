(() => {
  'use strict';
  const API = window.FLTPLAN_API || '/msfs/api';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let authState = {authenticated:false, csrf_token:'', guilds:[], selected_guild_id:null};
  let lastRoute = null;

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
      <div class="hist-wrap"><table class="hist"><thead><tr><th>Time</th><th>Route</th><th>Aircraft</th><th>Distance</th><th>Channel</th><th>Discord</th></tr></thead><tbody id="publishedBody"></tbody></table><div class="hist-empty" id="publishedEmpty">Sign in with Discord to view published routes.</div></div>`;
    const anchor = $('historyPanel');
    if (anchor) anchor.after(panel);
  }

  async function refreshPublished() {
    ensurePublishedPanel();
    if (!authState.authenticated) {
      $('publishedCount').textContent='—';
      $('publishedBody').innerHTML='';
      $('publishedEmpty').textContent='Sign in with Discord to view published routes.';
      $('publishedEmpty').style.display='block';
      return;
    }
    if (!authState.selected_guild_id) {
      $('publishedCount').textContent='—';
      $('publishedBody').innerHTML='';
      $('publishedEmpty').textContent='Choose a configured Discord server to view its published routes.';
      $('publishedEmpty').style.display='block';
      return;
    }
    try {
      const rows = await api('/published');
      $('publishedCount').textContent=`${rows.length} records`;
      $('publishedBody').innerHTML=rows.map(x=>`<tr><td>${new Date(x.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</td><td>${esc(x.dep)} → ${esc(x.arr)}</td><td>${esc(x.aircraft)}</td><td>${Number(x.distance_nm).toLocaleString()} NM</td><td>${esc(x.channel_name)}</td><td><a href="${esc(x.message_url)}" target="_blank" rel="noopener">Open ↗</a></td></tr>`).join('');
      $('publishedEmpty').style.display=rows.length?'none':'block';
      if (!rows.length) $('publishedEmpty').textContent='No routes published in this server yet.';
    } catch(e) { $('publishedEmpty').textContent=`Could not load published routes: ${e.message}`; }
  }

  async function refreshHistory() {
    const body=$('histBody'), empty=$('histEmpty'), table=$('histTable');
    if (!body || !empty || !table) return;
    if (!authState.authenticated) {
      body.innerHTML=''; table.style.display='none'; empty.style.display='block';
      empty.textContent='Sign in with Discord to save and view your search history.';
      if ($('histCount')) $('histCount').textContent='0 records';
      return;
    }
    try {
      const rows=await api('/history');
      $('histCount').textContent=`${rows.length} records`;
      if (!rows.length) { body.innerHTML=''; table.style.display='none'; empty.style.display='block'; empty.textContent='No searches yet — dispatch a route to start building your log.'; }
      else {
        table.style.display='table'; empty.style.display='none';
        body.innerHTML=rows.map((h,i)=>`<tr><td>${rows.length-i}</td><td>${new Date(h.created_at).toISOString().slice(11,16)}Z</td><td>${esc(h.dep)}</td><td>${esc(h.arr)}</td><td>${esc(h.aircraft)}</td><td>${esc(h.scenery)}</td><td>${esc(h.time_label)}</td><td>${Number(h.distance_nm).toLocaleString()}</td><td>${esc(h.duration)}</td></tr>`).join('');
      }
      if ($('pagerInfo')) $('pagerInfo').textContent=`${rows.length} records · automatically expire after 12 hours`;
      if ($('pagerBtns')) $('pagerBtns').innerHTML='';
    } catch(e) { console.error('History load failed:',e); }
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
