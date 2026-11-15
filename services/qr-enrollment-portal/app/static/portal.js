// QR Enrollment Portal — génération de QR et gestion des appareils.
// Toutes les requêtes qui modifient l'état portent l'en-tête anti-CSRF
// attendu par le serveur.
'use strict'

const CSRF = { 'X-Requested-With': 'qr-enrollment-portal' }

async function api(path, options = {}) {
  const resp = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: { 'Content-Type': 'application/json', ...CSRF, ...(options.headers || {}) },
  })
  let body = {}
  try {
    body = await resp.json()
  } catch {
    /* corps vide */
  }
  if (!resp.ok) {
    throw new Error(body.message || 'Une erreur est survenue, réessayez.')
  }
  return body
}

function show(el, text) {
  el.textContent = text
  el.hidden = !text
}

function formatDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' })
}

function fillTtlOptions(select) {
  const def = Number(select.dataset.default)
  const max = Number(select.dataset.max)
  const choices = [5, 15, 60, 24 * 60, 7 * 24 * 60].filter((m) => m <= max)
  if (!choices.includes(def)) choices.unshift(def)
  for (const minutes of choices.sort((a, b) => a - b)) {
    const opt = document.createElement('option')
    opt.value = String(minutes)
    opt.textContent =
      minutes < 60 ? `${minutes} min` : minutes < 1440 ? `${minutes / 60} h` : `${minutes / 1440} j`
    opt.selected = minutes === def
    select.append(opt)
  }
}

async function generate(event) {
  event.preventDefault()
  const error = document.getElementById('generate-error')
  const button = event.target.querySelector('button')
  show(error, '')
  button.disabled = true
  try {
    const data = await api('/api/generate-code', {
      method: 'POST',
      body: JSON.stringify({ ttl_minutes: Number(document.getElementById('ttl').value) }),
    })
    document.getElementById('qr-image').src = data.qr_image
    document.getElementById('simple-code').textContent = data.simple_code
    document.getElementById('expires').textContent = formatDate(data.expires_at)
    document.getElementById('result').hidden = false
  } catch (e) {
    show(error, e.message)
  } finally {
    button.disabled = false
  }
}

function deviceRow(device) {
  const li = document.createElement('li')
  const info = document.createElement('div')
  const name = document.createElement('strong')
  name.textContent = device.device_name || 'Appareil sans nom'
  const meta = document.createElement('span')
  meta.className = 'muted'
  meta.textContent = [
    device.status,
    device.session_simple_code ? `code ${device.session_simple_code}` : null,
    `${device.recent_uploads_24h || 0} dépôt(s) en 24 h`,
    device.session_expires_at ? `expire ${formatDate(device.session_expires_at)}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
  info.append(name, document.createElement('br'), meta)

  const actions = document.createElement('div')
  const rename = document.createElement('button')
  rename.type = 'button'
  rename.textContent = 'Renommer'
  rename.addEventListener('click', async () => {
    const value = window.prompt("Nom de l'appareil", name.textContent)
    if (!value) return
    await runDeviceAction(`/api/my-devices/${encodeURIComponent(device.device_id)}/rename`, {
      device_name: value,
    })
  })
  const revoke = document.createElement('button')
  revoke.type = 'button'
  revoke.className = 'btn-danger'
  revoke.textContent = 'Révoquer'
  revoke.addEventListener('click', async () => {
    if (!window.confirm('Révoquer cet appareil ? Il ne pourra plus déposer de fichiers.')) return
    await runDeviceAction(`/api/my-devices/${encodeURIComponent(device.device_id)}/revoke`)
  })
  actions.append(rename, revoke)
  li.append(info, actions)
  return li
}

async function runDeviceAction(path, body = {}) {
  const error = document.getElementById('devices-error')
  show(error, '')
  try {
    await api(path, { method: 'POST', body: JSON.stringify(body) })
  } catch (e) {
    show(error, e.message)
  }
  await loadDevices()
}

async function loadDevices() {
  const list = document.getElementById('devices')
  const error = document.getElementById('devices-error')
  try {
    const devices = await api('/api/my-devices')
    list.replaceChildren(...devices.map(deviceRow))
    document.getElementById('devices-empty').hidden = devices.length > 0
  } catch (e) {
    show(error, e.message)
  }
}

document.addEventListener('DOMContentLoaded', () => {
  fillTtlOptions(document.getElementById('ttl'))
  document.getElementById('generate-form').addEventListener('submit', generate)
  document.getElementById('revoke-all').addEventListener('click', async () => {
    if (!window.confirm('Révoquer tous vos appareils ?')) return
    await runDeviceAction('/api/my-devices/revoke-all')
  })
  loadDevices()
})
