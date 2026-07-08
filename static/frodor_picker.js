/* Frodor-Anmeldungs-Picker: Suchfeld + Trefferliste über den Camp-Anmeldungen.
 *
 * Verwendung:
 *   initFrodorPicker(containerEl, {
 *     onSelect: function (patient) { ... }   // nach erfolgreicher Übernahme
 *   });
 *
 * patient = { patient_id, name, vorname, nachname, geburtsdatum, stammnummer }
 * (Antwort von POST /api/frodor/adopt — legt den lokalen Patienten an und
 * übernimmt Notfallkontakt/Allergien/Medikamente aus der Anmeldung.)
 */
function initFrodorPicker(container, opts) {
  'use strict';
  var onSelect = (opts && opts.onSelect) || function () {};

  container.classList.add('frodor-picker');
  container.innerHTML =
    '<label class="frodor-picker-label">Aus Camp-Anmeldung suchen' +
    ' <span class="muted">(frodor)</span></label>' +
    '<input type="text" class="frodor-picker-input" autocomplete="off"' +
    ' placeholder="Name oder Stamm tippen …">' +
    '<div class="frodor-picker-results" hidden></div>' +
    '<div class="frodor-picker-status muted" hidden></div>';

  var input = container.querySelector('.frodor-picker-input');
  var results = container.querySelector('.frodor-picker-results');
  var status = container.querySelector('.frodor-picker-status');
  var timer = null;
  var seq = 0;

  function setStatus(text) {
    status.textContent = text || '';
    status.hidden = !text;
  }

  function render(items) {
    results.innerHTML = '';
    if (!items.length) { results.hidden = true; return; }
    items.forEach(function (r) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'frodor-picker-item';
      var dob = r.geburtsdatum
        ? r.geburtsdatum.split('-').reverse().join('.')
        : '—';
      btn.innerHTML = '<strong></strong> <span class="muted"></span>';
      btn.querySelector('strong').textContent = r.name;
      btn.querySelector('span').textContent =
        '* ' + dob + (r.stamm ? ' · ' + r.stamm : '');
      btn.addEventListener('click', function () { adopt(r); });
      results.appendChild(btn);
    });
    results.hidden = false;
  }

  function search() {
    var q = input.value.trim();
    if (q.length < 2) { render([]); setStatus(''); return; }
    var mySeq = ++seq;
    fetch('/api/frodor/registrations?q=' + encodeURIComponent(q), {
      credentials: 'same-origin'
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (mySeq !== seq) return; // veraltete Antwort
        if (!data.available) {
          container.hidden = true;
          return;
        }
        if (data.error) {
          render([]);
          setStatus('frodor nicht erreichbar — ' + data.error);
          return;
        }
        setStatus(data.results.length ? '' : 'Keine Anmeldung gefunden.');
        render(data.results);
      })
      .catch(function () {
        if (mySeq !== seq) return;
        render([]);
        setStatus('frodor nicht erreichbar.');
      });
  }

  function adopt(reg) {
    setStatus('Übernehme ' + reg.name + ' …');
    results.hidden = true;
    fetch('/api/frodor/adopt', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ uuid: reg.uuid })
    })
      .then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
          return data;
        });
      })
      .then(function (patient) {
        input.value = patient.name;
        setStatus('Übernommen: ' + patient.name + ' — Daten aus der Anmeldung sind verknüpft.');
        onSelect(patient);
      })
      .catch(function (e) {
        setStatus('Übernahme fehlgeschlagen: ' + e.message);
      });
  }

  input.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(search, 250);
  });
  // Klick außerhalb schließt die Trefferliste
  document.addEventListener('click', function (e) {
    if (!container.contains(e.target)) results.hidden = true;
  });
}
