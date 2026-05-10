
    const urlParams = new URLSearchParams(window.location.search);
    const adminKey = urlParams.get('admin');
    let firstLoad = true; let busHeading = 0; let busActive = false; let busPos = [0, 0]; let adminMarkers = {}; let userPos = [0, 0]; let busVisible = false;
    let wasBusActive = false; // for offline alert tracking

    var map = L.map('map', { zoomControl: false, rotate: true, touchRotate: true, rotateControl: false }).setView([28.6139, 77.2090], 13);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }).addTo(map);

    // ── OFFLINE ALERT ─────────────────────────────────────────
    if ('Notification' in window) Notification.requestPermission();
    function showOfflineAlert() {
      const toast = document.getElementById('offline-toast');
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 4000);
      if ('Notification' in window && Notification.permission === 'granted') {
        new Notification('🚌 Bus Signal Lost', { body: 'Bus 43 ka signal band ho gaya!', icon: '' });
      }
    }

    function formatDateTime(date) {
      return `<span style="white-space:nowrap;">${date.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }).toUpperCase()}</span> | <span style="white-space:nowrap;">${date.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true }).toUpperCase()}</span>`;
    }

    window.onload = () => {
      document.getElementById('map-wrapper').classList.add('active');
      map.invalidateSize();
      // PC slide panel toggle
      const tab = document.getElementById('contact-tab');
      if (tab) tab.addEventListener('click', () => {
        document.getElementById('contact-panel').classList.toggle('open');
      });
    };

    // Bus icon: directional arrow like Google Maps
    // We rotate only the INNER div (not the Leaflet wrapper) to avoid drift on zoom
    const busIconHtml = `<div id="bus-icon-inner" style="width:52px;height:62px;position:relative;transform-origin:26px 41px;transition:transform 0.4s ease;">
      <div style="position:absolute;top:0;left:50%;transform:translateX(-50%);width:0;height:0;border-left:9px solid transparent;border-right:9px solid transparent;border-bottom:14px solid #4169E1;"></div>
      <div style="background:var(--primary);width:42px;height:42px;border-radius:50%;border:4px solid white;display:flex;align-items:center;justify-content:center;box-shadow:0 8px 20px rgba(65,105,225,0.4);position:absolute;top:10px;left:5px;">
        <i class="fas fa-bus" style="color:white;font-size:18px;"></i>
      </div>
    </div>`;

    const busIcon = L.divIcon({
      html: busIconHtml,
      iconSize: [52, 62],
      iconAnchor: [26, 41],  // center of the bus circle = correct map pin point
      className: ''
    });

    const userIcon = L.divIcon({ html: `<div style="background:var(--accent);width:18px;height:18px;border-radius:50%;border:3px solid white;box-shadow:0 0 12px var(--accent);"></div>`, iconSize: [18, 18], iconAnchor: [9, 9], className: '' });
    const adminUserIcon = L.divIcon({ html: `<div style="background:#dc2626;width:14px;height:14px;border-radius:50%;border:2px solid white;box-shadow:0 0 8px #dc2626;"></div>`, iconSize: [14, 14], iconAnchor: [7, 7], className: '' });

    var busMarker = L.marker([0, 0], { icon: busIcon, opacity: 0 }).addTo(map).bindTooltip("Bus 43", { permanent: true, direction: 'right', className: 'map-label' });
    var userMarker = L.marker([0, 0], { icon: userIcon, opacity: 0 }).addTo(map).bindTooltip("You", { permanent: true, direction: 'top', className: 'map-label' });
    let isAutoTracking = true;

    // Rotate ONLY the inner icon div — Leaflet wrapper stays untouched so no zoom drift
    function updateBusIconRotation(heading) {
      const inner = document.getElementById('bus-icon-inner');
      if (inner) {
        inner.style.transform = `rotate(${heading}deg)`;
      }
    }

    function getBearing(sLat, sLng, dLat, dLng) {
      const toRad = d => d * Math.PI / 180;
      const y = Math.sin(toRad(dLng - sLng)) * Math.cos(toRad(dLat));
      const x = Math.cos(toRad(sLat)) * Math.sin(toRad(dLat)) - Math.sin(toRad(sLat)) * Math.cos(toRad(dLat)) * Math.cos(toRad(dLng - sLng));
      return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    }

    document.getElementById('focus-btn').onclick = () => {
      isAutoTracking = true;
      const target = busActive ? busPos : userPos;
      // flyTo gives smooth Google-Maps-like zoom+pan without judder
      map.flyTo(target, 17, { animate: true, duration: 0.8, easeLinearity: 0.5 });
    };

    map.on('dragstart', () => isAutoTracking = false);

    if (navigator.geolocation) {
      navigator.geolocation.watchPosition(pos => {
        userPos = [pos.coords.latitude, pos.coords.longitude];
        userMarker.setLatLng(userPos);
        userMarker.setOpacity(1);
        // Send viewer location to server so admin can see it
        fetch(`/update_viewer?lat=${userPos[0]}&lon=${userPos[1]}`).catch(() => { });
        // Instant First Snap to User
        if (firstLoad && !busActive) { map.setView(userPos, 17, { animate: false }); firstLoad = false; }
      }, null, { enableHighAccuracy: true });
    }

    // Network polling interval: WiFi = 3s, 4G = 3s, 3G = 5s, slow/unknown = 8s
    function getPollingInterval() {
      const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
      if (!conn) return 3000;
      // conn.type gives physical type: 'wifi', 'cellular', etc.
      const physType = (conn.type || '').toLowerCase();
      const effType = (conn.effectiveType || '').toLowerCase();
      if (physType === 'wifi') return 3000;
      if (effType === '4g') return 3000;
      if (effType === '3g') return 5000;
      return 8000;
    }

    // Update network indicator UI independently of data fetch
    function updateNetUI() {
      const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
      const bars = document.querySelectorAll('.bar');
      if (!conn) { bars.forEach(b => b.classList.add('fill')); document.getElementById('net-text').innerText = 'WIFI - STRONG'; return; }
      // Always check physical type FIRST; effectiveType='4g' even on WiFi (speed estimate)
      const physType = (conn.type || '').toLowerCase();
      const effType = (conn.effectiveType || '').toLowerCase();
      let str = 4, label = 'WIFI - STRONG';
      if (physType === 'wifi') {
        str = 4; label = 'WIFI - STRONG';
      } else if (physType === 'cellular' || physType === 'wimax' || physType === 'other') {
        if (effType === '4g') { str = 4; label = '4G - STRONG'; }
        else if (effType === '3g') { str = 3; label = '3G - MEDIUM'; }
        else if (effType === '2g' || effType === 'slow-2g') { str = 1; label = 'WEAK SIGNAL'; }
        else { str = 2; label = 'CELLULAR'; }
      } else {
        // Fallback when conn.type is empty (e.g. desktop browsers)
        if (effType === '4g') { str = 4; label = '4G - STRONG'; }
        else if (effType === '3g') { str = 3; label = '3G - MEDIUM'; }
        else if (effType === '2g' || effType === 'slow-2g') { str = 1; label = 'WEAK SIGNAL'; }
      }
      bars.forEach((bar, i) => { if (i < str) bar.classList.add('fill'); else bar.classList.remove('fill'); });
      document.getElementById('net-text').innerText = label;
    }

    // Listen for network type changes and instantly switch polling rate
    let currentPollTimer = null;
    const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (conn) {
      conn.addEventListener('change', () => {
        updateNetUI();
        // Restart polling loop immediately with new interval
        if (currentPollTimer) clearTimeout(currentPollTimer);
        updateData();
      });
    }

    function updateData() {
      fetch(adminKey ? `/get_bus?admin=${adminKey}&t=${Date.now()}` : `/get_bus?t=${Date.now()}`).then(res => res.json()).then(data => {
        document.getElementById('admin-badge').style.display = (data.admin_active || (adminKey && adminKey.includes('Laksh'))) ? 'inline-flex' : 'none';
        busActive = data.is_online;

        const pollMs = getPollingInterval();
        const animDuration = (pollMs / 1000) * 0.85; // animation slightly shorter than poll gap

        if (busActive) {
          const newPos = [data.lat, data.lon];
          if (busPos[0] !== 0) {
            if (map.distance(busPos, newPos) > 2.0) {
              busHeading = getBearing(busPos[0], busPos[1], data.lat, data.lon);
            }
          }
          busPos = newPos;
          busMarker.setLatLng(busPos);
          // Update directional arrow rotation
          updateBusIconRotation(busHeading);
          if (!busVisible) { busMarker.setOpacity(1); busVisible = true; }

          // AUTO ZOOM & TRACKING LOGIC
          if (firstLoad) {
            map.setView(busPos, 17, { animate: false });
            firstLoad = false;
          }

          if (isAutoTracking) {
            // Use panTo for smooth continuous tracking (no zoom change = no judder)
            map.panTo(busPos, { animate: true, duration: animDuration, easeLinearity: 1 });
          }

          document.getElementById('time').innerHTML = formatDateTime(new Date(data.time));
          document.getElementById('status-text').innerText = data.status;
          document.getElementById('status-text').style.color = data.status === "RUNNING" ? "var(--accent)" : "#dc2626";
          document.getElementById('conn-dot').className = "live-dot active";
          wasBusActive = true;
        } else {
          // Fire offline alert only when bus JUST went offline
          if (wasBusActive) { showOfflineAlert(); wasBusActive = false; }
          document.getElementById('conn-dot').className = "live-dot";
          document.getElementById('status-text').innerText = "NO SIGNAL";
        }


        if (adminKey && data.all_viewers) {
          Object.keys(data.all_viewers).forEach(ip => {
            let v = data.all_viewers[ip], lbl = v.device + ' | ' + ip;
            if (!adminMarkers[ip]) adminMarkers[ip] = L.marker([v.lat, v.lon], { icon: adminUserIcon }).addTo(map).bindTooltip(lbl, { direction: 'top', className: 'map-label', permanent: true });
            else { adminMarkers[ip].setLatLng([v.lat, v.lon]); adminMarkers[ip].setTooltipContent(lbl); }
          });
          Object.keys(adminMarkers).forEach(ip => { if (!data.all_viewers[ip]) { map.removeLayer(adminMarkers[ip]); delete adminMarkers[ip]; } });
        }
        currentPollTimer = setTimeout(updateData, pollMs);
      }).catch(() => {
        // Network error ya server down — retry karo, polling band mat ho
        currentPollTimer = setTimeout(updateData, 5000);
      });
    }

    updateNetUI(); // Set correct UI on first load

    setInterval(() => { document.getElementById('current-time-clock').innerHTML = formatDateTime(new Date()); }, 1000);
    updateData(); // Start polling loop
  
