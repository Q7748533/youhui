/* Recomputes the TCO table as inputs change. The server already rendered the
   default scenario, so this file only keeps the numbers in sync while typing.
   The three formula functions below are exported when loaded under Node so
   tests can compare them with tco.py scenario by scenario. */

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
  if (kind === 'tiered') {
    var tiers = calc.tiers || [], remaining = egressTb, prev = 0, total = 0;
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
  return 0;
}

function monthly(row, storageTb, egressTb) {
  var calc = row.egress || {};
  var billable = Math.max(storageTb, calc.min_storage_tb || 0);
  var storage = billable * row.storagePerTb;
  var egress = egressCharge(calc, billable, egressTb);
  var total = storage + egress;
  if (calc.min_monthly) total = Math.max(total, calc.min_monthly);
  return { storage: storage, egress: egress, total: total };
}

/* Computed totals round like money: two decimals. Published unit rates keep
   their extra precision elsewhere on the site. */
function usd(v) {
  if (!v) return '$0.00';
  return '$' + v.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { egressCharge: egressCharge, monthly: monthly, usd: usd };
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

    function readInputs() {
      var s = parseFloat(storageEl.value);
      var e = parseFloat(egressEl.value);
      if (!isFinite(s) || s < 0) s = 0;
      if (!isFinite(e) || e < 0) e = 0;
      return { storage: s, egress: e };
    }

    function render() {
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

      if (caption) {
        var cheapest = rows[0];
        caption.textContent = 'Showing ' + rows.length + ' providers at ' +
          input.storage + ' TB stored and ' + input.egress +
          ' TB of egress per month, cheapest total first.' +
          (cheapest ? ' Cheapest here is ' + cheapest.row.name + ' at ' +
            usd(cheapest.m.total) + ' per month.' : '');
      }

      var buttons = document.querySelectorAll('.preset');
      for (var i = 0; i < buttons.length; i++) {
        var match = parseFloat(buttons[i].getAttribute('data-s')) === input.storage &&
                    parseFloat(buttons[i].getAttribute('data-e')) === input.egress;
        buttons[i].setAttribute('aria-pressed', match ? 'true' : 'false');
      }
    }

    function onPreset(event) {
      var btn = event.currentTarget;
      storageEl.value = btn.getAttribute('data-s');
      egressEl.value = btn.getAttribute('data-e');
      render();
    }

    storageEl.addEventListener('input', render);
    egressEl.addEventListener('input', render);
    storageEl.addEventListener('change', render);
    egressEl.addEventListener('change', render);

    var presets = document.querySelectorAll('.preset');
    for (var i = 0; i < presets.length; i++) {
      presets[i].addEventListener('click', onPreset);
    }

    render();
  })();
}
