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
// 팝업: 바깥을 눌러도 안 닫힘 · 오른쪽 위 ✕ (PC 는 Esc) 로만 닫기
function modal(html) {
  const m = document.createElement('div'); m.className = 'modal';
  m.innerHTML = `<div class="sheet-box"><button type="button" class="x" aria-label="닫기">✕ 닫기</button><div class="sheet">${html}</div></div>`;
  const esc = e => { if (e.key === 'Escape' && document.querySelector('.modal:last-of-type') === m) m.remove(); };
  const obs = new MutationObserver(() => { if (!m.isConnected) { document.removeEventListener('keydown', esc); obs.disconnect(); } });
  m.querySelector('.x').onclick = () => m.remove();
  document.addEventListener('keydown', esc);
  document.body.appendChild(m); obs.observe(document.body, {childList: true}); return m;
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
// 달력 (월 단위) — o: {ym:'2026-10', min, max:'YYYY-MM-DD', selected, cell(d) → {off, cls, note}, onPick(d), onMonth(ym)}
function calendar(el, o) {
  const [y, m] = o.ym.split('-').map(Number), first = new Date(Date.UTC(y, m - 1, 1)), days = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const pad = n => String(n).padStart(2, '0'), ymOf = d => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}`;
  const prev = ymOf(new Date(Date.UTC(y, m - 2, 1))), next = ymOf(new Date(Date.UTC(y, m, 1)));
  const today = new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);
  let cells = '';
  for (let i = 0; i < first.getUTCDay(); i++) cells += '<span></span>';
  for (let d = 1; d <= days; d++) {
    const v = `${y}-${pad(m)}-${pad(d)}`, wd = (first.getUTCDay() + d - 1) % 7;
    const c = o.cell ? o.cell(v) : {}, out = (o.min && v < o.min) || (o.max && v > o.max);
    cells += `<button type="button" data-d="${v}" class="${c.cls || ''} ${v === o.selected ? 'sel' : ''} ${v === today ? 'today' : ''} ${wd === 0 ? 'sun' : wd === 6 ? 'sat' : ''}" ${out || c.off ? 'disabled' : ''}>
      <b>${d}</b>${c.note ? `<small>${esc(c.note)}</small>` : ''}</button>`;
  }
  el.innerHTML = `<div class="cal"><div class="cal-hd"><button type="button" data-m="${prev}" ${o.min && prev < o.min.slice(0, 7) ? 'disabled' : ''}>‹</button>
    <b>${y}년 ${m}월</b><button type="button" data-m="${next}" ${o.max && next > o.max.slice(0, 7) ? 'disabled' : ''}>›</button></div>
    <div class="cal-grid">${'일월화수목금토'.split('').map((w, i) => `<i class="${i === 0 ? 'sun' : i === 6 ? 'sat' : ''}">${w}</i>`).join('')}${cells}</div></div>`;
  el.querySelectorAll('[data-m]').forEach(b => b.onclick = () => o.onMonth(b.dataset.m));
  el.querySelectorAll('[data-d]').forEach(b => b.onclick = () => o.onPick(b.dataset.d));
}
