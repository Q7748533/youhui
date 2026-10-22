/* Recomputes the TCO table as inputs change. The server already rendered the
   default scenario, so this file only keeps the numbers in sync while typing.
   The three formula functions below are exported when loaded under Node so
   tests can compare them with tco.py scenario by scenario. */

function tiered(tiers, volumeTb) {
  var remaining = volumeTb, prev = 0, total = 0, GB_PER_TB = 1024;
  for (var i = 0; i < tiers.length; i++) {
    var cap = (tiers[i].upto_tb === null || tiers[i].upto_tb === undefined)
      ? Infinity : tiers[i].upto_tb;
    var span = Math.min(remaining, cap - prev);
    if (span <= 0) break;
    total += span * GB_PER_TB * (tiers[i].rate_per_gb || 0);
    remaining -= span;
    prev = cap;
    if (remaining <= 0) break;
  }
  return total;
}

function egressCharge(calc, storageTb, egressTb) {
  calc = calc || {};
  var kind = calc.kind || 'free';
  var GB_PER_TB = 1024;
  if (kind === 'free' || kind === 'bundled') return 0;
  if (kind === 'multiple') {
    var freeTb = storageTb * (calc.free_multiple || 0);
    return Math.max(0, egressTb - freeTb) * GB_PER_TB * (calc.rate_per_gb || 0);
  }
  if (kind === 'allowance') {
    return Math.max(0, egressTb - (calc.allowance_tb || 0)) * GB_PER_TB * (calc.rate_per_gb || 0);
  }
  if (kind === 'per_tb') return egressTb * (calc.rate_per_tb || 0);
  if (kind === 'per_gb') return egressTb * GB_PER_TB * (calc.rate_per_gb || 0);
  if (kind === 'tiered') return tiered(calc.tiers || [], egressTb);
  return 0;
}

function monthly(row, storageTb, egressTb) {
  var calc = row.egress || {};
  var billable = Math.max(storageTb, calc.min_storage_tb || 0);
  var storage = calc.storage_tiers
    ? tiered(calc.storage_tiers, billable)
    : billable * row.storagePerTb;
  var egress = egressCharge(calc, billable, egressTb);
  var total = storage + egress;
  if (calc.min_monthly) total = Math.max(total, calc.min_monthly);
  return { storage: storage, egress: egress, total: total };
}

/* Cheapest row at one point. Ties break on name so the answer never flickers
   between two equally priced providers. */
function cheapest(catalog, storageTb, egressTb) {
  var best = null;
  for (var i = 0; i < catalog.length; i++) {
    var row = catalog[i];
    var m = monthly(row, storageTb, egressTb);
    if (!best) { best = { row: row, m: m }; continue; }
    if (m.total < best.m.total ||
        (m.total === best.m.total &&
         String(row.name || '').toLowerCase() < String(best.row.name || '').toLowerCase())) {
      best = { row: row, m: m };
    }
  }
  return best;
}

var SCAN_FLOOR_TB = 1000, SCAN_SAMPLES = 400;

function crossover(catalog, storageTb, egressTb, samples) {
  samples = samples || SCAN_SAMPLES;
  if (!catalog || !catalog.length) {
    return { storage: storageTb, egress_max: 0, current: null, switches: [] };
  }
  var egressMax = Math.max(SCAN_FLOOR_TB, egressTb * 2);
  var step = egressMax / samples;
  var prev = cheapest(catalog, storageTb, 0);
  if (!prev) {
    return { storage: storageTb, egress_max: 0, current: null, switches: [] };
  }
  var switches = [];
  for (var i = 1; i <= samples; i++) {
    var point = step * i;
    var here = cheapest(catalog, storageTb, point);
    if (here.row.slug === prev.row.slug) continue;
    var lo = step * (i - 1), hi = point;
    for (var k = 0; k < 40; k++) {
      var mid = (lo + hi) / 2;
      if (cheapest(catalog, storageTb, mid).row.slug === prev.row.slug) lo = mid;
      else hi = mid;
    }
    var won = cheapest(catalog, storageTb, hi);
    switches.push({
      at: hi, from: prev.row.name, to: won.row.name,
      from_slug: prev.row.slug, to_slug: won.row.slug, cost: won.m.total
    });
    prev = won;
  }
  var current = cheapest(catalog, storageTb, egressTb);
  return {
    storage: storageTb, egress_max: egressMax,
    current: { slug: current.row.slug, name: current.row.name, total: current.m.total },
    switches: switches
  };
}

/* Volume for display: one decimal below 10 TB, whole TB above. Integer maths
   on both sides so Python and JS cannot print different sentences. */
function fmtTb(v) {
  var tenths = Math.floor(v * 10 + 0.5);
  if (tenths < 10) return Math.floor(tenths / 10) + '.' + (tenths % 10);
  return String(Math.floor(v + 0.5));
}

function crossoverText(catalog, storageTb, egressTb) {
  if (storageTb <= 0) {
    return {
      headline: 'No storage volume entered.',
      items: ['With nothing stored, every provider starts at $0, so there is ' +
              'no cheapest option to compare. Enter a storage volume above ' +
              'zero to see where the leader changes.']
    };
  }
  var r = crossover(catalog, storageTb, egressTb);
  if (!r.current) return { headline: '', items: [] };
  var headline = 'At ' + storageTb + ' TB stored, ' + r.current.name +
    ' is the cheapest choice.';
  var items = [];
  for (var i = 0; i < r.switches.length; i++) {
    var s = r.switches[i];
    items.push('From ' + fmtTb(s.at) + ' TB/month of egress onward, ' + s.to +
      ' is the cheapest option (' + usd(s.cost) + '/month).');
  }
  if (!items.length) {
    items.push('No provider overtakes ' + r.current.name + ' anywhere up to ' +
      fmtTb(r.egress_max) + ' TB/month of egress.');
  }
  return { headline: headline, items: items };
}


/* Computed totals round like money: two decimals. Published unit rates keep
   their extra precision elsewhere on the site. */
function usd(v) {
  if (!v) return '$0.00';
  return '$' + v.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    egressCharge: egressCharge, monthly: monthly, tiered: tiered,
    cheapest: cheapest, crossover: crossover, crossoverText: crossoverText,
    fmtTb: fmtTb, usd: usd
  };
}

if (typeof document !== 'undefined') {
  (function () {
    var dataEl = document.getElementById('tco-data');
    var storageEl = document.getElementById('tco-storage');
    var egressEl = document.getElementById('tco-egress');
    var body = document.getElementById('tco-body');
    var caption = document.getElementById('tco-caption');
    if (!dataEl || !storageEl || !egressEl || !body) return;

    var CATALOG;
    try { CATALOG = JSON.parse(dataEl.textContent); } catch (e) { return; }
    if (!CATALOG || !CATALOG.length) return;

    var xoHeadline = document.getElementById('xo-headline');
    var xoList = document.getElementById('xo-list');
    var urlTimer = null;
    var MAX_TB = 10000;

    function readInputs() {
      var s = parseFloat(storageEl.value);
      var e = parseFloat(egressEl.value);
      if (!isFinite(s) || s < 0) s = 0;
      if (!isFinite(e) || e < 0) e = 0;
      return { storage: s, egress: e };
    }

    /* Shareable scenario: the same URL reproduces the same table. replaceState
       rather than pushState so typing does not fill the back button with every
       keystroke. The page's canonical still points at the bare /calculator/. */
    function syncUrl(input) {
      if (!window.history || !window.history.replaceState) return;
      try {
        window.history.replaceState(null, '',
          window.location.pathname + '?s=' + input.storage + '&e=' + input.egress);
      } catch (e) { /* file:// or a blocked history API — the table still works */ }
    }

    function scheduleUrl(input) {
      if (urlTimer) clearTimeout(urlTimer);
      urlTimer = setTimeout(function () { syncUrl(input); }, 250);
    }

    /* Only accept a parameter that parses to a real, non-negative volume.
       Anything else keeps the server-rendered default rather than being
       rewritten to zero: a URL saying e=-5 should show the default scenario,
       not silently pretend the visitor asked for something else. */
    function applyQuery() {
      var q = window.location.search;
      if (!q || q.length < 2) return;
      var ms = /[?&]s=([^&]*)/.exec(q);
      var me = /[?&]e=([^&]*)/.exec(q);
      if (ms) {
        var s = parseFloat(decodeURIComponent(ms[1]));
        if (isFinite(s) && s >= 0) storageEl.value = Math.min(MAX_TB, s);
      }
      if (me) {
        var e = parseFloat(decodeURIComponent(me[1]));
        if (isFinite(e) && e >= 0) egressEl.value = Math.min(MAX_TB, e);
      }
    }

    function render(sync) {
      var input = readInputs();
      var rows = CATALOG.map(function (row) {
        return { row: row, m: monthly(row, input.storage, input.egress) };
      });
      rows.sort(function (a, b) {
        return (a.m.total - b.m.total) || a.row.name.localeCompare(b.row.name);
      });

      var frag = document.createDocumentFragment();
      rows.forEach(function (item) {
        var tr = body.querySelector('tr[data-slug="' + item.row.slug + '"]');
        if (!tr) return;
        var cellStorage = tr.querySelector('.c-storage');
        var cellEgress = tr.querySelector('.c-egress');
        var cellTotal = tr.querySelector('.c-total');
        var cellPer = tr.querySelector('.c-per');
        if (cellStorage) cellStorage.textContent = usd(item.m.storage);
        if (cellEgress) cellEgress.textContent = usd(item.m.egress);
        if (cellTotal) {
          var tag = cellTotal.querySelector('.cost') || cellTotal;
          tag.textContent = usd(item.m.total);
          tag.className = 'cost ' + (item.m.total === 0 ? 'zero' : 'total');
        }
        if (cellPer) {
          cellPer.textContent = input.storage > 0 ? usd(item.m.total / input.storage) : '\u2014';
        }
        frag.appendChild(tr);
      });
      body.appendChild(frag);

      if (xoHeadline && xoList) {
        var xo = crossoverText(CATALOG, input.storage, input.egress);
        xoHeadline.textContent = xo.headline;
        var items = document.createDocumentFragment();
        for (var k = 0; k < xo.items.length; k++) {
          var li = document.createElement('li');
          li.textContent = xo.items[k];
          items.appendChild(li);
        }
        xoList.textContent = '';
        xoList.appendChild(items);
      }

      if (caption) {
        var leader = rows[0];
        caption.textContent = 'Showing ' + rows.length + ' providers at ' +
          input.storage + ' TB stored and ' + input.egress +
          ' TB of egress per month, cheapest total first.' +
          (leader ? ' Cheapest here is ' + leader.row.name + ' at ' +
            usd(leader.m.total) + ' per month.' : '');
      }

      var buttons = document.querySelectorAll('.preset');
      for (var i = 0; i < buttons.length; i++) {
        var match = parseFloat(buttons[i].getAttribute('data-s')) === input.storage &&
                    parseFloat(buttons[i].getAttribute('data-e')) === input.egress;
        buttons[i].setAttribute('aria-pressed', match ? 'true' : 'false');
      }

      if (sync) scheduleUrl(input);
    }

    function onPreset(event) {
      var btn = event.currentTarget;
      storageEl.value = btn.getAttribute('data-s');
      egressEl.value = btn.getAttribute('data-e');
      render(true);
    }

    function onInput() { render(true); }

    storageEl.addEventListener('input', onInput);
    egressEl.addEventListener('input', onInput);
    storageEl.addEventListener('change', onInput);
    egressEl.addEventListener('change', onInput);

    var presets = document.querySelectorAll('.preset');
    for (var i = 0; i < presets.length; i++) {
      presets[i].addEventListener('click', onPreset);
    }

    applyQuery();
    render(false);
  })();
}
