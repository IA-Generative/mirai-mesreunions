"""Wizard standalone template — extrait de main.py en PR3-v2.

Ne contient que le ``PREP_BRIEF_TEMPLATE`` rendu par ``/meeting-prep/new``.
Le template reste inline (raw triple-quoted) plutôt quun fichier Jinja
séparé pour ne pas modifier le pipeline de rendu (render_template_string).
"""

PREP_BRIEF_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MIrAI - Préparer un brief de réunion</title>
  <link rel="icon" type="image/png" sizes="192x192" href="/static/icons/pwa-icon-192.png">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14.2/dist/dsfr/dsfr.min.css">
  <style>
    * { box-sizing: border-box; }
    body { background:#f6f6f6; color:#161616; margin:0; min-height:100vh; }
    .page { max-width: 760px; margin:0 auto; padding:1.5rem 1rem 3rem; }
    .card { background:#fff; border:1px solid #e5e5e5; border-radius:0.5rem;
            padding:1.25rem; margin-bottom:1rem; }
    h1 { font-size: 1.3rem; color:#1a1a2e; margin:0 0 0.4rem; }
    .banner { background:#eef3fb; border:1px solid #cfd9eb; padding:0.85rem 1rem;
              border-radius:0.5rem; font-size:0.9rem; color:#1a2640;
              margin-bottom: 1rem; line-height: 1.45; }
    .banner strong { display:block; margin-bottom:0.2rem; font-size:0.95rem; }
    .question { margin-bottom: 1.15rem; }
    .question > label { display:block; font-size:0.9rem; font-weight:600;
                        color:#1a1a2e; margin-bottom:0.35rem; }
    .question .hint { font-size:0.78rem; color:#64748b; margin-bottom:0.4rem; }
    input[type=text], input[type=number], textarea {
      width:100%; padding:0.55rem 0.7rem; border:1px solid #d0d5dd;
      border-radius:6px; font-size:0.92rem; background:#fff; color:#161616;
      font-family: inherit;
    }
    input:focus, textarea:focus { outline:2px solid #6a6af4; outline-offset:0; }
    .chips { display:flex; flex-wrap:wrap; gap:0.4rem; margin-top:0.4rem; }
    .chip { display:inline-block; padding:0.25rem 0.6rem; background:#f1f3f9;
            border:1px solid #d8def0; border-radius:999px; font-size:0.78rem;
            color:#33396b; cursor:pointer; user-select:none; }
    .chip:hover { background:#e3e8f6; }
    .focus-grid { display:grid; grid-template-columns: 1fr 1fr; gap:0.35rem 1rem;
                  margin-top:0.2rem; }
    .focus-grid label { font-size:0.85rem; font-weight:400; color:#1a1a2e;
                        display:flex; align-items:center; gap:0.45rem;
                        cursor:pointer; }
    .actions { display:flex; justify-content:space-between; align-items:center;
               margin-top:0.5rem; }
    .btn { padding:0.55rem 0.95rem; border-radius:6px; border:none;
           font-size:0.9rem; font-weight:600; cursor:pointer; }
    .btn-primary { background:#000091; color:#fff; }
    /* Meeting-prep v2 §5b : explicite la couleur de texte au hover/focus
       du bouton primaire pour éviter la régression d'inversion de contraste
       (label blanc sur fond gris clair hérité du CSS commun du portail). */
    .btn-primary:hover, .btn-primary:focus-visible, .btn-primary:active {
      background:#1212a0; color:#fff;
    }
    .btn-primary:disabled { background:#94a3b8; cursor:not-allowed; }
    .btn-secondary { background:#fff; color:#000091; border:1px solid #000091; }
    .btn-secondary:hover, .btn-secondary:focus-visible {
      background:#eef3fb; color:#000091;
    }
    .status { margin-top:0.8rem; padding:0.55rem 0.75rem; border-radius:6px;
              font-size:0.85rem; display:none; }
    .status.info { background:#eef3fb; color:#1a2640; display:block; }
    .status.err { background:#fbe5e5; color:#7a1f1f; display:block; }
    .brief-output { white-space: normal; }
    .brief-section { margin-bottom: 0.9rem; }
    .brief-section h3 { font-size:0.95rem; color:#1a1a2e; margin:0 0 0.35rem; }
    .brief-section ul { margin: 0; padding-left: 1.2rem; }
    .brief-section li { font-size: 0.88rem; margin-bottom: 0.2rem; }
    .doc-list { font-size: 0.78rem; color: #475569; margin-top: 0.5rem; }
    .doc-list li.ingested { color: #1a4d2b; }
    .doc-list li.skipped, .doc-list li.error { color: #7a4a1f; }
    .header-bar { display:flex; justify-content:space-between; align-items:baseline;
                  margin-bottom:0.8rem; }
    .header-bar .who { font-size:0.78rem; color:#475569; }
    .header-bar a { font-size:0.85rem; color:#000091; }
  </style>
</head>
<body>
<main class="page">
  <div class="header-bar">
    <span class="who">{{ user.name or user.email }}</span>
    <span><a href="/">Retour à l'accueil</a> · <a href="/logout">Déconnexion</a></span>
  </div>

  <div class="banner">
    <strong>📋 Préparer votre brief de réunion</strong>
    L'IA lit vos documents de prép (Drive) et produit un brief personnalisé +
    un glossaire qui servira à mieux retranscrire l'audio de la réunion.
    4 questions rapides pour adapter le brief à votre besoin.
  </div>

  <form id="prep-form" class="card" autocomplete="off">
    <h1>Brief de réunion</h1>

    <!-- Meeting-prep v2 §7 : badge "Suite de : <titre>" si série.
         Rempli côté JS par le handler du paramètre URL ?series_parent_id=<id>. -->
    <div id="series-parent-banner" data-series-parent-banner
         style="display:none;background:#eef3fb;border:1px solid #c0d2f5;
                border-radius:0.4rem;padding:0.45rem 0.7rem;margin-bottom:0.8rem;
                font-size:0.85rem;color:#1a2640;">
      Suite de : <strong id="series-parent-title">…</strong>
    </div>
    <input type="hidden" id="series_parent_id" name="series_parent_id" value="" />

    <div class="question">
      <label for="meeting_type">Type de réunion *</label>
      <div class="hint">Le brief sera structuré selon ce type.</div>
      <select id="meeting_type" name="meeting_type" required
              style="width:100%; padding:0.55rem 0.7rem; border:1px solid #d0d5dd;
                     border-radius:6px; font-size:0.92rem; background:#fff;
                     color:#161616; font-family: inherit;">
        <option value="general">Général</option>
        <option value="one_on_one">Entretien 1:1</option>
        <option value="project_update">Point projet / équipe</option>
        <option value="steering_committee">Comité de pilotage (COPIL)</option>
        <option value="brainstorm">Atelier / brainstorm</option>
      </select>
    </div>

    <div class="question">
      <label for="subject">Sujet de la réunion *</label>
      <div class="hint">En une phrase, le thème ou la décision principale.</div>
      <input type="text" id="subject" name="subject" required maxlength="500"
             placeholder="Ex : Arbitrer la trajectoire budgétaire 2027 du programme X">
    </div>

    <div class="question">
      <label for="drive_folder">Dossier Drive</label>
      <div class="hint">Optionnel — si renseigné, les documents seront lus pour enrichir le brief. Collez l'URL du dossier mesfichiers (ou l'identifiant brut).</div>
      <input type="text" id="drive_folder" name="drive_folder"
             placeholder="https://mesfichiers.…/explorer/items/xxxxxxxx">
      <div class="drive-actions" style="display:flex; flex-wrap:wrap; gap:0.5rem; margin-top:0.5rem; align-items:center;">
        {% if drive_base_url %}
        <a id="open-drive-btn" class="btn btn-secondary" style="padding:0.35rem 0.7rem; font-size:0.82rem; text-decoration:none; display:inline-block;"
           href="{{ drive_base_url }}" target="_blank" rel="noopener">
          Ouvrir mes fichiers ↗
        </a>
        {% else %}
        <a id="open-drive-btn" class="btn btn-secondary" style="padding:0.35rem 0.7rem; font-size:0.82rem; text-decoration:none; display:inline-block; opacity:0.5; cursor:not-allowed;"
           href="#" aria-disabled="true" title="DRIVE_BASE_URL non configuré côté serveur"
           onclick="event.preventDefault(); return false;">
          Ouvrir mes fichiers ↗
        </a>
        {% endif %}
        <button type="button" id="test-drive-btn" class="btn btn-secondary"
                style="padding:0.35rem 0.7rem; font-size:0.82rem;">
          Tester l'accès
        </button>
      </div>
      <div id="test-drive-result" aria-live="polite" style="margin-top:0.5rem; font-size:0.82rem;"></div>
    </div>

    <div class="question">
      <label for="role">1. Quel est votre rôle dans cette réunion ? *</label>
      <input type="text" id="role" name="role" required maxlength="300"
             placeholder="J'anime la réunion, je participe, je dois décider…">
      <div class="chips" data-target="role">
        <span class="chip">J'anime la réunion</span>
        <span class="chip">J'y participe</span>
        <span class="chip">Je dois décider</span>
        <span class="chip">Je découvre l'équipe</span>
        <span class="chip">J'observe</span>
      </div>
    </div>

    <div class="question">
      <label for="expectation">2. Qu'attendez-vous principalement de ce brief ? *</label>
      <input type="text" id="expectation" name="expectation" required maxlength="300"
             placeholder="Comprendre le contexte, anticiper les objections…">
      <div class="chips" data-target="expectation">
        <span class="chip">Comprendre le contexte</span>
        <span class="chip">Préparer ma prise de parole</span>
        <span class="chip">Anticiper les objections</span>
        <span class="chip">Valider une décision</span>
        <span class="chip">Apprendre le vocabulaire</span>
      </div>
    </div>

    <div class="question">
      <label>3. Sur quoi concentrer l'analyse ? (plusieurs choix possibles)</label>
      <div class="focus-grid">
        <label><input type="checkbox" name="focus" value="Aspects budgétaires"> Aspects budgétaires</label>
        <label><input type="checkbox" name="focus" value="Risques et points de vigilance"> Risques et points de vigilance</label>
        <label><input type="checkbox" name="focus" value="Décisions à prendre"> Décisions à prendre</label>
        <label><input type="checkbox" name="focus" value="Historique des échanges"> Historique des échanges</label>
        <label><input type="checkbox" name="focus" value="Acronymes et jargon"> Acronymes et jargon</label>
        <label><input type="checkbox" name="focus" value="Parties prenantes"> Parties prenantes</label>
        <label><input type="checkbox" name="focus" value="Calendrier et jalons"> Calendrier et jalons</label>
      </div>
    </div>

    <div class="question">
      <label for="duration">4. Durée prévue de la réunion *</label>
      <input type="text" id="duration" name="duration" required
             placeholder="Ex : 1 heure">
      <div class="chips" data-target="duration">
        <span class="chip" data-minutes="15">15 minutes</span>
        <span class="chip" data-minutes="30">30 minutes</span>
        <span class="chip" data-minutes="60">1 heure</span>
        <span class="chip" data-minutes="120">2 heures</span>
        <span class="chip" data-minutes="240">Demi-journée</span>
        <span class="chip" data-minutes="480">Journée complète</span>
      </div>
    </div>

    <div class="actions">
      <a class="btn btn-secondary" href="/">Annuler</a>
      <button id="submit-btn" class="btn btn-primary" type="submit">Générer le brief</button>
    </div>

    <div id="status" class="status"></div>
  </form>

  <div id="result" class="card" style="display:none;">
    <h1>Brief généré</h1>
    <div id="brief" class="brief-output"></div>
    <h3 style="margin-top:1rem;font-size:0.9rem;color:#1a1a2e;">Documents ingérés</h3>
    <ul id="docs" class="doc-list"></ul>
  </div>
</main>

<script>
  // Chip-to-input behaviour: clicking a chip fills the target text field
  // (and stores a parsed numeric value for the "duration" chips). The user
  // can then edit the filled text freely — chip is suggestion, not lock-in.
  document.querySelectorAll('.chips').forEach(function (group) {
    var targetId = group.getAttribute('data-target');
    var target = document.getElementById(targetId);
    if (!target) return;
    group.querySelectorAll('.chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        target.value = chip.textContent.trim();
        if (chip.dataset.minutes) {
          target.dataset.minutes = chip.dataset.minutes;
        } else {
          delete target.dataset.minutes;
        }
        target.focus();
      });
    });
  });
  // If the user types a custom duration like "45 minutes" or "1h30", we
  // parse it client-side; chip clicks short-circuit by setting dataset.minutes.
  function parseDurationMinutes(raw) {
    if (!raw) return null;
    var s = raw.trim().toLowerCase();
    var explicit = document.getElementById('duration').dataset.minutes;
    if (explicit) {
      var n = parseInt(explicit, 10);
      if (!isNaN(n) && n > 0) return n;
    }
    if (s === 'demi-journée') return 240;
    if (s === 'journée complète' || s === 'journée') return 480;
    var hM = s.match(/^(\d+)\s*h\s*(\d+)?$/);
    if (hM) return parseInt(hM[1], 10) * 60 + (hM[2] ? parseInt(hM[2], 10) : 0);
    var hOnly = s.match(/^(\d+)\s*(heure|heures|h)$/);
    if (hOnly) return parseInt(hOnly[1], 10) * 60;
    var mOnly = s.match(/^(\d+)\s*(minute|minutes|min|m)?$/);
    if (mOnly) return parseInt(mOnly[1], 10);
    return null;
  }

  var statusEl = document.getElementById('status');
  function setStatus(msg, kind) {
    statusEl.textContent = msg;
    statusEl.className = 'status ' + (kind || 'info');
  }
  function clearStatus() { statusEl.className = 'status'; statusEl.textContent = ''; }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderBrief(brief) {
    var html = '';
    function section(title, body) {
      if (!body) return '';
      return '<div class="brief-section"><h3>' + escapeHtml(title) + '</h3>' + body + '</div>';
    }
    function asUl(items, fmt) {
      if (!Array.isArray(items) || !items.length) return '';
      return '<ul>' + items.map(function (it) {
        return '<li>' + (fmt ? fmt(it) : escapeHtml(it)) + '</li>';
      }).join('') + '</ul>';
    }
    html += section('Objectif reformulé', brief.objective_reformulated
                    ? '<p>' + escapeHtml(brief.objective_reformulated) + '</p>' : '');
    html += section('Contexte', brief.context_recap
                    ? '<p>' + escapeHtml(brief.context_recap) + '</p>' : '');
    html += section('Agenda', asUl(brief.agenda, function (a) {
      return '<strong>' + escapeHtml(a.title || '') + '</strong>'
           + (a.duration_minutes ? ' (' + a.duration_minutes + ' min)' : '')
           + (a.objective ? ' — ' + escapeHtml(a.objective) : '')
           + asUl(a.key_questions);
    }));
    html += section('Fils ouverts', asUl(brief.open_threads, function (t) {
      return escapeHtml(t.item || '') + (t.source ? ' <em>(' + escapeHtml(t.source) + ')</em>' : '');
    }));
    html += section('Notes participants', asUl(brief.participants_notes, function (p) {
      return '<strong>' + escapeHtml(p.name || '') + '</strong> — ' + escapeHtml(p.note || '');
    }));
    html += section("Questions d'ouverture", asUl(brief.opening_questions));
    html += section('Points de vigilance', asUl(brief.risk_points));
    html += section('Checklist de préparation', asUl(brief.preparation_checklist));
    return html || '<p>(Brief vide — le LLM n\'a rien produit d\'exploitable.)</p>';
  }

  function renderDocs(docs) {
    var ul = document.getElementById('docs');
    ul.innerHTML = '';
    (docs || []).forEach(function (d) {
      var li = document.createElement('li');
      li.className = (d.status === 'ingested') ? 'ingested'
                   : (d.status && d.status.indexOf('error') === 0) ? 'error' : 'skipped';
      var label = d.name + ' — ' + d.status;
      if (d.chars) label += ' (' + d.chars + ' car.)';
      li.textContent = label;
      ul.appendChild(li);
    });
  }

  document.getElementById('prep-form').addEventListener('submit', async function (ev) {
    ev.preventDefault();
    clearStatus();
    document.getElementById('result').style.display = 'none';

    var subject = document.getElementById('subject').value.trim();
    var folder = document.getElementById('drive_folder').value.trim();
    var meetingType = document.getElementById('meeting_type').value;
    var role = document.getElementById('role').value.trim();
    var expectation = document.getElementById('expectation').value.trim();
    var duration = parseDurationMinutes(document.getElementById('duration').value);
    var focus = Array.from(document.querySelectorAll('input[name="focus"]:checked'))
                     .map(function (cb) { return cb.value; });

    // Le dossier Drive est désormais optionnel — on ne le bloque plus côté client.
    if (!subject || !role || !expectation) {
      setStatus('Tous les champs marqués * sont requis.', 'err');
      return;
    }
    if (!duration) {
      setStatus('Durée non reconnue — utilisez une suggestion ou un format comme "45 minutes", "1h30".', 'err');
      return;
    }

    var btn = document.getElementById('submit-btn');
    btn.disabled = true;
    setStatus(folder
      ? 'Lecture du Drive et génération du brief en cours…'
      : 'Génération du brief en cours…',
      'info');
    try {
      var spId = (document.getElementById('series_parent_id') || {}).value || '';
      var _bodyObj = {
        subject: subject, drive_folder: folder, role: role,
        expectation: expectation, duration_minutes: duration, focus: focus,
        meeting_type: meetingType,
      };
      if (spId) _bodyObj.series_parent_id = spId;
      var resp = await fetch('/api/preparations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(_bodyObj),
      });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) {
        setStatus(data.error || ('Erreur ' + resp.status), 'err');
        return;
      }
      clearStatus();
      document.getElementById('brief').innerHTML = renderBrief(data.brief || {});
      renderDocs(data.documents || []);
      document.getElementById('result').style.display = 'block';
      document.getElementById('result').scrollIntoView({ behavior: 'smooth' });
    } catch (err) {
      setStatus('Erreur réseau : ' + (err && err.message ? err.message : err), 'err');
    } finally {
      btn.disabled = false;
    }
  });

  // Bouton « Tester l'accès » — diagnostic en 3 étapes sur /api/preparations/test-drive.
  var testBtn = document.getElementById('test-drive-btn');
  if (testBtn) {
    testBtn.addEventListener('click', async function () {
      var btn = this;
      var out = document.getElementById('test-drive-result');
      btn.disabled = true;
      out.innerHTML = 'Test en cours…';
      out.style.color = '';
      try {
        // Si l'utilisateur a déjà saisi un dossier Drive, on pousse son
        // id dans la query pour tester aussi le listing children/ — c'est
        // ce probe qui révèle les 403 spécifiques aux sous-collections.
        var folderInput = document.getElementById('drive_folder');
        var folderRaw = folderInput ? folderInput.value.trim() : '';
        var url = '/api/preparations/test-drive';
        if (folderRaw) {
          var match = folderRaw.match(/\/(?:items|folders)\/([^\/?#\s]+)/);
          var folderId = match ? match[1] : folderRaw;
          url += '?folder_id=' + encodeURIComponent(folderId);
        }
        var resp = await fetch(url);
        var data = await resp.json().catch(function () { return {}; });
        var rows = [
          ['Refresh token stocké', !!data.token_stored],
          ['Échange OIDC réussi', !!data.exchange_ok],
          ['Drive accessible', !!data.drive_reachable],
        ];
        if (data.children_probe) {
          var cp = data.children_probe;
          rows.push(['Listing du dossier (' + (cp.status_code || '?') + ')',
                     cp.status_code >= 200 && cp.status_code < 300]);
        }
        out.innerHTML = rows.map(function (r) {
          return '<div>' + (r[1] ? '✅' : '❌') + ' ' + escapeHtml(r[0]) + '</div>';
        }).join('') + (data.error
          ? '<div style="color:#c00;margin-top:0.25rem">' + escapeHtml(data.error) + '</div>'
          : '');
      } catch (e) {
        out.textContent = 'Erreur réseau : ' + (e && e.message ? e.message : e);
        out.style.color = '#c00';
      } finally {
        btn.disabled = false;
      }
    });
  }

  // Meeting-prep v2 §7 — pré-remplissage du wizard depuis ?series_parent_id=<id>.
  // Fetche le brief parent pour afficher "Suite de : <titre>" et pré-remplir
  // sujet/rôle/expectation à partir du parent.
  (async function _prefillFromSeriesParent() {
    try {
      var params = new URLSearchParams(window.location.search || '');
      var pid = params.get('series_parent_id');
      if (!pid) return;
      var hidden = document.getElementById('series_parent_id');
      if (hidden) hidden.value = pid;
      var r = await fetch('/api/preparations/' + encodeURIComponent(pid));
      if (!r.ok) return;
      var d = await r.json();
      var b = (d && (d.preparation || d.brief)) || {};
      var banner = document.getElementById('series-parent-banner');
      var titleEl = document.getElementById('series-parent-title');
      if (banner && titleEl) {
        titleEl.textContent = b.title || b.subject || '(brief parent)';
        banner.style.display = '';
      }
      // Pré-remplissage best-effort.
      var subj = document.getElementById('subject');
      if (subj && !subj.value && b.subject) subj.value = b.subject;
      var roleEl = document.getElementById('role');
      if (roleEl && !roleEl.value && b.role) roleEl.value = b.role;
      var expEl = document.getElementById('expectation');
      if (expEl && !expEl.value && b.expectation) expEl.value = b.expectation;
      var mt = document.getElementById('meeting_type');
      if (mt && b.meeting_type) {
        var found = false;
        for (var i = 0; i < mt.options.length; i++) {
          if (mt.options[i].value === b.meeting_type) { found = true; break; }
        }
        if (found) mt.value = b.meeting_type;
      }
    } catch (e) { /* non-fatal */ }
  })();
</script>
</body>
</html>
"""
