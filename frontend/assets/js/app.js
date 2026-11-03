/* 闲鱼客服控制台 · 前端逻辑（原生 JS，无依赖） */
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  let current = 'dashboard';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function api(action, params = {}) {
    const fd = new URLSearchParams();
    fd.set('action', action);
    for (const k in params) fd.set(k, params[k]);
    return fetch('api.php', { method: 'POST', body: fd })
      .then(r => {
        // 登录过期/未登录：接口统一回 401 + need_login，这里直接送回登录页，
        // 避免每个调用点都自己判断一遍
        if (r.status === 401) {
          location.href = 'login.php';
          throw new Error('登录已过期，正在跳转登录页…');
        }
        return r.text();
      })
      .then(text => {
        let d;
        try { d = JSON.parse(text); }
        catch (e) { throw new Error('服务器返回了非预期内容（疑似 PHP 报错）：\n' + text.slice(0, 160)); }
        if (d && d.ok === false) throw new Error(d.msg || '请求失败');
        return d;
      });
  }
  function toast(msg, type = '') {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast show ' + type;
    setTimeout(() => t.className = 'toast', 2600);
  }

  // ---------- 弹层 ----------
  function modal(title, bodyHtml, onSave, saveText = '保存') {
    let mask = $('#modalMask');
    if (!mask) {
      mask = document.createElement('div');
      mask.id = 'modalMask'; mask.className = 'modal-mask';
      mask.innerHTML = '<div class="modal"><h3 id="mTitle"></h3><div id="mBody"></div>' +
        '<div class="foot"><button class="btn btn-ghost" id="mCancel">取消</button>' +
        '<button class="btn btn-primary" id="mSave">' + saveText + '</button></div></div>';
      document.body.appendChild(mask);
      mask.addEventListener('click', e => { if (e.target === mask) mask.classList.remove('show'); });
      $('#mCancel').onclick = () => mask.classList.remove('show');
    }
    $('#mTitle').textContent = title;
    $('#mBody').innerHTML = bodyHtml;
    // saveText 必须**每次都写**：弹窗 DOM 只创建一次并复用，首次创建时被拼进
    // 按钮标签后就不再变。漏了这句，先打开过「导入卡卷」的弹窗后，之后所有
    // 弹窗（绑定卡卷 / 新增规则…）的确认按钮都会一直显示"导入"
    // （2026-10-19 用户截图：绑定卡卷弹窗底部显示"导入"）。
    $('#mSave').textContent = saveText;
    mask.classList.add('show');
    $('#mSave').onclick = () => { try { onSave(); } catch (e) { toast(e.message, 'err'); } };
    return mask;
  }

  // ---------- 引擎状态 ----------
  function refreshEngine() {
    api('dashboard').then(d => {
      const st = d.engine || {};
      const dot = $('#engDot'), txt = $('#engText');
      dot.className = 'dot ' + (st.alive ? 'run' : (st.state === 'error' ? 'err' : ''));
      txt.textContent = st.alive ? '引擎运行中' : (st.state === 'running' ? '引擎启动中…' : '引擎已停止');
      const dry = $('#dryBadge');
      if (st && window.__dry != null) {}
    }).catch(() => {});
  }
  function refreshDryBadge() {
    api('settings_get').then(d => {
      const s = d.settings || {};
      window.__dry = s.dry_run;
      $('#dryBadge').classList.toggle('hidden', !(s.dry_run == 1 || s.dry_run === true || s.dry_run === '1'));
    }).catch(() => {});
  }

  // ---------- 各页面 ----------
  // ---------- 商品采集 / 进度轮询（商品列表页用） ----------
  let collectTimer = null;
  function setProgress(cur, total, msg) {
    const wrap = $('#collectWrap');
    if (!wrap) return;
    const pct = total > 0 ? Math.round(cur * 100 / total) : (cur > 0 ? 100 : 0);
    $('#collectBar').style.width = pct + '%';
    $('#collectText').textContent = total > 0 ? `${msg}（${cur}/${total}）` : msg;
  }
  function pollCollect() {
    clearTimeout(collectTimer);
    collectTimer = setTimeout(() => {
      api('product_collect_status').then(d => {
        if (d.status === 'running') {
          setProgress(d.current, d.total, d.message || '采集中…');
          pollCollect();
        } else if (d.status === 'done') {
          setProgress(d.total || d.current, d.total || d.current, d.message || '采集完成');
          // 入库已由后端在返回 done 时顺手完成（product_collect_status 内部
          // 调 xy_import_collected_products），前端不再额外发导入请求，
          // 也不再提示"正在导入数据库"——用户只看到采集完成、列表刷新。
          const n = d.imported || 0;
          toast(n > 0 ? `采集完成，已同步 ${n} 个商品` : '采集完成，商品已是最新', 'ok');
          pages.shopitems();
        } else if (d.status === 'error') {
          setProgress(0, 0, d.message || '采集失败');
          toast(d.message || '采集失败', 'err');
        } else {
          setProgress(0, 0, d.message || '尚未采集');
        }
      }).catch(() => pollCollect());
    }, 1500);
  }
  function startCollect() {
    api('product_collect')
      .then(d => { toast(d.msg || '采集已启动', 'ok'); $('#collectWrap').classList.remove('hidden'); setProgress(0, 0, '采集启动中…'); pollCollect(); })
      .catch(e => { toast(e.message, 'err'); $('#collectWrap').classList.remove('hidden'); pollCollect(); });
  }

  // ---------- 导出消息 / 进度轮询（会话页用；执行链：网页写触发器 →
  //   桌面控制台拉起 export_runner.py → 进度写 data/export_progress.json） ----------
  let exportTimer = null;
  function setExportProgress(cur, total, msg) {
    const wrap = $('#exportWrap');
    if (!wrap) return;
    const pct = total > 0 ? Math.round(cur * 100 / total) : (cur > 0 ? 100 : 0);
    $('#exportBar').style.width = pct + '%';
    $('#exportText').textContent = total > 0 ? `${msg}（${cur}/${total}）` : msg;
  }
  function pollExport() {
    clearTimeout(exportTimer);
    exportTimer = setTimeout(() => {
      api('export_msgs_status').then(d => {
        if (d.status === 'running' || d.status === 'pending') {
          setExportProgress(d.current, d.total, d.message || '导出中…');
          pollExport();
        } else if (d.status === 'done') {
          setExportProgress(d.total || d.current, d.total || d.current, d.message || '导出完成');
          toast('导出完成', 'ok');
          loadExportFiles();
        } else if (d.status === 'error') {
          setExportProgress(0, 0, d.message || '导出失败');
          toast(d.message || '导出失败', 'err');
        } else {
          setExportProgress(0, 0, d.message || '尚未导出过');
        }
      }).catch(() => pollExport());
    }, 2000);
  }
  function startExport() {
    if (!confirm('导出将重启闲鱼客服客户端（约 10 秒），期间自动回复 / 自动发货会暂停；\n' +
        '导出会逐个打开全部会话读取聊天记录，全程约几分钟。确定开始吗？')) return;
    api('export_msgs')
      .then(d => { toast(d.msg || '导出已启动', 'ok'); $('#exportWrap').classList.remove('hidden'); setExportProgress(0, 0, '已发送指令，等待桌面控制台执行…'); pollExport(); })
      .catch(e => { toast(e.message, 'err'); $('#exportWrap').classList.remove('hidden'); pollExport(); });
  }
  function loadExportFiles() {
    api('export_files').then(d => {
      const box = $('#exportFiles');
      if (!box) return;
      const files = d.files || [];
      if (!files.length) { box.innerHTML = ''; return; }
      const rows = files.map(f =>
        `<tr><td>${esc(f.name)}</td><td class="nowrap muted">${(f.size / 1024).toFixed(1)} KB</td>` +
        `<td class="nowrap muted">${esc(f.mtime)}</td>` +
        `<td class="nowrap"><a class="btn btn-sm btn-ghost" href="api.php?action=export_download&name=${encodeURIComponent(f.name)}">下载</a></td></tr>`).join('');
      box.innerHTML = `<div class="panel"><h3>📁 已导出的会话文件（${files.length}）</h3>` +
        `<table><thead><tr><th>文件（按买家昵称命名）</th><th>大小</th><th>时间</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>` +
        `<p class="muted" style="font-size:12px">文件同时保存在本地目录：<span class="mono">${esc(d.dir || '')}</span></p></div>`;
    }).catch(() => {});
  }

  /**
   * 把选中的图片压缩成 JPEG dataURL。
   *
   * 为什么要压：手机拍的图动辄 4-8MB，9 张直接 base64 传会撑爆请求体，
   * 也让落盘的商品文件夹臃肿。压到长边 1280、质量 0.85，肉眼看不出差别，
   * 体积通常降到 100-300KB。闲鱼商品图本来也就这个尺寸级别。
   *
   * @param {File} file 用户选择的原始文件
   * @param {number} maxSide 长边上限（像素）
   * @param {number} quality JPEG 质量 0-1
   * @returns {Promise<string>} data:image/jpeg;base64,...
   */
  function compressImage(file, maxSide = 1280, quality = 0.85) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        try {
          let w = img.naturalWidth, h = img.naturalHeight;
          const scale = Math.min(1, maxSide / Math.max(w, h));
          w = Math.max(1, Math.round(w * scale));
          h = Math.max(1, Math.round(h * scale));
          const cv = document.createElement('canvas');
          cv.width = w; cv.height = h;
          const ctx = cv.getContext('2d');
          // JPEG 无透明通道：先铺白底，避免 PNG 透明区域变黑
          ctx.fillStyle = '#fff';
          ctx.fillRect(0, 0, w, h);
          ctx.drawImage(img, 0, 0, w, h);
          resolve(cv.toDataURL('image/jpeg', quality));
        } catch (e) {
          reject(e);
        } finally {
          URL.revokeObjectURL(url);
        }
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片读取失败')); };
      img.src = url;
    });
  }

  /**
   * 执行「发布商品」。
   *
   * 流程（2026-10-20 用户要求）：
   *   1. 在 data/publish/ 下建本次商品文件夹；
   *   2. 逐张压缩图片并写入该文件夹（01.jpg、02.jpg…）；
   *   3. 写入 manifest.json（标题/价格/库存/描述/图片清单/状态）；
   *   4. 后台上传（当前接口未接入，只留日志，资料已安全落盘）。
   *
   * 为什么先落盘再上传：上传可能失败、可能断网，落盘后资料永远不丢，
   * 之后可以拿 publish/<dir> 重试上传；也方便事后核对"到底提交了什么"。
   *
   * @param {object} payload 表单内容
   * @param {(text:string)=>void} onStep 进度回调（用于页面状态文字）
   * @returns {Promise<{dir:string, images:number}>}
   */
  async function doPublish(payload, onStep = () => {}) {
    onStep('正在创建商品文件夹…');
    const begin = await api('publish_begin');
    const dir = begin.dir;

    const total = payload.imgs.length;
    for (let i = 0; i < total; i++) {
      onStep(`正在保存图片 ${i + 1}/${total}…`);
      let data;
      try {
        data = await compressImage(payload.imgs[i].file);
      } catch (e) {
        throw new Error(`第 ${i + 1} 张图片处理失败：${e.message}`);
      }
      await api('publish_image', { dir, idx: i + 1, data });
    }

    onStep('正在写入商品信息…');
    const fin = await api('publish_finish', {
      dir,
      title: payload.title,
      price: payload.price,
      origin: payload.origin,
      stock: payload.stock,
      desc: payload.desc,
      is_virtual: payload.isVirtual ? '1' : '0',
      bundle_id: payload.bundleId || '',
    });

    onStep('上传中，请稍后…');
    let up = { state: 'unknown', message: '' };
    try {
      // real=1：真正点「发布」提交到闲鱼。整个过程在服务端跑（传图+填表+提交+校验），
      // 通常 20-60 秒，期间页面一直显示「上传中，请稍后…」。
      up = await api('publish_upload', { dir, real: '1' });
    } catch (e) {
      // 请求本身失败（超时/后端异常）：资料已在文件夹里，可从日志重试
      up = { state: 'error', message: e.message || String(e) };
    }
    return { dir, images: fin.images || total, upload: up };
  }

  /**
   * 「智能匹配卡卷」：按名称/描述相似度给出商品↔卡卷绑定建议，勾选后一键写入。
   *
   * 为什么不让它全自动绑：绑错卡卷 = 买家付了钱收到无关内容，比不发货更严重。
   * 所以高分（标题完全包含卡券名，score 100）默认勾选，低分（描述里提到，60）
   * 默认不勾，必须由人确认。
   */
  function matchBundlesForm() {
    api('match_bundles').then(d => {
      const rows = d.rows || [];
      if (!rows.length) {
        toast('没有找到可自动匹配的组合，请手动绑定', 'err');
        return;
      }
      const body = `
        <p class="muted" style="font-size:12px;margin:0 0 10px">
          共 ${d.items} 个商品 / ${d.bundles} 个卡卷，找到 <b>${rows.length}</b> 条匹配建议。
          高分（标题完全匹配）已默认勾选；低分的请自己确认再勾。
        </p>
        <div class="panel" style="box-shadow:none;padding:0;max-height:440px;overflow:auto">
          <table>
            <thead><tr>
              <th style="width:34px"><input type="checkbox" id="mbAll"></th>
              <th>商品</th><th>建议卡卷</th><th style="width:130px">依据</th>
            </tr></thead>
            <tbody>${rows.map((r, i) => `
              <tr>
                <td><input type="checkbox" class="mbPick" data-i="${i}"
                     ${r.score >= 100 && !r.already ? 'checked' : ''} ${r.already ? 'disabled' : ''}></td>
                <td>${esc(r.item_title)}${r.already ? ' <span class="tag gray">已绑定</span>' : ''}</td>
                <td>${esc(r.bundle_name)}</td>
                <td><span class="tag ${r.score >= 100 ? '' : 'gray'}">${r.score}</span>
                  <span class="muted" style="font-size:12px">${esc(r.reason)}</span></td>
              </tr>`).join('')}</tbody>
          </table>
        </div>`;
      modal('智能匹配卡卷', body, () => {
        const picks = [];
        $$('#mBody .mbPick').forEach(cb => {
          if (!cb.checked) return;
          const r = rows[+cb.dataset.i];
          picks.push({ item_id: r.item_id, bundle_id: r.bundle_id });
        });
        if (!picks.length) { toast('没有勾选任何一项', 'err'); return; }
        api('bulk_bind', { pairs: JSON.stringify(picks) })
          .then(res => {
            $('#modalMask').classList.remove('show');
            toast(`已绑定 ${res.bound || picks.length} 组`, 'ok');
            pages.shopitems();
          })
          .catch(e => toast(e.message, 'err'));
      }, '一键绑定');

      // 全选只作用于「可勾选」的行（已绑定的行是 disabled，不能动）
      const all = $('#mbAll');
      if (all) {
        all.onchange = () => {
          $$('#mBody .mbPick').forEach(cb => { if (!cb.disabled) cb.checked = all.checked; });
        };
      }
    }).catch(e => toast(e.message, 'err'));
  }

  const pages = {
    dashboard() {
      api('dashboard').then(d => {
        const c = d.counts || {};
        const cards = `
          <div class="grid">
            <div class="card stat"><span class="num">${c.rules ?? 0}</span><span class="lbl">启用中的回复规则</span></div>
            <div class="card stat"><span class="num">${c.products ?? 0}</span><span class="lbl">发货商品数（关键词）</span></div>
            <div class="card stat"><span class="num">${c.shop_items ?? 0}</span><span class="lbl">店铺商品数</span></div>
            <div class="card stat"><span class="num">${c.bundles ?? 0}</span><span class="lbl">卡卷数</span></div>
            <div class="card stat"><span class="num">${c.cards_left ?? 0}</span><span class="lbl">可用卡密库存</span></div>
            <div class="card stat"><span class="num">${c.conv ?? 0}</span><span class="lbl">已记录会话</span></div>
            <div class="card stat"><span class="num">${c.deliv_today ?? 0}</span><span class="lbl">今日订单</span></div>
            <div class="card stat"><span class="num">${c.msg_today ?? 0}</span><span class="lbl">今日消息</span></div>
          </div>`;
        const act = (d.activity || []).map(a =>
          `<tr><td class="nowrap muted">${esc(a.created_at)}</td><td class="nowrap">${levelTag(a.level)}</td><td>${esc(a.message)}</td></tr>`).join('') || '<tr><td colspan="3" class="empty">暂无日志</td></tr>';
        const del = (d.deliveries || []).map(x =>
          `<tr><td class="nowrap">${esc(x.created_at)}</td><td>${esc(x.buyer_name)}</td><td>${esc(x.product_name || '-')}</td><td class="mono">${esc(x.code || '')}</td><td>${statusTag(x.status)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">暂无发货</td></tr>';
        const appPanel = `
          <div class="panel client-panel">
            <div class="engine-state"><span class="dot" id="dashAppDot"></span>
              <strong>闲鱼卖家客户端</strong>
              <span id="dashAppText" class="muted">检测中…</span>
              <span class="muted" style="margin-left:auto">调试端口 9222</span>
            </div>
            <p class="muted">自动回复 / 自动发货依赖此客户端，需后台监听 9222 端口。点下方按钮即可在后台（最小化、不抢前台）启动，无需再单独运行「启动软件.bat」。</p>
            <div class="engine-btns">
              <button id="dashAppStart" class="btn btn-primary btn-sm">一键启动软件</button>
              <button id="dashAppStop" class="btn btn-ghost btn-sm">停止软件</button>
            </div>
          </div>`;
        $('#content').innerHTML = appPanel + cards +
          `<div class="panel"><h3>📦 最近订单</h3><table><thead><tr><th>时间</th><th>买家</th><th>商品</th><th>卡密</th><th>状态</th></tr></thead><tbody>${del}</tbody></table></div>` +
          `<div class="panel"><h3>📜 最近运行日志</h3><table><thead><tr><th>时间</th><th>级别</th><th>内容</th></tr></thead><tbody>${act}</tbody></table></div>`;
      }).catch(e => $('#content').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`);
    },

    rules() {
      api('rules').then(d => {
        const rows = (d.rows || []).map(r => {
          const tags = (r.keywords || []).map(k => `<span class="tag">${esc(k)}</span>`).join('');
          return `<tr>
            <td class="nowrap">${r.priority}</td>
            <td>${tags || '<span class="muted">—</span>'}</td>
            <td>${esc(r.reply)}</td>
            <td class="nowrap">${r.enabled ? '<span class="tag">启用</span>' : '<span class="tag gray">停用</span>'}</td>
            <td class="nowrap">${r.hit_count}</td>
            <td class="nowrap">
              <button class="btn btn-sm btn-ghost" data-act="toggle" data-id="${r.id}" data-en="${r.enabled ? 0 : 1}">${r.enabled ? '停用' : '启用'}</button>
              <button class="btn btn-sm btn-ghost" data-act="edit" data-id="${r.id}">编辑</button>
              <button class="btn btn-sm btn-danger" data-act="del" data-id="${r.id}">删除</button>
            </td></tr>`;
        }).join('') || '<tr><td colspan="6" class="empty">还没有规则</td></tr>';
        $('#content').innerHTML = `
          <div class="row"><div class="spacer"></div><button class="btn btn-primary" id="addRule">+ 新增规则</button></div>
          <div class="panel"><table><thead><tr><th>优先级</th><th>关键词</th><th>回复内容</th><th>状态</th><th>命中</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
        $('#addRule').onclick = () => ruleForm();
        $$('#content [data-act]').forEach(b => b.onclick = () => {
          const id = b.dataset.id;
          if (b.dataset.act === 'del') {
            if (!confirm('确认删除该规则？')) return;
            api('rule_delete', { id }).then(() => { toast('已删除'); pages.rules(); }).catch(e => toast(e.message, 'err'));
          } else if (b.dataset.act === 'toggle') {
            api('rule_toggle', { id, enabled: b.dataset.en }).then(() => pages.rules()).catch(e => toast(e.message, 'err'));
          } else if (b.dataset.act === 'edit') {
            api('rules').then(dd => { const r = dd.rows.find(x => x.id == id); ruleForm(r); });
          }
        });
      }).catch(e => $('#content').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`);
    },

    shopitems() {
      api('shop_items').then(d => {
        // 缩略图取图顺序：本地缓存（img.php）→ 阿里 CDN → 隐藏。
        // 只挂 CDN 的话，部署机器访问不了 img.alicdn.com 就整列空白，
        // 而且不会显示"无图"，看着像 bug（2026-11-03 用户反馈）。
        const thumb = (s) => {
          const cdn = esc(s.img || '');
          if (s.item_id) {
            return `<img class="thumb" loading="lazy" referrerpolicy="no-referrer"
              src="img.php?item_id=${esc(String(s.item_id))}"
              data-cdn="${cdn}"
              onerror="var c=this.dataset.cdn;if(c&&this.src.indexOf(c)<0){this.src=c;}else{this.style.visibility='hidden';}">`;
          }
          return cdn
            ? `<img class="thumb" loading="lazy" referrerpolicy="no-referrer" src="${cdn}"
                 onerror="this.style.visibility='hidden'">`
            : '<span class="muted">无图</span>';
        };
        const rows = (d.rows || []).map(s => `
          <tr>
            <td class="nowrap">${thumb(s)}</td>
            <td>${esc(s.title)}${s.is_physical == 1 ? ' <span class="tag gray">实物</span>' : ''}</td>
            <td class="nowrap">${esc(s.price || '-')}${s.origin_price ? `<br><span class="muted" style="text-decoration:line-through;font-size:12px">${esc(s.origin_price)}</span>` : ''}</td>
            <td class="desc-clamp" title="${esc(s.descr || '')}">${esc((s.descr || '').slice(0, 60)) || '<span class="muted">—</span>'}</td>
            <td>${(s.bundles || []).map(b => `<span class="tag">${esc(b.name)}</span>`).join('') || '<span class="muted">未绑定</span>'}</td>
            <td class="nowrap">
              <button class="btn btn-sm btn-ghost" data-act="bind" data-id="${s.id}" data-bound="${(s.bundle_ids || []).join(',')}">绑定卡卷</button>
              <button class="btn btn-sm btn-danger" data-act="del" data-id="${s.id}">删除</button>
            </td></tr>`).join('') ||
          '<tr><td colspan="6" class="empty">数据库里还没有商品。点上方「⬇ 获取商品」开始采集（约 1-3 分钟），采完会自动入库，无需其他操作。</td></tr>';
        $('#content').innerHTML = `
          <div class="row">
            <button class="btn btn-primary" id="btnCollect">⬇ 获取商品</button>
            <button class="btn btn-ghost" id="btnMatchBundles">⚡ 智能匹配卡卷</button>
            <span class="muted">从闲鱼个人主页采集在售商品并自动同步到数据库；商品可绑定卡卷用于自动发货。</span>
          </div>
          <div id="collectWrap" class="progress-wrap hidden">
            <div class="progress-bar"><div id="collectBar" class="progress-inner"></div></div>
            <div id="collectText" class="progress-text">准备中…</div>
          </div>
          <div class="panel"><table><thead><tr><th>图片</th><th>商品名称</th><th>价格</th><th>简介</th><th>绑定卡卷</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
        $('#btnCollect').onclick = startCollect;
        $('#btnMatchBundles').onclick = matchBundlesForm;
        // 回显上次采集的最终状态
        api('product_collect_status').then(d => {
          if (d.status === 'done' || d.status === 'error') {
            $('#collectWrap').classList.remove('hidden');
            setProgress(d.current, d.total, d.message);
          }
        }).catch(() => {});
        $$('#content [data-act]').forEach(b => b.onclick = () => {
          const id = b.dataset.id;
          if (b.dataset.act === 'del') {
            if (!confirm('确认从数据库删除该商品？（不影响闲鱼在售状态）')) return;
            api('shop_item_delete', { id }).then(() => { toast('已删除'); pages.shopitems(); }).catch(e => toast(e.message, 'err'));
          } else if (b.dataset.act === 'bind') {
            bindBundlesForm(id, (b.dataset.bound || '').split(',').filter(Boolean));
          }
        });
      }).catch(e => $('#content').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`);
    },

    // ---------- 卡卷管理（原「卡密商品」演进而来） ----------
    // ---------- 发布商品（2026-10-20 新增，当前为界面原型） ----------
    // 设计取向：左边填内容（图/标题/描述），右边是价格库存与提交，符合
    // 电商后台的直觉；图片用 FileReader 本地预览，不落盘、不预先上传服务器，
    // 避免在没确认发布前就在服务器留一堆垃圾文件。
    publish() {
      // 页面状态只放内存：切走页面即清空，防止误以为"存过草稿"
      const state = { imgs: [], busy: false };
      const MAX_IMG = 9;

      const renderImgs = () => {
        const box = $('#pubImgs');
        if (!box) return;
        box.innerHTML = state.imgs.map((it, i) => `
          <div class="pub-thumb">
            <img src="${it.url}" alt="">
            <button class="del" data-i="${i}" title="删除">✕</button>
            ${i === 0 ? '<span class="main-tag">首图</span>' : ''}
          </div>`).join('')
          + (state.imgs.length < MAX_IMG
            ? `<div class="pub-add" id="pubAdd"><span class="plus">+</span><span>添加图片</span></div>`
            : '');
        const add = $('#pubAdd');
        if (add) add.onclick = () => $('#pubFile').click();
        box.querySelectorAll('.del').forEach(b => {
          b.onclick = () => {
            state.imgs.splice(+b.dataset.i, 1);
            renderImgs();
          };
        });
      };

      $('#content').innerHTML = `
        <div class="row" style="margin-bottom:12px">
          <span class="muted">发布闲置到闲鱼（虚拟商品）
            <span class="preview-badge">界面预览</span></span>
        </div>
        <div class="pub-layout">
          <div>
            <div class="pub-card">
              <h3>宝贝图片 <span class="muted" style="font-weight:400;font-size:12px">最多 ${MAX_IMG} 张，第一张为首图</span></h3>
              <p class="hint">建议 1:1 正方形，首图决定搜索结果里的展示效果。最多 ${MAX_IMG} 张。</p>
              <div class="pub-imgs" id="pubImgs"></div>
              <input type="file" id="pubFile" accept="image/*" multiple class="hidden">
            </div>

            <div class="pub-card">
              <h3>宝贝标题 <span class="pub-count" id="cntTitle">0/30</span></h3>
              <div class="field" style="margin:10px 0 0">
                <input class="input" id="pubTitle" maxlength="30"
                  placeholder="例如：我的世界UI面板 模型 支持ItemsAdder">
                <span class="tip">写清核心卖点（游戏名 / 用途 / 兼容插件），30 字以内，越具体越容易被搜到。</span>
              </div>
            </div>

            <div class="pub-card">
              <h3>宝贝描述 <span class="pub-count" id="cntDesc">0/1500</span></h3>
              <div class="field" style="margin:10px 0 0">
                <textarea class="input" id="pubDesc" maxlength="1500" style="min-height:160px"
                  placeholder="写清包含内容、使用方式、发货说明。虚拟商品务必写明「一经发出不支持退换」。"></textarea>
              </div>
            </div>

            <div class="pub-card">
              <h3>商品属性</h3>
              <p class="hint">上传主图后闲鱼会自动识别属性；识别不准可到闲鱼页面手动改。</p>
              <div class="field" style="margin:0">
                <label class="chk"><input type="checkbox" id="pubVirtual" checked>
                  <span>虚拟商品（无需邮寄，自动发卡密）<span class="tip">勾上后成交不需要填物流，直接发卡密；实物商品请取消勾选。</span></span></label>
              </div>
            </div>
          </div>

          <div class="pub-side">
            <div class="pub-card">
              <h3>价格与库存</h3>
              <div class="price-row" style="margin-top:10px">
                <div class="field">
                  <label>售价</label>
                  <div class="input-wrap"><span class="unit">¥</span>
                    <input class="input" id="pubPrice" placeholder="0.00"></div>
                </div>
                <div class="field">
                  <label>原价</label>
                  <div class="input-wrap"><span class="unit">¥</span>
                    <input class="input" id="pubOrigin" placeholder="0.00"></div>
                </div>
              </div>
              <div class="field">
                <label>库存</label>
                <input class="input" id="pubStock" placeholder="1">
                <span class="tip">虚拟卡密建议填大一点（如 999），卖完可随时再加。</span>
              </div>
              <div class="field" style="margin-bottom:0">
                <label>绑定卡卷</label>
                <select class="input" id="pubBundle"><option value="">暂不绑定</option></select>
                <span class="tip">绑定后，该商品成交时会自动发这个卡卷里的内容。</span>
              </div>
              <div class="pub-actions">
                <button class="btn btn-primary" id="pubSubmit">发布商品</button>
                <button class="btn btn-ghost" id="pubReset">清空重填</button>
              </div>
              <div id="pubStatus" class="pub-status hidden"></div>
              <p class="pub-note">发布过程中会自动<b>暂停自动发货引擎</b>（定时扫描会切页面、冲掉表单），
                发布结束立即恢复。全过程不弹提示，只写「运行日志」。<br>
                当前发布接口尚未接入，点按钮只做校验并留日志。</p>
            </div>
          </div>
        </div>`;

      renderImgs();

      // 图片选择 → 本地预览（不落盘）
      $('#pubFile').onchange = e => {
        const files = Array.from(e.target.files || []);
        const room = MAX_IMG - state.imgs.length;
        if (files.length > room) toast(`最多 ${MAX_IMG} 张，已取前 ${room} 张`, 'err');
        files.slice(0, room).forEach(f => {
          const url = URL.createObjectURL(f);
          state.imgs.push({ file: f, url, name: f.name });
        });
        e.target.value = '';
        renderImgs();
      };

      // 字数统计
      const bindCount = (inputId, cntId, max) => {
        const el = $('#' + inputId), cnt = $('#' + cntId);
        const upd = () => {
          const n = el.value.length;
          cnt.textContent = `${n}/${max}`;
          cnt.classList.toggle('over', n > max);
        };
        el.addEventListener('input', upd);
        upd();
      };
      bindCount('pubTitle', 'cntTitle', 30);
      bindCount('pubDesc', 'cntDesc', 1500);

      // 卡卷下拉（可选绑定）
      api('bundles').then(d => {
        const sel = $('#pubBundle');
        if (!sel) return;
        (d.rows || []).forEach(b => {
          const o = document.createElement('option');
          o.value = b.id;
          o.textContent = b.name;
          sel.appendChild(o);
        });
      }).catch(() => {});

      const submit = async () => {
        if (state.busy) return;
        const title = $('#pubTitle').value.trim();
        const price = $('#pubPrice').value.trim();
        const imgs = state.imgs.length;
        const errs = [];
        if (!imgs) errs.push('至少上传 1 张图片');
        if (!title) errs.push('填写宝贝标题');
        if (!price) errs.push('填写售价');
        else if (!/^\d+(\.\d{1,2})?$/.test(price)) errs.push('售价格式不对（最多两位小数）');
        if (errs.length) { toast(errs[0], 'err'); return; }

        // 发布期间**静默**暂停自动发货引擎：定时扫描会 goto 切路由，把正在
        // 填写的发布表单冲掉。全程不弹任何提示，只写运行日志（2026-10-20 用户要求）。
        state.busy = true;
        const btn = $('#pubSubmit');
        const btnText = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = '上传中，请稍后…'; }
        const st = $('#pubStatus');
        const setStep = t => {
          if (!st) return;
          st.classList.remove('hidden', 'err');
          st.textContent = t;
        };
        setStep('上传中，请稍后…');

        let wasRunning = false;
        let result = null;
        let failure = null;
        try {
          try {
            const r = await api('publish_engine_pause');
            wasRunning = !!(r && r.was_running);
          } catch (e) {
            // 接口异常不该阻塞发布：日志由后端/浏览器各自记录
            console.error('pause engine failed:', e);
          }
          result = await doPublish({
            title,
            price,
            origin: $('#pubOrigin').value.trim(),
            stock: $('#pubStock').value.trim() || '1',
            desc: $('#pubDesc').value.trim(),
            bundleId: $('#pubBundle').value,
            isVirtual: $('#pubVirtual').checked,
            imgs: state.imgs,
          }, setStep);
        } catch (e) {
          failure = e;
        } finally {
          // 无论成功失败都要恢复：否则引擎一直停着，静默漏单
          try {
            await api('publish_engine_resume', { force: wasRunning ? '1' : '0' });
          } catch (e) {
            console.error('resume engine failed:', e);
          }
          state.busy = false;
          if (btn) { btn.disabled = false; btn.textContent = btnText || '发布商品'; }
        }

        // 结果落在页面上（不弹窗）：成功显示商品文件夹，失败显示原因
        if (st) {
          if (failure) {
            st.classList.remove('hidden');
            st.classList.add('err');
            st.textContent = '保存失败：' + (failure.message || failure);
          } else if (result) {
            const up = result.upload || {};
            const published = up.state === 'done';
            st.classList.remove('hidden');
            st.classList.toggle('err', !published);
            st.textContent = published
              ? `发布成功 · publish/${result.dir}（${result.images} 张图）`
                + (up.message ? ` · ${up.message}` : '')
              : `已保存到 publish/${result.dir}（${result.images} 张图），但上传未成功：`
                + (up.message || up.state || '未知原因') + '，可从「运行日志」查看后重试';
          }
        }
      };
      $('#pubSubmit').onclick = submit;

      $('#pubReset').onclick = () => {
        state.imgs.forEach(i => URL.revokeObjectURL(i.url));
        state.imgs = [];
        ['pubTitle', 'pubDesc', 'pubPrice', 'pubOrigin', 'pubStock'].forEach(id => {
          const el = $('#' + id);
          if (el) el.value = '';
        });
        $('#pubBundle').value = '';
        renderImgs();
        $('#cntTitle').textContent = '0/30';
        $('#cntDesc').textContent = '0/1500';
        toast('已清空', 'ok');
      };
    },

    // ---------- 批量上传商品（文件夹模式，2026-10-20 新增） ----------
    // 用户在本地建一个工作目录，一个商品一个子文件夹，里面放「表格 + 图片」。
    // 网页只负责：选/填目录 → 扫描预览 → 一键逐个上传（异步，轮询进度）。
    batchpublish() {
      const LS_KEY = 'xy_batch_path';
      const defPath = localStorage.getItem(LS_KEY) || 'D:\\闲鱼批量上传';

      $('#content').innerHTML = `
        <div class="pub-card" style="margin-bottom:14px">
          <h3>批量上传商品</h3>
          <p class="hint">
            在电脑上建一个工作目录，<b>一个商品一个子文件夹</b>，每个文件夹里放：<br>
            ① 一个表格（<code>.xlsx</code> 或 <code>.csv</code>，名字随意），字段：
            <code>商品名称 / 价格 / 原价 / 库存 / 描述 / 卡密</code>（前两项必填，其余可选）<br>
            ② 该商品的图片（<code>1.jpg、2.jpg…</code>，按文件名顺序上传，第一张是首图）<br>
            表格写成「一行表头 + 一行内容」，或者「左列字段名 + 右列值」两种都认。<br>
            填了<b>卡密</b>的话，发布成功后会自动创建<b>同名卡卷</b>并绑定，直接就能自动发货。<br>
            <span class="muted">工作目录里已放好 <code>_模板-Excel横表</code> / <code>_模板-CSV横表</code> 和
            <code>使用说明.txt</code>：复制一份模板、改个文件夹名，直接替换内容即可。<br>
            以 <code>_</code> 开头的文件夹不会被上传；<b>名字里带「测试」或「test」的也会被跳过</b>
            （防止调试用的假商品被真发出去）。<br>
            <b>上传成功后，该商品文件夹会自动移到 <code>_已上传\</code> 里</b>，
            所以下次扫描不会再碰到它 —— 不会重复上传，也不用你自己清理。</span>
          </p>
          <div class="row" style="gap:8px;align-items:center">
            <input class="input" id="bpPath" style="flex:1;min-width:320px" value="${esc(defPath)}"
                   placeholder="例如 D:\\闲鱼批量上传">
            <button class="btn btn-ghost btn-sm" id="bpOpen">📂 打开文件夹</button>
            <button class="btn btn-primary btn-sm" id="bpScan">🔍 扫描</button>
            <button class="btn btn-primary btn-sm" id="bpRun">⬆ 开始上传</button>
          </div>
          <div id="bpLimits" class="muted" style="font-size:12px;margin-top:8px"></div>
        </div>
        <div id="bpProgress" class="pub-status hidden"></div>
        <div id="bpResult"></div>`;

      const path = () => {
        const v = $('#bpPath').value.trim();
        localStorage.setItem(LS_KEY, v);
        return v;
      };

      // 显示当前的节流节奏（设置页可改），让用户点上传前就知道要等多久
      let lim = { interval: 60, daily: 5 };
      api('settings_get').then(d => {
        const s = (d && d.settings) || {};
        lim.interval = parseInt(s.batch_interval_min ?? 60, 10) || 60;
        lim.daily = parseInt(s.batch_daily_max ?? 5, 10) || 5;
        const el = $('#bpLimits');
        if (el) {
          el.innerHTML = `当前节奏：每 <b>${lim.interval}</b> 分钟发 1 个，`
            + `每天最多 <b>${lim.daily}</b> 个 —— 可在左侧「⚙️ 设置 → 批量上传节奏」修改`;
        }
      }).catch(() => {});

      $('#bpOpen').onclick = () => {
        api('batch_open_folder', { path: path() })
          .then(r => toast('已打开：' + r.path, 'ok'))
          .catch(e => toast(e.message, 'err'));
      };

      $('#bpScan').onclick = () => {
        const p = path();
        if (!p) return toast('请先填写工作目录', 'err');
        $('#bpResult').innerHTML = '<p class="muted">扫描中…</p>';
        api('batch_scan', { path: p }).then(d => {
          const rows = d.rows || [];
          if (!d.exists) {
            $('#bpResult').innerHTML = '<p class="empty">目录不存在，点「打开文件夹」会自动创建。</p>';
            return;
          }
          if (!rows.length) {
            $('#bpResult').innerHTML = '<p class="empty">目录里没有子文件夹。请把每个商品做成一个子文件夹放进去。</p>';
            return;
          }
          const okN = rows.filter(r => r.ok).length;
          const skippedTest = d.skipped_test || [];
          $('#bpResult').innerHTML = `
            <p class="muted" style="font-size:12px;margin:0 0 8px">
              共 ${rows.length} 个文件夹，可上传 <b>${okN}</b> 个
              ${rows.length - okN ? `，<span style="color:var(--err)">${rows.length - okN} 个有问题</span>` : ''}
              ${skippedTest.length
                ? `<br><span style="color:var(--warn)">已跳过 ${skippedTest.length} 个测试文件夹</span>`
                  + `<span class="muted">：${esc(skippedTest.join('、'))}（名字含「测试」的不会上传）</span>`
                : ''}
            </p>
            <div class="panel"><table>
              <thead><tr><th style="width:72px">状态</th><th>商品名称</th><th style="width:70px">价格</th>
                <th style="width:64px">图片</th><th style="width:70px">卡密</th><th>文件夹 / 问题</th></tr></thead>
              <tbody>${rows.map(r => `
                <tr>
                  <td style="white-space:nowrap">${r.ok ? '<span class="tag">可传</span>' : '<span class="tag gray">跳过</span>'}</td>
                  <td>${esc(r.title || '—')}</td>
                  <td>${esc(r.price || '—')}</td>
                  <td>${r.image_count || 0} 张</td>
                  <td>${r.code ? '✓' : '—'}</td>
                  <td class="muted" style="font-size:12px">${esc(r.ok ? r.dir : (r.error || r.dir))}</td>
                </tr>`).join('')}</tbody>
            </table></div>`;
        }).catch(e => {
          $('#bpResult').innerHTML = `<p class="empty">扫描失败：${esc(e.message)}</p>`;
        });
      };

      // 轮询批量进度
      let timer = null;
      const poll = () => {
        api('batch_status').then(d => {
          const box = $('#bpProgress');
          if (!box) {  // 已切走页面：停掉定时器，别让它在后台一直轮询
            if (timer) { clearInterval(timer); timer = null; }
            return;
          }
          const st = d.status || 'idle';
          if (st === 'idle') { box.classList.add('hidden'); return; }
          box.classList.remove('hidden');
          box.classList.toggle('err', st === 'error');
          const cur = d.current || 0, total = d.total || 0;
          const pct = total ? Math.round(cur / total * 100) : 0;
          box.innerHTML = `<b>${esc(d.message || st)}</b>`
            + (total ? ` <span class="muted">(${cur}/${total} · ${pct}%)</span>` : '')
            + (d.ok || d.fail ? ` <span class="muted">成功 ${d.ok || 0} · 失败 ${d.fail || 0}</span>` : '');
          if (d.details && d.details.length) {
            box.innerHTML += '<div style="margin-top:6px;font-size:12px">'
              + d.details.slice(-6).map(x => `${x.ok ? '✅' : '❌'} ${esc(x.title || '')}`
                  + (x.ok ? (x.bundle ? ` · ${esc(x.bundle)}` : '') : ` · ${esc(x.error || '')}`)).join('<br>')
              + '</div>';
          }
          if (st === 'done' || st === 'error') {
            if (timer) { clearInterval(timer); timer = null; }
          }
        }).catch(() => {});
      };

      $('#bpRun').onclick = () => {
        const p = path();
        if (!p) return toast('请先填写工作目录', 'err');
        const tip = `确认开始批量上传？\n\n节奏：每 ${lim.interval} 分钟发 1 个，每天最多 ${lim.daily} 个。\n`
          + '发满当天配额会自动停下，剩下的留到第二天（会记住进度）。\n'
          + '过程中会自动暂停自动发货引擎，结束后恢复。';
        if (!confirm(tip)) return;
        api('batch_upload', { path: p })
          .then(() => {
            toast('已开始，进度见下方', 'ok');
            poll();
            if (!timer) timer = setInterval(poll, 3000);
          })
          .catch(e => toast(e.message, 'err'));
      };

      // 进页面先看一眼有没有在跑的任务
      api('batch_status').then(d => {
        if ((d.status || '') === 'running' || (d.status || '') === 'starting') {
          poll();
          if (!timer) timer = setInterval(poll, 3000);
        }
      }).catch(() => {});
    },

    products() {
      api('bundles').then(d => {
        const rows = (d.rows || []).map(b => `
          <tr>
            <td class="nowrap"><input type="checkbox" class="rowchk" value="${b.id}"></td>
            <td>${esc(b.name)}</td>
            <td class="desc-clamp" title="${esc(b.content)}">${esc((b.content || '').replace(/\n/g, ' ').slice(0, 70))}</td>
            <td class="nowrap">${b.item_cnt}</td>
            <td class="nowrap"><button class="btn btn-sm status-btn ${b.enabled == 1 ? 'on' : 'off'}" data-act="toggle" data-id="${b.id}">${b.enabled == 1 ? '启用' : '停用'}</button></td>
            <td class="nowrap">
              <button class="btn btn-sm btn-ghost" data-act="items" data-id="${b.id}">绑定商品</button>
              <button class="btn btn-sm btn-ghost" data-act="edit" data-id="${b.id}">编辑</button>
              <button class="btn btn-sm btn-danger" data-act="del" data-id="${b.id}">删除</button>
            </td></tr>`).join('') || '<tr><td colspan="6" class="empty">还没有卡卷，点上方「+ 创建卡卷」或「📋 导入卡卷」</td></tr>';
        $('#content').innerHTML = `
          <div class="row">
            <button class="btn btn-primary" id="addBundle">+ 创建卡卷</button>
            <button class="btn" id="btnImport">📋 导入卡卷</button>
            <button class="btn" id="btnExportSel" disabled>📤 导出所选 (0)</button>
            <button class="btn" id="btnExportAll">📤 导出全部</button>
          </div>
          <div class="panel"><table><thead><tr><th><input type="checkbox" id="chkAll" title="全选"></th><th>卡卷名称</th><th>内容预览</th><th>绑定商品数</th><th>状态（点击切换）</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>
          <p class="muted" style="font-size:12px">卡卷 = 一组卡密/兑换码/网盘链接。商品在「商品列表」中绑定卡卷（可多选），后续自动发货按绑定关系取用。导入支持表格文件（.xlsx/.csv/.tsv）、JSON（API 格式）和多行文本。</p>`;
        $('#addBundle').onclick = () => bundleForm();
        $('#btnImport').onclick = () => bundleImportForm();
        $('#btnExportSel').onclick = () => exportBundles(checkedIds());
        $('#btnExportAll').onclick = () => exportBundles([]);
        // 勾选同步（覆盖式赋值，不累积监听）
        $('#content').onchange = e => {
          if (e.target.id === 'chkAll') {
            $$('#content .rowchk').forEach(c => c.checked = e.target.checked);
          }
          const n = $$('#content .rowchk:checked').length;
          const btn = $('#btnExportSel');
          if (btn) { btn.textContent = `📤 导出所选 (${n})`; btn.disabled = n === 0; }
        };
        $$('#content [data-act]').forEach(btn => btn.onclick = () => {
          const id = btn.dataset.id;
          if (btn.dataset.act === 'del') {
            if (!confirm('删除卡卷会同时解除所有商品对它的绑定，确认？')) return;
            api('bundle_delete', { id }).then(() => { toast('已删除'); pages.products(); }).catch(e => toast(e.message, 'err'));
          } else if (btn.dataset.act === 'items') {
            bundleItems(id);
          } else if (btn.dataset.act === 'edit') {
            api('bundles').then(dd => { const b = (dd.rows || []).find(x => x.id == id); if (b) bundleForm(b); });
          } else if (btn.dataset.act === 'toggle') {
            api('bundle_toggle', { id }).then(() => pages.products()).catch(e => toast(e.message, 'err'));
          }
        });
      }).catch(e => $('#content').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`);
    },

    conversations() {
      api('conversations').then(d => {
        const rows = (d.rows || []).map(c => `
          <tr>
            <td>${esc(c.buyer_name)}</td>
            <td>${esc(c.preview || c.last_msg || '')}</td>
            <td class="nowrap">${c.unread ? '<span class="tag">未读</span>' : '<span class="tag gray">已读</span>'}</td>
            <td class="nowrap muted">${esc(c.last_active || '')}</td>
            <td class="nowrap"><button class="btn btn-sm btn-ghost" data-buyer="${esc(c.buyer_name)}">查看消息</button></td>
          </tr>`).join('') || '<tr><td colspan="5" class="empty">暂无会话记录（引擎运行并检测到会话后会自动写入）</td></tr>';
        $('#content').innerHTML = `
          <div class="row">
            <button class="btn btn-primary" id="btnExportMsgs">📤 导出消息</button>
            <span class="muted">把闲鱼客服里的全部会话（聊天记录 + 买家资料）导出为按昵称命名的文件，可在线下载。导出会重启客户端（自动回复暂停约 1 分钟），全程约几分钟。</span>
          </div>
          <div id="exportWrap" class="progress-wrap hidden">
            <div class="progress-bar"><div id="exportBar" class="progress-inner"></div></div>
            <div id="exportText" class="progress-text">准备中…</div>
          </div>
          <div id="exportFiles"></div>
          <div class="panel"><table><thead><tr><th>买家</th><th>预览</th><th>状态</th><th>最后活跃</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
        $('#btnExportMsgs').onclick = startExport;
        $$('#content [data-buyer]').forEach(b => b.onclick = () => msgView(b.dataset.buyer));
        // 回显上次/当前导出状态 + 已有文件（离开页面再回来不丢上下文）
        loadExportFiles();
        api('export_msgs_status').then(d => {
          if (d.status === 'running' || d.status === 'pending') {
            $('#exportWrap').classList.remove('hidden');
            setExportProgress(d.current, d.total, d.message);
            pollExport();
          } else if (d.status === 'done' || d.status === 'error') {
            $('#exportWrap').classList.remove('hidden');
            setExportProgress(d.current, d.total, d.message);
          }
        }).catch(() => {});
      }).catch(e => $('#content').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`);
    },

    // ---------- 订单管理（原「发货记录」，2026-10-17 改多列密集表格） ----------
    deliveries() {
      api('deliveries').then(d => {
        const rows = (d.rows || []).map(x => `
          <tr>
            <td class="nowrap"><input type="checkbox" class="rowchk" value="${x.id}"></td>
            <td class="mono nowrap">${esc(x.order_id || '-')}</td>
            <td class="nowrap">${x.product_id ? esc(x.product_id) : '<span class="muted">-</span>'}</td>
            <td>${esc(x.product_name || '-')}</td>
            <td>${esc(x.buyer_name || '-')}</td>
            <td class="mono desc-clamp" title="${esc(x.code || '')}">${esc((x.code || '').slice(0, 50)) || '<span class="muted">-</span>'}</td>
            <td class="nowrap">${x.status === 'failed' ? '<span class="tag gray">失败</span>' : '<span class="tag">已发货</span>'}</td>
            <td class="nowrap muted">${esc(x.created_at || '')}</td>
            <td class="nowrap">
              <button class="btn btn-sm btn-ghost" data-act="view" data-id="${x.id}">查看</button>
              <button class="btn btn-sm btn-danger" data-act="del" data-id="${x.id}">删除</button>
            </td></tr>`).join('') ||
          '<tr><td colspan="9" class="empty">暂无订单（引擎检测到买家付款并自动发货后，记录会出现在这里）</td></tr>';
        $('#content').innerHTML = `
          <div class="row">
            <input type="checkbox" id="chkAll" title="全选" style="width:15px;height:15px">
            <button class="btn btn-sm" id="btnDelSel" disabled>🗑 删除所选 (0)</button>
            <span class="muted">自动发货订单记录：订单 ID、商品、买家、发货内容一表总览。</span>
          </div>
          <div class="panel"><table><thead><tr>
            <th style="width:30px"></th><th>订单 ID</th><th>商品 ID</th><th>商品</th><th>买家昵称</th>
            <th>发货内容（卡密/链接）</th><th>状态</th><th>订单时间</th><th>操作</th>
          </tr></thead><tbody>${rows}</tbody></table></div>`;
        // 勾选同步（覆盖式赋值，不累积监听）
        $('#content').onchange = e => {
          if (e.target.id === 'chkAll') {
            $$('#content .rowchk').forEach(c => c.checked = e.target.checked);
          }
          const n = $$('#content .rowchk:checked').length;
          const btn = $('#btnDelSel');
          if (btn) { btn.textContent = `🗑 删除所选 (${n})`; btn.disabled = n === 0; }
        };
        $('#btnDelSel').onclick = () => {
          const ids = $$('#content .rowchk:checked').map(c => c.value);
          if (!ids.length) return;
          if (!confirm(`确认删除选中的 ${ids.length} 条订单记录？（仅删记录，不影响已发出的卡密）`)) return;
          api('delivery_delete', { ids: ids.join(',') })
            .then(() => { toast('已删除'); pages.deliveries(); })
            .catch(e => toast(e.message, 'err'));
        };
        $$('#content [data-act]').forEach(btn => btn.onclick = () => {
          const id = btn.dataset.id;
          if (btn.dataset.act === 'del') {
            if (!confirm('确认删除该订单记录？（仅删记录，不影响已发出的卡密）')) return;
            api('delivery_delete', { ids: id }).then(() => { toast('已删除'); pages.deliveries(); }).catch(e => toast(e.message, 'err'));
          } else if (btn.dataset.act === 'view') {
            api('deliveries').then(dd => {
              const x = (dd.rows || []).find(r => r.id == id);
              if (!x) { toast('记录不存在', 'err'); return; }
              modal('订单详情', `
                <table style="font-size:13px">
                  <tr><td class="muted nowrap" style="width:90px">订单 ID</td><td class="mono">${esc(x.order_id || '-')}</td></tr>
                  <tr><td class="muted">商品 ID</td><td>${esc(x.product_id || '-')}</td></tr>
                  <tr><td class="muted">商品</td><td>${esc(x.product_name || '-')}</td></tr>
                  <tr><td class="muted">买家</td><td>${esc(x.buyer_name || '-')}</td></tr>
                  <tr><td class="muted">发货内容</td><td class="mono" style="word-break:break-all">${esc(x.code || '-')}</td></tr>
                  <tr><td class="muted">状态</td><td>${x.status === 'failed' ? '失败' : '已发货'}</td></tr>
                  <tr><td class="muted">备注</td><td>${esc(x.note || '-')}</td></tr>
                  <tr><td class="muted">时间</td><td>${esc(x.created_at || '-')}</td></tr>
                </table>`, () => $('#modalMask').classList.remove('show'), '关闭');
            }).catch(e => toast(e.message, 'err'));
          }
        });
      }).catch(e => $('#content').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`);
    },

    activity() {
      api('activity').then(d => {
        const rows = (d.rows || []).map(a => `
          <tr><td class="nowrap muted">${esc(a.created_at)}</td><td class="nowrap">${levelTag(a.level)}</td><td>${esc(a.message)}</td></tr>`).join('') || '<tr><td colspan="3" class="empty">暂无日志</td></tr>';
        $('#content').innerHTML = `<div class="panel"><table><thead><tr><th>时间</th><th>级别</th><th>内容</th></tr></thead><tbody>${rows}</tbody></table></div>`;
      }).catch(e => $('#content').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`);
    },

    // ---------- 内网穿透（2026-10-22 新增） ----------
    // 本机跑 tunnel_client.py → 主动连你服务器的 tunnel_server.py
    // 外网访问 http://服务器:端口/ 就等于访问本机控制台（需登录）
    tunnel() {
      $('#content').innerHTML = '<p class="muted">加载中…</p>';
      Promise.all([api('tunnel_get'), api('tunnel_status')]).then(([cfgRes, st]) => {
        const c = cfgRes.config || {};
        const running = !!st.alive;
        $('#content').innerHTML = `
          <div class="pub-card" style="margin-bottom:14px">
            <h3>内网穿透</h3>
            <p class="hint">
              让<b>外网（手机 / 别的电脑）</b>也能打开这个控制台。原理：<br>
              本机客户端主动连你服务器（不需要公网 IP、不用路由器端口映射），
              服务器把收到的请求转给本机网页。<br>
              <span class="muted">⚠️ 控制台能操作店铺，暴露到公网<b>务必</b>设置令牌和访问密码，
              两者都不要留空。</span>
            </p>
            <div class="sect-grid">
              <div class="field">
                <label>服务器地址（公网 IP 或域名）</label>
                <input class="input" id="t_server" value="${esc(c.server || '')}" placeholder="例如 1.2.3.4">
              </div>
              <div class="field">
                <label>服务器端口（服务端 --port）</label>
                <input class="input" id="t_port" type="number" value="${esc(c.port ?? 8080)}">
              </div>
              <div class="field">
                <label>连接令牌（服务端 --token，两边必须一致）</label>
                <input class="input" id="t_token" value="${esc(c.token || '')}" placeholder="一串随机字符">
              </div>
              <div class="field">
                <label>本机网页端口</label>
                <input class="input" id="t_local_port" type="number" value="${esc(c.local_port ?? 8082)}">
              </div>
              <div class="field">
                <label>外网访问账号（填了服务端 --user 就写这里，仅备忘）</label>
                <input class="input" id="t_auser" value="${esc(c.access_user || '')}">
              </div>
              <div class="field">
                <label>外网访问密码（服务端 --password）</label>
                <input class="input" id="t_apass" value="${esc(c.access_password || '')}">
              </div>
            </div>
            <div class="row" style="gap:8px;margin-top:12px">
              <button class="btn btn-primary btn-sm" id="t_save">💾 保存配置</button>
              <button class="btn ${running ? 'btn-ghost' : 'btn-primary'} btn-sm" id="t_toggle">
                ${running ? '⏹ 停止客户端' : '▶ 启动客户端'}</button>
              <span id="t_state" class="muted" style="font-size:13px"></span>
            </div>
          </div>

          <div class="panel" style="margin-bottom:14px">
            <h4 style="margin:0 0 8px">① 先把服务端传上去（只需一次）</h4>
            <p class="hint" style="margin:0 0 8px">
              把 <code>tools/tunnel_server.py</code> 上传到你的服务器（Linux/Windows 都行，
              纯 Python 标准库，<b>不用 pip install</b>），然后运行：
            </p>
            <pre style="background:var(--bg-soft);padding:10px;border-radius:6px;overflow:auto;font-size:12px">python3 tunnel_server.py --port ${esc(c.port ?? 8080)} --token ${esc(c.token || '你的令牌')} \\
    --user ${esc(c.access_user || '访问账号')} --password ${esc(c.access_password || '访问密码')}</pre>
            <p class="hint" style="margin:8px 0 0">
              服务器防火墙/安全组要放行 <code>${esc(c.port ?? 8080)}</code> 端口。
              想更安全就用 nginx 反代加 HTTPS（服务端加 <code>--tls</code> 时要配证书）。
            </p>
          </div>

          <div class="panel">
            <h4 style="margin:0 0 8px">② 本机状态</h4>
            <p id="t_summary" class="hint" style="margin:0 0 8px"></p>
            <pre id="t_log" style="background:var(--bg-soft);padding:10px;border-radius:6px;max-height:220px;overflow:auto;font-size:12px;margin:0">${esc((st.log || []).join('\n') || '（暂无日志）')}</pre>
          </div>`;

        const render = () => {
          const s = $('#t_summary');
          if (!s) return;
          if (!running) {
            s.innerHTML = '<span style="color:var(--warn)">● 客户端未运行</span> —— 点「启动客户端」';
          } else if (st.connected) {
            s.innerHTML = `<span style="color:var(--ok)">● 已连接服务器</span> · PID ${st.pid}
              · 外网访问：<b>http://${esc(c.server)}:${esc(c.port)}/</b>`;
          } else {
            s.innerHTML = `<span style="color:var(--warn)">● 进程在跑，但还没连上服务器</span> · PID ${st.pid}
              <span class="muted">（检查地址/端口/令牌，或服务器防火墙）</span>`;
          }
        };
        render();

        const collect = () => ({
          server: $('#t_server').value.trim(),
          port: $('#t_port').value,
          token: $('#t_token').value.trim(),
          local_port: $('#t_local_port').value,
          access_user: $('#t_auser').value.trim(),
          access_password: $('#t_apass').value,
        });

        $('#t_save').onclick = () => {
          api('tunnel_save', collect())
            .then(() => toast('配置已保存', 'ok'))
            .catch(e => toast(e.message, 'err'));
        };

        $('#t_toggle').onclick = () => {
          if (running) {
            api('tunnel_stop').then(() => { toast('已停止', 'ok'); pages.tunnel(); })
              .catch(e => toast(e.message, 'err'));
            return;
          }
          // 先存配置再启动，避免用旧配置连
          api('tunnel_save', collect())
            .then(() => api('tunnel_start'))
            .then(() => { toast('已启动，正在连接…', 'ok'); setTimeout(() => pages.tunnel(), 2500); })
            .catch(e => toast(e.message, 'err'));
        };

        // 运行中每 4 秒刷一次状态与日志
        if (running) {
          const timer = setInterval(() => {
            if (!$('#t_summary')) { clearInterval(timer); return; }
            api('tunnel_status').then(x => {
              const el = $('#t_log');
              if (el && x.log) el.textContent = x.log.join('\n');
            }).catch(() => {});
          }, 4000);
        }
      }).catch(e => {
        $('#content').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
      });
    },

    settings() {
      Promise.all([api('settings_get'), api('db_get').catch(() => null)]).then(([d, db]) => {
        const s = d.settings || {};
        // db_get 直接返回 {ok, mode, local, remote, active, fallback}（不包 config）
        const dbc = (db && db.mode) ? db : {
          mode: 'local',
          local: { host: '', port: 3306, user: '', db: '', has_pass: false },
          remote: { host: '', port: 3306, user: '', db: '', has_pass: false },
          active: { host: '', port: '', db: '', user: '' },
          fallback: false,
        };
        // 兜底：即使接口返回了但缺字段，也不能让模板渲染崩掉
        dbc.local = dbc.local || {};
        dbc.remote = dbc.remote || {};
        dbc.active = dbc.active || { host: '', port: '', db: '', user: '' };
        const sel = typeof s.selectors === 'object' ? JSON.stringify(s.selectors, null, 2) : (s.selectors || '{}');
        $('#content').innerHTML = `
          <div class="panel">
            <h3>⚙️ 运行设置</h3>

            <div class="sect">
              <div class="sect-head"><span class="ico">🔌</span><h4>连接与轮询</h4></div>
              <p class="sect-desc">软件通过<b>调试端口</b>接管闲鱼客户端窗口，再按<b>轮询间隔</b>反复检查有没有新消息。这一组只在软件连不上、或想调整响应速度时才需要动。</p>
              <div class="sect-grid">
                <div class="field">
                  <label>调试端口 cdp_port</label>
                  <input class="input" id="f_port" value="${esc(s.cdp_port ?? 9222)}">
                  <span class="tip">闲鱼客户端开放的远程调试端口，默认 9222。改了这里就必须让客户端也用同一端口启动，否则引擎连不上。</span>
                </div>
                <div class="field">
                  <label>轮询间隔（秒）</label>
                  <input class="input" id="f_poll" value="${esc(s.poll_interval ?? 3)}">
                  <span class="tip">每隔多少秒检查一次会话列表。越小响应越快，但越占 CPU；3 秒是稳妥值，不建议低于 2。</span>
                </div>
              </div>
            </div>

            <div class="sect">
              <div class="sect-head"><span class="ico">🛡️</span><h4>回复频率保护</h4></div>
              <p class="sect-desc">防止<strong>同一句话被反复回给同一个人</strong>、或短时间内回复太密被平台判定异常。这几项只影响「关键词自动回复」，<b>不影响发卡密</b>——买家付了钱就一定发货，不卡频率。</p>
              <div class="sect-grid">
                <div class="field wide">
                  <label class="chk"><input type="checkbox" id="f_rl" ${(s.rate_limit_enabled ?? 1) ? 'checked' : ''}>
                    <span>启用频率限制<span class="tip">日常保持勾选。调试规则时取消，可以对着同一个会话反复触发回复。</span></span></label>
                </div>
                <div class="field">
                  <label>每小时最大回复数（全局）</label>
                  <input class="input" id="f_max" value="${esc(s.max_replies_per_hour ?? 60)}">
                  <span class="tip">全部买家合计，一小时最多自动回复多少条。超了就暂停到下一小时，防止刷屏。</span>
                </div>
                <div class="field">
                  <label>每买家每小时最多回复数</label>
                  <input class="input" id="f_peruser" value="${esc(s.rate_limit_per_user_hour ?? 3)}">
                  <span class="tip">单个买家一小时最多回几条。设小一点可避免对同一个人反复答同一问题。</span>
                </div>
                <div class="field wide">
                  <label>同一消息去重窗口（秒，防扫描抖动）</label>
                  <input class="input" id="f_window" value="${esc(s.rate_limit_window_sec ?? 120)}">
                  <span class="tip">同样的消息内容在这个时间内只回一次。窗口太短会重复回，太长则买家追问同一句话时不会被回应。</span>
                </div>
              </div>
            </div>

            <div class="sect">
              <div class="sect-head"><span class="ico">📦</span><h4>自动发货扫描</h4></div>
              <p class="sect-desc">买家付款后常常<b>一句话都不说</b>，既没有未读消息、也不会触发回复，光靠等消息是发现不了订单的。这组设置让软件定期主动翻会话、读订单，把这类「静默订单」捞出来自动发货。<b>这是自动发货的关键开关</b>，关掉后会漏单。</p>
              <div class="sect-grid">
                <div class="field wide">
                  <label class="chk"><input type="checkbox" id="f_scan_on" ${(s.scan_enabled ?? 0) ? 'checked' : ''}>
                    <span>定时扫描静默买家<span class="tip">开启后按下方间隔定期翻看会话列表，读订单面板判断谁付了款。买家付完款不再说话也能自动发货。</span></span></label>
                </div>
                <div class="field">
                  <label>扫描范围（0=全部会话，1=只看列表第一个，2=前两个，以此类推；默认 10）</label>
                  <input class="input" id="f_scan_scope" type="number" min="0" max="500" step="1" value="${esc(s.scan_scope ?? 10)}">
                  <span class="tip">每轮最多往前翻几个会话。会话多、怕漏单就填 0（全部）：更不容易漏，但一轮耗时更久。</span>
                </div>
                <div class="field">
                  <label>扫描间隔（秒，最小 30；默认 180）</label>
                  <input class="input" id="f_scan_interval" type="number" min="30" max="3600" step="1" value="${esc(s.scan_interval ?? 180)}">
                  <span class="tip">隔多久扫一轮。越短发货越及时，但翻看会话也越频繁；180 秒（3 分钟）是平衡值。</span>
                </div>
              </div>
            </div>

            <div class="sect">
              <div class="sect-head"><span class="ico">📚</span><h4>批量上传节奏</h4></div>
              <p class="sect-desc">控制「批量上传」发商品的节奏，避免短时间集中发布触发平台风控。改完保存立即生效，正在跑的队列会在下一个间隔按新值走。</p>
              <div class="sect-grid">
                <div class="field">
                  <label>上传间隔（分钟，最小 1；默认 60）</label>
                  <input class="input" id="f_batch_interval" type="number" min="1" max="1440" step="1"
                         value="${esc(s.batch_interval_min ?? 60)}">
                  <span class="tip">每发完一个商品，等这么久再发下一个。默认 60 分钟（一小时发一个）。</span>
                </div>
                <div class="field">
                  <label>每天最多上传（个，最小 1；默认 5）</label>
                  <input class="input" id="f_batch_daily" type="number" min="1" max="200" step="1"
                         value="${esc(s.batch_daily_max ?? 5)}">
                  <span class="tip">当天发满这个数就停，剩下的留到第二天继续（队列会记住进度）。</span>
                </div>
              </div>
            </div>

            <div class="sect">
              <div class="sect-head"><span class="ico">🗄</span><h4>数据库连接</h4></div>
              <p class="sect-desc">
                所有数据（卡卷、商品、规则、订单）都存在 MySQL 里。可以存<b>本机</b>，
                也可以存<b>你的服务器</b>——后者能让多台电脑共用同一份数据。
                ${dbc.fallback ? '<br><b style="color:var(--warn)">⚠️ 当前配置的是远程库，但连不上，已自动回退本地库。</b>' : ''}
                <br><span class="muted">切换后<b>引擎需要重启</b>才会用新库；网页端保存即生效。</span>
              </p>
              <div class="row" style="gap:20px;margin-bottom:12px">
                <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
                  <input type="radio" name="dbmode" value="local" ${dbc.mode !== 'remote' ? 'checked' : ''}> 本地数据库
                </label>
                <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
                  <input type="radio" name="dbmode" value="remote" ${dbc.mode === 'remote' ? 'checked' : ''}> 网络数据库（服务器）
                </label>
              </div>

              <div id="dbBoxLocal" style="display:${dbc.mode === 'remote' ? 'none' : 'block'}">
                <div class="sect-grid">
                  <div class="field"><label>主机</label>
                    <input class="input" id="db_l_host" value="${esc(dbc.local.host || '127.0.0.1')}"></div>
                  <div class="field"><label>端口</label>
                    <input class="input" id="db_l_port" type="number" value="${esc(dbc.local.port || 3306)}"></div>
                  <div class="field"><label>库名</label>
                    <input class="input" id="db_l_db" value="${esc(dbc.local.db || 'xianyu_autoreply')}"></div>
                  <div class="field"><label>账号</label>
                    <input class="input" id="db_l_user" value="${esc(dbc.local.user || 'root')}"></div>
                  <div class="field"><label>密码</label>
                    <input class="input" id="db_l_pass" type="password"
                           placeholder="${dbc.local.has_pass ? '已设置，留空不修改' : '未设置'}"></div>
                </div>
              </div>

              <div id="dbBoxRemote" style="display:${dbc.mode === 'remote' ? 'block' : 'none'}">
                <div class="sect-grid">
                  <div class="field"><label>主机（IP 或域名）</label>
                    <input class="input" id="db_r_host" value="${esc(dbc.remote.host || '')}" placeholder="例如 1.2.3.4"></div>
                  <div class="field"><label>端口</label>
                    <input class="input" id="db_r_port" type="number" value="${esc(dbc.remote.port || 3306)}"></div>
                  <div class="field"><label>库名</label>
                    <input class="input" id="db_r_db" value="${esc(dbc.remote.db || '')}"></div>
                  <div class="field"><label>账号</label>
                    <input class="input" id="db_r_user" value="${esc(dbc.remote.user || '')}"></div>
                  <div class="field"><label>密码</label>
                    <input class="input" id="db_r_pass" type="password"
                           placeholder="${dbc.remote.has_pass ? '已设置，留空不修改' : '未设置'}"></div>
                </div>
                <p class="hint" style="margin:8px 0 0">
                  服务器上的 MySQL 要允许远程连接（绑定 0.0.0.0 + 账号 host 为 <code>%</code>），
                  云服务器的安全组也要放行对应端口。
                </p>
              </div>

              <div class="row" style="gap:8px;margin-top:12px">
                <button class="btn btn-ghost btn-sm" id="dbTest">测试连接</button>
                <button class="btn btn-primary btn-sm" id="dbSave">保存并生效</button>
                <span id="dbState" class="muted" style="font-size:12px"></span>
              </div>
              <p class="hint" style="margin:8px 0 0">
                当前实际连接：<b>${esc(dbc.active.host || '-')}:${esc(dbc.active.port || '')}</b>
                / 库 <b>${esc(dbc.active.db || '-')}</b>
                ${dbc.fallback ? '<span style="color:var(--warn)">（回退中）</span>' : ''}
              </p>
            </div>

            <div class="sect">
              <div class="sect-head"><span class="ico">🔐</span><h4>登录密码</h4></div>
              <p class="sect-desc">控制台的登录账号固定为 <b>admin</b>，这里只改密码。密码只存哈希，忘了只能删 <code>data/auth.json</code> 重置回默认。</p>
              <div class="sect-grid">
                <div class="field">
                  <label>当前密码</label>
                  <input class="input" id="f_pass_old" type="password" autocomplete="current-password">
                </div>
                <div class="field">
                  <label>新密码（至少 6 位）</label>
                  <input class="input" id="f_pass_new" type="password" autocomplete="new-password">
                </div>
              </div>
              <div class="row" style="margin-top:10px">
                <button class="btn btn-primary btn-sm" id="btnChangePass">修改密码</button>
              </div>
              <div class="field" style="margin-top:14px">
                <label class="chk"><input type="checkbox" id="f_login_guard" ${(s.login_guard_enabled ?? 1) ? 'checked' : ''}>
                  <span>登录失败限制<span class="tip">同一 IP 连续输错 5 次密码，锁定 5 分钟，防止脚本暴力猜密码。只在自己电脑上用、没开内网穿透时可以关掉。改动立即生效。</span></span></label>
              </div>
            </div>

            <div class="sect">
              <div class="sect-head"><span class="ico">🧪</span><h4>调试与高级</h4></div>
              <p class="sect-desc">一般不需要动。只有在闲鱼改版、软件识别不到输入框或订单时，才需要调下面的选择器。</p>
              <div class="sect-grid">
                <div class="field wide">
                  <label class="chk"><input type="checkbox" id="f_dry" ${s.dry_run ? 'checked' : ''}>
                    <span>试运行模式（只检测不真正发送）<span class="tip">勾上后所有回复和卡密都只写日志、不真的发出去。适合先验证规则命中是否符合预期。</span></span></label>
                </div>
                <div class="field wide">
                  <label>页面选择器（JSON，软件升级导致检测失效时调整）</label>
                  <textarea class="input" id="f_sel">${esc(sel)}</textarea>
                  <span class="tip">闲鱼客户端页面元素的定位规则。改错会导致消息发不出去或订单读不到，改动前建议先备份这一段。</span>
                </div>
              </div>
            </div>

            <div class="sect-actions">
              <button class="btn btn-primary" id="saveSet">保存设置</button>
            </div>
          </div>`;
        // ---- 数据库连接 ----
        const dbMode = () => {
          const r = document.querySelector('input[name=dbmode]:checked');
          return r ? r.value : 'local';
        };
        const dbPrefix = () => (dbMode() === 'remote' ? 'db_r_' : 'db_l_');
        const dbOne = () => {
          const p = dbPrefix();
          return {
            host: $('#' + p + 'host').value.trim(),
            port: $('#' + p + 'port').value,
            user: $('#' + p + 'user').value.trim(),
            db: $('#' + p + 'db').value.trim(),
            pass: $('#' + p + 'pass').value,
          };
        };
        document.querySelectorAll('input[name=dbmode]').forEach(r => {
          r.onchange = () => {
            const remote = dbMode() === 'remote';
            $('#dbBoxLocal').style.display = remote ? 'none' : 'block';
            $('#dbBoxRemote').style.display = remote ? 'block' : 'none';
            $('#dbState').textContent = '';
          };
        });
        if ($('#dbTest')) {
          $('#dbTest').onclick = () => {
            const p = dbOne();
            $('#dbState').textContent = '测试中…';
            api('db_test', p).then(r => {
              $('#dbState').innerHTML = `<span style="color:var(--ok)">✓ 连接成功`
                + `（MySQL ${esc(r.version || '')}，${r.tables} 张表）</span>`;
            }).catch(e => {
              $('#dbState').innerHTML = `<span style="color:var(--err)">✗ ${esc(e.message)}</span>`;
            });
          };
          $('#dbSave').onclick = () => {
            const m = dbMode();
            const g = id => $('#' + id).value;
            const params = {
              mode: m,
              local_host: g('db_l_host'), local_port: g('db_l_port'),
              local_user: g('db_l_user'), local_db: g('db_l_db'), local_pass: g('db_l_pass'),
              remote_host: g('db_r_host'), remote_port: g('db_r_port'),
              remote_user: g('db_r_user'), remote_db: g('db_r_db'), remote_pass: g('db_r_pass'),
            };
            if (m === 'remote' && !params.remote_host) {
              toast('选择网络数据库时，主机地址必填', 'err'); return;
            }
            api('db_save', params).then(r => {
              toast(r.msg || '已保存', 'ok');
              // 网页端每次请求重新读配置，已生效；引擎是长驻进程，要重启才换库
              return api('app_status').then(st => {
                if (st && st.running) {
                  toast('正在重启引擎以使用新库…', '');
                  return api('engine_stop').then(() => api('engine_start'));
                }
              }).catch(() => {});
            }).then(() => {
              setTimeout(() => pages.settings(), 1500);
            }).catch(e => toast(e.message, 'err'));
          };
        }

        $('#btnChangePass').onclick = () => {
          const oldP = $('#f_pass_old').value;
          const newP = $('#f_pass_new').value;
          if (!oldP || !newP) { toast('请填写当前密码和新密码', 'err'); return; }
          if (newP.length < 6) { toast('新密码至少 6 位', 'err'); return; }
          api('auth_change_pass', { old: oldP, new: newP })
            .then(r => {
              toast(r.msg || '密码已修改', 'ok');
              $('#f_pass_old').value = '';
              $('#f_pass_new').value = '';
            })
            .catch(e => toast(e.message, 'err'));
        };

        // 登录失败限制开关：单独即时保存，不必再点底部「保存设置」
        // （这个开关跟密码放在一起，用户直觉是"勾了就生效"）
        const guardEl = $('#f_login_guard');
        if (guardEl && !guardEl.dataset.bound) {
          guardEl.dataset.bound = '1';
          guardEl.onchange = () => {
            const v = guardEl.checked ? 1 : 0;
            api('settings_save', { login_guard_enabled: v })
              .then(() => toast(v ? '已开启：连错 5 次锁 5 分钟' : '已关闭：不再限制登录失败次数', 'ok'))
              .catch(e => { toast(e.message, 'err'); guardEl.checked = !guardEl.checked; });
          };
        }

        $('#saveSet').onclick = () => {
          let sel;
          try { sel = JSON.stringify(JSON.parse($('#f_sel').value)); } catch (e) { toast('选择器不是合法 JSON', 'err'); return; }
          api('settings_save', {
            cdp_port: $('#f_port').value, poll_interval: $('#f_poll').value,
            max_replies_per_hour: $('#f_max').value, dry_run: $('#f_dry').checked ? 1 : 0,
            rate_limit_enabled: $('#f_rl').checked ? 1 : 0,
            rate_limit_per_user_hour: $('#f_peruser').value,
            rate_limit_window_sec: $('#f_window').value, selectors: sel,
            scan_enabled: $('#f_scan_on').checked ? 1 : 0,
            login_guard_enabled: $('#f_login_guard').checked ? 1 : 0,
            scan_scope: $('#f_scan_scope').value,
            scan_interval: $('#f_scan_interval').value,
            batch_interval_min: $('#f_batch_interval').value,
            batch_daily_max: $('#f_batch_daily').value
          }).then(() => { toast('已保存，频率与扫描参数即时生效', 'ok'); refreshDryBadge(); }).catch(e => toast(e.message, 'err'));
        };
      }).catch(e => $('#content').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`);
    }
  };

  function levelTag(l) {
    const c = { ERROR: 'err', WARN: 'warn', INFO: '' }[l] || '';
    const t = { ERROR: '错误', WARN: '警告', INFO: '信息' }[l] || l;
    return `<span class="tag ${c}">${t}</span>`;
  }
  function statusTag(s) {
    return s === 'failed' ? '<span class="tag gray">失败</span>' : '<span class="tag">已发</span>';
  }

  // ---------- 表单 ----------
  function ruleForm(r = null) {
    const kws = r ? (r.keywords || []).join('\n') : '';
    modal(r ? '编辑规则' : '新增规则', `
      <div class="field"><label>关键词（每行一个，或逗号分隔；任一命中即触发）</label><textarea class="input" id="r_kw">${esc(kws)}</textarea></div>
      <div class="field"><label>回复内容</label><textarea class="input" id="r_rep">${esc(r ? r.reply : '')}</textarea></div>
      <div class="field"><label>优先级（数字越大越优先匹配）</label><input class="input" id="r_pri" value="${r ? r.priority : 0}"></div>
      <div class="field"><label><input type="checkbox" id="r_en" ${r ? (r.enabled ? 'checked' : '') : 'checked'}> 启用</label></div>
    `, () => {
      const rep = $('#r_rep').value.trim();
      if (!rep) throw new Error('回复内容不能为空');
      const kw = $('#r_kw').value;
      if (!kw.trim()) throw new Error('至少填一个关键词');
      const p = { reply: rep, priority: $('#r_pri').value, enabled: $('#r_en').checked ? 1 : 0, keywords: kw };
      if (r) p.id = r.id;
      (r ? api('rule_update', p) : api('rule_add', p))
        .then(() => { $('#modalMask').classList.remove('show'); toast('已保存', 'ok'); pages.rules(); })
        .catch(e => toast(e.message, 'err'));
    });
  }
  function productForm() {
    modal('新增卡密商品', `
      <div class="field"><label>商品名称</label><input class="input" id="p_name"></div>
      <div class="field"><label>匹配关键词（订单卡片文案里出现即命中，每行一个）</label><textarea class="input" id="p_kw"></textarea></div>
    `, () => {
      const name = $('#p_name').value.trim();
      if (!name) throw new Error('商品名称不能为空');
      if (!$('#p_kw').value.trim()) throw new Error('至少填一个关键词');
      api('product_add', { name, keywords: $('#p_kw').value })
        .then(() => { $('#modalMask').classList.remove('show'); toast('已添加', 'ok'); pages.products(); })
        .catch(e => toast(e.message, 'err'));
    });
  }
  function codesForm(pid) {
    modal('导入卡密', `<div class="field"><label>每行一条卡密 / 网盘链接，可粘贴多行</label><textarea class="input" id="c_codes" placeholder="https://pan.quark.cn/xxxx&#10;ABCD-EFGH-IJKL&#10;..."></textarea></div>`, () => {
      const txt = $('#c_codes').value;
      if (!txt.trim()) throw new Error('请粘贴卡密');
      api('codes_add', { product_id: pid, codes: txt })
        .then(d => { $('#modalMask').classList.remove('show'); toast('已导入 ' + (d.imported || 0) + ' 条', 'ok'); pages.products(); })
        .catch(e => toast(e.message, 'err'));
    }, '导入');
  }
  function invView(pid) {
    api('inventory', { product_id: pid }).then(d => {
      const rows = (d.rows || []).map(x => `
        <tr><td class="mono">${esc(x.code)}</td>
          <td>${x.used ? '<span class="tag gray">已用</span>' : '<span class="tag">未用</span>'}</td>
          <td class="nowrap muted">${esc(x.used_by || '')} ${esc(x.used_at || '')}</td></tr>`).join('') || '<tr><td colspan="3" class="empty">该商品暂无卡密</td></tr>';
      modal('卡密库存', `<div class="panel" style="box-shadow:none;padding:0"><table><thead><tr><th>卡密</th><th>状态</th><th>核销信息</th></tr></thead><tbody>${rows}</tbody></table></div>`, () => $('#modalMask').classList.remove('show'), '关闭');
    }).catch(e => toast(e.message, 'err'));
  }
  // ---------- 卡卷表单 / 商品绑卡卷 ----------
  function bindBundlesForm(itemId, boundIds) {
    api('bundles').then(d => {
      const boxes = (d.rows || []).map(b =>
        `<label class="chk"><input type="checkbox" value="${b.id}" ${boundIds.includes(String(b.id)) ? 'checked' : ''}> ${esc(b.name)}</label>`
      ).join('') || '<div class="empty">还没有卡卷，请先到「卡卷管理」创建</div>';
      modal('绑定卡卷', `<div class="chk-list">${boxes}</div>
        <p class="muted" style="font-size:12px;margin-top:10px">勾选后保存：该商品绑定选中的卡卷（可多选）。发货时按绑定关系取用。</p>`, () => {
        const ids = $$('#mBody input:checked').map(x => x.value);
        api('item_bind_bundles', { id: itemId, bundle_ids: ids.join(',') })
          .then(() => { $('#modalMask').classList.remove('show'); toast('已保存绑定', 'ok'); pages.shopitems(); })
          .catch(e => toast(e.message, 'err'));
      }, '保存绑定');
    }).catch(e => toast(e.message, 'err'));
  }
  function bundleForm(b = null) {
    modal(b ? '编辑卡卷' : '创建卡卷', `
      <div class="field"><label>卡卷名称</label><input class="input" id="b_name" value="${esc(b ? b.name : '')}" placeholder="如：春节礼包 / ItemsAdder 模型包"></div>
      <div class="field"><label>卡卷具体内容（每行一条：卡密 / 兑换码 / 网盘链接+提取码）</label>
        <textarea class="input" id="b_content" style="min-height:180px" placeholder="https://pan.quark.cn/s/xxxx&#10;ABCD-EFGH-IJKL&#10;...">${esc(b ? b.content : '')}</textarea></div>
    `, () => {
      const name = $('#b_name').value.trim();
      const content = $('#b_content').value;
      if (!name) throw new Error('卡卷名称不能为空');
      if (!content.trim()) throw new Error('卡卷内容不能为空');
      const p = { name, content };
      if (b) p.id = b.id;
      (b ? api('bundle_update', p) : api('bundle_add', p))
        .then(() => { $('#modalMask').classList.remove('show'); toast('已保存', 'ok'); pages.products(); })
        .catch(e => toast(e.message, 'err'));
    });
  }
  function bundleItems(bid) {
    api('bundle_items', { id: bid }).then(d => {
      const rows = (d.rows || []).map(x => `<li>${esc(x.title)}</li>`).join('') || '<li class="muted">暂无商品绑定此卡卷（去「商品列表」给商品绑）</li>';
      modal('绑定的商品', `<ul style="margin:0;padding-left:18px;font-size:13px">${rows}</ul>`, () => $('#modalMask').classList.remove('show'), '关闭');
    }).catch(e => toast(e.message, 'err'));
  }

  // ---------- 卡卷导入 / 导出 ----------
  function checkedIds() {
    return $$('#content .rowchk:checked').map(c => c.value);
  }
  /** 解析粘贴的表格文本（Excel 复制为 TSV）：每行一条，Tab 分列；
   *  无 Tab 时容错按中文逗号/逗号拆分；名称列为空的行丢弃。 */
  // ---------- 卡卷导入：多格式解析（2026-10-19 用户要求） ----------
  // 支持三种来源，统一解析成 [{name, content}]：
  //   1. 表格文件：.xlsx（自带精简 zip 解析，零依赖）、.csv、.tsv
  //   2. JSON / API 格式：[{name,content}] / {data:[...]} / {"卡卷名":"内容"}
  //   3. 文本：块格式（=== 名称 === 后跟多行内容）或分隔符表格
  //
  // 为什么要支持块格式：卡密内容是**多行长文本**（网盘链接+提取码+温馨提示），
  // 老的按行 TSV 解析根本表达不了多行，只能拆成一堆残行。

  // 字段别名（大小写不敏感）——各家 API/导出的字段名不统一，尽量都认
  const BUNDLE_NAME_KEYS = ['name', '名称', '卡卷名称', '卡券名称', 'title', '标题',
    '卡密名称', 'item_name', 'card_name', 'cardname', 'label'];
  const BUNDLE_CONTENT_KEYS = ['content', '内容', '卡密', '卡密内容', 'body', 'text',
    'txt', '描述', '值', 'value', 'card', 'code', '数据', 'data'];

  function _normKey(k) { return String(k || '').trim().toLowerCase().replace(/[\s_-]/g, ''); }
  function _pickField(obj, keys) {
    const map = {};
    Object.keys(obj || {}).forEach(k => { map[_normKey(k)] = k; });
    for (const k of keys) {
      const hit = map[_normKey(k)];
      if (hit !== undefined && obj[hit] != null) return obj[hit];
    }
    return undefined;
  }

  /** 通用分隔符解析（支持引号包裹、引号内的分隔符与换行）。 */
  function _parseDelimited(text, delim) {
    const rows = [];
    let cur = [], cell = '', q = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (q) {
        if (ch === '"') {
          if (text[i + 1] === '"') { cell += '"'; i++; } else q = false;
        } else cell += ch;
      } else if (ch === '"') q = true;
      else if (ch === delim) { cur.push(cell); cell = ''; }
      else if (ch === '\n') { cur.push(cell); cell = ''; rows.push(cur); cur = []; }
      else if (ch === '\r') { /* 忽略 CR */ }
      else cell += ch;
    }
    if (cell !== '' || cur.length) { cur.push(cell); rows.push(cur); }
    return rows.filter(r => r.some(x => String(x).trim() !== ''));
  }

  /** 二维行 → items。首行若像表头则跳过；名称缺失自动补「卡卷N」。 */
  function _rowsToItems(rows) {
    if (!rows || !rows.length) return [];
    let start = 0;
    const h0 = _normKey(rows[0][0] || ''), h1 = _normKey(rows[0][1] || '');
    const isHeader = BUNDLE_NAME_KEYS.some(k => _normKey(k) === h0)
      || BUNDLE_CONTENT_KEYS.some(k => _normKey(k) === h1);
    if (isHeader) start = 1;
    const items = [];
    for (let i = start; i < rows.length; i++) {
      const r = rows[i];
      const content = String(r.length > 1 ? r.slice(1).join('\n') : (r[1] || '')).trim();
      if (!content) continue;
      let name = String(r[0] || '').trim();
      if (!name) name = '卡卷' + (items.length + 1);
      items.push({ name, content });
    }
    return items;
  }

  /** 文本格式：优先识别块分隔（=== 名称 === / ## 名称 / --- 名称 ---），
   *  否则按首行猜分隔符走表格解析。
   *  刻意**不**把 【名称】 当分隔符——卡密正文里就有「📁【网盘位置】」这类方括号，
   *  会把自己的正文切断。 */
  function parseBundleText(text) {
    const src = String(text || '');
    if (!src.trim()) return [];
    const sepRe = /^\s*(?:={2,}\s*(.+?)\s*={2,}|#{1,6}\s+(.+?)\s*|-{3,}\s*(.+?)\s*-{3,})\s*$/;
    const lines = src.split(/\r?\n/);
    let hasBlock = false;
    for (const l of lines) { if (sepRe.test(l)) { hasBlock = true; break; } }
    if (hasBlock) {
      const items = [];
      let curName = null, buf = [];
      const flush = () => {
        const content = buf.join('\n').trim();
        if (curName !== null && content) {
          items.push({ name: curName || ('卡卷' + (items.length + 1)), content });
        }
        buf = [];
      };
      for (const line of lines) {
        const m = line.match(sepRe);
        if (m) { flush(); curName = (m[1] || m[2] || m[3] || '').trim(); }
        else if (curName !== null) buf.push(line);
      }
      flush();
      if (items.length) return items;
    }
    // 没有块分隔 → 按分隔符表格解析
    const firstLine = lines.find(l => l.trim()) || '';
    let delim = '\t';
    if (!firstLine.includes('\t')) {
      if (firstLine.includes('，')) delim = '，';
      else if (firstLine.includes(',')) delim = ',';
      else if (firstLine.includes('|')) delim = '|';
      else if (firstLine.includes('=')) delim = '=';
      else if (firstLine.includes('：')) delim = '：';
      else if (firstLine.includes(':')) delim = ':';
    }
    return _rowsToItems(_parseDelimited(src, delim));
  }

  /** JSON / API 格式。兼容数组、常见包裹键、以及 {"名称":"内容"} 映射。 */
  function parseBundleJson(text) {
    let data;
    try { data = JSON.parse(text); }
    catch (e) { throw new Error('JSON 格式错误：' + e.message); }
    let arr = data;
    if (!Array.isArray(arr) && data && typeof data === 'object') {
      for (const k of ['data', 'items', 'rows', 'list', 'result', 'cards', 'bundles']) {
        if (Array.isArray(data[k])) { arr = data[k]; break; }
      }
    }
    if (Array.isArray(arr)) {
      const items = [];
      for (const it of arr) {
        if (it == null) continue;
        if (typeof it === 'string') {
          const c = it.trim();
          if (c) items.push({ name: '卡卷' + (items.length + 1), content: c });
          continue;
        }
        if (typeof it !== 'object') continue;
        const rawName = _pickField(it, BUNDLE_NAME_KEYS);
        const rawContent = _pickField(it, BUNDLE_CONTENT_KEYS);
        const content = rawContent == null ? ''
          : (typeof rawContent === 'string' ? rawContent : JSON.stringify(rawContent, null, 2));
        if (!content.trim()) continue;
        items.push({
          name: String(rawName == null ? '' : rawName).trim() || ('卡卷' + (items.length + 1)),
          content,
        });
      }
      return items;
    }
    if (data && typeof data === 'object') {
      // 单个卡卷对象：{"名称":"x","内容":"y"} / {"name":"..","content":".."}
      // 必须先判这一种，否则会被下面「名称→内容」映射分支拆成两条脏数据。
      const single = _pickField(data, BUNDLE_CONTENT_KEYS);
      if (single !== undefined && single !== null && typeof single !== 'object') {
        const nm = _pickField(data, BUNDLE_NAME_KEYS);
        const content = String(single);
        if (content.trim()) {
          return [{
            name: String(nm == null ? '' : nm).trim() || '卡卷1',
            content,
          }];
        }
      }
      const items = [];
      Object.keys(data).forEach(k => {
        const v = data[k];
        if (v == null) return;
        const content = typeof v === 'string' ? v : JSON.stringify(v, null, 2);
        if (!String(content).trim()) return;
        items.push({ name: k, content });
      });
      return items;
    }
    throw new Error('JSON 结构无法识别：需要数组，或 {"名称":"内容"} 对象');
  }

  // ---- 精简 xlsx 读取（xlsx = zip，浏览器原生 DecompressionStream 解压，
  //      不引入第三方库，保证离线可用） ----
  async function _unzip(buf) {
    if (typeof DecompressionStream === 'undefined') {
      throw new Error('当前浏览器不支持解压 xlsx，请另存为 CSV 再导入');
    }
    const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    let eocd = -1;
    const minPos = Math.max(0, buf.length - 65558);
    for (let i = buf.length - 22; i >= minPos; i--) {
      if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error('不是有效的 xlsx 文件（未找到 zip 结构）');
    const count = dv.getUint16(eocd + 10, true);
    let off = dv.getUint32(eocd + 16, true);
    const out = {};
    const dec = new TextDecoder();
    for (let i = 0; i < count; i++) {
      if (dv.getUint32(off, true) !== 0x02014b50) break;
      const method = dv.getUint16(off + 10, true);
      const csize = dv.getUint32(off + 20, true);
      const nlen = dv.getUint16(off + 28, true);
      const elen = dv.getUint16(off + 30, true);
      const clen = dv.getUint16(off + 32, true);
      const lho = dv.getUint32(off + 42, true);
      const name = dec.decode(buf.subarray(off + 46, off + 46 + nlen));
      const lnlen = dv.getUint16(lho + 26, true);
      const lelen = dv.getUint16(lho + 28, true);
      const start = lho + 30 + lnlen + lelen;
      out[name] = { method, raw: buf.subarray(start, start + csize) };
      off += 46 + nlen + elen + clen;
    }
    return out;
  }

  async function _inflate(entry) {
    if (!entry) return '';
    if (entry.method === 0) return new TextDecoder().decode(entry.raw);
    const stream = new Blob([entry.raw]).stream()
      .pipeThrough(new DecompressionStream('deflate-raw'));
    return new TextDecoder().decode(await new Response(stream).arrayBuffer());
  }

  async function _parseXlsx(arrayBuffer) {
    const buf = new Uint8Array(arrayBuffer);
    const files = await _unzip(buf);
    // 共享字符串表
    let shared = [];
    const ssEntry = files['xl/sharedStrings.xml'];
    if (ssEntry) {
      const xml = await _inflate(ssEntry);
      const doc = new DOMParser().parseFromString(xml, 'application/xml');
      shared = Array.from(doc.getElementsByTagName('si')).map(si =>
        Array.from(si.getElementsByTagName('t')).map(t => t.textContent || '').join(''));
    }
    // 第一个工作表
    let sheetKey = Object.keys(files).find(k => k === 'xl/worksheets/sheet1.xml')
      || Object.keys(files).filter(k => /^xl\/worksheets\/.+\.xml$/.test(k))[0];
    if (!sheetKey) throw new Error('xlsx 里没有找到工作表');
    const sx = await _inflate(files[sheetKey]);
    const sdoc = new DOMParser().parseFromString(sx, 'application/xml');
    const rows = [];
    Array.from(sdoc.getElementsByTagName('row')).forEach(rowEl => {
      const cells = [];
      Array.from(rowEl.getElementsByTagName('c')).forEach(c => {
        const t = c.getAttribute('t');
        const isEl = c.getElementsByTagName('is')[0];
        const vEl = c.getElementsByTagName('v')[0];
        let val = '';
        if (t === 'inlineStr' && isEl) {
          val = Array.from(isEl.getElementsByTagName('t')).map(x => x.textContent || '').join('');
        } else if (vEl) {
          val = vEl.textContent || '';
          if (t === 's') val = shared[parseInt(val, 10)] ?? '';
        }
        cells.push(val);
      });
      if (cells.some(x => String(x).trim() !== '')) rows.push(cells);
    });
    return rows;
  }

  /** 统一入口：按扩展名分派。 */
  async function parseBundleFile(file) {
    const nm = (file.name || '').toLowerCase();
    if (/\.xlsx$/.test(nm)) {
      return _rowsToItems(await _parseXlsx(await file.arrayBuffer()));
    }
    const text = await file.text();
    if (/\.json$/.test(nm)) return parseBundleJson(text);
    if (/\.(csv|tsv|txt)$/.test(nm)) return parseBundleText(text);
    // 扩展名不认识时按内容猜
    const t = text.trim();
    if (t.startsWith('[') || t.startsWith('{')) return parseBundleJson(t);
    return parseBundleText(t);
  }

  function bundleImportForm() {
    const state = { tab: 'file', items: [], err: '', busy: false, seq: 0 };
    let pickedFile = null;

    modal('导入卡卷', `
      <div class="row" id="biTabs" style="margin-bottom:10px">
        <button class="btn btn-sm" data-tab="file">📄 表格文件</button>
        <button class="btn btn-sm" data-tab="json">🔗 JSON / API</button>
        <button class="btn btn-sm" data-tab="text">📝 文本粘贴</button>
      </div>

      <div id="biPaneFile">
        <div class="field">
          <label>选择表格文件（.xlsx / .csv / .tsv / .txt / .json）</label>
          <input type="file" class="input" id="bi_file" accept=".xlsx,.csv,.tsv,.txt,.json">
          <span class="tip">两列：第一列<b>卡卷名称</b>，第二列<b>内容</b>。有表头会自动跳过；名称留空会自动编号。</span>
        </div>
      </div>

      <div id="biPaneJson" class="hidden">
        <div class="field">
          <label>粘贴 JSON（或接口返回的原文）</label>
          <textarea class="input" id="bi_json" style="min-height:110px"
            placeholder='[{"name":"奇幻套装","content":"卡密或网盘链接…"}]'></textarea>
          <span class="tip">支持 <code>[{name,content}]</code>、<code>{"data":[…]}</code>、<code>{"卡卷名":"内容"}</code>；也认 名称/内容、title/body 等常见字段名。</span>
        </div>
      </div>

      <div id="biPaneText" class="hidden">
        <div class="field">
          <label>粘贴文本</label>
          <textarea class="input" id="bi_text" style="min-height:130px"
            placeholder="=== 奇幻套装 ===&#10;在的呢~ 感谢咨询…&#10;📁【网盘位置】&#10;链接：https://pan.quark.cn/s/xxxx&#10;提取码：7Rdf&#10;&#10;=== 另一个卡卷 ===&#10;CODE-AAAA-1111"></textarea>
          <span class="tip">推荐用 <b>=== 卡卷名称 ===</b>（或 ## 名称）分隔，块内<b>多行内容</b>原样保留；
            也支持 Tab / 逗号 / 竖线的两列表格（每行一条，内容不能跨行）。</span>
        </div>
      </div>

      <div id="biPreview"></div>
    `, () => {
      if (state.busy) throw new Error('正在解析文件，请稍候');
      if (!state.items.length) throw new Error(state.err || '没有解析出有效数据');
      const payload = state.items.map(x => ({ name: x.name, content: x.content }));
      api('bundle_import', { items: JSON.stringify(payload) })
        .then(r => {
          $('#modalMask').classList.remove('show');
          toast(`已导入 ${r.imported} 个卡卷`, 'ok');
          pages.products();
        })
        .catch(e => toast(e.message, 'err'));
    }, '导入');

    const setTab = t => {
      state.tab = t;
      $$('#biTabs button').forEach(b => {
        b.classList.toggle('btn-primary', b.dataset.tab === t);
      });
      $('#biPaneFile').classList.toggle('hidden', t !== 'file');
      $('#biPaneJson').classList.toggle('hidden', t !== 'json');
      $('#biPaneText').classList.toggle('hidden', t !== 'text');
      refresh();
    };

    const render = () => {
      const box = $('#biPreview');
      if (!box) return;
      if (state.err) {
        box.innerHTML = `<div class="muted" style="color:var(--err);font-size:12px;margin-top:8px">解析失败：${esc(state.err)}</div>`;
        return;
      }
      if (!state.items.length) { box.innerHTML = ''; return; }
      const preview = state.items.slice(0, 5).map(it =>
        `<tr><td>${esc(it.name)}</td><td class="mono">${esc(String(it.content).replace(/\n/g, ' ⏎ ').slice(0, 70))}</td></tr>`).join('');
      box.innerHTML =
        `<div class="muted" style="font-size:12px;margin:10px 0 4px">已解析 ${state.items.length} 条${state.items.length > 5 ? '（预览前 5 条）' : ''}：</div>
         <table><thead><tr><th style="width:30%">卡卷名称</th><th>内容</th></tr></thead><tbody>${preview}</tbody></table>`;
    };

    async function refresh() {
      const my = ++state.seq;
      state.err = '';
      let items = [];
      try {
        if (state.tab === 'file') {
          if (pickedFile) {
            state.busy = true;
            items = await parseBundleFile(pickedFile);
          }
        } else if (state.tab === 'json') {
          const t = ($('#bi_json') || {}).value || '';
          if (t.trim()) items = parseBundleJson(t);
        } else {
          const t = ($('#bi_text') || {}).value || '';
          if (t.trim()) items = parseBundleText(t);
        }
      } catch (e) {
        state.err = e.message || String(e);
      } finally {
        state.busy = false;
      }
      if (my !== state.seq) return;   // 竞态保护：慢的旧解析结果不能覆盖新的
      state.items = items || [];
      render();
    }

    $('#biTabs').addEventListener('click', e => {
      const b = e.target.closest('button[data-tab]');
      if (b) setTab(b.dataset.tab);
    });
    $('#bi_file').addEventListener('change', e => {
      pickedFile = (e.target.files || [])[0] || null;
      refresh();
    });
    $('#bi_json').addEventListener('input', refresh);
    $('#bi_text').addEventListener('input', refresh);

    setTab('file');
  }
  /** 导出卡卷为 CSV（前端生成，BOM 保证 Excel 中文不乱码）；
   *  ids 为空 = 全部。 */
  function exportBundles(ids) {
    api('bundle_export', { ids: (ids || []).join(',') }).then(d => {
      const rows = d.rows || [];
      if (!rows.length) { toast('没有可导出的卡卷', 'err'); return; }
      const cell = s => '"' + String(s == null ? '' : s).replace(/"/g, '""') + '"';
      const csv = '\uFEFF卡券名称,内容\r\n'
        + rows.map(r => cell(r.name) + ',' + cell(r.content)).join('\r\n');
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = '卡卷_' + new Date().toISOString().slice(0, 10) + '.csv';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
      toast(`已导出 ${rows.length} 个卡卷`, 'ok');
    }).catch(e => toast(e.message, 'err'));
  }
  function msgView(buyer) {
    api('messages', { buyer }).then(d => {
      const rows = (d.rows || []).map(m => {
        const dir = m.direction === 'in'
          ? '<span class="tag">买家→我</span>' : '<span class="tag">我→买家</span>';
        const od = m.is_order ? ' <span class="tag gray">订单</span>' : '';
        return `<tr><td class="nowrap muted">${esc(m.created_at)}</td><td class="nowrap">${dir}${od}</td><td>${esc(m.content)}</td></tr>`;
      }).join('') || '<tr><td colspan="3" class="empty">该买家暂无消息记录</td></tr>';
      modal('与「' + buyer + '」的对话', `<div class="panel" style="box-shadow:none;padding:0"><table><thead><tr><th>时间</th><th>方向</th><th>内容</th></tr></thead><tbody>${rows}</tbody></table></div>`, () => $('#modalMask').classList.remove('show'), '关闭');
    }).catch(e => toast(e.message, 'err'));
  }

  // ---------- 导航 / 引擎按钮 ----------
  function go(page) {
    current = page;
    $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.page === page));
    const titles = { dashboard: '仪表盘', rules: '回复规则', shopitems: '商品列表', products: '卡卷管理', conversations: '会话 / 消息', deliveries: '订单管理', activity: '运行日志', settings: '设置', tunnel: '内网穿透', batchpublish: '批量上传' };
    $('#pageTitle').textContent = titles[page] || '';
    (pages[page] || pages.dashboard)();
    closeNav();          // 窄屏点完菜单自动收起抽屉
  }
  $$('#nav a').forEach(a => a.onclick = () => go(a.dataset.page));

  // ---------- 窄屏抽屉导航（手机 / Pad）----------
  // 桌面端侧栏常驻，这些逻辑不生效（CSS 里 .nav-toggle 默认 display:none）
  const isNarrow = () => window.matchMedia('(max-width: 820px)').matches;
  function openNav() { $('#app').classList.add('nav-open'); }
  function closeNav() { $('#app').classList.remove('nav-open'); }
  if ($('#navToggle')) $('#navToggle').onclick = () => {
    $('#app').classList.contains('nav-open') ? closeNav() : openNav();
  };
  if ($('#navMask')) $('#navMask').onclick = closeNav;
  // Esc 关闭（接了外接键盘的 Pad 也顺手）
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeNav(); });
  // 从窄屏拖宽到桌面时清掉状态，否则遮罩会留在页面上挡住点击
  window.addEventListener('resize', () => { if (!isNarrow()) closeNav(); });
  $('#btnRefresh').onclick = () => (pages[current] || pages.dashboard)();
  $('#btnStart').onclick = () => { $('#btnStart').disabled = true; api('engine_start').then(d => { toast(d.state === 'running' ? '引擎已启动' : '已尝试启动，请稍候', d.state === 'running' ? 'ok' : ''); refreshEngine(); }).catch(e => toast(e.message, 'err')).finally(() => $('#btnStart').disabled = false); };
  $('#btnStop').onclick = () => api('engine_stop').then(() => { toast('已发送停止指令'); refreshEngine(); }).catch(e => toast(e.message, 'err'));
  $('#btnScan').onclick = () => api('engine_scan').then(() => toast('已触发立即扫描', 'ok')).catch(e => toast(e.message, 'err'));

  // ---------- 卖家客户端（闲鱼软件）后台启动 / 状态 ----------
  function refreshApp() {
    api('app_status').then(d => {
      const on = !!d.running;
      const setDot = (sel, v) => { const e = $(sel); if (e) e.className = 'dot ' + (v ? 'run' : ''); };
      const setTxt = (sel, t) => { const e = $(sel); if (e) e.textContent = t; };
      const dis = (sel, v) => { const e = $(sel); if (e) e.disabled = v; };
      setDot('#appDot', on); setTxt('#appText', on ? '客户端已运行' : '客户端未运行');
      setDot('#dashAppDot', on); setTxt('#dashAppText', on ? '运行中 (9222)' : '未运行');
      dis('#btnAppStart', on); dis('#btnAppStop', !on);
      dis('#dashAppStart', on); dis('#dashAppStop', !on);
    }).catch(() => {});
  }
  function appStart() {
    const b = $('#btnAppStart'); if (b) b.disabled = true;
    api('app_start').then(d => toast(d.msg || '已发送启动指令', 'ok')).catch(e => toast(e.message, 'err')).finally(refreshApp);
  }
  function appStop() {
    api('app_stop').then(d => toast(d.msg || '已停止')).catch(e => toast(e.message, 'err')).finally(refreshApp);
  }
  const sb = $('#btnAppStart'); if (sb) sb.onclick = appStart;
  const sb2 = $('#btnAppStop'); if (sb2) sb2.onclick = appStop;
  $('#content').addEventListener('click', e => {
    if (e.target.closest('#dashAppStart')) { appStart(); return; }
    if (e.target.closest('#dashAppStop')) { appStop(); return; }
  });

  // ---------- 启动 ----------
  refreshEngine();
  refreshDryBadge();
  go('dashboard');
  refreshApp();
  setInterval(refreshApp, 3000);
  setInterval(() => { refreshEngine(); if (current === 'dashboard' || current === 'activity' || current === 'deliveries') (pages[current])(); }, 8000);
})();
