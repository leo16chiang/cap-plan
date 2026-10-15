(function () {
  var dataEl = document.getElementById('usage-data');
  if (!dataEl) return;
  var d = JSON.parse(dataEl.textContent);
  var inputs = Array.prototype.slice.call(document.querySelectorAll('.growth-input'));
  var previews = Array.prototype.slice.call(document.querySelectorAll('.msu-preview'));
  var css = getComputedStyle(document.documentElement);
  function token(name) { return css.getPropertyValue(name).trim(); }

  function pctFor(q) {
    var v = inputs[q] ? parseFloat(inputs[q].value) : NaN;
    return isNaN(v) || d.baseline === null ? null : v;
  }

  // Each quarter's % applies to its three months, relative to the current baseline.
  function newProjection() {
    var out = d.labels.map(function () { return null; });
    for (var q = 0; q < 4; q++) {
      var pct = pctFor(q);
      if (pct === null) continue;
      var v = Math.round(d.baseline * (1 + pct / 100) * 10) / 10;
      for (var k = 0; k < 3; k++) out[d.n_hist + q * 3 + k] = v;
    }
    return out;
  }

  var baselineLine = d.labels.map(function (_, i) { return i >= d.n_hist - (d.baseline_n || 1) ? d.baseline : null; });

  var chart = null;
  if (window.Chart) {
    Chart.defaults.font.family = token('--font');
    Chart.defaults.color = token('--muted');
    chart = new Chart(document.getElementById('usage-chart'), {
      type: 'line',
      data: {
        labels: d.labels,
        datasets: [
          { label: 'Actual', data: d.actual, borderColor: token('--series-actual'), backgroundColor: token('--series-actual'),
            borderWidth: 2, pointRadius: 2, tension: 0.25 },
          { label: d.prev_label ? d.prev_label.replace(/ review$/, '') + ' projection' : 'Last cycle projection', data: d.last_projection, borderColor: token('--series-last'), backgroundColor: token('--series-last'),
            borderWidth: 2, borderDash: [6, 4], pointRadius: 0 },
          { label: 'Your new projection', data: newProjection(), borderColor: token('--series-new'), backgroundColor: token('--series-new'),
            borderWidth: 2.5, pointRadius: 3, stepped: 'middle' },
          { label: 'Current baseline', data: baselineLine, borderColor: token('--grid-strong'), backgroundColor: token('--grid-strong'),
            borderWidth: 1, borderDash: [2, 3], pointRadius: 0 }
        ].concat(d.statistical_projection ? [
          { label: 'Statistical projection', data: d.statistical_projection, borderColor: token('--series-stat'), backgroundColor: token('--series-stat'),
            borderWidth: 1.5, borderDash: [3, 3], pointRadius: 0, hidden: false }
        ] : []).concat(d.adjusted_projection ? [
          { label: 'Capacity Planning adjusted', data: d.adjusted_projection, borderColor: token('--series-adj'), backgroundColor: token('--series-adj'),
            borderWidth: 2, pointRadius: 2, stepped: 'middle' }
        ] : [])
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { position: 'bottom', labels: { usePointStyle: true, pointStyle: 'line', boxWidth: 24 } },
          tooltip: {
            filter: function (item) { return item.parsed.y !== null; },
            callbacks: { label: function (c) { return ' ' + c.dataset.label + ': ' + c.parsed.y.toFixed(1) + ' MSU'; } }
          }
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 12 } },
          y: { grid: { color: token('--grid') }, title: { display: true, text: 'MSU' } }
        }
      }
    });
  }

  function refresh() {
    if (chart) {
      chart.data.datasets[2].data = newProjection();
      chart.update('none');
    }
    previews.forEach(function (out, q) {
      var pct = pctFor(q);
      out.textContent = pct === null ? '' : '≈ ' + Math.round(d.baseline * (1 + pct / 100)) + ' MSU';
    });
  }
  inputs.forEach(function (i) { i.addEventListener('input', refresh); });
  refresh();

  // Copy the statistical projection's percentages into the growth inputs (the team can then adjust).
  var useStat = document.getElementById('use-statistical');
  if (useStat && d.statistical) {
    useStat.addEventListener('click', function () {
      d.statistical.forEach(function (q, i) {
        if (inputs[i] && q.pct !== null) inputs[i].value = q.pct;
      });
      dirty = true;
      refresh();
    });
  }

  // "None planned" can't be combined with other choices.
  document.querySelectorAll('[data-exclusive]').forEach(function (group) {
    var exclusive = group.dataset.exclusive;
    group.addEventListener('change', function (e) {
      if (!e.target.checked) return;
      group.querySelectorAll('input[type=checkbox]').forEach(function (box) {
        if (box.checked && (e.target.value === exclusive ? box !== e.target : box.value === exclusive)) {
          box.checked = false;
          box.dispatchEvent(new Event('change'));  // lets the "Other" box hide itself
        }
      });
    });
  });

  // Show the "Other" text box only while "Other" is picked.
  document.querySelectorAll('[data-other-for]').forEach(function (box) {
    var name = box.dataset.otherFor;
    var controls = document.querySelectorAll('[name="' + name + '"]');
    function otherPicked() {
      return Array.prototype.some.call(controls, function (c) {
        return c.tagName === 'SELECT' ? c.value === 'Other' : (c.value === 'Other' && c.checked);
      });
    }
    controls.forEach(function (c) {
      c.addEventListener('change', function () {
        var show = otherPicked();
        var wasHidden = box.hidden;
        box.hidden = !show;
        if (show && wasHidden) box.querySelector('input').focus();
      });
    });
  });

  // Confirm submit / sign-off, and warn about unsaved edits to the questionnaire.
  var dirty = false;
  var form = document.getElementById('review-form');
  if (form) form.addEventListener('input', function () { dirty = true; });
  document.addEventListener('submit', function (e) {
    var btn = e.submitter;
    if (btn && btn.dataset.confirm && !confirm(btn.dataset.confirm)) { e.preventDefault(); return; }
    dirty = false;
  });
  window.addEventListener('beforeunload', function (e) { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

  var firstError = document.querySelector('.has-error');
  if (firstError) firstError.scrollIntoView({ block: 'center' });
})();
