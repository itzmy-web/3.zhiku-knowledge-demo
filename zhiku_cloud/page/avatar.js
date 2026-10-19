/* ============================================================
 * 汇思智藏 · AI 数字人「柜柜」——魔珐星云 3D 形象版
 * 自注入组件：任何页面引入 <script src="/avatar.js"></script> 即可
 * 形象 = 你在魔珐星云控制台配置的 AI 形象（由 App ID 绑定）
 * 能力：页面打开自动加载 / 可拖拽 / 可收起 / 机器人回复自动开口播报
 * 注意：SDK 要求 localhost 或 HTTPS 访问（不能用 127.0.0.1 + IP）
 * ============================================================ */
(function () {
  /* 关键规则 1：iframe 内不注入 —— 父页面（工作台）已有实例，避免同凭证双连接互踢 */
  if (window.self !== window.top) return;
  if (document.getElementById('zhigu-avatar')) return;

  /* ---------- 跨标签页会话锁：同一时刻只允许一个页面连接数字人 ---------- */
  var LOCK_KEY = 'dh_lock_v1';
  var myId = Math.random().toString(36).slice(2);
  function readLock() {
    try { return JSON.parse(localStorage.getItem(LOCK_KEY)); } catch (e) { return null; }
  }
  function lockAlive(lock) { return lock && (Date.now() - lock.ts < 12000) && lock.id !== myId; }
  function renewLock() {
    try { localStorage.setItem(LOCK_KEY, JSON.stringify({ ts: Date.now(), id: myId, page: location.pathname })); } catch (e) {}
  }
  /* 收到别的页面抢锁 → 自己断开，避免平台互踢 */
  window.addEventListener('storage', function (e) {
    if (e.key !== LOCK_KEY || !inited) return;
    var lock = readLock();
    if (lockAlive(lock)) {
      disconnect();
      setStatus('数字人已在其他页面运行（关闭那边即可切回这里）', 'error');
    }
  });

  var path = location.pathname || '';
  var pageName = path.indexOf('login') > -1 ? 'login'
               : path.indexOf('chat') > -1 ? 'chat'
               : path.indexOf('import') > -1 ? 'import' : 'home';
  var WELCOME = {
    login: '欢迎来到汇思智藏，登录后我带你翻遍全部资料。',
    chat:  '我在呢，想查什么直接问，答案都出自你的文档。',
    import:'把文档交给我，导入完我就全部记住了。',
    home:  '你好呀，我是汇思智藏的数字人柜柜，左边传文档，右边开始提问。'
  };

  /* ---------- 样式 ---------- */
  var style = document.createElement('style');
  style.textContent = [
    '#zhigu-avatar{position:fixed;z-index:99999;right:20px;bottom:20px;',
    '  font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;user-select:none}',
    '#zhigu-avatar.dragging{transition:none}',
    '#zhigu-avatar .za-card{width:192px;background:rgba(24,24,30,.42);border:1px solid rgba(255,255,255,.2);',
    '  border-radius:16px;box-shadow:0 18px 44px rgba(0,0,0,.35);overflow:hidden;',
    '  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);transition:box-shadow .3s}',
    '#zhigu-avatar .za-head{display:flex;align-items:center;gap:7px;padding:7px 10px;cursor:grab;touch-action:none}',
    '#zhigu-avatar.dragging .za-head{cursor:grabbing}',
    '#zhigu-avatar .za-dot{width:8px;height:8px;border-radius:50%;background:#ff9f0a;flex:none;',
    '  box-shadow:0 0 8px #ff9f0a;transition:background .3s,box-shadow .3s}',
    '#zhigu-avatar.ready .za-dot{background:#30d158;box-shadow:0 0 8px #30d158}',
    '#zhigu-avatar.error .za-dot{background:#ff453a;box-shadow:0 0 8px #ff453a}',
    '#zhigu-avatar .za-title{font-size:11.5px;font-weight:700;color:#f5f5f7;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '#zhigu-avatar .za-min{cursor:pointer;color:#8e8e93;font-size:12px;width:22px;height:22px;border-radius:6px;',
    '  display:grid;place-items:center;line-height:1;flex:none}',
    '#zhigu-avatar .za-min:hover{color:#fff;background:rgba(255,255,255,.12)}',
    '#zhigu-avatar .za-stage{position:relative;width:180px;height:290px;background:transparent;margin:0 auto 8px;',
    '  border-radius:12px;overflow:hidden}',
    '#zhigu-avatar .za-stage .za-sdkbox{position:absolute;inset:0}',
    '#zhigu-avatar .za-mask{position:absolute;inset:0;display:grid;place-items:center;text-align:center;',
    '  color:#98989f;font-size:11px;line-height:1.8;padding:0 18px;pointer-events:none;transition:opacity .4s}',
    '#zhigu-avatar .za-bar{height:4px;background:rgba(255,255,255,.12);margin:0 12px;border-radius:2px;overflow:hidden}',
    '#zhigu-avatar .za-fill{height:100%;width:0%;border-radius:2px;',
    '  background:linear-gradient(90deg,#0a84ff,#5ac8fa);transition:width .35s}',
    '#zhigu-avatar .za-foot{display:flex;align-items:center;justify-content:space-between;padding:5px 10px 8px}',
    '#zhigu-avatar .za-status{font-size:10px;color:#8e8e93;line-height:1.5}',
    '#zhigu-avatar .za-retry{display:none;cursor:pointer;border:none;border-radius:8px;padding:4px 10px;',
    '  background:#0a84ff;color:#fff;font-size:10.5px;font-weight:600;flex:none}',
    '#zhigu-avatar.error .za-retry{display:block}',
    /* 收起态 FAB */
    '#zhigu-avatar.min .za-card{display:none}',
    '#zhigu-avatar .za-fab{display:none;width:56px;height:56px;border-radius:50%;cursor:pointer;place-items:center;',
    '  font-size:26px;background:linear-gradient(135deg,#1a2a4a,#0a84ff);border:2px solid rgba(255,255,255,.25);',
    '  box-shadow:0 12px 30px rgba(10,132,255,.45)}',
        /* 覆盖魔珐星云字幕层：字体缩小、黑条调淡（字幕是 SDK 叠加的 DOM，样式带 translateX(-50%) 特征） */
    '#zhigu-avatar #dh-sdk-box div[style*="translateX(-50%)"]{font-size:10px !important;line-height:1.4 !important;padding:4px 8px !important;background:rgba(0,0,0,.4) !important;}',
    '#zhigu-avatar #dh-sdk-box div[style*="translateX(-50%)"] span,#zhigu-avatar #dh-sdk-box div[style*="translateX(-50%)"] p{font-size:10px !important;line-height:1.4 !important;}',
'#zhigu-avatar.min .za-fab{display:grid}'
  ].join('');
  document.head.appendChild(style);

  /* ---------- DOM ---------- */
  var root = document.createElement('div');
  root.id = 'zhigu-avatar';
  root.innerHTML =
    '<div class="za-card">' +
      '<div class="za-head">' +
        '<span class="za-dot"></span>' +
        '<span class="za-title">柜柜 · AI 数字人</span>' +
        '<span class="za-min" title="收起">–</span>' +
      '</div>' +
      '<div class="za-stage">' +
        '<div class="za-sdkbox" id="dh-sdk-box" style="position:absolute;left:0;top:0;width:100%;height:100%"></div>' +
        '<div class="za-mask">正在唤醒数字人…</div>' +
      '</div>' +
      '<div class="za-bar"><div class="za-fill"></div></div>' +
      '<div class="za-foot">' +
        '<span class="za-status">初始化…</span>' +
        '<button class="za-retry">重试</button>' +
      '</div>' +
    '</div>' +
    '<div class="za-fab" title="召唤数字人">🤖</div>';
  document.body.appendChild(root);

  var head = root.querySelector('.za-head');
  var mask = root.querySelector('.za-mask');
  var fill = root.querySelector('.za-fill');
  var statusEl = root.querySelector('.za-status');
  var retryBtn = root.querySelector('.za-retry');

  function setStatus(text, mode) {
    statusEl.textContent = text;
    root.classList.remove('ready', 'error');
    if (mode) root.classList.add(mode);
  }

  /* ---------- SDK 加载 + 自动连接 ---------- */
  var LiteSDK = null, sdkLoaded = false, inited = false, connecting = false;
  var SDK_URL = 'https://media.xingyun3d.com/xingyun3d/general/litesdk/xmovAvatar@latest.js';

  /* SDK 是 UMD 包：全局类注册发生在脚本主体执行完之后。
     如果页面环境干扰（全局 define/exports 存在时走 AMD/CommonJS 分支）或执行中途报错，
     window.XmovAvatar 就不会出现 —— 因此先轮询等待，超时后用「受控执行」强制接管导出。 */
  function waitGlobal(cb, onFail) {
    var n = 0;
    var timer = setInterval(function () {
      if (window.XmovAvatar) { clearInterval(timer); cb(); }
      else if (++n > 40) { clearInterval(timer); onFail(); }
    }, 200);
  }

  /* 兜底：fetch 源码后在一个受控伪 CommonJS 环境里执行，强制拿到工厂返回的类 */
  function forceLoadSdk(cb, onFail) {
    fetch(SDK_URL)
      .then(function (r) { return r.text(); })
      .then(function (code) {
        var fakeModule = { exports: {} };
        new Function('module', 'exports', 'define', 'return', code)(fakeModule, fakeModule.exports, undefined);
        var Klass = fakeModule.exports && fakeModule.exports.prototype ? fakeModule.exports : window.XmovAvatar;
        if (Klass) { window.XmovAvatar = Klass; cb(); }
        else onFail('SDK 代码执行后未产出类');
      })
      .catch(function (e) { onFail(e.message || 'SDK 源码拉取失败'); });
  }

  function loadSdk(cb) {
    if (sdkLoaded) return cb();
    var s = document.createElement('script');
    s.src = SDK_URL;
    s.onload = function () {
      /* 等待 UMD 完成全局注册；超时则强制接管 */
      waitGlobal(cb, function () {
        forceLoadSdk(cb, function (why) {
          setStatus('SDK 加载异常：' + (why || '全局类未注册'), 'error');
          connecting = false;
        });
      });
    };
    s.onerror = function () { setStatus('SDK 脚本加载失败，请检查网络后重试', 'error'); connecting = false; };
    document.head.appendChild(s);
  }

  function connect() {
    if (connecting || inited) return;
    /* 关键规则 2：会话锁 —— 别的标签页正连着就不抢 */
    var lock = readLock();
    if (lockAlive(lock)) {
      setStatus('数字人已在其他页面运行，先关闭那边的标签页', 'error');
      return;
    }
    renewLock();
    connecting = true;
    fill.style.width = '0%';
    mask.style.opacity = '1';
    setStatus('正在加载 SDK…');
    collectDiag('connect-start');
    /* 关键：先加载 SDK（含 UMD 兜底），加载完成后再走凭证+初始化流程 */
    loadSdk(function () {
      collectDiag('sdk-loaded');
      fetchCredentialAndInit();
    });
  }

  function fetchCredentialAndInit() {
    setStatus('正在获取凭证…');
    fetch('/api/digital-human/credentials')
      .then(function (r) { return r.json(); })
      .then(function (cred) {
        if (!cred.configured || !cred.app_id || !cred.secret_configured) {
          throw new Error('后端未配置凭证：请在 .env 填 DIGITAL_HUMAN_APP_ID / APP_SECRET');
        }
        if (location.protocol === 'https:' || /localhost|127\.0\.0\.1/.test(location.hostname)) {
          /* 环境OK */
        } else {
          throw new Error('请用 http://localhost:8000 访问本页（SDK 不支持 IP + HTTP）');
        }
        collectDiag('cred-ok');
        setStatus('正在下载 3D 模型（首次较慢）…');
        var AvatarClass = window.XmovAvatar;
        LiteSDK = new AvatarClass({
          containerId: '#dh-sdk-box',
          appId: cred.app_id,
          appSecret: '', // A5：密钥不下发，由服务端代理注入
          gatewayServer: location.origin + '/api/digital-human/proxy',
          hardwareAcceleration: 'prefer-hardware',
          enableLogger: false,
          onMessage: function (m) {
            try { diagMsgs.push(m); } catch (e) {}
            if (m && m.code && m.code !== 50001 && m.code !== 50002) {
              var tip = { 10001: '凭证无效', 10002: '授权已过期', 10003: '并发超限（关掉其他页面的数字人再试）',
                          10004: '会话已断开，可点重试', 10005: '并发超限（关掉其他页面的数字人再试）' }[m.code];
              setStatus('SDK 错误 ' + m.code + (tip ? ' · ' + tip : ''), 'error');
              connecting = false;
              collectDiag('msg-' + m.code);
            }
          },
          onVoiceStateChange: function (s) {
            if (s === 'start') setStatus('正在说话…', 'ready');
            else if (s === 'end') setStatus('就绪 · 提问后我会开口回答', 'ready');
          }
        });
        collectDiag('init-start');
        LiteSDK.init({
          initModel: 'normal',
          onDownloadProgress: function (p) {
            fill.style.width = p + '%';
            if (p >= 100 && !inited) {
              inited = true; connecting = false;
              mask.style.opacity = '0';
              renewLock();
              setInterval(function () { if (inited) renewLock(); }, 3000);   // 持有心跳锁
              setStatus('就绪 · 提问后我会开口回答', 'ready');
              /* 1.5 秒后自检画面：video 流没在播就尝试恢复；仍不行提示用户 */
              setTimeout(function () {
                var ok = ensureVideoPlaying();
                if (!ok) setStatus('画面未激活：请关闭本站其他标签页，然后点击画面唤醒', 'ready');
                else setStatus('就绪 · 点一下画面开启声音，提问后我会回答', 'ready');
              }, 1500);
              setTimeout(function () { collectDiag('ready+3s'); }, 3000);
              setTimeout(function () { DHSpeak(WELCOME[pageName] || WELCOME.home); }, 2500);
            }
          }
        });
      })
      .catch(function (e) {
        setStatus(e.message || '连接失败，请点重试', 'error');
        connecting = false;
        collectDiag('connect-error', e.message || String(e));
      });
  }

  function disconnect() {
    try { if (LiteSDK) LiteSDK.destroy(); } catch (e) {}
    LiteSDK = null; inited = false; connecting = false;
    fill.style.width = '0%';
    mask.style.opacity = '1';
    setStatus('未连接');
    try {
      var lock = readLock();
      if (lock && lock.id === myId) localStorage.removeItem(LOCK_KEY);
    } catch (e) {}
  }

  /* 画面自检：就绪后检查 SDK 容器里的 video 流是否真的在播，没播就尝试恢复 */
  function ensureVideoPlaying() {
    try {
      var box = document.getElementById('dh-sdk-box');
      if (!box) return false;
      var vids = box.querySelectorAll('video');
      var anyPlaying = false;
      for (var i = 0; i < vids.length; i++) {
        var v = vids[i];
        if (!v.paused && !v.ended && v.readyState > 2) { anyPlaying = true; continue; }
        var p = v.play();
        if (p && p.catch) p.catch(function () {});
        if (!v.paused) anyPlaying = true;
      }
      /* canvas 尺寸为 0 的兜底 */
      var cvs = box.querySelectorAll('canvas');
      for (var j = 0; j < cvs.length; j++) {
        var c = cvs[j];
        if (!c.style.width || c.style.width === '0px') { c.style.width = '100%'; c.style.height = '100%'; }
      }
      return anyPlaying || vids.length === 0;
    } catch (e) { return true; }
  }

  /* 点画面 = 唤醒（用户手势可解锁自动播放限制）+ 播报欢迎词（手势后声音必然可出） */
  var welcomed = false;
  function welcome() {
    if (welcomed || !inited) return;
    welcomed = true;
    DHSpeak(WELCOME[pageName] || WELCOME.home);
  }
  /* 点页面任意位置都能解锁声音（浏览器要求首次声音必须有用户手势） */
  document.addEventListener('pointerdown', function () { setTimeout(welcome, 60); });
  root.querySelector('.za-stage').addEventListener('click', function () {
    if (!inited) return;
    ensureVideoPlaying();
    try { LiteSDK.interactiveidle(); } catch (e) {}   // 点击即打断当前播报
    setStatus('就绪 · 提问后我会开口回答', 'ready');
    welcome();
  });

  /* 供聊天页钩子调用：数字人朗读机器人回复 */
  window.DHSpeak = function (text) {
    if (!inited || !LiteSDK || !text) return;
    try {
      LiteSDK.interactiveidle();                    // 官方建议：先切状态保证口型对齐
      setTimeout(function () { LiteSDK.speak(String(text), true, true); }, 120);
    } catch (e) {}
  };

  /* 供外部打断：用户发新问题/点击形象时，立刻停下当前播报 */
  window.DHStop = function () {
    if (!inited || !LiteSDK) return;
    try { LiteSDK.interactiveidle(); } catch (e) {}
  };

  /* ---------- 远程诊断：把 SDK 容器内实况上报后端，落盘 _dh_diag.json ---------- */
  var diagMsgs = [];
  function collectDiag(phase, extra) {
    try {
      var box = document.getElementById('dh-sdk-box');
      var info = {
        phase: phase,
        extra: extra || null,
        page: location.href,
        ua: navigator.userAgent,
        sdkMessages: diagMsgs,
        elements: []
      };
      if (box) {
        var nodes = box.querySelectorAll('*');
        for (var i = 0; i < nodes.length && i < 40; i++) {
          var n = nodes[i];
          var item = { tag: n.tagName, cls: (n.className || '').toString().slice(0, 60), w: n.clientWidth, h: n.clientHeight };
          if (n.tagName === 'VIDEO') {
            item.paused = n.paused; item.readyState = n.readyState;
            item.videoWidth = n.videoWidth; item.videoHeight = n.videoHeight;
            item.hasStream = !!n.srcObject; item.muted = n.muted; item.currentTime = Math.round(n.currentTime * 10) / 10;
            item.autoplay = n.autoplay;
          }
          if (n.tagName === 'CANVAS') { item.canvasW = n.width; item.canvasH = n.height; }
          info.elements.push(item);
        }
      } else {
        info.error = 'container #dh-sdk-box not found';
      }
      fetch('/api/digital-human/report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(info)
      });
    } catch (e) {}
  }

  /* 重试 = 强制抢锁：把会话从旧页面抢过来（旧页面通过 storage 事件自动让位断开） */
  retryBtn.onclick = function () {
    renewLock();
    setStatus('正在接管数字人…');
    setTimeout(function () { connect(); }, 500);
  };

  /* ---------- 拖拽（拖顶部状态栏） vs 收起 ---------- */
  var ptrStart = null, moved = false;
  head.addEventListener('pointerdown', function (e) {
    if (e.target === root.querySelector('.za-min')) return;
    ptrStart = { x: e.clientX, y: e.clientY, l: root.offsetLeft, b: window.innerHeight - root.offsetTop - root.offsetHeight };
    moved = false;
    head.setPointerCapture(e.pointerId);
  });
  head.addEventListener('pointermove', function (e) {
    if (!ptrStart) return;
    var dx = e.clientX - ptrStart.x, dy = e.clientY - ptrStart.y;
    if (Math.abs(dx) + Math.abs(dy) > 8) moved = true;
    if (moved) {
      root.classList.add('dragging');
      var l = Math.min(Math.max(ptrStart.l + dx, 8), window.innerWidth - root.offsetWidth - 8);
      var b = Math.min(Math.max(ptrStart.b - dy, 8), window.innerHeight - root.offsetHeight - 8);
      root.style.left = l + 'px'; root.style.right = 'auto'; root.style.bottom = b + 'px';
    }
  });
  head.addEventListener('pointerup', function () {
    root.classList.remove('dragging');
    ptrStart = null; moved = false;
  });

  root.querySelector('.za-min').onclick = function (e) {
    e.stopPropagation();
    root.classList.add('min');
  };
  root.querySelector('.za-fab').onclick = function () {
    root.classList.remove('min');
    if (!inited && !connecting) connect();
  };

  /* 页面卸载前断开并释放会话锁，避免触发并发限制（错误码 10005） */
  window.addEventListener('beforeunload', function () {
    try { if (LiteSDK && inited) LiteSDK.destroy(); } catch (e) {}
    try {
      var lock = readLock();
      if (lock && lock.id === myId) localStorage.removeItem(LOCK_KEY);
    } catch (e) {}
  });

  /* ---------- 自动启动：页面就绪 1 秒后自动连接 ---------- */
  if (document.readyState === 'complete') setTimeout(connect, 1000);
  else window.addEventListener('load', function () { setTimeout(connect, 1000); });
})();
