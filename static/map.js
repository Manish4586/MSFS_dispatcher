(() => {
  'use strict';

  const API = '/msfs/api/vatsim/registered';
  const REFRESH_MS = 15000;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[ch]));
  let map = null;
  let trackCanvas = null;
  let tileLayer = null;
  let markerLayer = null;
  let trackLayer = null;
  let markers = new Map();
  let lastFeedUpdate = null;
  let timer = null;
  let busy = false;

  function darkMode() {
    return document.body.classList.contains('dark-theme');
  }

  function setTiles() {
    if (!map || !window.L) return;
    if (tileLayer) map.removeLayer(tileLayer);
    tileLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      subdomains: 'abc',
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }).addTo(map);
    const tilePane = map.getPane('tilePane');
    if (tilePane) {
      tilePane.style.filter = darkMode()
        ? 'invert(1) hue-rotate(180deg) brightness(.82) contrast(.9)'
        : 'none';
    }
  }

  function ensureMap() {
    if (map || !window.L || !$('vatsimMap')) return;
    map = L.map('vatsimMap', {worldCopyJump:true, preferCanvas:true, zoomControl:true})
      .setView([22, 0], 2);
    trackCanvas = L.canvas({padding:0.5});
    markerLayer = L.layerGroup().addTo(map);
    trackLayer = L.layerGroup().addTo(map);
    setTiles();
    setTimeout(() => map && map.invalidateSize(), 100);
  }

  function aircraftIconProfile(rawType) {
    const code = String(rawType || '').toUpperCase().split(/[\/\s]/)[0];
    const widebody = new Set(['A330','A332','A333','A338','A339','A342','A343','A345','A346',
      'A359','A35K','A380','A388','B744','B748','B763','B764','B772','B77L','B77W',
      'B788','B789','B78X','MD11','DC10','IL96']);
    const regional = new Set(['CRJ1','CRJ2','CRJ7','CRJ9','E135','E145','E170','E75L','E75S',
      'E190','E195','E290','E295','F100','F70']);
    const turboprop = new Set(['AT43','AT45','AT72','AT75','DH8A','DH8B','DH8C','DH8D',
      'DHC6','SF34','JS41','BE20','PC12','C208']);
    const helicopter = new Set(['H135','H145','H160','H175','H225','B06','B407','B412','S76','S92']);
    if (helicopter.has(code)) return {kind:'helicopter', label:'Helicopter'};
    if (turboprop.has(code)) return {kind:'turboprop', label:'Turboprop'};
    if (widebody.has(code) || /^A3[358]/.test(code) || /^B7[4678]/.test(code)) {
      return {kind:'widebody', label:'Widebody'};
    }
    if (regional.has(code) || /^CRJ/.test(code) || /^E1[3579]/.test(code)) {
      return {kind:'regional', label:'Regional jet'};
    }
    if (/^(A31[89]|A320|A321|A20N|A21N|B7[378]|B38M|B39M|B3[78][0-9])/.test(code)) {
      return {kind:'narrowbody', label:'Narrowbody'};
    }
    return {kind:'generic', label:'Aircraft'};
  }

  function planeIcon(heading, aircraft) {
    const rotation = Number.isFinite(Number(heading)) ? Number(heading) : 0;
    const profile = aircraftIconProfile(aircraft);
    const code = String(aircraft || '').toUpperCase().split(/[\\/\\s]/)[0];
    const zoom = map ? map.getZoom() : 5;
    const base = profile.kind === 'widebody' ? 34 : 29;
    const size = Math.max(13, Math.min(36, Math.round(base * Math.pow(1.12, zoom - 5))));
    const a350 = /^A35(K|9)$/.test(code);
    const wideSvg = a350
      ? `<path d="M48 4 Q50 1 52 4 L55 36 L94 59 L96 64 L56 53 L54 76 L67 87 L67 90 L51 85 L33 90 L33 87 L46 76 L44 53 L4 64 L6 59 L45 36 Z"/>
         <path d="M29 48 Q35 45 39 50 L42 59 Q36 63 31 58 Z"/><path d="M61 50 Q65 45 71 48 L69 58 Q64 63 58 59 Z"/>`
      : `<path d="M48 4 Q50 1 52 4 L55 36 L94 59 L96 64 L56 53 L54 76 L67 87 L67 90 L51 85 L33 90 L33 87 L46 76 L44 53 L4 64 L6 59 L45 36 Z"/>
         <path d="M25 48 Q30 44 35 49 L39 59 Q33 63 28 58 Z"/><path d="M65 49 Q70 44 75 48 L72 58 Q67 63 61 59 Z"/>`;
    const svg = `<svg viewBox="0 0 100 100" width="${size}" height="${size}" aria-label="${profile.label}" style="display:block;transform:rotate(${rotation}deg);overflow:visible">
      <g fill="#f8fafc" stroke="#18212f" stroke-width="2.2" stroke-linejoin="round">${wideSvg}</g></svg>`;
    return L.divIcon({className:'vatsim-plane-marker', iconSize:[size,size],
      iconAnchor:[size/2,size/2], tooltipAnchor:[0,-size/2],
      html:`<span style="display:block;width:${size}px;height:${size}px;filter:drop-shadow(0 1px 2px #0009)">${svg}</span>`});
  }

  function popupHtml(p) {
    const fp = p.departure || p.arrival ? `${esc(p.departure || '—')} → ${esc(p.arrival || '—')}` : 'No flight plan filed';
    return `<div><strong>${esc(p.callsign || `CID ${p.cid}`)}</strong><br>
      ${esc(p.name)}<br><span>${esc(fp)}</span><br>
      <span>${esc(p.aircraft || 'Aircraft unknown')} · ${Number(p.altitude || 0).toLocaleString()} ft</span><br>
      <span>${Number(p.groundspeed || 0)} kt GS · HDG ${Number(p.heading || 0)}°</span><br>
      <span>VATSIM CID ${esc(p.cid)}</span></div>`;
  }

  let selectedCid = null;

  function renderPilotList(pilots) {
    const list = $('vatsimPilotList');
    if (!list) return;
    if (!pilots.length) {
      list.innerHTML = '<div class="live-pilot-empty">No registered pilots are currently online as pilots on VATSIM.</div>';
      return;
    }
    list.innerHTML = pilots.map(p => {
      const route = p.departure && p.arrival ? `${esc(p.departure)} → ${esc(p.arrival)}` : 'No filed route';
      return `<button type="button" class="live-pilot-card" data-cid="${esc(p.cid)}">
        <span class="live-pilot-main"><span class="live-pilot-callsign">${esc(p.callsign || `CID ${p.cid}`)}</span>
        <span class="live-pilot-route">${route} · ${esc(p.aircraft || 'Unknown aircraft')}</span></span>
        <span class="live-pilot-alt">${Number(p.altitude || 0).toLocaleString()} ft<br>${Number(p.groundspeed || 0)} kt</span>
      </button>`;
    }).join('');
    list.querySelectorAll('[data-cid]').forEach(button => {
      button.addEventListener('click', () => {
        const marker = markers.get(String(button.dataset.cid));
        if (marker && map) {
          selectedCid = String(button.dataset.cid);
          list.querySelectorAll('[data-cid]').forEach(card => {
            card.classList.toggle('is-selected', card === button);
            card.setAttribute('aria-pressed', card === button ? 'true' : 'false');
          });
          map.flyTo(marker.getLatLng(), Math.max(map.getZoom(), 6), {duration:0.6});
          marker.openPopup();
        }
      });
    });
  }

  let fittedRouteKey = '';

  function fitDepartureArrival(pilots) {
    if (!map || !pilots.length) return;
    const coords = [];
    const routeKeys = [];
    for (const pilot of pilots) {
      const dep = pilot.departure_airport;
      const arr = pilot.arrival_airport;
      const valid = airport => airport &&
        Number.isFinite(Number(airport.latitude)) &&
        Number.isFinite(Number(airport.longitude));
      if (valid(dep)) coords.push([Number(dep.latitude), Number(dep.longitude)]);
      if (valid(arr)) coords.push([Number(arr.latitude), Number(arr.longitude)]);
      routeKeys.push(`${pilot.cid}:${pilot.departure || ''}:${pilot.arrival || ''}`);
    }
    if (coords.length < 2) return;
    const key = routeKeys.sort().join('|');
    if (key === fittedRouteKey) return;

    fittedRouteKey = key;
    setTimeout(() => {
      if (!map) return;
      map.invalidateSize({pan:false});
      const bounds = L.latLngBounds(coords);
      const padding = L.point(64, 64);
      const fitZoom = map.getBoundsZoom(bounds, false, padding);
      const routeMinZoom = Math.max(0, fitZoom);
      map.setMinZoom(routeMinZoom);
      map.fitBounds(bounds, {
        padding,
        maxZoom: fitZoom,
        animate: false
      });
    }, 180);
  }

  function updateMarkers(pilots) {
    const present = new Set();
    for (const pilot of pilots) {
      const cid = String(pilot.cid);
      present.add(cid);
      let marker = markers.get(cid);
      const latlng = [Number(pilot.latitude), Number(pilot.longitude)];
      if (marker) {
        marker.setLatLng(latlng);
        marker.setIcon(planeIcon(pilot.heading, pilot.aircraft));
        marker.setPopupContent(popupHtml(pilot));
      } else {
        marker = L.marker(latlng, {icon:planeIcon(pilot.heading, pilot.aircraft), title:`${pilot.callsign || `VATSIM ${cid}`} · ${pilot.aircraft || "Aircraft"}`})
          .bindPopup(popupHtml(pilot));
        marker.addTo(markerLayer);
        markers.set(cid, marker);
      }
    }
    for (const [cid, marker] of markers) {
      if (!present.has(cid)) {
        markerLayer.removeLayer(marker);
        markers.delete(cid);
      }
    }
  }

  function altitudeColor(altitude) {
    const ft = Math.max(0, Math.min(45000, Number(altitude) || 0));
    const stops = [
      [0, '#2563eb'], [5000, '#06b6d4'], [12000, '#22c55e'],
      [22000, '#eab308'], [32000, '#f97316'], [40000, '#ef4444']
    ];
    for (let i = 1; i < stops.length; i++) {
      if (ft <= stops[i][0]) {
        const [aAlt, aHex] = stops[i - 1];
        const [bAlt, bHex] = stops[i];
        const t = (ft - aAlt) / (bAlt - aAlt);
        const a = [1, 3, 5].map(k => parseInt(aHex.slice(k, k + 2), 16));
        const b = [1, 3, 5].map(k => parseInt(bHex.slice(k, k + 2), 16));
        return '#' + a.map((v, n) => Math.round(v + (b[n] - v) * t).toString(16).padStart(2, '0')).join('');
      }
    }
    return '#ef4444';
  }

  function simplifyTrack(points, toleranceMeters = 220) {
    if (points.length < 3) return points;
    const meanLat = points.reduce((s,p) => s + p.lat, 0) / points.length;
    const cosLat = Math.max(0.15, Math.cos(meanLat * Math.PI / 180));
    const xy = points.map(p => ({
      x: p.lon * 111320 * cosLat,
      y: p.lat * 110540
    }));
    const keep = new Uint8Array(points.length);
    keep[0] = keep[points.length - 1] = 1;
    const stack = [[0, points.length - 1]];
    while (stack.length) {
      const [first, last] = stack.pop();
      const a = xy[first], b = xy[last];
      const dx = b.x - a.x, dy = b.y - a.y;
      const denom = dx * dx + dy * dy;
      let maxDistance = toleranceMeters, index = -1;
      for (let i = first + 1; i < last; i++) {
        const p = xy[i];
        const t = denom ? Math.max(0, Math.min(1, ((p.x-a.x)*dx + (p.y-a.y)*dy) / denom)) : 0;
        const ex = p.x - (a.x + t*dx), ey = p.y - (a.y + t*dy);
        const distance = Math.hypot(ex, ey);
        if (distance > maxDistance) { maxDistance = distance; index = i; }
      }
      if (index > 0) {
        keep[index] = 1;
        stack.push([first,index],[index,last]);
      }
    }
    return points.filter((_,i) => keep[i]);
  }

  function renderFlightTracks(pilots) {
    if (!trackLayer || !window.L) return;
    trackLayer.clearLayers();

    for (const pilot of pilots) {
      const points = (Array.isArray(pilot.track) ? pilot.track : [])
        .map(point => ({
          lat: Number(point.latitude),
          lon: Number(point.longitude),
          altitude: point.altitude == null ? NaN : Number(point.altitude),
          timestamp: point.timestamp,
          gapBefore: Boolean(point.gap_before),
          gapSeconds: Number(point.gap_seconds || 0)
        }))
        .filter(point => Number.isFinite(point.lat) && Number.isFinite(point.lon));

      if (points.length === 1) {
        const first = points[0];
        L.circleMarker([first.lat, first.lon], {radius:3, color:altitudeColor(first.altitude),
          weight:1, fillColor:altitudeColor(first.altitude), fillOpacity:1}).addTo(trackLayer);
      }

      let run = [];
      let runBucket = null;
      const flushRun = () => {
        if (run.length < 2) { run = []; return; }
        const simplified = simplifyTrack(run, 220);
        const coords = simplified.map(p => [p.lat, p.lon]);
        const altitude = run.reduce((sum,p) => sum + (Number.isFinite(p.altitude) ? p.altitude : 0), 0) / run.length;
        const color = altitudeColor(altitude);
        L.polyline(coords, {
          color, weight:3.2, opacity:1, lineCap:'round', lineJoin:'round',
          smoothFactor:1.5, interactive:true, renderer: trackCanvas
        }).bindPopup(
          `<strong>${esc(pilot.callsign || `CID ${pilot.cid}`)}</strong><br>` +
          `${esc(pilot.departure || '—')} → ${esc(pilot.arrival || '—')}<br>` +
          `Altitude: ${Number.isFinite(altitude) ? Math.round(altitude).toLocaleString() + ' ft' : 'Unavailable'}`
        ).addTo(trackLayer);
        run = [];
      };
      for (let i = 0; i < points.length; i++) {
        const point = points[i];
        if (i === 0) { run = [point]; runBucket = Math.round((point.altitude || 0) / 1000); continue; }
        if (point.gapBefore) {
          flushRun();
          const prev = points[i - 1];
          L.polyline([[prev.lat,prev.lon],[point.lat,point.lon]], {
            color: altitudeColor(point.altitude), weight:3, opacity:.9,
            dashArray:'8 8', lineCap:'round', lineJoin:'round', renderer:trackCanvas
          }).bindPopup(`Telemetry gap: ${Number(point.gapSeconds).toLocaleString()} sec · reconnect continued`).addTo(trackLayer);
          run = [point]; runBucket = Math.round((point.altitude || 0) / 1000);
          continue;
        }
        const bucket = Math.round((point.altitude || 0) / 1000);
        if (bucket !== runBucket && run.length > 1) {
          const previous = run[run.length - 1];
          flushRun();
          run = [previous, point];
        } else {
          run.push(point);
        }
        runBucket = bucket;
      }
      flushRun();

      for (const [kind, airport] of [
        ['Departure', pilot.departure_airport],
        ['Arrival', pilot.arrival_airport]
      ]) {
        if (!airport) continue;
        const departure = kind === 'Departure';
        const accent = departure ? '#7056a8' : '#35a987';
        const markerHtml = `<span style="display:block;width:18px;height:18px;border-radius:50%;background:${accent};border:3px solid #fff;box-shadow:0 1px 7px #25213b66;position:relative"><i style="position:absolute;inset:5px;border-radius:50%;background:#fff;opacity:.9"></i></span>`;
        L.marker([Number(airport.latitude), Number(airport.longitude)], {
          icon: L.divIcon({
            className: 'vatsim-airport-dot',
            html: markerHtml,
            iconSize: [18,18],
            iconAnchor: [9,9]
          }),
          keyboard: false,
          zIndexOffset: 900
        }).bindPopup(`<strong>${kind}: ${esc(airport.code)}</strong><br>${esc(airport.name)}<br>${esc(pilot.callsign || `CID ${pilot.cid}`)}`).addTo(trackLayer);
      }
    }
  }

  function formatTimestamp(value) {
    if (!value) return 'Feed timestamp unavailable';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return `Feed updated ${date.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit', timeZone:'UTC'})} UTC`;
  }

  async function refresh() {
    if (busy || !document.getElementById('tab-live-map')?.classList.contains('active')) return;
    busy = true;
    try {
      ensureMap();
      const response = await fetch(API, {cache:'no-store', credentials:'same-origin'});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      const pilots = Array.isArray(data.pilots) ? data.pilots : [];
      updateMarkers(pilots);
      if (selectedCid) {
        const selectedMarker = markers.get(selectedCid);
        if (selectedMarker && map) {
          map.panTo(selectedMarker.getLatLng(), {animate:true, duration:0.45});
        } else {
          selectedCid = null;
          fittedRouteKey = '';
          fitDepartureArrival(pilots);
        }
      } else {
        fitDepartureArrival(pilots);
      }
      renderFlightTracks(pilots);
      renderPilotList(pilots);
      if ($('vatsimPilotCount')) $('vatsimPilotCount').textContent = `${data.online_count || 0} registered online`;
      if ($('vatsimPilotListCount')) $('vatsimPilotListCount').textContent = `${data.online_count || 0} online`;
      if ($('vatsimUpdatedAt')) $('vatsimUpdatedAt').textContent = formatTimestamp(data.updated_at);
      if ($('vatsimMapStatus')) $('vatsimMapStatus').textContent = pilots.length ? 'VATSIM network connected' : 'Feed connected · no registered pilots online';
      if (data.updated_at && data.updated_at !== lastFeedUpdate) lastFeedUpdate = data.updated_at;
      if (map) setTimeout(() => map.invalidateSize(), 50);
    } catch (error) {
      if ($('vatsimMapStatus')) $('vatsimMapStatus').textContent = `Live feed unavailable · ${error.message}`;
    } finally {
      busy = false;
    }
  }

  function start() {
    ensureMap();
    if (map) setTimeout(() => map.invalidateSize(), 100);
    refresh();
    if (!timer) timer = setInterval(refresh, REFRESH_MS);
  }

  document.querySelectorAll('.tabbtn[data-tab="live-map"]').forEach(button => {
    button.addEventListener('click', () => setTimeout(start, 80));
  });
  window.addEventListener('fltplan:themechange', setTiles);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh();
  });
})();
