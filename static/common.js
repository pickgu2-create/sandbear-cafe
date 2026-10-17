// 공통 도우미
const $ = (s, el = document) => el.querySelector(s);
const won = n => (Math.round(n) || 0).toLocaleString('ko-KR') + '원';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, body) {
  const opt = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  let j = {}; try { j = await r.json(); } catch (e) {}
  if (!r.ok) { const e = new Error(j.error || ('오류 ' + r.status)); e.status = r.status; throw e; }
  return j;
}
function toast(msg, ms = 2200) {
  let t = $('#toast'); if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add('on'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), ms);
}
// 알림음 (파일 없이 브라우저에서 소리 만들기)
let _ac;
function beep(times = 2) {
  try {
    _ac = _ac || new (window.AudioContext || window.webkitAudioContext)();
    for (let i = 0; i < times; i++) {
      const o = _ac.createOscillator(), g = _ac.createGain(), t = _ac.currentTime + i * 0.35;
      o.frequency.value = i % 2 ? 1046 : 784; o.type = 'sine';
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.4, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.3);
      o.connect(g).connect(_ac.destination); o.start(t); o.stop(t + 0.32);
    }
  } catch (e) {}
}
function unlockAudio() { try { _ac = _ac || new (window.AudioContext || window.webkitAudioContext)(); _ac.resume(); } catch (e) {} }
function modal(html) {
  const m = document.createElement('div'); m.className = 'modal';
  m.innerHTML = `<div class="sheet">${html}</div>`;
  m.addEventListener('click', e => { if (e.target === m) m.remove(); });
  document.body.appendChild(m); return m;
}
// 숫자 PIN 입력판
function pinPad(el, len, onDone) {
  let v = '';
  el.innerHTML = `<div class="pin-dots">${'<i></i>'.repeat(len)}</div><div class="pin-pad">${[1,2,3,4,5,6,7,8,9,'지움',0,'확인'].map(k => `<button type="button" data-k="${k}">${k}</button>`).join('')}</div>`;
  const draw = () => el.querySelectorAll('.pin-dots i').forEach((d, i) => d.classList.toggle('on', i < v.length));
  el.querySelectorAll('.pin-pad button').forEach(b => b.onclick = () => {
    const k = b.dataset.k;
    if (k === '지움') v = v.slice(0, -1);
    else if (k === '확인') { if (v.length >= 4) { onDone(v); v = ''; } }
    else if (v.length < len) v += k;
    draw();
    if (v.length === len) { onDone(v); v = ''; setTimeout(draw, 150); }
  });
  return { clear() { v = ''; draw(); } };
}
