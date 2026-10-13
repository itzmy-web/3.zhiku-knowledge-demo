/* ============================================================
 * 掌柜智库 · 数字人助手「柜柜」v2 猫咪管家版
 * 自注入组件：任何页面引入 <script src="/assistant.js"></script> 即可
 * 能力：巡逻移动 / 可拖拽 / 眨眼 / 气泡对话（打字机）/ 点击开口说话（男声）
 * ============================================================ */
(function () {
  if (document.getElementById('zhigu-assistant')) return;

  var path = location.pathname || '';
  var pageName = path.indexOf('login') > -1 ? 'login'
               : path.indexOf('chat') > -1 ? 'chat'
               : path.indexOf('import') > -1 ? 'import' : 'home';

  var GREETINGS = {
    login: ['喵～欢迎来到掌柜智库！本猫看仓库的，登录后随你翻资料～', '📱 输入手机号，或者点「游客体验」，本猫带路'],
    chat: ['喵？想查什么？本猫先去仓库帮你翻手册～', '问题里带上型号，本猫找得更快喵', '答案都出自你的文档，本猫亲口核对过的喵'],
    import: ['把文档扔给本猫！PDF Word MD 来者不拒喵', '导入完本猫就把它们全部背下来喵'],
    home: ['喵～本猫是柜柜，掌柜智库的猫管家！', '左边上传文档，右边开始提问，本猫全程陪同喵']
  };
  var tips = GREETINGS[pageName] || GREETINGS.home;
  var tipIdx = 0;

  /* ---------- 样式 ---------- */
  var style = document.createElement('style');
  style.textContent = [
    '#zhigu-assistant{position:fixed;z-index:99999;right:24px;bottom:24px;',
    '  font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;user-select:none;',
    '  transition:left 1.4s cubic-bezier(.45,.05,.35,1),bottom 1.4s cubic-bezier(.45,.05,.35,1)}',
    '#zhigu-assistant.dragging{transition:none}',
    '#zhigu-assistant .za-wrap{position:relative;display:flex;flex-direction:column;align-items:flex-end;gap:10px}',
    /* 气泡 */
    '#zhigu-assistant .za-bubble{max-width:250px;background:rgba(28,28,32,.94);color:#f5f5f7;',
    '  border:1px solid rgba(255,255,255,.15);border-radius:16px 16px 4px 16px;padding:12px 15px;',
    '  font-size:13px;line-height:1.7;box-shadow:0 16px 44px rgba(0,0,0,.4);',
    '  backdrop-filter:blur(16px);opacity:0;transform:translateY(8px) scale(.95);',
    '  transition:all .35s cubic-bezier(.34,1.56,.64,1);pointer-events:none;position:relative}',
    '#zhigu-assistant .za-bubble.show{opacity:1;transform:none;pointer-events:auto}',
    '#zhigu-assistant .za-bubble .za-tail{position:absolute;bottom:-7px;right:26px;width:12px;height:12px;',
    '  background:rgba(28,28,32,.94);border-right:1px solid rgba(255,255,255,.15);border-bottom:1px solid rgba(255,255,255,.15);',
    '  transform:rotate(45deg)}',
    '#zhigu-assistant .za-bubble .za-name{font-size:11px;font-weight:700;margin-bottom:4px;',
    '  background:linear-gradient(90deg,#0a84ff,#bf5af2);-webkit-background-clip:text;background-clip:text;color:transparent}',
    '#zhigu-assistant .za-bubble .za-close{position:absolute;top:6px;right:9px;cursor:pointer;color:#6e6e73;font-size:12px}',
    '#zhigu-assistant .za-bubble .za-close:hover{color:#f5f5f7}',
    /* 角色本体 */
    '#zhigu-assistant .za-avatar{width:100px;height:100px;cursor:grab;position:relative;',
    '  animation:zaFloat 3.6s ease-in-out infinite;filter:drop-shadow(0 14px 28px rgba(0,0,0,.4));',
    '  transition:transform .2s;touch-action:none}',
    '#zhigu-assistant .za-avatar:hover{transform:scale(1.06)}',
    '#zhigu-assistant.dragging .za-avatar{cursor:grabbing;animation:none;transform:scale(1.12)}',
    '@keyframes zaFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-9px)}}',
    '#zhigu-assistant .za-ring{position:absolute;inset:-6px;border-radius:50%;',
    '  background:conic-gradient(#30d158,#0a84ff,#5e5ce6,#bf5af2,#30d158);',
    '  animation:zaSpin 6s linear infinite;opacity:.85;z-index:0}',
    '@keyframes zaSpin{to{transform:rotate(360deg)}}',
    '#zhigu-assistant .za-body{position:absolute;inset:0;border-radius:50%;background:#0b0b10;z-index:1;',
    '  display:grid;place-items:center;border:1px solid rgba(255,255,255,.15)}',
    '#zhigu-assistant .za-body svg{width:76px;height:76px}',
    '#zhigu-assistant .za-eye{animation:zaBlink 3.8s infinite;transform-origin:center}',
    '@keyframes zaBlink{0%,90%,100%{transform:scaleY(1)}94%{transform:scaleY(.06)}}',
    /* 尾巴摆动 */
    '#zhigu-assistant .za-tailcat{transform-origin:82px 78px;animation:zaTail 2.8s ease-in-out infinite alternate}',
    '@keyframes zaTail{from{transform:rotate(-14deg)}to{transform:rotate(16deg)}}',
    /* 说话动画 */
    '#zhigu-assistant.talking .za-mouth{animation:zaTalk .32s ease-in-out infinite alternate;transform-origin:center}',
    '@keyframes zaTalk{from{transform:scaleY(.35)}to{transform:scaleY(1.2)}}',
    '#zhigu-assistant.talking .za-body{animation:zaNod .65s ease-in-out infinite}',
    '@keyframes zaNod{0%,100%{transform:translateY(0)}50%{transform:translateY(2px)}}',
    '#zhigu-assistant .za-wave{position:absolute;inset:-6px;border-radius:50%;border:2px solid #30d158;',
    '  opacity:0;z-index:0}',
    '#zhigu-assistant.talking .za-wave{animation:zaWave 1.4s ease-out infinite}',
    '@keyframes zaWave{0%{opacity:.7;transform:scale(1)}100%{opacity:0;transform:scale(1.55)}}',
    /* 移动时的爪印小尾巴 */
    '#zhigu-assistant .za-hide{position:absolute;top:-6px;left:-6px;width:22px;height:22px;border-radius:50%;',
    '  border:1px solid rgba(255,255,255,.2);background:rgba(40,40,46,.95);color:#a1a1a6;font-size:11px;',
    '  cursor:pointer;display:none;place-items:center;z-index:3;line-height:1}',
    '#zhigu-assistant:hover .za-hide{display:grid}',
    '#zhigu-assistant .za-hide:hover{color:#fff;background:rgba(255,59,48,.85);border-color:#ff375f}',
    '#zhigu-assistant.min .za-wrap>*{display:none!important}',
    '#zhigu-assistant.min .za-fab{display:grid!important}',
    '#zhigu-assistant.min{transition:none}',
    '#zhigu-assistant .za-fab{display:none;width:52px;height:52px;border-radius:50%;cursor:pointer;',
    '  place-items:center;font-size:24px;background:linear-gradient(135deg,#30d158,#0a84ff);',
    '  box-shadow:0 10px 26px rgba(48,209,88,.4);border:2px solid rgba(255,255,255,.25)}'
  ].join('');
  document.head.appendChild(style);

  /* ---------- DOM ---------- */
  var root = document.createElement('div');
  root.id = 'zhigu-assistant';
  root.innerHTML =
    '<div class="za-wrap">' +
      '<div class="za-bubble">' +
        '<span class="za-close" title="关闭">×</span>' +
        '<div class="za-name">柜柜 · 猫管家</div>' +
        '<div class="za-text"></div>' +
        '<span class="za-tail"></span>' +
      '</div>' +
      '<div class="za-avatar" title="拖我到任意位置，点我说话！">' +
        '<div class="za-ring"></div><div class="za-wave"></div>' +
        '<div class="za-body">' +
          '<svg viewBox="0 0 100 100">' +
            /* 耳朵 */
            '<path d="M26 30 L32 8 L44 24 Z" fill="#f5b942" stroke="#c98a1e" stroke-width="2"/>' +
            '<path d="M74 30 L68 8 L56 24 Z" fill="#f5b942" stroke="#c98a1e" stroke-width="2"/>' +
            '<path d="M30 24 L33 13 L39 22 Z" fill="#ff9ff3"/>' +
            '<path d="M70 24 L67 13 L61 22 Z" fill="#ff9ff3"/>' +
            /* 猫头 */
            '<circle cx="50" cy="55" r="30" fill="#f5b942"/>' +
            '<circle cx="50" cy="55" r="30" fill="url(#zaG)"/>' +
            '<defs><radialGradient id="zaG" cx=".35" cy=".3"><stop offset="0" stop-color="#ffd97a"/><stop offset="1" stop-color="#f0a830"/></radialGradient></defs>' +
            /* 虎纹 */
            '<path d="M38 30 q3 6 0 10 M50 27 q3 7 0 12 M62 30 q-3 6 0 10" fill="none" stroke="#c98a1e" stroke-width="2.4" stroke-linecap="round"/>' +
            /* 眼睛 */
            '<g class="za-eye"><ellipse cx="39" cy="52" rx="4.6" ry="6" fill="#1d1d1f"/>' +
            '<ellipse cx="61" cy="52" rx="4.6" ry="6" fill="#1d1d1f"/>' +
            '<circle cx="40.6" cy="50" r="1.5" fill="#fff"/><circle cx="62.6" cy="50" r="1.5" fill="#fff"/>' +
            '<circle cx="37.5" cy="54.5" r="1" fill="#30d158"/><circle cx="59.5" cy="54.5" r="1" fill="#30d158"/></g>' +
            /* 鼻子 + 嘴巴 */
            '<path d="M47 61 L53 61 L50 65 Z" fill="#ff9ff3"/>' +
            '<g class="za-mouth"><path d="M43 68 Q46.5 72 50 68 Q53.5 72 57 68" fill="none" stroke="#1d1d1f" stroke-width="2.4" stroke-linecap="round"/></g>' +
            /* 胡须 */
            '<path d="M30 60 L14 57 M30 65 L15 66 M70 60 L86 57 M70 65 L85 66" stroke="rgba(255,255,255,.85)" stroke-width="1.8" stroke-linecap="round"/>' +
            /* 领结 */
            '<path d="M42 79 L50 83 L42 87 Z M58 79 L50 83 L58 87 Z" fill="#ff375f"/>' +
            '<circle cx="50" cy="83" r="2.2" fill="#c1123f"/>' +
            /* 尾巴 */
            '<g class="za-tailcat"><path d="M82 76 Q94 70 92 58" fill="none" stroke="#f0a830" stroke-width="5" stroke-linecap="round"/></g>' +
          '</svg>' +
        '</div>' +
        '<span class="za-hide" title="收起">✕</span>' +
      '</div>' +
    '</div>' +
    '<div class="za-fab" title="召唤柜柜">🐱</div>';
  document.body.appendChild(root);

  var bubble = root.querySelector('.za-bubble');
  var bubbleText = root.querySelector('.za-text');
  var avatar = root.querySelector('.za-avatar');

  /* ---------- 气泡：打字机效果 ---------- */
  var typeTimer = null;
  function showBubble(text) {
    bubble.classList.add('show');
    if (typeTimer) clearInterval(typeTimer);
    bubbleText.textContent = '';
    var i = 0;
    typeTimer = setInterval(function () {
      bubbleText.textContent = text.slice(0, ++i);
      if (i >= text.length) clearInterval(typeTimer);
    }, 34);
  }
  root.querySelector('.za-close').onclick = function (e) {
    e.stopPropagation();
    bubble.classList.remove('show');
  };

  /* ---------- 声音： prefer 男声（管家感） ---------- */
  function pickVoice() {
    try {
      var vs = speechSynthesis.getVoices().filter(function (v) { return /zh|Chinese/i.test(v.lang); });
      if (!vs.length) return null;
      // 优先男声（微软中文男声通常叫 Yunyang / Kangkang / Yunxi）
      var male = vs.filter(function (v) { return /Yunyang|Kangkang|Yunxi|Yunjian|male|男/i.test(v.name); });
      return male.length ? male[0] : vs[vs.length - 1];   // 兜底取最后一个，通常和默认女声不同
    } catch (e) { return null; }
  }

  var speaking = false;
  function speak(text) {
    showBubble(text);
    try {
      if (!('speechSynthesis' in window)) return;
      speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN';
      u.rate = 0.95;                      // 语速放慢，管家腔
      u.pitch = 0.75;                     // 音调压低，低沉猫管家
      var v = pickVoice();
      if (v) u.voice = v;
      u.onstart = function () { root.classList.add('talking'); speaking = true; };
      u.onend = u.onerror = function () { root.classList.remove('talking'); speaking = false; };
      speechSynthesis.speak(u);
    } catch (e) { /* 静默降级：只有气泡 */ }
  }
  // 预热声音列表（部分浏览器异步加载）
  if ('speechSynthesis' in window) { speechSynthesis.getVoices(); speechSynthesis.onvoiceschanged = function(){}; }

  /* ---------- 点击 vs 拖拽 ---------- */
  var ptrStart = null, moved = false;
  avatar.addEventListener('pointerdown', function (e) {
    if (e.target === root.querySelector('.za-hide')) return;
    ptrStart = { x: e.clientX, y: e.clientY, l: root.offsetLeft, b: window.innerHeight - root.offsetTop - root.offsetHeight };
    moved = false;
    avatar.setPointerCapture(e.pointerId);
  });
  avatar.addEventListener('pointermove', function (e) {
    if (!ptrStart) return;
    var dx = e.clientX - ptrStart.x, dy = e.clientY - ptrStart.y;
    if (Math.abs(dx) + Math.abs(dy) > 8) moved = true;
    if (moved) {
      root.classList.add('dragging');
      var l = Math.min(Math.max(ptrStart.l + dx, 8), window.innerWidth - root.offsetWidth - 8);
      var b = Math.min(Math.max(ptrStart.b - dy, 8), window.innerHeight - root.offsetHeight - 8);
      root.style.left = l + 'px'; root.style.right = 'auto';
      root.style.bottom = b + 'px';
    }
  });
  avatar.addEventListener('pointerup', function (e) {
    var wasDrag = moved;
    root.classList.remove('dragging');
    ptrStart = null;
    if (wasDrag) return;                  // 拖拽结束不触发说话
    // —— 点击行为 ——
    if (speaking) {
      try { speechSynthesis.cancel(); } catch (err) {}
      root.classList.remove('talking'); speaking = false;
      if (!bubble.classList.contains('show')) showBubble(tips[tipIdx % tips.length]);
      return;
    }
    tipIdx++;
    speak(tips[(tipIdx - 1) % tips.length]);
  });

  /* ---------- 自动巡逻：每 9 秒慢悠悠换个位置 ---------- */
  var wanderOn = true;
  setInterval(function () {
    if (!wanderOn || root.classList.contains('min') || root.classList.contains('dragging') || ptrStart) return;
    var vw = window.innerWidth, vh = window.innerHeight;
    var w = root.offsetWidth || 100, h = root.offsetHeight || 100;
    var left = 20 + Math.random() * Math.max(vw - w - 40, 40);
    var bottom = 20 + Math.random() * Math.max(vh * 0.45, 60);   // 只在下半屏溜达
    root.style.left = left + 'px'; root.style.right = 'auto';
    root.style.bottom = bottom + 'px';
  }, 9000);

  /* ---------- 收起 / 召回 ---------- */
  root.querySelector('.za-hide').onclick = function (e) {
    e.stopPropagation();
    try { speechSynthesis.cancel(); } catch (err) {}
    root.classList.remove('talking');
    root.classList.add('min');
  };
  root.querySelector('.za-fab').onclick = function () {
    root.classList.remove('min');
    showBubble(tips[tipIdx % tips.length]);
  };

  /* ---------- 开场 ---------- */
  setTimeout(function () { showBubble(tips[0]); }, 1200);
  setInterval(function () {
    if (!bubble.classList.contains('show')) return;
    tipIdx++;
    showBubble(tips[tipIdx % tips.length]);
  }, 16000);
})();
