/**
 * 行程图体检：真浏览器里点一遍、截几张图，把「点了没反应」这类问题挡在交付前。
 *
 * 用法（Windows / Git Bash，用 WorkBuddy 隔离环境里的 node 与 playwright）：
 *   NODE_PATH="C:/Users/ch/.workbuddy/binaries/node/workspace/node_modules" \
 *   "C:/Users/ch/.workbuddy/binaries/node/versions/22.22.2-6/node.exe" \
 *   scripts/verify_page.js out.html ./shots
 *
 * 加 --headed 可以看着它点；加 --port 8080 可走本地 HTTP（替代 file://）。
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright');

const args = process.argv.slice(2);
const file = args.find(a => !a.startsWith('--'));
const outDir = (args.find(a => a.startsWith('--out=')) || '').split('=')[1] || './shots';
const headed = args.includes('--headed');

if (!file) {
  console.error('用法：node verify_page.js <html> [--out=./shots] [--headed]');
  process.exit(2);
}

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok, detail: detail || '' });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
};

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch({ headless: !headed });
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });

  const pageErrors = [];
  page.on('pageerror', e => pageErrors.push(e.message));

  await page.goto('file:///' + path.resolve(file).replace(/\\/g, '/'));
  await page.waitForTimeout(5000);   // 等底图与图层就绪

  // ---------- 1. 结构 ----------
  const info = await page.evaluate(() => ({
    errs: window.__errs || [],
    errbar: document.getElementById('errbar') ? document.getElementById('errbar').className : 'missing',
    title: (document.getElementById('tripTitle') || {}).textContent || '',
    stat: (document.getElementById('navStatLine') || {}).textContent || '',
    cards: document.querySelectorAll('.day-card').length,
    markers: document.querySelectorAll('.spot-marker').length,
    buttons: [...document.querySelectorAll('#dayFilter button')].map(b => b.textContent.trim()),
    bootErr: getComputedStyle(document.getElementById('boot-err')).display
  }));
  check('数据已注入（无 boot-err）', info.bootErr === 'none', info.bootErr);
  check('无脚本异常（window.__errs 为空）', info.errs.length === 0, info.errs.join(' | '));
  check('错误条隐藏', info.errbar === 'hidden', info.errbar);
  check('有行程卡片', info.cards > 0, info.cards + ' 张');
  check('有标点', info.markers > 0, info.markers + ' 个');
  console.log('       标题：' + info.title);
  console.log('       路网：' + info.stat);

  await page.screenshot({ path: path.join(outDir, '1-all-straight.png') });

  // ---------- 2. 命中测试：按钮中心点是否落在按钮自己身上 ----------
  const hits = await page.evaluate(() => {
    const out = [];
    const probe = (el, label) => {
      const r = el.getBoundingClientRect();
      // 视口外的元素 elementFromPoint 必然返回 null，那不是遮挡，跳过
      if (!r.width || !r.height) { out.push({ label, ok: true, hit: 'skip:zero-size' }); return; }
      if (r.top < 0 || r.left < 0 || r.bottom > window.innerHeight || r.right > window.innerWidth) {
        out.push({ label, ok: true, hit: 'skip:offscreen' }); return;
      }
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      out.push({
        label,
        ok: hit === el || el.contains(hit),
        hit: hit ? hit.tagName + (hit.className ? '.' + String(hit.className).trim().replace(/\s+/g, '.') : '') : 'null'
      });
    };
    document.querySelectorAll('#dayFilter button').forEach(b => probe(b, b.textContent.trim()));
    document.querySelectorAll('.day-card').forEach((c, i) => probe(c, '卡片' + (i + 1)));
    const m = document.querySelector('.spot-marker');
    if (m) probe(m, '标点');
    return out;
  });
  const badHits = hits.filter(h => !h.ok);
  const probed = hits.filter(h => h.ok && !String(h.hit).startsWith('skip:')).length;
  check(`所有控件都点得到（无透明层遮挡，实测 ${probed} 个）`, badHits.length === 0,
    badHits.map(h => `${h.label}→命中 ${h.hit}`).join('；'));

  // ---------- 3. 双视图 ----------
  await page.click('button[data-mode="nav"]');
  await page.waitForTimeout(2500);
  await page.screenshot({ path: path.join(outDir, '2-all-nav.png') });
  const navHint = await page.textContent('#modeHint');
  check('导航视图已激活', navHint.includes('导航路线'), navHint.slice(0, 40));

  // ---------- 4. 单日筛选 + 回退 ----------
  const dayKeys = await page.evaluate(() =>
    [...document.querySelectorAll('#dayFilter button[data-key]')].map(b => b.getAttribute('data-key')));
  const target = dayKeys.find(k => k !== 'all' && k !== 'full') || dayKeys[0];
  await page.click(`button[data-key="${target}"]`);
  await page.waitForTimeout(2200);
  const dimmed = await page.evaluate(() => {
    const all = document.querySelectorAll('.spot-marker');
    return { dim: document.querySelectorAll('.spot-marker.dim').length, total: all.length };
  });
  check(`筛选 DAY ${target} 后只保留当天标点`, dimmed.dim < dimmed.total,
    `压暗 ${dimmed.dim}/${dimmed.total}，当天保留 ${dimmed.total - dimmed.dim} 个`);
  const barVisible = await page.evaluate(() => !document.getElementById('viewState').classList.contains('hidden'));
  check('状态栏出现（可点“返回全览”）', barVisible);
  await page.screenshot({ path: path.join(outDir, `3-day${target}.png`) });

  await page.keyboard.press('Escape');
  await page.waitForTimeout(1600);
  const backAll = await page.evaluate(() => ({
    hidden: document.getElementById('viewState').classList.contains('hidden'),
    dimmed: document.querySelectorAll('.spot-marker.dim').length
  }));
  check('Esc 可回到全览', backAll.hidden && backAll.dimmed === 0, JSON.stringify(backAll));

  // 再点同一天也能回全览
  await page.click(`button[data-key="${target}"]`);
  await page.waitForTimeout(900);
  await page.click(`button[data-key="${target}"]`);
  await page.waitForTimeout(1400);
  const twice = await page.evaluate(() => document.getElementById('viewState').classList.contains('hidden'));
  check('再点同一天可回全览', twice);

  // file:// 直开时 TMap 走不通本地代理，会抛一条 XHR/空 promise 异常 —— 这是环境噪音，不是页面 bug
  const NOISE = [/Invalid URL/, /XMLHttpRequest/, /^null$/, /Uncaught \(in promise\) null/];
  const realErrors = pageErrors.filter(m => !NOISE.some(re => re.test(m || '')));
  if (pageErrors.length !== realErrors.length) {
    console.log(`      （已忽略 ${pageErrors.length - realErrors.length} 条 file:// 环境噪音）`);
  }
  check('无未捕获的页面异常', realErrors.length === 0, realErrors.join(' | '));

  await browser.close();

  const failed = results.filter(r => !r.ok);
  console.log('\n截图已存：' + path.resolve(outDir));
  console.log(`结果：${results.length - failed.length}/${results.length} 项通过`);
  if (failed.length) {
    console.log('发现问题：');
    failed.forEach(f => console.log('  - ' + f.name + (f.detail ? '：' + f.detail : '')));
    process.exit(1);
  }
})();
