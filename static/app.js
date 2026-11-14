const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

let TOKEN = localStorage.getItem('nd_token') || '';
let IS_ADMIN = false;
let USERS = [];
let pollTimer = null;
let curPlat = 'netease';
let REQ_ROWS = [], PL_ROWS = [];

const PLAT_NAMES = { netease: '网易云', qq: 'QQ音乐', kugou: '酷狗', kuwo: '酷我' };

function toast(msg, ms = 2600) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.remove('show'), ms);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      'X-Panel-Request': '1',
      ...(TOKEN ? { Authorization: 'Bearer ' + TOKEN } : {}),
      ...(opts.headers || {}),
    },
  });
  let d = {};
  try { d = await r.json(); } catch (e) { d = {}; }
  if (r.status === 401) { logout(true); throw new Error('登录已失效'); }
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * (ts > 1e12 ? 1 : 1000));
  if (isNaN(d)) return '—';
  const p = n => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ---------- 登录 ---------- */
$('#loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = $('#loginBtn');
  btn.disabled = true;
  $('#loginErr').textContent = '';
  try {
    const d = await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({ username: $('#lu').value.trim(), password: $('#lp').value }),
    });
    TOKEN = d.token;
    localStorage.setItem('nd_token', TOKEN);
    IS_ADMIN = !!d.isAdmin;
    playBookThenEnter(d.username);
  } catch (err) {
    $('#loginErr').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

/* 登录成功：翻书过场。概览在书后先行渲染，动画结束揭页即见。 */
function playBookThenEnter(username) {
  const scene = $('#bookScene');
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  enterApp(username);
  if (reduce || !scene) return;
  scene.classList.remove('hidden');
  requestAnimationFrame(() => requestAnimationFrame(() => scene.classList.add('open')));
  setTimeout(() => scene.classList.add('fade'), 1400);
  setTimeout(() => {
    scene.classList.add('hidden');
    scene.classList.remove('open', 'fade');
  }, 1900);
}

function enterApp(username) {
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  $('#meName').textContent = username;
  // 普通用户只保留 概览 / 求歌 / 歌单
  const allowed = IS_ADMIN ? null : ['overview', 'plays', 'request', 'playlist'];
  $$('.side nav a').forEach(a => {
    const ok = !allowed || allowed.includes(a.dataset.page);
    a.style.display = ok ? '' : 'none';
  });
  if (allowed && !allowed.includes(curPage)) go('overview');
  syncHealthBtn();
  go(curPage);
}

function logout(silent) {
  if (!silent && TOKEN) api('/api/logout', { method: 'POST' }).catch(() => {});
  TOKEN = '';
  localStorage.removeItem('nd_token');
  clearInterval(pollTimer);
  $('#app').classList.add('hidden');
  $('#login').classList.remove('hidden');
}

$('#logoutBtn').addEventListener('click', () => logout(false));

/* ---------- 导航 ---------- */
let curPage = 'overview';
const TITLES = {
  overview: '概览', request: '求歌', playlist: '歌单',
  users: '用户', plays: '播放', review: '审核', servers: '线路', audit: '日志'
};

function go(page) {
  if (!IS_ADMIN && ['users', 'review', 'servers', 'audit'].includes(page)) return;
  curPage = page;
  $$('.side nav a').forEach(a => a.classList.toggle('on', a.dataset.page === page));
  $$('.page').forEach(p => p.classList.toggle('on', p.id === 'page-' + page));
  $('#pageTitle').textContent = TITLES[page] || '';
  // 切换页面后回到顶部，避免停留在上次滚动位置造成大片留白
  window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
  clearInterval(pollTimer);
  if (page === 'overview') { loadOverview(); loadOverviewPlays(); }
  if (page === 'users') { loadUsers(true); pollTimer = setInterval(() => loadUsers(false), 15000); }
  if (page === 'plays') loadPlays();
  if (page === 'review') { cursorRow = -1; loadReview(); }
  if (page === 'servers') loadServers();
  if (page === 'audit') loadAudit();
  if (page === 'request') { loadQuota(); loadSuggest(); }
  if (page === 'playlist') loadQuota();
}

$$('.side nav a').forEach(a => a.addEventListener('click', () => go(a.dataset.page)));

/* ---------- 播放数据 ---------- */
const fmtNum = n => (n == null ? '—' : Number(n).toLocaleString('zh-CN'));

function skeleton(rows = 3) {
  return `<div class="sk">${Array.from({length: rows}, (_, i) =>
    `<div class="sk-line" style="animation-delay:${i * 90}ms"></div>`).join('')}</div>`;
}

function stateBox(kind, title, desc, action) {
  const icon = kind === 'empty' ? 'empty' : kind === 'error' ? 'error' : 'loading';
  return `<div class="state ${icon}"><span class="state-mark"></span>
    <p class="state-t">${esc(title)}</p>
    ${desc ? `<p class="state-d">${esc(desc)}</p>` : ''}
    ${action || ''}</div>`;
}

function sparkBars(series, max) {
  if (!series || !series.length) return '';
  const m = max || Math.max(1, ...series.map(x => x.n));
  return `<div class="spark">${series.map(x => {
    const h = Math.max(2, Math.round(x.n / m * 100));
    return `<i style="height:${h}%" title="${x.d} ${x.n} 次"></i>`;
  }).join('')}</div>`;
}

async function loadOverviewPlays(force) {
  const box = $('#ovPlays');
  if (!box) return;
  box.innerHTML = skeleton(2);
  try {
    const d = await api('/api/stats' + (force ? '?force=1' : ''));
    const t = d.totals || {};
    const t7 = (d.trend || []).slice(-7).reduce((a, x) => a + x.n, 0);
    let html = `<div class="cards">
      <div class="card"><div class="k">曲库歌曲</div><div class="v">${fmtNum(t.songs)}</div></div>
      <div class="card"><div class="k">累计播放</div><div class="v">${fmtNum(t.plays)}</div></div>
      <div class="card"><div class="k">近 7 天</div><div class="v">${fmtNum(t7)}</div></div>
      <div class="card"><div class="k">媒体在线</div><div class="v">${d.online}<span class="v-sub"> / ${d.total}</span></div></div>
    </div>`;
    const off = Object.entries(d.servers || {}).filter(([, v]) => !v.ok);
    if (off.length) {
      html += `<p class="note">${off.map(([k]) => esc(k)).join('、')} 的统计数据暂时读不到，总数不含这些线路。</p>`;
    }
    html += `<div class="trend-wrap"><div class="trend-cap">最近 30 天播放</div>${sparkBars(d.trend)}</div>`;
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = stateBox('error', '播放数据读取失败', e.message,
      `<button class="ghost" onclick="loadOverviewPlays(true)">重试</button>`);
  }
}

async function loadPlays() {
  const box = $('#playsBody');
  box.innerHTML = skeleton(4);
  try {
    const d = await api('/api/stats');
    const songs = d.top_songs || [], artists = d.top_artists || [];
    const listeners = d.top_listeners || [], recent = d.recent || [];

    const listRank = (arr, render) => arr.length
      ? `<ol class="rank">${arr.map((x, i) => `<li>${render(x, i)}</li>`).join('')}</ol>`
      : stateBox('empty', '这个时间段还没有记录', '有人播放歌曲后，这里会出现排行。');

    box.innerHTML = `
      <h3>热门歌曲</h3>
      ${listRank(songs.slice(0, 12), (x, i) =>
        `<span class="rk">${i + 1}</span><span class="rn"><b>${esc(x.title)}</b>
         <em>${esc(x.artist || '')}</em></span><span class="rv">${x.plays}</span>`)}
      <h3>活跃听众</h3>
      ${listRank(listeners.slice(0, 10), (x, i) =>
        `<span class="rk">${i + 1}</span><span class="rn"><b>${esc(x.user)}</b>
         <em>${esc(x.name && x.name !== x.user ? x.name : '')}</em></span><span class="rv">${x.plays}</span>`)}
      <h3>最近播放</h3>
      ${recent.length ? `<div class="recent">${recent.map(r =>
        `<div class="recent-row"><span class="rt">${esc(r.title)}</span>
         <span class="ra">${esc(r.artist || '')}</span>
         <span class="ru">${esc(r.user || '')}</span>
         <span class="rs">${esc(r.server || '')}</span></div>`).join('')}</div>`
        : stateBox('empty', '还没有播放记录', '')}
    `;
  } catch (e) {
    box.innerHTML = stateBox('error', '播放数据读取失败', e.message,
      `<button class="ghost" onclick="loadPlays()">重试</button>`);
  }
}

/* ---------- 健康自检 ---------- */
async function runHealth() {
  const btn = $('#healthBtn');
  if (btn) { btn.disabled = true; btn.textContent = '检查中…'; }
  try {
    const d = await api('/api/health');
    const bad = (d.items || []).filter(i => !i.ok);
    toast(bad.length ? `${bad.length} 项异常：${bad.map(b => b.name).join('、')}`
                     : `全部 ${d.summary.total} 项正常`);
    go('servers');
    setTimeout(() => { if ($('#healthPanel')) $('#healthPanel').scrollIntoView({behavior: 'smooth'}); }, 380);
  } catch (e) {
    toast(e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '运行自检'; }
  }
}
$('#healthBtn') && $('#healthBtn').addEventListener('click', runHealth);
/* 自检是管理员能力，普通用户隐藏入口，避免点了报 403 */
function syncHealthBtn() {
  const b = $('#healthBtn');
  if (b) b.style.display = IS_ADMIN ? '' : 'none';
}

/* ---------- 求歌搜索建议 ---------- */
const PLAT_LABEL = { netease: '网易云', qq: 'QQ音乐', kugou: '酷狗', kuwo: '酷我' };

async function loadSuggest() {
  const box = $('#reqSuggest');
  if (!box) return;
  try {
    const d = await api('/api/suggest');
    if (!d.items || !d.items.length) { box.innerHTML = ''; return; }
    box.innerHTML = `<span class="sg-cap">最近搜过</span>` + d.items.map(it =>
      `<button class="sg" data-kw="${esc(it.keyword)}" data-plat="${esc(it.platform)}">
        ${esc(it.keyword)}${it.n > 1 ? `<i>${it.n}</i>` : ''}</button>`).join('');
    $$('.sg', box).forEach(b => b.addEventListener('click', () => {
      $('#reqKw').value = b.dataset.kw;
      if (b.dataset.plat && b.dataset.plat !== curPlat) {
        const tab = $(`.tabs button[data-p="${b.dataset.plat}"]`);
        if (tab) tab.click();
      }
      doSearch();
    }));
  } catch (e) { box.innerHTML = ''; }
}

/* ---------- 审核页快捷键 ---------- */
let cursorRow = -1;
document.addEventListener('keydown', e => {
  if (curPage !== 'review') return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || e.ctrlKey || e.metaKey || e.altKey) return;
  const rows = $$('#reqTable tbody tr').filter(r => r.querySelector('.acts button'));
  if (!rows.length) return;
  if (e.key === 'j' || e.key === 'ArrowDown') {
    e.preventDefault();
    cursorRow = Math.min(rows.length - 1, cursorRow + 1);
    rows[cursorRow].classList.add('cur');
    rows.forEach((r, i) => i !== cursorRow && r.classList.remove('cur'));
    rows[cursorRow].scrollIntoView({block: 'nearest'});
  } else if (e.key === 'k' || e.key === 'ArrowUp') {
    e.preventDefault();
    cursorRow = Math.max(0, cursorRow - 1);
    rows[cursorRow].classList.add('cur');
    rows.forEach((r, i) => i !== cursorRow && r.classList.remove('cur'));
    rows[cursorRow].scrollIntoView({block: 'nearest'});
  } else if (['a', 'u', 'r'].includes(e.key.toLowerCase()) && cursorRow >= 0 && rows[cursorRow]) {
    const map = { a: 'approved', u: 'uploaded', r: 'rejected' };
    const btn = rows[cursorRow].querySelector(`.acts button[data-s="${map[e.key.toLowerCase()]}"]`);
    if (btn) { e.preventDefault(); btn.click(); }
  }
});
$('#refreshBtn').addEventListener('click', () => go(curPage));

/* ---------- 概览 ---------- */
async function loadOverview(force) {
  try {
    const d = await api('/api/overview' + (force ? '?force=1' : ''));
    let html = `
      <div class="card"><div class="k">用户总数</div><div class="v">${d.userCount}</div></div>
      <div class="card"><div class="k">管理员</div><div class="v">${d.adminCount}</div></div>
      <div class="card"><div class="k">在线服务器</div><div class="v">${d.serverOnline}<span style="font-size:15px;color:var(--muted)"> / ${d.serverTotal}</span></div></div>`;
    $('#statCards').innerHTML = html;
    // 普通用户也能看服务器状态列表（只读）
    renderServers($('#serverList'), d.servers, true);
  } catch (e) { toast(e.message); }
}

function renderServers(el, servers, withCount) {
  el.innerHTML = Object.entries(servers).map(([name, s]) => `
    <div class="server-row">
      <span class="dot ${s.online ? 'on' : 'off'}"></span>
      <span class="sname">${esc(name)}</span>
      <span class="meta">${s.online ? '在线' : '离线'}</span>
      ${withCount || s.count != null ? `<span class="meta">${s.count ?? 0} 用户</span>` : ''}
      ${s.error ? `<span class="meta" style="color:var(--bad)">${esc(s.error)}</span>` : ''}
    </div>`).join('') || '<div class="muted">无服务器</div>';
}

async function loadServers() {
  try {
    const d = await api('/api/overview?force=1');
    renderServers($('#serverDetail'), d.servers, true);
  } catch (e) { toast(e.message); }
}

/* ---------- 配额 ---------- */
async function loadQuota() {
  try {
    const d = await api('/api/quota');
    const s = d.song, p = d.playlist;
    $('#songQuota').innerHTML = quotaHtml('本月求歌', s.used, s.limit);
    $('#plQuota').innerHTML = quotaHtml('本月歌单', p.used, p.limit);
  } catch (e) { /* 静默 */ }
}

function quotaHtml(label, used, limit) {
  const left = Math.max(0, limit - used);
  const pct = limit ? Math.min(100, (used / limit) * 100) : 0;
  const cls = left === 0 ? 'bad' : (pct >= 80 ? 'warn' : '');
  return `<span class="q-label">${label}</span>
    <span class="q-num ${cls}">剩余 ${left} / ${limit}</span>
    <span class="q-track"><span class="q-fill ${cls}" style="width:${pct}%"></span></span>`;
}

/* ---------- 用户 ---------- */
async function loadUsers(force) {
  try {
    const d = await api('/api/users' + (force ? '?force=1' : ''));
    USERS = d.users || [];
    renderUsers();
  } catch (e) { toast(e.message); }
}

function renderUsers() {
  const kw = $('#userSearch').value.trim().toLowerCase();
  const list = kw
    ? USERS.filter(u => u.username.toLowerCase().includes(kw) || (u.name || '').toLowerCase().includes(kw))
    : USERS;
  $('#userCount').textContent = `共 ${list.length} 个`;
  $('#userTable tbody').innerHTML = list.map(u => `
    <tr>
      <td data-l="用户名">${esc(u.username)}</td>
      <td data-l="昵称">${esc(u.name)}</td>
      <td data-l="权限">${u.isAdmin ? '<span class="badge admin">管理员</span>' : '<span class="badge">普通</span>'}</td>
      <td class="srv-cell" data-l="服务器">${Object.keys(u.servers).map(esc).join('、')}</td>
      <td><div class="acts">
        <button data-act="pw" data-u="${esc(u.username)}">改密码</button>
        ${u.username.toLowerCase() === 'botadmin' ? '' :
      `<button data-act="del" data-u="${esc(u.username)}" class="danger">删除</button>`}
      </div></td>
    </tr>`).join('') || '<tr><td colspan="5" class="muted" style="padding:22px">没有匹配的用户</td></tr>';
}

$('#userSearch').addEventListener('input', renderUsers);

$('#userTable').addEventListener('click', async e => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const name = btn.dataset.u;
  if (btn.dataset.act === 'pw') {
    const pw = prompt(`为 ${name} 设置新密码（至少 6 位，不能纯数字/纯字母）`);
    if (pw == null) return;
    if (pw.length < 6) return toast('密码至少 6 位');
    try {
      const d = await api('/api/users/update', {
        method: 'POST',
        body: JSON.stringify({ username: name, password: pw }),
      });
      toast(d.ok ? `已更新 ${name} 的密码` : `部分失败：${d.failed.join('，')}`);
    } catch (err) { toast(err.message); }
  }
  if (btn.dataset.act === 'del') {
    if (!confirm(`确定删除 ${name}？\n会从所有服务器移除，不可恢复。`)) return;
    try {
      const d = await api('/api/users/delete', {
        method: 'POST',
        body: JSON.stringify({ username: name }),
      });
      toast(d.ok ? `已删除 ${name}` : `部分失败：${d.failed.join('，')}`);
      loadUsers(true);
    } catch (err) { toast(err.message); }
  }
});

/* ---------- 求歌 ---------- */
$('#reqTabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-p]');
  if (!b) return;
  curPlat = b.dataset.p;
  $$('#reqTabs button').forEach(x => x.classList.toggle('on', x === b));
  const kw = $('#reqKw').value.trim();
  if (kw) doSearch();
});

async function doSearch() {
  const kw = $('#reqKw').value.trim();
  if (!kw) return toast('请输入关键词');
  const box = $('#reqResults');
  box.innerHTML = skeleton(4);
  try {
    const d = await api(`/api/search?platform=${encodeURIComponent(curPlat)}&kw=${encodeURIComponent(kw)}`);
    const res = d.results || [];
    loadSuggest();
    if (!res.length) {
      box.innerHTML = stateBox('empty', '没有找到这首歌', '换个关键词，或者换一个平台试试。');
      return;
    }
    box.innerHTML = res.map((r, i) => `
      <div class="song-row">
        <span class="idx">${i + 1}</span>
        ${r.cover
          ? `<img class="cov" src="${esc(r.cover)}" alt="" loading="lazy" decoding="async"
                  referrerpolicy="no-referrer" onerror="this.classList.add('cov-fail')">`
          : `<span class="cov cov-none" aria-hidden="true"></span>`}
        <div class="info">
          <div class="t">${esc(r.title)}</div>
          <div class="a">${esc(r.artist)}${r.album ? ' · ' + esc(r.album) : ''}</div>
        </div>
        <span class="plat">${PLAT_NAMES[r.platform] || r.platform}</span>
        <button data-act="submit" data-i="${i}">求歌</button>
      </div>`).join('');
    box._res = res;
  } catch (e) {
    box.innerHTML = stateBox('error', '搜索失败', e.message,
      '<button class="ghost" onclick="doSearch()">重试</button>');
  }
}

$('#reqSearch').addEventListener('click', doSearch);
$('#reqKw').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });

$('#reqResults').addEventListener('click', async e => {
  const b = e.target.closest('button[data-act="submit"]');
  if (!b) return;
  const r = ($('#reqResults')._res || [])[+b.dataset.i];
  if (!r) return;
  b.disabled = true;
  try {
    await api('/api/requests/submit', {
      method: 'POST',
      body: JSON.stringify({
        platform: r.platform, song_id: r.song_id,
        title: r.title, artist: r.artist,
      }),
    });
    b.textContent = '已提交';
    toast('已提交求歌，等待审核');
    loadQuota();
  } catch (err) {
    b.disabled = false;
    toast(err.message, 4000);
  }
});

/* ---------- 歌单 ---------- */
$('#plSubmit').addEventListener('click', async () => {
  const url = $('#plUrl').value.trim();
  if (!url) return toast('请粘贴歌单链接');
  const btn = $('#plSubmit');
  btn.disabled = true;
  try {
    await api('/api/playlists/submit', {
      method: 'POST',
      body: JSON.stringify({ url }),
    });
    $('#plUrl').value = '';
    $('#plResult').innerHTML = `<div class="ok-tip">已提交，等管理员审核</div>`;
    toast('歌单已提交');
    loadQuota();
  } catch (e) {
    toast(e.message, 4000);
  } finally {
    btn.disabled = false;
  }
});
$('#plUrl').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('#plSubmit').click();
});

/* ---------- 审核 ---------- */
const ST = {
  pending: ['待审核', ''], approved: ['已通过', 'ok'],
  uploaded: ['已上传', 'ok'], rejected: ['已拒绝', 'bad']
};

async function loadReview() {
  try {
    const d = await api('/api/requests');
    REQ_ROWS = d.requests || [];
    PL_ROWS = d.playlists || [];
    renderReqTable();
    renderPlTable();
    updateSel('request');
    updateSel('playlist');
  } catch (e) { toast(e.message); }
}

function renderReqTable() {
  $('#reqTable tbody').innerHTML = REQ_ROWS.map(r => `
    <tr>
      <td><input type="checkbox" class="rowchk" data-k="request" data-id="${r.id}"></td>
      <td data-l="时间" class="cell-time">${fmtTime(r.ts)}</td>
      <td data-l="用户">${esc(r.username)}${r.bound ? '' : ' <span class="nobind">未绑定 TG</span>'}</td>
      <td data-l="歌曲">${esc(r.title)}${r.artist ? ` <span class="muted">- ${esc(r.artist)}</span>` : ''}</td>
      <td data-l="票数">${r.votes || 0}</td>
      <td data-l="状态"><span class="badge ${ST[r.status]?.[1] || ''}">${ST[r.status]?.[0] || r.status}</span></td>
      <td><div class="acts">
        <button data-k="request" data-id="${r.id}" data-s="approved">通过</button>
        <button data-k="request" data-id="${r.id}" data-s="uploaded">已上传</button>
        <button data-k="request" data-id="${r.id}" data-s="rejected" class="danger">拒绝</button>
      </div></td>
    </tr>`).join('') || '<tr><td colspan="7" class="muted" style="padding:22px">暂无求歌</td></tr>';
}

function renderPlTable() {
  $('#plTable tbody').innerHTML = PL_ROWS.map(p => {
    const link = p.note || '';
    const show = link
      ? `<a href="${esc(link)}" target="_blank" rel="noopener">${esc(shortUrl(link))}</a>`
      : esc(p.name || '—');
    return `
    <tr>
      <td><input type="checkbox" class="rowchk" data-k="playlist" data-id="${p.id}"></td>
      <td data-l="时间" class="cell-time">${fmtTime(p.ts)}</td>
      <td data-l="用户">${esc(p.username)}${p.bound ? '' : ' <span class="nobind">未绑定 TG</span>'}</td>
      <td data-l="歌单">${show}${p.platform ? ` <span class="badge">${PLAT_NAMES[p.platform] || p.platform}</span>` : ''}</td>
      <td data-l="状态"><span class="badge ${ST[p.status]?.[1] || ''}">${ST[p.status]?.[0] || p.status}</span></td>
      <td><div class="acts">
        <button data-k="playlist" data-id="${p.id}" data-s="approved">通过</button>
        <button data-k="playlist" data-id="${p.id}" data-s="uploaded">已上传</button>
        <button data-k="playlist" data-id="${p.id}" data-s="rejected" class="danger">拒绝</button>
      </div></td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="muted" style="padding:22px">暂无歌单</td></tr>';
}

function shortUrl(u) {
  try {
    const o = new URL(u);
    const id = (u.match(/(\d{5,})/) || [])[1];
    return o.hostname.replace('www.', '') + (id ? ' / ' + id : '');
  } catch (e) { return u.slice(0, 40); }
}

/* 单选操作 */
$('#page-review').addEventListener('click', async e => {
  const btn = e.target.closest('button[data-s]');
  if (!btn || btn.classList.contains('batch-act')) return;
  const kind = btn.dataset.k, id = btn.dataset.id, status = btn.dataset.s;
  const note = status === 'rejected' ? (prompt('拒绝原因（可留空）') || '') : '';
  try {
    const res = await api('/api/requests/review', {
      method: 'POST',
      body: JSON.stringify({ kind, id, status, note }),
    });
    if (res.notified) toast('已处理，已通知用户');
    else if (!res.bound) toast('已处理，但该用户未绑定 TG');
    else toast('已处理，但通知发送失败');
    loadReview();
  } catch (err) { toast(err.message); }
});

/* 多选：勾选框与全选 */
document.addEventListener('change', e => {
  const t = e.target;
  if (t.classList && t.classList.contains('rowchk')) {
    updateSel(t.dataset.k);
    return;
  }
  if (t.id === 'reqHead' || t.id === 'reqSelAll') {
    const k = 'request';
    $$(`.rowchk[data-k="${k}"]`).forEach(c => c.checked = t.checked);
    const h = $('#reqHead'), a = $('#reqSelAll');
    if (h) h.checked = t.checked;
    if (a) a.checked = t.checked;
    updateSel(k);
    return;
  }
  if (t.id === 'plHead' || t.id === 'plSelAll') {
    const k = 'playlist';
    $$(`.rowchk[data-k="${k}"]`).forEach(c => c.checked = t.checked);
    const h = $('#plHead'), a = $('#plSelAll');
    if (h) h.checked = t.checked;
    if (a) a.checked = t.checked;
    updateSel(k);
    return;
  }
});

function selectedIds(kind) {
  return $$(`.rowchk[data-k="${kind}"]:checked`).map(c => c.dataset.id);
}

function updateSel(kind) {
  const ids = selectedIds(kind);
  const el = kind === 'request' ? $('#reqSelCount') : $('#plSelCount');
  if (el) el.textContent = `已选 ${ids.length}`;
}

/* 批量操作 */
$('#page-review').addEventListener('click', async e => {
  const btn = e.target.closest('button.batch-act');
  if (!btn) return;
  const kind = btn.dataset.k;
  const ids = selectedIds(kind);
  if (!ids.length) return toast('请先勾选记录');

  if (btn.dataset.del === '1') {
    if (!confirm(`确定删除选中的 ${ids.length} 条记录？不可恢复。`)) return;
    try {
      const d = await api('/api/review/delete', {
        method: 'POST',
        body: JSON.stringify({ kind, ids }),
      });
      toast(`已删除 ${d.count} 条`);
      loadReview();
    } catch (err) { toast(err.message); }
    return;
  }

  const status = btn.dataset.s;
  const note = status === 'rejected'
    ? (prompt(`拒绝选中的 ${ids.length} 条，原因（可留空）`) || '')
    : '';
  try {
    const d = await api('/api/review/batch', {
      method: 'POST',
      body: JSON.stringify({ kind, ids, status, note }),
    });
    let msg = `已处理 ${d.count} 条`;
    if (typeof d.sent === 'number') {
      msg += `，已通知 ${d.sent} 人`;
      if (d.unbound) msg += `，${d.unbound} 人未绑定 TG`;
      if (d.failed) msg += `，${d.failed} 条发送失败`;
    }
    toast(msg);
    loadReview();
  } catch (err) { toast(err.message); }
});

/* ---------- 日志 ---------- */
async function loadAudit() {
  try {
    const d = await api('/api/audit');
    $('#auditTable tbody').innerHTML = (d.logs || []).map(l => `
      <tr>
        <td data-l="时间" class="cell-time">${fmtTime(l.ts)}</td>
        <td data-l="操作人">${esc(l.actor)}</td>
        <td data-l="动作">${esc(l.action)}</td>
        <td data-l="对象">${esc(l.target)}</td>
        <td class="muted">${esc(l.detail)}</td>
      </tr>`).join('') || '<tr><td colspan="5" class="muted" style="padding:22px">暂无日志</td></tr>';
  } catch (e) { toast(e.message); }
}

/* ---------- 启动 ---------- */
(async function boot() {
  if (!TOKEN) return;
  try {
    const d = await api('/api/me');
    IS_ADMIN = !!d.isAdmin;
    enterApp(d.username);
  } catch (e) {
    logout(true);
  }
})();
